"""Background service for Grok session history sync.

Code version: v1.0.1-codex.1
"""

from __future__ import annotations

import logging
from pathlib import Path
from threading import Thread

from .cache_service_support import (
    CooperativeCacheWorker,
    append_shadow_backup_completion,
    summarize_status_error,
)
from .config import LOCAL_STORE_ROOT, CrawlConfig
from .grok_history import sync_grok_history
from .job_lock import CacheTaskLock
from .shadow_backup import ShadowBackupService
from .state import TaskState


logger = logging.getLogger(__name__)


def summarize_grok_history_error_for_status(error: Exception) -> str:
    """Return a concise Grok status message while logs retain full details."""

    return summarize_status_error(
        error,
        launch_message=(
            "Grok history could not launch the selected Edge profile. "
            "Check the local log for the complete Playwright error."
        ),
    )


class GrokHistoryService(CooperativeCacheWorker):
    """Manage one Grok text-history sync worker."""

    def __init__(
        self,
        state: TaskState,
        local_store_root: Path | str = LOCAL_STORE_ROOT,
        task_lock: CacheTaskLock | None = None,
        shadow_backup_service: ShadowBackupService | None = None,
    ) -> None:
        super().__init__(state, task_lock)
        self._local_store_root = Path(local_store_root)
        self._config = CrawlConfig()
        self._shadow_backup_service = shadow_backup_service

    def start(self, config: CrawlConfig) -> None:
        """Start one Grok text-history sync worker."""

        def prepare() -> None:
            self._config = config

        self._start_worker(
            lock_owner="grok-history-sync",
            already_running_message="A Grok history sync is already running.",
            lock_busy_message=(
                "A cache task is already running in another Cache Likes window or browser. "
                "Stop it there before starting a Grok history sync."
            ),
            target=self._run,
            prepare=prepare,
            thread_factory=Thread,
        )

    def request_stop(self) -> bool:
        """Request a cooperative stop after the active session."""

        return self._request_stop(
            "Stop requested for Grok history sync. Waiting for the current session to finish."
        )

    def _run(self) -> None:
        """Execute the Grok text-history sync pipeline."""

        job_id, token = self._begin_worker_job()
        try:
            logger.info(
                "Grok history sync started.",
                extra={"job_id": job_id, "grok_browser": self._config.grok_browser},
            )
            if self._is_stop_requested():
                self._state.finish_stopped("Grok history sync stopped before the browser was launched.")
                return
            result = sync_grok_history(
                self._state,
                self._config,
                self._is_stop_requested,
                local_store_root=self._local_store_root,
            )
            if result["stopped"]:
                self._state.finish_stopped(
                    f"Grok history sync stopped. Cached {result['messages']:,} messages."
                )
                return
            completion_message = (
                f"Finished Grok history sync. Inspected {result['sessions']:,} sessions, "
                f"found {result['messages']:,} messages, added or changed "
                f"{result['added_or_changed']:,}, unchanged {result['unchanged']:,}, "
                f"failed {result['failed']:,}."
            )
            completion_message = append_shadow_backup_completion(
                completion_message,
                shadow_backup_service=self._shadow_backup_service,
                state=self._state,
                config=self._config,
            )
            self._state.finish_success(completion_message)
            logger.info("Grok history sync finished successfully.", extra={"job_id": job_id, **result})
        except Exception as exc:  # pragma: no cover - depends on live browser state
            self._state.finish_error(summarize_grok_history_error_for_status(exc))
            logger.exception("Grok history sync failed.", extra={"job_id": job_id, "error": str(exc)})
        finally:
            self._finish_worker_job(token)
