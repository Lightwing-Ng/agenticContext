"""Preserve, verify, and restore a complete official export without changing its source.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile


VERSION = "v1.0.1-codex.0"
ARCHIVE = "official-export.zip"
MANIFEST = "source-manifest.v1.json"
VERIFICATION = "verification.v1.json"
CHECKSUMS = "SHA256SUMS"
ARTIFACTS = {ARCHIVE, MANIFEST, VERIFICATION}


def _hash_file(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _mac_xattr(path: Path, name: str | None = None, value: bytes | None = None):
    """Use Darwin's native xattr ABI when Python lacks os.listxattr."""
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    encoded = os.fsencode(path)
    if name is None:
        function = libc.listxattr
        function.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
        function.restype = ctypes.c_ssize_t
        size = function(encoded, None, 0, 1)
        if size < 0:
            raise OSError(ctypes.get_errno(), "Cannot list extended attributes")
        buffer = ctypes.create_string_buffer(max(size, 1))
        actual = function(encoded, buffer, size, 1)
        if actual != size:
            raise RuntimeError("Extended attributes changed while reading")
        return [part.decode() for part in buffer.raw[:actual].split(b"\0") if part]
    if value is None:
        function = libc.getxattr
        function.argtypes = [
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
            ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int,
        ]
        function.restype = ctypes.c_ssize_t
        size = function(encoded, name.encode(), None, 0, 0, 1)
        if size < 0:
            raise OSError(ctypes.get_errno(), "Cannot read extended attribute")
        buffer = ctypes.create_string_buffer(max(size, 1))
        actual = function(encoded, name.encode(), buffer, size, 0, 1)
        if actual != size:
            raise RuntimeError("Extended attribute changed while reading")
        return buffer.raw[:actual]
    function = libc.setxattr
    function.argtypes = [
        ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int,
    ]
    function.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(value, len(value) + 1)
    if function(encoded, name.encode(), buffer, len(value), 0, 1) != 0:
        raise OSError(ctypes.get_errno(), "Cannot restore extended attribute")


def _attributes(path: Path) -> dict[str, str]:
    if hasattr(os, "listxattr"):
        return {
            name: base64.b64encode(os.getxattr(path, name, follow_symlinks=False)).decode("ascii")
            for name in sorted(os.listxattr(path, follow_symlinks=False))
        }
    if sys.platform == "darwin":
        return {
            name: base64.b64encode(_mac_xattr(path, name)).decode("ascii")
            for name in sorted(_mac_xattr(path))
        }
    return {}


def _safe_path(value: str, *, allow_root: bool = False) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise ValueError("Unsafe archive path")
    path = PurePosixPath(value)
    if path.is_absolute() or PureWindowsPath(value).drive or ".." in path.parts:
        raise ValueError("Unsafe archive path")
    normalized = path.as_posix()
    if normalized == "." and not allow_root:
        raise ValueError("Empty archive path")
    if normalized != (value[:-1] if value.endswith("/") else value):
        raise ValueError("Noncanonical archive path")
    return normalized


def _signature(value: os.stat_result) -> tuple:
    return (
        value.st_dev, value.st_ino, value.st_mode, value.st_size,
        value.st_mtime_ns, value.st_ctime_ns,
    )


def _source_paths(root: Path):
    """Propagate unreadable-directory errors instead of silently skipping entries."""
    yield root
    for child in sorted(root.iterdir()):
        if stat.S_ISDIR(child.lstat().st_mode):
            yield from _source_paths(child)
        else:
            yield child


def _snapshot(root: Path) -> dict:
    files = []
    directories = []
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Source must be a real directory")
    for path in _source_paths(root):
        before = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        _safe_path(relative, allow_root=path == root)
        if not (stat.S_ISREG(before.st_mode) or stat.S_ISDIR(before.st_mode)):
            raise ValueError("Source contains a symbolic link or special file")
        entry = {
            "path": relative,
            "mode": stat.S_IMODE(before.st_mode),
            "mtime_ns": before.st_mtime_ns,
            "xattrs_base64": _attributes(path),
        }
        if stat.S_ISREG(before.st_mode):
            entry.update(size=before.st_size, sha256=_hash_file(path))
            files.append(entry)
        else:
            directories.append(entry)
        if _signature(before) != _signature(path.lstat()):
            raise RuntimeError("Source changed during inventory")
    return {"files": files, "directories": directories}


