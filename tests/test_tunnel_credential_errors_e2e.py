"""Disposable-browser coverage for Tunnel credential save failures.

Code version: v1.0.0-claude.0
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests import test_sidebar_e2e as fixtures

disposable_browser = fixtures.disposable_browser
sidebar_server_url = fixtures.sidebar_server_url

TUNNEL_ID = "tunnel_" + "c" * 32
OTHER_TUNNEL_ID = "tunnel_" + "d" * 32
REFUSAL = "Tunnel credentials could not be saved with owner-only access."


def _ready_status() -> dict:
    project = {
        "id": "main",
        "identity": "main000000000000",
        "root": "/tmp/main",
        "writable": True,
        "access": "Read and write",
        "registered": True,
        "selected": True,
        "available": True,
        "availability": "Available",
        "problem": "",
        "description": "Frontend test project",
        "source": "selection",
    }
    return {
        "platform": "chatgpt",
        "enabled": True,
        "state": "ready",
        "ready": True,
        "activity_observed": True,
        "project_context": {
            "registry_configured": True,
            "revision": 1,
            "selected_at": 1,
            "source": "selection",
            "current": project,
            "selected_project_ids": ["main"],
            "projects": [project],
            "browse_root": "/tmp",
            "problem": "",
        },
        "status_observed_at": 1,
        "runtime_instance_id": "runtime-a",
        "generation": 9,
        "state_revision": 9,
        "active_calls": [],
        "recent_calls": [],
        "recent_usage": {"estimated_tokens": 0, "complete": True},
        "credentials": {
            "tunnel_id": "",
            "tunnel_id_valid": False,
            "api_key_saved": True,
            "api_key_hint": "…ABCD",
            "qualified": False,
        },
        "presentation": {
            "tone": "ready",
            "label": "Tunnel ready",
            "message": "Ready for a project tool call.",
            "hint": "",
            "action": None,
        },
    }


@pytest.mark.parametrize(
    ("width", "height"),
    [(1_280, 900), (390, 844), (1_280, 560)],
    ids=["desktop", "narrow", "short"],
)
def test_storage_refusal_is_announced_until_the_next_edit(
    disposable_browser,
    sidebar_server_url,
    width: int,
    height: int,
) -> None:
    """A refused save has no field to mark, so the Tunnel status line reports it."""
    context = disposable_browser.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route(
        "**/api/agent/tunnel/status?platform=chatgpt",
        lambda route: route.fulfill(json=_ready_status()),
    )
    replies = [
        {"status": 500, "json": {"error": REFUSAL}},
        {"status": 400, "json": {"error": "Enter the API key for this Tunnel."}},
    ]
    page.route(
        "**/api/agent/tunnel/credentials",
        lambda route: route.fulfill(**replies.pop(0)),
    )
    try:
        page.goto(sidebar_server_url + "/agent/tunnel/chatgpt")
        problem = page.locator("[data-agent-tunnel-problem]")
        expect(problem).to_be_hidden()
        tunnel_id = page.locator("#chatgpt_tunnel_id")

        with page.expect_response("**/api/agent/tunnel/credentials"):
            tunnel_id.fill(TUNNEL_ID)
        expect(problem).to_have_text(REFUSAL)
        expect(problem).to_have_attribute("aria-live", "polite")
        if not problem.is_visible():
            # Narrow layouts keep every Tunnel status message in the collapsible sidebar.
            page.get_by_role("button", name="Toggle sidebar").click()
        problem.scroll_into_view_if_needed()
        expect(problem).to_be_visible()
        box = problem.bounding_box()
        assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= width + 1
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert "sk-" not in problem.inner_text()

        # Editing retracts the refusal; a rejected field value stays a field marker only.
        with page.expect_response("**/api/agent/tunnel/credentials"):
            tunnel_id.fill(OTHER_TUNNEL_ID)
        expect(problem).to_be_hidden()
        expect(problem).to_have_text("")
        assert errors == []
    finally:
        context.close()
