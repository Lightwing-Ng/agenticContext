"""PDF system notices remain separate from preserved source-message bubbles.

Code version: v1.0.2-codex.0
"""

from __future__ import annotations

from collections import Counter
import csv
from io import StringIO
import json
import sqlite3
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

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
from tests.test_wechat_browser_e2e import (
    _assert_directional_shadow_pixels,
    _assert_wechat_effect_geometry,
)
from tests.test_wechat_session_csv import _expected_rows


disposable_browser = test_sidebar_e2e.disposable_browser
RAW_TEXTS = (
    "An original message.\nSystem Message: I ${wxid_sender@textstatusicon} "
    "tickled Synthetic contact${wxid_contact@textstatusicon}'s custom suffix",
    "Another original message.\nSystem Message: First notice\n"
    "System Message: <script>window.noticeExecuted = true</script> & second notice",
    "A literal System Message: ${wxid_human@textstatusicon} belongs to the speaker.",
    "System Message: A notice without a preceding chat bubble",
    "An empty marker remains original text.\nSystem Message:",
    "A short final message.\nSystem Message: "
    + (
        "A long synthetic notification with 中文 characters and wrapping text. " * 8
    ).rstrip(),
)
EXPECTED_NOTICES = (
    "I tickled Synthetic contact's custom suffix",
    "First notice",
    "<script>window.noticeExecuted = true</script> & second notice",
    "A notice without a preceding chat bubble",
    (
        "A long synthetic notification with 中文 characters and wrapping text. " * 8
    ).rstrip(),
)


def _scoped_export_urls(page):
    query = parse_qs(urlsplit(page.url).query)
    assert query["source"] == ["wechat"]
    session = query["session"][0]
    account = query["account"][0]
    kind = query.get("chat_kind", ["all"])[0]
    csv_url = page.get_by_role(
        "link", name="Export conversation as CSV", exact=True
    ).get_attribute("href")
    assert urlsplit(csv_url).path == f"/browser/wechat/session/{session}/export.csv"
    scope = parse_qs(urlsplit(csv_url).query)
    assert scope["account"] == [account]
    assert scope["chat_kind"] == [kind]
    markdown_url = f"/browser/session/{session}/export?" + urlencode(
        {
            "source": "wechat",
            "account": account,
            "chat_kind": kind,
            "sort": query.get("sort", ["newest"])[0],
        }
    )
    return csv_url, markdown_url


def _assert_csv_source_rows(page, history, encoded):
    session = parse_qs(urlsplit(page.url).query)["session"][0]
    reader = csv.DictReader(StringIO(encoded.removeprefix("\ufeff"), newline=""))
    columns = ["Time", "Speaker ID", "Statement"]
    assert reader.fieldnames == columns
    rows = list(reader)
    with sqlite3.connect(history) as connection:
        connection.row_factory = sqlite3.Row
        original = connection.execute(
            "SELECT sender_id,sender_label,text,timestamp_value FROM messages "
            "WHERE conversation_id=?",
            (session,),
        ).fetchall()
    expected = _expected_rows(original)
    assert len(rows) == len(original)
    assert Counter(tuple(row[column] for column in columns) for row in rows) == Counter(
        tuple(row[column] for column in columns) for row in expected
    )
    return rows


