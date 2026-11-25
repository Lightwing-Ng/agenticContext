#!/usr/bin/env python3
"""Rebase a sealed global repair onto the latest published WeChat recovery bank.

Code version: v1.0.1-codex.0

The incoming candidate remains immutable evidence. --apply preserves the current
bank, holds the global UI lease and publication guard, and stages validated replay
before the normal preservation checks and atomic publication. No key or native
database is read. Reports contain only counters, identities, hashes, and paths.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
import sys
import time


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import wechat_macos_recovery_bank as bank
from app.core.owner_only_files import ensure_owner_only_directory, owner_only_directory_problem
from app.core.wechat_archive import wechat_archive_path
from app.core.wechat_global_guard import global_refresh_guard
from scripts.archive_wechat_rollbacks import archive_file
from scripts.refresh_wechat_globally import (
    GlobalRepairError,
    WORKSTATIONS,
    bank_summary,
    readonly,
    utc_now,
    verify_preservation,
    write_json,
)


VERSION = "v1.0.1-codex.0"


def merge_accounts(local_store, incoming):
    """Restrict incoming owners to the existing canonical archive registry."""
    history = wechat_archive_path(local_store)
    with closing(readonly(history)) as original, closing(readonly(incoming)) as source:
        registered = {
            row[0] for row in original.execute(
                "SELECT DISTINCT account_id FROM partitions "
                "WHERE account_id IS NOT NULL AND account_id<>'' "
                "AND account_key='account:'||account_id"
            )
        }
        owners = tuple(row[0] for row in source.execute(
            "SELECT DISTINCT account_id FROM sources ORDER BY account_id LIMIT 1001"
        ))
    if not owners or len(owners) > 1_000 or any(owner not in registered for owner in owners):
        raise GlobalRepairError("incoming_bank_has_unregistered_account")
    return owners


def run(local_store, workstation, incoming, *, apply=False, global_report=None):
    from app.core.wechat_macos_bank_merge import stage_merged_mac_recovery_bank

    local_store = local_store.expanduser().resolve()
    workstation = workstation.expanduser().absolute()
    incoming = incoming.expanduser().absolute()
    if workstation.is_symlink() or not workstation.resolve().is_relative_to(WORKSTATIONS.resolve()):
        raise GlobalRepairError("workstation_outside_canonical_root")
    if incoming.is_symlink() or not incoming.resolve().is_relative_to(workstation.resolve()):
        raise GlobalRepairError("incoming_bank_outside_selected_workstation")
    if owner_only_directory_problem(workstation):
        raise GlobalRepairError("workstation_must_be_owner_only")
    bank._identity(incoming)
    directory = workstation / ("merge-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ"))
    directory.mkdir(mode=0o700)
    ensure_owner_only_directory(directory)
    report_path = directory / "result.json"
    report = {
        "version": VERSION, "started_utc": utc_now(), "status": "inventory",
        "published": False, "local_store": str(local_store),
        "incoming": str(incoming),
        "global_report": str(global_report) if global_report else None,
    }
    write_json(report_path, report)
    last_progress = 0.0

    def update(status=None, **values):
        if status:
            report["status"] = status
        report.update(values, updated_utc=utc_now())
        write_json(report_path, report)
        print(json.dumps({"status": report["status"], **values}, sort_keys=True), flush=True)

    def progress(value):
        nonlocal last_progress
        safe = {
            key: count for key, count in value.items()
            if type(count) is int and 0 <= count <= 2**53 - 1
        }
        report["progress"] = {**report.get("progress", {}), **safe}
        if time.monotonic() - last_progress >= 2:
            last_progress = time.monotonic()
            update(progress=report["progress"])

    try:
        report["incoming_summary"] = bank_summary(incoming)
        owners = merge_accounts(local_store, incoming)
        report["account_count"] = len(owners)
        update("inventoried")
        if not apply:
            update("inventory_only_not_verified", completed_utc=utc_now())
            return report_path
        history = wechat_archive_path(local_store)
        target = bank.mac_recovery_bank_path(local_store)
        update("waiting_for_exclusive_publication")
        with global_refresh_guard(local_store), bank.mac_recovery_publication_guard(local_store) as guard:
            before = bank_summary(target)
            report["before"] = before
            report["input_identities"] = bank._inputs(history)
            required = (
                3 * before.get("identity", {}).get("size", 0)
                + 3 * report["incoming_summary"]["identity"]["size"]
                + 1024**3
            )
            if shutil.disk_usage(directory).free < required:
                raise GlobalRepairError("insufficient_merge_and_rollback_capacity")
            if before["exists"]:
                update("preserving_current_rollback")
                rollback = archive_file(target, directory / "bank-before-merge.sqlite3.zst")
                report["rollback"] = {
                    key: rollback[key] for key in (
                        "archive", "receipt", "source_sha256", "archive_sha256",
                        "archive_bytes", "decompressed_bytes", "full_roundtrip_verified",
                    )
                }
                if bank._identity(target) != before["identity"]:
                    raise GlobalRepairError("current_bank_changed_during_rollback")
            candidate = directory / "candidate.sqlite3"
            update("replaying_validated_observations")
            receipt = stage_merged_mac_recovery_bank(
                local_store, incoming, candidate, account_ids=owners,
                publication_guard=guard, progress=progress,
            )
            report["staging_receipt"] = receipt
            if receipt["input_identities"] != report["input_identities"]:
                raise GlobalRepairError("merge_baseline_differs_from_reviewed_inputs")
            update("checking_current_preservation")
            expected = verify_preservation(target, candidate, before, receipt)
            report["expected_after"] = expected
            update("publishing")
            publication = bank.publish_mac_recovery_bank(
                local_store, candidate, receipt, publication_guard=guard,
            )
            report["published"] = True
            report["publication_receipt"] = publication
            update("published")
            after = bank_summary(target)
            if after["counts"] != expected["counts"] or after["payload_sha256"] != expected["payload_sha256"]:
                raise GlobalRepairError("published_merge_readback_differs_from_candidate")
            update("verified", after=after, completed_utc=utc_now())
        return report_path
    except BaseException as error:
        report.update(
            failed_phase=report["status"],
            status="published_verification_failed" if report["published"] else "failed_before_publication",
            error_type=type(error).__name__, completed_utc=utc_now(),
        )
        if isinstance(error, GlobalRepairError):
            report["error_code"] = str(error)
        write_json(report_path, report)
        print(json.dumps({"status": report["status"], "error_type": report["error_type"], "report": str(report_path)}), flush=True)
        raise GlobalRepairError("bank_merge_failed_see_private_report") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-store", type=Path, required=True)
    parser.add_argument("--workstation", type=Path, required=True)
    parser.add_argument("--incoming", type=Path, required=True)
    parser.add_argument("--global-report", type=Path)
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        result = run(
            arguments.local_store, arguments.workstation, arguments.incoming,
            apply=arguments.apply, global_report=arguments.global_report,
        )
    except (GlobalRepairError, OSError):
        print("The validated bank merge did not complete; inspect its private report.", file=sys.stderr)
        return 1
    print(json.dumps({"report": str(result), "version": VERSION}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
