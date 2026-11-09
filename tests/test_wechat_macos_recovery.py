"""Authenticated Mac recovery preserves source boundaries and original archive bytes.

Code version: v1.0.0-codex.0
"""

from copy import deepcopy
import hashlib
import json
import sqlite3
import stat

import pytest

from app.core import wechat_macos_recovery as recovery
from app.core.wechat_archive import (
    WechatArchiveRepository,
    _SCHEMA,
    _identifier,
    _time_fields,
)
from app.core.wechat_text import extract_wechat_text


OWNER = "wxid_synthetic_mac_owner"
PEER = "wxid_synthetic_mac_peer"


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture
def mac_archive(tmp_path):
    root = tmp_path / "store"
    history = root / "messaging/wechat/history.sqlite3"
    history.parent.mkdir(parents=True)
    partition = "ios-" + hashlib.md5(OWNER.encode(), usedforsecurity=False).hexdigest()
    session = "wechat:" + _identifier(partition, PEER)
    anchors = []
    with sqlite3.connect(history) as connection:
        connection.executescript(_SCHEMA)
        connection.execute(
            "INSERT INTO partitions VALUES(?,?,?,?,?,?,?,?)",
            (
                partition,
                "account:" + OWNER,
                OWNER,
                "Synthetic owner",
                "Verified archive ID",
                "iPhone backup",
                "synthetic",
                "",
            ),
        )
        for ordinal, server in enumerate(("101", "102"), 1):
            text = f"Original anchor {ordinal}\n\nLiteral source text"
            identifier = _hash("original-" + server)
            order, stamp, label, basis = _time_fields(
                {"sent_at_utc_iso": f"2023-11-14T22:13:{20 + ordinal:02d}+00:00"}
            )
            connection.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    session,
                    ordinal,
                    PEER,
                    PEER,
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
                session,
                partition,
                "account:" + OWNER,
                PEER,
                "Synthetic peer",
                "direct",
                2,
                order - 1,
                order,
                stamp,
                stamp,
                text[:300],
            ),
        )
    messages = []
    for local, typ in enumerate((3, 3, 3, 43), 10):
        kind, label = ("image", "[Image]") if typ == 3 else ("video", "[Video]")
        epoch = 1_700_000_000 + local
        messages.append(
            {
                "local_id": local,
                "local_type": typ,
                "server_id": str(200 + local),
                "create_time": epoch,
                "sender_id": PEER,
                "direction": "received",
                "text": label,
                "content_sha256": _hash(f"synthetic-source-content-{local}"),
                "resource": {"kind": kind, "label": label, "native_time": epoch},
            }
        )
    native = extract_wechat_text(
        49,
        (
            "<msg><appmsg><title>Reply kept exactly</title><type>57</type><refermsg>"
            f"<type>3</type><svrid>210</svrid><fromusr>{PEER}</fromusr>"
            "<displayname>Synthetic peer</displayname><createtime>1700000010</createtime>"
            "</refermsg></appmsg></msg>"
        ),
    )
    messages.append(
        {
            "local_id": 14,
            "local_type": (57 << 32) | 49,
            "server_id": "214",
            "create_time": 1_700_000_014,
            "sender_id": OWNER,
            "direction": "sent",
            "text": native["text"],
            "content_sha256": _hash("synthetic-quote-source"),
            "native_text": native,
        }
    )
    packet = {
        "schema_version": 1,
        "receipt_id": "synthetic-receipt-v1",
        "account_id": OWNER,
        "session_id": session,
        "peer_id": PEER,
        "source": {
            "source_id": "macos-synthetic-source",
            "snapshot_id": "synthetic-sealed-snapshot",
            "main_sha256": _hash("synthetic-main"),
            "wal_sha256": _hash("synthetic-wal"),
            "database_relative_path": "db_storage/message/message_0.db",
            "table": "Msg_synthetic_peer",
            "integrity_check": "ok",
            "authenticated_pages": True,
            "page_count": 10,
            "authenticated_page_count": 10,
        },
        "anchors": anchors,
        "messages": messages,
    }
    return root, history, session, packet


