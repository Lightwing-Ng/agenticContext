"""The audit builder retains exact source frames without opening media payloads.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from scripts.index_wechat_html_media import projected
from tests import test_wechat_media_placeholders as frames


def _source():
    entry = frames._entry(8)
    header = {
        "member_path": frames.MEMBER,
        "conversation_kind": "direct",
        "declared_async_counts": {"numberOfPages": 1, "data_path": "Chat_files/Data"},
    }
    members = {
        frames.MEMBER: {
            "member_path": frames.MEMBER,
            "sha256": frames.MEMBER_SHA,
            "size": 4096,
        },
    }
    return entry, header, members


def _async_source():
    entry, header, members = _source()
    member = "Synthetic/Chat_files/Data/msg-1.js"
    receipt = {
        "member_path": member,
        "sha256": hashlib.sha256(b"Original asynchronous metadata").hexdigest(),
        "size": 2048,
    }
    members[member] = receipt
    entry["member_sha256"] = receipt["sha256"]
    entry["node"]["source_locator"].update(
        member_path=member, page=1, page_message_ordinal=2
    )
    return entry, header, members


def test_published_frame_identity_survives_clock_type_and_export_id_changes():
    entry, header, members = _source()
    original = deepcopy(entry)
    baseline, _ = projected(entry, header, members, frames.OWNER, frames.PEER)
    changed = deepcopy(entry)
    changed["node"].update(
        raw_timestamp="2024-01-01 08:09:10", type="video", export_msgid="999"
    )
    later, kind = projected(changed, header, members, frames.OWNER, frames.PEER)
    assert len(baseline[0]) == 64 and int(baseline[0], 16) >= 0
    assert baseline[0] == later[0]
    assert kind == "video" and later[6] == "[Video]"
    assert baseline[8] != later[8]
    changed["node"]["source_locator"].update(message_ordinal=9, page_message_ordinal=9)
    next_frame, _ = projected(changed, header, members, frames.OWNER, frames.PEER)
    assert next_frame[0] != baseline[0]
    assert entry == original


def test_asynchronous_frame_uses_its_declared_page_and_own_member_receipt():
    entry, header, members = _async_source()
    row, kind = projected(entry, header, members, frames.OWNER, frames.PEER)
    proof = json.loads(row[11])
    receipt = members[entry["node"]["source_locator"]["member_path"]]
    assert kind == "image" and row[6] == "[Image]"
    assert proof["locator"]["member"] == receipt
    assert proof["media_reference"]["member_sha256"] == receipt["sha256"]
    assert receipt["sha256"] != frames.MEMBER_SHA
    assert proof["locator"]["page"] == 1
    assert proof["locator"]["message_ordinal"] == 8
    assert proof["locator"]["page_message_ordinal"] == 2
    assert proof["media_reference"]["availability"] == "metadata_only"
    assert proof["original_node"] == entry["node"]


@pytest.mark.parametrize(
    "damage", ["undeclared_page", "different_member", "missing_receipt"]
)
def test_asynchronous_event_requires_the_exact_declared_selected_member(damage):
    entry, header, members = _async_source()
    if damage == "undeclared_page":
        header["declared_async_counts"]["numberOfPages"] = 0
    elif damage == "different_member":
        header["declared_async_counts"]["data_path"] = "Different_files/Data"
    else:
        del members[entry["node"]["source_locator"]["member_path"]]
    with pytest.raises(ValueError):
        projected(entry, header, members, frames.OWNER, frames.PEER)


@pytest.mark.parametrize("damage", ["sent_owner", "direct_peer", "avatar_identifier"])
def test_sender_and_direction_cannot_cross_the_original_account_or_direct_peer(damage):
    entry, header, members = _source()
    node = entry["node"]
    other = "wxid_conflicting_original_sender"
    if damage == "sent_owner":
        node.update(sender_native_id=other, avatar_path=f"Portrait/{other}.jpg")
    elif damage == "direct_peer":
        node.update(
            direction="received",
            sender_native_id=other,
            avatar_path=f"Portrait/{other}.jpg",
            classes=["msg", "media", "left"],
        )
    else:
        node["avatar_path"] = f"Portrait/{other}.jpg"
    with pytest.raises(ValueError):
        projected(entry, header, members, frames.OWNER, frames.PEER)
    if damage == "direct_peer":
        header["conversation_kind"] = "group"
        group_row, _ = projected(entry, header, members, frames.OWNER, frames.PEER)
        assert group_row[3] == other and group_row[5] == "received"


def test_unavailable_or_epoch_placeholder_time_remains_unknown_without_payload_io(
    monkeypatch,
):
    def no_file_read(*_args, **_kwargs):
        raise AssertionError("Projecting one audited frame must not read a file.")

    monkeypatch.setattr(Path, "open", no_file_read)
    for raw in ("", "1970-01-01 00:00:00", "time unavailable"):
        entry, header, members = _source()
        entry["node"]["raw_timestamp"] = raw
        row, _ = projected(entry, header, members, frames.OWNER, frames.PEER)
        proof = json.loads(row[11])
        assert row[7:11] == (0.0, "", "Unknown time", "Unavailable source time")
        assert proof["source_time"] == raw
        assert proof["source_clock_precision"] == "unknown"
        assert proof["media_reference"]["availability"] == "metadata_only"
        assert "media_payload_not_read" in json.loads(row[12])


def test_media_app_contact_location_and_unknown_events_keep_distinct_labels():
    expected = {
        "3": ("image", "[Image]"),
        "43": ("video", "[Video]"),
        "34": ("audio", "[Voice message]"),
        "47": ("sticker", "[Sticker]"),
        "file": ("file", "[File]"),
        "49": ("app", "[App message]"),
        "42": ("contact", "[Contact card]"),
        "48": ("location", "[Location]"),
        "unsupported-future-type": ("other", "[Other message]"),
    }
    for source_type, (kind, label) in expected.items():
        entry, header, members = _source()
        entry["node"]["type"] = source_type
        row, actual_kind = projected(entry, header, members, frames.OWNER, frames.PEER)
        proof = json.loads(row[11])
        assert (actual_kind, row[6]) == (kind, label)
        assert proof["media_reference"]["kind"] == kind
        assert proof["locator"]["export_msgtype"] == source_type
        assert proof["original_node"]["type"] == source_type
        assert "resource_reference" not in proof


def _empty_app_source():
    entry, header, members = _source()
    entry["node"].update(
        type="49",
        text_kind="rendered_app_text",
        text_length=0,
        text_sha256=hashlib.sha256(b"").hexdigest(),
        classes=["msg", "app", "right"],
    )
    return entry, header, members


def test_exact_empty_app_preserves_its_original_author_clock_and_audited_node():
    entry, header, members = _empty_app_source()
    original = deepcopy(entry)
    row, kind = projected(entry, header, members, frames.OWNER, frames.PEER)
    proof = json.loads(row[11])
    assert (kind, row[6]) == ("app", "[App message]")
    assert row[3:6] == (frames.OWNER, "Synthetic owner", "sent")
    assert row[8:11] == (
        "2023-12-29T13:00:08",
        "29 Dec 2023 13:00:08",
        "Source clock; timezone unknown",
    )
    assert proof["source_time"] == entry["node"]["raw_timestamp"]
    assert proof["original_node"] == entry["node"]
    assert proof["media_reference"]["kind"] == "app"
    assert proof["media_reference"]["availability"] == "metadata_only"
    assert entry == original


def test_rendered_app_with_text_or_a_conflicting_empty_digest_is_not_a_placeholder():
    for changed in (
        {"text_length": 1, "text_sha256": hashlib.sha256(b"x").hexdigest()},
        {"text_sha256": "0" * 64},
    ):
        entry, header, members = _empty_app_source()
        entry["node"].update(changed)
        with pytest.raises(ValueError):
            projected(entry, header, members, frames.OWNER, frames.PEER)


def test_empty_notice_and_plain_text_do_not_become_media_events():
    for source_type, text_kind in (("10000", "notice"), ("1", "plain_text")):
        entry, header, members = _empty_app_source()
        entry["node"].update(type=source_type, text_kind=text_kind)
        with pytest.raises(ValueError):
            projected(entry, header, members, frames.OWNER, frames.PEER)
