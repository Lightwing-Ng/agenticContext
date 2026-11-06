"""Incomplete native quotes need independent occurrence and reference evidence.

Code version: v1.0.0-codex.0
"""

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib

import pytest

from app.core.wechat_contacts import canonical_contact_id
from app.core.wechat_incomplete_quotes import MATCH_BASIS, bridge_incomplete_quotes


OWNER = "wxid_incompleteowner"
PEER = "wxid_incompletepeer"
OTHER = "wxid_incompleteother"
ORIGINAL = "wxid_incompleteoriginal"
ROOM = "123456789@chatroom"
HTML = ("html-incomplete", "html-snapshot")
NATIVE = ("ios-incomplete", "native-snapshot")
SECOND = ("ios-incomplete-second", "second-snapshot")


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _canonical(sender):
    return canonical_contact_id(sender, authority="native_message_sender")


def _item(form, order, sender, *, body=None):
    native = form == "native"
    source = NATIVE if native else HTML
    clock = (datetime(2021, 7, 22, 22) + timedelta(seconds=order)).isoformat()
    identifier = f"{form}-{order}"
    author = _canonical(sender)
    return {
        "id": identifier,
        "format": form,
        "source_key": source,
        "order": order + (1000 if native else 0),
        "key": (
            _hash(body or f"Distinct original body {order}"),
            clock,
            "sent" if sender == OWNER else "received",
        ),
        "message_type": 1,
        "author": author,
        "server": f"1000000000000000{order}" if native else "",
        "epoch": str(int(datetime.fromisoformat(clock + "+08:00").timestamp()))
        if native
        else None,
        "rank": 0 if native else 2,
        "context": {
            "id": identifier,
            "source_id": source[0],
            "source_snapshot_id": source[1],
            "contact_id": author,
            "sender_id": sender if native else "alias-" + sender,
            "sender_label": "Original source label",
        },
    }


@pytest.fixture
def sequence():
    candidates, mapping, members = [], {}, {}
    for order, sender in ((1, OWNER), (20, PEER), (21, OTHER), (24, PEER)):
        left, right = _item("html", order, sender), _item("native", order, sender)
        candidates.extend((left, right))
        mapping[left["id"]] = right["id"]
        members[right["id"]] = [right["context"], left["context"]]
    original = _item("native", 19, ORIGINAL, body="Original quoted reply")
    original["message_type"] = 49
    original["quote"] = {
        "reply_hash": original["key"][0],
        "text_hash": _hash("A deeper original quote"),
        "speaker": "Older display label",
    }
    original["key"] = (
        (
            "quoted_reply",
            original["quote"]["reply_hash"],
            original["quote"]["text_hash"],
        ),
        *original["key"][1:],
    )
    left, right = (
        _item("html", 23, PEER, body="Reply body"),
        _item("native", 23, PEER, body="Reply body"),
    )
    for item in (left, right):
        item["message_type"] = 49
        item["quote"] = {
            "reply_hash": item["key"][0],
            "text_hash": ""
            if item["format"] == "native"
            else original["quote"]["reply_hash"],
            "speaker": "Historical quoted name",
        }
        item["key"] = (
            ("quoted_reply", item["quote"]["reply_hash"], item["quote"]["text_hash"]),
            *item["key"][1:],
        )
    right["quote"].update(
        unavailable=True,
        original_message_type=49,
        reference_signature=(49, ROOM, ORIGINAL, original["server"]),
        server=original["server"],
        author="",
        group_room=ROOM,
        reference_sender_id=ROOM,
        reference_chat_id=ORIGINAL,
    )
    candidates.extend((original, left, right))
    current = {
        "kind": "group",
        "account_id": OWNER,
        "account_key": "account:" + OWNER,
        "peer_id": ROOM,
    }
    return candidates, current, mapping, members


def _records(sequence):
    return {item["id"]: item for item in sequence[0]}


