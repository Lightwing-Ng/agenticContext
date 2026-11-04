"""Isolated browser coverage for a Cache task that waits for a busy browser.

Code version: v1.1.0-codex.0
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


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(("width", "height"), ((1_006, 791), (390, 844)))
def test_transient_safari_probe_recovers_automatically_without_caching_busy(
    disposable_browser: Browser,
    sidebar_server_url: str,
    macos_host,
    width: int,
    height: int,
) -> None:
    """An overlapping account probe stays neutral and retries after the lease is free."""
    context = disposable_browser.new_context(
        viewport={"width": width, "height": height},
        reduced_motion="reduce",
    )
    requests: list[str] = []

    def respond(route) -> None:
        requests.append(route.request.url)
        ready = len(requests) > 1
        route.fulfill(json={
            "platform": "x", "browser": "safari", "browser_label": "Safari",
            "logged_in": True if ready else None,
            "can_download": ready,
            "account_name": "Isolated X account" if ready else "",
            "busy": not ready,
            "retry_after_seconds": 2 if not ready else 0,
            "message": "Ready." if ready else "Safari is finishing another browser check.",
        })

    context.route("**/api/browser-session**", respond)
    cache_key = "cachelikes:browser-session:v8:default:x:safari"
    context.add_init_script(
        "sessionStorage.setItem(%s, JSON.stringify({cached_at: Date.now(), payload: %s}));"
        % (json.dumps(cache_key), json.dumps({
            "logged_in": None, "can_download": False,
            "message": (
                "Could not verify the account: RuntimeError: "
                "Safari is busy with another active browser task. Try again after it finishes."
            ),
        }))
    )
    page = context.new_page()
    page.clock.install()
    try:
        page.goto(f"{sidebar_server_url}/cache/x/media/safari")
        if width < 768:
            page.locator("#sidebar_toggle").click()
        panel = page.locator("[data-browser-session-panel]")
        account = panel.locator('[data-role="browser-session-account"]')
        expect(account).to_have_text("Waiting for Safari")
        expect(panel.locator('[data-role="browser-session-checkmark"]')).to_be_hidden()
        expect(panel.locator('[data-role="browser-session-spinner"]')).to_be_visible()
        assert page.evaluate("key => sessionStorage.getItem(key)", cache_key) is None
        assert len(requests) == 1

        page.clock.run_for(2_100)
        expect(account).to_have_text("Isolated X account")
        expect(panel.locator('[data-role="browser-session-spinner"]')).to_be_hidden()
        expect(panel.locator('[data-role="browser-session-checkmark"]')).to_have_attribute(
            "data-status-state", "ready"
        )
        assert len(requests) == 2
        cached = json.loads(page.evaluate("key => sessionStorage.getItem(key)", cache_key))
        assert cached["payload"]["can_download"] is True
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_transient_safari_probe_retries_stop_after_a_bounded_attempt_count(
    disposable_browser: Browser,
    sidebar_server_url: str,
    macos_host,
) -> None:
    """Persistent contention remains neutral and eventually requires an explicit Recheck."""
    context = disposable_browser.new_context(viewport={"width": 1_006, "height": 791})
    requests: list[str] = []

    def respond(route) -> None:
        requests.append(route.request.url)
        route.fulfill(json={
            "platform": "x", "browser": "safari", "browser_label": "Safari",
            "logged_in": None, "can_download": False, "account_name": "",
            "busy": True, "retry_after_seconds": 2,
            "message": f"Waiting on check {len(requests)}.",
        })

    context.route("**/api/browser-session**", respond)
    page = context.new_page()
    page.clock.install()
    try:
        page.goto(f"{sidebar_server_url}/cache/x/media/safari")
        message = page.locator('[data-role="browser-session-message"]')
        expect(message).to_have_text("Waiting on check 1.")
        for attempt in range(2, 32):
            page.clock.run_for(2_100)
            expect(message).to_have_text(
                "Safari is still busy. Choose Recheck when it is free."
                if attempt == 31 else f"Waiting on check {attempt}."
            )
        page.clock.run_for(10_000)
        assert len(requests) == 31
        expect(page.locator('[data-role="browser-session-account"]')).to_have_text("Waiting for Safari")
        expect(page.locator('[data-role="browser-session-checkmark"]')).to_be_hidden()
        expect(page.locator('[data-role="browser-session-spinner"]')).to_be_hidden()
        expect(page.locator('[data-role="browser-session-recheck"]')).to_be_visible()
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_transient_safari_probe_retry_is_cancelled_when_browser_changes(
    disposable_browser: Browser,
    sidebar_server_url: str,
    macos_host,
) -> None:
    """A pending Safari retry cannot overwrite a newly selected browser account."""
    context = disposable_browser.new_context(viewport={"width": 1_006, "height": 791})
    safari_requests: list[str] = []

    def respond(route) -> None:
        safari = "browser=safari" in route.request.url
        if safari:
            safari_requests.append(route.request.url)
        route.fulfill(json={
            "platform": "x", "browser": "safari" if safari else "edge",
            "browser_label": "Safari" if safari else "Edge",
            "logged_in": None if safari else True,
            "can_download": not safari, "busy": safari,
            "retry_after_seconds": 2 if safari else 0,
            "account_name": "" if safari else "Isolated Edge account",
            "message": "Waiting." if safari else "Ready.",
        })

    context.route("**/api/browser-session**", respond)
    page = context.new_page()
    page.clock.install()
    try:
        page.goto(f"{sidebar_server_url}/cache/x/media/safari")
        account = page.locator('[data-role="browser-session-account"]')
        expect(account).to_have_text("Waiting for Safari")
        page.locator('[data-role="browser-picker-trigger"]').click()
        page.locator('[data-browser-option="edge"]').click()
        expect(account).to_have_text("Isolated Edge account")
        page.clock.run_for(5_000)
        expect(account).to_have_text("Isolated Edge account")
        assert len(safari_requests) == 1
    finally:
        context.close()
