"""Add native text observations without replacing earlier WeChat evidence."""

# Code version: v1.1.0-codex.0

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any

from .wechat_archive import (
    _SCHEMA,
    _identifier,
    _source_exclusions,
    _time_fields,
    wechat_archive_path,
)


_ADDITIVE_SCHEMA = """
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
CREATE INDEX IF NOT EXISTS evidence_message ON message_evidence(message_id);
CREATE TEMP TABLE reconcile_server_ids (
 conversation_id TEXT NOT NULL, server_id TEXT NOT NULL, message_id TEXT NOT NULL,
 PRIMARY KEY(conversation_id, server_id, message_id)
) WITHOUT ROWID;
"""
_APP_TEXT_KINDS = {"app_text", "text_appmsg", "forwarded_text", "quoted_reply"}
_HEX = re.compile(r"[0-9a-f]{32}")


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _file_identity(path: Path):
    if path.is_symlink():
        raise ValueError("Reconciliation files must not be symbolic links.")
    if not path.exists():
        return None
    metadata = path.stat()
    if not path.is_file():
        raise ValueError("Reconciliation files must be regular files.")
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _database_identity(path: Path):
    return tuple(
        _file_identity(Path(str(path) + suffix))
        for suffix in ("", "-wal", "-journal", "-shm")
    )


def _server_id(value: Any) -> str:
    # Keep large identifiers as decimal strings, never floating-point numbers.
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    text = str(value)
    return text if re.fullmatch(r"-?[1-9][0-9]*", text) else ""


def _epoch(value: Any):
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number > 0 else None


def _comparable_time(provenance, timestamp, basis, flags):
    if basis == "Unavailable source time" or "possible_epoch_zero_placeholder" in flags:
        return None
    unix_time = _epoch(provenance.get("unix_timestamp"))
    if unix_time is not None:
        return unix_time
    try:
        parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return Decimal(str(parsed.timestamp()))
    except (ValueError, OverflowError, OSError):
        pass
    return None


def _compatible(row: sqlite3.Row, record: dict[str, Any]) -> bool:
    if row["text"] != record["text"]:
        return False
    old_provenance = json.loads(row["provenance"])
    old_server_id = _server_id(old_provenance.get("server_id"))
    if old_server_id and record["server_id"] and old_server_id != record["server_id"]:
        return False
    old_sender = row["sender_id"] or ""
    if old_sender and record["sender_id"] and old_sender != record["sender_id"]:
        return False
    if (
        row["direction"] != "unknown"
        and record["direction"] != "unknown"
        and row["direction"] != record["direction"]
    ):
        return False
    old_time = _comparable_time(
        old_provenance,
        row["timestamp_value"],
        row["time_basis"],
        json.loads(row["quality_flags"]),
    )
    new_time = record["comparable_time"]
    return old_time is None or new_time is None or old_time == new_time


def _is_replay(row: sqlite3.Row, record: dict[str, Any]) -> bool:
    return (
        json.loads(row["provenance"]) == record["provenance"]
        and (row["sender_id"] or "") == record["sender_id"]
        and row["sender_label"] == record["sender"]
        and row["direction"] == record["direction"]
        and row["timestamp_value"] == record["timestamp"]
        and json.loads(row["quality_flags"]) == json.loads(record["flags"])
    )


def _normalize_sources(sources, exclusions):
    result = {}
    excluded = []
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("Native source metadata must be an object.")
        source_id = source.get("source_id")
        if not isinstance(source_id, str):
            raise ValueError("Native source metadata needs a source identifier.")
        if source_id in exclusions:
            excluded.append(source_id)
            continue
        if source_id in result:
            raise ValueError("Duplicate native source identifier.")
        account_hash = source.get("account_hash")
        if not isinstance(account_hash, str) or not _HEX.fullmatch(account_hash):
            raise ValueError("Native source metadata needs its account directory hash.")
        owner = source.get("source_account_id")
        verified = source.get("account_hash_verified") is True
        if verified:
            if (
                not isinstance(owner, str)
                or not owner
                or hashlib.md5(owner.encode()).hexdigest() != account_hash
                or source_id != "ios-" + account_hash
            ):
                raise ValueError("Verified source account does not match its hash.")
            account_key = "account:" + owner
        else:
            if source_id != "ios-unconfirmed-" + account_hash:
                raise ValueError("Unconfirmed native sources must stay independent.")
            account_key = "source:" + source_id
        metadata = dict(source)
        encoded = _json(metadata)
        if len(encoded.encode()) > 8 * 1024 * 1024:
            raise ValueError("Native source metadata exceeds the supported size.")
        result[source_id] = {
            "metadata": metadata,
            "metadata_json": encoded,
            "owner": owner if verified else None,
            "account_key": account_key,
            "verified": verified,
        }
    return result, sorted(set(excluded))


