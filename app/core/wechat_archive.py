"""Source-separated, text-only WeChat archive import and indexed local queries."""

# Code version: v1.8.0-codex.0

from __future__ import annotations

from contextlib import closing
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
from threading import RLock
from typing import Any, Protocol


WECHAT_SOURCE = "wechat"
WECHAT_KINDS = ("all", "direct", "group", "unclassified")
_DISPLAY_ZONE = timezone(timedelta(hours=8))
_RESOURCE_CACHE_LOCK = RLock()
_RESOURCE_CACHE = {}


class WechatTimelineProjectionError(ValueError):
    """Refuse a strict timeline whose retained metadata cannot be projected."""

    def __init__(self, status: str) -> None:
        super().__init__("The scoped WeChat timeline is incomplete.")
        self.status = status


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


def wechat_history_partition_fingerprint(connection: sqlite3.Connection) -> str:
    """Identify the exact source-account partitions shared by a resource index."""
    rows = [
        tuple(row)
        for row in connection.execute(
            "SELECT id,account_key,account_id FROM partitions ORDER BY id"
        )
    ]
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _file_identity(path: Path) -> dict[str, int]:
    state = path.stat()
    return {
        "dev": state.st_dev,
        "inode": state.st_ino,
        "size": state.st_size,
        "mtime_ns": state.st_mtime_ns,
    }


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
                hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest() != metadata.get("account_hash")
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
    def _validate_resource_conversations(connection, *, conversation_id=None):
        """Check source boundaries and cache corrected statistics, never main content."""
        columns = (
            "id",
            "partition_id",
            "account_key",
            "peer_id",
            "title",
            "kind",
            "message_count",
            "first_sort",
            "last_sort",
            "first_time",
            "last_time",
            "latest_text",
        )
        native = {}
        for row in connection.execute(
            "SELECT id,account_key,account_id FROM partitions"
        ):
            owner = row["account_id"]
            if (
                isinstance(owner, str)
                and owner
                and row["id"]
                == "ios-"
                + hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest()
                and row["account_key"] == "account:" + owner
            ):
                native[row["id"]] = row["account_key"]
        scope = " WHERE id=?" if conversation_id is not None else ""
        parameters = (conversation_id,) if conversation_id is not None else ()
        original = {
            row["id"]: dict(row)
            for row in connection.execute("SELECT * FROM main.conversations" + scope, parameters)
        }
        resources = {
            row["id"]: dict(row)
            for row in connection.execute(
                "SELECT * FROM resource_index.conversations" + scope, parameters
            )
        }
        counts = dict(
            connection.execute(
                "SELECT conversation_id,COUNT(*) FROM resource_index.messages"
                + (" WHERE conversation_id=?" if conversation_id is not None else "")
                + " GROUP BY conversation_id",
                parameters,
            )
        )
        if set(counts) - set(resources):
            raise ValueError("Resource messages need their own scoped conversation.")
        for identifier, row in resources.items():
            if (
                native.get(row["partition_id"]) != row["account_key"]
                or row["kind"] not in WECHAT_KINDS[1:]
                or type(row["message_count"]) is not int
                or row["message_count"] < 1
                or row["message_count"] != counts.get(identifier, 0)
            ):
                raise ValueError(
                    "Resource conversation statistics or account do not match."
                )
            if identifier in original:
                source = original[identifier]
                if any(
                    row[key] != source[key]
                    for key in ("partition_id", "account_key", "peer_id", "kind")
                ):
                    raise ValueError(
                        "Resource conversations cannot change an existing scope."
                    )
            elif row["peer_id"]:
                if not isinstance(
                    row["peer_id"], str
                ) or identifier != "wechat:" + _identifier(
                    row["partition_id"], row["peer_id"]
                ):
                    raise ValueError(
                        "Resource-only conversations need a native peer identity."
                    )
            else:
                tables = [
                    item[0]
                    for item in connection.execute(
                        "SELECT DISTINCT json_extract(provenance,'$.locator.table') "
                        "FROM resource_index.messages WHERE conversation_id=?",
                        (identifier,),
                    )
                ]
                match = (
                    re.fullmatch(r"Chat_([0-9a-f]{32})", tables[0])
                    if len(tables) == 1 and isinstance(tables[0], str)
                    else None
                )
                if (
                    row["peer_id"] != ""
                    or row["kind"] != "unclassified"
                    or match is None
                    or identifier
                    != "wechat:" + _identifier(row["partition_id"], match.group(1))
                ):
                    raise ValueError(
                        "Unidentified peers must retain one exact native table hash."
                    )
        if (
            connection.execute(
                "SELECT 1 FROM resource_index.messages m "
                "JOIN resource_index.conversations c ON c.id=m.conversation_id "
                "JOIN main.partitions p ON p.id=c.partition_id WHERE CASE "
                "WHEN json_valid(m.provenance) AND json_valid(m.quality_flags) THEN "
                "json_type(m.quality_flags) IS NOT 'array' OR "
                "json_type(m.provenance,'$.resource_reference') IS NOT 'object' OR "
                "json_extract(m.provenance,'$.resource_reference.account_id') IS NOT p.account_id OR "
                "json_extract(m.provenance,'$.resource_reference.peer_id') IS NOT c.peer_id "
                "ELSE 1 END"
                + (" AND m.conversation_id=?" if conversation_id is not None else "")
                + " LIMIT 1",
                parameters,
            ).fetchone()
            is not None
        ):
            raise ValueError(
                "Resource references must retain their exact account and peer scope."
            )

        # Sorted covering-ID probes avoid random primary-index reads in the common
        # disjoint case. Keep the original grouped correction when overlap exists.
        # A missing required ID index fails closed through the caller's SQL guard.
        has_duplicates = (
            connection.execute(
                "SELECT 1 FROM resource_index.messages r "
                "INDEXED BY "
                + (
                    "message_conversation_time"
                    if conversation_id is not None
                    else "sqlite_autoindex_messages_1"
                )
                + " CROSS JOIN main.messages m "
                "WHERE m.id=r.id"
                + (" AND r.conversation_id=?" if conversation_id is not None else "")
                + " LIMIT 1",
                parameters,
            ).fetchone()
            is not None
        )
        duplicates = (
            dict(
                connection.execute(
                    "SELECT r.conversation_id,COUNT(*) FROM resource_index.messages r "
                    "JOIN main.messages m ON m.id=r.id"
                    + (" WHERE r.conversation_id=?" if conversation_id is not None else "")
                    + " GROUP BY r.conversation_id",
                    parameters,
                )
            )
            if has_duplicates
            else {}
        )
        for identifier in duplicates:
            rows = connection.execute(
                "SELECT r.time_sort,r.timestamp_value,r.text FROM resource_index.messages r "
                "WHERE r.conversation_id=? AND NOT EXISTS("
                "SELECT 1 FROM main.messages m WHERE m.id=r.id) "
                "ORDER BY (r.time_sort=0),r.time_sort,r.ordinal,r.id",
                (identifier,),
            ).fetchall()
            if not rows:
                resources.pop(identifier)
                continue
            first = next((row for row in rows if row["time_sort"]), rows[0])
            last = max(rows, key=lambda row: row["time_sort"])
            resources[identifier].update(
                message_count=len(rows),
                first_sort=first["time_sort"],
                first_time=first["timestamp_value"],
                last_sort=last["time_sort"],
                last_time=last["timestamp_value"],
                latest_text=last["text"][:300],
            )
        return tuple(
            tuple(row[column] for column in columns) for row in resources.values()
        )

    def _timeline_conversation_id(self, connection, message_id):
        """Locate a candidate scope; strict timeline validation still precedes its use."""
        if not isinstance(message_id, str) or re.fullmatch(r"[0-9a-f]{64}", message_id) is None:
            raise ValueError("A timeline message ID must be a canonical archive identifier.")
        row = connection.execute(
            "SELECT conversation_id FROM main.messages WHERE id=?", (message_id,)
        ).fetchone()
        if row is not None:
            return row[0]
        from .wechat_macos_recovery import mac_recovery_conversation

        try:
            mac_conversation = mac_recovery_conversation(self.path, message_id)
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
            raise WechatTimelineProjectionError("invalid") from exc
        if mac_conversation is not None:
            return mac_conversation
        from .wechat_macos_recovery_bank import bank_message_conversation

        try:
            bank_conversation = bank_message_conversation(self.path, message_id)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            raise WechatTimelineProjectionError("invalid") from exc
        if bank_conversation is not None:
            return bank_conversation
        from .wechat_secondary_completions import completion_message_conversation

        try:
            completion_conversation = completion_message_conversation(self.path, message_id)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            raise WechatTimelineProjectionError("invalid") from exc
        if completion_conversation is not None:
            return completion_conversation
        from .wechat_native_resource_revalidation import (
            locate_revalidated_resource,
            select_resource_index,
        )

        fallback_conversation = locate_revalidated_resource(self.path, message_id)
        if fallback_conversation is not None:
            return fallback_conversation

        resource_path = select_resource_index(self.path)
        if resource_path.exists() or resource_path.is_symlink():
            if (
                resource_path.is_symlink()
                or not resource_path.is_file()
                or resource_path.resolve().parent != self.path.resolve().parent
            ):
                raise WechatTimelineProjectionError("invalid")
            if not getattr(resource_path.stat(), "st_flags", 0) & 0x40000000:
                identity = _file_identity(resource_path)
                with closing(
                    sqlite3.connect(
                        resource_path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
                    )
                ) as resource:
                    ordinary = resource.execute(
                        "SELECT type,sql FROM sqlite_master WHERE name='messages'"
                    ).fetchone()
                    if ordinary is None or ordinary[0] != "table" or "VIRTUAL" in str(ordinary[1]).upper():
                        raise WechatTimelineProjectionError("invalid")
                    row = resource.execute(
                        "SELECT conversation_id FROM messages WHERE id=?", (message_id,)
                    ).fetchone()
                if _file_identity(resource_path) != identity:
                    raise WechatTimelineProjectionError("stale")
                if row is not None:
                    return row[0]
        from .wechat_media_event_index import locate_media_event
        from .wechat_media_placeholders import media_placeholder_conversation

        try:
            return media_placeholder_conversation(
                self.path, message_id
            ) or locate_media_event(self.path, message_id)
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
            raise WechatTimelineProjectionError("invalid") from exc

    @staticmethod
    def _native_resource_scope(
        connection, conversation_id, *, conversation_table="main.conversations"
    ):
        """Keep native catalog notices within their authoritative source scope."""
        if conversation_id is None:
            return True
        if re.fullmatch(
            r"(?:(?:main|temp)\.)?[A-Za-z_][A-Za-z0-9_]*", conversation_table
        ) is None:
            raise ValueError("Resource notices need a validated conversation table.")
        partition = connection.execute(
            f"SELECT p.id,p.account_id,p.account_key FROM {conversation_table} c "
            "JOIN main.partitions p ON p.id=c.partition_id WHERE c.id=?",
            (conversation_id,),
        ).fetchone()
        if partition is None:
            # Unresolved scopes still require the native catalog proof.
            return True
        owner = partition["account_id"]
        return (
            isinstance(owner, str)
            and bool(owner)
            and partition["account_key"] == "account:" + owner
            and partition["id"]
            == "ios-" + hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest()
        )

    def _timeline_tables(
        self,
        connection,
        history_identity,
        *,
        conversation_id=None,
        status_conversation_id=None,
        strict=False,
        include_secondary=True,
    ):
        """Share source-bound metadata placeholders across browsing and export."""
        from .wechat_media_event_index import attach_media_event_index
        from .wechat_media_placeholders import attach_media_placeholders
        from .wechat_macos_recovery import attach_mac_recovery
        from .wechat_macos_recovery_bank import attach_mac_recovery_bank
        from .wechat_secondary_completions import attach_secondary_message_completions

        conversations, messages, status = self._resource_timeline_tables(
            connection, history_identity, conversation_id=conversation_id
        )
        native_status = status
        conversations, messages, placeholder_status = attach_media_placeholders(
            connection,
            self.path,
            conversations,
            messages,
            conversation_id=conversation_id,
        )
        if strict and placeholder_status not in {"ready", "absent"}:
            raise WechatTimelineProjectionError(placeholder_status)
        conversations, messages, event_status = attach_media_event_index(
            connection,
            self.path,
            conversations,
            messages,
            conversation_id=conversation_id,
        )
        if event_status not in {"ready", "absent"}:
            if strict:
                raise WechatTimelineProjectionError(event_status)
            status = event_status
        conversations, messages, mac_status = attach_mac_recovery(
            connection,
            self.path,
            conversations,
            messages,
            conversation_id=conversation_id,
        )
        if mac_status not in {"ready", "absent"}:
            if strict:
                raise WechatTimelineProjectionError(mac_status)
            status = mac_status
        conversations, messages, bank_status = attach_mac_recovery_bank(
            connection,
            self.path,
            conversations,
            messages,
            conversation_id=conversation_id,
        )
        if bank_status not in {"ready", "absent"}:
            if strict:
                raise WechatTimelineProjectionError(bank_status)
            status = bank_status
        completion_status = "absent"
        if include_secondary:
            conversations, messages, completion_status = attach_secondary_message_completions(
                connection,
                self.path,
                conversations,
                messages,
                conversation_id=conversation_id,
            )
        if completion_status not in {"ready", "absent"}:
            if strict:
                raise WechatTimelineProjectionError(completion_status)
            status = completion_status
        if status_conversation_id is not None:
            status = (
                native_status
                if self._native_resource_scope(
                    connection,
                    status_conversation_id,
                    conversation_table=conversations,
                )
                else "absent"
            )
            for source_status in (placeholder_status, event_status):
                if source_status not in {"ready", "absent"}:
                    status = source_status
                elif source_status == "ready" and status == "absent":
                    status = "ready"
            if mac_status not in {"ready", "absent"}:
                status = mac_status
            if bank_status not in {"ready", "absent"}:
                status = bank_status
            if completion_status not in {"ready", "absent"}:
                status = completion_status
            elif completion_status == "ready" and status == "absent":
                status = "ready"
        return conversations, messages, status

    def _resource_timeline_tables(self, connection, history_identity, *, conversation_id=None):
        """Attach an optional pinned index and expose one chronological query surface."""
        from .wechat_native_resource_revalidation import select_resource_index

        resource_path = select_resource_index(self.path, conversation_id=conversation_id)
        if not resource_path.exists():
            return "main.conversations", "main.messages", "absent"
        if (
            resource_path.is_symlink()
            or not resource_path.is_file()
            or resource_path.resolve().parent != self.path.resolve().parent
        ):
            return "main.conversations", "main.messages", "invalid"
        try:
            if getattr(resource_path.stat(), "st_flags", 0) & 0x40000000:
                # Text queries must not download an optional cloud-only resource index.
                return "main.conversations", "main.messages", "absent"
            resource_identity = _file_identity(resource_path)
            connection.execute(
                "ATTACH DATABASE ? AS resource_index",
                (resource_path.resolve().as_uri() + "?mode=ro&immutable=1",),
            )
            metadata = dict(
                connection.execute("SELECT key,value FROM resource_index.archive_info")
            )
            if metadata.get("schema_version") != "1":
                raise ValueError("Unsupported resource index schema.")
            expected_identity = json.loads(
                metadata.get("history_file_identity", "null")
            )
            if (
                not isinstance(expected_identity, dict)
                or set(expected_identity) != {"dev", "inode", "size", "mtime_ns"}
                or any(type(value) is not int for value in expected_identity.values())
            ):
                raise ValueError(
                    "Resource indices need a complete pinned history identity."
                )
            fingerprint = metadata.get("history_partitions_sha256", "")
            if re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
                raise ValueError(
                    "Resource indices need a source-partition fingerprint."
                )
            if (
                expected_identity != history_identity
                or _file_identity(self.path) != history_identity
            ):
                return "main.conversations", "main.messages", "stale"
            if fingerprint != wechat_history_partition_fingerprint(connection):
                return "main.conversations", "main.messages", "stale"
            for table in ("messages", "conversations"):
                expected = [
                    row[1]
                    for row in connection.execute(f"PRAGMA main.table_info({table})")
                ]
                actual = [
                    row[1]
                    for row in connection.execute(
                        f"PRAGMA resource_index.table_info({table})"
                    )
                ]
                ordinary = connection.execute(
                    "SELECT type,sql FROM resource_index.sqlite_master WHERE name=?",
                    (table,),
                ).fetchone()
                if (
                    expected != actual
                    or ordinary is None
                    or ordinary[0] != "table"
                    or "VIRTUAL" in str(ordinary[1]).upper()
                ):
                    raise ValueError(
                        "Resource table columns differ from the archive contract."
                    )
            key = (
                str(self.path.resolve()),
                tuple(history_identity.items()),
                str(resource_path.resolve()),
                tuple(resource_identity.items()),
                conversation_id,
            )
            with _RESOURCE_CACHE_LOCK:
                if key not in _RESOURCE_CACHE:
                    corrected = (
                        self._validate_resource_conversations(connection)
                        if conversation_id is None
                        else self._validate_resource_conversations(
                            connection, conversation_id=conversation_id
                        )
                    )
                    if len(_RESOURCE_CACHE) >= 4:
                        _RESOURCE_CACHE.pop(next(iter(_RESOURCE_CACHE)))
                    _RESOURCE_CACHE[key] = corrected
                corrected = _RESOURCE_CACHE[key]
            if (
                _file_identity(self.path) != history_identity
                or _file_identity(resource_path) != resource_identity
            ):
                return "main.conversations", "main.messages", "stale"
            connection.execute(
                "CREATE TEMP TABLE resource_conversations AS "
                "SELECT * FROM resource_index.conversations WHERE 0"
            )
            connection.executemany(
                "INSERT INTO resource_conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                corrected,
            )
            resource_scope = ""
            if conversation_id is not None:
                if not isinstance(conversation_id, str) or not conversation_id:
                    raise ValueError("A scoped resource timeline needs a conversation ID.")
                # A TEMP scope table keeps the view's identity value parameterized.
                connection.execute("CREATE TEMP TABLE resource_timeline_scope(id TEXT PRIMARY KEY)")
                connection.execute(
                    "INSERT INTO resource_timeline_scope VALUES(?)", (conversation_id,)
                )
                resource_scope = "r.conversation_id=(SELECT id FROM temp.resource_timeline_scope) AND "
            connection.execute(
                "CREATE TEMP VIEW timeline_messages AS SELECT * FROM main.messages UNION ALL "
                "SELECT r.* FROM resource_index.messages r WHERE "
                + resource_scope
                + "NOT EXISTS("
                "SELECT 1 FROM main.messages m WHERE m.id=r.id)"
            )
            resource_first = (
                "r.first_sort>0 AND (c.first_sort=0 OR r.first_sort<c.first_sort)"
            )
            resource_last = "r.last_sort>=c.last_sort"
            connection.execute(
                "CREATE TEMP VIEW timeline_conversations AS "
                "SELECT c.id,c.partition_id,c.account_key,c.peer_id,c.title,c.kind,"
                "c.message_count+COALESCE(r.message_count,0) AS message_count,"
                f"CASE WHEN {resource_first} THEN r.first_sort ELSE c.first_sort END AS first_sort,"
                f"CASE WHEN {resource_last} THEN r.last_sort ELSE c.last_sort END AS last_sort,"
                f"CASE WHEN {resource_first} THEN r.first_time ELSE c.first_time END AS first_time,"
                f"CASE WHEN {resource_last} THEN r.last_time ELSE c.last_time END AS last_time,"
                f"CASE WHEN {resource_last} THEN r.latest_text ELSE c.latest_text END AS latest_text "
                "FROM main.conversations c LEFT JOIN temp.resource_conversations r ON r.id=c.id "
                "UNION ALL SELECT r.* FROM temp.resource_conversations r WHERE NOT EXISTS("
                "SELECT 1 FROM main.conversations c WHERE c.id=r.id)"
            )
            return "timeline_conversations", "timeline_messages", "ready"
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return "main.conversations", "main.messages", "invalid"

    def resource_message(self, message_id):
        """Resolve one indexed reference by identity, never by a browser-provided path."""
        if (
            not isinstance(message_id, str)
            or re.fullmatch(r"[0-9a-f]{64}", message_id) is None
        ):
            return None
        if not self.path.is_file():
            return None
        history_identity = _file_identity(self.path)
        connection = sqlite3.connect(
            self.path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
        )
        connection.row_factory = sqlite3.Row
        try:
            conversation_id = self._timeline_conversation_id(connection, message_id)
            _conversations, _messages, status = self._timeline_tables(
                connection, history_identity, conversation_id=conversation_id
            )
            if status != "ready":
                return None
            row = connection.execute(
                "SELECT r.*,p.account_id,c.peer_id FROM resource_index.messages r "
                "JOIN resource_index.conversations c ON c.id=r.conversation_id "
                "JOIN main.partitions p ON p.id=c.partition_id WHERE r.id=? "
                "AND NOT EXISTS(SELECT 1 FROM main.messages m WHERE m.id=r.id)",
                (message_id,),
            ).fetchone()
            if row is None or _file_identity(self.path) != history_identity:
                return None
            return dict(row)
        finally:
            connection.close()

    @staticmethod
    def _resource_index_accounts(connection):
        """Read optional coverage metadata without making it an access decision."""
        try:
            row = connection.execute(
                "SELECT value FROM resource_index.archive_info WHERE key='report'"
            ).fetchone()
            report = json.loads(row[0]) if row is not None else None
            if not isinstance(report, dict) or not isinstance(
                report.get("accounts"), list
            ):
                return None
            native = set()
            for partition in connection.execute(
                "SELECT id,account_key,account_id FROM partitions"
            ):
                owner = partition["account_id"]
                if (
                    isinstance(owner, str)
                    and owner
                    and partition["account_key"] == "account:" + owner
                    and partition["id"]
                    == "ios-"
                    + hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest()
                ):
                    native.add(owner)
            accounts = []
            for item in report["accounts"]:
                if not isinstance(item, dict) or not isinstance(
                    item.get("account_id"), str
                ):
                    return None
                owner = item["account_id"]
                if owner not in native or owner in accounts:
                    return None
                accounts.append(owner)
            return tuple(sorted(accounts))
        except (sqlite3.Error, TypeError, ValueError):
            return None

    @staticmethod
    def _quoted_resource_originals(
        connection, messages, conversation_id, *, message_table=None
    ):
        """Read bounded server-ID candidates from the accepted message timeline.

        The caller supplies the same verified source view used for paging.
        Candidates remain separate from the page and require full reference
        proof in the presentation layer before any quote can use their metadata.
        """
        if len(messages) > 1000:
            return []
        servers = set()
        for message in messages:
            try:
                proof = json.loads(message["provenance"])
                reference = proof.get("quoted_reference", {})
                server = reference.get("source_ids", {}).get("svrid")
                if (
                    proof.get("message_type") == 49
                    and proof.get("text_extraction_kind") == "quoted_reply"
                    and isinstance(server, str)
                    and re.fullmatch(r"[1-9][0-9]{0,18}", server)
                    and int(server) <= 9223372036854775807
                ):
                    servers.add(server)
            except (ValueError, TypeError, AttributeError):
                continue
        if not servers:
            return []
        placeholders = ",".join("?" for _ in servers)
        if message_table is not None:
            if re.fullmatch(
                r"(?:(?:main|temp)\.)?[A-Za-z_][A-Za-z0-9_]*", message_table
            ) is None:
                raise ValueError("Quote originals need a validated timeline table.")
            # Paging and quote originals must use the same accepted row per ID.
            rows, byte_count = [], 0
            for candidate in connection.execute(
                f"SELECT * FROM {message_table} WHERE conversation_id=? "
                f"AND json_extract(provenance,'$.server_id') IN ({placeholders}) "
                "ORDER BY id LIMIT 1001",
                [conversation_id, *sorted(servers)],
            ):
                row = dict(candidate)
                byte_count += sum(
                    len(value.encode())
                    for value in row.values()
                    if isinstance(value, str)
                )
                if len(rows) >= 1000 or byte_count > 8 * 1024**2:
                    return []
                rows.append(row)
            from .wechat_macos_recovery import mark_mac_recovery_rows

            mark_mac_recovery_rows(connection, rows)
            return rows
        rows = []
        if connection.execute(
            "SELECT 1 FROM temp.sqlite_master WHERE type='table' AND name='macos_recovery_bank_validated'"
        ).fetchone() is not None:
            rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT m.* FROM macos_recovery_bank.messages m JOIN macos_recovery_bank.event_keys k "
                    "ON k.message_id=m.id WHERE m.conversation_id=? "
                    f"AND k.server_id IN ({placeholders}) ORDER BY m.id LIMIT 1001",
                    [conversation_id, *sorted(servers)],
                )
            ]
        if connection.execute(
            "SELECT 1 FROM temp.sqlite_master WHERE type='table' AND name='macos_recovery_validated'"
        ).fetchone() is not None:
            rows.extend([
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM temp.macos_recovery_messages WHERE conversation_id=? "
                    f"AND json_extract(provenance,'$.server_id') IN ({placeholders}) "
                    "ORDER BY id LIMIT 1001",
                    [conversation_id, *sorted(servers)],
                )
            ])
        byte_count = sum(
            len(value.encode())
            for row in rows
            for value in row.values()
            if isinstance(value, str)
        )
        if len(rows) > 1000 or byte_count > 8 * 1024**2:
            return []
        if rows:
            from .wechat_macos_recovery import mark_mac_recovery_rows

            mark_mac_recovery_rows(connection, rows)
        return rows

    @staticmethod
    def _source_snapshots(connection, messages, partition_id):
        """Read local checkpoints, proving any native source routed into this container."""
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
                f"WHERE id IN ({placeholders})",
                batch,
            ):
                try:
                    metadata = json.loads(row["metadata"])
                except (TypeError, ValueError):
                    continue
                if (
                    not isinstance(metadata, dict)
                    or metadata.get("source_id", row["source_id"]) != row["source_id"]
                ):
                    continue
                routed = {}
                if row["source_id"] != partition_id:
                    from .wechat_contact_analysis import _routed_native_source

                    native = connection.execute(
                        "SELECT * FROM partitions WHERE id=?", (row["source_id"],)
                    ).fetchone()
                    if native is None:
                        continue
                    for message in messages:
                        provenance = json.loads(message["provenance"])
                        if provenance.get("source_snapshot") != row["id"]:
                            continue
                        container = connection.execute(
                            "SELECT c.*,p.account_id,p.account_label,p.identity_status,"
                            "p.source_label,p.source_file,p.account_key AS container_account_key "
                            "FROM conversations c JOIN partitions p ON p.id=c.partition_id "
                            "WHERE c.id=? AND c.partition_id=?",
                            (message["conversation_id"], partition_id),
                        ).fetchone()
                        if container is None:
                            continue
                        context = {
                            **dict(container),
                            **dict(message),
                            "source_id": partition_id,
                        }
                        try:
                            _routed_native_source(context, provenance, row, native)
                        except (ValueError, TypeError, KeyError):
                            continue
                        routed[message["id"]] = {
                            key: context[key]
                            for key in (
                                "source_id",
                                "account_key",
                                "account_id",
                                "account_label",
                                "identity_status",
                                "source_label",
                                "source_file",
                                "peer_id",
                                "kind",
                            )
                        }
                        routed[message["id"]]["partition_id"] = context["source_id"]
                    if not routed:
                        continue
                snapshots[row["id"]] = {
                    "snapshot_id": row["snapshot_id"],
                    "source_id": row["source_id"],
                    "metadata": metadata,
                    **({"routed_messages": routed} if routed else {}),
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
        persist_event_cache=True,
    ):
        kind = kind if kind in WECHAT_KINDS else "all"
        empty = {
            "accounts": [],
            "conversations": [],
            "messages": [],
            "quote_resource_originals": [],
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
            "resource_index_status": "absent",
            "resource_index_accounts": None,
        }
        if not self.path.is_file():
            return empty
        history_identity = _file_identity(self.path)
        connection = sqlite3.connect(
            self.path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
        )
        connection.row_factory = sqlite3.Row
        try:
            conversation_table, message_table, resource_status = self._timeline_tables(
                connection,
                history_identity,
                conversation_id=(session or None)
                if not persist_event_cache or self.path.with_name("resource_revalidated.sqlite3").exists()
                else None,
                status_conversation_id=session or None,
            )
            empty["resource_index_status"] = resource_status
            if resource_status == "ready" and self._native_resource_scope(
                connection, session or None, conversation_table=conversation_table
            ):
                empty["resource_index_accounts"] = self._resource_index_accounts(
                    connection
                )
            accounts = [
                dict(row)
                for row in connection.execute(
                    "SELECT account_key AS key,account_label AS name,identity_status,"
                    "account_id,id AS partition_id FROM partitions "
                    "ORDER BY account_label COLLATE NOCASE,id"
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
            join = f" FROM {conversation_table} c JOIN partitions p ON p.id=c.partition_id "
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
                from .wechat_conversation_events import (
                    canonical_message_table,
                    conversation_events,
                )

                events = conversation_events(
                    self, connection, session, current, message_table, history_identity,
                    persist=persist_event_cache,
                )
                source_message_table = message_table
                message_table = canonical_message_table(
                    connection, message_table, events
                )
                current["message_count"] -= events["count_delta"]
                connection.execute(
                    "CREATE TEMP TABLE conversation_event_links("
                    "id TEXT PRIMARY KEY,preferred_id TEXT NOT NULL) WITHOUT ROWID"
                )
                connection.executemany(
                    "INSERT INTO temp.conversation_event_links VALUES(?,?)",
                    events["preferred_by_id"].items(),
                )
                message_where = " WHERE m.conversation_id=?"
                message_params = [session]
                for term in terms:
                    message_where += (
                        " AND (instr(m.search_text,?)>0 OR EXISTS("
                        "SELECT 1 FROM temp.conversation_event_links l JOIN "
                        + source_message_table
                        + " observed ON observed.id=l.id WHERE l.preferred_id=m.id "
                        "AND instr(observed.search_text,?)>0))"
                    )
                    message_params.extend((term, term))
                total = connection.execute(
                    f"SELECT COUNT(*) FROM {message_table} m" + message_where,
                    message_params,
                ).fetchone()[0]
                pages = max(1, math.ceil(total / page_size))
                safe_page = min(pages, max(1, int(page)))
                messages = [
                    dict(row)
                    for row in connection.execute(
                        f"SELECT m.* FROM {message_table} m"
                        + message_where
                        + " ORDER BY (m.time_sort=0),m.time_sort,m.ordinal,m.id LIMIT ? OFFSET ?",
                        [*message_params, page_size, (safe_page - 1) * page_size],
                    )
                ]
                from .wechat_macos_recovery import mark_mac_recovery_rows

                mark_mac_recovery_rows(connection, messages)
                quote_originals = self._quoted_resource_originals(
                    connection, messages, session, message_table=source_message_table
                )
                return {
                    **empty,
                    "accounts": accounts,
                    "messages": messages,
                    "quote_resource_originals": quote_originals,
                    "source_snapshots": self._source_snapshots(
                        connection, [*messages, *quote_originals], current["partition_id"]
                    ),
                    "current": current,
                    "event_projection": events,
                    "count": total,
                    "message_count": current["message_count"],
                    "conversation_count": 1,
                    "page": safe_page,
                    "pages": pages,
                }
            from .wechat_conversation_events import cache_only_summaries

            summaries = cache_only_summaries(self, history_identity)
            connection.execute(
                "CREATE TEMP TABLE conversation_event_counts("
                "session_id TEXT PRIMARY KEY,delta INTEGER NOT NULL) WITHOUT ROWID"
            )
            connection.executemany(
                "INSERT INTO temp.conversation_event_counts VALUES(?,?)",
                summaries.items(),
            )
            join += " LEFT JOIN temp.conversation_event_counts e ON e.session_id=c.id "
            for term in terms:
                filters += f" AND (instr(lower(c.title),?)>0 OR EXISTS(SELECT 1 FROM {message_table} m WHERE m.conversation_id=c.id AND instr(m.search_text,?)>0))"
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
            for conversation in conversations:
                conversation["message_count"] -= summaries.get(conversation["id"], 0)
            message_count = connection.execute(
                "SELECT COALESCE(SUM(c.message_count-COALESCE(e.delta,0)),0)"
                + join
                + filters,
                params,
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
