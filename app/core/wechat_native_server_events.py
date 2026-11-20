"""Project verified cross-snapshot native events without changing source clocks.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from pathlib import PurePosixPath
import re

from .wechat_contacts import canonical_contact_id


VERSION = "v1.0.0-codex.0"


def _sender_id(row):
    try:
        return canonical_contact_id(row["sender_id"], authority="native_message_sender")
    except (TypeError, ValueError):
        return ""


def _validation_provenance(provenance, metadata):
    """Recover only a legacy locator's uniquely receipted database coordinate."""
    locator = provenance.get("locator", {})
    if not isinstance(locator, dict) or locator.get("database_relative_path"):
        return provenance
    if locator.get("text_origin") != "native_wcdb_message_cell":
        return provenance
    database = locator.get("database")
    if not isinstance(database, str) or PurePosixPath(database).name != database:
        return provenance
    receipts = metadata.get("input_databases")
    if not isinstance(receipts, list):
        return provenance
    matches = [
        receipt
        for receipt in receipts
        if isinstance(receipt, dict)
        and isinstance(receipt.get("path"), str)
        and PurePosixPath(receipt["path"]).name == database
    ]
    if len(matches) != 1:
        return provenance
    receipt = matches[0]
    relative = PurePosixPath(receipt["path"])
    if (
        relative.parts != ("Documents", metadata.get("account_hash"), "DB", database)
        or re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("sha256", ""))) is None
        or re.fullmatch(r"[0-9a-f]{40}", str(receipt.get("file_id", ""))) is None
        or type(receipt.get("bytes")) is not int
        or receipt["bytes"] <= 0
    ):
        return provenance
    return {
        **provenance,
        "locator": {**locator, "database_relative_path": receipt["path"]},
    }


