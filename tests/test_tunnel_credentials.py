"""Tunnel credential storage and Agent Tunnel route tests.

Code version: v1.2.0-codex.0
"""

from __future__ import annotations

import json
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


def test_tunnel_credentials_move_from_settings_to_agent_and_never_echo_key(agent_client) -> None:
    settings_body = agent_client.get("/settings").get_data(as_text=True)
    assert 'name="chatgpt_tunnel_id"' not in settings_body
    assert 'name="chatgpt_tunnel_api_key"' not in settings_body
    assert "settings-tunnel-package" not in settings_body
    assert "settings-tunnel.js" not in settings_body

    empty_body = agent_client.get("/agent/tunnel/chatgpt").get_data(as_text=True)
    assert 'id="chatgpt_tunnel_id"' in empty_body
    assert 'id="chatgpt_tunnel_api_key"' in empty_body
    assert 'data-agent-tunnel-credentials-url="/api/agent/tunnel/credentials"' in empty_body
    for marker in ("➊", "➋", "➌", "➍"):
        assert marker in empty_body
    assert 'target="_blank" rel="noopener noreferrer"' in empty_body
    assert "Connect ChatGPT to this local project" in empty_body
    assert "Install and prepare" not in empty_body
    assert 'class="secondary-button browser-refresh-button agent-tunnel-step-action"' in empty_body

    invalid = agent_client.post(
        "/api/agent/tunnel/credentials",
        json={"tunnel_id": "tunnel_bad", "api_key": "sk-proj-secret-HMAA"},
    )
    assert invalid.status_code == 400
    assert not load_tunnel_credentials().configured

    tunnel_id = "tunnel_" + "a" * 32
    response = agent_client.post(
        "/api/agent/tunnel/credentials",
        json={"tunnel_id": tunnel_id, "api_key": "sk-proj-secret-HMAA"},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["credentials"]["qualified"] is True
    assert payload["credentials"]["tunnel_id_valid"] is True
    assert payload["credentials"]["api_key_saved"] is True
    assert "sk-proj-secret-HMAA" not in str(payload)
    assert load_tunnel_credentials() == TunnelCredentials(tunnel_id, "sk-proj-secret-HMAA")

    body = agent_client.get("/agent/tunnel/chatgpt").get_data(as_text=True)
    assert f'value="{tunnel_id}"' in body
    assert 'placeholder="••••••••HMAA"' in body
    assert "sk-proj-secret-HMAA" not in body


def test_agent_tunnel_route_reflects_tunnel_status(agent_client) -> None:
    default_redirect = agent_client.get("/agent")
    assert default_redirect.status_code == 302
    assert default_redirect.headers["Location"] == "/agent/tunnel/chatgpt"

    redirect = agent_client.get("/agent/tunnel/")
    assert redirect.status_code == 302
    assert redirect.headers["Location"] == "/agent/tunnel/chatgpt"
    assert agent_client.get("/agent/tunnel/unknown").status_code == 404

    body = agent_client.get("/agent/tunnel/chatgpt").get_data(as_text=True)
    assert 'data-segmented-active-index="0"' in body
    assert body.index("<span>Tunnel</span>") < body.index("<span>Browser</span>")
    assert 'value="tunnel" data-agent-connection-mode checked' in body
    assert "data-agent-tunnel-state>Not configured</span>" in body
    assert 'data-agent-tunnel-onboarding' in body
    assert 'data-agent-browser-task hidden' in body
    assert 'href="/settings#settings-llm"' not in body
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
    assert status["credentials"]["qualified"] is True
    assert status["presentation"]["action"] is None
    assert "sk-proj-secret" not in str(status)

    browser_body = agent_client.get("/agent/edge/chatgpt").get_data(as_text=True)
    assert 'data-segmented-active-index="1"' in browser_body
    assert 'value="browser" data-agent-connection-mode checked' in browser_body
    assert "data-agent-tunnel-mode-field" in browser_body

@pytest.mark.parametrize(
    "payload",
    [{}, [], False, None, {"tunnel_id": None}, {"tunnel_id": "tunnel_" + "b" * 32, "api_key": []}],
)
def test_malformed_tunnel_credentials_do_not_clear_saved_pair(agent_client, payload) -> None:
    saved = TunnelCredentials("tunnel_" + "b" * 32, "sk-proj-onboarding-test-ABCD")
    save_tunnel_credentials(saved)

    response = agent_client.post(
        "/api/agent/tunnel/credentials",
        data=json.dumps(payload),
        content_type="application/json",
    )

    assert response.status_code == 400
    assert load_tunnel_credentials() == saved
    assert saved.api_key not in response.get_data(as_text=True)
