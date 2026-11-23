"""Load reviewed HTML group frames bound to retained source snapshot metadata.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import stat
import unicodedata
from urllib.parse import unquote

from .wechat_archive import wechat_archive_path
from .windows_file_metadata import file_change_time


VERSION = "v1.0.0-codex.0"
IS_WINDOWS = os.name == "nt"
MAX_REGISTRY_BYTES = 1024**2
MAX_SNAPSHOT_BYTES = 4 * 1024**2
MAX_FRAMES = 128
MAX_SELECTED_MEMBERS = 8192
MAX_FRAGMENTS = 512
_HASH = re.compile(r"[0-9a-f]{64}")
_FRAME_FIELDS = {
    "snapshot_numeric_id",
    "snapshot_id",
    "source_id",
    "source_metadata_sha256",
    "archive_sha256",
    "peer_id",
    "index_member",
    "main_html_member",
    "declared_data_path",
    "fragments",
}


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _identity(path, info, *, descriptor=None):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        file_change_time(path, descriptor=descriptor) if IS_WINDOWS else info.st_ctime_ns,
    )


def _parent_identity(info):
    # Sibling activity changes directory timestamps without redirecting the registry.
    return info.st_dev, info.st_ino, info.st_mode, getattr(info, "st_file_attributes", 0) & 0x400


def _registry_bytes(root):
    """Never materialize cloud-only evidence or follow an unreviewed path link."""
    path = wechat_archive_path(root).parent / "html_group_source_frames.json"
    try:
        parents = [(part, part.lstat()) for part in path.parents]
        if any(stat.S_ISLNK(info.st_mode) for _, info in parents):
            return "unavailable", b""
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or getattr(before, "st_flags", 0) & 0x40000000
        ):
            return "unavailable", b""
        if before.st_size > MAX_REGISTRY_BYTES:
            return "oversized", b""
        identity = _identity(path, before)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            if _identity(path, os.fstat(descriptor), descriptor=descriptor) != identity:
                return "changed", b""
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                raw = stream.read(MAX_REGISTRY_BYTES + 1)
            if (
                _identity(path, os.fstat(descriptor), descriptor=descriptor) != identity
                or _identity(path, path.lstat()) != identity
                or any(
                    _parent_identity(part.lstat()) != _parent_identity(info)
                    for part, info in parents
                )
            ):
                return "changed", b""
        finally:
            os.close(descriptor)
        if len(raw) != before.st_size or len(raw) > MAX_REGISTRY_BYTES:
            return "changed", b""
        return "present", raw
    except FileNotFoundError:
        return "missing", b""
    except OSError:
        return "unavailable", b""


def group_frame_registry_digest(root):
    """Fingerprint the exact local registry and its unusable or absent states."""
    status, raw = _registry_bytes(root)
    return _sha(json.dumps([VERSION, status, _sha(raw)]).encode())


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate HTML group frame field.")
        result[key] = value
    return result


def _path(value, *, decode=False):
    if not isinstance(value, str) or not value or "\0" in value or "\\" in value:
        raise ValueError("Invalid archive member path.")
    value = unicodedata.normalize("NFC", unquote(value) if decode else value)
    if "\0" in value or "\\" in value or any(ord(char) < 32 for char in value):
        raise ValueError("Invalid encoded archive member path.")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value or value == ".":
        raise ValueError("Unsafe or ambiguous archive member path.")
    return value


def _pin(value):
    if not isinstance(value, dict) or set(value) != {"member_path", "sha256"}:
        raise ValueError("Invalid archive member pin.")
    path = _path(value["member_path"])
    digest = value["sha256"]
    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
        raise ValueError("Invalid archive member digest.")
    return path, digest


def _selected_members(metadata):
    selected = metadata.get("selected_text_members")
    if not isinstance(selected, list) or len(selected) > MAX_SELECTED_MEMBERS:
        raise ValueError("Missing or oversized imported member inventory.")
    pins = {}
    for value in selected:
        if not isinstance(value, dict):
            raise ValueError("Invalid imported member inventory.")
        path, digest = _pin({key: value.get(key) for key in ("member_path", "sha256")})
        if path in pins:
            raise ValueError("Ambiguous imported member path.")
        pins[path] = digest
    return pins


def _frame(entry, snapshots):
    if not isinstance(entry, dict) or set(entry) != _FRAME_FIELDS:
        raise ValueError("Invalid HTML group frame entry.")
    numeric = entry["snapshot_numeric_id"]
    peer = entry["peer_id"]
    if (
        type(numeric) is not int
        or numeric <= 0
        or not isinstance(peer, str)
        or not peer.endswith(("@chatroom", "@im.chatroom"))
        or not peer.partition("@")[0]
        or any(char.isspace() or char in "\0/\\" for char in peer)
    ):
        raise ValueError("Invalid snapshot or literal group identity.")
    for key in ("snapshot_id", "source_metadata_sha256", "archive_sha256"):
        if not isinstance(entry[key], str) or not _HASH.fullmatch(entry[key]):
            raise ValueError("Invalid HTML group frame digest.")
    snapshot = snapshots.get(numeric)
    if snapshot is None:
        raise ValueError("The pinned source snapshot is unavailable.")
    raw = snapshot["metadata"]
    if (
        not isinstance(raw, str)
        or len(raw.encode()) > MAX_SNAPSHOT_BYTES
        or _sha(raw.encode()) != entry["source_metadata_sha256"]
        or type(snapshot["id"]) is not int
        or snapshot["id"] != numeric
        or snapshot["snapshot_id"] != entry["snapshot_id"]
        or snapshot["source_id"] != entry["source_id"]
        or entry["snapshot_id"] != entry["archive_sha256"]
        or entry["source_id"] != "html-" + entry["archive_sha256"][:24]
    ):
        raise ValueError("The source snapshot changed its pinned identity.")
    metadata = json.loads(raw, object_pairs_hook=_unique_fields)
    if (
        not isinstance(metadata, dict)
        or metadata.get("source_id") != entry["source_id"]
        or metadata.get("source_sha256") != entry["archive_sha256"]
        or metadata.get("owner_verified") is not True
        or not isinstance(metadata.get("source_account_id"), str)
        or not metadata["source_account_id"]
        or not isinstance(metadata.get("owner_evidence"), dict)
        or metadata["owner_evidence"].get("native_username")
        != metadata["source_account_id"]
    ):
        raise ValueError("The imported source owner or archive is not verified.")
    selected = _selected_members(metadata)
    index, index_hash = _pin(entry["index_member"])
    main, main_hash = _pin(entry["main_html_member"])
    if (
        PurePosixPath(index).name != "index.html"
        or PurePosixPath(index).parent != PurePosixPath(main).parent
        or index == main
        or not main.endswith(".html")
        or selected.get(index) != index_hash
        or selected.get(main) != main_hash
    ):
        raise ValueError("The source index or main HTML member changed.")
    catalog = metadata.get("source_conversation_catalog")
    if not isinstance(catalog, list) or len(catalog) > MAX_SELECTED_MEMBERS:
        raise ValueError("The imported source conversation catalog is unavailable.")
    matches = []
    for row in catalog:
        if not isinstance(row, dict):
            raise ValueError("Invalid imported source catalog entry.")
        member = row.get("member_path")
        if row.get("index_avatar_native_id") == peer or (
            isinstance(member, str) and unicodedata.normalize("NFC", member) == main
        ):
            matches.append(row)
    if (
        len(matches) != 1
        or matches[0].get("conversation_kind") != "group"
        or matches[0].get("index_avatar_native_id") != peer
        or _path(matches[0].get("member_path")) != main
    ):
        raise ValueError("The imported group index binding is ambiguous.")
    declared = _path(entry["declared_data_path"], decode=True)
    data_root = PurePosixPath(main).parent / PurePosixPath(declared)
    fragments = entry["fragments"]
    if not isinstance(fragments, list) or not 0 < len(fragments) <= MAX_FRAGMENTS:
        raise ValueError("Missing or oversized source fragment pins.")
    pins = {}
    for value in fragments:
        path, digest = _pin(value)
        member = PurePosixPath(path)
        if (
            path in pins
            or member.suffix != ".js"
            or not member.is_relative_to(data_root)
            or selected.get(path) != digest
        ):
            raise ValueError("The source fragment changed its declared binding.")
        pins[path] = digest
    selected_fragments = {
        path: digest
        for path, digest in selected.items()
        if PurePosixPath(path).is_relative_to(data_root) and path.endswith(".js")
    }
    if pins != selected_fragments:
        raise ValueError("The declared source fragment inventory is incomplete.")
    return (numeric, peer), {
        "index_member_path": main,
        "physical_member_path": main,
        "main_html_sha256": main_hash,
        "declared_data_path": entry["declared_data_path"],
        "archive_sha256": entry["archive_sha256"],
        "selected_fragment_sha256": pins,
        "source_index_member_path": index,
        "source_index_sha256": index_hash,
    }


def load_group_source_frames(root, snapshots):
    """Validate reviewed frames against exact metadata already loaded by the caller.

    This registry supplements evidence omitted by an older importer. It never
    changes source metadata, establishes an account owner, or identifies events.
    Every invalid entry invalidates the complete registry instead of selecting
    a usable subset from contradictory evidence.
    """
    status, raw = _registry_bytes(root)
    if status != "present" or not isinstance(snapshots, dict):
        return {}
    try:
        value = json.loads(raw, object_pairs_hook=_unique_fields)
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "frames"}
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or not isinstance(value["frames"], list)
            or len(value["frames"]) > MAX_FRAMES
        ):
            return {}
        result = {}
        for entry in value["frames"]:
            key, frame = _frame(entry, snapshots)
            if key in result:
                return {}
            result[key] = frame
        return result
    except (ValueError, TypeError, KeyError, IndexError, UnicodeError, RecursionError):
        return {}
