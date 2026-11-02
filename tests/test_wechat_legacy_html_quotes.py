"""Legacy Type 1 HTML quotes preserve source text and shared presentation roles.

Code version: v1.0.1-codex.0
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
from app.core.wechat_history_browser import _legacy_html_quoted_reply_presentation
from tests.test_wechat_archive import make_archive


LEGACY_RULE = "- " * 14 + "-"
LEGACY_SPEAKER = "Earlier legacy speaker"
LEGACY_QUOTE = "A legacy quoted question?"
LEGACY_REPLY = "The actual legacy HTML reply"
LEGACY_ENVELOPE = f"「{LEGACY_SPEAKER}：{LEGACY_QUOTE}」"
LEGACY_TEXT = f"{LEGACY_ENVELOPE}\n{LEGACY_RULE}\n{LEGACY_REPLY}"


def legacy_html_quote_evidence(raw=LEGACY_TEXT, **changes):
    return {
        "text_extraction_kind": "plain_text",
        "export_msgid_namespace": "MesLocalID",
        "locator": {
            "export_msgtype": "1",
            "export_msgid_namespace": "MesLocalID",
            "export_msgid": "4525",
        },
        "quoted_components": [],
        "forwarded_components": [],
        "primary_text_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        **changes,
    }


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
def test_legacy_html_quote_uses_exact_full_payload_hash_and_source_spans(ending):
    quoted = f"First line{ending}Nested「Original：literal quotation」"
    envelope = f"「Earlier：{quoted}」"
    reply = f"  Reply{ending}Second line  {ending}"
    raw = ending.join([envelope, LEGACY_RULE, reply])
    evidence = legacy_html_quote_evidence(raw)
    before = deepcopy(evidence)
    result = _legacy_html_quoted_reply_presentation(raw, evidence)
    quote = result["quoted_reply"]
    assert quote["speaker"] == "Earlier"
    assert quote["text"] == quoted
    assert result["display_text"] == reply
    assert raw[quote["source_start"] : quote["source_end"]] == envelope
    assert raw[quote["reply_source_start"] : quote["reply_source_end"]] == reply
    assert not any(
        key in quote for key in ("sender_id", "contact_id", "account_id", "reference")
    )
    assert evidence == before


def test_legacy_html_quote_fields_remain_literal_text():
    speaker = '<img src=x onerror="alert(1)">'
    quoted = "<script>alert('quote')</script> & literal markup"
    reply = "<svg onload=alert(2)> literal reply  "
    raw = f"「{speaker}：{quoted}」\n{LEGACY_RULE}\n{reply}"
    result = _legacy_html_quoted_reply_presentation(
        raw, legacy_html_quote_evidence(raw)
    )
    assert result["quoted_reply"]["speaker"] == speaker
    assert result["quoted_reply"]["text"] == quoted
    assert result["display_text"] == reply
    assert "html" not in result and "html" not in result["quoted_reply"]


@pytest.mark.parametrize("evidence", [None, {}, {"text_extraction_kind": "plain_text"}])
def test_legacy_shape_without_verified_html_payload_evidence_stays_literal(evidence):
    assert _legacy_html_quoted_reply_presentation(LEGACY_TEXT, evidence) == {}


@pytest.mark.parametrize(
    "changes",
    [
        {"text_extraction_kind": "quoted_reply"},
        {"message_type": 1},
        {"message_type": None},
        {"export_msgid_namespace": "server_id"},
        {"locator": None},
        {"locator": {"export_msgtype": 1, "export_msgid_namespace": "MesLocalID"}},
        {"locator": {"export_msgtype": "49", "export_msgid_namespace": "MesLocalID"}},
        {"locator": {"export_msgtype": "1"}},
        {"quoted_components": [{"kind": "rendered_quote", "text": LEGACY_ENVELOPE}]},
        {"quoted_components": None},
        {"forwarded_components": [{"text": "Unrelated forwarded content"}]},
        {"forwarded_components": None},
        {"primary_text_sha256": None},
        {"primary_text_sha256": hashlib.sha256(LEGACY_REPLY.encode()).hexdigest()},
        {"primary_text_sha256": "0" * 64},
    ],
)
def test_legacy_html_quote_rejects_inconsistent_source_evidence(changes):
    evidence = legacy_html_quote_evidence(**changes)
    before = deepcopy(evidence)
    assert _legacy_html_quoted_reply_presentation(LEGACY_TEXT, evidence) == {}
    assert evidence == before


@pytest.mark.parametrize("nested", [False, True], ids=["top-level", "locator"])
@pytest.mark.parametrize(
    "field",
    [
        "message_type",
        "appmsg_subtype",
        "quoted_reference",
        "text_components",
        "forwarded_references",
    ],
)
def test_legacy_html_quote_does_not_override_native_or_reference_roles(field, nested):
    evidence = legacy_html_quote_evidence()
    (evidence["locator"] if nested else evidence)[field] = []
    before = deepcopy(evidence)
    assert _legacy_html_quoted_reply_presentation(LEGACY_TEXT, evidence) == {}
    assert evidence == before


@pytest.mark.parametrize(
    "raw",
    [
        f"{LEGACY_ENVELOPE}\n\n{LEGACY_REPLY}",
        LEGACY_TEXT.replace(LEGACY_RULE, "---"),
        LEGACY_TEXT.replace(LEGACY_RULE, "-" * 15),
        LEGACY_TEXT.replace(LEGACY_RULE, "- " * 13 + "-"),
        "Prefix " + LEGACY_TEXT,
        f"「An ordinary quotation」\n{LEGACY_RULE}\n{LEGACY_REPLY}",
        f"{LEGACY_ENVELOPE}\n{LEGACY_RULE}\n \t",
    ],
)
def test_ordinary_quotes_or_incomplete_legacy_envelopes_remain_literal(raw):
    assert (
        _legacy_html_quoted_reply_presentation(raw, legacy_html_quote_evidence(raw))
        == {}
    )


def test_query_and_export_preserve_legacy_html_record_account_and_source_bytes(
    tmp_path,
):
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
        for index, identifier in enumerate(identifiers):
            evidence = legacy_html_quote_evidence()
            if index:
                evidence["primary_text_sha256"] = "0" * 64
            connection.execute(
                "UPDATE messages SET text=?,provenance=?,sender_label='Current legacy sender' WHERE id=?",
                (LEGACY_TEXT, json.dumps(evidence), identifier),
            )
    before = history.read_bytes()
    page = query_chat_history(
        store, source="wechat", account="source:xlsx-3", session=session
    )
    assert [item.stable_id for item in page.items] == identifiers
    assert len(page.items) == page.current_session.message_count == 2
    first, literal = page.items
    assert first.messaging["display_text"] == LEGACY_REPLY
    assert first.messaging["quoted_reply"]["speaker"] == LEGACY_SPEAKER
    assert first.messaging["quoted_reply"]["text"] == LEGACY_QUOTE
    assert first.messaging["provenance"] == legacy_html_quote_evidence()
    assert first.messaging["contact"]["identity_status"] == "unresolved"
    assert literal.messaging["display_text"] == LEGACY_TEXT
    assert not literal.messaging.get("quoted_reply")
    for item in page.items:
        assert item.content_text == LEGACY_TEXT
        assert item.author_label == "Current legacy sender"
        assert item.messaging["account_key"] == "source:xlsx-3"
        assert item.messaging["partition_id"] == "xlsx-3"
    assert build_chat_history_markdown(page).count(LEGACY_TEXT) == 2
    assert history.read_bytes() == before
