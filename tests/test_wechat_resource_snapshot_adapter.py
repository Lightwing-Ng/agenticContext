"""Legacy receipt adapters preserve original snapshots without guessed aliases.

Code version: v1.0.1-codex.0
"""

from copy import deepcopy
import hashlib
import json

import pytest

from app.core import wechat_resource_snapshot_adapter as adapter


OWNER = "wxid_synthetic_snapshot_owner"
HASHED = hashlib.md5(OWNER.encode()).hexdigest()
PATH = "Documents/" + HASHED + "/DB/MM.sqlite"
SHA = "a" * 64
BACKUP = "synthetic-retained-backup"
FILE_ID = hashlib.sha1(("AppDomain-com.tencent.xin-" + PATH).encode()).hexdigest()
KEY = (OWNER, BACKUP, PATH, SHA)


def fixture(shape, sid=25):
    metadata = {
        "source_id": "ios-" + HASHED,
        "source_account_id": OWNER,
        "account_hash": HASHED,
        "account_hash_verified": True,
        "backup_id": BACKUP,
    }
    if shape == "member":
        proof = {
            "bytes": 512,
            "member_path": "Preserved archive/" + PATH,
            "sha256": SHA,
        }
        metadata.update(
            input_databases=[proof],
            container_kind="legacy_ios",
            archive_sha256="b" * 64,
            identity_sha256="c" * 64,
        )
    elif shape == "native":
        occurrence = {
            "relative_path": PATH,
            "account_hash": HASHED,
            "sha256": SHA,
            "file_id": FILE_ID,
            "object": "objects/" + SHA,
            "kind": "DB/MM.sqlite",
            "manifest_sha256": "b" * 64,
            "bytes": 512,
            "backup_folder": "original-native-backup",
        }
        proof = {"native_occurrences": [occurrence]}
        metadata.update(
            native_occurrences=[occurrence],
            native_restore_version="v1.0.0",
            source_inventory_sha256="d" * 64,
        )
    else:
        proof = {
            "entry": {
                "domain": "AppDomain-com.tencent.xin",
                "relative_path": PATH,
                "input_relative_path": "inputs/" + PATH,
                "file_id": FILE_ID,
                "sha256": SHA,
                "snapshot_sha256": SHA,
                "source_sha256": SHA,
                "bytes": 512,
            },
            "report": "/relocated/preservation-report.json",
            "report_sha256": "e" * 64,
            "sources": "/relocated/sources.json",
            "sources_sha256": "f" * 64,
        }
        metadata.update(source_inventory_file="/original/preservation-report.json")
    snapshot = {
        "id": sid,
        "snapshot_id": BACKUP,
        "source_id": "ios-" + HASHED,
        "metadata": adapter._json(metadata),
    }
    source = {
        "source_snapshot_id": sid,
        "database_relative_path": PATH,
        "database_sha256": SHA,
        "account_id": OWNER,
        "source_id": "ios-" + HASHED,
        "backup_id": BACKUP,
        "metadata_sha256": hashlib.sha256(snapshot["metadata"].encode()).hexdigest(),
        "file_id": FILE_ID,
        "receipt_basis": "preservation_manifest",
        "proof": adapter._json(proof),
    }
    return snapshot, source


@pytest.mark.parametrize("shape", ["member", "native", "report"])
def test_existing_primary_snapshot_is_resolved_from_exact_retained_catalog_receipt(
    shape,
):
    snapshot, source = fixture(shape)
    original = deepcopy((snapshot, source))
    index = adapter.ResourceSnapshotAdapter({}, [snapshot], [source])
    resolved = index.resolve(KEY)
    assert resolved["source_snapshot_id"] == snapshot["id"]
    assert (
        resolved["receipt_proof"]["source_snapshot_metadata_sha256"]
        == source["metadata_sha256"]
    )
    assert resolved["receipt_proof"]["catalog_source_receipt_sha256"] == adapter._sha(
        source
    )
    assert (snapshot, source) == original
    assert not index.rejected


def test_existing_successful_exact_resolution_is_byte_compatible_and_independent_of_fallback():
    snapshot, source = fixture("report")
    source["metadata_sha256"] = "0" * 64
    index = adapter.ResourceSnapshotAdapter(
        {KEY: [snapshot["id"]]}, [snapshot], [source]
    )
    assert index.resolve(KEY) == {
        "source_snapshot_id": snapshot["id"],
        "receipt_proof": None,
    }
    assert not index.rejected
    assert sum(index.ignored.values()) == 1


