"""Preserve resource diagnostics when Stop ends an Agent turn.

Code version: v1.0.0
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from app.core import browser_sessions, computer_use_agent
from app.core.config import CrawlConfig
from app.core.computer_use_agent import ComputerUseAgentService, ComputerUseSettingsStore

def _descriptor(source: Path) -> browser_sessions.BrowserDescriptor:
    return browser_sessions.BrowserDescriptor(
        browser_id="edge",
        label="Edge",
        icon_filename="",
        engine="chromium",
        user_data_dir=source,
        profile_directory="Default",
        channel="msedge",
    )

def test_stop_preserves_browser_profile_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "synthetic-source"
    source.mkdir()
    retained = tmp_path / "cachelikes-edge-synthetic-retained"
    retained.mkdir()
    profile = SimpleNamespace(
        name=str(retained),
        cleanup=Mock(side_effect=PermissionError("Synthetic profile remains locked")),
    )
    context = SimpleNamespace(close=Mock())
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch_persistent_context=Mock(return_value=context))
    )
    monkeypatch.setattr(browser_sessions, "is_macos_host", lambda: False)
    monkeypatch.setattr(browser_sessions, "is_windows_host", lambda: False)
    monkeypatch.setattr(browser_sessions, "_ACTIVE_CHROMIUM_PROFILE_ROOTS", {retained})
    monkeypatch.setattr(
        browser_sessions,
        "clone_browser_profile",
        lambda _descriptor: (retained, profile),
    )
    monkeypatch.setattr(computer_use_agent, "_start_macos_idle_sleep_assertion", lambda: None)

    def runner(**_kwargs: object) -> tuple[str, str, int, bool]:
        with browser_sessions.launch_chromium_context(
            playwright,
            _descriptor(source),
            headless=False,
        ):
            service._stop_requested.set()
            return "", "", 0, False

    service = ComputerUseAgentService(
        ComputerUseSettingsStore(tmp_path / "settings.json"),
        runner=runner,
        runtime_root=tmp_path / "runtime",
    )
    service.start("Inspect the synthetic workspace", str(workspace), CrawlConfig())
    assert service._worker is not None
    service._worker.join(timeout=5)
    assert not service._worker.is_alive()
    snapshot = service.snapshot()
    assert snapshot["phase"] == "stopped"
    assert snapshot["running"] is False
    assert "temporary browser profile" in snapshot["last_error"]
    assert "PermissionError" in snapshot["error_traceback"]
    assert str(retained) in snapshot["cleanup_error"]
    assert retained.is_dir()
    assert retained in browser_sessions._ACTIVE_CHROMIUM_PROFILE_ROOTS
    assert str(retained) in caplog.text
    assert "Could not remove" in caplog.text
    restored = ComputerUseAgentService(ComputerUseSettingsStore(tmp_path / "settings.json"),
                                       runner=runner, runtime_root=tmp_path / "runtime")
    assert str(retained) in restored.snapshot()["last_error"]

    checks = {item["id"]: item for item in restored.doctor()["checks"]}
    assert checks["browser_cleanup"]["status"] == "fail"
