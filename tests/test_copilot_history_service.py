"""Copilot background queue, stop, durability, and shadow backup regressions."""

# Code version: v1.2.0-codex.0

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.core.cache_task_coordinator import CacheTaskCoordinator, CacheTaskIdentity
from app.core.copilot_history_service import CopilotHistoryService
from app.core.copilot_history_store import CopilotHistoryStore, build_copilot_initial_snapshot
from app.core.copilot_sync_state import read_copilot_sync_state, write_copilot_sync_state
from app.core.job_lock import CacheTaskLock
from app.core.state import TaskSnapshot, TaskState


class ImmediateThread:
    def __init__(self, *, target, **_kwargs) -> None:
        self.target = target

    def start(self) -> None:
        self.target()


def _state() -> TaskState:
    return TaskState("test", snapshot_factory=lambda version: TaskSnapshot(version))


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        copilot_browser="safari",
        shadow_backup_enabled=True,
        shadow_backup_auto_sync=True,
    )


def _result(**overrides) -> dict:
    return {
        "sessions": 1,
        "messages": 1,
        "added_or_changed": 1,
        "unchanged": 0,
        "failed": 0,
        "stopped": False,
        "discovery_complete": True,
        "sync_complete": True,
        "expected_sessions": 1,
        "discovered_sessions": 1,
        "completion_basis": "Fixture complete index with exhausted continuation.",
        **overrides,
    }


def _persist(root: Path) -> Path:
    store = CopilotHistoryStore(root)
    store.replace_conversation(
        {"conversation_id": "session-1", "title": "Fixture session"},
        [{"role": "user", "content_text": "Fixture message."}],
    )
    return store.path


def test_success_uses_configured_browser_refreshes_durable_totals_and_runs_backup(tmp_path: Path) -> None:
    state = _state()
    lock = CacheTaskLock(tmp_path / "task.lock")
    backup = Mock()
    backup.sync_after_cache_task.return_value = "Shadow backup complete."
    service = CopilotHistoryService(state, tmp_path, lock, backup)
    config = _config()

    def collect(actual_state, actual_config, should_stop, root):
        assert actual_state is state
        assert actual_config is config
        assert not should_stop()
        assert root == tmp_path
        _persist(root)
        return _result()

    with patch("app.core.copilot_history_service.Thread", ImmediateThread), patch(
        "app.core.copilot_history_service.sync_copilot_history", side_effect=collect
    ):
        service.start(config)
    snapshot = state.snapshot()
    assert snapshot["phase"] == "finished"
    assert snapshot["downloaded_posts"] == snapshot["downloaded_tweets"] == 1
    assert snapshot["performance_metrics"]["content_mode"] == "text"
    assert snapshot["performance_metrics"]["added_or_changed"] == 1
    assert "Shadow backup complete." in snapshot["message"]
    assert read_copilot_sync_state(tmp_path)["sync_complete"] is True
    assert build_copilot_initial_snapshot("reloaded", tmp_path).phase == "finished"
    backup.sync_after_cache_task.assert_called_once_with(config)
    assert lock.acquire("after-success")
    lock.release()


@pytest.mark.parametrize("stopped,failed,phase", [(True, 0, "stopped"), (False, 1, "failed")])
def test_stopped_or_failed_collection_retains_rows_and_skips_backup(
    tmp_path: Path, stopped: bool, failed: int, phase: str
) -> None:
    path = _persist(tmp_path)
    before = path.read_bytes()
    state = _state()
    backup = Mock()
    lock = CacheTaskLock(tmp_path / "task.lock")
    service = CopilotHistoryService(state, tmp_path, lock, backup)
    with patch("app.core.copilot_history_service.Thread", ImmediateThread), patch(
        "app.core.copilot_history_service.sync_copilot_history",
        return_value=_result(stopped=stopped, failed=failed),
    ):
        service.start(_config())
    assert state.snapshot()["phase"] == phase
    assert state.snapshot()["downloaded_tweets"] == 1
    assert path.read_bytes() == before
    assert read_copilot_sync_state(tmp_path)["sync_complete"] is False
    backup.sync_after_cache_task.assert_not_called()
    assert lock.acquire("after-incomplete")
    lock.release()


