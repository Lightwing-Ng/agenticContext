"""Compact chat padding and unclipped external shadows. Code version: v1.0.0-codex.0."""

from io import BytesIO
import math
import re

from PIL import Image, ImageChops
import pytest
from playwright.sync_api import Browser, Page, expect

from tests import test_browser_chat_ruler_e2e, test_sidebar_e2e


disposable_browser = test_sidebar_e2e.disposable_browser
seeded_ruler_browser_server_url = test_browser_chat_ruler_e2e.seeded_ruler_browser_server_url


def _wait_for_effect_sync(page: Page) -> None:
    page.wait_for_function("""() => {
        const pane = document.querySelector('[data-browser-chat-pane]');
        const list = pane?.querySelector('[data-chat-scrollport]');
        const layer = pane?.querySelector('.browser-chat-effects');
        if (!list || !layer) return false;
        const boundary = list.getBoundingClientRect();
        const visible = [...list.querySelectorAll('[data-chat-message-id]')].filter(card => {
            const box = card.getBoundingClientRect();
            return box.bottom > boundary.top + 1 && box.top < boundary.bottom - 1;
        });
        return visible.length > 0 && visible.every(card => {
            const effect = layer.querySelector(`[data-chat-effect-for="${card.id}"]`);
            if (!effect || effect.hidden || getComputedStyle(effect).display === 'none') return false;
            const actual = effect.getBoundingClientRect();
            const expected = card.getBoundingClientRect();
            return ['left', 'top', 'width', 'height'].every(key => Math.abs(actual[key] - expected[key]) < 0.75);
        });
    }""")


def _chat_geometry(page: Page) -> dict:
    return page.locator("[data-browser-chat-pane]").evaluate("""pane => {
        const list = pane.querySelector('[data-chat-scrollport]');
        const layer = pane.querySelector('.browser-chat-effects');
        const rectangle = element => {
            const box = element.getBoundingClientRect();
            return {left: box.left, right: box.right, top: box.top, bottom: box.bottom,
                width: box.width, height: box.height};
        };
        const boundary = list.getBoundingClientRect();
        const style = getComputedStyle(list);
        const cards = [...list.querySelectorAll('[data-chat-message-id]')];
        const metrics = pane.closest('.browser-text-summary-card').querySelector('.browser-text-metric-grid');
        return {
            pane: rectangle(pane), list: rectangle(list),
            padding: [style.paddingTop, style.paddingRight, style.paddingBottom, style.paddingLeft],
            listOverflow: [style.overflowX, style.overflowY],
            effectLayerParent: layer.parentElement === pane,
            effectLayerOutsideList: !list.contains(layer),
            effectLayerHidden: layer.getAttribute('aria-hidden'),
            effectLayerPointerEvents: getComputedStyle(layer).pointerEvents,
            effectLayerBackground: getComputedStyle(layer).backgroundColor,
            effectLayerText: layer.textContent.trim(),
            effectLayerFocusable: layer.querySelectorAll('a,button,input,[tabindex]').length,
            cards: cards.map(card => {
                const box = card.getBoundingClientRect();
                const effect = layer.querySelector(`[data-chat-effect-for="${card.id}"]`);
                return {
                    id: card.id, box: rectangle(card), nativeShadow: getComputedStyle(card).boxShadow,
                    visible: box.bottom > boundary.top + 1 && box.top < boundary.bottom - 1,
                    effect: effect ? rectangle(effect) : null,
                    shadow: effect ? getComputedStyle(effect).boxShadow : null,
                    effectBackground: effect ? getComputedStyle(effect).backgroundColor : null,
                };
            }),
            ruler: pane.querySelector('.browser-chat-ruler') ? rectangle(pane.querySelector('.browser-chat-ruler')) : null,
            metricsBottom: metrics.getBoundingClientRect().bottom,
            scrollTop: list.scrollTop, scrollHeight: list.scrollHeight, clientHeight: list.clientHeight,
            documentOverflow: document.documentElement.scrollWidth - innerWidth,
        };
    }""")


