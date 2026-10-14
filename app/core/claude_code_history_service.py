"""Background import of local Claude Code transcripts into their own cache."""

# Code version: v1.0.0-codex.0

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
from .claude_code_history import claude_code_history_path, import_claude_code_history
from .config import LOCAL_STORE_ROOT, CrawlConfig
from .job_lock import CacheTaskLock
from .resource_persistence import read_parquet_rows
from .shadow_backup import ShadowBackupService
from .state import TaskSnapshot, TaskState


logger = logging.getLogger(__name__)


def build_claude_code_initial_snapshot(
    version: str,
    local_store_root: Path | str = LOCAL_STORE_ROOT,
) -> TaskSnapshot:
    """Read Claude Code cache totals independently of Claude web chat history."""
    history_path = claude_code_history_path(local_store_root)
    snapshot = TaskSnapshot(
        version=version,
        account_name="Claude Code",
        output_dir=str(history_path.parent),
        progress_unit="sessions",
        performance_metrics={"content_mode": "text", "source_kind": "local"},
    )
    rows = read_parquet_rows(history_path)
    if rows is None and history_path.exists():
        snapshot.last_error = f"Claude Code history Parquet is unreadable: {history_path}"
        snapshot.message = snapshot.last_error
        return snapshot
    messages = {
        str(row["message_key"]): row
        for row in rows or []
        if row.get("message_key") and row.get("platform") == "claude_code"
    }
    sessions = len({str(row.get("conversation_id") or "") for row in messages.values()})
    snapshot.discovered_tweets = sessions
    snapshot.queued_tweets = sessions
    snapshot.processed_tweets = sessions
    snapshot.downloaded_posts = sessions
    snapshot.downloaded_tweets = len(messages)
    snapshot.discovered_images = len(messages)
    snapshot.message = (
        f"Ready. Found existing Claude Code history: {sessions:,} sessions, "
        f"{len(messages):,} messages."
    )
    return snapshot


class ClaudeCodeHistoryService(CooperativeCacheWorker):
    """Run a local text import without acquiring browser resources."""

    def __init__(
        self,
        state: TaskState,
        local_store_root: Path | str = LOCAL_STORE_ROOT,
        task_lock: CacheTaskLock | None = None,
        shadow_backup_service: ShadowBackupService | None = None,
        *,
        task: CacheTaskIdentity | None = None,
        coordinator: CacheTaskCoordinator | None = None,
    ) -> None:
        super().__init__(state, task_lock, task=task, coordinator=coordinator)
        self._local_store_root = Path(local_store_root)
        self._config = CrawlConfig()
        self._shadow_backup_service = shadow_backup_service

    def start(self, config: CrawlConfig, content_mode: str = "text") -> None:
        """Start the local text task with the shared queue and cooperative lifecycle."""
        if content_mode != "text":
            raise ValueError("Claude Code supports local text history only.")

        def prepare() -> None:
            self._config = config

        self._start_worker(
            lock_owner="claude-code-history-import",
            already_running_message="A Claude Code history import is already running.",
            lock_busy_message=(
                "A cache task is already running in another Cache window. "
                "Stop it there before importing Claude Code history."
            ),
            target=self._run,
            prepare=prepare,
            thread_factory=Thread,
            browser="",
        )

    def request_stop(self) -> bool:
        """Stop at the next safe import boundary while preserving cached history."""
        return self._request_stop(
            "Stop requested for Claude Code. Waiting for the current transcript to finish."
        )

    def _update_progress(self, progress: dict[str, Any]) -> None:
        """Expose file progress without including private transcript contents."""
        metrics = dict(self._state.snapshot()["performance_metrics"])
        metrics.update({
            key: value
            for key, value in progress.items()
            if key in {
                "sessions", "messages", "added_or_changed", "unchanged_messages",
                "unchanged_sessions", "failed", "stopped", "source_directory",
                "transcript_directory", "files_scanned", "files_total", "applied",
                "imported_sessions", "imported_messages",
            }
        })
        self._state.update(
            performance_metrics=metrics,
            discovered_tweets=int(metrics.get("files_total", 0)),
            queued_tweets=int(metrics.get("files_total", 0)),
            processed_tweets=int(metrics.get("files_scanned", 0)),
            skipped_tweets=int(metrics.get("unchanged_sessions", 0)),
            failed_tweets=int(metrics.get("failed", 0)),
        )

    def _refresh_cache_totals(self) -> None:
        """Read durable totals after success, interruption, or a partial failure."""
        snapshot = build_claude_code_initial_snapshot(
            self._state.snapshot()["version"], self._local_store_root
        )
        self._state.update(
            downloaded_posts=snapshot.downloaded_posts,
            downloaded_tweets=snapshot.downloaded_tweets,
            discovered_images=snapshot.discovered_images,
        )

    def _run(self) -> None:
        """Import the configured source and keep all browser state untouched."""
        job_id, token = self._begin_worker_job()
        self._state.update(
            phase="importing",
            account_name="Claude Code",
            output_dir=str(claude_code_history_path(self._local_store_root).parent),
            progress_unit="sessions",
            performance_metrics={
                "content_mode": "text",
                "source_kind": "local",
                "source_directory": str(self._config.claude_code_directory),
            },
        )
        try:
            if self._is_stop_requested():
                self._refresh_cache_totals()
                self._state.finish_stopped("Claude Code import stopped before reading transcripts.")
                return
            result = import_claude_code_history(
                self._local_store_root,
                self._config.claude_code_directory,
                should_stop=self._is_stop_requested,
                on_progress=self._update_progress,
            )
            self._update_progress(result)
            self._refresh_cache_totals()
            self._state.update(
                failed_tweets=result["failed"],
                discovery_complete=not result["stopped"],
            )
            if result["stopped"]:
                self._state.finish_stopped(
                    "Claude Code import stopped. Cached history was preserved; "
                    f"inspected {result['files_scanned']:,} transcripts."
                )
                return
            if result["failed"]:
                self._state.finish_error(
                    "Claude Code import is incomplete. Cached history was preserved; "
                    f"{result['failed']:,} transcripts failed."
                )
                return
            completion_message = (
                f"Finished Claude Code import. Read {result['imported_sessions']:,} sessions and "
                f"{result['imported_messages']:,} messages; added or changed "
                f"{result['added_or_changed']:,} messages. The local cache contains "
                f"{result['sessions']:,} sessions and {result['messages']:,} messages. "
                f"Source: {result['source_directory']}."
            )
            completion_message = append_shadow_backup_completion(
                completion_message,
                shadow_backup_service=self._shadow_backup_service,
                state=self._state,
                config=self._config,
                coordinator=self._coordinator,
                task=self._task,
            )
            self._state.finish_success(completion_message)
            logger.info("Claude Code history import finished.", extra={"job_id": job_id})
        except Exception as exc:
            self._refresh_cache_totals()
            self._state.finish_error(summarize_status_error(exc))
            logger.exception("Claude Code history import failed.", extra={"job_id": job_id})
        finally:
            self._finish_worker_job(token)
