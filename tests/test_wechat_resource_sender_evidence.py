"""Secondary native evidence fills only exactly matched missing resource authors.

Code version: v1.0.0-codex.0
"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from app.core import wechat_resource_sender_evidence as evidence
from tests import test_wechat_macos_recovery_account_binding as account
from tests import test_wechat_macos_recovery_bank as native


mac_archive = native.mac_archive
bank_archive = native.bank_archive
source_binding = account.source_binding
PRIMARY = "wxid_primary_resource_owner"


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture
def witness(source_binding):
    packet = deepcopy(source_binding.packet)
    room = native.ROOM
    session = "wechat:" + _sha("secondary-resource-session")
    packet.update(
        peer_id=room,
        conversation_kind="group",
        session_id=session,
        peer_proof=native._proof(10, room, is_session=1),
    )
    packet["source"]["table"] = (
        "Msg_" + hashlib.md5(room.encode(), usedforsecurity=False).hexdigest()
    )
    packet["messages"] = [native._message(4, 47, resource="sticker")]
    epoch = packet["messages"][0]["create_time"]
    primary_session = "wechat:" + _sha("primary-resource-session")
    identifier = _sha("original-primary-resource")
    proof = {
        "record_id": identifier,
        "backup_id": _sha("original-backup"),
        "source_relative_path": "AppDomain-com.tencent.xin/Documents/"
        + hashlib.md5(PRIMARY.encode(), usedforsecurity=False).hexdigest()
        + "/DB/message_2.sqlite",
        "source_sha256": _sha("original-native-database"),
        "message_type": 47,
        "server_id": packet["messages"][0]["server_id"],
        "unix_timestamp": epoch,
        "locator": {
            "table": "Chat_"
            + hashlib.md5(room.encode(), usedforsecurity=False).hexdigest(),
            "local_id": 64,
        },
        "resource_reference": {
            "kind": "sticker",
            "account_id": PRIMARY,
            "peer_id": room,
            "unix_timestamp": epoch,
        },
    }
    row = {
        "id": identifier,
        "conversation_id": primary_session,
        "ordinal": 64,
        "sender_id": None,
        "sender_label": "Unknown sender",
        "direction": "received",
        "text": "[Sticker]",
        "time_sort": epoch + 28800,
        "timestamp_value": "2023-11-15T06:13:24+08:00",
        "timestamp_label": "15 Nov 2023 06:13:24",
        "time_basis": "Retained source clock",
        "provenance": json.dumps(proof),
        "quality_flags": '["resource_placeholder","sender_id_unavailable"]',
        "search_text": "[sticker]",
    }
    conversation = {
        "id": primary_session,
        "account_key": "account:" + PRIMARY,
        "peer_id": room,
        "kind": "group",
    }
    return row, conversation, packet


def test_exact_secondary_sender_keeps_original_owner_clock_and_message_bytes(witness):
    row, conversation, packet = witness
    before = deepcopy(witness)
    events = evidence.secondary_native_events(packet)
    result = evidence.recover_native_resource_sender_from_secondary(
        row, conversation, events
    )
    assert result["resolution"]["status"] == "resolved"
    assert result["message"]["sender_id"] == native.MEMBER
    assert result["message"]["sender_label"] == native.MEMBER
    for field in row.keys() - {
        "sender_id",
        "sender_label",
        "quality_flags",
        "provenance",
        "search_text",
    }:
        assert result["message"][field] == row[field]
    proof = json.loads(result["message"]["provenance"])[
        "secondary_source_sender_evidence"
    ]
    assert proof["primary"]["account_id"] == PRIMARY
    assert proof["secondary_source"]["account_id"] == native.OWNER
    assert proof["primary"]["account_id"] != proof["secondary_source"]["account_id"]
    assert proof["secondary_source"]["source"] == packet["source"]
    assert (
        proof["secondary_source"]["sender_proof"]
        == packet["messages"][0]["sender_proof"]
    )
    assert proof["clock_skew_seconds"] == 0
    assert "sender_id_unavailable" not in json.loads(result["message"]["quality_flags"])
    assert "secondary_native_sender_verified" in json.loads(
        result["message"]["quality_flags"]
    )
    assert witness == before


def test_known_sender_and_display_label_are_never_rewritten(witness):
    row, conversation, packet = witness
    row.update(sender_id=native.MEMBER, sender_label="Retained source display name")
    result = evidence.recover_native_resource_sender_from_secondary(
        row, conversation, evidence.secondary_native_events(packet)
    )
    assert result["message"] == row
    assert result["resolution"]["status"] == "known_sender_preserved"
    row["sender_id"] = "wxid_conflicting_known_author"
    with pytest.raises(ValueError, match="known original author"):
        evidence.recover_native_resource_sender_from_secondary(
            row, conversation, evidence.secondary_native_events(packet)
        )


@pytest.mark.parametrize(
    "change", ["record", "owner", "peer", "type", "clock", "source", "table", "ordinal"]
)
def test_original_binding_cannot_be_relaxed_to_match_an_unrelated_event(
    witness, change
):
    row, conversation, packet = witness
    proof = json.loads(row["provenance"])
    if change == "record":
        proof["record_id"] = _sha("another-original-row")
    elif change == "owner":
        proof["resource_reference"]["account_id"] = native.OWNER
    elif change == "peer":
        proof["resource_reference"]["peer_id"] = "another-room@chatroom"
    elif change == "type":
        proof["message_type"] = True
    elif change == "clock":
        proof["resource_reference"]["unix_timestamp"] += 1
    elif change == "source":
        proof["source_relative_path"] = "Documents/unbound-owner/DB/message_2.sqlite"
    elif change == "table":
        proof["locator"]["table"] = "Chat_" + "0" * 32
    else:
        row["ordinal"] += 1
    row["provenance"] = json.dumps(proof)
    with pytest.raises(ValueError):
        evidence.recover_native_resource_sender_from_secondary(
            row, conversation, evidence.secondary_native_events(packet)
        )


@pytest.mark.parametrize("change", ["peer", "server", "type", "clock"])
def test_nearby_or_similarly_timed_native_rows_do_not_supply_a_sender(witness, change):
    row, conversation, packet = witness
    events = list(evidence.secondary_native_events(packet))
    if change == "peer":
        events[0]["peer_id"] = "another-room@chatroom"
    elif change == "server":
        events[0]["server_id"] = "999999"
    elif change == "type":
        events[0]["local_type"] = 3
    else:
        events[0]["create_time"] += 1
    result = evidence.recover_native_resource_sender_from_secondary(
        row, conversation, events
    )
    assert result["message"] == row
    assert result["resolution"]["status"] == "unmatched"


@pytest.mark.parametrize(
    "change",
    [
        "sender_hash",
        "sender_row",
        "peer_hash",
        "source_auth",
        "account",
        "content_hash",
    ],
)
def test_native_identity_and_source_proofs_are_revalidated(witness, change):
    row, conversation, packet = witness
    events = list(evidence.secondary_native_events(packet))
    event = events[0]
    if change == "sender_hash":
        event["sender_proof"]["row_sha256"] = "0" * 64
    elif change == "sender_row":
        event["real_sender_id"] += 1
    elif change == "peer_hash":
        event["peer_proof"]["row_sha256"] = "0" * 64
    elif change == "source_auth":
        event["source"]["authenticated_pages"] = False
    elif change == "account":
        event["account_id"] = PRIMARY
    else:
        event["content_sha256"] = "not-a-digest"
    with pytest.raises(ValueError):
        evidence.recover_native_resource_sender_from_secondary(
            row, conversation, events
        )


def test_exact_duplicate_witness_is_idempotent_but_multiple_native_rows_are_ambiguous(
    witness,
):
    row, conversation, packet = witness
    event = evidence.secondary_native_events(packet)[0]
    result = evidence.recover_native_resource_sender_from_secondary(
        row, conversation, [event, deepcopy(event)]
    )
    assert result["resolution"]["status"] == "resolved"
    other = deepcopy(event)
    other["local_id"] += 1
    with pytest.raises(ValueError, match="one unambiguous"):
        evidence.recover_native_resource_sender_from_secondary(
            row, conversation, [event, other]
        )
    repeated = evidence.recover_native_resource_sender_from_secondary(
        result["message"], conversation, [event]
    )
    assert repeated["message"] == result["message"]


def test_retained_direction_conflict_is_not_corrected_by_guessing(witness):
    row, conversation, packet = witness
    row["direction"] = "sent"
    with pytest.raises(ValueError, match="retained primary direction"):
        evidence.recover_native_resource_sender_from_secondary(
            row, conversation, evidence.secondary_native_events(packet)
        )


def test_one_second_display_matching_does_not_relax_sender_recovery(witness):
    row, conversation, packet = witness
    event = evidence.secondary_native_events(packet)[0]
    event["create_time"] += 1
    event["resource"]["native_time"] += 1
    matched = evidence.match_secondary_native_event(
        row, conversation, [event], max_clock_skew=1
    )
    assert matched["clock_skew_seconds"] == 1
    assert row["time_basis"] == "Retained source clock"
    assert (
        evidence.recover_native_resource_sender_from_secondary(
            row, conversation, [event]
        )["resolution"]["status"]
        == "unmatched"
    )


def test_sender_recovery_performs_no_file_database_or_source_discovery(
    witness, monkeypatch
):
    row, conversation, packet = witness
    events = evidence.secondary_native_events(packet)

    def fail(*args, **kwargs):
        raise AssertionError("Sender recovery must remain free of I/O")

    monkeypatch.setattr(Path, "open", fail)
    monkeypatch.setattr(sqlite3, "connect", fail)
    result = evidence.recover_native_resource_sender_from_secondary(
        row, conversation, events
    )
    assert result["resolution"]["status"] == "resolved"
