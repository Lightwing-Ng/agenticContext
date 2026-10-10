"""Gemini mode isolation and truthful worker completion.

Code version: v1.1.0-claude.0
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


@pytest.mark.parametrize("selected_mode,other_mode", (("text", "media"), ("media", "text")))
@pytest.mark.parametrize("other_running", (False, True))
def test_mode_snapshots_never_mix_text_and_media_status(tmp_path, selected_mode, other_mode, other_running) -> None:
    """Each mode owns its task state, so the other mode's run never shows on its page."""
    states = {
        "text": TaskState("test", snapshot_factory=TaskSnapshot),
        "media": TaskState("test", snapshot_factory=TaskSnapshot),
    }
    states[other_mode].update(
        downloaded_tweets=999, downloaded_images=999, running=other_running,
        phase="downloading" if other_running else "finished", started_at="2026-09-30T00:00:00Z",
    )
    hydrated = {
        "text": TaskSnapshot(version="test", downloaded_posts=2, downloaded_tweets=4),
        "media": TaskSnapshot(version="test", downloaded_images=3),
    }
    context = SimpleNamespace(
        cache_runtimes={"gemini": {
            mode: CacheRuntimeAdapter(states[mode], None, lambda mode=mode: hydrated[mode])
            for mode in ("text", "media")
        }},
        media_catalog=SimpleNamespace(local_store_root=tmp_path),
    )
    result = build_reconciled_cache_snapshot(context, "gemini", selected_mode)
    assert result == asdict(hydrated[selected_mode])
    assert result["running"] is False
    # The mode that ran keeps its own live status.
    other = build_reconciled_cache_snapshot(context, "gemini", other_mode)
    assert other["running"] is other_running
    assert other["downloaded_images"] == (999 if other_running else hydrated[other_mode].downloaded_images)


def test_a_mode_that_ran_or_failed_keeps_its_task_status_over_the_store(tmp_path) -> None:
    state = TaskState("test", snapshot_factory=TaskSnapshot)
    hydrated = TaskSnapshot(version="test", downloaded_images=3, message="Ready. 3 images.")
    context = SimpleNamespace(
        cache_runtimes={"gemini": {"media": CacheRuntimeAdapter(state, None, lambda: hydrated)}},
        media_catalog=SimpleNamespace(local_store_root=tmp_path),
    )
    # A single-mode registration answers every requested mode.
    assert build_reconciled_cache_snapshot(context, "gemini", "text") == asdict(hydrated)

    state.finish_error("Unsupported Gemini browser: opera")
    failed = build_reconciled_cache_snapshot(context, "gemini", "media")
    assert failed["phase"] == "failed"
    assert failed["message"] == "Unsupported Gemini browser: opera"

    state.reset_for_run()
    state.update(downloaded_images=1)
    state.finish_success("Finished Gemini media cache.")
    finished = build_reconciled_cache_snapshot(context, "gemini", "media")
    assert finished["phase"] == "finished"
    assert finished["message"] == "Finished Gemini media cache."
    # Counters still follow the store once the run is over.
    assert finished["downloaded_images"] == 3


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
