"""System-only status decorations disappear without changing source evidence.

Code version: v1.1.1-codex.0
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
from app.core.wechat_history_browser import (
    _pdf_message_presentation,
    _system_notice_display_text,
)
from tests.test_wechat_archive import make_archive


STATUS_A = "${wxid_synthetic_status_a@textstatusicon}"
STATUS_B = "${wxid_synthetic_status_b@textstatusicon}"
RAW_NOTICE = f"I {STATUS_A} tickled 合成联系人{STATUS_B}'s synthetic后缀"
DISPLAY_NOTICE = "I tickled 合成联系人's synthetic后缀"


@pytest.mark.parametrize(
    "raw,expected",
    [
        (RAW_NOTICE, DISPLAY_NOTICE),
        (f"甲{STATUS_A}拍了拍乙{STATUS_B}的肩膀", "甲拍了拍乙的肩膀"),
        (f"Earlier{STATUS_A}'s suffix", "Earlier's suffix"),
        (f"Contact{STATUS_A}{STATUS_B} tickled me", "Contact tickled me"),
        (f"I {STATUS_A}{STATUS_B} tickled Contact", "I tickled Contact"),
        (f"I {STATUS_A}tickled Contact", "I tickled Contact"),
        (f"I  {STATUS_A}  tickled Contact", "I   tickled Contact"),
        (f"I\t{STATUS_A}\ttickled Contact", "I\t\ttickled Contact"),
        (f"I\n{STATUS_A}\ntickled Contact", "I\n\ntickled Contact"),
        (f"First line\nContact{STATUS_A}\nLast line", "First line\nContact\nLast line"),
        (f"<b>Contact</b>{STATUS_A} & literal text", "<b>Contact</b> & literal text"),
        ("I tickled Contact's synthetic后缀", "I tickled Contact's synthetic后缀"),
        ("System Message: Contact tickled me", "Contact tickled me"),
        ("System Message:\t Contact tickled me  ", "Contact tickled me  "),
        ("System Message: " + RAW_NOTICE, DISPLAY_NOTICE),
        ("System Message: System Message: literal", "System Message: literal"),
        ("System Message:", "System Message:"),
        ("System Message: \t", "System Message: \t"),
        ("A literal System Message: stays here", "A literal System Message: stays here"),
        ("", ""),
    ],
)
def test_recognized_system_decoration_preserves_name_suffix_and_other_text(
    raw, expected
):
    assert _system_notice_display_text(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "${wxid_example@othericon}",
        "${wxid_example@textstatusicon2}",
        "${wxid_example@TextStatusIcon}",
        "${@textstatusicon}",
        "${wxid example@textstatusicon}",
        "${wxid_example\n@textstatusicon}",
        "${wxid_{example}@textstatusicon}",
        "${wxid_example@textstatusicon",
        "$wxid_example@textstatusicon}",
        "wxid_example@textstatusicon",
    ],
)
def test_unknown_or_malformed_placeholders_remain_literal(raw):
    assert _system_notice_display_text(raw) == raw


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
def test_pdf_notice_cleanup_keeps_exact_original_source_offsets(ending):
    body = f"Human literal {STATUS_A} remains unchanged"
    first = "System Message: " + RAW_NOTICE
    second = f"System Message: Contact{STATUS_B} tickled me"
    raw = ending.join((body, first, second)) + ending
    result = _pdf_message_presentation(raw, ("pdf_text_layer",))
    assert result["display_text"] == body
    assert [notice["text"] for notice in result["system_notices"]] == [
        DISPLAY_NOTICE,
        "Contact tickled me",
    ]
    for notice, original in zip(result["system_notices"], (first, second), strict=True):
        assert raw[notice["source_start"] : notice["source_end"]] == original + ending


def test_pdf_notice_removes_only_one_exporter_prefix():
    raw = "System Message: System Message: literal"
    result = _pdf_message_presentation(raw, ("pdf_text_layer",))
    assert result["system_notices"] == (
        {"text": "System Message: literal", "source_start": 0, "source_end": len(raw)},
    )


@pytest.mark.parametrize("flags", [(), ("xlsx_export",), ("system_notice",)])
def test_marker_like_text_does_not_make_an_ordinary_message_a_system_notice(flags):
    raw = "Human prose\nSystem Message: " + RAW_NOTICE
    result = _pdf_message_presentation(raw, flags)
    assert result == {"display_text": raw, "system_notices": ()}


def test_recognized_quote_keeps_status_marker_as_quoted_literal_text():
    quote = f"A discussion of the literal token {STATUS_A}"
    separator = "- - - - - - - - - - - - - - -"
    raw = f"「Earlier speaker：{quote}」\n{separator}\nActual reply"
    result = _pdf_message_presentation(raw, ("pdf_text_layer",))
    assert result["quoted_reply"]["text"] == quote
    assert result["display_text"] == "Actual reply"
    assert result["system_notices"] == ()


@pytest.mark.parametrize("source_kind", ["native", "export_prefixed", "pdf"])
def test_system_presentation_preserves_raw_record_identity_exports_and_database(
    tmp_path, source_kind
):
    archive = make_archive(tmp_path / "archive", repeated=2)
    store = tmp_path / "store"
    import_wechat_archive(archive, store)
    session = next(
        item
        for item in query_chat_history(store, source="wechat").sessions
        if (source_kind == "pdf" and item.messaging["partition_id"] == "xlsx-2")
        or (
            source_kind != "pdf"
            and item.messaging["account_key"] == "account:wxid_account_a"
            and item.messaging["kind"] == "direct"
        )
    )
    database = wechat_archive_path(store)
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT id,provenance FROM messages WHERE conversation_id=? ORDER BY ordinal",
            (session.stable_id,),
        ).fetchall()
        identifier, encoded = rows[0]
        provenance = json.loads(encoded)
        if source_kind != "pdf":
            provenance.update(text_extraction_kind="system_text", message_type=10000)
            raw = ("System Message: " if source_kind == "export_prefixed" else "") + RAW_NOTICE
            flags = ["system_notice"]
        else:
            raw = f"Human {STATUS_A} literal\nSystem Message: {RAW_NOTICE}"
            flags = ["pdf_text_layer"]
        connection.execute(
            "UPDATE messages SET text=?,quality_flags=?,provenance=? WHERE id=?",
            (raw, json.dumps(flags), json.dumps(provenance), identifier),
        )
        spoof_id = rows[1][0]
        spoof_text = "Human literal " + RAW_NOTICE
        connection.execute(
            "UPDATE messages SET text=?,sender_label='System',quality_flags=? WHERE id=?",
            (spoof_text, json.dumps(["system_notice"]), spoof_id),
        )
        originals = {
            row[0]: row
            for row in connection.execute(
                "SELECT id,sender_id,sender_label,direction,provenance,timestamp_value "
                "FROM messages WHERE conversation_id=?",
                (session.stable_id,),
            )
        }
    preserved = database.read_bytes()
    page = query_chat_history(store, source="wechat", session=session.stable_id)
    message = next(item for item in page.items if item.stable_id == identifier)
    assert message.content_text == raw
    assert message.author_label == originals[identifier][2]
    assert message.role == originals[identifier][3]
    assert message.messaging["sender_id"] == originals[identifier][1]
    assert message.messaging["provenance"] == json.loads(originals[identifier][4])
    assert message.last_seen_at == originals[identifier][5]
    if source_kind != "pdf":
        assert message.messaging["is_system_notice"]
        assert message.messaging["display_text"] == DISPLAY_NOTICE
    else:
        assert message.messaging["display_text"] == f"Human {STATUS_A} literal"
        assert message.messaging["system_notices"][0]["text"] == DISPLAY_NOTICE
    spoof = next(item for item in page.items if item.stable_id == spoof_id)
    assert not spoof.messaging["is_system_notice"]
    assert spoof.messaging["display_text"] == spoof_text
    assert len(page.items) == session.message_count
    assert raw in build_chat_history_markdown(page)
    assert spoof_text in build_chat_history_markdown(page)
    assert database.read_bytes() == preserved
