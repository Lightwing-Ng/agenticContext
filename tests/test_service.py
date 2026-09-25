"""Focused regression tests for the cache orchestration service.

Code version: v1.3.0-codex.0
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.core.config import CrawlConfig, LOCAL_STORE_ROOT
from app.core.downloader import DownloadResult
from app.core.service import CacheLikesService
from app.core.state import TaskState
from app.core.x_text_history import XTextHistoryStore, XTextPost, x_text_history_path


class CacheLikesServiceTests(unittest.TestCase):
    """Validate the worker handoff and temporary download cap."""

    def test_run_passes_config_to_downloader(self) -> None:
        state = TaskState(version="test")
        service = CacheLikesService(state)
        config = CrawlConfig(max_media_items=10)

        with patch(
            "app.core.service.collect_liked_tweet_urls",
            return_value=("demo_account", ["https://x.com/demo/status/1"]),
        ), patch("app.core.service.download_tweet_media", return_value=DownloadResult(downloaded_media_count=1)) as mock_download:
            service._run(config)

        self.assertEqual(mock_download.call_count, 1)
        call = mock_download.call_args
        self.assertEqual(call.args[0], "https://x.com/demo/status/1")
        self.assertEqual(call.args[1], LOCAL_STORE_ROOT / "x")
        self.assertIs(call.args[2], config)
        self.assertIs(call.args[3], state)
        self.assertIsNone(call.kwargs["remaining_media_items"])
        self.assertIn("cache_index", call.kwargs)
        self.assertTrue(callable(call.kwargs["should_stop"]))

    def test_inflight_stop_preserves_completed_media_and_skips_next_tweet(self) -> None:
        state = TaskState(version="test")
        service = CacheLikesService(state)
        config = CrawlConfig(download_workers=1, max_media_items=1_000)
        tweet_urls = ["https://x.com/demo/status/1", "https://x.com/demo/status/2"]

        with patch(
            "app.core.service.collect_liked_tweet_urls",
            return_value=("demo", tweet_urls),
        ), patch(
            "app.core.service.download_tweet_media",
            return_value=DownloadResult(
                downloaded_media_count=1,
                downloaded_post_count=1,
                downloaded_image_count=1,
                stopped=True,
            ),
        ) as download:
            service._run(config)

        snapshot = state.snapshot()
        self.assertEqual(download.call_count, 1)
        self.assertEqual(snapshot["phase"], "stopped")
        self.assertEqual(snapshot["downloaded_posts"], 1)
        self.assertEqual(snapshot["downloaded_images"], 1)
        self.assertEqual(snapshot["failed_tweets"], 0)

    def test_timed_out_tweet_counts_partial_media_and_one_failure(self) -> None:
        state = TaskState(version="test")
        service = CacheLikesService(state)
        config = CrawlConfig(download_workers=1, max_media_items=1_000)

        with patch(
            "app.core.service.collect_liked_tweet_urls",
            return_value=(
                "demo",
                ["https://x.com/demo/status/1", "https://x.com/demo/status/2"],
            ),
        ), patch(
            "app.core.service.download_tweet_media",
            side_effect=[
                DownloadResult(
                    downloaded_media_count=1,
                    downloaded_post_count=1,
                    downloaded_image_count=1,
                    timed_out=True,
                ),
                DownloadResult(skipped=True),
            ],
        ) as download:
            service._run(config)

        snapshot = state.snapshot()
        self.assertEqual(download.call_count, 2)
        self.assertEqual(snapshot["phase"], "finished")
        self.assertEqual(snapshot["downloaded_posts"], 1)
        self.assertEqual(snapshot["downloaded_images"], 1)
        self.assertEqual(snapshot["failed_tweets"], 1)

    def test_run_stops_after_media_cap_is_reached(self) -> None:
        state = TaskState(version="test")
        service = CacheLikesService(state)
        config = CrawlConfig(max_media_items=10)
        tweet_urls = [f"https://x.com/demo/status/{index}" for index in range(1, 5)]

        with patch(
            "app.core.service.collect_liked_tweet_urls",
            return_value=("demo_account", tweet_urls),
        ), patch(
            "app.core.service.download_tweet_media",
            side_effect=[
                DownloadResult(downloaded_media_count=4),
                DownloadResult(downloaded_media_count=4),
                DownloadResult(downloaded_media_count=2),
                DownloadResult(downloaded_media_count=1),
            ],
        ) as mock_download:
            service._run(config)

        # Parallel mode deliberately treats the media limit as a soft ceiling so a
        # queued tweet is never split across workers.
        self.assertEqual(mock_download.call_count, 4)
        snapshot = state.snapshot()
        self.assertEqual(snapshot["phase"], "finished")
        self.assertEqual(snapshot["downloaded_tweets"], 11)
        self.assertIn("downloaded 11 media files", snapshot["message"])

    def test_request_stop_returns_false_when_idle(self) -> None:
        state = TaskState(version="test")
        service = CacheLikesService(state)

        self.assertFalse(service.request_stop())

    def test_non_safari_media_cache_ignores_corrupt_x_text_history(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            history_path = x_text_history_path(root)
            history_path.parent.mkdir(parents=True)
            history_path.write_bytes(b"unreadable text history")

            for browser in ("edge", "chrome"):
                with self.subTest(browser=browser):
                    state = TaskState(version="test")
                    service = CacheLikesService(state)
                    config = CrawlConfig(
                        x_browser=browser,
                        download_workers=1,
                        max_media_items=1,
                    )
                    with patch("app.core.service.LOCAL_STORE_ROOT", root), patch(
                        "app.core.service.collect_liked_tweet_urls",
                        return_value=("liker", ["https://x.com/poster/status/123"]),
                    ) as collect, patch(
                        "app.core.service.download_tweet_media",
                        return_value=DownloadResult(downloaded_media_count=1),
                    ) as download:
                        service._run(config)

                    self.assertEqual(state.snapshot()["phase"], "finished")
                    self.assertIsNone(collect.call_args.kwargs["on_text_posts"])
                    download.assert_called_once()
            self.assertEqual(history_path.read_bytes(), b"unreadable text history")

    def test_safari_stop_keeps_text_only_post_without_touching_media(self) -> None:
        state = TaskState(version="test")
        service = CacheLikesService(state)
        config = CrawlConfig(x_browser="safari", max_media_items=1)

        def collect(_config, _state, *, on_text_posts, should_stop):
            self.assertIs(_config, config)
            self.assertFalse(should_stop())
            on_text_posts([XTextPost(
                url="https://x.com/poster/status/123",
                content_text="Liked text without media",
                author_handle="poster",
            )])
            service._stop_requested.set()
            return "liker", ["https://x.com/poster/status/123"]

        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("app.core.service.LOCAL_STORE_ROOT", root), patch(
                "app.core.service.collect_liked_tweet_urls", side_effect=collect
            ), patch("app.core.service.download_tweet_media") as download:
                service._run(config)

            snapshot = state.snapshot()
            self.assertEqual(snapshot["phase"], "stopped")
            self.assertEqual(snapshot["cached_text_posts"], 1)
            self.assertEqual(snapshot["downloaded_posts"], 0)
            self.assertEqual(snapshot["downloaded_images"], 0)
            download.assert_not_called()
            self.assertFalse((root / "x").exists())
            self.assertEqual(XTextHistoryStore(x_text_history_path(root)).cached_posts, 1)

    def test_run_stops_when_emergency_stop_is_requested(self) -> None:
        state = TaskState(version="test")
        service = CacheLikesService(state)
        config = CrawlConfig(max_media_items=10)
        tweet_urls = [f"https://x.com/demo/status/{index}" for index in range(1, 4)]

        def fake_download(*_args, **_kwargs):
            service._stop_requested.set()
            return DownloadResult(downloaded_media_count=1)

        with patch(
            "app.core.service.collect_liked_tweet_urls",
            return_value=("demo_account", tweet_urls),
        ), patch("app.core.service.download_tweet_media", side_effect=fake_download) as mock_download:
            service._run(config)

        self.assertEqual(mock_download.call_count, 3)
        snapshot = state.snapshot()
        self.assertEqual(snapshot["phase"], "stopped")
        self.assertEqual(snapshot["downloaded_tweets"], 3)
        self.assertIn("Emergency stop completed", snapshot["message"])


if __name__ == "__main__":
    unittest.main()
