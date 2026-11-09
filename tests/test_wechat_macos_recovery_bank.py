"""Indexed Mac recovery keeps authenticated scopes, literal roles and original bytes.

Code version: v1.0.0-codex.0
"""

from copy import deepcopy
import hashlib
import json
import sqlite3
import stat
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core import wechat_macos_recovery as legacy
from app.core import wechat_macos_recovery_bank as bank
from app.core.wechat_archive import WechatArchiveRepository, _identifier, _time_fields
from app.core.wechat_history_browser import query_wechat_history
from app.core.wechat_text import extract_wechat_text
from tests import test_wechat_macos_recovery as recovery_fixtures


mac_archive = recovery_fixtures.mac_archive
OWNER = recovery_fixtures.OWNER
PEER = recovery_fixtures.PEER
ROOM = "123456789@chatroom"
MEMBER = "wxid_synthetic_mac_member"


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _proof(rowid, username, *, is_session=0):
    row = {"rowid": rowid, "user_name": username, "is_session": is_session}
    encoded = json.dumps(
        row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return {"row": row, "row_sha256": _hash(encoded)}


def _message(local, message_type, *, raw=None, resource=None, sender=MEMBER):
    epoch = 1_700_000_000 + local
    system = message_type in {10000, 10002}
    result = {
        "local_id": local,
        "local_type": message_type,
        "server_id": str(500 + local),
        "create_time": epoch,
        "sender_id": "" if system else sender,
        "sender_label": "System Message" if system else sender,
        "direction": "unknown" if system else "sent" if sender == OWNER else "received",
        "content_sha256": _hash(raw if raw is not None else f"resource-source-{local}"),
        "real_sender_id": 0 if system else 1 if sender == OWNER else 3,
    }
    if not system:
        result["sender_proof"] = _proof(result["real_sender_id"], sender)
    if resource is not None:
        label = {"audio": "[Voice message]", "sticker": "[Sticker]", "file": "[File]"}[
            resource
        ]
        result["text"] = label
        result["resource"] = {"kind": resource, "label": label, "native_time": epoch}
        if resource == "file":
            result["local_type"] = (6 << 32) | 49
            result["resource"]["app_subtype"] = "6"
            result["resource"]["safe_title"] = "Synthetic inert file.txt"
    else:
        native = extract_wechat_text(message_type, raw)
        assert native is not None
        result["text"] = native["text"]
        result["native_text"] = native
        if message_type == 49:
            result["local_type"] = (native["appmsg_subtype"] << 32) | 49
    return result


@pytest.fixture
def bank_archive(mac_archive):
    root, history, direct_session, original_packet = mac_archive
    legacy_packet = deepcopy(original_packet)
    legacy_packet["source"]["table"] = (
        "Msg_" + hashlib.md5(PEER.encode(), usedforsecurity=False).hexdigest()
    )
    direct = deepcopy(legacy_packet)
    direct.update(
        schema_version=2,
        conversation_kind="direct",
        peer_proof=_proof(2, PEER, is_session=1),
    )
    for message in direct["messages"]:
        sender = message["sender_id"]
        message["real_sender_id"] = 1 if sender == OWNER else 2
        message["sender_proof"] = _proof(
            message["real_sender_id"], sender, is_session=1 if sender == PEER else 0
        )
    with sqlite3.connect(history) as connection:
        partition = connection.execute("SELECT id FROM partitions").fetchone()[0]
        group_session = "wechat:" + _identifier(partition, ROOM)
        anchors = []
        for ordinal, server in enumerate(("301", "302"), 1):
            text = f"Unchanged group anchor {ordinal}\n\nLiteral source text"
            identifier = _hash("group-anchor-" + server)
            order, stamp, label, basis = _time_fields(
                {"sent_at_utc_iso": f"2023-11-14T22:13:{30 + ordinal:02d}+00:00"}
            )
            connection.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    group_session,
                    ordinal,
                    MEMBER,
                    MEMBER,
                    "received",
                    text,
                    order,
                    stamp,
                    label,
                    basis,
                    json.dumps({"server_id": server, "message_type": 1}),
                    "[]",
                    text.casefold(),
                ),
            )
            anchors.append(
                {
                    "server_id": server,
                    "archive_message_id": identifier,
                    "text_sha256": _hash(text),
                }
            )
        connection.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                group_session,
                partition,
                "account:" + OWNER,
                ROOM,
                "Synthetic group",
                "group",
                2,
                order - 1,
                order,
                stamp,
                stamp,
                text[:300],
            ),
        )
    group = deepcopy(direct)
    group.update(
        receipt_id="synthetic-group-bank-v2",
        conversation_kind="group",
        peer_id=ROOM,
        session_id=group_session,
        peer_proof=_proof(10, ROOM, is_session=1),
        anchors=anchors,
    )
    group["source"]["table"] = (
        "Msg_" + hashlib.md5(ROOM.encode(), usedforsecurity=False).hexdigest()
    )
    forwarded = (
        "<msg><appmsg><type>19</type><title>Literal source bundle</title><recorditem>"
        '<recordinfo><datalist><dataitem datatype="1"><sourcename>Original source name</sourcename>'
        "<dataitemsource><fromusr>wxid_synthetic_original</fromusr></dataitemsource>"
        '<datadesc>Forwarded literal text</datadesc></dataitem><dataitem datatype="2">'
        "<sourcename>Image source name</sourcename></dataitem>"
        "</datalist></recordinfo></recorditem></appmsg></msg>"
    )
    group["messages"] = [
        _message(50, 1, raw="Group line  \n\n• Literal bullet\n  Second line"),
        _message(51, 10000, raw="System Message: A member joined"),
        _message(
            52,
            10002,
            raw='<sysmsg type="revokemsg"><revokemsg><replacemsg>Literal revoked notice</replacemsg>'
            "<newmsgid>12345</newmsgid></revokemsg></sysmsg>",
        ),
        _message(53, 49, raw=forwarded, sender=OWNER),
        _message(54, 34, resource="audio"),
        _message(55, 47, resource="sticker"),
        _message(56, 49, resource="file"),
    ]
    # A server ID is scoped to its real account and peer, not globally unique.
    group["messages"][0]["server_id"] = direct["messages"][0]["server_id"]
    return SimpleNamespace(
        root=root,
        history=history,
        direct=direct,
        group=group,
        legacy_packet=legacy_packet,
        direct_session=direct_session,
        group_session=group_session,
        packets=[direct, group],
        bank_path=history.with_name("macos_recovery.sqlite3"),
    )


