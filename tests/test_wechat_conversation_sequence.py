"""Bounded event bridges preserve unresolved labels and every source occurrence.

Code version: v1.0.1-codex.0
"""

from copy import deepcopy
import hashlib

import pytest

from app.core.wechat_contacts import canonical_contact_id
from app.core.wechat_conversation_sequence import (
    MATCH_BASIS,
    bridge_unidentified_events,
)


OWNER = "wxid_sequenceowner"
PEER = "wxid_sequencepeer"
UNKNOWN = "wxid_sequenceunknown"
HTML = ("html-sequence", "html-snapshot")
NATIVE = ("ios-sequence", "native-snapshot")
SCOPE = ("Documents/owner/DB/message_3.sqlite", "Chat_synthetic_group")


def _canonical(sender):
    return canonical_contact_id(sender, authority="native_message_sender")


def _pair(local, sender, *, unknown=False):
    key = (
        hashlib.sha256(f"Distinct source message {local}".encode()).hexdigest(),
        f"2019-04-05T20:15:{local - 200:02}",
        "sent" if sender == OWNER else "received",
    )
    values = []
    for form, source, rank in (("html", HTML, 2), ("native", NATIVE, 0)):
        identifier = f"{form}-{local}"
        native = form == "native"
        author = "" if unknown and not native else _canonical(sender)
        context = {
            "id": identifier,
            "source_id": source[0],
            "source_snapshot_id": source[1],
            "contact_id": author,
            "sender_id": sender if native else "",
            "sender_label": sender if unknown else "Original display label",
        }
        values.append(
            {
                "id": identifier,
                "format": form,
                "source_key": source,
                "local_scope": SCOPE if native else ("Group.html",),
                "order": local + (1000 if native else 0),
                "local_id": local,
                "key": key,
                "author": author,
                "server": f"1341796733735{local}" if native else "",
                "epoch": "1554466530" if native else None,
                "rank": rank,
                "context": context,
                **(
                    {"unidentified_native_label": True}
                    if unknown and not native
                    else {}
                ),
            }
        )
    return values


@pytest.fixture
def sequence():
    candidates, mapping, members = [], {}, {}
    for local, sender in ((215, OWNER), (216, PEER), (218, OWNER)):
        left, right = _pair(local, sender)
        candidates.extend((left, right))
        mapping[left["id"]] = right["id"]
        members[right["id"]] = [right["context"], left["context"]]
    candidates.extend(_pair(217, UNKNOWN, unknown=True))
    return candidates, {"kind": "group", "account_id": OWNER}, mapping, members


def _records(sequence):
    return {item["id"]: item for item in sequence[0]}


def test_verified_neighbors_bridge_only_occurrence_without_promoting_identity(sequence):
    before = deepcopy(sequence)
    mapping, members = bridge_unidentified_events(*sequence)
    assert mapping == {**sequence[2], "html-217": "native-217"}
    assert members.keys() == sequence[3].keys() | {"native-217"}
    for preferred, group in sequence[3].items():
        assert members[preferred] == group
    group = members["native-217"]
    assert [item["id"] for item in group] == ["native-217", "html-217"]
    assert group[0] == _records(sequence)["native-217"]["context"]
    assert group[1]["contact_id"] == ""
    assert group[1]["sender_label"] == UNKNOWN
    assert group[1]["event_match_basis"] == MATCH_BASIS
    assert group[1]["event_match_anchor_ids"] == [
        "native-215",
        "native-216",
        "native-218",
    ]
    assert sequence == before


@pytest.mark.parametrize(
    "damage",
    [
        "no_anchors",
        "two_anchors",
        "missing_owner",
        "one_author",
        "label",
        "private_chat",
        "target_local",
        "native_local",
        "anchor_local",
        "far_local",
        "missing_clock",
        "quote",
        "unflagged",
        "existing_contact",
        "already_identified",
        "direction",
        "duplicate_html",
        "duplicate_native",
        "server_conflict",
        "native_source",
        "native_scope",
        "anchor_scope",
        "crossing_order",
        "target_order",
        "explicit_epoch",
        "native_author",
        "duplicate_anchor_local",
    ],
)
def test_insufficient_or_conflicting_sequence_evidence_stays_separate(sequence, damage):
    candidates, current, mapping, members = sequence
    rows = _records(sequence)
    left, right = rows["html-217"], rows["native-217"]
    if damage == "no_anchors":
        mapping.clear()
        members.clear()
    elif damage == "two_anchors":
        mapping.pop("html-218")
        members.pop("native-218")
    elif damage in {"missing_owner", "one_author"}:
        sender = PEER if damage == "missing_owner" else OWNER
        for item in candidates[:6]:
            item["author"] = _canonical(sender)
            item["context"]["sender_id"] = sender if item["format"] == "native" else ""
            item["context"]["contact_id"] = item["author"]
    elif damage == "label":
        left["context"]["sender_label"] = "wxid_somebody_else"
    elif damage == "private_chat":
        current["kind"] = "direct"
    elif damage == "target_local":
        left["local_id"] = None
    elif damage == "native_local":
        right["local_id"] = None
    elif damage == "anchor_local":
        rows["native-218"]["local_id"] = 219
    elif damage == "far_local":
        rows["html-215"]["local_id"] = rows["native-215"]["local_id"] = 200
    elif damage == "missing_clock":
        left["key"] = (left["key"][0], "", "received")
    elif damage == "quote":
        left["quote"] = {}
    elif damage == "unflagged":
        left["unidentified_native_label"] = False
    elif damage == "existing_contact":
        left["context"]["contact_id"] = _canonical(UNKNOWN)
    elif damage == "already_identified":
        left["author"] = _canonical(UNKNOWN)
    elif damage == "direction":
        left["key"] = (*left["key"][:2], "sent")
        right["key"] = left["key"]
    elif damage in {"duplicate_html", "duplicate_native", "server_conflict"}:
        duplicate = deepcopy(left if damage == "duplicate_html" else right)
        duplicate["id"] += "-duplicate"
        duplicate["context"]["id"] = duplicate["id"]
        if damage == "server_conflict":
            duplicate["key"] = ("Conflicting body", *duplicate["key"][1:])
        candidates.append(duplicate)
    elif damage == "native_source":
        right["source_key"] = ("ios-another-source", "another-snapshot")
    elif damage == "native_scope":
        right["local_scope"] = ("Documents/owner/DB/message_4.sqlite", SCOPE[1])
    elif damage == "anchor_scope":
        rows["native-218"]["local_scope"] = (SCOPE[0], "Chat_another_group")
    elif damage == "crossing_order":
        rows["native-216"]["order"] = 1220
    elif damage == "target_order":
        right["order"] = 1215
    elif damage == "explicit_epoch":
        left["epoch"] = "1554466531"
    elif damage == "native_author":
        right["author"] = _canonical(PEER)
    elif damage == "duplicate_anchor_local":
        rows["html-216"]["local_id"] = rows["native-216"]["local_id"] = 215
    before = deepcopy(sequence)
    result = bridge_unidentified_events(*sequence)
    assert result == (mapping, members)
    assert sequence == before


