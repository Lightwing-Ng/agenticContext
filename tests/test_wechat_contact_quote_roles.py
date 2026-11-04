"""Native room/author quote roles require original conversation evidence.

Code version: v1.0.1-codex.0
"""

from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from app.core import wechat_contact_embedded as embedded
from app.core.wechat_text import native_components_text


OWNER = "wxid_quote_owner"
TARGET = "wxid_synthetic_quote_author"
OTHER = "wxid_quote_replying_author"
ROOM = "91000000001@chatroom"
OTHER_ROOM = "91000000002@chatroom"
PARENT = "a" * 64
BODY = "合成测试消息：保留原始引文\n  Preserve literal spacing."
REPLY = "The replying author's separate statement."


def _quote(*, sender=ROOM, chat=TARGET):
    reference = {
        "source_sender_id": sender,
        "source_chat_id": chat,
        "source_sender_label": "Historical nickname",
        "original_message_type": 1,
        "source_timestamp": "0",
        "source_ids": {"svrid": "910000000000000001"},
    }
    return {
        "message_type": 49,
        "appmsg_subtype": 57,
        "text_extraction_kind": "quoted_reply",
        "source_time": "2024-01-02 03:04:05",
        "quoted_reference": reference,
        "text_components": [
            {"kind": "reply_text", "text": REPLY},
            {"kind": "quoted_text", "text": BODY, **reference},
        ],
    }


def _nested(*, chat, grouped=False):
    quote = _quote()
    component = {
        "kind": "forwarded_quote",
        "text": native_components_text(quote["text_components"]),
        "text_components": quote["text_components"],
        "quoted_reference": quote["quoted_reference"],
        "appmsg_subtype": 57,
        "source_sender_id": OTHER,
        "source_chat_id": chat,
        "source_create_time": "1704164645",
    }
    children = [component]
    if grouped:
        children = [
            {
                "kind": "forwarded_group",
                "text": native_components_text(children),
                "text_components": children,
            }
        ]
    return {
        "message_type": 49,
        "appmsg_subtype": 19,
        "text_extraction_kind": "forwarded_text",
        "text_components": children,
    }


def _details(proof, *, peer=ROOM, account=OWNER, parent=PARENT):
    return list(
        embedded.native_details(
            proof, parent_id=parent, account_id=account, peer_id=peer
        )
    )


def test_matching_native_room_recovers_actual_author_and_original_chat():
    proof = _quote()
    original = deepcopy(proof)
    (detail,) = _details(proof)
    assert detail["sender_id"] == TARGET
    assert detail["original_chat_id"] == ROOM
    assert detail["original_conversation_unknown"] is False
    assert detail["sender_label"] == "Historical nickname"
    assert detail["text"] == BODY
    assert REPLY not in detail["text"]
    assert detail["path"] == ["quote", 2]
    assert detail["id"] == PARENT + "_quoted_2"
    assert detail["anchor_id"] == "wechat_message_" + PARENT
    assert detail["source_ids"] == proof["quoted_reference"]["source_ids"]
    assert detail["timestamp_value"] == ""
    assert detail["source_clock"] == "0"
    assert detail["time_basis"] == "Unavailable source time"
    assert "possible_epoch_zero_placeholder" in detail["quality_flags"]
    assert embedded.native_detail(
        proof, ["quote", 2], parent_id=PARENT, account_id=OWNER, peer_id=ROOM
    ) == detail
    assert proof == original


@pytest.mark.parametrize("peer", ["", OTHER_ROOM, TARGET, None])
def test_reversed_roles_require_the_actual_parent_room(peer):
    proof = _quote()
    original = deepcopy(proof)
    assert _details(proof, peer=peer) == []
    assert embedded.native_detail(
        proof, ["quote", 2], parent_id=PARENT, account_id=OWNER, peer_id=peer
    ) is None
    assert proof == original


@pytest.mark.parametrize("chat", ["", "PublicAlias", ROOM, TARGET + " "])
def test_room_reference_cannot_infer_author_from_name_or_ambiguous_chat(chat):
    proof = _quote(chat=chat)
    for reference in (proof["quoted_reference"], proof["text_components"][1]):
        reference["source_sender_label"] = TARGET
    original = deepcopy(proof)
    assert _details(proof) == []
    assert proof == original


