"""Session search scope remains navigable before its module initializes.

Code version: v1.0.1-codex.0
"""

from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from tests import test_sidebar_e2e as fixtures


disposable_browser = fixtures.disposable_browser
annotated_resources_server_url = fixtures.annotated_resources_server_url


@pytest.mark.parametrize("search_module_available", [False, True])
@pytest.mark.parametrize("activation", ["click", "Enter", "Space"])
def test_session_scope_removal_has_a_native_navigation_fallback(
    disposable_browser, annotated_resources_server_url, search_module_available, activation
):
    context = disposable_browser.new_context(reduced_motion="reduce")
    page = context.new_page()
    if not search_module_available:
        page.route("**/static/browser-search.js?*", lambda route: route.abort())
    try:
        page.goto(
            annotated_resources_server_url
            + "/browser?view=text&source=chatgpt&session=chatgpt:long-session"
            + "&session_page=2&q=crosspage+needle",
            wait_until="load",
        )
        expect(page.locator("[data-browser-session-tag]")).to_be_visible()
        scope_remove = page.locator("[data-browser-session-scope-remove]")
        expect(scope_remove).to_have_js_property("tagName", "BUTTON")
        if activation == "click":
            scope_remove.click()
        else:
            scope_remove.press(activation)
        expect(page.locator("[data-browser-session-tag]")).to_have_count(0)
        expect(page.locator("[data-chat-message-id]")).to_have_count(1)
        parameters = parse_qs(urlsplit(page.url).query)
        assert parameters["source"] == ["chatgpt"]
        assert parameters["q"] == ["crosspage needle"]
        assert parameters["session_view"] == ["0"]
        assert "session" not in parameters
        assert "session_page" not in parameters
    finally:
        context.close()
