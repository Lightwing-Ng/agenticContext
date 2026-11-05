"""Prepare and verify one lossless SQLite compaction outside the runtime directory.

Code version: v1.2.1-codex.0

This tool never publishes, removes, or backs up a live database. The caller must
preserve an independently verified external rollback before an atomic pathname
replacement, while all writers remain quiescent. A prepared file must stay on
the runtime volume for that replacement to preserve its reader identity.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import pickle
import sqlite3
import subprocess
import sys


VERSION = "v1.2.1-codex.0"
_SUPPORTED_VERSIONS = {"v1.0.0-codex.0", "v1.1.0-codex.0", "v1.1.1-codex.0", "v1.2.0-codex.0", VERSION}
_MODULUS = 1 << 256
_DATALESS = 0x40000000
_PIN = "history_file_identity"
# The host ditto probe succeeds at this size and skips compression one byte above.
# This conservative tool cap is not a claim about the decmpfs format itself.
_DITTO_COMPRESSION_MAX_BYTES = 512 * 1024**2


def identity(path):
    state = Path(path).stat()
    return {
        "dev": state.st_dev,
        "inode": state.st_ino,
        "size": state.st_size,
        "mtime_ns": state.st_mtime_ns,
    }


def resident(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Require a regular, resident SQLite file.")
    if getattr(path.stat(), "st_flags", 0) & _DATALESS:
        raise ValueError("Cloud-only inputs must not be hydrated by this tool.")
    for suffix in ("-wal", "-shm", "-journal"):
        extra = Path(str(path) + suffix)
        if extra.is_symlink() or extra.exists() and extra.stat().st_size:
            raise ValueError("Finish the SQLite writer and nonempty sidecars first.")
    return identity(path)


def _distinct_files(paths):
    """Require independent inodes across a publication or mount-refresh batch."""
    states = [resident(path) for path in paths]
    if len({(state["dev"], state["inode"]) for state in states}) != len(states):
        raise ValueError("Require distinct file identities for all sources and candidates.")
    return states


def _independent_candidate(database, candidate):
    states = _distinct_files((database, candidate))
    if Path(candidate).stat().st_nlink != 1:
        raise ValueError("Require an independent single-link candidate before modification.")
    return states


def _inside_directory(path, directory):
    """Recognize case and filesystem aliases through existing directory identities."""
    path, directory = Path(path).resolve(), Path(directory).resolve()
    if path.is_relative_to(directory):
        return True
    root = directory.stat()
    for ancestor in (path, *path.parents):
        try:
            state = ancestor.stat()
        except FileNotFoundError:
            continue
        if (state.st_dev, state.st_ino) == (root.st_dev, root.st_ino):
            return True
    return False


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(4 * 1024**2):
            digest.update(chunk)
    return digest.hexdigest()


def readonly(path, *, immutable=True):
    resident(path)
    suffix = "?mode=ro&immutable=1" if immutable else "?mode=ro"
    return sqlite3.connect(Path(path).absolute().as_uri() + suffix, uri=True)


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def logical_content(path, *, original_pin=None):
    """Hash typed row multisets without sorting or exposing private row content.

    SQLite VACUUM may change implicit rowids. Every declared column, duplicate
    row, schema object, and application header is included. Pickle is used only
    as an internal typed encoder; no external pickle is ever deserialized.
    """
    with readonly(path) as connection:
        schema = connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "ORDER BY type,name,tbl_name"
        ).fetchall()
        if any(row[3] and "CREATE VIRTUAL TABLE" in row[3].upper() for row in schema):
            raise ValueError("Virtual-table archives need a separate compaction review.")
        result = {
            "schema_sha256": hashlib.sha256(pickle.dumps(schema, protocol=5)).hexdigest(),
            "headers": {
                name: connection.execute("PRAGMA " + name).fetchone()[0]
                for name in ("application_id", "user_version", "encoding")
            },
            "tables": {},
        }
        for kind, name, _owner, _sql in schema:
            if kind != "table":
                continue
            count = total = exclusive = 0
            for row in connection.execute("SELECT * FROM " + _quote(name)):
                if name == "archive_info" and original_pin is not None and row[0] == _PIN:
                    if len(row) != 2:
                        raise ValueError("An archive pin requires the two-column info schema.")
                    row = (row[0], original_pin)
                value = int.from_bytes(hashlib.sha256(pickle.dumps(row, protocol=5)).digest())
                total = (total + value) % _MODULUS
                exclusive ^= value
                count += 1
            result["tables"][name] = {
                "rows": count,
                "sum_sha256": f"{total:064x}",
                "xor_sha256": f"{exclusive:064x}",
            }
        return result


def _quick_check(path):
    with readonly(path) as connection:
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("The candidate SQLite quick_check failed.")
        return {
            "page_size": connection.execute("PRAGMA page_size").fetchone()[0],
            "page_count": connection.execute("PRAGMA page_count").fetchone()[0],
            "freelist_count": connection.execute("PRAGMA freelist_count").fetchone()[0],
        }


def _write_receipt(path, report, *, replace=False):
    path = Path(path)
    if not replace and path.exists():
        raise ValueError("Use a new receipt path.")
    temporary = path.with_name(path.name + ".writing")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _compress_path(candidate):
    """Compress a closed candidate through APFS without changing its byte stream."""
    if sys.platform != "darwin":
        raise ValueError("Explicit filesystem compression requires macOS.")
    candidate = Path(candidate)
    baseline = resident(candidate)
    if baseline["size"] > _DITTO_COMPRESSION_MAX_BYTES:
        raise ValueError("The host-verified ditto compression cap is 512 MiB; adopt the plain candidate instead.")
    digest = sha256(candidate)
    allocated_before = candidate.stat().st_blocks * 512
    pending = candidate.with_name(candidate.name + ".hfs-pending")
    if pending.exists() or pending.is_symlink():
        raise ValueError("Review the existing pending compression output first.")
    subprocess.run(
        ["/usr/bin/ditto", "--hfsCompression", "--noclone", str(candidate), str(pending)],
        check=True,
        capture_output=True,
        text=True,
    )
    resident(pending)
    state = pending.stat()
    if not getattr(state, "st_flags", 0) & 0x20 or state.st_blocks * 512 >= allocated_before:
        raise ValueError("The candidate did not gain verified filesystem compression.")
    if sha256(pending) != digest or resident(candidate) != baseline:
        raise ValueError("Filesystem compression changed bytes or its source drifted.")
    with pending.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(pending, candidate)
    return {
        "enabled": True,
        "sha256_before_and_after": digest,
        "allocated_bytes_before": allocated_before,
        "allocated_bytes_after": candidate.stat().st_blocks * 512,
        "compressed_flag": bool(candidate.stat().st_flags & 0x20),
        "write_behavior": "A later SQLite write can decompress the entire file.",
    }


def prepare(database, candidate, receipt, *, page_size=32768, hfs_compression=False):
    """Build one candidate; preserve failed output for an explicit later review."""
    database, candidate, receipt = (Path(item).absolute() for item in (database, candidate, receipt))
    if page_size not in (4096, 8192, 16384, 32768, 65536):
        raise ValueError("Choose a supported power-of-two SQLite page size.")
    if hfs_compression and sys.platform != "darwin":
        raise ValueError("Explicit filesystem compression requires macOS.")
    baseline = resident(database)
    for output in (candidate, receipt):
        if output.exists() or output.is_symlink():
            raise ValueError("Candidate and receipt paths must both be new.")
        if not output.parent.is_dir() or _inside_directory(output.parent, database.parent):
            raise ValueError("Keep candidates and receipts in an existing external workstation.")
    if candidate == receipt or candidate.parent.stat().st_dev != baseline["dev"]:
        raise ValueError("Keep distinct output paths on the runtime volume.")
    before_sha = sha256(database)
    before_logical = logical_content(database)
    if resident(database) != baseline or sha256(database) != before_sha:
        raise ValueError("The source changed during preflight.")
    # immutable=1 suppresses the connection's requested destination page size.
    with readonly(database, immutable=False) as source:
        source.execute("PRAGMA page_size=" + str(page_size))
        source.execute("PRAGMA synchronous=FULL")
        source.execute("VACUUM INTO ?", (str(candidate),))
    candidate.chmod(0o600)
    statistics = _quick_check(candidate)
    if statistics["page_size"] != page_size:
        raise ValueError("SQLite did not apply the requested page size.")
    if logical_content(candidate) != before_logical:
        raise ValueError("Compaction changed retained logical content.")
    compression = _compress_path(candidate) if hfs_compression else {"enabled": False}
    if resident(database) != baseline or sha256(database) != before_sha:
        raise ValueError("The source changed while the candidate was prepared.")
    with candidate.open("rb") as stream:
        os.fsync(stream.fileno())
    report = {
        "version": VERSION,
        "prepared_at": datetime.now(UTC).isoformat(),
        "status": "prepared_not_published",
        "database": str(database),
        "candidate": str(candidate),
        "source_identity": baseline,
        "source_sha256": before_sha,
        "candidate_identity": resident(candidate),
        "candidate_sha256": sha256(candidate),
        "logical_content": before_logical,
        "statistics": statistics,
        "hfs_compression": compression,
        "database_file_bytes_saved": baseline["size"] - candidate.stat().st_size,
        "filesystem_bytes_reclaimed": 0,
        "intentional_changes": {},
    }
    _write_receipt(receipt, report)
    return report


def _read_receipt(path):
    path = Path(path)
    resident(path)
    if path.stat().st_size > 1024**2:
        raise ValueError("The receipt exceeds its metadata budget.")
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("version") not in _SUPPORTED_VERSIONS or report.get("status") != "prepared_not_published":
        raise ValueError("Use a current, unpublished preparation receipt.")
    return report


def adopt_existing(database, candidate, receipt, *, page_size=32768, compression_status="not_requested"):
    """Audit an existing successful VACUUM output without copying or rebuilding it.

    A failed optional filesystem-compression attempt does not invalidate a
    separately verified plain VACUUM output. The failed pending output itself
    is never eligible for adoption.
    """
    database, candidate, receipt = (Path(item).absolute() for item in (database, candidate, receipt))
    if compression_status not in {"not_requested", "unsupported_recorded"}:
        raise ValueError("Record an explicit supported compression-attempt status.")
    if candidate.name.endswith(".hfs-pending"):
        raise ValueError("Do not adopt an output from a failed compression attempt.")
    baseline, candidate_baseline = _independent_candidate(database, candidate)
    if (
        database.resolve() == candidate.resolve()
        or _inside_directory(candidate.parent, database.parent)
        or _inside_directory(receipt.parent, database.parent)
        or not receipt.parent.is_dir()
        or receipt.exists()
        or receipt.is_symlink()
        or candidate_baseline["dev"] != baseline["dev"]
    ):
        raise ValueError("Use an external existing candidate and a new external receipt on the runtime volume.")
    if compression_status == "unsupported_recorded" and getattr(candidate.stat(), "st_flags", 0) & 0x20:
        raise ValueError("An unsupported-compression record requires the plain candidate.")
    source_sha, candidate_sha = sha256(database), sha256(candidate)
    original = logical_content(database)
    statistics = _quick_check(candidate)
    if statistics["page_size"] != page_size or logical_content(candidate) != original:
        raise ValueError("The existing candidate page size or retained logical content differs.")
    if (
        resident(database) != baseline or resident(candidate) != candidate_baseline
        or sha256(database) != source_sha or sha256(candidate) != candidate_sha
    ):
        raise ValueError("A source or candidate changed during adoption verification.")
    report = {
        "version": VERSION,
        "prepared_at": datetime.now(UTC).isoformat(),
        "status": "prepared_not_published",
        "preparation_method": "adopt_existing_vacuum_output",
        "database": str(database), "candidate": str(candidate),
        "source_identity": baseline, "source_sha256": source_sha,
        "candidate_identity": candidate_baseline, "candidate_sha256": candidate_sha,
        "logical_content": original, "statistics": statistics,
        "hfs_compression": {
            "enabled": bool(getattr(candidate.stat(), "st_flags", 0) & 0x20),
            "status": compression_status,
            "allocated_bytes_after": candidate.stat().st_blocks * 512,
            "reason": "The optional compression attempt was not adopted; no general format-size limit is inferred."
            if compression_status == "unsupported_recorded" else "No new filesystem compression was attempted.",
        },
        "database_file_bytes_saved": baseline["size"] - candidate_baseline["size"],
        "filesystem_bytes_reclaimed": 0, "intentional_changes": {},
    }
    _write_receipt(receipt, report)
    return report


def verify(receipt, *, full_content=True):
    """Recheck the exact candidate and unchanged source before caller publication."""
    report = _read_receipt(receipt)
    _independent_candidate(report["database"], report["candidate"])
    source_pin_identities(report)
    for prefix, key in (("source", "database"), ("candidate", "candidate")):
        path = Path(report[key])
        baseline = resident(path)
        if baseline != report[prefix + "_identity"] or sha256(path) != report[prefix + "_sha256"]:
            raise ValueError("The " + prefix + " changed after preparation.")
        if resident(path) != baseline:
            raise ValueError("The " + prefix + " changed during verification.")
    if full_content:
        old_pin = report["intentional_changes"].get(_PIN, {}).get("before")
        if logical_content(report["candidate"], original_pin=old_pin) != report["logical_content"]:
            raise ValueError("The candidate logical content differs from its receipt.")
        _quick_check(report["candidate"])
    return report


def _device_only_change(before, after):
    keys = {"dev", "inode", "size", "mtime_ns"}
    return (
        set(before) == keys and set(after) == keys
        and before["dev"] != after["dev"]
        and all(before[key] == after[key] for key in keys - {"dev"})
    )


def source_pin_identities(report):
    """Return only current or explicitly byte-verified pre-remount source pins."""
    current = report["source_identity"]
    refresh = report.get("mount_identity_refresh")
    if refresh is None:
        return [current]
    previous = refresh.get("previous_source_identity", {})
    if (
        not _device_only_change(previous, current)
        or refresh.get("source_sha256") != report["source_sha256"]
        or refresh.get("verified_source_bytes") is not True
        or not isinstance(refresh.get("prior_receipt_sha256"), str)
        or len(refresh["prior_receipt_sha256"]) != 64
        or any(character not in "0123456789abcdef" for character in refresh["prior_receipt_sha256"])
    ):
        raise ValueError("Invalid exact-byte mount identity refresh evidence.")
    return [current, previous]


def refresh_mount(receipts, output_directory):
    """Reaudit one uniform device-number change without weakening ordinary pins.

    The immutable old receipts must still describe every source and candidate
    byte. Only a device-number change is accepted. The primary's historical
    source identity remains explicit so existing current sidecars can be rebound
    without treating an independently stale sidecar as current.
    """
    output_directory = Path(output_directory).absolute()
    if not output_directory.is_dir():
        raise ValueError("Use an existing external receipt directory.")
    inputs = [Path(path).absolute() for path in receipts]
    outputs = [output_directory / path.name for path in inputs]
    if not inputs or len(set(outputs)) != len(outputs) or any(path.exists() for path in outputs):
        raise ValueError("Select distinct old receipts and new output receipt paths.")
    receipt_evidence = [(resident(path), sha256(path)) for path in inputs]
    reports = [_read_receipt(path) for path in inputs]
    file_paths = [Path(item[key]).resolve() for item in reports for key in ("database", "candidate")]
    if len(set(file_paths)) != len(file_paths):
        raise ValueError("Mount refresh source and candidate paths must all be distinct.")
    _distinct_files(file_paths)
    for report in reports:
        _independent_candidate(report["database"], report["candidate"])
    histories = [item for item in reports if Path(item["database"]).name == "history.sqlite3"]
    if len(histories) != 1 or histories[0]["intentional_changes"]:
        raise ValueError("Include exactly one unchanged primary archive receipt.")
    history = histories[0]
    transitions = set()
    baselines = []
    for report in reports:
        if report.get("mount_identity_refresh"):
            raise ValueError("Repeated mount refreshes require a separate explicit review.")
        source_pin_identities(report)
        states = {}
        for prefix, key in (("source", "database"), ("candidate", "candidate")):
            path = Path(report[key])
            if _inside_directory(output_directory, path.resolve().parent):
                raise ValueError("Keep refreshed receipts outside source and candidate directories.")
            previous, current = report[prefix + "_identity"], resident(path)
            if not _device_only_change(previous, current):
                raise ValueError("A mount refresh accepts only a changed device number.")
            transitions.add((previous["dev"], current["dev"]))
            if sha256(path) != report[prefix + "_sha256"] or resident(path) != current:
                raise ValueError("A mount refresh requires unchanged exact source and candidate bytes.")
            states[prefix] = current
        before_pin = report["intentional_changes"].get(_PIN, {}).get("before")
        if logical_content(report["candidate"], original_pin=before_pin) != report["logical_content"]:
            raise ValueError("A remounted candidate's retained logical content differs.")
        _quick_check(report["candidate"])
        baselines.append(states)
    if len(transitions) != 1:
        raise ValueError("Require one uniform previous-to-current volume device transition.")
    if any(
        resident(path) != state or sha256(path) != digest
        for path, (state, digest) in zip(inputs, receipt_evidence, strict=True)
    ):
        raise ValueError("A prior immutable receipt changed during mount verification.")
    new_history = baselines[reports.index(history)]["candidate"]
    for report in reports:
        if report is history:
            continue
        changes = report["intentional_changes"]
        if set(changes) != {_PIN}:
            raise ValueError("Refresh only sidecars already explicitly rebound to this primary.")
        if (
            json.loads(changes[_PIN]["before"]) != history["source_identity"]
            or json.loads(changes[_PIN]["after"]) != history["candidate_identity"]
        ):
            raise ValueError("Do not refresh an independently stale sidecar pin.")
        with readonly(report["database"]) as connection:
            source_info = dict(connection.execute("SELECT key,value FROM archive_info"))
        with readonly(report["candidate"]) as connection:
            candidate_info = dict(connection.execute("SELECT key,value FROM archive_info"))
        if (
            source_info.get(_PIN) != changes[_PIN]["before"]
            or candidate_info.get(_PIN) != changes[_PIN]["after"]
        ):
            raise ValueError("The sidecar's physical pin differs from its prior proof.")
    results = []
    for report, states, old_receipt, output, (_receipt_state, receipt_sha) in zip(
        reports, baselines, inputs, outputs, receipt_evidence, strict=True
    ):
        previous_source, previous_candidate = report["source_identity"], report["candidate_identity"]
        if report is not history:
            _independent_candidate(report["database"], report["candidate"])
            replacement = json.dumps(new_history, sort_keys=True, separators=(",", ":"))
            with sqlite3.connect(report["candidate"]) as connection:
                connection.execute("UPDATE archive_info SET value=? WHERE key=?", (replacement, _PIN))
            old_pin = report["intentional_changes"][_PIN]["before"]
            if logical_content(report["candidate"], original_pin=old_pin) != report["logical_content"]:
                raise ValueError("Mount rebinding changed content outside the permitted history pin.")
            _quick_check(report["candidate"])
            if report["hfs_compression"]["enabled"]:
                report["hfs_compression"] = _compress_path(report["candidate"])
            report["intentional_changes"][_PIN]["after"] = replacement
        if resident(report["database"]) != states["source"]:
            raise ValueError("A source changed during mount refresh.")
        report.update(
            version=VERSION, prepared_at=datetime.now(UTC).isoformat(),
            source_identity=states["source"], candidate_identity=resident(report["candidate"]),
            candidate_sha256=sha256(report["candidate"]),
            mount_identity_refresh={
                "prior_receipt": str(old_receipt), "prior_receipt_sha256": receipt_sha,
                "previous_source_identity": previous_source,
                "previous_candidate_identity": previous_candidate,
                "source_sha256": report["source_sha256"], "verified_source_bytes": True,
            },
        )
        source_pin_identities(report)
        _write_receipt(output, report)
        results.append({"receipt": str(output), "database": report["database"]})
    return {"version": VERSION, "status": "mount_refresh_prepared_not_published", "receipts": results}


def rebind_sidecar(receipt, history_receipt):
    """Rebind only a candidate that was pinned to the unchanged original history."""
    report = verify(receipt)
    history = verify(history_receipt, full_content=False)
    if report["intentional_changes"]:
        raise ValueError("Do not rebind an already modified candidate.")
    candidate = Path(report["candidate"])
    with readonly(candidate) as connection:
        columns = connection.execute("PRAGMA table_info(archive_info)").fetchall()
        if [row[1:3] for row in columns] != [("key", "TEXT"), ("value", "TEXT")]:
            raise ValueError("The sidecar info schema is unsupported.")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"messages", "conversations", "archive_info"} <= tables:
            raise ValueError("The sidecar timeline schema is incomplete.")
        info = dict(connection.execute("SELECT key,value FROM archive_info"))
    if info.get("schema_version") != "1":
        raise ValueError("The sidecar schema version is unsupported.")
    if json.loads(info.get(_PIN, "null")) != history["source_identity"]:
        return {"status": "already_stale_unchanged", "receipt": str(receipt)}
    if "history_partitions_sha256" in info or Path(report["database"]).name != "media_events.sqlite3":
        with readonly(history["database"]) as connection:
            rows = connection.execute("SELECT id,account_key,account_id FROM partitions ORDER BY id").fetchall()
        fingerprint = hashlib.sha256(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        if info.get("history_partitions_sha256") != fingerprint:
            return {"status": "partition_pin_stale_unchanged", "receipt": str(receipt)}
    replacement = json.dumps(history["candidate_identity"], sort_keys=True, separators=(",", ":"))
    _independent_candidate(report["database"], candidate)
    with sqlite3.connect(candidate) as connection:
        connection.execute("UPDATE archive_info SET value=? WHERE key=?", (replacement, _PIN))
    if logical_content(candidate, original_pin=info[_PIN]) != report["logical_content"]:
        raise ValueError("Rebinding changed content outside the permitted history pin.")
    _quick_check(candidate)
    if report["hfs_compression"]["enabled"]:
        report["hfs_compression"] = _compress_path(candidate)
    report["intentional_changes"][_PIN] = {"before": info[_PIN], "after": replacement}
    report["candidate_identity"] = resident(candidate)
    report["candidate_sha256"] = sha256(candidate)
    _write_receipt(receipt, report, replace=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    stage = actions.add_parser("prepare")
    stage.add_argument("--database", type=Path, required=True)
    stage.add_argument("--candidate", type=Path, required=True)
    stage.add_argument("--receipt", type=Path, required=True)
    stage.add_argument("--page-size", type=int, default=32768)
    stage.add_argument("--hfs-compression", action="store_true")
    adopt = actions.add_parser("adopt-existing")
    adopt.add_argument("--database", type=Path, required=True)
    adopt.add_argument("--candidate", type=Path, required=True)
    adopt.add_argument("--receipt", type=Path, required=True)
    adopt.add_argument("--page-size", type=int, default=32768)
    adopt.add_argument("--compression-status", choices=("not_requested", "unsupported_recorded"), default="not_requested")
    check = actions.add_parser("verify")
    check.add_argument("--receipt", type=Path, required=True)
    rebind = actions.add_parser("rebind-sidecar")
    rebind.add_argument("--receipt", type=Path, required=True)
    rebind.add_argument("--history-receipt", type=Path, required=True)
    refresh = actions.add_parser("refresh-mount")
    refresh.add_argument("--receipt", dest="receipts", type=Path, action="append", required=True)
    refresh.add_argument("--output-directory", type=Path, required=True)
    arguments = vars(parser.parse_args())
    action = arguments.pop("action")
    result = {
        "prepare": prepare, "adopt-existing": adopt_existing, "verify": verify,
        "rebind-sidecar": rebind_sidecar, "refresh-mount": refresh_mount,
    }[action](**arguments)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
