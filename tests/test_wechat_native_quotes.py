"""Native quote evidence survives reconciliation without inventing message rows.

Code version: v1.0.1-codex.0
"""

from copy import deepcopy
import json
import sqlite3
from xml.sax.saxutils import escape

import pytest

from app.core.chat_history_browser import (
    build_chat_history_markdown,
    query_chat_history,
)
from app.core.wechat_archive import wechat_archive_path
from app.core.wechat_history_browser import _native_quoted_reply_presentation
from app.core.wechat_reconcile import native_text_metadata
from app.core.wechat_text import extract_wechat_text
from tests.test_wechat_reconcile import merge, record, rows, source


def quoted_forward():
    original = "<script>literal text</script> & original\nsecond line"
    forwarded_record = (
        '<recordinfo><datalist><dataitem datatype="1" dataid="original-item">'
        "<sourcename>Original participant</sourcename><dataitemsource>"
        "<fromusr>wxid_original</fromusr><msgid>9223372036854775807</msgid>"
        "</dataitemsource><datadesc>"
        + escape(original)
        + "</datadesc></dataitem></datalist></recordinfo>"
    )
    forwarded = (
        "<msg><appmsg><type>19</type><title>Forwarded conversation</title>"
        "<recorditem>" + escape(forwarded_record) + "</recorditem></appmsg></msg>"
    )
    reply = "  Actual <b>literal</b> reply\nwith whitespace  "
    envelope = (
        "<msg><appmsg><type>57</type><title>"
        + escape(reply)
        + "</title><refermsg><type>49</type><fromusr>wxid_forwarder</fromusr>"
        "<displayname>Forwarding participant</displayname>"
        "<svrid>18446744073709551615</svrid><content>"
        + escape(forwarded)
        + "</content></refermsg></appmsg></msg>"
    )
    parsed = extract_wechat_text(49, envelope)
    assert parsed is not None
    return parsed, reply, original


def detail(store, *, account=""):
    sessions = query_chat_history(store, source="wechat", account=account).sessions
    assert len(sessions) == 1
    return query_chat_history(
        store, source="wechat", account=account, session=sessions[0].stable_id
    )


@pytest.mark.parametrize("legacy_locator", [False, True])
def test_extracted_quote_survives_reconcile_query_and_raw_export(
    tmp_path, legacy_locator
):
    metadata = source()
    parsed, reply, original = quoted_forward()
    observation = record(metadata, message_type=49, **parsed)
    if legacy_locator:
        for key in ("text_components", "quoted_reference", "appmsg_subtype"):
            observation["source_locator"][key] = observation.pop(key)
    result = merge(tmp_path, [metadata], [observation])
    assert result["inserted_messages"] == result["messages_after"] == 1
    before = wechat_archive_path(tmp_path).read_bytes()
    page = detail(tmp_path)
    assert page.total_count == len(page.items) == 1
    message = page.items[0]
    assert message.content_text == parsed["text"] == reply + "\n\n" + original
    assert message.author_label == observation["sender_display_name"]
    assert message.messaging["sender_id"] == "wxid_peer"
    assert message.messaging["display_text"] == reply
    quote = message.messaging["quoted_reply"]
    assert quote["speaker"] == "Forwarding participant"
    assert quote["text"] == "Original participant:\n" + original
    assert quote["reference"] == parsed["quoted_reference"]
    assert quote["reference"]["source_sender_id"] == "wxid_forwarder"
    assert quote["reference"]["source_ids"] == {"svrid": "18446744073709551615"}
    assert quote["text_components"] == parsed["text_components"]
    assert (
        quote["text_components"][1]["text_components"][0]["source_sender_id"]
        == "wxid_original"
    )
    provenance = message.messaging["provenance"]
    for key in ("text_components", "quoted_reference", "appmsg_subtype"):
        assert provenance[key] == parsed[key]
    markdown = build_chat_history_markdown(page)
    assert parsed["text"].strip() in markdown
    assert json.dumps(provenance, ensure_ascii=False) in markdown
    assert "html" not in quote and "content_html" not in quote
    assert message.content_html == ""
    assert wechat_archive_path(tmp_path).read_bytes() == before
    replay = merge(tmp_path, [metadata], [observation])
    assert replay["replayed_observations"] == 1 and not replay["changed"]
    assert wechat_archive_path(tmp_path).read_bytes() == before


