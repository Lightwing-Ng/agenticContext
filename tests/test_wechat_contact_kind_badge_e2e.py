"""Contact tables reuse the canonical conversation-kind badge material.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from copy import deepcopy
import json
from unittest.mock import patch

import pytest
from playwright.sync_api import expect

from app.core.wechat_contact_analysis import query_contact_analysis
from tests import test_wechat_contact_analysis_e2e as fixtures


disposable_browser = fixtures.disposable_browser
contact_analysis_server = fixtures.contact_analysis_server
KIND_LABELS = {
    "group": "Group chat",
    "direct": "Private chat",
    "unclassified": "Unclassified chat",
}


def _badge_state(badge):
    return badge.evaluate(
        """node => {
        const style = getComputedStyle(node);
        const box = node.getBoundingClientRect();
        const range = document.createRange();
        range.selectNodeContents(node);
        const textBox = range.getBoundingClientRect();
        const rects = [...range.getClientRects()].filter(rect => rect.width && rect.height);
        const properties = ['display', 'alignItems', 'backgroundColor', 'color',
            'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft',
            'borderTopLeftRadius', 'borderTopRightRadius',
            'borderBottomRightRadius', 'borderBottomLeftRadius',
            'fontFamily', 'fontSize', 'fontWeight', 'lineHeight', 'verticalAlign'];
        return {text: node.textContent.trim(),
            material: Object.fromEntries(properties.map(property => [property, style[property]])),
            whiteSpace: style.whiteSpace,
            bounds: {left: box.left, right: box.right, width: box.width, top: box.top, height: box.height},
            textFits: textBox.left >= box.left - 1 && textBox.right <= box.right + 1,
            lineCount: new Set(rects.map(rect => Math.round(rect.top * 100) / 100)).size,
            contentOverflow: node.scrollWidth - node.clientWidth};
    }"""
    )


def _assert_shared_badge_loaded(page):
    loaded = page.evaluate(
        """() => [...document.styleSheets].filter(sheet => sheet.href
            && new URL(sheet.href).pathname === '/static/wechat-kind-badge.css')
            .map(sheet => ({href: sheet.href, ruleCount: sheet.cssRules.length}))"""
    )
    assert len(loaded) == 1 and loaded[0]["ruleCount"] > 0, loaded
    return loaded[0]["href"]


def _contact_badge_results(fixture):
    result = query_contact_analysis(fixture.store, fixture.target_id, background=False)
    assert result["status"] == "ready"
    result = deepcopy(result)
    result.update(
        items=result["items"][:3],
        total_count=3,
        conversation_count=3,
        total_pages=1,
        current_page=1,
    )
    for item, kind in zip(result["items"], KIND_LABELS):
        item.update(
            kind=kind,
            content_text=f"Statement for {kind}",
            conversation_title=f"Research {kind}",
            viewer_account_label="Source account A",
            source_label="iPhone archive",
            timestamp_label="5 Apr 2019 19:40:17",
        )
    return result


@pytest.mark.parametrize("width", [823, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_contact_kind_badges_match_the_canonical_material_and_fit_their_column(
    disposable_browser, contact_analysis_server, tmp_path, width, color_scheme
):
    fixture = contact_analysis_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1000},
        reduced_motion="reduce",
        color_scheme=color_scheme,
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    report = {
        "width": width,
        "color_scheme": color_scheme,
        "canonical": {},
        "contact": {},
    }
    try:
        page.goto(
            fixture.url
            + "/browser?view=text&source=wechat&chat_kind=all&session_view=1",
            wait_until="networkidle",
        )
        page.evaluate("() => document.fonts.ready")
        report["canonical_asset"] = _assert_shared_badge_loaded(page)
        for kind, label in KIND_LABELS.items():
            badge = (
                page.locator(f'tr[data-wechat-conversation-kind="{kind}"]')
                .get_by_text(label, exact=True)
                .first
            )
            expect(badge).to_be_visible()
            state = _badge_state(badge)
            assert state["whiteSpace"] == "nowrap" and state["lineCount"] == 1, state
            assert state["material"]["display"] == "inline-flex", state
            assert state["material"]["paddingTop"] == "2px", state
            assert state["material"]["paddingLeft"] == "6px", state
            assert state["material"]["borderTopLeftRadius"] == "2px", state
            assert state["material"]["backgroundColor"] != "rgba(0, 0, 0, 0)", state
            assert state["material"]["backgroundColor"] != state["material"]["color"], (
                state
            )
            report["canonical"][kind] = state

        with patch(
            "app.core.wechat_contact_analysis.query_contact_analysis",
            return_value=_contact_badge_results(fixture),
        ):
            page.goto(
                fixture.url + "/browser/wechat/contacts/" + fixture.target_id,
                wait_until="networkidle",
            )
            page.evaluate("() => document.fonts.ready")
            report["contact_asset"] = _assert_shared_badge_loaded(page)
            assert report["contact_asset"] == report["canonical_asset"]
            rows = page.locator("[data-table-body] tbody tr")
            expect(rows).to_have_count(3)
            for index, (kind, label) in enumerate(KIND_LABELS.items()):
                row = rows.nth(index)
                conversation = row.locator("td").nth(3)
                badge = conversation.get_by_text(label, exact=True)
                expect(badge).to_be_visible()
                state = _badge_state(badge)
                state["columns"] = row.evaluate(
                    """row => {
                    const conversation = row.cells[3].getBoundingClientRect();
                    const account = row.cells[4].getBoundingClientRect();
                    return {conversationLeft: conversation.left,
                        conversationRight: conversation.right, accountLeft: account.left};
                }"""
                )
                report["contact"][kind] = state
                assert state["material"] == report["canonical"][kind]["material"], (
                    report
                )
                assert state["whiteSpace"] == "normal", state
                assert state["textFits"] and state["contentOverflow"] <= 1, state
                assert (
                    state["bounds"]["left"] >= state["columns"]["conversationLeft"] - 1
                ), state
                assert (
                    state["bounds"]["right"]
                    <= state["columns"]["conversationRight"] + 1
                ), state
                assert (
                    state["bounds"]["right"] <= state["columns"]["accountLeft"] + 1
                ), state
                if width == 390 and kind == "unclassified":
                    assert state["lineCount"] > 1, state
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            port = page.locator("[data-table-scroll]")
            assert port.evaluate("node => node.scrollWidth <= node.clientWidth")
            bounds = page.locator(".wechat-contact-table-shell").bounding_box()
            page.screenshot(
                path=tmp_path / f"contact-kind-badges-{width}-{color_scheme}.png",
                clip={
                    "x": bounds["x"],
                    "y": bounds["y"],
                    "width": bounds["width"],
                    "height": min(300, bounds["height"]),
                },
            )
        assert not errors
        assert fixture.before == (
            fixture.history.read_bytes(),
            fixture.registry.read_bytes(),
        )
        (tmp_path / "badge-comparison.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )
    finally:
        context.close()
