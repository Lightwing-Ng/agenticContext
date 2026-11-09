"""Source-level account evidence authorizes only existing, exactly bound scopes.

Code version: v1.0.1-codex.0
"""

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import wechat_macos_recovery_bank as bank
from app.core.wechat_archive import WechatArchiveRepository, _identifier, _time_fields
from tests import test_wechat_macos_recovery_bank as bank_fixtures


mac_archive = bank_fixtures.mac_archive
bank_archive = bank_fixtures.bank_archive
OWNER = bank_fixtures.OWNER
PEER = bank_fixtures.PEER
ROOM = bank_fixtures.ROOM
MEMBER = bank_fixtures.MEMBER
TARGET = "wxid_synthetic_underanchored_peer"


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _table(peer):
    return "Msg_" + hashlib.md5(peer.encode(), usedforsecurity=False).hexdigest()


def _stamp_senders(packet):
    if packet["peer_id"] == PEER:
        packet["peer_proof"] = bank_fixtures._proof(4, PEER, is_session=1)
    for message in packet["messages"]:
        if not message["sender_id"]:
            continue
        native = {OWNER: 2, PEER: 4, MEMBER: 3}[message["sender_id"]]
        message["real_sender_id"] = native
        message["sender_proof"] = bank_fixtures._proof(
            native,
            message["sender_id"],
            is_session=1 if message["sender_id"] == PEER else 0,
        )


@pytest.fixture
def source_binding(bank_archive):
    fixture = bank_archive
    for packet in fixture.packets:
        _stamp_senders(packet)
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        for row in connection.execute(
            "SELECT id,timestamp_value,provenance FROM messages"
        ).fetchall():
            proof = json.loads(row["provenance"])
            proof["unix_timestamp"] = int(
                datetime.fromisoformat(row["timestamp_value"]).timestamp()
            )
            connection.execute(
                "UPDATE messages SET provenance=? WHERE id=?",
                (json.dumps(proof), row["id"]),
            )
        first_owner = fixture.direct["anchors"][0]["archive_message_id"]
        connection.execute(
            "UPDATE messages SET sender_id=?,sender_label=?,direction='sent' WHERE id=?",
            (OWNER, OWNER, first_owner),
        )
        partition = connection.execute("SELECT id FROM partitions").fetchone()[0]
        session = "wechat:" + _identifier(partition, TARGET)
        identifier = _sha("existing-underanchored-native-resource")
        order, stamp, label, basis = _time_fields(
            {"sent_at_utc_iso": "2023-11-14T22:13:40+00:00"}
        )
        connection.execute(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                identifier,
                session,
                1,
                TARGET,
                TARGET,
                "received",
                "[Image]",
                order,
                stamp,
                label,
                basis,
                json.dumps(
                    {
                        "server_id": "401",
                        "message_type": 3,
                        "unix_timestamp": int(order),
                    }
                ),
                "[]",
                "[image]",
            ),
        )
        connection.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                session,
                partition,
                "account:" + OWNER,
                TARGET,
                "Existing underanchored peer",
                "direct",
                1,
                order,
                order,
                stamp,
                stamp,
                "[Image]",
            ),
        )
        anchors = []
        for number, (packet, peer_row) in enumerate(
            ((fixture.direct, 4), (fixture.group, 10)), 1
        ):
            original = dict(
                connection.execute(
                    "SELECT * FROM messages WHERE id=?",
                    (packet["anchors"][0]["archive_message_id"],),
                ).fetchone()
            )
            proof = json.loads(original["provenance"])
            sender = original["sender_id"]
            sender_row = 2 if sender == OWNER else 3
            anchors.append(
                {
                    "archive_message_id": original["id"],
                    "peer_id": packet["peer_id"],
                    "peer_proof": bank_fixtures._proof(
                        peer_row, packet["peer_id"], is_session=1
                    ),
                    "server_id": proof["server_id"],
                    "message_type": 1,
                    "create_time": proof["unix_timestamp"],
                    "direction": original["direction"],
                    "sender_id": sender,
                    "text_sha256": _sha(original["text"]),
                    "table": _table(packet["peer_id"]),
                    "local_id": 1_000 + number,
                    "content_sha256": _sha(
                        "synthetic-native-source:" + original["text"]
                    ),
                    "real_sender_id": sender_row,
                    "sender_proof": bank_fixtures._proof(sender_row, sender),
                }
            )
    packet = deepcopy(fixture.direct)
    packet.update(
        receipt_id="underanchored-global-account-proof",
        session_id=session,
        peer_id=TARGET,
        peer_proof=bank_fixtures._proof(5, TARGET, is_session=1),
        anchors=[],
    )
    packet["source"].update(
        table=_table(TARGET), account_directory=OWNER + "_syntheticcapture"
    )
    message = bank_fixtures._message(
        80, 1, raw="Recovered literal body in the existing scope", sender=OWNER
    )
    message.update(real_sender_id=2, sender_proof=bank_fixtures._proof(2, OWNER))
    packet["messages"] = [message]
    packet["account_binding"] = {
        "schema_version": 1,
        "account_id": OWNER,
        "account_directory": packet["source"]["account_directory"],
        "owner_proof": bank_fixtures._proof(2, OWNER),
        "source": {
            key: packet["source"][key]
            for key in (
                "source_id",
                "snapshot_id",
                "main_sha256",
                "wal_sha256",
                "database_relative_path",
            )
        },
        "anchors": anchors,
    }
    return SimpleNamespace(
        fixture=fixture,
        root=fixture.root,
        history=fixture.history,
        packet=packet,
        target_session=session,
        target_original=identifier,
    )


