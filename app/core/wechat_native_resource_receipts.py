"""Bind newer iPad metadata receipts to preserved native text witnesses.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re


VERSION = "v1.0.0-codex.0"
_HEX = re.compile(r"[0-9a-f]{64}")
WITNESS_SCHEMA = """
CREATE TABLE revalidation_native_witnesses (
 message_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
 source_snapshot_id INTEGER NOT NULL, provenance_sha256 TEXT NOT NULL,
 direction TEXT NOT NULL, database_relative_path TEXT NOT NULL,
 database_sha256 TEXT NOT NULL, manifest_sha256 TEXT NOT NULL
) WITHOUT ROWID;
"""


def _snapshot(history, receipt):
    if (
        not isinstance(receipt, dict)
        or type(receipt.get("source_snapshot_id")) is not int
        or receipt["source_snapshot_id"] < 1
    ):
        raise ValueError("A supplemental receipt needs an exact native snapshot ID.")
    row = history.execute(
        "SELECT * FROM source_snapshots WHERE id=?",
        (receipt.get("source_snapshot_id"),),
    ).fetchone()
    if row is None:
        raise ValueError("A supplemental snapshot is no longer published.")
    metadata = json.loads(row["metadata"])
    if not isinstance(metadata, dict) or not isinstance(
        metadata.get("identity_evidence"), dict
    ):
        raise ValueError("A supplemental snapshot lacks native identity evidence.")
    owner = metadata.get("source_account_id")
    hashed = hashlib.md5(str(owner).encode(), usedforsecurity=False).hexdigest()
    identity = metadata.get("identity_evidence", {})
    if (
        not isinstance(owner, str)
        or not owner
        or row["source_id"] != "ios-" + hashed
        or row["source_id"] != receipt.get("source_id")
        or row["snapshot_id"] != receipt.get("snapshot_id")
        or metadata.get("snapshot_id") != row["snapshot_id"]
        or metadata.get("source_id") != row["source_id"]
        or metadata.get("account_hash") != hashed
        or metadata.get("account_hash_verified") is not True
        or identity.get("kind") != "native_login_info_with_directory_md5"
        or identity.get("directory_hash") != hashed
        or identity.get("directory_hash_algorithm") != "MD5-UTF8"
        or not _HEX.fullmatch(str(identity.get("login_info_sha256", "")))
        or hashlib.sha256(row["metadata"].encode()).hexdigest()
        != receipt.get("metadata_sha256")
    ):
        raise ValueError(
            "A supplemental receipt changed its native account or snapshot."
        )
    return row, metadata


def load_supplement_receipts(history, metadata, *, sealed=False):
    """Validate a fresh manifest once; sealed prior inputs need no cloud access."""
    from .wechat_native_resource_revalidation import _relative, _resident

    info = dict(metadata.execute("SELECT key,value FROM archive_info"))
    values = json.loads(
        info.get("supplement_source_receipts" if sealed else "source_receipts", "[]")
    )
    if not isinstance(values, list) or len(values) > 4096:
        raise ValueError("Supplemental native receipts exceed their metadata budget.")
    for receipt in values:
        snapshot, original = _snapshot(history, receipt)
        relative = _relative(
            receipt.get("database_relative_path"), original["source_account_id"]
        )
        if (
            not _HEX.fullmatch(str(receipt.get("database_sha256", "")))
            or not _HEX.fullmatch(str(receipt.get("manifest_sha256", "")))
            or receipt.get("file_id")
            != hashlib.sha1(
                ("AppDomain-com.tencent.xin-" + relative).encode(),
                usedforsecurity=False,
            ).hexdigest()
            or original.get("source_inventory_file")
            != receipt.get("original_source_inventory_file")
            or type(receipt.get("database_bytes")) is not int
            or receipt["database_bytes"] < 1
        ):
            raise ValueError(
                "A supplemental database receipt lost its preserved locator."
            )
        if sealed:
            continue
        path = Path(receipt["manifest_path"])
        before = _resident(path)
        if before["size"] > 8 * 1024**2:
            raise ValueError("A supplemental preservation manifest exceeds its budget.")
        raw = path.read_bytes()
        if (
            _resident(path) != before
            or hashlib.sha256(raw).hexdigest() != receipt["manifest_sha256"]
        ):
            raise ValueError("A supplemental preservation manifest changed.")
        manifest = json.loads(raw)
        if (
            not isinstance(manifest, dict)
            or manifest.get("snapshot_id") != snapshot["snapshot_id"]
            or manifest.get("status") != "complete"
            or manifest.get("all_snapshot_hashes_equal_source_hashes") is not True
        ):
            raise ValueError(
                "A supplemental manifest does not describe a complete preservation."
            )
        selected = manifest.get("selected_files")
        if not isinstance(selected, list) or any(
            not isinstance(item, dict) for item in selected
        ):
            raise ValueError("A supplemental manifest needs object file receipts.")
        matches = [
            item
            for item in selected
            if item.get("domain") == "AppDomain-com.tencent.xin"
            and item.get("relative_path") == relative
        ]
        if len(matches) != 1 or any(
            matches[0].get(key) != expected
            for key, expected in (
                ("sha256", receipt["database_sha256"]),
                ("snapshot_sha256", receipt["database_sha256"]),
                ("source_sha256", receipt["database_sha256"]),
                ("file_id", receipt["file_id"]),
                ("bytes", receipt["database_bytes"]),
                ("input_relative_path", "inputs/" + relative),
            )
        ):
            raise ValueError(
                "The supplemental database differs from its preservation manifest."
            )
    return values


def _witness(history, conversation, receipt, row):
    snapshot, metadata = _snapshot(history, receipt)
    proof = json.loads(row["provenance"])
    if not isinstance(proof, dict) or not isinstance(proof.get("locator"), dict):
        raise ValueError("A supplemental native witness lacks its exact locator.")
    locator = proof.get("locator", {})
    expected_table = (
        "Chat_"
        + hashlib.md5(
            conversation["peer_id"].encode(), usedforsecurity=False
        ).hexdigest()
    )
    if (
        snapshot["source_id"] != conversation["partition_id"]
        or conversation["account_key"] != "account:" + metadata["source_account_id"]
        or proof.get("source_snapshot") != snapshot["id"]
        or locator.get("database_relative_path") != receipt["database_relative_path"]
        or locator.get("database_sha256") != receipt["database_sha256"]
        or locator.get("file_id") != receipt["file_id"]
        or locator.get("table") != expected_table
        or type(locator.get("local_id")) is not int
        or locator["local_id"] < 1
        or locator.get("row_id") != locator["local_id"]
        or type(proof.get("unix_timestamp")) is not int
        or type(proof.get("message_type")) is not int
        or row["direction"] not in {"sent", "received", "unknown"}
        or {0: "sent", 1: "received"}.get(locator.get("native_direction"), "unknown")
        != row["direction"]
    ):
        raise ValueError("A supplemental database lacks an exact native text witness.")
    return (
        row["id"],
        conversation["id"],
        snapshot["id"],
        hashlib.sha256(row["provenance"].encode()).hexdigest(),
        row["direction"],
        receipt["database_relative_path"],
        receipt["database_sha256"],
        receipt["manifest_sha256"],
    )


def extend_scoped_receipts(history, conversation, base, supplemental):
    """Corroborate a newer receipt using a current, exact-account native row."""
    owner, receipts, snapshots = base
    witnesses = []
    for receipt in supplemental:
        if receipt["source_id"] != conversation["partition_id"]:
            continue
        row = history.execute(
            "SELECT id,provenance,direction FROM messages WHERE conversation_id=? "
            "AND json_extract(provenance,'$.source_snapshot')=? "
            "AND json_extract(provenance,'$.locator.database_relative_path')=? "
            "AND json_extract(provenance,'$.locator.database_sha256')=? LIMIT 1",
            (
                conversation["id"],
                receipt["source_snapshot_id"],
                receipt["database_relative_path"],
                receipt["database_sha256"],
            ),
        ).fetchone()
        if row is None:
            raise ValueError(
                "A newer native metadata database has no current text witness."
            )
        witnesses.append(_witness(history, conversation, receipt, row))
        key = receipt["snapshot_id"], receipt["database_relative_path"]
        if key in receipts and receipts[key] != receipt["database_sha256"]:
            raise ValueError(
                "A supplemental database conflicts with a retained receipt."
            )
        receipts[key] = receipt["database_sha256"]
        snapshots[receipt["source_snapshot_id"]] = (
            receipt["snapshot_id"],
            {receipt["database_relative_path"]: receipt["database_sha256"]},
            {
                key: receipt[key]
                for key in (
                    "source_snapshot_id",
                    "source_id",
                    "snapshot_id",
                    "metadata_sha256",
                )
            },
        )
    return (owner, receipts, snapshots), witnesses


def verify_native_witnesses(history, source):
    """Read back sealed witnesses from the primary; never open a manifest here."""
    if (
        source.execute(
            "SELECT 1 FROM sqlite_master WHERE name='revalidation_native_witnesses'"
        ).fetchone()
        is None
    ):
        return
    receipts = load_supplement_receipts(history, source, sealed=True)
    rows = source.execute(
        "SELECT * FROM revalidation_native_witnesses LIMIT 4097"
    ).fetchall()
    if not 1 <= len(rows) <= 4096:
        raise ValueError("Native metadata witnesses exceed their sealed budget.")
    for sealed in rows:
        matching = [
            receipt
            for receipt in receipts
            if receipt["source_snapshot_id"] == sealed["source_snapshot_id"]
            and receipt["database_relative_path"] == sealed["database_relative_path"]
            and receipt["database_sha256"] == sealed["database_sha256"]
        ]
        conversation = history.execute(
            "SELECT * FROM conversations WHERE id=?", (sealed["conversation_id"],)
        ).fetchone()
        row = history.execute(
            "SELECT id,provenance,direction FROM messages WHERE id=? AND conversation_id=?",
            (sealed["message_id"], sealed["conversation_id"]),
        ).fetchone()
        if (
            len(matching) != 1
            or conversation is None
            or row is None
            or _witness(history, conversation, matching[0], row) != tuple(sealed)
        ):
            raise ValueError("A sealed native metadata witness changed.")
