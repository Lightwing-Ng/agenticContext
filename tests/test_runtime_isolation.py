"""Regression tests for the process-wide pytest runtime boundary.

Code version: v1.2.1-codex.0
"""

from __future__ import annotations

from app.core import config
from app.core import grok_downloader
from app.web.app import create_app


def test_pytest_runtime_paths_are_outside_the_repository() -> None:
    """Keep default application construction away from user-owned runtime data."""
    assert config.RUNTIME_ROOT != config.PROJECT_ROOT
    assert not config.LOCAL_STORE_ROOT.is_relative_to(config.PROJECT_ROOT)
    assert not config.LOGS_ROOT.is_relative_to(config.PROJECT_ROOT)
    assert not config.LEGACY_SETTINGS_PATH.is_relative_to(config.PROJECT_ROOT)
    assert config.SETTINGS_PATH.is_relative_to(config.RUNTIME_ROOT)
    assert grok_downloader.GROK_TARGET_DIR.is_relative_to(config.RUNTIME_ROOT)

    application = create_app()
    catalog = application.extensions["local_media_catalog"]

    assert catalog.local_store_root == config.LOCAL_STORE_ROOT.resolve(strict=False)


def test_grok_snapshot_resolves_its_default_target_at_call_time(monkeypatch, tmp_path) -> None:
    """Allow tests to redirect the Grok cache without a frozen default argument."""
    target_dir = tmp_path / "media" / "grok"
    monkeypatch.setattr(grok_downloader, "GROK_TARGET_DIR", target_dir)

    snapshot = grok_downloader.build_grok_initial_snapshot("v-test")

    assert snapshot.output_dir == str(target_dir)
    assert not target_dir.exists()


def test_runtime_helpers_preserve_production_defaults_when_unconfigured(monkeypatch, tmp_path) -> None:
    """Keep runtime injection opt-in outside pytest."""
    home_dir = tmp_path / "home"
    monkeypatch.delenv(config.RUNTIME_ROOT_ENV)
    monkeypatch.delenv(config.LEGACY_RUNTIME_ROOT_ENV, raising=False)
    monkeypatch.delenv(config.SETTINGS_PATH_ENV)
    monkeypatch.delenv(config.LEGACY_SETTINGS_PATH_ENV, raising=False)
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("USERPROFILE", str(home_dir))
    monkeypatch.setenv("APPDATA", str(home_dir / "AppData/Roaming"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home_dir / ".config"))

    assert config.resolve_runtime_root() == config.PROJECT_ROOT
    if config.is_macos_host():
        expected_path = home_dir / "Library/Application Support/agenticContext/settings.json"
    elif config.is_windows_host():
        expected_path = home_dir / "AppData/Roaming/agenticContext/settings.json"
    else:
        expected_path = home_dir / ".config/agenticContext/settings.json"

    assert config.default_settings_path() == expected_path


def test_config_read_write_helpers_resolve_settings_path_at_call_time(monkeypatch, tmp_path) -> None:
    """Resolve redirected settings paths when the helper is called, not imported."""
    first_path = tmp_path / "first" / "settings.json"
    second_path = tmp_path / "second" / "settings.json"

    monkeypatch.setenv(config.SETTINGS_PATH_ENV, str(first_path))
    config.save_config(config.CrawlConfig(account_name_override="first"))

    monkeypatch.setenv(config.SETTINGS_PATH_ENV, str(second_path))
    config.save_config(config.CrawlConfig(account_name_override="second"))

    assert first_path.is_file()
    assert second_path.is_file()
    assert config.load_saved_config().account_name_override == "second"
