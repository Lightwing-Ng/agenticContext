"""Protect lossless SQLite histories, atomic publication, and incremental updates."""

# Code version: v2.0.1-codex.0

from __future__ import annotations

import sqlite3
import hashlib
import json
from pathlib import Path

import pyarrow as pa
import pytest

from app.core.llm_sqlite import (
    BUCKET_COUNT,
    STORE_VERSION,
    read_sqlite_rows,
    read_sqlite_schema,
    write_sqlite_rows,
)
from app.core.resource_persistence import read_resource_rows, write_resource_rows_atomic


SCHEMA = pa.schema(
    [
        ("conversation_id", pa.string()),
        ("message_key", pa.string()),
        ("content_text", pa.string()),
        ("source_line", pa.int64()),
        ("links", pa.list_(pa.string())),
        ("optional", pa.string()),
    ]
)


def _rows():
    return [
        {
            "conversation_id": key,
            "message_key": str(index),
            "content_text": "中文\x00 🧪" * 100,
            "source_line": index,
            "links": ["https://example.com", ""],
            "optional": None,
        }
        for index, key in enumerate(["a", "b", "a"])
    ]


def test_lossless_interleaved_rows_and_indexed_scope(tmp_path: Path):
    path = tmp_path / "data.sqlite3"
    rows = _rows()
    write_sqlite_rows(path, rows, SCHEMA)
    assert read_sqlite_rows(path) == rows
    assert read_sqlite_rows(path, "a") == [rows[0], rows[2]]
    assert read_sqlite_rows(path, "missing") == []
    assert read_sqlite_schema(path).equals(SCHEMA, check_metadata=True)
    with sqlite3.connect(path) as connection:
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT payload FROM row_groups WHERE group_key=?",
            ("a",),
        ).fetchall()
        assert "SEARCH" in plan[0][-1]
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_updates_do_not_rewrite_other_groups_and_remove_only_requested_rows(
    tmp_path: Path,
):
    path = tmp_path / "data.sqlite3"
    rows = _rows()
    write_sqlite_rows(path, rows, SCHEMA)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE updates (id TEXT); CREATE TRIGGER changed AFTER UPDATE ON row_groups BEGIN INSERT INTO updates VALUES (new.group_key); END;"
        )
    write_sqlite_rows(path, rows, SCHEMA)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM updates").fetchall() == []
    rows[0]["content_text"] = "changed"
    write_sqlite_rows(path, rows, SCHEMA)
    with sqlite3.connect(path) as connection:
        expected = connection.execute(
            "SELECT group_key FROM conversation_groups WHERE conversation_id='a'"
        ).fetchone()[0]
        assert connection.execute("SELECT * FROM updates").fetchall() == [(expected,)]
    write_sqlite_rows(path, [], SCHEMA)
    assert read_sqlite_rows(path) == []


def test_failed_transaction_keeps_existing_data(tmp_path: Path):
    path = tmp_path / "data.sqlite3"
    rows = _rows()
    write_sqlite_rows(path, rows, SCHEMA)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_update BEFORE UPDATE ON row_groups BEGIN SELECT RAISE(ABORT,'test failure'); END"
        )
    rows[0]["content_text"] = "changed"
    with pytest.raises(sqlite3.IntegrityError):
        write_sqlite_rows(path, rows, SCHEMA)
    assert read_sqlite_rows(path) == _rows()


def test_failed_initial_write_does_not_publish_empty_database(
    tmp_path: Path, monkeypatch
):
    from app.core import llm_sqlite

    path = tmp_path / "data.sqlite3"

    def fail(*args, **kwargs):
        raise RuntimeError("test failure")

    monkeypatch.setattr(llm_sqlite, "_write_store", fail)
    with pytest.raises(RuntimeError):
        write_sqlite_rows(path, _rows(), SCHEMA)
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_legacy_fallback_is_read_only_and_existing_sqlite_wins(tmp_path: Path):
    legacy = tmp_path / "data.parquet"
    canonical = legacy.with_suffix(".sqlite3")
    write_resource_rows_atomic(legacy, _rows(), SCHEMA)
    assert read_resource_rows(canonical) == _rows()
    assert not canonical.exists()
    write_resource_rows_atomic(canonical, [], SCHEMA)
    assert read_resource_rows(canonical) == []
    canonical.write_bytes(b"corrupt")
    assert read_resource_rows(canonical) is None


def test_llm_root_catalog_and_media_catalog_keep_distinct_sqlite_formats(tmp_path: Path):
    from app.core.cache_catalog import LocalTweetCacheIndex

    llm = tmp_path / "llm"
    llm.mkdir()
    index = LocalTweetCacheIndex.build(llm)
    assert index.catalog_path == llm / ".cache_catalog.sqlite3"
    index.dirty = True
    index.flush()
    assert index.catalog_path.read_bytes().startswith(b"SQLite format 3")
    media = tmp_path / "x"
    media.mkdir()
    assert (
        LocalTweetCacheIndex.build(media).catalog_path.name == ".cache_catalog.sqlite3"
    )


