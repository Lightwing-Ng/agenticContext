"""Shared select keyboard adapters. Code version: v1.1.0-codex.1."""

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
def test_settings_operating_system_uses_standard_shared_select(
    disposable_browser, sidebar_server_url, width,
):
    context = disposable_browser.new_context(viewport={"width": width, "height": 900})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def keep_settings_read_only(route):
        if route.request.method == "POST":
            route.abort()
        else:
            route.continue_()

    page.route("**/settings", keep_settings_read_only)
    try:
        page.goto(f"{sidebar_server_url}/settings#settings-agent")
        native = page.locator("#agent_operating_system")
        field = page.locator('[data-shared-select-kind="agent-operating-system"]')
        trigger = field.locator("[data-shared-select-trigger]")
        menu = page.locator("#" + trigger.get_attribute("aria-controls"))

        expect(native).to_be_hidden()
        expect(native).to_have_attribute("aria-hidden", "true")
        expect(native).to_have_attribute("tabindex", "-1")
        expect(trigger).to_be_visible()
        expect(trigger).to_have_attribute("aria-label", re.compile(r"^Operating system: .+"))
        assert round(trigger.bounding_box()["height"]) == 30

        page.evaluate(
            """() => {
                window.__settingsSharedSelectChanges = 0;
                document.querySelector('#agent_operating_system').addEventListener(
                    'change', () => window.__settingsSharedSelectChanges += 1
                );
            }"""
        )
        closed_transform = field.locator(".browser-picker-trigger-chevron").evaluate(
            "element => getComputedStyle(element).transform"
        )
        trigger.press("ArrowDown")
        selected = menu.locator('[aria-selected="true"]')
        expect(selected).to_be_focused()
        page.wait_for_timeout(350)
        assert round(selected.bounding_box()["height"]) >= 36
        materials = page.evaluate(
            """({trigger, menu}) => ({
                triggerBlur: getComputedStyle(trigger).backdropFilter,
                menuBlur: getComputedStyle(menu).backdropFilter,
                triggerBackground: getComputedStyle(trigger).backgroundImage,
                menuBackground: getComputedStyle(menu).backgroundImage,
            })""",
            {"trigger": trigger.element_handle(), "menu": menu.element_handle()},
        )
        assert materials["triggerBlur"] == "blur(12px)"
        assert materials["menuBlur"] == "blur(12px)"
        assert materials["triggerBackground"] != "none"
        assert materials["menuBackground"] != "none"
        open_transform = field.locator(".browser-picker-trigger-chevron").evaluate(
            "element => getComputedStyle(element).transform"
        )
        assert open_transform != closed_transform
        assert selected.locator(":scope > .trade-strategy-dropdown-check").count() == 1
        assert selected.locator(":scope > .trade-strategy-dropdown-text").count() == 1

        selected.press("Enter")
        expect(menu).to_be_hidden()
        expect(trigger).to_be_focused()
        assert page.evaluate("window.__settingsSharedSelectChanges") == 0

        target = menu.locator('[role="option"]:not([aria-selected="true"])').first
        trigger.click()
        target_value = target.get_attribute("data-shared-select-option")
        target.click()
        expect(trigger).to_be_focused()
        expect(native).to_have_value(target_value)
        assert page.evaluate("window.__settingsSharedSelectChanges") == 1
        assert native.evaluate(
            "select => Array.from(select.options).every(option => "
            "option.selected === option.defaultSelected)"
        )

        trigger.press("ArrowDown")
        page.keyboard.press("Escape")
        expect(menu).to_be_hidden()
        expect(trigger).to_be_focused()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
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
