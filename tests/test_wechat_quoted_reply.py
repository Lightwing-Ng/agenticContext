"""Legacy PDF quote presentation preserves raw text, exports, and source scope.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.core.chat_history_browser import (
    build_chat_history_markdown,
    query_chat_history,
)
from app.core.wechat_archive import import_wechat_archive, wechat_archive_path
from app.core.wechat_history_browser import _pdf_message_presentation
from tests.test_wechat_archive import make_archive


SEPARATOR = "- - - - - - - - - - - - - - -"


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
def test_legacy_pdf_quote_splits_source_fields_with_exact_offsets(ending):
    envelope = "「Earlier speaker：A quoted question?」"
    reply = "A reply  " + ending + "Second reply line" + ending
    raw = ending.join([envelope, SEPARATOR, reply])
    result = _pdf_message_presentation(raw, ("pdf_text_layer",))
    quote = result["quoted_reply"]
    assert quote["speaker"] == "Earlier speaker"
    assert quote["text"] == "A quoted question?"
    assert result["display_text"] == reply
    assert result["system_notices"] == ()
    assert raw[quote["source_start"] : quote["source_end"]] == envelope
    assert raw[quote["reply_source_start"] : quote["reply_source_end"]] == reply


def test_selected_message_shape_preserves_unresolved_quoted_speaker_label():
    raw = f"「正知正念正行：你的上级是不是表哥？」\n{SEPARATOR}\n不是"
    result = _pdf_message_presentation(raw, ("pdf_text_layer",))
    assert result["quoted_reply"]["speaker"] == "正知正念正行"
    assert result["quoted_reply"]["text"] == "你的上级是不是表哥？"
    assert result["display_text"] == "不是"
    assert not any(
        key in result["quoted_reply"]
        for key in ("contact_id", "sender_id", "account_id", "source_timestamp")
    )


def test_nested_and_multiline_quote_content_remains_literal_text():
    quoted = "First line\nShe said「Nested speaker：nested message」.\nURL: https://example.test/"
    reply = f"Reply retains its own separator\n{SEPARATOR}\nAnd another line"
    raw = f"「Earlier speaker：{quoted}」\n{SEPARATOR}\n{reply}"
    result = _pdf_message_presentation(raw, ("pdf_text_layer",))
    assert result["quoted_reply"]["text"] == quoted
    assert result["display_text"] == reply


def test_quote_fields_never_create_html_or_normalize_message_payload():
    speaker = '<img src=x onerror="alert(1)">'
    quoted = "<script>alert('quote')</script> & literal <b>markup</b>"
    reply = "<svg onload=alert(2)> literal reply &  two spaces"
    raw = f"「{speaker}：{quoted}」\n{SEPARATOR}\n{reply}"
    result = _pdf_message_presentation(raw, ("pdf_text_layer",))
    assert result["quoted_reply"]["speaker"] == speaker
    assert result["quoted_reply"]["text"] == quoted
    assert result["display_text"] == reply
    assert "html" not in result and "html" not in result["quoted_reply"]


@pytest.mark.parametrize(
    "raw,flags",
    [
        (f"「Earlier：Question」\n{SEPARATOR}\nReply", ()),
        (f"「Earlier：Question」\n{SEPARATOR}\nReply", ("xlsx_export",)),
        (f"A normal message\n{SEPARATOR}\nReply", ("pdf_text_layer",)),
        (f"「An ordinary quotation」\n{SEPARATOR}\nReply", ("pdf_text_layer",)),
        ("「Earlier：Question」\n---\nReply", ("pdf_text_layer",)),
        ("「Earlier：Question」\n---------------\nReply", ("pdf_text_layer",)),
        (f"Prefix 「Earlier：Question」\n{SEPARATOR}\nReply", ("pdf_text_layer",)),
        (f"「：Question」\n{SEPARATOR}\nReply", ("pdf_text_layer",)),
        (f"「 Earlier：Question」\n{SEPARATOR}\nReply", ("pdf_text_layer",)),
        (f"「Earlier\nspeaker：Question」\n{SEPARATOR}\nReply", ("pdf_text_layer",)),
        (f"「Earlier: alias：Question」\n{SEPARATOR}\nReply", ("pdf_text_layer",)),
        (f"「Earlier： 」\n{SEPARATOR}\nReply", ("pdf_text_layer",)),
        (f"「Earlier：Question」\n{SEPARATOR}\n \t", ("pdf_text_layer",)),
        (f"「Earlier：Question」\n\n{SEPARATOR}\nReply", ("pdf_text_layer",)),
        (f"「Earlier：Question」 extra」\n{SEPARATOR}\nReply", ("pdf_text_layer",)),
        (f"「Earlier：「unbalanced quote」\n{SEPARATOR}\nReply", ("pdf_text_layer",)),
    ],
)
def test_unproven_ambiguous_or_ordinary_text_remains_in_the_original_bubble(raw, flags):
    assert _pdf_message_presentation(raw, flags) == {
        "display_text": raw,
        "system_notices": (),
    }


def test_quote_and_trailing_notice_keep_independent_original_source_spans():
    envelope = "「Earlier：Question」"
    reply = "Reply  \nSecond line"
    notice = "System Message: Earlier tickled me"
    body = f"{envelope}\n{SEPARATOR}\n{reply}"
    raw = body + "\n" + notice
    result = _pdf_message_presentation(raw, ("pdf_text_layer",))
    quote = result["quoted_reply"]
    assert result["display_text"] == reply
    assert raw[quote["reply_source_start"] : quote["reply_source_end"]] == reply
    assert result["system_notices"] == (
        {
            "text": "Earlier tickled me",
            "source_start": len(body) + 1,
            "source_end": len(raw),
        },
    )


def test_presentation_keeps_raw_record_identity_contact_and_export_unchanged(tmp_path):
    archive = make_archive(tmp_path / "archive")
    store = tmp_path / "store"
    import_wechat_archive(archive, store)
    session = next(
        item
        for item in query_chat_history(store, source="wechat").sessions
        if item.messaging["partition_id"] == "xlsx-2"
    )
    database = wechat_archive_path(store)
    raw = f"「Earlier speaker：A quoted question?」\n{SEPARATOR}\nThe actual reply"
    with sqlite3.connect(database) as connection:
        message_id, author, direction, provenance = connection.execute(
            "SELECT id,sender_label,direction,provenance FROM messages "
            "WHERE conversation_id=? ORDER BY ordinal LIMIT 1",
            (session.stable_id,),
        ).fetchone()
        connection.execute(
            "UPDATE messages SET text=?,quality_flags=? WHERE id=?",
            (raw, json.dumps(["pdf_text_layer"]), message_id),
        )
    before = database.read_bytes()
    page = query_chat_history(store, source="wechat", session=session.stable_id)
    message = next(item for item in page.items if item.stable_id == message_id)
    assert message.content_text == raw
    assert message.author_label == author
    assert message.role == direction
    assert message.messaging["provenance"] == json.loads(provenance)
    assert message.messaging["partition_id"] == "xlsx-2"
    assert message.messaging["display_text"] == "The actual reply"
    assert message.messaging["quoted_reply"]["speaker"] == "Earlier speaker"
    assert message.messaging["contact"]["identity_status"] == "unresolved"
    assert page.current_session.message_count == session.message_count
    assert len(page.items) == session.message_count
    assert raw in build_chat_history_markdown(page)
    assert database.read_bytes() == before
