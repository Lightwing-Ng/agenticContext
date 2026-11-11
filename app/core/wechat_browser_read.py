"""Build exact, source-pinned browser pages outside document requests.

Code version: v1.0.1-codex.0
"""

from collections import OrderedDict, deque
from contextlib import closing
from dataclasses import fields, is_dataclass
import json
from itertools import islice
from pathlib import Path
import secrets
import sqlite3
import stat
import sys
from threading import Condition, Event, Thread
import time


MAX_TASKS = 16
MAX_RESULT_BYTES = 64 * 1024**2
MAX_PIN_FILES = 8192
READY_GRACE_SECONDS = 0.05
RESULT_SECONDS = 300
_GENERATED = {"browser_cache", "conversation_events", "__pycache__"}
_RESOURCE_INDEX_WARNING = "Media and file references need refreshing after the archive changed."


class _UnverifiedResourceIndex(ValueError):
    """Identify an explicitly rejected resource index without exposing source details."""


def _state(path):
    try:
        value = path.lstat()
    except FileNotFoundError:
        return None
    return tuple(getattr(value, "st_" + key, 0) for key in (
        "dev", "ino", "mode", "size", "mtime_ns", "ctime_ns", "flags"
    ))


def _result_size(value):
    pending, seen, total = [value], set(), 0
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        total += sys.getsizeof(item)
        if total > MAX_RESULT_BYTES or len(seen) > 500_000:
            raise ValueError("The saved view exceeds its bounded presentation size.")
        if is_dataclass(item) and not isinstance(item, type):
            pending.extend(getattr(item, field.name) for field in fields(item))
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (tuple, list, set, frozenset)):
            pending.extend(item)
    return total