def test_five_mac_events_are_additive_searchable_and_idempotent(mac_archive):
    root, history, session, packet = mac_archive
    original = history.read_bytes()
    staged = recovery.stage_mac_recovery(root, packet)
    assert staged["added"] == staged["total"] == 5
    assert not history.with_name("macos_recovery.json").exists()
    report = recovery.publish_mac_recovery(root, staged)
    assert report["added"] == 5
    assert (
        stat.S_IMODE(history.with_name("macos_recovery.json").stat().st_mode) == 0o600
    )
    repository = WechatArchiveRepository(root)
    detail = repository.query(session=session, persist_event_cache=False)
    assert (
        detail["count"]
        == detail["message_count"]
        == detail["current"]["message_count"]
        == 7
    )
    assert repository.query(persist_event_cache=False)["message_count"] == 7
    assert (
        repository.query(
            session=session, query="Reply kept", persist_event_cache=False
        )["count"]
        == 1
    )
    assert repository.query(query="Reply kept", persist_event_cache=False)["count"] == 1
    recovered = [
        row for row in detail["messages"] if recovery.validated_recovery_receipt(row)
    ]
    assert len(recovered) == 5
    assert [row["text"] for row in recovered].count("[Image]") == 3
    assert [row["text"] for row in recovered].count("[Video]") == 1
    quote_page = repository.query(
        session=session, query="Reply kept", page_size=1, persist_event_cache=False
    )
    assert len(quote_page["quote_resource_originals"]) == 1
    assert (
        recovery.validated_recovery_receipt(quote_page["quote_resource_originals"][0])[
            "server_id"
        ]
        == "210"
    )
    for row in recovered:
        proof = json.loads(row["provenance"])
        assert "source_snapshot" not in proof
        assert proof["macos_recovery"]["source_id"].startswith("macos-")
        assert recovery.mac_recovery_conversation(history, row["id"]) == session
        with sqlite3.connect(history) as connection:
            assert (
                repository._timeline_conversation_id(connection, row["id"]) == session
            )
    replay = recovery.stage_mac_recovery(root, packet)
    assert replay["added"] == 0 and replay["existing"] == replay["total"] == 5
    assert recovery.publish_mac_recovery(root, replay)["added"] == 0
    assert history.read_bytes() == original


@pytest.mark.parametrize(
    "change",
    ["owner", "peer", "session", "anchor_hash", "anchor_server", "anchor_missing"],
)
def test_wrong_binding_or_missing_anchor_refuses_publication(mac_archive, change):
    root, history, _session, packet = mac_archive
    original = history.read_bytes()
    if change in {"owner", "peer"}:
        packet[{"owner": "account_id", "peer": "peer_id"}[change]] = "wxid_different"
    elif change == "session":
        packet["session_id"] = "wechat:" + "e" * 64
    elif change == "anchor_hash":
        packet["anchors"][0]["text_sha256"] = "a" * 64
    elif change == "anchor_server":
        packet["anchors"][0]["server_id"] = "999"
    else:
        packet["anchors"] = packet["anchors"][:1]
    with pytest.raises(ValueError):
        recovery.stage_mac_recovery(root, packet)
    assert not history.with_name("macos_recovery.json").exists()
    assert history.read_bytes() == original


@pytest.mark.parametrize(
    "change",
    [
        "authentication",
        "integrity",
        "source",
        "body",
        "direction",
        "secret",
        "nested_secret",
        "path",
    ],
)
def test_incomplete_or_conflicting_source_proof_is_refused(mac_archive, change):
    root, _history, _session, packet = mac_archive
    if change == "authentication":
        packet["source"]["authenticated_page_count"] -= 1
    elif change == "integrity":
        packet["source"]["integrity_check"] = "failed"
    elif change == "source":
        packet["source"]["source_id"] = "ios-fake"
    elif change == "body":
        packet["messages"][0]["text"] = "Invented image text"
    elif change == "direction":
        packet["messages"][0]["direction"] = "sent"
    elif change == "secret":
        packet["messages"][0]["raw_xml"] = "Rejected before persistence"
    elif change == "nested_secret":
        packet["messages"][-1]["native_text"]["quoted_reference"]["aeskey"] = (
            "Rejected before persistence"
        )
    else:
        packet["source"]["database_relative_path"] = "../escape.db"
    with pytest.raises(ValueError):
        recovery.stage_mac_recovery(root, packet)


