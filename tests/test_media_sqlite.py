"""Verify native media queries, transactional deltas, and nonmutating fallback."""

# Code version: v1.0.0-codex.0

from __future__ import annotations

import hashlib
import math
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.core.llm_sqlite import read_sqlite_rows, write_sqlite_rows
from app.core.media_sqlite import (
    MediaSQLiteStore,
    media_store_for,
    read_media_sqlite_schema,
    read_media_legacy_schema,
    sqlite_storage_kind,
)
from app.core.resource_persistence import (
    MEDIA_SQLITE_SPECS,
    X_CACHE_CATALOG_FILENAME,
    read_resource_rows,
    write_resource_rows_atomic,
)


SCHEMA = pa.schema(
    [
        pa.field("identity", pa.string(), nullable=False, metadata={b"key": b"source"}),
        pa.field("status", pa.string(), nullable=False),
        pa.field("attempts", pa.int32(), nullable=False),
        pa.field("links", pa.list_(pa.string())),
        pa.field("stamp", pa.float64()),
        pa.field("enabled", pa.bool_()),
    ],
    metadata={b"source": b"synthetic media"},
)


def _row(key: str, **updates):
    return {
        "identity": key,
        "status": "pending",
        "attempts": 0,
        "links": ["https://example.com/中文", None, ""],
        "stamp": None,
        "enabled": False,
        **updates,
    }


def _store(path: Path, schema=SCHEMA):
    return MediaSQLiteStore(path, schema, ("identity",), ("status",))


def test_exact_types_metadata_null_lists_and_insertion_order(tmp_path):
    path = tmp_path / "media.sqlite3"
    store = _store(path)
    rows = [_row("b", links=None), _row("a", stamp=42.5, enabled=True)]
    store.replace_rows(rows)
    assert store.rows() == rows
    assert store.get("a") == rows[1]
    assert store.get("missing") is None
    assert store.find("status", "pending") == rows
    assert read_media_sqlite_schema(path).equals(SCHEMA, check_metadata=True)
    assert sqlite_storage_kind(path) == "media"
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM media_rows WHERE status IS ?",
            ("pending",),
        ).fetchall()
        assert any("SEARCH" in row[-1] and "media_index_0" in row[-1] for row in plan)
        assert connection.execute(
            "SELECT typeof(identity),typeof(attempts),typeof(stamp) FROM media_rows "
            "WHERE identity='a'"
        ).fetchone() == ("text", "integer", "real")
    with pytest.raises(ValueError, match="not indexed"):
        store.find("links", [])


def test_delta_changes_only_touched_rows_and_retains_order(tmp_path):
    path = tmp_path / "media.sqlite3"
    store = _store(path)
    store.replace_rows([_row("a"), _row("b"), _row("c")])
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE audit(identity TEXT);"
            "CREATE TRIGGER updates AFTER UPDATE ON media_rows "
            "BEGIN INSERT INTO audit VALUES(NEW.identity); END;"
        )
    store.apply_changes(
        rows=[_row("a"), _row("b", status="complete"), _row("d")],
        delete_keys=["c"],
    )
    assert store.rows() == [_row("a"), _row("b", status="complete"), _row("d")]
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM audit").fetchall() == [("b",)]
    store.delete_keys(["a", "missing"])
    assert [row["identity"] for row in store.rows()] == ["b", "d"]


def test_mixed_upsert_delete_rolls_back_as_one_transaction(tmp_path):
    path = tmp_path / "media.sqlite3"
    store = _store(path)
    original = [_row("a"), _row("b")]
    store.replace_rows(original)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER rejected BEFORE INSERT ON media_rows "
            "WHEN NEW.identity='reject' BEGIN SELECT RAISE(ABORT,'injected failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        store.apply_changes(rows=[_row("c"), _row("reject")], delete_keys=["a"])
    assert store.rows() == original
    store.apply_changes(rows=[_row("a", status="complete")], delete_keys=["a"])
    assert store.get("a")["status"] == "complete"


def test_missing_reads_and_legacy_reads_never_create_files(tmp_path):
    path = tmp_path / "media.sqlite3"
    store = _store(path)
    assert store.rows() == []
    assert store.get("a") is None
    assert store.find("status", "pending") == []
    assert not path.exists()
    legacy = path.with_suffix(".parquet")
    rows = [_row("a"), _row("b")]
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), legacy)
    before = legacy.read_bytes()
    assert store.rows() == rows
    assert store.get("b") == rows[1]
    assert store.find("status", "pending") == rows
    assert not path.exists()
    store.upsert_rows([_row("c")])
    assert store.rows() == rows + [_row("c")]
    assert legacy.read_bytes() == before


