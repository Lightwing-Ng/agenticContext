"""Append source-separated WeChat exports without traversing existing messages.

Code version: v1.1.1-codex.0
"""

from __future__ import annotations

from collections.abc import Iterable
import ctypes
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from typing import Any

from .wechat_archive import _SCHEMA, _identifier, _source_exclusions, _time_fields
from .wechat_archive import wechat_archive_path
from .wechat_html_export import normalize_html_clock
from .wechat_reconcile import _database_identity, _file_identity


_EXPORT_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_snapshots (
 id INTEGER PRIMARY KEY,
 snapshot_id TEXT NOT NULL, source_id TEXT NOT NULL, metadata TEXT NOT NULL,
 UNIQUE(snapshot_id, source_id, metadata)
);
CREATE TABLE IF NOT EXISTS message_evidence (
 id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
 source_id TEXT NOT NULL, snapshot_id TEXT NOT NULL,
 extraction_record_id TEXT NOT NULL, observation TEXT NOT NULL,
 match_kind TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS export_imports (
 source_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL,
 source_sha256 TEXT NOT NULL, metadata TEXT NOT NULL,
 record_count INTEGER NOT NULL, conversation_count INTEGER NOT NULL,
 evidence_count INTEGER NOT NULL, content_fingerprint TEXT NOT NULL
);
"""
_MAX_PARTITIONS = 10_000
_MODULUS = 1 << 256
_PROTECTED_TABLES = frozenset({"messages", "partitions", "conversations"})


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _text(value: Any, name: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or "\0" in value or not empty and not value:
        raise ValueError(f"An exact {name} string is required.")
    return value


def _clonefile(source: Path, destination: Path) -> None:
    """Require a same-volume copy-on-write clone; never fall back to copying bytes."""
    if sys.platform != "darwin":
        raise ValueError("APFS clonefile is required to stage an existing archive.")
    try:
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        clone = library.clonefile
    except (OSError, AttributeError) as error:
        raise ValueError(
            "APFS clonefile is unavailable; no full-copy fallback."
        ) from error
    clone.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int)
    clone.restype = ctypes.c_int
    if clone(os.fsencode(source), os.fsencode(destination), 0) != 0:
        error_number = ctypes.get_errno()
        raise ValueError(
            "APFS clonefile failed; no full-copy fallback: " + os.strerror(error_number)
        )


def _source_metadata(source: dict) -> dict:
    if not isinstance(source, dict):
        raise ValueError("Export source metadata must be an object.")
    metadata = dict(source)
    source_id = metadata.get("source_id")
    if not isinstance(source_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,100}", source_id
    ):
        raise ValueError("An export needs a stable source identifier.")
    if source_id.startswith("ios-"):
        raise ValueError("An export cannot claim an iOS archive source identifier.")
    digest = metadata.get("source_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("An export needs its original source SHA-256 evidence.")
    _text(metadata.get("source_file"), "source filename")
    metadata["source_label"] = _text(
        metadata.get("source_label", "HTML export"), "source label"
    )
    verified = metadata.get("owner_verified") is True
    if verified:
        owner = _text(metadata.get("source_account_id"), "verified source owner")
        if (
            owner != owner.strip()
            or not isinstance(metadata.get("owner_evidence"), dict)
            or not metadata["owner_evidence"]
        ):
            raise ValueError("A verified export owner needs independent evidence.")
    metadata["owner_verified"] = verified
    if len(_json(metadata).encode()) > 8 * 1024 * 1024:
        raise ValueError("Export source metadata exceeds the supported size.")
    return metadata


def _manifest(connection: sqlite3.Connection, *, existing: bool) -> dict:
    row = connection.execute(
        "SELECT value FROM archive_info WHERE key='manifest'"
    ).fetchone()
    if row is None:
        if existing:
            raise ValueError("An existing archive needs its manifest message counter.")
        return {"schema_version": 1, "sources": [], "messages": 0}
    result = json.loads(row[0])
    if (
        not isinstance(result, dict)
        or type(result.get("messages")) is not int
        or result["messages"] < 0
        or not isinstance(result.get("sources"), list)
    ):
        raise ValueError(
            "The archive manifest needs exact source and message counters."
        )
    return result


def _partition_rows(connection: sqlite3.Connection) -> list[tuple]:
    rows = connection.execute(
        "SELECT * FROM partitions ORDER BY id LIMIT ?", (_MAX_PARTITIONS + 1,)
    ).fetchall()
    if len(rows) > _MAX_PARTITIONS:
        raise ValueError(
            "Too many source partitions for bounded metadata verification."
        )
    return [tuple(row) for row in rows]


def _install_guard(connection: sqlite3.Connection) -> dict:
    from .wechat_shared_storage import shared_storage_installed

    shared_storage = shared_storage_installed(connection)
    scope = {"new_message_read": False, "shared_storage": shared_storage}
    protected_tables = set(_PROTECTED_TABLES)
    if shared_storage:
        protected_tables.update({"message_storage", "shared_message_bodies"})
        connection.execute(
            "CREATE TEMP TRIGGER export_no_replace_messages BEFORE INSERT ON main.message_storage "
            "WHEN EXISTS(SELECT 1 FROM main.message_storage WHERE id=NEW.id) "
            "BEGIN SELECT RAISE(ABORT,'Export cannot replace retained rows'); END"
        )

    def authorize(action, table, column, database, trigger):
        if shared_storage and action in {
            sqlite3.SQLITE_ALTER_TABLE,
            sqlite3.SQLITE_CREATE_INDEX,
            sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_CREATE_TRIGGER,
            sqlite3.SQLITE_CREATE_VIEW,
            sqlite3.SQLITE_CREATE_TEMP_INDEX,
            sqlite3.SQLITE_CREATE_TEMP_TABLE,
            sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
            sqlite3.SQLITE_CREATE_TEMP_VIEW,
            sqlite3.SQLITE_DROP_INDEX,
            sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_DROP_TRIGGER,
            sqlite3.SQLITE_DROP_VIEW,
            sqlite3.SQLITE_DROP_TEMP_INDEX,
            sqlite3.SQLITE_DROP_TEMP_TABLE,
            sqlite3.SQLITE_DROP_TEMP_TRIGGER,
            sqlite3.SQLITE_DROP_TEMP_VIEW,
        }:
            return sqlite3.SQLITE_DENY
        if action in {sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
            if table in protected_tables:
                return sqlite3.SQLITE_DENY
        if shared_storage and action == sqlite3.SQLITE_INSERT:
            if table == "shared_message_bodies":
                return sqlite3.SQLITE_DENY
            if table == "message_storage" and trigger != "shared_messages_insert":
                return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_READ and table in {
            "messages", "message_storage", "shared_message_bodies"
        }:
            if shared_storage and trigger in {
                "shared_messages_insert",
                "shared_body_reference_insert",
                "export_no_replace_messages",
            }:
                return sqlite3.SQLITE_OK
            if not scope["new_message_read"]:
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorize)
    return scope


def _row_fingerprint(row) -> int:
    return int.from_bytes(hashlib.sha256(_json(tuple(row)).encode()).digest(), "big")


def _record(raw: dict, source_id: str, snapshot: int, position: int):
    if not isinstance(raw, dict):
        raise ValueError("Export observations must be objects.")
    if raw.get("source_id", source_id) != source_id:
        raise ValueError("An export observation crosses its source partition.")
    record_id = _text(raw.get("extraction_record_id"), "extraction record identifier")
    key = _text(raw.get("conversation_key"), "conversation key")
    peer = _text(raw.get("peer_id", ""), "conversation peer", empty=True)
    title = _text(raw.get("title", peer or key), "conversation title", empty=True)
    kind = raw.get("kind", "unclassified")
    if kind not in {"direct", "group", "unclassified"}:
        raise ValueError("Unsupported export conversation kind.")
    sender = _text(raw.get("sender_id", ""), "source sender identifier", empty=True)
    label = _text(raw.get("sender_label", ""), "source sender label", empty=True)
    text = _text(raw.get("text"), "message text", empty=True)
    direction = raw.get("direction", "unknown")
    if direction not in {"sent", "received", "unknown"}:
        raise ValueError("Unsupported export message direction.")
    ordinal = raw.get("ordinal", position)
    if type(ordinal) is not int or ordinal < 1:
        raise ValueError("Export message ordinals must be positive integers.")
    flags = raw.get("flags", [])
    if not isinstance(flags, list) or any(not isinstance(flag, str) for flag in flags):
        raise ValueError("Export quality flags must be a list of strings.")
    original = raw.get("provenance", {})
    if not isinstance(original, dict):
        raise ValueError("Export provenance must be an object.")
    raw_time = raw.get("sent_at_raw", "")
    if raw_time is not None:
        _text(raw_time, "source clock", empty=True)
    clock, precision = raw_time, ""
    if source_id.startswith("html-"):
        normalized_clock, precision = normalize_html_clock(raw_time)
        full_iso_clock = isinstance(raw_time, str) and re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}"
            r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})?",
            raw_time,
        )
        epoch_placeholder = isinstance(raw_time, str) and re.match(
            r"1970-0?1-0?1(?:[ T]|$)", raw_time.strip()
        )
        clock = normalized_clock or (raw_time if full_iso_clock else "")
        if epoch_placeholder:
            clock = ""
        if normalized_clock and precision == "minute":
            flags = [*flags]
            if "export_timestamp_precision_minutes" not in flags:
                flags.append("export_timestamp_precision_minutes")
    time_sort, timestamp, time_label, basis = _time_fields(
        {"sent_at_raw": clock, "flags": flags}
    )
    if timestamp and precision == "minute":
        basis += "; precision: minute (seconds unavailable)"
    provenance = {
        **original,
        "record_id": record_id,
        "source_snapshot": snapshot,
        "source_time": raw_time,
    }
    if any(key in original for key in ("record_id", "source_snapshot", "source_time")):
        provenance["raw_export_provenance"] = original
    conversation_id = "wechat:" + _identifier(source_id, key)
    row = (
        _identifier(source_id, record_id),
        conversation_id,
        ordinal,
        sender or None,
        label,
        direction,
        text,
        time_sort,
        timestamp,
        time_label,
        basis,
        _json(provenance),
        _json(flags),
        "\n".join((text, label, title, peer)).casefold(),
    )
    crossrefs = raw.get("crossrefs", [])
    if not isinstance(crossrefs, list) or any(
        not isinstance(ref, dict) for ref in crossrefs
    ):
        raise ValueError("Export cross-references must be a list of evidence objects.")
    return row, (conversation_id, peer, title, kind), record_id, crossrefs


def _verify_new_messages(connection, scope, conversations, expected_count, fingerprint):
    actual_count = 0
    actual_fingerprint = 0
    message_read = (
        "messages" if scope["shared_storage"]
        else "messages INDEXED BY message_conversation_time"
    )
    scope["new_message_read"] = True
    try:
        for identifier, conversation in conversations.items():
            count = 0
            for row in connection.execute(
                f"SELECT * FROM {message_read} "
                "WHERE conversation_id=?",
                (identifier,),
            ):
                count += 1
                actual_fingerprint = (
                    actual_fingerprint + _row_fingerprint(row)
                ) % _MODULUS
            if count != conversation["count"]:
                raise ValueError("A new export conversation has inconsistent messages.")
            actual_count += count
    finally:
        scope["new_message_read"] = False
    if actual_count != expected_count or actual_fingerprint != fingerprint:
        raise ValueError("New export message counts or content fingerprints differ.")


def merge_wechat_export(
    local_store_root: Path | str,
    *,
    source: dict,
    records: Iterable[dict],
    snapshot_id: str,
    expected_records: int | None = None,
    database_cache_mib: int = 16,
) -> dict:
    """Publish one immutable export partition, or skip an exact source/SHA replay.

    A new snapshot with different source bytes requires a different source_id. The
    owner is asserted only when owner_verified and independent owner_evidence are
    present. Counts of observations never assert unique original-message totals.
    """
    _text(snapshot_id, "snapshot identifier")
    if expected_records is not None and (
        type(expected_records) is not int or expected_records < 0
    ):
        raise ValueError("Expected export records must be a nonnegative integer.")
    if type(database_cache_mib) is not int or database_cache_mib < 1:
        raise ValueError(
            "The database cache budget must be a positive integer MiB value."
        )
    metadata = _source_metadata(source)
    source_id = metadata["source_id"]
    encoded_metadata = _json(metadata)
    target = wechat_archive_path(local_store_root)
    policy_path = target.with_name("exclusions.json")
    policy_identity = _file_identity(policy_path)
    exclusions = _source_exclusions(policy_path)
    if source_id in exclusions:
        raise ValueError("This export source is excluded by the local source policy.")
    database_identity = _database_identity(target)
    if any(
        database_identity[index] and database_identity[index][2] for index in (1, 2)
    ):
        raise ValueError("Finish the active SQLite writer before publishing an export.")
    exists = database_identity[0] is not None
    if exists:
        original = sqlite3.connect(target.resolve().as_uri() + "?mode=ro", uri=True)
        original.row_factory = sqlite3.Row
        try:
            old_manifest = _manifest(original, existing=True)
            old_partitions = _partition_rows(original)
            has_receipts = original.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='export_imports'"
            ).fetchone()
            receipt = (
                original.execute(
                    "SELECT * FROM export_imports WHERE source_id=?", (source_id,)
                ).fetchone()
                if has_receipts
                else None
            )
            existing_partition = original.execute(
                "SELECT id FROM partitions WHERE id=?", (source_id,)
            ).fetchone()
            if receipt is not None:
                if (
                    receipt["snapshot_id"] != snapshot_id
                    or receipt["source_sha256"] != metadata["source_sha256"]
                    or receipt["metadata"] != encoded_metadata
                    or existing_partition is None
                    or expected_records is not None
                    and expected_records != receipt["record_count"]
                ):
                    raise ValueError(
                        "An existing export source has different snapshot evidence."
                    )
                if (
                    _database_identity(target) != database_identity
                    or _file_identity(policy_path) != policy_identity
                ):
                    raise ValueError(
                        "The archive or source policy changed during export preflight."
                    )
                return {
                    "changed": False,
                    "replayed": True,
                    "source_id": source_id,
                    "snapshot_id": snapshot_id,
                    "source_records": receipt["record_count"],
                    "input_records": 0,
                    "inserted_messages": 0,
                    "inserted_conversations": 0,
                    "new_evidence": 0,
                    "messages_before": old_manifest["messages"],
                    "messages_after": old_manifest["messages"],
                    "manifest_before": old_manifest,
                    "manifest_after": old_manifest,
                    "content_fingerprint": receipt["content_fingerprint"],
                    "unique_original_message_count": None,
                    "database_path": str(target),
                }
            if existing_partition is not None:
                raise ValueError(
                    "A new export must not reuse an existing source partition."
                )
        finally:
            original.close()
    else:
        old_manifest = {"schema_version": 1, "sources": [], "messages": 0}
        old_partitions = []
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=".wechat-export-", suffix=".sqlite3", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(name)
    connection = None
    try:
        if exists:
            temporary.unlink()
            _clonefile(target, temporary)
            if _database_identity(target) != database_identity:
                raise ValueError(
                    "The archive changed while creating its copy-on-write clone."
                )
        os.chmod(temporary, 0o600)
        connection = sqlite3.connect(temporary)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA temp_store=FILE")
        connection.execute(f"PRAGMA cache_size={-database_cache_mib * 1024}")
        if not exists:
            connection.executescript(_SCHEMA)
        had_evidence = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_evidence'"
        ).fetchone()
        connection.executescript(_EXPORT_SCHEMA)
        if not had_evidence:
            connection.execute(
                "CREATE INDEX evidence_message ON message_evidence(message_id)"
            )
        scope = _install_guard(connection)
        owner = (
            metadata.get("source_account_id") if metadata["owner_verified"] else None
        )
        account_key = "account:" + owner if owner else "source:" + source_id
        account_label = str(
            metadata.get("source_account_alias") or owner or "Unconfirmed"
        )
        connection.execute(
            "INSERT INTO partitions VALUES(?,?,?,?,?,?,?,?)",
            (
                source_id,
                account_key,
                owner,
                account_label
                + " · "
                + (owner[-6:] if owner else metadata["source_file"]),
                "Corroborated export ID" if owner else "Source account unconfirmed",
                metadata["source_label"],
                metadata["source_file"],
                str(metadata.get("payload_file") or ""),
            ),
        )
        snapshot = connection.execute(
            "INSERT INTO source_snapshots(snapshot_id,source_id,metadata) VALUES(?,?,?)",
            (snapshot_id, source_id, encoded_metadata),
        ).lastrowid
        count = 0
        evidence_count = 0
        fingerprint = 0
        conversations = {}
        for raw in records:
            count += 1
            row, conversation, record_id, crossrefs = _record(
                raw, source_id, snapshot, count
            )
            if owner and raw.get("source_account_id", owner) != owner:
                raise ValueError("An export message crosses its verified source owner.")
            identifier, peer, title, kind = conversation
            entry = conversations.setdefault(
                identifier,
                {
                    "identity": conversation,
                    "count": 0,
                    "first": None,
                    "last": None,
                    "fallback_first": (row[2], row[8]),
                    "fallback_last": (row[2], row[8], row[6][:300]),
                },
            )
            if entry["identity"] != conversation:
                raise ValueError(
                    "One export conversation key has conflicting metadata."
                )
            entry["count"] += 1
            first = (row[7], row[2], row[8])
            last = (row[7], row[2], row[8], row[6][:300])
            if row[7] and (entry["first"] is None or first[:2] < entry["first"][:2]):
                entry["first"] = first
            if row[7] and (entry["last"] is None or last[:2] > entry["last"][:2]):
                entry["last"] = last
            if row[2] < entry["fallback_first"][0]:
                entry["fallback_first"] = (row[2], row[8])
            if row[2] > entry["fallback_last"][0]:
                entry["fallback_last"] = (row[2], row[8], row[6][:300])
            connection.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row
            )
            fingerprint = (fingerprint + _row_fingerprint(row)) % _MODULUS
            for crossref in crossrefs:
                observation = _json(
                    {
                        "kind": "export_cross_reference",
                        "content_sha256": hashlib.sha256(row[6].encode()).hexdigest(),
                        "cross_reference": crossref,
                    }
                )
                evidence_count += connection.execute(
                    "INSERT OR IGNORE INTO message_evidence VALUES(?,?,?,?,?,?,?)",
                    (
                        _identifier(source_id, record_id, observation),
                        row[0],
                        source_id,
                        snapshot_id,
                        record_id,
                        observation,
                        "cross_reference",
                    ),
                ).rowcount
        if expected_records is not None and count != expected_records:
            raise ValueError(
                "Export record count differs from the verified input count."
            )
        for identifier, entry in conversations.items():
            _, peer, title, kind = entry["identity"]
            first = entry["first"] or (0, *entry["fallback_first"])
            last = entry["last"] or (0, *entry["fallback_last"])
            connection.execute(
                "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    source_id,
                    account_key,
                    peer,
                    title,
                    kind,
                    entry["count"],
                    first[0],
                    last[0],
                    first[2],
                    last[2],
                    last[3],
                ),
            )
        _verify_new_messages(connection, scope, conversations, count, fingerprint)
        for old in old_partitions:
            retained = connection.execute(
                "SELECT * FROM partitions WHERE id=?", (old[0],)
            ).fetchone()
            if retained is None or tuple(retained) != old:
                raise ValueError("Pre-existing source partition evidence changed.")
        content_fingerprint = f"sha256-sum:{fingerprint:064x}"
        connection.execute(
            "INSERT INTO export_imports VALUES(?,?,?,?,?,?,?,?)",
            (
                source_id,
                snapshot_id,
                metadata["source_sha256"],
                encoded_metadata,
                count,
                len(conversations),
                evidence_count,
                content_fingerprint,
            ),
        )
        manifest = json.loads(_json(old_manifest))
        manifest["sources"].append(
            {
                "source_id": source_id,
                "account_id": owner,
                "messages": count,
                "conversations": len(conversations),
                "snapshot_id": snapshot_id,
                "source_sha256": metadata["source_sha256"],
            }
        )
        manifest["messages"] += count
        manifest["unique_original_message_count"] = None
        event = {
            "source_id": source_id,
            "snapshot_id": snapshot_id,
            "at": datetime.now(UTC).isoformat(),
            "inserted_messages": count,
            "inserted_conversations": len(conversations),
            "content_fingerprint": content_fingerprint,
        }
        imports = manifest.setdefault("export_imports", [])
        if not isinstance(imports, list):
            raise ValueError("The existing export import history is not a list.")
        imports.append(event)
        connection.execute(
            "INSERT OR REPLACE INTO archive_info VALUES('manifest',?)",
            (_json(manifest),),
        )
        connection.commit()
        connection.close()
        connection = None
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        if (
            _database_identity(target) != database_identity
            or _file_identity(policy_path) != policy_identity
        ):
            raise ValueError(
                "The archive or exclusion policy changed during export publication."
            )
        os.replace(temporary, target)
        return {
            "changed": True,
            "replayed": False,
            "source_id": source_id,
            "snapshot_id": snapshot_id,
            "source_records": count,
            "input_records": count,
            "inserted_messages": count,
            "inserted_conversations": len(conversations),
            "added_partitions": 1,
            "added_source_snapshots": 1,
            "new_evidence": evidence_count,
            "messages_before": old_manifest["messages"],
            "messages_after": manifest["messages"],
            "manifest_before": old_manifest,
            "manifest_after": manifest,
            "old_partition_rows_verified": len(old_partitions),
            "old_message_rows_read": 0,
            "old_identity_rows_unchanged": True,
            "content_fingerprint": content_fingerprint,
            "unique_original_message_count": None,
            "database_path": str(target),
            "database_bytes": target.stat().st_size,
        }
    finally:
        if connection is not None:
            connection.close()
        for suffix in ("", "-journal", "-wal", "-shm"):
            Path(str(temporary) + suffix).unlink(missing_ok=True)
