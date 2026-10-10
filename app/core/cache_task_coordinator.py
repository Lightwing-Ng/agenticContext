"""Admission, queueing, and cross-process exclusion for concurrent cache tasks.

Every source and content mode is one task. Tasks that need different resources run
together. A task whose browser or store is already in use waits in arrival order and
starts as soon as that resource is free, so a request never has to be repeated.

The coordinator owns three things the workers used to share through one global lock:

- which tasks are running or queued in this process, and why a queued task waits;
- the per-resource lock files that keep another process off the same task, browser,
  or store;
- the rule that a shadow backup never copies the store while a task is writing it.
"""

# Code version: v1.0.0-claude.0

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any

from .config import LOCAL_STORE_ROOT, is_windows_host
from .job_lock import (
    CACHE_TASK_LOCK_NAME,
    SHARED_CACHE_TASK_LOCK,
    CacheTaskLock,
    cache_task_resource_lock_path,
    held_cache_task_resource_locks,
)


logger = logging.getLogger(__name__)

CACHE_CONTENT_MODE_LABELS = {"text": "Text", "media": "Media"}
CACHE_BROWSER_LABELS = {"safari": "Safari", "edge": "Edge", "chrome": "Chrome"}
CACHE_TASK_GATE_WAIT_SECONDS = 0.5
CACHE_TASK_GATE_POLL_SECONDS = 0.01
SHADOW_BACKUP_MAINTENANCE_LABEL = "shadow cloud backup"
SHADOW_BACKUP_DEFERRED_MESSAGE = (
    "Shadow cloud backup will run after the other active cache tasks finish."
)
SHADOW_BACKUP_SKIPPED_MESSAGE = (
    "Shadow cloud backup was skipped because another window has an active cache task."
)


class CacheTaskBusyError(RuntimeError):
    """Raised when a cache task cannot start now and may not wait in the queue."""


@dataclass(frozen=True, slots=True)
class CacheTaskIdentity:
    """Name one independently runnable cache task: a source and its content mode."""

    source: str
    content_mode: str
    label: str

    @property
    def key(self) -> str:
        """Return the stable identifier shared by the registry, locks, and status."""
        return f"{self.source}:{self.content_mode}"

    @property
    def title(self) -> str:
        """Return the operator-facing name, such as ``ChatGPT · Text``."""
        mode_label = CACHE_CONTENT_MODE_LABELS.get(self.content_mode, "")
        return f"{self.label} · {mode_label}" if mode_label else self.label


def exclusive_cache_browser(browser: str) -> str:
    """Return the browser id when only one cache task may drive that browser."""
    normalized = str(browser or "").strip().lower()
    if normalized == "safari":
        # Safari automation owns one task window behind a single context lease.
        return normalized
    if normalized in {"edge", "chrome"} and is_windows_host():
        # A running Windows browser locks its cookies, so tasks attach to one CDP profile.
        return normalized
    return ""


def _browser_label(browser: str) -> str:
    """Return the display name for one browser id."""
    normalized = str(browser or "").strip().lower()
    return CACHE_BROWSER_LABELS.get(normalized, normalized.title())


@dataclass(slots=True)
class _Ticket:
    """One admitted task and the callbacks that move its worker between states."""

    identity: CacheTaskIdentity
    browser: str
    resources: frozenset[str]
    launch: Callable[[bool], None]
    on_queued: Callable[[str], None]
    on_queue_update: Callable[[str], None]
    on_launch_error: Callable[[Exception], None]
    lock_busy_message: str
    sequence: int
    locks: list[CacheTaskLock] = field(default_factory=list)
    waiting_for: str = ""
    blocked_by: str = ""
    queue_message: str = ""


