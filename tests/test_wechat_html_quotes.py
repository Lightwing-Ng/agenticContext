"""HTML quote presentation requires explicit, hash-consistent export evidence.

Code version: v1.1.0-codex.0
"""

from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from app.core.chat_history_browser import (
    build_chat_history_markdown,
    query_chat_history,
)
from app.core.wechat_archive import import_wechat_archive, wechat_archive_path
from app.core.wechat_history_browser import _html_quoted_reply_presentation
from tests.test_wechat_archive import make_archive


HTML_SPEAKER = "Exported earlier speaker"
HTML_QUOTE = "A quoted question?"
HTML_REPLY = "The actual HTML reply"
HTML_ENVELOPE = f"「{HTML_SPEAKER}：{HTML_QUOTE}」"
HTML_TEXT = HTML_ENVELOPE + "\n\n" + HTML_REPLY


def html_quote_evidence(envelope=HTML_ENVELOPE, reply=HTML_REPLY, **changes):
    return {
        "text_extraction_kind": "quoted_reply",
        "export_msgid_namespace": "MesLocalID",
        "locator": {
            "export_msgtype": "49",
            "export_msgid_namespace": "MesLocalID",
            "export_msgid": "71",
        },
        "quoted_components": [{"kind": "rendered_quote", "text": envelope}],
        "primary_text_sha256": hashlib.sha256(reply.encode("utf-8")).hexdigest(),
        **changes,
    }


@pytest.mark.parametrize("ending", ["\n", "\r\n"])
def test_html_quote_preserves_exact_reply_whitespace_and_source_offsets(ending):
    quoted = "原文「Nested：quote」" + ending + "<b>literal</b> & text"
    envelope = f"「Earlier：{quoted}」"
    reply = "  Reply" + ending + "Second line  " + ending
    raw = envelope + "\n\n" + reply
    evidence = html_quote_evidence(envelope, reply)
    before = deepcopy(evidence)
    result = _html_quoted_reply_presentation(raw, evidence)
    quote = result["quoted_reply"]
    assert result["display_text"] == reply
    assert quote["speaker"] == "Earlier"
    assert quote["text"] == quoted
    assert raw[quote["source_start"] : quote["source_end"]] == envelope
    assert raw[quote["reply_source_start"] : quote["reply_source_end"]] == reply
    assert not any(key in quote for key in ("sender_id", "contact_id", "account_id"))
    assert evidence == before


def test_html_quote_fields_remain_literal_without_creating_html_or_contact_roles():
    speaker = "<script>window.quoteExecuted = true</script>"
    quoted = '<img src=x onerror="alert(1)"> & original'
    reply = '<svg onload="alert(2)"> reply'
    envelope = f"「{speaker}：{quoted}」"
    result = _html_quoted_reply_presentation(
        envelope + "\n\n" + reply, html_quote_evidence(envelope, reply)
    )
    assert result["display_text"] == reply
    assert result["quoted_reply"]["speaker"] == speaker
    assert result["quoted_reply"]["text"] == quoted
    assert "html" not in result and "html" not in result["quoted_reply"]


@pytest.mark.parametrize("evidence", [None, {}, {"text_extraction_kind": "plain_text"}])
def test_ordinary_typed_quote_syntax_never_establishes_html_quote_roles(evidence):
    assert _html_quoted_reply_presentation(HTML_TEXT, evidence) == {}


@pytest.mark.parametrize(
    "changes",
    [
        {"text_extraction_kind": "plain_text"},
        {"message_type": 1},
        {"message_type": 49},
        {"message_type": None},
        {"export_msgid_namespace": "server_id"},
        {"locator": None},
        {"locator": {"export_msgtype": "1", "export_msgid_namespace": "MesLocalID"}},
        {"locator": {"export_msgtype": 49, "export_msgid_namespace": "MesLocalID"}},
        {"locator": {"export_msgtype": "49"}},
        {"quoted_components": None},
        {"quoted_components": []},
        {"quoted_components": [{"kind": "rendered_quote", "text": HTML_ENVELOPE}] * 2},
        {"quoted_components": [{"kind": "quoted_text", "text": HTML_ENVELOPE}]},
        {"quoted_components": [{"kind": "rendered_quote", "text": None}]},
        {"primary_text_sha256": None},
        {"forwarded_components": [{"text": "Unrelated forwarded content"}]},
        {"forwarded_components": None},
        {"primary_text_sha256": "0" * 64},
        {
            "primary_text_sha256": hashlib.sha256(HTML_REPLY.encode())
            .hexdigest()
            .upper()
        },
    ],
)
def test_html_quote_rejects_incomplete_conflicting_or_unverified_evidence(changes):
    evidence = html_quote_evidence(**changes)
    before = deepcopy(evidence)
    assert _html_quoted_reply_presentation(HTML_TEXT, evidence) == {}
    assert evidence == before


