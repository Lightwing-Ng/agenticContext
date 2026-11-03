"""Known system clocks keep their rail; untimed HTML notices stay compact.

Code version: v1.1.2-codex.0
"""

from __future__ import annotations

import json
import sqlite3
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from app.core.chat_history_browser import query_chat_history
from app.core.config import CrawlConfig
from app.core.wechat_archive import import_wechat_archive, wechat_archive_path
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from tests import test_sidebar_e2e
from tests.test_wechat_archive import make_archive
from tests.test_wechat_system_notices_e2e import (
    _assert_csv_source_rows,
    _notice_geometry,
    _scoped_export_urls,
)


disposable_browser = test_sidebar_e2e.disposable_browser
RAW_TEXTS = (
    "A regular received message.",
    "System Message: You've added Synthetic Contact${wxid_contact@textstatusicon}. "
    "Greetings are shown above.",
    "System Message: A human with the display name System still has a chat bubble: "
    "${wxid_human@textstatusicon}.",
    '"Synthetic Contact" recalled a message. '
    + "A long synthetic notice with 中文 wrapping text. " * 5
    + "<script>window.nativeNoticeExecuted = true</script> & preserved text.",
    "Another regular received message.",
    "A regular sent message.",
)
SYSTEM_INDICES = (1, 3)
SYSTEM_DISPLAY_TEXTS = {
    1: "You've added Synthetic Contact. Greetings are shown above.",
    3: RAW_TEXTS[3],
}
UNTIMED_DISPLAY_TEXTS = (
    "Synthetic contact tickled me.",
    "Another synthetic contact tickled me. "
    + "A wrapped system notice preserves 中文 content. " * 5
    + "End.",
)
UNTIMED_TEXTS = tuple("System Message: " + text for text in UNTIMED_DISPLAY_TEXTS)


