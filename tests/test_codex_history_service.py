"""Local Codex configuration, lifecycle, and cache isolation regressions."""

# Code version: v1.0.0-codex.0

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from flask import Flask

from app.core.cache_task_coordinator import CacheTaskCoordinator, CacheTaskIdentity
from app.core.codex_history_service import (
    CodexHistoryService,
    build_codex_initial_snapshot,
)
from app.core.config import (
    CrawlConfig,
    load_saved_config,
    save_config,
)
from app.core.job_lock import CacheTaskLock
from app.core.resource_persistence import CLAUDE_HISTORY_SCHEMA, write_parquet_rows_atomic
from app.core.state import TaskSnapshot, TaskState
from app.web.form_config import parse_form_config


class ImmediateThread:
    def __init__(self, *, target, **_kwargs) -> None:
        self.target = target

    def start(self) -> None:
        self.target()


def _state() -> TaskState:
    return TaskState("test", snapshot_factory=lambda version: TaskSnapshot(version))


def _cache_fixture(root: Path, *, source: str = "codex") -> Path:
    path = root / "llm" / source / "history.parquet"
    write_parquet_rows_atomic(
        path,
        [{
            "schema_version": 1,
            "platform": source,
            "conversation_id": "session-1",
            "conversation_url": "",
            "conversation_title": "Fixture session",
            "message_key": "session-1:message-1",
            "turn_index": 1,
            "message_index": 1,
            "author_label": "You",
            "content_text": "Fixture message.",
            "content_html": "",
            "content_sha256": "fixture-hash",
            "source_links": [],
            "model_label": "",
            "first_seen_at": "2026-10-13T00:00:00Z",
            "last_seen_at": "2026-10-13T00:00:00Z",
            "role": "user",
        }],
        CLAUDE_HISTORY_SCHEMA,
    )
    return path


def _result(source: Path, **overrides) -> dict:
    return {
        "sessions": 1,
        "messages": 1,
        "imported_sessions": 1,
        "imported_messages": 1,
        "added_or_changed": 1,
        "unchanged_messages": 0,
        "unchanged_sessions": 0,
        "failed": 0,
        "stopped": False,
        "source_directory": str(source),
        "transcript_directory": str(source / "sessions"),
        "files_scanned": 1,
        "files_total": 1,
        "applied": True,
        **overrides,
    }


def test_codex_directory_round_trip_and_legacy_settings(tmp_path: Path) -> None:
    source = tmp_path / "Desktop Codex"
    path = tmp_path / "settings.json"
    save_config(CrawlConfig(codex_directory=source, chatgpt_browser="chrome"), path)
    loaded = load_saved_config(path)
    assert loaded.codex_directory == source
    assert loaded.chatgpt_browser == "chrome"
    for payload in ({}, {"codex_directory": ""}, {"codex_directory": None}):
        path.write_text(json.dumps(payload), encoding="utf-8")
        with patch("app.core.config.Path.home", return_value=tmp_path), patch(
            "app.core.config.is_macos_host", return_value=True
        ), patch("app.core.config.is_windows_host", return_value=False):
            assert load_saved_config(path).codex_directory == tmp_path / ".codex"


def test_partial_forms_preserve_local_directory_and_expand_explicit_path(tmp_path: Path) -> None:
    app = Flask(__name__)
    base = CrawlConfig(codex_directory=tmp_path / "existing", chatgpt_browser="safari")
    with app.test_request_context(method="POST", data={"grok_browser": "chrome"}):
        config = parse_form_config(base, preserve_missing_booleans=True)
    assert config.codex_directory == base.codex_directory
    assert config.chatgpt_browser == "safari"
    with app.test_request_context(method="POST", data={"codex_directory": "~/fixture"}), patch(
        "os.path.expanduser", return_value=str(tmp_path)
    ):
        config = parse_form_config(base, preserve_missing_booleans=True)
    assert config.codex_directory == tmp_path / "fixture"


def test_initial_snapshot_counts_only_codex_cache(tmp_path: Path) -> None:
    _cache_fixture(tmp_path, source="chatgpt")
    assert build_codex_initial_snapshot("test", tmp_path).downloaded_tweets == 0
    _cache_fixture(tmp_path)
    snapshot = build_codex_initial_snapshot("test", tmp_path)
    assert snapshot.downloaded_posts == 1
    assert snapshot.downloaded_tweets == 1
    assert snapshot.output_dir == str(tmp_path / "llm/codex")


def test_corrupt_cache_snapshot_exposes_error_without_replacing_file(tmp_path: Path) -> None:
    path = tmp_path / "llm/codex/history.parquet"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"invalid fixture")
    snapshot = build_codex_initial_snapshot("test", tmp_path)
    assert "unreadable" in snapshot.last_error
    assert path.read_bytes() == b"invalid fixture"


