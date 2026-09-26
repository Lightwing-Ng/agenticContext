"""Search lens alignment on isolated Local resources pages. Code version: v1.0.1-codex.0."""

import pytest
from playwright.sync_api import Browser, Page, expect

from tests import test_sidebar_e2e


disposable_browser = test_sidebar_e2e.disposable_browser
seeded_chatgpt_browser_server_url = test_sidebar_e2e.seeded_chatgpt_browser_server_url


def _assert_search_lens_center(page: Page) -> None:
    geometry = page.locator(".browser-search-control").evaluate("""async control => {
        const bounds = control.getBoundingClientRect();
        const controlStyle = getComputedStyle(control);
        const icon = control.querySelector('.browser-search-icon');
        const iconBounds = icon.getBoundingClientRect();
        const style = getComputedStyle(icon);
        const iconUrl = style.maskImage.match(/url\\(["']?(.*?)["']?\\)/)[1];
        const markup = await (await fetch(iconUrl)).text();
        const svg = new DOMParser().parseFromString(markup, 'image/svg+xml');
        const [, , sourceWidth, sourceHeight] = svg.documentElement.getAttribute('viewBox').split(/\\s+/).map(Number);
        const circleCenter = Number(svg.querySelector('path').getAttribute('d').match(/^M0 ([\\d.]+)/)[1]);
        const scale = style.maskSize === 'contain'
            ? Math.min(iconBounds.width / sourceWidth, iconBounds.height / sourceHeight)
            : parseFloat(style.maskSize) / sourceWidth;
        const position = (value, available) => {
            const percent = value.match(/([-\\d.]+)%/);
            const pixels = value.match(/([-+]?\\s*[\\d.]+)px/);
            return (percent ? Number(percent[1]) * available / 100 : 0)
                + (pixels ? Number(pixels[1].replace(/\\s/g, '')) : 0);
        };
        const positions = style.maskPosition.match(/calc\\([^)]+\\)|\\S+/g);
        const circleX = iconBounds.x + position(positions[0], iconBounds.width - sourceWidth * scale)
            + circleCenter * scale;
        const circleY = iconBounds.y + position(positions[1], iconBounds.height - sourceHeight * scale)
            + circleCenter * scale;
        return {
            height: bounds.height,
            inputHeight: control.querySelector('.browser-search-input').getBoundingClientRect().height,
            radius: parseFloat(controlStyle.borderTopLeftRadius),
            lensOffsetX: circleX - bounds.x - bounds.height / 2,
            lensOffsetY: circleY - bounds.y - bounds.height / 2,
            glyphWidth: sourceWidth * scale,
            overflow: document.documentElement.scrollWidth - innerWidth,
        };
    }""")
    assert geometry["height"] == 32, geometry
    assert geometry["inputHeight"] == 30, geometry
    assert geometry["radius"] >= geometry["height"] / 2, geometry
    assert abs(geometry["lensOffsetX"]) <= 1, geometry
    assert abs(geometry["lensOffsetY"]) <= 1, geometry
    assert geometry["glyphWidth"] == 16, geometry
    assert geometry["overflow"] <= 1, geometry


@pytest.mark.parametrize(("width", "height"), ((1_006, 791), (390, 844), (1_006, 500)))
@pytest.mark.parametrize("color_scheme", ("light", "dark"))
def test_search_lens_stays_concentric_with_and_without_session_scope(
    disposable_browser: Browser,
    seeded_chatgpt_browser_server_url: str,
    width: int,
    height: int,
    color_scheme: str,
) -> None:
    context = disposable_browser.new_context(
        viewport={"width": width, "height": height},
        color_scheme=color_scheme,
        reduced_motion="reduce",
        has_touch=width == 390,
    )
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(
            f"{seeded_chatgpt_browser_server_url}/browser?view=text&source=chatgpt&session_view=1",
            wait_until="networkidle",
        )
        page.evaluate("document.fonts.ready")
        _assert_search_lens_center(page)
        page.locator(".browser-session-table-title").click()
        expect(page.locator("[data-browser-session-tag]")).to_be_visible()
        _assert_search_lens_center(page)
        page.locator("#browser_search_input").fill("timestamp")
        page.locator("#browser_search_input").press("Enter")
        expect(page.locator("[data-chat-message-id]")).to_have_count(1)
        expect(page.locator("[data-browser-session-tag]")).to_be_visible()
        _assert_search_lens_center(page)
        page.locator("[data-browser-session-scope-remove]").click()
        expect(page.locator("[data-browser-session-tag]")).to_have_count(0)
        expect(page.locator("[data-chat-message-id]")).to_have_count(1)
        expect(page.locator("#browser_search_input")).to_have_value("timestamp")
        _assert_search_lens_center(page)
        assert not errors
    finally:
        context.close()
