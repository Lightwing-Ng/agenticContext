"""PDF and HTML replies share quote order and the message type hierarchy.

Code version: v1.2.1-codex.0
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

from app.core.chat_history_browser import (
    build_chat_history_markdown,
    query_chat_history,
)
from app.core.config import CrawlConfig
from app.core.wechat_archive import import_wechat_archive, wechat_archive_path
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from tests import test_sidebar_e2e
from tests.test_wechat_archive import make_archive
from tests.test_wechat_html_quotes import (
    HTML_ENVELOPE,
    HTML_QUOTE,
    HTML_REPLY,
    HTML_SPEAKER,
    HTML_TEXT,
    html_quote_evidence,
)
from tests.test_wechat_legacy_html_quotes import (
    LEGACY_ENVELOPE,
    LEGACY_QUOTE,
    LEGACY_REPLY,
    LEGACY_SPEAKER,
    LEGACY_TEXT,
    legacy_html_quote_evidence,
)


disposable_browser = test_sidebar_e2e.disposable_browser
LEGACY_RULE = "- " * 14 + "-"
QUOTE_SPEAKER = "Earlier speaker"
QUOTE_TEXT = "这个合成问题是否保留？"
REPLY = "合成回复"
SCRIPT_SPEAKER = "Earlier <script>window.quoteExecuted = true</script>"
SCRIPT_QUOTE = '<img src=x onerror="window.quoteExecuted = true"> & literal text'
SCRIPT_REPLY = "Reply <b>stays literal</b>\nSecond reply line"
RAW_TEXTS = (
    f"「{QUOTE_SPEAKER}：{QUOTE_TEXT}」\n{LEGACY_RULE}\n{REPLY}",
    f"「{SCRIPT_SPEAKER}：{SCRIPT_QUOTE}」\n{LEGACY_RULE}\n{SCRIPT_REPLY}",
    f"Ordinary prose above a literal rule.\n{LEGACY_RULE}\nKeep the rule as text.",
    "A normal message retains its author and body.",
)


LEGACY_LOOKALIKE = LEGACY_TEXT.replace(LEGACY_RULE, "---")
HTML_RAW_TEXTS = (HTML_TEXT, HTML_TEXT, LEGACY_TEXT, LEGACY_LOOKALIKE)


@pytest.fixture(scope="module")
def quoted_reply_server(tmp_path_factory):
    sandbox = tmp_path_factory.mktemp("wechat-quoted-reply")
    archive = make_archive(sandbox / "archive", repeated=2)
    store = sandbox / "store"
    import_wechat_archive(
        archive, store, source_kinds={"xlsx-2": "direct", "xlsx-3": "direct"}
    )
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
            "source_file='synthetic-quoted.pdf' WHERE id='xlsx-2'"
        )
        for identifier, text in zip(identifiers, RAW_TEXTS, strict=True):
            connection.execute(
                "UPDATE messages SET text=?,quality_flags=?,search_text=?,"
                "sender_label='Current sender' WHERE id=?",
                (text, json.dumps(["pdf_text_layer"]), text.casefold(), identifier),
            )
        html_session = connection.execute(
            "SELECT id FROM conversations WHERE partition_id='xlsx-3'"
        ).fetchone()[0]
        html_identifiers = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM messages WHERE conversation_id=? ORDER BY ordinal",
                (html_session,),
            )
        ]
        connection.execute(
            "UPDATE partitions SET source_label='HTML export',source_file='synthetic-quoted.html' WHERE id='xlsx-3'"
        )
        for index, (identifier, text) in enumerate(
            zip(html_identifiers, HTML_RAW_TEXTS, strict=True)
        ):
            evidence = (
                html_quote_evidence()
                if index == 0
                else {
                    "text_extraction_kind": "plain_text",
                    "export_msgid_namespace": "MesLocalID",
                    "locator": {
                        "export_msgtype": "1",
                        "export_msgid_namespace": "MesLocalID",
                    },
                }
            )
            if index >= 2:
                evidence = legacy_html_quote_evidence(text)
            connection.execute(
                "UPDATE messages SET text=?,provenance=?,quality_flags='[]',search_text=?,sender_label='Current HTML sender' WHERE id=?",
                (text, json.dumps(evidence), text.casefold(), identifier),
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
            history=history,
            preserved_history=preserved_history,
            store=store,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _quote_geometry(card):
    return card.evaluate(
        """card => {
        const quote = card.querySelector('blockquote');
        const quoteSpeaker = quote.querySelector('.wechat-quoted-speaker');
        const quoteText = quote.querySelector('.wechat-quoted-text');
        const speaker = card.querySelector('.wechat-speaker');
        const reply = card.querySelector('.wechat-message-text');
        const summary = card.querySelector('summary');
        const rule = card.querySelector('hr');
        const boundary = rule || quote;
        const row = card.closest('.wechat-message-row');
        const time = row.querySelector(':scope > time.wechat-message-time');
        const slot = summary.querySelector('.wechat-time-slot');
        const q = getComputedStyle(quote);
        const qs = getComputedStyle(quoteSpeaker);
        const qt = getComputedStyle(quoteText);
        const s = getComputedStyle(speaker);
        const r = getComputedStyle(reply);
        const b = getComputedStyle(boundary);
        const cardBox = card.getBoundingClientRect();
        const quoteSpeakerBox = quoteSpeaker.getBoundingClientRect();
        const quoteTextBox = quoteText.getBoundingClientRect();
        const speakerBox = speaker.getBoundingClientRect();
        const replyBox = reply.getBoundingClientRect();
        const boundaryBox = boundary.getBoundingClientRect();
        const summaryBox = summary.getBoundingClientRect();
        return {
            ordered: Boolean(quote.compareDocumentPosition(speaker) & Node.DOCUMENT_POSITION_FOLLOWING)
                && Boolean(speaker.compareDocumentPosition(reply) & Node.DOCUMENT_POSITION_FOLLOWING),
            quoteSpeakerTop: quoteSpeakerBox.top,
            quoteSpeakerBottom: quoteSpeakerBox.bottom,
            quoteTextTop: quoteTextBox.top,
            quoteTextBottom: quoteTextBox.bottom,
            separatorTop: rule ? boundaryBox.top : boundaryBox.bottom - parseFloat(b.borderBottomWidth),
            separatorBottom: boundaryBox.bottom,
            speakerTop: speakerBox.top,
            speakerBottom: speakerBox.bottom,
            replyTop: replyBox.top,
            sizes: [qs.fontSize, qt.fontSize, s.fontSize, r.fontSize],
            families: [qs.fontFamily, qt.fontFamily, s.fontFamily, r.fontFamily],
            quoteColors: [qs.color, qt.color],
            foreground: r.color,
            muted: getComputedStyle(card).getPropertyValue('--theme-muted').trim(),
            separatorWidths: [b.borderTopWidth, b.borderBottomWidth],
            separatorStyles: [b.borderTopStyle, b.borderBottomStyle],
            separatorColors: [b.borderTopColor, b.borderBottomColor],
            horizontalQuoteMargins: [q.marginLeft, q.marginRight],
            quoteInset: quote.getBoundingClientRect().left - replyBox.left,
            contentRight: cardBox.right - parseFloat(getComputedStyle(card).paddingRight),
            separatorRight: boundaryBox.right,
            summaryHeight: summaryBox.height,
            timePresent: Boolean(time && slot),
            timeAlignmentError: time && slot
                ? Math.abs(time.getBoundingClientRect().top - slot.getBoundingClientRect().top) : null,
            documentOverflow: document.documentElement.scrollWidth - innerWidth,
            listOverflow: row.parentElement.scrollWidth - row.parentElement.clientWidth,
        };
    }"""
    )


@pytest.mark.parametrize("width", [1280, 710, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_legacy_quote_is_muted_first_and_reply_identity_stays_compact(
    disposable_browser, quoted_reply_server, width, color_scheme
):
    fixture = quoted_reply_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1109},
        color_scheme=color_scheme,
        reduced_motion="reduce",
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(
            fixture.url
            + "/browser?"
            + urlencode(
                {
                    "view": "text",
                    "source": "wechat",
                    "account": "source:xlsx-2",
                    "session": fixture.session,
                }
            ),
            wait_until="networkidle",
        )
        toggle = page.locator("#sidebar_toggle")
        if toggle.get_attribute("aria-expanded") == "true":
            toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "false")
        page.evaluate("() => document.fonts.ready")
        expect(page.locator(".wechat-message")).to_have_count(4)
        target = page.locator(f"#wechat_message_{fixture.identifiers[0]}")
        quote = target.locator("blockquote")
        expect(quote.locator(".wechat-quoted-speaker")).to_have_text(QUOTE_SPEAKER)
        expect(quote.locator(".wechat-quoted-text")).to_have_text(QUOTE_TEXT)
        expect(target.locator(".wechat-speaker")).to_have_text("Current sender")
        expect(target.locator(".wechat-message-text")).to_have_text(REPLY)
        assert LEGACY_RULE not in target.inner_text()
        assert "「" not in quote.inner_text() and "」" not in quote.inner_text()
        metrics = _quote_geometry(target)
        assert metrics["ordered"], metrics
        assert metrics["quoteSpeakerBottom"] <= metrics["quoteTextTop"] + 1, metrics
        assert metrics["quoteTextBottom"] <= metrics["separatorTop"] + 1, metrics
        assert metrics["separatorBottom"] <= metrics["speakerTop"] + 1, metrics
        assert metrics["speakerBottom"] <= metrics["replyTop"] + 1, metrics
        assert metrics["sizes"] == ["15px", "15px", "17px", "15px"], metrics
        assert len(set(metrics["families"])) == 1, metrics
        assert len(set(metrics["quoteColors"])) == 1, metrics
        assert metrics["quoteColors"][0] != metrics["foreground"], metrics
        assert "1px" in metrics["separatorWidths"], metrics
        assert "solid" in metrics["separatorStyles"], metrics
        painted_colors = [
            color
            for color, size in zip(
                metrics["separatorColors"], metrics["separatorWidths"], strict=True
            )
            if size == "1px"
        ]
        assert painted_colors and all(
            color != metrics["foreground"] for color in painted_colors
        ), metrics
        assert metrics["horizontalQuoteMargins"] == ["0px", "0px"], metrics
        assert abs(metrics["quoteInset"]) <= 1, metrics
        assert metrics["separatorRight"] <= metrics["contentRight"] + 1, metrics
        assert 24 <= metrics["summaryHeight"] <= 36, metrics
        assert metrics["timePresent"] and metrics["timeAlignmentError"] <= 1.5, metrics
        assert metrics["documentOverflow"] <= 1 and metrics["listOverflow"] <= 1, (
            metrics
        )

        disclosure = target.locator("details.wechat-provenance")
        summary = disclosure.locator("summary")
        expect(summary).to_have_accessible_name("Source details")
        summary.focus()
        page.keyboard.press("Enter")
        expect(disclosure).to_have_attribute("open", "")
        expect(disclosure.locator(".ui-collapse-body")).to_be_visible()
        page.keyboard.press("Enter")
        expect(disclosure).not_to_have_attribute("open", "")

        literal = page.locator(f"#wechat_message_{fixture.identifiers[1]}")
        expect(literal.locator(".wechat-quoted-speaker")).to_have_text(SCRIPT_SPEAKER)
        expect(literal.locator(".wechat-quoted-text")).to_have_text(SCRIPT_QUOTE)
        expect(literal.locator(".wechat-message-text")).to_have_text(SCRIPT_REPLY)
        assert literal.locator("script,img,b").count() == 0
        assert page.evaluate("typeof window.quoteExecuted") == "undefined"
        for index in (2, 3):
            normal = page.locator(f"#wechat_message_{fixture.identifiers[index]}")
            expect(normal.locator("blockquote")).to_have_count(0)
            expect(normal.locator(".wechat-speaker")).to_have_text("Current sender")
            expect(normal.locator(".wechat-message-text")).to_have_text(
                RAW_TEXTS[index]
            )
        original_page = query_chat_history(
            fixture.store, source="wechat", session=fixture.session
        )
        assert tuple(item.content_text for item in original_page.items) == RAW_TEXTS
        exported = build_chat_history_markdown(original_page)
        assert all(raw in exported for raw in RAW_TEXTS)
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
        page.evaluate("() => document.fonts.ready")
        expect(page.locator(".wechat-message")).to_have_count(4)
        html_target = page.locator(f"#wechat_message_{fixture.html_identifiers[0]}")
        expect(html_target.locator(".wechat-quoted-speaker")).to_have_text(HTML_SPEAKER)
        expect(html_target.locator(".wechat-quoted-text")).to_have_text(HTML_QUOTE)
        expect(html_target.locator(".wechat-speaker")).to_have_text(
            "Current HTML sender"
        )
        expect(html_target.locator(".wechat-message-text")).to_have_text(HTML_REPLY)
        assert HTML_ENVELOPE not in html_target.inner_text()
        html_geometry = _quote_geometry(html_target)
        assert html_geometry["ordered"]
        assert html_geometry["quoteTextBottom"] <= html_geometry["separatorTop"] + 1
        assert html_geometry["separatorBottom"] <= html_geometry["speakerTop"] + 1
        assert html_geometry["sizes"] == metrics["sizes"]
        assert html_geometry["quoteColors"] == metrics["quoteColors"]
        assert html_geometry["separatorWidths"] == metrics["separatorWidths"]
        assert abs(html_geometry["quoteInset"]) <= 1
        assert (
            html_geometry["timePresent"] and html_geometry["timeAlignmentError"] <= 1.5
        )
        assert (
            html_geometry["documentOverflow"] <= 1
            and html_geometry["listOverflow"] <= 1
        )
        lookalike = page.locator(f"#wechat_message_{fixture.html_identifiers[1]}")
        expect(lookalike.locator("blockquote")).to_have_count(0)
        expect(lookalike.locator(".wechat-message-text")).to_have_text(HTML_TEXT)
        legacy_target = page.locator(f"#wechat_message_{fixture.html_identifiers[2]}")
        expect(legacy_target.locator(".wechat-quoted-speaker")).to_have_text(
            LEGACY_SPEAKER
        )
        expect(legacy_target.locator(".wechat-quoted-text")).to_have_text(LEGACY_QUOTE)
        expect(legacy_target.locator(".wechat-message-text")).to_have_text(LEGACY_REPLY)
        assert LEGACY_ENVELOPE not in legacy_target.inner_text()
        assert LEGACY_RULE not in legacy_target.inner_text()
        legacy_geometry = _quote_geometry(legacy_target)
        assert legacy_geometry["ordered"]
        assert legacy_geometry["quoteTextBottom"] <= legacy_geometry["separatorTop"] + 1
        assert legacy_geometry["separatorBottom"] <= legacy_geometry["speakerTop"] + 1
        assert legacy_geometry["sizes"] == metrics["sizes"]
        assert legacy_geometry["quoteColors"] == metrics["quoteColors"]
        assert legacy_geometry["separatorWidths"] == metrics["separatorWidths"]
        assert (
            legacy_geometry["timePresent"]
            and legacy_geometry["timeAlignmentError"] <= 1.5
        )
        assert (
            legacy_geometry["documentOverflow"] <= 1
            and legacy_geometry["listOverflow"] <= 1
        )
        legacy_lookalike = page.locator(
            f"#wechat_message_{fixture.html_identifiers[3]}"
        )
        expect(legacy_lookalike.locator("blockquote")).to_have_count(0)
        expect(legacy_lookalike.locator(".wechat-message-text")).to_have_text(
            LEGACY_LOOKALIKE
        )
        html_page = query_chat_history(
            fixture.store, source="wechat", session=fixture.html_session
        )
        assert tuple(item.content_text for item in html_page.items) == HTML_RAW_TEXTS
        assert all(
            raw in build_chat_history_markdown(html_page) for raw in HTML_RAW_TEXTS
        )
        assert fixture.history.read_bytes() == fixture.preserved_history
        assert errors == []
    finally:
        context.close()
