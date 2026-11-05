"""Exercise canonical media migration, immutable rollback, and drift refusal.

Code version: v1.2.0
"""

from __future__ import annotations

import gzip
import importlib.util
import os
from pathlib import Path, PurePosixPath, PureWindowsPath

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.core.owner_only_files import owner_only_problem


_SCRIPT = Path(__file__).parents[1] / "scripts" / "migrate_media_sqlite.py"
_SPEC = importlib.util.spec_from_file_location("migrate_media_sqlite", _SCRIPT)
migrator = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(migrator)


def media_rows(schema, key):
    rows = []
    for ordinal in range(3):
        row = {}
        for field in schema:
            if pa.types.is_string(field.type):
                row[field.name] = f"synthetic-{field.name}-{ordinal}"
            elif pa.types.is_list(field.type):
                row[field.name] = [f"synthetic-{ordinal}"]
            elif pa.types.is_integer(field.type):
                row[field.name] = 2 if field.name == "schema_version" else ordinal
            elif pa.types.is_floating(field.type):
                row[field.name] = ordinal + 0.5
            else:
                raise AssertionError(f"Unhandled test type: {field.type}")
        row[key] = f"key-{ordinal}"
        rows.append(row)
    return rows


@pytest.fixture
def media(tmp_path, monkeypatch):
    root = tmp_path / "store"
    source = root / "media" / "grok" / ".grok_work_queue.parquet"
    source.parent.mkdir(parents=True)
    schema, key, _indexes = migrator.SPECS[source.name]
    schema = schema.with_metadata({b"provenance": b"isolated synthetic media fixture"})
    rows = media_rows(schema, key)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), source)
    monkeypatch.setattr(migrator, "assert_no_open_handles", lambda _path: None)
    monkeypatch.setattr(migrator, "benchmark", lambda *_arguments: {"synthetic": True})
    return root, source, rows, schema


def test_preview_excludes_numbered_files_archives_llm_and_payloads(media, tmp_path):
    root, source, _rows, _schema = media
    for relative in [
        "media/grok/.grok_work_queue 2.parquet",
        "llm/.cache_catalog.parquet",
        "media/llm/.cache_catalog.parquet",
        "recovery/.grok_work_queue.parquet",
        "media/grok/movie.mp4",
    ]:
        excluded = root / relative
        excluded.parent.mkdir(parents=True, exist_ok=True)
        excluded.write_bytes(b"Protected synthetic evidence")
    original = source.read_bytes()
    report = migrator.migrate(root, tmp_path / "preview.json")
    assert [entry["source"] for entry in report["entries"]] == [
        "media/grok/.grok_work_queue.parquet"
    ]
    assert source.read_bytes() == original
    assert not source.with_suffix(".sqlite3").exists()


@pytest.mark.parametrize("root", [PurePosixPath("/store"), PureWindowsPath("C:/store")])
def test_relative_report_identifiers_are_portable_across_path_flavors(root):
    source = root / "media" / "grok" / ".grok_work_queue.parquet"
    assert migrator._relative_report_path(source, root) == "media/grok/.grok_work_queue.parquet"
    assert migrator._relative_report_path(source.with_suffix(".sqlite3"), root) == "media/grok/.grok_work_queue.sqlite3"


@pytest.mark.parametrize("name", sorted(migrator.SPECS))
def test_all_media_types_preserve_exact_schema_rows_backup_and_retire(
    name, tmp_path, monkeypatch
):
    root = tmp_path / "store"
    source = root / "media" / "synthetic" / name
    source.parent.mkdir(parents=True)
    schema, key, _indexes = migrator.SPECS[name]
    schema = schema.with_metadata({b"origin": b"synthetic migration test"})
    rows = media_rows(schema, key)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), source)
    schema = migrator.read_media_legacy_schema(source)
    original = source.read_bytes()
    original_mtime = source.stat().st_mtime_ns
    monkeypatch.setattr(migrator, "assert_no_open_handles", lambda _path: None)
    monkeypatch.setattr(migrator, "benchmark", lambda *_arguments: {})
    report = migrator.migrate(
        root, tmp_path / "report.json", apply=True, retire_legacy=True
    )
    target = source.with_suffix(".sqlite3")
    assert not source.exists()
    assert migrator._store(target, schema).rows() == rows
    assert migrator.read_media_sqlite_schema(target).equals(schema, check_metadata=True)
    assert target.stat().st_mtime_ns == original_mtime
    rollback = Path(report["entries"][0]["rollback"]["path"])
    assert gzip.decompress(rollback.read_bytes()) == original
    assert owner_only_problem(rollback) == ""
    if os.name == "posix":
        assert rollback.stat().st_mode & 0o777 == 0o600
    assert report["totals"]["rows"] == len(rows)