def test_source_receipt_drift_and_conflicting_replay_preserve_sidecar(mac_archive):
    root, history, _session, packet = mac_archive
    recovery.publish_mac_recovery(root, recovery.stage_mac_recovery(root, packet))
    sidecar = history.with_name("macos_recovery.json")
    original = sidecar.read_bytes()
    for key in ("main_sha256", "wal_sha256", "snapshot_id"):
        changed = deepcopy(packet)
        changed["source"][key] = _hash("different")
        with pytest.raises(ValueError):
            recovery.stage_mac_recovery(root, changed)
    changed = deepcopy(packet)
    changed["messages"][0]["content_sha256"] = _hash("different-source-content")
    with pytest.raises(ValueError):
        recovery.stage_mac_recovery(root, changed)
    changed = deepcopy(packet)
    changed["messages"] = changed["messages"][:1]
    changed["messages"][0]["server_id"] = "987"
    with pytest.raises(ValueError):
        recovery.stage_mac_recovery(root, changed)
    assert sidecar.read_bytes() == original


def test_existing_server_conflict_and_duplicate_packet_events(mac_archive):
    root, history, session, packet = mac_archive
    duplicate = deepcopy(packet)
    duplicate["messages"].append(deepcopy(duplicate["messages"][0]))
    assert recovery.stage_mac_recovery(root, duplicate)["total"] == 5
    with sqlite3.connect(history) as connection:
        connection.execute(
            "UPDATE messages SET provenance=? WHERE conversation_id=? AND ordinal=1",
            (json.dumps({"server_id": packet["messages"][0]["server_id"]}), session),
        )
    with pytest.raises(ValueError):
        recovery.stage_mac_recovery(root, packet)


def test_changed_archive_or_sidecar_invalidates_staged_candidate(mac_archive):
    root, history, _session, packet = mac_archive
    staged = recovery.stage_mac_recovery(root, packet)
    with sqlite3.connect(history) as connection:
        connection.execute("INSERT INTO archive_info VALUES('changed','true')")
    with pytest.raises(ValueError):
        recovery.publish_mac_recovery(root, staged)
    assert not history.with_name("macos_recovery.json").exists()


def test_invalid_seal_and_symlink_fail_closed(mac_archive, tmp_path):
    root, history, session, packet = mac_archive
    recovery.publish_mac_recovery(root, recovery.stage_mac_recovery(root, packet))
    path = history.with_name("macos_recovery.json")
    value = json.loads(path.read_text())
    value["messages"][0]["content_sha256"] = _hash("tampered")
    path.write_text(json.dumps(value))
    detail = WechatArchiveRepository(root).query(
        session=session, persist_event_cache=False
    )
    assert detail["count"] == 2
    assert detail["resource_index_status"] == "invalid"
    with pytest.raises(ValueError):
        recovery.stage_mac_recovery(root, packet)
    moved = tmp_path / "moved.json"
    path.rename(moved)
    path.symlink_to(moved)
    with pytest.raises(ValueError):
        recovery.stage_mac_recovery(root, packet)


def test_runtime_authority_cannot_be_spoofed_by_json(mac_archive):
    root, _history, session, packet = mac_archive
    recovery.publish_mac_recovery(root, recovery.stage_mac_recovery(root, packet))
    rows = WechatArchiveRepository(root).query(
        session=session, persist_event_cache=False
    )["messages"]
    row = next(row for row in rows if recovery.validated_recovery_receipt(row))
    assert recovery.validated_recovery_receipt(row)
    copied = {**row, "_macos_recovery_validated": True}
    assert recovery.validated_recovery_receipt(copied) is None


def test_event_allowlist_and_cache_fingerprint_include_mac_sidecar(
    mac_archive, monkeypatch
):
    from app.core import wechat_conversation_events as events

    root, history, _session, packet = mac_archive
    contacts = history.with_name("contacts.sqlite3")
    with sqlite3.connect(contacts) as connection:
        connection.execute("CREATE TABLE synthetic(id INTEGER)")
    repository = WechatArchiveRepository(root)
    before = events._fingerprint(events._inputs(repository))
    recovery.publish_mac_recovery(root, recovery.stage_mac_recovery(root, packet))
    after = events._fingerprint(events._inputs(repository))
    assert before != after
    assert str(history.with_name("macos_recovery.json").resolve()) in events._inputs(
        repository
    )
    assert "macos_recovery_timeline_messages" in events._TABLES
