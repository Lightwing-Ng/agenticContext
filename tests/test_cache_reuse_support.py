"""Regression tests for shared Cache lifecycle and history-row contracts."""

# Code version: v1.0.2-codex.1

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.cache_service_support import (
    CooperativeCacheWorker,
    append_shadow_backup_completion,
    summarize_status_error,
)
from app.core.chatgpt_service import ChatGPTDownloadService
from app.core.claude_history_service import ClaudeHistoryService
from app.core.config import CrawlConfig
from app.core.gemini_service import GeminiHistoryService
from app.core.grok_history_service import GrokHistoryService
from app.core.grok_service import GrokDownloadService
from app.core.history_rows import (
    history_rows_match,
    partition_conversation_rows,
    sort_history_rows,
)
from app.core.job_lock import CacheTaskLock
from app.core.resource_persistence import (
    CHATGPT_HISTORY_SCHEMA,
    CLAUDE_HISTORY_SCHEMA,
    GEMINI_HISTORY_SCHEMA,
    GROK_HISTORY_SCHEMA,
    ZHIHU_HISTORY_SCHEMA,
)
from app.core.service import CacheLikesService
from app.core.state import TaskSnapshot, TaskState
from app.core.zhihu_history_service import ZhihuHistoryService


def _state() -> TaskState:
    return TaskState("test", snapshot_factory=lambda version: TaskSnapshot(version))


class _FailingThread:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def start(self) -> None:
        raise RuntimeError("thread unavailable")


class _IdleThread:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def start(self) -> None:
        pass


class _BackupService:
    def sync_after_cache_task(self, config: CrawlConfig) -> str:
        assert config.download_workers == 3
        return "Shadow cloud backup copied 2 files."


class _DisabledBackupService:
    def sync_after_cache_task(self, _config: CrawlConfig) -> None:
        return None


def test_worker_start_failure_releases_lock_and_publishes_terminal_state(
    tmp_path: Path,
) -> None:
    task_lock = CacheTaskLock(tmp_path / "cache-task.lock")
    state = _state()
    worker = CooperativeCacheWorker(state, task_lock)

    with pytest.raises(RuntimeError, match="thread unavailable"):
        worker._start_worker(
            lock_owner="fixture",
            already_running_message="already running",
            lock_busy_message="busy",
            target=lambda: None,
            thread_factory=_FailingThread,
        )

    snapshot = state.snapshot()
    assert snapshot["running"] is False
    assert snapshot["phase"] == "failed"
    assert snapshot["last_error"] == "thread unavailable"
    assert task_lock.acquire("next-run")
    task_lock.release()


@pytest.mark.parametrize(
    "service_type",
    (
        CacheLikesService,
        ChatGPTDownloadService,
        GrokDownloadService,
        GrokHistoryService,
        GeminiHistoryService,
        ClaudeHistoryService,
        ZhihuHistoryService,
    ),
)
def test_every_cache_service_uses_recoverable_shared_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    service_type: type[CooperativeCacheWorker],
) -> None:
    task_lock = CacheTaskLock(tmp_path / f"{service_type.__name__}.lock")
    state = _state()
    service = service_type(state, task_lock=task_lock)
    monkeypatch.setattr(f"{service_type.__module__}.Thread", _FailingThread)

    with pytest.raises(RuntimeError, match="thread unavailable"):
        service.start(CrawlConfig())

    assert state.snapshot()["phase"] == "failed"
    assert task_lock.acquire("next-run")
    task_lock.release()


def test_worker_rejects_running_or_locked_start_without_stealing_lock(
    tmp_path: Path,
) -> None:
    task_lock = CacheTaskLock(tmp_path / "cache-task.lock")
    running_state = _state()
    running_state.reset_for_run()
    running_worker = CooperativeCacheWorker(running_state, task_lock)
    with pytest.raises(RuntimeError, match="already running"):
        running_worker._start_worker(
            lock_owner="fixture",
            already_running_message="already running",
            lock_busy_message="busy",
            target=lambda: None,
            thread_factory=_IdleThread,
        )

    assert task_lock.acquire("external-owner")
    idle_worker = CooperativeCacheWorker(_state(), task_lock)
    with pytest.raises(RuntimeError, match="busy"):
        idle_worker._start_worker(
            lock_owner="fixture",
            already_running_message="already running",
            lock_busy_message="busy",
            target=lambda: None,
            thread_factory=_IdleThread,
        )
    task_lock.release()


