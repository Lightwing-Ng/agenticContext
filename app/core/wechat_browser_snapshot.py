"""Persist authenticated raw conversation summaries after full timeline validation.

Code version: v1.0.2-codex.0
"""

from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future
from contextlib import closing
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import tempfile
from threading import RLock

from .owner_only_files import (
    ensure_owner_only,
    ensure_owner_only_directory,
    owner_only_directory_problem,
    owner_only_problem,
)
from .windows_file_metadata import file_change_time


VERSION = "v1.0.0-codex.0"
MAX_ROWS = 100_000
MAX_SNAPSHOT_BYTES = 32 * 1024**2
MAX_CODE_BYTES = 32 * 1024**2
MAX_MEMORY_SNAPSHOTS = 2
_TABLE = "browser_snapshot_conversations"
_COLUMNS = (
    "id", "partition_id", "account_key", "peer_id", "title", "kind",
    "message_count", "first_sort", "last_sort", "first_time", "last_time",
    "latest_text",
)
_SQL_TABLE = re.compile(r"(?:(?:main|temp)\.)?[A-Za-z_][A-Za-z0-9_]*")
_MAGIC = b"wechat-browser-snapshot-v1\n"
_LOCK = RLock()
_SNAPSHOTS = OrderedDict()
_PENDING = {}
_CODE_DIGEST = None


class _CacheUnavailable(ValueError):
    """An optional cache cannot be used under its private-file contract."""


def _json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _state(value, *, change_field="ctime_ns"):
    return tuple(
        getattr(value, "st_" + name, 0)
        for name in ("dev", "ino", "mode", "nlink", "uid", "size",
                     "mtime_ns", change_field, "flags")
    )


def _owned(value):
    return os.name == "nt" or value.st_uid == os.getuid()


def _private_file(path, value):
    return (
        stat.S_ISREG(value.st_mode)
        and value.st_nlink == 1
        and _owned(value)
        and (
            not owner_only_problem(path)
            if os.name == "nt"
            else stat.S_IMODE(value.st_mode) == 0o600
        )
        and not getattr(value, "st_flags", 0) & 0x40000000
    )


def _cache_file_state(path, *, descriptor=None):
    value = path.lstat() if descriptor is None else os.fstat(descriptor)
    if not _private_file(path, value):
        raise _CacheUnavailable("The browser cache file must be owner-private.")
    if os.name != "nt":
        return _state(value)
    # Windows path stat and descriptor stat expose different ctime values.
    if not hasattr(value, "st_birthtime_ns"):
        raise _CacheUnavailable("Windows browser caches require creation metadata.")
    return (
        *_state(value, change_field="birthtime_ns"),
        file_change_time(path, descriptor=descriptor),
    )


