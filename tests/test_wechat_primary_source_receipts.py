"""Primary database receipts preserve native identity and independent evidence.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from app.core import wechat_primary_source_receipts as receipts
from tests import test_wechat_native_resource_revalidation as fixtures


native_metadata = fixtures.native_metadata


@pytest.fixture
def primary_receipts(native_metadata):
    fixture = native_metadata
    with sqlite3.connect(fixture.history) as primary:
        metadata = deepcopy(fixture.metadata)
        del metadata["backup_id"]
        del metadata["input_databases"]
        metadata["snapshot_id"] = "ipad-pro-original-snapshot"
        metadata["identity_evidence"] = {
            "directory_hash": fixture.conversation["partition_id"][4:],
        }
        primary.execute(
            "UPDATE source_snapshots SET snapshot_id=?,metadata=? WHERE id=1",
            (metadata["snapshot_id"], json.dumps(metadata)),
        )
        raw = primary.execute(
            "SELECT provenance FROM messages WHERE id=?",
            (fixture.primary_id,),
        ).fetchone()[0]
        proof = json.loads(raw)
        path = (
            "Documents/"
            + fixture.conversation["partition_id"][4:]
            + "/DB/message_2.sqlite"
        )
        proof["locator"].update(
            database_relative_path=path,
            database_sha256=fixtures.DATABASE,
            file_id=hashlib.sha1(
                ("AppDomain-com.tencent.xin-" + path).encode(),
                usedforsecurity=False,
            ).hexdigest(),
        )
        primary.execute(
            "UPDATE messages SET provenance=? WHERE id=?",
            (json.dumps(proof), fixture.primary_id),
        )
    fixture.primary_source_metadata = metadata
    fixture.primary_source_proof = proof
    return fixture


def _read(fixture, conversation=None):
    with sqlite3.connect(fixture.history) as primary:
        return receipts.primary_source_database_receipts(
            primary,
            conversation or fixture.conversation,
            1,
        )


def test_independent_primary_receipt_works_without_legacy_manifest_fields(
    primary_receipts,
):
    fixture = primary_receipts
    before = fixture.history.read_bytes(), fixture.original.read_bytes()
    result = _read(fixture)
    path = fixture.primary_source_proof["locator"]["database_relative_path"]
    assert result["basis"] == "primary_source_database_receipt"
    assert result["owner"] == fixtures.OWNER
    assert result["backup_id"] == "ipad-pro-original-snapshot"
    assert result["native_partition_id"] == fixture.conversation["partition_id"]
    assert result["receipts"] == {
        ("ipad-pro-original-snapshot", path): fixtures.DATABASE
    }
    assert result["snapshots"][1][1] == {path: fixtures.DATABASE}
    assert result["snapshots"][1][2] == {
        "source_snapshot_id": 1,
        "source_id": fixture.conversation["partition_id"],
        "snapshot_id": "ipad-pro-original-snapshot",
        "metadata_sha256": hashlib.sha256(
            json.dumps(fixture.primary_source_metadata).encode(),
        ).hexdigest(),
    }
    assert result["anchors_checked"] == 1
    assert (fixture.history.read_bytes(), fixture.original.read_bytes()) == before


def test_same_account_html_conversation_can_retain_exact_native_source_receipts(
    primary_receipts,
):
    fixture = primary_receipts
    conversation = deepcopy(fixture.conversation)
    conversation["partition_id"] = "html-" + "c" * 24
    with sqlite3.connect(fixture.history) as primary:
        primary.execute(
            "UPDATE conversations SET partition_id=? WHERE id=?",
            (conversation["partition_id"], conversation["id"]),
        )
    result = _read(fixture, conversation)
    assert result["native_partition_id"] == fixture.conversation["partition_id"]
    assert result["owner"] == fixtures.OWNER
    assert result["anchors_checked"] == 1


@pytest.mark.parametrize(
    "damage",
    [
        "path",
        "database",
        "digest",
        "file_id",
        "table",
        "account",
        "snapshot",
        "directory",
        "placeholder",
    ],
)
def test_receipt_conflicts_are_rejected(primary_receipts, damage):
    fixture = primary_receipts
    proof = deepcopy(fixture.primary_source_proof)
    metadata = deepcopy(fixture.primary_source_metadata)
    if damage == "path":
        proof["locator"]["database_relative_path"] = (
            "Documents/another-owner/DB/message_2.sqlite"
        )
    elif damage == "database":
        proof["locator"]["database"] = "message_7.sqlite"
    elif damage == "digest":
        proof["locator"]["database_sha256"] = "not-a-receipt"
    elif damage == "file_id":
        proof["locator"]["file_id"] = "f" * 40
    elif damage == "table":
        proof["locator"]["table"] = "Chat_" + "f" * 32
    elif damage == "account":
        metadata["source_account_id"] = "wxid_other"
    elif damage == "snapshot":
        metadata["snapshot_id"] = "another-snapshot"
    elif damage == "directory":
        metadata["identity_evidence"]["directory_hash"] = "f" * 32
    elif damage == "placeholder":
        proof["text_extraction_kind"] = "resource_placeholder"
    with sqlite3.connect(fixture.history) as primary:
        primary.execute(
            "UPDATE source_snapshots SET metadata=? WHERE id=1", (json.dumps(metadata),)
        )
        primary.execute(
            "UPDATE messages SET provenance=? WHERE id=?",
            (json.dumps(proof), fixture.primary_id),
        )
    before = fixture.history.read_bytes()
    with pytest.raises(ValueError):
        _read(fixture)
    assert fixture.history.read_bytes() == before


def test_same_database_with_different_primary_checksum_is_rejected(primary_receipts):
    fixture = primary_receipts
    with sqlite3.connect(fixture.history) as primary:
        row = list(
            primary.execute(
                "SELECT * FROM messages WHERE id=?", (fixture.primary_id,)
            ).fetchone()
        )
        row[0] = "independent-other-primary-id"
        proof = deepcopy(fixture.primary_source_proof)
        proof["locator"]["local_id"] = proof["locator"]["row_id"] = 20
        proof["locator"]["database_sha256"] = "f" * 64
        row[11] = json.dumps(proof)
        primary.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
    with pytest.raises(ValueError, match="conflicting checksum"):
        _read(fixture)


def test_no_matching_primary_rows_cannot_be_proved_by_a_sidecar(primary_receipts):
    fixture = primary_receipts
    proof = deepcopy(fixture.primary_source_proof)
    proof["source_snapshot"] = 999
    with sqlite3.connect(fixture.history) as primary:
        primary.execute(
            "UPDATE messages SET provenance=? WHERE id=?",
            (json.dumps(proof), fixture.primary_id),
        )
    with pytest.raises(ValueError, match="No independent"):
        _read(fixture)


def test_a_changed_conversation_account_cannot_borrow_native_receipts(primary_receipts):
    fixture = primary_receipts
    conversation = deepcopy(fixture.conversation)
    conversation["account_key"] = "account:wxid_other"
    with sqlite3.connect(fixture.history) as primary:
        primary.execute(
            "UPDATE conversations SET account_key=? WHERE id=?",
            (conversation["account_key"], conversation["id"]),
        )
    with pytest.raises(ValueError, match="snapshot account"):
        _read(fixture, conversation)


def test_primary_receipt_scan_has_a_hard_metadata_budget(primary_receipts, monkeypatch):
    monkeypatch.setattr(receipts, "MAX_BYTES", 1)
    with pytest.raises(ValueError, match="budget"):
        _read(primary_receipts)