def test_old_locator_only_quote_projects_without_rewriting_or_new_reference_identity():
    parsed, reply, _ = quoted_forward()
    provenance = {
        "message_type": 49,
        "text_extraction_kind": "quoted_reply",
        "locator": {
            "text_components": parsed["text_components"],
            "appmsg_subtype": 57,
        },
    }
    before = deepcopy(provenance)
    presentation = _native_quoted_reply_presentation(provenance)
    assert presentation["display_text"] == reply
    quote = presentation["quoted_reply"]
    assert quote["speaker"] == "Forwarding participant"
    assert quote["reference"]["source_sender_id"] == "wxid_forwarder"
    assert "sender_id" not in quote and "contact_id" not in quote
    assert provenance == before


def test_native_plain_text_quote_keeps_current_and_quoted_speakers_separate(tmp_path):
    metadata = source()
    envelope = (
        "<msg><appmsg><type>57</type><title>Current reply</title>"
        "<refermsg><type>1</type><fromusr>wxid_earlier</fromusr>"
        "<displayname>Earlier participant</displayname>"
        "<content>Quoted &lt;img src=x&gt; text</content>"
        "</refermsg></appmsg></msg>"
    )
    parsed = extract_wechat_text(49, envelope)
    merge(tmp_path, [metadata], [record(metadata, message_type=49, **parsed)])
    page = detail(tmp_path)
    assert len(page.items) == 1
    message = page.items[0]
    assert message.author_label == "Synthetic sender"
    assert message.messaging["display_text"] == "Current reply"
    assert message.messaging["quoted_reply"]["speaker"] == "Earlier participant"
    assert message.messaging["quoted_reply"]["text"] == "Quoted <img src=x> text"
    assert (
        message.messaging["quoted_reply"]["reference"]["source_sender_id"]
        == "wxid_earlier"
    )
    assert message.content_text == parsed["text"]


def test_index_recovered_reply_retains_user_confirmation_and_unavailable_quote(
    tmp_path,
):
    metadata = source()
    reply = "Recovered reply, punctuation may differ"
    reference = {
        "original_message_type": 49,
        "relationship": "forwarded_conversation",
        "authority": "user_confirmation",
        "content_unavailable": True,
        "source_sender_id": "wxid_forwarder",
    }
    observation = record(
        metadata,
        text=reply,
        message_type=49,
        text_extraction_kind="quoted_reply",
        text_components=[{"kind": "reply_text", "text": reply}],
        quoted_reference=reference,
        flags=["index_recovered", "punctuation_normalized", "missing_dictionary_5"],
    )
    merge(tmp_path, [metadata], [observation])
    page = detail(tmp_path)
    assert len(page.items) == 1
    message = page.items[0]
    assert message.content_text == message.messaging["display_text"] == reply
    quote = message.messaging["quoted_reply"]
    assert quote["speaker"] == "Quoted forwarded conversation (user-confirmed)"
    assert quote["text"] == "Original quoted content is unavailable."
    assert quote["reference"] == reference
    assert "source_ids" not in quote["reference"]
    assert message.messaging["quality_flags"] == tuple(observation["flags"])
    assert "Original quoted content is unavailable." not in message.content_text
    assert "user_confirmation" in build_chat_history_markdown(page)


def test_missing_native_quote_body_has_explicit_placeholder_without_a_target_id():
    provenance = {
        "message_type": 49,
        "text_extraction_kind": "quoted_reply",
        "text_components": [{"kind": "reply_text", "text": "Reply"}],
        "quoted_reference": {"original_message_type": 3, "content_unavailable": True},
    }
    result = _native_quoted_reply_presentation(provenance)
    assert result["quoted_reply"]["speaker"] == "Quoted message"
    assert result["quoted_reply"]["text"] == "Original quoted content is unavailable."
    assert "source_ids" not in result["quoted_reply"]["reference"]


