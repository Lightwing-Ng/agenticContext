"""Bound local ChatGPT image reads without abandoning writable background work."""

# Code version: v1.0.1-codex.0

from __future__ import annotations

import errno
from pathlib import Path
from queue import Empty, Queue
from threading import BoundedSemaphore, Thread
import time


CACHE_IMAGE_READ_TIMEOUT_SECONDS = 5.0
_READ_SLOTS = BoundedSemaphore(4)


class ChatGPTCacheReadTimeoutError(RuntimeError):
    """Keep unavailable cached images distinct from corrupt image payloads."""


def read_cached_image(path: Path, size: int = -1) -> bytes:
    """Read on a bounded daemon thread; a timed-out read can never mutate the cache.

    Cloud-backed files can stall a regular-file read indefinitely, even when stat
    succeeds. Keep the slot until that read exits so retries cannot leak threads.
    """
    deadline = time.monotonic() + CACHE_IMAGE_READ_TIMEOUT_SECONDS
    message = (
        f"Reading cached ChatGPT image {path.name} timed out. "
        "Existing files were preserved. Make the cache available locally, then retry."
    )
    if not _READ_SLOTS.acquire(blocking=False):
        raise ChatGPTCacheReadTimeoutError(
            "Earlier ChatGPT cache reads are still blocked. "
            "Existing files were preserved; retry when local file access recovers."
        )
    result: Queue[bytes | Exception] = Queue(maxsize=1)

    def read() -> None:
        try:
            with path.open("rb") as handle:
                result.put(handle.read(size))
        except Exception as exc:
            result.put(exc)
        finally:
            _READ_SLOTS.release()

    try:
        Thread(target=read, name="chatgpt-cache-read", daemon=True).start()
    except Exception:
        _READ_SLOTS.release()
        raise
    try:
        value = result.get(timeout=max(0.0, deadline - time.monotonic()))
    except Empty:
        raise ChatGPTCacheReadTimeoutError(message) from None
    if isinstance(value, TimeoutError) or (
        isinstance(value, OSError) and value.errno == errno.ETIMEDOUT
    ):
        raise ChatGPTCacheReadTimeoutError(message) from value
    if isinstance(value, Exception):
        raise value
    return value