@pytest.fixture(scope="module")
def native_notice_server(tmp_path_factory):
    sandbox = tmp_path_factory.mktemp("wechat-native-system-notices")
    archive = make_archive(sandbox / "archive", repeated=6)
    store = sandbox / "store"
    import_wechat_archive(archive, store)
    history = wechat_archive_path(store)
    with sqlite3.connect(history) as connection:
        session = connection.execute(
            "SELECT id FROM conversations WHERE account_key='account:wxid_account_a' "
            "AND kind='direct'"
        ).fetchone()[0]
        rows = connection.execute(
            "SELECT id,provenance FROM messages WHERE conversation_id=? ORDER BY ordinal",
            (session,),
        ).fetchall()
        assert len(rows) == len(RAW_TEXTS)
        identifiers = [row[0] for row in rows]
        for index, ((identifier, encoded), text) in enumerate(
            zip(rows, RAW_TEXTS, strict=True)
        ):
            provenance = json.loads(encoded)
            system = index in SYSTEM_INDICES
            if system:
                provenance.update(
                    text_extraction_kind="system_text", message_type=10000
                )
            connection.execute(
                "UPDATE messages SET text=?,search_text=?,provenance=?,"
                "quality_flags=?,timestamp_value=?,timestamp_label=?,time_sort=? "
                "WHERE id=?",
                (
                    text,
                    text.casefold(),
                    json.dumps(provenance),
                    json.dumps(["system_notice"] if system or index == 2 else []),
                    f"2026-05-11T14:23:{index + 12:02d}+08:00",
                    f"11 May 2026 14:23:{index + 12:02d}",
                    1778480592 + index,
                    identifier,
                ),
            )
            if system:
                connection.execute(
                    "UPDATE messages SET sender_id=NULL,sender_label='System',"
                    "direction='unknown' WHERE id=?",
                    (identifier,),
                )
        connection.execute(
            "UPDATE messages SET sender_label='System' WHERE id=?",
            (identifiers[2],),
        )
        html_session = connection.execute(
            "SELECT id FROM conversations WHERE partition_id='xlsx-3'"
        ).fetchone()[0]
        html_identifiers = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM messages WHERE conversation_id=? ORDER BY ordinal LIMIT 2",
                (html_session,),
            )
        ]
        connection.execute(
            "UPDATE partitions SET source_label='HTML export',source_file='synthetic-untimed.html' WHERE id='xlsx-3'"
        )
        for index, (identifier, text) in enumerate(
            zip(html_identifiers, UNTIMED_TEXTS, strict=True)
        ):
            provenance = {
                "text_extraction_kind": "system_text",
                "source_time": "",
                "export_msgid_namespace": "MesLocalID",
                "locator": {
                    "export_msgtype": "",
                    "export_msgid_namespace": "MesLocalID",
                    "export_msgid": str(5910 + index),
                },
            }
            connection.execute(
                "UPDATE messages SET text=?,search_text=?,provenance=?,sender_id=NULL,"
                "sender_label='System',direction='unknown',timestamp_value='',"
                "timestamp_label='Unknown time',time_sort=0,time_basis=? WHERE id=?",
                (
                    text,
                    text.casefold(),
                    json.dumps(provenance),
                    "Unavailable source time" if index == 0 else "",
                    identifier,
                ),
            )
    preserved_history = history.read_bytes()
    with patch(
        "app.web.app.SavedConfigStore.load",
        return_value=SavedConfigStore(CrawlConfig()),
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
            identifiers=identifiers,
            html_session=html_session,
            html_identifiers=html_identifiers,
            store=store,
            history=history,
            preserved_history=preserved_history,
            app=app,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(("width", "scheme"), [(831, "light"), (390, "dark")])
def test_untimed_html_system_notices_omit_empty_clock_and_preserve_raw_source(
    disposable_browser, native_notice_server, tmp_path, width, scheme
):
    fixture = native_notice_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1109},
        color_scheme=scheme,
        reduced_motion="reduce",
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
                    "account": "source:xlsx-3",
                    "session": fixture.html_session,
                }
            ),
            wait_until="networkidle",
        )
        if page.locator("#sidebar_toggle").get_attribute("aria-expanded") == "true":
            page.locator("#sidebar_toggle").click()
        expect(
            page.get_by_role("note", name="System message", exact=True)
        ).to_have_count(2)
        for index, (identifier, text) in enumerate(
            zip(fixture.html_identifiers, UNTIMED_DISPLAY_TEXTS, strict=True)
        ):
            row = page.locator(f"#wechat_message_{identifier}")
            row.scroll_into_view_if_needed()
            expect(row.locator("time")).to_have_count(0)
            assert row.get_attribute("aria-describedby") is None
            expect(row).to_have_attribute(
                "title",
                "Unavailable source time"
                if index == 0
                else "Source timestamp unavailable",
            )
            expect(row.locator(".wechat-system-notice")).to_have_text(text)
            assert "Unknown time" not in row.inner_text()
            geometry = row.evaluate("""row => {
                const body=row.querySelector('.wechat-system-notice');
                return {height:row.getBoundingClientRect().height,
                    bodyHeight:body.getBoundingClientRect().height,
                    children:row.children.length,
                    bubbleAncestor:Boolean(row.closest('.browser-chat-message,.wechat-message-row')),
                    overflow:document.documentElement.scrollWidth-innerWidth};
            }""")
            assert geometry["children"] == 1
            assert abs(geometry["height"] - geometry["bodyHeight"]) <= 1
            assert not geometry["bubbleAncestor"]
            assert geometry["overflow"] <= 1
        page.screenshot(path=tmp_path / f"untimed-html-notice-{width}-{scheme}.png")
        source = query_chat_history(
            fixture.store,
            source="wechat",
            account="source:xlsx-3",
            session=fixture.html_session,
        )
        selected = {message.stable_id: message for message in source.items}
        for identifier, text in zip(
            fixture.html_identifiers, UNTIMED_TEXTS, strict=True
        ):
            message = selected[identifier]
            assert message.content_text == text
            assert message.last_seen_at == ""
            assert message.messaging["timestamp_label"] == "Unknown time"
            assert message.messaging["provenance"]["source_time"] == ""
            assert message.messaging["account_key"] == "source:xlsx-3"
        known = query_chat_history(
            fixture.store,
            source="wechat",
            account="account:wxid_account_a",
            session=fixture.session,
        )
        known_messages = {message.stable_id: message for message in known.items}
        for index in SYSTEM_INDICES:
            message = known_messages[fixture.identifiers[index]]
            assert message.last_seen_at == f"2026-05-11T14:23:{index + 12:02d}+08:00"
        csv_url, markdown_url = _scoped_export_urls(page)
        exported_csv = page.request.get(fixture.url + csv_url)
        assert exported_csv.status == 200
        assert exported_csv.headers["content-type"].startswith("text/csv")
        csv_rows = _assert_csv_source_rows(page, fixture.history, exported_csv.text())
        assert {
            row["Time"] for row in csv_rows if row["Statement"] in UNTIMED_TEXTS
        } == {""}
        exported = page.request.get(fixture.url + markdown_url)
        assert exported.ok
        assert exported.headers["content-type"].startswith("text/markdown")
        assert all(text in exported.text() for text in UNTIMED_TEXTS)
        assert "Unknown time" in exported.text()
        assert fixture.history.read_bytes() == fixture.preserved_history
    finally:
        context.close()


