"""Newer native metadata needs an intact preservation receipt and text witness.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import wechat_native_resource_revalidation as refresh
from app.core.wechat_archive import WechatArchiveRepository, _identifier, _time_fields
from app.core.wechat_resource_index import _SCHEMA
from tests import test_wechat_native_resource_revalidation as fixtures
from tests.test_wechat_native_resource_refresh import _resource


native_metadata = fixtures.native_metadata
OWNER = "wxid_account_a"
BACKUP, DATABASE = "c" * 64, "d" * 64


def _write_receipt(fixture):
    raw = json.dumps(fixture.manifest_value).encode()
    fixture.manifest.write_bytes(raw)
    fixture.receipt["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
    with sqlite3.connect(fixture.supplemental) as output:
        output.execute(
            "INSERT OR REPLACE INTO archive_info VALUES('source_receipts',?)",
            (json.dumps([fixture.receipt]),),
        )


@pytest.fixture
def witnessed_metadata(native_metadata):
    fixture = native_metadata
    relative = (
        "Documents/" + fixture.conversation["partition_id"][4:] + "/DB/message_2.sqlite"
    )
    file_id = hashlib.sha1(
        ("AppDomain-com.tencent.xin-" + relative).encode(), usedforsecurity=False
    ).hexdigest()
    table = (
        "Chat_"
        + hashlib.md5(
            fixture.conversation["peer_id"].encode(), usedforsecurity=False
        ).hexdigest()
    )
    metadata = {
        "source_id": fixture.conversation["partition_id"],
        "source_account_id": OWNER,
        "account_hash_verified": True,
        "account_hash": fixture.conversation["partition_id"][4:],
        "snapshot_id": BACKUP,
        "source_inventory_file": "Original native snapshot inventory.json",
        "identity_evidence": {
            "kind": "native_login_info_with_directory_md5",
            "directory_hash": fixture.conversation["partition_id"][4:],
            "directory_hash_algorithm": "MD5-UTF8",
            "login_info_sha256": "f" * 64,
        },
    }
    encoded = json.dumps(metadata)
    witness_id = _identifier("synthetic-newer-native-text-witness")
    proof = {
        "record_id": "native-text-witness-50",
        "source_snapshot": 2,
        "server_id": "10050",
        "unix_timestamp": 1650000050,
        "message_type": 1,
        "text_extraction_kind": "plain_text",
        "locator": {
            "native_direction": 1,
            "database": "message_2.sqlite",
            "database_relative_path": relative,
            "database_sha256": DATABASE,
            "file_id": file_id,
            "table": table,
            "local_id": 50,
            "row_id": 50,
        },
    }
    clock = _time_fields(
        {"sent_at_utc_iso": datetime.fromtimestamp(1650000050, UTC).isoformat()}
    )
    with sqlite3.connect(fixture.history) as primary:
        primary.execute(
            "INSERT INTO source_snapshots VALUES(2,?,?,?)",
            (BACKUP, fixture.conversation["partition_id"], encoded),
        )
        primary.execute(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                witness_id,
                fixture.conversation["id"],
                50,
                fixture.conversation["peer_id"],
                "Recorded sender",
                "received",
                "Preserved native text witness",
                *clock,
                json.dumps(proof),
                "[]",
                "preserved native text witness",
            ),
        )
        primary.execute(
            "UPDATE conversations SET message_count=message_count+1,last_sort=?,last_time=? WHERE id=?",
            (clock[0], clock[1], fixture.conversation["id"]),
        )
    supplemental = fixture.store.parent / "witnessed-native-metadata.sqlite3"
    resource = _resource(
        fixture.resources[1],
        fixture.conversation,
        OWNER,
        BACKUP,
        DATABASE,
        51,
        "image",
        3,
        "[Image]",
    )
    with (
        sqlite3.connect(supplemental) as output,
        sqlite3.connect(fixture.original) as source,
    ):
        output.executescript(_SCHEMA)
        conversation = list(source.execute("SELECT * FROM conversations").fetchone())
        conversation[6:] = (
            1,
            resource[7],
            resource[7],
            resource[8],
            resource[8],
            resource[6],
        )
        output.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", conversation
        )
        output.execute(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", resource
        )
    manifest = fixture.store.parent / "complete-preservation-manifest.json"
    manifest_value = {
        "snapshot_id": BACKUP,
        "status": "complete",
        "all_snapshot_hashes_equal_source_hashes": True,
        "selected_files": [
            {
                "domain": "AppDomain-com.tencent.xin",
                "relative_path": relative,
                "input_relative_path": "inputs/" + relative,
                "file_id": file_id,
                "bytes": 4096,
                "sha256": DATABASE,
                "snapshot_sha256": DATABASE,
                "source_sha256": DATABASE,
            }
        ],
    }
    receipt = {
        "source_snapshot_id": 2,
        "source_id": fixture.conversation["partition_id"],
        "snapshot_id": BACKUP,
        "metadata_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
        "database_relative_path": relative,
        "database_sha256": DATABASE,
        "database_bytes": 4096,
        "file_id": file_id,
        "manifest_path": str(manifest),
        "original_source_inventory_file": metadata["source_inventory_file"],
    }
    result = SimpleNamespace(
        **vars(fixture),
        supplemental=supplemental,
        manifest=manifest,
        manifest_value=manifest_value,
        receipt=receipt,
        witness_id=witness_id,
        newer_metadata=metadata,
        resource=resource,
    )
    _write_receipt(result)
    return result


def _stage(fixture):
    candidate = fixture.original.with_name("witnessed-candidate.sqlite3")
    return refresh.stage_extended_native_resource_revalidation(
        fixture.store,
        candidate,
        conversation_ids=[fixture.conversation["id"]],
        supplemental_path=fixture.supplemental,
    )


def test_newer_ipad_format_requires_manifest_and_current_native_text_then_reads_offline(
    witnessed_metadata,
    monkeypatch,
):
    fixture = witnessed_metadata
    before = (
        fixture.history.read_bytes(),
        fixture.original.read_bytes(),
        fixture.supplemental.read_bytes(),
        fixture.manifest.read_bytes(),
    )
    report = _stage(fixture)
    assert report["media_payload_files_read"] == report["cloud_inputs_hydrated"] == 0
    refresh.publish_native_resource_revalidation(
        fixture.store,
        report,
        rollback_path=fixture.store.parent / "unused-rollback.sqlite3",
    )
    read_bytes = Path.read_bytes

    def no_manifest(path):
        assert path != fixture.manifest, (
            "Sealed reads must not hydrate a cloud manifest."
        )
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", no_manifest)
    repository = WechatArchiveRepository(fixture.store)
    page = repository.query(session=fixture.conversation["id"])
    assert page["resource_index_status"] == "ready" and page["count"] == 5
    assert fixture.resource[0] in {row["id"] for row in page["messages"]}
    assert repository.resource_message(fixture.resource[0])["account_id"] == OWNER
    assert (
        fixture.history.read_bytes(),
        fixture.original.read_bytes(),
        fixture.supplemental.read_bytes(),
    ) == before[:3]


@pytest.mark.parametrize(
    "defect",
    ["account", "digest", "witness_snapshot", "witness_direction", "witness_peer"],
)
def test_unproved_newer_ipad_receipt_never_creates_a_candidate(
    witnessed_metadata, defect
):
    fixture = witnessed_metadata
    if defect == "account":
        metadata = deepcopy(fixture.newer_metadata)
        metadata["source_account_id"] = "wxid_other_owner"
        encoded = json.dumps(metadata)
        with sqlite3.connect(fixture.history) as primary:
            primary.execute(
                "UPDATE source_snapshots SET metadata=? WHERE id=2", (encoded,)
            )
        fixture.receipt["metadata_sha256"] = hashlib.sha256(
            encoded.encode()
        ).hexdigest()
        _write_receipt(fixture)
    elif defect == "digest":
        fixture.manifest_value["selected_files"][0]["source_sha256"] = "e" * 64
        _write_receipt(fixture)
    else:
        with sqlite3.connect(fixture.history) as primary:
            proof = json.loads(
                primary.execute(
                    "SELECT provenance FROM messages WHERE id=?", (fixture.witness_id,)
                ).fetchone()[0]
            )
            if defect == "witness_snapshot":
                proof["source_snapshot"] = 1
            elif defect == "witness_direction":
                proof["locator"]["native_direction"] = 0
            else:
                proof["locator"]["table"] = "Chat_" + "0" * 32
            primary.execute(
                "UPDATE messages SET provenance=? WHERE id=?",
                (json.dumps(proof), fixture.witness_id),
            )
    before = (
        fixture.history.read_bytes(),
        fixture.original.read_bytes(),
        fixture.supplemental.read_bytes(),
    )
    with pytest.raises(ValueError):
        _stage(fixture)
    assert not fixture.original.with_name("witnessed-candidate.sqlite3").exists()
    assert not fixture.history.with_name("resource_revalidated.sqlite3").exists()
    assert (
        fixture.history.read_bytes(),
        fixture.original.read_bytes(),
        fixture.supplemental.read_bytes(),
    ) == before


@pytest.mark.parametrize(
    "defect", ["incomplete", "snapshot", "bytes", "duplicate_member"]
)
def test_incomplete_or_ambiguous_preservation_manifest_is_refused(
    witnessed_metadata, defect
):
    fixture = witnessed_metadata
    if defect == "incomplete":
        fixture.manifest_value["status"] = "copying"
    elif defect == "snapshot":
        fixture.manifest_value["snapshot_id"] = "e" * 64
    elif defect == "bytes":
        fixture.manifest_value["selected_files"][0]["bytes"] += 1
    else:
        fixture.manifest_value["selected_files"].append(
            deepcopy(fixture.manifest_value["selected_files"][0])
        )
    _write_receipt(fixture)
    with pytest.raises(ValueError):
        _stage(fixture)
    assert not fixture.original.with_name("witnessed-candidate.sqlite3").exists()


def test_cloud_only_manifest_is_not_opened_or_hydrated(witnessed_metadata, monkeypatch):
    fixture = witnessed_metadata
    lstat = Path.lstat

    def cloud(path):
        state = lstat(path)
        if path == fixture.manifest:
            return SimpleNamespace(
                **{
                    key: getattr(state, key)
                    for key in dir(state)
                    if key.startswith("st_") and key != "st_flags"
                },
                st_flags=0x40000000,
            )
        return state

    read_bytes = Path.read_bytes

    def no_cloud_read(path):
        assert path != fixture.manifest
        return read_bytes(path)

    monkeypatch.setattr(Path, "lstat", cloud)
    monkeypatch.setattr(Path, "read_bytes", no_cloud_read)
    with pytest.raises(ValueError, match="resident"):
        _stage(fixture)
    assert not fixture.original.with_name("witnessed-candidate.sqlite3").exists()


@pytest.mark.parametrize("invalid", [None, 42, "not-a-receipt", []])
def test_malformed_receipt_entry_is_a_controlled_refusal(witnessed_metadata, invalid):
    fixture = witnessed_metadata
    with sqlite3.connect(fixture.supplemental) as source:
        source.execute(
            "UPDATE archive_info SET value=? WHERE key='source_receipts'",
            (json.dumps([invalid]),),
        )
    before = (
        fixture.history.read_bytes(),
        fixture.original.read_bytes(),
        fixture.supplemental.read_bytes(),
    )
    with pytest.raises(ValueError):
        _stage(fixture)
    assert not fixture.original.with_name("witnessed-candidate.sqlite3").exists()
    assert (
        fixture.history.read_bytes(),
        fixture.original.read_bytes(),
        fixture.supplemental.read_bytes(),
    ) == before