def _second_native_source(sequence, *, different_server=False):
    candidates, _, mapping, members = sequence
    for original in list(candidates):
        if original["format"] != "native":
            continue
        item = deepcopy(original)
        item["id"] += "-second"
        item["rank"] = 1
        item["source_key"] = ("ios-second", "second-snapshot")
        item["context"].update(
            id=item["id"], source_id="ios-second", source_snapshot_id="second-snapshot"
        )
        if different_server and item["local_id"] == 217:
            item["server"] = "987654321"
        candidates.append(item)
        if item["local_id"] != 217:
            mapping[item["id"]] = original["id"]
            members[original["id"]].append(item["context"])


def test_matching_native_sources_retain_every_occurrence_and_native_preference(
    sequence,
):
    _second_native_source(sequence)
    before = deepcopy(sequence)
    mapping, members = bridge_unidentified_events(*sequence)
    assert mapping["html-217"] == "native-217"
    assert mapping["native-217-second"] == "native-217"
    assert {row["id"] for row in members["native-217"]} == {
        "html-217",
        "native-217",
        "native-217-second",
    }
    assert sequence == before


def test_different_servers_in_qualifying_native_sources_veto_the_bridge(sequence):
    _second_native_source(sequence, different_server=True)
    before = deepcopy(sequence)
    assert bridge_unidentified_events(*sequence) == (sequence[2], sequence[3])
    assert sequence == before


def test_conflicting_native_instants_veto_even_without_an_html_timezone(sequence):
    _second_native_source(sequence)
    rows = _records(sequence)
    assert rows["html-217"]["epoch"] is None
    rows["native-217-second"]["epoch"] = "1554495330"
    before = deepcopy(sequence)

    assert bridge_unidentified_events(*sequence) == (sequence[2], sequence[3])
    assert sequence == before


@pytest.mark.parametrize("epoch", [None, "", "invalid", "NaN", "Infinity", "-Infinity"])
def test_native_candidates_require_a_finite_absolute_instant(sequence, epoch):
    _records(sequence)["native-217"]["epoch"] = epoch
    before = deepcopy(sequence)

    assert bridge_unidentified_events(*sequence) == (sequence[2], sequence[3])
    assert sequence == before


def test_equivalent_native_epoch_representations_remain_compatible(sequence):
    _second_native_source(sequence)
    _records(sequence)["native-217-second"]["epoch"] = "1554466530.000"
    before = deepcopy(sequence)

    mapping, members = bridge_unidentified_events(*sequence)

    assert mapping["html-217"] == "native-217"
    assert mapping["native-217-second"] == "native-217"
    assert len(members["native-217"]) == 3
    assert sequence == before


def test_existing_verified_event_group_and_context_metadata_are_retained(sequence):
    candidates, _, mapping, members = sequence
    left = deepcopy(_records(sequence)["html-217"])
    left.update(
        id="another-verified-html",
        source_key=("html-verified", "snapshot"),
        author=_canonical(UNKNOWN),
    )
    left["context"].update(
        id=left["id"], contact_id=_canonical(UNKNOWN), retained_marker="original proof"
    )
    left.pop("unidentified_native_label")
    candidates.append(left)
    mapping[left["id"]] = "native-217"
    members["native-217"] = [
        _records(sequence)["native-217"]["context"],
        left["context"],
    ]
    before = deepcopy(sequence)
    result_mapping, result_members = bridge_unidentified_events(*sequence)
    assert all(result_mapping[key] == value for key, value in mapping.items())
    group = {row["id"]: row for row in result_members["native-217"]}
    assert set(group) == {"native-217", "html-217", left["id"]}
    assert group[left["id"]] == left["context"]
    assert sequence == before
