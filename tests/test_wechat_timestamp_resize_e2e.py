"""Keep nested source clocks on one outer rail after repeated viewport changes.

Code version: v1.0.0-codex.0
"""

import json

import pytest
from playwright.sync_api import expect

from tests import test_wechat_nested_forwarded_e2e as fixtures


disposable_browser = fixtures.disposable_browser
forwarded_activity_store = fixtures.forwarded_activity_store
nested_server = fixtures.nested_server


@pytest.mark.parametrize("initial_width", [390, 1280])
def test_nested_clock_rail_survives_repeated_resizes_and_disclosure_reentry(
    disposable_browser, nested_server, initial_width,
):
    fixture = nested_server
    context = disposable_browser.new_context(
        viewport={"width": initial_width, "height": 1100}, reduced_motion="reduce",
    )
    page = context.new_page()
    try:
        card = fixtures._open(page, fixture)
        current, disclosures = card, []
        for _depth in range(3):
            disclosure = current.locator(":scope > details[data-wechat-forwarded]")
            disclosure.locator(":scope > summary").press("Enter")
            disclosures.append(disclosure)
            current = disclosure.locator(".wechat-forwarded-transcript > .wechat-message-row > article").first
        expect(current.locator(":scope > .wechat-message-text")).to_have_text(fixtures.BODY)
        for width in (initial_width, 390, 1280, 390, 1280):
            disclosures[1].locator(":scope > summary").press("Space")
            disclosures[1].locator(":scope > summary").press("Enter")
            page.set_viewport_size({"width": width, "height": 1100})
            geometry = card.evaluate("""async card => {
                    await new Promise(requestAnimationFrame);
                    await new Promise(requestAnimationFrame);
                    const root = card.parentElement;
                    const clocks = [root, ...card.querySelectorAll('.wechat-message-row')]
                        .filter(row => !row.closest('details:not([open])'))
                        .map(row => {
                            const time = row.querySelector(':scope > time');
                            const bubble = row.querySelector(':scope > article');
                            const bounds = row.getBoundingClientRect();
                            const style = getComputedStyle(row);
                            return {
                                clockRight: time?.getBoundingClientRect().right,
                                rowRight: bounds.right, width: bounds.width,
                                height: bounds.height, logicalHeight: style.height,
                                inset: row.style.getPropertyValue('--wechat-time-inset'),
                                right: time ? getComputedStyle(time).right : '',
                                bubblePadding: getComputedStyle(bubble).paddingRight,
                            };
                        });
                    const ancestors = [];
                    for (let node = root; node; node = node.parentElement) {
                        const style = getComputedStyle(node);
                        ancestors.push({
                            owner: node.className, property: style.transitionProperty,
                            duration: style.transitionDuration,
                        });
                    }
                    return {
                        reference: root.querySelector(':scope > time').getBoundingClientRect().right,
                        clocks: clocks.filter(clock => clock.clockRight !== undefined),
                        ancestors,
                        animations: document.getAnimations().map(animation => ({
                            target: animation.effect?.target?.className,
                            property: animation.transitionProperty,
                            time: animation.currentTime,
                            duration: animation.effect?.getTiming().duration,
                        })),
                    };
                }""")
            assert len(geometry["clocks"]) == 5, geometry
            assert all(
                abs(clock["clockRight"] - geometry["reference"]) <= 1
                for clock in geometry["clocks"]
            ), json.dumps(geometry)
        assert fixture.history.read_bytes() == fixture.preserved
    finally:
        context.close()
