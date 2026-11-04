"""Inspect one explicit OneDrive workstation without downloading or evicting payloads.

Code version: v1.1.1-codex.0
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime
import json
import os
from pathlib import Path
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import archive_wechat_rollbacks as archiver


VERSION = "v1.1.1-codex.0"
PUBLIC_ROOT = Path.home() / "Library/CloudStorage/OneDrive-Personal"
CACHE_ROOT = Path.home() / "Library/Group Containers/UBF8T346G9.OneDriveSyncClientSuite/OneDrive.noindex/OneDrive"
TRANSACTION_LIMIT = 65_536


def _metadata(path):
    value = path.lstat()
    return {
        "device": value.st_dev,
        "inode": value.st_ino,
        "bytes": value.st_size,
        "allocated_bytes": value.st_blocks * 512,
        "mtime_ns": value.st_mtime_ns,
        "dataless": bool(getattr(value, "st_flags", 0) & archiver.DATALESS),
        "kind": "file" if stat.S_ISREG(value.st_mode) else "other",
    }


def _same_identity(before, after):
    return all(before.get(key) == after.get(key) for key in ("device", "inode", "bytes", "mtime_ns", "kind"))


def canonical_cache_path(path):
    """Map only the currently verified Personal sync-root prefix, without resolving links."""
    path = Path(path).absolute()
    return CACHE_ROOT / path.relative_to(PUBLIC_ROOT)


def _cache_metadata(path):
    cached = canonical_cache_path(path)
    try:
        return {"path": str(cached), "exists": True, **_metadata(cached)}
    except FileNotFoundError:
        return {"path": str(cached), "exists": False}


def _enumerate(root, report):
    records = {}
    errors = []
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    if path == report:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(path)
                    else:
                        try:
                            records[str(path)] = _metadata(path)
                        except OSError as error:
                            errors.append({"path": str(path), "error_type": type(error).__name__})
        except OSError as error:
            errors.append({"path": str(directory), "error_type": type(error).__name__})
    return records, errors


def _in_progress_reason(path, before):
    if path.name.endswith(".pending.json"):
        if before["dataless"] or before["bytes"] > TRANSACTION_LIMIT:
            return "transaction_state_unavailable_without_downloading_payloads"
        # This bounded control receipt is the only document body this inspector reads.
        try:
            with path.open("rb") as stream:
                data = stream.read(TRANSACTION_LIMIT + 1)
            if len(data) > TRANSACTION_LIMIT:
                return "transaction_control_exceeds_size_limit"
            transaction = json.loads(data)
            if not isinstance(transaction, dict):
                return "transaction_state_unverified"
            if transaction.get("status") != "completed_receipt_published":
                return "transaction_not_completed"
            for key in ("archive", "receipt"):
                final = Path(transaction.get(key, ""))
                if not final.is_absolute() or final.parent != path.parent or _metadata(final)["kind"] != "file":
                    return "transaction_final_artifact_or_receipt_unavailable"
            if not _same_identity(before, _metadata(path)):
                return "transaction_changed_during_inspection"
        except (OSError, ValueError, TypeError):
            return "transaction_state_unverified"
    elif path.name.endswith(".pending"):
        return "pending_artifact_still_in_progress"
    return None


def _inspect_file(path, before):
    row = {
        "path": str(path), "before": before,
        "cloud_preservation_verified": False, "safe_to_release_local_content": False,
    }
    try:
        row["cache_before"] = _cache_metadata(path)
        if before["kind"] != "file":
            row.update(status="excluded", reason="nonregular_item_requires_review")
            return row
        reason = _in_progress_reason(path, before)
        if reason:
            row.update(status="excluded_in_progress", reason=reason)
            return row
        provider = archiver._provider_state(path)
        after = _metadata(path)
        stable = _same_identity(before, after)
        uploaded = (
            stable
            and before["bytes"] == provider["documentSize"]
            and provider["isUploaded"] == 1
            and all(provider[key] == 0 for key in archiver.UPLOAD_FIELDS if key != "isUploaded")
            and not provider["upload_error_present"]
        )
        row.update(
            status="provider_upload_reported_cloud_unverified" if uploaded else "pending_or_unverified",
            provider_upload_reported=uploaded,
            provider=provider,
            after=after,
            identity_stable=stable,
            size_matches_provider=before["bytes"] == provider["documentSize"],
        )
    except (OSError, ValueError, KeyError, archiver.subprocess.SubprocessError) as error:
        row.update(status="pending_or_unverified", error_type=type(error).__name__)
    finally:
        try:
            row["cache_after"] = _cache_metadata(path)
        except OSError as error:
            row["cache_inspection_error"] = type(error).__name__
    return row


def _allocation_summary(metadata):
    files = [value for value in metadata if value.get("kind") == "file"]
    unique = {(value["device"], value["inode"]): value for value in files}
    return {
        "files": len(files),
        "logical_bytes": sum(value["bytes"] for value in files),
        "allocated_bytes": sum(value["allocated_bytes"] for value in files),
        "unique_inode_allocated_bytes": sum(value["allocated_bytes"] for value in unique.values()),
        "dataless_files": sum(value["dataless"] for value in files),
    }


def inspect_workspace(root, report, *, max_workers=4):
    root = Path(root).expanduser().absolute()
    report = Path(report).expanduser().absolute()
    if not 1 <= max_workers <= 4:
        raise ValueError("Use between one and four metadata workers.")
    canonical_cache_path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Select an existing, explicit workstation directory.")
    if os.path.lexists(report) or not report.parent.is_dir():
        raise ValueError("Select a new report path in an existing directory.")
    started = datetime.datetime.now(datetime.UTC).isoformat()
    before, errors = _enumerate(root, report)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        records = list(executor.map(lambda item: _inspect_file(Path(item[0]), item[1]), sorted(before.items())))
    after, final_errors = _enumerate(root, report)
    added = sorted(after.keys() - before.keys())
    removed = sorted(before.keys() - after.keys())
    changed = sorted(path for path in before.keys() & after.keys() if not _same_identity(before[path], after[path]))
    namespace_stable = not (added or removed or changed or errors or final_errors)
    uploaded = sum(row["status"] == "provider_upload_reported_cloud_unverified" for row in records)
    pending = sum(row["status"] == "pending_or_unverified" for row in records)
    in_progress = sum(row["status"] == "excluded_in_progress" for row in records)
    excluded = sum(row["status"].startswith("excluded") for row in records)
    complete = bool(records) and namespace_stable and uploaded == len(records)
    return {
        "version": VERSION,
        "started_utc": started,
        "finished_utc": datetime.datetime.now(datetime.UTC).isoformat(),
        "root": str(root),
        "status": "all_provider_uploads_reported_cloud_unverified" if complete else "provider_uploads_pending_or_unverified",
        "all_provider_uploads_reported": complete,
        # Preserve a fail-closed value for callers of the legacy completion field.
        "all_scanned_files_uploaded": False,
        "cloud_preservation_verified": False,
        "safe_to_release_local_content": False,
        "totals": {
            "observed_items": len(records),
            "regular_files": sum(row["before"]["kind"] == "file" for row in records),
            "provider_reported_uploaded_files": uploaded,
            "pending_or_unverified_files": pending,
            "in_progress_files": in_progress,
            "excluded_items": excluded,
        },
        "public_allocation_after": _allocation_summary(after.values()),
        "canonical_cache_allocation_after": _allocation_summary(row.get("cache_after", {}) for row in records),
        "canonical_cache_missing_files": sum(not row.get("cache_after", {}).get("exists", False) for row in records),
        "canonical_cache_mapping": {"public_prefix": str(PUBLIC_ROOT), "cache_prefix": str(CACHE_ROOT)},
        "namespace": {"stable": namespace_stable, "added": added, "removed": removed, "changed": changed},
        "enumeration_errors": errors + final_errors,
        "records": records,
        "report_output_excluded": str(report),
        "report_requires_separate_remote_verification": True,
        "payload_bytes_read": 0,
        "control_read_policy": "Only resident transaction JSON files of at most 65,536 bytes may be read.",
        "remote_content_hash_verified": False,
        "eviction_performed": False,
        "source_files_deleted": False,
        "verification_boundary": "Preliminary local File Provider metadata can conflict with remote sync state; independent remote evidence is required before source retirement or local content release.",
        "allocation_boundary": "Metadata allocation per inode; APFS shared extents and physical free space are not inferred.",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("root", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--max-workers", type=int, default=4)
    arguments = parser.parse_args(argv)
    try:
        result = inspect_workspace(arguments.root, arguments.report, max_workers=arguments.max_workers)
        archiver._write_json(arguments.report.expanduser().absolute(), result, exclusive=True)
    except (OSError, ValueError) as error:
        print(json.dumps({"version": VERSION, "status": "inspection_failed", "error": str(error)}), file=sys.stderr)
        return 1
    print(json.dumps({
        "version": VERSION, "status": result["status"], "report": str(arguments.report),
        "all_provider_uploads_reported": result["all_provider_uploads_reported"],
        "cloud_preservation_verified": False, "safe_to_release_local_content": False,
        **result["totals"],
    }, sort_keys=True))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
