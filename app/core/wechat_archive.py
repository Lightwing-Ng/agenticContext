"""Source-separated, text-only WeChat archive import and indexed local queries."""

# Code version: v1.1.1-codex.0

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol


WECHAT_SOURCE = "wechat"
WECHAT_KINDS = ("all", "direct", "group", "unclassified")
_DISPLAY_ZONE = timezone(timedelta(hours=8))
_SCHEMA = """
CREATE TABLE partitions (
 id TEXT PRIMARY KEY, account_key TEXT NOT NULL, account_id TEXT,
 account_label TEXT NOT NULL, identity_status TEXT NOT NULL,
 source_label TEXT NOT NULL, source_file TEXT NOT NULL, payload_file TEXT NOT NULL
);
CREATE TABLE conversations (
 id TEXT PRIMARY KEY, partition_id TEXT NOT NULL, account_key TEXT NOT NULL,
 peer_id TEXT, title TEXT NOT NULL, kind TEXT NOT NULL,
 message_count INTEGER NOT NULL, first_sort REAL NOT NULL, last_sort REAL NOT NULL,
 first_time TEXT NOT NULL, last_time TEXT NOT NULL, latest_text TEXT NOT NULL
);
CREATE TABLE messages (
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
 sender_id TEXT, sender_label TEXT NOT NULL, direction TEXT NOT NULL,
 text TEXT NOT NULL, time_sort REAL NOT NULL, timestamp_value TEXT NOT NULL,
 timestamp_label TEXT NOT NULL, time_basis TEXT NOT NULL,
 provenance TEXT NOT NULL, quality_flags TEXT NOT NULL, search_text TEXT NOT NULL
);
CREATE INDEX message_conversation_time ON messages(conversation_id, time_sort, ordinal);
CREATE INDEX conversation_account_kind ON conversations(account_key, kind, last_sort);
CREATE TABLE archive_info (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def wechat_archive_path(local_store_root: Path | str) -> Path:
    """Keep human messaging separate from every LLM transcript store."""
    return (
        Path(local_store_root).expanduser()
        / "messaging"
        / WECHAT_SOURCE
        / "history.sqlite3"
    )


def _identifier(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _archive_file(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Archive paths must stay inside the selected directory.")
    result = root / path
    if (
        not result.is_file()
        or result.is_symlink()
        or not result.resolve().is_relative_to(root)
    ):
        raise ValueError("Archive member is missing or outside the selected directory.")
    return result


def _read_object(path: Path) -> dict[str, Any]:
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("Archive metadata exceeds the supported size.")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Archive metadata must be a JSON object.")
    return value


def _source_exclusions(path: Path) -> dict[str, dict[str, Any]]:
    """Honor private source decisions before opening any excluded cloud payload."""
    if path.is_symlink():
        raise ValueError("The source exclusion policy must be a local regular file.")
    if not path.exists():
        return {}
    policy = _read_object(path)
    if policy.get("schema_version") != 1 or not isinstance(policy.get("sources"), list):
        raise ValueError("Unsupported source exclusion policy.")
    excluded = {}
    for item in policy["sources"]:
        source_id = item.get("source_id") if isinstance(item, dict) else None
        if (
            not isinstance(source_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", source_id)
            or source_id in excluded
        ):
            raise ValueError("Invalid or duplicate excluded source identifier.")
        excluded[source_id] = item
    return excluded


def _time_fields(record: dict[str, Any]) -> tuple[float, str, str, str]:
    native = record.get("sent_at_utc_iso")
    raw = native or record.get("sent_at_local_iso") or record.get("sent_at_raw")
    flags = record.get("flags", [])
    if (
        not raw
        or record.get("timeline_timestamp_usable") is False
        or "possible_epoch_zero_placeholder" in flags
    ):
        return 0.0, "", "Unknown time", "Unavailable source time"
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if native:
            if parsed.tzinfo is None:
                raise ValueError("Native timestamps require a UTC offset.")
            parsed = parsed.astimezone(_DISPLAY_ZONE)
        # A naive export clock is only a sorting proxy, never an asserted UTC time.
        ordering = parsed.replace(tzinfo=UTC).timestamp()
        if not math.isfinite(ordering):
            raise ValueError("Invalid source time")
    except (ValueError, OverflowError, OSError):
        return 0.0, "", "Unknown time", "Unavailable source time"
    label = f"{parsed.day} {parsed.strftime('%b %Y %H:%M:%S')}"
    return (
        ordering,
        parsed.isoformat(),
        label,
        "UTC+08:00" if native else "Source clock; timezone unknown",
    )


class RecordAdapter(Protocol):
    """Normalize format differences without guessing an archive's account owner."""

    def conversation(
        self, record: dict[str, Any], metadata: dict[str, Any]
    ) -> tuple[str, str, str, str]: ...


