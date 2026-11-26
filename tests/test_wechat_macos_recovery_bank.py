"""Indexed Mac recovery keeps authenticated scopes, literal roles and original bytes.

Code version: v1.0.6-codex.0
"""

from contextlib import closing
from copy import deepcopy
import hashlib
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core import wechat_macos_recovery as legacy
from app.core import wechat_macos_recovery_bank as bank
from app.core.owner_only_files import owner_only_problem
from app.core.wechat_archive import WechatArchiveRepository, _identifier, _time_fields
from app.core.wechat_contacts import (
    canonical_contact_id,
    contact_observation,
    contact_registry_path,
    initialize_contact_registry,
)
from app.core.wechat_history_browser import query_wechat_history
from app.core.wechat_text import extract_wechat_text
from tests.test_wechat_contacts import bind, insert_contact, observe
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
    assert owner_only_problem(fixture.bank_path) == ""
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


def test_bank_direct_text_quotes_resolve_filtered_original_authors(bank_archive):
    fixture = bank_archive
    excerpt = "The original literal excerpt"
    remark = "Verified account remark"
    original = _message(60, 1, raw=excerpt, sender=PEER)
    cases = (
        ("Explicit", OWNER, PEER, "", 0, excerpt, "Historical peer label"),
        ("Inverted", PEER, OWNER, PEER, -1, excerpt, "Historical owner label"),
        ("Different text", PEER, OWNER, PEER, -1,
         "A different literal excerpt", "Only a captured source label"),
        ("Different time", PEER, OWNER, PEER, -2, excerpt, "Another captured source label"),
    )
    packet = fixture.direct
    packet["messages"] = [original]
    for local_id, (case, sender, source_sender, source_chat, offset, text, label) in enumerate(
        cases, 61
    ):
        epoch = original["create_time"] + offset
        packet["messages"].append(_message(
            local_id, 49, sender=sender,
            raw=(
                "<msg><appmsg><type>57</type>"
                f"<title>Filtered reply {case}</title><refermsg><type>1</type>"
                f"<svrid>{original['server_id']}</svrid><fromusr>{source_sender}</fromusr>"
                f"<chatusr>{source_chat}</chatusr><displayname>{label}</displayname>"
                f"<createtime>{epoch}</createtime><content>{text}</content>"
                "</refermsg></appmsg></msg>"
            ),
        ))
    registry = contact_registry_path(fixture.root)
    partition = "ios-" + hashlib.md5(OWNER.encode(), usedforsecurity=False).hexdigest()
    identifiers = {
        sender: canonical_contact_id(sender, authority="native_contact_username")
        for sender in (PEER, OWNER)
    }
    with sqlite3.connect(registry) as connection:
        initialize_contact_registry(connection)
        for sender, identifier in identifiers.items():
            connection.execute(
                "INSERT INTO contacts VALUES(?,?,?,?,?,?)",
                (identifier, "wechat", "", sender, "wechat_account", "verified"),
            )
            connection.execute(
                "INSERT INTO contact_identifiers VALUES(?,?,?,?,?)",
                (identifier, "native_username", sender, "", "native_contact_username"),
            )
            connection.execute(
                "INSERT INTO sender_bindings VALUES(?,?,?,?,?,?,?)",
                (partition, sender, "", "", identifier, "native_contact_username", "{}"),
            )
        for sender, owner, kind, value in (
            (PEER, OWNER, "remark", remark),
            (PEER, OWNER, "nickname", "Historical account nickname"),
            (PEER, "wxid_other_account", "remark", "Another account's remark"),
            (OWNER, OWNER, "nickname", "The viewing account"),
        ):
            observation = contact_observation(
                identifiers[sender],
                source_id="ios-" + hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest(),
                source_snapshot="older-native-contact-snapshot",
                owner_account_id=owner,
                kind=kind,
                value=value,
            )
            connection.execute(
                "INSERT INTO contact_observations VALUES(:id,:contact_id,:source_id,"
                ":source_snapshot,:owner_account_id,:room_id,:kind,:value,:evidence)",
                observation,
            )
    _publish(fixture, packets=[packet])
    sources = (fixture.history, registry, fixture.bank_path)
    sources_before = tuple(path.read_bytes() for path in sources)
    page = query_wechat_history(
        fixture.root,
        account="account:" + OWNER,
        chat_kind="direct",
        query="Filtered reply",
        session=fixture.direct_session,
        persist_event_cache=False,
        strict_timeline=True,
    )
    assert len(page.items) == len(cases)
    replies = {item.messaging["display_text"]: item for item in page.items}
    for scenario, stored in zip(cases, packet["messages"][1:], strict=True):
        case, sender, source_sender, _chat, _offset, text, label = scenario
        item = replies["Filtered reply " + case]
        quote = item.messaging["quoted_reply"]
        assert item.author_label == sender
        assert quote["speaker"] == label
        assert quote["text"] == text
        assert quote["reference"] == stored["native_text"]["quoted_reference"]
        assert quote["reference"]["source_sender_id"] == source_sender
        if case in {"Explicit", "Inverted"}:
            assert quote["viewer_display_name"] == remark
            assert quote["contact"]["id"] == identifiers[PEER]
        else:
            assert not quote.get("viewer_display_name")
    assert tuple(path.read_bytes() for path in sources) == sources_before


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


