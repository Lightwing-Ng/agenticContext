"""Expanded source metadata closes back to its compact, selectable message.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from urllib.parse import urlencode

import pytest
from playwright.sync_api import expect

from tests import test_wechat_card_click_e2e as card_fixtures
from tests import test_wechat_forwarded_e2e as forwarded_fixtures
from tests import test_wechat_quoted_reply_e2e as quote_fixtures
from tests import test_wechat_sender_runs_e2e as sender_fixtures


disposable_browser = sender_fixtures.disposable_browser
sender_run_server = sender_fixtures.sender_run_server
forwarded_server = forwarded_fixtures.forwarded_server
quoted_reply_server = quote_fixtures.quoted_reply_server


def _settle(page, details):
    page.wait_for_function(
        "element => !element.hasAttribute('data-collapse-motion')",
        arg=details.element_handle(),
    )
    page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame("
        "() => requestAnimationFrame(resolve)))"
    )


def _geometry(card):
    return card.evaluate("""card => {
        const box = card.getBoundingClientRect();
        const row = card.closest('.wechat-message-row');
        const time = row.querySelector(':scope > time');
        return {width:box.width, height:box.height,
            contained:row.hasAttribute('data-wechat-time-contained'),
            time:time.textContent, datetime:time.getAttribute('datetime')};
    }""")


def _border(separator):
    return separator.evaluate("""element => {
        const style = getComputedStyle(element);
        return {width:style.borderTopWidth, style:style.borderTopStyle,
            color:style.borderTopColor};
    }""")


def _assert_restored(page, card, details, baseline):
    expect(details).not_to_have_attribute("open", "")
    _settle(page, details)
    restored = _geometry(card)
    assert restored["width"] == pytest.approx(baseline["width"], abs=1)
    assert restored["height"] == pytest.approx(baseline["height"], abs=1)
    assert restored["contained"] == baseline["contained"]
    assert restored["time"] == baseline["time"]
    assert restored["datetime"] == baseline["datetime"]
    expect(details.locator(".wechat-provenance-separator")).not_to_be_visible()


@pytest.mark.parametrize("width", [803, 390])
@pytest.mark.parametrize("motion", ["no-preference", "reduce"])
def test_passive_metadata_restores_compact_geometry_and_reuses_quote_divider(
    disposable_browser, sender_run_server, quoted_reply_server, width, motion
):
    fixture = sender_run_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1217}, reduced_motion=motion
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        card = card_fixtures._open(page, fixture).nth(1)
        details = card.locator(":scope > .wechat-provenance")
        summary = details.locator(":scope > summary")
        text = card.locator(":scope > .wechat-message-text")
        expect(card).to_have_attribute("data-wechat-show-sender", "false")
        _settle(page, details)
        baseline = _geometry(card)
        separator = details.locator(
            ":scope > .ui-collapse-body > hr.wechat-quote-separator.wechat-provenance-separator"
        )
        expect(separator).to_have_count(1)
        expect(separator).not_to_be_visible()

        for label in ("Direction", "Source account", "Contact ID"):
            card_fixtures._click_text(text)
            expect(details).to_have_attribute("open", "")
            _settle(page, details)
            expect(separator).to_be_visible()
            assert separator.evaluate(
                "element => element.previousElementSibling.matches('dl.wechat-provenance-fields')"
            )
            assert _geometry(card)["height"] > baseline["height"] + 100
            details.get_by_text(label, exact=True).click()
            _assert_restored(page, card, details, baseline)

        summary.focus()
        summary.press("Enter")
        expect(details).to_have_attribute("open", "")
        _settle(page, details)
        source_border = _border(separator)
        assert source_border["width"] == "1px"
        assert source_border["style"] == "solid"
        summary.press("Space")
        _assert_restored(page, card, details, baseline)
        summary.click()
        expect(details).to_have_attribute("open", "")
        _settle(page, details)
        summary.click()
        _assert_restored(page, card, details, baseline)
        expect(text).to_have_text("Two.")
        assert fixture.history.read_bytes() == fixture.original

        quote = quoted_reply_server
        response = page.goto(
            quote.url
            + "/browser?"
            + urlencode(
                {
                    "view": "text",
                    "source": "wechat",
                    "account": "source:xlsx-2",
                    "session": quote.session,
                }
            ),
            wait_until="networkidle",
        )
        assert response.status == 200
        quote_separator = page.locator(
            "article.wechat-message > hr.wechat-quote-separator"
        ).first
        expect(quote_separator).to_be_visible()
        assert _border(quote_separator) == source_border
        assert quote.history.read_bytes() == quote.preserved_history
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("width", [803, 390])
def test_real_metadata_drag_preserves_selection_and_open_disclosure(
    disposable_browser, sender_run_server, width
):
    fixture = sender_run_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1217}, reduced_motion="reduce"
    )
    page = context.new_page()
    try:
        card = card_fixtures._open(page, fixture).nth(1)
        details = card.locator(":scope > .wechat-provenance")
        card_fixtures._click_text(card.locator(":scope > .wechat-message-text"))
        expect(details).to_have_attribute("open", "")
        _settle(page, details)
        value = details.locator("dt").filter(has_text="Source account").locator(
            "xpath=following-sibling::dd[1]"
        )
        expect(value).to_have_text(sender_fixtures.OWNER)
        value.scroll_into_view_if_needed()
        points = value.evaluate("""element => {
            const text = element.firstChild;
            const first = document.createRange();
            first.setStart(text, 0); first.setEnd(text, 1);
            const last = document.createRange();
            last.setStart(text, text.length - 1); last.setEnd(text, text.length);
            const a = first.getBoundingClientRect(), b = last.getBoundingClientRect();
            return {x1:a.left + 0.5, y1:a.top + a.height / 2,
                x2:b.right + 0.5, y2:b.top + b.height / 2};
        }""")
        page.mouse.move(points["x1"], points["y1"])
        page.mouse.down()
        page.mouse.move(points["x2"], points["y2"], steps=16)
        page.mouse.up()
        selection = page.evaluate("""() => {
            const selection = getSelection();
            return {collapsed:selection.isCollapsed, text:selection.toString()};
        }""")
        assert not selection["collapsed"]
        assert len(selection["text"]) >= 8
        assert selection["text"] in sender_fixtures.OWNER
        expect(details).to_have_attribute("open", "")
        details.locator(":scope > summary").focus()
        details.locator(":scope > summary").press("Space")
        expect(details).not_to_have_attribute("open", "")
        assert fixture.history.read_bytes() == fixture.original
    finally:
        context.close()


@pytest.mark.parametrize("width", [803, 390])
def test_nested_metadata_close_preserves_parent_and_forwarded_disclosures(
    disposable_browser, forwarded_server, width
):
    fixture = forwarded_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1217}, reduced_motion="reduce"
    )
    page = context.new_page()
    try:
        parent, forwarded = forwarded_fixtures.open_forwarded(page, fixture)
        outer = parent.locator(":scope > .wechat-provenance")
        forwarded.locator(":scope > summary").click()
        expect(forwarded).to_have_attribute("open", "")
        child = forwarded.locator(".wechat-message").first
        details = child.locator(":scope > .wechat-provenance")
        summary = details.locator(":scope > summary")
        card_fixtures._click_text(child.locator(":scope > .wechat-message-text"))
        expect(details).to_have_attribute("open", "")
        details.get_by_text("Source account", exact=True).click()
        expect(details).not_to_have_attribute("open", "")
        expect(outer).not_to_have_attribute("open", "")
        expect(forwarded).to_have_attribute("open", "")
        summary.focus()
        summary.press("Enter")
        expect(details).to_have_attribute("open", "")
        summary.press("Space")
        expect(details).not_to_have_attribute("open", "")
        summary.click()
        expect(details).to_have_attribute("open", "")
        summary.click()
        expect(details).not_to_have_attribute("open", "")
        expect(outer).not_to_have_attribute("open", "")
        expect(forwarded).to_have_attribute("open", "")
        forwarded.locator(":scope > summary").click()
        expect(forwarded).not_to_have_attribute("open", "")
        assert fixture.history.read_bytes() == fixture.preserved
    finally:
        context.close()