def _stage(fixture, packet=None, name="account-binding-candidate"):
    return bank_fixtures._stage(
        fixture.fixture,
        packets=[packet if packet is not None else fixture.packet],
        name=name,
    )


@pytest.mark.parametrize("local_count", [0, 1])
def test_global_proof_authorizes_only_the_existing_underanchored_scope(
    source_binding, local_count
):
    fixture = source_binding
    packet = deepcopy(fixture.packet)
    if local_count:
        body = "The only original local Type 1 anchor"
        with sqlite3.connect(fixture.history) as connection:
            proof = json.loads(
                connection.execute(
                    "SELECT provenance FROM messages WHERE id=?",
                    (fixture.target_original,),
                ).fetchone()[0]
            )
            proof["message_type"] = 1
            connection.execute(
                "UPDATE messages SET text=?,search_text=?,provenance=? WHERE id=?",
                (body, body.casefold(), json.dumps(proof), fixture.target_original),
            )
        packet["anchors"] = [
            {
                "archive_message_id": fixture.target_original,
                "server_id": "401",
                "text_sha256": _sha(body),
            }
        ]
    before = fixture.history.read_bytes()
    candidate, receipt = _stage(fixture, packet)
    assert receipt["added"] == 1
    assert fixture.history.read_bytes() == before
    bank.publish_mac_recovery_bank(fixture.root, candidate, receipt)
    result = WechatArchiveRepository(fixture.root).query(
        session=fixture.target_session, persist_event_cache=False
    )
    assert result["count"] == result["current"]["message_count"] == 2
    assert (
        sum(row["text"] == packet["messages"][0]["text"] for row in result["messages"])
        == 1
    )
    assert (
        WechatArchiveRepository(fixture.root).query(persist_event_cache=False)[
            "conversation_count"
        ]
        == 3
    )
    assert fixture.history.read_bytes() == before


def test_two_independent_sent_owner_anchors_are_valid(source_binding):
    fixture = source_binding
    packet = deepcopy(fixture.packet)
    anchor = packet["account_binding"]["anchors"][1]
    with sqlite3.connect(fixture.history) as connection:
        connection.execute(
            "UPDATE messages SET sender_id=?,sender_label=?,direction='sent' WHERE id=?",
            (OWNER, OWNER, anchor["archive_message_id"]),
        )
    anchor.update(
        sender_id=OWNER,
        direction="sent",
        real_sender_id=2,
        sender_proof=bank_fixtures._proof(2, OWNER),
    )
    _, receipt = _stage(fixture, packet)
    assert receipt["added"] == 1


@pytest.mark.parametrize(
    "source_key",
    ["source_id", "snapshot_id", "main_sha256", "wal_sha256", "database_relative_path"],
)
def test_global_proof_cannot_cross_a_source_capture(source_binding, source_key):
    fixture = source_binding
    packet = deepcopy(fixture.packet)
    packet["account_binding"]["source"][source_key] = {
        "source_id": "macos-another-synthetic-source",
        "snapshot_id": "another-synthetic-snapshot",
        "main_sha256": "c" * 64,
        "wal_sha256": "d" * 64,
        "database_relative_path": "db_storage/message/message_1.db",
    }[source_key]
    with pytest.raises(ValueError):
        _stage(fixture, packet)
    assert not fixture.fixture.bank_path.exists()


