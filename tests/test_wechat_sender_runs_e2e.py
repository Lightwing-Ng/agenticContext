"""Consecutive native senders retain clocks and actions while reclaiming headers.

Code version: v1.2.0-codex.0
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from app.core.config import CrawlConfig
from app.core.wechat_archive import (
    _time_fields,
    import_wechat_archive,
    wechat_archive_path,
)
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from tests import test_sidebar_e2e
from tests.test_wechat_archive import make_archive
from tests.test_wechat_native_system_notices_e2e import _assert_bubble_effects


disposable_browser = test_sidebar_e2e.disposable_browser
OWNER = "wxid_account_a"
SENDER = "wxid_peer"
LONG_TEXT = (
    "A longer consecutive message with 中文 and enough text to wrap safely. " * 12
)
TEXTS = (
    "One.",
    "Two.",
    "[Image]",
    "Four.",
    "A reply.",
    LONG_TEXT,
    "Last reply.",
    "Next sender.",
)
EXPECTED_NAMES = [True, False, False, False, True, False, False, True]


@pytest.fixture(scope="module")
def sender_run_server(tmp_path_factory):
    sandbox = tmp_path_factory.mktemp("wechat-sender-runs")
    store = sandbox / "store"
    import_wechat_archive(make_archive(sandbox / "archive", repeated=8), store)
    history = wechat_archive_path(store)
    clocks = []
    with sqlite3.connect(history) as connection:
        session, peer = connection.execute(
            "SELECT id,peer_id FROM conversations WHERE account_key=? AND kind='direct'",
            ("account:" + OWNER,),
        ).fetchone()
        rows = connection.execute(
            "SELECT id,provenance FROM messages WHERE conversation_id=? ORDER BY ordinal",
            (session,),
        ).fetchall()
        assert len(rows) == 8
        start = datetime(2016, 2, 27, 20, 6, 45, tzinfo=timezone(timedelta(hours=8)))
        for index, ((identifier, encoded), text) in enumerate(
            zip(rows, TEXTS, strict=True)
        ):
            # The same outgoing sender remains adjacent across a three-day gap.
            stamp = start + timedelta(seconds=index, days=3 if index >= 5 else 0)
            fields = _time_fields({"sent_at_utc_iso": stamp.isoformat()})
            clocks.append(fields)
            sent = index in {4, 5, 6}
            provenance = json.loads(encoded)
            provenance.update(
                unix_timestamp=int(stamp.timestamp()),
                server_id=str(1_000_000 + index),
                message_type=3 if index == 2 else 1,
            )
            if index == 2:
                provenance["resource_reference"] = {
                    "kind": "image",
                    "filename": "",
                    "account_id": OWNER,
                    "peer_id": peer,
                    "unix_timestamp": int(stamp.timestamp()),
                }
            connection.execute(
                "UPDATE messages SET text=?,search_text=?,sender_id=?,sender_label=?,direction=?,"
                "time_sort=?,timestamp_value=?,timestamp_label=?,time_basis=?,provenance=? WHERE id=?",
                (
                    text,
                    text.casefold(),
                    OWNER if sent else SENDER,
                    "Contact Me" if sent else "Contact A",
                    "sent" if sent else "received",
                    *fields,
                    json.dumps(provenance),
                    identifier,
                ),
            )
        connection.execute(
            "UPDATE conversations SET first_sort=?,last_sort=?,first_time=?,last_time=?,latest_text=? WHERE id=?",
            (
                clocks[0][0],
                clocks[-1][0],
                clocks[0][1],
                clocks[-1][1],
                TEXTS[-1],
                session,
            ),
        )
    original = history.read_bytes()

    class Resolver:
        def __init__(self, _root):
            pass

        def account_mapping(self, _owner):
            return {"status": "mapped", "root_count": 1, "evidence": ["synthetic"]}

        def resolve(self, _reference):
            return {
                "status": "folder",
                "path": sandbox,
                "label": "Open local WeChat folder",
                "evidence": "synthetic",
            }

    with (
        patch("app.core.wechat_history_browser.WechatMacResourceResolver", Resolver),
        patch("app.web.local_resource_routes.reveal_media_path") as reveal,
        patch("app.web.local_resource_routes.open_directory_path") as open_folder,
        patch(
            "app.web.app.SavedConfigStore.load",
            return_value=SavedConfigStore(CrawlConfig()),
        ),
    ):
        app = create_app(
            store,
            computer_use_settings_path=sandbox / "agent.json",
            computer_use_runtime_root=sandbox / "runtime",
            agent_external_operations_enabled=False,
        )
        app.config.update(TESTING=True)
        server = make_server("127.0.0.1", 0, app, threaded=True)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield SimpleNamespace(
                url=f"http://127.0.0.1:{server.server_port}",
                session=session,
                identifiers=[row[0] for row in rows],
                clocks=clocks,
                history=history,
                original=original,
                reveal=reveal,
                open_folder=open_folder,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def _run_geometry(page):
    return page.locator(".wechat-message-row").evaluate_all("""rows => {
        const rectangle = node => node.getBoundingClientRect();
        const intersects = (a, b) => Math.min(a.right,b.right)-Math.max(a.left,b.left) > 0.5
            && Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top) > 0.5;
        return rows.map(row => {
            const card = row.querySelector('.wechat-message');
            const time = row.querySelector(':scope > time');
            const header = card.querySelector('.wechat-message-header');
            const summary = card.querySelector('summary');
            const body = card.querySelector('.wechat-message-text, .wechat-resource');
            const range = document.createRange();range.selectNodeContents(body);
            const rects = body.matches('.wechat-resource') ? [rectangle(body)] : [...range.getClientRects()];
            const style = getComputedStyle(header), box = rectangle(header);
            const primaryContent = card.querySelector('.wechat-message-text, .wechat-resource-label');
            return {show:card.dataset.wechatShowSender, direction:card.dataset.wechatDirection,
                headerWidth:box.width, headerHeight:box.height,
                headerClip:style.clipPath, cardHeight:rectangle(card).height,
                speakerFont:getComputedStyle(card.querySelector('.wechat-speaker')).fontSize,
                contentFont:getComputedStyle(primaryContent).fontSize,
                clockFont:getComputedStyle(time).fontSize,
                slotFont:getComputedStyle(card.querySelector('.wechat-time-slot')).fontSize,
                resourceStatusFont:card.querySelector('.wechat-resource-status')
                    ? getComputedStyle(card.querySelector('.wechat-resource-status')).fontSize : null,
                timeCount:row.querySelectorAll('time').length, timeRight:rectangle(time).right,
                timeInsideRow:rectangle(time).top >= rectangle(row).top-0.5
                    && rectangle(time).bottom <= rectangle(row).bottom+0.5,
                contentOverlapsTime:rects.some(rect => intersects(rect,rectangle(time))),
                contentOverlapsSource:card.dataset.wechatShowSender === 'false'
                    && rects.some(rect => intersects(rect,rectangle(summary))),
                summaryHeight:rectangle(summary).height,
                firstContentTop:Math.min(...rects.filter(rect => rect.width > 0).map(rect => rect.top)),
                timeBottom:rectangle(time).bottom,
                clearance:parseFloat(row.style.getPropertyValue('--wechat-run-time-clearance')) || 0,
                closedDetailsHeight:rectangle(card.querySelector('details')).height};
        });
    }""")


@pytest.mark.parametrize("width", [831, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_sender_runs_keep_only_the_first_name_and_preserve_clocks_and_actions(
    disposable_browser, sender_run_server, tmp_path, width, color_scheme
):
    fixture = sender_run_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1109},
        color_scheme=color_scheme,
        reduced_motion="reduce",
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    requests = []

    def finder_response(route):
        requests.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {"revealed": True, "target_kind": "folder", "file_manager": "Finder"}
            ),
        )

    page.route("**/api/browser/wechat/resources/*/reveal", finder_response)
    try:
        response = page.goto(
            fixture.url
            + "/browser?"
            + urlencode(
                {
                    "view": "text",
                    "source": "wechat",
                    "account": "account:" + OWNER,
                    "session": fixture.session,
                }
            ),
            wait_until="networkidle",
        )
        assert response.status == 200
        toggle = page.locator("#sidebar_toggle")
        if toggle.get_attribute("aria-expanded") == "true":
            toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "false")
        page.evaluate("() => document.fonts.ready")
        cards = page.locator(".wechat-message")
        expect(cards).to_have_count(8)
        assert (
            cards.evaluate_all(
                "cards => cards.map(card => card.dataset.wechatShowSender === 'true')"
            )
            == EXPECTED_NAMES
        )
        assert fixture.clocks[5][0] - fixture.clocks[4][0] > 3 * 86400
        expect(cards.nth(5)).to_have_attribute("data-wechat-show-sender", "false")
        for index, identifier in enumerate(fixture.identifiers):
            card = page.locator(f"#wechat_message_{identifier}")
            expect(card).to_have_accessible_name(
                "Contact Me" if index in {4, 5, 6} else "Contact A"
            )
            clock = page.locator(f"#wechat_time_{identifier}")
            expect(clock).to_have_text(fixture.clocks[index][2])
            expect(clock).to_have_attribute("datetime", fixture.clocks[index][1])
        assert page.locator(".wechat-message-row time").count() == 8
        geometry = _run_geometry(page)
        assert (
            max(item["timeRight"] for item in geometry)
            - min(item["timeRight"] for item in geometry)
            <= 1
        )
        for item, show in zip(geometry, EXPECTED_NAMES, strict=True):
            assert item["speakerFont"] == "17px" and item["contentFont"] == "15px", item
            assert item["clockFont"] == item["slotFont"] == "12.8px", item
            assert item["resourceStatusFont"] in {None, "12.8px"}, item
            assert item["timeCount"] == 1 and item["timeInsideRow"], item
            assert (
                not item["contentOverlapsTime"] and not item["contentOverlapsSource"]
            ), item
            if not show:
                assert item["headerWidth"] <= 1.1 and item["headerHeight"] <= 1.1, item
                assert (
                    item["headerClip"] != "none" and item["closedDetailsHeight"] <= 0.5
                ), item
                assert item["summaryHeight"] >= 23.5, item
                if item["direction"] == "sent" and item["clearance"] > 0:
                    glyph_gap = item["firstContentTop"] - item["timeBottom"]
                    assert 4 <= glyph_gap <= 8, item
        assert geometry[5]["clearance"] > 0, geometry[5]
        if width == 831:
            assert geometry[1]["cardHeight"] < geometry[0]["cardHeight"] - 20, geometry
        assert page.evaluate("document.documentElement.scrollWidth-innerWidth") <= 1
        assert (
            page.locator("[data-chat-scrollport]").evaluate(
                "node => node.scrollWidth-node.clientWidth"
            )
            <= 1
        )

        summary = cards.nth(1).locator("summary")
        details = cards.nth(1).locator("details")
        summary.focus()
        expect(summary).to_be_focused()
        summary.press("Enter")
        expect(details).to_have_attribute("open", "")
        expect(details.locator(".ui-collapse-body")).to_be_visible()
        expect(details.get_by_text("Contact ID", exact=True)).to_be_visible()
        summary.press("Space")
        expect(details).not_to_have_attribute("open", "")
        assert _run_geometry(page)[1]["closedDetailsHeight"] <= 0.5
        try:
            _assert_bubble_effects(page)
        except Exception:
            diagnostics = page.locator(".browser-chat-pane").evaluate("""pane => {
                const rect = node => {
                    if (!node) return null;
                    const r = node.getBoundingClientRect();
                    return {left:r.left,top:r.top,width:r.width,height:r.height,bottom:r.bottom};
                };
                const list = pane.querySelector('[data-chat-scrollport]');
                return {list:rect(list),scrollTop:list.scrollTop,cards:[...list.querySelectorAll('.browser-chat-message')].map(card => {
                    const effect = pane.querySelector(`[data-chat-effect-for="${card.id}"]`);
                    return {id:card.id,card:rect(card),effect:rect(effect),hidden:effect?.hidden,display:effect ? getComputedStyle(effect).display : null};
                })};
            }""")
            (tmp_path / "shadow-diagnostics.json").write_text(
                json.dumps(diagnostics, indent=2)
            )
            page.screenshot(path=tmp_path / "shadow-failure.png")
            raise

        resource = cards.nth(2).locator("[data-wechat-resource-reveal]")
        resource.click()
        expect(resource.locator("[data-wechat-resource-feedback]")).to_have_text(
            "Folder shown in Finder"
        )
        assert len(requests) == 1
        expect(cards.nth(2).locator("details")).not_to_have_attribute("open", "")
        fixture.reveal.assert_not_called()
        fixture.open_folder.assert_not_called()
        assert fixture.history.read_bytes() == fixture.original
        assert not errors
        cards.first.scroll_into_view_if_needed()
        page.screenshot(path=tmp_path / f"sender-runs-{width}-{color_scheme}.png")
    finally:
        context.close()
