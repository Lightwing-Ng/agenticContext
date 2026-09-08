"""Shared component annotation regressions. Code version: v1.2.0-codex.1."""

import pytest
from playwright.sync_api import expect

from tests import test_sidebar_e2e

disposable_browser = test_sidebar_e2e.disposable_browser
sidebar_server_url = test_sidebar_e2e.sidebar_server_url


def assert_field_title_contract(locator):
    expect(locator).to_have_css("font-size", "15px")
    expect(locator).to_have_css("font-weight", "400")
    expect(locator).to_have_css("line-height", "normal")
    expect(locator).to_have_css("letter-spacing", "normal")
    expect(locator).to_have_css("color", "rgb(11, 12, 12)")


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
            surface = close.locator('..')
            expect(surface).to_have_css("padding", "12px")
            expect(close).to_have_css("width", "24px")
            expect(close).to_have_css("height", "24px")
            expect(close).to_have_css("border-radius", "50%")
            geometry = surface.evaluate(
                """node => {
                    const button = node.querySelector('.workspace-modal-close').getBoundingClientRect();
                    const icon = node.querySelector('.workspace-modal-icon').getBoundingClientRect();
                    const bounds = node.getBoundingClientRect();
                    return {
                        centerTop: button.top + (button.height / 2) - bounds.top,
                        centerLeft: button.left + (button.width / 2) - bounds.left,
                        controlIconGap: icon.left - button.right,
                    };
                }"""
            )
            assert abs(geometry["centerTop"] - geometry["centerLeft"]) <= 1
            assert geometry["controlIconGap"] > 0
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
def test_field_titles_match_the_agent_reference_at_shared_breakpoints(
    disposable_browser,
    sidebar_server_url,
    width,
):
    context = disposable_browser.new_context(viewport={"width": width, "height": 863})
    page = context.new_page()
    try:
        page.goto(f"{sidebar_server_url}/settings/style-tokens")
        for selector in (
            ".style-token-agent-browser-demo .style-token-component-kicker",
            ".style-token-scrollable-table thead th:nth-child(2)",
            ".style-token-text-input-demo > span:first-child",
        ):
            assert_field_title_contract(page.locator(selector))

        page.goto(
            f"{sidebar_server_url}/browser?view=text&source=all&kind=all"
            "&q=&sort=newest&session_view=1"
        )
        filter_titles = page.locator(".browser-filter-field > span")
        assert filter_titles.count() >= 2
        for title in filter_titles.all():
            assert_field_title_contract(title)
        table_headers = page.locator(".browser-session-table thead th")
        if table_headers.count():
            for header in table_headers.all():
                assert_field_title_contract(header)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
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
        ready = "refresh=1" in route.request.url
        route.fulfill(json={
            "platform": "chatgpt", "browser": "edge", "browser_label": "Edge",
            "logged_in": True if ready else logged_in, "can_download": ready,
            "message": "Ready" if ready else "Could not verify: net::ERR_CONNECTION_CLOSED",
            "agent_sources": {"recent_sessions": [], "projects": []},
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
        retry.click()
        expect(retry).to_be_hidden()
        expect(message).to_be_hidden()
        expect(page.locator('[data-role="browser-session-checkmark"]')).to_have_attribute("data-status-state", "ready")
        assert any("refresh=1" in url for url in requests)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        context.close()
