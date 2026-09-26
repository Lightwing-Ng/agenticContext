"""Regression checks for truthful media cache completion states.

Code version: v1.1.0-codex.0
"""

from pathlib import Path
import re
from unittest.mock import patch

import pytest

from app.core.chatgpt_downloader import ChatGPTSyncResult
from app.core.chatgpt_service import ChatGPTDownloadService
from app.core.claude_history_service import ClaudeHistoryService
from app.core.claude_media import ClaudeMediaSyncResult
from app.core.config import CrawlConfig
from app.core.downloader import DownloadResult
from app.core.grok_downloader import GrokSyncResult
from app.core.grok_service import GrokDownloadService
from app.core.job_lock import CacheTaskLock
from app.core.service import CacheLikesService
from app.core.state import TaskSnapshot, TaskState


@pytest.mark.parametrize("provider", ("chatgpt", "grok", "x"))
@pytest.mark.parametrize(
    "failure_kind,expected_phase",
    (("download", "failed"), ("task", "failed"), ("stopped", "stopped"), ("size_skip", "finished")),
)
def test_media_worker_reports_incomplete_downloads_without_losing_successful_counts(
    tmp_path: Path, provider: str, failure_kind: str, expected_phase: str
) -> None:
    """Failures remain failures, while cooperative stops and configured size skips keep their meaning."""
    state = TaskState("test", snapshot_factory=TaskSnapshot)
    module = {
        "chatgpt": "app.core.chatgpt_service",
        "grok": "app.core.grok_service",
        "x": "app.core.service",
    }[provider]
    failed = int(failure_kind == "download")
    stopped = failure_kind == "stopped"
    skipped_size = int(failure_kind == "size_skip")

    def update_task_state() -> None:
        state.update(downloaded_images=1, failed_tweets=failed)
        if failure_kind == "task":
            state.finish_error("Provider inspection failed after caching one image.")

    with patch(f"{module}.append_shadow_backup_completion", side_effect=lambda message, **_kwargs: message) as backup:
        if provider == "chatgpt":
            service = ChatGPTDownloadService(state)

            def sync(*_args, **_kwargs):
                update_task_state()
                return ChatGPTSyncResult(
                    downloaded_count=1, cached_count=1, failed_count=failed,
                    stopped=stopped, skipped_size=skipped_size,
                )

            with patch(f"{module}.sync_chatgpt_images", side_effect=sync):
                service._run()
        elif provider == "grok":
            service = GrokDownloadService(state)

            def sync(*_args, **_kwargs):
                update_task_state()
                return GrokSyncResult(
                    downloaded_count=1, cached_count=1, failed_count=failed,
                    stopped=stopped, skipped_size=skipped_size,
                )

            with patch(f"{module}.sync_grok_media", side_effect=sync):
                service._run()
        else:
            service = CacheLikesService(state)

            def download(*_args, **_kwargs):
                update_task_state()
                return DownloadResult(
                    downloaded_media_count=1, downloaded_image_count=1,
                    timed_out=bool(failed), stopped=stopped,
                    skipped_oversized_media_count=skipped_size,
                )

            with patch(f"{module}.LOCAL_STORE_ROOT", tmp_path), patch(
                f"{module}.collect_liked_tweet_urls",
                return_value=("fixture", ["https://x.com/fixture/status/123"]),
            ), patch(f"{module}.LocalTweetCacheIndex.build"), patch(
                f"{module}.download_tweet_media", side_effect=download
            ):
                service._run(CrawlConfig(x_browser="safari", download_workers=1), content_mode="media")

    snapshot = state.snapshot()
    assert snapshot["phase"] == expected_phase
    assert snapshot["running"] is False
    assert snapshot["downloaded_images"] == 1
    assert snapshot["failed_tweets"] == failed
    if expected_phase == "failed":
        assert "incomplete" in snapshot["message"]
        assert snapshot["task_failures"] > 0
    if expected_phase == "finished":
        backup.assert_called_once()
    else:
        backup.assert_not_called()


@pytest.mark.parametrize(
    "failed_images,stopped,expected_phase",
    ((0, False, "finished"), (1, False, "failed"), (0, True, "stopped")),
)
def test_claude_media_service_keeps_size_skips_separate_from_failures(
    tmp_path: Path, failed_images: int, stopped: bool, expected_phase: str
) -> None:
    class ImmediateThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    state = TaskState("test")
    service = ClaudeHistoryService(
        state, local_store_root=tmp_path,
        task_lock=CacheTaskLock(tmp_path / "cache-task.lock"),
    )

    def sync(*_args, **_kwargs):
        state.update(
            downloaded_images=1, queued_tweets=10 + failed_images,
            processed_tweets=10 + failed_images, skipped_tweets=9,
            failed_tweets=failed_images,
        )
        return ClaudeMediaSyncResult(
            sessions=1, discovered_images=10 + failed_images, cached_images=1,
            downloaded_images=1, skipped_known=2, skipped_excluded=3,
            skipped_size=4, skipped_unsupported=5,
            failed_images=failed_images, stopped=stopped,
        )

    with patch("app.core.claude_history_service.Thread", ImmediateThread), patch(
        "app.core.claude_history_service.sync_claude_media", side_effect=sync
    ), patch(
        "app.core.claude_history_service.append_shadow_backup_completion",
        side_effect=lambda message, **_kwargs: message,
    ) as backup:
        service.start(CrawlConfig(claude_browser="safari"), content_mode="media")

    snapshot = state.snapshot()
    assert snapshot["phase"] == expected_phase
    assert snapshot["running"] is False
    assert snapshot["downloaded_images"] == 1
    assert snapshot["skipped_tweets"] == 9
    assert snapshot["failed_tweets"] == failed_images
    assert snapshot["processed_tweets"] == snapshot["queued_tweets"] == 10 + failed_images
    assert "2 known files" in snapshot["message"]
    assert "3 excluded by deletion" in snapshot["message"]
    assert "4 over the size limit" in snapshot["message"]
    assert "5 unsupported references" in snapshot["message"]
    if expected_phase == "finished":
        assert snapshot["task_failures"] == 0
        backup.assert_called_once()
    else:
        backup.assert_not_called()
    if expected_phase == "failed":
        assert "incomplete" in snapshot["message"]
        assert snapshot["task_failures"] > 0


def test_claude_media_labels_all_skipped_images_without_calling_them_known(
    tmp_path: Path, macos_host
) -> None:
    from app.web.app import create_app

    application = create_app(tmp_path / "local_store")
    response = application.test_client().get("/cache/claude/media/safari")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    label = re.search(
        r'<span class="metric-label">([^<]+)</span>\s*<strong\s+'
        r'id="claude_media_skipped"\s+data-status-field="skipped_tweets"',
        body,
    )
    assert label is not None
    assert label.group(1) == "Images skipped"
