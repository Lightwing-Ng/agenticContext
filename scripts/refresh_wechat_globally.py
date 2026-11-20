#!/usr/bin/env python3
"""Repair all eligible archived WeChat conversations from configured verified keys.

Code version: v1.6.0-codex.0

The default is a metadata-only inventory. --apply preserves a byte-exact compressed
rollback, streams authenticated packets into one candidate, checks preservation,
and publishes once through the existing compare-and-swap boundary. Native plaintext
and packet bodies remain in memory. Reports contain counters and source coordinates.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import wechat_macos_recovery_bank as bank
from app.core.wechat_archive import wechat_archive_path
from app.core.wechat_global_guard import global_refresh_guard
from scripts.archive_wechat_rollbacks import archive_file


VERSION = "v1.6.0-codex.0"
WORKSTATIONS = Path.home() / (
    "Library/CloudStorage/OneDrive-Personal/WeChat Text Archive/Workstations"
)
APPEND_ONLY_TABLES = (
    "sources",
    "occurrences",
    "conversation_proofs",
    "gaps",
    "shadow_replacements",
    "projection_revisions",
)
COUNT_TABLES = ("messages", "conversations", "event_keys", *APPEND_ONLY_TABLES)
EXCLUSION_FIELDS = frozenset(
    {
        "reason", "reason_code", "account_id", "session_id", "source_id",
        "snapshot_id", "table", "local_id", "server_id", "base_type",
        "message_type", "database_relative_path", "row_count", "native_rows",
    }
)


class GlobalRepairError(ValueError):
    """Expose only a fixed operation code, never native content or key material."""


def utc_now():
    return datetime.now(UTC).isoformat()


def failure_frames(error):
    """Retain code locations without exception text, locals, or native bodies."""
    frames = []
    trace = error.__traceback__
    while trace is not None:
        code = trace.tb_frame.f_code
        frames.append({
            "file": code.co_filename, "function": code.co_name,
            "line": trace.tb_lineno,
        })
        trace = trace.tb_next
    return frames[-24:]


def write_json(path, value):
    """Publish one owner-only report atomically beside its pending file."""
    descriptor, pending = tempfile.mkstemp(prefix=".report-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
    finally:
        Path(pending).unlink(missing_ok=True)


def readonly(path):
    connection = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
    )
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA temp_store=MEMORY")
    connection.execute("PRAGMA trusted_schema=OFF")
    return connection


def table_exists(connection, table, schema="main"):
    return connection.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def bank_summary(path):
    if not path.exists():
        return {"exists": False, "counts": dict.fromkeys(COUNT_TABLES, 0)}
    before = bank._identity(path)
    with closing(readonly(path)) as connection:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if table_exists(connection, table)
            else 0
            for table in COUNT_TABLES
        }
        metadata = dict(connection.execute("SELECT key,value FROM archive_info"))
        repairs = dict(
            connection.execute(
                "SELECT reason,COUNT(*) FROM projection_revisions GROUP BY reason"
            )
        ) if table_exists(connection, "projection_revisions") else {}
    if bank._identity(path) != before:
        raise GlobalRepairError("bank_changed_during_summary")
    return {
        "exists": True,
        "identity": before,
        "counts": counts,
        "net_added": counts["messages"] - counts["shadow_replacements"],
        "repair_reasons": repairs,
        "schema_version": metadata.get("schema_version"),
        "code_version": metadata.get("code_version"),
        "payload_sha256": metadata.get("payload_sha256"),
    }


def _quoted(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def completion_coverage(report):
    """Keep inaccessible accounts, rejected rows, and conflicts visible after success."""
    inventory = report["inventory"]
    preparation = report.get("preparation_progress", {})
    limits = {
        "accounts_without_keys": inventory["accounts_without_keys"],
        "unconfigured_message_databases": inventory["unavailable_sources"],
        "unavailable_accounts": inventory["unavailable_accounts"],
        "skipped_sources": preparation.get("skipped_sources", 0),
        "skipped_scope_sources": preparation.get("skipped_scopes", 0),
        "excluded_records": preparation.get("excluded", 0),
        "conflicting_candidates": report.get("staging_receipt", {}).get("conflicts", 0),
        "incomplete_wal_sources": report.get("reason_codes", {}).get(
            "source_incomplete_wal_coverage", 0
        ),
    }
    if report.get("retry_conflicts"):
        limits["unavailable_recorded_conflict_pairs"] = report.get("selection", {}).get(
            "unavailable_conflict_pairs", 0
        )
    return {"partial": any(limits.values()), "limitations": limits}


def _read_retry_report(report_path):
    """Read a small stable report only from the canonical workstation boundary."""
    if report_path.is_symlink():
        raise GlobalRepairError("retry_report_must_not_be_a_symlink")
    report_path = report_path.expanduser().resolve(strict=True)
    if not report_path.is_relative_to(WORKSTATIONS.resolve()):
        raise GlobalRepairError("retry_report_outside_canonical_workstations")
    _private_evidence_path(report_path)
    identity = bank._identity(report_path)
    if identity["size"] > 2 * 1024**2:
        raise GlobalRepairError("retry_report_exceeds_metadata_budget")
    value = json.loads(report_path.read_text(encoding="utf-8"))
    if bank._identity(report_path) != identity:
        raise GlobalRepairError("retry_report_changed_during_read")
    return report_path, value


def _private_evidence_path(path):
    """Reject shared or replaceable evidence before trusting its audit coordinates."""
    for candidate, directory in ((path, False), (path.parent, True), (path.parent.parent, True)):
        observed = candidate.lstat()
        if (
            not (stat.S_ISDIR(observed.st_mode) if directory else stat.S_ISREG(observed.st_mode))
            or observed.st_mode & 0o077
            or not directory and observed.st_nlink != 1
            or hasattr(os, "getuid") and observed.st_uid != os.getuid()
        ):
            raise GlobalRepairError("retry_evidence_must_be_private_owned_regular_paths")


def _retry_origin(report_path):
    """Follow one verified merge to its completely scanned, sealed input run."""
    report_path, prior = _read_retry_report(report_path)
    if prior.get("global_report") is None:
        return report_path, prior, False
    if prior.get("status") != "verified" or prior.get("published") is not True:
        raise GlobalRepairError("retry_merge_report_is_not_verified")
    origin_path, origin = _read_retry_report(Path(prior["global_report"]))
    if origin_path.parent.parent != report_path.parent.parent:
        raise GlobalRepairError("retry_merge_origin_is_outside_selected_workstation")
    if (
        origin.get("status") != "failed_before_publication"
        or origin.get("failed_phase") != "staging"
        or origin.get("published") is not False
    ):
        raise GlobalRepairError("retry_merge_origin_has_an_unsupported_boundary")
    preparation = origin.get("preparation_progress", {})
    complete, skipped, selected = (
        preparation.get(key) for key in (
            "completed_sources", "skipped_sources", "selected_sources"
        )
    )
    if (
        any(type(value) is not int or value < 0 for value in (complete, skipped, selected))
        or selected == 0 or complete + skipped != selected
    ):
        raise GlobalRepairError("retry_merge_origin_scan_was_incomplete")
    incoming = origin_path.with_name("candidate.sqlite3")
    if incoming.is_symlink() or Path(prior.get("incoming", "")).resolve() != incoming:
        raise GlobalRepairError("retry_merge_incoming_does_not_match_origin")
    _private_evidence_path(incoming)
    summary = prior.get("incoming_summary", {})
    replay = prior.get("staging_receipt", {}).get("merge", {})
    if (
        summary.get("identity") != replay.get("incoming_identity")
        or summary.get("payload_sha256") != replay.get("incoming_payload_sha256")
        or bank._identity(incoming) != summary.get("identity")
    ):
        raise GlobalRepairError("retry_merge_incoming_identity_is_stale")
    with closing(readonly(incoming)) as connection:
        metadata = dict(connection.execute("SELECT key,value FROM archive_info"))
    if (
        metadata.get("payload_sha256") != summary.get("payload_sha256")
        or bank._identity(incoming) != summary.get("identity")
    ):
        raise GlobalRepairError("retry_merge_incoming_payload_is_stale")
    if (
        prior.get("local_store") != origin.get("local_store")
        or prior.get("input_identities", {}).get("history.sqlite3")
        != origin.get("input_identities", {}).get("history.sqlite3")
    ):
        raise GlobalRepairError("retry_merge_origin_archive_does_not_match")
    return origin_path, origin, {
        "incoming": incoming, "identity": summary["identity"],
    }


def _recorded_conflict_scopes(merged, scopes, references, audit):
    """Retry sealed rejected observations without assuming their semantic cause."""
    incoming, identity = merged["incoming"], merged["identity"]
    if bank._identity(incoming) != identity:
        raise GlobalRepairError("retry_conflict_bank_identity_is_stale")
    selected, unavailable, decoded = set(), set(), {}
    with closing(readonly(incoming)) as connection:
        if not all(table_exists(connection, table) for table in ("gaps", "sources")):
            raise GlobalRepairError("retry_conflict_bank_has_no_observation_tables")
        if connection.execute("SELECT COUNT(*) FROM gaps").fetchone()[0] > 2_000_000:
            raise GlobalRepairError("retry_conflict_rows_exceed_metadata_budget")
        rows = connection.execute(
            "SELECT DISTINCT conversation_id,source_id FROM gaps WHERE reason=? LIMIT 100001",
            ("native_or_projection_conflict",),
        )
        pairs = 0
        for session, source_key in rows:
            pairs += 1
            if pairs > 100_000:
                raise GlobalRepairError("retry_conflict_pairs_exceed_metadata_budget")
            if source_key not in decoded:
                source = connection.execute(
                    "SELECT account_id,peer_id,proof FROM sources WHERE id=?", (source_key,)
                ).fetchone()
                if source is None or len(source[2]) > 2 * 1024**2:
                    raise GlobalRepairError("retry_conflict_source_is_unavailable")
                packet = json.loads(source[2])
                if not isinstance(packet, dict) or bank._sha(packet) != source_key:
                    raise GlobalRepairError("retry_conflict_source_proof_does_not_match")
                decoded[source_key] = (source[0], source[1], packet)
            owner, peer, packet = decoded[source_key]
            scope = scopes.get(session)
            if (
                scope is None or scope.account_id != owner or scope.peer_id != peer
                or packet.get("session_id") != session or packet.get("account_id") != owner
                or packet.get("peer_id") != peer or not isinstance(packet.get("source"), dict)
            ):
                raise GlobalRepairError("retry_conflict_scope_is_no_longer_eligible")
            relative = packet["source"].get("database_relative_path")
            if not isinstance(relative, str) or not relative:
                raise GlobalRepairError("retry_conflict_source_has_no_database_coordinate")
            coordinate = (owner, relative, session)
            if (owner, relative) not in references:
                unavailable.add(coordinate)
                continue
            selected.add(coordinate)
    if bank._identity(incoming) != identity:
        raise GlobalRepairError("retry_conflict_bank_changed_during_read")
    audit.update(
        recorded_conflict_pairs=pairs, eligible_conflict_pairs=len(selected),
        unavailable_conflict_pairs=len(unavailable), decoded_conflict_sources=len(decoded),
    )
    return selected


def retry_scope_sources(report_path, inventory, *, include_conflicts=False, selection_audit=None):
    """Select terminal recorded failures, including a verified sealed-run merge."""
    report_path, prior, merged = _retry_origin(report_path)
    if include_conflicts and not merged:
        raise GlobalRepairError("conflict_retry_requires_a_verified_merge_report")
    if prior.get("status") not in {"verified", "no_candidate_updates"}:
        if not merged:
            raise GlobalRepairError("retry_report_is_not_terminal")
    if Path(prior.get("local_store", "")).resolve() != inventory.history_path.parents[2]:
        raise GlobalRepairError("retry_report_belongs_to_another_archive")
    if prior.get("input_identities", {}).get("history.sqlite3") != inventory.history_identity:
        raise GlobalRepairError("retry_report_history_identity_is_stale")
    failures = report_path.with_name("exclusions.jsonl")
    before = bank._identity(failures)
    if before["size"] > 512 * 1024**2:
        raise GlobalRepairError("retry_coordinates_exceed_metadata_budget")
    scopes = {scope.session_id: scope for scope in inventory.scopes}
    references = {
        (reference.account_id, reference.relative_path)
        for reference in inventory.references
    }
    allowed_reasons = {
        "scope_native_row_budget_exceeded", "scope_account_binding_unavailable"
    }
    if merged:
        allowed_reasons.add("scope_projection_failed")
    selected = set()
    with failures.open(encoding="utf-8") as stream:
        for index, line in enumerate(stream):
            if index >= 2_000_000 or len(line) > 16 * 1024:
                raise GlobalRepairError("retry_coordinates_exceed_row_budget")
            value = json.loads(line)
            if value.get("reason") not in allowed_reasons:
                continue
            scope = scopes.get(value.get("session_id"))
            owner, relative = value.get("account_id"), value.get("database_relative_path")
            if scope is None or scope.account_id != owner or (owner, relative) not in references:
                raise GlobalRepairError("retry_coordinates_are_no_longer_eligible")
            selected.add((owner, relative, scope.session_id))
    if bank._identity(failures) != before:
        raise GlobalRepairError("retry_coordinates_changed_during_read")
    if include_conflicts:
        audit = {} if selection_audit is None else selection_audit
        selected.update(_recorded_conflict_scopes(merged, scopes, references, audit))
    if not selected:
        raise GlobalRepairError("retry_report_has_no_selected_scope_failures")
    return frozenset(selected)


def verify_preservation(previous, candidate, before, receipt):
    """Retain every old observation and require journals for changed old messages."""
    after = bank_summary(candidate)
    expected = {
        "messages": receipt["added"] + receipt["upgraded"],
        "shadow_replacements": receipt["upgraded"],
        "occurrences": receipt["occurrences_added"],
        "projection_revisions": receipt.get("reparsed", 0),
    }
    for table, increment in expected.items():
        if after["counts"][table] - before["counts"][table] != increment:
            raise GlobalRepairError("receipt_count_mismatch_" + table)
    if not before["exists"]:
        return after
    if bank._identity(previous) != before["identity"]:
        raise GlobalRepairError("bank_changed_before_preservation_check")
    with closing(readonly(candidate)) as connection:
        connection.execute(
            "ATTACH DATABASE ? AS prior",
            (previous.resolve().as_uri() + "?mode=ro&immutable=1",),
        )
        connection.execute("PRAGMA prior.cache_size=-65536")
        for table in APPEND_ONLY_TABLES:
            if not table_exists(connection, table, "prior"):
                continue
            columns = list(connection.execute(f"PRAGMA prior.table_info({table})"))
            primary = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
            if not primary:
                raise GlobalRepairError("append_only_table_has_no_primary_key")
            join = " AND ".join(f"n.{_quoted(col)} IS o.{_quoted(col)}" for col in primary)
            same = " AND ".join(
                f"n.{_quoted(row[1])} IS o.{_quoted(row[1])}" for row in columns
            )
            if connection.execute(
                f"SELECT 1 FROM prior.{table} o LEFT JOIN main.{table} n ON {join} "
                f"WHERE n.{_quoted(primary[0])} IS NULL OR NOT ({same}) LIMIT 1"
            ).fetchone():
                raise GlobalRepairError("prior_observation_changed_" + table)
        if connection.execute(
            "SELECT 1 FROM prior.conversations o LEFT JOIN main.conversations n "
            "ON n.id=o.id WHERE n.id IS NULL OR n.partition_id IS NOT o.partition_id "
            "OR n.account_key IS NOT o.account_key OR n.peer_id IS NOT o.peer_id "
            "OR n.kind IS NOT o.kind LIMIT 1"
        ).fetchone():
            raise GlobalRepairError("prior_conversation_identity_changed")
        if connection.execute(
            "SELECT 1 FROM prior.messages o LEFT JOIN main.messages n ON n.id=o.id "
            "WHERE n.id IS NULL LIMIT 1"
        ).fetchone():
            raise GlobalRepairError("prior_message_identity_removed")
        if connection.execute(
            "SELECT 1 FROM prior.event_keys o LEFT JOIN main.event_keys n "
            "ON n.message_id=o.message_id WHERE n.message_id IS NULL "
            "OR n.account_id IS NOT o.account_id OR n.peer_id IS NOT o.peer_id "
            "OR n.server_id IS NOT o.server_id LIMIT 1"
        ).fetchone():
            raise GlobalRepairError("prior_event_identity_changed")
        columns = list(connection.execute("PRAGMA prior.table_info(messages)"))
        same = " AND ".join(
            f"n.{_quoted(row[1])} IS o.{_quoted(row[1])}" for row in columns
        )
        prior_revisions = table_exists(connection, "projection_revisions", "prior")
        candidate_revisions = table_exists(connection, "projection_revisions")
        new_revision = (
            "AND NOT EXISTS(SELECT 1 FROM prior.projection_revisions old "
            "WHERE old.message_id=r.message_id AND old.sequence=r.sequence)"
            if prior_revisions else ""
        )
        journal_guard = (
            " AND NOT EXISTS(SELECT 1 FROM main.projection_revisions r "
            f"WHERE r.message_id=o.id {new_revision})"
            if candidate_revisions else ""
        )
        if connection.execute(
            "SELECT 1 FROM prior.messages o JOIN main.messages n ON n.id=o.id "
            f"WHERE NOT ({same}){journal_guard} LIMIT 1"
        ).fetchone():
            raise GlobalRepairError("changed_message_has_no_new_repair_journal")
    if bank._identity(previous) != before["identity"]:
        raise GlobalRepairError("bank_changed_after_preservation_check")
    return after


def reusable_baseline(report_path, local_store, workstation, before, inputs, guard, *, refresh=False):
    """Reuse only a failed, unpublished scratch whose complete baseline is verified."""
    report_path, prior = _read_retry_report(report_path)
    synchronized = prior.get("baseline_refresh", {})
    committed_resume = (
        isinstance(synchronized, dict)
        and prior.get("failed_phase") == "verifying_reusable_baseline"
        and prior.get("error_type") == "BaselineCopySynchronizationError"
        and synchronized.get("committed") is True
    )
    if (
        report_path.parent.parent != workstation.resolve()
        or prior.get("status") != "failed_before_publication"
        or (prior.get("failed_phase") != "staging" and not committed_resume)
        or prior.get("published") is not False
        or prior.get("local_store") != str(local_store)
    ):
        raise GlobalRepairError("reuse_report_does_not_match_current_baseline")
    if committed_resume:
        scratch = Path(synchronized.get("scratch_path", "")).absolute()
        after = synchronized.get("after_identity", {})
        if (
            prior.get("before") != before or prior.get("input_identities") != inputs
            or synchronized.get("input_identities") != inputs
            or synchronized.get("current_payload_sha256") != before.get("payload_sha256")
            or scratch.name != "candidate.sqlite3"
            or scratch.is_symlink() or scratch.resolve().parent.parent != workstation.resolve()
            or not isinstance(after, dict)
            or any(value != after.get(key) for key, value in bank._identity(scratch).items() if key != "ctime_ns")
        ):
            raise GlobalRepairError("committed_baseline_sync_is_disconnected")
        capability = bank.verify_baseline_copy(local_store, scratch, publication_guard=guard)
        return capability, {
            "failed_report": str(report_path), **capability.summary,
            "resumed_committed_synchronization": True,
            "baseline_refresh": synchronized,
        }
    if prior.get("input_identities") != inputs or prior.get("before") != before:
        if not refresh:
            raise GlobalRepairError("reuse_report_does_not_match_current_baseline")
        from app.core.wechat_macos_baseline_copy import synchronize_baseline_copy

        capability, audit = synchronize_baseline_copy(
            local_store, report_path.with_name("candidate.sqlite3"),
            publication_guard=guard,
            expected_payload_sha256=prior.get("before", {}).get("payload_sha256"),
            expected_inputs=prior.get("input_identities"),
        )
        return capability, {
            "failed_report": str(report_path), **capability.summary,
            "baseline_refresh": audit,
        }
    capability = bank.verify_baseline_copy(
        local_store, report_path.with_name("candidate.sqlite3"),
        publication_guard=guard,
    )
    return capability, {"failed_report": str(report_path), **capability.summary}


def source_capacity(inventory, selection):
    """Reserve the largest serial memory snapshot, including its WAL buffers."""
    from app.core import wechat_mac_refresh_source as source

    selected = None if selection is None else {item[:2] for item in selection}
    peak, groups = 0, []
    for reference in inventory.references:
        if selected is not None and (reference.account_id, reference.relative_path) not in selected:
            continue
        path = reference.database_path
        group = {
            suffix: source.identity(Path(str(path) + suffix), optional=bool(suffix))
            for suffix in source._GROUP_SUFFIXES
        }
        if group["-journal"] is not None and group["-journal"][2]:
            raise GlobalRepairError("source_capacity_requires_committed_group")
        source._admit_snapshot(group)
        main, wal, shm = (group[suffix][2] if group[suffix] else 0 for suffix in ("", "-wal", "-shm"))
        needed = (
            4 * min(source.MAX_DATABASE_BYTES, main + wal)
            + 2 * wal + shm + source.SNAPSHOT_MEMORY_OVERHEAD
        )
        peak = max(peak, needed)
        groups.append({"database_relative_path": reference.relative_path, "identities": group})
    return {"serial_snapshot_peak_bytes": peak, "source_groups": groups}


def run(local_store, workstation, *, apply=False, retry_report=None, include_conflicts=False, reuse_failed_report=None, refresh_failed_copy=False):
    """Keep both coordination leases across every apply-phase source observation."""
    if reuse_failed_report is not None and not apply:
        raise GlobalRepairError("scratch_reuse_requires_apply")
    if refresh_failed_copy and reuse_failed_report is None:
        raise GlobalRepairError("baseline_refresh_requires_a_failed_report")
    if not apply:
        return _run(
            local_store, workstation, retry_report=retry_report,
            include_conflicts=include_conflicts,
        )
    with global_refresh_guard(local_store), bank.mac_recovery_publication_guard(local_store) as guard:
        return _run(
            local_store, workstation, apply=True, retry_report=retry_report,
            publication_guard=guard, include_conflicts=include_conflicts,
            reuse_failed_report=reuse_failed_report,
            refresh_failed_copy=refresh_failed_copy,
        )


def _run(
    local_store, workstation, *, apply=False, retry_report=None,
    publication_guard=None, include_conflicts=False, reuse_failed_report=None,
    refresh_failed_copy=False,
):
    from app.core.wechat_global_refresh import (
        inventory_global_refresh,
        iter_global_refresh_packets,
    )

    local_store = local_store.expanduser().resolve()
    workstation = workstation.expanduser().absolute()
    if workstation.is_symlink() or not workstation.resolve().is_relative_to(WORKSTATIONS.resolve()):
        raise GlobalRepairError("workstation_outside_canonical_root")
    workstation.mkdir(mode=0o700, parents=False, exist_ok=True)
    if not workstation.is_dir() or workstation.stat().st_mode & 0o077:
        raise GlobalRepairError("workstation_must_be_owner_only")
    run_directory = workstation / ("run-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ"))
    run_directory.mkdir(mode=0o700)
    report_path = run_directory / "result.json"
    report = {
        "version": VERSION, "started_utc": utc_now(), "published": False,
        "status": "inventory", "local_store": str(local_store),
    }
    write_json(report_path, report)
    last_progress = 0.0
    staging_candidate = None

    def update(status=None, **values):
        if status is not None:
            report["status"] = status
        report.update(values, updated_utc=utc_now())
        write_json(report_path, report)
        print(json.dumps({"status": report["status"], **values}, sort_keys=True), flush=True)

    def progress(value):
        nonlocal last_progress
        if "phase" not in value and staging_candidate is not None and staging_candidate.exists():
            available = shutil.disk_usage(run_directory).free
            report["capacity_budget"]["minimum_free_during_staging_bytes"] = min(
                report["capacity_budget"].get("minimum_free_during_staging_bytes", available),
                available,
            )
            if available < 1024**3:
                raise GlobalRepairError("insufficient_free_space_during_staging")
        section = "preparation_progress" if "phase" in value else "staging_progress"
        safe = {
            key: item for key, item in value.items()
            if type(item) is int and 0 <= item <= 2**53 - 1
        }
        report[section] = {**report.get(section, {}), **safe}
        if isinstance(value.get("reason_codes"), dict):
            report["reason_codes"] = {
                key: count for key, count in value["reason_codes"].items()
                if re.fullmatch(r"[a-z][a-z0-9_]{0,80}", key)
                and type(count) is int and count >= 0
            }
        if time.monotonic() - last_progress >= 2:
            last_progress = time.monotonic()
            update(**{section: report[section]})

    try:
        inventory = inventory_global_refresh(local_store)
        report["inventory"] = inventory.summary
        report["inventory"]["configured_account_scopes"] = sum(
            scope.account_id in inventory.account_ids for scope in inventory.scopes
        )
        if include_conflicts and retry_report is None:
            raise GlobalRepairError("conflict_retry_requires_a_verified_merge_report")
        selection_audit = {}
        selection = retry_scope_sources(
            retry_report, inventory, include_conflicts=include_conflicts,
            selection_audit=selection_audit,
        ) if retry_report else None
        if selection is not None:
            report["retry_report"] = str(retry_report)
            report["selection"] = {
                "scope_source_pairs": len(selection),
                "scopes": len({value[2] for value in selection}),
                "sources": len({value[:2] for value in selection}),
                **selection_audit,
            }
            report["retry_conflicts"] = include_conflicts
        target = bank.mac_recovery_bank_path(local_store)
        before = bank_summary(target)
        report["before"] = before
        update("inventoried", inventory=inventory.summary)
        if not apply:
            update("inventory_complete", completed_utc=utc_now())
            return report_path
        if not inventory.account_ids:
            raise GlobalRepairError("no_configured_verified_account")
        bank_bytes = before.get("identity", {}).get("size", 0)
        report["input_identities"] = bank._inputs(wechat_archive_path(local_store))
        baseline_copy = None
        if reuse_failed_report is not None:
            update("verifying_reusable_baseline")
            baseline_copy, report["baseline_reuse"] = reusable_baseline(
                reuse_failed_report, local_store, workstation, before,
                report["input_identities"], publication_guard,
                refresh=refresh_failed_copy,
            )
        capacity = source_capacity(inventory, selection)
        # The archive utility checks its own worst-case compressed output. Once
        # verified, its actual allocation is reflected in free space below.
        # Native snapshots never write to disk and retain their independent
        # physical-memory admission above. Disk reserves cover the candidate,
        # one full-bank allowance for SQLite work, and the fixed free-space floor.
        credit = report.get("baseline_reuse", {}).get("reused_clone_credit_bytes", 0)
        required = 2 * bank_bytes - credit + 1024**3
        available = shutil.disk_usage(run_directory).free
        report["capacity_budget"] = {
            **capacity,
            "snapshot_storage": "memory_only",
            "stage_required_bytes": required,
            "free_before_rollback_bytes": available,
            "rollback_accounted_as_existing_archive": True,
        }
        if available < required:
            raise GlobalRepairError("insufficient_staging_capacity")
        if before["exists"]:
            update("preserving_rollback")
            rollback = archive_file(target, run_directory / "bank-before.sqlite3.zst")
            report["rollback"] = {
                key: rollback[key] for key in (
                    "archive", "receipt", "source_sha256", "archive_sha256",
                    "archive_bytes", "decompressed_bytes", "full_roundtrip_verified",
                )
            }
            if bank._identity(target) != before["identity"]:
                raise GlobalRepairError("bank_changed_during_rollback")
        if bank._inputs(wechat_archive_path(local_store)) != report["input_identities"]:
            raise GlobalRepairError("source_changed_during_rollback")
        available = shutil.disk_usage(run_directory).free
        report["capacity_budget"]["free_after_rollback_bytes"] = available
        if available < required:
            raise GlobalRepairError("insufficient_staging_capacity_after_rollback")
        exclusion_counts = Counter()
        exclusion_path = run_directory / "exclusions.jsonl"
        with exclusion_path.open("x", encoding="utf-8") as excluded:
            os.chmod(exclusion_path, 0o600)

            def on_excluded(record):
                safe = {key: value for key, value in record.items() if key in EXCLUSION_FIELDS}
                reason = safe.get("reason_code", safe.get("reason", "unclassified"))
                exclusion_counts[str(reason)] += 1
                excluded.write(json.dumps(safe, sort_keys=True) + "\n")
                excluded.flush()

            update("preparing_sources")
            packets = iter_global_refresh_packets(
                local_store, inventory=inventory,
                on_progress=progress, on_excluded=on_excluded,
                **({"scope_sources": selection} if selection is not None else {}),
            )
            with closing(packets):
                first = next(packets, None)
                if first is None:
                    report["coverage"] = completion_coverage(report)
                    update(
                        "no_candidate_updates", exclusion_reasons=dict(exclusion_counts),
                        completed_utc=utc_now(),
                    )
                    return report_path
                candidate = (
                    Path(report["baseline_reuse"]["scratch_path"])
                    if baseline_copy is not None else run_directory / "candidate.sqlite3"
                )
                staging_candidate = candidate
                update("staging")
                receipt = bank.stage_mac_recovery_bank(
                    local_store, itertools.chain((first,), packets), candidate,
                    account_ids=inventory.account_ids, progress=progress,
                    **({"baseline_copy": baseline_copy, "publication_guard": publication_guard}
                       if baseline_copy is not None else {}),
                )
        if receipt["input_identities"] != report["input_identities"]:
            raise GlobalRepairError("staging_baseline_differs_from_reviewed_inputs")
        report["exclusion_reasons"] = dict(exclusion_counts)
        report["staging_receipt"] = receipt
        report["coverage"] = completion_coverage(report)
        update("checking_preservation")
        expected_after = verify_preservation(target, candidate, before, receipt)
        report["expected_after"] = expected_after
        update("publishing")
        publication = bank.publish_mac_recovery_bank(
            local_store, candidate, receipt, publication_guard=publication_guard,
        )
        report["published"] = True
        report["publication_receipt"] = publication
        if baseline_copy is not None:
            report["baseline_reuse"]["consumed_by_publication"] = True
        update("published")
        after = bank_summary(target)
        if after["counts"] != expected_after["counts"] or after["payload_sha256"] != expected_after["payload_sha256"]:
            raise GlobalRepairError("published_readback_differs_from_candidate")
        update("verified", after=after, completed_utc=utc_now())
        return report_path
    except BaseException as error:
        synchronization = sys.modules.get("app.core.wechat_macos_baseline_copy")
        if synchronization is not None and isinstance(
            error, getattr(synchronization, "BaselineCopySynchronizationError", ())
        ):
            report["baseline_refresh"] = error.audit_summary
        report.update(
            failed_phase=report["status"],
            status="published_verification_failed" if report["published"] else "failed_before_publication",
            error_type=type(error).__name__, failure_frames=failure_frames(error),
            completed_utc=utc_now(),
        )
        if isinstance(error, GlobalRepairError):
            report["error_code"] = str(error)
        write_json(report_path, report)
        print(json.dumps({"status": report["status"], "error_type": report["error_type"], "report": str(report_path)}), flush=True)
        raise GlobalRepairError("global_repair_failed_see_private_report") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-store", type=Path, required=True)
    parser.add_argument("--workstation", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--retry-report", type=Path)
    parser.add_argument("--retry-conflicts", action="store_true")
    parser.add_argument("--reuse-failed-report", type=Path)
    parser.add_argument("--refresh-failed-copy", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.retry_conflicts and arguments.retry_report is None:
        parser.error("--retry-conflicts requires --retry-report")
    if arguments.reuse_failed_report is not None and not arguments.apply:
        parser.error("--reuse-failed-report requires --apply")
    if arguments.refresh_failed_copy and arguments.reuse_failed_report is None:
        parser.error("--refresh-failed-copy requires --reuse-failed-report")
    try:
        result = run(
            arguments.local_store, arguments.workstation,
            apply=arguments.apply, retry_report=arguments.retry_report,
            include_conflicts=arguments.retry_conflicts,
            **({"reuse_failed_report": arguments.reuse_failed_report}
               if arguments.reuse_failed_report is not None else {}),
            **({"refresh_failed_copy": True} if arguments.refresh_failed_copy else {}),
        )
    except (ValueError, OSError):
        print("Global repair did not complete; inspect the private run report.", file=sys.stderr)
        return 1
    print(json.dumps({"report": str(result), "version": VERSION}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