def _stage(fixture, *, packets=None, name="candidate"):
    candidate = fixture.history.with_name(name + ".sqlite3")
    receipt = bank.stage_mac_recovery_bank(
        fixture.root,
        packets if packets is not None else fixture.packets,
        candidate,
        account_ids=(OWNER,),
    )
    return candidate, receipt


def _publish(fixture, *, packets=None, name="candidate"):
    candidate, receipt = _stage(fixture, packets=packets, name=name)
    bank.publish_mac_recovery_bank(fixture.root, candidate, receipt)
    return receipt


def _detail(fixture, session):
    return WechatArchiveRepository(fixture.root).query(
        session=session, persist_event_cache=False
    )


def test_bank_adds_authenticated_direct_and_group_events_without_changing_history(
    bank_archive,
):
    fixture = bank_archive
    original = fixture.history.read_bytes()
    candidate, receipt = _stage(fixture)
    assert receipt["added"] == 12
    assert (
        receipt["candidate_sha256"]
        == hashlib.sha256(candidate.read_bytes()).hexdigest()
    )
    assert receipt["input_identities"]
    assert not fixture.bank_path.exists()
    assert fixture.history.read_bytes() == original
    bank.publish_mac_recovery_bank(fixture.root, candidate, receipt)
    assert stat.S_IMODE(fixture.bank_path.stat().st_mode) == 0o600
    assert fixture.history.read_bytes() == original
    assert _detail(fixture, fixture.direct_session)["count"] == 7
    group = _detail(fixture, fixture.group_session)
    assert group["count"] == group["current"]["message_count"] == 9
    added = [row for row in group["messages"] if legacy.validated_recovery_receipt(row)]
    assert len(added) == 7
    assert (
        next(row for row in added if row["text"].startswith("Group line"))["sender_id"]
        == MEMBER
    )
    listing = WechatArchiveRepository(fixture.root).query(
        account="account:" + OWNER, persist_event_cache=False
    )
    assert listing["conversation_count"] == 2
    assert (
        listing["message_count"]
        == sum(row["message_count"] for row in listing["conversations"])
        == 16
    )
    assert fixture.history.read_bytes() == original