def _assert_notice_rails(page):
    page.wait_for_function(
        """() => document.querySelector('.wechat-message-row > time.wechat-message-time')"""
    )
    geometry = page.locator(".wechat-system-message-row").evaluate_all(
        """rows => {
        const reference = document.querySelector('.wechat-message-row > time.wechat-message-time');
        const referenceBox = reference.getBoundingClientRect();
        const fields = ['fontFamily', 'fontSize', 'fontWeight', 'lineHeight',
            'color', 'letterSpacing'];
        const referenceStyle = getComputedStyle(reference);
        return rows.map(row => {
            const time = row.querySelector(':scope > time.wechat-message-time');
            const body = row.querySelector(':scope > .wechat-system-notice');
            const rowBox = row.getBoundingClientRect();
            const timeBox = time.getBoundingClientRect();
            const bodyBox = body.getBoundingClientRect();
            const style = getComputedStyle(time);
            const rowStyle = getComputedStyle(row);
            return {
                timeStyleMatches: fields.every(field => style[field] === referenceStyle[field]),
                rightError: Math.abs(timeBox.right - referenceBox.right),
                textBelowTime: bodyBox.top >= timeBox.bottom - 0.5,
                timeContained: timeBox.left >= rowBox.left - 0.5
                    && timeBox.right <= rowBox.right + 0.5
                    && timeBox.top >= rowBox.top - 0.5,
                bodyContained: bodyBox.bottom <= rowBox.bottom + 0.5,
                semanticTimeCount: row.querySelectorAll('time').length,
                cardAncestor: Boolean(row.closest('.browser-chat-message, .wechat-message-row')),
                disclosureCount: row.querySelectorAll('details, summary, .wechat-speaker').length,
                rowBackground: rowStyle.backgroundColor,
                rowShadow: rowStyle.boxShadow,
                rowBorder: rowStyle.borderTopWidth,
            };
        });
    }"""
    )
    assert len(geometry) == 2
    for sample in geometry:
        assert sample["timeStyleMatches"], sample
        assert sample["rightError"] <= 1, sample
        assert sample["textBelowTime"], sample
        assert sample["timeContained"] and sample["bodyContained"], sample
        assert sample["semanticTimeCount"] == 1, sample
        assert not sample["cardAncestor"], sample
        assert sample["disclosureCount"] == 0, sample
        assert sample["rowBackground"] in {"rgba(0, 0, 0, 0)", "transparent"}
        assert sample["rowShadow"] == "none"
        assert sample["rowBorder"] == "0px"
    notices = _notice_geometry(page)
    assert notices["documentOverflow"] <= 1, notices
    assert notices["listOverflow"] <= 1, notices
    for sample in notices["samples"]:
        assert sample["centerError"] <= 1.5, sample
        assert max(sample["textCenterErrors"]) <= 1.5, sample
        assert sample["textAlign"] == "center", sample
        assert sample["background"] in {"rgba(0, 0, 0, 0)", "transparent"}
        assert sample["borderWidths"] == ["0px"] * 4
        assert sample["shadow"] == "none"
        assert not sample["bubbleAncestor"]
    assert notices["samples"][-1]["lineCount"] > 1


def _assert_bubble_effects(page):
    # Ruler anchors include plain system rows; only bubble cards own shadow effects.
    page.wait_for_function(
        """() => {
        const list = document.querySelector('[data-chat-scrollport]');
        const boundary = list.getBoundingClientRect();
        const cards = [...list.querySelectorAll('.browser-chat-message')];
        const visible = cards.filter(card => {
            const box = card.getBoundingClientRect();
            return box.bottom > boundary.top + 1 && box.top < boundary.bottom - 1;
        });
        return visible.length > 0 && visible.every(card => {
            const effect = document.querySelector(`[data-chat-effect-for="${card.id}"]`);
            if (!effect || effect.hidden) return false;
            const actual = effect.getBoundingClientRect();
            const expected = card.getBoundingClientRect();
            return ['left', 'top', 'width', 'height'].every(key => Math.abs(actual[key] - expected[key]) < 0.75);
        });
    }"""
    )


