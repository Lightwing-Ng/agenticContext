"""Incomplete native group references retain evidence without inventing content.

Code version: v1.0.0-codex.0
"""

from copy import deepcopy
import hashlib

import pytest

from app.core import wechat_conversation_quotes as quotes
from app.core.wechat_contacts import canonical_contact_id


ROOM = "24215197347@chatroom"
OTHER_ROOM = "24215197348@chatroom"
AUTHOR = "wxid_original_author"
REPLY_AUTHOR = "wxid_reply_author"
REPLY = "  Exact reply; preserve  two spaces\nand line breaks  "
LABEL = "Historical source remark"
SERVER = "3435701104298019647"


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _data(original_type=3, *, sender=ROOM, chat=AUTHOR, server=SERVER):
    reference = {
        "original_message_type": original_type,
        "source_sender_id": sender,
        "source_chat_id": chat,
        "source_sender_label": LABEL,
        "source_ids": {"svrid": server},
        "source_timestamp": "",
    }
    proof = {
        "message_type": 49,
        "text_extraction_kind": "quoted_reply",
        "appmsg_subtype": 57,
        "quoted_reference": reference,
        "text_components": [{"kind": "reply_text", "text": REPLY}],
        "locator": {},
    }
    row = {
        "kind": "group",
        "peer_id": ROOM,
        "sender_id": REPLY_AUTHOR,
        "text": REPLY,
    }
    return row, proof


@pytest.mark.parametrize("original_type", [1, 3, 49])
@pytest.mark.parametrize("legacy_locator", [False, True])
@pytest.mark.parametrize("server", [SERVER, "-3435701104298019647"])
def test_incomplete_group_reference_preserves_exact_source_evidence(
    original_type, legacy_locator, server
):
    row, proof = _data(original_type, server=server)
    if legacy_locator:
        for key in ("appmsg_subtype", "quoted_reference", "text_components"):
            proof["locator"][key] = proof.pop(key)
    before = deepcopy((row, proof))
    result = quotes.quoted_event(row, proof, native=True)
    assert result == {
        "reply_hash": _hash(REPLY),
        "text_hash": "",
        "author": "",
        "speaker": LABEL,
        "server": server,
        "unavailable": True,
        "original_message_type": original_type,
        "reference_signature": (original_type, ROOM, AUTHOR, server),
        "group_room": ROOM,
        "reference_sender_id": ROOM,
        "reference_chat_id": AUTHOR,
    }
    assert (row, proof) == before


@pytest.mark.parametrize("sender", [AUTHOR, "chenjianustc"])
@pytest.mark.parametrize("chat", [ROOM, AUTHOR, REPLY_AUTHOR])
def test_explicit_native_sender_has_a_canonical_author(sender, chat):
    row, proof = _data(sender=sender, chat=chat)
    before = deepcopy((row, proof))
    result = quotes.quoted_event(row, proof, native=True)
    assert result["author"] == canonical_contact_id(
        sender, authority="native_message_sender"
    )
    assert result["reference_signature"] == (3, sender, chat, SERVER)
    assert result["text_hash"] == ""
    assert (row, proof) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("original_message_type", 0),
        ("original_message_type", 2),
        ("original_message_type", True),
        ("original_message_type", "3"),
        ("source_sender_id", OTHER_ROOM),
        ("source_sender_id", ""),
        ("source_sender_id", "Unknown source name"),
        ("source_sender_id", "原始作者"),
        ("source_sender_id", "wxid_unknwn_123"),
        ("source_sender_id", "wxid_person@openim"),
        ("source_chat_id", OTHER_ROOM),
        ("source_chat_id", ""),
        ("source_chat_id", "Unknown source name"),
        ("source_chat_id", "原始会话"),
        ("source_chat_id", "wxid_unknwn_123"),
        ("source_sender_label", ""),
        ("source_sender_label", "  \n  "),
        ("source_sender_label", None),
        ("source_ids", {}),
        ("source_ids", {"svrid": "0"}),
        ("source_ids", {"svrid": "01"}),
        ("source_ids", {"svrid": "+1"}),
        ("source_ids", {"svrid": "1.0"}),
        ("source_ids", {"svrid": " 1"}),
        ("source_ids", {"svrid": "1 "}),
        ("source_ids", {"svrid": True}),
        ("source_ids", {"svrid": 1}),
        ("source_ids", {"svrid": 1.0}),
    ],
)
def test_malformed_reference_is_rejected_without_mutating_source(field, value):
    row, proof = _data()
    proof["quoted_reference"][field] = value
    before = deepcopy((row, proof))
    try:
        result = quotes.quoted_event(row, proof, native=True)
    except ValueError:
        # The shared native metadata validator retains its stricter JSON schema.
        result = None
    assert result is None
    assert (row, proof) == before