def test_conflicting_existing_target_is_not_overwritten(media, tmp_path):
    root, source, rows, schema = media
    changed = [dict(row) for row in rows]
    changed[0]["status"] = "independent-newer-state"
    target = source.with_suffix(".sqlite3")
    migrator._store(target, schema).replace_rows(changed)
    before = target.read_bytes()
    with pytest.raises(ValueError, match="differs"):
        migrator.migrate(root, tmp_path / "report.json", apply=True)
    assert target.read_bytes() == before
    assert source.exists()


def test_matching_target_resumes_and_retains_original_by_default(media, tmp_path):
    root, source, rows, schema = media
    target = source.with_suffix(".sqlite3")
    migrator._store(target, schema).replace_rows(rows)
    report = migrator.migrate(root, tmp_path / "report.json", apply=True)
    assert report["entries"][0]["status"] == "existing_target_verified"
    assert source.exists()


def test_source_drift_after_verified_backup_prevents_publication(
    media, tmp_path, monkeypatch
):
    root, source, _rows, _schema = media
    backup = migrator.preserve_backup

    def changing_source(path, *arguments):
        receipt = backup(path, *arguments)
        path.write_bytes(path.read_bytes() + b"concurrent writer")
        return receipt

    monkeypatch.setattr(migrator, "preserve_backup", changing_source)
    with pytest.raises(ValueError, match="changed after backup"):
        migrator.migrate(root, tmp_path / "report.json", apply=True, retire_legacy=True)
    assert source.exists()
    assert not source.with_suffix(".sqlite3").exists()


def test_open_handle_prevents_migration(media, tmp_path, monkeypatch):
    root, source, _rows, _schema = media

    def owned(_path):
        raise RuntimeError("An active process owns this source")

    monkeypatch.setattr(migrator, "assert_no_open_handles", owned)
    with pytest.raises(RuntimeError, match="active process"):
        migrator.migrate(root, tmp_path / "report.json", apply=True)
    assert source.exists()
    assert not source.with_suffix(".sqlite3").exists()


def test_duplicate_primary_keys_are_never_silently_collapsed(media, tmp_path):
    root, source, rows, schema = media
    pq.write_table(pa.Table.from_pylist(rows + rows[:1], schema=schema), source)
    with pytest.raises(ValueError, match="Duplicate media primary keys"):
        migrator.migrate(root, tmp_path / "report.json", apply=True)
    assert source.exists()
    assert not source.with_suffix(".sqlite3").exists()


def test_report_cannot_overwrite_a_source(media):
    root, source, _rows, _schema = media
    original = source.read_bytes()
    with pytest.raises(ValueError, match="must not overwrite"):
        migrator.migrate(root, source)
    assert source.read_bytes() == original


def test_expanded_home_report_cannot_overwrite_existing_file(media):
    root, _source, _rows, _schema = media
    existing = Path.home() / "existing-media-migration-report.json"
    existing.write_bytes(b"Existing isolated report")
    try:
        with pytest.raises(ValueError, match="must not overwrite"):
            migrator.migrate(root, Path("~/existing-media-migration-report.json"))
        assert existing.read_bytes() == b"Existing isolated report"
    finally:
        existing.unlink()


def test_retirement_requires_apply(media, tmp_path):
    root, source, _rows, _schema = media
    with pytest.raises(ValueError, match="requires --apply"):
        migrator.migrate(root, tmp_path / "report.json", retire_legacy=True)
    assert source.exists()