@pytest.mark.parametrize("grouped", [False, True])
def test_nested_quote_uses_explicit_original_room_without_receiving_room(grouped):
    proof = _nested(chat=ROOM, grouped=grouped)
    original = deepcopy(proof)
    details = _details(proof, peer=OTHER_ROOM)
    quoted = next(detail for detail in details if detail["reference_kind"] == "quoted_text")
    replying = next(detail for detail in details if detail["reference_kind"] == "native_forwarded")
    position = [1, 1] if grouped else [1]
    assert quoted["path"] == ["native_quote", *position, 2]
    assert quoted["sender_id"] == TARGET
    assert quoted["original_chat_id"] == ROOM
    assert quoted["text"] == BODY
    assert quoted["timestamp_value"] == ""
    assert replying["sender_id"] == OTHER
    assert replying["text"] == REPLY
    assert embedded.native_detail(
        proof,
        quoted["path"],
        parent_id=PARENT,
        account_id=OWNER,
        peer_id=OTHER_ROOM,
    ) == quoted
    assert proof == original


@pytest.mark.parametrize("chat", ["", OTHER_ROOM, TARGET])
def test_nested_quote_cannot_borrow_receiving_room_when_original_context_is_missing(chat):
    proof = _nested(chat=chat, grouped=True)
    original = deepcopy(proof)
    details = _details(proof, peer=ROOM)
    assert [(detail["sender_id"], detail["text"]) for detail in details] == [
        (OTHER, REPLY)
    ]
    assert proof == original


def test_ordinary_forwarded_sender_is_not_reinterpreted_as_quote_author():
    proof = {
        "message_type": 49,
        "appmsg_subtype": 19,
        "text_extraction_kind": "forwarded_text",
        "text_components": [
            {
                "kind": "forwarded_text",
                "text": BODY,
                "source_sender_id": ROOM,
                "source_chat_id": TARGET,
            }
        ],
    }
    original = deepcopy(proof)
    (detail,) = _details(proof)
    assert detail["sender_id"] == ROOM
    assert detail["original_chat_id"] == TARGET
    assert proof == original


def test_legacy_native_username_and_explicit_chat_keep_existing_assertion():
    proof = _quote(sender="legacy_native_username", chat="explicit-original@chatroom")
    (detail,) = _details(proof, peer="")
    assert detail["sender_id"] == "legacy_native_username"
    assert detail["original_chat_id"] == "explicit-original@chatroom"


@pytest.mark.parametrize(
    "damage", ["missing_owner", "invalid_parent", "wrong_subtype", "forged_reference"]
)
def test_role_resolution_does_not_bypass_native_envelope_validation(damage):
    proof = _quote()
    account, parent = OWNER, PARENT
    if damage == "missing_owner":
        account = ""
    elif damage == "invalid_parent":
        parent = "not-a-native-parent-id"
    elif damage == "wrong_subtype":
        proof["appmsg_subtype"] = 19
    else:
        proof["quoted_reference"]["source_chat_id"] = OTHER
    assert _details(proof, account=account, parent=parent) == []


def test_candidate_index_rebuilds_old_room_only_cache_and_preserves_source(tmp_path):
    history = tmp_path / "source.sqlite3"
    directory = tmp_path / "cache"
    directory.mkdir()
    proof = _quote()
    with sqlite3.connect(history) as connection:
        connection.execute("CREATE TABLE messages(id TEXT PRIMARY KEY,provenance TEXT)")
        connection.execute("INSERT INTO messages VALUES(?,?)", (PARENT, json.dumps(proof)))
    original = history.read_bytes()
    old_fingerprint = hashlib.sha256(
        json.dumps(
            ["v1.0.0-codex.0", str(history.resolve()), embedded._identity(history)]
        ).encode()
    ).hexdigest()
    with sqlite3.connect(directory / "native-author-candidates.sqlite3") as connection:
        connection.executescript(
            "CREATE TABLE candidates(author TEXT,parent_id TEXT,PRIMARY KEY(author,parent_id)) WITHOUT ROWID;"
            "CREATE TABLE info(key TEXT PRIMARY KEY,value TEXT) WITHOUT ROWID;"
        )
        connection.execute("INSERT INTO candidates VALUES(?,?)", (ROOM, PARENT))
        connection.execute("INSERT INTO info VALUES('fingerprint',?)", (old_fingerprint,))
    assert list(embedded.native_candidate_ids(history, directory, TARGET)) == [PARENT]
    assert list(embedded.native_candidate_ids(history, directory, ROOM)) == [PARENT]
    assert embedded.native_quote_roles(proof["quoted_reference"]) is None
    assert embedded.native_quote_roles(proof["quoted_reference"], peer_id=ROOM) == (
        TARGET,
        ROOM,
    )
    assert history.read_bytes() == original
