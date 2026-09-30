"""Cross-process exclusion for locally initiated cache tasks.

Cache tasks for different sources and content modes may run together, so each one
holds a lock for every resource it needs: its own task, an exclusive browser, or a
shared store. The original ``.cache_task.lock`` remains the gate that a starting task
passes through and that a maintenance step, such as a shadow backup, holds to keep
every cache writer out.
"""

# Code version: v1.1.0-claude.0

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from threading import Lock
from typing import TextIO

from .config import LOCAL_STORE_ROOT
from .platform_lock import lock_file, unlock_file
from .state import utc_now


CACHE_TASK_LOCK_NAME = ".cache_task.lock"
CACHE_TASK_RESOURCE_LOCK_PREFIX = ".cache_task."
CACHE_TASK_RESOURCE_LOCK_SUFFIX = ".lock"


class CacheTaskLock:
    """Hold a non-blocking advisory lock while one cache task is active."""

    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        self._guard = Lock()
        self._handle: TextIO | None = None

    @property
    def path(self) -> Path:
        """Return the lock file this owner coordinates through."""
        return self._lock_path

    def acquire(self, task_name: str) -> bool:
        """Acquire the lock, returning false when another app process owns it."""
        with self._guard:
            if self._handle is not None:
                return False

            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = self._lock_path.open("a+", encoding="utf-8")
            try:
                lock_file(handle, blocking=False)
            except BlockingIOError:
                handle.close()
                return False

            try:
                handle.seek(0)
                handle.truncate()
                json.dump(
                    {
                        "pid": os.getpid(),
                        "started_at": utc_now(),
                        "task_name": task_name,
                    },
                    handle,
                    sort_keys=True,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            except OSError:
                unlock_file(handle)
                handle.close()
                raise

            self._handle = handle
            return True

    def release(self) -> None:
        """Release the lock when its owning worker has finished."""
        with self._guard:
            if self._handle is None:
                return
            try:
                unlock_file(self._handle)
            finally:
                self._handle.close()
                self._handle = None


def cache_task_resource_lock_path(lock_root: Path, resource: str) -> Path:
    """Return the lock file that guards one named cache resource."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(resource or "").strip().lower()).strip("-")
    if not slug:
        raise ValueError("A cache resource lock requires a name.")
    return Path(lock_root) / (
        f"{CACHE_TASK_RESOURCE_LOCK_PREFIX}{slug}{CACHE_TASK_RESOURCE_LOCK_SUFFIX}"
    )


def is_cache_task_lock_name(name: str) -> bool:
    """Return whether a local-store entry is cache coordination state, not cached data."""
    return name == CACHE_TASK_LOCK_NAME or (
        name.startswith(CACHE_TASK_RESOURCE_LOCK_PREFIX)
        and name.endswith(CACHE_TASK_RESOURCE_LOCK_SUFFIX)
    )


def _lock_file_is_held(lock_path: Path) -> bool:
    """Probe one lock file without recording this process as its owner."""
    try:
        handle = lock_path.open("a+", encoding="utf-8")
    except OSError:
        return True
    try:
        lock_file(handle, blocking=False)
    except BlockingIOError:
        return True
    else:
        unlock_file(handle)
        return False
    finally:
        handle.close()


def held_cache_task_resource_locks(
    lock_root: Path,
    *,
    ignore: frozenset[Path] = frozenset(),
) -> tuple[Path, ...]:
    """Return the resource locks that an active cache task currently holds."""
    root = Path(lock_root)
    if not root.is_dir():
        return ()
    candidates = sorted(
        path
        for path in root.glob(
            f"{CACHE_TASK_RESOURCE_LOCK_PREFIX}*{CACHE_TASK_RESOURCE_LOCK_SUFFIX}"
        )
        if path.name != CACHE_TASK_LOCK_NAME and path not in ignore
    )
    return tuple(path for path in candidates if _lock_file_is_held(path))


class CacheMaintenanceLock:
    """Exclude every cache task, in any process, while a maintenance step runs.

    The holder keeps the gate for the whole step so no task can start, and it only
    succeeds when no task already holds one of the per-resource locks.
    """

    def __init__(self, gate: CacheTaskLock, lock_root: Path | None = None) -> None:
        self._gate = gate
        self._lock_root = Path(lock_root) if lock_root is not None else gate.path.parent

    def acquire(
        self,
        task_name: str,
        *,
        ignore: frozenset[Path] = frozenset(),
    ) -> bool:
        """Acquire exclusivity, returning false while any cache task is active."""
        if not self._gate.acquire(task_name):
            return False
        try:
            busy = bool(held_cache_task_resource_locks(self._lock_root, ignore=ignore))
        except BaseException:
            self._gate.release()
            raise
        if busy:
            self._gate.release()
            return False
        return True

    def release(self) -> None:
        """Let cache tasks start again."""
        self._gate.release()


SHARED_CACHE_TASK_LOCK = CacheTaskLock(LOCAL_STORE_ROOT / CACHE_TASK_LOCK_NAME)
SHARED_CACHE_MAINTENANCE_LOCK = CacheMaintenanceLock(SHARED_CACHE_TASK_LOCK)
