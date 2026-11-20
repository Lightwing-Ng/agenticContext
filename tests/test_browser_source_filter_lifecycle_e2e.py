"""Keep source pickers unavailable until their actual interaction handlers exist.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Browser, Route, expect

from tests import test_sidebar_e2e as fixtures


annotated_resources_server_url = fixtures.annotated_resources_server_url
disposable_browser = fixtures.disposable_browser


@pytest.mark.parametrize("width", (1_024, 390))
@pytest.mark.parametrize("asset", ("browser-source-filter.js", "select-controller.js"))
def test_source_pickers_become_available_only_after_delayed_handlers_bind(
    disposable_browser: Browser,
    annotated_resources_server_url: str,
    width: int,
    asset: str,
) -> None:
    context = disposable_browser.new_context(viewport={"width": width, "height": 1_332})
    page = context.new_page()
    held_routes: list[Route] = []
    pattern = f"**/{asset}?*"
    page.route(pattern, lambda route: held_routes.append(route))
    try:
        with page.expect_request(lambda request: urlsplit(request.url).path.endswith("/" + asset)):
            page.goto(
                annotated_resources_server_url
                + "/browser?view=text&source=all&session_view=1&sort=oldest&q=crosspage%20needle",
                wait_until="commit",
            )
        trigger = page.locator("[data-browser-header-filter] [data-browser-source-filter-trigger]")
        sidebar_trigger = page.locator("#browser_filter_form [data-browser-source-filter-trigger]")
        menu = page.locator("#browser_header_source_filter_options")
        expect(trigger).to_have_css("height", "22px")
        expect(trigger).to_be_disabled()
        expect(sidebar_trigger).to_be_disabled()
        expect(menu).to_be_hidden()
        assert held_routes

        while held_routes:
            held_routes.pop(0).continue_()
        page.unroute(pattern)

        expect(trigger).to_be_enabled()
        expect(sidebar_trigger).to_be_enabled()
        trigger.press("ArrowDown")
        expect(menu).to_be_visible()
        assert menu.evaluate("element => element.parentElement === document.body")
        selected = menu.locator('[data-browser-source-filter-option="chatgpt"]')
        expect(selected).to_be_focused()
        assert selected.evaluate("""element => {
            const rect = element.getBoundingClientRect();
            return element.contains(document.elementFromPoint(
                rect.left + rect.width / 2, rect.top + rect.height / 2,
            ));
        }""")
        selected.press("Escape")
        expect(menu).to_be_hidden()
        expect(trigger).to_be_focused()
        trigger.click()
        expect(menu).to_be_visible()
        gemini = menu.locator('[data-browser-source-filter-option="gemini"]')
        gemini.focus()
        gemini.press("Enter")
        expect(page.locator("#browser_filter_form [data-browser-source-filter-input]")).to_have_value("gemini")
        expect(page.locator("#browser_search_input")).to_have_value("crosspage needle")
        query = parse_qs(urlsplit(page.url).query)
        assert query["source"] == ["gemini"]
        assert query["sort"] == ["oldest"]
        assert query["session_view"] == ["1"]
    finally:
        for route in held_routes:
            route.abort()
        context.close()
