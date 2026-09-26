"""Browser coverage for Cache selection and manual browser-session recovery.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Browser, expect

from test_sidebar_e2e import disposable_browser as disposable_browser
from test_sidebar_e2e import sidebar_server_url as sidebar_server_url


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize("storage_denied", (False, True))
@pytest.mark.parametrize(("width", "height"), ((914, 791), (390, 844)))
def test_source_switching_uses_current_mode_when_storage_is_unavailable(
    disposable_browser: Browser,
    sidebar_server_url: str,
    macos_host,
    storage_denied: bool,
    width: int,
    height: int,
) -> None:
    """Keep URL and in-page mode choices authoritative across provider navigation."""
    context = disposable_browser.new_context(
        viewport={"width": width, "height": height},
        reduced_motion="reduce",
    )
    if storage_denied:
        context.add_init_script(
            """Object.defineProperty(window, "sessionStorage", {
                configurable: true,
                get() { throw new DOMException("Storage is unavailable.", "SecurityError"); },
            });"""
        )
    context.route(
        "**/api/browser-session**",
        lambda route: route.fulfill(json={
            "browser": "safari",
            "logged_in": True,
            "can_download": True,
            "account_name": "Signed in",
            "message": "Ready.",
        }),
    )
    page = context.new_page()
    try:
        page.goto(f"{sidebar_server_url}/cache/chatgpt/media/safari", wait_until="domcontentloaded")
        for source, mode in (("x", "media"), ("grok", "text"), ("claude", "media")):
            if width <= 900 and page.locator("#sidebar_toggle").get_attribute("aria-expanded") != "true":
                page.locator("#sidebar_toggle").click()
            page.locator(f'[data-cache-content-mode-option="{mode}"]').click()
            page.locator("[data-cache-source-switcher-trigger]").click()
            with page.expect_request(
                f"**/api/cache/{source}/status?content_mode={mode}", timeout=7_000
            ):
                page.locator(f'[data-cache-source-switcher-option="{source}"]').click()
            expect(page).to_have_url(f"{sidebar_server_url}/cache/{source}/{mode}/safari")
            expect(page.locator(f'[data-cache-content-mode-option="{mode}"]')).to_have_attribute(
                "aria-checked", "true"
            )
            expect(page.locator(f'input[name="{source}_browser"]')).to_have_value("safari")
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(("width", "height"), ((914, 791), (390, 844)))
def test_cached_human_verification_requires_manual_recheck_without_login(
    disposable_browser: Browser,
    sidebar_server_url: str,
    macos_host,
    width: int,
    height: int,
) -> None:
    """Keep cached challenges paused until the user chooses a visible Recheck."""
    context = disposable_browser.new_context(
        viewport={"width": width, "height": height},
        reduced_motion="reduce",
    )
    requests: list[str] = []

    def probe(route) -> None:
        assert route.request.method == "GET"
        requests.append(route.request.url)
        ready = len(requests) > 1
        route.fulfill(json={
            "platform": "chatgpt",
            "browser": "safari",
            "browser_label": "Safari",
            "logged_in": ready,
            "can_download": ready,
            "human_verification": not ready,
            "account_name": "Signed in" if ready else "Human verification required",
            "message": "Ready." if ready else "Complete verification in Safari, then choose Recheck.",
        })

    context.route("**/api/browser-session**", probe)
    page = context.new_page()
    try:
        page.goto(f"{sidebar_server_url}/cache/chatgpt/text/safari", wait_until="domcontentloaded")
        account = page.locator('[data-role="browser-session-account"]')
        recheck = page.get_by_role("button", name="Recheck", exact=True)
        expect(account).to_have_text("Complete verification now")
        assert len(requests) == 1
        page.reload(wait_until="domcontentloaded")
        if width <= 900 and page.locator("#sidebar_toggle").get_attribute("aria-expanded") != "true":
            page.locator("#sidebar_toggle").click()
        expect(account).to_have_text("Complete verification now")
        expect(recheck).to_be_visible()
        expect(page.locator('[data-role="browser-session-message"]')).to_contain_text("choose Recheck")
        expect(page.locator('[data-role="browser-session-login"]')).to_have_count(0)
        expect(page.locator("#start_button")).to_be_disabled()
        assert len(requests) == 1
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        recheck.click()
        expect(account).to_have_text("Signed in")
        expect(recheck).to_be_hidden()
        expect(page.locator("#start_button")).to_be_enabled()
        assert len(requests) == 2
        assert "refresh=1" in requests[-1]
    finally:
        context.close()