class IOSRecordAdapter:
    def conversation(self, record, metadata):
        if record.get("message_type") != 1:
            raise ValueError("Only native Type=1 text messages can be imported.")
        if record.get("source_account_id") != metadata.get("source_account_id"):
            raise ValueError("A message's account conflicts with its source metadata.")
        peer = str(record.get("conversation_id") or "")
        key = peer or str(record.get("conversation_hash") or "")
        if not key:
            raise ValueError("A native conversation needs its original ID or hash.")
        kind = str(record.get("conversation_kind") or "unclassified")
        kind = kind if kind in {"direct", "group"} else "unclassified"
        title = str(
            record.get("conversation_display_name")
            or peer
            or f"Unidentified conversation · {key[-8:]}"
        )
        return key, peer, title, kind


class ExportRecordAdapter:
    def conversation(self, record, metadata):
        label = str(record.get("conversation_label") or "").strip()
        source_name = Path(
            str(metadata.get("source_relative_path") or "Text export")
        ).stem
        title = label or source_name.removesuffix(" - WeChat Chat History")
        # A spreadsheet column named Group Name does not establish a group chat.
        return label or "export-conversation", "", title, "unclassified"


def import_wechat_archive(
    archive_root: Path | str,
    local_store_root: Path | str,
    *,
    source_kinds: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Publish a verified, complete SQLite snapshot; failures preserve the previous one."""
    root = Path(archive_root).expanduser().resolve(strict=True)
    catalog = _read_object(_archive_file(root, "catalog.json"))
    compressed = _read_object(_archive_file(root, "compression_manifest.json"))
    evidence = {item["stored_relative_path"]: item for item in compressed["files"]}
    kinds = source_kinds or {}
    if any(kind not in WECHAT_KINDS[1:] for kind in kinds.values()):
        raise ValueError("Unsupported conversation type.")
    target = wechat_archive_path(local_store_root)
    exclusions = _source_exclusions(target.with_name("exclusions.json"))
    catalog_total = sum(
        int(source["text_rows"]) for source in catalog.get("sources", [])
    )
    if any(
        int(source["text_rows"]) < 0 for source in catalog.get("sources", [])
    ) or catalog_total != int(catalog["total_source_separated_text_rows"]):
        raise ValueError("The source catalog totals are inconsistent.")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=".wechat-import-", suffix=".sqlite3", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(name)
    connection = sqlite3.connect(temporary)
    imported = []
    excluded = []
    payloads = []
    try:
        connection.executescript(_SCHEMA)
        for source in catalog.get("sources", []):
            source_id = str(source["source_id"])
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", source_id):
                raise ValueError("Invalid source partition identifier.")
            if source_id in exclusions:
                excluded.append(
                    {
                        "source_id": source_id,
                        "declared_text_rows": int(source["text_rows"]),
                        "reason": str(
                            exclusions[source_id].get("reason")
                            or "Excluded by local policy"
                        ),
                    }
                )
                continue
            metadata_path = _archive_file(root, f"{source_id}/source.json")
            metadata = _read_object(metadata_path)
            native = metadata.get("account_hash_verified") is True
            adapter: RecordAdapter = (
                IOSRecordAdapter() if native else ExportRecordAdapter()
            )
            owner = str(source.get("source_account_id") or "") if native else ""
            if native and (not owner or owner != metadata.get("source_account_id")):
                raise ValueError("Verified account metadata is inconsistent.")
            if native and (
                hashlib.md5(owner.encode()).hexdigest() != metadata.get("account_hash")
                or source_id != "ios-" + str(metadata.get("account_hash"))
            ):
                raise ValueError(
                    "Source account ID does not match its archive directory hash."
                )
            account_key = f"account:{owner}" if owner else f"source:{source_id}"
            source_label = (
                "iPhone archive"
                if native
                else "Excel export"
                if source_id.startswith("xlsx")
                else "PDF export"
            )
            source_file = Path(
                str(metadata.get("source_relative_path") or source_id)
            ).name
            alias = str(metadata.get("source_account_alias") or "")
            account_label = (
                f"{alias or owner} · {owner[-6:]}"
                if owner
                else f"Unconfirmed · {source_file}"
            )
            payload_name = str(source["messages_file"])
            payload = _archive_file(root, payload_name)
            expected = evidence.get(payload_name)
            if not expected or int(expected["uncompressed_bytes"]) > 512 * 1024 * 1024:
                raise ValueError("Missing or oversized payload integrity evidence.")
            actual_hash = hashlib.sha256()
            with payload.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    actual_hash.update(block)
            if actual_hash.hexdigest() != expected["compressed_sha256"]:
                raise ValueError("Compressed archive integrity check failed.")
            connection.execute(
                "INSERT INTO partitions VALUES(?,?,?,?,?,?,?,?)",
                (
                    source_id,
                    account_key,
                    owner or None,
                    account_label,
                    "Verified archive ID" if native else "Source account unconfirmed",
                    source_label,
                    source_file,
                    payload_name,
                ),
            )
            conversations: dict[str, dict[str, Any]] = {}
            batch = []
            count = 0
            byte_count = 0
            decoded_hash = hashlib.sha256()
            with gzip.open(payload, "rb") as stream:
                while line := stream.readline(2 * 1024 * 1024 + 1):
                    if len(line) > 2 * 1024 * 1024:
                        raise ValueError(
                            "An archive record exceeds the supported size."
                        )
                    byte_count += len(line)
                    if byte_count > int(expected["uncompressed_bytes"]):
                        raise ValueError("Decoded archive exceeds its verified size.")
                    decoded_hash.update(line)
                    record = json.loads(line)
                    if (
                        record.get("source_id") != source_id
                        or record.get("account_partition") != source_id
                    ):
                        raise ValueError(
                            "A message crosses its declared source partition."
                        )
                    key, peer, title, kind = adapter.conversation(record, metadata)
                    kind = kinds.get(source_id, kind)
                    conversation_id = "wechat:" + _identifier(source_id, key)
                    record_id = str(record.get("extraction_record_id") or "")
                    if not record_id:
                        raise ValueError(
                            "Messages need a durable extraction record ID."
                        )
                    text = record.get("text")
                    if not isinstance(text, str):
                        raise ValueError("Message text must remain a string.")
                    count += 1
                    sort_time, timestamp, timestamp_label, basis = _time_fields(record)
                    conversation = conversations.setdefault(
                        conversation_id,
                        {
                            "id": conversation_id,
                            "peer": peer,
                            "title": title,
                            "kind": kind,
                            "count": 0,
                            "first": sort_time,
                            "last": sort_time,
                            "first_time": timestamp,
                            "last_time": timestamp,
                            "preview": text[:300],
                        },
                    )
                    conversation["count"] += 1
                    if sort_time and (
                        not conversation["first"] or sort_time < conversation["first"]
                    ):
                        conversation.update(first=sort_time, first_time=timestamp)
                    if sort_time >= conversation["last"]:
                        conversation.update(
                            last=sort_time, last_time=timestamp, preview=text[:300]
                        )
                    sender = str(record.get("sender_display_name") or "").strip()
                    if not sender or sender.casefold() in {"none", "null"}:
                        sender = str(record.get("sender_id") or "Unknown sender")
                    direction = record.get("direction")
                    direction = (
                        direction if direction in {"sent", "received"} else "unknown"
                    )
                    provenance = {
                        "record_id": record_id,
                        "locator": record.get("source_locator", {}),
                        "server_id": record.get("server_id"),
                        "unix_timestamp": record.get("unix_timestamp"),
                        "source_time": record.get("sent_at_raw"),
                    }
                    batch.append(
                        (
                            _identifier(source_id, record_id),
                            conversation_id,
                            count,
                            record.get("sender_id"),
                            sender,
                            direction,
                            text,
                            sort_time,
                            timestamp,
                            timestamp_label,
                            basis,
                            json.dumps(provenance, ensure_ascii=False),
                            json.dumps(record.get("flags", []), ensure_ascii=False),
                            "\n".join((text, sender, title, peer)).casefold(),
                        )
                    )
                    if len(batch) >= 1000:
                        connection.executemany(
                            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            batch,
                        )
                        batch.clear()
            if batch:
                connection.executemany(
                    "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch
                )
            if (
                byte_count != int(expected["uncompressed_bytes"])
                or decoded_hash.hexdigest() != expected["uncompressed_sha256"]
            ):
                raise ValueError("Decoded archive integrity check failed.")
            if count != int(source["text_rows"]):
                raise ValueError(
                    "Imported message count differs from the source catalog."
                )
            connection.executemany(
                "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        c["id"],
                        source_id,
                        account_key,
                        c["peer"],
                        c["title"],
                        c["kind"],
                        c["count"],
                        c["first"],
                        c["last"],
                        c["first_time"],
                        c["last_time"],
                        c["preview"],
                    )
                    for c in conversations.values()
                ],
            )
            imported.append(
                {
                    "source_id": source_id,
                    "account_id": owner or None,
                    "messages": count,
                    "conversations": len(conversations),
                }
            )
            payloads.extend((str(metadata_path), str(payload)))
        total = sum(item["messages"] for item in imported)
        if (
            total + sum(item["declared_text_rows"] for item in excluded)
            != catalog_total
        ):
            raise ValueError("The archive snapshot is incomplete.")
        report = {
            "schema_version": 1,
            "sources": imported,
            "messages": total,
            "excluded_sources": excluded,
            "unique_original_message_count": None,
        }
        connection.execute(
            "INSERT INTO archive_info VALUES('manifest',?)", (json.dumps(report),)
        )
        connection.commit()
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("Imported database failed its integrity check.")
        connection.close()
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        return {
            **report,
            "database_path": str(target),
            "database_bytes": target.stat().st_size,
            "read_files": [
                str(root / "catalog.json"),
                str(root / "compression_manifest.json"),
                *payloads,
            ],
        }
    finally:
        connection.close()
        temporary.unlink(missing_ok=True)


class WechatArchiveRepository:
    """Read paginated messaging data without reading or downloading original archives."""

    def __init__(self, local_store_root: Path | str):
        self.path = wechat_archive_path(local_store_root)

    @staticmethod
    def _source_snapshots(connection, messages, partition_id):
        """Read referenced snapshot metadata once, within the current source partition."""
        identifiers = set()
        for message in messages:
            provenance = json.loads(message["provenance"])
            if not isinstance(provenance, dict):
                continue
            identifier = provenance.get("source_snapshot")
            if type(identifier) is int and 0 < identifier <= 9223372036854775807:
                identifiers.add(identifier)
        if (
            not identifiers
            or connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_snapshots'"
            ).fetchone()
            is None
        ):
            return {}
        snapshots = {}
        identifiers = sorted(identifiers)
        for offset in range(0, len(identifiers), 500):
            batch = identifiers[offset : offset + 500]
            placeholders = ",".join("?" for _ in batch)
            for row in connection.execute(
                "SELECT id,snapshot_id,source_id,metadata FROM source_snapshots "
                f"WHERE source_id=? AND id IN ({placeholders})",
                [partition_id, *batch],
            ):
                try:
                    metadata = json.loads(row["metadata"])
                except (TypeError, ValueError):
                    continue
                if (
                    not isinstance(metadata, dict)
                    or metadata.get("source_id", partition_id) != partition_id
                ):
                    continue
                snapshots[row["id"]] = {
                    "snapshot_id": row["snapshot_id"],
                    "source_id": row["source_id"],
                    "metadata": metadata,
                }
        return snapshots

    def query(
        self,
        *,
        account="",
        kind="all",
        query="",
        session="",
        sort="newest",
        page=1,
        page_size=100,
    ):
        kind = kind if kind in WECHAT_KINDS else "all"
        empty = {
            "accounts": [],
            "conversations": [],
            "messages": [],
            "source_snapshots": {},
            "current": None,
            "count": 0,
            "message_count": 0,
            "conversation_count": 0,
            "page": 1,
            "pages": 1,
            "page_size": page_size,
            "account": account,
            "kind": kind,
        }
        if not self.path.is_file():
            return empty
        connection = sqlite3.connect(
            self.path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
        )
        connection.row_factory = sqlite3.Row
        try:
            accounts = [
                dict(row)
                for row in connection.execute(
                    "SELECT account_key AS key,account_label AS name,identity_status FROM partitions ORDER BY account_label COLLATE NOCASE,id"
                )
            ]
            where = []
            params = []
            if account:
                where.append("c.account_key=?")
                params.append(account)
            if kind != "all":
                where.append("c.kind=?")
                params.append(kind)
            join = " FROM conversations c JOIN partitions p ON p.id=c.partition_id "
            filters = " WHERE " + " AND ".join(where) if where else " WHERE 1=1"
            current = None
            terms = str(query or "").casefold().split()[:12]
            if session:
                row = connection.execute(
                    "SELECT c.*,p.account_label,p.account_id,p.identity_status,p.source_label,p.source_file"
                    + join
                    + filters
                    + " AND c.id=?",
                    [*params, session],
                ).fetchone()
                if row is None:
                    return {**empty, "accounts": accounts}
                current = dict(row)
                message_where = " WHERE m.conversation_id=?"
                message_params = [session]
                for term in terms:
                    message_where += " AND instr(m.search_text,?)>0"
                    message_params.append(term)
                total = connection.execute(
                    "SELECT COUNT(*) FROM messages m" + message_where, message_params
                ).fetchone()[0]
                pages = max(1, math.ceil(total / page_size))
                safe_page = min(pages, max(1, int(page)))
                messages = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT m.* FROM messages m"
                        + message_where
                        + " ORDER BY (m.time_sort=0),m.time_sort,m.ordinal LIMIT ? OFFSET ?",
                        [*message_params, page_size, (safe_page - 1) * page_size],
                    )
                ]
                return {
                    **empty,
                    "accounts": accounts,
                    "messages": messages,
                    "source_snapshots": self._source_snapshots(
                        connection, messages, current["partition_id"]
                    ),
                    "current": current,
                    "count": total,
                    "message_count": current["message_count"],
                    "conversation_count": 1,
                    "page": safe_page,
                    "pages": pages,
                }
            for term in terms:
                filters += " AND (instr(lower(c.title),?)>0 OR EXISTS(SELECT 1 FROM messages m WHERE m.conversation_id=c.id AND instr(m.search_text,?)>0))"
                params.extend((term, term))
            total = connection.execute(
                "SELECT COUNT(*)" + join + filters, params
            ).fetchone()[0]
            pages = max(1, math.ceil(total / page_size))
            safe_page = min(pages, max(1, int(page)))
            ordering = (
                "c.title COLLATE NOCASE,c.id"
                if sort == "name"
                else "(c.last_sort=0),c.last_sort "
                + ("ASC" if sort == "oldest" else "DESC")
                + ",c.id"
            )
            conversations = [
                dict(row)
                for row in connection.execute(
                    "SELECT c.*,p.account_label,p.account_id,p.identity_status,p.source_label,p.source_file"
                    + join
                    + filters
                    + " ORDER BY "
                    + ordering
                    + " LIMIT ? OFFSET ?",
                    [*params, page_size, (safe_page - 1) * page_size],
                )
            ]
            message_count = connection.execute(
                "SELECT COALESCE(SUM(c.message_count),0)" + join + filters, params
            ).fetchone()[0]
            return {
                **empty,
                "accounts": accounts,
                "conversations": conversations,
                "count": total,
                "message_count": message_count,
                "conversation_count": total,
                "page": safe_page,
                "pages": pages,
            }
        finally:
            connection.close()
