"""Production and catalog segmented controls share their complete visual contract.

Code version: v1.0.0-codex.0
"""

import pytest
from playwright.sync_api import expect

from tests import test_sidebar_e2e


disposable_browser = test_sidebar_e2e.disposable_browser
sidebar_server_url = test_sidebar_e2e.sidebar_server_url


@pytest.mark.parametrize("width", [1024, 390])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_production_and_catalog_segmented_materials_match(
    disposable_browser, sidebar_server_url, width, scheme
):
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 863}, color_scheme=scheme
    )
    page = context.new_page()
    try:
        page.goto(f"{sidebar_server_url}/settings/style-tokens")
        catalog = page.locator("#segmented-control .range-mode-shell")
        specimen = catalog.evaluate(
            """control => {
                const style = getComputedStyle(control);
                return [style.backgroundImage, style.backgroundColor,
                    style.boxShadow, style.backdropFilter];
            }"""
        )
        page.goto(f"{sidebar_server_url}/browser?view=text&source=chatgpt&session_view=1")
        production = page.locator(".browser-content-mode-control")
        actual = production.evaluate(
            """control => {
                const style = getComputedStyle(control);
                return [style.backgroundImage, style.backgroundColor,
                    style.boxShadow, style.backdropFilter];
            }"""
        )
        assert actual == specimen
        expect(production).to_have_css("height", "32px")
        for label in production.locator(".segmented-control-option > span").all():
            expect(label).to_have_css("white-space", "nowrap")
    finally:
        context.close()


@pytest.mark.parametrize("width", [1024, 390])
def test_long_labels_scroll_without_losing_disabled_slots_or_active_pill(
    disposable_browser, sidebar_server_url, width
):
    context = disposable_browser.new_context(viewport={"width": width, "height": 863})
    page = context.new_page()
    try:
        page.goto(f"{sidebar_server_url}/settings/style-tokens")
        page.evaluate(
            """() => {
                const host = document.createElement('div');
                host.style.cssText = 'width:220px;max-width:100%;position:fixed;top:10px;left:10px;z-index:99999';
                host.innerHTML = `<div id="long_mode" class="segmented-control" data-option-count="3">
                    <label class="segmented-control-option"><input id="long_a" name="long_mode" type="radio" checked><span>Long text mode one</span></label>
                    <label class="segmented-control-option"><input id="long_b" name="long_mode" type="radio" disabled><span>Unavailable mode</span></label>
                    <label class="segmented-control-option"><input id="long_c" name="long_mode" type="radio"><span>Third long option</span></label>
                </div>`;
                document.body.append(host);
                window.CACHELIKES_SEGMENTED_CONTROLS.syncAll();
            }"""
        )
        control = page.locator("#long_mode")
        expect(control).to_have_attribute("data-option-count", "3")
        expect(control).to_have_attribute("data-segmented-overflow", "1")
        expect(page.locator("#long_b")).to_be_disabled()
        expect(control.locator("label").nth(1).locator("span")).to_have_css("opacity", "0.55")
        page.locator("#long_a").focus()
        page.locator("#long_a").press("ArrowRight")
        expect(page.locator("#long_c")).to_be_checked()
        expect(control).to_have_attribute("data-segmented-active-index", "2")
        assert control.evaluate(
            """shell => {
                const option = shell.lastElementChild;
                const box = shell.getBoundingClientRect();
                const active = option.getBoundingClientRect();
                const pill = getComputedStyle(shell, '::before');
                return shell.scrollWidth > shell.clientWidth
                    && active.left >= box.left - 1 && active.right <= box.right + 1
                    && Math.abs(parseFloat(pill.width) - option.offsetWidth) <= 1
                    && Array.from(shell.querySelectorAll('label > span')).every(label =>
                        label.scrollWidth <= label.clientWidth + 1)
                    && document.documentElement.scrollWidth <= innerWidth;
            }"""
        )
    finally:
        context.close()
