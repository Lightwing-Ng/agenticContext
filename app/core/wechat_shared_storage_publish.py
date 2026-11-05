"""Stage reversible shared-body migrations and retain exact rollback databases.

Code version: v1.1.2-codex.0
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from .wechat_archive import _file_identity, wechat_archive_path, wechat_history_partition_fingerprint
from .wechat_export_merge import _clonefile
from .wechat_shared_storage import (
    analyze_shared_conversation_bodies,
    analyze_shared_conversation_scopes,
    analyze_shared_group_bodies,
    compact_shared_conversation_bodies,
    compact_shared_conversation_scopes,
    compact_shared_group_bodies,
)


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _identity(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("Shared-body publication requires regular local database files.")
    if getattr(path.stat(), "st_flags", 0) & 0x40000000:
        raise ValueError("Keep the database downloaded before shared-body publication.")
    for suffix in ("-wal", "-journal", "-shm"):
        journal = Path(str(path) + suffix)
        if journal.is_symlink() or journal.exists() and journal.stat().st_size:
            raise ValueError("Finish the SQLite writer before shared-body publication.")
    return _file_identity(path)


def _stage(path):
    descriptor, name = tempfile.mkstemp(prefix=".wechat-share-", suffix=".sqlite3", dir=path.parent)
    os.close(descriptor)
    result = Path(name)
    result.unlink()
    try:
        _clonefile(path, result)
        result.chmod(0o600)
    except BaseException:
        result.unlink(missing_ok=True)
        raise
    return result


def _sync(path):
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _storage_snapshot(path):
    state = path.stat()
    blocks = getattr(state, "st_blocks", None)
    return {
        "file_bytes": state.st_size,
        "allocated_bytes": blocks * 512 if blocks is not None else None,
    }


def _storage_report(before, after):
    """Keep file allocation distinct from net space while rollback clones remain."""
    return {
        "database_bytes_before": before["file_bytes"],
        "database_bytes_after": after["file_bytes"],
        "database_file_bytes_change": after["file_bytes"] - before["file_bytes"],
        "database_file_bytes_saved": max(before["file_bytes"] - after["file_bytes"], 0),
        "database_allocated_bytes_before": before["allocated_bytes"],
        "database_allocated_bytes_after": after["allocated_bytes"],
        # st_blocks can include APFS extents still shared with retained clones.
        "filesystem_bytes_reclaimed": 0,
    }


def _inside_directory(path, directory):
    """Fence rollback destinations by existing directory identity as well as spelling."""
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


def _publish_shared_bodies(
    local_store_root, scope, *, backup_directory, analyze, compact, scope_label
):
    """Publish only after exact backup verification and logical-row equality.

    Existing local sidecars are rebound only when their prior identity matches.
    Cloud-only and already stale indexes remain unchanged and explicitly reported.
    No VACUUM, archive deletion, media read, or cloud release occurs here.
    """
    target = wechat_archive_path(local_store_root).absolute()
    baseline = _identity(target)
    storage_before = _storage_snapshot(target)
    with sqlite3.connect(target.as_uri() + "?mode=ro", uri=True) as source:
        analysis = analyze(source, scope)
        fingerprint = wechat_history_partition_fingerprint(source)
    if not analysis["observations_to_rewrite"]:
        return {
            **analysis, "changed": False, "sidecars": {},
            **_storage_report(storage_before, _storage_snapshot(target)),
        }
    backup = Path(backup_directory).absolute()
    if _inside_directory(backup, local_store_root):
        raise ValueError("Keep rollback workstations outside the live local_store directory.")
    if backup.exists():
        raise ValueError("Use a new backup directory for each shared-body publication.")
    if backup.is_relative_to(target) or target.is_relative_to(backup):
        raise ValueError("The backup directory must be separate from the live database.")
    backup.mkdir(parents=True, mode=0o700)
    staged = {}
    originals = {target: baseline}
    sidecars = {}
    published = []
    receipt = {"version": "v1.1.2-codex.0", "at": datetime.now(UTC).isoformat(), "backups": {}}
    try:
        # clonefile creates copy-on-write rollback evidence, not a raw archive copy.
        saved = backup / target.name
        _clonefile(target, saved)
        saved.chmod(0o600)
        original_digest = _sha256(target)
        if _sha256(saved) != original_digest or _identity(target) != baseline:
            raise ValueError("The rollback database does not match the unchanged original.")
        _sync(saved)
        receipt["backups"][target.name] = original_digest
        staged[target] = _stage(target)
        with sqlite3.connect(staged[target]) as output:
            result = compact(output, scope)
            if result != analysis:
                raise ValueError(
                    f"The staged {scope_label} scope differs from its verified preflight."
                )
            output.commit()
            if output.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("The staged shared-body database failed its integrity check.")
        _sync(staged[target])
        replacement_identity = _file_identity(staged[target])
        for name in ("resource_references.sqlite3", "media_events.sqlite3"):
            path = target.with_name(name)
            if not path.exists():
                sidecars[name] = "absent; unchanged"
                continue
            if path.is_symlink() or getattr(path.stat(), "st_flags", 0) & 0x40000000:
                sidecars[name] = "cloud-only or linked; unchanged, not revalidated"
                continue
            identity = _identity(path)
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as source:
                info = dict(source.execute("SELECT key,value FROM archive_info"))
            if (
                info.get("schema_version") != "1"
                or json.loads(info.get("history_file_identity", "null")) != baseline
                or name == "resource_references.sqlite3"
                and info.get("history_partitions_sha256") != fingerprint
            ):
                sidecars[name] = "already stale; unchanged"
                continue
            originals[path] = identity
            saved = backup / name
            _clonefile(path, saved)
            digest = _sha256(path)
            if _sha256(saved) != digest or _identity(path) != identity:
                raise ValueError("A sidecar rollback database failed exact verification.")
            _sync(saved)
            receipt["backups"][name] = digest
            staged[path] = _stage(path)
            with sqlite3.connect(staged[path]) as output:
                output.execute(
                    "UPDATE archive_info SET value=? WHERE key='history_file_identity'",
                    (json.dumps(replacement_identity, sort_keys=True, separators=(",", ":")),),
                )
            _sync(staged[path])
            sidecars[name] = "rebound after exact logical-history verification"
        if any(_identity(path) != identity for path, identity in originals.items()):
            raise ValueError("A live database changed before shared-body publication.")
        receipt.update(
            analysis=analysis, sidecars=sidecars,
            storage=_storage_report(storage_before, _storage_snapshot(staged[target])),
        )
        receipt_path = backup / "receipt.json"
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
        receipt_path.chmod(0o600)
        _sync(receipt_path)
        _sync_directory(backup)
        _sync_directory(backup.parent)
        for path, temporary in staged.items():
            os.replace(temporary, path)
            published.append(path)
        _sync_directory(target.parent)
        # The original files stay in backup even after successful publication.
        return {
            **analysis, "changed": True, "sidecars": sidecars,
            "backup_directory": str(backup), "database_path": str(target),
            **_storage_report(storage_before, _storage_snapshot(target)),
        }
    except BaseException:
        # Restore the primary first: its new inode requires fresh rollback pins.
        if target in published:
            restored = _stage(backup / target.name)
            os.replace(restored, target)
        restored_identity = _file_identity(target)
        for path in published:
            if path == target:
                continue
            restored = _stage(backup / path.name)
            with sqlite3.connect(restored) as output:
                output.execute(
                    "UPDATE archive_info SET value=? WHERE key='history_file_identity'",
                    (json.dumps(restored_identity, sort_keys=True, separators=(",", ":")),),
                )
            os.replace(restored, path)
        # A sidecar whose replacement failed still has the old primary pin.
        if target in published:
            for path in staged:
                if path == target or path in published:
                    continue
                restored = _stage(backup / path.name)
                with sqlite3.connect(restored) as output:
                    output.execute(
                        "UPDATE archive_info SET value=? WHERE key='history_file_identity'",
                        (json.dumps(restored_identity, sort_keys=True, separators=(",", ":")),),
                    )
                os.replace(restored, path)
        _sync_directory(target.parent)
        raise
    finally:
        for path in staged.values():
            path.unlink(missing_ok=True)


def publish_shared_group_bodies(local_store_root, conversation_ids, *, backup_directory):
    """Preserve the original single-group publication interface."""
    return _publish_shared_bodies(
        local_store_root, conversation_ids, backup_directory=backup_directory,
        analyze=analyze_shared_group_bodies, compact=compact_shared_group_bodies,
        scope_label="group",
    )


def publish_shared_conversation_bodies(
    local_store_root, conversation_ids, *, backup_directory
):
    """Publish one verified group or reciprocal private conversation scope."""
    return _publish_shared_bodies(
        local_store_root, conversation_ids, backup_directory=backup_directory,
        analyze=analyze_shared_conversation_bodies,
        compact=compact_shared_conversation_bodies, scope_label="conversation",
    )


def publish_shared_conversation_scopes(local_store_root, scopes, *, backup_directory):
    """Publish disjoint verified scopes with one backup, stage, and integrity check."""
    return _publish_shared_bodies(
        local_store_root, scopes, backup_directory=backup_directory,
        analyze=analyze_shared_conversation_scopes,
        compact=compact_shared_conversation_scopes, scope_label="conversation",
    )