class WechatBrowserReadService:
    """Retain one-use verified results; status never opens a database or waits."""

    def __init__(self, root, *, loader=None, pin=None, validator=None):
        self.root = Path(root)
        base = self.root / "messaging" / "wechat"
        self._history_required = any((base / name).exists() for name in (
            "history.sqlite3", "contacts.sqlite3", "resource_references.sqlite3",
            "resource_revalidated.sqlite3", "macos_recovery.sqlite3",
        ))
        self.loader = loader
        self._pin_override = pin
        self.validator = validator or self._validate
        self._condition = Condition()
        self._tasks = OrderedDict()
        self._keys = {}
        self._queue = deque()
        self._thread = None
        self._closing = False
        self._result_bytes = 0

    def _pin(self):
        if self._pin_override is not None:
            return self._pin_override()
        from .wechat_archive import WechatArchiveRepository
        from .wechat_browser_snapshot import _code_fingerprint
        from .wechat_conversation_events import _inputs

        repository = WechatArchiveRepository(self.root)
        if repository.path.exists():
            self._history_required = True
        elif self._history_required:
            raise ValueError("The saved archive is unavailable.")
        def authority_inputs():
            try:
                return _inputs(repository) if repository.path.exists() else {}
            except FileNotFoundError as error:
                contacts = repository.path.with_name("contacts.sqlite3")
                if error.filename is None or Path(error.filename).absolute() != contacts.absolute():
                    raise
                if contacts.is_symlink():
                    raise ValueError("The saved identity context cannot be a symbolic link.") from None
                from .wechat_reviewed_quote_events import reviewed_quotes_digest

                # Missing optional identity context never creates inferred identities.
                return {str(contacts): None, "reviewed_quotes": reviewed_quotes_digest(self.root)}

        inputs = authority_inputs()
        managed = []
        base = repository.path.parent
        entries_seen = 0

        def inventory(directory):
            nonlocal entries_seen
            before = _state(directory)
            for path in sorted(islice(directory.iterdir(), MAX_PIN_FILES + 1)):
                entries_seen += 1
                if entries_seen > MAX_PIN_FILES:
                    raise ValueError("The saved view exceeds its bounded input inventory.")
                if path.name.startswith(".") or path.name in _GENERATED:
                    continue
                state = _state(path)
                if state is None:
                    raise ValueError("Saved-view inputs changed during inventory.")
                if stat.S_ISDIR(state[2]):
                    inventory(path)
                else:
                    managed.append((str(path.relative_to(base)), state))
                    if len(managed) > MAX_PIN_FILES:
                        raise ValueError("The saved view exceeds its bounded input inventory.")
            # Directory metadata checks collection coherence, not cross-query freshness.
            if _state(directory) != before:
                raise ValueError("Saved-view input membership changed during inventory.")

        if repository.path.exists() and base.exists():
            inventory(base)
        # Presentation registries and their managed evidence stay source-pinned too.
        extra_code = (
            Path(__file__).with_name("chat_history_browser.py"),
            Path(__file__).parents[1] / "web" / "presentation.py",
        )
        code = [(str(path), _state(path)) for path in extra_code]
        fingerprint = _code_fingerprint()
        if authority_inputs() != inputs or any(_state(base / name) != state for name, state in managed):
            raise ValueError("Saved-view inputs changed during identity collection.")
        return json.dumps((inputs, managed, fingerprint, code), sort_keys=True)

    def _proofs(self):
        if self._pin_override is not None:
            return ()
        from .wechat_archive import WechatArchiveRepository
        directory = WechatArchiveRepository(self.root).path.with_name("conversation_events")
        before = _state(directory)
        if before is None:
            return ()
        if not stat.S_ISDIR(before[2]) or directory.is_symlink():
            raise ValueError("Saved-view proofs must remain a local directory.")
        files = []
        for path in directory.glob("*.json"):
            if len(files) >= MAX_PIN_FILES:
                raise ValueError("The saved view exceeds its bounded proof inventory.")
            files.append((path.name, _state(path)))
        if _state(directory) != before:
            raise ValueError("Saved-view proofs changed during checking.")
        return before, tuple(sorted(files))

    def _validate(self, parameters):
        from .wechat_archive import WechatArchiveRepository, _file_identity

        repository = WechatArchiveRepository(self.root)
        if not repository.path.exists():
            return
        with closing(sqlite3.connect(
            repository.path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
        )) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA temp_store=MEMORY")
            identity = _file_identity(repository.path)
            summaries = None
            if not parameters.get("session") and not str(parameters.get("query", "")).strip():
                from .wechat_browser_snapshot import load_conversation_snapshot

                summaries = load_conversation_snapshot(repository, connection, identity)
            if summaries is None:
                repository._timeline_tables(
                    connection, identity,
                    conversation_id=parameters.get("session") or None, strict=True,
                )
        contacts = repository.path.with_name("contacts.sqlite3")
        if contacts.exists():
            with closing(sqlite3.connect(
                contacts.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
            )) as connection:
                connection.execute("PRAGMA query_only=ON")
                if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise ValueError("The saved identity context is not verified.")
                schema = connection.execute(
                    "SELECT value FROM contact_registry_info WHERE key='schema_version'"
                ).fetchone()
                if schema is None or schema[0] not in {"1", "2"}:
                    raise ValueError("The saved identity context has an unsupported schema.")

    @staticmethod
    def _public(task):
        return {key: task[key] for key in (
            "task_id", "generation", "state", "message"
        )}

    def _remove_key(self, task):
        if self._keys.get(task["key"]) == task["task_id"]:
            self._keys.pop(task["key"], None)

    def _expire_result(self, task, message):
        self._discard(task)
        self._remove_key(task)
        task.update(state="failed", message=message)

    def acquire(self, parameters, *, wait=True, _restart=True, _consume=True):
        """Return an exact ready result once, or a bounded nonblocking job handle."""
        parameters = dict(parameters)
        pin = self._pin()
        key = (pin, json.dumps(parameters, sort_keys=True))
        with self._condition:
            if self._closing:
                raise RuntimeError("The saved-view reader is shutting down.")
            token = self._keys.get(key)
            task = self._tasks.get(token)
            if task is not None and task["state"] == "succeeded" and task.get("result") is None:
                self._remove_key(task)
                task = None
            if task is not None and task.get("result") is not None:
                if time.monotonic() - task["finished"] > RESULT_SECONDS:
                    self._expire_result(task, "This saved view expired. Try loading it again.")
                    task = None
            if task is None:
                for previous, value in list(self._tasks.items()):
                    if len(self._tasks) < MAX_TASKS:
                        break
                    if value["state"] != "running":
                        self._discard(value)
                        self._tasks.pop(previous)
                        self._remove_key(value)
                if len(self._tasks) >= MAX_TASKS:
                    raise RuntimeError("Several saved views are being checked. Try again shortly.")
                token = secrets.token_urlsafe(24)
                task = {
                    "task_id": token, "generation": 1, "state": "running",
                    "message": "Checking saved records before displaying this view.",
                    "parameters": parameters, "pin": pin, "key": key,
                    "done": Event(), "result": None, "size": 0,
                }
                self._tasks[token] = task
                self._keys[key] = token
                self._queue.append(token)
                if self._thread is None or not self._thread.is_alive():
                    self._thread = Thread(target=self._run, daemon=True, name="wechat-browser-read")
                    self._thread.start()
                self._condition.notify_all()
        if wait:
            task["done"].wait(READY_GRACE_SECONDS)
        if not _consume:
            with self._condition:
                return None, self._public(task)
        with self._condition:
            candidate = task.get("result")
        current_proofs = self._proofs() if candidate is not None else None
        current_pin = self._pin() if candidate is not None else None
        drift = False
        with self._condition:
            result = task.get("result")
            if result is not candidate:
                result = None
            if result is not None:
                if current_pin != task["pin"] or current_proofs != task["proofs"]:
                    self._discard(task)
                    task.update(state="failed", message="Saved records changed. Try loading this view again.")
                    self._remove_key(task)
                    result = None
                    drift = True
                else:
                    # Transfer ownership; copying would destroy runtime authority sentinels.
                    ready_public = self._public(task)
                    self._expire_result(task, "This saved view has already been opened. Try loading it again.")
            public = self._public(task)
            if result is not None:
                public = ready_public
        if drift and _restart:
            return self.acquire(parameters, wait=False, _restart=False, _consume=_consume)
        return result, public

    def status(self, token):
        with self._condition:
            task = self._tasks.get(token)
            if task is not None and task.get("result") is not None and time.monotonic() - task["finished"] > RESULT_SECONDS:
                self._expire_result(task, "This saved view expired. Try loading it again.")
            return self._public(task) if task is not None else None

    def retry(self, token):
        with self._condition:
            task = self._tasks.get(token)
            if task is None:
                raise LookupError("This saved-view task has expired.")
            if task["state"] == "running":
                return self._public(task)
            parameters = dict(task["parameters"])
            self._discard(task)
            self._remove_key(task)
        _page, public = self.acquire(parameters, wait=False, _consume=False)
        return public

    def _discard(self, task):
        self._result_bytes -= task.get("size", 0)
        task["result"], task["size"] = None, 0

    def _run(self):
        while True:
            with self._condition:
                while not self._queue and not self._closing:
                    self._condition.wait()
                if self._closing:
                    return
                task = self._tasks[self._queue.popleft()]
            try:
                if self._pin() != task["pin"]:
                    raise ValueError("Saved-view inputs changed before checking.")
                self.validator(task["parameters"])
                with self._condition:
                    if self._closing:
                        raise RuntimeError("The saved-view reader is shutting down.")
                if self.loader is None:
                    from .chat_history_browser import query_chat_history

                    page = query_chat_history(self.root, source="wechat", **task["parameters"])
                else:
                    page = self.loader(task["parameters"])
                metadata = page.messaging or {}
                if metadata.get("resource_index_status") in {"stale", "invalid"}:
                    raise _UnverifiedResourceIndex
                if metadata.get("resource_index_status") not in {"ready", "absent"}:
                    raise ValueError("The saved timeline is not verified.")
                projection = metadata.get("event_projection", {})
                if projection.get("status") in {"invalid", "stale"}:
                    raise ValueError("The saved message projection is not verified.")
                size = _result_size(page)
                proofs = self._proofs()
                if self._pin() != task["pin"]:
                    raise ValueError("Saved-view inputs changed while checking.")
                with self._condition:
                    if self._closing:
                        raise RuntimeError("The saved-view reader is shutting down.")
                    for previous in self._tasks.values():
                        if self._result_bytes + size <= MAX_RESULT_BYTES:
                            break
                        if previous is not task and previous.get("result") is not None:
                            self._expire_result(previous, "This saved view expired. Try loading it again.")
                    self._result_bytes += size
                    task.update(result=page, size=size, proofs=proofs, finished=time.monotonic(),
                                state="succeeded", message="Saved records are ready.")
            except _UnverifiedResourceIndex:
                with self._condition:
                    task.update(state="failed", message=_RESOURCE_INDEX_WARNING)
            except Exception:
                with self._condition:
                    task.update(state="failed", message="Saved records could not be checked. Try loading this view again.")
            finally:
                task["done"].set()

    def shutdown(self, *, wait=True, timeout=0.5):
        with self._condition:
            self._closing = True
            for token in self._queue:
                task = self._tasks[token]
                task.update(state="failed", message="The saved-view reader is shutting down.")
                task["done"].set()
            self._queue.clear()
            self._condition.notify_all()
            thread = self._thread
        if wait and thread is not None:
            thread.join(min(0.5, max(0, timeout)))