def _photo(sequence):
    records = _records(sequence)
    left, right = records["html-23"], records["native-23"]
    left["quote"]["text_hash"] = _hash("[Photo]")
    left["key"] = (
        ("quoted_reply", left["quote"]["reply_hash"], left["quote"]["text_hash"]),
        *left["key"][1:],
    )
    right["quote"].update(
        original_message_type=3,
        reference_signature=(3, ROOM, ORIGINAL, "9000000000000000001"),
        server="9000000000000000001",
    )
    sequence[0].remove(records["native-19"])


def _change_sender(item, sender):
    item["author"] = _canonical(sender)
    item["context"]["contact_id"] = item["author"]
    item["context"]["sender_id"] = (
        sender if item["format"] == "native" else "alias-" + sender
    )


@pytest.mark.parametrize("original_type", [1, 49])
def test_exact_native_original_and_verified_neighbors_prefer_complete_html(
    sequence, original_type
):
    records = _records(sequence)
    if original_type == 1:
        original, right = records["native-19"], records["native-23"]
        original["message_type"] = 1
        original["key"] = (original["quote"]["reply_hash"], *original["key"][1:])
        original.pop("quote")
        right["quote"]["original_message_type"] = 1
        right["quote"]["reference_signature"] = (1, ROOM, ORIGINAL, original["server"])
    before = deepcopy(sequence)
    mapping, members = bridge_incomplete_quotes(*sequence)
    assert mapping == {**sequence[2], "native-23": "html-23"}
    assert set(members) == set(sequence[3]) | {"html-23"}
    for preferred, contexts in sequence[3].items():
        assert members[preferred] == contexts
    contexts = members["html-23"]
    assert [row["id"] for row in contexts] == ["html-23", "native-23"]
    assert all(row["quote_match_basis"] == MATCH_BASIS for row in contexts)
    assert all(len(row["quote_match_anchor_ids"]) == 3 for row in contexts)
    assert contexts[1]["quoted_reference_server_id"] == records["native-19"]["server"]
    assert contexts[1]["quoted_reference_type"] == original_type
    assert sequence == before


def test_photo_metadata_needs_no_missing_media_original_or_invented_fields(sequence):
    _photo(sequence)
    before = deepcopy(sequence)
    mapping, members = bridge_incomplete_quotes(*sequence)
    assert mapping["native-23"] == "html-23"
    assert "native-19" not in _records(sequence)
    for context in members["html-23"]:
        assert "quoted_original_id" not in context
        assert "quoted_contact_id" not in context
        assert "quoted_timestamp" not in context
    assert sequence == before


def test_owner_can_be_global_while_local_bracketing_authors_are_independent(sequence):
    mapping, members = bridge_incomplete_quotes(*sequence)
    assert mapping["native-23"] == "html-23"
    assert members["html-23"][0]["quote_match_anchor_ids"] == [
        "native-20",
        "native-21",
        "native-24",
    ]


