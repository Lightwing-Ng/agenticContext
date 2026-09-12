"""Background service for Grok media sync."""

# Code version: v1.3.1-codex.1

from __future__ import annotations

import logging
from threading import Thread

from .cache_service_support import (
    CooperativeCacheWorker,
    append_shadow_backup_completion,
    summarize_status_error,
)
from .config import CrawlConfig
from .grok_downloader import sync_grok_media
from .job_lock import CacheTaskLock
from .shadow_backup import ShadowBackupService
from .state import TaskState


logger = logging.getLogger(__name__)


def summarize_error_for_status(error: Exception) -> str:
    """Return a compact status message while logs retain full exception details."""
    return summarize_status_error(
        error,
        launch_message=(
            "Grok browser automation failed while launching the selected browser profile. "
            "The full Playwright output was written to the log."
        ),
    )


class GrokDownloadService(CooperativeCacheWorker):
    """Manage a single Grok sync worker."""

    def __init__(
        self,
        state: TaskState,
        task_lock: CacheTaskLock | None = None,
        shadow_backup_service: ShadowBackupService | None = None,
    ) -> None:
        super().__init__(state, task_lock)
        self._config = CrawlConfig()
        self._shadow_backup_service = shadow_backup_service

    def start(self, config: CrawlConfig) -> None:
        """Start a new Grok sync worker."""
        def prepare() -> None:
            self._config = config

        self._start_worker(
            lock_owner="grok-sync",
            already_running_message="A Grok sync is already running.",
            lock_busy_message=(
                "A cache task is already running in another Cache Likes window or browser. "
                "Stop it there before starting a Grok sync."
            ),
            target=self._run,
            prepare=prepare,
            thread_factory=Thread,
        )

    def request_stop(self) -> bool:
        """Request cooperative stop for the active Grok sync."""
        return self._request_stop(
            "Emergency stop requested for Grok sync. Waiting for the current task to stop."
        )

    def _run(self) -> None:
        """Execute the Grok sync pipeline."""
        job_id, token = self._begin_worker_job()
        try:
            logger.info(
                "Grok sync started.",
                extra={
                    "job_id": job_id,
                    "grok_browser": self._config.grok_browser,
                },
            )
            if self._is_stop_requested():
                self._state.finish_stopped("Grok sync stopped before the browser was launched.")
                return

            result = sync_grok_media(self._state, config=self._config, should_stop=self._is_stop_requested)
            if result.stopped:
                self._state.finish_stopped(
                    f"Grok sync stopped. Cached {result.cached_count} assets "
                    f"({result.cached_images} images, {result.cached_videos} videos)."
                )
                logger.info(
                    "Grok sync stopped by operator.",
                    extra={
                        "job_id": job_id,
                        "cached_count": result.cached_count,
                        "cached_images": result.cached_images,
                        "cached_videos": result.cached_videos,
                    },
                )
                return

            completion_message = (
                f"Finished Grok sync. Discovered {result.discovered_count} assets, "
                f"added {result.downloaded_count} new files ({result.downloaded_images} images, "
                f"{result.downloaded_videos} videos), deduped {result.deduped_by_hash}, "
                f"skipped over size limit {result.skipped_size}, "
                f"failed {result.failed_count}, "
                f"cached total {result.cached_count} assets."
            )
            completion_message = append_shadow_backup_completion(
                completion_message,
                shadow_backup_service=self._shadow_backup_service,
                state=self._state,
                config=self._config,
            )
            self._state.finish_success(completion_message)
            logger.info(
                "Grok sync finished successfully.",
                extra={
                    "job_id": job_id,
                    "discovered_count": result.discovered_count,
                    "downloaded_count": result.downloaded_count,
                    "downloaded_images": result.downloaded_images,
                    "downloaded_videos": result.downloaded_videos,
                    "deduped_by_hash": result.deduped_by_hash,
                    "failed_count": result.failed_count,
                    "cached_count": result.cached_count,
                },
            )
        except Exception as exc:  # pragma: no cover
            self._state.finish_error(summarize_error_for_status(exc))
            logger.exception(
                "Grok sync failed.",
                extra={
                    "job_id": job_id,
                    "error": str(exc),
                },
            )
        finally:
            self._finish_worker_job(token)
