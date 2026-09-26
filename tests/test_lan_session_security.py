"""Verify isolated LAN unlock sessions and their application-wide boundaries.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient

from app.core.agent import AGENT_ACCESS_SESSION_KEY
from app.web.app import create_app


LAN_HOST = "192.168.124.10"
LAN_ORIGIN = f"http://{LAN_HOST}:8666"
LAN_REQUEST = {
    "base_url": LAN_ORIGIN,
    "environ_overrides": {"REMOTE_ADDR": "192.168.124.20"},
}
TEST_PIN = "246810"


@pytest.fixture
def lan_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Flask]:
    """Keep browser operations and all mutable application state isolated."""
    monkeypatch.setenv("AGENTIC_CONTEXT_AGENT_PASSWORD", TEST_PIN)
    application = create_app(
        tmp_path / "local_store",
        computer_use_settings_path=tmp_path / "settings" / "computer_use.json",
        computer_use_runtime_root=tmp_path / "agent_runtime",
        tunnel_credentials_path=tmp_path / "settings" / "tunnel_credentials.json",
        tunnel_projects_path=tmp_path / "settings" / "tunnel_projects.json",
        tunnel_runtime_root=tmp_path / "tunnel_runtime",
        agent_external_operations_enabled=False,
    )
    application.config.update(TESTING=True)
    yield application
    application.extensions["runtime_shutdown"]()


@pytest.fixture
def lan_client(lan_app: Flask) -> FlaskClient:
    return lan_app.test_client()


def unlock(client: FlaskClient, pin: str = TEST_PIN):
    return client.post(
        "/agent/unlock",
        data={"password": pin},
        headers={"Origin": LAN_ORIGIN},
        **LAN_REQUEST,
    )


def test_correct_pin_issues_an_isolated_strict_signed_session(lan_app, lan_client):
    response = unlock(lan_client)

    assert response.status_code == 303
    cookie = response.headers["Set-Cookie"]
    assert cookie.startswith("agentic_context_session=")
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie
    assert "no-store" in response.headers["Cache-Control"]
    signed_cookie = lan_client.get_cookie("agentic_context_session", domain=LAN_HOST)
    serializer = lan_app.session_interface.get_signing_serializer(lan_app)
    assert serializer.loads(signed_cookie.value) == {AGENT_ACCESS_SESSION_KEY: True}
    assert lan_client.get("/api/zhihu/status", **LAN_REQUEST).status_code == 200


def test_wrong_pin_keeps_private_data_locked(lan_client):
    response = unlock(lan_client, "000000")

    assert response.status_code == 401
    assert "Set-Cookie" not in response.headers
    assert lan_client.get("/api/zhihu/status", **LAN_REQUEST).status_code == 401


@pytest.mark.parametrize("path", ["/api/zhihu/status", "/api/agent/status", "/api/browser-session"])
def test_lan_apis_require_an_authenticated_session(lan_client, path):
    response = lan_client.get(path, **LAN_REQUEST)

    assert response.status_code == 401
    assert not response.is_json


@pytest.mark.parametrize("path", ["/", "/settings", "/browser"])
def test_lan_pages_redirect_to_the_unlock_gate(lan_client, path):
    response = lan_client.get(path, **LAN_REQUEST)

    assert response.status_code == 302
    assert response.headers["Location"] == "/agent"


def test_worthward_cookie_cannot_unlock_or_overwrite_the_agent_session(lan_client):
    reference_app = Flask("worthward_session_reference")
    reference_app.secret_key = "isolated-worthward-session-test-key"
    serializer = reference_app.session_interface.get_signing_serializer(reference_app)
    worthward_cookie = serializer.dumps({"live_trading_unlocked": True})
    lan_client.set_cookie("session", worthward_cookie, domain=LAN_HOST)

    assert lan_client.get("/api/zhihu/status", **LAN_REQUEST).status_code == 401
    assert unlock(lan_client).status_code == 303
    assert lan_client.get_cookie("session", domain=LAN_HOST).value == worthward_cookie

    lan_client.set_cookie("session", "another-worthward-session", domain=LAN_HOST)
    assert lan_client.get("/api/zhihu/status", **LAN_REQUEST).status_code == 200


def test_a_cookie_signed_by_another_application_cannot_unlock_lan(lan_client):
    foreign_app = Flask("foreign_session_reference")
    foreign_app.secret_key = "isolated-foreign-session-test-key"
    serializer = foreign_app.session_interface.get_signing_serializer(foreign_app)
    forged_cookie = serializer.dumps({AGENT_ACCESS_SESSION_KEY: True})
    lan_client.set_cookie("agentic_context_session", forged_cookie, domain=LAN_HOST)

    assert lan_client.get("/api/zhihu/status", **LAN_REQUEST).status_code == 401


def test_successful_unlock_discards_prior_session_state(lan_client):
    with lan_client.session_transaction(**LAN_REQUEST) as prior_session:
        prior_session["unrelated_marker"] = "before-unlock"
        prior_session["live_trading_unlocked"] = True

    assert unlock(lan_client).status_code == 303
    with lan_client.session_transaction(**LAN_REQUEST) as current_session:
        assert dict(current_session) == {AGENT_ACCESS_SESSION_KEY: True}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "https://untrusted.example"},
        {"Origin": f"http://{LAN_HOST}:8688"},
        {"Origin": LAN_ORIGIN, "Sec-Fetch-Site": "cross-site"},
    ],
)
def test_unlock_rejects_missing_or_cross_origin_requests(lan_client, headers):
    response = lan_client.post(
        "/agent/unlock",
        data={"password": TEST_PIN},
        headers=headers,
        **LAN_REQUEST,
    )

    assert response.status_code == 403
    assert lan_client.get("/api/zhihu/status", **LAN_REQUEST).status_code == 401


def test_unlocked_session_still_rejects_cross_origin_writes(lan_client):
    assert unlock(lan_client).status_code == 303

    response = lan_client.post(
        "/settings",
        headers={"Origin": f"http://{LAN_HOST}:8688"},
        **LAN_REQUEST,
    )

    assert response.status_code == 403


def test_failed_unlocks_retain_the_peer_cooldown(lan_client, monkeypatch):
    now = [1_000.0]
    monkeypatch.setattr("app.web.agent_routes.monotonic", lambda: now[0])
    for _ in range(5):
        assert unlock(lan_client, "000000").status_code == 401

    limited = unlock(lan_client)
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1
    assert lan_client.get("/api/zhihu/status", **LAN_REQUEST).status_code == 401

    now[0] += 301
    assert unlock(lan_client).status_code == 303