@pytest.mark.parametrize("width", [1280, 710, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_native_system_notices_preserve_the_real_right_aligned_timestamp(
    disposable_browser, native_notice_server, tmp_path, width, color_scheme
):
    fixture = native_notice_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1109},
        color_scheme=color_scheme,
        reduced_motion="reduce",
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    query = {
        "view": "text",
        "source": "wechat",
        "account": "account:wxid_account_a",
        "session": fixture.session,
    }
    try:
        page.goto(
            fixture.url + "/browser?" + urlencode(query), wait_until="networkidle"
        )
        toggle = page.locator("#sidebar_toggle")
        if toggle.get_attribute("aria-expanded") == "true":
            toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "false")
        page.evaluate("() => document.fonts.ready")
        expect(page.locator(".wechat-message")).to_have_count(4)
        expect(
            page.get_by_role("note", name="System message", exact=True)
        ).to_have_count(2)
        for index in SYSTEM_INDICES:
            identifier = fixture.identifiers[index]
            row = page.locator(f"#wechat_message_{identifier}")
            expect(row).to_have_class("wechat-system-message-row")
            expect(row).to_have_attribute("data-wechat-source-message-id", identifier)
            expect(row).to_have_attribute(
                "aria-describedby", f"wechat_time_{identifier}"
            )
            expect(row.locator(".wechat-system-notice")).to_have_text(
                SYSTEM_DISPLAY_TEXTS[index]
            )
            time = row.locator("time")
            expect(time).to_have_text(f"11 May 2026 14:23:{index + 12:02d}")
            expect(time).to_have_attribute(
                "datetime", f"2026-05-11T14:23:{index + 12:02d}+08:00"
            )
        spoof = page.locator(f"#wechat_message_{fixture.identifiers[2]}")
        expect(spoof.locator(".wechat-speaker")).to_have_text("System")
        expect(spoof.locator(".wechat-message-text")).to_have_text(RAW_TEXTS[2])
        assert page.locator(".wechat-system-notice script").count() == 0
        assert page.evaluate("typeof window.nativeNoticeExecuted") == "undefined"
        _assert_notice_rails(page)
        _assert_bubble_effects(page)
        assert page.locator(".browser-chat-effect").count() == 4
        for index in SYSTEM_INDICES:
            assert (
                page.locator(
                    f'.browser-chat-effect[data-chat-effect-for="wechat_message_{fixture.identifiers[index]}"]'
                ).count()
                == 0
            )
        page.screenshot(
            path=tmp_path / f"native-system-notices-{width}-{color_scheme}.png"
        )

        page.set_viewport_size({"width": 507, "height": 1109})
        _assert_notice_rails(page)
        page.locator(".browser-chat-pane").evaluate(
            "element => {element.style.transform = 'scale(0.85)'; element.style.transformOrigin = 'top left';}"
        )
        _assert_notice_rails(page)
        assert not errors

        source_page = query_chat_history(
            fixture.store,
            source="wechat",
            account="account:wxid_account_a",
            session=fixture.session,
        )
        assert source_page.total_count == len(RAW_TEXTS)
        assert source_page.current_session.message_count == len(RAW_TEXTS)
        assert [message.content_text for message in source_page.items] == list(
            RAW_TEXTS
        )
        assert [
            bool(message.messaging.get("is_system_notice"))
            for message in source_page.items
        ] == [index in SYSTEM_INDICES for index in range(len(RAW_TEXTS))]
        csv_url, markdown_url = _scoped_export_urls(page)
        exported_csv = fixture.app.test_client().get(csv_url)
        assert exported_csv.status_code == 200
        assert exported_csv.mimetype == "text/csv"
        csv_rows = _assert_csv_source_rows(
            page, fixture.history, exported_csv.get_data(as_text=True)
        )
        assert [row["Time"] for row in csv_rows] == [
            f"2026-05-11 14:23:{index + 12:02d} UTC+08:00"
            for index in range(len(RAW_TEXTS))
        ]
        exported = fixture.app.test_client().get(markdown_url)
        assert exported.status_code == 200
        assert exported.mimetype == "text/markdown"
        markdown = exported.get_data(as_text=True)
        assert "- Messages: 6\n" in markdown
        assert all(text in markdown for text in RAW_TEXTS)
        assert fixture.history.read_bytes() == fixture.preserved_history
    finally:
        context.close()
