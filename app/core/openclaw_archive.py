"""Preserve local OpenClaw state without depending on its running gateway.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
from datetime import UTC, datetime
from uuid import uuid4


OPENCLAW_ARCHIVE_SCHEMA_VERSION = 1
_EXCLUDED_ROOTS = {
    "npm": "Regenerable package installations; conversations are stored separately.",
    "cache": "Regenerable application and control-panel caches.",
    "browser": "Authenticated browser profile intentionally excluded from chat migration.",
}
_SQLITE_HEADER = b"SQLite format 3\x00"


def _raise_walk_error(error: OSError) -> None:
    """Refuse to publish a complete archive after an unreadable subtree."""
    raise error


def _canonical_path(path: Path) -> Path:
    """Normalize dot segments only after rejecting symbolic-link ancestors."""
    candidate = path.expanduser().absolute()
    if any(parent.is_symlink() for parent in (candidate, *candidate.parents)):
        raise ValueError("Archive and source paths must not contain symbolic links.")
    return Path(os.path.abspath(candidate))


def _private_directory(path: Path) -> None:
    """Create private archive folders without accepting symbolic-link parents."""
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("Archive and source paths must not contain symbolic links.")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _write_json(path: Path, payload: dict) -> None:
    """Publish private JSON atomically within an existing archive directory."""
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _is_sqlite(path: Path) -> bool:
    with path.open("rb") as stream:
        return stream.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER


def _copy_file(source: Path, destination: Path) -> dict:
    """Copy through a no-follow descriptor and verify the resulting bytes."""
    descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as original:
        before = os.fstat(original.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Archive inputs must be regular files.")
        with destination.open("xb") as target:
            destination.chmod(0o600)
            shutil.copyfileobj(original, target, 1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        after = os.fstat(original.fileno())
    digest = _digest(destination)
    unchanged = (
        before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
        and digest == _digest(source)
    )
    if not unchanged:
        raise RuntimeError("An OpenClaw source file changed during preservation; retry the snapshot.")
    os.utime(destination, ns=(before.st_atime_ns, before.st_mtime_ns))
    return {
        "strategy": "byte-copy", "sha256": digest,
        "size": destination.stat().st_size,
        "source_size": before.st_size, "source_mtime_ns": before.st_mtime_ns,
    }


def _backup_sqlite(source: Path, destination: Path) -> dict:
    """Capture committed SQLite WAL contents using the online backup API."""
    before = source.stat()
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    connection = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True, timeout=10)
    target = sqlite3.connect(destination)
    validation = "ok"
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.backup(target, pages=1024, sleep=0.05)
        target.execute("PRAGMA journal_mode=DELETE").fetchone()
        try:
            results = [row[0] for row in target.execute("PRAGMA quick_check")]
        except sqlite3.DatabaseError as error:
            # OpenClaw can install vec0 or custom tokenizer modules unavailable here.
            if "no such module" not in str(error) and "no such tokenizer" not in str(error):
                raise
            validation = "unavailable: optional SQLite extension is not installed"
        else:
            if results != ["ok"]:
                raise RuntimeError("An OpenClaw database snapshot failed SQLite quick_check.")
        target.execute("SELECT count(*) FROM sqlite_master").fetchone()
    finally:
        target.close()
        connection.close()
    destination.chmod(0o600)
    return {
        "strategy": "sqlite-backup", "sha256": _digest(destination),
        "size": destination.stat().st_size, "quick_check": validation,
        "source_size": before.st_size, "source_mtime_ns": before.st_mtime_ns,
    }


def create_openclaw_archive(
    local_store_root: Path | str,
    source_root: Path | str | None = None,
) -> dict:
    """Preserve source state and atomically point readers at a verified snapshot.

    Each SQLite database has its own consistent backup. The running source is
    never stopped, so this does not claim a transaction across separate databases
    and files. A failed run remains available for investigation and never becomes
    the latest archive. Browser state and package caches are explicitly excluded.
    """
    source = _canonical_path(Path(source_root or Path.home() / ".openclaw"))
    if not source.is_dir() or any(parent.is_symlink() for parent in (source, *source.parents)):
        raise ValueError("OpenClaw source must be an existing directory without symbolic links.")
    archive_root = _canonical_path(Path(local_store_root)) / "openclaw_archive"
    if archive_root.is_relative_to(source) or source.is_relative_to(archive_root):
        raise ValueError("Archive and OpenClaw source directories must not overlap.")
    _private_directory(archive_root)
    created_at = datetime.now(UTC)
    archive = archive_root / f"{created_at:%Y%m%dT%H%M%S.%fZ}-{uuid4().hex[:8]}"
    archive.mkdir(mode=0o700)
    state = archive / "state"
    state.mkdir(mode=0o700)
    manifest = {
        "schema_version": OPENCLAW_ARCHIVE_SCHEMA_VERSION,
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "source_root": str(source), "archive_directory": str(archive),
        "state_directory": str(state), "manifest_path": str(archive / "manifest.json"),
        "files": [], "symlinks": [],
        "exclusions": [], "warnings": [], "status": "incomplete",
        "consistency": "Per-database online SQLite backups and verified stable file copies; not a global transaction.",
    }
    inputs = []
    sqlite_inputs = set()
    try:
        for directory, names, filenames in os.walk(
            source, followlinks=False, onerror=_raise_walk_error,
        ):
            current = Path(directory)
            relative_directory = current.relative_to(source)
            target_directory = state / relative_directory
            _private_directory(target_directory)
            for name in sorted(names + filenames):
                path = current / name
                relative = path.relative_to(source)
                if path.is_symlink():
                    manifest["symlinks"].append({"path": str(relative), "target": os.readlink(path)})
                    if name in names:
                        names.remove(name)
                    continue
                reason = _EXCLUDED_ROOTS.get(name) if current == source else None
                if name == "node_modules":
                    reason = "Regenerable dependency tree."
                if reason:
                    manifest["exclusions"].append({"path": str(relative), "reason": reason})
                    if name in names:
                        names.remove(name)
                    continue
                if path.is_dir():
                    continue
                if not path.is_file():
                    manifest["warnings"].append({"path": str(relative), "reason": "Non-regular runtime entry was not copied."})
                    continue
                inputs.append(path)
                if _is_sqlite(path):
                    sqlite_inputs.add(path)
            names.sort()
        for path in sorted(inputs):
            relative = path.relative_to(source)
            sidecar = next((
                primary for suffix in ("-wal", "-shm", "-journal")
                if str(path).endswith(suffix)
                and (primary := Path(str(path)[:-len(suffix)])) in sqlite_inputs
            ), None)
            if sidecar is not None:
                manifest["exclusions"].append({
                    "path": str(relative),
                    "reason": "SQLite sidecar incorporated through online database backup.",
                })
                continue
            target = state / relative
            entry = _backup_sqlite(path, target) if path in sqlite_inputs else _copy_file(path, target)
            entry.update({"path": str(relative), "source_path": str(path)})
            manifest["files"].append(entry)
            if entry.get("quick_check", "ok") != "ok":
                manifest["warnings"].append({"path": str(relative), "reason": entry["quick_check"]})
        manifest["file_count"] = len(manifest["files"])
        manifest["total_bytes"] = sum(entry["size"] for entry in manifest["files"])
        manifest["sqlite_count"] = len(sqlite_inputs)
        manifest["status"] = "complete"
    except Exception as error:
        manifest["failure_type"] = type(error).__name__
        _write_json(archive / "manifest.json", manifest)
        raise
    manifest_path = archive / "manifest.json"
    _write_json(manifest_path, manifest)
    _write_json(archive_root / "latest.json", {
        "archive_directory": str(archive), "manifest_path": str(manifest_path),
    })
    return manifest
