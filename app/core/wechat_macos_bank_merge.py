"""Rebase a sealed Mac recovery candidate without replacing newer observations.

Code version: v1.0.2-codex.0

This module stages a new bank only. Both input banks must validate against the
same current non-bank sources, and the caller must retain publication exclusion
through preservation verification and publication. Native keys and plaintext
databases are neither opened nor reconstructed.
"""

from __future__ import annotations

from contextlib import closing
import itertools
import json
import os
from pathlib import Path
import sqlite3

from . import wechat_macos_recovery_bank as bank
from . import wechat_macos_reparse as reparse
from .wechat_archive import wechat_archive_path


VERSION = "v1.0.1-codex.0"
_UNION_TABLES = ("sources", "conversation_proofs", "gaps")


class BankMergeError(ValueError):
    """A sealed observation cannot be merged within its existing proof boundary."""


def _readonly(path):
    connection = sqlite3.connect(
        Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA temp_store=MEMORY")
    return connection


def _has_table(connection, table, schema="main"):
    return connection.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _packet_chunks(packet, messages, *, max_messages=None, max_bytes=None):
    """Bound serialized packet bytes without repeatedly encoding a growing list."""
    row_limit = bank.MAX_PACKET_MESSAGES if max_messages is None else max_messages
    byte_limit = bank.MAX_PACKET_BYTES if max_bytes is None else max_bytes
    if type(row_limit) is not int or row_limit < 1 or type(byte_limit) is not int or byte_limit < 1:
        raise BankMergeError("invalid_merge_packet_budget")
    envelope = len(bank._json({**packet, "messages": []}).encode())
    rows, encoded_bytes = [], envelope
    for message in messages:
        size = len(bank._json(message).encode())
        extra = size + bool(rows)
        if rows and (len(rows) >= row_limit or encoded_bytes + extra > byte_limit):
            yield {**packet, "messages": rows}
            rows, encoded_bytes = [], envelope
            extra = size
        if encoded_bytes + extra > byte_limit:
            raise BankMergeError("one_merge_observation_exceeds_packet_budget")
        rows.append(message)
        encoded_bytes += extra
    if rows:
        yield {**packet, "messages": rows}


def _latest_message(packet, row, reason):
    """Rebuild only a projection already sealed under this latest revision source."""
    proof = json.loads(row["provenance"])
    canonical_native = proof["macos_recovery"]
    origin_fields = getattr(
        reparse,
        "METADATA_ORIGIN_FIELDS",
        {"app_card_metadata_completion": "app_card_metadata_origin"},
    )
    origin_field = origin_fields.get(reason)
    native = proof.get(origin_field) if origin_field else canonical_native
    if (
        not isinstance(native, dict)
        or packet["session_id"] != row["conversation_id"]
        or packet["account_id"] != native.get("account_id")
        or packet["peer_id"] != native.get("peer_id")
        or any(
            packet["source"].get(key) != native.get(key)
            for key in (
                "source_id", "snapshot_id", "main_sha256", "wal_sha256",
                "database_relative_path", "table",
            )
        )
    ):
        raise BankMergeError("latest_projection_has_a_different_source_receipt")
    message = {
        key: native[key]
        for key in (
            "local_id", "local_type", "server_id", "create_time",
            "content_sha256", "real_sender_id",
        )
    }
    message.update(
        sender_id=row["sender_id"], sender_label=row["sender_label"],
        direction=row["direction"], text=row["text"],
    )
    if canonical_native.get("sender_proof") is not None:
        message["sender_proof"] = canonical_native["sender_proof"]
    if proof.get("text_extraction_kind") == "resource_placeholder":
        resource = proof.get("resource_reference")
        if not isinstance(resource, dict):
            raise BankMergeError("latest_resource_projection_lost_its_reference")
        allowed = {
            "kind", "label", "native_time", "app_subtype", "app_metadata_version",
            *bank._APP_LABEL_FIELDS,
        }
        message["resource"] = {key: value for key, value in resource.items() if key in allowed}
    else:
        message["native_text"] = {
            key: value for key, value in reparse._projection(row).items()
            if key in bank._NATIVE_FIELDS
        }
    bank._validate_packet({**packet, "messages": [message]}, (packet["account_id"],))
    columns = tuple(row.keys())
    projected = dict(zip(columns, bank._row(packet, message), strict=True))
    if (
        bank._event_signature(projected) != bank._event_signature(row)
        or reparse._identity(projected)[1] != reparse._identity(row)[1]
    ):
        raise BankMergeError("latest_projection_reconstruction_changed_its_identity")
    return message


def _replay_packets(connection, owners, check, counters):
    """Replay observations first, then exact latest sealed semantic projections."""
    for source in connection.execute("SELECT * FROM sources ORDER BY id"):
        check()
        packet = json.loads(source["proof"])
        if packet["account_id"] not in owners or bank._sha(packet) != source["id"]:
            raise BankMergeError("incoming_source_is_outside_the_merge_allowlist")

        def observations():
            for row in connection.execute(
                "SELECT proof FROM occurrences WHERE conversation_id=? AND source_id=? "
                "ORDER BY local_id", (packet["session_id"], source["id"]),
            ):
                counters["replayed_occurrences"] += 1
                if counters["replayed_occurrences"] % 1_024 == 0:
                    check()
                yield json.loads(row["proof"])

        for chunk in _packet_chunks(packet, observations()):
            counters["replayed_packets"] += 1
            yield chunk
    if not _has_table(connection, "projection_revisions"):
        return
    revisions = connection.execute(
        "SELECT r.message_id,r.source_key,r.reason FROM projection_revisions r "
        "WHERE r.sequence=(SELECT MAX(p.sequence) FROM projection_revisions p "
        "WHERE p.message_id=r.message_id) ORDER BY r.source_key,r.message_id"
    )
    for source_key, group in itertools.groupby(revisions, key=lambda row: row["source_key"]):
        check()
        source = connection.execute("SELECT proof FROM sources WHERE id=?", (source_key,)).fetchone()
        if source is None:
            raise BankMergeError("latest_projection_source_is_unavailable")
        packet = json.loads(source[0])
        if packet["account_id"] not in owners or bank._sha(packet) != source_key:
            raise BankMergeError("latest_projection_source_is_outside_the_merge_allowlist")

        def projections():
            for revision in group:
                if counters["synthesized_latest_projections"] % 1_024 == 0:
                    check()
                row = connection.execute(
                    "SELECT * FROM messages WHERE id=?", (revision["message_id"],)
                ).fetchone()
                if row is None:
                    raise BankMergeError("latest_projection_message_is_unavailable")
                message = _latest_message(packet, row, revision["reason"])
                counters["synthesized_latest_projections"] += 1
                yield message

        for chunk in _packet_chunks(packet, projections()):
            counters["replayed_packets"] += 1
            yield chunk


def _union_metadata(output, incoming):
    """Append source proof and gap rows, refusing every unequal primary-key collision."""
    output.execute(
        "ATTACH DATABASE ? AS merge_input",
        (incoming.resolve().as_uri() + "?mode=ro&immutable=1",),
    )
    added = {}
    for table in _UNION_TABLES:
        columns = list(output.execute(f"PRAGMA main.table_info({table})"))
        incoming_columns = list(output.execute(f"PRAGMA merge_input.table_info({table})"))
        if columns != incoming_columns:
            raise BankMergeError("merge_metadata_schema_differs_" + table)
        primary = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
        if not primary:
            raise BankMergeError("merge_metadata_has_no_primary_key")
        join = " AND ".join(f"n.{key} IS i.{key}" for key in primary)
        same = " AND ".join(f"n.{row[1]} IS i.{row[1]}" for row in columns)
        if output.execute(
            f"SELECT 1 FROM merge_input.{table} i JOIN main.{table} n ON {join} "
            f"WHERE NOT ({same}) LIMIT 1"
        ).fetchone():
            raise BankMergeError("merge_metadata_collision_" + table)
        before = output.total_changes
        output.execute(
            f"INSERT INTO main.{table} SELECT i.* FROM merge_input.{table} i "
            f"WHERE NOT EXISTS(SELECT 1 FROM main.{table} n WHERE {join})"
        )
        added[table] = output.total_changes - before
    return added


def stage_merged_mac_recovery_bank(
    local_store_root, incoming_path, candidate_path, *, account_ids,
    publication_guard, progress=None,
):
    """Rebase sealed observations into the current bank while its publication is held."""
    history = wechat_archive_path(local_store_root)
    target = bank.mac_recovery_bank_path(local_store_root)
    incoming, candidate = Path(incoming_path).expanduser(), Path(candidate_path).expanduser()
    owners = tuple(sorted(set(account_ids)))
    if not owners or any(not isinstance(owner, str) or not owner for owner in owners):
        raise BankMergeError("merge_requires_an_explicit_account_allowlist")
    if (
        candidate.exists() or candidate.is_symlink()
        or incoming.resolve() in {target.resolve(), history.resolve(), candidate.resolve()}
    ):
        raise BankMergeError("merge_requires_distinct_new_candidate_and_input_paths")
    bank._require_publication_guard(publication_guard, history)
    baseline = bank._inputs(history)
    if baseline.get(target.name) is None:
        raise BankMergeError("merge_requires_a_current_published_bank")
    incoming_identity = bank._identity(incoming)
    counters = {
        "replayed_occurrences": 0, "synthesized_latest_projections": 0,
        "replayed_packets": 0,
    }

    def check():
        bank._require_publication_guard(publication_guard, history)
        if bank._inputs(history) != baseline or bank._identity(incoming) != incoming_identity:
            raise BankMergeError("merge_inputs_changed_during_staging")

    with closing(_readonly(incoming)) as source, closing(_readonly(target)) as current:
        if progress is not None:
            progress({"phase": "validating_merge_inputs", **counters})
        incoming_metadata = bank._validate_bank(source, history, baseline)
        bank._validate_bank(current, history, baseline)
        check()
        packets = _replay_packets(source, owners, check, counters)
        with closing(packets):
            first = next(packets, None)
            if first is None:
                with bank._staging_output(candidate, history, None, publication_guard) as output:
                    current.backup(output)
                receipt = {
                    "added": 0, "upgraded": 0, "reparsed": 0, "existing": 0,
                    "occurrences_added": 0, "variants": 0, "conflicts": 0,
                    "input_identities": baseline, "account_ids": list(owners),
                }
            else:
                def staging_progress(value):
                    check()
                    if progress is not None:
                        progress({**value, **counters})

                receipt = bank.stage_mac_recovery_bank(
                    local_store_root, itertools.chain((first,), packets), candidate,
                    account_ids=owners, progress=staging_progress,
                )
        check()
        if receipt["input_identities"] != baseline:
            raise BankMergeError("merge_staging_baseline_changed")
        if receipt.get("conflicts", 0):
            raise BankMergeError("sealed_merge_replay_conflicted")
        with closing(sqlite3.connect(candidate, uri=True)) as output:
            if not _has_table(output, "shadow_replacements"):
                output.executescript(bank._SHADOW_SCHEMA)
            metadata_added = _union_metadata(output, incoming)
            metadata = {
                "schema_version": "4" if _has_table(output, "projection_revisions") else "3",
                "code_version": bank.VERSION,
                "input_identities": bank._json({
                    key: value for key, value in baseline.items() if key != target.name
                }),
                "account_ids": bank._json([
                    row[0] for row in output.execute(
                        "SELECT DISTINCT account_id FROM sources ORDER BY account_id"
                    )
                ]),
                "payload_sha256": bank._rows_digest(output),
            }
            output.executemany(
                "INSERT OR REPLACE INTO archive_info VALUES(?,?)", metadata.items()
            )
            output.commit()
            if output.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise BankMergeError("merged_candidate_failed_integrity_check")
            receipt.update(
                total=output.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                scopes=output.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
                upgraded_total=output.execute("SELECT COUNT(*) FROM shadow_replacements").fetchone()[0],
            )
        receipt["added_total"] = receipt["total"] - receipt["upgraded_total"]
        check()
        with closing(_readonly(candidate)) as final:
            bank._validate_bank(final, history, baseline)
        with candidate.open("r+b" if os.name == "nt" else "rb") as stream:
            os.fsync(stream.fileno())
        candidate_identity = bank._identity(candidate)
        candidate_sha256 = bank._file_sha(candidate)
        check()
        if bank._identity(candidate) != candidate_identity:
            raise BankMergeError("merged_candidate_changed_during_sealing")
        receipt.update(
            candidate_path=str(candidate), candidate_identity=candidate_identity,
            candidate_sha256=candidate_sha256,
            merge={
                "version": VERSION, "incoming_path": str(incoming),
                "incoming_identity": incoming_identity,
                "incoming_payload_sha256": incoming_metadata["payload_sha256"],
                "metadata_rows_added": metadata_added, **counters,
            },
        )
        if progress is not None:
            progress({"phase": "merge_candidate_sealed", **counters})
        return receipt
