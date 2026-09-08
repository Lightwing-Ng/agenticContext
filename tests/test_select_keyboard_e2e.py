"""Shared select keyboard adapters. Code version: v1.0.0-codex.1."""

import pytest
from playwright.sync_api import expect
from urllib.parse import parse_qs, urlsplit
import re

from tests import test_sidebar_e2e

disposable_browser = test_sidebar_e2e.disposable_browser
sidebar_server_url = test_sidebar_e2e.sidebar_server_url
seeded_chatgpt_browser_server_url = test_sidebar_e2e.seeded_chatgpt_browser_server_url


@pytest.mark.parametrize("width", [1024, 390])
@pytest.mark.parametrize("cached_template", [False, True])
def test_select_keyboard_adapters(disposable_browser, sidebar_server_url, width, cached_template):
    context = disposable_browser.new_context(viewport={"width": width, "height": 900})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    if cached_template:
        def cached_response(route):
            response = route.fetch()
            body = re.sub(
                r'<script[^>]+src="[^"]*select-controller\.js[^"]*"[^>]*></script>',
                "", response.text(),
            )
            route.fulfill(response=response, body=body)
        page.route("**/browser?**", cached_response)
    try:
        page.goto(f"{sidebar_server_url}/browser?view=text&session_view=0&source=all")
        if width < 901:
            page.locator("#sidebar_toggle").click()
        for selector in [
            "[data-browser-filter-select-trigger]",
            "[data-browser-source-filter-trigger]",
        ]:
            trigger = page.locator(selector).first
            expect(trigger).to_be_visible()
            menu = page.locator("#" + trigger.get_attribute("aria-controls"))
            trigger.press("ArrowDown")
            selected = menu.locator('[aria-selected="true"]')
            expect(selected).to_be_focused()
            assert trigger.get_attribute("aria-activedescendant") is None
            selected.press("End")
            expect(menu.locator('[role="option"]').last).to_be_focused()
            expect(selected).to_have_attribute("aria-selected", "true")
            page.keyboard.press("Escape")
            expect(menu).to_be_hidden()
            expect(trigger).to_be_focused()
            trigger.press("Home")
            expect(menu.locator('[role="option"]').first).to_be_focused()
            page.keyboard.press("Tab")
            expect(menu).to_be_hidden()
            assert page.evaluate("document.activeElement !== document.body")
            trigger.click()
            expect(menu).to_be_visible()
            page.locator("h1").first.click()
            expect(menu).to_be_hidden()
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("width", [1024, 390])
def test_portaled_source_keyboard_commit(
    disposable_browser, seeded_chatgpt_browser_server_url, width,
):
    context = disposable_browser.new_context(viewport={"width": width, "height": 900})
    page = context.new_page()
    try:
        page.goto(
            f"{seeded_chatgpt_browser_server_url}/browser"
            "?view=text&session_view=1&source=all&sort=newest"
        )
        trigger = page.locator("[data-browser-header-filter] [data-browser-source-filter-trigger]")
        menu = page.locator("#" + trigger.get_attribute("aria-controls"))
        trigger.press("ArrowDown")
        assert menu.evaluate("element => element.parentElement === document.body")
        expect(menu.locator('[aria-selected="true"]')).to_be_focused()
        page.keyboard.press("Tab")
        expect(menu).to_be_hidden()
        assert page.evaluate("document.activeElement !== document.body")
        trigger.press("End")
        option = menu.locator('[role="option"]').last
        value = option.get_attribute("data-browser-source-filter-option")
        option.press("Enter")
        page.wait_for_load_state("domcontentloaded")
        assert parse_qs(urlsplit(page.url).query)["source"] == [value]
    finally:
        context.close()
