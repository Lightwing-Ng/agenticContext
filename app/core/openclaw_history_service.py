"""Read preserved OpenClaw transcripts without depending on its installed runtime.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import logging
from pathlib import Path
from threading import Thread
from typing import Any

from .cache_service_support import (
    CooperativeCacheWorker,
    append_shadow_backup_completion,
    summarize_status_error,
)
from .cache_task_coordinator import CacheTaskCoordinator, CacheTaskIdentity
from .config import LOCAL_STORE_ROOT, CrawlConfig
from .job_lock import CacheTaskLock
from .openclaw_history import (
    OPENCLAW_SOURCE_LABELS,
    import_openclaw_history,
    openclaw_history_path,
)
from .resource_persistence import read_parquet_rows
from .shadow_backup import ShadowBackupService
from .state import TaskSnapshot, TaskState


logger = logging.getLogger(__name__)


def build_openclaw_initial_snapshot(
    version: str,
    local_store_root: Path | str = LOCAL_STORE_ROOT,
    *,
    source: str,
) -> TaskSnapshot:
    """Hydrate one provider's archive independently of its browser history."""
    label = OPENCLAW_SOURCE_LABELS[source]
    path = openclaw_history_path(local_store_root, source)
    snapshot = TaskSnapshot(
        version=version,
        account_name=label,
        output_dir=str(path.parent),
        progress_unit="sessions",
        performance_metrics={"content_mode": "text", "source_kind": "archive"},
    )
    rows = read_parquet_rows(path)
    if rows is None and path.exists():
        snapshot.last_error = f"{label} history Parquet is unreadable: {path}"
        snapshot.message = snapshot.last_error
        return snapshot
    messages = {
        str(row["message_key"]): row
        for row in rows or []
        if row.get("message_key") and row.get("platform") == source
    }
    sessions = len({str(row.get("conversation_id") or "") for row in messages.values()})
    snapshot.discovered_tweets = sessions
    snapshot.queued_tweets = sessions
    snapshot.processed_tweets = sessions
    snapshot.downloaded_posts = sessions
    snapshot.downloaded_tweets = len(messages)
    snapshot.discovered_images = len(messages)
    snapshot.message = (
        f"Ready. Found preserved {label} history: {sessions:,} sessions, "
        f"{len(messages):,} messages."
    )
    return snapshot


class OpenClawHistoryService(CooperativeCacheWorker):
    """Import one attributed provider from the project's preserved snapshot."""

    def __init__(
        self,
        state: TaskState,
        local_store_root: Path | str = LOCAL_STORE_ROOT,
        task_lock: CacheTaskLock | None = None,
        shadow_backup_service: ShadowBackupService | None = None,
        *,
        source: str,
        task: CacheTaskIdentity | None = None,
        coordinator: CacheTaskCoordinator | None = None,
    ) -> None:
        if source not in OPENCLAW_SOURCE_LABELS:
            raise ValueError("Unsupported OpenClaw archive source.")
        super().__init__(state, task_lock, task=task, coordinator=coordinator)
        self._local_store_root = Path(local_store_root)
        self._source = source
        self._label = OPENCLAW_SOURCE_LABELS[source]
        self._shadow_backup_service = shadow_backup_service
        self._config = CrawlConfig()

    def start(self, config: CrawlConfig, content_mode: str = "text") -> None:
        """Read the archived source only, without probing OpenClaw or a browser."""
        if content_mode != "text":
            raise ValueError("OpenClaw archives support text history only.")

        def prepare() -> None:
            self._config = config

        self._start_worker(
            lock_owner=f"{self._source}-archive-import",
            already_running_message=f"A {self._label} import is already running.",
            lock_busy_message="Another Cache window is reading this OpenClaw archive.",
            target=self._run,
            prepare=prepare,
            thread_factory=Thread,
            browser="",
        )

    def request_stop(self) -> bool:
        """Stop at a safe transcript boundary and retain the existing cache."""
        return self._request_stop(
            f"Stop requested for {self._label}. Finishing the current transcript."
        )

    def _update_progress(self, progress: dict[str, Any]) -> None:
        """Expose counts without disclosing transcript text or credentials."""
        metrics = dict(self._state.snapshot()["performance_metrics"])
        metrics.update({
            key: value
            for key, value in progress.items()
            if key in {
                "sessions", "messages", "imported_sessions", "imported_messages",
                "files_total", "files_scanned", "failed", "stopped",
            }
        })
        self._state.update(
            performance_metrics=metrics,
            discovered_tweets=int(metrics.get("files_total", 0)),
            queued_tweets=int(metrics.get("files_total", 0)),
            processed_tweets=int(metrics.get("files_scanned", 0)),
            failed_tweets=int(metrics.get("failed", 0)),
        )

    def _refresh_cache_totals(self) -> None:
        snapshot = build_openclaw_initial_snapshot(
            self._state.snapshot()["version"], self._local_store_root, source=self._source,
        )
        self._state.update(
            downloaded_posts=snapshot.downloaded_posts,
            downloaded_tweets=snapshot.downloaded_tweets,
            discovered_images=snapshot.discovered_images,
        )

    def _run(self) -> None:
        job_id, token = self._begin_worker_job()
        self._state.update(
            phase="importing",
            account_name=self._label,
            output_dir=str(openclaw_history_path(self._local_store_root, self._source).parent),
            progress_unit="sessions",
            performance_metrics={"content_mode": "text", "source_kind": "archive"},
        )
        try:
            if self._is_stop_requested():
                self._refresh_cache_totals()
                self._state.finish_stopped(f"{self._label} import stopped before reading archives.")
                return
            result = import_openclaw_history(
                self._local_store_root,
                source=self._source,
                should_stop=self._is_stop_requested,
                on_progress=self._update_progress,
            )
            self._update_progress(result)
            self._refresh_cache_totals()
            self._state.update(discovery_complete=not result["stopped"] and not result["failed"])
            if result["stopped"]:
                self._state.finish_stopped(f"{self._label} import stopped. Cached history was preserved.")
            elif result["failed"]:
                self._state.finish_error(
                    f"{self._label} import is incomplete: {result['failed']:,} transcript failures."
                )
            else:
                completion = (
                    f"Finished {self._label} archive import. Read "
                    f"{result['imported_sessions']:,} sessions and "
                    f"{result['imported_messages']:,} messages."
                )
                self._state.finish_success(append_shadow_backup_completion(
                    completion,
                    shadow_backup_service=self._shadow_backup_service,
                    state=self._state,
                    config=self._config,
                    coordinator=self._coordinator,
                    task=self._task,
                ))
        except Exception as exc:
            self._refresh_cache_totals()
            self._state.finish_error(summarize_status_error(exc))
            logger.exception("OpenClaw archive import failed.", extra={"job_id": job_id})
        finally:
            self._finish_worker_job(token)