def test_bank_preserves_text_system_forwarded_and_metadata_only_resource_roles(
    bank_archive,
):
    fixture = bank_archive
    _publish(fixture)
    with patch(
        "app.core.wechat_mac_resources.WechatMacResourceResolver.resolve"
    ) as resolver:
        page = query_wechat_history(
            fixture.root, session=fixture.group_session, persist_event_cache=False
        )
        resolver.assert_not_called()
    recovered = {
        item.messaging["provenance"]["macos_recovery"]["local_id"]: item
        for item in page.items
        if "macos_recovery" in item.messaging["provenance"]
    }
    assert (
        recovered[50].content_text
        == recovered[50].messaging["display_text"]
        == fixture.group["messages"][0]["text"]
    )
    for local in (51, 52):
        assert recovered[local].messaging["is_system_notice"] is True
        assert recovered[local].messaging["sender_id"] == ""
        assert recovered[local].role == "unknown"
    proof = recovered[52].messaging["provenance"]
    assert proof["system_message_subtype"] == "revokemsg"
    assert proof["system_source_ids"] == {"revokemsg/newmsgid": "12345"}
    forwarded = recovered[53].messaging["forwarded_record"]
    assert forwarded["title"] == "Literal source bundle"
    assert forwarded["items"][0].content_text == "Forwarded literal text"
    assert forwarded["items"][0].author_label == "Original source name"
    assert forwarded["items"][1].messaging["resource"]["kind"] == "image"
    for local, kind in ((54, "audio"), (55, "sticker"), (56, "file")):
        resource = recovered[local].messaging["resource"]
        assert resource["kind"] == kind
        assert resource["status"] == "metadata_only" and resource["can_reveal"] is False
    assert all(
        "WeChat for Mac local database" in item.messaging["source_file"]
        for item in recovered.values()
    )


def test_replay_keeps_bubble_counts_and_new_snapshot_retains_another_occurrence(
    bank_archive,
):
    fixture = bank_archive
    _publish(fixture)
    before = fixture.history.read_bytes()
    replay = _publish(fixture, name="replay")
    assert replay["added"] == replay["occurrences_added"] == 0
    assert _detail(fixture, fixture.group_session)["count"] == 9
    alternative = deepcopy(fixture.packets)
    for packet in alternative:
        packet["source"]["snapshot_id"] = "another-authenticated-snapshot"
        packet["receipt_id"] += "-another-snapshot"
    receipt = _publish(fixture, packets=alternative, name="another-snapshot")
    assert receipt["added"] == 0
    assert receipt["occurrences_added"] == 12
    assert _detail(fixture, fixture.direct_session)["count"] == 7
    assert _detail(fixture, fixture.group_session)["count"] == 9
    assert fixture.history.read_bytes() == before


def test_an_authenticated_source_row_cannot_change_during_replay(bank_archive):
    fixture = bank_archive
    _publish(fixture)
    before = fixture.bank_path.read_bytes()
    changed = deepcopy(fixture.group)
    message = changed["messages"][0]
    message["text"] = "Conflicting literal text from the same claimed source row"
    message["native_text"]["text"] = message["text"]
    message["content_sha256"] = _hash(message["text"])
    with pytest.raises(ValueError):
        _stage(fixture, packets=[changed], name="conflicting-source-row")
    assert fixture.bank_path.read_bytes() == before
    assert _detail(fixture, fixture.group_session)["count"] == 9


def test_cross_source_server_conflicts_remain_classified_gaps_without_changing_a_bubble(
    bank_archive,
):
    fixture = bank_archive
    _publish(fixture)
    original_history = fixture.history.read_bytes()
    changed = deepcopy(fixture.group)
    changed["receipt_id"] = "another-native-source-with-one-conflict"
    changed["source"].update(
        source_id="macos-another-authenticated-device",
        snapshot_id="another-authenticated-source-snapshot",
        main_sha256=_hash("another-authenticated-main"),
        wal_sha256=_hash("another-authenticated-wal"),
    )
    message = changed["messages"][0]
    message["text"] = "A different native body with the same scoped server ID"
    message["native_text"]["text"] = message["text"]
    message["content_sha256"] = _hash(message["text"])
    receipt = _publish(fixture, packets=[changed], name="cross-source-conflict")
    assert receipt["added"] == 0
    assert receipt["conflicts"] == 1
    assert receipt["occurrences_added"] == 6
    with sqlite3.connect(fixture.bank_path) as connection:
        gaps = connection.execute(
            "SELECT conversation_id,local_id,server_id,reason,content_sha256 FROM gaps"
        ).fetchall()
    assert gaps == [
        (
            fixture.group_session,
            50,
            message["server_id"],
            "native_or_projection_conflict",
            message["content_sha256"],
        )
    ]
    group = _detail(fixture, fixture.group_session)
    assert group["count"] == 9
    assert fixture.group["messages"][0]["text"] in [
        row["text"] for row in group["messages"]
    ]
    assert message["text"] not in [row["text"] for row in group["messages"]]
    assert fixture.history.read_bytes() == original_history