def test_old_legacy_schema_requires_explicit_normalization(tmp_path):
    path = tmp_path / "media.sqlite3"
    legacy = path.with_suffix(".parquet")
    pq.write_table(
        pa.Table.from_pylist([{"identity": "a", "links_json": "[]"}]), legacy
    )
    store = _store(path)
    assert store.rows() == [{"identity": "a", "links_json": "[]"}]
    with pytest.raises(ValueError, match="explicit normalization"):
        store.upsert_rows([_row("b")])
    assert not path.exists()
    original = legacy.read_bytes()
    store.replace_rows([_row("a", links=[])])
    assert store.rows() == [_row("a", links=[])]
    assert legacy.read_bytes() == original


def test_invalid_rows_and_duplicate_keys_preserve_existing_snapshot(tmp_path):
    path = tmp_path / "media.sqlite3"
    store = _store(path)
    store.replace_rows([_row("a")])
    for rows in (
        [_row("b"), _row("b")],
        [_row("a", identity=None)],
        [_row("a", status=None)],
        [_row("a", unexpected=True)],
    ):
        with pytest.raises(ValueError):
            store.replace_rows(rows)
        assert store.rows() == [_row("a")]
    with pytest.raises(ValueError, match="Duplicate"):
        store.upsert_rows([_row("b"), _row("b")])


def test_schema_version_metadata_and_corruption_fail_closed(tmp_path):
    path = tmp_path / "media.sqlite3"
    pq.write_table(
        pa.Table.from_pylist([_row("legacy")], schema=SCHEMA),
        path.with_suffix(".parquet"),
    )
    store = _store(path)
    store.replace_rows([_row("canonical")])
    with pytest.raises(ValueError, match="schema"):
        _store(path, SCHEMA.with_metadata({b"different": b"metadata"}))
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE media_info SET value='999' WHERE key='format_version'"
        )
    with pytest.raises(ValueError, match="Unsupported"):
        store.rows()
    path.write_bytes(b"not a SQLite database")
    with pytest.raises(sqlite3.DatabaseError):
        store.rows()
    with pytest.raises(sqlite3.DatabaseError):
        store.upsert_rows([_row("new")])
    assert read_resource_rows(path) is None
    assert path.read_bytes() == b"not a SQLite database"


def test_failed_initial_write_publishes_no_partial_database(tmp_path, monkeypatch):
    path = tmp_path / "nested" / "media.sqlite3"
    store = _store(path)
    initialize = store._initialize

    def fail(connection):
        initialize(connection)
        raise RuntimeError("injected initialization failure")

    monkeypatch.setattr(store, "_initialize", fail)
    with pytest.raises(RuntimeError, match="initialization failure"):
        store.upsert_rows([_row("a")])
    assert not path.exists()
    assert list(path.parent.iterdir()) == []


def test_first_writer_race_retains_both_keyed_updates(tmp_path, monkeypatch):
    path = tmp_path / "media.sqlite3"
    barrier = Barrier(2)
    stores = [_store(path), _store(path)]
    initialize = MediaSQLiteStore._initialize

    def synchronized(store, connection):
        initialize(store, connection)
        barrier.wait(timeout=10)

    monkeypatch.setattr(MediaSQLiteStore, "_initialize", synchronized)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(store.upsert_rows, [_row(key)])
            for store, key in zip(stores, ("a", "b"), strict=True)
        ]
        for future in futures:
            future.result(timeout=20)
    assert {row["identity"] for row in stores[0].rows()} == {"a", "b"}


def test_compare_and_swap_claims_once_across_connections(tmp_path):
    path = tmp_path / "media.sqlite3"
    store = _store(path)
    store.replace_rows([_row("a")])
    barrier = Barrier(2)

    def claim():
        worker = _store(path)
        barrier.wait(timeout=10)
        return worker.compare_and_swap(
            "a",
            {"status": "pending", "attempts": 0},
            {"status": "running", "attempts": 1},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: claim(), range(2)))
    assert sum(result is not None for result in results) == 1
    assert store.get("a") == _row("a", status="running", attempts=1)
    assert store.compare_and_swap("missing", {}, {"status": "running"}) is None
    with pytest.raises(ValueError, match="primary keys"):
        store.compare_and_swap("a", {}, {"identity": "b"})


