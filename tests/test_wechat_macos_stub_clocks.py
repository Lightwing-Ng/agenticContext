"""One-second semantic stub upgrades retain both observed clocks and native proof.

Code version: v1.0.0-codex.0
"""

from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from app.core import wechat_macos_bank_merge as merge
from app.core import wechat_macos_recovery_bank as bank
from scripts import refresh_wechat_globally as runner
from tests import test_wechat_macos_recovery_bank_upgrades as fixtures
from tests import test_wechat_macos_reparse as repairs


mac_archive = fixtures.mac_archive
bank_archive = fixtures.bank_archive
app_stubs = fixtures.app_stubs


def _packet(fixture, delta=1):
    packet = deepcopy(fixture.packets[0])
    packet["messages"][0]["create_time"] += delta
    return packet


def _stage(fixture, packet, name="clock-candidate"):
    return fixtures.bank_fixtures._stage(fixture.fixture, packets=[packet], name=name)


def _clock_row(path):
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return dict(connection.execute("SELECT * FROM messages ORDER BY id LIMIT 1").fetchone())


@pytest.mark.parametrize("delta", (-1, 1))
def test_clock_upgrade_publication_and_replay_keep_catalog_display_and_native_epoch(app_stubs, delta):
    fixture = app_stubs
    packet = _packet(fixture, delta)
    original = fixture.originals[packet["messages"][0]["server_id"]]
    catalog = fixture.catalog.read_bytes()
    candidate, receipt = _stage(fixture, packet)
    assert receipt["upgraded"] == 1
    bank.publish_mac_recovery_bank(fixture.root, candidate, receipt)
    published = _clock_row(fixture.fixture.bank_path)

    replay, repeated = _stage(fixture, packet, "clock-idempotent-replay")

    assert repeated["added"] == repeated["upgraded"] == repeated["reparsed"] == 0
    assert _clock_row(replay) == published
    for key in ("id", "ordinal", "time_sort", "timestamp_value", "timestamp_label", "time_basis"):
        assert published[key] == original[key]
    proof = json.loads(published["provenance"])
    assert proof["unix_timestamp"] == packet["messages"][0]["create_time"]
    assert proof["macos_recovery"]["create_time"] == packet["messages"][0]["create_time"]
    assert proof["macos_stub_upgrade"]["clock_observations"]["native_minus_catalog_seconds"] == delta
    assert fixture.catalog.read_bytes() == catalog


@pytest.mark.parametrize(
    "defect", (
        "delta", "native_epoch", "catalog_epoch", "source_key", "original_row_hash",
        "observation", "missing_shadow",
    )
)
def test_resealed_clock_receipt_tampering_is_rejected_by_the_reader(app_stubs, defect):
    fixture = app_stubs
    candidate, _receipt = _stage(fixture, _packet(fixture))
    with sqlite3.connect(candidate) as connection:
        row = connection.execute("SELECT id,provenance FROM messages").fetchone()
        proof = json.loads(row[1])
        upgrade = proof["macos_stub_upgrade"]
        if defect == "delta":
            upgrade["clock_observations"]["native_minus_catalog_seconds"] = -1
        elif defect == "native_epoch":
            upgrade["clock_observations"]["native"]["epoch"] += 1
        elif defect == "catalog_epoch":
            upgrade["clock_observations"]["catalog"]["epoch"] += 1
        elif defect == "source_key":
            upgrade["native_source_key"] = "f" * 64
        elif defect == "original_row_hash":
            upgrade["original_row_sha256"] = "0" * 64
            connection.execute(
                "UPDATE shadow_replacements SET original_row_sha256=? WHERE original_id=?",
                (upgrade["original_row_sha256"], row[0]),
            )
        elif defect == "observation":
            observed = connection.execute("SELECT id,proof FROM occurrences").fetchone()
            message = json.loads(observed[1])
            message["create_time"] += 1
            connection.execute(
                "UPDATE occurrences SET proof=? WHERE id=?", (bank._json(message), observed[0])
            )
        else:
            connection.execute("DELETE FROM shadow_replacements WHERE original_id=?", (row[0],))
        connection.execute("UPDATE messages SET provenance=? WHERE id=?", (bank._json(proof), row[0]))
        connection.execute(
            "UPDATE shadow_replacements SET proof=? WHERE original_id=?",
            (bank._json(upgrade), row[0]),
        )
        connection.execute(
            "UPDATE archive_info SET value=? WHERE key='payload_sha256'", (bank._rows_digest(connection),)
        )
    with sqlite3.connect(candidate) as connection:
        with pytest.raises(ValueError):
            bank._validate_bank(connection, fixture.history)