class CacheTaskCoordinator:
    """Admit, queue, and release cache tasks for one application process."""

    def __init__(
        self,
        lock_root: Path | str | None = None,
        *,
        gate_lock: CacheTaskLock | None = None,
    ) -> None:
        self._lock_root = Path(lock_root) if lock_root is not None else LOCAL_STORE_ROOT
        if gate_lock is not None:
            self._gate = gate_lock
        elif lock_root is None:
            self._gate = SHARED_CACHE_TASK_LOCK
        else:
            self._gate = CacheTaskLock(self._lock_root / CACHE_TASK_LOCK_NAME)
        self._guard = RLock()
        self._running: dict[str, _Ticket] = {}
        self._queue: list[_Ticket] = []
        self._sequence = 0
        self._maintenance = ""
        self._pending_backup: tuple[Any, Any] | None = None

    # Admission -----------------------------------------------------------------

    def submit(
        self,
        identity: CacheTaskIdentity,
        *,
        launch: Callable[[bool], None],
        on_queued: Callable[[str], None],
        on_queue_update: Callable[[str], None],
        on_launch_error: Callable[[Exception], None],
        lock_busy_message: str,
        browser: str = "",
        resources: Iterable[str] = (),
        allow_queue: bool = True,
    ) -> bool:
        """Start one task now or queue it; return whether it started immediately.

        ``launch`` receives whether the task waited in the queue. It runs while the
        coordinator is locked, so it must only prepare state and start its thread.
        """
        with self._guard:
            self._sequence += 1
            ticket = _Ticket(
                identity=identity,
                browser=str(browser or "").strip().lower(),
                resources=self._resource_names(identity, browser, resources),
                launch=launch,
                on_queued=on_queued,
                on_queue_update=on_queue_update,
                on_launch_error=on_launch_error,
                lock_busy_message=lock_busy_message,
                sequence=self._sequence,
            )
            if any(queued.identity.key == identity.key for queued in self._queue):
                raise CacheTaskBusyError(f"The {identity.title} cache is already queued.")
            waiting_for, blocked_by = self._blocker(ticket)
            if blocked_by:
                message = self._waiting_message(waiting_for, blocked_by)
                if not allow_queue:
                    raise CacheTaskBusyError(self._busy_message(identity, waiting_for, blocked_by))
                ticket.waiting_for = waiting_for
                ticket.blocked_by = blocked_by
                ticket.queue_message = message
                self._queue.append(ticket)
                on_queued(message)
                return False
            locks = self._acquire_locks(ticket, gate_held=False)
            if locks is None:
                raise CacheTaskBusyError(lock_busy_message)
            self._mark_running(ticket, locks)
            self._start(ticket, queued=False)
            return True

    def cancel(self, key: str) -> bool:
        """Remove one queued task; return false when it is not waiting."""
        with self._guard:
            for ticket in self._queue:
                if ticket.identity.key == key:
                    self._queue.remove(ticket)
                    self._advance_queue()
                    return True
            return False

    def finish(self, key: str) -> None:
        """Release one finished task and start whatever it was blocking."""
        backup: tuple[Any, Any] | None = None
        with self._guard:
            ticket = self._running.pop(key, None)
            if ticket is None:
                return
            # Hold the gate across the handover so a maintenance step cannot start
            # between this task's release and the next task's acquisition.
            gate_held = self._acquire_gate(key)
            try:
                self._release_locks(ticket)
                ready = self._claim_ready(gate_held=gate_held)
            finally:
                if gate_held:
                    self._gate.release()
            if self._start_ready(ready):
                self._advance_queue()
            else:
                self._refresh_queue_messages()
            backup = self._claim_pending_backup()
        if backup is not None:
            self._run_deferred_backup(*backup)

    # Read models ---------------------------------------------------------------

    def task_states(self) -> dict[str, dict[str, str]]:
        """Return the browser and queue details of every admitted task.

        A queued task's message is built from registered labels only, so it is safe
        to show beside tasks from other sources.
        """
        with self._guard:
            states = {
                key: {
                    "status": "running",
                    "browser": ticket.browser,
                    "waiting_for": "",
                    "blocked_by": "",
                    "message": "",
                }
                for key, ticket in self._running.items()
            }
            for ticket in self._queue:
                states[ticket.identity.key] = {
                    "status": "queued",
                    "browser": ticket.browser,
                    "waiting_for": ticket.waiting_for,
                    "blocked_by": ticket.blocked_by,
                    "message": ticket.queue_message,
                }
            return states

    def running_browser_task(self, browser: str) -> str:
        """Return the title of a running task that owns the given exclusive browser."""
        normalized = str(browser or "").strip().lower()
        with self._guard:
            for ticket in self._running.values():
                if ticket.browser == normalized and exclusive_cache_browser(normalized):
                    return ticket.identity.title
        return ""

    # Shadow backup -------------------------------------------------------------

    def sync_shadow_backup_after_task(
        self,
        key: str,
        shadow_backup_service: Any,
        config: Any,
    ) -> str | None:
        """Back up after one task, or defer until no other task is writing the store."""
        with self._guard:
            if any(running_key != key for running_key in self._running) or self._queue:
                self._pending_backup = (shadow_backup_service, config)
                return SHADOW_BACKUP_DEFERRED_MESSAGE
            own = self._running.get(key)
            ignore = frozenset(lock.path for lock in own.locks) if own is not None else frozenset()
            if not self._begin_maintenance(ignore):
                return SHADOW_BACKUP_SKIPPED_MESSAGE
        try:
            return shadow_backup_service.sync_after_cache_task(config)
        finally:
            self._end_maintenance()

    def _claim_pending_backup(self) -> tuple[Any, Any] | None:
        """Take the deferred backup once the last task has finished."""
        if self._pending_backup is None or self._running or self._queue:
            return None
        backup = self._pending_backup
        self._pending_backup = None
        if not self._begin_maintenance(frozenset()):
            logger.info("Deferred shadow cloud backup skipped: another window has an active cache task.")
            return None
        return backup

    def _run_deferred_backup(self, shadow_backup_service: Any, config: Any) -> None:
        try:
            message = shadow_backup_service.sync_after_cache_task(config)
            if message:
                logger.info("Deferred shadow cloud backup finished: %s", message)
        except Exception:  # pragma: no cover - the backup service reports its own failures
            logger.exception("Deferred shadow cloud backup failed.")
        finally:
            self._end_maintenance()

    def _begin_maintenance(self, ignore: frozenset[Path]) -> bool:
        """Keep every task out while the backup copies the store."""
        if not self._acquire_gate(SHADOW_BACKUP_MAINTENANCE_LABEL):
            return False
        try:
            busy = bool(held_cache_task_resource_locks(self._lock_root, ignore=ignore))
        except BaseException:
            self._gate.release()
            raise
        if busy:
            self._gate.release()
            return False
        self._maintenance = SHADOW_BACKUP_MAINTENANCE_LABEL
        return True

    def _end_maintenance(self) -> None:
        with self._guard:
            self._maintenance = ""
            self._gate.release()
            self._advance_queue()

    # Scheduling ----------------------------------------------------------------

    @staticmethod
    def _resource_names(
        identity: CacheTaskIdentity,
        browser: str,
        resources: Iterable[str],
    ) -> frozenset[str]:
        names = {f"task:{identity.key}"}
        exclusive_browser = exclusive_cache_browser(browser)
        if exclusive_browser:
            names.add(f"browser:{exclusive_browser}")
        names.update(f"store:{name}" for name in resources if name)
        return frozenset(names)

    def _blocker(self, ticket: _Ticket) -> tuple[str, str]:
        """Return what an admitted task waits for and which task holds it."""
        if self._maintenance:
            return "", self._maintenance
        holders = [*self._running.values()]
        holders.extend(queued for queued in self._queue if queued.sequence < ticket.sequence)
        for holder in holders:
            if holder is ticket:
                continue
            shared = ticket.resources & holder.resources
            if not shared:
                continue
            browser = next(
                (name.partition(":")[2] for name in sorted(shared) if name.startswith("browser:")),
                "",
            )
            return _browser_label(browser) if browser else "", holder.identity.title
        return "", ""

    def _waiting_message(self, waiting_for: str, blocked_by: str) -> str:
        if blocked_by == self._maintenance and self._maintenance:
            return f"Queued. This task starts automatically after the {blocked_by} finishes."
        if waiting_for:
            return (
                f"Queued. {waiting_for} is busy with the {blocked_by} cache. "
                f"This task starts automatically when {waiting_for} is free."
            )
        return f"Queued. This task starts automatically after the {blocked_by} cache finishes."

    def _busy_message(self, identity: CacheTaskIdentity, waiting_for: str, blocked_by: str) -> str:
        if blocked_by == self._maintenance and self._maintenance:
            return f"The {blocked_by} is running. Start the {identity.title} cache after it finishes."
        if waiting_for:
            return (
                f"{waiting_for} is busy with the {blocked_by} cache. "
                f"Start the {identity.title} cache after it finishes."
            )
        return (
            f"The {blocked_by} cache is still running. "
            f"Start the {identity.title} cache after it finishes."
        )

    def _acquire_gate(self, owner: str) -> bool:
        """Pass the shared gate, tolerating another task that is starting right now."""
        deadline = time.monotonic() + CACHE_TASK_GATE_WAIT_SECONDS
        while True:
            if self._gate.acquire(owner):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(CACHE_TASK_GATE_POLL_SECONDS)

    def _acquire_locks(self, ticket: _Ticket, *, gate_held: bool) -> list[CacheTaskLock] | None:
        """Take every resource lock for one task, or none of them."""
        owns_gate = False
        if not gate_held:
            owns_gate = self._acquire_gate(ticket.identity.key)
            if not owns_gate:
                return None
        acquired: list[CacheTaskLock] = []
        try:
            for resource in sorted(ticket.resources):
                lock = CacheTaskLock(cache_task_resource_lock_path(self._lock_root, resource))
                if not lock.acquire(ticket.identity.key):
                    self._release_all(acquired)
                    return None
                acquired.append(lock)
            return acquired
        except BaseException:
            self._release_all(acquired)
            raise
        finally:
            if owns_gate:
                self._gate.release()

    @staticmethod
    def _release_all(locks: list[CacheTaskLock]) -> None:
        for lock in locks:
            lock.release()

    def _release_locks(self, ticket: _Ticket) -> None:
        self._release_all(ticket.locks)
        ticket.locks = []

    def _mark_running(self, ticket: _Ticket, locks: list[CacheTaskLock]) -> None:
        ticket.locks = locks
        ticket.waiting_for = ""
        ticket.blocked_by = ""
        ticket.queue_message = ""
        # Registered before the thread starts so a worker that ends at once is found.
        self._running[ticket.identity.key] = ticket

    def _start(self, ticket: _Ticket, *, queued: bool) -> bool:
        """Start one claimed task; a queued task reports its own start failure."""
        try:
            ticket.launch(queued)
        except Exception as exc:
            if self._running.get(ticket.identity.key) is ticket:
                del self._running[ticket.identity.key]
            self._release_locks(ticket)
            if not queued:
                raise
            ticket.on_launch_error(exc)
            return False
        return True

    def _claim_ready(self, *, gate_held: bool) -> list[_Ticket]:
        """Move every queued task whose resources are free into the running set."""
        if self._maintenance:
            return []
        ready: list[_Ticket] = []
        for ticket in list(self._queue):
            if self._blocker(ticket)[1]:
                continue
            self._queue.remove(ticket)
            locks = self._acquire_locks(ticket, gate_held=gate_held)
            if locks is None:
                ticket.on_launch_error(CacheTaskBusyError(ticket.lock_busy_message))
                continue
            self._mark_running(ticket, locks)
            ready.append(ticket)
        return ready

    def _start_ready(self, ready: list[_Ticket]) -> bool:
        """Start claimed tasks; return whether one failed and freed its resources."""
        failed = False
        for ticket in ready:
            if not self._start(ticket, queued=True):
                failed = True
        return failed

    def _advance_queue(self) -> None:
        """Start queued tasks in arrival order until nothing else can start."""
        while self._start_ready(self._claim_ready(gate_held=False)):
            continue
        self._refresh_queue_messages()

    def _refresh_queue_messages(self) -> None:
        """Tell each waiting task when the task it waits behind has changed."""
        for ticket in self._queue:
            waiting_for, blocked_by = self._blocker(ticket)
            if not blocked_by:
                continue
            message = self._waiting_message(waiting_for, blocked_by)
            if message == ticket.queue_message:
                continue
            ticket.waiting_for = waiting_for
            ticket.blocked_by = blocked_by
            ticket.queue_message = message
            ticket.on_queue_update(message)


__all__ = [
    "CacheTaskBusyError",
    "CacheTaskCoordinator",
    "CacheTaskIdentity",
    "exclusive_cache_browser",
]
