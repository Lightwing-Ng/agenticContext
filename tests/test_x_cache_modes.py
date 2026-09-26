"""Regression checks for independently selected X text and media caching."""

# Code version: v1.0.0-codex.0

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.core.config import CrawlConfig
from app.core.scraper import collect_liked_tweet_urls
from app.core.service import CacheLikesService
from app.core.state import TaskSnapshot, TaskState, build_x_text_snapshot
from app.core.x_text_history import XTextHistoryStore, XTextPost, x_text_history_path


@pytest.mark.parametrize("content_mode", ("text", "media"))
def test_x_start_hands_selected_mode_to_worker(content_mode: str) -> None:
    state = TaskState("test", snapshot_factory=TaskSnapshot)
    service = CacheLikesService(state)
    config = CrawlConfig(x_browser="safari")

    with patch.object(service, "_start_worker") as start:
        service.start(config, content_mode=content_mode)

    assert start.call_args.kwargs["target"] == service._run
    assert start.call_args.kwargs["args"] == (config, content_mode)


def test_x_start_rejects_unknown_mode_before_starting_worker() -> None:
    service = CacheLikesService(TaskState("test", snapshot_factory=TaskSnapshot))

    with patch.object(service, "_start_worker") as start:
        with pytest.raises(RuntimeError, match="Unsupported X cache content mode"):
            service.start(CrawlConfig(), content_mode="unknown")

    start.assert_not_called()


@pytest.mark.parametrize("browser", ("safari", "chrome", "edge"))
def test_x_text_run_persists_text_without_starting_media_downloads(tmp_path: Path, browser: str) -> None:
    state = TaskState("test", snapshot_factory=TaskSnapshot)
    service = CacheLikesService(state)
    config = CrawlConfig(x_browser=browser, max_media_items=1)

    def collect(_config, _state, *, on_text_posts, should_stop):
        assert _config is config
        assert not should_stop()
        on_text_posts([
            XTextPost("https://x.com/poster/status/123", "First liked text", "poster"),
            XTextPost("https://x.com/poster/status/456", "Second liked text", "poster"),
        ])
        return "liker", ["https://x.com/poster/status/123", "https://x.com/poster/status/456"]

    with patch("app.core.service.LOCAL_STORE_ROOT", tmp_path), patch(
        "app.core.service.collect_liked_tweet_urls", side_effect=collect
    ), patch("app.core.service.LocalTweetCacheIndex.build") as build_index, patch(
        "app.core.service.download_tweet_media"
    ) as download:
        service._run(config, content_mode="text")

    snapshot = state.snapshot()
    assert snapshot["phase"] == "finished"
    assert snapshot["performance_metrics"]["content_mode"] == "text"
    assert snapshot["cached_text_posts"] == 2
    assert snapshot["discovered_tweets"] == 2
    assert snapshot["processed_tweets"] == 2
    assert snapshot["discovery_complete"] is True
    assert snapshot["downloaded_tweets"] == 0
    assert snapshot["downloaded_posts"] == 0
    assert snapshot["downloaded_images"] == 0
    assert snapshot["downloaded_videos"] == 0
    assert snapshot["output_dir"] == str(tmp_path / "llm" / "x")
    assert XTextHistoryStore(x_text_history_path(tmp_path)).cached_posts == 2
    assert not (tmp_path / "x").exists()
    build_index.assert_not_called()
    download.assert_not_called()


@pytest.mark.parametrize("browser", ("chrome", "edge"))
def test_chromium_text_reads_rendered_likes_without_waiting_for_media_api(browser: str) -> None:
    page = MagicMock()
    page.url = "https://x.com/liker/likes"
    page.evaluate.side_effect = [
        json.dumps(["https://x.com/poster/status/123"]),
        json.dumps([{
            "url": "https://x.com/poster/status/123",
            "content_text": "A text-only liked post",
            "author_handle": "poster",
        }]),
    ]
    context = MagicMock()
    context.pages = [page]
    context.__enter__.return_value = context
    descriptor = SimpleNamespace(engine="chromium", browser_id=browser, label=browser.title())
    captured = []

    with patch("app.core.scraper.selected_x_browser_descriptor", return_value=descriptor), patch(
        "app.core.scraper.ensure_playwright_available"
    ), patch("app.core.scraper.sync_playwright", return_value=MagicMock()), patch(
        "app.core.scraper.launch_chromium_context", return_value=context
    ), patch("app.core.scraper.wait_for_likes_page_ready"), patch(
        "app.core.scraper.detect_account_handle", return_value="liker"
    ), patch("app.core.scraper.wait_for_initial_likes_timeline_response") as wait_for_api, patch(
        "app.core.scraper.collect_liked_tweet_urls_via_api"
    ) as media_api:
        handle, urls = collect_liked_tweet_urls(
            CrawlConfig(x_browser=browser, max_scroll_rounds=1),
            TaskState("test", snapshot_factory=TaskSnapshot),
            on_text_posts=captured.extend,
        )

    assert handle == "liker"
    assert urls == ["https://x.com/poster/status/123"]
    assert len(captured) == 1
    assert captured[0].content_text == "A text-only liked post"
    assert captured[0].author_handle == "poster"
    wait_for_api.assert_not_called()
    media_api.assert_not_called()
    context.__exit__.assert_called_once()


def test_x_text_snapshot_does_not_load_or_count_media(tmp_path: Path) -> None:
    XTextHistoryStore(x_text_history_path(tmp_path)).upsert([
        XTextPost("https://x.com/poster/status/123", "Cached text", "poster")
    ])
    with patch("app.core.state.summarize_local_store_root") as media_summary:
        snapshot = build_x_text_snapshot("test", tmp_path)

    assert snapshot.cached_text_posts == 1
    assert snapshot.downloaded_tweets == 0
    assert snapshot.downloaded_posts == 0
    assert snapshot.downloaded_images == 0
    assert snapshot.downloaded_videos == 0
    assert snapshot.output_dir == str(tmp_path / "llm" / "x")
    assert snapshot.performance_metrics["content_mode"] == "text"
    media_summary.assert_not_called()


def test_x_text_snapshot_preserves_unreadable_history(tmp_path: Path) -> None:
    history = x_text_history_path(tmp_path)
    history.parent.mkdir(parents=True)
    history.write_bytes(b"unreadable history")

    snapshot = build_x_text_snapshot("test", tmp_path)

    assert snapshot.cached_text_posts == 0
    assert "not overwritten" in snapshot.last_error
    assert history.read_bytes() == b"unreadable history"