def test_local_service_runs_alongside_browser_task_and_reports_durable_totals(tmp_path: Path) -> None:
    root = tmp_path / "store"
    source = tmp_path / "source"
    coordinator = CacheTaskCoordinator(tmp_path / "locks")
    browser_task = CacheTaskIdentity("chatgpt", "text", "ChatGPT Chat")
    coordinator.submit(
        browser_task,
        browser="safari",
        launch=lambda _queued: None,
        on_queued=lambda _message: None,
        on_queue_update=lambda _message: None,
        on_launch_error=lambda _error: None,
        lock_busy_message="busy",
    )
    state = _state()
    service = CodexHistoryService(
        state,
        root,
        coordinator=coordinator,
        task=CacheTaskIdentity("codex", "text", "Codex"),
    )

    def import_fixture(store_root, source_directory, *, should_stop, on_progress):
        assert store_root == root
        assert source_directory == source
        assert not should_stop()
        _cache_fixture(root)
        on_progress({"files_scanned": 1, "private_content": "must not reach status"})
        return _result(source)

    try:
        with patch("app.core.codex_history_service.Thread", ImmediateThread), patch(
            "app.core.codex_history_service.import_codex_history", side_effect=import_fixture
        ):
            service.start(CrawlConfig(codex_directory=source, chatgpt_browser="safari"))
    finally:
        coordinator.finish(browser_task.key)
    snapshot = state.snapshot()
    assert snapshot["phase"] == "finished"
    assert snapshot["downloaded_posts"] == snapshot["downloaded_tweets"] == 1
    assert snapshot["discovered_tweets"] == snapshot["processed_tweets"] == 1
    assert snapshot["performance_metrics"]["source_directory"] == str(source)
    assert "private_content" not in snapshot["performance_metrics"]


def test_service_imports_real_fixture_and_is_idempotent(tmp_path: Path) -> None:
    session = "019c0000-0000-7000-8000-000000000001"
    source = tmp_path / "source"
    directory = source / "sessions/2026/10/13"
    directory.mkdir(parents=True)
    transcript = directory / f"rollout-2026-10-13T00-00-00-{session}.jsonl"
    records = [
        {"type": "session_meta", "payload": {"id": session, "source": "vscode"}},
        {"type": "response_item", "timestamp": "2026-10-13T00:00:00Z", "payload": {
            "type": "message", "role": "user", "id": "message-1",
            "content": [{"type": "input_text", "text": "Fixture question."}],
        }},
    ]
    transcript.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
    original = transcript.read_bytes()
    root = tmp_path / "store"
    state = _state()
    service = CodexHistoryService(state, root, CacheTaskLock(tmp_path / "cache.lock"))
    with patch("app.core.codex_history_service.Thread", ImmediateThread):
        config = CrawlConfig(codex_directory=source, shadow_backup_enabled=False)
        service.start(config)
        first = state.snapshot()
        service.start(config)
        second = state.snapshot()
    assert first["phase"] == second["phase"] == "finished"
    assert first["downloaded_tweets"] == second["downloaded_tweets"] == 1
    assert first["performance_metrics"]["added_or_changed"] == 1
    assert second["performance_metrics"]["added_or_changed"] == 0
    assert second["skipped_tweets"] == 1
    assert transcript.read_bytes() == original


@pytest.mark.parametrize("stopped,failed,phase", [(True, 0, "stopped"), (False, 1, "failed")])
def test_incomplete_import_preserves_cache_and_skips_backup(
    tmp_path: Path, stopped: bool, failed: int, phase: str
) -> None:
    cache_path = _cache_fixture(tmp_path)
    before = cache_path.read_bytes()
    state = _state()
    backup = Mock()
    service = CodexHistoryService(
        state, tmp_path, CacheTaskLock(tmp_path / "cache.lock"), backup
    )
    with patch("app.core.codex_history_service.Thread", ImmediateThread), patch(
        "app.core.codex_history_service.import_codex_history",
        return_value=_result(tmp_path / "source", stopped=stopped, failed=failed, files_total=3),
    ):
        service.start(CrawlConfig(codex_directory=tmp_path / "source"))
    assert state.snapshot()["phase"] == phase
    assert state.snapshot()["downloaded_tweets"] == 1
    assert state.snapshot()["discovered_tweets"] == 3
    assert state.snapshot()["processed_tweets"] == 1
    assert cache_path.read_bytes() == before
    backup.sync_after_cache_task.assert_not_called()


def test_import_exception_releases_lock_and_preserves_existing_totals(tmp_path: Path) -> None:
    _cache_fixture(tmp_path)
    state = _state()
    lock = CacheTaskLock(tmp_path / "cache.lock")
    service = CodexHistoryService(state, tmp_path, lock)
    with patch("app.core.codex_history_service.Thread", ImmediateThread), patch(
        "app.core.codex_history_service.import_codex_history",
        side_effect=ValueError("Configured source does not contain Codex transcripts."),
    ):
        service.start(CrawlConfig(codex_directory=tmp_path / "missing"))
    assert state.snapshot()["phase"] == "failed"
    assert state.snapshot()["downloaded_tweets"] == 1
    assert "Configured source" in state.snapshot()["last_error"]
    assert lock.acquire("next-run")
    lock.release()


def test_cooperative_stop_before_read_does_not_call_importer(tmp_path: Path) -> None:
    class DeferredThread(ImmediateThread):
        def start(self) -> None:
            pass

    state = _state()
    service = CodexHistoryService(state, tmp_path, CacheTaskLock(tmp_path / "cache.lock"))
    with patch("app.core.codex_history_service.Thread", DeferredThread), patch(
        "app.core.codex_history_service.import_codex_history"
    ) as importer:
        service.start(CrawlConfig(codex_directory=tmp_path / "source"))
        assert service.request_stop()
        service._worker.target()
    assert state.snapshot()["phase"] == "stopped"
    importer.assert_not_called()


def test_local_service_rejects_media_without_starting(tmp_path: Path) -> None:
    service = CodexHistoryService(_state(), tmp_path)
    with pytest.raises(ValueError, match="text history only"):
        service.start(CrawlConfig(codex_directory=tmp_path / "source"), content_mode="media")
    assert not service.is_running()
