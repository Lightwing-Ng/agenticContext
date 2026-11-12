"""Timestamp rails choose complete containment or complete separation.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

import json
from urllib.parse import urlencode

import pytest
from playwright.sync_api import expect

from tests import test_wechat_forwarded_e2e as forwarding
from tests import test_wechat_quoted_reply_e2e as quoting
from tests import test_wechat_sender_runs_e2e as senders
from tests.test_wechat_disclosure_e2e import (
    _capture_paused_motions,
    _finish_motions,
    _sample_motion,
)


disposable_browser = senders.disposable_browser
sender_run_server = senders.sender_run_server
forwarded_server = forwarding.forwarded_server
quoted_reply_server = quoting.quoted_reply_server


_GEOMETRY = """() => {
    const rect = node => node.getBoundingClientRect();
    const intersects = (a,b) => Math.min(a.right,b.right)-Math.max(a.left,b.left)>0.5
        && Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>0.5;
    const samples = [...document.querySelectorAll('.wechat-message-row')].flatMap(row => {
        const card = row.querySelector(':scope > .wechat-message');
        const time = row.querySelector(':scope > time.wechat-message-time');
        if (!card || !time || row.closest('details:not([open])')
            || !row.getClientRects().length || !rect(card).width) return [];
        const box = rect(card), clock = rect(time), style = getComputedStyle(card);
        const leftInset = parseFloat(style.paddingLeft)+parseFloat(style.borderLeftWidth);
        const rightInset = parseFloat(style.paddingRight)+parseFloat(style.borderRightWidth);
        const inside = clock.left >= box.left+leftInset-1.1
            && clock.right <= box.right-rightInset+1.1
            && clock.top >= box.top-1.1 && clock.bottom <= box.bottom+1.1;
        const gap = clock.left-box.right;
        const source = card.querySelector(':scope > .wechat-provenance > summary');
        const sourceBox = source ? rect(source) : null;
        const collisions = [];
        card.querySelectorAll(':scope > .wechat-message-text, :scope > .wechat-message-quote > p, '
            + ':scope > .wechat-resource, :scope > .wechat-provenance[open] > .ui-collapse-body, '
            + ':scope > .wechat-forwarded > summary').forEach(content => {
            const boxes = [];
            if (content.matches('.wechat-resource, summary')) boxes.push(rect(content));
            else {
                // Empty DL padding is not painted content or an active source target.
                const texts = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
                while (texts.nextNode()) {
                    if (!texts.currentNode.textContent.trim()) continue;
                    const range = document.createRange();range.selectNodeContents(texts.currentNode);
                    boxes.push(...range.getClientRects());
                }
            }
            for (const contentBox of boxes) {
                if (!contentBox.width || !contentBox.height) continue;
                const record = kind => ({kind,content:content.className,
                    top:contentBox.top,bottom:contentBox.bottom,
                    left:contentBox.left,right:contentBox.right});
                if (intersects(contentBox, clock)) collisions.push(record('clock'));
                if (sourceBox && intersects(contentBox, sourceBox)) collisions.push(record('source'));
            }
        });
        let root = row, outer = root.parentElement?.closest('.wechat-message-row');
        while (outer) {root=outer;outer=root.parentElement?.closest('.wechat-message-row');}
        return [{id:card.id, direction:card.dataset.wechatDirection,
            text:card.querySelector(':scope > .wechat-message-text')?.textContent.slice(0,50),
            contained:card.classList.contains('wechat-message--time-contained'),
            inside,gap,right:clock.right,cardRight:box.right,outerRight:rect(root).right,
            top:box.top,timeTop:clock.top,sourceTop:sourceBox?.top,
            resource: (()=>{const el=card.querySelector(':scope > .wechat-resource');
                if(!el)return null;const r=rect(el);return {top:r.top,bottom:r.bottom,
                    left:r.left,right:r.right,height:r.height,
                    beforeHeight:getComputedStyle(card,'::before').height,
                    beforeBlock:getComputedStyle(card,'::before').blockSize,
                    beforeDisplay:getComputedStyle(card,'::before').display,
                    beforeMin:getComputedStyle(card,'::before').minHeight,
                    beforeMax:getComputedStyle(card,'::before').maxHeight,
                    beforePosition:getComputedStyle(card,'::before').position,
                    beforeContent:getComputedStyle(card,'::before').content,
                    cardClearance:style.getPropertyValue('--wechat-run-time-clearance'),
                    rowClearance:getComputedStyle(row).getPropertyValue('--wechat-run-time-clearance'),
                    classes:card.className,
                    display:style.display,cardHeight:box.height,
                    gridRows:style.gridTemplateRows,gridAuto:style.gridAutoRows,
                    style:card.getAttribute('style')};})(),
            source:sourceBox?{top:sourceBox.top,bottom:sourceBox.bottom,height:sourceBox.height}:null,
            clearance:row.style.getPropertyValue('--wechat-run-time-clearance'),
            collisions,timeCount:row.querySelectorAll(':scope > time').length}];
    });
    const port = document.querySelector('[data-chat-scrollport]');
    return {samples,documentOverflow:document.documentElement.scrollWidth-innerWidth,
        portOverflow:port.scrollWidth-port.clientWidth};
}"""


def _snapshot(page):
    return page.locator(".wechat-message-row").evaluate_all("""rows => rows.map(row => {
        const card = row.querySelector(':scope > .wechat-message');
        const time = row.querySelector(':scope > time');
        return {id:card.id,text:card.querySelector(':scope > .wechat-message-text')?.textContent,
            clock:time?.textContent,datetime:time?.getAttribute('datetime')};
    })""")


def _open(page, fixture, *, source="wechat", account=""):
    response = page.goto(
        fixture.url
        + "/browser?"
        + urlencode(
            {
                "view": "text",
                "source": source,
                "account": account,
                "session": fixture.session,
            }
        ),
        wait_until="domcontentloaded",
    )
    assert response.status == 200
    expect(page.locator(".wechat-message-row").first).to_be_attached()
    toggle = page.locator("#sidebar_toggle")
    if toggle.get_attribute("aria-expanded") == "true":
        toggle.click()
    page.evaluate("() => document.fonts.ready")
    _settle(page)


def _settle(page):
    page.evaluate("""async () => {
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    }""")


def _assert_layout(page):
    page.wait_for_function(
        """() => [...document.querySelectorAll('.wechat-message-row')]
            .filter(row => !row.closest('details:not([open])'))
            .every(row => row.style.getPropertyValue('--wechat-time-top'))"""
    )
    _settle(page)
    geometry = page.evaluate(_GEOMETRY)
    samples = geometry["samples"]
    assert samples
    assert geometry["documentOverflow"] <= 1, geometry
    assert geometry["portOverflow"] <= 1, geometry
    assert (
        max(row["right"] for row in samples) - min(row["right"] for row in samples)
        <= 1.1
    ), geometry
    for row in samples:
        assert row["inside"] or row["gap"] >= 11.9, row
        assert row["cardRight"] <= row["outerRight"] + 1.1, row
        assert row["timeCount"] == 1, row
        if row["collisions"]:
            print(
                "CLOCK_COLLISION", json.dumps({"viewport": page.viewport_size, **row})
            )
        assert not row["collisions"], row
        if row["contained"]:
            assert row["inside"], row
    return geometry


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_short_received_bubbles_snap_inside_and_release_on_widening(
    disposable_browser, sender_run_server, theme
):
    fixture = sender_run_server
    context = disposable_browser.new_context(
        viewport={"width": 1280, "height": 1109},
        color_scheme=theme,
        reduced_motion="reduce",
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        _open(page, fixture, account="account:" + senders.OWNER)
        original = _snapshot(page)
        wide = _assert_layout(page)
        first = wide["samples"][0]
        assert first["direction"] == "received"
        assert not first["contained"] and first["gap"] >= 26, first
        compact = page.locator(".wechat-message").nth(1)
        closed_height = compact.bounding_box()["height"]
        page.set_viewport_size({"width": 390, "height": 1109})
        narrow = _assert_layout(page)
        assert narrow["samples"][0]["contained"], narrow
        assert narrow["samples"][0]["inside"], narrow
        page.set_viewport_size({"width": 1280, "height": 1109})
        released = _assert_layout(page)
        assert not released["samples"][0]["contained"], released
        assert compact.bounding_box()["height"] == pytest.approx(closed_height, abs=1)
        assert _snapshot(page) == original
        assert fixture.history.read_bytes() == fixture.original
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("motion", ["no-preference", "reduce"])
def test_threshold_sweep_has_only_complete_states_and_no_observer_loop(
    disposable_browser, sender_run_server, motion
):
    fixture = sender_run_server
    context = disposable_browser.new_context(
        viewport={"width": 1000, "height": 1109}, reduced_motion=motion
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "console",
        lambda message: (
            errors.append(message.text)
            if "ResizeObserver loop" in message.text
            else None
        ),
    )
    try:
        _open(page, fixture, account="account:" + senders.OWNER)
        original = _snapshot(page)
        widths = [1000, 803, 767, 625, 560, 520, 490, 470, 450, 430, 410, 390]
        for width in [*widths, *reversed(widths)]:
            page.set_viewport_size({"width": width, "height": 1109})
            _assert_layout(page)
        assert _snapshot(page) == original
        assert not errors
        assert fixture.history.read_bytes() == fixture.original
    finally:
        context.close()


@pytest.mark.parametrize("motion", ["no-preference", "reduce"])
def test_provenance_motion_and_reversal_never_cross_the_clock(
    disposable_browser, sender_run_server, motion
):
    fixture = sender_run_server
    context = disposable_browser.new_context(
        viewport={"width": 767, "height": 1109}, reduced_motion=motion
    )
    page = context.new_page()
    try:
        _open(page, fixture, account="account:" + senders.OWNER)
        original = _snapshot(page)
        card = page.locator(".wechat-message").first
        summary = card.locator(":scope > .wechat-provenance > summary")
        details = card.locator(":scope > .wechat-provenance")
        if motion == "no-preference":
            _capture_paused_motions(card)
        summary.press("Enter")
        expect(details).to_have_attribute("open", "")
        if motion == "no-preference":
            page.wait_for_function("window.wechatDisclosureMotions.length > 0")
            for fraction in (0.0, 0.15, 0.55):
                _sample_motion(card, fraction)
                _assert_layout(page)
            summary.press("Space")
            for fraction in (0.0, 0.3, 1.0):
                _sample_motion(card, fraction)
                _assert_layout(page)
            _finish_motions(card)
        else:
            _assert_layout(page)
            summary.press("Space")
        expect(details).not_to_have_attribute("open", "")
        _assert_layout(page)
        assert _snapshot(page) == original
        assert fixture.history.read_bytes() == fixture.original
    finally:
        context.close()


@pytest.mark.parametrize("width", [767, 390])
def test_quoted_messages_keep_literal_content_and_a_clear_clock(
    disposable_browser, quoted_reply_server, width
):
    fixture = quoted_reply_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1109}, reduced_motion="reduce"
    )
    page = context.new_page()
    try:
        _open(page, fixture, account="source:xlsx-2")
        expect(page.locator(".wechat-message-quote")).to_have_count(2)
        original = _snapshot(page)
        _assert_layout(page)
        page.set_viewport_size({"width": 390 if width == 767 else 767, "height": 1109})
        _assert_layout(page)
        assert _snapshot(page) == original
        assert fixture.history.read_bytes() == fixture.preserved_history
    finally:
        context.close()


@pytest.mark.parametrize("width", [767, 390])
def test_nested_sent_and_received_clocks_share_the_outer_rail_without_crossing(
    disposable_browser, forwarded_server, width
):
    fixture = forwarded_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1109}, reduced_motion="reduce"
    )
    page = context.new_page()
    try:
        _open(page, fixture, account="account:" + fixture.owner)
        original = _snapshot(page)
        parent = page.locator("#wechat_message_" + fixture.parent_id)
        forwarded = parent.locator(":scope > .wechat-forwarded")
        forwarded.locator(":scope > summary").press("Enter")
        expect(forwarded).to_have_attribute("open", "")
        geometry = _assert_layout(page)
        assert {row["direction"] for row in geometry["samples"]} >= {
            "sent",
            "received",
        }
        assert len(geometry["samples"]) > 3
        child = forwarded.locator(".wechat-message").last
        child.locator(":scope > .wechat-provenance > summary").press("Enter")
        _assert_layout(page)
        child.locator(":scope > .wechat-provenance > summary").press("Space")
        _assert_layout(page)
        forwarded.locator(":scope > summary").press("Space")
        expect(forwarded).not_to_have_attribute("open", "")
        _assert_layout(page)
        assert _snapshot(page) == original
        assert fixture.history.read_bytes() == fixture.preserved
    finally:
        context.close()