@pytest.mark.parametrize(
    "damage",
    [
        "private",
        "wrong_account",
        "wrong_room",
        "missing_owner",
        "html_author",
        "native_author",
        "native_epoch",
        "html_epoch",
        "reply_hash",
        "reference_server",
        "reference_zero",
        "reference_sender_room",
        "reference_chat",
        "original_type",
        "original_author",
        "original_late",
        "original_body",
        "available_native",
        "empty_html_quote",
        "unverified_html",
        "no_anchors",
        "two_local_anchors",
        "one_local_author",
        "missing_global_owner",
        "crossed_anchor_order",
        "duplicate_anchor_order",
        "target_order",
        "invalid_clock",
        "reference_signature",
    ],
)
def test_uncertainty_or_conflict_preserves_both_occurrences(sequence, damage):
    records = _records(sequence)
    left, right, original = (
        records["html-23"],
        records["native-23"],
        records["native-19"],
    )
    if damage == "private":
        sequence[1]["kind"] = "direct"
    elif damage == "wrong_account":
        sequence[1]["account_key"] = "account:wxid_otherowner"
    elif damage == "wrong_room":
        sequence[1]["peer_id"] = "987654321@chatroom"
    elif damage == "missing_owner":
        sequence[1]["account_id"] = ""
    elif damage == "html_author":
        _change_sender(left, OTHER)
    elif damage == "native_author":
        _change_sender(right, OTHER)
    elif damage == "native_epoch":
        right["epoch"] = "NaN"
    elif damage == "html_epoch":
        left["epoch"] = str(int(right["epoch"]) + 1)
    elif damage == "reply_hash":
        right["quote"]["reply_hash"] = _hash("Different reply")
    elif damage == "reference_server":
        right["quote"]["server"] = "8888888888888888888"
        right["quote"]["reference_signature"] = (
            49,
            ROOM,
            ORIGINAL,
            right["quote"]["server"],
        )
    elif damage == "reference_zero":
        right["quote"]["server"] = "0"
        right["quote"]["reference_signature"] = (49, ROOM, ORIGINAL, "0")
    elif damage == "reference_sender_room":
        other_room = "987654321@chatroom"
        right["quote"]["reference_sender_id"] = other_room
        right["quote"]["reference_signature"] = (
            49,
            other_room,
            ORIGINAL,
            original["server"],
        )
    elif damage == "reference_chat":
        right["quote"]["reference_chat_id"] = OTHER
        right["quote"]["reference_signature"] = (49, ROOM, OTHER, original["server"])
    elif damage == "original_type":
        original["message_type"] = 1
    elif damage == "original_author":
        _change_sender(original, OTHER)
    elif damage == "original_late":
        original["order"] = right["order"] + 1
    elif damage == "original_body":
        original["quote"]["reply_hash"] = _hash("Different original reply")
    elif damage == "available_native":
        right["quote"]["unavailable"] = False
    elif damage == "empty_html_quote":
        left["quote"]["text_hash"] = ""
    elif damage == "unverified_html":
        left["author"] = left["context"]["contact_id"] = ""
    elif damage == "no_anchors":
        sequence[2].clear()
        sequence[3].clear()
    elif damage == "two_local_anchors":
        sequence[2].pop("html-24")
        sequence[3].pop("native-24")
    elif damage == "one_local_author":
        _change_sender(records["html-21"], PEER)
        _change_sender(records["native-21"], PEER)
    elif damage == "missing_global_owner":
        _change_sender(records["html-1"], PEER)
        _change_sender(records["native-1"], PEER)
    elif damage == "crossed_anchor_order":
        records["native-21"]["order"] = records["native-24"]["order"] + 1
    elif damage == "duplicate_anchor_order":
        records["native-21"]["order"] = records["native-20"]["order"]
    elif damage == "target_order":
        right["order"] = records["native-24"]["order"] + 1
    elif damage == "invalid_clock":
        left["key"] = (left["key"][0], "2021-07-22T22:00", left["key"][2])
    elif damage == "reference_signature":
        right["quote"]["reference_signature"] = (49, ROOM, ORIGINAL, "different")
    before = deepcopy(sequence)
    mapping, members = bridge_incomplete_quotes(*sequence)
    assert mapping == sequence[2]
    assert members == sequence[3]
    assert sequence == before


@pytest.mark.parametrize("damage", ["speaker", "marker", "missing_label", "type"])
def test_photo_does_not_treat_arbitrary_missing_quote_as_image(sequence, damage):
    _photo(sequence)
    records = _records(sequence)
    left, right = records["html-23"], records["native-23"]
    if damage == "speaker":
        left["quote"]["speaker"] = "A different old name"
    elif damage == "marker":
        left["quote"]["text_hash"] = _hash("[Image]")
    elif damage == "missing_label":
        right["quote"]["speaker"] = ""
    elif damage == "type":
        right["quote"]["original_message_type"] = 49
        right["quote"]["reference_signature"] = (
            49,
            ROOM,
            ORIGINAL,
            right["quote"]["server"],
        )
    before = deepcopy(sequence)
    assert bridge_incomplete_quotes(*sequence) == (sequence[2], sequence[3])
    assert sequence == before


