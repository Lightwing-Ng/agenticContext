"""Shared lifecycle and status helpers for background Cache services."""

# Code version: v1.0.0-codex.1

from __future__ import annotations

from collections.abc import Callable
from threading import Event, RLock, Thread
from typing import Any
from uuid import uuid4

from .config import CrawlConfig
from .job_lock import CacheTaskLock, SHARED_CACHE_TASK_LOCK
from .logging_setup import reset_job_id, set_job_id
from .shadow_backup import ShadowBackupService
from .state import TaskState


class CooperativeCacheWorker:
    """Own the provider-neutral lifecycle of one cooperative Cache worker."""

    def __init__(
        self,
        state: TaskState,
        task_lock: CacheTaskLock | None = None,
    ) -> None:
        self._state = state
        self._worker: Thread | None = None
        self._stop_requested = Event()
        self._lifecycle_lock = RLock()
        self._task_lock = task_lock or SHARED_CACHE_TASK_LOCK
        self._owns_task_lock = False

    def is_running(self) -> bool:
        """Return whether the task state reports an active worker."""

        return bool(self._state.snapshot()["running"])

    def _start_worker(
        self,
        *,
        lock_owner: str,
        already_running_message: str,
        lock_busy_message: str,
        target: Callable[..., None],
        args: tuple[Any, ...] = (),
        prepare: Callable[[], None] | None = None,
        thread_factory: Callable[..., Thread] = Thread,
    ) -> None:
        """Acquire the shared task lock and start one daemon worker atomically."""

        with self._lifecycle_lock:
            if self.is_running():
                raise RuntimeError(already_running_message)
            if not self._task_lock.acquire(lock_owner):
                raise RuntimeError(lock_busy_message)

            self._owns_task_lock = True
            try:
                self._stop_requested.clear()
                if prepare is not None:
                    prepare()
                self._state.reset_for_run()
                self._worker = thread_factory(target=target, args=args, daemon=True)
                self._worker.start()
            except Exception as exc:
                try:
                    self._state.finish_error(str(exc))
                finally:
                    self._release_task_lock()
                raise

    def _request_stop(self, message: str) -> bool:
        """Publish one cooperative stop request when the worker is active."""

        if not self.is_running():
            return False
        self._stop_requested.set()
        self._state.update(phase="stopping")
        self._state.append_event(message)
        return True

    def _is_stop_requested(self) -> bool:
        return self._stop_requested.is_set()

    def _begin_worker_job(self) -> tuple[str, Any]:
        """Create the bounded job ID and bind it to structured logging."""

        job_id = uuid4().hex[:12]
        return job_id, set_job_id(job_id)

    def _finish_worker_job(self, token: Any) -> None:
        """Reset logging context and release the shared task lock exactly once."""

        try:
            reset_job_id(token)
        finally:
            self._release_task_lock()

    def _release_task_lock(self) -> None:
        with self._lifecycle_lock:
            if self._owns_task_lock:
                self._owns_task_lock = False
                self._task_lock.release()


def summarize_status_error(
    error: Exception,
    *,
    launch_message: str | None = None,
    max_length: int = 500,
) -> str:
    """Return one bounded status line while the full exception remains in logs."""

    error_text = str(error).strip()
    if launch_message and "BrowserType.launch_persistent_context" in error_text:
        return launch_message
    first_line = error_text.splitlines()[0] if error_text else error.__class__.__name__
    if len(first_line) <= max_length:
        return first_line
    return f"{first_line[: max_length - 3]}..."


def append_shadow_backup_completion(
    completion_message: str,
    *,
    shadow_backup_service: ShadowBackupService | None,
    state: TaskState,
    config: CrawlConfig,
) -> str:
    """Run an enabled post-cache backup and append its message to task status."""

    if shadow_backup_service is None:
        return completion_message
    backup_message = shadow_backup_service.sync_after_cache_task(config)
    if not backup_message:
        return completion_message
    state.append_event(backup_message)
    return f"{completion_message} {backup_message}"