def _write_format_two(path: Path, rows: list[dict], schema: pa.Schema) -> None:
    """Construct the previous JSON payload format without invoking its writer."""
    codec = pa.Codec("zstd", compression_level=6)
    groups = {}
    mappings = {}
    for ordinal, row in enumerate(rows):
        identity = row["conversation_id"]
        bucket = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:2], "big") % BUCKET_COUNT
        key = f"bucket:{bucket}"
        mappings[identity] = key
        groups.setdefault(key, []).append([ordinal, [row[name] for name in schema.names]])
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE store_info(key TEXT PRIMARY KEY,value BLOB NOT NULL);"
            "CREATE TABLE row_groups(group_key TEXT PRIMARY KEY,first_ordinal INTEGER NOT NULL,"
            "row_count INTEGER NOT NULL,digest TEXT NOT NULL,raw_bytes INTEGER NOT NULL,payload BLOB NOT NULL,summary BLOB);"
            "CREATE TABLE conversation_groups(conversation_id TEXT PRIMARY KEY,group_key TEXT NOT NULL);"
            "PRAGMA user_version=2;"
        )
        connection.executemany("INSERT INTO store_info VALUES (?,?)", [
            ("format_version", "2"), ("columns", json.dumps(schema.names)),
            ("arrow_schema", schema.serialize().to_pybytes()),
        ])
        for key, values in groups.items():
            raw = json.dumps(values, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
            connection.execute("INSERT INTO row_groups VALUES (?,?,?,?,?,?,NULL)", (
                key, values[0][0], len(values), hashlib.sha256(raw).hexdigest(), len(raw), codec.compress(raw).to_pybytes(),
            ))
        connection.executemany("INSERT INTO conversation_groups VALUES (?,?)", mappings.items())


def test_format_two_remains_readable_and_upgrades_without_changing_rows(tmp_path: Path):
    path = tmp_path / "legacy.sqlite3"
    rows = _rows()
    _write_format_two(path, rows, SCHEMA)
    assert read_sqlite_rows(path) == rows
    assert read_sqlite_rows(path, "a") == [rows[0], rows[2]]
    original = path.read_bytes()
    assert read_sqlite_schema(path).equals(SCHEMA, check_metadata=True)
    assert path.read_bytes() == original
    write_sqlite_rows(path, rows, SCHEMA)
    assert read_sqlite_rows(path) == rows
    assert read_sqlite_rows(path, "a") == [rows[0], rows[2]]
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (STORE_VERSION,)
        assert connection.execute("SELECT value FROM store_info WHERE key='format_version'").fetchone() == (str(STORE_VERSION),)
        raw_bytes, payload = connection.execute("SELECT raw_bytes,payload FROM row_groups LIMIT 1").fetchone()
    raw = pa.Codec("zstd").decompress(payload, decompressed_size=raw_bytes)
    table = pa.ipc.open_stream(raw).read_all()
    assert table.column_names == [*SCHEMA.names, "__llm_original_ordinal"]
    assert table.schema.field("__llm_original_ordinal").type == pa.int64()


def test_failed_format_upgrade_preserves_previous_payload_and_version(tmp_path: Path):
    path = tmp_path / "legacy.sqlite3"
    rows = _rows()
    _write_format_two(path, rows, SCHEMA)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TRIGGER reject_upgrade BEFORE UPDATE ON row_groups BEGIN SELECT RAISE(ABORT,'upgrade failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="upgrade failure"):
        write_sqlite_rows(path, rows, SCHEMA)
    assert read_sqlite_rows(path) == rows
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
        assert connection.execute("SELECT value FROM store_info WHERE key='format_version'").fetchone() == ("2",)


def test_reserved_ordinal_column_is_rejected_before_publication(tmp_path: Path):
    path = tmp_path / "absent" / "data.sqlite3"
    schema = SCHEMA.append(pa.field("__llm_original_ordinal", pa.int64()))
    with pytest.raises(ValueError, match="reserved internal ordinal"):
        write_sqlite_rows(path, _rows(), schema)
    assert not path.parent.exists()


def test_no_op_snapshot_keeps_database_bytes_and_modification_time(tmp_path: Path):
    path = tmp_path / "data.sqlite3"
    rows = _rows()
    write_sqlite_rows(path, rows, SCHEMA)
    original = path.read_bytes()
    timestamp = path.stat().st_mtime_ns
    write_sqlite_rows(path, rows, SCHEMA)
    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == timestamp


def test_identity_mapping_uses_arrow_validated_strings(tmp_path: Path):
    path = tmp_path / "data.sqlite3"
    rows = _rows()
    rows[0]["conversation_id"] = b"a"
    write_sqlite_rows(path, rows, SCHEMA)
    expected = pa.Table.from_pylist(rows, schema=SCHEMA).to_pylist()
    assert read_sqlite_rows(path) == expected
    assert read_sqlite_rows(path, "a") == [expected[0], expected[2]]


def test_scoped_arrow_reads_preserve_python_whitespace_matching(tmp_path: Path):
    from app.core.llm_sqlite import read_sqlite_conversations

    path = tmp_path / "data.sqlite3"
    rows = _rows()
    rows[0]["conversation_id"] = "\u2003a\u2003"
    rows[1]["conversation_id"] = "\u00a0b\u00a0"
    write_sqlite_rows(path, rows, SCHEMA)
    assert read_sqlite_rows(path, "a") == [rows[0], rows[2]]
    assert read_sqlite_conversations(path, {"a", "b"}) == rows
    assert read_sqlite_conversations(path, set()) == []