def _assert_compact_geometry(geometry: dict, *, has_ruler: bool) -> None:
    assert geometry["padding"] == ["8px"] * 4, geometry
    assert geometry["listOverflow"] == ["hidden", "auto"], geometry
    assert geometry["effectLayerParent"] and geometry["effectLayerOutsideList"], geometry
    assert geometry["effectLayerHidden"] == "true", geometry
    assert geometry["effectLayerPointerEvents"] == "none", geometry
    assert geometry["effectLayerBackground"] == "rgba(0, 0, 0, 0)", geometry
    assert geometry["effectLayerText"] == "" and geometry["effectLayerFocusable"] == 0, geometry
    assert geometry["documentOverflow"] <= 1, geometry
    assert geometry["list"]["top"] >= geometry["metricsBottom"] + 7, geometry
    assert geometry["pane"]["right"] - geometry["list"]["right"] == pytest.approx(32 if has_ruler else 0, abs=1), geometry
    for card in geometry["cards"]:
        assert card["nativeShadow"] == "none" or all(
            "inset" in layer for layer in re.split(r",\s*(?![^()]*\))", card["nativeShadow"])
        ), card
        assert card["box"]["left"] - geometry["list"]["left"] == pytest.approx(8, abs=1), card
        assert geometry["list"]["right"] - card["box"]["right"] == pytest.approx(8, abs=1), card
        if not card["visible"]:
            continue
        assert card["shadow"] and card["shadow"] != "none", card
        assert card["effectBackground"] == "rgba(0, 0, 0, 0)", card
        for edge in ("left", "top", "width", "height"):
            assert card["effect"][edge] == pytest.approx(card["box"][edge], abs=0.75), card
        if has_ruler:
            assert geometry["ruler"]["left"] >= card["box"]["right"], geometry
    if geometry["scrollTop"] == 0:
        assert geometry["cards"][0]["box"]["top"] - geometry["list"]["top"] == pytest.approx(8, abs=1), geometry
    if geometry["scrollTop"] >= geometry["scrollHeight"] - geometry["clientHeight"] - 1:
        assert geometry["list"]["bottom"] - geometry["cards"][-1]["box"]["bottom"] == pytest.approx(8, abs=1), geometry


def _assert_shadow_pixels_outside_scrollport(page: Page, geometry: dict) -> None:
    """Compare the painted and hidden layer in the gutter beyond the scroll clipping edge."""
    boundary = geometry["list"]
    visible = [card for card in geometry["cards"] if card["visible"]]
    target = max(visible, key=lambda card: min(card["box"]["bottom"], boundary["bottom"])
                 - max(card["box"]["top"], boundary["top"]))
    top = max(target["box"]["top"], boundary["top"]) + 12
    bottom = min(target["box"]["bottom"], boundary["bottom"]) - 12
    assert bottom - top >= 12, geometry
    center = (top + bottom) / 2
    crop = (math.ceil(boundary["right"] + 2), math.floor(center - 6),
            math.ceil(boundary["right"] + 10), math.ceil(center + 6))
    layer = page.locator(".browser-chat-effects")
    painted = Image.open(BytesIO(page.screenshot(animations="disabled"))).convert("RGB")
    layer.evaluate("node => { node.style.visibility = 'hidden'; }")
    try:
        hidden = Image.open(BytesIO(page.screenshot(animations="disabled"))).convert("RGB")
    finally:
        layer.evaluate("node => { node.style.removeProperty('visibility'); }")
    difference = ImageChops.difference(painted.crop(crop), hidden.crop(crop))
    pixels = difference.load()
    changed_pixels = sum(
        max(pixels[x, y]) >= 1
        for x in range(difference.width)
        for y in range(difference.height)
    )
    assert changed_pixels >= 24, {"crop": crop, "changedPixels": changed_pixels, "geometry": geometry}


@pytest.mark.parametrize(
    ("width", "height", "touch"),
    ((1_006, 791, False), (390, 844, True), (1_006, 500, False)),
)
def test_chat_effects_keep_eight_pixel_padding_and_paint_outside_scrollport(
    disposable_browser: Browser,
    seeded_ruler_browser_server_url: str,
    width: int,
    height: int,
    touch: bool,
) -> None:
    context = disposable_browser.new_context(
        viewport={"width": width, "height": height},
        has_touch=touch,
        is_mobile=touch,
        reduced_motion="reduce",
        color_scheme="light",
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(f"{seeded_ruler_browser_server_url}/browser?view=text&source=chatgpt&session_view=1")
        page.locator(".browser-session-index-table .browser-session-table-title").first.click()
        expect(page.locator("[data-chat-message-id]")).to_have_count(7)
        page.evaluate("document.fonts.ready")
        scrollport = page.locator("[data-chat-scrollport]")
        for fraction in (0, 0.5, 1):
            scrollport.evaluate("(list, fraction) => { list.scrollTop = fraction * (list.scrollHeight - list.clientHeight); }", fraction)
            _wait_for_effect_sync(page)
            geometry = _chat_geometry(page)
            _assert_compact_geometry(geometry, has_ruler=True)
            _assert_shadow_pixels_outside_scrollport(page, geometry)
        page.set_viewport_size({"width": 430 if touch else 720, "height": 700 if touch else 560})
        _wait_for_effect_sync(page)
        resized = _chat_geometry(page)
        _assert_compact_geometry(resized, has_ruler=True)
        _assert_shadow_pixels_outside_scrollport(page, resized)
        page.locator("#global_theme_toggle").click()
        expect(page.locator("html")).to_have_attribute("data-theme-override", "dark")
        _wait_for_effect_sync(page)
        _assert_compact_geometry(_chat_geometry(page), has_ruler=True)
        if not touch and height == 791:
            page.goto(f"{seeded_ruler_browser_server_url}/browser?view=text&source=chatgpt&session_view=0")
            expect(page.locator(".browser-chat-ruler")).to_have_count(0)
            _wait_for_effect_sync(page)
            _assert_compact_geometry(_chat_geometry(page), has_ruler=False)
        assert not errors
    finally:
        context.close()