def test_collection_exception_releases_lock_and_retains_durable_totals(tmp_path: Path) -> None:
    path = _persist(tmp_path)
    before = path.read_bytes()
    state = _state()
    lock = CacheTaskLock(tmp_path / "task.lock")
    service = CopilotHistoryService(state, tmp_path, lock)
    with patch("app.core.copilot_history_service.Thread", ImmediateThread), patch(
        "app.core.copilot_history_service.sync_copilot_history",
        side_effect=RuntimeError("Fixture browser unavailable.\nAdditional diagnostic detail."),
    ):
        service.start(_config())
    snapshot = state.snapshot()
    assert snapshot["phase"] == "failed"
    assert snapshot["last_error"] == "Fixture browser unavailable."
    assert snapshot["downloaded_tweets"] == 1
    assert read_copilot_sync_state(tmp_path)["status"] == "failed"
    assert path.read_bytes() == before
    assert lock.acquire("after-exception")
    lock.release()


def test_text_only_validation_precedes_worker_or_browser_launch(tmp_path: Path) -> None:
    service = CopilotHistoryService(_state(), tmp_path, CacheTaskLock(tmp_path / "task.lock"))
    with patch("app.core.copilot_history_service.sync_copilot_history") as collect:
        with pytest.raises(ValueError, match="text history only"):
            service.start(_config(), "media")
    collect.assert_not_called()
    assert not service.is_running()
    assert not service.request_stop()


def test_copilot_waits_for_selected_browser_and_can_leave_queue_without_launch(tmp_path: Path) -> None:
    coordinator = CacheTaskCoordinator(tmp_path / "locks")
    running = CacheTaskIdentity("chatgpt", "text", "ChatGPT Chat")
    coordinator.submit(
        running,
        browser="safari",
        launch=lambda _queued: None,
        on_queued=lambda _message: None,
        on_queue_update=lambda _message: None,
        on_launch_error=lambda _error: None,
        lock_busy_message="busy",
    )
    service = CopilotHistoryService(
        _state(),
        tmp_path,
        task=CacheTaskIdentity("copilot", "text", "Microsoft Copilot"),
        coordinator=coordinator,
    )
    try:
        with patch("app.core.copilot_history_service.Thread", ImmediateThread), patch(
            "app.core.copilot_history_service.sync_copilot_history"
        ) as collect:
            service.start(_config())
            assert service.is_queued()
            assert service.request_stop()
            assert not service.is_running()
            collect.assert_not_called()
    finally:
        coordinator.finish(running.key)


def test_stop_before_launch_does_not_load_browser_collector(tmp_path: Path) -> None:
    state = _state()
    service = CopilotHistoryService(state, tmp_path, CacheTaskLock(tmp_path / "task.lock"))

    class StoppedThread(ImmediateThread):
        def start(self) -> None:
            service._stop_requested.set()
            super().start()

    with patch("app.core.copilot_history_service.Thread", StoppedThread), patch(
        "app.core.copilot_history_service.sync_copilot_history"
    ) as collect:
        service.start(_config())
    assert state.snapshot()["phase"] == "stopped"
    assert read_copilot_sync_state(tmp_path)["status"] == "stopped"
    collect.assert_not_called()


@pytest.mark.parametrize("discovery_complete,sync_complete", [(False, False), (True, False), (False, True)])
def test_partial_discovery_never_finishes_or_runs_backup(
    tmp_path: Path, discovery_complete: bool, sync_complete: bool
) -> None:
    path = _persist(tmp_path)
    before = path.read_bytes()
    state = _state()
    backup = Mock()
    service = CopilotHistoryService(state, tmp_path, CacheTaskLock(tmp_path / "task.lock"), backup)
    with patch("app.core.copilot_history_service.Thread", ImmediateThread), patch(
        "app.core.copilot_history_service.sync_copilot_history",
        return_value=_result(
            sessions=17,
            discovered_sessions=17,
            expected_sessions=None,
            discovery_complete=discovery_complete,
            sync_complete=sync_complete,
        ),
    ):
        service.start(_config())
    assert state.snapshot()["phase"] == "failed"
    assert state.snapshot()["performance_metrics"]["sync_complete"] is False
    reloaded = build_copilot_initial_snapshot("reloaded", tmp_path)
    assert reloaded.phase == "failed"
    assert reloaded.processed_tweets == reloaded.queued_tweets == 17
    assert reloaded.performance_metrics["sync_complete"] is False
    assert path.read_bytes() == before
    backup.sync_after_cache_task.assert_not_called()


