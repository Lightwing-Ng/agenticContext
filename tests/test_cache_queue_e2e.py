"""Isolated browser coverage for a Cache task that waits for a busy browser.

Code version: v1.0.0-claude.0
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Browser, expect

from tests import test_sidebar_e2e as fixtures


disposable_browser = fixtures.disposable_browser
sidebar_server_url = fixtures.sidebar_server_url

SESSION_CACHE_KEY = "cachelikes:browser-session:v8:default:chatgpt:safari"
BUSY_MESSAGE = (
    "Safari is busy with the X · Media cache. "
    "Start adds this task to the queue; the account is checked when it runs."
)
QUEUED_MESSAGE = (
    "Queued. Safari is busy with the X · Media cache. "
    "This task starts automatically when Safari is free."
)


def _busy_session() -> dict:
    return {
        "platform": "chatgpt",
        "browser": "safari",
        "browser_label": "Safari",
        "can_download": False,
        "account_name": "",
        "message": BUSY_MESSAGE,
        "busy": True,
        "queue_available": True,
    }


def _install_status(context, overrides: dict) -> None:
    """Serve the real idle snapshot with the task fields a test wants to vary."""

    def respond(route) -> None:
        response = route.fetch()
        snapshot = response.json()
        snapshot.update(overrides)
        route.fulfill(response=response, json=snapshot)

    context.route("**/api/cache/chatgpt/status**", respond)


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(("width", "height"), ((1_006, 791), (390, 844)))
def test_busy_safari_keeps_start_available_and_the_queued_task_explains_itself(
    disposable_browser: Browser,
    sidebar_server_url: str,
    macos_host,
    width: int,
    height: int,
) -> None:
    """The second Safari task can be queued from its own page and shows that it waits."""
    context = disposable_browser.new_context(
        viewport={"width": width, "height": height},
        has_touch=width < 768,
        is_mobile=width < 768,
        reduced_motion="reduce",
    )
    context.route("**/api/browser-session**", lambda route: route.fulfill(json=_busy_session()))
    context.route("**/api/cache/activity**", lambda route: route.fulfill(json={"tasks": []}))
    status: dict = {}
    _install_status(context, status)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(f"{sidebar_server_url}/cache/chatgpt/text/safari")
        if width < 768:
            page.locator("#sidebar_toggle").click()
        panel = page.locator("[data-browser-session-panel]")
        start = page.locator("#start_button")
        stop = page.locator("#stop_button")
        account = panel.locator('[data-role="browser-session-account"]')

        # The account cannot be checked while another task owns Safari, yet Start works.
        expect(account).to_have_text("Not checked")
        expect(panel.locator('[data-role="browser-session-message"]')).to_have_text(BUSY_MESSAGE)
        expect(panel.locator('[data-role="browser-session-checkmark"]')).to_be_hidden()
        expect(panel.locator('[data-role="browser-session-recheck"]')).to_be_visible()
        expect(panel).to_have_attribute("data-browser-download-ready", "true")
        expect(panel).not_to_have_class(re.compile(r"\bis-browser-ready\b"))
        expect(start).to_be_visible()
        expect(start).to_be_enabled()
        assert page.evaluate("key => sessionStorage.getItem(key)", SESSION_CACHE_KEY) is None

        status.update(running=True, phase="queued", message=QUEUED_MESSAGE)
        progress = page.locator("#status_progress")
        expect(page.locator("#phase_value")).to_have_text("queued", timeout=7_000)
        expect(page.locator("#phase_chip")).to_have_attribute("data-phase", "queued")
        expect(page.locator("#status_progress_value")).to_have_text("Queued")
        expect(page.locator("#status_progress_detail")).to_have_text(
            "This task starts automatically. Stop removes it from the queue."
        )
        expect(progress).to_have_class(re.compile(r"\bis-unavailable\b"))
        expect(progress).not_to_have_class(re.compile(r"\bis-indeterminate\b"))
        assert progress.get_attribute("aria-valuenow") is None
        assert progress.locator("#status_progress_fill").evaluate(
            "element => element.getBoundingClientRect().width"
        ) == 0
        expect(page.locator("#message")).to_have_text(QUEUED_MESSAGE)
        expect(stop).to_be_visible()
        expect(stop).to_be_enabled()
        expect(start).to_be_hidden()

        # Safari is free: the same page follows the task into its first scan.
        status.update(running=True, phase="starting", message="Initializing job.")
        expect(page.locator("#phase_value")).to_have_text("starting", timeout=7_000)
        expect(page.locator("#status_progress_value")).to_have_text("Scanning")
        expect(progress).to_have_class(re.compile(r"\bis-indeterminate\b"))
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert not errors, errors
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_busy_safari_keeps_the_last_account_check_and_an_unverified_account_still_blocks_start(
    disposable_browser: Browser,
    sidebar_server_url: str,
    macos_host,
) -> None:
    """Queue availability is not a sign-in result, and a real failure still gates Start."""
    context = disposable_browser.new_context(
        viewport={"width": 1_006, "height": 791},
        reduced_motion="reduce",
    )
    ready = {
        "platform": "chatgpt",
        "browser": "safari",
        "browser_label": "Safari",
        "logged_in": True,
        "can_download": True,
        "account_name": "Isolated ChatGPT account",
        "message": "Safari is ready for ChatGPT.",
    }
    session = {"payload": _busy_session(), "requests": 0}

    def respond(route) -> None:
        session["requests"] += 1
        route.fulfill(json=session["payload"])

    context.route("**/api/browser-session**", respond)
    context.route("**/api/cache/activity**", lambda route: route.fulfill(json={"tasks": []}))
    # An earlier check is old enough to be refreshed, but not old enough to be dropped.
    context.add_init_script(
        "sessionStorage.setItem(%s, JSON.stringify({cached_at: Date.now() - 600000, payload: %s}));"
        % (json.dumps(SESSION_CACHE_KEY), json.dumps(ready))
    )
    page = context.new_page()
    try:
        page.goto(f"{sidebar_server_url}/cache/chatgpt/text/safari")
        panel = page.locator("[data-browser-session-panel]")
        start = page.locator("#start_button")
        account = panel.locator('[data-role="browser-session-account"]')
        expect(account).to_have_text("Not checked")
        expect(start).to_be_enabled()
        assert session["requests"] >= 1
        cached = json.loads(page.evaluate("key => sessionStorage.getItem(key)", SESSION_CACHE_KEY))
        assert cached["payload"]["account_name"] == "Isolated ChatGPT account"

        session["payload"] = {
            "platform": "chatgpt",
            "browser": "safari",
            "browser_label": "Safari",
            "logged_in": False,
            "can_download": False,
            "account_name": "",
            "message": "Safari is not signed in to ChatGPT.",
        }
        panel.locator('[data-role="browser-session-recheck"]').click()
        expect(account).to_have_text("Not signed in")
        expect(start).to_be_disabled()
        expect(panel).to_have_attribute("data-browser-download-ready", "false")
        expect(panel.locator('[data-role="browser-session-message"]')).to_have_text(
            "Safari is not signed in to ChatGPT."
        )
    finally:
        context.close()
