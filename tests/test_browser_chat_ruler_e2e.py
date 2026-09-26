"""Isolated message ruler behavior and geometry. Code version: v1.0.0-codex.0."""

import pytest
from playwright.sync_api import Browser, Page, expect

from tests import test_sidebar_e2e


disposable_browser = test_sidebar_e2e.disposable_browser
seeded_chatgpt_table_browser_server_url = test_sidebar_e2e.seeded_chatgpt_table_browser_server_url


def _scroll_positions(page: Page) -> dict:
    return page.locator("[data-chat-scrollport]").evaluate("""scrollport => {
        const ancestors = [];
        for (let node = scrollport.parentElement; node; node = node.parentElement) {
            ancestors.push({top: node.scrollTop, left: node.scrollLeft});
        }
        return {messageTop: scrollport.scrollTop, ancestors, windowX: scrollX, windowY: scrollY};
    }""")


def _assert_preview_geometry(page: Page) -> None:
    geometry = page.locator("[data-chat-preview]").evaluate("""preview => {
        const rect = element => {
            const box = element.getBoundingClientRect();
            return {left: box.left, right: box.right, top: box.top, bottom: box.bottom};
        };
        const pane = document.querySelector('[data-browser-chat-pane]');
        const style = getComputedStyle(preview);
        return {
            preview: rect(preview), pane: rect(pane),
            viewport: {left: 0, right: innerWidth, top: 0, bottom: innerHeight},
            radii: [style.borderTopLeftRadius, style.borderTopRightRadius,
                style.borderBottomLeftRadius, style.borderBottomRightRadius],
            blur: style.backdropFilter,
            overflowX: preview.scrollWidth - preview.clientWidth,
            overflowY: preview.scrollHeight - preview.clientHeight,
        };
    }""")
    for owner in (geometry["pane"], geometry["viewport"]):
        for edge in ("left", "top"):
            assert geometry["preview"][edge] >= owner[edge] - 1, geometry
        for edge in ("right", "bottom"):
            assert geometry["preview"][edge] <= owner[edge] + 1, geometry
    assert geometry["radii"] == ["10px"] * 4, geometry
    assert "blur(" in geometry["blur"], geometry
    assert geometry["overflowX"] <= 1, geometry
    assert geometry["overflowY"] <= 1, geometry


@pytest.mark.parametrize(
    ("width", "height", "touch"),
    ((1_006, 791, False), (390, 844, True), (1_006, 500, True)),
)
def test_message_ruler_preview_navigation_and_scroll_containment(
    disposable_browser: Browser,
    seeded_chatgpt_table_browser_server_url: str,
    width: int,
    height: int,
    touch: bool,
) -> None:
    context = disposable_browser.new_context(
        viewport={"width": width, "height": height},
        has_touch=touch,
        is_mobile=touch,
        reduced_motion="reduce",
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(
            f"{seeded_chatgpt_table_browser_server_url}/browser"
            "?view=text&source=chatgpt&session_view=1",
        )
        page.locator(".browser-session-index-table .browser-session-table-title").first.click()
        pane = page.locator("[data-browser-chat-pane]")
        scrollport = pane.locator("[data-chat-scrollport]")
        articles = scrollport.locator("article.browser-chat-message")
        expect(articles).to_have_count(2)
        ruler = pane.get_by_role("navigation", name="Message navigation", exact=True)
        markers = ruler.locator("button[data-chat-marker]")
        expect(markers).to_have_count(2)
        expect(ruler).to_be_visible()
        for index in range(2):
            article_id = articles.nth(index).get_attribute("id")
            assert article_id
            expect(markers.nth(index)).to_have_attribute("data-chat-target", article_id)
            expect(articles.nth(index)).to_have_attribute("data-chat-number", str(index + 1))
        assistant = articles.nth(1)
        expect(assistant.locator(".browser-chat-role-mark")).to_be_visible()
        expect(assistant.locator(".browser-chat-message-role")).to_have_count(0)
        author_id = assistant.get_attribute("aria-labelledby")
        author = page.locator("#" + author_id)
        expect(author).to_have_class("browser-chat-role-label")
        expect(author).to_have_text("ChatGPT")
        hidden_author = author.bounding_box()
        assert hidden_author is not None
        assert hidden_author["width"] <= 1 and hidden_author["height"] <= 1
        expect(author).to_have_css("clip-path", "inset(50%)")
        expect(articles.first.locator(".browser-chat-message-role")).to_have_text("You")

        contained = pane.evaluate("""pane => {
            const inside = (child, owner) => {
                const box = child.getBoundingClientRect();
                const boundary = owner.getBoundingClientRect();
                return box.left >= boundary.left - 1 && box.right <= boundary.right + 1
                    && box.top >= boundary.top - 1 && box.bottom <= boundary.bottom + 1;
            };
            return {
                pane: inside(pane, pane.closest('.browser-text-summary-card')),
                scrollport: inside(pane.querySelector('[data-chat-scrollport]'), pane),
                ruler: inside(pane.querySelector('.browser-chat-ruler'), pane),
                overflow: document.documentElement.scrollWidth - innerWidth,
            };
        }""")
        assert contained["pane"] and contained["scrollport"] and contained["ruler"], contained
        assert contained["overflow"] <= 1, contained
        expect(markers.first).to_have_attribute("aria-current", "true")

        preview = pane.get_by_role("tooltip")
        markers.last.hover()
        expect(preview).to_be_visible()
        expect(preview.locator("[data-chat-preview-label]")).to_contain_text("ChatGPT")
        expect(preview.locator("[data-chat-preview-copy]")).to_contain_text("Metadata field")
        assert preview.locator("[data-chat-preview-copy]").evaluate("node => node.childElementCount") == 0
        _assert_preview_geometry(page)

        markers.first.focus()
        expect(preview).to_be_visible()
        expect(preview.locator("[data-chat-preview-label]")).to_contain_text("You")
        expect(preview.locator("[data-chat-preview-copy]")).to_have_text("Please review the metadata table.")
        _assert_preview_geometry(page)
        keyboard_scroll = _scroll_positions(page)
        markers.first.press("ArrowDown")
        expect(markers.last).to_be_focused()
        expect(preview.locator("[data-chat-preview-label]")).to_contain_text("ChatGPT")
        assert _scroll_positions(page) == keyboard_scroll
        markers.last.press("Home")
        expect(markers.first).to_be_focused()
        page.keyboard.press("Escape")
        expect(preview).to_be_hidden()
        expect(markers.first).to_be_focused()
        scrollport.evaluate("""async element => {
            element.scrollTop = 1;
            await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        }""")
        expect(preview).to_be_hidden()
        scrollport.evaluate("element => { element.scrollTop = 0; }")

        before = _scroll_positions(page)
        markers.last.click()
        expect(markers.last).to_have_attribute("aria-current", "true")
        page.wait_for_function("document.querySelector('[data-chat-scrollport]').scrollTop > 0")
        after = _scroll_positions(page)
        assert after["messageTop"] > before["messageTop"]
        assert after["ancestors"] == before["ancestors"]
        assert (after["windowX"], after["windowY"]) == (before["windowX"], before["windowY"])

        scrollport.evaluate("element => { element.scrollTop = 0; }")
        expect(markers.first).to_have_attribute("aria-current", "true")
        scrollport.evaluate("element => { element.scrollTop = element.scrollHeight; }")
        expect(markers.last).to_have_attribute("aria-current", "true")
        markers.first.click()
        expect(markers.first).to_have_attribute("aria-current", "true")
        assert not errors
    finally:
        context.close()