@pytest.mark.parametrize(
    "change",
    [
        "owner",
        "owner_row",
        "owner_hash",
        "directory",
        "directory_mismatch",
        "extra_field",
        "schema",
        "one_anchor",
        "duplicate_anchor",
        "one_peer",
        "no_sent_owner",
        "unknown_scope",
    ],
)
def test_incomplete_or_cross_account_bindings_are_refused(source_binding, change):
    fixture = source_binding
    packet = deepcopy(fixture.packet)
    binding = packet["account_binding"]
    if change == "owner":
        binding["account_id"] = "wxid_another_native_owner"
    elif change == "owner_row":
        binding["owner_proof"] = bank_fixtures._proof(1, OWNER)
    elif change == "owner_hash":
        binding["owner_proof"]["row_sha256"] = "a" * 64
    elif change == "directory":
        binding["account_directory"] = packet["source"]["account_directory"] = (
            "wxid_another_native_owner_capture"
        )
    elif change == "directory_mismatch":
        binding["account_directory"] = OWNER + "_anothercapture"
    elif change == "extra_field":
        binding["raw_payload"] = "Unknown fields do not establish authority"
    elif change == "schema":
        binding["schema_version"] = True
    elif change == "one_anchor":
        binding["anchors"] = binding["anchors"][:1]
    elif change == "duplicate_anchor":
        binding["anchors"] = [binding["anchors"][0], deepcopy(binding["anchors"][0])]
    elif change == "one_peer":
        with sqlite3.connect(fixture.history) as connection:
            connection.row_factory = sqlite3.Row
            original = connection.execute(
                "SELECT * FROM messages WHERE id=?",
                (fixture.fixture.direct["anchors"][1]["archive_message_id"],),
            ).fetchone()
        proof = json.loads(original["provenance"])
        second = deepcopy(binding["anchors"][0])
        second.update(
            archive_message_id=original["id"],
            server_id=proof["server_id"],
            create_time=proof["unix_timestamp"],
            direction=original["direction"],
            sender_id=PEER,
            text_sha256=_sha(original["text"]),
            local_id=1_002,
            content_sha256=_sha("synthetic-native-source:" + original["text"]),
            real_sender_id=4,
            sender_proof=bank_fixtures._proof(4, PEER, is_session=1),
        )
        binding["anchors"][1] = second
    elif change == "no_sent_owner":
        anchor = binding["anchors"][0]
        with sqlite3.connect(fixture.history) as connection:
            connection.execute(
                "UPDATE messages SET sender_id=?,direction='received' WHERE id=?",
                (PEER, anchor["archive_message_id"]),
            )
        anchor.update(
            sender_id=PEER,
            direction="received",
            real_sender_id=4,
            sender_proof=bank_fixtures._proof(4, PEER, is_session=1),
        )
    else:
        packet["session_id"] = "wechat:" + "e" * 64
    before = fixture.history.read_bytes()
    with pytest.raises(ValueError):
        _stage(fixture, packet)
    assert fixture.history.read_bytes() == before
    assert not fixture.fixture.bank_path.exists()


@pytest.mark.parametrize(
    "change",
    [
        "archive_id",
        "peer",
        "peer_hash",
        "server",
        "epoch",
        "direction",
        "sender",
        "text_hash",
        "table",
        "local_id",
        "content_hash",
        "sender_row",
        "sender_hash",
        "type",
        "extra_field",
    ],
)
def test_each_native_archive_anchor_must_independently_agree(source_binding, change):
    fixture = source_binding
    packet = deepcopy(fixture.packet)
    anchor = packet["account_binding"]["anchors"][0]
    if change == "archive_id":
        anchor["archive_message_id"] = "f" * 64
    elif change == "peer":
        anchor["peer_id"] = TARGET
        anchor["peer_proof"] = bank_fixtures._proof(5, TARGET, is_session=1)
        anchor["table"] = _table(TARGET)
    elif change == "peer_hash":
        anchor["peer_proof"]["row_sha256"] = "a" * 64
    elif change == "server":
        anchor["server_id"] = "999999"
    elif change == "epoch":
        anchor["create_time"] += 1
    elif change == "direction":
        anchor["direction"] = "received"
    elif change == "sender":
        anchor.update(
            sender_id=PEER,
            real_sender_id=4,
            sender_proof=bank_fixtures._proof(4, PEER, is_session=1),
        )
    elif change == "text_hash":
        anchor["text_sha256"] = "a" * 64
    elif change == "table":
        anchor["table"] = _table(TARGET)
    elif change == "local_id":
        anchor["local_id"] = 0
    elif change == "content_hash":
        anchor["content_sha256"] = "not-a-digest"
    elif change == "sender_row":
        anchor["sender_proof"] = bank_fixtures._proof(99, OWNER)
    elif change == "sender_hash":
        anchor["sender_proof"]["row_sha256"] = "a" * 64
    elif change == "type":
        anchor["message_type"] = 49
    else:
        anchor["unreviewed_native_payload"] = (
            "Unexpected fields are not source evidence"
        )
    with pytest.raises(ValueError):
        _stage(fixture, packet)
    assert not fixture.fixture.bank_path.exists()


