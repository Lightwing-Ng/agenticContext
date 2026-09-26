"""Shared select keyboard adapters. Code version: v1.2.0-codex.0."""

from pathlib import Path

import pytest
from playwright.sync_api import expect
from urllib.parse import parse_qs, urlsplit
import re

from tests import test_sidebar_e2e

disposable_browser = test_sidebar_e2e.disposable_browser
sidebar_server_url = test_sidebar_e2e.sidebar_server_url
seeded_chatgpt_browser_server_url = test_sidebar_e2e.seeded_chatgpt_browser_server_url


def assert_shared_chevron_rotation(page, chevron, degrees):
    """Wait for the semantic direction instead of measuring an in-flight transform."""
    page.wait_for_function(
        """({node, degrees}) => {
            const matrix = new DOMMatrix(getComputedStyle(node).transform);
            const rotation = Math.atan2(matrix.b, matrix.a) * 180 / Math.PI;
            return Math.abs(rotation - degrees) < 0.1;
        }""",
        arg={"node": chevron.element_handle(), "degrees": degrees},
    )


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


@pytest.mark.parametrize(
    ("width", "height", "touch"),
    ((1_006, 791, False), (390, 844, True), (1_006, 500, False)),
)
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
@pytest.mark.parametrize("motion", ["no-preference", "reduce"])
def test_settings_operating_system_uses_standard_shared_select(
    disposable_browser, sidebar_server_url, width, height, touch, color_scheme, motion,
):
    context = disposable_browser.new_context(
        viewport={"width": width, "height": height},
        color_scheme=color_scheme,
        has_touch=touch,
        is_mobile=touch,
        reduced_motion=motion,
    )
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
        expect(page.locator("#global_theme_toggle")).to_have_attribute(
            "data-effective-theme", color_scheme,
        )
        native = page.locator("#agent_operating_system")
        field = page.locator('.browser-filter-select[data-shared-select-kind="agent-operating-system"]')
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
        chevron = field.locator(".browser-picker-trigger-chevron")
        assert_shared_chevron_rotation(page, chevron, -90)
        expect(chevron).to_have_css(
            "transition-duration", "0.001s" if motion == "reduce" else "0.18s",
        )
        trigger.press("ArrowDown")
        selected = menu.locator('[aria-selected="true"]')
        expect(selected).to_be_focused()
        assert_shared_chevron_rotation(page, chevron, 0)
        page.wait_for_function(
            "node => node.getAnimations().every(animation => animation.playState === 'finished')",
            arg=menu.element_handle(),
        )
        assert round(selected.bounding_box()["height"]) >= 36
        materials = page.evaluate(
            """({trigger, menu}) => {
                const probe = document.createElement('span');
                probe.style.backgroundColor = 'color-mix(in srgb, var(--theme-background) 62%, transparent)';
                document.body.append(probe);
                const themeBackground = getComputedStyle(probe).backgroundColor;
                probe.remove();
                const style = getComputedStyle(menu);
                const bounds = menu.getBoundingClientRect();
                const hitTests = [...menu.querySelectorAll('[role="option"]')].map(option => {
                    const box = option.getBoundingClientRect();
                    return option.contains(document.elementFromPoint(
                        box.left + box.width / 2, box.top + box.height / 2,
                    ));
                });
                const ancestorFilters = [];
                for (let owner = menu.parentElement; owner; owner = owner.parentElement) {
                    const ownerStyle = getComputedStyle(owner);
                    ancestorFilters.push({
                        backdrop: ownerStyle.backdropFilter,
                        filter: ownerStyle.filter,
                    });
                }
                return {
                    triggerBlur: getComputedStyle(trigger).backdropFilter,
                    triggerColor: getComputedStyle(trigger).backgroundColor,
                    menuBlur: style.backdropFilter,
                    triggerBackground: getComputedStyle(trigger).backgroundImage,
                    menuBackground: style.backgroundImage,
                    menuColor: style.backgroundColor,
                    menuOpacity: style.opacity,
                    menuBorder: style.border,
                    menuShadow: style.boxShadow,
                    menuRadius: style.borderRadius,
                    themeBackground,
                    left: bounds.left, right: bounds.right,
                    top: bounds.top, bottom: bounds.bottom,
                    hitTests,
                    ancestorFilters,
                    documentOverflow: document.documentElement.scrollWidth - innerWidth,
                };
            }""",
            {"trigger": trigger.element_handle(), "menu": menu.element_handle()},
        )
        assert materials["triggerBlur"] == "blur(12px)"
        assert materials["menuBlur"] == "blur(12px)"
        assert materials["triggerBackground"] != "none"
        assert materials["menuBackground"] != "none"
        assert materials["menuColor"] == materials["themeBackground"], materials
        assert materials["menuOpacity"] == "1", materials
        assert materials["triggerColor"] != materials["menuColor"], materials
        assert all(
            owner["backdrop"] == "none" and owner["filter"] == "none"
            for owner in materials["ancestorFilters"]
        ), materials["ancestorFilters"]
        assert materials["left"] >= -1 and materials["right"] <= width + 1, materials
        assert materials["top"] >= -1 and materials["bottom"] <= height + 1, materials
        assert all(materials["hitTests"]), materials
        assert materials["documentOverflow"] <= 1, materials
        screenshot_dir = Path(__file__).resolve().parents[1] / "test-results"
        screenshot_dir.mkdir(exist_ok=True)
        page.screenshot(path=str(
            screenshot_dir
            / f"settings-os-dropdown-{color_scheme}-{width}x{height}-{motion}.png"
        ))
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
        assert_shared_chevron_rotation(page, chevron, -90)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

        page.goto(f"{sidebar_server_url}/settings/style-tokens#shared-select-dropdown")
        catalog = page.locator("#shared-select-dropdown [data-style-token-shared-filter]")
        catalog_trigger = catalog.locator("[data-style-token-shared-filter-trigger]")
        catalog_menu = catalog.locator("[data-style-token-shared-filter-menu]")
        catalog_chevron = catalog.locator(".browser-picker-trigger-chevron")
        catalog_trigger.evaluate("node => node.scrollIntoView({block: 'start'})")
        assert_shared_chevron_rotation(page, catalog_chevron, -90)
        catalog_trigger.press("ArrowDown")
        expect(catalog_menu.locator('[aria-selected="true"]')).to_be_focused()
        assert_shared_chevron_rotation(page, catalog_chevron, 0)
        page.wait_for_function(
            "node => node.getAnimations().every(animation => animation.playState === 'finished')",
            arg=catalog_menu.element_handle(),
        )
        catalog_material = catalog_menu.evaluate(
            """node => {
                const style = getComputedStyle(node);
                const box = node.getBoundingClientRect();
                return {
                    background: style.backgroundImage,
                    color: style.backgroundColor,
                    blur: style.backdropFilter,
                    border: style.border,
                    shadow: style.boxShadow,
                    radius: style.borderRadius,
                    left: box.left, right: box.right,
                    top: box.top, bottom: box.bottom,
                    hitTests: [...node.querySelectorAll('[role="option"]')].filter(option => {
                        const rect = option.getBoundingClientRect();
                        return rect.top >= box.top && rect.bottom <= box.bottom;
                    }).map(option => {
                        const rect = option.getBoundingClientRect();
                        return option.contains(document.elementFromPoint(
                            rect.left + rect.width / 2, rect.top + rect.height / 2,
                        ));
                    }),
                    blockedOptions: [...node.querySelectorAll('[role="option"]')].flatMap(option => {
                        const rect = option.getBoundingClientRect();
                        if (rect.top < box.top || rect.bottom > box.bottom) return [];
                        const hit = document.elementFromPoint(
                            rect.left + rect.width / 2, rect.top + rect.height / 2,
                        );
                        return option.contains(hit) ? [] : [{
                            option: option.textContent.trim(),
                            hit: hit?.outerHTML.slice(0, 350),
                        }];
                    }),
                };
            }"""
        )
        assert catalog_material["color"] == materials["menuColor"], catalog_material
        assert catalog_material["background"] == materials["menuBackground"], catalog_material
        assert catalog_material["blur"] == materials["menuBlur"], catalog_material
        assert catalog_material["border"] == materials["menuBorder"], catalog_material
        assert catalog_material["shadow"] == materials["menuShadow"], catalog_material
        assert catalog_material["radius"] == materials["menuRadius"], catalog_material
        assert catalog_material["left"] >= -1, catalog_material
        assert catalog_material["right"] <= width + 1, catalog_material
        assert catalog_material["top"] >= -1, catalog_material
        assert catalog_material["bottom"] <= height + 1, catalog_material
        assert catalog_material["hitTests"] and all(catalog_material["hitTests"]), catalog_material["blockedOptions"]
        expect(catalog_chevron).to_have_css(
            "transition-duration", "0.001s" if motion == "reduce" else "0.18s",
        )
        page.screenshot(path=str(
            screenshot_dir
            / f"shared-select-catalog-{color_scheme}-{width}x{height}-{motion}.png"
        ))
        page.keyboard.press("Escape")
        expect(catalog_menu).to_be_hidden()
        expect(catalog_trigger).to_be_focused()
        assert_shared_chevron_rotation(page, catalog_chevron, -90)
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