@pytest.mark.parametrize(
    "envelope",
    [
        "「An ordinary quotation」",
        "「：Question」",
        "「 Earlier：Question」",
        "「Earlier\nspeaker：Question」",
        "「Earlier: alias：Question」",
        "「Earlier：Question」 extra」",
        "「Earlier：「Unbalanced」",
        "「Earlier： 」",
    ],
)
def test_explicit_html_quote_keeps_ambiguous_labels_literal_with_an_unresolved_speaker(
    envelope,
):
    result = _html_quoted_reply_presentation(
        envelope + "\n\n" + HTML_REPLY, html_quote_evidence(envelope)
    )
    assert result["display_text"] == HTML_REPLY
    assert result["quoted_reply"]["speaker"] == "Quoted message"
    assert result["quoted_reply"]["text"] == envelope
    assert "sender_id" not in result["quoted_reply"]
    assert "contact_id" not in result["quoted_reply"]


@pytest.mark.parametrize(
    "raw",
    ["prefix " + HTML_TEXT, HTML_TEXT.replace("\n\n", "\n"), HTML_TEXT + " changed"],
)
def test_html_quote_rejects_a_prefix_or_reply_that_disagrees_with_source_evidence(raw):
    assert _html_quoted_reply_presentation(raw, html_quote_evidence()) == {}


@pytest.mark.parametrize("reply", ["", " \t\r\n"])
def test_html_quote_requires_a_nonblank_reply_even_with_an_exact_hash(reply):
    assert (
        _html_quoted_reply_presentation(
            HTML_ENVELOPE + "\n\n" + reply, html_quote_evidence(reply=reply)
        )
        == {}
    )


def test_query_and_raw_export_preserve_html_record_identity_and_stored_bytes(tmp_path):
    archive = make_archive(tmp_path / "archive")
    store = tmp_path / "store"
    import_wechat_archive(archive, store, source_kinds={"xlsx-3": "direct"})
    history = wechat_archive_path(store)
    with sqlite3.connect(history) as connection:
        session = connection.execute(
            "SELECT id FROM conversations WHERE partition_id='xlsx-3'"
        ).fetchone()[0]
        identifiers = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM messages WHERE conversation_id=? ORDER BY ordinal",
                (session,),
            )
        ]
        connection.execute(
            "UPDATE partitions SET source_label='HTML export',source_file='synthetic.html' WHERE id='xlsx-3'"
        )
        for index, identifier in enumerate(identifiers):
            evidence = html_quote_evidence()
            if index:
                evidence["text_extraction_kind"] = "plain_text"
                evidence["locator"]["export_msgtype"] = "1"
            connection.execute(
                "UPDATE messages SET text=?,provenance=?,sender_label='Current HTML author' WHERE id=?",
                (HTML_TEXT, json.dumps(evidence), identifier),
            )
    before = history.read_bytes()
    page = query_chat_history(
        store, source="wechat", account="source:xlsx-3", session=session
    )
    assert len(page.items) == page.current_session.message_count == 2
    assert [item.stable_id for item in page.items] == identifiers
    first, literal = page.items
    assert first.messaging["display_text"] == HTML_REPLY
    assert first.messaging["quoted_reply"]["speaker"] == HTML_SPEAKER
    assert first.messaging["quoted_reply"]["text"] == HTML_QUOTE
    assert first.messaging["provenance"] == html_quote_evidence()
    assert first.messaging["contact"]["identity_status"] == "unresolved"
    assert literal.messaging["display_text"] == HTML_TEXT
    assert not literal.messaging.get("quoted_reply")
    for item in page.items:
        assert item.content_text == HTML_TEXT
        assert item.author_label == "Current HTML author"
        assert item.messaging["partition_id"] == "xlsx-3"
        assert item.messaging["account_key"] == "source:xlsx-3"
    assert build_chat_history_markdown(page).count(HTML_TEXT) == 2
    assert history.read_bytes() == before
