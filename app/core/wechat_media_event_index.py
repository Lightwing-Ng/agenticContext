"""Read a published global metadata timeline without opening media payloads.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import stat
from threading import Lock

from .wechat_archive import _file_identity, _identifier
from .wechat_resource_index import _SCHEMA as _TIMELINE_SCHEMA


VERSION = "v1.0.0-codex.0"
MEDIA_EVENT_SCHEMA = (
    _TIMELINE_SCHEMA
    + """
CREATE TABLE sources (
 id INTEGER PRIMARY KEY, snapshot_id TEXT NOT NULL, source_id TEXT NOT NULL,
 archive_sha256 TEXT NOT NULL, metadata_sha256 TEXT NOT NULL
);
CREATE TABLE conversation_proofs (
 conversation_id TEXT PRIMARY KEY, source_snapshot_id INTEGER NOT NULL,
 member_path TEXT NOT NULL, member_sha256 TEXT NOT NULL, audit_sha256 TEXT NOT NULL
) WITHOUT ROWID;
"""
)
SCHEMA = MEDIA_EVENT_SCHEMA
MAX_SOURCES = 4096
MAX_CONVERSATIONS = 100_000
MAX_SOURCE_METADATA_BYTES = 16 * 1024**2
_HEX = re.compile(r"[0-9a-f]{64}")
_TABLE = re.compile(r"(?:(?:main|temp)\.)?[A-Za-z_][A-Za-z0-9_]*")
_COLUMNS = (
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
_VALIDATED = {}
_LOCK = Lock()


class _StaleIndex(ValueError):
    """Reject a publication whose pinned primary archive changed."""


def _row(connection, sql, parameters=()):
    cursor = connection.execute(sql, parameters)
    value = cursor.fetchone()
    return (
        dict(zip((column[0] for column in cursor.description), value, strict=True))
        if value is not None
        else None
    )


def _rows(connection, sql):
    cursor = connection.execute(sql)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, value, strict=True)) for value in cursor]


def _digest(value):
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def _local_index(history_path):
    path = Path(history_path).with_name("media_events.sqlite3")
    try:
        history_state = Path(history_path).lstat()
        state = path.lstat()
    except FileNotFoundError:
        return path, "absent"
    if (
        stat.S_ISLNK(state.st_mode)
        or not stat.S_ISREG(state.st_mode)
        or stat.S_ISLNK(history_state.st_mode)
        or not stat.S_ISREG(history_state.st_mode)
    ):
        return path, "invalid"
    if any(
        getattr(value, "st_flags", 0) & getattr(stat, "UF_DATALESS", 0x40000000)
        for value in (state, history_state)
    ):
        return path, "absent"
    if path.resolve().parent != Path(history_path).resolve().parent:
        return path, "invalid"
    if any(
        Path(str(database) + suffix).exists()
        for database in (path, Path(history_path))
        for suffix in ("-wal", "-shm", "-journal")
    ):
        return path, "stale"
    return path, "ready"


def _validate_schema(connection):
    specifications = {
        "messages": [
            value[1] for value in connection.execute("PRAGMA main.table_info(messages)")
        ],
        "conversations": list(_COLUMNS),
        "sources": [
            "id",
            "snapshot_id",
            "source_id",
            "archive_sha256",
            "metadata_sha256",
        ],
        "conversation_proofs": [
            "conversation_id",
            "source_snapshot_id",
            "member_path",
            "member_sha256",
            "audit_sha256",
        ],
        "archive_info": ["key", "value"],
    }
    for table, expected in specifications.items():
        ordinary = _row(
            connection,
            "SELECT type,sql FROM media_event_index.sqlite_master WHERE name=?",
            (table,),
        )
        actual = [
            value[1]
            for value in connection.execute(
                f"PRAGMA media_event_index.table_info({table})"
            )
        ]
        if (
            ordinary is None
            or ordinary["type"] != "table"
            or "VIRTUAL" in str(ordinary["sql"]).upper()
            or actual != expected
        ):
            raise ValueError("The media event index does not use the published schema.")
    for index, columns in (
        ("sqlite_autoindex_messages_1", ["id"]),
        ("message_conversation_time", ["conversation_id", "time_sort", "ordinal"]),
    ):
        if (
            connection.execute(
                "SELECT 1 FROM media_event_index.sqlite_master "
                "WHERE type='index' AND tbl_name='messages' AND name=?",
                (index,),
            ).fetchone()
            is None
            or [
                value[2]
                for value in connection.execute(
                    f"PRAGMA media_event_index.index_info({index})"
                )
            ]
            != columns
        ):
            raise ValueError("The media event index lacks a required lookup index.")


def _source_proofs(connection):
    sources = _rows(connection, "SELECT * FROM media_event_index.sources LIMIT 4097")
    if len(sources) > MAX_SOURCES:
        raise ValueError("Media source evidence exceeds its metadata budget.")
    result, total_bytes = {}, 0
    for value in sources:
        if (
            type(value["id"]) is not int
            or value["id"] <= 0
            or not all(
                _digest(value[key]) for key in ("archive_sha256", "metadata_sha256")
            )
            or value["snapshot_id"] != value["archive_sha256"]
            or not isinstance(value["source_id"], str)
            or re.fullmatch(
                r"html-" + value["archive_sha256"][:24] + r"(?:-text-completion-v1)?",
                value["source_id"],
            )
            is None
        ):
            raise ValueError("A media source lacks an exact archive receipt.")
        snapshot = _row(
            connection,
            "SELECT * FROM main.source_snapshots WHERE id=? AND source_id=?",
            (value["id"], value["source_id"]),
        )
        partition = _row(
            connection,
            "SELECT * FROM main.partitions WHERE id=?",
            (value["source_id"],),
        )
        if (
            snapshot is None
            or partition is None
            or not isinstance(snapshot["metadata"], str)
        ):
            raise ValueError("A media source is no longer published.")
        encoded = snapshot["metadata"].encode("utf-8")
        total_bytes += len(encoded)
        if total_bytes > MAX_SOURCE_METADATA_BYTES:
            raise ValueError("Media source receipts exceed their metadata budget.")
        metadata = json.loads(encoded)
        owner = partition["account_id"]
        evidence = (
            metadata.get("owner_evidence", {}) if isinstance(metadata, dict) else {}
        )
        if (
            snapshot["snapshot_id"] != value["snapshot_id"]
            or hashlib.sha256(encoded).hexdigest() != value["metadata_sha256"]
            or not isinstance(metadata, dict)
            or metadata.get("source_id") != value["source_id"]
            or metadata.get("source_sha256") != value["archive_sha256"]
            or not isinstance(owner, str)
            or not owner
            or partition["account_key"] != "account:" + owner
            or partition["identity_status"]
            not in {"Corroborated export ID", "Verified archive ID"}
            or metadata.get("source_account_id") != owner
            or metadata.get("owner_verified") is not True
            or not isinstance(evidence, dict)
            or evidence.get("native_username") != owner
        ):
            raise ValueError("A media source crosses its original account or receipt.")
        members = metadata.get("selected_text_members")
        if not isinstance(members, list):
            raise ValueError("Media sources require exact published member receipts.")
        receipts = {}
        for member in members:
            if (
                not isinstance(member, dict)
                or not isinstance(member.get("member_path"), str)
                or not _digest(member.get("sha256"))
            ):
                raise ValueError("An original media member receipt is invalid.")
            if (
                member["member_path"] in receipts
                and receipts[member["member_path"]] != member["sha256"]
            ):
                raise ValueError("An original media member has conflicting receipts.")
            receipts[member["member_path"]] = member["sha256"]
        result[value["id"]] = (value, metadata, partition, receipts)
    return result


def _validated_conversations(connection, history_identity):
    _validate_schema(connection)
    info = dict(
        connection.execute("SELECT key,value FROM media_event_index.archive_info")
    )
    if (
        info.get("schema_version") != "1"
        or info.get("proof_version") != VERSION
        or info.get("published") != "1"
        or info.get("primary_disjoint") != "1"
    ):
        raise ValueError("The global media metadata has not passed publication checks.")
    pinned_identity = json.loads(info.get("history_file_identity", "null"))
    if (
        not isinstance(pinned_identity, dict)
        or set(pinned_identity) != set(history_identity)
        or any(type(value) is not int for value in pinned_identity.values())
    ):
        raise ValueError(
            "A media publication requires an exact primary identity receipt."
        )
    if pinned_identity != history_identity:
        raise _StaleIndex(
            "The media event publication refers to another primary archive."
        )
    sources = _source_proofs(connection)
    conversations = _rows(
        connection, "SELECT * FROM media_event_index.conversations LIMIT 100001"
    )
    if len(conversations) > MAX_CONVERSATIONS:
        raise ValueError("Media conversation evidence exceeds its metadata budget.")
    proofs = {
        value["conversation_id"]: value
        for value in _rows(
            connection,
            "SELECT * FROM media_event_index.conversation_proofs LIMIT 100001",
        )
    }
    if len(proofs) != len(conversations):
        raise ValueError("Every media conversation needs one exact source proof.")
    for conversation in conversations:
        proof = proofs.get(conversation["id"])
        source = sources.get(proof["source_snapshot_id"]) if proof else None
        original = _row(
            connection,
            "SELECT * FROM main.conversations WHERE id=?",
            (conversation["id"],),
        )
        if source is None or (
            original is not None
            and any(
                conversation[key] != original[key]
                for key in ("partition_id", "account_key", "peer_id", "kind")
            )
        ):
            raise ValueError(
                "Media events cannot create or change an account conversation."
            )
        record, metadata, partition, members = source
        if (
            conversation["partition_id"] != record["source_id"]
            or conversation["account_key"] != partition["account_key"]
            or type(conversation["message_count"]) is not int
            or conversation["message_count"] < 1
            or not _digest(proof["member_sha256"])
            or not _digest(proof["audit_sha256"])
            or members.get(proof["member_path"]) != proof["member_sha256"]
        ):
            raise ValueError(
                "A media conversation differs from its original source member."
            )
        catalog = metadata.get("source_conversation_catalog")
        if not isinstance(catalog, list):
            raise ValueError("An original media conversation catalog is required.")
        entries = [
            value
            for value in catalog
            if isinstance(value, dict)
            and value.get("member_path") == proof["member_path"]
        ]
        if len(entries) != 1:
            raise ValueError(
                "A media conversation must select one original main member."
            )
        entry = entries[0]
        if original is None and (
            conversation["id"]
            != "wechat:" + _identifier(record["source_id"], proof["member_path"])
            or entry.get("index_avatar_native_id") != conversation["peer_id"]
            or entry.get("conversation_kind") != conversation["kind"]
        ):
            raise ValueError(
                "A media-only conversation requires its exact original peer identity."
            )
        if any(
            key in entry and entry[key] != conversation[field]
            for key, field in (
                ("index_avatar_native_id", "peer_id"),
                ("conversation_kind", "kind"),
            )
        ):
            raise ValueError("A media source catalog has another original peer.")
        resolution = entry.get("reviewed_resolution")
        if resolution is not None and (
            not isinstance(resolution, dict)
            or "main_html_sha256" in resolution
            and resolution["main_html_sha256"] != proof["member_sha256"]
        ):
            raise ValueError("A media main member differs from its reviewed receipt.")
    return conversations


def _attach_verified(connection, history_path):
    path, status = _local_index(history_path)
    if status != "ready":
        return path, status, []
    identity = _file_identity(path)
    history_identity = _file_identity(Path(history_path))
    connection.execute(
        "ATTACH DATABASE ? AS media_event_index",
        (path.resolve().as_uri() + "?mode=ro&immutable=1",),
    )
    key = (
        str(path.resolve()),
        tuple(identity.items()),
        str(Path(history_path).resolve()),
        tuple(history_identity.items()),
    )
    with _LOCK:
        conversations = _VALIDATED.get(key)
    if conversations is None:
        conversations = _validated_conversations(connection, history_identity)
        with _LOCK:
            if len(_VALIDATED) >= 4:
                _VALIDATED.pop(next(iter(_VALIDATED)))
            _VALIDATED[key] = conversations
    if (
        _file_identity(path) != identity
        or _file_identity(Path(history_path)) != history_identity
    ):
        raise _StaleIndex("The media metadata changed during source verification.")
    return path, "ready", conversations


def attach_media_event_index(
    connection, history_path, conversation_table, message_table, conversation_id=None
):
    """Expose metadata events through the existing timeline with indexed source scope."""
    default = (conversation_table, message_table)
    try:
        if any(
            not isinstance(value, str) or _TABLE.fullmatch(value) is None
            for value in default
        ):
            raise ValueError("Unsupported input timeline tables.")
        _path, status, conversations = _attach_verified(connection, history_path)
        if status != "ready":
            return *default, status
        if conversation_id is not None and (
            not isinstance(conversation_id, str) or not conversation_id
        ):
            raise ValueError("A media event scope needs an exact conversation ID.")
        selected = [
            row
            for row in conversations
            if conversation_id is None or row["id"] == conversation_id
        ]
        if not selected:
            return *default, "absent"
        connection.execute(
            "CREATE TEMP TABLE media_event_selected_conversations(id TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        connection.executemany(
            "INSERT INTO media_event_selected_conversations VALUES(?)",
            [(value["id"],) for value in selected],
        )
        connection.execute(
            f"CREATE TEMP VIEW media_event_timeline_messages AS SELECT * FROM {message_table} UNION ALL SELECT e.* FROM media_event_index.messages e WHERE e.conversation_id IN (SELECT id FROM temp.media_event_selected_conversations) AND NOT EXISTS(SELECT 1 FROM {message_table} b WHERE b.id=e.id)"
        )
        duplicates = Counter()
        if (
            conversation_id is None
            and connection.execute(
                "SELECT 1 FROM temp.sqlite_master WHERE type='table' AND name='media_placeholder_messages'"
            ).fetchone()
        ):
            duplicates.update(
                dict(
                    connection.execute(
                        "SELECT e.conversation_id,COUNT(*) FROM temp.media_placeholder_messages p CROSS JOIN media_event_index.messages e ON e.id=p.id GROUP BY e.conversation_id"
                    )
                )
            )
        connection.execute(
            f"CREATE TEMP TABLE media_event_conversation_summaries AS SELECT * FROM {conversation_table} WHERE 0"
        )
        for media in selected:
            original = _row(
                connection,
                f"SELECT * FROM {conversation_table} WHERE id=?",
                (media["id"],),
            )
            overlap = duplicates[media["id"]]
            if conversation_id is not None:
                overlap = connection.execute(
                    f"SELECT COUNT(*) FROM media_event_index.messages e WHERE e.conversation_id=? AND EXISTS(SELECT 1 FROM {message_table} b WHERE b.id=e.id)",
                    (media["id"],),
                ).fetchone()[0]
            added = media["message_count"] - overlap
            if added < 0:
                raise ValueError("Media event overlap exceeds its published summary.")
            if not added:
                continue
            if original is None:
                if overlap:
                    raise ValueError(
                        "A media-only conversation has inconsistent input membership."
                    )
                connection.execute(
                    "INSERT INTO media_event_conversation_summaries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    tuple(media[key] for key in _COLUMNS),
                )
                continue
            first_media = media["first_sort"] > 0 and (
                original["first_sort"] == 0
                or media["first_sort"] < original["first_sort"]
            )
            last_media = media["last_sort"] >= original["last_sort"]
            row = {**original, "message_count": original["message_count"] + added}
            if first_media:
                row.update(
                    first_sort=media["first_sort"], first_time=media["first_time"]
                )
            if last_media:
                row.update(
                    last_sort=media["last_sort"],
                    last_time=media["last_time"],
                    latest_text=media["latest_text"],
                )
                if media["last_sort"] == original["last_sort"]:
                    last = _row(
                        connection,
                        "SELECT timestamp_value,text FROM media_event_timeline_messages "
                        "WHERE conversation_id=? AND time_sort=? "
                        "ORDER BY ordinal DESC,id DESC LIMIT 1",
                        (media["id"], media["last_sort"]),
                    )
                    row.update(
                        last_time=last["timestamp_value"],
                        latest_text=last["text"][:300],
                    )
            connection.execute(
                "INSERT INTO media_event_conversation_summaries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(row[key] for key in _COLUMNS),
            )
        connection.execute(
            f"CREATE TEMP VIEW media_event_timeline_conversations AS SELECT c.* FROM {conversation_table} c WHERE NOT EXISTS(SELECT 1 FROM temp.media_event_conversation_summaries s WHERE s.id=c.id) UNION ALL SELECT * FROM temp.media_event_conversation_summaries"
        )
        return (
            "media_event_timeline_conversations",
            "media_event_timeline_messages",
            "ready",
        )
    except _StaleIndex:
        return *default, "stale"
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
        return *default, "invalid"


def locate_media_event(history_path, message_id):
    """Locate a published event by primary key, never by scanning media or JSON rows."""
    if not _digest(message_id):
        return None
    try:
        path, status = _local_index(history_path)
        if status != "ready":
            return None
        with closing(
            sqlite3.connect(
                Path(history_path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True
            )
        ) as connection:
            _path, status, _conversations = _attach_verified(connection, history_path)
            if status != "ready":
                return None
            row = connection.execute(
                "SELECT conversation_id FROM media_event_index.messages WHERE id=?",
                (message_id,),
            ).fetchone()
            return row[0] if row else None
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
        return None