def test_latest_group_name_includes_the_verified_mac_recovery_timeline(bank_archive):
    fixture = bank_archive
    original = fixture.history.read_bytes()
    old_title = query_wechat_history(fixture.root, chat_kind="group").sessions[0]
    assert old_title.conversation_title == "Synthetic group"
    fixture.group["messages"].append(
        _message(57, 10000, raw='You changed the group name to "Recovered group name"')
    )
    _publish(fixture)
    new_title = query_wechat_history(fixture.root, chat_kind="group").sessions[0]
    assert new_title.conversation_title == "Recovered group name"
    assert new_title.stable_id == old_title.stable_id == fixture.group_session
    detail = query_wechat_history(fixture.root, session=fixture.group_session)
    assert detail.current_session.conversation_title == new_title.conversation_title
    changes = detail.current_session.messaging["group_name_history"]["changes"]
    assert len(changes) == 1
    notice = next(item for item in detail.items if item.stable_id == changes[0]["message_id"])
    assert notice.content_text == fixture.group["messages"][-1]["text"]
    assert notice.messaging["is_system_notice"]
    assert fixture.history.read_bytes() == original


@pytest.mark.parametrize("conflicting_nickname", [False, True])
def test_mac_group_sender_uses_complete_exact_nickname_scope(bank_archive, conflicting_nickname):
    fixture = bank_archive
    _publish(fixture)
    partition = "ios-" + hashlib.md5(OWNER.encode(), usedforsecurity=False).hexdigest()
    registry = contact_registry_path(fixture.root)
    nickname = "Verified group sender"
    with sqlite3.connect(registry) as connection:
        initialize_contact_registry(connection)
        contact = insert_contact(connection, MEMBER)
        bind(connection, contact, source=partition, sender=MEMBER)
        for index in range(65):
            observe(
                connection, contact, "source_sender_label", f"Recovered sender {index}",
                source=partition, owner=OWNER, room=ROOM,
            )
        for snapshot in ("older-verified-contacts", "later-verified-contacts"):
            observe(
                connection, contact, "nickname", nickname,
                source=partition, owner=OWNER, snapshot=snapshot,
            )
        observe(connection, contact, "nickname", "Different account's nickname", source=partition)
        observe(
            connection, contact, "nickname", "Different partition's nickname", owner=OWNER,
        )
        observe(
            connection, contact, "nickname", "Room label", source=partition, owner=OWNER, room=ROOM,
        )
        if conflicting_nickname:
            observe(
                connection, contact, "nickname", "Another exact nickname", source=partition, owner=OWNER,
            )

    before = fixture.history.read_bytes(), registry.read_bytes()
    page = query_wechat_history(fixture.root, session=fixture.group_session)
    message = next(item for item in page.items if item.content_text.startswith("Group line"))
    context = message.messaging["contact"]
    assert context["observations_truncated"] is True
    assert context["identity_status"] == "verified"
    assert message.messaging.get("viewer_display_name") == (None if conflicting_nickname else nickname)
    assert message.author_label == context["source_display_name"] == MEMBER
    assert (fixture.history.read_bytes(), registry.read_bytes()) == before


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


