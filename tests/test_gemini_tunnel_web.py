"""Gemini HTTPS/OAuth MCP ingress and local UI API tests.

Code version: v1.5.0-codex.0
"""

from __future__ import annotations

import base64
import hashlib
from io import BytesIO
import logging
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from werkzeug.test import EnvironBuilder

from app.web.app import create_app


EXPECTED_TOOLS = [
    "project_overview",
    "list_files",
    "search_files",
    "read_files",
    "apply_edits",
    "write_file",
    "delete_file",
    "run_check",
    "show_changes",
    "review_changes",
]
PUBLIC_ORIGIN = "https://agent.example.com"
CONSUMER_CALLBACK = "https://consumer-callback.example/oauth/callback"


@pytest.fixture
def gemini_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        "AGENTIC_CONTEXT_SETTINGS_PATH",
        str(tmp_path / "settings" / "settings.json"),
    )
    application = create_app(
        tmp_path / "store",
        computer_use_settings_path=tmp_path / "computer-use-settings.json",
        computer_use_runtime_root=tmp_path / "runtime",
        agent_external_operations_enabled=False,
    )
    application.config.update(TESTING=True)
    with application.test_client() as client:
        yield client


def configure(client) -> dict[str, str]:
    response = client.post(
        "/api/agent/tunnel/gemini/config",
        json={"public_origin": PUBLIC_ORIGIN},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["platform"] == "gemini"
    assert payload["presentation"]["tone"] == "configured"
    assert payload["presentation"]["label"] == "Configured"
    assert payload["activity_observed"] is False
    assert "gts_" not in str(payload)
    assert "gtk_" not in str(payload)
    values = {}
    for kind in ("mcp_url", "client_id", "client_secret"):
        copied = client.post(
            "/api/agent/tunnel/gemini/copy-value",
            json={"value": kind},
        )
        assert copied.status_code == 200
        assert "no-store" in copied.headers["Cache-Control"]
        values[kind] = copied.get_json()["value"]
    return values


def test_composition_root_owns_the_app_local_gemini_gateway(gemini_client) -> None:
    application = gemini_client.application
    gateway = application.extensions["gemini_tunnel_gateway"]

    assert gateway.tunnel_mcp_service is application.extensions["tunnel_mcp_service"]


def oauth_authorization_request(
    values: dict[str, str],
    *,
    redirect_uri: str = CONSUMER_CALLBACK,
    state: str = "state-123",
) -> tuple[str, dict[str, str]]:
    verifier = "v" * 43
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, {
        "response_type": "code",
        "client_id": values["client_id"],
        "redirect_uri": redirect_uri,
        "resource": values["mcp_url"],
        "scope": "mcp:tools",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }


def oauth_tokens(client, values: dict[str, str]) -> dict[str, object]:
    verifier, query = oauth_authorization_request(values)
    pending = client.get(
        "/oauth/authorize",
        base_url=PUBLIC_ORIGIN,
        query_string=query,
    )
    assert pending.status_code == 202
    assert "Location" not in pending.headers
    assert 'http-equiv="refresh"' in pending.get_data(as_text=True)

    status = client.get(
        "/api/agent/tunnel/status?platform=gemini"
    ).get_json()
    assert status["authorization"]["pending"] is True
    assert status["authorization"]["redirect_uri"] == CONSUMER_CALLBACK
    review_id = status["authorization"]["review_id"]
    approved = client.post(
        "/api/agent/tunnel/gemini/authorization",
        json={"action": "approve", "review_id": review_id},
    )
    assert approved.status_code == 200
    assert approved.get_json()["authorization"]["pending"] is False

    authorize = client.get(
        "/oauth/authorize",
        base_url=PUBLIC_ORIGIN,
        query_string=query,
    )
    assert authorize.status_code == 302
    redirect_query = parse_qs(urlsplit(authorize.headers["Location"]).query)
    assert redirect_query["state"] == ["state-123"]
    assert redirect_query["iss"] == [PUBLIC_ORIGIN]
    code = redirect_query["code"][0]
    basic = base64.b64encode(
        f"{values['client_id']}:{values['client_secret']}".encode()
    ).decode()
    token = client.post(
        "/oauth/token",
        base_url=PUBLIC_ORIGIN,
        headers={"Authorization": f"Basic {basic}"},
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CONSUMER_CALLBACK,
            "resource": values["mcp_url"],
            "scope": "mcp:tools",
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200
    assert "no-store" in token.headers["Cache-Control"]
    return token.get_json()


def test_local_config_status_and_html_never_return_gemini_secrets(gemini_client) -> None:
    initial = gemini_client.get(
        "/api/agent/tunnel/status?platform=gemini"
    ).get_json()
    assert initial["platform"] == "gemini"
    assert initial["presentation"]["label"] == "Not configured"

    values = configure(gemini_client)
    assert values["mcp_url"] == f"{PUBLIC_ORIGIN}/mcp/gemini"
    assert values["client_id"].startswith("gtc_")
    assert values["client_secret"].startswith("gts_")

    status_text = gemini_client.get(
        "/api/agent/tunnel/status?platform=gemini"
    ).get_data(as_text=True)
    html = gemini_client.get("/agent/tunnel/gemini").get_data(as_text=True)
    for secret in (values["client_secret"],):
        assert secret not in status_text
        assert secret not in html
    assert "Connect Gemini to this local project" in html
    assert "Copy client secret" in html
    assert "/mcp/gemini" in html
    assert "sk-proj-" not in html[html.index('data-agent-tunnel-provider-panel="gemini"') :]


def test_public_host_is_path_confined_and_challenges_unauthorized_mcp(gemini_client) -> None:
    configure(gemini_client)

    metadata = gemini_client.get(
        "/.well-known/oauth-protected-resource/mcp/gemini",
        base_url=PUBLIC_ORIGIN,
    )
    assert metadata.status_code == 200
    assert metadata.get_json()["resource"] == f"{PUBLIC_ORIGIN}/mcp/gemini"
    assert metadata.headers["Referrer-Policy"] == "no-referrer"

    unauthorized = gemini_client.get("/mcp/gemini", base_url=PUBLIC_ORIGIN)
    assert unauthorized.status_code == 401
    assert (
        f'resource_metadata="{PUBLIC_ORIGIN}/.well-known/'
        'oauth-protected-resource/mcp/gemini"'
    ) in unauthorized.headers["WWW-Authenticate"]

    assert gemini_client.get("/", base_url=PUBLIC_ORIGIN).status_code == 404
    assert (
        gemini_client.get("/agent/tunnel/gemini", base_url=PUBLIC_ORIGIN).status_code
        == 404
    )
    assert gemini_client.get("/mcp", base_url=PUBLIC_ORIGIN).status_code == 404
    assert (
        gemini_client.post(
            "/api/agent/tunnel/gemini/copy-value",
            base_url=PUBLIC_ORIGIN,
            json={"value": "client_secret"},
        ).status_code
        == 404
    )
    assert (
        gemini_client.get(
            "/.well-known/oauth-authorization-server",
            base_url=PUBLIC_ORIGIN,
            environ_overrides={"REMOTE_ADDR": "203.0.113.10"},
        ).status_code
        == 403
    )


def test_oauth_round_trip_discovers_exact_shared_tools_and_activates_status(
    gemini_client,
) -> None:
    values = configure(gemini_client)
    tokens = oauth_tokens(gemini_client, values)
    authorization = {"Authorization": f"Bearer {tokens['access_token']}"}

    listed = gemini_client.post(
        "/mcp/gemini",
        base_url=PUBLIC_ORIGIN,
        headers=authorization,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert listed.status_code == 200
    assert [tool["name"] for tool in listed.get_json()["result"]["tools"]] == EXPECTED_TOOLS

    called = gemini_client.post(
        "/mcp/gemini",
        base_url=PUBLIC_ORIGIN,
        headers=authorization,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "list_files",
                "arguments": {"project": "missing"},
            },
        },
    )
    assert called.status_code == 200

    status = gemini_client.get(
        "/api/agent/tunnel/status?platform=gemini"
    ).get_json()
    assert status["activity_observed"] is True
    assert status["presentation"]["label"] == "Active"
    assert status["recent_calls"][0]["provider"] == "gemini"


def test_authorization_requires_exact_local_review_and_secret_copy_is_not_approval(
    gemini_client,
) -> None:
    values = configure(gemini_client)
    _verifier, query = oauth_authorization_request(values)

    head = gemini_client.head(
        "/oauth/authorize",
        base_url=PUBLIC_ORIGIN,
        query_string=query,
    )
    assert head.status_code == 405
    assert head.headers["Allow"] == "GET"
    assert gemini_client.get(
        "/api/agent/tunnel/status?platform=gemini"
    ).get_json()["authorization"]["pending"] is False

    pending = gemini_client.get(
        "/oauth/authorize",
        base_url=PUBLIC_ORIGIN,
        query_string=query,
    )
    assert pending.status_code == 202
    assert "Location" not in pending.headers
    assert CONSUMER_CALLBACK not in pending.get_data(as_text=True)
    status = gemini_client.get(
        "/api/agent/tunnel/status?platform=gemini"
    ).get_json()
    assert status["authorization"]["pending"] is True
    assert status["authorization"]["redirect_uri"] == CONSUMER_CALLBACK
    assert status["authorization"]["redirect_host"] == "consumer-callback.example"
    review_id = status["authorization"]["review_id"]
    assert len(review_id) >= 16

    malformed_review = gemini_client.post(
        "/api/agent/tunnel/gemini/authorization",
        json={"action": "approve", "review_id": "é" * 16},
    )
    assert malformed_review.status_code == 400
    stale_review = gemini_client.post(
        "/api/agent/tunnel/gemini/authorization",
        json={"action": "approve", "review_id": "A" * 32},
    )
    assert stale_review.status_code == 409
    assert "changed" in stale_review.get_json()["error"]
    assert gemini_client.get(
        "/api/agent/tunnel/status?platform=gemini"
    ).get_json()["authorization"]["review_id"] == review_id

    changed_uri = gemini_client.get(
        "/oauth/authorize",
        base_url=PUBLIC_ORIGIN,
        query_string={
            **query,
            "redirect_uri": "https://different-callback.example/oauth/callback",
        },
    )
    assert changed_uri.status_code == 409
    assert changed_uri.get_json()["error"] == "temporarily_unavailable"
    changed_state = gemini_client.get(
        "/oauth/authorize",
        base_url=PUBLIC_ORIGIN,
        query_string={**query, "state": "different-state"},
    )
    assert changed_state.status_code == 409
    assert changed_state.get_json()["error"] == "temporarily_unavailable"
    empty_state = gemini_client.get(
        "/oauth/authorize",
        base_url=PUBLIC_ORIGIN,
        query_string={**query, "state": ""},
    )
    assert empty_state.status_code == 400
    assert empty_state.get_json()["error"] == "invalid_request"

    approved = gemini_client.post(
        "/api/agent/tunnel/gemini/authorization",
        json={"action": "approve", "review_id": review_id},
    )
    assert approved.status_code == 200

    changed_uri_after_approval = gemini_client.get(
        "/oauth/authorize",
        base_url=PUBLIC_ORIGIN,
        query_string={
            **query,
            "redirect_uri": "https://different-callback.example/oauth/callback",
        },
    )
    assert changed_uri_after_approval.status_code == 409
    assert (
        changed_uri_after_approval.get_json()["error"]
        == "temporarily_unavailable"
    )
    changed_after_approval = gemini_client.get(
        "/oauth/authorize",
        base_url=PUBLIC_ORIGIN,
        query_string={**query, "state": "different-state"},
    )
    assert changed_after_approval.status_code == 409
    assert changed_after_approval.get_json()["error"] == "temporarily_unavailable"
    changed_pkce_after_approval = gemini_client.get(
        "/oauth/authorize",
        base_url=PUBLIC_ORIGIN,
        query_string={**query, "code_challenge": "A" * 43},
    )
    assert changed_pkce_after_approval.status_code == 409
    assert (
        changed_pkce_after_approval.get_json()["error"]
        == "temporarily_unavailable"
    )
    authorized = gemini_client.get(
        "/oauth/authorize",
        base_url=PUBLIC_ORIGIN,
        query_string=query,
    )
    assert authorized.status_code == 302
    assert urlsplit(authorized.headers["Location"]).netloc == "consumer-callback.example"


def test_authorization_denial_rejects_the_same_exact_request(gemini_client) -> None:
    values = configure(gemini_client)
    _verifier, query = oauth_authorization_request(values)
    assert gemini_client.get(
        "/oauth/authorize",
        base_url=PUBLIC_ORIGIN,
        query_string=query,
    ).status_code == 202
    review_id = gemini_client.get(
        "/api/agent/tunnel/status?platform=gemini"
    ).get_json()["authorization"]["review_id"]
    denied = gemini_client.post(
        "/api/agent/tunnel/gemini/authorization",
        json={"action": "deny", "review_id": review_id},
    )
    assert denied.status_code == 200
    assert denied.get_json()["authorization"]["pending"] is False

    rejected = gemini_client.get(
        "/oauth/authorize",
        base_url=PUBLIC_ORIGIN,
        query_string=query,
    )
    assert rejected.status_code == 400
    assert rejected.get_json()["error"] == "access_denied"
    assert "Location" not in rejected.headers


def test_first_approved_callback_is_pinned_for_the_process(gemini_client) -> None:
    values = configure(gemini_client)
    _verifier, query = oauth_authorization_request(values)
    assert gemini_client.get(
        "/oauth/authorize",
        base_url=PUBLIC_ORIGIN,
        query_string=query,
    ).status_code == 202
    review_id = gemini_client.get(
        "/api/agent/tunnel/status?platform=gemini"
    ).get_json()["authorization"]["review_id"]
    assert gemini_client.post(
        "/api/agent/tunnel/gemini/authorization",
        json={"action": "approve", "review_id": review_id},
    ).status_code == 200
    assert gemini_client.get(
        "/oauth/authorize",
        base_url=PUBLIC_ORIGIN,
        query_string=query,
    ).status_code == 302

    changed_callback = gemini_client.get(
        "/oauth/authorize",
        base_url=PUBLIC_ORIGIN,
        query_string={
            **query,
            "redirect_uri": "https://different-callback.example/oauth/callback",
        },
    )
    assert changed_callback.status_code == 400
    assert changed_callback.get_json()["error"] == "invalid_request"


def test_credential_rotation_revokes_tokens_and_resets_active_evidence(
    gemini_client,
) -> None:
    values = configure(gemini_client)
    tokens = oauth_tokens(gemini_client, values)
    authorization = {"Authorization": f"Bearer {tokens['access_token']}"}
    called = gemini_client.post(
        "/mcp/gemini",
        base_url=PUBLIC_ORIGIN,
        headers=authorization,
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "list_files",
                "arguments": {"project": "missing"},
            },
        },
    )
    assert called.status_code == 200
    assert gemini_client.get(
        "/api/agent/tunnel/status?platform=gemini"
    ).get_json()["activity_observed"] is True

    _verifier, pending_query = oauth_authorization_request(
        values,
        state="rotation-pending",
    )
    pending = gemini_client.get(
        "/oauth/authorize",
        base_url=PUBLIC_ORIGIN,
        query_string=pending_query,
    )
    assert pending.status_code == 202
    assert gemini_client.get(
        "/api/agent/tunnel/status?platform=gemini"
    ).get_json()["authorization"]["pending"] is True

    cleared = gemini_client.post(
        "/api/agent/tunnel/gemini/config",
        json={"public_origin": ""},
    )
    assert cleared.status_code == 200
    assert cleared.get_json()["activity_observed"] is False
    assert cleared.get_json()["authorization"]["pending"] is False
    replaced = gemini_client.post(
        "/api/agent/tunnel/gemini/config",
        json={"public_origin": PUBLIC_ORIGIN},
    )
    assert replaced.status_code == 200
    replacement_status = replaced.get_json()
    assert replacement_status["presentation"]["label"] == "Configured"
    assert replacement_status["activity_observed"] is False
    assert replacement_status["authorization"]["pending"] is False

    rejected = gemini_client.post(
        "/mcp/gemini",
        base_url=PUBLIC_ORIGIN,
        headers=authorization,
        json={"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
    )
    assert rejected.status_code == 401


def _streaming_environ(
    path: str,
    body: bytes,
    content_type: str,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, object]:
    builder = EnvironBuilder(
        path=path,
        base_url=PUBLIC_ORIGIN,
        method="POST",
        input_stream=BytesIO(body),
        content_length=len(body),
        content_type=content_type,
        headers=headers,
    )
    environ = builder.get_environ()
    environ.pop("CONTENT_LENGTH", None)
    environ["wsgi.input_terminated"] = True
    return environ


def test_streaming_bodies_without_content_length_still_hit_route_limits(
    gemini_client,
) -> None:
    values = configure(gemini_client)
    tokens = oauth_tokens(gemini_client, values)

    oversized_form = urlencode(
        [
            ("grant_type", "refresh_token"),
            ("refresh_token", str(tokens["refresh_token"])),
            ("resource", values["mcp_url"]),
            ("scope", "mcp:tools"),
            ("client_id", values["client_id"]),
            ("client_secret", values["client_secret"]),
            ("padding", "x" * (16 * 1024)),
        ]
    ).encode()
    oauth_response = gemini_client.open(
        _streaming_environ(
            "/oauth/token",
            oversized_form,
            "application/x-www-form-urlencoded",
        )
    )
    assert oauth_response.status_code == 413
    assert oauth_response.get_json()["error"] == "invalid_request"

    oversized_json = (
        b'{"jsonrpc":"2.0","id":99,"method":"tools/list","params":{}}'
        + (b" " * (2 * 1024 * 1024))
    )
    mcp_response = gemini_client.open(
        _streaming_environ(
            "/mcp/gemini",
            oversized_json,
            "application/json",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
    )
    assert mcp_response.status_code == 413
    assert mcp_response.get_json()["error"]["message"] == "Request is too large."


def test_runtime_logs_never_include_gemini_secrets_or_bearer_tokens(
    gemini_client,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    values = configure(gemini_client)
    tokens = oauth_tokens(gemini_client, values)
    response = gemini_client.post(
        "/mcp/gemini",
        base_url=PUBLIC_ORIGIN,
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "list_files",
                "arguments": {"project": "missing"},
            },
        },
    )
    assert response.status_code == 200
    for private_value in (
        values["client_secret"],
        tokens["access_token"],
        tokens["refresh_token"],
    ):
        assert str(private_value) not in caplog.text


@pytest.mark.parametrize(
    "origin",
    [
        "http://agent.example.com",
        "https://127.0.0.1",
        "https://localhost",
        "https://agent.example.com/path",
        "https://user:secret@agent.example.com",
    ],
)
def test_local_config_rejects_non_public_or_non_origin_urls(
    gemini_client,
    origin: str,
) -> None:
    response = gemini_client.post(
        "/api/agent/tunnel/gemini/config",
        json={"public_origin": origin},
    )
    assert response.status_code == 400
    assert "gts_" not in response.get_data(as_text=True)
