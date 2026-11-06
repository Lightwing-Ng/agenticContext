"""Project compatible retained native structures without rewriting source records.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

from collections.abc import Iterable
import hashlib
from itertools import islice
import json
from pathlib import Path
import re
import sqlite3

from .wechat_archive import _identifier, wechat_archive_path
from .wechat_forwarded import _native_owner, _read_only
from .wechat_reconcile import (
    _comparable_time,
    _database_identity,
    _json,
    _server_id,
    native_text_metadata,
    native_text_metadata_extends,
)


_ID = re.compile(r"[0-9a-f]{64}")
_MAX_MESSAGES = 1000
_MAX_EVIDENCE = 32
_MAX_BYTES = 2 * 1024 * 1024
_PAGE_BYTES = 8 * 1024 * 1024


def _object(value):
    if isinstance(value, str):
        if len(value.encode()) > _MAX_BYTES:
            raise ValueError("Retained message evidence exceeds the supported size.")
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("Retained message evidence must be an object.")
    return value


def _flags(value):
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list) or any(not isinstance(flag, str) for flag in value):
        raise ValueError("Retained message quality flags must be strings.")
    return value


def _coherent_body(metadata, text):
    components = metadata.get("text_components")
    if not isinstance(components, list) or not components:
        return False
    from .wechat_text import native_components_text

    return native_components_text(components) == text


class WechatContextEvidence:
    """Read bounded current-page evidence by its existing message index.

    Native observations can supplement compatible source structure. Export
    cross-references do not prove parsed HTML roles and are never promoted here.
    Original provenance, source identity, contact requests, and exports stay intact.
    """

    def __init__(self, local_store_root: Path | str):
        self.path = wechat_archive_path(local_store_root)
        self.statuses = {}

    @staticmethod
    def _candidate(row, primary, provenance, source, snapshots):
        if (
            row["source_id"] != primary["partition_id"]
            or row["message_id"] != primary["id"]
        ):
            return None
        observation = _object(row["observation"])
        if observation.get("kind") == "export_cross_reference":
            return None
        record_id = observation.get("record_id")
        if (
            not isinstance(record_id, str)
            or not record_id
            or record_id != row["extraction_record_id"]
        ):
            return None
        if row["id"] != _identifier(row["source_id"], record_id, _json(observation)):
            return None
        if (
            observation.get("content_sha256")
            != hashlib.sha256(primary["text"].encode()).hexdigest()
        ):
            return None
        if (
            type(observation.get("message_type")) is not int
            or observation["message_type"] != 49
        ):
            return None
        kind = observation.get("text_extraction_kind")
        if kind not in {"quoted_reply", "forwarded_text"}:
            return None
        snapshot_id = observation.get("source_snapshot")
        if type(snapshot_id) is not int or not 0 < snapshot_id <= 2**63 - 1:
            return None
        snapshot = snapshots.get(snapshot_id)
        if (
            snapshot is None
            or snapshot["source_id"] != row["source_id"]
            or snapshot["snapshot_id"] != row["snapshot_id"]
        ):
            return None
        receipt = _object(snapshot["metadata"])
        if (
            receipt.get("account_hash_verified") is not True
            or receipt.get("source_account_id") != primary["account_id"]
            or receipt.get("source_id") != row["source_id"]
            or receipt.get("account_hash")
            != hashlib.md5(primary["account_id"].encode()).hexdigest()
        ):
            return None
        locator = observation.get("locator")
        original_locator = provenance.get("locator", {})
        if not isinstance(locator, dict) or not isinstance(original_locator, dict):
            return None
        if (
            primary["peer_id"]
            and locator.get("table")
            != "Chat_" + hashlib.md5(primary["peer_id"].encode()).hexdigest()
        ):
            return None
        if (
            original_locator.get("table")
            and locator.get("table") != original_locator["table"]
        ):
            return None
        original_server = _server_id(provenance.get("server_id"))
        observed_server = _server_id(observation.get("server_id"))
        if original_server and observed_server and original_server != observed_server:
            return None
        same_record = provenance.get("record_id") == record_id
        same_server = bool(original_server and original_server == observed_server)
        if not same_record and not same_server:
            return None
        if same_record and any(
            original_locator[key] != locator[key]
            for key in ("database", "table", "local_id", "row_id", "row")
            if key in original_locator and key in locator
        ):
            return None
        observed_sender = observation.get("sender_id")
        if not isinstance(observed_sender, str):
            return None
        if (
            primary["sender_id"]
            and observed_sender
            and primary["sender_id"] != observed_sender
        ):
            return None
        observed_direction = observation.get("direction")
        if observed_direction not in {"sent", "received", "unknown"} or (
            primary["direction"] != "unknown"
            and observed_direction != "unknown"
            and primary["direction"] != observed_direction
        ):
            return None
        original_time = _comparable_time(
            provenance,
            primary["timestamp_value"],
            primary["time_basis"],
            _flags(primary["quality_flags"]),
        )
        observed_time = _comparable_time(
            observation,
            observation.get("timestamp_value"),
            observation.get("time_basis"),
            _flags(observation.get("flags", [])),
        )
        if (
            original_time is not None
            and observed_time is not None
            and original_time != observed_time
        ):
            return None
        metadata = native_text_metadata(observation, locator)
        if not _coherent_body(metadata, primary["text"]):
            return None
        primary_kind = provenance.get("text_extraction_kind")
        if primary_kind not in {None, "app_text", "text_appmsg", kind}:
            return None
        if not native_text_metadata_extends(source, metadata):
            return None
        if "forwarded_references" in source or "forwarded_references" in metadata:
            return None
        combined = {**source, **metadata}
        native_text_metadata(combined, {})
        return {"text_extraction_kind": kind, **combined}

    def project(self, messages: Iterable[dict]) -> dict:
        """Return presenter-only provenance copies for compatible current-page rows."""
        self.statuses = {}
        requested = list(islice(messages, _MAX_MESSAGES + 1))
        if len(requested) > _MAX_MESSAGES or not requested:
            return {}
        by_id = {}
        seen = set()
        for row in requested:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("id"), str)
                or _ID.fullmatch(row["id"]) is None
                or row["id"] in seen
            ):
                return {}
            seen.add(row["id"])
            try:
                supplied = _object(row.get("provenance"))
            except (ValueError, TypeError, RecursionError):
                continue
            if (
                type(supplied.get("message_type")) is not int
                or supplied["message_type"] != 49
                or supplied.get("text_extraction_kind")
                not in {
                    None,
                    "app_text",
                    "text_appmsg",
                    "quoted_reply",
                    "forwarded_text",
                }
            ):
                continue
            by_id[row["id"]] = row
        if not by_id:
            return {}
        connection = None
        try:
            if not self.path.is_file() or self.path.is_symlink():
                return {}
            initial = _database_identity(self.path)
            if any(initial[index] and initial[index][2] for index in (1, 2)):
                return {}
            connection = _read_only(self.path)
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('message_evidence','source_snapshots')"
                )
            }
            if tables != {"message_evidence", "source_snapshots"}:
                return {}
            if (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' AND name='evidence_message'"
                ).fetchone()
                is None
            ):
                return {}
            result, page_bytes, snapshots = {}, 0, {}
            ids = list(by_id)
            for start in range(0, len(ids), 400):
                batch = ids[start : start + 400]
                placeholders = ",".join("?" for _ in batch)
                groups = {}
                values = ",".join("(?)" for _ in batch)
                cursor = connection.execute(
                    f"WITH requested(message_id) AS (VALUES {values}) "
                    "SELECT e.id,e.message_id,e.source_id,e.snapshot_id,e.extraction_record_id,"
                    "CASE WHEN length(CAST(e.observation AS BLOB))<=? THEN e.observation ELSE NULL END AS observation,"
                    "e.match_kind,length(CAST(e.observation AS BLOB)) AS observation_bytes "
                    "FROM requested r CROSS JOIN message_evidence e "
                    "WHERE e.id IN (SELECT bounded.id FROM message_evidence bounded INDEXED BY evidence_message "
                    "WHERE bounded.message_id=r.message_id LIMIT ?) AND e.message_id=r.message_id",
                    [*batch, _MAX_BYTES, _MAX_EVIDENCE + 1],
                )
                for evidence in cursor:
                    page_bytes += evidence["observation_bytes"]
                    if page_bytes > _PAGE_BYTES:
                        return {}
                    groups.setdefault(evidence["message_id"], []).append(evidence)
                primaries = connection.execute(
                    "SELECT m.*,c.partition_id,c.account_key,c.peer_id,c.kind,p.account_id,"
                    "p.account_key AS partition_account_key,p.identity_status "
                    "FROM messages m JOIN conversations c ON c.id=m.conversation_id "
                    f"JOIN partitions p ON p.id=c.partition_id WHERE m.id IN ({placeholders})",
                    batch,
                ).fetchall()
                for current in primaries:
                    primary = dict(current)
                    identifier = primary["id"]
                    self.statuses[identifier] = "no_compatible_structure"
                    if not _native_owner(primary):
                        continue
                    supplied = by_id[identifier]
                    try:
                        provenance = _object(primary["provenance"])
                        if supplied.get("text") != primary["text"] or _json(
                            _object(supplied.get("provenance"))
                        ) != _json(provenance):
                            self.statuses[identifier] = "primary_changed"
                            continue
                        if (
                            type(provenance.get("message_type")) is not int
                            or provenance["message_type"] != 49
                            or not isinstance(provenance.get("locator", {}), dict)
                        ):
                            continue
                        source = native_text_metadata(
                            provenance, provenance.get("locator", {})
                        )
                        candidates, evidence_ids = {}, []
                        rows = groups.get(identifier, [])
                        if len(rows) > _MAX_EVIDENCE:
                            self.statuses[identifier] = "evidence_limit"
                            continue
                        for evidence in rows:
                            try:
                                observation = _object(evidence["observation"])
                                snapshot_id = observation.get("source_snapshot")
                                if (
                                    type(snapshot_id) is int
                                    and 0 < snapshot_id <= 2**63 - 1
                                    and snapshot_id not in snapshots
                                ):
                                    receipt = connection.execute(
                                        "SELECT * FROM source_snapshots WHERE id=?",
                                        (snapshot_id,),
                                    ).fetchone()
                                    snapshots[snapshot_id] = (
                                        dict(receipt) if receipt is not None else None
                                    )
                                candidate = self._candidate(
                                    evidence, primary, provenance, source, snapshots
                                )
                                if candidate is not None:
                                    candidates.setdefault(_json(candidate), candidate)
                                    evidence_ids.append(evidence["id"])
                            except (ValueError, TypeError, KeyError, RecursionError):
                                continue
                        if len(candidates) > 1:
                            self.statuses[identifier] = "conflicting_structures"
                            continue
                        if not candidates:
                            continue
                        candidate = next(iter(candidates.values()))
                        effective = {**provenance, **candidate}
                        effective["locator"] = {
                            key: value
                            for key, value in provenance.get("locator", {}).items()
                            if key not in candidate
                        }
                        native_text_metadata(effective, effective["locator"])
                        if _json(effective) == _json(provenance):
                            self.statuses[identifier] = "already_present"
                            continue
                        result[identifier] = {
                            "provenance": effective,
                            "evidence_ids": tuple(sorted(evidence_ids)),
                            "authority": "retained_native_text_observation",
                        }
                        self.statuses[identifier] = "compatible_structure"
                    except (ValueError, TypeError, KeyError, RecursionError):
                        continue
            return result if _database_identity(self.path) == initial else {}
        except (
            OSError,
            sqlite3.Error,
            ValueError,
            TypeError,
            KeyError,
            RecursionError,
        ):
            return {}
        finally:
            if connection is not None:
                connection.close()
