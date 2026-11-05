"""Migrate canonical media metadata with verified rollback and comparable timings.

Code version: v1.0.0

The migration excludes media payloads, numbered copies, browser profiles, LLM
histories, deleted-resource records, saved prompts, and recovery evidence.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import UTC, datetime
import os
from pathlib import Path
import platform
import sqlite3
import sys
import tempfile
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.media_sqlite import (  # noqa: E402
    MediaSQLiteStore,
    media_store_for,
    read_media_legacy_schema,
    read_media_sqlite_schema,
)
from app.core.resource_persistence import (  # noqa: E402
    CHATGPT_CATALOG_SCHEMA,
    GROK_CATALOG_SCHEMA,
    GROK_DOWNLOAD_MANIFEST_SCHEMA,
    GROK_WORK_QUEUE_SCHEMA,
    X_CACHE_CATALOG_SCHEMA,
)
from scripts.migrate_llm_sqlite import (  # noqa: E402
    _json_report,
    assert_no_open_handles,
    file_state,
    measure,
    preserve_backup,
    sha256,
)


VERSION = "v1.0.0"
SPECS = {
    ".chatgpt_catalog.parquet": (
        CHATGPT_CATALOG_SCHEMA,
        "file_id",
        ("content_sha256", "relative_path", "conversation_url"),
    ),
    ".grok_catalog.parquet": (
        GROK_CATALOG_SCHEMA,
        "identity",
        ("content_sha256", "relative_path"),
    ),
    ".grok_download_manifest.parquet": (
        GROK_DOWNLOAD_MANIFEST_SCHEMA,
        "identity",
        ("asset_id", "status"),
    ),
    ".grok_work_queue.parquet": (
        GROK_WORK_QUEUE_SCHEMA,
        "asset_id",
        ("identity", "status"),
    ),
    ".cache_catalog.parquet": (X_CACHE_CATALOG_SCHEMA, "relative_tweet_dir", ()),
}


def canonical_sources(root: Path) -> list[Path]:
    """Discover only canonical metadata inside the two admitted media roots."""
    sources = []
    for admitted in (Path(root) / "media", Path(root) / "x"):
        if admitted.is_symlink():
            raise ValueError("Media roots must not be symbolic links.")
        for directory, children, names in os.walk(admitted, followlinks=False):
            children[:] = [
                name
                for name in children
                if name not in {"llm", "recovery", "migrations", "openclaw_archive"}
                and not (Path(directory) / name).is_symlink()
            ]
            for name in names:
                if name in SPECS:
                    sources.append(Path(directory) / name)
    return sorted(sources)


def _store(path: Path, schema: pa.Schema) -> MediaSQLiteStore:
    return media_store_for(path, schema)


def benchmark(
    source: Path, target: Path, table: pa.Table, repeats: int
) -> dict[str, Any]:
    """Compare disk-backed equivalent operations on disposable writable copies."""
    schema = table.schema
    rows = table.to_pylist()
    _expected, key, _indexes = SPECS[source.name]
    store = _store(target, schema)
    result = {
        "scope": "warm filesystem cache; disk APIs, not process-local memory hits",
        "full_read_parquet": measure(
            lambda: pq.read_table(source).to_pylist(), repeats
        ),
        "full_read_sqlite": measure(store.rows, repeats),
    }
    if not rows:
        return result
    selected = rows[len(rows) // 2][key]
    result["key_read_parquet"] = measure(
        lambda: next(
            (row for row in pq.read_table(source).to_pylist() if row[key] == selected),
            None,
        ),
        repeats,
    )
    result["key_read_sqlite"] = measure(lambda: store.get(selected), repeats)
    fields = [
        field.name
        for field in schema
        if pa.types.is_string(field.type) and field.name != key
    ]
    if not fields:
        return result
    field = "status" if "status" in fields else fields[0]
    with tempfile.TemporaryDirectory(prefix="media-state-benchmark-") as directory:
        temporary = Path(directory)
        sqlite_copy = temporary / target.name
        with (
            closing(sqlite3.connect(target)) as original,
            closing(sqlite3.connect(sqlite_copy)) as copy,
        ):
            original.backup(copy)
        mutable = _store(sqlite_copy, schema)
        parquet_copy = temporary / source.name
        pq.write_table(table, parquet_copy, compression="zstd")
        snapshot_rows = [dict(row) for row in rows]
        counter = 0

        def updated_row() -> dict[str, Any]:
            nonlocal counter
            counter += 1
            row = dict(rows[len(rows) // 2])
            row[field] = f"benchmark-{counter}"
            return row

        def parquet_update() -> None:
            updated = updated_row()
            snapshot_rows[len(snapshot_rows) // 2] = updated
            candidate = temporary / "candidate.parquet"
            pq.write_table(
                pa.Table.from_pylist(snapshot_rows, schema=schema),
                candidate,
                compression="zstd",
            )
            verified = pq.read_table(candidate)
            if verified.num_rows != len(rows):
                raise RuntimeError("Parquet benchmark verification failed.")
            os.replace(candidate, parquet_copy)

        result["one_row_update_parquet"] = measure(parquet_update, repeats)
        result["one_row_update_sqlite"] = measure(
            lambda: mutable.upsert_rows([updated_row()]), repeats
        )
        result["update_scope"] = (
            "one changed existing row; existing in-memory Parquet snapshot serialize/verify/replace versus SQLite keyed transaction"
        )
    return result


def migrate(
    root: Path,
    report_path: Path,
    *,
    apply: bool = False,
    retire_legacy: bool = False,
    backup_directory: Path | None = None,
    repeats: int = 7,
) -> dict[str, Any]:
    """Keep migration publication mutually exclusive with application cache tasks."""
    from app.core.job_lock import (
        CACHE_TASK_LOCK_NAME,
        CacheMaintenanceLock,
        CacheTaskLock,
    )

    root = Path(root).expanduser().resolve()
    if retire_legacy and not apply:
        raise ValueError("--retire-legacy requires --apply.")
    lock = CacheMaintenanceLock(CacheTaskLock(root / CACHE_TASK_LOCK_NAME), root)
    if apply and not lock.acquire("media-sqlite-migration"):
        raise RuntimeError("A cache task is running; media stores were not modified.")
    try:
        return _migrate(
            root, report_path, apply, retire_legacy, backup_directory, repeats
        )
    finally:
        if apply:
            lock.release()


def _migrate(root, report_path, apply, retire_legacy, backup_directory, repeats):
    report_path = Path(report_path).expanduser()
    if report_path.exists() or report_path.is_symlink():
        raise ValueError(
            "Reports must not overwrite existing files; choose a new report path."
        )
    report_path = report_path.resolve()
    if report_path.exists():
        raise ValueError(
            "Reports must not overwrite existing files; choose a new report path."
        )
    backup_root = (
        Path(
            backup_directory
            or root
            / "migrations"
            / ("media-sqlite-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ"))
        )
        .expanduser()
        .resolve()
    )
    for output in (report_path, backup_root):
        if output == root or (
            root in output.parents and root / "migrations" not in output.parents
        ):
            raise ValueError(
                "Reports and rollback directories must not overwrite active media stores or protected evidence; use the migrations directory."
            )
    sources = canonical_sources(root)
    protected = {
        path.resolve()
        for source in sources
        for path in (source, source.with_suffix(".sqlite3"))
    }
    if (
        report_path in protected
        or backup_root == root
        or any(path == backup_root or backup_root in path.parents for path in protected)
    ):
        raise ValueError(
            "Reports and rollback directories must not overwrite active media stores."
        )
    report = {
        "version": VERSION,
        "started_at_utc": datetime.now(UTC).isoformat(),
        "root": str(root),
        "mode": "apply" if apply else "preview",
        "retire_legacy": retire_legacy,
        "runtime": {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "pyarrow": pa.__version__,
        },
        "entries": [],
        "limits": [
            "Only canonical media/x metadata names are included; media payloads and preserved evidence are excluded.",
            "Preview reads metadata only; applying can hydrate cloud placeholders.",
            "Timings are repeated warm-cache measurements; process-local catalog memory hits are not compared.",
            "Logical bytes, initial/resident allocation, rollback allocation and net allocation are separate.",
            "Allocation is file-level evidence, not proof of APFS container free-space recovery.",
        ],
    }
    for source in sources:
        target = source.with_suffix(".sqlite3")
        if source.is_symlink() or target.is_symlink():
            raise ValueError("Canonical migration paths must not be symbolic links.")
        entry = {
            "source": str(source.relative_to(root)),
            "target": str(target.relative_to(root)),
            "source_initial": file_state(source),
            "status": "planned",
        }
        report["entries"].append(entry)
        _json_report(report_path, report)
        if not apply:
            continue
        try:
            assert_no_open_handles(source)
            assert_no_open_handles(target)
            before_hash = file_state(source)
            digest = sha256(source)
            original = file_state(source)
            identity_fields = ("device", "inode", "logical_bytes", "mtime_ns")
            if any(before_hash[field] != original[field] for field in identity_fields):
                raise ValueError(
                    "Source changed before reading rows; publication was refused."
                )
            table = pq.read_table(source).cast(read_media_legacy_schema(source))
            rows = table.to_pylist()
            if file_state(source) != original or sha256(source) != digest:
                raise ValueError(
                    "Source changed while reading rows; publication was refused."
                )
            atime_ns = source.stat().st_atime_ns
            entry.update(
                source_resident=original, source_sha256=digest, rows=table.num_rows
            )
            expected, key, _indexes = SPECS[source.name]
            if not table.schema.equals(expected, check_metadata=False):
                raise ValueError(
                    "The canonical media schema differs; review this source before migration."
                )
            if len({row[key] for row in rows}) != len(rows):
                raise ValueError(
                    "Duplicate media primary keys require explicit reconciliation; source was preserved."
                )
            existing = target.exists()
            if existing:
                target_before = file_state(target)
                target_hash = sha256(target)
                if (
                    not read_media_sqlite_schema(target).equals(
                        table.schema, check_metadata=True
                    )
                    or _store(target, table.schema).rows() != rows
                ):
                    raise ValueError(
                        "An existing SQLite target differs; neither store was overwritten."
                    )
                if file_state(target) != target_before or sha256(target) != target_hash:
                    raise ValueError(
                        "The existing SQLite target changed during validation."
                    )
            entry["rollback"] = preserve_backup(
                source,
                backup_root / source.relative_to(root).with_suffix(".parquet.gz"),
                digest,
            )
            if file_state(source) != original or sha256(source) != digest:
                raise ValueError(
                    "Source changed after backup; publication was refused."
                )
            if not existing:
                with tempfile.TemporaryDirectory(
                    prefix=".media-migration-", dir=source.parent
                ) as directory:
                    candidate = Path(directory) / target.name
                    _store(candidate, table.schema).replace_rows(rows)
                    if (
                        not read_media_sqlite_schema(candidate).equals(
                            table.schema, check_metadata=True
                        )
                        or _store(candidate, table.schema).rows() != rows
                    ):
                        raise ValueError("Candidate readback differs from the source.")
                    with closing(sqlite3.connect(candidate)) as connection:
                        if (
                            connection.execute("PRAGMA integrity_check").fetchone()[0]
                            != "ok"
                        ):
                            raise ValueError("Candidate SQLite integrity check failed.")
                    assert_no_open_handles(source)
                    assert_no_open_handles(target)
                    if file_state(source) != original or sha256(source) != digest:
                        raise ValueError(
                            "Source changed during migration; publication was refused."
                        )
                    os.link(candidate, target)
                os.utime(target, ns=(atime_ns, original["mtime_ns"]))
                target_before = file_state(target)
                target_hash = sha256(target)
            entry["target_final"] = file_state(target)
            entry["benchmark"] = benchmark(source, target, table, repeats)
            if retire_legacy:
                assert_no_open_handles(source)
                assert_no_open_handles(target)
                if file_state(source) != original or sha256(source) != digest:
                    raise ValueError(
                        "Source changed before retirement; source was preserved."
                    )
                if file_state(target) != target_before or sha256(target) != target_hash:
                    raise ValueError(
                        "SQLite target changed before retirement; source was preserved."
                    )
                source.unlink()
            entry["status"] = (
                "migrated_legacy_retired"
                if retire_legacy
                else (
                    "existing_target_verified"
                    if existing
                    else "migrated_legacy_retained"
                )
            )
        except Exception as error:
            entry["status"] = "failed"
            entry["error"] = f"{type(error).__name__}: {error}"
            _json_report(report_path, report)
            raise
        _json_report(report_path, report)
    completed = [entry for entry in report["entries"] if "target_final" in entry]
    report["totals"] = {
        "stores": len(completed),
        "rows": sum(entry["rows"] for entry in completed),
        "source_logical_bytes": sum(
            entry["source_resident"]["logical_bytes"] for entry in completed
        ),
        "sqlite_logical_bytes": sum(
            entry["target_final"]["logical_bytes"] for entry in completed
        ),
        "rollback_logical_bytes": sum(
            entry["rollback"]["logical_bytes"] for entry in completed
        ),
        "source_initial_allocated_bytes": sum(
            entry["source_initial"]["allocated_bytes"] for entry in completed
        ),
        "source_resident_allocated_bytes": sum(
            entry["source_resident"]["allocated_bytes"] for entry in completed
        ),
        "sqlite_allocated_bytes": sum(
            entry["target_final"]["allocated_bytes"] for entry in completed
        ),
        "rollback_allocated_bytes": sum(
            entry["rollback"]["allocated_bytes"] for entry in completed
        ),
    }
    report["finished_at_utc"] = datetime.now(UTC).isoformat()
    _json_report(report_path, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-store", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--retire-legacy", action="store_true")
    parser.add_argument("--backup-directory", type=Path)
    parser.add_argument("--repeats", type=int, default=7)
    arguments = parser.parse_args()
    result = migrate(
        arguments.local_store,
        arguments.report,
        apply=arguments.apply,
        retire_legacy=arguments.retire_legacy,
        backup_directory=arguments.backup_directory,
        repeats=arguments.repeats,
    )
    print(
        {
            "stores": len(result["entries"]),
            "mode": result["mode"],
            "report": str(arguments.report),
        }
    )


if __name__ == "__main__":
    main()
