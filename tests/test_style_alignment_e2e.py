"""Shared component annotation regressions. Code version: v1.0.2-codex.1."""

import pytest

from tests.agent_browser_fixtures import browser_profile_identity
from playwright.sync_api import expect

from tests import test_sidebar_e2e

disposable_browser = test_sidebar_e2e.disposable_browser
sidebar_server_url = test_sidebar_e2e.sidebar_server_url


@pytest.mark.parametrize("width", [1024, 800, 390])
def test_shared_component_annotations(disposable_browser, sidebar_server_url, width):
    context = disposable_browser.new_context(viewport={"width": width, "height": 863})
    page = context.new_page()
    try:
        page.goto(f"{sidebar_server_url}/settings/style-tokens")
        expect(page.locator('[data-style-token-card="workspace-article"]')).to_have_count(0)
        controls = page.locator('.style-token-demo .browser-filter-select-trigger')
        assert controls.count() >= 2
        for control in controls.all():
            expect(control).to_have_css("height", "30px")
        secondary = page.locator('.style-token-secondary-button-demo .secondary-button')
        expect(secondary).to_have_css("font-size", "13px")
        expect(secondary).to_have_css("height", "31px")
        assert secondary.evaluate("e => Math.abs(e.getBoundingClientRect().right - e.closest('.style-token-demo').getBoundingClientRect().right) <= 1")
        assert secondary.evaluate("e => e.getBoundingClientRect().width < e.closest('.style-token-demo').getBoundingClientRect().width")
        closes = page.locator('.style-token-modal-demo > .workspace-modal-close')
        expect(closes).to_have_count(2)
        for close in closes.all():
            page.mouse.move(0, 0)
            expect(close).to_have_css("opacity", "0")
            close.locator('..').hover()
            expect(close).to_have_css("opacity", "1")
            expect(close).to_have_css("color", "rgb(200, 30, 30)")
            page.mouse.move(0, 0)
            close.focus()
            expect(close).to_have_css("opacity", "1")
            close.evaluate("e => e.blur()")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        context.close()


def test_touch_dismiss_visibility(disposable_browser, sidebar_server_url):
    context = disposable_browser.new_context(has_touch=True, is_mobile=True, viewport={"width": 390, "height": 863})
    page = context.new_page()
    try:
        page.goto(f"{sidebar_server_url}/settings/style-tokens")
        controls = page.locator('.style-token-modal-demo > .workspace-modal-close')
        assert controls.count() == 2
        for close in controls.all():
            expect(close).to_have_css("opacity", "1")
    finally:
        context.close()


@pytest.mark.parametrize("width", [1024, 390])
@pytest.mark.parametrize("logged_in", [None, False])
def test_account_probe_failure_can_recheck_without_signing_in(disposable_browser, sidebar_server_url, width, logged_in):
    context = disposable_browser.new_context(viewport={"width": width, "height": 863})
    page = context.new_page()
    requests = []
    def probe(route):
        requests.append(route.request.url)
        ready = len(requests) == 2
        route.fulfill(json={"profile_identity": browser_profile_identity("edge"),
            "platform": "chatgpt", "browser": "edge", "browser_label": "Edge",
            "logged_in": True if ready else logged_in, "can_download": ready,
            "message": "Ready" if ready else "Could not verify: net::ERR_CONNECTION_CLOSED",
            "agent_sources": {"profile_identity": browser_profile_identity("edge"), "recent_sessions": [], "projects": []},
        })
    page.route("**/api/browser-session**", probe)
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        if width < 900:
            page.get_by_role("button", name="Toggle sidebar", exact=True).click()
        message = page.locator('[data-role="browser-session-message"]')
        retry = page.get_by_role("button", name="Recheck", exact=True)
        expect(message).to_contain_text("ERR_CONNECTION_CLOSED")
        login = page.locator('[data-role="browser-session-login"]')
        if logged_in is None:
            expect(login).to_be_hidden()
        else:
            expect(login).to_be_visible()
            assert login.evaluate("e => Math.abs(e.getBoundingClientRect().right - e.parentElement.getBoundingClientRect().right) <= 1")
        expect(retry).to_be_visible()
        assert retry.evaluate("e => Math.abs(e.getBoundingClientRect().right - e.parentElement.getBoundingClientRect().right) <= 1")
        assert len(requests) == 1
        retry.click()
        expect(retry).to_be_hidden()
        expect(message).to_be_hidden()
        expect(page.locator('[data-role="browser-session-checkmark"]')).to_have_attribute("data-status-state", "ready")
        assert len(requests) == 2
        assert "refresh=1" in requests[-1]
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        context.close()
