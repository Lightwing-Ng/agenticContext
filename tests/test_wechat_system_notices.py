"""PDF notice presentation retains original records, identities, and text exports.

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


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("final_newline", [False, True])
def test_trailing_pdf_notices_keep_order_and_exact_source_offsets(
    ending, final_newline
):
    body = "A multiline message  " + ending + "Second line"
    lines = ["System Message: Contact tickled me", "System Message:\tAnother notice"]
    raw = ending.join([body, *lines]) + (ending if final_newline else "")
    result = _pdf_message_presentation(raw, ("pdf_text_layer",))
    assert result["display_text"] == body
    assert [notice["text"] for notice in result["system_notices"]] == [
        "Contact tickled me",
        "Another notice",
    ]
    spans = result["system_notices"]
    assert spans[0]["source_start"] == len(body + ending)
    assert spans[0]["source_end"] == spans[1]["source_start"]
    assert spans[-1]["source_end"] == len(raw)
    for notice, line in zip(spans, lines, strict=True):
        assert raw[notice["source_start"] : notice["source_end"]].rstrip("\r\n") == line


@pytest.mark.parametrize(
    "raw,flags",
    [
        ("A message\nSystem Message: Contact tickled me", ()),
        ("A message\nSystem Message: Contact tickled me", ("xlsx_export",)),
        ("I quoted System Message: an example", ("pdf_text_layer",)),
        ("A message\n> System Message: quoted", ("pdf_text_layer",)),
        ("A message\n System Message: indented", ("pdf_text_layer",)),
        ("System Message: quoted\nStill the person's text", ("pdf_text_layer",)),
        ("A message\nSystem Message: \t", ("pdf_text_layer",)),
        ("A message\nSystem Message: notice\n\n", ("pdf_text_layer",)),
    ],
)
def test_ambiguous_or_non_pdf_markers_remain_in_the_original_bubble(raw, flags):
    assert _pdf_message_presentation(raw, flags) == {
        "display_text": raw,
        "system_notices": (),
    }


def test_notice_only_record_does_not_require_a_speaker_bubble():
    raw = "System Message: Contact tickled me"
    result = _pdf_message_presentation(raw, ("pdf_text_layer",))
    assert result["display_text"] == ""
    assert result["system_notices"] == (
        {"text": "Contact tickled me", "source_start": 0, "source_end": len(raw)},
    )


def test_presentation_preserves_raw_text_source_identity_and_export(tmp_path):
    archive = make_archive(tmp_path / "archive")
    store = tmp_path / "store"
    import_wechat_archive(archive, store)
    session = next(
        session
        for session in query_chat_history(store, source="wechat").sessions
        if session.messaging["partition_id"] == "xlsx-2"
    )
    database = wechat_archive_path(store)
    raw = "Original message\nSystem Message: Contact tickled me"
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
    message = next(message for message in page.items if message.stable_id == message_id)
    assert message.content_text == raw
    assert message.author_label == author
    assert message.role == direction
    assert message.messaging["provenance"] == json.loads(provenance)
    assert message.messaging["partition_id"] == "xlsx-2"
    assert message.messaging["display_text"] == "Original message"
    assert message.messaging["system_notices"][0]["text"] == "Contact tickled me"
    assert page.current_session.message_count == session.message_count
    assert len(page.items) == session.message_count
    assert raw in build_chat_history_markdown(page)
    assert database.read_bytes() == before
