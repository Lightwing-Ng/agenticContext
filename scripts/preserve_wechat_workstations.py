"""Relocate sealed, local workstation files without copying or downloading data.

Code version: v1.2.0-codex.0

The default invocation only inspects metadata and open handles. DATALESS files
remain in their original provider domain. This tool never releases local content
and never treats a completed rename as proof of cloud upload.
"""

from __future__ import annotations

import argparse
import ctypes
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile


# Existing preservation receipts retain their compatible format discriminator.
VERSION = "v1.0.0-codex.0"
DATALESS = 0x40000000
_WINDOWS_CLOUD_FLAGS = 0x1000 | 0x40000 | 0x400000
_WINDOWS_REPARSE_POINT = 0x400
DIRECTORIES = (
    "storage_backups", "repair_audits", "directory_audits", "imports",
    "recovery", "source_retirements", "contact_checkpoints",
)
RECEIPT = "preservation-receipt-v1.0.0.json"
LOCATOR = "workstation-locations-v1.0.0.json"
STAGE = re.compile(r"\.wechat-invite-(?:all|rebase)-[0-9TZ]+\.sqlite3(?:-(?:journal|wal|shm))?")
TEMPORARY = re.compile(r"\.(?:person-)?contact-analysis-[A-Za-z0-9_-]+\.sqlite3(?:-(?:journal|wal|shm))?")


def identity(path):
    state = path.lstat()
    blocks = getattr(state, "st_blocks", None)
    return {
        "device": state.st_dev, "inode": state.st_ino, "bytes": state.st_size,
        "mtime_ns": state.st_mtime_ns, "mode": state.st_mode,
        "uid": state.st_uid, "gid": state.st_gid, "links": state.st_nlink,
        "flags": getattr(state, "st_flags", 0),
        "file_attributes": getattr(state, "st_file_attributes", 0),
        "reparse_tag": getattr(state, "st_reparse_tag", 0),
        "allocated_bytes": blocks * 512 if blocks is not None else None,
    }


def _unavailable(value):
    return bool(
        value["flags"] & DATALESS
        or value.get("file_attributes", 0) & (_WINDOWS_CLOUD_FLAGS | _WINDOWS_REPARSE_POINT)
    )


def stable_identity(value):
    return {key: value[key] for key in (
        "device", "inode", "bytes", "mtime_ns", "mode", "uid", "gid", "links",
    )}


def assert_plain_path(path):
    # Inspect ancestors first so even metadata lookup cannot cross a junction.
    for component in reversed((path, *path.parents)):
        try:
            state = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(state.st_mode):
            raise ValueError(f"Symbolic links are not allowed: {component}")
        if stat.S_ISDIR(state.st_mode) and _unavailable({
            "flags": getattr(state, "st_flags", 0),
            "file_attributes": getattr(state, "st_file_attributes", 0),
        }):
            raise ValueError(f"An unavailable or reparse directory requires separate review: {component}")


def local_json(path):
    assert_plain_path(path)
    before = identity(path)
    if (
        not stat.S_ISREG(before["mode"])
        or _unavailable(before) or before["bytes"] > 4 * 1024**2
    ):
        raise ValueError(f"A bounded local JSON file is required: {path}")
    value = json.loads(path.read_bytes())
    if identity(path) != before:
        raise ValueError(f"JSON changed during inspection: {path}")
    return value


def runtime_pins(root):
    """Keep the observation registry's bounded source artifacts in managed storage."""
    registry = root / "text_observations.json"
    if not registry.exists():
        return set()
    value = local_json(registry)
    if value.get("schema_version") != 1 or not isinstance(value.get("observations"), list):
        raise ValueError("Unsupported runtime text-observation registry.")
    result = set()
    for observation in value["observations"]:
        relative = observation["source"]["path"]
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or path.parts[0] != "repair_audits":
            raise ValueError("A runtime observation has an unsupported source path.")
        assert_plain_path(root / path)
        result.add(path.as_posix())
    return result


def allowed(relative):
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        return False
    return (
        path.parts[0] in DIRECTORIES and len(path.parts) > 1
        or len(path.parts) == 1 and (
            path.name == "contacts 2.sqlite3" or STAGE.fullmatch(path.name) is not None
        )
        or len(path.parts) == 2 and path.parts[0] == "contact_activity"
        and TEMPORARY.fullmatch(path.name) is not None
    )


def _raise_walk_error(error):
    raise error


