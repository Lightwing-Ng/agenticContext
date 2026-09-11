"""Background service for the formal Zhihu text cache.

Code version: v1.1.0-codex.1
"""

from __future__ import annotations

import logging
from pathlib import Path
from threading import Event, RLock, Thread
from uuid import uuid4

from .config import LOCAL_STORE_ROOT, CrawlConfig
from .job_lock import CacheTaskLock, SHARED_CACHE_TASK_LOCK
from .logging_setup import reset_job_id, set_job_id
from .shadow_backup import ShadowBackupService
from .state import TaskState
from .zhihu_history import sync_zhihu_history


logger = logging.getLogger(__name__)


def _summarize_error_for_status(error: Exception) -> str:
    """Return a concise status error while the full traceback stays in logs."""

    text = str(error).strip()
    first_line = text.splitlines()[0] if text else error.__class__.__name__
    return first_line if len(first_line) <= 500 else f"{first_line[:497]}..."


class ZhihuHistoryService:
    """Own one cooperative Zhihu history worker."""

    def __init__(
        self,
        state: TaskState,
        local_store_root: Path | str = LOCAL_STORE_ROOT,
        task_lock: CacheTaskLock | None = None,
        shadow_backup_service: ShadowBackupService | None = None,
    ) -> None:
        self._state = state
        self._local_store_root = Path(local_store_root)
        self._task_lock = task_lock or SHARED_CACHE_TASK_LOCK
        self._shadow_backup_service = shadow_backup_service
        self._worker: Thread | None = None
        self._stop_requested = Event()
        self._lifecycle_lock = RLock()
        self._owns_task_lock = False
        self._config = CrawlConfig()
        self._author_url = ""

    def is_running(self) -> bool:
        """Return whether the Zhihu history worker is active."""

        return bool(self._state.snapshot()["running"])

    def start(self, config: CrawlConfig, *, author_url: str = "") -> None:
        """Start one signed-in vote-up or author-answer cache run."""

        normalized_author_url = str(author_url or "").strip()
        with self._lifecycle_lock:
            if self.is_running():
                raise RuntimeError("A Zhihu answer cache is already running.")
            if not self._task_lock.acquire("zhihu-history-sync"):
                raise RuntimeError(
                    "A cache task is already running in another window or browser. "
                    "Stop it there before starting the Zhihu answer cache."
                )
            self._owns_task_lock = True
            self._config = config
            self._author_url = normalized_author_url
            self._stop_requested.clear()
            self._state.reset_for_run()
            try:
                self._worker = Thread(target=self._run, daemon=True)
                self._worker.start()
            except Exception:
                self._owns_task_lock = False
                self._task_lock.release()
                raise

    def request_stop(self) -> bool:
        """Request a cooperative stop before the final atomic write."""

        if not self.is_running():
            return False
        self._stop_requested.set()
        self._state.update(phase="stopping")
        self._state.append_event(
            "Stop requested. The existing Zhihu text cache will remain available."
        )
        return True

    def _run(self) -> None:
        job_id = uuid4().hex[:12]
        token = set_job_id(job_id)
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
            if self._shadow_backup_service is not None:
                backup_message = self._shadow_backup_service.sync_after_cache_task(
                    self._config
                )
                if backup_message:
                    self._state.append_event(backup_message)
                    completion_message = f"{completion_message} {backup_message}"
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
            reset_job_id(token)
            with self._lifecycle_lock:
                if self._owns_task_lock:
                    self._owns_task_lock = False
                    self._task_lock.release()