@pytest.mark.parametrize("same_locator", [False, True], ids=["new-snapshot", "same-locator"])
def test_native_event_remains_in_its_original_conversation_across_archive_sources(
    bank_archive, same_locator,
):
    fixture = bank_archive
    incumbent = deepcopy(fixture.group)
    incumbent["messages"] = incumbent["messages"][:1]
    duplicate_partition = "html-synthetic-overlapping-group"
    duplicate_session = "wechat:" + _identifier(duplicate_partition, ROOM)
    duplicate_anchors = []
    with closing(sqlite3.connect(fixture.history)) as connection, connection:
        connection.execute(
            "INSERT INTO partitions VALUES(?,?,?,?,?,?,?,?)",
            (
                duplicate_partition, "account:" + OWNER, OWNER, "Synthetic account",
                "Corroborated export ID", "HTML export", "synthetic-overlap.html", "",
            ),
        )
        connection.execute(
            "INSERT INTO conversations SELECT ?,?,account_key,peer_id,title,kind,"
            "message_count,first_sort,last_sort,first_time,last_time,latest_text "
            "FROM conversations WHERE id=?",
            (duplicate_session, duplicate_partition, fixture.group_session),
        )
        for anchor in incumbent["anchors"]:
            archive_id = _identifier(duplicate_session, anchor["archive_message_id"])
            connection.execute(
                "INSERT INTO messages SELECT ?,?,ordinal,sender_id,sender_label,direction,"
                "text,time_sort,timestamp_value,timestamp_label,time_basis,provenance,"
                "quality_flags,search_text FROM messages WHERE id=?",
                (archive_id, duplicate_session, anchor["archive_message_id"]),
            )
            duplicate_anchors.append({**anchor, "archive_message_id": archive_id})

    original_history = fixture.history.read_bytes()
    _publish(fixture, packets=[incumbent])
    overlapping = incumbent["messages"][0]
    with closing(sqlite3.connect(fixture.bank_path)) as connection:
        original_message = connection.execute("SELECT * FROM messages").fetchone()

    alternate = deepcopy(incumbent)
    alternate.update(
        session_id=duplicate_session,
        receipt_id="synthetic-overlapping-group-archive",
        anchors=duplicate_anchors,
    )
    if not same_locator:
        alternate["source"].update(
            snapshot_id="synthetic-overlapping-group-new-snapshot",
            main_sha256=_hash("synthetic-overlapping-group-new-main"),
        )
    unrelated = _message(90, 1, raw="A new native message in the selected conversation")
    alternate["messages"].append(unrelated)

    receipt = _publish(fixture, packets=[alternate], name="overlapping-archive-source")
    assert receipt["added"] == receipt["occurrences_added"] == receipt["conflicts"] == 1
    with closing(sqlite3.connect(fixture.bank_path)) as connection:
        assert connection.execute(
            "SELECT * FROM messages WHERE id=?", (original_message[0],),
        ).fetchone() == original_message
        assert connection.execute(
            "SELECT conversation_id,server_id,reason FROM gaps",
        ).fetchall() == [
            (
                duplicate_session, overlapping["server_id"],
                "native_event_owned_by_another_conversation",
            ),
        ]
        assert set(connection.execute(
            "SELECT o.conversation_id,m.conversation_id,o.server_id "
            "FROM occurrences o JOIN messages m ON m.id=o.canonical_id",
        )) == {
            (fixture.group_session, fixture.group_session, overlapping["server_id"]),
            (duplicate_session, duplicate_session, unrelated["server_id"]),
        }
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
