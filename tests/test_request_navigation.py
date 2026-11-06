"""Regression tests for document navigation across the local request boundary.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from app.web.app import create_app


NAVIGATION_HEADERS = {
    "Sec-Fetch-Site": "cross-site",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-User": "?1",
}


@pytest.fixture
def navigation_client(tmp_path: Path) -> Iterator[FlaskClient]:
    """Exercise real routes with disposable state and no external browser operations."""
    application = create_app(
        tmp_path / "local_store",
        agent_external_operations_enabled=False,
    )
    application.config.update(TESTING=True)
    with application.test_client() as client:
        yield client
    application.extensions["runtime_shutdown"]()


def test_user_activated_document_navigation_reaches_the_start_page(
    navigation_client: FlaskClient,
) -> None:
    """An explicit browser navigation must reach the page through its normal redirect."""
    response = navigation_client.get(
        "/",
        headers=NAVIGATION_HEADERS,
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert response.history
    assert response.history[0].status_code == 302
    assert "Cache" in response.get_data(as_text=True)


def test_document_navigation_headers_do_not_authorize_cross_site_writes(
    navigation_client: FlaskClient,
) -> None:
    response = navigation_client.post("/settings", headers=NAVIGATION_HEADERS)

    assert response.status_code == 403


@pytest.mark.parametrize(
    ("mode", "destination"),
    (
        ("navigate", "iframe"),
        ("navigate", "embed"),
        ("navigate", "object"),
        ("no-cors", "image"),
        ("no-cors", "script"),
        ("cors", "empty"),
        ("no-cors", "document"),
    ),
)
def test_embedded_and_fetch_requests_remain_cross_site_denied(
    navigation_client: FlaskClient,
    mode: str,
    destination: str,
) -> None:
    headers = {
        **NAVIGATION_HEADERS,
        "Sec-Fetch-Mode": mode,
        "Sec-Fetch-Dest": destination,
    }

    response = navigation_client.get("/", headers=headers)

    assert response.status_code == 403


@pytest.mark.parametrize("user_header", (None, "?0", "true"))
def test_unactivated_cross_site_document_navigation_remains_denied(
    navigation_client: FlaskClient,
    user_header: str | None,
) -> None:
    headers = dict(NAVIGATION_HEADERS)
    if user_header is None:
        headers.pop("Sec-Fetch-User")
    else:
        headers["Sec-Fetch-User"] = user_header

    response = navigation_client.get("/", headers=headers)

    assert response.status_code == 403


@pytest.mark.parametrize("path", ("/api/cache/activity", "/mcp", "/mcp/gemini"))
def test_document_navigation_does_not_authorize_cross_site_api_reads(
    navigation_client: FlaskClient,
    path: str,
) -> None:
    response = navigation_client.get(
        path,
        headers=NAVIGATION_HEADERS,
    )

    assert response.status_code == 403


def test_document_navigation_preserves_origin_validation(
    navigation_client: FlaskClient,
) -> None:
    response = navigation_client.get(
        "/",
        headers={**NAVIGATION_HEADERS, "Origin": "https://malicious.example"},
    )

    assert response.status_code == 403


def test_document_navigation_preserves_host_rebinding_protection(
    navigation_client: FlaskClient,
) -> None:
    response = navigation_client.get(
        "/",
        headers={**NAVIGATION_HEADERS, "Host": "malicious.example"},
    )

    assert response.status_code == 403


def test_lan_document_navigation_still_requires_the_existing_session_gate(
    navigation_client: FlaskClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The navigation exception must not turn an unauthenticated LAN client local."""
    monkeypatch.setenv("AGENTIC_CONTEXT_AGENT_PASSWORD", "246810")
    headers = {**NAVIGATION_HEADERS, "Host": "192.168.124.10:8666"}
    environment = {"REMOTE_ADDR": "192.168.124.20"}

    response = navigation_client.get(
        "/settings",
        headers=headers,
        environ_overrides=environment,
    )
    gate_response = navigation_client.get(
        response.headers.get("Location", "/agent"),
        headers=headers,
        environ_overrides=environment,
    )
    api_response = navigation_client.get(
        "/api/cache/activity",
        headers={"Host": "192.168.124.10:8666"},
        environ_overrides=environment,
    )
    cross_site_api_response = navigation_client.get(
        "/api/cache/activity",
        headers=headers,
        environ_overrides=environment,
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "/agent"
    assert gate_response.status_code == 200
    assert 'id="agent_access_password"' in gate_response.get_data(as_text=True)
    assert api_response.status_code == 401
    assert cross_site_api_response.status_code == 403


@pytest.mark.parametrize("headers", ({}, NAVIGATION_HEADERS))
def test_navigation_responses_vary_by_browser_request_context(
    navigation_client: FlaskClient,
    headers: dict[str, str],
) -> None:
    """Shared caches must not reuse a response from another fetch context."""
    response = navigation_client.get("/", headers=headers)

    assert set(NAVIGATION_HEADERS).issubset(set(response.vary))