@pytest.fixture(scope="module")
def notice_server(tmp_path_factory):
    sandbox = tmp_path_factory.mktemp("wechat-system-notices")
    archive = make_archive(sandbox / "archive", repeated=3)
    store = sandbox / "store"
    import_wechat_archive(archive, store)
    history = wechat_archive_path(store)
    with sqlite3.connect(history) as connection:
        session = connection.execute(
            "SELECT id FROM conversations WHERE partition_id='xlsx-2'"
        ).fetchone()[0]
        identifiers = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM messages WHERE conversation_id=? ORDER BY ordinal",
                (session,),
            )
        ]
        assert len(identifiers) == len(RAW_TEXTS)
        connection.execute(
            "UPDATE partitions SET source_label='PDF export',"
            "source_file='synthetic.pdf' WHERE id='xlsx-2'"
        )
        for identifier, text in zip(identifiers, RAW_TEXTS, strict=True):
            connection.execute(
                "UPDATE messages SET text=?,quality_flags=?,search_text=? WHERE id=?",
                (text, json.dumps(["pdf_text_layer"]), text.casefold(), identifier),
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
            store=store,
            history=history,
            preserved_history=preserved_history,
            app=app,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _notice_geometry(page):
    return page.locator(".wechat-system-notice").evaluate_all(
        """notices => {
        const list = document.querySelector('.wechat-message-list');
        const listBox = list.getBoundingClientRect();
        const samples = notices.map(notice => {
            const style = getComputedStyle(notice);
            const box = notice.getBoundingClientRect();
            const walker = document.createTreeWalker(notice, NodeFilter.SHOW_TEXT);
            const lines = [];
            let node;
            while ((node = walker.nextNode())) {
                for (let index = 0; index < node.length; index++) {
                    if (/\\s/u.test(node.textContent[index])) continue;
                    const range = document.createRange();
                    range.setStart(node, index);
                    range.setEnd(node, index + 1);
                    const glyph = range.getBoundingClientRect();
                    if (!glyph.width) continue;
                    const center = (glyph.top + glyph.bottom) / 2;
                    let line = lines.find(value => Math.abs(value.center - center) < glyph.height / 2);
                    if (!line) {
                        line = {center, left: glyph.left, right: glyph.right};
                        lines.push(line);
                    } else {
                        line.left = Math.min(line.left, glyph.left);
                        line.right = Math.max(line.right, glyph.right);
                    }
                }
            }
            return {
                centerError: Math.abs((box.left + box.right) / 2
                    - (listBox.left + listBox.right) / 2),
                textCenterErrors: lines.map(rect => Math.abs((rect.left + rect.right) / 2
                    - (box.left + box.right) / 2)),
                lineCount: lines.length,
                textAlign: style.textAlign,
                background: style.backgroundColor,
                borderWidths: [style.borderTopWidth, style.borderRightWidth,
                    style.borderBottomWidth, style.borderLeftWidth],
                shadow: style.boxShadow,
                width: box.width,
                listWidth: list.clientWidth,
                bubbleAncestor: Boolean(notice.closest('.browser-chat-message, .wechat-message-row')),
                nestedTime: notice.querySelectorAll('time, .wechat-time-slot').length,
                directListChild: notice.parentElement === list,
            };
        });
        return {
            samples,
            documentOverflow: document.documentElement.scrollWidth - innerWidth,
            listOverflow: list.scrollWidth - list.clientWidth,
        };
    }"""
    )


@pytest.mark.parametrize("width", [1280, 612, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_pdf_system_notices_are_centered_without_bubbles_and_preserve_source(
    disposable_browser, notice_server, tmp_path, width, color_scheme
):
    fixture = notice_server
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
        "account": "source:xlsx-2",
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
        expect(page.locator(".wechat-message")).to_have_count(5)
        notices = page.get_by_role("note", name="System message", exact=True)
        expect(notices).to_have_count(5)
        assert [value.rstrip() for value in notices.all_text_contents()] == [
            value.rstrip() for value in EXPECTED_NOTICES
        ]
        for index, identifier in (
            (0, fixture.identifiers[0]),
            (1, fixture.identifiers[1]),
            (2, fixture.identifiers[1]),
            (3, fixture.identifiers[3]),
            (4, fixture.identifiers[5]),
        ):
            expect(notices.nth(index)).to_have_attribute(
                "data-wechat-source-message-id", identifier
            )
        expect(
            page.locator(
                f"#wechat_message_{fixture.identifiers[0]} .wechat-message-text"
            )
        ).to_have_text("An original message.")
        expect(
            page.locator(
                f"#wechat_message_{fixture.identifiers[2]} .wechat-message-text"
            )
        ).to_have_text(RAW_TEXTS[2])
        expect(
            page.locator(
                f"#wechat_message_{fixture.identifiers[4]} .wechat-message-text"
            )
        ).to_have_text(RAW_TEXTS[4])
        expect(
            page.locator(f"#wechat_message_{fixture.identifiers[3]}.wechat-message")
        ).to_have_count(0)
        expect(
            page.locator(
                f"#wechat_message_{fixture.identifiers[3]}.wechat-system-notice"
            )
        ).to_have_text(EXPECTED_NOTICES[3])
        expect(page.locator(f"#wechat_notice_{fixture.identifiers[1]}_1")).to_have_text(
            EXPECTED_NOTICES[1]
        )
        expect(page.locator(f"#wechat_notice_{fixture.identifiers[1]}_2")).to_have_text(
            EXPECTED_NOTICES[2]
        )
        assert page.locator(".wechat-system-notice script").count() == 0
        assert page.evaluate("typeof window.noticeExecuted") == "undefined"
        geometry = _notice_geometry(page)
        assert geometry["documentOverflow"] <= 1, geometry
        assert geometry["listOverflow"] <= 1, geometry
        for sample in geometry["samples"]:
            assert sample["centerError"] <= 1.5, sample
            assert max(sample["textCenterErrors"]) <= 1.5, sample
            assert sample["textAlign"] == "center", sample
            assert sample["background"] in {"rgba(0, 0, 0, 0)", "transparent"}, sample
            assert sample["borderWidths"] == ["0px"] * 4, sample
            assert sample["shadow"] == "none", sample
            assert sample["width"] <= sample["listWidth"] + 1, sample
            assert not sample["bubbleAncestor"], sample
            assert sample["nestedTime"] == 0, sample
            assert sample["directListChild"], sample
        assert geometry["samples"][-1]["lineCount"] > 1, geometry
        effects = _assert_wechat_effect_geometry(page)
        _assert_directional_shadow_pixels(page, effects, "unknown")
        assert page.locator(".browser-chat-effect").count() == 5
        assert (
            page.locator(
                '.browser-chat-effect[data-chat-effect-for^="wechat_notice_"]'
            ).count()
            == 0
        )
        assert not errors
        page.screenshot(
            path=tmp_path / f"wechat-system-notices-{width}-{color_scheme}.png"
        )

        source_page = query_chat_history(
            fixture.store,
            source="wechat",
            account="source:xlsx-2",
            session=fixture.session,
        )
        assert source_page.total_count == len(RAW_TEXTS)
        assert source_page.current_session.message_count == len(RAW_TEXTS)
        assert [message.content_text for message in source_page.items] == list(
            RAW_TEXTS
        )
        assert source_page.current_session.messaging["account_key"] == "source:xlsx-2"
        csv_url, markdown_url = _scoped_export_urls(page)
        exported_csv = fixture.app.test_client().get(csv_url)
        assert exported_csv.status_code == 200
        assert exported_csv.mimetype == "text/csv"
        _assert_csv_source_rows(
            page, fixture.history, exported_csv.get_data(as_text=True)
        )
        exported = fixture.app.test_client().get(markdown_url)
        assert exported.status_code == 200
        assert exported.mimetype == "text/markdown"
        markdown = exported.get_data(as_text=True)
        assert "- Messages: 6\n" in markdown
        assert all(text.strip() in markdown for text in RAW_TEXTS)
        assert fixture.history.read_bytes() == fixture.preserved_history
    finally:
        context.close()