@pytest.mark.parametrize("reference_in_locator", [False, True])
@pytest.mark.parametrize("components_in_locator", [False, True])
@pytest.mark.parametrize("quoted_kind", ["quoted_text", "quoted_forwarded_text"])
def test_unavailable_quote_rejects_body_components_and_old_rows_stay_literal(
    tmp_path, reference_in_locator, components_in_locator, quoted_kind
):
    reference = {
        "original_message_type": 49,
        "relationship": "forwarded_conversation",
        "authority": "user_confirmation",
        "content_unavailable": True,
    }
    components = [
        {"kind": "reply_text", "text": "Actual reply"},
        {"kind": quoted_kind, "text": "Unconfirmed quoted body"},
    ]
    top, locator = {}, {}
    (locator if reference_in_locator else top)["quoted_reference"] = reference
    (locator if components_in_locator else top)["text_components"] = components
    with pytest.raises(ValueError, match="Unavailable native quoted content conflicts"):
        native_text_metadata(top, locator)
    provenance = {
        "message_type": 49,
        "text_extraction_kind": "quoted_reply",
        **top,
        "locator": locator,
    }
    assert _native_quoted_reply_presentation(provenance) == {}
    metadata = source()
    literal = "Original literal reply and quote remain unchanged"
    merge(tmp_path, [metadata], [record(metadata, text=literal)])
    with sqlite3.connect(wechat_archive_path(tmp_path)) as connection:
        connection.execute(
            "UPDATE messages SET provenance=?", (json.dumps(provenance),)
        )
    before = wechat_archive_path(tmp_path).read_bytes()
    message = detail(tmp_path).items[0]
    assert message.content_text == message.messaging["display_text"] == literal
    assert "quoted_reply" not in message.messaging
    assert wechat_archive_path(tmp_path).read_bytes() == before


@pytest.mark.parametrize("legacy_locator", [False, True])
def test_unavailable_quote_accepts_reply_only_evidence(legacy_locator):
    fields = {
        "text_components": [{"kind": "reply_text", "text": "Actual reply"}],
        "quoted_reference": {
            "original_message_type": 49,
            "relationship": "forwarded_conversation",
            "authority": "user_confirmation",
            "content_unavailable": True,
        },
    }
    top, locator = ({}, fields) if legacy_locator else (fields, {})
    assert native_text_metadata(top, locator) == fields
    presentation = _native_quoted_reply_presentation(
        {
            "message_type": 49,
            "text_extraction_kind": "quoted_reply",
            **top,
            "locator": locator,
        }
    )
    assert presentation["display_text"] == "Actual reply"
    assert (
        presentation["quoted_reply"]["speaker"]
        == "Quoted forwarded conversation (user-confirmed)"
    )
    assert (
        presentation["quoted_reply"]["text"]
        == "Original quoted content is unavailable."
    )


def test_later_structured_observation_never_rewrites_existing_message(tmp_path):
    metadata = source()
    parsed, _, _ = quoted_forward()
    old = record(
        metadata,
        text=parsed["text"],
        message_type=49,
        text_extraction_kind="quoted_reply",
    )
    merge(tmp_path, [metadata], [old])
    before = rows(tmp_path)
    richer = record(metadata, message_type=49, **parsed)
    result = merge(tmp_path, [metadata], [richer])
    assert result["inserted_messages"] == 0
    assert result["new_evidence"] == 1 and result["old_messages_unchanged"]
    assert rows(tmp_path) == before
    evidence = json.loads(rows(tmp_path, "message_evidence")[0][5])
    assert evidence["quoted_reference"] == parsed["quoted_reference"]
    assert evidence["text_components"] == parsed["text_components"]


@pytest.mark.parametrize(
    "field", ["text_components", "quoted_reference", "appmsg_subtype"]
)
def test_conflicting_top_level_and_locator_claims_reject_atomically(tmp_path, field):
    metadata = source()
    parsed, _, _ = quoted_forward()
    merge(tmp_path, [metadata], [record(metadata)])
    before = wechat_archive_path(tmp_path).read_bytes()
    observation = record(metadata, "quote", message_type=49, **parsed)
    conflicting = {
        "text_components": [{"kind": "reply_text", "text": "Conflicting reply"}],
        "quoted_reference": {"source_sender_id": "wxid_other"},
        "appmsg_subtype": 19,
    }
    observation["source_locator"][field] = conflicting[field]
    with pytest.raises(ValueError, match="conflicts with its source locator"):
        merge(tmp_path, [metadata], [observation])
    assert wechat_archive_path(tmp_path).read_bytes() == before
    malformed = {**observation, "locator": observation["source_locator"]}
    assert _native_quoted_reply_presentation(malformed) == {}


