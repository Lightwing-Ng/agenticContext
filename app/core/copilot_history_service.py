"""Background lifecycle for Microsoft Copilot's authenticated browser history."""

# Code version: v1.2.1-codex.0

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
from .copilot_history_store import build_copilot_initial_snapshot
from .copilot_sync_state import read_copilot_sync_state, write_copilot_sync_state
from .job_lock import CacheTaskLock
from .shadow_backup import ShadowBackupService
from .state import TaskState, utc_now


logger = logging.getLogger(__name__)


def sync_copilot_history(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Load browser collection only when its worker actually runs."""

    from .copilot_history import sync_copilot_history as collect_history

    return collect_history(*args, **kwargs)


class CopilotHistoryService(CooperativeCacheWorker):
    """Run Copilot text collection through the shared queue and browser lease."""

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
        """Start one text worker using the selected authorized browser."""

        if content_mode != "text":
            raise ValueError("Microsoft Copilot supports text history only.")

        def prepare() -> None:
            self._config = config

        self._start_worker(
            lock_owner="copilot-history-sync",
            already_running_message="A Microsoft Copilot history sync is already running.",
            lock_busy_message=(
                "A cache task is already running in another Cache window. "
                "Stop it there before starting Microsoft Copilot history sync."
            ),
            target=self._run,
            prepare=prepare,
            thread_factory=Thread,
            browser=config.copilot_browser,
        )

    def request_stop(self) -> bool:
        """Stop after the active session while retaining durable cached history."""

        return self._request_stop(
            "Stop requested for Microsoft Copilot history sync. "
            "Waiting for the current session to finish."
        )

    def _refresh_cache_totals(self) -> None:
        snapshot = build_copilot_initial_snapshot(
            self._state.snapshot()["version"], self._local_store_root
        )
        self._state.update(
            downloaded_posts=snapshot.downloaded_posts,
            downloaded_tweets=snapshot.downloaded_tweets,
            discovered_images=snapshot.discovered_images,
        )

    def _persist_outcome(
        self,
        status: str,
        reason: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        """Retain committed checkpoint counts when stopping or unwinding an error."""

        coverage = read_copilot_sync_state(self._local_store_root)
        coverage.update(status=status, sync_complete=False, finished_at=utc_now(), reason=reason)
        if result is not None:
            discovered_sessions = int(
                result.get("discovered_sessions", max(coverage["discovered_sessions"], int(result["sessions"])))
            )
            processed_sessions = int(result["sessions"])
            failed_sessions = int(result["failed"])
            if status != "complete":
                discovered_sessions = max(discovered_sessions, coverage["discovered_sessions"])
                processed_sessions = max(processed_sessions, coverage["processed_sessions"])
                failed_sessions = max(failed_sessions, coverage["failed_sessions"])
            coverage.update(
                discovery_complete=result.get("discovery_complete") is True,
                sync_complete=status == "complete" and result.get("sync_complete") is True,
                expected_sessions=(
                    result["expected_sessions"]
                    if result.get("expected_sessions") is not None
                    else coverage["expected_sessions"]
                ),
                discovered_sessions=discovered_sessions,
                processed_sessions=processed_sessions,
                failed_sessions=failed_sessions,
                completion_basis=str(result.get("completion_basis") or coverage["completion_basis"]),
            )
        write_copilot_sync_state(self._local_store_root, coverage)
        snapshot = self._state.snapshot()
        self._state.update(
            discovery_complete=coverage["discovery_complete"],
            discovered_tweets=coverage["discovered_sessions"],
            queued_tweets=coverage["discovered_sessions"],
            processed_tweets=coverage["processed_sessions"],
            failed_tweets=coverage["failed_sessions"],
            performance_metrics={**snapshot["performance_metrics"], **coverage},
        )

    def _finish_cancelled_browser_wait(self) -> None:
        """Persist a stopped coverage record when Safari admission is cancelled."""
        try:
            self._refresh_cache_totals()
            self._persist_outcome(
                "stopped", "Microsoft Copilot sync stopped while waiting for Safari."
            )
        except Exception:
            logger.exception("Microsoft Copilot stopped coverage could not be persisted.")
            self._state.update(
                last_error="Microsoft Copilot stopped, but its coverage state could not be saved."
            )
        finally:
            super()._finish_cancelled_browser_wait()

    def _run(self) -> None:
        job_id, token = self._begin_worker_job()
        try:
            write_copilot_sync_state(
                self._local_store_root,
                {
                    "status": "running",
                    "started_at": self._state.snapshot()["started_at"] or utc_now(),
                    "reason": "Full history completeness has not been verified for this sync.",
                },
            )
            self._state.update(
                account_name="Microsoft Copilot",
                output_dir=str(self._local_store_root / "llm" / "copilot"),
                progress_unit="sessions",
                performance_metrics={"content_mode": "text"},
            )
            self._refresh_cache_totals()
            if self._is_stop_requested():
                message = "Microsoft Copilot sync stopped before the browser was launched."
                self._persist_outcome("stopped", message)
                self._state.finish_stopped(message)
                return
            result = sync_copilot_history(
                self._state,
                self._config,
                self._is_stop_requested,
                self._local_store_root,
            )
            self._refresh_cache_totals()
            self._state.update(
                failed_tweets=int(result["failed"]),
                skipped_tweets=int(result["unchanged"]),
                performance_metrics={
                    "content_mode": "text",
                    **{
                        key: result[key]
                        for key in ("sessions", "messages", "added_or_changed", "unchanged", "failed", "stopped")
                    },
                },
            )
            if result["stopped"]:
                message = f"Microsoft Copilot history sync stopped. Cached {result['messages']:,} messages."
                self._persist_outcome("stopped", message, result)
                self._state.finish_stopped(message)
                return
            if (
                result["failed"]
                or result.get("discovery_complete") is not True
                or result.get("sync_complete") is not True
            ):
                reason = (
                    f"{result['failed']:,} sessions failed."
                    if result["failed"]
                    else str(result.get("scope_reason") or "Full history completeness has not been verified.")
                )
                message = f"Microsoft Copilot history sync is incomplete. Cached history was preserved; {reason}"
                self._persist_outcome("incomplete", message, result)
                self._state.finish_error(message)
                return
            completion_message = (
                f"Finished Microsoft Copilot history sync. Inspected {result['sessions']:,} sessions, "
                f"found {result['messages']:,} messages, added or changed "
                f"{result['added_or_changed']:,} messages, unchanged {result['unchanged']:,} sessions."
            )
            self._persist_outcome("complete", completion_message, result)
            completion_message = append_shadow_backup_completion(
                completion_message,
                shadow_backup_service=self._shadow_backup_service,
                state=self._state,
                config=self._config,
                coordinator=self._coordinator,
                task=self._task,
            )
            self._state.finish_success(completion_message)
            logger.info("Microsoft Copilot history sync finished.", extra={"job_id": job_id, **result})
        except Exception as exc:
            self._refresh_cache_totals()
            message = summarize_status_error(exc)
            try:
                self._persist_outcome("failed", message)
            except Exception:
                logger.exception("Microsoft Copilot sync coverage could not be persisted.")
            self._state.finish_error(message)
            logger.exception("Microsoft Copilot history sync failed.", extra={"job_id": job_id})
        finally:
            self._finish_worker_job(token)