def _normalize_record(raw, source, source_snapshot):
    if not isinstance(raw, dict):
        raise ValueError("A native text observation must be an object.")
    if source["verified"] and raw.get("source_account_id") != source["owner"]:
        raise ValueError("A message crosses its verified source account.")
    source_id = source["metadata"]["source_id"]
    if raw.get("account_partition", source_id) != source_id:
        raise ValueError("A message crosses its source partition.")
    record_id = raw.get("extraction_record_id")
    text = raw.get("text")
    locator = raw.get("source_locator", {})
    flags = raw.get("flags", [])
    if not isinstance(record_id, str) or not record_id or "\0" in record_id:
        raise ValueError("Native text observations need stable extraction identifiers.")
    if not isinstance(text, str) or not isinstance(locator, dict):
        raise ValueError("Native text and source locators have incompatible types.")
    if not isinstance(flags, list) or any(not isinstance(flag, str) for flag in flags):
        raise ValueError("Native quality flags must be a list of strings.")
    original_type = raw.get("message_type")
    text_kind = str(raw.get("text_extraction_kind") or "plain_text")
    if type(original_type) is not int or not (
        original_type == 1
        or original_type == 49
        and text_kind in _APP_TEXT_KINDS
        or original_type in {10000, 10002}
        and text_kind == "system_text"
    ):
        raise ValueError("Only explicitly classified native text can be reconciled.")
    conversation_hash = raw.get("conversation_hash")
    if not isinstance(conversation_hash, str) or not _HEX.fullmatch(conversation_hash):
        raise ValueError("Native observations need their conversation table hash.")
    peer = str(raw.get("conversation_id") or "")
    if peer and hashlib.md5(peer.encode()).hexdigest() != conversation_hash:
        raise ValueError("Conversation ID does not match its native table hash.")
    kind = str(raw.get("conversation_kind") or "unclassified")
    if kind not in {"direct", "group", "unclassified"}:
        raise ValueError("Unsupported native conversation kind.")
    direction = raw.get("direction")
    direction = direction if direction in {"sent", "received"} else "unknown"
    sender_id = str(raw.get("sender_id") or "")
    sender = str(raw.get("sender_display_name") or "").strip()
    if not sender or sender.casefold() in {"none", "null"}:
        sender = sender_id or "Unknown sender"
    time_sort, timestamp, timestamp_label, basis = _time_fields(raw)
    provenance = {
        "record_id": record_id,
        "locator": locator,
        "server_id": raw.get("server_id"),
        "unix_timestamp": raw.get("unix_timestamp"),
        "source_time": raw.get("sent_at_raw"),
        "source_snapshot": source_snapshot,
        "message_type": original_type,
        "text_extraction_kind": text_kind,
    }
    # Extra observations contain a digest, not another copy of the message body.
    observation = {
        **provenance,
        "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "sender_id": sender_id,
        "sender_label": sender,
        "direction": direction,
        "timestamp_value": timestamp,
        "time_basis": basis,
        "flags": flags,
    }
    observation_json = _json(observation)
    return {
        "record_id": record_id,
        "base_id": _identifier(source_id, record_id),
        "observation_id": _identifier(source_id, record_id, observation_json),
        "observation_json": observation_json,
        "provenance": provenance,
        "conversation_hash": conversation_hash,
        "peer": peer,
        "title": str(
            raw.get("conversation_display_name")
            or peer
            or "Unidentified conversation · " + conversation_hash[-8:]
        ),
        "kind": kind,
        "direction": direction,
        "sender_id": sender_id,
        "sender": sender,
        "text": text,
        "time_sort": time_sort,
        "timestamp": timestamp,
        "timestamp_label": timestamp_label,
        "basis": basis,
        "comparable_time": _comparable_time(provenance, timestamp, basis, flags),
        "flags": _json(flags),
        "server_id": _server_id(raw.get("server_id")),
    }