def test_mm_alias_selects_only_explicit_receipt_link_not_the_smallest_snapshot():
    snapshot, source = fixture("member", sid=8)
    index = adapter.ResourceSnapshotAdapter({KEY: [6, 8]}, [snapshot], [source])
    assert index.resolve(KEY)["source_snapshot_id"] == 8
    with pytest.raises(ValueError):
        index.resolve(KEY, existing_source_snapshot=6)


def test_two_explicit_alias_receipts_stay_ambiguous_without_original_row_snapshot():
    first, a = fixture("native", sid=6)
    second, b = fixture("native", sid=8)
    index = adapter.ResourceSnapshotAdapter({KEY: [6, 8]}, [first, second], [a, b])
    with pytest.raises(ValueError):
        index.resolve(KEY)
    assert index.resolve(KEY, existing_source_snapshot=8)["source_snapshot_id"] == 8


@pytest.mark.parametrize(
    "field",
    [
        "source_snapshot_id",
        "source_id",
        "backup_id",
        "metadata_sha256",
        "database_sha256",
        "file_id",
        "account_id",
    ],
)
def test_mismatched_snapshot_owner_hash_or_physical_database_is_never_admitted(field):
    snapshot, source = fixture("report")
    source[field] = 99 if field == "source_snapshot_id" else "tampered"
    index = adapter.ResourceSnapshotAdapter({}, [snapshot], [source])
    with pytest.raises(ValueError):
        index.resolve(KEY)
    assert sum(index.rejected.values()) == 1


@pytest.mark.parametrize("shape", ["member", "native", "report"])
def test_receipt_body_checksum_mismatch_cannot_be_fixed_by_rehashing_the_row(shape):
    snapshot, source = fixture(shape)
    proof = json.loads(source["proof"])
    if shape == "member":
        proof["sha256"] = "b" * 64
    elif shape == "native":
        proof["native_occurrences"][0]["object"] = "objects/" + "b" * 64
    else:
        proof["entry"]["source_sha256"] = "b" * 64
    source["proof"] = adapter._json(proof)
    index = adapter.ResourceSnapshotAdapter({}, [snapshot], [source])
    with pytest.raises(ValueError):
        index.resolve(KEY)


def test_catalog_receipt_cannot_create_a_missing_primary_snapshot():
    _snapshot, source = fixture("report")
    index = adapter.ResourceSnapshotAdapter({}, [], [source])
    with pytest.raises(ValueError):
        index.resolve(KEY)


@pytest.mark.parametrize("value", [True, False, "25", 25.0, 0, -1])
def test_existing_snapshot_id_cannot_use_boolean_or_coerced_identity(value):
    snapshot, source = fixture("report")
    index = adapter.ResourceSnapshotAdapter({KEY: [25]}, [snapshot], [source])
    with pytest.raises(ValueError):
        index.resolve(KEY, existing_source_snapshot=value)


def test_optional_primary_inventory_hash_must_match_its_already_named_report():
    snapshot, source = fixture("report")
    metadata = json.loads(snapshot["metadata"])
    metadata["source_inventory_sha256"] = "0" * 64
    snapshot["metadata"] = adapter._json(metadata)
    source["metadata_sha256"] = hashlib.sha256(
        snapshot["metadata"].encode()
    ).hexdigest()
    index = adapter.ResourceSnapshotAdapter({}, [snapshot], [source])
    with pytest.raises(ValueError):
        index.resolve(KEY)
    metadata["source_inventory_sha256"] = json.loads(source["proof"])["report_sha256"]
    snapshot["metadata"] = adapter._json(metadata)
    source["metadata_sha256"] = hashlib.sha256(
        snapshot["metadata"].encode()
    ).hexdigest()
    index = adapter.ResourceSnapshotAdapter({}, [snapshot], [source])
    assert index.resolve(KEY)["source_snapshot_id"] == 25


@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_same_snapshot_receipts_never_select_the_first(reverse):
    snapshot, source = fixture("report")
    other = deepcopy(source)
    proof = json.loads(other["proof"])
    proof["sources_sha256"] = "b" * 64
    other["proof"] = adapter._json(proof)
    receipts = [source, other]
    if reverse:
        receipts.reverse()
    index = adapter.ResourceSnapshotAdapter({}, [snapshot], receipts)
    with pytest.raises(ValueError):
        index.resolve(KEY)
    assert len(index.conflicts) == 1
    assert adapter.VERSION == "v1.0.0-codex.0"