def test_unverified_same_source_reply_is_still_negative_ambiguity_evidence(sequence):
    duplicate = deepcopy(_records(sequence)["html-23"])
    duplicate["id"] = duplicate["context"]["id"] = "unverified-html-repeat"
    duplicate["order"] += 100
    duplicate["author"] = duplicate["context"]["contact_id"] = ""
    duplicate["context"]["sender_id"] = "unresolved-alias"
    sequence[0].append(duplicate)
    before = deepcopy(sequence)
    assert bridge_incomplete_quotes(*sequence) == (sequence[2], sequence[3])
    assert sequence == before


@pytest.mark.parametrize("damage", ["different_server", "same_server", "original"])
def test_same_source_duplicate_or_different_native_event_is_not_collapsed(
    sequence, damage
):
    identifier = "native-19" if damage == "original" else "native-23"
    duplicate = deepcopy(_records(sequence)[identifier])
    duplicate["id"] = duplicate["context"]["id"] = "another-native-occurrence"
    duplicate["order"] += 1
    if damage == "different_server":
        duplicate["server"] = "8000000000000000001"
    sequence[0].append(duplicate)
    assert bridge_incomplete_quotes(*sequence) == (sequence[2], sequence[3])


def _second_native_source(sequence):
    additions = []
    for original in list(sequence[0]):
        if original["format"] != "native":
            continue
        item = deepcopy(original)
        item["id"] += "-second"
        item["context"].update(
            id=item["id"], source_id=SECOND[0], source_snapshot_id=SECOND[1]
        )
        item["source_key"] = SECOND
        item["order"] += 5000
        additions.append(item)
        if original["id"] in sequence[3]:
            sequence[3][original["id"]].append(item["context"])
            sequence[2][item["id"]] = original["id"]
    sequence[0].extend(additions)


def test_multiple_native_sources_and_offsets_preserve_every_occurrence(sequence):
    _second_native_source(sequence)
    before = deepcopy(sequence)
    mapping, members = bridge_incomplete_quotes(*sequence)
    assert mapping == {
        **sequence[2],
        "native-23": "html-23",
        "native-23-second": "html-23",
    }
    assert {row["id"] for row in members["html-23"]} == {
        "html-23",
        "native-23",
        "native-23-second",
    }
    assert sequence == before


@pytest.mark.parametrize("damage", ["server", "epoch", "reference"])
def test_cross_source_native_conflicts_veto_bridge(sequence, damage):
    _second_native_source(sequence)
    item = _records(sequence)["native-23-second"]
    if damage == "server":
        item["server"] = "8000000000000000001"
    elif damage == "epoch":
        item["epoch"] = str(int(item["epoch"]) + 1)
    elif damage == "reference":
        quote = item["quote"]
        quote["reference_chat_id"] = OTHER
        quote["reference_signature"] = (49, ROOM, OTHER, quote["server"])
    before = deepcopy(sequence)
    assert bridge_incomplete_quotes(*sequence) == (sequence[2], sequence[3])
    assert sequence == before


def test_existing_complete_native_group_keeps_its_original_preference(sequence):
    records = _records(sequence)
    left, right = records["html-23"], records["native-23"]
    unavailable = deepcopy(right)
    unavailable["id"] += "-incomplete"
    unavailable["context"]["id"] = unavailable["id"]
    unavailable["source_key"] = SECOND
    sequence[0].append(unavailable)
    right["quote"].update(unavailable=False, text_hash=left["quote"]["text_hash"])
    right["key"] = left["key"]
    sequence[2][left["id"]] = right["id"]
    sequence[3][right["id"]] = [right["context"], left["context"]]
    before = deepcopy(sequence)
    assert bridge_incomplete_quotes(*sequence) == (sequence[2], sequence[3])
    assert sequence == before


def test_unrelated_preexisting_source_group_cannot_be_absorbed(sequence):
    records = _records(sequence)
    right = records["native-23"]
    sequence[2][right["id"]] = "native-20"
    sequence[3]["native-20"].append(right["context"])
    before = deepcopy(sequence)
    assert bridge_incomplete_quotes(*sequence) == (sequence[2], sequence[3])
    assert sequence == before
