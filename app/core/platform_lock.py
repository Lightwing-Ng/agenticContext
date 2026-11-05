"""Small cross-platform file-locking helpers."""

# Code version: v1.0.1-codex.0

from __future__ import annotations

import os
from typing import TextIO

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def lock_file(handle: TextIO, *, blocking: bool = True) -> None:
    """Acquire an exclusive lock for the lifetime of an open file handle."""
    if os.name == "nt":
        # Windows byte locks may extend beyond EOF; initialization needs no write.
        handle.seek(0)
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        try:
            msvcrt.locking(handle.fileno(), mode, 1)
        except OSError as exc:
            raise BlockingIOError(exc.errno, str(exc)) from exc
        return
    flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
    fcntl.flock(handle.fileno(), flags)


def unlock_file(handle: TextIO) -> None:
    """Release a lock previously acquired with :func:`lock_file`."""
    if os.name == "nt":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
