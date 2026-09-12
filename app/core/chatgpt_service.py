"""Background service for ChatGPT text and media sync."""

# Code version: v1.4.1-codex.1

from __future__ import annotations

import logging
from threading import Thread

from .cache_service_support import (
    CooperativeCacheWorker,
    append_shadow_backup_completion,
    summarize_status_error,
)
from .chatgpt_downloader import sync_chatgpt_images
from .config import CrawlConfig
from .job_lock import CacheTaskLock
from .shadow_backup import ShadowBackupService
from .state import TaskState


logger = logging.getLogger(__name__)


def summarize_chatgpt_error_for_status(error: Exception) -> str:
    """Return a concise ChatGPT status message while retaining full logs."""
    return summarize_status_error(
        error,
        launch_message=(
            "ChatGPT browser automation failed while launching the selected Edge profile. "
            "Close any duplicate cache window and check the local log for the full Playwright output."
        ),
    )


class ChatGPTDownloadService(CooperativeCacheWorker):
    """Manage one mode-specific ChatGPT cache worker."""

    def __init__(
        self,
        state: TaskState,
        task_lock: CacheTaskLock | None = None,
        shadow_backup_service: ShadowBackupService | None = None,
    ) -> None:
        super().__init__(state, task_lock)
        self._config = CrawlConfig()
        self._content_mode = "media"
        self._shadow_backup_service = shadow_backup_service

    def start(self, config: CrawlConfig, content_mode: str = "media") -> None:
        """Start a new ChatGPT text or media cache worker."""
        def prepare() -> None:
            self._config = config
            self._content_mode = "media" if content_mode == "media" else "text"

        self._start_worker(
            lock_owner="chatgpt-sync",
            already_running_message="A ChatGPT sync is already running.",
            lock_busy_message=(
                "A cache task is already running in another Cache Likes window or browser. "
                "Stop it there before starting a ChatGPT sync."
            ),
            target=self._run,
            prepare=prepare,
            thread_factory=Thread,
        )

    def request_stop(self) -> bool:
        """Request a cooperative stop for the active ChatGPT sync."""
        return self._request_stop(
            "Emergency stop requested for ChatGPT sync. "
            "Waiting for the current task to stop."
        )

    def _run(self) -> None:
        """Execute the selected ChatGPT cache pipeline."""
        job_id, token = self._begin_worker_job()
        try:
            logger.info(
                "ChatGPT sync started.",
                extra={
                    "job_id": job_id,
                    "chatgpt_browser": self._config.chatgpt_browser,
                    "chatgpt_project_name": self._config.chatgpt_project_name,
                    "chatgpt_content_mode": self._content_mode,
                },
            )
            if self._is_stop_requested():
                self._state.finish_stopped("ChatGPT sync stopped before the browser was launched.")
                return

            result = sync_chatgpt_images(
                self._state,
                config=self._config,
                should_stop=self._is_stop_requested,
                content_mode=self._content_mode,
            )
            if result.stopped:
                if self._content_mode == "text":
                    stopped_message = (
                        f"ChatGPT text sync stopped. Cached {result.cached_messages:,} text messages."
                    )
                else:
                    stopped_message = (
                        f"ChatGPT media sync stopped. Cached {result.cached_count:,} original images."
                    )
                self._state.finish_stopped(stopped_message)
                logger.info(
                    "ChatGPT sync stopped by operator.",
                    extra={
                        "job_id": job_id,
                        "discovered_conversations": result.discovered_conversations,
                        "discovered_images": result.discovered_images,
                        "downloaded_count": result.downloaded_count,
                        "cached_count": result.cached_count,
                    },
                )
                return

            if self._content_mode == "text" and result.incomplete:
                self._state.finish_error(
                    "ChatGPT text sync is incomplete. Cached messages were preserved; "
                    "retry to refresh the remaining sessions. See recent activity for details."
                )
                return

            if self._content_mode == "text":
                completion_message = (
                    f"Finished ChatGPT text sync. Inspected {result.discovered_conversations:,} sessions "
                    f"and cached {result.cached_messages:,} text messages."
                )
            else:
                completion_message = (
                    f"Finished ChatGPT media sync. Inspected {result.discovered_conversations:,} sessions, "
                    f"found {result.discovered_images:,} original images, added {result.downloaded_count:,} new files, "
                    f"skipped over size limit {result.skipped_size:,}, "
                    f"failed {result.failed_count:,}; cached total {result.cached_count:,} images."
                )
            completion_message = append_shadow_backup_completion(
                completion_message,
                shadow_backup_service=self._shadow_backup_service,
                state=self._state,
                config=self._config,
            )
            self._state.finish_success(completion_message)
            logger.info(
                "ChatGPT sync finished successfully.",
                extra={
                    "job_id": job_id,
                    "discovered_conversations": result.discovered_conversations,
                    "discovered_images": result.discovered_images,
                    "downloaded_count": result.downloaded_count,
                    "skipped_known": result.skipped_known,
                    "failed_count": result.failed_count,
                    "cached_count": result.cached_count,
                },
            )
        except Exception as exc:  # pragma: no cover - depends on live browser state
            self._state.finish_error(summarize_chatgpt_error_for_status(exc))
            logger.exception(
                "ChatGPT sync failed.",
                extra={
                    "job_id": job_id,
                    "error": str(exc),
                },
            )
        finally:
            self._finish_worker_job(token)
