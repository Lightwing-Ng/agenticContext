"""Contact statements navigate precisely while preserving plain-text selection.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import sys
from urllib.parse import urlencode, urlsplit

import pytest
from playwright.sync_api import expect

from tests import test_wechat_contact_analysis_e2e as fixtures


contact_analysis_server = fixtures.contact_analysis_server
duplicate_contact_analysis_server = fixtures.duplicate_contact_analysis_server
disposable_browser = fixtures.disposable_browser


def _open_contact(page, fixture, query=""):
    url = (
        fixture.url
        + "/browser/wechat/contacts/"
        + fixture.target_id
        + "?"
        + urlencode({"q": query, "sort": "newest"})
    )
    page.goto(url, wait_until="domcontentloaded")
    expect(page.locator("[data-contact-analysis-state]")).to_have_attribute(
        "data-contact-analysis-state", "ready"
    )
    toggle = page.locator("#sidebar_toggle")
    if toggle.get_attribute("aria-expanded") == "true":
        toggle.click()
    page.evaluate(
        """async () => {
            await document.fonts.ready;
            await Promise.allSettled(document.getAnimations().filter(animation =>
                Number.isFinite(animation.effect?.getComputedTiming().endTime)
            ).map(animation => animation.finished));
        }"""
    )
    return url


def _expect_original(page, message_id):
    page.wait_for_url("**/browser?**", wait_until="domcontentloaded")
    assert urlsplit(page.url).fragment == "wechat_message_" + message_id
    expect(page.locator("#wechat_message_" + message_id)).to_be_visible()


@pytest.mark.parametrize("width", [1026, 390])
def test_statement_drag_and_copy_preserve_selection_then_click_opens_original(
    disposable_browser, contact_analysis_server, tmp_path, width
):
    fixture = contact_analysis_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 900}, reduced_motion="reduce"
    )
    context.set_default_timeout(7000)
    context.set_default_navigation_timeout(15000)
    page = context.new_page()
    try:
        origin = _open_contact(page, fixture)
        row = page.locator("tr[data-contact-statement-id]").first
        message_id = row.get_attribute("data-contact-message-id")
        statement = row.locator("a[data-wechat-contact-statement]")
        expect(statement).to_have_attribute(
            "href", "/browser/wechat/messages/" + message_id
        )
        assert statement.evaluate("node => getComputedStyle(node).userSelect") == "text"
        assert statement.get_attribute("draggable") == "false"
        points = statement.evaluate(
            """node => {
                const text = node.firstChild;
                const start = document.createRange();
                start.setStart(text, 2);
                start.setEnd(text, 3);
                const end = document.createRange();
                end.setStart(text, 13);
                end.setEnd(text, 14);
                const first = start.getBoundingClientRect();
                const last = end.getBoundingClientRect();
                return {
                    x1: first.x, y1: first.y + first.height / 2,
                    x2: last.right, y2: last.y + last.height / 2,
                };
            }"""
        )
        page.mouse.move(points["x1"], points["y1"])
        page.mouse.down()
        page.mouse.move(points["x2"], points["y2"], steps=12)
        page.mouse.up()
        selected = page.evaluate("window.getSelection().toString()")
        assert selected and selected in statement.inner_text(), {
            "points": points,
            "pointer_target": page.evaluate(
                "point => document.elementFromPoint(point.x1, point.y1)?.outerHTML",
                points,
            ),
        }
        expect(page).to_have_url(origin)
        # Observe the copy command without changing the host system clipboard.
        page.evaluate(
            """() => document.addEventListener('copy', event => {
                window.contactCopiedText = window.getSelection().toString();
                event.preventDefault();
            }, {once: true})"""
        )
        page.keyboard.press("Meta+c" if sys.platform == "darwin" else "Control+c")
        assert page.evaluate("window.contactCopiedText") == selected
        expect(page).to_have_url(origin)
        page.screenshot(path=tmp_path / f"statement-selection-{width}.png")
        page.locator("h2[data-contact-analysis-name]").click()
        assert page.evaluate("window.getSelection().isCollapsed")
        statement.click()
        _expect_original(page, message_id)
        assert len(context.pages) == 1
        assert fixture.before == (
            fixture.history.read_bytes(),
            fixture.registry.read_bytes(),
        )
    finally:
        context.close()


def test_statement_keyboard_and_modified_click_keep_native_link_navigation(
    disposable_browser, contact_analysis_server
):
    fixture = contact_analysis_server
    context = disposable_browser.new_context(reduced_motion="reduce")
    context.set_default_timeout(7000)
    context.set_default_navigation_timeout(15000)
    page = context.new_page()
    try:
        origin = _open_contact(page, fixture)
        row = page.locator("tr[data-contact-statement-id]").first
        message_id = row.get_attribute("data-contact-message-id")
        statement = row.locator("a[data-wechat-contact-statement]")
        statement.focus()
        statement.press("Enter")
        _expect_original(page, message_id)
        page.go_back(wait_until="domcontentloaded")
        expect(page).to_have_url(origin)
        with context.expect_page() as opened:
            page.locator("a[data-wechat-contact-statement]").first.click(
                modifiers=["Meta" if sys.platform == "darwin" else "Control"]
            )
        other_page = opened.value
        _expect_original(other_page, message_id)
        expect(page).to_have_url(origin)
        other_page.close()
    finally:
        context.close()


@pytest.mark.parametrize("width", [1026, 390])
def test_statement_and_compact_source_disclosure_are_independent(
    disposable_browser, duplicate_contact_analysis_server, tmp_path, width
):
    fixture = duplicate_contact_analysis_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 900}, reduced_motion="reduce"
    )
    context.set_default_timeout(7000)
    context.set_default_navigation_timeout(15000)
    page = context.new_page()
    try:
        origin = _open_contact(page, fixture, fixture.shared_text)
        row = page.locator("tr[data-contact-statement-id]")
        expect(row).to_have_count(1)
        statement = row.locator("a[data-wechat-contact-statement]")
        expect(statement).to_have_text(fixture.shared_text)
        sources = row.locator("details[data-wechat-contact-sources]")
        summary = sources.locator("summary")
        expect(summary).to_have_attribute("aria-label", "Show original sources")
        expect(summary).to_have_text("")
        assert not sources.evaluate("node => node.open")
        trigger_box = summary.bounding_box()
        statement_box = statement.bounding_box()
        assert abs(trigger_box["y"] - statement_box["y"]) < 2
        assert trigger_box["height"] <= 24
        summary.focus()
        summary.press("Enter")
        expect(sources).to_have_attribute("open", "")
        expect(page).to_have_url(origin)
        expect(sources.locator("[data-wechat-contact-observation]")).to_have_count(2)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=tmp_path / f"statement-compact-sources-{width}.png")
        source = sources.locator("[data-wechat-contact-observation]").first
        source_id = source.get_attribute("data-wechat-contact-observation")
        source.locator("a[data-wechat-contact-source]").click()
        _expect_original(page, source_id)
    finally:
        context.close()
