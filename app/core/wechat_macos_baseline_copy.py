"""Synchronize an owned stale working copy through sealed monotone bank changes.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import re
import shutil
import sqlite3

from . import wechat_macos_recovery_bank as bank
from .wechat_archive import wechat_archive_path


VERSION = "v1.0.1-codex.0"
_PRIMARY = {
    "sources": ("id",), "occurrences": ("id",),
    "conversation_proofs": ("conversation_id", "source_id"),
    "gaps": ("id",), "shadow_replacements": ("original_id",),
    "projection_revisions": ("message_id", "sequence"),
    "messages": ("id",), "event_keys": ("message_id",),
    "conversations": ("id",), "archive_info": ("key",),
}
_IMMUTABLE = (
    "sources", "occurrences", "conversation_proofs", "gaps",
    "shadow_replacements", "projection_revisions",
)
_DERIVED_METADATA = {
    "schema_version", "code_version", "input_identities", "account_ids", "payload_sha256",
}


class BaselineCopySynchronizationError(ValueError):
    """Expose a fixed code and safe audit, including an already committed sync."""

    def __init__(self, code, audit_summary):
        super().__init__(code)
        self.audit_summary = json.loads(bank._json(audit_summary))


def _readonly(path):
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA temp_store=MEMORY")
    connection.execute("PRAGMA trusted_schema=OFF")
    return connection


def _quote(value):
    return '"' + value.replace('"', '""') + '"'


def _layout(connection):
    names = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )}
    required = set(_PRIMARY) - {"projection_revisions"}
    if names not in (required, set(_PRIMARY)):
        raise ValueError("baseline_sync_requires_known_tables")
    result = {}
    for table in _PRIMARY:
        if table not in names:
            continue
        columns = [tuple(row) for row in connection.execute(f"PRAGMA main.table_info({_quote(table)})")]
        primary = tuple(row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5])
        if primary != _PRIMARY[table]:
            raise ValueError("baseline_sync_requires_known_primary_keys")
        result[table] = tuple(row[1] for row in columns)
    return result


def _match(table, first="n", second="o"):
    return " AND ".join(f"{first}.{_quote(key)} IS {second}.{_quote(key)}" for key in _PRIMARY[table])


def _same(columns, first="n", second="o"):
    return " AND ".join(f"{first}.{_quote(key)} IS {second}.{_quote(key)}" for key in columns)


def _preservation(connection, layout):
    """Prove every old key and observation survives before any working-file write."""
    for table, columns in layout.items():
        name = _quote(table)
        if connection.execute(
            f"SELECT 1 FROM main.{name} o LEFT JOIN newer.{name} n ON {_match(table)} "
            f"WHERE n.{_quote(_PRIMARY[table][0])} IS NULL LIMIT 1"
        ).fetchone():
            raise ValueError("baseline_sync_old_key_removed")
        if table in _IMMUTABLE and connection.execute(
            f"SELECT 1 FROM main.{name} o JOIN newer.{name} n ON {_match(table)} "
            f"WHERE NOT ({_same(columns)}) LIMIT 1"
        ).fetchone():
            raise ValueError("baseline_sync_old_observation_changed")
    message_columns = layout["messages"]
    changed = connection.execute(
        "SELECT o.* FROM main.messages o JOIN newer.messages n ON n.id=o.id "
        f"WHERE NOT ({_same(message_columns)})"
    )
    bridged = 0
    for observed in changed:
        old = dict(observed)
        if "projection_revisions" not in layout:
            raise ValueError("baseline_sync_message_has_no_revision_bridge")
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence),0) FROM main.projection_revisions WHERE message_id=?",
            (old["id"],),
        ).fetchone()[0]
        bridge = connection.execute(
            "SELECT prior_row FROM newer.projection_revisions WHERE message_id=? AND sequence=?",
            (old["id"], sequence + 1),
        ).fetchone()
        if bridge is None or bank._json(json.loads(bridge[0])) != bank._json(old):
            raise ValueError("baseline_sync_message_has_no_exact_prior_row")
        bridged += 1
    stable_keys = ("account_id", "peer_id", "server_id")
    if connection.execute(
        "SELECT 1 FROM main.event_keys o JOIN newer.event_keys n ON n.message_id=o.message_id "
        f"WHERE NOT ({_same(stable_keys)}) LIMIT 1"
    ).fetchone():
        raise ValueError("baseline_sync_event_identity_changed")
    keys = connection.execute(
        "SELECT n.message_id,n.signature FROM newer.event_keys n LEFT JOIN main.event_keys o "
        "ON n.message_id=o.message_id WHERE o.message_id IS NULL OR n.signature IS NOT o.signature"
    )
    for key in keys:
        message = connection.execute("SELECT * FROM newer.messages WHERE id=?", (key[0],)).fetchone()
        if message is None or key[1] != bank._event_signature(dict(message)):
            raise ValueError("baseline_sync_event_signature_is_not_derived")
        old = connection.execute("SELECT * FROM main.messages WHERE id=?", (key[0],)).fetchone()
        if old is not None and dict(old) == dict(message):
            raise ValueError("baseline_sync_unchanged_message_has_changed_event_key")
    stable_conversation = ("partition_id", "account_key", "peer_id", "title", "kind")
    if connection.execute(
        "SELECT 1 FROM main.conversations o JOIN newer.conversations n ON n.id=o.id "
        f"WHERE NOT ({_same(stable_conversation)}) LIMIT 1"
    ).fetchone():
        raise ValueError("baseline_sync_conversation_identity_changed")
    conversations = connection.execute(
        "SELECT n.* FROM newer.conversations n LEFT JOIN main.conversations o ON n.id=o.id "
        f"WHERE o.id IS NULL OR NOT ({_same(layout['conversations'])})"
    )
    for row in conversations:
        count = connection.execute(
            "SELECT COUNT(*) FROM newer.messages m WHERE conversation_id=? AND NOT EXISTS("
            "SELECT 1 FROM newer.shadow_replacements s WHERE s.original_id=m.id)", (row["id"],)
        ).fetchone()[0]
        first = connection.execute(
            "SELECT time_sort,timestamp_value FROM newer.messages WHERE conversation_id=? "
            "ORDER BY time_sort,ordinal,id LIMIT 1", (row["id"],)
        ).fetchone()
        last = connection.execute(
            "SELECT time_sort,timestamp_value,text FROM newer.messages WHERE conversation_id=? "
            "ORDER BY time_sort DESC,ordinal DESC,id DESC LIMIT 1", (row["id"],)
        ).fetchone()
        if first is None or (
            row["message_count"], row["first_sort"], row["last_sort"], row["first_time"],
            row["last_time"], row["latest_text"],
        ) != (count, first[0], last[0], first[1], last[1], last[2][:300]):
            raise ValueError("baseline_sync_conversation_summary_is_not_derived")
    prior_meta = dict(connection.execute("SELECT key,value FROM main.archive_info"))
    current_meta = dict(connection.execute("SELECT key,value FROM newer.archive_info"))
    if any(
        key not in _DERIVED_METADATA and prior_meta.get(key) != value
        for key, value in current_meta.items()
    ):
        raise ValueError("baseline_sync_unknown_metadata_changed")
    return bridged


def _new_message_proofs(connection, original):
    """Require an authenticated retained observation for each newly appended event."""
    decoded = {}
    rows = connection.execute(
        "SELECT n.* FROM newer.messages n LEFT JOIN main.messages o ON n.id=o.id WHERE o.id IS NULL"
    )
    for observed in rows:
        row = dict(observed)
        key = connection.execute("SELECT * FROM newer.event_keys WHERE message_id=?", (row["id"],)).fetchone()
        if key is None:
            raise ValueError("baseline_sync_new_message_has_no_event_key")
        signature = bank._event_signature(row)
        proved = False
        occurrences = connection.execute(
            "SELECT * FROM newer.occurrences WHERE canonical_id=?",
            (row["id"],),
        )
        for occurrence in occurrences:
            source_key = occurrence["source_id"]
            if source_key not in decoded:
                source = connection.execute("SELECT proof FROM newer.sources WHERE id=?", (source_key,)).fetchone()
                if source is None:
                    raise ValueError("baseline_sync_new_message_has_no_source")
                packet = json.loads(source[0])
                if bank._sha(packet) != source_key:
                    raise ValueError("baseline_sync_source_receipt_mismatch")
                bound = bank._binding(original, packet)
                conversation = connection.execute(
                    "SELECT * FROM newer.conversations WHERE id=?", (packet["session_id"],)
                ).fetchone()
                if conversation is None or any(
                    conversation[field] != bound[field]
                    for field in ("id", "partition_id", "account_key", "peer_id", "kind", "title")
                ):
                    raise ValueError("baseline_sync_new_conversation_has_no_matching_binding")
                decoded[source_key] = packet
            packet = decoded[source_key]
            message = json.loads(occurrence["proof"])
            bank._validate_packet({**packet, "messages": [message]}, (packet["account_id"],))
            projected = dict(zip(row, bank._row(packet, message), strict=True))
            if (
                packet["session_id"] == row["conversation_id"]
                and packet["account_id"] == key["account_id"]
                and packet["peer_id"] == key["peer_id"]
                and message["server_id"] == key["server_id"] == occurrence["server_id"]
                and message["local_id"] == occurrence["local_id"]
                and message["content_sha256"] == occurrence["content_sha256"]
                and key["signature"] == signature
                and occurrence["signature"] == bank._event_signature(projected)
                and (
                    occurrence["signature"] == signature
                    or bank.reparse.historical_occurrence(connection, dict(occurrence), schema="newer")
                )
                and (
                    occurrence["canonical_store"] == "macos_bank" and projected["id"] == row["id"]
                    or occurrence["canonical_store"] == "macos_bank_shadow" and connection.execute(
                        "SELECT 1 FROM newer.shadow_replacements WHERE original_id=?", (row["id"],)
                    ).fetchone() is not None
                )
            ):
                proved = True
                break
        if not proved:
            raise ValueError("baseline_sync_new_message_has_no_matching_observation")


def _apply_differences(connection, layout):
    counts = {}
    for table, columns in layout.items():
        name = _quote(table)
        mutable = [key for key in columns if key not in _PRIMARY[table]]
        updates = ",".join(
            f"{_quote(key)}=(SELECT n.{_quote(key)} FROM newer.{name} n WHERE {_match(table, 'n', 'o')})"
            for key in mutable
        )
        updated = 0
        if mutable:
            updated = connection.execute(
                f"UPDATE main.{name} AS o SET {updates} WHERE EXISTS(SELECT 1 FROM newer.{name} n "
                f"WHERE {_match(table)} AND NOT ({_same(columns)}))"
            ).rowcount
        inserted = connection.execute(
            f"INSERT INTO main.{name} SELECT n.* FROM newer.{name} n WHERE NOT EXISTS("
            f"SELECT 1 FROM main.{name} o WHERE {_match(table)})"
        ).rowcount
        counts[table] = {"updated": updated, "inserted": inserted}
    return counts


def synchronize_baseline_copy(
    local_store_root, candidate_path, *, publication_guard,
    expected_payload_sha256, expected_inputs,
):
    """Refresh a private old working copy and return strict capability plus safe audit."""
    history = wechat_archive_path(local_store_root)
    target = bank.mac_recovery_bank_path(local_store_root)
    scratch = Path(candidate_path).expanduser().absolute()
    bank._require_publication_guard(publication_guard, history)
    inputs = bank._inputs(history)
    if (
        not isinstance(expected_inputs, dict) or set(expected_inputs) != set(inputs)
        or expected_inputs.get(target.name) is None
        or {key: value for key, value in expected_inputs.items() if key != target.name}
        != {key: value for key, value in inputs.items() if key != target.name}
        or not isinstance(expected_payload_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_payload_sha256) is None
        or scratch.resolve() in {history.resolve(), target.resolve()}
    ):
        raise ValueError("baseline_sync_old_authority_does_not_match")
    before = bank._baseline_copy_file(scratch, target)
    bank._no_baseline_copy_handles(scratch)
    with closing(_readonly(scratch)) as old, closing(_readonly(target)) as current, closing(_readonly(history)) as original:
        old_metadata = bank._validate_bank(old, history, expected_inputs)
        current_metadata = bank._validate_bank(current, history, inputs, allow_parallel=True)
        schema = bank._baseline_schema(current)
        if bank._baseline_schema(old) != schema:
            raise ValueError("baseline_sync_requires_identical_closed_schema")
        already_current = old_metadata == current_metadata
        if old_metadata["payload_sha256"] != expected_payload_sha256 and not already_current:
            raise ValueError("baseline_sync_old_payload_does_not_match")
        layout = _layout(old)
        if layout != _layout(current):
            raise ValueError("baseline_sync_table_layout_changed")
        if [row[0] for row in old.execute("PRAGMA integrity_check")] != ["ok"]:
            raise ValueError("baseline_sync_old_integrity_failed")
        old.execute("ATTACH DATABASE ? AS newer", (target.resolve().as_uri() + "?mode=ro&immutable=1",))
        old.execute("PRAGMA newer.cache_size=-65536")
        bridged = _preservation(old, layout)
        _new_message_proofs(old, original)
    bank._require_publication_guard(publication_guard, history)
    if bank._inputs(history) != inputs or bank._baseline_copy_file(scratch, target) != before:
        raise ValueError("baseline_sync_inputs_changed_during_validation")
    allocated = getattr(scratch.stat(), "st_blocks", 0) * 512
    growth = max(0, inputs[target.name]["size"] - allocated)
    required = 0 if already_current else before["size"] + growth + 1024**3
    free = shutil.disk_usage(scratch.parent).free
    if free < required:
        raise ValueError("baseline_sync_insufficient_rollback_workspace")
    audit = {
        "version": VERSION, "scratch_path": str(scratch), "committed": already_current,
        "already_current": already_current,
        "before_identity": before, "input_identities": inputs,
        "old_input_identities": json.loads(bank._json(expected_inputs)),
        "old_payload_sha256": expected_payload_sha256,
        "current_payload_sha256": current_metadata["payload_sha256"],
        "journal_bridged_messages": bridged, "required_free_bytes": required,
        "working_allocated_before_bytes": allocated,
        "additional_materialization_or_growth_bytes": 0 if already_current else growth,
        "free_before_bytes": free,
    }
    if already_current:
        audit["changed_rows"] = {table: {"inserted": 0, "updated": 0} for table in layout}
        return _verified_result(local_store_root, scratch, publication_guard, audit)
    bank._no_baseline_copy_handles(scratch)
    with closing(sqlite3.connect(scratch.resolve().as_uri() + "?mode=rw", uri=True, timeout=0)) as output:
        output.row_factory = sqlite3.Row
        output.execute("PRAGMA temp_store=MEMORY")
        output.execute("PRAGMA trusted_schema=OFF")
        if output.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
            raise ValueError("baseline_sync_requires_ordinary_rollback_journal")
        output.execute("PRAGMA synchronous=FULL")
        output.execute("PRAGMA locking_mode=EXCLUSIVE")
        output.execute("BEGIN EXCLUSIVE")
        try:
            if bank._baseline_copy_file(scratch, target) != before or bank._inputs(history) != inputs:
                raise ValueError("baseline_sync_inputs_changed_before_write")
            output.execute("ATTACH DATABASE ? AS newer", (target.resolve().as_uri() + "?mode=ro&immutable=1",))
            output.execute("PRAGMA newer.cache_size=-65536")
            audit["changed_rows"] = _apply_differences(output, layout)
            metadata = bank._validate_bank(output, history, inputs)
            if metadata != current_metadata or bank._baseline_schema(output) != schema:
                raise ValueError("baseline_sync_output_does_not_equal_current")
            if [row[0] for row in output.execute("PRAGMA integrity_check")] != ["ok"]:
                raise ValueError("baseline_sync_output_integrity_failed")
            bank._require_publication_guard(publication_guard, history)
            named = scratch.lstat()
            if bank._inputs(history) != inputs or (named.st_dev, named.st_ino, named.st_nlink) != (before["dev"], before["ino"], 1):
                raise ValueError("baseline_sync_inputs_changed_before_commit")
            output.commit()
            audit["committed"] = True
        except Exception:
            try:
                output.rollback()
                audit["rollback_completed"] = True
            except Exception:
                audit["rollback_completed"] = False
                raise BaselineCopySynchronizationError("baseline_copy_sync_rollback_failed", audit) from None
            raise BaselineCopySynchronizationError("baseline_copy_sync_failed_before_commit", audit) from None
    return _verified_result(local_store_root, scratch, publication_guard, audit)


def _verified_result(local_store_root, scratch, publication_guard, audit):
    try:
        audit["after_identity"] = bank._identity(scratch)
        capability = bank.verify_baseline_copy(local_store_root, scratch, publication_guard=publication_guard)
    except Exception as error:
        locations = []
        trace = error.__traceback__
        while trace is not None:
            code = trace.tb_frame.f_code
            locations.append({
                "file": code.co_filename, "function": code.co_name,
                "line": trace.tb_lineno,
            })
            trace = trace.tb_next
        audit["capability_failure"] = {
            "error_type": type(error).__name__, "frames": locations[-16:],
        }
        raise BaselineCopySynchronizationError("baseline_copy_synced_but_capability_unavailable", audit) from None
    audit["after_identity"] = capability.summary["scratch_identity"]
    return capability, audit
