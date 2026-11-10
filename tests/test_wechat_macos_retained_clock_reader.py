"""Retained source-clock receipts require their exact catalog and native witnesses.

Code version: v1.0.0-codex.0
"""

from copy import deepcopy
import json
import sqlite3

import pytest

from app.core import wechat_macos_recovery_bank as bank
from tests import test_wechat_macos_recovery_bank_upgrades as upgrades
from tests import test_wechat_macos_bank_v4_reader as revisions


mac_archive = upgrades.mac_archive
bank_archive = upgrades.bank_archive
app_stubs = upgrades.app_stubs
retained_v4 = revisions.retained_v4


@pytest.fixture
def retained_clock(app_stubs):
    fixture = app_stubs
    upgrades._publish(fixture)
    connection = sqlite3.connect(fixture.fixture.bank_path)
    connection.row_factory = sqlite3.Row
    original = fixture.originals[fixture.quote["server_id"]]
    row = dict(
        connection.execute(
            "SELECT * FROM messages WHERE id=?", (original["id"],)
        ).fetchone()
    )
    proof = json.loads(row["provenance"])
    native_epoch = proof["unix_timestamp"] + 1
    proof["unix_timestamp"] = native_epoch
    proof["macos_recovery"]["create_time"] = native_epoch
    source, encoded = connection.execute(
        "SELECT source_id,proof FROM occurrences WHERE canonical_id=?", (row["id"],)
    ).fetchone()
    observation = json.loads(encoded)
    observation["create_time"] = native_epoch
    upgrade = proof["macos_stub_upgrade"]
    upgrade.update(
        upgrade_basis=bank._STUB_CLOCK_POLICY,
        native_source_key=source,
        clock_observations={
            "catalog": bank._catalog_clock(original),
            "native": {"epoch": native_epoch},
            "native_minus_catalog_seconds": 1,
        },
    )
    row["provenance"] = bank._json(proof)
    signature = bank._event_signature(row)
    connection.execute(
        "UPDATE messages SET provenance=? WHERE id=?", (row["provenance"], row["id"])
    )
    connection.execute(
        "UPDATE shadow_replacements SET proof=? WHERE original_id=?",
        (bank._json(upgrade), row["id"]),
    )
    connection.execute(
        "UPDATE occurrences SET proof=?,signature=? WHERE canonical_id=?",
        (bank._json(observation), signature, row["id"]),
    )
    connection.execute(
        "UPDATE event_keys SET signature=? WHERE message_id=?", (signature, row["id"])
    )
    revisions._seal(connection)
    try:
        yield fixture, connection, row, upgrade
    finally:
        connection.close()


def test_retained_clocks_preserve_both_original_observations_without_writes(
    retained_clock,
):
    fixture, connection, row, upgrade = retained_clock
    before = (fixture.fixture.bank_path.read_bytes(), fixture.catalog.read_bytes())
    assert bank._validate_bank(connection, fixture.history)["schema_version"] == "3"
    assert upgrade["clock_observations"]["native_minus_catalog_seconds"] == 1
    assert (
        row["time_sort"] == fixture.originals[fixture.quote["server_id"]]["time_sort"]
    )
    assert (
        fixture.fixture.bank_path.read_bytes(),
        fixture.catalog.read_bytes(),
    ) == before


@pytest.mark.parametrize(
    "defect",
    [
        "native_source_key",
        "delta",
        "catalog_clock",
        "canonical_receipt",
        "lost_occurrence",
        "original_catalog",
    ],
)
def test_forged_clock_receipt_is_rejected_after_payload_resealing(
    retained_clock, defect
):
    fixture, connection, row, retained = retained_clock
    upgrade = deepcopy(retained)
    if defect == "native_source_key":
        upgrade["native_source_key"] = "f" * 64
    elif defect == "delta":
        upgrade["clock_observations"]["native_minus_catalog_seconds"] = -1
    elif defect == "catalog_clock":
        upgrade["clock_observations"]["catalog"]["time_sort"] += 1
    elif defect == "lost_occurrence":
        connection.execute("DELETE FROM occurrences WHERE canonical_id=?", (row["id"],))
    elif defect == "original_catalog":
        with sqlite3.connect(fixture.catalog) as catalog:
            catalog.execute(
                "UPDATE messages SET text='Changed original catalog row' WHERE id=?",
                (row["id"],),
            )
    if defect != "canonical_receipt":
        proof = json.loads(row["provenance"])
        proof["macos_stub_upgrade"] = upgrade
        connection.execute(
            "UPDATE messages SET provenance=? WHERE id=?",
            (bank._json(proof), row["id"]),
        )
    else:
        upgrade["native_source_key"] = "f" * 64
    connection.execute(
        "UPDATE shadow_replacements SET proof=? WHERE original_id=?",
        (bank._json(upgrade), row["id"]),
    )
    revisions._seal(connection)
    with pytest.raises(ValueError):
        bank._validate_bank(connection, fixture.history)


def test_schema4_chain_cannot_hide_an_orphan_retained_clock_claim(retained_v4):
    fixture, connection, prior, following, source, reason = retained_v4
    proof = json.loads(following["provenance"])
    proof["macos_stub_upgrade"] = {"upgrade_basis": bank._STUB_CLOCK_POLICY}
    following["provenance"] = bank._json(proof)
    revisions._record(connection, prior, following, source, reason)
    revisions._seal(connection)
    with pytest.raises(ValueError, match="lost its original shadow receipt"):
        bank._validate_bank(connection, fixture.history)
