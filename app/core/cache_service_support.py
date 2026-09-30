"""Shared lifecycle and status helpers for background Cache services."""

# Code version: v1.1.0-claude.0

from __future__ import annotations

from collections.abc import Callable, Iterable
from threading import Event, RLock, Thread
from typing import Any
from uuid import uuid4

from .cache_task_coordinator import CacheTaskCoordinator, CacheTaskIdentity
from .config import CrawlConfig
from .job_lock import CacheTaskLock, SHARED_CACHE_TASK_LOCK
from .logging_setup import reset_job_id, set_job_id
from .shadow_backup import ShadowBackupService
from .state import TaskState, utc_now


CACHE_QUEUED_PHASE = "queued"


class CooperativeCacheWorker:
    """Own the provider-neutral lifecycle of one cooperative Cache worker.

    A worker registered with a coordinator runs beside the other sources and modes
    and waits in its queue when a needed browser or store is busy. A worker built
    without one keeps the original contract: it holds its task lock exclusively.
    """

    def __init__(
        self,
        state: TaskState,
        task_lock: CacheTaskLock | None = None,
        *,
        task: CacheTaskIdentity | None = None,
        coordinator: CacheTaskCoordinator | None = None,
    ) -> None:
        self._state = state
        self._worker: Thread | None = None
        self._stop_requested = Event()
        self._lifecycle_lock = RLock()
        self._task_lock = task_lock or SHARED_CACHE_TASK_LOCK
        self._owns_task_lock = False
        self._task = task
        self._coordinator = coordinator if task is not None else None

    def is_running(self) -> bool:
        """Return whether the task state reports an active or queued worker."""

        return bool(self._state.snapshot()["running"])

    def is_queued(self) -> bool:
        """Return whether the worker is admitted but still waiting for a resource."""

        snapshot = self._state.snapshot()
        return bool(snapshot["running"]) and snapshot["phase"] == CACHE_QUEUED_PHASE

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
        browser: str = "",
        resources: Iterable[str] = (),
        allow_queue: bool = True,
    ) -> None:
        """Admit one daemon worker: start it now, or queue it behind a busy resource."""

        with self._lifecycle_lock:
            if self.is_queued() and self._task is not None:
                raise RuntimeError(f"The {self._task.title} cache is already queued.")
            if self.is_running():
                raise RuntimeError(already_running_message)
            if self._coordinator is None or self._task is None:
                self._start_exclusive_worker(
                    lock_owner=lock_owner,
                    lock_busy_message=lock_busy_message,
                    target=target,
                    args=args,
                    prepare=prepare,
                    thread_factory=thread_factory,
                )
                return

            def begin_run() -> None:
                self._stop_requested.clear()
                if prepare is not None:
                    prepare()
                self._state.reset_for_run()

            def mark_queued(message: str) -> None:
                begin_run()
                self._state.update(phase=CACHE_QUEUED_PHASE)
                self._state.append_event(message)

            def update_queue_message(message: str) -> None:
                if self.is_queued():
                    self._state.append_event(message)

            def launch(queued: bool) -> None:
                if queued:
                    self._state.update(
                        phase="starting",
                        message="Initializing job.",
                        started_at=utc_now(),
                    )
                else:
                    begin_run()
                self._worker = thread_factory(target=target, args=args, daemon=True)
                self._worker.start()

            def fail_queued_launch(error: Exception) -> None:
                self._state.finish_error(str(error))

            try:
                self._coordinator.submit(
                    self._task,
                    launch=launch,
                    on_queued=mark_queued,
                    on_queue_update=update_queue_message,
                    on_launch_error=fail_queued_launch,
                    lock_busy_message=lock_busy_message,
                    browser=browser,
                    resources=resources,
                    allow_queue=allow_queue,
                )
            except Exception as exc:
                if self.is_running():
                    self._state.finish_error(str(exc))
                raise

    def _start_exclusive_worker(
        self,
        *,
        lock_owner: str,
        lock_busy_message: str,
        target: Callable[..., None],
        args: tuple[Any, ...],
        prepare: Callable[[], None] | None,
        thread_factory: Callable[..., Thread],
    ) -> None:
        """Acquire the task lock and start one daemon worker atomically."""

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
        """Publish one cooperative stop request, or withdraw a queued worker."""

        if not self.is_running():
            return False
        if (
            self._coordinator is not None
            and self._task is not None
            and self._coordinator.cancel(self._task.key)
        ):
            self._state.finish_stopped(
                f"The {self._task.title} cache left the queue before it started."
            )
            return True
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
        """Reset logging context and release the task's resources exactly once."""

        try:
            reset_job_id(token)
        finally:
            self._release_task_lock()

    def _release_task_lock(self) -> None:
        with self._lifecycle_lock:
            if self._owns_task_lock:
                self._owns_task_lock = False
                self._task_lock.release()
        if self._coordinator is not None and self._task is not None:
            self._coordinator.finish(self._task.key)


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
    coordinator: CacheTaskCoordinator | None = None,
    task: CacheTaskIdentity | None = None,
) -> str:
    """Run an enabled post-cache backup and append its message to task status.

    A coordinated task defers the backup while another task is still writing the
    store, so the copy never captures a partially written cache.
    """

    if shadow_backup_service is None:
        return completion_message
    auto_sync = bool(
        getattr(config, "shadow_backup_enabled", False)
        and getattr(config, "shadow_backup_auto_sync", False)
    )
    if coordinator is not None and task is not None and auto_sync:
        backup_message = coordinator.sync_shadow_backup_after_task(
            task.key,
            shadow_backup_service,
            config,
        )
    else:
        backup_message = shadow_backup_service.sync_after_cache_task(config)
    if not backup_message:
        return completion_message
    state.append_event(backup_message)
    return f"{completion_message} {backup_message}"