def _write_json(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _read_manifest(path: Path) -> dict:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    root = _safe_path(manifest["archive_root"])
    if "/" in root or root == "__MACOSX":
        raise ValueError("Invalid archive root")
    seen = set()
    for kind in ("files", "directories"):
        for entry in manifest[kind]:
            relative = _safe_path(entry["path"], allow_root=kind == "directories")
            if relative in seen:
                raise ValueError("Duplicate manifest path")
            seen.add(relative)
            if not isinstance(entry["mode"], int) or not 0 <= entry["mode"] <= 0o7777:
                raise ValueError("Invalid manifest mode")
            if not isinstance(entry["mtime_ns"], int):
                raise ValueError("Invalid manifest timestamp")
            for name, value in entry["xattrs_base64"].items():
                if not isinstance(name, str) or not name or "\0" in name:
                    raise ValueError("Invalid extended attribute name")
                base64.b64decode(value, validate=True)
            if kind == "files":
                if not isinstance(entry["size"], int) or entry["size"] < 0:
                    raise ValueError("Invalid manifest file size")
                if len(entry["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in entry["sha256"]):
                    raise ValueError("Invalid manifest file hash")
    directories = {entry["path"] for entry in manifest["directories"]}
    if "." not in directories:
        raise ValueError("Manifest root directory is missing")
    for relative in seen - {"."}:
        parent = PurePosixPath(relative).parent.as_posix()
        if parent not in directories:
            raise ValueError("Manifest parent directory is missing")
    return manifest


def _use_native_zip() -> bool:
    return sys.platform == "darwin"


def _create_zip(source: Path, path: Path, manifest: dict) -> None:
    if _use_native_zip():
        subprocess.run(
            ["/usr/bin/ditto", "-c", "-k", "--keepParent", "--sequesterRsrc",
             "--rsrc", "--extattr", "--qtn", "--acl", str(source), str(path)],
            check=True, capture_output=True,
        )
        return
    with zipfile.ZipFile(
        path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6, strict_timestamps=False
    ) as zipped:
        root = manifest["archive_root"]
        for entry in manifest["directories"]:
            relative = "" if entry["path"] == "." else entry["path"] + "/"
            info = zipfile.ZipInfo(root + "/" + relative)
            info.external_attr = (stat.S_IFDIR | entry["mode"]) << 16
            zipped.writestr(info, b"")
        for entry in manifest["files"]:
            zipped.write(source / entry["path"], root + "/" + entry["path"])


def _verify_zip(path: Path, manifest: dict) -> dict:
    expected = {entry["path"]: entry for entry in manifest["files"]}
    directories = {entry["path"] for entry in manifest["directories"]}
    files_seen = set()
    directories_seen = set()
    metadata_members = 0
    prefix = manifest["archive_root"] + "/"
    with zipfile.ZipFile(path, metadata_encoding="utf-8") as zipped:
        names = [entry.filename for entry in zipped.infolist()]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate ZIP member")
        for entry in zipped.infolist():
            _safe_path(entry.filename)
            member_type = stat.S_IFMT(entry.external_attr >> 16)
            if member_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise ValueError("ZIP contains a symbolic link or special member")
            metadata = entry.filename == "__MACOSX/" or entry.filename.startswith("__MACOSX/")
            if not metadata and not entry.filename.startswith(prefix):
                raise ValueError("Unexpected ZIP root")
            relative = entry.filename[len(prefix):].rstrip("/") or "."
            if not metadata:
                if entry.is_dir():
                    if relative not in directories:
                        raise ValueError("Unexpected ZIP directory")
                    directories_seen.add(relative)
                elif relative not in expected or entry.file_size != expected[relative]["size"]:
                    raise ValueError("Unexpected ZIP file or size")
            with zipped.open(entry) as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            if metadata:
                metadata_members += 1
            elif not entry.is_dir():
                if digest != expected[relative]["sha256"]:
                    raise ValueError("ZIP content hash mismatch")
                files_seen.add(relative)
    if files_seen != set(expected) or directories_seen != directories:
        raise ValueError("ZIP file or directory coverage differs from manifest")
    return {"zip_files_verified": len(files_seen), "zip_metadata_members_verified": metadata_members}


def _restore_metadata(path: Path, entry: dict) -> int:
    reapplied = 0
    actual = _attributes(path)
    for name, encoded in entry["xattrs_base64"].items():
        if actual.get(name) == encoded:
            continue
        value = base64.b64decode(encoded, validate=True)
        if hasattr(os, "setxattr"):
            os.setxattr(path, name, value, follow_symlinks=False)
        elif sys.platform == "darwin":
            _mac_xattr(path, name, value)
        else:
            raise RuntimeError("This host cannot restore original extended attributes")
        reapplied += 1
    actual = _attributes(path)
    if any(actual.get(name) != value for name, value in entry["xattrs_base64"].items()):
        raise RuntimeError("Restored extended attribute mismatch")
    current = path.stat()
    os.utime(path, ns=(current.st_atime_ns, entry["mtime_ns"]), follow_symlinks=False)
    if path.stat().st_mtime_ns != entry["mtime_ns"]:
        raise RuntimeError("Restored modification time mismatch")
    return reapplied


def _restore_zip(path: Path, manifest: dict, destination: Path) -> dict:
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Restore destination already exists")
    destination.mkdir(mode=0o700)
    root = destination / manifest["archive_root"]
    native = _use_native_zip()
    if native:
        subprocess.run(
            ["/usr/bin/ditto", "-x", "-k", "--rsrc", "--extattr", "--qtn", "--acl",
             str(path), str(destination)], check=True, capture_output=True,
        )
    else:
        for entry in manifest["directories"]:
            target = root if entry["path"] == "." else root / entry["path"]
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
        with zipfile.ZipFile(path, metadata_encoding="utf-8") as zipped:
            for entry in manifest["files"]:
                target = root / entry["path"]
                with zipped.open(manifest["archive_root"] + "/" + entry["path"]) as source:
                    with target.open("xb") as output:
                        shutil.copyfileobj(source, output)
    actual = _snapshot(root)
    expected_files = {entry["path"]: entry for entry in manifest["files"]}
    actual_files = {entry["path"]: entry for entry in actual["files"]}
    if set(expected_files) != set(actual_files):
        raise RuntimeError("Restored file coverage differs")
    if {entry["path"] for entry in actual["directories"]} != {entry["path"] for entry in manifest["directories"]}:
        raise RuntimeError("Restored directory coverage differs")
    for relative, entry in expected_files.items():
        if any(actual_files[relative][key] != entry[key] for key in ("size", "sha256")):
            raise RuntimeError("Restored file content mismatch")
    reapplied = 0
    entries = [*manifest["files"], *sorted(manifest["directories"], key=lambda item: len(item["path"]), reverse=True)]
    for entry in entries:
        target = root if entry["path"] == "." else root / entry["path"]
        reapplied += _restore_metadata(target, entry)
        if not native:
            os.chmod(target, entry["mode"])
        if stat.S_IMODE(target.stat().st_mode) != entry["mode"]:
            raise RuntimeError("Restored file permission mismatch")
    return {"restored_files_verified": len(expected_files), "original_attributes_reapplied": reapplied}


def _write_checksums(directory: Path) -> None:
    with (directory / CHECKSUMS).open("x", encoding="utf-8") as handle:
        os.chmod(directory / CHECKSUMS, 0o600)
        for name in sorted(ARTIFACTS):
            handle.write(f"{_hash_file(directory / name)}  {name}\n")
        handle.flush()
        os.fsync(handle.fileno())


def _sync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in {errno.EINVAL, errno.ENOTSUP}:
                raise
    finally:
        os.close(descriptor)


def _check_checksums(directory: Path) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Archive must be a real directory")
    if {entry.name for entry in directory.iterdir()} != ARTIFACTS | {CHECKSUMS}:
        raise ValueError("Incomplete archive directory or unexpected artifact")
    for name in ARTIFACTS | {CHECKSUMS}:
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Archive artifact is not a regular file")
    expected = {}
    for line in (directory / CHECKSUMS).read_text(encoding="utf-8").splitlines():
        digest, separator, name = line.partition("  ")
        if not separator or name not in ARTIFACTS or name in expected:
            raise ValueError("Invalid checksum list")
        expected[name] = digest
    if set(expected) != ARTIFACTS:
        raise ValueError("Incomplete checksum list")
    for name in ARTIFACTS:
        if _hash_file(directory / name) != expected[name]:
            raise ValueError("Archive artifact checksum mismatch")
    report = json.loads((directory / VERIFICATION).read_text(encoding="utf-8"))
    if not report.get("verified") or not report.get("ready_to_delete_source"):
        raise ValueError("Archive was not successfully completed")


def _summary(manifest: dict) -> dict:
    return {
        "version": VERSION, "verified": True,
        "file_count": len(manifest["files"]),
        "directory_count_excluding_root": len(manifest["directories"]) - 1,
        "total_file_bytes": sum(entry["size"] for entry in manifest["files"]),
    }


def create_archive(source: Path, destination: Path) -> dict:
    """Create a complete private archive after a real restoration and source recheck."""
    source = Path(source)
    destination = Path(destination)
    if source.is_symlink() or not source.is_dir():
        raise ValueError("Source must be a real directory")
    source = source.resolve()
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Archive destination already exists")
    if destination.resolve().is_relative_to(source):
        raise ValueError("Archive destination must be outside the source")
    _safe_path(source.name)
    if source.name == "__MACOSX":
        raise ValueError("Source name conflicts with ZIP metadata directory")
    baseline = _snapshot(source)
    manifest = {
        "version": VERSION, "archive_root": source.name,
        "created_at": datetime.now(timezone.utc).isoformat(), **baseline,
    }
    destination.mkdir(mode=0o700)
    _write_json(destination / MANIFEST, manifest)
    staged = destination / (ARCHIVE + ".partial")
    _create_zip(source, staged, manifest)
    os.chmod(staged, 0o600)
    zip_report = _verify_zip(staged, manifest)
    with tempfile.TemporaryDirectory(prefix="official-export-restore-") as temporary:
        restore_report = _restore_zip(staged, manifest, Path(temporary) / "restored")
    if _snapshot(source) != baseline:
        raise RuntimeError("Source changed after archive creation; do not delete it")
    archive = destination / ARCHIVE
    os.link(staged, archive)
    staged.unlink()
    with archive.open("rb") as handle:
        os.fsync(handle.fileno())
    report = {
        **_summary(manifest), **zip_report, **restore_report,
        "archive_sha256": _hash_file(archive),
        "source_unchanged": True, "ready_to_delete_source": True,
        "source_deleted": False,
        "acl_policy": "macOS ditto preserves ACLs; ACLs are not independently compared.",
    }
    _write_json(destination / VERIFICATION, report)
    _write_checksums(destination)
    _check_checksums(destination)
    _sync_directory(destination)
    return report


def verify_archive(archive_directory: Path) -> dict:
    """Read every artifact and ZIP member without changing the retained archive."""
    directory = Path(archive_directory)
    _check_checksums(directory)
    manifest = _read_manifest(directory / MANIFEST)
    return {**_summary(manifest), **_verify_zip(directory / ARCHIVE, manifest)}


def restore_archive(archive_directory: Path, destination: Path) -> dict:
    """Verify first, then restore and check only a new destination directory."""
    directory = Path(archive_directory)
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Restore destination already exists")
    if destination.resolve().is_relative_to(directory.resolve()):
        raise ValueError("Restore destination must be outside the archive")
    result = verify_archive(directory)
    manifest = _read_manifest(directory / MANIFEST)
    return {**result, **_restore_zip(directory / ARCHIVE, manifest, destination)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="Create and fully restore-test an export archive")
    create.add_argument("source", type=Path)
    create.add_argument("destination", type=Path)
    verify = commands.add_parser("verify", help="Verify all retained artifacts and ZIP file hashes")
    verify.add_argument("archive_directory", type=Path)
    restore = commands.add_parser("restore", help="Verify and restore into a new directory")
    restore.add_argument("archive_directory", type=Path)
    restore.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            result = create_archive(args.source, args.destination)
        elif args.command == "verify":
            result = verify_archive(args.archive_directory)
        else:
            result = restore_archive(args.archive_directory, args.destination)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, zipfile.BadZipFile, subprocess.SubprocessError):
        print("Archive operation failed; retain the source and inspect the incomplete destination.", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
