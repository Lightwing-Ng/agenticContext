"""Read configured Mac WeChat sources into authenticated memory-only snapshots.

Code version: v1.3.1-codex.0
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

from . import wechat_mac_refresh_cipher as cipher
from .compute_resources import discover_compute_resources
from .owner_only_files import owner_only_problem
from .wechat_mac_refresh_status import STORAGE_FAILURE_MESSAGE


MAX_DATABASE_BYTES = 2 * 1024**3
LEGACY_DATABASE_BYTES = 1024**3
MAX_SHM_BYTES = 16 * 1024**2
SNAPSHOT_MEMORY_OVERHEAD = 512 * 1024**2
MAX_VAULT_BYTES = 1024**2
MAX_KEY_FILES = 32
MAX_KEY_RECORDS = 512
MAX_NAMES = 1_000_000
_DATABASE = re.compile(r"db_storage/message/message_[0-9]+\.db")
_DIRECTORY = re.compile(r"[A-Za-z0-9_.-]{1,256}")
_HEX_KEY = re.compile(r"[0-9a-fA-F]{64}")
_GROUP_SUFFIXES = ("", "-wal", "-shm", "-journal")


class RefreshError(ValueError):
    """A source cannot be refreshed faithfully within the supported boundary."""


class RefreshIssue(StrEnum):
    """Fixed, public reasons for a refresh that cannot proceed."""

    INVALID_CONVERSATION = "Open a verified private or group conversation to refresh it."
    UNSUPPORTED_HOST = "Refreshing from the local WeChat database requires macOS."
    CONFIGURATION_UNAVAILABLE = (
        "No usable local message database configuration was found for this WeChat account. "
        "Check that the verified key configuration and its original WeChat database files are available."
    )
    BUSY = "Another conversation is refreshing. Wait for it to finish, then try again."
    ACCOUNT_UNAVAILABLE = "This account cannot be refreshed on this Mac."
    ACCOUNT_SOURCES_SKIPPED = "No configured Mac WeChat source could be read for this account."
    ACCOUNT_SOURCE_CHANGED = (
        "The live Mac WeChat database changed while it was being read. Try again in a moment."
    )
    INTERRUPTED = "The local refresh was interrupted before it could finish. Try again."
    WORKER_START_FAILED = "The local refresh worker could not be started. Try again shortly."
    WORKER_UNAVAILABLE = "The local refresh worker stopped before returning a result. Try again."
    WORKER_TIMEOUT = "The local refresh worker reached its time limit. Try again."
    INTERPRETER_UNAVAILABLE = "A compatible local WeChat refresh interpreter is unavailable."
    MESSAGE_BUDGET = "This conversation's new records exceed the per-refresh message size limit."
    DISK_SPACE = STORAGE_FAILURE_MESSAGE
    ARCHIVE_CHANGED = "The WeChat archive changed during refresh. Try again after it finishes updating."
    VIEW_UNAVAILABLE = "The refreshed conversation is temporarily unavailable. Reopen it and try again."


class RefreshNotice(RefreshError):
    """An expected refresh failure with a fixed message safe to render."""

    def __init__(self, issue: RefreshIssue):
        self.issue = issue
        super().__init__(issue.value)


class SourceSnapshotChanged(RefreshError):
    """An encrypted source changed while its live snapshot was being collected."""


class SourceAccountBindingUnavailable(RefreshError):
    """An authenticated shard lacks independently revalidated account witnesses."""


@dataclass(frozen=True)
class KeyReference:
    account_id: str
    account_directory: str
    relative_path: str
    database_path: Path = field(repr=False)
    key: bytes | None = field(default=None, repr=False)


@dataclass
class AuthenticatedDatabase:
    connection: sqlite3.Connection
    source: dict
    account_directory: str
    partial: bool
    wal_tail_status: str = "unknown"

    def close(self):
        self.connection.close()


def _admit_snapshot(group):
    """Bound large snapshots before allocation using conservative physical capacity."""
    main_bytes = group[""][2]
    wal_bytes = group["-wal"][2] if group["-wal"] else 0
    shm_bytes = group["-shm"][2] if group["-shm"] else 0
    if (
        main_bytes > MAX_DATABASE_BYTES
        or wal_bytes > cipher.MAX_WAL_BYTES
        or shm_bytes > MAX_SHM_BYTES
    ):
        raise RefreshError("The source group exceeds its bounded snapshot size.")
    # A committed WAL can grow the output, but every new page needs WAL bytes.
    output_bytes = min(MAX_DATABASE_BYTES, main_bytes + wal_bytes)
    if output_bytes <= LEGACY_DATABASE_BYTES:
        return
    estimated_bytes = (
        4 * output_bytes
        + 2 * wal_bytes
        + shm_bytes
        + SNAPSHOT_MEMORY_OVERHEAD
    )
    try:
        memory_bytes = discover_compute_resources().memory_bytes
    except (AttributeError, OSError, TypeError, ValueError):
        raise RefreshError(
            "Physical memory is unavailable for large-source admission."
        ) from None
    if type(memory_bytes) is not int or memory_bytes <= 0:
        raise RefreshError(
            "Physical memory is unavailable for large-source admission."
        )
    if estimated_bytes > memory_bytes // 4:
        raise RefreshError(
            "The large source exceeds the conservative snapshot memory budget."
        )


def _source_receipt(reference, main_sha, wal_sha, crypto):
    """Keep physical and authenticated content-page counts distinct in source proof."""
    source = {
        "source_id": "macos-refresh-"
        + hashlib.sha256(
            (reference.account_id + "\0" + reference.relative_path).encode()
        ).hexdigest()[:32],
        "snapshot_id": "committed-"
        + hashlib.sha256((main_sha + wal_sha).encode()).hexdigest(),
        "main_sha256": main_sha,
        "wal_sha256": wal_sha,
        "database_relative_path": reference.relative_path,
        "integrity_check": "ok",
        "authenticated_pages": crypto["encrypted_pages_authenticated"],
        "page_count": crypto["output_page_count"],
        "authenticated_page_count": crypto["authenticated_output_page_count"],
    }
    if crypto["lock_byte_page_count"]:
        source.update(
            lock_byte_page_count=crypto["lock_byte_page_count"],
            authenticated_content_pages=crypto["content_pages_authenticated"],
        )
    return source


def _checked_path(path):
    path = Path(path)
    if not path.is_absolute():
        raise RefreshError("A configured source path must be absolute.")
    for component in (path, *path.parents):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_flags", 0) & 0x40000000:
            raise RefreshError(
                "A configured source must be resident and cannot use symlinks."
            )
    return path


def identity(path, *, optional=False):
    try:
        info = Path(path).lstat()
    except FileNotFoundError:
        if optional:
            return None
        raise
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or getattr(info, "st_flags", 0) & 0x40000000
    ):
        raise RefreshError(
            "A source must be a resident regular file without hard links."
        )
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_stable(path, maximum, *, private=False):
    path = _checked_path(path)
    before = identity(path)
    if before[2] > maximum:
        if not private:
            raise SourceSnapshotChanged("A source changed before it could be read.")
        raise RefreshError("The configured key file must be bounded and owner-only.")
    if private and owner_only_problem(path):
        raise RefreshError("The configured key file must be bounded and owner-only.")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != before[:2]:
            raise SourceSnapshotChanged("A source changed before it could be read.")
        raw = stream.read(maximum + 1)
    if private and owner_only_problem(path):
        raise RefreshError("A source changed while it was being read.")
    if len(raw) > maximum or identity(path) != before:
        raise SourceSnapshotChanged("A source changed while it was being read.")
    return raw


class KeyRegistry:
    """Keep key discovery local, bounded and separate from browser-visible state."""

    def __init__(self, *, key_root=None, native_root=None):
        home = Path.home()
        self.key_root = (
            Path(key_root)
            if key_root is not None
            else (
                home / "Library/Application Support/agenticContext/WeChat Recovery/Keys"
            )
        )
        self.native_root = (
            Path(native_root)
            if native_root is not None
            else (
                home
                / "Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
            )
        )

    def references(self, account_id, *, load_keys=False):
        if not isinstance(account_id, str) or _DIRECTORY.fullmatch(account_id) is None:
            return []
        if not self.key_root.is_dir():
            return []
        _checked_path(self.key_root)
        files = sorted(self.key_root.glob("macos-*/verified-db-keys*.jsonl"))
        if len(files) > MAX_KEY_FILES:
            raise RefreshError(
                "The configured key registry exceeds its bounded file budget."
            )
        references = {}
        records = 0
        for path in files:
            raw = read_stable(path, MAX_VAULT_BYTES, private=True)
            for line in raw.splitlines():
                if not line.strip():
                    continue
                records += 1
                if records > MAX_KEY_RECORDS:
                    raise RefreshError(
                        "The configured key registry exceeds its row budget."
                    )
                row = json.loads(line)
                directory = (
                    row.get("account_directory") if isinstance(row, dict) else None
                )
                relative = row.get("relative_path") if isinstance(row, dict) else None
                if (
                    not isinstance(directory, str)
                    or directory != account_id
                    and not directory.startswith(account_id + "_")
                    or not isinstance(relative, str)
                    or _DATABASE.fullmatch(relative) is None
                ):
                    continue
                if _DIRECTORY.fullmatch(directory) is None:
                    raise RefreshError("A configured account directory is invalid.")
                expected = self.native_root / directory / relative
                key = row.get("enc_key")
                if (
                    row.get("database_path") != str(expected)
                    or not isinstance(key, str)
                    or _HEX_KEY.fullmatch(key) is None
                    or not row.get("verification")
                ):
                    raise RefreshError(
                        "A configured key lost its exact database binding."
                    )
                _checked_path(expected)
                identity(expected)
                reference = KeyReference(
                    account_id,
                    directory,
                    relative,
                    expected,
                    bytes.fromhex(key) if load_keys else None,
                )
                previous = references.get(expected)
                if previous is not None and load_keys and previous.key != reference.key:
                    raise RefreshError("Configured database keys conflict.")
                references[expected] = reference
        return list(references.values())

    def missing_database_count(self, references):
        configured = {reference.database_path for reference in references}
        missing = 0
        for directory in {reference.database_path.parent for reference in references}:
            _checked_path(directory)
            for path in directory.glob("message_*.db"):
                if _DATABASE.fullmatch("db_storage/message/" + path.name) is not None:
                    identity(path)
                    missing += path not in configured
        return missing


def read_database(reference, *, check_cancel=None):
    """Authenticate a stable main/WAL image without writing plaintext or snapshots."""
    check_cancel = check_cancel if check_cancel is not None else lambda: None
    check_cancel()
    if reference.key is None:
        raise RefreshError("A configured database key is required.")
    path = _checked_path(reference.database_path)
    before = {
        suffix: identity(Path(str(path) + suffix), optional=bool(suffix))
        for suffix in _GROUP_SUFFIXES
    }
    if before["-journal"] is not None and before["-journal"][2]:
        raise RefreshError("A nonempty rollback journal requires a separate recovery.")
    _admit_snapshot(before)
    try:
        main = read_stable(path, before[""][2])
        wal = (
            read_stable(Path(str(path) + "-wal"), before["-wal"][2])
            if before["-wal"]
            else b""
        )
        if before["-shm"]:
            read_stable(Path(str(path) + "-shm"), before["-shm"][2])
        after = {
            suffix: identity(Path(str(path) + suffix), optional=bool(suffix))
            for suffix in _GROUP_SUFFIXES
        }
    except FileNotFoundError:
        raise SourceSnapshotChanged(
            "The live database group changed during its read-only snapshot."
        ) from None
    if before != after:
        raise SourceSnapshotChanged(
            "The live database group changed during its read-only snapshot."
        )
    main_sha = hashlib.sha256(main).hexdigest()
    wal_sha = hashlib.sha256(wal).hexdigest()
    plaintext, crypto = cipher.decrypt_database(
        main,
        wal,
        reference.key,
        max_database_bytes=MAX_DATABASE_BYTES,
        check_cancel=check_cancel,
    )
    del main, wal
    memory_view = plaintext[:18] + b"\x01\x01" + plaintext[20:]
    del plaintext
    connection = sqlite3.connect(":memory:")
    try:
        connection.row_factory = sqlite3.Row
        connection.deserialize(memory_view)
        del memory_view

        def cancel_query():
            check_cancel()
            return 0

        connection.set_progress_handler(cancel_query, 10_000)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA temp_store=MEMORY")
        if [row[0] for row in connection.execute("PRAGMA integrity_check")] != ["ok"]:
            raise RefreshError(
                "The authenticated committed database failed its integrity check."
            )
        source = _source_receipt(reference, main_sha, wal_sha, crypto)
        return AuthenticatedDatabase(
            connection, source, reference.account_directory, not crypto["complete"],
            crypto.get("tail_status", "unknown"),
        )
    except BaseException:
        connection.close()
        raise
