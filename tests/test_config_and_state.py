"""Tests for durable settings and thread-safe task state.

Code version: v1.5.1-codex.1
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import MAX_DOWNLOAD_WORKERS, CrawlConfig, load_saved_config, save_config
from app.core.state import TaskSnapshot, TaskState


def test_crawl_config_sanitizes_account_names() -> None:
    config = CrawlConfig(account_name_override="  account/name?  ")

    assert config.sanitized_account_name("fallback") == "account_name"
    assert CrawlConfig().sanitized_account_name("  ") == "unknown_account"
    assert CrawlConfig().sanitized_account_name(".hidden.") == "hidden"


def test_windows_defaults_use_native_application_data_roots(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import app.core.config as config

    local_app_data = tmp_path / "LocalAppData"
    roaming_app_data = tmp_path / "RoamingAppData"
    monkeypatch.setattr(config, "is_windows_host", lambda: True)
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setenv("APPDATA", str(roaming_app_data))
    monkeypatch.delenv(config.SETTINGS_PATH_ENV, raising=False)

    assert config.default_chrome_user_data_dir() == local_app_data / "Google/Chrome/User Data"
    assert config.default_edge_user_data_dir() == local_app_data / "Microsoft/Edge/User Data"
    assert config.default_settings_path() == roaming_app_data / "agenticContext/settings.json"


def test_default_settings_read_legacy_application_data_without_writing_to_it(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import app.core.config as config

    home_dir = tmp_path / "home"
    monkeypatch.setattr(config, "is_macos_host", lambda: True)
    monkeypatch.setattr(config, "is_windows_host", lambda: False)
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("USERPROFILE", str(home_dir))
    monkeypatch.delenv(config.SETTINGS_PATH_ENV, raising=False)
    monkeypatch.delenv(config.LEGACY_SETTINGS_PATH_ENV, raising=False)

    legacy_path = config.legacy_default_settings_path()
    legacy_path.parent.mkdir(parents=True)
    save_config(CrawlConfig(account_name_override="legacy"), legacy_path)

    assert config.default_settings_path() == home_dir / "Library/Application Support/agenticContext/settings.json"
    assert load_saved_config().account_name_override == "legacy"


def test_settings_round_trip_and_invalid_payload_fall_back_to_defaults(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("app.core.config.is_macos_host", lambda: True)
    monkeypatch.setattr("app.core.config.is_windows_host", lambda: False)
    settings_path = tmp_path / "settings.json"
    expected = CrawlConfig(
        headless=True,
        download_workers=7,
        max_media_items=1_234,
        chatgpt_startup_timeout_seconds=45.0,
        chatgpt_scan_wait_seconds=0.25,
        x_browser="safari",
        grok_browser="chrome",
        chrome_user_data_dir=tmp_path / "Chrome",
        chrome_profile_directory="Profile 4",
        account_name_override="demo",
        shadow_backup_enabled=True,
        shadow_backup_auto_sync=True,
        shadow_backup_mirror_deletions=True,
        shadow_backup_destination=tmp_path / "OneDrive" / "AICaches",
    )

    save_config(expected, settings_path)
    loaded = load_saved_config(settings_path)

    assert loaded == expected
    settings_path.write_text("not json", encoding="utf-8")
    assert load_saved_config(settings_path) == CrawlConfig()


@pytest.mark.parametrize("windows", [False, True])
def test_saved_safari_choices_follow_the_current_host(monkeypatch, tmp_path: Path, windows: bool) -> None:
    monkeypatch.setattr("app.core.config.is_windows_host", lambda: windows)
    monkeypatch.setattr("app.core.config.is_macos_host", lambda: not windows)
    browser_fields = ("x_browser", "grok_browser", "chatgpt_browser", "gemini_browser", "claude_browser")
    settings_path = tmp_path / "settings.json"
    save_config(CrawlConfig(**dict.fromkeys(browser_fields, "safari")), settings_path)

    loaded = load_saved_config(settings_path)
    defaults = CrawlConfig()

    for field in browser_fields:
        assert getattr(loaded, field) == (getattr(defaults, field) if windows else "safari")


def test_settings_clamp_workers_and_normalize_browser_names(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        '{"download_workers": 0, "x_browser": " EDGE ", "grok_browser": "  "}',
        encoding="utf-8",
    )

    loaded = load_saved_config(settings_path)

    assert loaded.download_workers == 1
    assert loaded.x_browser == "edge"
    assert loaded.grok_browser == CrawlConfig().grok_browser


def test_download_workers_have_a_hard_cap_for_direct_and_legacy_config(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        '{"download_workers": "not-a-number"}',
        encoding="utf-8",
    )

    assert CrawlConfig(download_workers=-10).download_workers == 1
    assert CrawlConfig(download_workers=10_000).download_workers == MAX_DOWNLOAD_WORKERS
    assert load_saved_config(settings_path).download_workers == CrawlConfig().download_workers

    settings_path.write_text('{"download_workers": 10000}', encoding="utf-8")
    assert load_saved_config(settings_path).download_workers == MAX_DOWNLOAD_WORKERS


def test_settings_clamp_universal_media_file_size(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        '{"max_media_file_size_mib": 0}',
        encoding="utf-8",
    )
    assert load_saved_config(settings_path).max_media_file_size_mib == 1

    settings_path.write_text(
        '{"max_media_file_size_mib": 999999}',
        encoding="utf-8",
    )
    assert load_saved_config(settings_path).max_media_file_size_mib == 10_240


def test_settings_clamp_chatgpt_timeout_and_scan_wait_seconds(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        '{"chatgpt_startup_timeout_seconds": 9999, "chatgpt_scan_wait_seconds": 0}',
        encoding="utf-8",
    )

    loaded = load_saved_config(settings_path)

    assert loaded.chatgpt_startup_timeout_seconds == 600.0
    assert loaded.chatgpt_scan_wait_seconds == 0.1


def test_task_state_lifecycle_caps_events_and_preserves_version() -> None:
    state = TaskState("v-test", snapshot_factory=lambda version: TaskSnapshot(version=version))

    state.reset_for_run()
    for index in range(55):
        state.append_event(f"event {index}")
    state.finish_success("Finished test run.")
    snapshot = state.snapshot()

    assert snapshot["version"] == "v-test"
    assert snapshot["running"] is False
    assert snapshot["phase"] == "finished"
    assert snapshot["queued_tweets"] == 0
    assert snapshot["processed_tweets"] == 0
    assert snapshot["discovery_complete"] is False
    assert snapshot["message"] == "Finished test run."
    assert snapshot["finished_at"].endswith("Z")
    assert len(snapshot["recent_events"]) == 50
    assert snapshot["recent_events"][0].endswith("event 5")


def test_task_state_stop_and_error_transitions() -> None:
    state = TaskState("v-test", snapshot_factory=lambda version: TaskSnapshot(version=version))

    state.reset_for_run()
    state.finish_stopped("Stopped.")
    assert state.snapshot()["phase"] == "stopped"

    state.reset_for_run()
    state.finish_error("Failure.")
    snapshot = state.snapshot()
    assert snapshot["phase"] == "failed"
    assert snapshot["last_error"] == "Failure."
    assert snapshot["task_failures"] == 1
