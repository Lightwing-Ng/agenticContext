"""Keep style-token select examples reachable near viewport and scroll boundaries.

Code version: v1.0.0-codex.0
"""

import pytest
from playwright.sync_api import expect

from tests import test_sidebar_e2e as fixtures


disposable_browser = fixtures.disposable_browser
sidebar_server_url = fixtures.sidebar_server_url


def _move_trigger(page, trigger, top):
    trigger.evaluate(
        """(node, top) => {
            let owner = node.parentElement;
            while (owner && !(owner.scrollHeight > owner.clientHeight + 1
                && /auto|scroll/.test(getComputedStyle(owner).overflowY))) {
                owner = owner.parentElement;
            }
            const scroller = owner || document.scrollingElement;
            scroller.scrollTo({
                top: scroller.scrollTop + node.getBoundingClientRect().top - top,
                behavior: 'instant',
            });
        }""",
        top,
    )
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    assert abs(trigger.bounding_box()["y"] - top) <= 1


def _assert_reachable(menu, width, height):
    geometry = menu.evaluate(
        """node => {
            const bounds = node.getBoundingClientRect();
            const visible = [...node.querySelectorAll('[role="option"]')].filter(option => {
                const rect = option.getBoundingClientRect();
                return rect.top >= bounds.top && rect.bottom <= bounds.bottom;
            });
            return {
                top: bounds.top, bottom: bounds.bottom,
                left: bounds.left, right: bounds.right,
                hit: visible.map(option => {
                    const rect = option.getBoundingClientRect();
                    return option.contains(document.elementFromPoint(
                        rect.left + rect.width / 2, rect.top + rect.height / 2,
                    ));
                }),
            };
        }"""
    )
    assert 0 <= geometry["top"] <= geometry["bottom"] <= height + 1, geometry
    assert 0 <= geometry["left"] <= geometry["right"] <= width + 1, geometry
    assert geometry["hit"] and all(geometry["hit"]), geometry


@pytest.mark.parametrize("width", [1006, 390])
def test_catalog_select_stays_reachable_when_opened_low_then_scrolled_and_resized(
    disposable_browser, sidebar_server_url, width,
):
    context = disposable_browser.new_context(viewport={"width": width, "height": 500})
    page = context.new_page()
    try:
        page.goto(f"{sidebar_server_url}/settings/style-tokens#shared-select-dropdown")
        page.evaluate("() => document.fonts.ready")
        catalog = page.locator("#shared-select-dropdown [data-style-token-shared-filter]")
        trigger = catalog.locator("[data-style-token-shared-filter-trigger]")
        menu = catalog.locator("[data-style-token-shared-filter-menu]")
        _move_trigger(page, trigger, 390)
        trigger.press("ArrowDown")
        expect(menu.locator('[aria-selected="true"]')).to_be_focused()
        page.wait_for_function(
            "node => node.getAnimations().every(animation => animation.playState === 'finished')",
            arg=menu.element_handle(),
        )
        _assert_reachable(menu, width, 500)
        _move_trigger(page, trigger, 100)
        _assert_reachable(menu, width, 500)
        page.set_viewport_size({"width": width, "height": 300})
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        _assert_reachable(menu, width, 300)
        page.keyboard.press("Escape")
        expect(menu).to_be_hidden()
        expect(trigger).to_be_focused()
    finally:
        context.close()
