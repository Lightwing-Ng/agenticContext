"""Tunnel credential storage and Agent Tunnel route tests.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.computer_use_agent import ComputerUseSettings
from app.core.tunnel_credentials import (
    TunnelCredentials,
    default_tunnel_credentials_path,
    load_tunnel_credentials,
    merge_tunnel_credentials,
    save_tunnel_credentials,
)
from app.web.app import create_app


@pytest.fixture
def credentials_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTIC_CONTEXT_SETTINGS_PATH", str(tmp_path / "settings.json"))
    return default_tunnel_credentials_path()


@pytest.fixture
def agent_client(credentials_path: Path):
    with patch(
        "app.core.computer_use_agent.load_computer_use_settings",
        return_value=ComputerUseSettings(browser="edge", platform="chatgpt"),
    ):
        application = create_app()
    application.config.update(TESTING=True)
    with application.test_client() as client:
        yield client


def test_credentials_round_trip_with_owner_only_file(credentials_path: Path) -> None:
    save_tunnel_credentials(TunnelCredentials("tunnel_abc", "sk-proj-secret-HMAA"))

    loaded = load_tunnel_credentials()
    assert loaded == TunnelCredentials("tunnel_abc", "sk-proj-secret-HMAA")
    assert loaded.configured
    assert loaded.snapshot() == {
        "configured": True,
        "tunnel_id": "tunnel_abc",
        "api_key_saved": True,
        "api_key_hint": "…HMAA",
    }
    if os.name == "posix":
        assert stat.S_IMODE(credentials_path.stat().st_mode) == 0o600


def test_merge_keeps_saved_key_when_blank_and_clears_with_tunnel_id() -> None:
    saved = TunnelCredentials("tunnel_abc", "sk-proj-old")

    assert merge_tunnel_credentials(saved, "tunnel_new", "") == TunnelCredentials(
        "tunnel_new", "sk-proj-old"
    )
    assert merge_tunnel_credentials(saved, "tunnel_abc", " sk-proj-new ") == TunnelCredentials(
        "tunnel_abc", "sk-proj-new"
    )
    assert merge_tunnel_credentials(saved, "", "") == TunnelCredentials()


def test_settings_saves_credentials_without_rendering_the_key(agent_client) -> None:
    empty_body = agent_client.get("/settings").get_data(as_text=True)
    assert 'name="chatgpt_tunnel_id"' in empty_body
    assert 'name="chatgpt_tunnel_api_key"' in empty_body
    assert 'type="password"' in empty_body

    response = agent_client.post(
        "/settings",
        data={"chatgpt_tunnel_id": "tunnel_abc", "chatgpt_tunnel_api_key": "sk-proj-secret-HMAA"},
    )
    assert response.status_code == 302
    assert load_tunnel_credentials().configured

    body = agent_client.get("/settings").get_data(as_text=True)
    assert 'value="tunnel_abc"' in body
    assert 'class="text-input-control-shell"' in body
    assert "data-text-input-clear" in body
    assert "sk-proj-secret-HMAA" not in body
    assert 'placeholder="Saved …HMAA"' in body


def test_agent_tunnel_route_reflects_tunnel_status(agent_client) -> None:
    redirect = agent_client.get("/agent/tunnel/")
    assert redirect.status_code == 302
    assert redirect.headers["Location"] == "/agent/tunnel/chatgpt"
    assert agent_client.get("/agent/tunnel/unknown").status_code == 404

    body = agent_client.get("/agent/tunnel/chatgpt").get_data(as_text=True)
    assert 'value="tunnel" data-agent-connection-mode checked' in body
    assert "data-agent-tunnel-state>Not configured</span>" in body
    assert 'href="/settings#settings-llm"' in body
    assert "Open Settings</a>" in body
    # Recent sessions belong to Browser runs only.
    assert 'aria-label="Recent sessions" data-agent-browser-mode-field hidden' in body

    save_tunnel_credentials(TunnelCredentials("tunnel_" + "a" * 32, "sk-proj-secret"))
    # Tests never start tunnel-client, so saved credentials alone are not "Connected".
    configured_body = agent_client.get("/agent/tunnel/chatgpt").get_data(as_text=True)
    assert "data-agent-tunnel-state>Not running</span>" in configured_body
    assert "sk-proj-secret" not in configured_body

    status = agent_client.get("/api/agent/tunnel/status").get_json()
    assert status["configured"] is True
    assert status["state"] == "disabled"
    assert status["presentation"]["label"] == "Not running"
    assert "sk-proj-secret" not in str(status)

    browser_body = agent_client.get("/agent/edge/chatgpt").get_data(as_text=True)
    assert 'value="browser" data-agent-connection-mode checked' in browser_body
    assert "data-agent-tunnel-mode-field" in browser_body
