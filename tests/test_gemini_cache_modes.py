"""Gemini mode isolation and truthful worker completion.

Code version: v1.0.0-codex.0
"""

from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core.config import CrawlConfig
from app.core.gemini_downloader import GeminiSyncResult
from app.core.gemini_media import GeminiMediaSyncResult
from app.core.gemini_service import GeminiHistoryService
from app.core.job_lock import CacheTaskLock
from app.core.state import TaskSnapshot, TaskState
from app.web.app import create_app
from app.web.cache_routes import build_reconciled_cache_snapshot, CacheRuntimeAdapter


class ImmediateThread:
    def __init__(self, *, target, **_kwargs):
        self.target = target

    def start(self):
        self.target()


@pytest.mark.parametrize("mode", ("text", "media"))
@pytest.mark.parametrize("outcome", ("success", "failed", "stopped"))
def test_worker_dispatches_mode_and_preserves_failure_state(tmp_path: Path, mode: str, outcome: str) -> None:
    state = TaskState("test")
    lock = CacheTaskLock(tmp_path / "cache-task.lock")
    service = GeminiHistoryService(state, local_store_root=tmp_path, task_lock=lock)
    text_result = GeminiSyncResult(2, 2, 4, 1, 1, int(outcome == "failed"), 2, 4, outcome == "stopped")
    media_result = GeminiMediaSyncResult(
        sessions=2, cached_images=1, downloaded_images=1,
        failed_images=int(outcome == "failed"), stopped=outcome == "stopped", skipped_size=2,
    )
    with patch("app.core.gemini_service.Thread", ImmediateThread), patch(
        "app.core.gemini_service.sync_gemini_history", return_value=text_result,
    ) as text_sync, patch(
        "app.core.gemini_service.sync_gemini_media", return_value=media_result,
    ) as media_sync, patch(
        "app.core.gemini_service.append_shadow_backup_completion",
        side_effect=lambda message, **_kwargs: message,
    ) as backup:
        service.start(CrawlConfig(gemini_browser="safari"), content_mode=mode)
    (media_sync if mode == "media" else text_sync).assert_called_once()
    (text_sync if mode == "media" else media_sync).assert_not_called()
    snapshot = state.snapshot()
    assert snapshot["phase"] == {"success": "finished", "failed": "failed", "stopped": "stopped"}[outcome]
    assert not snapshot["running"]
    assert snapshot["performance_metrics"]["content_mode"] == mode
    assert backup.call_count == int(outcome == "success")
    if mode == "media":
        assert "2 over the size limit" in snapshot["message"]
    assert lock.acquire("next-task")
    lock.release()


@pytest.mark.parametrize("selected_mode,live_mode", (("text", "media"), ("media", "text")))
@pytest.mark.parametrize("running", (False, True))
def test_mode_snapshots_never_mix_text_and_media_counts(tmp_path, selected_mode, live_mode, running) -> None:
    state = TaskState("test")
    state.update(
        downloaded_tweets=999, downloaded_images=999, running=running,
        phase="downloading" if running else "finished",
        performance_metrics={"content_mode": live_mode},
    )
    text_snapshot = TaskSnapshot(version="test", downloaded_posts=2, downloaded_tweets=4)
    media_snapshot = TaskSnapshot(version="test", downloaded_images=3)
    context = SimpleNamespace(
        cache_runtimes={"gemini": CacheRuntimeAdapter(state, None, lambda: text_snapshot)},
        media_catalog=SimpleNamespace(local_store_root=tmp_path),
    )
    with patch("app.web.cache_routes.build_gemini_media_initial_snapshot", return_value=media_snapshot):
        result = build_reconciled_cache_snapshot(context, "gemini", selected_mode)
    expected = asdict(text_snapshot if selected_mode == "text" else media_snapshot)
    for field in ("downloaded_posts", "downloaded_tweets", "downloaded_images"):
        assert result[field] == expected[field]
    assert result["running"] is running


def test_media_page_and_status_do_not_report_history_as_images(tmp_path: Path, macos_host) -> None:
    app = create_app(tmp_path)
    client = app.test_client()
    page = client.get("/cache/gemini/media/safari").get_data(as_text=True)
    assert "Images cached" in page
    assert "Gemini media cache notice" in page
    assert "Other attachments and videos are not included" in page
    snapshot = client.get("/api/gemini/status?content_mode=media").get_json()
    assert snapshot["downloaded_images"] == 0
    assert snapshot["output_dir"] == str(tmp_path / "media" / "gemini")
    assert snapshot["performance_metrics"]["content_mode"] == "media"
