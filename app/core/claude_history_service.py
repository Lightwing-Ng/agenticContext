"""Background service for Claude session history sync.

Code version: v1.1.3-codex.0
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
from .claude_history import sync_claude_history
from .claude_media import sync_claude_media
from .config import LOCAL_STORE_ROOT, CrawlConfig
from .job_lock import CacheTaskLock
from .shadow_backup import ShadowBackupService
from .state import TaskState


logger = logging.getLogger(__name__)


def summarize_claude_error_for_status(error: Exception) -> str:
    """Return a concise Claude status message while logs retain full details."""

    return summarize_status_error(
        error,
        launch_message=(
            "Claude history could not launch the selected browser session. "
            "Check the local log for the complete browser error."
        ),
    )


class ClaudeHistoryService(CooperativeCacheWorker):
    """Manage one Claude text-history sync worker."""

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
        self._content_mode = "text"
        self._shadow_backup_service = shadow_backup_service

    def start(self, config: CrawlConfig, content_mode: str = "text") -> None:
        """Start one mode-specific Claude cache worker."""

        def prepare() -> None:
            self._config = config
            self._content_mode = "media" if content_mode == "media" else "text"

        self._start_worker(
            lock_owner="claude-history-sync",
            already_running_message="A Claude history sync is already running.",
            lock_busy_message=(
                "A cache task is already running in another Cache Likes window or browser. "
                "Stop it there before starting a Claude history sync."
            ),
            target=self._run,
            prepare=prepare,
            thread_factory=Thread,
        )
        self._state.update(performance_metrics={"content_mode": self._content_mode})

    def request_stop(self) -> bool:
        """Request a cooperative stop after the active session."""

        return self._request_stop(
            "Stop requested for Claude history sync. Waiting for the current session to finish."
        )

    def _run(self) -> None:
        """Execute the Claude text-history sync pipeline."""

        job_id, token = self._begin_worker_job()
        try:
            logger.info(
                "Claude sync started.",
                extra={
                    "job_id": job_id,
                    "claude_browser": self._config.claude_browser,
                    "content_mode": self._content_mode,
                },
            )
            if self._is_stop_requested():
                self._state.finish_stopped("Claude sync stopped before the browser was launched.")
                return
            if self._content_mode == "media":
                result = sync_claude_media(
                    self._state,
                    self._config,
                    self._is_stop_requested,
                    self._local_store_root,
                )
                skip_summary = (
                    f"Skipped {result.skipped_known:,} known files, "
                    f"{result.skipped_excluded:,} excluded by deletion, "
                    f"{result.skipped_size:,} over the size limit, and "
                    f"{result.skipped_unsupported:,} unsupported references."
                )
                if result.stopped:
                    self._state.finish_stopped(
                        f"Claude rendered-image cache stopped. {result.cached_images:,} local image files are present. "
                        f"{skip_summary}"
                    )
                    return
                if result.incomplete:
                    self._state.finish_error(
                        "Claude rendered-image cache is incomplete. Cached files were preserved; "
                        f"{result.failed_images:,} images and {result.failed_sessions:,} sessions failed. "
                        f"{skip_summary}"
                    )
                    return
                completion_message = (
                    f"Finished Claude rendered-image cache. Inspected {result.sessions:,} sessions, "
                    f"found {result.discovered_images:,} eligible images, downloaded "
                    f"{result.downloaded_images:,} new files. {skip_summary} "
                    f"{result.cached_images:,} local image files present. Other attachments and videos "
                    "are outside this image-only mode."
                )
                completion_message = append_shadow_backup_completion(
                    completion_message,
                    shadow_backup_service=self._shadow_backup_service,
                    state=self._state,
                    config=self._config,
                )
                self._state.finish_success(completion_message)
                logger.info(
                    "Claude rendered-image cache finished successfully.",
                    extra={"job_id": job_id, "cached_images": result.cached_images},
                )
                return
            result = sync_claude_history(
                self._state,
                self._config,
                self._is_stop_requested,
                self._local_store_root,
            )
            if result["stopped"]:
                self._state.finish_stopped(
                    f"Claude history sync stopped. Cached {result['messages']:,} messages."
                )
                return
            if result["failed"]:
                self._state.finish_error(
                    "Claude history sync is incomplete. Cached history was preserved; "
                    f"{result['failed']:,} sessions failed."
                )
                return
            completion_message = (
                f"Finished Claude history sync. Inspected {result['sessions']:,} sessions, "
                f"found {result['messages']:,} messages, added or changed "
                f"{result['added_or_changed']:,} messages, unchanged "
                f"{result['unchanged']:,} sessions, "
                f"failed {result['failed']:,}."
            )
            completion_message = append_shadow_backup_completion(
                completion_message,
                shadow_backup_service=self._shadow_backup_service,
                state=self._state,
                config=self._config,
            )
            self._state.finish_success(completion_message)
            logger.info("Claude history sync finished successfully.", extra={"job_id": job_id, **result})
        except Exception as exc:  # pragma: no cover - depends on live browser state
            self._state.finish_error(summarize_claude_error_for_status(exc))
            logger.exception("Claude history sync failed.", extra={"job_id": job_id, "error": str(exc)})
        finally:
            self._finish_worker_job(token)
