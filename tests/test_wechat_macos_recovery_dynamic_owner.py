"""Dynamic source owners retain exact witnesses and legacy proof boundaries.

Code version: v1.0.2-codex.0
"""

from contextlib import closing
from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from app.core import wechat_macos_recovery_bank as bank
from app.core.wechat_archive import WechatArchiveRepository
from tests import test_wechat_macos_recovery_account_binding as binding_fixtures
from tests import test_wechat_macos_recovery_bank as bank_fixtures


mac_archive = binding_fixtures.mac_archive
bank_archive = binding_fixtures.bank_archive
source_binding = binding_fixtures.source_binding
OWNER = binding_fixtures.OWNER
PEER = binding_fixtures.PEER


def _dynamic_packet(fixture, owner_row=17):
    packet = deepcopy(fixture.packet)
    binding = packet["account_binding"]
    binding["schema_version"] = 2
    binding["owner_proof"] = bank_fixtures._proof(owner_row, OWNER)
    for anchor in binding["anchors"]:
        if anchor["direction"] == "sent":
            anchor["real_sender_id"] = owner_row
            anchor["sender_proof"] = deepcopy(binding["owner_proof"])
    for message in packet["messages"]:
        if message["direction"] == "sent":
            message["real_sender_id"] = owner_row
            message["sender_proof"] = deepcopy(binding["owner_proof"])
    return packet


@pytest.mark.parametrize("owner_row", [1, 17, 2**31 + 1])
def test_dynamic_owner_proof_stages_publishes_and_replays_without_source_changes(
    source_binding, owner_row
):
    fixture = source_binding
    packet = _dynamic_packet(fixture, owner_row)
    original = fixture.history.read_bytes()
    candidate, receipt = binding_fixtures._stage(fixture, packet)
    assert receipt["added"] == 1
    assert not fixture.fixture.bank_path.exists()
    assert fixture.history.read_bytes() == original
    bank.publish_mac_recovery_bank(fixture.root, candidate, receipt)
    with closing(sqlite3.connect(fixture.fixture.bank_path)) as connection, connection:
        retained = [
            json.loads(row[0])
            for row in connection.execute("SELECT proof FROM sources")
        ]
    assert len(retained) == 1
    assert retained[0]["account_binding"] == packet["account_binding"]
    result = WechatArchiveRepository(fixture.root).query(
        session=fixture.target_session, persist_event_cache=False
    )
    assert result["count"] == 2
    assert sum(
        row["text"] == packet["messages"][0]["text"] for row in result["messages"]
    ) == 1
    repeated, replay = binding_fixtures._stage(
        fixture, packet, name="dynamic-owner-replay"
    )
    assert replay["added"] == replay["upgraded"] == replay["occurrences_added"] == 0
    assert replay["existing"] == 1
    bank.publish_mac_recovery_bank(fixture.root, repeated, replay)
    assert fixture.history.read_bytes() == original


@pytest.mark.parametrize(
    "change",
    [
        "owner_name",
        "owner_hash",
        "owner_row_zero",
        "owner_row_bool",
        "sent_row",
        "sent_proof",
        "account",
        "schema_bool",
    ],
)
def test_dynamic_owner_identity_and_sent_witness_must_agree(source_binding, change):
    fixture = source_binding
    packet = _dynamic_packet(fixture)
    binding = packet["account_binding"]
    sent = next(
        anchor for anchor in binding["anchors"] if anchor["direction"] == "sent"
    )
    if change == "owner_name":
        binding["owner_proof"] = bank_fixtures._proof(17, "wxid_other_owner")
    elif change == "owner_hash":
        binding["owner_proof"]["row_sha256"] = "a" * 64
    elif change == "owner_row_zero":
        binding["owner_proof"] = bank_fixtures._proof(0, OWNER)
    elif change == "owner_row_bool":
        binding["owner_proof"] = bank_fixtures._proof(True, OWNER)
    elif change == "sent_row":
        sent.update(real_sender_id=18, sender_proof=bank_fixtures._proof(18, OWNER))
    elif change == "sent_proof":
        sent["sender_proof"] = bank_fixtures._proof(17, OWNER, is_session=1)
    elif change == "account":
        binding["account_id"] = "wxid_other_owner"
    else:
        binding["schema_version"] = True
    original = fixture.history.read_bytes()
    with pytest.raises(ValueError):
        binding_fixtures._stage(fixture, packet)
    assert not fixture.fixture.bank_path.exists()
    assert fixture.history.read_bytes() == original


@pytest.mark.parametrize(
    "source_field",
    ["source_id", "snapshot_id", "main_sha256", "wal_sha256", "database_relative_path"],
)
def test_dynamic_owner_proof_cannot_move_between_authenticated_sources(
    source_binding, source_field
):
    fixture = source_binding
    packet = _dynamic_packet(fixture)
    packet["account_binding"]["source"][source_field] = {
        "source_id": "macos-different-source",
        "snapshot_id": "different-committed-snapshot",
        "main_sha256": "c" * 64,
        "wal_sha256": "d" * 64,
        "database_relative_path": "db_storage/message/message_9.db",
    }[source_field]
    with pytest.raises(ValueError):
        binding_fixtures._stage(fixture, packet)
    assert not fixture.fixture.bank_path.exists()


