"""Migrate canonical LLM Parquet stores with verified rollback and benchmarks.

Code version: v1.2.1-codex.0

Only explicit canonical stores are eligible. Numbered histories, import
evidence, media, unrelated catalogs, and browser profiles are never modified.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable

import pyarrow as pa
import pyarrow.parquet as pq


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.llm_sqlite import read_sqlite_rows, read_sqlite_schema, write_sqlite_rows  # noqa: E402


VERSION = "v1.2.1-codex.0"


def file_state(path: Path) -> dict[str, int]:
    state = path.stat()
    return {
        "device": state.st_dev,
        "inode": state.st_ino,
        "logical_bytes": state.st_size,
        "allocated_bytes": getattr(state, "st_blocks", 0) * 512,
        "mtime_ns": state.st_mtime_ns,
        "flags": getattr(state, "st_flags", 0),
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024**2), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_sources(root: Path) -> list[Path]:
    llm = root / "llm"
    sources = list(llm.glob("*/history.parquet"))
    sources.extend(llm / "copilot" / name for name in ("legacy-catalog.parquet", "sync-state.parquet"))
    sources.append(llm / ".cache_catalog.parquet")
    return sorted(path for path in sources if path.exists())


def assert_no_open_handles(path: Path) -> None:
    """Fail closed when an active process owns a migration input or target."""
    if not path.exists():
        return
    executable = shutil.which("lsof")
    if executable is None:
        raise RuntimeError("lsof is required before migrating or retiring local history.")
    result = subprocess.run(
        [executable, "-t", "--", str(path)],
        capture_output=True, text=True, check=False, timeout=30,
    )
    if result.returncode == 0 and result.stdout.strip():
        raise RuntimeError(f"An active process has an open handle on {path.name}.")
    if result.returncode not in {0, 1}:
        raise RuntimeError("The open-handle ownership check failed.")


def _json_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def measure(operation: Callable[[], Any], repeats: int) -> dict[str, Any]:
    operation()
    samples = []
    for _ in range(max(3, repeats)):
        started = time.perf_counter()
        operation()
        samples.append((time.perf_counter() - started) * 1000)
    ordered = sorted(samples)
    return {
        "median_ms": statistics.median(samples),
        "p95_ms": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "samples_ms": samples,
        "cache": "warm OS filesystem cache; one untimed warmup",
    }


def benchmark(source: Path, target: Path, table: pa.Table, repeats: int) -> dict[str, Any]:
    """Measure equivalent full snapshots and indexed conversation reads."""
    original_rows = table.to_pylist()
    result = {
        "full_read_parquet": measure(lambda: pq.read_table(source).to_pylist(), repeats),
        "full_read_sqlite": measure(lambda: read_sqlite_rows(target), repeats),
    }
    counts = Counter(str(row.get("conversation_id") or "") for row in original_rows)
    if counts and any(counts):
        ordered = sorted((count, identity) for identity, count in counts.items() if identity)
        count, conversation = ordered[len(ordered) // 2]
        result["session_selection"] = "median conversation by row count; provider identity omitted"
        result["session_rows"] = count
        result["session_read_parquet_full_scan"] = measure(
            lambda: [row for row in pq.read_table(source).to_pylist() if row.get("conversation_id") == conversation],
            repeats,
        )
        result["session_read_sqlite_indexed"] = measure(lambda: read_sqlite_rows(target, conversation), repeats)
        result["query_plan"] = []
        with closing(sqlite3.connect(target)) as connection:
            has_mapping = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_groups'"
            ).fetchone() is not None
            indexed_query = (
                "SELECT raw_bytes,payload FROM row_groups WHERE group_key IN "
                "(SELECT group_key FROM conversation_groups WHERE conversation_id IN (?))"
                if has_mapping else "SELECT raw_bytes,payload FROM row_groups WHERE group_key=?"
            )
            result["query_plan"] = [row[3] for row in connection.execute(
                "EXPLAIN QUERY PLAN " + indexed_query, (conversation,),
            )]
    # A changing value prevents the warmup and subsequent samples from becoming no-op writes.
    changed = [dict(row) for row in original_rows]
    if changed and "content_text" in changed[0]:
        with tempfile.TemporaryDirectory(prefix="llm-write-benchmark-") as temporary:
            temporary_root = Path(temporary)
            sqlite_copy = temporary_root / target.parent.name / target.name
            sqlite_copy.parent.mkdir()
            shutil.copyfile(target, sqlite_copy)
            parquet_copy = temporary_root / source.name
            counter = 0

            def update_rows() -> None:
                nonlocal counter
                counter += 1
                changed[0]["content_text"] = str(original_rows[0]["content_text"]) + f" benchmark {counter}"

            def sqlite_write() -> None:
                update_rows()
                write_sqlite_rows(sqlite_copy, changed, table.schema)

            def parquet_write() -> None:
                update_rows()
                descriptor, temporary_name = tempfile.mkstemp(dir=temporary_root)
                os.close(descriptor)
                try:
                    pq.write_table(pa.Table.from_pylist(changed, schema=table.schema), temporary_name, compression="zstd")
                    verified = pq.read_table(temporary_name)
                    if verified.num_rows != len(changed):
                        raise RuntimeError("The benchmark snapshot did not round-trip.")
                    os.replace(temporary_name, parquet_copy)
                finally:
                    if os.path.exists(temporary_name):
                        os.unlink(temporary_name)

            result["snapshot_update_parquet"] = measure(parquet_write, repeats)
            result["snapshot_update_sqlite"] = measure(sqlite_write, repeats)
            result["write_scope"] = "one message changed; full snapshot API; disposable copies only"
    return result


def preserve_backup(source: Path, backup: Path, expected_sha256: str) -> dict[str, Any]:
    """Preserve and verify the exact original file bytes before any retirement."""
    backup.parent.mkdir(parents=True, exist_ok=True)
    if backup.exists():
        raise ValueError("The rollback backup already exists; preserve it and choose a new migration directory.")
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with source.open("rb") as original, os.fdopen(descriptor, "wb") as output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
            shutil.copyfileobj(original, compressed, length=4 * 1024**2)
        output.flush()
        os.fsync(output.fileno())
    digest = hashlib.sha256()
    decoded_bytes = 0
    with gzip.open(backup, "rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024**2), b""):
            digest.update(block)
            decoded_bytes += len(block)
    if digest.hexdigest() != expected_sha256:
        raise ValueError("The rollback backup failed exact-byte verification.")
    return {
        "path": str(backup),
        "compressed_sha256": sha256(backup),
        "restored_sha256": digest.hexdigest(),
        "restored_bytes": decoded_bytes,
        **file_state(backup),
    }


def migrate(
    root: Path,
    report_path: Path,
    *,
    apply: bool = False,
    retire_legacy: bool = False,
    repeats: int = 3,
    backup_directory: Path | None = None,
) -> dict[str, Any]:
    """Hold the application maintenance gate throughout an applied migration."""
    options = {
        "apply": apply,
        "retire_legacy": retire_legacy,
        "repeats": repeats,
        "backup_directory": backup_directory,
    }
    if not apply:
        return _migrate(root, report_path, **options)
    from app.core.job_lock import CACHE_TASK_LOCK_NAME, CacheMaintenanceLock, CacheTaskLock

    root = Path(root).expanduser().resolve()
    lock = CacheMaintenanceLock(CacheTaskLock(root / CACHE_TASK_LOCK_NAME), root)
    if not lock.acquire("llm-sqlite-migration"):
        raise RuntimeError("A cache task is running; LLM history was not modified.")
    try:
        return _migrate(root, report_path, **options)
    finally:
        lock.release()


def _migrate(
    root: Path,
    report_path: Path,
    *,
    apply: bool = False,
    retire_legacy: bool = False,
    repeats: int = 3,
    backup_directory: Path | None = None,
) -> dict[str, Any]:
    root = Path(root).expanduser().resolve()
    report_path = Path(report_path).expanduser().resolve()
    if retire_legacy and not apply:
        raise ValueError("--retire-legacy requires --apply.")
    started = datetime.now(UTC).isoformat()
    backup_directory = Path(backup_directory or root / "migrations" / ("llm-sqlite-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ"))).expanduser().resolve()
    if backup_directory == root / "llm" or root / "llm" in backup_directory.parents:
        raise ValueError("The rollback directory must remain outside the active llm directory.")
    report: dict[str, Any] = {
        "version": VERSION,
        "started_at_utc": started,
        "root": str(root),
        "mode": "apply" if apply else "preview",
        "retire_legacy": retire_legacy,
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "pyarrow": pa.__version__,
        "entries": [],
        "limits": [
            "Numbered files, import evidence, media, and unrelated catalogs are excluded.",
            "Warm-cache timings are local measurements, not cold-start or universal guarantees.",
            "Logical active-store savings exclude verified rollback backups.",
            "Allocated bytes are per-file allocation, not APFS container free-space attribution.",
            "Cloud-only sources may hydrate during migration; initial allocation and resident baseline are separate.",
        ],
    }
    sources = canonical_sources(root)
    if any(report_path in {source.resolve(), source.with_suffix(".sqlite3").resolve()} for source in sources):
        raise ValueError("The report path must not overwrite a migration source or target.")
    for source in sources:
        target = source.with_suffix(".sqlite3")
        if source.is_symlink() or not source.is_file() or target.is_symlink():
            raise ValueError("Migration requires regular canonical files without symlink aliases.")
        entry: dict[str, Any] = {
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
            table = pq.read_table(source)
            original_rows = table.to_pylist()
            original_identity = file_state(source)
            original_hash = sha256(source)
            source_atime_ns = source.stat().st_atime_ns
            entry["source_resident"] = original_identity
            entry["source_sha256"] = original_hash
            entry["rows"] = table.num_rows
            entry["columns"] = table.num_columns
            entry["conversations"] = len({row.get("conversation_id") for row in original_rows}) if "conversation_id" in table.column_names else None
            if target.exists():
                if not read_sqlite_schema(target).equals(table.schema, check_metadata=True) or read_sqlite_rows(target) != original_rows:
                    raise ValueError("An existing SQLite target differs from its Parquet source; neither was overwritten.")
                entry["status"] = "existing_target_verified"
            else:
                with tempfile.TemporaryDirectory(prefix=".llm-sqlite-", dir=target.parent) as temporary:
                    candidate = Path(temporary) / source.parent.name / target.name
                    candidate.parent.mkdir()
                    write_sqlite_rows(candidate, original_rows, table.schema)
                    if not read_sqlite_schema(candidate).equals(table.schema, check_metadata=True) or read_sqlite_rows(candidate) != original_rows:
                        raise ValueError("Migration changed a field, schema, duplicate, or original row order.")
                    with closing(sqlite3.connect(candidate)) as connection:
                        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                            raise ValueError("The migration candidate failed SQLite integrity_check.")
                    if file_state(source) != original_identity or sha256(source) != original_hash:
                        raise ValueError("The Parquet source changed while migration was being prepared.")
                    # Discovery overlays compare the history's modification time.
                    # Publication must not make old history appear newly observed.
                    os.utime(candidate, ns=(source_atime_ns, original_identity["mtime_ns"]))
                    # link is an atomic no-clobber publication on the same filesystem.
                    os.link(candidate, target)
                entry["status"] = "migrated"
            os.utime(target, ns=(source_atime_ns, original_identity["mtime_ns"]))
            entry["target_state"] = file_state(target)
            with closing(sqlite3.connect(target.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                entry["store_format_version"] = connection.execute("PRAGMA user_version").fetchone()[0]
            entry["benchmarks"] = benchmark(source, target, table, repeats)
            backup = backup_directory / source.relative_to(root).with_suffix(source.suffix + ".gz")
            entry["rollback"] = preserve_backup(source, backup, original_hash)
            _json_report(report_path, report)
            if file_state(source) != original_identity or sha256(source) != original_hash:
                raise ValueError("The Parquet source changed after backup; it was retained.")
            if retire_legacy:
                assert_no_open_handles(source)
                assert_no_open_handles(target)
                source.unlink()
                entry["status"] = "migrated_legacy_retired"
            entry["source_remaining_allocated_bytes"] = file_state(source)["allocated_bytes"] if source.exists() else 0
        except Exception as error:
            entry["status"] = "failed"
            entry["error"] = str(error)
            _json_report(report_path, report)
            raise
        _json_report(report_path, report)
    report["totals"] = {
        "stores": len(report["entries"]),
        "rows": sum(entry.get("rows", 0) for entry in report["entries"]),
        "source_logical_bytes": sum(entry["source_initial"]["logical_bytes"] for entry in report["entries"]),
        "source_initial_allocated_bytes": sum(entry["source_initial"]["allocated_bytes"] for entry in report["entries"]),
        "source_resident_allocated_bytes": sum(entry.get("source_resident", {}).get("allocated_bytes", 0) for entry in report["entries"]),
        "source_remaining_allocated_bytes": sum(entry.get("source_remaining_allocated_bytes", 0) for entry in report["entries"]),
        "target_logical_bytes": sum(entry.get("target_state", {}).get("logical_bytes", 0) for entry in report["entries"]),
        "target_allocated_bytes": sum(entry.get("target_state", {}).get("allocated_bytes", 0) for entry in report["entries"]),
        "rollback_allocated_bytes": sum(entry.get("rollback", {}).get("allocated_bytes", 0) for entry in report["entries"]),
    }
    report["finished_at_utc"] = datetime.now(UTC).isoformat()
    _json_report(report_path, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT / "local_store")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--retire-legacy", action="store_true")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--backup-directory", type=Path)
    arguments = parser.parse_args()
    result = migrate(arguments.root, arguments.report, apply=arguments.apply, retire_legacy=arguments.retire_legacy, repeats=arguments.repeats, backup_directory=arguments.backup_directory)
    print(json.dumps({"report": str(arguments.report), "totals": result["totals"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