def _content_signature(row, provenance, wall_clock):
    """Unknown, structured, and conflicting observations fence their server group."""
    if (
        row["direction"] not in {"sent", "received"}
        or not isinstance(row["text"], str)
        or not row["text"].strip()
        or wall_clock(row, provenance, native=True) is None
        or provenance.get("forwarded_components")
        or provenance.get("resource_reference")
    ):
        return None
    if row["kind"] == "direct" and row["sender_id"] != (
        row["account_id"] if row["direction"] == "sent" else row["peer_id"]
    ):
        return None
    sender = _sender_id(row)
    if not sender:
        return None
    message_type = provenance.get("message_type")
    if type(message_type) is not int:
        return None
    extraction = provenance.get("text_extraction_kind")
    if (
        message_type == 1
        and extraction == "plain_text"
        and not any(
            provenance.get(field)
            for field in ("text_components", "quoted_components", "quoted_reference")
        )
    ):
        semantics = "plain_text"
    elif message_type == 49 and extraction == "quoted_reply":
        from .wechat_conversation_quotes import quoted_event

        quote = quoted_event(row, provenance, native=True)
        if quote is None or quote.get("unavailable"):
            return None
        semantics = json.dumps(
            {
                "event": {
                    key: value for key, value in quote.items() if key != "speaker"
                },
                "text_components": provenance.get("text_components"),
                "quoted_reference": provenance.get("quoted_reference"),
                "quoted_components": provenance.get("quoted_components"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    else:
        return None
    return (
        sender,
        row["direction"],
        message_type,
        hashlib.sha256(row["text"].encode()).hexdigest(),
        semantics,
    )


def project_native_server_events(
    connection,
    session,
    current,
    message_table,
    *,
    wall_clock,
    max_rows,
    max_source_bytes,
):
    """Collapse only unambiguous verified native server events across snapshots."""
    from .wechat_contact_analysis import _routed_native_source

    owner, peer = current.get("account_id"), current.get("peer_id")
    if (
        not isinstance(owner, str)
        or not owner
        or not isinstance(peer, str)
        or not peer
        or any(character.isspace() or character == "\0" for character in peer)
        or current.get("account_key") != "account:" + owner
        or current.get("kind") not in {"direct", "group"}
    ):
        return {}, {}
    partition = connection.execute(
        "SELECT * FROM main.partitions WHERE id=?", (current["partition_id"],)
    ).fetchone()
    if (
        partition is None
        or partition["id"] != "ios-" + hashlib.md5(owner.encode()).hexdigest()
        or partition["account_id"] != owner
        or partition["account_key"] != current["account_key"]
        or partition["identity_status"] != "Verified archive ID"
    ):
        return {}, {}
    snapshots, groups = {}, defaultdict(list)
    count, size = 0, 0
    cursor = connection.execute(
        "SELECT m.* FROM "
        + message_table
        + " m WHERE m.conversation_id=? ORDER BY m.ordinal,m.id",
        (session,),
    )
    while batch := cursor.fetchmany(200):
        for source_row in batch:
            row = dict(source_row)
            count += 1
            size += len(row["text"].encode()) + len(row["provenance"].encode())
            if count > max_rows or size > max_source_bytes:
                raise ValueError("Native event proof exceeds its bounded budget.")
            try:
                provenance = json.loads(row["provenance"])
                snapshot_key = provenance.get("source_snapshot")
                if type(snapshot_key) is not int:
                    continue
                if snapshot_key not in snapshots:
                    found = connection.execute(
                        "SELECT * FROM main.source_snapshots WHERE id=?",
                        (snapshot_key,),
                    ).fetchone()
                    snapshots[snapshot_key] = (
                        (dict(found), json.loads(found["metadata"]))
                        if found
                        else (None, None)
                    )
                snapshot, metadata = snapshots[snapshot_key]
                if (
                    snapshot is None
                    or snapshot["source_id"] != partition["id"]
                    or not isinstance(snapshot["snapshot_id"], str)
                    or not snapshot["snapshot_id"]
                    or not isinstance(metadata, dict)
                ):
                    continue
                row.update(
                    peer_id=peer,
                    account_id=owner,
                    account_key=current["account_key"],
                    container_account_key=current["account_key"],
                    identity_status=partition["identity_status"],
                    source_id=partition["id"],
                    kind=current["kind"],
                )
                _routed_native_source(
                    row,
                    _validation_provenance(provenance, metadata),
                    snapshot,
                    partition,
                )
                server = provenance.get("server_id")
                if (
                    type(server) not in {str, int}
                    or re.fullmatch(r"-?[1-9][0-9]*", str(server)) is None
                ):
                    continue
                groups[str(server)].append(
                    {
                        "id": row["id"],
                        "source": (snapshot["source_id"], snapshot["snapshot_id"]),
                        "signature": _content_signature(row, provenance, wall_clock),
                        "rank": 0
                        if metadata.get("source_label") == "iPad archive"
                        else 1,
                        "context": {
                            "id": row["id"],
                            "source_id": snapshot["source_id"],
                            "source_snapshot_id": snapshot["snapshot_id"],
                            "source_label": metadata.get("source_label")
                            or partition["source_label"],
                            "source_clock": provenance.get("source_time")
                            or row["timestamp_value"],
                            "timestamp_value": row["timestamp_value"],
                            "contact_id": _sender_id(row),
                            "record_id": provenance.get("record_id", ""),
                            "sender_id": row["sender_id"] or "",
                            "sender_label": row["sender_label"],
                        },
                    }
                )
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
    mapping, members = {}, {}
    for observations in groups.values():
        signatures = {item["signature"] for item in observations}
        if (
            len(observations) < 2
            or None in signatures
            or len(signatures) != 1
            or len({item["source"] for item in observations}) != len(observations)
        ):
            continue
        ordered = sorted(observations, key=lambda item: (item["rank"], item["id"]))
        preferred = ordered[0]["id"]
        conflict = len({item["context"]["timestamp_value"] for item in ordered}) > 1
        members[preferred] = [
            {**item["context"], "clock_disagreement": conflict} for item in ordered
        ]
        mapping.update({item["id"]: preferred for item in ordered[1:]})
    return mapping, members