@pytest.mark.parametrize("change", ["one_anchor", "no_sent_owner", "not_type1"])
def test_dynamic_owner_still_requires_independent_native_text_witnesses(
    source_binding, change
):
    fixture = source_binding
    packet = _dynamic_packet(fixture)
    binding = packet["account_binding"]
    if change == "one_anchor":
        binding["anchors"] = binding["anchors"][:1]
    elif change == "no_sent_owner":
        sent = next(
            anchor for anchor in binding["anchors"] if anchor["direction"] == "sent"
        )
        sent.update(
            sender_id=PEER,
            direction="received",
            real_sender_id=4,
            sender_proof=bank_fixtures._proof(4, PEER, is_session=1),
        )
    else:
        binding["anchors"][0]["message_type"] = 49
    with pytest.raises(ValueError):
        binding_fixtures._stage(fixture, packet)
    assert not fixture.fixture.bank_path.exists()


def test_dynamic_owner_witnesses_cannot_be_two_events_from_one_peer(source_binding):
    fixture = source_binding
    packet = _dynamic_packet(fixture)
    binding = packet["account_binding"]
    first = binding["anchors"][0]
    identifier = fixture.fixture.direct["anchors"][1]["archive_message_id"]
    with closing(sqlite3.connect(fixture.history)) as connection, connection:
        connection.row_factory = sqlite3.Row
        original = connection.execute(
            "SELECT * FROM messages WHERE id=?", (identifier,)
        ).fetchone()
    proof = json.loads(original["provenance"])
    second = deepcopy(first)
    second.update(
        archive_message_id=original["id"],
        server_id=str(proof["server_id"]),
        create_time=proof["unix_timestamp"],
        direction=original["direction"],
        sender_id=original["sender_id"],
        text_sha256=hashlib.sha256(original["text"].encode()).hexdigest(),
        local_id=1_003,
        content_sha256=hashlib.sha256(
            ("synthetic-native-source:" + original["text"]).encode()
        ).hexdigest(),
        real_sender_id=4,
        sender_proof=bank_fixtures._proof(4, PEER, is_session=1),
    )
    binding["anchors"] = [first, second]
    with pytest.raises(ValueError, match="distinct peers"):
        binding_fixtures._stage(fixture, packet)
    assert not fixture.fixture.bank_path.exists()


def test_dynamic_owner_witness_must_match_the_original_primary_row(source_binding):
    fixture = source_binding
    packet = _dynamic_packet(fixture)
    anchor = packet["account_binding"]["anchors"][0]
    with closing(sqlite3.connect(fixture.history)) as connection, connection:
        connection.execute(
            "UPDATE messages SET text='A changed primary witness' WHERE id=?",
            (anchor["archive_message_id"],),
        )
    with pytest.raises(ValueError, match="exact native archive identity"):
        binding_fixtures._stage(fixture, packet)
    assert not fixture.fixture.bank_path.exists()


def test_legacy_schema_does_not_silently_accept_a_dynamic_owner_row(source_binding):
    fixture = source_binding
    packet = _dynamic_packet(fixture)
    packet["account_binding"]["schema_version"] = 1
    with pytest.raises(ValueError):
        binding_fixtures._stage(fixture, packet)
    assert not fixture.fixture.bank_path.exists()


@pytest.mark.parametrize(
    "legacy_version", ["v1.2.0-codex.0", "v1.2.1-codex.0", "v1.2.2-codex.0"]
)
def test_dynamic_staging_retains_legacy_bank_payload_and_fixed_owner_proof(
    source_binding, legacy_version
):
    fixture = source_binding
    candidate, receipt = binding_fixtures._stage(fixture)
    bank.publish_mac_recovery_bank(fixture.root, candidate, receipt)
    with closing(sqlite3.connect(fixture.fixture.bank_path)) as connection, connection:
        connection.execute(
            "UPDATE archive_info SET value=? WHERE key='code_version'",
            (legacy_version,),
        )
        old_messages = connection.execute(
            "SELECT * FROM messages ORDER BY id"
        ).fetchall()
        old_sources = connection.execute(
            "SELECT id,proof FROM sources ORDER BY id"
        ).fetchall()
    old_bytes = fixture.fixture.bank_path.read_bytes()
    staged, repeated = binding_fixtures._stage(
        fixture, _dynamic_packet(fixture), name="dynamic-legacy-bank-candidate"
    )
    assert repeated["added"] == repeated["occurrences_added"] == 0
    assert repeated["existing"] == 1
    assert fixture.fixture.bank_path.read_bytes() == old_bytes
    with closing(sqlite3.connect(staged)) as connection, connection:
        assert (
            connection.execute("SELECT * FROM messages ORDER BY id").fetchall()
            == old_messages
        )
        retained = dict(connection.execute("SELECT id,proof FROM sources"))
        assert all(retained[identifier] == proof for identifier, proof in old_sources)
        assert connection.execute(
            "SELECT value FROM archive_info WHERE key='code_version'"
        ).fetchone()[0] == bank.VERSION
    bank.publish_mac_recovery_bank(fixture.root, staged, repeated)
    result = WechatArchiveRepository(fixture.root).query(
        session=fixture.target_session, persist_event_cache=False
    )
    assert result["count"] == 2
