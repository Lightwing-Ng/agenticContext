"""Derive bounded native database receipts from independently retained primary rows.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
import re

from .wechat_native_resource_revalidation import _anchor, _owner, _relative


VERSION = "v1.0.0-codex.0"
BASIS = "primary_source_database_receipt"
MAX_ROWS = 100_000
MAX_BYTES = 128 * 1024**2
MAX_CELL_BYTES = 16 * 1024**2
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SCOPE = ("id", "partition_id", "account_key", "peer_id", "kind")


def _row(connection, query, parameters):
    cursor = connection.execute(query, parameters)
    value = cursor.fetchone()
    if value is None:
        return None
    return dict(zip((column[0] for column in cursor.description), value, strict=True))


def _object(raw):
    if not isinstance(raw, str) or len(raw.encode()) > MAX_CELL_BYTES:
        raise ValueError("A primary source receipt exceeds its metadata cell budget.")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Primary source receipts require object metadata.")
    return value


def primary_source_database_receipts(history, conversation, source_snapshot_id):
    """Reprove one native scope from primary observations, never from sidecars.

    This verifies retained source receipts. A caller must independently compare
    any newly materialized database with the returned digest before importing
    additional rows. No source database or media payload is opened here.
    """
    if type(source_snapshot_id) is not int or source_snapshot_id < 1:
        raise ValueError("Select one exact positive source snapshot ID.")
    current = _row(
        history,
        "SELECT * FROM main.conversations WHERE id=?",
        (conversation["id"],),
    )
    if current is None or any(current[key] != conversation[key] for key in _SCOPE):
        raise ValueError("The primary receipt conversation changed its source scope.")
    snapshot = _row(
        history,
        "SELECT * FROM main.source_snapshots WHERE id=?",
        (source_snapshot_id,),
    )
    partition = (
        _row(
            history,
            "SELECT * FROM main.partitions WHERE id=?",
            (snapshot["source_id"],),
        )
        if snapshot is not None
        else None
    )
    if partition is None or snapshot is None or not current["peer_id"]:
        raise ValueError("A primary database receipt needs its native source account.")
    owner = _owner(partition)
    metadata = _object(snapshot["metadata"])
    backup = snapshot["snapshot_id"]
    identity = metadata.get("identity_evidence", {})
    if (
        not isinstance(backup, str)
        or not backup
        or len(backup) > 1024
        or snapshot["source_id"] != partition["id"]
        or current["account_key"] != partition["account_key"]
        or metadata.get("source_id") != partition["id"]
        or metadata.get("source_account_id") != owner
        or metadata.get("account_hash_verified") is not True
        or metadata.get("account_hash") != partition["id"][4:]
        or metadata.get("snapshot_id", metadata.get("backup_id")) != backup
        or not isinstance(identity, dict)
        or "directory_hash" in identity
        and identity["directory_hash"] != partition["id"][4:]
    ):
        raise ValueError(
            "A primary database receipt crosses its verified snapshot account."
        )
    expected_table = (
        "Chat_"
        + hashlib.md5(
            current["peer_id"].encode(),
            usedforsecurity=False,
        ).hexdigest()
    )
    sources, file_ids, anchors = {}, {}, {}
    scanned = matched = total_bytes = 0
    query = (
        "SELECT id,direction,CASE WHEN length(CAST(provenance AS BLOB))<=? "
        "THEN provenance END AS provenance FROM main.messages "
        "WHERE conversation_id=? LIMIT ?"
    )
    for row in history.execute(query, (MAX_CELL_BYTES, current["id"], MAX_ROWS + 1)):
        # This projection also works with a caller's default tuple row factory.
        message_id, direction, raw = row
        scanned += 1
        proof = _object(raw)
        total_bytes += len(raw.encode())
        if scanned > MAX_ROWS or total_bytes > MAX_BYTES:
            raise ValueError(
                "The selected primary source scope exceeds its receipt budget."
            )
        if proof.get("source_snapshot") != source_snapshot_id:
            continue
        if proof.get("text_extraction_kind") == "resource_placeholder":
            raise ValueError(
                "A supplemental placeholder cannot prove its own source receipt."
            )
        locator = proof.get("locator")
        if not isinstance(locator, dict):
            raise ValueError("A primary native receipt lost its database locator.")
        path = _relative(locator.get("database_relative_path"), owner)
        digest = locator.get("database_sha256")
        expected_file_id = hashlib.sha1(
            ("AppDomain-com.tencent.xin-" + path).encode(),
            usedforsecurity=False,
        ).hexdigest()
        if (
            not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or locator.get("database") != PurePosixPath(path).name
            or locator.get("file_id") != expected_file_id
            or locator.get("table") != expected_table
            or direction not in {"sent", "received", "unknown"}
        ):
            raise ValueError(
                "A primary native database receipt changed its source frame."
            )
        local, signature = _anchor(proof, direction, locator)
        if path in sources and sources[path] != digest:
            raise ValueError(
                "One primary native database has conflicting checksum receipts."
            )
        key = (path, local)
        previous = anchors.get(key)
        if previous is not None and previous != signature:
            raise ValueError("One primary native row has conflicting event evidence.")
        anchors[key] = signature
        sources[path] = digest
        file_ids[path] = expected_file_id
        matched += 1
        if not isinstance(message_id, str) or not message_id:
            raise ValueError(
                "A primary native receipt has no retained message identifier."
            )
    if not matched:
        raise ValueError(
            "No independent primary native source receipts matched this snapshot."
        )
    receipt = {
        "source_snapshot_id": source_snapshot_id,
        "source_id": partition["id"],
        "snapshot_id": backup,
        "metadata_sha256": hashlib.sha256(snapshot["metadata"].encode()).hexdigest(),
    }
    return {
        "version": VERSION,
        "basis": BASIS,
        "owner": owner,
        "backup_id": backup,
        "native_partition_id": partition["id"],
        "receipts": {(backup, path): digest for path, digest in sources.items()},
        "snapshots": {source_snapshot_id: (backup, sources, receipt)},
        "file_ids": file_ids,
        "anchors_checked": matched,
        "primary_rows_scanned": scanned,
        "metadata_bytes": total_bytes,
    }
