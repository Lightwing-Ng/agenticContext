"""Publish verified WeChat SQLite candidates with reversible atomic macOS swaps.

Code version: v1.1.2-codex.0

Each pathname pair is exchanged atomically. The operation receipt journals the
ordered multi-file publication; it is not a filesystem-wide transaction. Writers
must remain quiescent throughout publication. No service is stopped or started,
and no preserved original or external rollback is removed by this tool.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import ctypes
from datetime import UTC, datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pickle
import subprocess
import sys


VERSION = "v1.1.1-codex.0"
_SPEC = importlib.util.spec_from_file_location(
    "wechat_sqlite_compactor", Path(__file__).with_name("compact_wechat_sqlite.py")
)
compactor = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(compactor)
_SIDECARS = ("resource_references.sqlite3", "media_events.sqlite3", "resource_revalidated.sqlite3")


def _now():
    return datetime.now(UTC).isoformat()


def _ensure_platform():
    if sys.platform != "darwin":
        raise ValueError("Atomic renamex_np publication requires macOS.")


def _swap(source, candidate):
    """RENAME_SWAP preserves both files and their original inode identities."""
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    function = library.renamex_np
    function.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
    function.restype = ctypes.c_int
    if function(os.fsencode(source), os.fsencode(candidate), 2):
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))


def _assert_no_writers(path):
    result = subprocess.run(
        ["lsof", "+c", "0", "-nP", "-Fpcfa", "--", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode not in (0, 1) or result.stderr.strip():
        raise ValueError("Open-file inspection failed; publication was refused.")
    current = {}
    records = []
    for line in result.stdout.splitlines():
        key, value = line[:1], line[1:]
        if key == "p":
            current = {"pid": int(value), "command": ""}
        elif key == "c":
            current["command"] = value
        elif key == "a" and value in {"w", "u"}:
            records.append({**current, "access": value})
    if any(row["command"].casefold() not in {"fileproviderd", "fileprovider"} for row in records):
        raise ValueError("A non-FileProvider process has a publication file open for writing.")
    compactor.resident(path)
    return records


def _read_json(path):
    path = Path(path)
    compactor.resident(path)
    if path.stat().st_size > 1024**2:
        raise ValueError("A publication receipt exceeds its metadata budget.")
    return json.loads(path.read_text(encoding="utf-8"))


def _view_content(path):
    """Verify complete projected views as typed row multisets without private output."""
    result = {}
    with closing(compactor.readonly(path)) as connection, connection:
        names = connection.execute("SELECT name FROM sqlite_master WHERE type='view' ORDER BY name").fetchall()
        for (name,) in names:
            count = total = exclusive = 0
            for row in connection.execute("SELECT * FROM " + compactor._quote(name)):
                value = int.from_bytes(hashlib.sha256(pickle.dumps(row, protocol=5)).digest())
                total = (total + value) % (1 << 256)
                exclusive ^= value
                count += 1
            result[name] = {"rows": count, "sum_sha256": f"{total:064x}", "xor_sha256": f"{exclusive:064x}"}
    return result


def _info(path):
    with closing(compactor.readonly(path)) as connection, connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='archive_info' AND type='table'").fetchone() is None:
            return {}
        return dict(connection.execute("SELECT key,value FROM archive_info"))


def _backup_index(paths, reports):
    records = {}
    for path in paths:
        path = Path(path).absolute()
        record = _read_json(path)
        if (
            record.get("status") != "byte_exact_preservation_verified_upload_pending"
            or record.get("full_roundtrip_verified") is not True
            or record.get("source_unchanged") is not True
            or record.get("source_deleted") is not False
        ):
            raise ValueError("Require a completed byte-exact external rollback receipt.")
        source = str(Path(record["source"]).absolute())
        if source in records:
            raise ValueError("Select exactly one external rollback receipt per source.")
        records[source] = (path, record)
    result = {}
    for report in reports:
        source = report["database"]
        if source not in records:
            raise ValueError("Every source needs its own verified external rollback.")
        path, record = records[source]
        if (
            record.get("source_sha256") != report["source_sha256"]
            or record.get("decompressed_sha256", report["source_sha256"]) != report["source_sha256"]
            or record.get("decompressed_bytes") != report["source_identity"]["size"]
        ):
            raise ValueError("The external rollback does not preserve the current source bytes.")
        archive = Path(record["archive"]).absolute()
        if not archive.name.endswith(".zst") or archive.parent != path.parent:
            raise ValueError("Keep the explicit Zstandard artifact beside its rollback receipt.")
        if any(compactor._inside_directory(archive, Path(item["database"]).parent) for item in reports):
            raise ValueError("Keep external rollback artifacts outside runtime directories.")
        before = compactor.resident(archive)
        if before["size"] != record["archive_bytes"] or compactor.sha256(archive) != record["archive_sha256"]:
            raise ValueError("The current compressed rollback differs from its verified receipt.")
        if compactor.resident(archive) != before:
            raise ValueError("The compressed rollback changed during verification.")
        result[source] = {"receipt": str(path), "archive": str(archive), "archive_sha256": record["archive_sha256"]}
    return result


def _check_pins(reports):
    histories = [item for item in reports if Path(item["database"]).name == "history.sqlite3"]
    if len(histories) > 1:
        raise ValueError("Publish at most one primary WeChat archive per operation.")
    if not histories:
        if any(item["intentional_changes"] for item in reports):
            raise ValueError("A rebound sidecar requires its prepared primary in this operation.")
        return
    history = histories[0]
    old_identities = compactor.source_pin_identities(history)
    new_identity = history["candidate_identity"]
    selected = {item["database"]: item for item in reports}
    for name in _SIDECARS:
        path = Path(history["database"]).with_name(name)
        if not os.path.lexists(path):
            continue
        # An unreadable existing sidecar is not evidence that no current pin exists.
        # Materialize it and verify its pin before replacing the primary archive.
        compactor.resident(path)
        info = _info(path)
        if json.loads(info.get("history_file_identity", "null")) in old_identities and str(path) not in selected:
            raise ValueError("Include every currently pinned resident sidecar in primary publication.")
    for report in reports:
        if report is history:
            if report["intentional_changes"]:
                raise ValueError("A compacted primary must preserve all original values.")
            continue
        before_info, after_info = _info(report["database"]), _info(report["candidate"])
        before_pin = json.loads(before_info.get("history_file_identity", "null"))
        after_pin = json.loads(after_info.get("history_file_identity", "null"))
        if before_pin in old_identities:
            if after_pin != new_identity or set(report["intentional_changes"]) != {"history_file_identity"}:
                raise ValueError("A current sidecar must be explicitly rebound to the exact new primary.")
        elif report["intentional_changes"] or before_info != after_info:
            raise ValueError("Do not promote or otherwise modify an already stale sidecar.")


def _sync_pair(source, candidate):
    for path in (source, candidate):
        with Path(path).open("rb") as stream:
            os.fsync(stream.fileno())
    for directory in {Path(source).parent, Path(candidate).parent}:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _same(report, *, hash_bytes=False):
    compactor._independent_candidate(report["database"], report["candidate"])
    for prefix, key in (("source", "database"), ("candidate", "candidate")):
        if compactor.resident(report[key]) != report[prefix + "_identity"]:
            raise ValueError("A publication file changed after preflight.")
        if hash_bytes and compactor.sha256(report[key]) != report[prefix + "_sha256"]:
            raise ValueError("A publication file's exact bytes changed after preflight.")


def publish(receipts, backup_receipts, operation_receipt):
    """Swap verified pairs; reverse every completed swap on any caught failure."""
    _ensure_platform()
    operation_receipt = Path(operation_receipt).absolute()
    if operation_receipt.exists() or not operation_receipt.parent.is_dir():
        raise ValueError("Select a new operation receipt in an existing external workstation.")
    reports = [compactor.verify(path) for path in receipts]
    if not reports:
        raise ValueError("Select at least one verified compaction receipt.")
    reports.sort(key=lambda item: (Path(item["database"]).name != "history.sqlite3", item["database"]))
    paths = [Path(item[key]).resolve() for item in reports for key in ("database", "candidate")]
    if len(set(paths)) != len(paths):
        raise ValueError("Publication paths must all be distinct.")
    compactor._distinct_files(paths)
    if any(compactor._inside_directory(operation_receipt, Path(item["database"]).parent) for item in reports):
        raise ValueError("Keep the operation receipt outside all runtime directories.")
    backups = _backup_index(backup_receipts, reports)
    _check_pins(reports)
    entries = []
    for report in reports:
        source, candidate = Path(report["database"]), Path(report["candidate"])
        if report["source_identity"]["dev"] != report["candidate_identity"]["dev"]:
            raise ValueError("Atomic swaps require both files on the same volume.")
        writers = {str(path): _assert_no_writers(path) for path in (source, candidate)}
        source_views, candidate_views = _view_content(source), _view_content(candidate)
        if source_views != candidate_views:
            raise ValueError("Compaction changed the complete logical view output.")
        _same(report, hash_bytes=True)
        entries.append({
            "source": str(source), "candidate": str(candidate),
            "old_identity": report["source_identity"], "new_identity": report["candidate_identity"],
            "old_sha256": report["source_sha256"], "new_sha256": report["candidate_sha256"],
            "complete_view_content": source_views, "intentional_changes": report["intentional_changes"],
            "external_rollback": backups[str(source)], "allowed_write_handles": writers,
            "state": "preflight_verified",
        })
    record = {
        "version": VERSION, "started_at": _now(), "status": "preflight_verified",
        "source_receipts": [str(Path(path).absolute()) for path in receipts],
        "files": entries, "originals_deleted": False,
        "atomicity": "Each pair uses RENAME_SWAP; the receipt journals the ordered multi-file operation.",
    }
    compactor._write_receipt(operation_receipt, record)
    swapped = []
    try:
        for report, entry in zip(reports, entries, strict=True):
            _same(report)
            _assert_no_writers(entry["source"])
            _assert_no_writers(entry["candidate"])
            entry["state"] = "swap_pending"
            record["status"] = "publication_in_progress"
            compactor._write_receipt(operation_receipt, record, replace=True)
            _swap(entry["source"], entry["candidate"])
            swapped.append(entry)
            _sync_pair(entry["source"], entry["candidate"])
            if compactor.resident(entry["source"]) != entry["new_identity"] or compactor.resident(entry["candidate"]) != entry["old_identity"]:
                raise ValueError("A completed swap did not preserve the expected inode identities.")
            entry["state"] = "swapped_old_original_retained"
            compactor._write_receipt(operation_receipt, record, replace=True)
        for entry in entries:
            if compactor.sha256(entry["source"]) != entry["new_sha256"]:
                raise ValueError("Published exact-byte readback failed.")
        record.update(status="published_originals_retained", completed_at=_now())
        compactor._write_receipt(operation_receipt, record, replace=True)
        return record
    except BaseException as error:
        errors = []
        for entry in reversed(swapped):
            try:
                _swap(entry["source"], entry["candidate"])
                _sync_pair(entry["source"], entry["candidate"])
                if compactor.resident(entry["source"]) != entry["old_identity"] or compactor.sha256(entry["source"]) != entry["old_sha256"]:
                    raise ValueError("Restored original identity or bytes differ from preflight.")
                entry["state"] = "rolled_back_original_inode_restored"
            except BaseException as rollback_error:
                errors.append({"source": entry["source"], "error_type": type(rollback_error).__name__})
        record.update(
            status="rollback_failed_review_required" if errors else "failed_originals_restored",
            error_type=type(error).__name__, rollback_errors=errors, failed_at=_now(),
        )
        compactor._write_receipt(operation_receipt, record, replace=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, action="append", required=True)
    parser.add_argument("--backup-receipt", type=Path, action="append", required=True)
    parser.add_argument("--operation-receipt", type=Path, required=True)
    arguments = parser.parse_args()
    result = publish(arguments.receipt, arguments.backup_receipt, arguments.operation_receipt)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