def test_symlinked_root_is_refused_without_following_it(media, tmp_path):
    root, source, _rows, _schema = media
    alias = tmp_path / "alias"
    alias.mkdir()
    (alias / "media").symlink_to(root / "media", target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic links"):
        migrator.migrate(alias, tmp_path / "report.json", apply=True)
    assert source.exists()


def test_source_replacement_after_row_read_is_not_accepted_as_the_baseline(
    media,
    tmp_path,
    monkeypatch,
):
    root, source, rows, schema = media
    newer_rows = [dict(row) for row in rows]
    newer_rows[0]["status"] = "newer-independent-source"
    read_table = migrator.pq.read_table
    replacements = []

    def replaced_after_read(path, *arguments, **options):
        table = read_table(path, *arguments, **options)
        if Path(path) == source and not replacements:
            pq.write_table(pa.Table.from_pylist(newer_rows, schema=schema), source)
            replacements.append(source.read_bytes())
        return table

    monkeypatch.setattr(migrator.pq, "read_table", replaced_after_read)
    with pytest.raises(ValueError, match="Source changed"):
        migrator.migrate(
            root,
            tmp_path / "report.json",
            apply=True,
            retire_legacy=True,
        )
    assert replacements
    assert source.read_bytes() == replacements[0]
    assert not source.with_suffix(".sqlite3").exists()


@pytest.mark.parametrize("incompatible", ["field_type", "nullability"])
def test_runtime_incompatible_schema_is_refused_before_publication(
    media,
    tmp_path,
    incompatible,
):
    root, source, rows, schema = media
    invalid_rows = [dict(row) for row in rows]
    fields = []
    for field in schema:
        if incompatible == "field_type" and field.name == "schema_version":
            fields.append(pa.field(field.name, pa.string(), nullable=field.nullable))
            for row in invalid_rows:
                row[field.name] = str(row[field.name])
        elif incompatible == "nullability" and field.name == "status":
            assert not field.nullable
            fields.append(pa.field(field.name, field.type, nullable=True))
        else:
            fields.append(field)
    invalid_schema = pa.schema(fields, metadata=schema.metadata)
    assert invalid_schema.names == schema.names
    assert not invalid_schema.equals(schema, check_metadata=False)
    pq.write_table(pa.Table.from_pylist(invalid_rows, schema=invalid_schema), source)
    before = source.read_bytes()
    with pytest.raises(ValueError, match="schema"):
        migrator.migrate(root, tmp_path / "report.json", apply=True)
    assert source.read_bytes() == before
    assert not source.with_suffix(".sqlite3").exists()


@pytest.mark.parametrize(
    "relative",
    [
        "media/grok/preserved-image.png",
        "llm/chatgpt/history.sqlite3",
        "messaging/wechat/history.sqlite3",
        "agent_browser_profile/edge/Preferences",
        "recovery/preserved-source.zip",
    ],
)
def test_report_cannot_overwrite_excluded_protected_content(media, relative):
    root, source, _rows, _schema = media
    protected = root / relative
    protected.parent.mkdir(parents=True, exist_ok=True)
    protected.write_bytes(b"Protected original content")
    source_before = source.read_bytes()
    with pytest.raises(ValueError):
        migrator.migrate(root, protected)
    assert protected.read_bytes() == b"Protected original content"
    assert source.read_bytes() == source_before
    assert not source.with_suffix(".sqlite3").exists()


@pytest.mark.parametrize("relative", ["media/grok/new-rollback", "llm/new-rollback"])
def test_rollback_directory_cannot_be_created_inside_active_media_or_llm_trees(
    media,
    tmp_path,
    relative,
):
    root, source, _rows, _schema = media
    backup_root = root / relative
    source_before = source.read_bytes()
    with pytest.raises(ValueError):
        migrator.migrate(
            root,
            tmp_path / "report.json",
            apply=True,
            backup_directory=backup_root,
        )
    assert source.read_bytes() == source_before
    assert not backup_root.exists()
    assert not source.with_suffix(".sqlite3").exists()


def test_existing_report_is_not_replaced_at_migration_start(media, tmp_path):
    root, source, _rows, _schema = media
    report = tmp_path / "previous-report.json"
    report.write_bytes(b"Previously preserved report")
    source_before = source.read_bytes()
    with pytest.raises(ValueError):
        migrator.migrate(root, report, apply=True)
    assert report.read_bytes() == b"Previously preserved report"
    assert source.read_bytes() == source_before
    assert not source.with_suffix(".sqlite3").exists()