@pytest.mark.parametrize(
    "defect", ("missing_epoch", "string_epoch", "boolean_epoch", "zero_epoch", "unknown_sender", "wrong_peer")
)
def test_one_second_policy_does_not_relax_other_identity_or_epoch_boundaries(app_stubs, defect):
    fixture = app_stubs
    packet = _packet(fixture)
    message = packet["messages"][0]
    original = deepcopy(fixture.originals[message["server_id"]])
    proof = json.loads(original["provenance"])
    if defect == "missing_epoch":
        proof.pop("unix_timestamp")
    elif defect == "string_epoch":
        proof["unix_timestamp"] = str(proof["unix_timestamp"])
    elif defect == "boolean_epoch":
        proof["unix_timestamp"] = True
    elif defect == "zero_epoch":
        proof["unix_timestamp"] = 0
    elif defect == "unknown_sender":
        original["sender_id"] = None
    else:
        proof["resource_reference"]["peer_id"] = "wxid_another_synthetic_peer"
    original["provenance"] = bank._json(proof)
    stub = {
        "original_row": bank._json(original), "source_file_name": fixture.catalog.name,
        "source_file_sha256": hashlib.sha256(fixture.catalog.read_bytes()).hexdigest(),
    }

    assert bank._upgrade_stub(stub, packet, message) is None


@pytest.mark.parametrize("delta", (-1, 1))
def test_sealed_bank_merge_replays_clock_upgrades_without_changing_catalog_time(app_stubs, delta):
    fixture = app_stubs
    packet = _packet(fixture, delta)
    # Create the required current bank from an independent group source first.
    fixtures._publish(fixture, packets=[fixture.packets[1]], name="current-before-clock-global")
    incoming, _receipt = _stage(fixture, packet, "sealed-clock-global")
    original_incoming = incoming.read_bytes()
    original_target = fixture.fixture.bank_path.read_bytes()
    candidate = fixture.history.with_name("merged-clock-global.sqlite3")

    with bank.mac_recovery_publication_guard(fixture.root) as guard:
        before = runner.bank_summary(fixture.fixture.bank_path)
        receipt = merge.stage_merged_mac_recovery_bank(
            fixture.root, incoming, candidate, account_ids=(fixtures.OWNER,),
            publication_guard=guard,
        )
        runner.verify_preservation(fixture.fixture.bank_path, candidate, before, receipt)

    assert fixture.fixture.bank_path.read_bytes() == original_target
    assert incoming.read_bytes() == original_incoming
    assert receipt["upgraded"] == 1
    with sqlite3.connect(candidate) as connection:
        connection.row_factory = sqlite3.Row
        original = fixture.originals[packet["messages"][0]["server_id"]]
        row = dict(connection.execute("SELECT * FROM messages WHERE id=?", (original["id"],)).fetchone())
        assert tuple(row[key] for key in bank._STUB_CLOCK_FIELDS) == tuple(
            original[key] for key in bank._STUB_CLOCK_FIELDS
        )
        bank._validate_bank(connection, fixture.history)


def test_latest_semantic_projection_merge_retains_the_earlier_catalog_clock(app_stubs):
    fixture = app_stubs
    partial, complete = repairs._packets(fixture.fixture, "quote")
    packets = []
    native_coordinate = fixture.packets[0]["messages"][0]
    for projection in (partial, complete):
        packet = deepcopy(fixture.packets[0])
        message = deepcopy(projection["messages"][0])
        for key in (
            "local_id", "server_id", "create_time", "sender_id", "sender_label",
            "direction", "real_sender_id", "sender_proof",
        ):
            message[key] = deepcopy(
                native_coordinate.get("sender_label", native_coordinate["sender_id"])
                if key == "sender_label" else native_coordinate[key]
            )
        message["create_time"] += 1
        packet["messages"] = [message]
        packets.append(packet)
    partial_candidate, partial_receipt = _stage(fixture, packets[0], "partial-clock-shadow")
    bank.publish_mac_recovery_bank(fixture.root, partial_candidate, partial_receipt)
    incoming, _receipt = _stage(fixture, packets[1], "complete-clock-shadow")
    candidate = fixture.history.with_name("merged-latest-clock-shadow.sqlite3")

    with bank.mac_recovery_publication_guard(fixture.root) as guard:
        before = runner.bank_summary(fixture.fixture.bank_path)
        receipt = merge.stage_merged_mac_recovery_bank(
            fixture.root, incoming, candidate, account_ids=(fixtures.OWNER,),
            publication_guard=guard,
        )
        runner.verify_preservation(fixture.fixture.bank_path, candidate, before, receipt)

    assert receipt["reparsed"] == 1 and receipt["merge"]["synthesized_latest_projections"] == 1
    row = _clock_row(candidate)
    original = fixture.originals[native_coordinate["server_id"]]
    assert row["text"] == packets[1]["messages"][0]["text"]
    assert tuple(row[key] for key in bank._STUB_CLOCK_FIELDS) == tuple(
        original[key] for key in bank._STUB_CLOCK_FIELDS
    )
    assert json.loads(row["provenance"])["macos_recovery"]["create_time"] == native_coordinate["create_time"] + 1
    with sqlite3.connect(candidate) as connection:
        bank._validate_bank(connection, fixture.history)
