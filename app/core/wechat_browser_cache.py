"""Reuse immutable conversation pages without retaining message bodies.

Code version: v1.0.0-codex.0
"""

from collections import OrderedDict
from concurrent.futures import Future
from copy import deepcopy
import json
import stat
from threading import RLock


MAX_PAGES = 32
MAX_PAGE_BYTES = 1024**2
_LOCK = RLock()
_PAGES = OrderedDict()
_PENDING = {}


def _proof_publications(repository):
    """Pin proof membership and identities, including replacements with equal counts."""
    from .wechat_conversation_events import _directory

    directory = _directory(repository)

    def identity(path):
        value = path.lstat()
        return tuple(
            getattr(value, "st_" + name, 0)
            for name in ("dev", "ino", "mode", "size", "mtime_ns", "ctime_ns", "flags")
        )

    try:
        before = identity(directory)
    except FileNotFoundError:
        return ()
    if not stat.S_ISDIR(before[2]):
        return (before,)
    files = tuple(sorted((path.name, identity(path)) for path in directory.glob("*.json")))
    if identity(directory) != before:
        raise ValueError("Conversation proofs changed during page identity collection.")
    return before, files


def _page_identity(repository, parameters):
    """Include every source receipt and the currently published event counts."""
    from .wechat_conversation_events import _inputs, cache_only_summaries

    try:
        before = _inputs(repository)
        proofs = _proof_publications(repository)
        summaries = cache_only_summaries(repository)
        if _inputs(repository) != before or _proof_publications(repository) != proofs:
            return None
        return (
            str(repository.path.resolve()),
            json.dumps(before, sort_keys=True),
            json.dumps(summaries, sort_keys=True),
            proofs,
            json.dumps(parameters, sort_keys=True),
        )
    except (OSError, ValueError, TypeError):
        return None


def conversation_page(repository, parameters, load):
    """Share one cold page and invalidate it whenever a pinned input changes."""
    if parameters["session"] or not 1 <= parameters["page_size"] <= 1000:
        return load()
    key = _page_identity(repository, parameters)
    if key is None:
        return load()
    with _LOCK:
        cached = _PAGES.get(key)
        if cached is not None:
            _PAGES.move_to_end(key)
        pending = _PENDING.get(key)
        owner = pending is None and cached is None
        if owner:
            pending = Future()
            _PENDING[key] = pending
    if cached is not None:
        return deepcopy(cached) if _page_identity(repository, parameters) == key else load()
    if not owner:
        value = pending.result()
        return deepcopy(value) if _page_identity(repository, parameters) == key else load()
    try:
        value = load()
        if (
            value["resource_index_status"] in {"ready", "absent"}
            and len(json.dumps(value).encode()) <= MAX_PAGE_BYTES
            and _page_identity(repository, parameters) == key
        ):
            with _LOCK:
                _PAGES[key] = deepcopy(value)
                _PAGES.move_to_end(key)
                while len(_PAGES) > MAX_PAGES:
                    _PAGES.popitem(last=False)
        pending.set_result(deepcopy(value))
        return value
    except BaseException as error:
        pending.set_exception(error)
        raise
    finally:
        with _LOCK:
            _PENDING.pop(key, None)
