"""Background service for the formal Zhihu text cache.

Code version: v1.1.1-codex.1
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
from .job_lock import CacheTaskLock
from .shadow_backup import ShadowBackupService
from .state import TaskState
from .zhihu_history import sync_zhihu_history


logger = logging.getLogger(__name__)


def _summarize_error_for_status(error: Exception) -> str:
    """Return a concise status error while the full traceback stays in logs."""

    return summarize_status_error(error)


class ZhihuHistoryService(CooperativeCacheWorker):
    """Own one cooperative Zhihu history worker."""

    def __init__(
        self,
        state: TaskState,
        local_store_root: Path | str = LOCAL_STORE_ROOT,
        task_lock: CacheTaskLock | None = None,
        shadow_backup_service: ShadowBackupService | None = None,
    ) -> None:
        super().__init__(state, task_lock)
        self._local_store_root = Path(local_store_root)
        self._shadow_backup_service = shadow_backup_service
        self._config = CrawlConfig()
        self._author_url = ""

    def start(self, config: CrawlConfig, *, author_url: str = "") -> None:
        """Start one signed-in vote-up or author-answer cache run."""

        normalized_author_url = str(author_url or "").strip()
        def prepare() -> None:
            self._config = config
            self._author_url = normalized_author_url

        self._start_worker(
            lock_owner="zhihu-history-sync",
            already_running_message="A Zhihu answer cache is already running.",
            lock_busy_message=(
                "A cache task is already running in another window or browser. "
                "Stop it there before starting the Zhihu answer cache."
            ),
            target=self._run,
            prepare=prepare,
            thread_factory=Thread,
        )

    def request_stop(self) -> bool:
        """Request a cooperative stop before the final atomic write."""

        return self._request_stop(
            "Stop requested. The existing Zhihu text cache will remain available."
        )

    def _run(self) -> None:
        job_id, token = self._begin_worker_job()
        try:
            result = sync_zhihu_history(
                self._state,
                self._config,
                self._stop_requested.is_set,
                self._local_store_root,
                author_url=self._author_url,
            )
            if result["stopped"]:
                self._state.finish_stopped(
                    f"Zhihu answer cache stopped after reading {result['processed_answers']:,} answers. "
                    "The existing text cache was preserved."
                )
                return
            completion_message = (
                f"Finished Zhihu answer cache. Read {result['processed_answers']:,} answers across "
                f"{result['pages_processed']:,} pages; added {result['added']:,}, changed "
                f"{result['changed']:,}, unchanged {result['unchanged']:,}; cached total "
                f"{result['cached_answers']:,}."
            )
            completion_message = append_shadow_backup_completion(
                completion_message,
                shadow_backup_service=self._shadow_backup_service,
                state=self._state,
                config=self._config,
            )
            self._state.finish_success(completion_message)
            logger.info(
                "Zhihu history sync finished successfully.",
                extra={
                    "job_id": job_id,
                    "collection_mode": result["collection_mode"],
                    "processed_answers": result["processed_answers"],
                    "cached_answers": result["cached_answers"],
                },
            )
        except Exception as exc:  # pragma: no cover - live browser failures are environment-specific
            if self._stop_requested.is_set():
                self._state.finish_stopped(
                    "Zhihu answer cache stopped. The existing text cache was preserved."
                )
            else:
                self._state.finish_error(_summarize_error_for_status(exc))
            logger.exception(
                "Zhihu history sync failed.",
                extra={"job_id": job_id, "error": str(exc)},
            )
        finally:
            self._finish_worker_job(token)