def _read_private(path, limit):
    flags = (
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    identity = _cache_file_state(path)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        before = os.fstat(stream.fileno())
        if (
            _cache_file_state(path, descriptor=stream.fileno()) != identity
            or _cache_file_state(path) != identity
            or before.st_size > limit
        ):
            raise _CacheUnavailable("The browser cache file is not private and bounded.")
        data = stream.read(limit + 1)
        if (
            len(data) != before.st_size
            or _cache_file_state(path, descriptor=stream.fileno()) != identity
            or _cache_file_state(path) != identity
        ):
            raise _CacheUnavailable("The browser cache changed during reading.")
    return data, identity


def _directory_identity(directory):
    value = directory.lstat()
    if (
        not stat.S_ISDIR(value.st_mode)
        or not _owned(value)
        or (
            owner_only_directory_problem(directory)
            if os.name == "nt"
            else stat.S_IMODE(value.st_mode) != 0o700
        )
    ):
        raise _CacheUnavailable("The browser cache directory must be owner-private.")
    return _state(value)


def _read_signing_key(path):
    key, identity = _read_private(path, 32)
    if len(key) != 32:
        raise _CacheUnavailable("The browser cache signing key has an invalid length.")
    return key, identity


def _storage(repository):
    return _cache_storage(repository.path.parent / "browser_cache")


def _cache_storage(directory):
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    else:
        if os.name == "nt":
            ensure_owner_only_directory(directory)
    _directory_identity(directory)
    path = directory / "signing.key"
    flags = (
        os.O_WRONLY | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, "wb") as stream:
            if os.name == "nt":
                ensure_owner_only(path)
            stream.write(secrets.token_bytes(32))
            stream.flush()
            os.fsync(stream.fileno())
    key, identity = _read_signing_key(path)
    return directory, key, identity


def _code_files():
    return sorted(Path(__file__).parent.glob("wechat*.py"))


def _code_fingerprint():
    global _CODE_DIGEST
    paths = _code_files()
    identities = tuple((path.name, _state(path.lstat())) for path in paths)
    with _LOCK:
        if _CODE_DIGEST is not None and _CODE_DIGEST[0] == identities:
            return _CODE_DIGEST[1]
    digest = hashlib.sha256()
    total = 0
    for path in paths:
        with path.open("rb") as stream:
            data = stream.read(MAX_CODE_BYTES - total + 1)
        total += len(data)
        if total > MAX_CODE_BYTES:
            raise _CacheUnavailable("WeChat reader code exceeds its fingerprint budget.")
        digest.update(path.name.encode("utf-8") + b"\0" + data + b"\0")
    if tuple((path.name, _state(path.lstat())) for path in _code_files()) != identities:
        raise _CacheUnavailable("WeChat reader code changed during fingerprinting.")
    value = digest.hexdigest()
    with _LOCK:
        _CODE_DIGEST = identities, value
    return value


def _pin(repository, history_identity):
    from .wechat_conversation_events import _inputs

    return {
        "version": VERSION,
        "inputs": _inputs(repository, history_identity),
        "code": _code_fingerprint(),
    }


def _valid_row(row):
    return (
        isinstance(row, list)
        and len(row) == len(_COLUMNS)
        and all(isinstance(row[index], str) for index in (0, 1, 2, 4, 5, 9, 10, 11))
        and (row[3] is None or isinstance(row[3], str))
        and type(row[6]) is int and 0 <= row[6] <= 2**63 - 1
        and all(
            type(row[index]) in (int, float) and math.isfinite(row[index])
            for index in (7, 8)
        )
    )


def _valid_payload(value, pin):
    if (
        not isinstance(value, dict)
        or set(value) != {"pin", "rows", "resource_status", "resource_index_accounts"}
        or value["pin"] != pin
        or value["resource_status"] not in {"ready", "absent"}
        or not isinstance(value["rows"], list)
        or len(value["rows"]) > MAX_ROWS
    ):
        return False
    accounts = value["resource_index_accounts"]
    if accounts is not None and (
        not isinstance(accounts, list)
        or any(not isinstance(account, str) for account in accounts)
    ):
        return False
    seen = set()
    for row in value["rows"]:
        if not _valid_row(row) or row[0] in seen:
            return False
        seen.add(row[0])
    return True


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise _CacheUnavailable("The browser snapshot has duplicate object fields.")
        value[key] = item
    return value


def _decode_snapshot(encoded, key):
    if not encoded.startswith(_MAGIC):
        return None
    signature, separator, payload = encoded[len(_MAGIC):].partition(b"\n")
    expected = hmac.new(key, payload, hashlib.sha256).hexdigest().encode("ascii")
    if not separator or not hmac.compare_digest(signature, expected):
        return None
    return json.loads(payload, object_pairs_hook=_unique)


def _read_snapshot(directory, key, pin):
    try:
        encoded, _identity = _read_private(directory / "conversations.snapshot", MAX_SNAPSHOT_BYTES)
    except FileNotFoundError:
        return None
    try:
        value = _decode_snapshot(encoded, key)
        return value if _valid_payload(value, pin) else None
    except (ValueError, TypeError, KeyError, RecursionError):
        return None


def _write_snapshot(directory, key, payload, unchanged):
    encoded = _json(payload)
    signature = hmac.new(key, encoded, hashlib.sha256).hexdigest().encode("ascii")
    encoded = _MAGIC + signature + b"\n" + encoded
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        return False
    return _write_private_cache(
        directory / "conversations.snapshot", encoded, unchanged, prefix=".conversation-"
    )


def _write_private_cache(target, encoded, unchanged, *, prefix):
    try:
        _cache_file_state(target)
    except FileNotFoundError:
        pass
    descriptor, name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            if os.name == "nt":
                ensure_owner_only(temporary)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if not unchanged():
            return False
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def _build(repository, history_identity, pin):
    from .wechat_archive import WechatTimelineProjectionError

    with closing(sqlite3.connect(
        repository.path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
    )) as connection:
        connection.row_factory = sqlite3.Row
        try:
            table, _messages, status = repository._timeline_tables(
                connection, history_identity, strict=True
            )
        except WechatTimelineProjectionError:
            return None
        if status not in {"ready", "absent"} or not _SQL_TABLE.fullmatch(table):
            return None
        columns = ",".join('"' + column + '"' for column in _COLUMNS)
        rows, byte_count = [], 0
        for row in connection.execute(
            "SELECT " + columns + " FROM " + table + " LIMIT ?", (MAX_ROWS + 1,)
        ):
            row = list(row)
            if not _valid_row(row):
                return None
            byte_count += len(_json(row))
            if len(rows) >= MAX_ROWS or byte_count > MAX_SNAPSHOT_BYTES:
                return None
            rows.append(row)
        accounts = repository._resource_index_accounts(connection) if status == "ready" else None
    payload = {
        "pin": pin,
        "rows": rows,
        "resource_status": status,
        "resource_index_accounts": list(accounts) if accounts is not None else None,
    }
    if not _valid_payload(payload, pin) or len(_json(payload)) + len(_MAGIC) + 65 > MAX_SNAPSHOT_BYTES:
        return None
    return payload


def _unchanged(repository, history_identity, pin, directory, key_identity):
    try:
        return (
            _pin(repository, history_identity) == pin
            and _cache_file_state(directory / "signing.key") == key_identity
        )
    except (OSError, ValueError, TypeError):
        return False


def _install(connection, payload):
    connection.execute("DROP TABLE IF EXISTS temp." + _TABLE)
    connection.execute(
        "CREATE TEMP TABLE " + _TABLE + " AS SELECT * FROM main.conversations WHERE 0"
    )
    columns = ",".join('"' + column + '"' for column in _COLUMNS)
    connection.executemany(
        "INSERT INTO temp." + _TABLE + "(" + columns + ") VALUES("
        + ",".join("?" for _ in _COLUMNS) + ")",
        payload["rows"],
    )
    connection.execute("CREATE UNIQUE INDEX temp.browser_snapshot_id ON " + _TABLE + "(id)")
    accounts = payload["resource_index_accounts"]
    return _TABLE, payload["resource_status"], tuple(accounts) if accounts is not None else None


def load_conversation_snapshot(repository, connection, history_identity):
    """Reuse authenticated raw summaries; an unavailable optional cache is a miss."""
    try:
        pin = _pin(repository, history_identity)
        directory, key, key_identity = _storage(repository)
        cache_key = str(repository.path.resolve()), _json(pin), hashlib.sha256(key).digest()
    except (OSError, ValueError, TypeError):
        return None
    with _LOCK:
        payload = _SNAPSHOTS.get(cache_key)
        if payload is not None:
            _SNAPSHOTS.move_to_end(cache_key)
        pending = _PENDING.get(cache_key)
        owner = payload is None and pending is None
        if owner:
            pending = Future()
            _PENDING[cache_key] = pending
    if payload is None and not owner:
        payload = pending.result()
    if owner:
        try:
            try:
                payload = _read_snapshot(directory, key, pin)
            except (OSError, _CacheUnavailable):
                payload = None
            if payload is None:
                payload = _build(repository, history_identity, pin)
                if payload is not None and _unchanged(
                    repository, history_identity, pin, directory, key_identity
                ):
                    try:
                        _write_snapshot(
                            directory, key, payload,
                            lambda: _unchanged(
                                repository, history_identity, pin, directory,
                                key_identity,
                            ),
                        )
                    except (OSError, _CacheUnavailable):
                        pass
            if payload is not None and not _unchanged(
                repository, history_identity, pin, directory, key_identity
            ):
                payload = None
            if payload is not None:
                with _LOCK:
                    _SNAPSHOTS[cache_key] = payload
                    _SNAPSHOTS.move_to_end(cache_key)
                    while len(_SNAPSHOTS) > MAX_MEMORY_SNAPSHOTS:
                        _SNAPSHOTS.popitem(last=False)
            pending.set_result(payload)
        except (OSError, _CacheUnavailable):
            pending.set_result(None)
            payload = None
        except BaseException as error:
            pending.set_exception(error)
            raise
        finally:
            with _LOCK:
                _PENDING.pop(cache_key, None)
    try:
        if payload is None or not _unchanged(
            repository, history_identity, pin, directory, key_identity
        ):
            return None
    except (OSError, ValueError, TypeError):
        return None
    result = _install(connection, payload)
    try:
        if not _unchanged(repository, history_identity, pin, directory, key_identity):
            connection.execute("DROP TABLE temp." + _TABLE)
            return None
    except (OSError, ValueError, TypeError):
        connection.execute("DROP TABLE temp." + _TABLE)
        return None
    return result


def _completion_identity(value):
    return (
        isinstance(value, dict)
        and value.keys() == {"dev", "ino", "size", "mtime_ns", "ctime_ns"}
        and all(type(field) is int for field in value.values())
        and value["size"] > 0
    )


def _preview_pin_matches(previous, current, completion_path):
    if (
        not isinstance(previous, dict)
        or previous.keys() != current.keys()
        or previous["version"] != current["version"]
        or not isinstance(previous["code"], str)
        or not previous["code"]
    ):
        return False
    old_inputs = previous["inputs"]
    new_inputs = current["inputs"]
    if old_inputs == new_inputs:
        return previous["code"] != current["code"]
    return (
        isinstance(old_inputs, dict)
        and old_inputs.keys() | {completion_path} == new_inputs.keys()
        and (completion_path not in old_inputs or _completion_identity(old_inputs[completion_path]))
        and _completion_identity(new_inputs.get(completion_path))
        and old_inputs.get(completion_path) != new_inputs[completion_path]
        and all(
            identity == new_inputs[path]
            for path, identity in old_inputs.items()
            if path != completion_path
        )
    )


def load_previous_conversation_snapshot(repository, connection, history_identity):
    """Install a verified preview after a reader or secondary completion change."""
    from .wechat_secondary_completions import FILENAME

    directory = repository.path.parent / "browser_cache"
    key_path = directory / "signing.key"
    snapshot_path = directory / "conversations.snapshot"
    completion_path = str(repository.path.with_name(FILENAME).absolute())
    try:
        pin = _pin(repository, history_identity)
        directory_identity = _directory_identity(directory)
        key, key_identity = _read_signing_key(key_path)
        encoded, snapshot_identity = _read_private(snapshot_path, MAX_SNAPSHOT_BYTES)
        payload = _decode_snapshot(encoded, key)
        previous = payload.get("pin") if isinstance(payload, dict) else None
        if not _preview_pin_matches(previous, pin, completion_path) or not _valid_payload(
            payload, previous
        ):
            return None
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return None

    def unchanged():
        try:
            return (
                _pin(repository, history_identity) == pin
                and _directory_identity(directory) == directory_identity
                and _cache_file_state(key_path) == key_identity
                and _cache_file_state(snapshot_path) == snapshot_identity
            )
        except (OSError, ValueError, TypeError):
            return False

    if not unchanged():
        return None
    result = _install(connection, payload)
    if not unchanged():
        connection.execute("DROP TABLE temp." + _TABLE)
        return None
    return result
