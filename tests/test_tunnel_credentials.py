"""Tunnel credential storage and Agent Tunnel route tests.

Code version: v1.7.4-codex.0
"""

from __future__ import annotations

import json
import os
import re
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
    onboarding_start = empty_body.index(
        'data-agent-tunnel-provider-panel="chatgpt"'
    )
    onboarding_end = empty_body.index(
        'data-agent-tunnel-provider-panel="gemini"', onboarding_start
    )
    onboarding_body = empty_body[onboarding_start:onboarding_end]
    assert '<ol class="process-list agent-tunnel-onboarding-steps" role="list">' in onboarding_body
    for step_number in range(1, 5):
        assert (
            '<span class="process-list-marker agent-tunnel-step-number" aria-hidden="true">'
            f"{step_number}</span>"
        ) in onboarding_body
    assert onboarding_body.count('<h3 class="process-list-heading agent-tunnel-step-heading">') == 4
    assert onboarding_body.count("data-process-continues") == 3
    guide_start = onboarding_body.index('<ul class="agent-tunnel-guide-list agent-tunnel-bullet-list ')
    guide_end = onboarding_body.index("</ul>", guide_start) + len("</ul>")
    guide_body = onboarding_body[guide_start:guide_end]
    assert guide_body.count('<li class="agent-tunnel-guide-item">') == 4
    assert "agent-tunnel-guide-number" not in guide_body
    assert re.findall(
        r'<span class="agent-tunnel-guide-title"[^>]*>(.*?)</span>',
        guide_body,
    ) == [
        "Create a tunnel",
        "Copy the Tunnel ID",
        "Create an API key",
        "Copy the secret key",
    ]
    detail_tags = re.findall(r"<details\b[^>]*>", guide_body)
    assert len(detail_tags) == 4
    assert all(
        re.search(r'class="[^"]*\bui-collapse\b[^"]*"', tag) is not None
        for tag in detail_tags
    )
    assert all(re.search(r"\sopen(?:\s|>)", tag) is None for tag in detail_tags)
    assert guide_body.count("<summary>") == 4
    assert len(re.findall(r"<svg\b", guide_body)) == 4
    assert guide_body.count("data-agent-tunnel-guide-scroll") == 0
    assert guide_body.count('role="region"') == 4
    assert 'tabindex="0"' not in guide_body
    assert guide_body.count('aria-labelledby="agent_tunnel_guide_title_') == 4
    guide_lower = guide_body.lower()
    for unsafe_markup in ("<img", "<image", "<foreignobject", "data:image"):
        assert unsafe_markup not in guide_lower
    assert "<circle" not in guide_lower
    assert "guide-window" not in guide_body
    assert guide_body.count('class="guide-card"') == 3
    assert guide_body.count('viewBox="0 0 640 ') == 4
    assert 'class="guide-control guide-accent-stroke" x="80" y="427"' not in guide_body
    guide_select_chevrons = re.findall(
        r'<path class="guide-select-chevron" data-guide-select-chevron d="([^"]+)">',
        guide_body,
    )
    assert len(guide_select_chevrons) == 4
    assert all(
        re.fullmatch(r"M\d+ \d+l5 5 5-5", path)
        for path in guide_select_chevrons
    )
    guide_rects = re.findall(r"<rect\b[^>]*>", guide_body)
    assert guide_rects
    assert all(re.search(r'\brx="10"', rect) for rect in guide_rects)
    assert 'data-guide-expiration="never"' in guide_body
    assert 'data-guide-selected="all"' in guide_body
    assert ">Expiration<" in guide_body
    assert ">Never<" in guide_body
    assert ">Permissions<" in guide_body
    assert ">All<" in guide_body
    assert "Read + Use" not in guide_body
    assert "Read and write API resources" in guide_body
    assert re.search(r"tunnel_[A-Za-z0-9]{16,}", guide_body) is None
    assert re.search(r"sk-proj-[A-Za-z0-9_-]{12,}", guide_body) is None
    assert (
        'href="https://platform.openai.com/settings/organization/api-keys"'
        in guide_body
    )
    chatgpt_guide_start = onboarding_body.index(
        '<ul class="agent-tunnel-guide-list agent-tunnel-bullet-list agent-tunnel-chatgpt-guide-list '
    )
    chatgpt_guide_end = onboarding_body.index("</ul>", chatgpt_guide_start) + len("</ul>")
    chatgpt_guide_body = onboarding_body[chatgpt_guide_start:chatgpt_guide_end]
    assert re.findall(
        r'<span class="agent-tunnel-guide-title"[^>]*>(.*?)</span>',
        chatgpt_guide_body,
    ) == [
        "Enable Developer mode",
        "Create the AgenticContext plugin",
    ]
    assert chatgpt_guide_body.count('<details class="ui-collapse agent-tunnel-guide"') == 2
    assert chatgpt_guide_body.count('<svg class="agent-tunnel-guide-svg"') == 2
    assert chatgpt_guide_body.count('class="guide-card"') == 1
    assert chatgpt_guide_body.count('viewBox="0 0 640 ') == 2
    assert chatgpt_guide_body.count("data-guide-select-chevron") == 2
    assert 'data-guide-selected="tunnel"' in chatgpt_guide_body
    assert 'data-guide-existing-tunnel' in chatgpt_guide_body
    assert 'data-guide-auth="none"' in chatgpt_guide_body
    assert "No Auth" in chatgpt_guide_body
    assert 'href="https://chatgpt.com/plugins"' in chatgpt_guide_body
    chatgpt_guide_rects = re.findall(r"<rect\b[^>]*>", chatgpt_guide_body)
    assert chatgpt_guide_rects
    assert all(re.search(r'\brx="10"', rect) for rect in chatgpt_guide_rects)
    assert re.search(r"sk-proj-[A-Za-z0-9_-]{12,}", chatgpt_guide_body) is None
    assert 'target="_blank" rel="noopener noreferrer"' in empty_body
    assert "agent-tunnel-substep-number" not in onboarding_body
    credential_sequence_start = onboarding_body.index(
        '<ul class="agent-tunnel-numbered-list agent-tunnel-bullet-list agent-tunnel-credential-fields"'
    )
    credential_sequence_end = onboarding_body.index("</ul>", credential_sequence_start)
    credential_sequence = onboarding_body[
        credential_sequence_start:credential_sequence_end
    ]
    assert credential_sequence.count('<li class="agent-tunnel-numbered-item">') == 2
    assert credential_sequence.index("Tunnel ID") < credential_sequence.index("Tunnel API key")
    assert 'placeholder=' not in credential_sequence
    kickoff_sequence_start = onboarding_body.index(
        '<ul class="agent-tunnel-numbered-list agent-tunnel-bullet-list agent-tunnel-kickoff-sequence"'
    )
    kickoff_sequence_end = onboarding_body.index("</ul>", kickoff_sequence_start)
    kickoff_sequence = onboarding_body[kickoff_sequence_start:kickoff_sequence_end]
    assert (
        kickoff_sequence.index("data-agent-tunnel-kickoff")
        < kickoff_sequence.index("Copy this prompt")
        < kickoff_sequence.index("Edit and copy the prompt")
        < kickoff_sequence.index("Ask in ChatGPT")
    )
    assert "Edit and copy the prompt" in kickoff_sequence
    assert 'data-agent-tunnel-kickoff-next-step aria-live="polite"' in kickoff_sequence
    assert "Complete the Tunnel credentials" in kickoff_sequence
    assert 'data-agent-tunnel-kickoff-state="credentials"' in kickoff_sequence
    assert '<textarea id="agent_tunnel_kickoff"' in kickoff_sequence
    assert "After copying the prompt" not in kickoff_sequence
    assert "After creation, allow all actions" not in onboarding_body
    assert "Connect ChatGPT to this local project" in empty_body
    assert "Install and prepare" not in empty_body
    assert onboarding_body.count('class="secondary-button agent-tunnel-step-action"') == 6
    assert 'data-agent-tunnel-toggle' not in empty_body
    assert 'data-agent-tunnel-connect-url="/api/agent/tunnel/connect"' in empty_body
    assert 'data-agent-tunnel-reconnect' in empty_body
    assert 'data-agent-tunnel-disconnect-url' not in empty_body
    assert 'data-agent-tunnel-message' not in empty_body
    assert 'data-agent-tunnel-activity' not in empty_body

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
    key_input = re.search(r'<input id="chatgpt_tunnel_api_key"[^>]*>', body).group(0)
    assert 'value=""' in key_input
    assert 'placeholder="••••••••HMAA"' in key_input
    assert "sk-proj-secret-HMAA" not in body


def test_clearing_saved_credentials_immediately_revokes_the_live_bearer(
    agent_client,
    credentials_path: Path,
) -> None:
    credentials = TunnelCredentials(
        "tunnel_" + "b" * 32,
        "sk-proj-existing-secret",
    )
    save_tunnel_credentials(credentials, credentials_path)
    runtime = agent_client.application.extensions["tunnel_runtime"]
    runtime._enabled = True
    runtime._authorization = "Bearer active-test-token"

    response = agent_client.post(
        "/api/agent/tunnel/credentials",
        json={"tunnel_id": "", "api_key": ""},
    )

    assert response.status_code == 200
    assert not load_tunnel_credentials(credentials_path).configured
    assert runtime.authorization_matches("Bearer active-test-token") is False


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