def test_worker_prepare_and_cooperative_stop_share_state_contract(
    tmp_path: Path,
) -> None:
    task_lock = CacheTaskLock(tmp_path / "cache-task.lock")
    state = _state()
    worker = CooperativeCacheWorker(state, task_lock)
    prepared: list[str] = []
    worker._start_worker(
        lock_owner="fixture",
        already_running_message="already running",
        lock_busy_message="busy",
        target=lambda: None,
        prepare=lambda: prepared.append("ready"),
        thread_factory=_IdleThread,
    )

    assert prepared == ["ready"]
    assert worker._request_stop("Stopping after the current item.") is True
    snapshot = state.snapshot()
    assert snapshot["phase"] == "stopping"
    assert snapshot["recent_events"][0].endswith("Stopping after the current item.")
    assert worker._is_stop_requested() is True
    worker._release_task_lock()

    idle_worker = CooperativeCacheWorker(_state(), task_lock)
    assert idle_worker._request_stop("unused") is False


def test_shared_error_summary_preserves_provider_copy_and_bounds_fallback() -> None:
    launch_message = "Provider profile launch failed."
    launch_error = RuntimeError("BrowserType.launch_persistent_context: locked")

    assert summarize_status_error(
        launch_error,
        launch_message=launch_message,
    ) == launch_message
    assert summarize_status_error(RuntimeError("first line\nsecond line")) == "first line"
    assert summarize_status_error(RuntimeError("x" * 600)).endswith("...")
    assert len(summarize_status_error(RuntimeError("x" * 600))) == 500


def test_shadow_backup_completion_appends_one_event() -> None:
    state = _state()
    config = CrawlConfig(download_workers=3)
    completion = append_shadow_backup_completion(
        "Finished provider sync.",
        shadow_backup_service=_BackupService(),
        state=state,
        config=config,
    )

    assert completion == (
        "Finished provider sync. Shadow cloud backup copied 2 files."
    )
    assert state.snapshot()["recent_events"][0].endswith(
        "Shadow cloud backup copied 2 files."
    )
    assert append_shadow_backup_completion(
        "Finished provider sync.",
        shadow_backup_service=None,
        state=state,
        config=config,
    ) == "Finished provider sync."
    assert append_shadow_backup_completion(
        "Finished provider sync.",
        shadow_backup_service=_DisabledBackupService(),
        state=state,
        config=config,
    ) == "Finished provider sync."


def test_history_schemas_share_one_ordered_message_contract() -> None:
    common_schemas = (
        GEMINI_HISTORY_SCHEMA,
        GROK_HISTORY_SCHEMA,
        CLAUDE_HISTORY_SCHEMA,
        ZHIHU_HISTORY_SCHEMA,
    )

    assert all(schema.equals(GEMINI_HISTORY_SCHEMA) for schema in common_schemas)
    assert CHATGPT_HISTORY_SCHEMA.names == [
        "schema_version",
        "provider_revision",
        *GEMINI_HISTORY_SCHEMA.names[1:],
    ]
    assert CHATGPT_HISTORY_SCHEMA.field("provider_revision").nullable is True


def test_history_row_helpers_partition_compare_and_sort_without_provider_rules() -> None:
    rows = {
        "b:1": {"conversation_id": "b", "message_index": 1, "content_text": "B"},
        "a:2": {"conversation_id": "a", "message_index": 2, "content_text": "A2"},
        "a:0": {"conversation_id": "a", "message_index": 0, "content_text": "A0"},
    }

    previous, retained = partition_conversation_rows(rows, "a")

    assert list(previous) == ["a:2", "a:0"]
    assert list(retained) == ["b:1"]
    assert [row["content_text"] for row in sort_history_rows(rows.values())] == [
        "A0",
        "A2",
        "B",
    ]
    assert history_rows_match(
        previous["a:0"],
        {**previous["a:0"], "ignored": True},
        ("conversation_id", "message_index", "content_text"),
    )
    assert not history_rows_match(
        previous["a:0"],
        {**previous["a:0"], "content_text": "changed"},
        ("conversation_id", "message_index", "content_text"),
    )
