"""Coordinate global repair applications and merges with local chat refreshes.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import stat
from typing import Iterator, TextIO

from .platform_lock import lock_file, unlock_file
from .wechat_archive import wechat_archive_path


LOCK_FILENAME = ".wechat-global-refresh.lock"
BUSY_MESSAGE = "A global chat repair is running. Try again after it finishes."
_METADATA = '{"schema_version":1,"purpose":"global_chat_repair_final_merge"}\n'


class GlobalRefreshBusy(RuntimeError):
    """A global repair application or merge currently excludes local refreshes."""

    def __init__(self):
        super().__init__(BUSY_MESSAGE)


def _path(local_store_root: Path | str) -> Path:
    return wechat_archive_path(local_store_root).with_name(LOCK_FILENAME)


def _verify(handle: TextIO, path: Path) -> None:
    opened = os.fstat(handle.fileno())
    named = path.lstat()
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or opened.st_nlink != 1
        or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        or os.name != "nt" and opened.st_uid != os.getuid()
    ):
        raise OSError("The global chat repair lock must be an unchanged owned file.")


def _open(path: Path, *, create: bool) -> TextIO:
    flags = (
        os.O_RDWR
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOINHERIT", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    if create:
        flags |= os.O_CREAT
    descriptor = os.open(path, flags, 0o600)
    return os.fdopen(descriptor, "r+", encoding="ascii")


def global_refresh_busy(local_store_root: Path | str) -> bool:
    """Probe the advisory lease without creating files or interpreting stale metadata."""
    path = _path(local_store_root)
    try:
        handle = _open(path, create=False)
    except FileNotFoundError:
        return False
    with handle:
        _verify(handle, path)
        try:
            lock_file(handle, blocking=False)
        except BlockingIOError:
            return True
        try:
            _verify(handle, path)
            return False
        finally:
            unlock_file(handle)


@contextmanager
def global_refresh_guard(local_store_root: Path | str) -> Iterator[None]:
    """Hold the exclusive lease during a global repair application or final merge.

    The persistent lock inode must never be unlinked as an unlock operation. Its
    fixed, credential-free metadata is descriptive; only the advisory lock is live.
    Existing publication input checks and bank locking remain authoritative.
    """
    path = _path(local_store_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _open(path, create=True) as handle:
        _verify(handle, path)
        lock_file(handle)
        try:
            _verify(handle, path)
            if os.name != "nt":
                os.fchmod(handle.fileno(), 0o600)
            handle.seek(0)
            handle.write(_METADATA)
            handle.truncate()
            handle.flush()
            yield
        finally:
            unlock_file(handle)
