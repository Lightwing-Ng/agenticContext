"""Background service for Gemini session history sync."""

# Code version: v1.0.2-codex.1

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
from .gemini_downloader import sync_gemini_history
from .job_lock import CacheTaskLock
from .shadow_backup import ShadowBackupService
from .state import TaskState


logger = logging.getLogger(__name__)


def summarize_gemini_error_for_status(error: Exception) -> str:
    """Return a concise Gemini status message while logs retain full details."""
    return summarize_status_error(
        error,
        launch_message=(
            "Gemini browser automation could not launch the selected Chromium profile. "
            "Check the local log for the complete Playwright error."
        ),
    )


class GeminiHistoryService(CooperativeCacheWorker):
    """Manage one Gemini history sync worker."""

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
        """Start one Gemini history sync worker."""
        def prepare() -> None:
            self._config = config

        self._start_worker(
            lock_owner="gemini-history-sync",
            already_running_message="A Gemini history sync is already running.",
            lock_busy_message=(
                "A cache task is already running in another Cache Likes window or browser. "
                "Stop it there before starting a Gemini history sync."
            ),
            target=self._run,
            prepare=prepare,
            thread_factory=Thread,
        )

    def request_stop(self) -> bool:
        """Request a cooperative stop after the active session."""
        return self._request_stop(
            "Stop requested for Gemini history sync. Waiting for the current session to finish."
        )

    def _run(self) -> None:
        """Execute the Gemini history sync pipeline."""
        job_id, token = self._begin_worker_job()
        try:
            logger.info(
                "Gemini history sync started.",
                extra={"job_id": job_id, "gemini_browser": self._config.gemini_browser},
            )
            if self._is_stop_requested():
                self._state.finish_stopped("Gemini history sync stopped before the browser was launched.")
                return
            result = sync_gemini_history(
                self._state,
                self._config,
                self._is_stop_requested,
                self._local_store_root,
            )
            if result.stopped:
                self._state.finish_stopped(
                    f"Gemini history sync stopped. Cached {result.cached_conversations:,} sessions "
                    f"and {result.cached_messages:,} messages."
                )
                return
            completion_message = (
                f"Finished Gemini history sync. Inspected {result.processed_conversations:,} of "
                f"{result.discovered_conversations:,} sessions, found {result.discovered_messages:,} messages, "
                f"added or changed {result.new_messages:,}, unchanged sessions "
                f"{result.unchanged_conversations:,}, failed {result.failed_conversations:,}; "
                f"cached total {result.cached_conversations:,} sessions and {result.cached_messages:,} messages."
            )
            completion_message = append_shadow_backup_completion(
                completion_message,
                shadow_backup_service=self._shadow_backup_service,
                state=self._state,
                config=self._config,
            )
            self._state.finish_success(completion_message)
            logger.info(
                "Gemini history sync finished successfully.",
                extra={
                    "job_id": job_id,
                    "discovered_conversations": result.discovered_conversations,
                    "processed_conversations": result.processed_conversations,
                    "cached_messages": result.cached_messages,
                    "failed_conversations": result.failed_conversations,
                },
            )
        except Exception as exc:  # pragma: no cover - depends on live browser state
            self._state.finish_error(summarize_gemini_error_for_status(exc))
            logger.exception("Gemini history sync failed.", extra={"job_id": job_id, "error": str(exc)})
        finally:
            self._finish_worker_job(token)
