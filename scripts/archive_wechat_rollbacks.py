"""Preserve one resident rollback as a verified Zstandard artifact without deleting its source.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid


VERSION = "v1.1.0-codex.0"
SUPPORTED_RECEIPT_VERSIONS = {"v1.0.0-codex.0", VERSION}
CHUNK_BYTES = 8 * 1024 * 1024
DATALESS = 0x40000000
UPLOAD_FIELDS = (
    "isUploaded",
    "isUploading",
    "hasUnresolvedConflicts",
    "isExcludedFromSync",
    "isSyncPaused",
    "isTrashed",
)


def _utc_now():
    return datetime.datetime.now(datetime.UTC).isoformat()


def _identity(path):
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode):
        raise ValueError("Select a regular file rather than a symlink or directory.")
    if getattr(value, "st_flags", 0) & DATALESS:
        raise ValueError("Cloud placeholders must remain untouched; select a resident file.")
    return {
        "device": value.st_dev,
        "inode": value.st_ino,
        "bytes": value.st_size,
        "mtime_ns": value.st_mtime_ns,
        "ctime_ns": value.st_ctime_ns,
    }


def _same_source(path, expected):
    if _identity(path) != expected:
        raise ValueError("Source identity changed; the source and pending evidence were retained.")


def _writer_records(output):
    records = []
    current = {}
    for line in output.splitlines():
        key, value = line[:1], line[1:]
        if key == "p":
            current = {"pid": int(value), "command": ""}
        elif key == "c":
            current["command"] = value
        elif key == "a" and value in {"w", "u"}:
            records.append({**current, "access": value})
    return records


def _assert_no_writers(path):
    result = subprocess.run(
        ["lsof", "+c", "0", "-nP", "-Fpcfa", "--", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode not in {0, 1} or result.stderr.strip():
        raise ValueError("Open-file inspection failed; no artifact was published.")
    records = _writer_records(result.stdout)
    writers = [
        row for row in records
        if row["command"].casefold() not in {"fileproviderd", "fileprovider"}
    ]
    if writers:
        raise ValueError("A non-FileProvider process has the source open for writing.")
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(path) + suffix)
        if os.path.lexists(sidecar) and sidecar.lstat().st_size:
            raise ValueError("A nonempty SQLite journal requires separate preservation.")
    return records


def _write_json(path, record, *, exclusive=False):
    descriptor, name = tempfile.mkstemp(prefix=".receipt-", suffix=".pending", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(record, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path)
            temporary.unlink()
        else:
            os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _hash_file(path):
    before = _identity(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(CHUNK_BYTES):
            digest.update(block)
    _same_source(path, before)
    return digest.hexdigest()


def _compress(source, pending, zstd, *, level, threads):
    digest = hashlib.sha256()
    total = 0
    with pending.open("xb") as output:
        process = subprocess.Popen(
            [zstd, f"-{level}", f"-T{threads}", "-q", "--check", "--no-progress", "-c"],
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=subprocess.DEVNULL,
        )
        try:
            descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                while block := stream.read(CHUNK_BYTES):
                    digest.update(block)
                    total += len(block)
                    process.stdin.write(block)
            process.stdin.close()
            if process.wait():
                raise ValueError("Zstandard compression failed; pending evidence was retained.")
            output.flush()
            os.fsync(output.fileno())
        except BaseException:
            if process.poll() is None:
                process.terminate()
            process.wait()
            raise
    return digest.hexdigest(), total


def _verify_compressed(path, zstd):
    before = _identity(path)
    digest = hashlib.sha256()
    total = 0
    process = subprocess.Popen(
        [zstd, "-d", "-q", "-c", "--", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        with process.stdout as stream:
            while block := stream.read(CHUNK_BYTES):
                digest.update(block)
                total += len(block)
        if process.wait():
            raise ValueError("The complete Zstandard decompression check failed.")
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()
    _same_source(path, before)
    return digest.hexdigest(), total


def archive_file(source, target, *, level=3, threads=2, minimum_free_bytes=268_435_456):
    """Write one compressed artifact and receipts; never rename or delete the source."""
    source = Path(source).expanduser().absolute()
    target = Path(target).expanduser().absolute()
    receipt = target.with_name(target.name + ".receipt.json")
    if source == target or not target.parent.is_dir():
        raise ValueError("Select a new artifact in an existing workstation directory.")
    if any(os.path.lexists(path) for path in (target, receipt)):
        raise ValueError("The final artifact or receipt already exists; nothing was overwritten.")
    if not 1 <= level <= 19 or not 1 <= threads <= 32 or minimum_free_bytes < 0:
        raise ValueError("Compression level, thread count, or free-space reserve is invalid.")
    before = _identity(source)
    writers = _assert_no_writers(source)
    zstd = shutil.which("zstd")
    if zstd is None:
        raise ValueError("The existing zstd CLI is required.")
    required = before["bytes"] + before["bytes"] // 128 + 1_048_576 + minimum_free_bytes
    if shutil.disk_usage(target.parent).free < required:
        raise ValueError("Free space is insufficient for one worst-case compressed artifact and reserve.")
    token = uuid.uuid4().hex
    pending = target.with_name(f".{target.name}.{token}.pending")
    transaction = target.with_name(f".{target.name}.{token}.pending.json")
    record = {
        "version": VERSION,
        "started_utc": _utc_now(),
        "status": "compression_pending",
        "source": str(source),
        "source_identity": before,
        "archive": str(target),
        "receipt": str(receipt),
        "pending_archive": str(pending),
        "transaction_receipt": str(transaction),
        "source_deleted": False,
        "source_write_handles_allowed": writers,
        "cloud_upload_verified": False,
        "remote_content_hash_verified": False,
        "temporary_payload_policy": "One compressed artifact; no uncompressed restoration copy.",
        "free_space_required_bytes": required,
    }
    _write_json(transaction, record, exclusive=True)
    try:
        source_digest, source_bytes = _compress(source, pending, zstd, level=level, threads=threads)
        _same_source(source, before)
        restored_digest, restored_bytes = _verify_compressed(pending, zstd)
        if (source_digest, source_bytes) != (restored_digest, restored_bytes):
            raise ValueError("The complete decompressed bytes differ from the original stream.")
        if source_bytes != before["bytes"]:
            raise ValueError("The original stream size differs from its initial identity.")
        compressed_digest = _hash_file(pending)
        _assert_no_writers(source)
        _same_source(source, before)
        record.update(
            status="roundtrip_verified_publication_pending",
            source_sha256=source_digest,
            archive_sha256=compressed_digest,
            archive_bytes=pending.stat().st_size,
            decompressed_sha256=restored_digest,
            decompressed_bytes=restored_bytes,
            verified=True,
            full_roundtrip_verified=True,
            source_unchanged=True,
        )
        _write_json(transaction, record)
        # Same-directory links publish atomically and refuse an existing destination.
        os.link(pending, target)
        pending.unlink()
        record.update(
            status="byte_exact_preservation_verified_upload_pending",
            archive_identity=_identity(target),
            completed_utc=_utc_now(),
        )
        _write_json(receipt, record, exclusive=True)
        _write_json(transaction, {**record, "status": "completed_receipt_published"})
        return record
    except BaseException as error:
        record.update(status="failed_source_retained", error_type=type(error).__name__, failed_utc=_utc_now())
        _write_json(transaction, record)
        raise


def _read_receipt(receipt):
    receipt = Path(receipt).expanduser().absolute()
    before = _identity(receipt)
    with receipt.open(encoding="utf-8") as stream:
        record = json.load(stream)
    _same_source(receipt, before)
    if record.get("version") not in SUPPORTED_RECEIPT_VERSIONS or not record.get("full_roundtrip_verified"):
        raise ValueError("An unsupported or unverified preservation receipt was selected.")
    archive = Path(record["archive"])
    if not archive.is_absolute() or archive.parent != receipt.parent:
        raise ValueError("The receipt must remain beside its explicit archive.")
    return receipt, record, archive


def verify_receipt(receipt, *, check_source=True):
    receipt, record, archive = _read_receipt(receipt)
    before = _identity(archive)
    if before["bytes"] != record["archive_bytes"] or _hash_file(archive) != record["archive_sha256"]:
        raise ValueError("The compressed artifact differs from its receipt.")
    zstd = shutil.which("zstd")
    if zstd is None:
        raise ValueError("The existing zstd CLI is required.")
    digest, size = _verify_compressed(archive, zstd)
    if digest != record["source_sha256"] or size != record["decompressed_bytes"]:
        raise ValueError("The complete decompressed artifact differs from its receipt.")
    if check_source:
        source = Path(record["source"])
        _assert_no_writers(source)
        _same_source(source, record["source_identity"])
        if _hash_file(source) != record["source_sha256"]:
            raise ValueError("The current source bytes differ from their preservation receipt.")
        _same_source(source, record["source_identity"])
    _same_source(archive, before)
    return {
        "version": VERSION,
        "status": "full_roundtrip_verified",
        "receipt": str(receipt),
        "source_checked": check_source,
        "source_deleted": False,
        "remote_content_hash_verified": False,
    }


def _provider_state(path):
    result = subprocess.run(
        ["fileproviderctl", "evaluate", str(path)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if result.returncode:
        raise ValueError("File Provider inspection failed; upload is unverified.")
    fields = dict(re.findall(r"^\s*(\w+)\s*=\s*([^;\n]+);", result.stdout, re.MULTILINE))
    state = {key: int(fields[key]) if fields.get(key) in {"0", "1"} else None for key in UPLOAD_FIELDS}
    state["documentSize"] = int(fields["documentSize"]) if fields.get("documentSize", "").isdigit() else None
    state["upload_error_present"] = bool(re.search(r"upload\w*error\s*=", result.stdout, re.IGNORECASE))
    return state


def check_upload(receipt):
    """Report preliminary local provider claims without certifying cloud preservation."""
    receipt, record, archive = _read_receipt(receipt)
    checks = []
    for path, expected_bytes in ((archive, record["archive_bytes"]), (receipt, receipt.stat().st_size)):
        value = path.lstat()
        state = _provider_state(path)
        after = path.lstat()
        stable = all(
            getattr(value, key) == getattr(after, key)
            for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        )
        archive_identity_matches = path != archive or all(
            getattr(value, attribute) == record.get("archive_identity", {}).get(key)
            for attribute, key in (
                ("st_dev", "device"), ("st_ino", "inode"),
                ("st_size", "bytes"), ("st_mtime_ns", "mtime_ns"),
            )
        )
        uploaded = (
            stat.S_ISREG(value.st_mode)
            and stable
            and archive_identity_matches
            and value.st_size == expected_bytes == state["documentSize"]
            and state["isUploaded"] == 1
            and all(state[key] == 0 for key in UPLOAD_FIELDS if key != "isUploaded")
            and not state["upload_error_present"]
        )
        checks.append({
            "path": str(path), "provider_upload_reported": uploaded,
            "identity_stable": stable, "archive_identity_matches_receipt": archive_identity_matches,
            "provider": state,
        })
    all_reported = all(row["provider_upload_reported"] for row in checks)
    return {
        "version": VERSION,
        "status": "provider_uploads_reported_cloud_unverified" if all_reported else "provider_uploads_pending_or_unverified",
        "all_provider_uploads_reported": all_reported,
        "checks": checks,
        "cloud_preservation_verified": False,
        "safe_to_release_local_content": False,
        "remote_content_hash_verified": False,
        "verification_boundary": "Preliminary local File Provider metadata can conflict with remote sync state; independent remote evidence is required before source retirement or local content release.",
        "source_deleted": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    commands = parser.add_subparsers(dest="command", required=True)
    archive = commands.add_parser("archive", help="Preserve one source; never delete it.")
    archive.add_argument("source", type=Path)
    archive.add_argument("target", type=Path)
    archive.add_argument("--level", type=int, default=3)
    archive.add_argument("--threads", type=int, default=2)
    verify = commands.add_parser("verify", help="Verify compressed bytes, the full round trip, and current source.")
    verify.add_argument("receipt", type=Path)
    verify.add_argument("--skip-source", action="store_true")
    upload = commands.add_parser("check-upload", help="Report preliminary local upload metadata; cloud preservation remains unverified.")
    upload.add_argument("receipt", type=Path)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "archive":
            report = archive_file(arguments.source, arguments.target, level=arguments.level, threads=arguments.threads)
        elif arguments.command == "verify":
            report = verify_receipt(arguments.receipt, check_source=not arguments.skip_source)
        else:
            report = check_upload(arguments.receipt)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(json.dumps({"version": VERSION, "status": "failed_source_retained", "error": str(error), "source_deleted": False}), file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 2 if arguments.command == "check-upload" else 0


if __name__ == "__main__":
    raise SystemExit(main())
