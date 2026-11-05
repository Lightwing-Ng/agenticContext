"""Transactional, indexed, compressed local history storage.

Code version: v2.0.0-codex.0

Conversations map to stable compressed hash buckets, sharing compression context
without requiring full-store reads. Typed Arrow IPC payloads avoid a full Python
serialization round trip on writes. Original ordinals preserve lossless reads;
updates replace changed buckets, and format 2 remains readable during upgrades.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

import pyarrow as pa

STORE_VERSION = 3
BUCKET_COUNT = 32
_ORDINAL_COLUMN = "__llm_original_ordinal"
_CODEC = pa.Codec("zstd", compression_level=6)


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    connection = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro" if readonly else str(path),
        uri=readonly,
        timeout=30,
    )
    connection.execute("PRAGMA busy_timeout=30000")
    if not readonly:
        connection.execute("PRAGMA synchronous=FULL")
    return connection


def read_sqlite_schema(path: Path) -> pa.Schema:
    """Return the exact persisted Arrow validation schema, including provenance."""
    with closing(_connect(path, readonly=True)) as connection:
        raw = connection.execute(
            "SELECT value FROM store_info WHERE key='arrow_schema'"
        ).fetchone()
        if raw is None:
            raise ValueError("SQLite history schema is missing.")
        return pa.ipc.read_schema(pa.BufferReader(raw[0]))


def read_sqlite_rows(
    path: Path,
    conversation_id: str | None = None,
) -> list[dict[str, Any]]:
    """Decode all rows or only the bucket containing one indexed conversation."""
    return _read_rows(path, None if conversation_id is None else {conversation_id})


def read_sqlite_conversations(
    path: Path,
    conversation_ids: Iterable[str],
) -> list[dict[str, Any]]:
    """Read displayed conversations while decoding each shared bucket only once."""
    return _read_rows(path, set(conversation_ids))


def _read_rows(path: Path, identities: set[str] | None) -> list[dict[str, Any]]:
    with closing(_connect(path, readonly=True)) as connection:
        connection.execute("BEGIN")
        columns = json.loads(
            connection.execute(
                "SELECT value FROM store_info WHERE key='columns'"
            ).fetchone()[0]
        )
        version_row = connection.execute(
            "SELECT value FROM store_info WHERE key='format_version'"
        ).fetchone()
        version = int(version_row[0]) if version_row is not None else 2
        if version not in {2, STORE_VERSION}:
            raise ValueError("Unsupported SQLite history payload format.")
        sql = "SELECT raw_bytes,payload FROM row_groups"
        parameters: tuple[str, ...] = ()
        if identities is not None:
            if not identities:
                return []
            parameters = tuple(identities)
            placeholders = ",".join("?" for _ in parameters)
            sql += f" WHERE group_key IN (SELECT group_key FROM conversation_groups WHERE conversation_id IN ({placeholders}))"
        ordered: list[tuple[int, dict[str, Any]]] = []
        for raw_bytes, payload in connection.execute(sql, parameters):
            raw = _CODEC.decompress(payload, decompressed_size=raw_bytes)
            if version == 2:
                ordered.extend(
                    (ordinal, dict(zip(columns, values, strict=True)))
                    for ordinal, values in json.loads(raw.to_pybytes())
                )
            else:
                with pa.ipc.open_stream(raw) as stream:
                    table = stream.read_all()
                if table.column_names != [*columns, _ORDINAL_COLUMN] or not pa.types.is_int64(
                    table.schema.field(_ORDINAL_COLUMN).type
                ):
                    raise ValueError("SQLite history payload columns are invalid.")
                if identities is not None:
                    # Match Python's exact whitespace semantics on the narrow ID
                    # column before allocating any unselected Python message body.
                    if "conversation_id" not in columns:
                        continue
                    mask = pa.array(
                        [
                            str(value or "").strip() in identities
                            for value in table.column("conversation_id").to_pylist()
                        ],
                        type=pa.bool_(),
                    )
                    table = table.filter(mask)
                for row in table.to_pylist():
                    ordered.append((row.pop(_ORDINAL_COLUMN), row))
    ordered.sort(key=lambda item: item[0])
    return [
        row
        for _, row in ordered
        if identities is None
        or str(row.get("conversation_id") or "").strip() in identities
    ]


def read_sqlite_summaries(path: Path) -> list[dict[str, Any]] | None:
    """Read compressed browser summaries without decoding transcript groups."""
    with closing(_connect(path, readonly=True)) as connection:
        if "summary" not in {
            row[1] for row in connection.execute("PRAGMA table_info(row_groups)")
        }:
            return None
        summaries = []
        for (payload,) in connection.execute(
            "SELECT summary FROM row_groups ORDER BY first_ordinal"
        ):
            if payload is None:
                return None
            size = int.from_bytes(payload[:8], "big")
            group_summaries = json.loads(
                _CODEC.decompress(payload[8:], decompressed_size=size).to_pybytes()
            )
            summaries.extend(
                summary for summary in group_summaries if summary is not None
            )
        return summaries


def write_sqlite_rows(
    path: Path,
    rows: Iterable[Mapping[str, Any]],
    schema: pa.Schema,
) -> None:
    """Commit a typed snapshot atomically while updating only changed groups."""
    if _ORDINAL_COLUMN in schema.names:
        raise ValueError("The history schema uses a reserved internal ordinal column.")
    path = Path(path)
    source = path.parent.name if path.name == "history.sqlite3" else None
    if path.exists():
        _write_store(path, rows, schema, source)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        _write_store(temporary, rows, schema, source)
        with closing(_connect(temporary, readonly=True)) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("SQLite history integrity verification failed.")
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_store(
    path: Path,
    rows: Iterable[Mapping[str, Any]],
    schema: pa.Schema,
    source: str | None,
) -> None:
    """Write one database inside a rollback-journal transaction."""
    if _ORDINAL_COLUMN in schema.names:
        raise ValueError("The history schema uses a reserved internal ordinal column.")
    materialized = [dict(row) for row in rows]
    table = pa.Table.from_pylist(materialized, schema=schema).append_column(
        _ORDINAL_COLUMN, pa.array(range(len(materialized)), type=pa.int64())
    )
    columns = schema.names
    groups: dict[str, list[int]] = defaultdict(list)
    conversation_groups: dict[str, str] = {}
    identities = (
        table.column("conversation_id").to_pylist()
        if "conversation_id" in columns
        else [None] * table.num_rows
    )
    del materialized
    for ordinal, value in enumerate(identities):
        identity = str(value or "").strip()
        if identity:
            key = conversation_groups.get(identity)
            if key is None:
                bucket = int.from_bytes(
                    hashlib.sha256(identity.encode("utf-8")).digest()[:2], "big"
                ) % BUCKET_COUNT
                key = f"bucket:{bucket}"
                conversation_groups[identity] = key
        else:
            key = f"batch:{ordinal // 256}"
        groups[key].append(ordinal)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(path)) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS store_info (key TEXT PRIMARY KEY,value BLOB NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS row_groups ("
                "group_key TEXT PRIMARY KEY,first_ordinal INTEGER NOT NULL,"
                "row_count INTEGER NOT NULL,digest TEXT NOT NULL,raw_bytes INTEGER NOT NULL,"
                "payload BLOB NOT NULL,summary BLOB)"
            )
            if "summary" not in {
                row[1] for row in connection.execute("PRAGMA table_info(row_groups)")
            }:
                connection.execute("ALTER TABLE row_groups ADD COLUMN summary BLOB")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS conversation_groups (conversation_id TEXT PRIMARY KEY,group_key TEXT NOT NULL)"
            )
            previous = dict(
                connection.execute("SELECT group_key,digest FROM row_groups")
            )
            old_columns = connection.execute(
                "SELECT value FROM store_info WHERE key='columns'"
            ).fetchone()
            old_version_row = connection.execute(
                "SELECT value FROM store_info WHERE key='format_version'"
            ).fetchone()
            old_version = int(old_version_row[0]) if old_version_row is not None else None
            if old_version not in {None, 2, STORE_VERSION}:
                raise ValueError("Unsupported SQLite history payload format.")
            same_columns = (
                old_columns is not None and json.loads(old_columns[0]) == columns
            )
            if not same_columns or old_version != STORE_VERSION:
                previous = {key: "" for key in previous}
            for key, ordinals in groups.items():
                bucket_table = table.take(pa.array(ordinals, type=pa.int64()))
                sink = pa.BufferOutputStream()
                with pa.ipc.new_stream(sink, bucket_table.schema) as stream:
                    stream.write_table(bucket_table)
                raw = sink.getvalue()
                digest = hashlib.sha256(raw).hexdigest()
                if previous.get(key) == digest:
                    continue
                payload = _CODEC.compress(raw).to_pybytes()
                if (
                    not _CODEC.decompress(payload, decompressed_size=len(raw)).equals(raw)
                ):
                    raise RuntimeError(
                        "SQLite history compression verification failed."
                    )
                summary_payload = None
                if source is not None:
                    from .llm_history_index import build_history_summary

                    conversations: dict[str, list[dict[str, Any]]] = defaultdict(list)
                    for row in bucket_table.select(columns).to_pylist():
                        identity = str(row.get("conversation_id") or "").strip()
                        if identity:
                            conversations[identity].append(row)
                    summaries = [
                        build_history_summary(conversation, source)
                        for conversation in conversations.values()
                    ]
                    encoded = json.dumps(
                        summaries, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8")
                    summary_payload = (
                        len(encoded).to_bytes(8, "big")
                        + _CODEC.compress(encoded).to_pybytes()
                    )
                connection.execute(
                    "INSERT INTO row_groups VALUES (?,?,?,?,?,?,?) ON CONFLICT(group_key) DO UPDATE SET "
                    "first_ordinal=excluded.first_ordinal,row_count=excluded.row_count,"
                    "digest=excluded.digest,raw_bytes=excluded.raw_bytes,payload=excluded.payload,summary=excluded.summary",
                    (
                        key,
                        ordinals[0],
                        len(ordinals),
                        digest,
                        len(raw),
                        payload,
                        summary_payload,
                    ),
                )
            connection.executemany(
                "DELETE FROM row_groups WHERE group_key=?",
                [(key,) for key in previous.keys() - groups.keys()],
            )
            old_mappings = dict(
                connection.execute(
                    "SELECT conversation_id,group_key FROM conversation_groups"
                )
            )
            connection.executemany(
                "INSERT INTO conversation_groups VALUES (?,?) ON CONFLICT(conversation_id) DO UPDATE SET group_key=excluded.group_key",
                [
                    (identity, key)
                    for identity, key in conversation_groups.items()
                    if old_mappings.get(identity) != key
                ],
            )
            connection.executemany(
                "DELETE FROM conversation_groups WHERE conversation_id=?",
                [(key,) for key in old_mappings.keys() - conversation_groups.keys()],
            )
            for key, value in (
                ("format_version", str(STORE_VERSION)),
                ("columns", json.dumps(columns)),
                ("arrow_schema", schema.serialize().to_pybytes()),
            ):
                connection.execute(
                    "INSERT INTO store_info VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE value<>excluded.value",
                    (key, value),
                )
            if connection.execute("PRAGMA user_version").fetchone()[0] != STORE_VERSION:
                connection.execute(f"PRAGMA user_version={STORE_VERSION}")


def sqlite_snapshot_bytes(path: Path) -> bytes:
    """Return a consistent database image for verified rollback receipts."""
    with closing(_connect(path, readonly=True)) as connection:
        return connection.serialize()