@pytest.mark.parametrize(
    "damage",
    [
        "wrong_subtype",
        "boolean_subtype",
        "no_components",
        "two_replies",
        "other_component",
        "blank_reply",
        "different_body",
        "direct_chat",
        "missing_room",
        "wrong_room",
        "missing_reference",
        "conflicting_locator",
    ],
)
def test_incomplete_branch_requires_exact_reply_shape_and_group_context(damage):
    row, proof = _data()
    if damage == "wrong_subtype":
        proof["appmsg_subtype"] = 19
    elif damage == "boolean_subtype":
        proof["appmsg_subtype"] = True
    elif damage == "no_components":
        proof["text_components"] = []
    elif damage == "two_replies":
        proof["text_components"].append({"kind": "reply_text", "text": REPLY})
    elif damage == "other_component":
        proof["text_components"][0]["kind"] = "forwarded_text"
    elif damage == "blank_reply":
        row["text"] = proof["text_components"][0]["text"] = " \n "
    elif damage == "different_body":
        row["text"] = REPLY + "!"
    elif damage == "direct_chat":
        row["kind"] = "direct"
    elif damage == "missing_room":
        row["peer_id"] = ""
    elif damage == "wrong_room":
        row["peer_id"] = OTHER_ROOM
    elif damage == "missing_reference":
        proof.pop("quoted_reference")
    elif damage == "conflicting_locator":
        proof["locator"]["appmsg_subtype"] = 19
    before = deepcopy((row, proof))
    try:
        result = quotes.quoted_event(row, proof, native=True)
    except ValueError:
        result = None
    assert result is None
    assert (row, proof) == before


def test_complete_native_quote_keeps_existing_text_hash_and_author_semantics():
    row, proof = _data(1, sender=AUTHOR, chat=ROOM)
    body = "Quoted original; preserve  literal spacing."
    proof["text_components"].append(
        {"kind": "quoted_text", "text": body, **deepcopy(proof["quoted_reference"])}
    )
    row["text"] = REPLY + "\n\n" + body
    before = deepcopy((row, proof))
    result = quotes.quoted_event(row, proof, native=True)
    assert result["reply_hash"] == _hash(REPLY)
    assert result["text_hash"] == _hash(body)
    assert result["author"] == canonical_contact_id(
        AUTHOR, authority="native_message_sender"
    )
    assert "unavailable" not in result
    assert (row, proof) == before


def test_original_matching_explicitly_skips_unavailable_references(monkeypatch):
    row, proof = _data()
    quote = quotes.quoted_event(row, proof, native=True)
    original = {
        "id": "a" * 64,
        "format": "native",
        "source_key": ("ios-synthetic", "snapshot-a"),
        "server": SERVER,
        "author": canonical_contact_id(AUTHOR, authority="native_message_sender"),
        "order": 1,
        "key": (_hash("Original body"), "2021-07-22T22:00:00", "received"),
        "context": {"sender_id": AUTHOR},
    }
    incomplete = {
        **original,
        "id": "b" * 64,
        "server": "1234567890123456789",
        "order": 2,
        "quote": quote,
    }

    def must_not_resolve(*args):
        raise AssertionError("An unavailable quote cannot prove an original message.")

    monkeypatch.setattr(quotes, "_original_author_matches", must_not_resolve)
    before = deepcopy((original, incomplete))
    assert quotes.proven_quote_candidates(
        [original, incomplete], {original["id"]: original["id"]}, {}
    ) == []
    assert (original, incomplete) == before