def candidates(root):
    assert_plain_path(root)
    pins = runtime_pins(root)
    paths = []
    deferred = {}

    def usable_directory(directory):
        try:
            metadata = identity(directory)
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(metadata["mode"]):
            raise ValueError(f"A workstation directory is a symbolic link: {directory}")
        if not stat.S_ISDIR(metadata["mode"]):
            raise ValueError(f"A workstation directory must be a directory: {directory}")
        if _unavailable(metadata):
            deferred[directory.relative_to(root).as_posix()] = {
                "source_identity": metadata, "defer_reason": "unavailable_directory_requires_review",
            }
            return False
        return True

    for name in DIRECTORIES:
        directory = root / name
        if not usable_directory(directory):
            continue
        for parent, directories, filenames in os.walk(directory, followlinks=False, onerror=_raise_walk_error):
            assert_plain_path(Path(parent))
            directories[:] = [child for child in directories if usable_directory(Path(parent) / child)]
            paths.extend(Path(parent) / filename for filename in filenames)
    paths.extend(path for path in root.iterdir() if allowed(path.name))
    activity = root / "contact_activity"
    if usable_directory(activity):
        paths.extend(path for path in activity.iterdir() if TEMPORARY.fullmatch(path.name))
    result = dict(deferred)
    for path in sorted(set(paths)):
        assert_plain_path(path.parent)
        relative = path.relative_to(root).as_posix()
        if not allowed(relative):
            raise ValueError(f"Candidate escaped the allowlist: {relative}")
        metadata = identity(path)
        reason = (
            "runtime_pinned_artifact" if relative in pins
            else "nonregular_file" if not stat.S_ISREG(metadata["mode"])
            else "multiple_hardlinks" if metadata["links"] != 1
            else "cloud_placeholder_requires_separate_preservation"
            if metadata["flags"] & DATALESS or metadata["file_attributes"] & _WINDOWS_CLOUD_FLAGS
            else "native_reparse_requires_separate_preservation"
            if metadata["file_attributes"] & _WINDOWS_REPARSE_POINT else None
        )
        result[relative] = {"source_identity": metadata, "defer_reason": reason}
    return result


def open_handles(paths):
    """Read structured lsof records; an inspection failure blocks relocation."""
    result = {}
    paths = list(paths)
    for start in range(0, len(paths), 80):
        batch = [str(path) for path in paths[start:start + 80]]
        if not batch:
            continue
        process = subprocess.run(
            ["lsof", "-nP", "-F0pcfaln", "--", *batch], capture_output=True,
        )
        if process.returncode not in (0, 1) or process.stderr:
            raise ValueError("Open-handle inspection failed: " + process.stderr.decode(errors="replace"))
        pid = command = None
        current = None
        for field in process.stdout.split(b"\0"):
            field = field.lstrip(b"\n")
            if not field:
                continue
            key, value = chr(field[0]), os.fsdecode(field[1:])
            if key == "p":
                pid, current = int(value), None
            elif key == "c":
                command = value
            elif key == "f":
                current = {"pid": pid, "process": command, "fd": value}
            elif key in ("a", "l") and current is not None:
                current[{"a": "access", "l": "lock"}[key]] = value
            elif key == "n" and current is not None:
                result.setdefault(value, []).append(current.copy())
    return result


def permitted_handles(handles, allow_fileprovider_rename):
    return not handles or allow_fileprovider_rename and all(
        handle["process"] == "fileproviderd" and not handle.get("lock", "").strip()
        for handle in handles
    )


def hash_local(path, expected):
    assert_plain_path(path)
    before = identity(path)
    if _unavailable(before) or stable_identity(before) != stable_identity(expected):
        raise ValueError(f"The sealed local file changed or became unavailable: {path}")
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
    )
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (
            getattr(opened, "st_flags", 0) & DATALESS
            or getattr(opened, "st_file_attributes", 0) & (_WINDOWS_CLOUD_FLAGS | _WINDOWS_REPARSE_POINT)
            or (opened.st_dev, opened.st_ino) != (before["device"], before["inode"])
        ):
            raise ValueError("File changed before checksum read.")
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if identity(path) != before:
        raise ValueError(f"File changed during checksum read: {path}")
    return digest


def atomic_json(path, value):
    assert_plain_path(path)
    descriptor, name = tempfile.mkstemp(prefix=".preservation-", suffix=".json", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def sync_directory(path):
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def rename_exclusive(source, target):
    """Require a same-volume exclusive rename; never overwrite or copy a target."""
    if sys.platform == "win32":
        # Windows os.rename fails atomically when the destination already exists.
        os.rename(source, target)
        return
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = library.renamex_np
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        result = rename(os.fsencode(source), os.fsencode(target), 4)
    elif sys.platform.startswith("linux"):
        rename = library.renameat2
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        result = rename(-100, os.fsencode(source), -100, os.fsencode(target), 1)
    else:
        raise ValueError("This platform has no supported exclusive-rename primitive.")
    if result:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), str(source), str(target))


