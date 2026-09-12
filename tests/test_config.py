"""Focused regression tests for persisted crawler settings.

Code version: v1.5.1-codex.1
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.config import CrawlConfig, load_saved_config, resolve_listen_host, save_config


class ConfigPersistenceTests(unittest.TestCase):
    """Validate saved settings survive app restarts."""

    def test_new_configuration_uses_non_disruptive_gemini_default(self) -> None:
        self.assertEqual(CrawlConfig().gemini_browser, "edge")
        self.assertEqual(CrawlConfig().claude_browser, "edge")

    def test_server_defaults_to_loopback_and_requires_an_explicit_lan_bind(self) -> None:
        with patch.dict(
            "os.environ",
            {},
            clear=False,
        ):
            for variable_name in ("AGENTIC_CONTEXT_HOST", "CACHELIKES_HOST"):
                os.environ.pop(variable_name, None)
            self.assertEqual(resolve_listen_host(), "127.0.0.1")

        with patch.dict("os.environ", {"AGENTIC_CONTEXT_HOST": "0.0.0.0"}):
            self.assertEqual(resolve_listen_host(), "0.0.0.0")

        with patch.dict(
            "os.environ",
            {
                "AGENTIC_CONTEXT_HOST": "",
                "CACHELIKES_HOST": "192.168.124.10",
            },
        ):
            self.assertEqual(resolve_listen_host(), "192.168.124.10")

    def test_cache_scan_intervals_are_independent_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "settings.json"
            config = CrawlConfig(
                cache_scan_waits={"chatgpt_text": 0.3, "claude_text": 1.4, "grok_text": 0.2, "grok_media": 2.5},
                chatgpt_scan_wait_seconds=3.0,
                chatgpt_text_startup_timeout_seconds=45.0,
            )
            save_config(config, path)
            loaded = load_saved_config(path)
        self.assertEqual(loaded.cache_scan_wait("chatgpt", "text"), 0.3)
        self.assertEqual(loaded.cache_scan_wait("claude", "text"), 1.4)
        self.assertEqual(loaded.cache_scan_wait("grok", "text"), 0.2)
        self.assertEqual(loaded.cache_scan_wait("grok", "media"), 2.5)
        self.assertEqual(loaded.chatgpt_scan_wait_seconds, 3.0)
        self.assertEqual(loaded.chatgpt_text_startup_timeout_seconds, 45.0)
        self.assertEqual(CrawlConfig().cache_scan_wait("grok", "media"), 0.8)

    def test_explicit_blank_chatgpt_project_url_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "settings.json"
            save_config(CrawlConfig(chatgpt_project_url=""), settings_path)

            loaded = load_saved_config(settings_path)

        self.assertEqual(loaded.chatgpt_project_url, "")

    def test_save_and_load_config_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "settings.json"
            config = CrawlConfig(
                headless=True,
                max_media_file_size_mib=128,
                max_media_items=50_000,
                max_scroll_rounds=5_000,
                scroll_pause_seconds=1.0,
                stale_round_limit=12,
                claude_browser="chrome",
                gemini_browser="safari",
                gemini_max_conversations=2_000,
                gemini_scroll_pause_seconds=0.35,
                gemini_stale_round_limit=7,
                chrome_user_data_dir=Path("/tmp/chrome-profile"),
                chrome_profile_directory="Profile 2",
                account_name_override="demo_override",
                shadow_backup_enabled=True,
                shadow_backup_auto_sync=True,
                shadow_backup_mirror_deletions=True,
                shadow_backup_destination=Path("/tmp/OneDrive/AICaches"),
            )

            save_config(config, settings_path)
            loaded = load_saved_config(settings_path)

        self.assertTrue(loaded.headless)
        self.assertEqual(loaded.max_media_file_size_mib, 128)
        self.assertEqual(loaded.max_media_file_size_bytes, 128 * 1024 * 1024)
        self.assertEqual(loaded.max_media_items, 50_000)
        self.assertEqual(loaded.max_scroll_rounds, 5_000)
        self.assertEqual(loaded.scroll_pause_seconds, 1.0)
        self.assertEqual(loaded.stale_round_limit, 12)
        self.assertEqual(loaded.claude_browser, "chrome")
        self.assertEqual(loaded.gemini_browser, "safari")
        self.assertEqual(loaded.gemini_max_conversations, 2_000)
        self.assertEqual(loaded.gemini_scroll_pause_seconds, 0.35)
        self.assertEqual(loaded.gemini_stale_round_limit, 7)
        self.assertEqual(loaded.chrome_user_data_dir, Path("/tmp/chrome-profile"))
        self.assertEqual(loaded.chrome_profile_directory, "Profile 2")
        self.assertEqual(loaded.account_name_override, "demo_override")
        self.assertTrue(loaded.shadow_backup_enabled)
        self.assertTrue(loaded.shadow_backup_auto_sync)
        self.assertTrue(loaded.shadow_backup_mirror_deletions)
        self.assertEqual(loaded.shadow_backup_destination, Path("/tmp/OneDrive/AICaches"))


if __name__ == "__main__":
    unittest.main()


def test_cache_scan_wait_honors_interval_and_cooperative_stop():
    from app.core.cache_timing import wait_for_cache_scan

    intervals = []
    assert not wait_for_cache_scan(0.7, lambda: False, intervals.append)
    assert abs(sum(intervals) - 0.7) < 0.000001
    assert max(intervals) <= 0.25
    intervals.clear()
    assert wait_for_cache_scan(60.0, lambda: bool(intervals), intervals.append)
    assert intervals == [0.25]