def test_equal_duplicate_declarations_preserve_exact_metadata():
    parsed, _, _ = quoted_forward()
    locator = {
        key: deepcopy(parsed[key])
        for key in ("text_components", "quoted_reference", "appmsg_subtype")
    }
    assert native_text_metadata(parsed, locator) == locator


@pytest.mark.parametrize(
    "metadata",
    [
        {"text_components": "not a list"},
        {"text_components": ["not an object"]},
        {"text_components": [{"kind": [], "text": "Reply"}]},
        {"text_components": [{"kind": "reply_text", "text": 1}]},
        {"quoted_reference": []},
        {"quoted_reference": {"source_sender_id": 123}},
        {"quoted_reference": {"source_ids": {"svrid": 1.5}}},
        {"quoted_reference": {"original_message_type": True}},
        {"quoted_reference": {"content_unavailable": "yes"}},
        {"appmsg_subtype": "57"},
        {"appmsg_subtype": True},
        {"quoted_reference": {"source_ids": {"svrid": 2**300}}},
        {"quoted_reference": {"source_sender_label": "x" * (2 * 1024 * 1024 + 1)}},
        {"text_components": [{"kind": "reply_text", "text": "x"}] * 1_001},
        {"quoted_reference": {"unknown": {"nested": {"invalid": object()}}}},
    ],
)
def test_invalid_or_oversized_native_metadata_is_rejected(metadata):
    with pytest.raises(ValueError):
        native_text_metadata(metadata, {})
    provenance = {
        "message_type": 49,
        "text_extraction_kind": "quoted_reply",
        **metadata,
    }
    assert _native_quoted_reply_presentation(provenance) == {}


def test_deep_and_cyclic_metadata_are_bounded():
    deep = {}
    cursor = deep
    for _ in range(14):
        cursor["child"] = {}
        cursor = cursor["child"]
    cyclic = {}
    cyclic["child"] = cyclic
    for value in (deep, cyclic):
        with pytest.raises(ValueError, match="supported structure"):
            native_text_metadata({"quoted_reference": value}, {})


def test_quote_reference_cannot_disagree_with_forwarder_component():
    parsed, _, _ = quoted_forward()
    parsed["quoted_reference"]["source_sender_id"] = "wxid_different_forwarder"
    with pytest.raises(ValueError, match="source evidence conflicts"):
        native_text_metadata(parsed, {})


def test_quote_evidence_never_crosses_accounts_or_creates_a_snippet_message(tmp_path):
    first, second = source("wxid_account_a"), source("wxid_account_b")
    parsed, reply, _ = quoted_forward()
    result = merge(
        tmp_path,
        [first, second],
        [
            record(first, message_type=49, **parsed),
            record(second, message_type=49, **parsed),
        ],
    )
    assert result["inserted_messages"] == result["messages_after"] == 2
    for metadata in (first, second):
        page = detail(tmp_path, account="account:" + metadata["source_account_id"])
        assert page.total_count == len(page.items) == 1
        assert page.items[0].messaging["account_id"] == metadata["source_account_id"]
        assert page.items[0].messaging["display_text"] == reply
    before = wechat_archive_path(tmp_path).read_bytes()
    crossing = record(first, "crossing", message_type=49, **parsed)
    crossing["source_account_id"] = second["source_account_id"]
    with pytest.raises(ValueError, match="crosses its verified source account"):
        merge(tmp_path, [first], [crossing])
    assert wechat_archive_path(tmp_path).read_bytes() == before


@pytest.mark.parametrize(
    "provenance",
    [
        {"message_type": 1, "text_extraction_kind": "plain_text"},
        {"message_type": 49, "text_extraction_kind": "forwarded_text"},
        {
            "message_type": 49,
            "text_extraction_kind": "quoted_reply",
            "text_components": [],
        },
    ],
)
def test_nonquote_and_unproven_roles_keep_the_existing_presentation(provenance):
    assert _native_quoted_reply_presentation(provenance) == {}