def preserve(
    root, destination, *, apply=False, allow_fileprovider_rename=False,
    max_file_bytes=None,
):
    if max_file_bytes is not None and max_file_bytes < 0:
        raise ValueError("The maximum file size cannot be negative.")
    root, destination = Path(root).absolute(), Path(destination).absolute()
    assert_plain_path(root)
    assert_plain_path(destination)
    if destination == root or destination.is_relative_to(root) or root.is_relative_to(destination):
        raise ValueError("The workstation destination must be separate from runtime storage.")
    ancestor = destination
    while not ancestor.exists():
        ancestor = ancestor.parent
    if root.stat().st_dev != ancestor.stat().st_dev:
        raise ValueError("Cross-volume relocation requires a separate preservation workflow.")
    items = candidates(root)
    handles = open_handles(root / relative for relative in items)
    for relative, item in items.items():
        if (
            not item["defer_reason"] and max_file_bytes is not None
            and item["source_identity"]["bytes"] > max_file_bytes
        ):
            item["defer_reason"] = "requires_compressed_preservation"
        item["open_handles"] = handles.get(str(root / relative), [])
        if not item["defer_reason"] and not permitted_handles(item["open_handles"], allow_fileprovider_rename):
            item["defer_reason"] = "open_handle_requires_review"
    blocked_families = {
        re.sub(r"-(?:journal|wal|shm)$", "", relative)
        for relative, item in items.items()
        if item["defer_reason"]
    }
    for relative, item in items.items():
        if not item["defer_reason"] and re.sub(r"-(?:journal|wal|shm)$", "", relative) in blocked_families:
            item["defer_reason"] = "database_family_requires_review"
    report = {"version": VERSION, "source_root": str(root), "destination_root": str(destination),
              "dry_run": not apply, "cloud_upload_verified": False, "local_release_attempted": False,
              "max_file_bytes": max_file_bytes, "entries": items}
    report["deferred_subtrees"] = sorted(
        relative for relative, item in items.items()
        if item["defer_reason"] == "unavailable_directory_requires_review"
    )
    report["enumeration_complete"] = not report["deferred_subtrees"]
    if not apply:
        return report
    destination.mkdir(parents=True, mode=0o700, exist_ok=True)
    receipt_path = destination / RECEIPT
    receipt = local_json(receipt_path) if receipt_path.exists() else {
        "version": VERSION, "source_root": str(root), "destination_root": str(destination),
        "cloud_upload_verified": False, "local_release_attempted": False, "entries": {},
    }
    if any(receipt.get(key) != report[key] for key in ("version", "source_root", "destination_root")):
        raise ValueError("The existing receipt belongs to a different preservation operation.")

    def save():
        receipt["updated_utc"] = datetime.now(UTC).isoformat()
        atomic_json(receipt_path, receipt)
        atomic_json(root / LOCATOR, {
            "version": VERSION, "workstation_root": str(destination), "receipt": str(receipt_path),
            "cloud_upload_verified": False,
            "locations": {relative: {"path": str(destination / relative), "sha256": entry["sha256"]}
                          for relative, entry in receipt["entries"].items() if entry.get("status") == "moved_verified"},
        })

    for relative, entry in receipt["entries"].items():
        if not allowed(relative):
            raise ValueError("The existing receipt contains a path outside the allowlist.")
        target = destination / relative
        assert_plain_path(target)
        if entry.get("status") == "moved_verified":
            if stable_identity(identity(target)) != stable_identity(entry["destination_identity"]):
                raise ValueError("A previously preserved destination changed or disappeared.")
        if entry.get("status") == "sealed_before_rename" and not (root / relative).exists():
            if hash_local(target, entry["source_identity"]) != entry["sha256"]:
                raise ValueError("An interrupted rename does not match its sealed checksum.")
            entry.update(status="moved_verified", destination_identity=identity(target))
            save()
    receipt["deferred_entries"] = {
        relative: item for relative, item in items.items() if item["defer_reason"]
    }
    receipt["max_file_bytes"] = max_file_bytes
    save()
    for relative, item in items.items():
        if item["defer_reason"]:
            continue
        source, target = root / relative, destination / relative
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"An existing destination requires review: {target}")
        assert_plain_path(target)
        checksum = hash_local(source, item["source_identity"])
        current_handles = open_handles([source]).get(str(source), [])
        if not permitted_handles(current_handles, allow_fileprovider_rename):
            raise ValueError(f"A file acquired an active handle: {source}")
        if identity(source) != item["source_identity"]:
            raise ValueError(f"A candidate changed after planning: {source}")
        entry = {**item, "sha256": checksum, "status": "sealed_before_rename"}
        receipt["entries"][relative] = entry
        save()
        assert_plain_path(target.parent)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if source.stat().st_dev != target.parent.stat().st_dev:
            raise ValueError("The actual destination changed to a different volume.")
        assert_plain_path(source)
        assert_plain_path(target)
        rename_exclusive(source, target)
        sync_directory(source.parent)
        sync_directory(target.parent)
        if hash_local(target, item["source_identity"]) != checksum:
            raise ValueError(f"Destination checksum differs: {target}")
        entry.update(status="moved_verified", destination_identity=identity(target))
        save()
    report.update(entries=items, moved_entries=receipt["entries"], receipt=str(receipt_path))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-fileprovider-rename", action="store_true")
    parser.add_argument("--max-file-bytes", type=int)
    arguments = parser.parse_args()
    try:
        report = preserve(arguments.archive_root, arguments.destination, apply=arguments.apply,
                          allow_fileprovider_rename=arguments.allow_fileprovider_rename,
                          max_file_bytes=arguments.max_file_bytes)
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
