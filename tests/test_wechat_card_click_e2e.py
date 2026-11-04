"""Message cards share provenance activation without taking over text or actions.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

import json
from urllib.parse import urlencode

import pytest
from playwright.sync_api import expect

from tests import test_wechat_contact_analysis_e2e as contact_fixtures
from tests import test_wechat_forwarded_e2e as forwarded_fixtures
from tests import test_wechat_sender_runs_e2e as sender_fixtures


disposable_browser = sender_fixtures.disposable_browser
sender_run_server = sender_fixtures.sender_run_server
forwarded_server = forwarded_fixtures.forwarded_server
contact_analysis_server = contact_fixtures.contact_analysis_server


def _open(page, fixture):
    response = page.goto(
        fixture.url
        + "/browser?"
        + urlencode(
            {
                "view": "text",
                "source": "wechat",
                "account": "account:" + sender_fixtures.OWNER,
                "session": fixture.session,
            }
        ),
        wait_until="networkidle",
    )
    assert response.status == 200
    toggle = page.locator("#sidebar_toggle")
    if toggle.get_attribute("aria-expanded") == "true":
        toggle.click()
    page.evaluate("() => document.fonts.ready")
    cards = page.locator(".wechat-message")
    expect(cards).to_have_count(8)
    return cards


def _click_text(text):
    """Use a visible glyph rather than empty width under the external clock."""
    text.scroll_into_view_if_needed()
    position = text.evaluate("""element => {
        const range = document.createRange();
        range.setStart(element.firstChild, 0);
        range.setEnd(element.firstChild, 1);
        const glyph = range.getBoundingClientRect();
        const bounds = element.getBoundingClientRect();
        return {x:glyph.left + glyph.width / 2 - bounds.left,
            y:glyph.top + glyph.height / 2 - bounds.top};
    }""")
    text.click(position=position)


@pytest.mark.parametrize("width", [767, 390])
@pytest.mark.parametrize("motion", ["no-preference", "reduce"])
def test_normal_and_compact_body_clicks_share_the_native_disclosure(
    disposable_browser, sender_run_server, width, motion
):
    fixture = sender_run_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1109}, reduced_motion=motion
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        cards = _open(page, fixture)
        for index in (0, 1, 6):
            card = cards.nth(index)
            details = card.locator(":scope > .wechat-provenance")
            summary = details.locator(":scope > summary")
            text = card.locator(":scope > .wechat-message-text")
            expect(card).to_have_attribute(
                "data-wechat-show-sender", "true" if index == 0 else "false"
            )
            expect(details).not_to_have_attribute("open", "")
            _click_text(text)
            expect(details).to_have_attribute("open", "")
            expect(details.get_by_text("Contact ID", exact=True)).to_be_visible()
            details.get_by_text("Source account", exact=True).click()
            expect(details).not_to_have_attribute("open", "")
            _click_text(text)
            expect(details).to_have_attribute("open", "")
            _click_text(text)
            expect(details).not_to_have_attribute("open", "")
            expect(summary).to_have_accessible_name("Source details")
            if index == 6:
                text.scroll_into_view_if_needed()
                point = text.evaluate("""element => {
                    const box = element.getBoundingClientRect();
                    const x = box.left + box.width / 2;
                    const y = box.top + box.height / 2;
                    const target = document.elementFromPoint(x, y);
                    return {x,y,owner:target?.closest('.wechat-message')?.id,
                        summary:Boolean(target?.closest('summary'))};
                }""")
                assert point["owner"] == card.get_attribute("id")
                page.mouse.click(point["x"], point["y"])
                expect(details).to_have_attribute("open", "")
                _click_text(text)
                expect(details).not_to_have_attribute("open", "")
        compact = cards.nth(1).locator(":scope > .wechat-provenance")
        summary = compact.locator(":scope > summary")
        summary.focus()
        summary.press("Enter")
        expect(compact).to_have_attribute("open", "")
        summary.press("Space")
        expect(compact).not_to_have_attribute("open", "")
        assert not errors
        assert fixture.history.read_bytes() == fixture.original
    finally:
        context.close()


@pytest.mark.parametrize("width", [767, 390])
def test_real_drag_selection_keeps_compact_text_and_details_closed(
    disposable_browser, sender_run_server, width
):
    fixture = sender_run_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1109}, reduced_motion="reduce"
    )
    page = context.new_page()
    try:
        card = _open(page, fixture).nth(5)
        expect(card).to_have_attribute("data-wechat-show-sender", "false")
        text = card.locator(":scope > .wechat-message-text")
        text.scroll_into_view_if_needed()
        text.evaluate("""element => {
            const port = element.closest('[data-chat-scrollport]');
            port.scrollTop += element.getBoundingClientRect().top
                - port.getBoundingClientRect().top - 24;
        }""")
        coordinates = text.evaluate("""element => {
            const text = element.firstChild;
            const first = document.createRange();
            first.setStart(text, 0);first.setEnd(text, 1);
            const last = document.createRange();
            last.setStart(text, 14);last.setEnd(text, 15);
            const a = first.getBoundingClientRect();
            const b = last.getBoundingClientRect();
            return {x1:a.left + 0.5,y1:a.top + a.height / 2,
                x2:b.right + 0.5,y2:b.top + b.height / 2};
        }""")
        page.mouse.move(coordinates["x1"], coordinates["y1"])
        page.mouse.down()
        page.mouse.move(coordinates["x2"], coordinates["y2"], steps=16)
        page.mouse.up()
        selected = page.evaluate("() => window.getSelection().toString()")
        assert len(selected) >= 8
        assert selected in sender_fixtures.LONG_TEXT
        expect(card.locator(":scope > .wechat-provenance")).not_to_have_attribute(
            "open", ""
        )
        assert fixture.history.read_bytes() == fixture.original
    finally:
        context.close()


def test_contact_link_navigates_without_opening_message_details(
    disposable_browser, contact_analysis_server
):
    fixture = contact_analysis_server
    context = disposable_browser.new_context(reduced_motion="reduce")
    page = context.new_page()
    page.route(
        "**/browser/wechat/contacts/**",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body="<!doctype html><title>Contact destination</title>",
        ),
    )
    try:
        page.goto(
            fixture.url
            + "/browser?"
            + urlencode(
                {
                    "view": "text",
                    "source": "wechat",
                    "account": "account:" + sender_fixtures.OWNER,
                    "session": fixture.session,
                }
            ),
            wait_until="networkidle",
        )
        toggle = page.locator("#sidebar_toggle")
        if toggle.get_attribute("aria-expanded") == "true":
            toggle.click()
        card = page.locator(".wechat-message:has(.wechat-contact-link)").first
        link = card.locator(".wechat-contact-link")
        expect(link).to_have_count(1)
        href = link.get_attribute("href")
        card.evaluate("""card => {
            const details = card.querySelector(':scope > .wechat-provenance');
            addEventListener('beforeunload', () => {
                sessionStorage.setItem('messageDetailsBeforeContactNavigation', String(details.open));
            }, {once:true});
        }""")
        link.click()
        expect(page).to_have_url(fixture.url + href)
        assert (
            page.evaluate(
                "() => sessionStorage.getItem('messageDetailsBeforeContactNavigation')"
            )
            == "false"
        )
        assert fixture.history.read_bytes() == fixture.before[0]
    finally:
        context.close()


def test_resource_action_keeps_its_own_behavior_without_opening_details(
    disposable_browser, sender_run_server
):
    fixture = sender_run_server
    context = disposable_browser.new_context(reduced_motion="reduce")
    page = context.new_page()
    requests = []

    def resource_response(route):
        requests.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"revealed": True, "target_kind": "folder"}),
        )

    page.route("**/api/browser/wechat/resources/*/reveal", resource_response)
    try:
        card = _open(page, fixture).nth(2)
        button = card.locator("[data-wechat-resource-reveal]")
        button.click()
        expect(button.locator("[data-wechat-resource-feedback]")).to_have_text(
            "Folder shown in Finder"
        )
        assert len(requests) == 1
        expect(card.locator(":scope > .wechat-provenance")).not_to_have_attribute(
            "open", ""
        )
        fixture.reveal.assert_not_called()
        fixture.open_folder.assert_not_called()
        assert fixture.history.read_bytes() == fixture.original
    finally:
        context.close()


@pytest.mark.parametrize("width", [767, 390])
def test_nested_body_only_toggles_its_own_provenance(
    disposable_browser, forwarded_server, width
):
    fixture = forwarded_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1109}, reduced_motion="reduce"
    )
    page = context.new_page()
    try:
        page.goto(
            fixture.url
            + "/browser?"
            + urlencode(
                {
                    "view": "text",
                    "source": "wechat",
                    "account": "account:" + fixture.owner,
                    "session": fixture.session,
                }
            ),
            wait_until="networkidle",
        )
        toggle = page.locator("#sidebar_toggle")
        if toggle.get_attribute("aria-expanded") == "true":
            toggle.click()
        parent = page.locator("#wechat_message_" + fixture.parent_id)
        outer_details = parent.locator(":scope > .wechat-provenance")
        forwarded = parent.locator(":scope > .wechat-forwarded")
        forwarded.locator(":scope > summary").click()
        expect(forwarded).to_have_attribute("open", "")
        expect(outer_details).not_to_have_attribute("open", "")
        child = forwarded.locator(".wechat-message").first
        details = child.locator(":scope > .wechat-provenance")
        text = child.locator(":scope > .wechat-message-text")
        _click_text(text)
        expect(details).to_have_attribute("open", "")
        expect(outer_details).not_to_have_attribute("open", "")
        expect(forwarded).to_have_attribute("open", "")
        details.get_by_text("Source account", exact=True).click()
        expect(details).not_to_have_attribute("open", "")
        expect(outer_details).not_to_have_attribute("open", "")
        expect(forwarded).to_have_attribute("open", "")
        _click_text(text)
        expect(details).to_have_attribute("open", "")
        _click_text(text)
        expect(details).not_to_have_attribute("open", "")
        expect(outer_details).not_to_have_attribute("open", "")
        forwarded.locator(":scope > summary").click()
        expect(forwarded).not_to_have_attribute("open", "")
        expect(outer_details).not_to_have_attribute("open", "")
        assert fixture.history.read_bytes() == fixture.preserved
    finally:
        context.close()