def test_legacy_collector_result_without_coverage_proof_is_incomplete(tmp_path: Path) -> None:
    result = {key: value for key, value in _result().items() if key in {
        "sessions", "messages", "added_or_changed", "unchanged", "failed", "stopped"
    }}
    state = _state()
    service = CopilotHistoryService(state, tmp_path, CacheTaskLock(tmp_path / "task.lock"))
    with patch("app.core.copilot_history_service.Thread", ImmediateThread), patch(
        "app.core.copilot_history_service.sync_copilot_history", return_value=result
    ):
        service.start(_config())
    assert state.snapshot()["phase"] == "failed"
    assert read_copilot_sync_state(tmp_path)["sync_complete"] is False


def test_incomplete_legacy_scope_reason_survives_service_checkpoint(tmp_path: Path) -> None:
    state = _state()
    reason = "The preserved Edge legacy directory contains 357 sessions. Full legacy transcript coverage remains unverified."
    service = CopilotHistoryService(state, tmp_path, CacheTaskLock(tmp_path / "task.lock"))
    with patch("app.core.copilot_history_service.Thread", ImmediateThread), patch(
        "app.core.copilot_history_service.sync_copilot_history",
        return_value=_result(
            sessions=17, discovered_sessions=357, discovery_complete=False, sync_complete=False,
            expected_sessions=None, scope_reason=reason,
        ),
    ):
        service.start(_config())
    coverage = read_copilot_sync_state(tmp_path)
    assert coverage["discovered_sessions"] == 357 and not coverage["sync_complete"]
    assert reason in coverage["reason"] and reason in state.snapshot()["message"]


def test_unexpected_error_retains_committed_checkpoint_counts(tmp_path: Path) -> None:
    path = _persist(tmp_path)
    before = path.read_bytes()
    state = _state()
    service = CopilotHistoryService(state, tmp_path, CacheTaskLock(tmp_path / "task.lock"))

    def fail_after_checkpoint(_state, _config, _should_stop, root):
        write_copilot_sync_state(root, {
            "status": "running",
            "discovered_sessions": 500,
            "processed_sessions": 17,
            "expected_sessions": 500,
        })
        raise RuntimeError("Fixture interruption after durable checkpoint.")

    with patch("app.core.copilot_history_service.Thread", ImmediateThread), patch(
        "app.core.copilot_history_service.sync_copilot_history", side_effect=fail_after_checkpoint
    ):
        service.start(_config())
    coverage = read_copilot_sync_state(tmp_path)
    assert coverage["status"] == "failed"
    assert coverage["discovered_sessions"] == 500 and coverage["processed_sessions"] == 17
    assert coverage["expected_sessions"] == 500 and not coverage["sync_complete"]
    reloaded = build_copilot_initial_snapshot("reloaded", tmp_path)
    assert reloaded.phase == "failed" and reloaded.processed_tweets == 17
    assert path.read_bytes() == before


def test_stop_result_cannot_erase_committed_checkpoint_counts(tmp_path: Path) -> None:
    state = _state()
    service = CopilotHistoryService(state, tmp_path, CacheTaskLock(tmp_path / "task.lock"))

    def stop_after_checkpoint(_state, _config, _should_stop, root):
        write_copilot_sync_state(root, {
            "status": "running",
            "discovered_sessions": 500,
            "processed_sessions": 17,
            "expected_sessions": 500,
        })
        return _result(
            sessions=0,
            discovered_sessions=0,
            expected_sessions=None,
            stopped=True,
            discovery_complete=False,
            sync_complete=False,
        )

    with patch("app.core.copilot_history_service.Thread", ImmediateThread), patch(
        "app.core.copilot_history_service.sync_copilot_history", side_effect=stop_after_checkpoint
    ):
        service.start(_config())
    coverage = read_copilot_sync_state(tmp_path)
    assert coverage["status"] == "stopped"
    assert coverage["discovered_sessions"] == 500 and coverage["processed_sessions"] == 17
    assert coverage["expected_sessions"] == 500
    assert not coverage["sync_complete"]