def _old_fingerprints(connection):
    result = {}
    specifications = (
        ("messages", "m.*", "id"),
        ("partitions", "m.*", "id"),
        (
            "conversations",
            "m.id,m.partition_id,m.account_key,m.peer_id,m.title,m.kind",
            "id",
        ),
    )
    for table, columns, key in specifications:
        digest = hashlib.sha256()
        count = 0
        for row in connection.execute(
            f"SELECT {columns} FROM reconcile_old_{table} o "
            f"CROSS JOIN {table} m ON m.{key}=o.id ORDER BY o.id"
        ):
            digest.update(_json(tuple(row)).encode())
            digest.update(b"\n")
            count += 1
        result[table] = {"rows": count, "sha256": digest.hexdigest()}
    return result


def _update_conversation(connection, conversation_id):
    total = connection.execute(
        "SELECT COUNT(*) FROM messages WHERE conversation_id=?", (conversation_id,)
    ).fetchone()[0]
    first = connection.execute(
        "SELECT time_sort,timestamp_value FROM messages WHERE conversation_id=? "
        "AND time_sort!=0 ORDER BY time_sort,ordinal LIMIT 1",
        (conversation_id,),
    ).fetchone()
    last = connection.execute(
        "SELECT time_sort,timestamp_value,text FROM messages WHERE conversation_id=? "
        "AND time_sort!=0 ORDER BY time_sort DESC,ordinal DESC LIMIT 1",
        (conversation_id,),
    ).fetchone()
    if first is None:
        first = connection.execute(
            "SELECT time_sort,timestamp_value FROM messages WHERE conversation_id=? "
            "ORDER BY ordinal LIMIT 1",
            (conversation_id,),
        ).fetchone()
        last = connection.execute(
            "SELECT time_sort,timestamp_value,text FROM messages WHERE conversation_id=? "
            "ORDER BY ordinal DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
    connection.execute(
        "UPDATE conversations SET message_count=?,first_sort=?,last_sort=?,"
        "first_time=?,last_time=?,latest_text=? WHERE id=?",
        (total, first[0], last[0], first[1], last[1], last[2][:300], conversation_id),
    )


def reconcile_wechat_records(
    local_store_root: Path | str,
    *,
    sources: list[dict],
    records: Iterable[dict],
    snapshot_id: str,
    database_cache_mib: int = 16,
    server_id_cache_mib: int = 8,
) -> dict[str, Any]:
    """Atomically add verified text; old messages and partition evidence never change."""
    if not isinstance(snapshot_id, str) or not snapshot_id or "\0" in snapshot_id:
        raise ValueError("Reconciliation needs a stable snapshot identifier.")
    if any(
        type(value) is not int or value <= 0
        for value in (database_cache_mib, server_id_cache_mib)
    ):
        raise ValueError("SQLite cache budgets must be positive integer MiB values.")
    target = wechat_archive_path(local_store_root)
    policy_path = target.with_name("exclusions.json")
    policy_identity = _file_identity(policy_path)
    exclusions = _source_exclusions(policy_path)
    metadata, excluded_sources = _normalize_sources(sources, exclusions)
    database_identity = _database_identity(target)
    if any(
        database_identity[index] and database_identity[index][2] for index in (1, 2)
    ):
        raise ValueError("Finish the active SQLite writer before reconciling.")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=".wechat-reconcile-", suffix=".sqlite3", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(name)
    connection = sqlite3.connect(temporary)
    connection.row_factory = sqlite3.Row
    report = {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "input_records": 0,
        "inserted_messages": 0,
        "matched_messages": 0,
        "replayed_observations": 0,
        "new_evidence": 0,
        "conflict_variants": 0,
        "identity_conflicts": 0,
        "excluded_records": 0,
        "excluded_sources": excluded_sources,
        "added_partitions": 0,
        "added_conversations": 0,
        "added_source_snapshots": 0,
        "unique_original_message_count": None,
    }
    try:
        if database_identity[0] is not None:
            original = sqlite3.connect(target.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                original.backup(connection)
            finally:
                original.close()
        else:
            connection.executescript(_SCHEMA)
        # This file is disposable until its complete integrity check and atomic publication.
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA temp_store=FILE")
        connection.execute(f"PRAGMA cache_size={-database_cache_mib * 1024}")
        connection.executescript(_ADDITIVE_SCHEMA)
        connection.execute(f"PRAGMA temp.cache_size={-server_id_cache_mib * 1024}")
        for table in ("messages", "partitions", "conversations"):
            connection.execute(
                f"CREATE TEMP TABLE reconcile_old_{table}(id TEXT PRIMARY KEY) WITHOUT ROWID"
            )
            connection.execute(
                f"INSERT INTO reconcile_old_{table} SELECT id FROM {table}"
            )
        before = _old_fingerprints(connection)
        report["messages_before"] = before["messages"]["rows"]
        report["conversations_before"] = before["conversations"]["rows"]
        report["source_partitions_before"] = before["partitions"]["rows"]
        if any(
            row[0] in exclusions
            for row in connection.execute("SELECT id FROM partitions")
        ):
            raise ValueError("An excluded source already exists in the current index.")
        sources_by_id = {}
        for source_id, source in metadata.items():
            existing = connection.execute(
                "SELECT * FROM partitions WHERE id=?", (source_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["account_key"] != source["account_key"]
                    or existing["account_id"] != source["owner"]
                ):
                    raise ValueError(
                        "Native source conflicts with the existing partition."
                    )
            else:
                owner = source["owner"]
                alias = str(source["metadata"].get("source_account_alias") or "")
                source_file = str(
                    source["metadata"].get("source_file") or "Finder backup"
                )
                label = (
                    f"{alias or owner} · {owner[-6:]}"
                    if owner
                    else "Unconfirmed · " + source_file
                )
                connection.execute(
                    "INSERT INTO partitions VALUES(?,?,?,?,?,?,?,?)",
                    (
                        source_id,
                        source["account_key"],
                        owner,
                        label,
                        "Verified archive ID"
                        if owner
                        else "Source account unconfirmed",
                        "iPhone archive",
                        source_file,
                        "",
                    ),
                )
                report["added_partitions"] += 1
            previous_snapshot = connection.execute(
                "SELECT id FROM source_snapshots WHERE snapshot_id=? "
                "AND source_id=? AND metadata=?",
                (snapshot_id, source_id, source["metadata_json"]),
            ).fetchone()
            if previous_snapshot:
                source_snapshot = previous_snapshot[0]
            else:
                cursor = connection.execute(
                    "INSERT INTO source_snapshots(snapshot_id,source_id,metadata) VALUES(?,?,?)",
                    (snapshot_id, source_id, source["metadata_json"]),
                )
                source_snapshot = cursor.lastrowid
                report["added_source_snapshots"] += 1
            sources_by_id[source_id] = {
                **source,
                "snapshot": source_snapshot,
                "ordinal": connection.execute(
                    "SELECT COALESCE(MAX(m.ordinal),0) FROM messages m "
                    "JOIN conversations c ON c.id=m.conversation_id WHERE c.partition_id=?",
                    (source_id,),
                ).fetchone()[0],
            }
        conversations = {}
        by_hash = {}
        for row in connection.execute("SELECT * FROM conversations"):
            if row["partition_id"] not in sources_by_id:
                continue
            value = dict(row)
            conversations[row["id"]] = value
            if row["peer_id"]:
                key = (
                    row["partition_id"],
                    hashlib.md5(row["peer_id"].encode()).hexdigest(),
                )
                by_hash.setdefault(key, set()).add(row["id"])
        # This index stays on disk and grows independently of Python memory.
        for row in connection.execute(
            "SELECT m.id,m.conversation_id,m.provenance,c.partition_id "
            "FROM messages m JOIN conversations c ON c.id=m.conversation_id"
        ):
            source = sources_by_id.get(row["partition_id"])
            if source is None or not source["verified"]:
                continue
            server_id = _server_id(json.loads(row["provenance"]).get("server_id"))
            if server_id:
                connection.execute(
                    "INSERT INTO reconcile_server_ids VALUES(?,?,?)",
                    (row["conversation_id"], server_id, row["id"]),
                )
        for row in connection.execute(
            "SELECT e.message_id,e.source_id,e.observation,m.conversation_id,c.partition_id "
            "FROM message_evidence e JOIN messages m ON m.id=e.message_id "
            "JOIN conversations c ON c.id=m.conversation_id"
        ):
            source = sources_by_id.get(row["partition_id"])
            if source is None or not source["verified"]:
                continue
            if row["source_id"] != row["partition_id"]:
                raise ValueError("Saved message evidence crosses a source partition.")
            server_id = _server_id(json.loads(row["observation"]).get("server_id"))
            if server_id:
                connection.execute(
                    "INSERT OR IGNORE INTO reconcile_server_ids VALUES(?,?,?)",
                    (row["conversation_id"], server_id, row["message_id"]),
                )
        touched = set()
        for raw in records:
            report["input_records"] += 1
            if not isinstance(raw, dict):
                raise ValueError("A native text observation must be an object.")
            source_id = raw.get("source_id")
            if not isinstance(source_id, str):
                raise ValueError("A native text observation needs a source identifier.")
            if source_id in exclusions:
                report["excluded_records"] += 1
                continue
            source = sources_by_id.get(source_id)
            if source is None:
                raise ValueError("A message lacks declared native source metadata.")
            record = _normalize_record(raw, source, source["snapshot"])
            candidates = set(by_hash.get((source_id, record["conversation_hash"]), ()))
            for key in (record["peer"], record["conversation_hash"]):
                candidate = "wechat:" + _identifier(source_id, key)
                if key and candidate in conversations:
                    candidates.add(candidate)
            if candidates:
                peer_candidate = "wechat:" + _identifier(source_id, record["peer"])
                conversation_id = (
                    peer_candidate
                    if record["peer"] and peer_candidate in candidates
                    else min(candidates)
                )
            else:
                conversation_id = "wechat:" + _identifier(
                    source_id, record["peer"] or record["conversation_hash"]
                )
                connection.execute(
                    "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        conversation_id,
                        source_id,
                        source["account_key"],
                        record["peer"],
                        record["title"],
                        record["kind"],
                        0,
                        0,
                        0,
                        "",
                        "",
                        "",
                    ),
                )
                conversations[conversation_id] = {
                    "id": conversation_id,
                    "partition_id": source_id,
                }
                candidates.add(conversation_id)
                report["added_conversations"] += 1
            by_hash.setdefault((source_id, record["conversation_hash"]), set()).update(
                candidates
            )
            direct = connection.execute(
                "SELECT * FROM messages WHERE id=?", (record["base_id"],)
            ).fetchone()
            match = None
            match_kind = "new"
            identity_conflict = direct is not None and (
                direct["conversation_id"] not in candidates
                or not _compatible(direct, record)
            )
            if identity_conflict:
                report["identity_conflicts"] += 1
                variant_id = _identifier(
                    source_id, record["record_id"], "variant", record["observation_id"]
                )
                variant = connection.execute(
                    "SELECT * FROM messages WHERE id=?", (variant_id,)
                ).fetchone()
                if variant is not None:
                    if variant["conversation_id"] not in candidates or not _compatible(
                        variant, record
                    ):
                        raise ValueError(
                            "A retained message variant conflicts with its identity."
                        )
                    match = variant
                    match_kind = "extraction_id_variant"
            elif direct is not None:
                match = direct
                match_kind = "extraction_id"
            sid_conflict = False
            if match is None and source["verified"] and record["server_id"]:
                placeholders = ",".join("?" for _ in candidates)
                matches = connection.execute(
                    "SELECT m.* FROM reconcile_server_ids s "
                    "JOIN messages m ON m.id=s.message_id "
                    f"WHERE s.conversation_id IN ({placeholders}) AND s.server_id=? "
                    "ORDER BY m.id",
                    [*sorted(candidates), record["server_id"]],
                )
                for candidate in matches:
                    sid_conflict = True
                    if _compatible(candidate, record):
                        match = candidate
                        match_kind = "server_id"
                        break
            if match is not None:
                if source["verified"] and record["server_id"]:
                    connection.execute(
                        "INSERT OR IGNORE INTO reconcile_server_ids VALUES(?,?,?)",
                        (match["conversation_id"], record["server_id"], match["id"]),
                    )
                if _is_replay(match, record):
                    report["replayed_observations"] += 1
                    continue
                inserted = connection.execute(
                    "INSERT OR IGNORE INTO message_evidence VALUES(?,?,?,?,?,?,?)",
                    (
                        record["observation_id"],
                        match["id"],
                        source_id,
                        snapshot_id,
                        record["record_id"],
                        record["observation_json"],
                        match_kind,
                    ),
                ).rowcount
                if inserted:
                    report["new_evidence"] += 1
                    report["matched_messages"] += 1
                else:
                    report["replayed_observations"] += 1
                continue
            message_id = record["base_id"]
            conflict = identity_conflict or sid_conflict
            if identity_conflict:
                message_id = _identifier(
                    source_id, record["record_id"], "variant", record["observation_id"]
                )
            collision = connection.execute(
                "SELECT * FROM messages WHERE id=?", (message_id,)
            ).fetchone()
            if collision is not None:
                raise ValueError(
                    "A retained message variant conflicts with its identity."
                )
            source["ordinal"] += 1
            connection.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    message_id,
                    conversation_id,
                    source["ordinal"],
                    record["sender_id"] or None,
                    record["sender"],
                    record["direction"],
                    record["text"],
                    record["time_sort"],
                    record["timestamp"],
                    record["timestamp_label"],
                    record["basis"],
                    _json(record["provenance"]),
                    record["flags"],
                    "\n".join(
                        (
                            record["text"],
                            record["sender"],
                            record["title"],
                            record["peer"],
                        )
                    ).casefold(),
                ),
            )
            if source["verified"] and record["server_id"]:
                connection.execute(
                    "INSERT INTO reconcile_server_ids VALUES(?,?,?)",
                    (conversation_id, record["server_id"], message_id),
                )
            touched.add(conversation_id)
            report["inserted_messages"] += 1
            if conflict:
                report["conflict_variants"] += 1
        for conversation_id in touched:
            _update_conversation(connection, conversation_id)
        after = _old_fingerprints(connection)
        if before != after:
            raise ValueError(
                "Reconciliation changed pre-existing message or identity rows."
            )
        report.update(
            cache_budgets_mib={
                "database": database_cache_mib,
                "server_ids": server_id_cache_mib,
            },
            messages_after=connection.execute(
                "SELECT COUNT(*) FROM messages"
            ).fetchone()[0],
            conversations=connection.execute(
                "SELECT COUNT(*) FROM conversations"
            ).fetchone()[0],
            source_partitions=connection.execute(
                "SELECT COUNT(*) FROM partitions"
            ).fetchone()[0],
            old_messages_unchanged=True,
            old_partitions_unchanged=True,
            old_conversation_identity_unchanged=True,
            preserved_fingerprints=after,
            database_path=str(target),
        )
        report["changed"] = any(
            report[key]
            for key in (
                "inserted_messages",
                "new_evidence",
                "added_partitions",
                "added_conversations",
                "added_source_snapshots",
            )
        )
        if report["changed"]:
            row = connection.execute(
                "SELECT value FROM archive_info WHERE key='manifest'"
            ).fetchone()
            manifest = json.loads(row[0]) if row else {"schema_version": 1}
            manifest["sources"] = [
                dict(row)
                for row in connection.execute(
                    "SELECT p.id AS source_id,p.account_id,"
                    "COUNT(c.id) AS conversations,COALESCE(SUM(c.message_count),0) AS messages "
                    "FROM partitions p LEFT JOIN conversations c ON c.partition_id=p.id GROUP BY p.id"
                )
            ]
            manifest["messages"] = report["messages_after"]
            manifest["unique_original_message_count"] = None
            manifest.setdefault("reconciliations", []).append(
                {
                    "snapshot_id": snapshot_id,
                    "at": datetime.now(UTC).isoformat(),
                    **{
                        key: report[key]
                        for key in (
                            "inserted_messages",
                            "matched_messages",
                            "new_evidence",
                            "conflict_variants",
                            "messages_before",
                            "messages_after",
                        )
                    },
                }
            )
            connection.execute(
                "INSERT OR REPLACE INTO archive_info VALUES('manifest',?)",
                (_json(manifest),),
            )
        connection.commit()
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        if [row[0] for row in connection.execute("PRAGMA quick_check")] != ["ok"]:
            raise ValueError("Reconciled database failed its integrity check.")
        if connection.execute(
            "SELECT 1 FROM message_evidence e LEFT JOIN messages m ON m.id=e.message_id "
            "WHERE m.id IS NULL LIMIT 1"
        ).fetchone():
            raise ValueError("Reconciled observations reference missing messages.")
        report["integrity_check"] = "ok"
        connection.close()
        if (
            _database_identity(target) != database_identity
            or _file_identity(policy_path) != policy_identity
        ):
            raise ValueError(
                "The archive or exclusion policy changed during reconciliation."
            )
        if report["changed"]:
            os.chmod(temporary, 0o600)
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        report["database_bytes"] = target.stat().st_size if target.exists() else 0
        return report
    finally:
        connection.close()
        temporary.unlink(missing_ok=True)
        for suffix in ("-journal", "-wal", "-shm"):
            Path(str(temporary) + suffix).unlink(missing_ok=True)
