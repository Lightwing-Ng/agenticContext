"""Verify migration safety, exact rollback, and excluded protected histories.

Code version: v1.2.1-codex.0
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.core.llm_sqlite import STORE_VERSION, read_sqlite_rows, write_sqlite_rows


_SCRIPT = Path(__file__).parents[1] / "scripts" / "migrate_llm_sqlite.py"
_SPEC = importlib.util.spec_from_file_location("migrate_llm_sqlite", _SCRIPT)
migrator = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(migrator)
_BENCHMARK = migrator.benchmark


@pytest.fixture
def history(tmp_path, monkeypatch):
    source = tmp_path / "store" / "llm" / "synthetic" / "history.parquet"
    source.parent.mkdir(parents=True)
    rows = [
        {"conversation_id": "first", "message_key": "a", "content_text": "First body", "source_line": 7, "nullable": None},
        {"conversation_id": "second", "message_key": "b", "content_text": "Second body", "source_line": 9, "nullable": "retained"},
        {"conversation_id": "first", "message_key": "a", "content_text": "First body", "source_line": 7, "nullable": None},
    ]
    schema = pa.schema([
        pa.field("conversation_id", pa.string()), pa.field("message_key", pa.string()),
        pa.field("content_text", pa.string()), pa.field("source_line", pa.int64()), pa.field("nullable", pa.string()),
    ], metadata={b"provenance": b"synthetic migration fixture"})
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), source)
    monkeypatch.setattr(migrator, "assert_no_open_handles", lambda _path: None)
    monkeypatch.setattr(migrator, "benchmark", lambda *_arguments: {"synthetic": True})
    return source, rows, schema


def test_preview_preserves_sources_and_ignores_numbered_or_import_files(history, tmp_path):
    source, _rows, _schema = history
    numbered = source.with_name("history 2.parquet")
    numbered.write_bytes(b"Separate protected source")
    report = migrator.migrate(source.parents[2], tmp_path / "preview.json")
    assert len(report["entries"]) == 1
    assert report["entries"][0]["status"] == "planned"
    assert source.exists() and numbered.read_bytes() == b"Separate protected source"
    assert not source.with_suffix(".sqlite3").exists()


def test_exact_rows_order_duplicates_schema_backup_and_retirement(history, tmp_path):
    source, rows, schema = history
    original_bytes = source.read_bytes()
    report = migrator.migrate(source.parents[2], tmp_path / "report.json", apply=True, retire_legacy=True)
    entry = report["entries"][0]
    assert entry["status"] == "migrated_legacy_retired"
    assert entry["store_format_version"] == STORE_VERSION
    assert not source.exists()
    target = source.with_suffix(".sqlite3")
    assert read_sqlite_rows(target) == rows
    assert migrator.read_sqlite_schema(target).equals(schema, check_metadata=True)
    assert gzip.decompress(Path(entry["rollback"]["path"]).read_bytes()) == original_bytes
    assert Path(entry["rollback"]["path"]).stat().st_mode & 0o777 == 0o600
    assert json.loads((tmp_path / "report.json").read_text())["totals"]["rows"] == 3


def test_publication_preserves_history_modification_time(history, tmp_path):
    source, _rows, _schema = history
    original_mtime_ns = 1_700_000_000_123_456_789
    os.utime(source, ns=(original_mtime_ns, original_mtime_ns))
    migrator.migrate(source.parents[2], tmp_path / "report.json", apply=True)
    assert source.with_suffix(".sqlite3").stat().st_mtime_ns == original_mtime_ns


def test_llm_cache_catalog_is_included_but_other_catalogs_are_excluded(history, tmp_path):
    source, _rows, _schema = history
    root = source.parents[2]
    catalog = root / "llm" / ".cache_catalog.parquet"
    pq.write_table(pa.table({"relative_path": ["synthetic/path"], "content_bytes": [123]}), catalog)
    unrelated = root / "x" / ".cache_catalog.parquet"
    unrelated.parent.mkdir()
    unrelated.write_bytes(b"Unrelated runtime catalog")
    report = migrator.migrate(root, tmp_path / "report.json", apply=True)
    assert len(report["entries"]) == 2
    assert read_sqlite_rows(catalog.with_suffix(".sqlite3")) == [{"relative_path": "synthetic/path", "content_bytes": 123}]
    assert unrelated.read_bytes() == b"Unrelated runtime catalog"


def test_conflicting_existing_target_is_not_overwritten(history, tmp_path):
    source, rows, schema = history
    target = source.with_suffix(".sqlite3")
    changed = [dict(row) for row in rows]
    changed[0]["content_text"] = "Independent target changes"
    write_sqlite_rows(target, changed, schema)
    with pytest.raises(ValueError, match="differs"):
        migrator.migrate(source.parents[2], tmp_path / "report.json", apply=True, retire_legacy=True)
    assert source.exists() and read_sqlite_rows(target) == changed


def test_matching_target_can_resume_without_clobbering(history, tmp_path):
    source, rows, schema = history
    target = source.with_suffix(".sqlite3")
    write_sqlite_rows(target, rows, schema)
    report = migrator.migrate(source.parents[2], tmp_path / "report.json", apply=True)
    assert report["entries"][0]["status"] == "existing_target_verified"
    assert source.exists()
    assert target.stat().st_mtime_ns == source.stat().st_mtime_ns


def test_changed_source_after_backup_blocks_retirement(history, tmp_path, monkeypatch):
    source, _rows, _schema = history
    original_backup = migrator.preserve_backup

    def mutate_after_backup(path, *arguments):
        result = original_backup(path, *arguments)
        path.write_bytes(path.read_bytes() + b"concurrent mutation")
        return result

    monkeypatch.setattr(migrator, "preserve_backup", mutate_after_backup)
    with pytest.raises(ValueError, match="changed after backup"):
        migrator.migrate(source.parents[2], tmp_path / "report.json", apply=True, retire_legacy=True)
    assert source.exists()


def test_active_source_handle_blocks_changes(history, tmp_path, monkeypatch):
    source, _rows, _schema = history

    def owned(_path):
        raise RuntimeError("An active process has an open handle")

    monkeypatch.setattr(migrator, "assert_no_open_handles", owned)
    with pytest.raises(RuntimeError, match="active process"):
        migrator.migrate(source.parents[2], tmp_path / "report.json", apply=True)
    assert not source.with_suffix(".sqlite3").exists()


def test_retirement_requires_explicit_apply(history, tmp_path):
    source, _rows, _schema = history
    with pytest.raises(ValueError, match="requires --apply"):
        migrator.migrate(source.parents[2], tmp_path / "report.json", retire_legacy=True)


def test_report_and_backup_paths_cannot_overwrite_active_stores(history, tmp_path):
    source, _rows, _schema = history
    original = source.read_bytes()
    with pytest.raises(ValueError, match="report path"):
        migrator.migrate(source.parents[2], source, apply=True)
    with pytest.raises(ValueError, match="outside the active llm"):
        migrator.migrate(source.parents[2], tmp_path / "report.json", apply=True, backup_directory=source.parent / "rollback")
    assert source.read_bytes() == original


def test_open_handle_probe_has_a_bounded_timeout(tmp_path, monkeypatch):
    source = tmp_path / "source.parquet"
    source.write_bytes(b"Synthetic input")
    monkeypatch.setattr(migrator.shutil, "which", lambda _name: "/usr/sbin/lsof")
    observed = {}

    def probe(*arguments, **options):
        observed.update(options)
        raise migrator.subprocess.TimeoutExpired(arguments[0], options["timeout"])

    monkeypatch.setattr(migrator.subprocess, "run", probe)
    with pytest.raises(migrator.subprocess.TimeoutExpired):
        migrator.assert_no_open_handles(source)
    assert observed["timeout"] == 30


def test_benchmark_query_plan_uses_packed_conversation_mapping(history, monkeypatch):
    source, rows, schema = history
    target = source.with_suffix(".sqlite3")
    write_sqlite_rows(target, rows, schema)
    monkeypatch.setattr(migrator, "measure", lambda _operation, _repeats: {"median_ms": 1.0})
    result = _BENCHMARK(source, target, pq.read_table(source), 3)
    assert any("conversation_groups" in detail for detail in result["query_plan"])
    assert any("SEARCH row_groups" in detail for detail in result["query_plan"])


def test_busy_cache_writer_blocks_apply_without_changing_history(history, tmp_path):
    from app.core.job_lock import CacheTaskLock, cache_task_resource_lock_path

    source, _rows, _schema = history
    root = source.parents[2]
    original = source.read_bytes()
    writer = CacheTaskLock(cache_task_resource_lock_path(root, "source-synthetic"))
    assert writer.acquire("synthetic-cache-writer")
    try:
        with pytest.raises(RuntimeError, match="cache task is running"):
            migrator.migrate(root, tmp_path / "report.json", apply=True, retire_legacy=True)
    finally:
        writer.release()
    assert source.read_bytes() == original
    assert not source.with_suffix(".sqlite3").exists()
    assert not (tmp_path / "report.json").exists()


def test_preview_does_not_create_or_acquire_maintenance_gate(history, tmp_path):
    from app.core.job_lock import CACHE_TASK_LOCK_NAME, CacheTaskLock, cache_task_resource_lock_path

    source, _rows, _schema = history
    root = source.parents[2]
    writer = CacheTaskLock(cache_task_resource_lock_path(root, "source-synthetic"))
    assert writer.acquire("synthetic-cache-writer")
    try:
        result = migrator.migrate(root, tmp_path / "preview.json")
    finally:
        writer.release()
    assert result["entries"][0]["status"] == "planned"
    assert not (root / CACHE_TASK_LOCK_NAME).exists()


def test_failed_migration_releases_maintenance_gate(history, tmp_path, monkeypatch):
    from app.core.job_lock import CACHE_TASK_LOCK_NAME, CacheTaskLock

    source, _rows, _schema = history
    root = source.parents[2]

    def fail(*_arguments, **_options):
        raise ValueError("Synthetic migration failure")

    monkeypatch.setattr(migrator, "_migrate", fail)
    with pytest.raises(ValueError, match="Synthetic migration failure"):
        migrator.migrate(root, tmp_path / "report.json", apply=True)
    gate = CacheTaskLock(root / CACHE_TASK_LOCK_NAME)
    assert gate.acquire("synthetic-gate-probe")
    gate.release()