def test_composite_primary_key_and_nullable_index(tmp_path):
    schema = pa.schema(
        [("first", pa.string()), ("second", pa.int64()), ("tag", pa.string())]
    )
    store = MediaSQLiteStore(
        tmp_path / "composite.sqlite3", schema, ("first", "second"), ("tag",)
    )
    rows = [{"first": "a", "second": number, "tag": None} for number in (1, 2)]
    store.replace_rows(rows)
    assert store.get(("a", 2)) == rows[1]
    assert store.find("second", 2) == [rows[1]]
    assert store.find("tag", None) == rows
    store.delete_keys([("a", 1)])
    assert store.rows() == [rows[1]]


def test_float_nan_is_distinct_from_null(tmp_path):
    store = _store(tmp_path / "media.sqlite3")
    store.replace_rows(
        [
            _row("nan", stamp=float("nan")),
            _row("null"),
            _row("infinity", stamp=float("inf")),
        ]
    )
    assert math.isnan(store.get("nan")["stamp"])
    assert store.get("null")["stamp"] is None
    assert store.get("infinity")["stamp"] == float("inf")


@pytest.mark.parametrize("filename", tuple(MEDIA_SQLITE_SPECS))
def test_canonical_factory_and_generic_dispatch(tmp_path, filename):
    schema, keys, _ = MEDIA_SQLITE_SPECS[filename]
    row = {}
    for field in schema:
        if pa.types.is_string(field.type):
            row[field.name] = "value"
        elif pa.types.is_list(field.type):
            row[field.name] = []
        elif pa.types.is_floating(field.type):
            row[field.name] = None
        else:
            row[field.name] = 1
    path = tmp_path / filename
    write_resource_rows_atomic(path, [row], schema)
    store = media_store_for(path)
    assert store.get(row[keys[0]]) == row
    assert read_resource_rows(path) == [row]
    assert sqlite_storage_kind(path) == "media"


def test_llm_cache_catalog_same_basename_keeps_original_format(tmp_path):
    path = tmp_path / X_CACHE_CATALOG_FILENAME
    schema = pa.schema([("conversation_id", pa.string()), ("value", pa.string())])
    rows = [{"conversation_id": "a", "value": "history"}]
    write_sqlite_rows(path, rows, schema)
    assert sqlite_storage_kind(path) == "llm"
    assert read_resource_rows(path) == rows
    write_resource_rows_atomic(
        path, rows + [{"conversation_id": "b", "value": "new"}], schema
    )
    assert read_sqlite_rows(path) == rows + [{"conversation_id": "b", "value": "new"}]
    assert sqlite_storage_kind(path) == "llm"


def test_unknown_existing_sqlite_is_not_replaced_by_dispatch(tmp_path):
    path = tmp_path / X_CACHE_CATALOG_FILENAME
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert read_resource_rows(path) is None
    with pytest.raises(ValueError, match="Unrecognized"):
        write_resource_rows_atomic(path, [], MEDIA_SQLITE_SPECS[path.name][0])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_factory_preserves_original_parquet_provenance_through_runtime_updates(
    tmp_path,
):
    path = tmp_path / X_CACHE_CATALOG_FILENAME
    canonical, _, _ = MEDIA_SQLITE_SPECS[path.name]
    schema = canonical.with_metadata({b"provenance": b"original source"})
    row = {
        field.name: (
            []
            if pa.types.is_list(field.type)
            else None
            if field.nullable
            else ""
            if pa.types.is_string(field.type)
            else 1
        )
        for field in schema
    }
    row["relative_tweet_dir"] = "post"
    legacy = path.with_suffix(".parquet")
    pq.write_table(pa.Table.from_pylist([row], schema=schema), legacy)
    assert read_media_legacy_schema(legacy).equals(schema, check_metadata=True)
    store = media_store_for(path)
    assert store.schema.equals(schema, check_metadata=True)
    assert store.rows() == [row]
    assert not path.exists()
    store.upsert_rows([{**row, "title": "updated"}])
    reloaded = media_store_for(path)
    assert reloaded.schema.equals(schema, check_metadata=True)
    assert reloaded.get("post")["title"] == "updated"
    assert read_resource_rows(path) == [{**row, "title": "updated"}]
    with pytest.raises(ValueError, match="schema"):
        media_store_for(path, canonical)


def test_absent_llm_cache_catalog_keeps_compressed_history_format(tmp_path):
    path = tmp_path / "llm" / X_CACHE_CATALOG_FILENAME
    schema = pa.schema([("conversation_id", pa.string()), ("value", pa.string())])
    rows = [{"conversation_id": "a", "value": "history"}]
    write_resource_rows_atomic(path, rows, schema)
    assert sqlite_storage_kind(path) == "llm"
    assert read_resource_rows(path) == rows
