"""Resolve existing snapshots through exact retained resource-source receipts.

Code version: v1.0.1-codex.1
This pure adapter never discovers files or creates source declarations. Its caller
must pin the primary archive and the already validated resource catalog.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
from pathlib import PurePosixPath
import re

CODE_VERSION = "v1.0.1-codex.1"
PROOF_VERSION = "v1.0.0-codex.0"
VERSION = PROOF_VERSION
_HEX = re.compile(r"[0-9a-f]{64}")
_SOURCE_FIELDS = {
    "source_snapshot_id",
    "database_relative_path",
    "database_sha256",
    "account_id",
    "source_id",
    "backup_id",
    "metadata_sha256",
    "file_id",
    "receipt_basis",
    "proof",
}


def _json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _object(raw):
    if not isinstance(raw, str) or len(raw.encode()) > 16 * 1024**2:
        raise ValueError("A retained snapshot receipt exceeds its object budget.")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("A retained snapshot receipt needs object metadata.")
    return result


def _digest(value):
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def _relative(value, owner_hash):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("A resource source path has no exact native frame.")
    path = PurePosixPath(value)
    parts = path.parts
    if path.is_absolute() or ".." in parts or parts.count("Documents") != 1:
        raise ValueError("A resource source path escapes its retained archive frame.")
    relative = parts[parts.index("Documents") :]
    if (
        len(relative) != 4
        or relative[:3] != ("Documents", owner_hash, "DB")
        or re.fullmatch(r"(?:MM|message_[0-9]+)\.sqlite", relative[3]) is None
    ):
        raise ValueError("A resource source path crosses its native account directory.")
    return "/".join(relative)


def validate_catalog_source_receipt(snapshot, receipt):
    """Validate only the three observed legacy receipt shapes against primary metadata."""
    if not isinstance(receipt, dict) or set(receipt) != _SOURCE_FIELDS:
        raise ValueError("A resource source receipt has unsupported fields.")
    if not isinstance(snapshot, dict) or set(snapshot) != {
        "id",
        "snapshot_id",
        "source_id",
        "metadata",
    }:
        raise ValueError(
            "A resource source needs its complete existing primary snapshot."
        )
    metadata = _object(snapshot["metadata"])
    owner = receipt["account_id"]
    if not isinstance(owner, str) or not owner:
        raise ValueError("A resource source needs an explicit native owner.")
    # Native WeChat account directories require this MD5 identifier.
    # codeql[py/weak-sensitive-data-hashing] Not a security or credential digest.
    hashed = hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest()
    relative = _relative(receipt["database_relative_path"], hashed)
    digest = receipt["database_sha256"]
    file_id = hashlib.sha1(
        ("AppDomain-com.tencent.xin-" + relative).encode(), usedforsecurity=False
    ).hexdigest()
    if (
        type(receipt["source_snapshot_id"]) is not int
        or receipt["source_snapshot_id"] < 1
        or receipt["source_snapshot_id"] != snapshot["id"]
        or receipt["source_id"] != snapshot["source_id"]
        or snapshot["source_id"] != "ios-" + hashed
        or receipt["backup_id"] != snapshot["snapshot_id"]
        or metadata.get("source_id") != snapshot["source_id"]
        or metadata.get("source_account_id") != owner
        or metadata.get("account_hash_verified") is not True
        or metadata.get("account_hash") != hashed
        or receipt["metadata_sha256"]
        != hashlib.sha256(snapshot["metadata"].encode()).hexdigest()
        or not _digest(digest)
        or receipt["file_id"] != file_id
        or receipt["receipt_basis"] != "preservation_manifest"
        or relative != receipt["database_relative_path"]
    ):
        raise ValueError(
            "A resource receipt differs from its exact primary owner, snapshot or file."
        )
    backup = metadata.get("backup_id", metadata.get("snapshot_id"))
    if (
        backup is None
        and metadata.get("native_restore_version")
        and _digest(metadata.get("source_inventory_sha256"))
    ):
        backup = snapshot["snapshot_id"]
    if backup != snapshot["snapshot_id"]:
        raise ValueError("A resource receipt lost its retained backup declaration.")
    proof = _object(receipt["proof"])
    if set(proof) == {"bytes", "member_path", "sha256"}:
        if (
            proof not in metadata.get("input_databases", ())
            or _relative(proof["member_path"], hashed) != relative
            or proof["sha256"] != digest
            or type(proof["bytes"]) is not int
            or proof["bytes"] < 0
            or metadata.get("container_kind") != "legacy_ios"
            or not _digest(metadata.get("archive_sha256"))
            or not _digest(metadata.get("identity_sha256"))
        ):
            raise ValueError(
                "A member-path receipt lost its exact archive declaration."
            )
        shape = "legacy_archive_member_path"
    elif set(proof) == {"native_occurrences"}:
        occurrences = proof["native_occurrences"]
        if not isinstance(occurrences, list) or not 1 <= len(occurrences) <= 4096:
            raise ValueError(
                "A restored source needs bounded retained native occurrences."
            )
        for occurrence in occurrences:
            if not isinstance(occurrence, dict) or (
                occurrence not in metadata.get("native_occurrences", ())
                or _relative(occurrence.get("relative_path"), hashed) != relative
                or occurrence.get("account_hash") != hashed
                or occurrence.get("sha256") != digest
                or occurrence.get("file_id") != file_id
                or occurrence.get("object") != "objects/" + digest
                or occurrence.get("kind") != "DB/" + PurePosixPath(relative).name
                or not _digest(occurrence.get("manifest_sha256"))
                or type(occurrence.get("bytes")) is not int
                or occurrence["bytes"] < 0
                or not isinstance(occurrence.get("backup_folder"), str)
                or not occurrence["backup_folder"]
            ):
                raise ValueError(
                    "A restored source changed its sealed native occurrence."
                )
        shape = "restored_native_occurrences"
    elif set(proof) == {
        "entry",
        "report",
        "report_sha256",
        "sources",
        "sources_sha256",
    }:
        entry = proof["entry"]
        if not isinstance(entry, dict) or (
            not _digest(proof["report_sha256"])
            or not _digest(proof["sources_sha256"])
            or not isinstance(proof["report"], str)
            or not proof["report"]
            or not isinstance(proof["sources"], str)
            or not proof["sources"]
            or not isinstance(metadata.get("source_inventory_file"), str)
            or PurePosixPath(proof["report"]).name
            != PurePosixPath(metadata["source_inventory_file"]).name
            or metadata.get("source_inventory_sha256") is not None
            and metadata["source_inventory_sha256"] != proof["report_sha256"]
            or entry.get("domain") != "AppDomain-com.tencent.xin"
            or entry.get("relative_path") != relative
            or entry.get("input_relative_path") != "inputs/" + relative
            or entry.get("file_id") != file_id
            or any(
                entry.get(key) != digest
                for key in ("sha256", "snapshot_sha256", "source_sha256")
            )
            or type(entry.get("bytes")) is not int
            or entry["bytes"] < 0
        ):
            raise ValueError(
                "A preservation report lost its exact selected database receipt."
            )
        shape = "sealed_preservation_report"
    else:
        raise ValueError("A resource source uses an unrecognized legacy receipt shape.")
    return {
        "key": (owner, receipt["backup_id"], relative, digest),
        "source_snapshot_id": snapshot["id"],
        "receipt_proof": {
            "version": VERSION,
            "basis": "catalog_native_resource_source_receipt",
            "source_snapshot_id": snapshot["id"],
            "source_id": snapshot["source_id"],
            "source_snapshot_metadata_sha256": receipt["metadata_sha256"],
            "catalog_source_receipt_sha256": _sha(receipt),
            "receipt_shape": shape,
            "database_relative_path": relative,
            "database_sha256": digest,
        },
    }


class ResourceSnapshotAdapter:
    """Preserve exact resolutions and use only explicitly linked legacy receipts."""

    def __init__(self, declared_index, snapshot_rows, source_receipts):
        self.declared = {
            key: tuple(dict.fromkeys(value)) for key, value in declared_index.items()
        }
        snapshots = {row["id"]: deepcopy(dict(row)) for row in snapshot_rows}
        self.linked = defaultdict(list)
        self.rejected = Counter()
        self.ignored = Counter()
        self.conflicts = set()
        for value in source_receipts:
            receipt = deepcopy(dict(value))
            key = (
                receipt.get("account_id"),
                receipt.get("backup_id"),
                receipt.get("database_relative_path"),
                receipt.get("database_sha256"),
            )
            if len(self.declared.get(key, ())) == 1:
                self.ignored["unique_primary_declaration_already_resolves"] += 1
                continue
            try:
                result = validate_catalog_source_receipt(
                    snapshots.get(receipt.get("source_snapshot_id")), receipt
                )
            except (ValueError, TypeError, KeyError) as error:
                self.rejected[str(error)] += 1
                continue
            key, sid = result["key"], result["source_snapshot_id"]
            slot = key, sid
            if slot in self.conflicts:
                continue
            prior = [
                item for item in self.linked[key] if item["source_snapshot_id"] == sid
            ]
            if prior and any(item != result for item in prior):
                self.linked[key] = [
                    item
                    for item in self.linked[key]
                    if item["source_snapshot_id"] != sid
                ]
                self.conflicts.add(slot)
                self.rejected[
                    "Conflicting retained receipts name the same snapshot and database."
                ] += 1
            elif prior:
                self.ignored["duplicate_identical_source_receipt"] += 1
            else:
                self.linked[key].append(result)

    def resolve(self, primary_key, *, existing_source_snapshot=None):
        if existing_source_snapshot is not None and (
            type(existing_source_snapshot) is not int or existing_source_snapshot < 1
        ):
            raise ValueError(
                "An existing source snapshot must retain its exact positive integer ID."
            )
        declared = self.declared.get(primary_key, ())
        if len(declared) == 1:
            if (
                existing_source_snapshot is not None
                and existing_source_snapshot != declared[0]
            ):
                raise ValueError(
                    "A completion cannot change its original source snapshot."
                )
            return {"source_snapshot_id": declared[0], "receipt_proof": None}
        linked = self.linked.get(primary_key, ())
        candidates = {}
        for receipt in linked:
            sid = receipt["source_snapshot_id"]
            if declared and sid not in declared:
                continue
            if existing_source_snapshot is not None and existing_source_snapshot != sid:
                continue
            candidates.setdefault(sid, receipt)
        if len(candidates) != 1:
            raise ValueError(
                "A completion needs one exactly linked retained resource snapshot."
            )
        selected = next(iter(candidates.values()))
        return {
            "source_snapshot_id": selected["source_snapshot_id"],
            "receipt_proof": deepcopy(selected["receipt_proof"]),
        }
