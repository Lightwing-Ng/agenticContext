"""Offline browser-engine smoke checks with explicitly simulated device viewports.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from collections.abc import Iterator
import os
import re
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import Mock

import pytest
from playwright.sync_api import Browser, Error as PlaywrightError, expect, sync_playwright

from tests import test_sidebar_e2e as fixtures


sidebar_server_url = fixtures.sidebar_server_url


@pytest.fixture(scope="module")
def smoke_browser() -> Iterator[Browser]:
    """Keep Chromium as the default; CI separately requires the WebKit profile."""
    engine = os.environ.get("AGENTIC_CONTEXT_SMOKE_BROWSER", "chromium")
    with sync_playwright() as playwright:
        browser = fixtures._launch_disposable_browser(playwright, engine=engine)
        try:
            yield browser
        finally:
            browser.close()


def test_webkit_launch_failure_never_falls_back_to_chromium() -> None:
    webkit = Mock()
    chromium = Mock()
    webkit.launch.side_effect = PlaywrightError("Synthetic missing WebKit installation")

    with pytest.raises(PlaywrightError, match="missing WebKit"):
        fixtures._launch_disposable_browser(
            SimpleNamespace(webkit=webkit, chromium=chromium), engine="webkit"
        )

    chromium.launch.assert_not_called()


def test_unknown_smoke_engine_is_rejected_before_launch() -> None:
    with pytest.raises(ValueError, match="Unsupported disposable browser engine"):
        fixtures._launch_disposable_browser(SimpleNamespace(), engine="unknown")


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(
    ("viewport_label", "width", "height", "touch"),
    [
        ("desktop", 1_280, 900, False),
        ("simulated-tablet", 820, 1_180, True),
        ("simulated-phone", 390, 844, True),
    ],
    ids=["desktop", "simulated-tablet", "simulated-phone"],
)
def test_local_resources_render_and_interact_in_selected_engine(
    smoke_browser: Browser,
    sidebar_server_url: str,
    record_property,
    viewport_label: str,
    width: int,
    height: int,
    touch: bool,
) -> None:
    """Verify actual local navigation, keyboard selection, theme, and drawer hit testing."""
    context = smoke_browser.new_context(
        viewport={"width": width, "height": height},
        has_touch=touch,
        is_mobile=touch,
        reduced_motion="reduce",
        color_scheme="light",
    )
    unexpected_requests: list[str] = []
    page_errors: list[str] = []
    expected_origin = urlsplit(sidebar_server_url)

    def local_requests_only(route) -> None:
        target = urlsplit(route.request.url)
        if target.scheme in {"data", "blob"} or (
            target.scheme == expected_origin.scheme
            and target.netloc == expected_origin.netloc
        ):
            route.continue_()
        else:
            unexpected_requests.append(route.request.url)
            route.abort()

    context.route("**/*", local_requests_only)
    page = context.new_page()
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    record_property("browser_engine", smoke_browser.browser_type.name)
    record_property("viewport_profile", viewport_label)
    record_property("device_evidence", "simulated viewport; no physical device")
    try:
        response = page.goto(
            sidebar_server_url + "/browser?view=text&source=claude&session_view=1",
            wait_until="domcontentloaded",
        )
        assert response is not None and response.ok
        theme = page.locator("#global_theme_toggle")
        expect(theme).to_have_attribute("data-effective-theme", "light")
        theme.click()
        expect(theme).to_have_attribute("data-effective-theme", "dark")
        expect(theme).to_have_attribute("aria-pressed", "true")

        toggle = page.locator("#sidebar_toggle")
        if touch:
            expect(toggle).to_have_attribute("aria-expanded", "false")
            fixtures._assert_toggle_hit_target(page)
            toggle.tap()
        expect(toggle).to_have_attribute("aria-expanded", "true")
        expect(page.get_by_role("heading", name="Local resources", exact=True)).to_be_visible()
        trigger = page.locator("[data-browser-source-filter-trigger]")
        menu = page.locator("[data-browser-source-filter-menu]")
        expect(trigger).to_be_visible()
        trigger.focus()
        trigger.press("ArrowDown")
        expect(menu).to_be_visible()
        selected = menu.locator('[data-browser-source-filter-option="claude"]')
        expect(selected).to_be_focused()
        expect(selected).to_have_attribute("aria-selected", "true")
        selected.press("Escape")
        expect(menu).to_be_hidden()
        expect(trigger).to_be_focused()

        trigger.press("ArrowDown")
        target = menu.locator('[data-browser-source-filter-option="chatgpt"]')
        target.focus()
        target.press("Enter")
        expect(page).to_have_url(re.compile(r"[?&]source=chatgpt(?:&|$)"))
        expect(page.locator('[data-browser-source-filter-input]')).to_have_value("chatgpt")
        expect(page.locator("#global_theme_toggle")).to_have_attribute(
            "data-effective-theme", "dark"
        )

        if touch:
            if toggle.get_attribute("aria-expanded") == "true":
                toggle.tap()
            expect(toggle).to_have_attribute("aria-expanded", "false")
            fixtures._assert_toggle_hit_target(page)
            toggle.tap()
            expect(page.locator("#sidebar_backdrop")).to_be_visible()
            assert page.evaluate(
                "({x, y}) => document.elementFromPoint(x, y)?.id",
                {"x": width - 2, "y": height / 2},
            ) == "sidebar_backdrop"
            page.touchscreen.tap(width - 2, height / 2)
            expect(toggle).to_have_attribute("aria-expanded", "false")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert page_errors == []
        assert unexpected_requests == []
    finally:
        context.close()