@pytest.mark.parametrize(
    "change",
    [
        "sender_hash",
        "sender_row",
        "sender_username",
        "room_author",
        "sent_owner",
        "peer_hash",
        "peer_username",
        "table",
        "kind",
        "anchor_scope",
        "anchor_count",
        "unknown_session",
        "authentication",
    ],
)
def test_invalid_sender_peer_or_existing_scope_proofs_refuse_staging(
    bank_archive, change
):
    fixture = bank_archive
    packet = deepcopy(fixture.group)
    message = packet["messages"][0]
    if change == "sender_hash":
        message["sender_proof"]["row_sha256"] = "a" * 64
    elif change == "sender_row":
        message["sender_proof"] = _proof(99, MEMBER)
    elif change == "sender_username":
        message["sender_proof"] = _proof(3, OWNER)
    elif change == "room_author":
        message["sender_id"] = ROOM
        message["sender_proof"] = _proof(3, ROOM)
    elif change == "sent_owner":
        message["direction"] = "sent"
    elif change == "peer_hash":
        packet["peer_proof"]["row_sha256"] = "b" * 64
    elif change == "peer_username":
        packet["peer_proof"] = _proof(10, PEER, is_session=1)
    elif change == "table":
        packet["source"]["table"] = fixture.direct["source"]["table"]
    elif change == "kind":
        packet["conversation_kind"] = "direct"
    elif change == "anchor_scope":
        packet["anchors"] = deepcopy(fixture.direct["anchors"])
    elif change == "anchor_count":
        packet["anchors"] = packet["anchors"][:1]
    elif change == "unknown_session":
        packet["session_id"] = "wechat:" + "e" * 64
    else:
        packet["source"]["authenticated_page_count"] -= 1
    before = fixture.history.read_bytes()
    with pytest.raises(ValueError):
        _stage(fixture, packets=[packet])
    assert not fixture.bank_path.exists()
    assert fixture.history.read_bytes() == before


def test_account_allowlist_cannot_expand_to_another_owner(bank_archive):
    fixture = bank_archive
    with pytest.raises(ValueError):
        bank.stage_mac_recovery_bank(
            fixture.root,
            fixture.packets,
            fixture.history.with_name("denied.sqlite3"),
            account_ids=("wxid_another_owner",),
        )
    assert not fixture.bank_path.exists()


@pytest.mark.parametrize("change", ["direction", "sender", "classification"])
def test_system_notices_cannot_acquire_a_human_sender_role(bank_archive, change):
    fixture = bank_archive
    packet = deepcopy(fixture.group)
    message = packet["messages"][1]
    if change == "direction":
        message["direction"] = "received"
    elif change == "sender":
        message["sender_id"] = MEMBER
        message["sender_proof"] = _proof(3, MEMBER)
    else:
        message["native_text"]["text_extraction_kind"] = "plain_text"
    with pytest.raises(ValueError):
        _stage(fixture, packets=[packet])
    assert not fixture.bank_path.exists()


def test_bank_preserves_the_legacy_five_message_sidecar(bank_archive):
    fixture = bank_archive
    legacy.publish_mac_recovery(
        fixture.root, legacy.stage_mac_recovery(fixture.root, fixture.legacy_packet)
    )
    path = fixture.history.with_name("macos_recovery.json")
    original_history, original_legacy = fixture.history.read_bytes(), path.read_bytes()
    _publish(fixture)
    assert _detail(fixture, fixture.direct_session)["count"] == 7
    assert _detail(fixture, fixture.group_session)["count"] == 9
    assert fixture.history.read_bytes() == original_history
    assert path.read_bytes() == original_legacy


@pytest.mark.parametrize("changed_input", ["history", "legacy", "bank", "candidate"])
def test_publication_requires_unchanged_staged_inputs(bank_archive, changed_input):
    fixture = bank_archive
    legacy.publish_mac_recovery(
        fixture.root, legacy.stage_mac_recovery(fixture.root, fixture.legacy_packet)
    )
    _publish(fixture, name="initial")
    candidate, receipt = _stage(fixture, name="staged-update")
    selected = {
        "history": fixture.history,
        "legacy": fixture.history.with_name("macos_recovery.json"),
        "bank": fixture.bank_path,
        "candidate": candidate,
    }[changed_input]
    if changed_input == "history":
        with sqlite3.connect(selected) as connection:
            connection.execute(
                "UPDATE conversations SET title=title || ' changed' WHERE id=?",
                (fixture.direct_session,),
            )
    else:
        with selected.open("ab") as stream:
            stream.write(b"synthetic-staged-input-drift")
    before = fixture.bank_path.read_bytes()
    with pytest.raises(ValueError):
        bank.publish_mac_recovery_bank(fixture.root, candidate, receipt)
    assert fixture.bank_path.read_bytes() == before


def test_staged_candidate_cannot_be_replaced_by_a_symlink(bank_archive):
    fixture = bank_archive
    candidate, receipt = _stage(fixture)
    preserved = candidate.with_name("preserved-candidate.sqlite3")
    candidate.rename(preserved)
    candidate.symlink_to(preserved)
    with pytest.raises(ValueError):
        bank.publish_mac_recovery_bank(fixture.root, candidate, receipt)
    assert preserved.is_file() and not fixture.bank_path.exists()