@pytest.mark.parametrize("local_count", [0, 1])
def test_underanchored_packets_without_global_proof_remain_rejected(
    source_binding, local_count
):
    fixture = source_binding
    packet = deepcopy(fixture.packet)
    packet.pop("account_binding")
    packet["source"].pop("account_directory")
    if local_count:
        packet["anchors"] = deepcopy(fixture.fixture.direct["anchors"][:1])
    with pytest.raises(ValueError):
        _stage(fixture, packet)


def test_original_two_local_anchor_contract_needs_no_global_binding(source_binding):
    fixture = source_binding
    packet = deepcopy(fixture.fixture.direct)
    assert len(packet["anchors"]) == 2 and "account_binding" not in packet
    candidate, receipt = _stage(fixture, packet)
    assert receipt["added"] == 5
    bank.publish_mac_recovery_bank(fixture.root, candidate, receipt)
    assert (
        WechatArchiveRepository(fixture.root).query(
            session=packet["session_id"], persist_event_cache=False
        )["count"]
        == 7
    )


@pytest.mark.parametrize("damage", ["missing_epoch", "not_type1", "unverified_account"])
def test_global_archive_witnesses_require_explicit_native_type_clock_and_account(
    source_binding, damage
):
    fixture = source_binding
    anchor = fixture.packet["account_binding"]["anchors"][0]
    with sqlite3.connect(fixture.history) as connection:
        if damage == "unverified_account":
            connection.execute(
                "UPDATE partitions SET identity_status='Unverified export'"
            )
        else:
            proof = json.loads(
                connection.execute(
                    "SELECT provenance FROM messages WHERE id=?",
                    (anchor["archive_message_id"],),
                ).fetchone()[0]
            )
            if damage == "missing_epoch":
                proof.pop("unix_timestamp")
            else:
                proof["message_type"] = 49
            connection.execute(
                "UPDATE messages SET provenance=? WHERE id=?",
                (json.dumps(proof), anchor["archive_message_id"]),
            )
    with pytest.raises(ValueError):
        _stage(fixture)


def test_two_local_anchors_do_not_silently_bypass_a_bad_supplied_global_proof(
    source_binding,
):
    fixture = source_binding
    packet = deepcopy(fixture.fixture.direct)
    packet["source"]["account_directory"] = fixture.packet["source"][
        "account_directory"
    ]
    packet["account_binding"] = deepcopy(fixture.packet["account_binding"])
    packet["account_binding"]["owner_proof"]["row_sha256"] = "a" * 64
    with pytest.raises(ValueError):
        _stage(fixture, packet)


def test_legacy_local_mode_does_not_accept_unbound_capture_directory_metadata(
    source_binding,
):
    fixture = source_binding
    packet = deepcopy(fixture.fixture.direct)
    packet["source"]["account_directory"] = OWNER + "_syntheticcapture"
    with pytest.raises(ValueError):
        _stage(fixture, packet)


def test_global_archive_validation_is_cached_once_and_archive_drift_invalidates_it(
    source_binding,
):
    fixture = source_binding
    packet = fixture.packet
    anchors = packet["account_binding"]["anchors"]
    reads = []

    def trace(statement):
        normalized = " ".join(statement.casefold().split())
        if (
            "from main.messages m" in normalized
            and "join main.conversations c" in normalized
            and any(anchor["archive_message_id"] in statement for anchor in anchors)
        ):
            reads.append(statement)

    # Match staging's immutable read path rather than relying on SQLite to
    # refresh an ordinary reader's page cache after another connection writes.
    connection = sqlite3.connect(
        fixture.history.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.set_trace_callback(trace)
        first = bank._binding(connection, packet)
        assert first["id"] == fixture.target_session
        assert len(reads) == len(anchors)
        assert bank._binding(connection, packet) == first
        assert len(reads) == len(anchors)
        with sqlite3.connect(fixture.history) as writer:
            writer.execute(
                "UPDATE messages SET text=text || ' changed after validation' WHERE id=?",
                (anchors[0]["archive_message_id"],),
            )
        with pytest.raises(ValueError):
            bank._binding(connection, packet)
    finally:
        connection.close()
