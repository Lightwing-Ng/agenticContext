#!/usr/bin/env python3
"""Inventory retained native app witnesses or capture a bounded repair batch.

Code version: v1.0.0-codex.0
"""

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import wechat_mac_refresh_projection as projection
from app.core import wechat_mac_refresh_source as native
from app.core import wechat_macos_recovery_bank as bank
from app.core import wechat_secondary_completions as completions
from app.core.wechat_archive import WechatArchiveRepository, _file_identity
from app.core.wechat_completion_json_codec import load_json_cell
from app.core.wechat_conversation_events import _inputs
from app.core.wechat_resource_detail_repair import MAX_EVENTS, MAX_PACKETS, _readonly
from app.core.wechat_resource_details import (
    COLUMNS, MAX_ENTRIES, load_registry, row_sha256,
)
from app.core.wechat_resource_sender_evidence import match_secondary_native_event
from scripts.repair_wechat_resource_details import MAX_PACKET_FILE_BYTES, WORKSTATIONS


_ID = re.compile(r"[0-9a-f]{64}")
_SESSION = re.compile(r"wechat:[0-9a-f]{64}")
_LABELS = tuple(key for key in bank._APP_LABEL_FIELDS if not key.endswith("_source"))


@dataclass
class Candidate:
    row: dict
    source_key: str
    witness_key: str
    projected_sha256: str
    donor: dict
    reference: dict

    @property
    def shard(self):
        source = self.donor["source"]
        return self.donor["account_id"], source["account_directory"], source["database_relative_path"]


def _candidate(record, retained, counts):
    counts["retained_replacements"] += 1
    if record["id"] in retained:
        counts["already_repaired"] += 1
        return None
    if record["quarantined"]:
        counts["quarantined"] += 1
        return None
    try:
        row = completions._decoded_message({key: record[key] for key in COLUMNS})
        proof = json.loads(row["provenance"])
        reference = proof.get("resource_reference")
        if not isinstance(reference, dict) or reference.get("kind") != "app":
            counts["other_resource"] += 1
            return None
        if any(reference.get(key) for key in _LABELS):
            counts["already_labeled"] += 1
            return None
        completion = proof["secondary_resource_completion"]
        donor = completion["secondary_source"]
        source = donor["source"]
        if (
            completion["strategy"] != "native_sender"
            or completion["clock_skew_seconds"] != 0
            or completion["primary"]["message_type"] != 49
            or type(donor["local_type"]) is not int
            or donor["local_type"] & 0xFFFFFFFF != 49
            or not row["sender_id"]
            or donor["sender_id"] != row["sender_id"]
            or not all(isinstance(value, str) and value for value in (
                donor["account_id"], source["account_directory"], source["database_relative_path"],
            ))
        ):
            counts["unsupported_witness"] += 1
            return None
    except (AttributeError, KeyError, TypeError, ValueError):
        counts["invalid_retained_proof"] += 1
        return None
    counts["candidates"] += 1
    return Candidate(
        row, record["source_key"], record["witness_key"], record["projected_sha256"],
        donor, reference,
    )


def _inventory(derived, retained, *, session, after, identifiers, limit, counts):
    condition = ["e.operation='replace'", "e.strategy='native_sender'", "e.id>?"]
    parameters = [after]
    if session is not None:
        condition.append("e.conversation_id=?")
        parameters.append(session)
    if identifiers:
        condition.append("e.id IN (SELECT value FROM json_each(?))")
        parameters.append(json.dumps(identifiers))
    condition = " AND ".join(condition)
    rows = derived.execute(
        "SELECT m.*,e.source_key,e.witness_key,e.projected_sha256,"
        "EXISTS(SELECT 1 FROM ambiguities a WHERE a.id=e.id) AS quarantined "
        "FROM entries e JOIN messages m ON m.id=e.id AND m.conversation_id=e.conversation_id "
        f"WHERE {condition} ORDER BY e.id",
        parameters,
    )
    selected, sources = [], Counter()
    for record in rows:
        candidate = _candidate(record, retained, counts)
        if candidate is None:
            continue
        sources[candidate.shard] += 1
        if limit is not None:
            if len(selected) == limit:
                if identifiers:
                    continue
                break
            selected.append(candidate)
    if identifiers:
        counts["requested_ids"] = len(identifiers)
        counts["requested_not_eligible"] = len(identifiers) - counts["candidates"]
    return selected, [
        {
            "account_id": account,
            "account_directory": directory,
            "database_relative_path": path,
            "candidates": total,
        }
        for (account, directory, path), total in sorted(sources.items())
    ]


def _retained_event(derived, candidate, conversation):
    source = derived.execute(
        "SELECT header,sha256 FROM sources WHERE id=?", (candidate.source_key,),
    ).fetchone()
    witness = derived.execute(
        "SELECT source_key,message,sha256 FROM witnesses WHERE id=?", (candidate.witness_key,),
    ).fetchone()
    if source is None or witness is None:
        raise ValueError("A retained capture source or witness is missing.")
    header, message = load_json_cell(source["header"]), load_json_cell(witness["message"])
    if (
        source["sha256"] != candidate.source_key
        or completions._sha(header) != candidate.source_key
        or witness["source_key"] != candidate.source_key
        or completions._sha(message) != witness["sha256"]
        or completions._sha([candidate.source_key, message["local_id"], message["content_sha256"]])
        != candidate.witness_key
        or completions._sha(candidate.row) != candidate.projected_sha256
        or header["peer_id"] != conversation["peer_id"]
        or "account_binding" not in header
    ):
        raise ValueError("Retained capture source, witness or projection changed.")
    matched = match_secondary_native_event(candidate.row, conversation, [{**header, **message}])
    if matched is None or matched["secondary_source"] != candidate.donor:
        raise ValueError("The retained witness differs from its active resource completion.")
    return header, message


def _active_targets(repository, derived, selected, counts):
    by_session, by_shard = defaultdict(list), defaultdict(list)
    for candidate in selected:
        by_session[candidate.row["conversation_id"]].append(candidate)
    for session, candidates in by_session.items():
        with closing(_readonly(repository.path)) as history:
            conversations, _messages, status = repository._timeline_tables(
                history, _file_identity(repository.path), conversation_id=session,
                strict=True, include_resource_details=False,
            )
            context = history.execute(
                f"SELECT * FROM {conversations} WHERE id=?", (session,),
            ).fetchone()
            if context is None or status not in {"ready", "absent"}:
                counts["unavailable_target_scope"] += len(candidates)
                continue
            for candidate in candidates:
                identifier = candidate.row["id"]
                # The timeline already chooses exactly one expanded completion
                # per active ID. Check that decision via its primary-key table.
                active = history.execute(
                    "SELECT 1 FROM temp.secondary_completion_active a "
                    "JOIN secondary_completions.entries e ON e.id=a.id "
                    "WHERE a.id=? AND e.conversation_id=? AND e.projected_sha256=?",
                    (identifier, session, candidate.projected_sha256),
                ).fetchone()
                if active is None:
                    counts["inactive_target_witness"] += 1
                    continue
                try:
                    header, witness = _retained_event(derived, candidate, dict(context))
                except (AttributeError, KeyError, TypeError, ValueError):
                    counts["conflicting_retained_witness"] += 1
                    continue
                by_shard[candidate.shard].append((candidate, header, witness))
    return by_shard


def _native_message(context, scope, witness, reference, counts):
    table = projection._table(scope.peer_id)
    try:
        rows = projection._native_rows(
            context.database.connection, table, local_id=witness["local_id"],
        )
    except native.RefreshError:
        counts["unsupported_native_table"] += 1
        return None
    row = rows.fetchone()
    if row is None:
        counts["native_row_unavailable"] += 1
        return None
    raw = row["message_content"]
    if (
        rows.fetchone() is not None
        or any(row[key] != witness[key] for key in (
            "local_id", "local_type", "create_time", "real_sender_id",
        ))
        or str(row["server_id"]) != witness["server_id"]
        or not isinstance(raw, (bytes, str))
        or hashlib.sha256(raw.encode() if isinstance(raw, str) else raw).hexdigest()
        != witness["content_sha256"]
    ):
        counts["native_coordinate_conflict"] += 1
        return None
    try:
        message = projection._project_message(
            row, scope.account_id, scope.kind, context.names, peer_id=scope.peer_id,
        )
    except (ValueError, projection.WCDBBackendUnavailableError) as error:
        counts[projection.projection_exclusion_reason(error)] += 1
        return None
    if any(message.get(key) != witness.get(key) for key in (
        "sender_id", "sender_proof", "direction",
    )):
        counts["native_sender_conflict"] += 1
        return None
    resource = message.get("resource", {})
    if (
        resource.get("kind") != "app"
        or resource.get("app_metadata_version") != "1"
        or not bank._valid_resource_labels(resource, 49)
    ):
        counts["native_labels_unavailable"] += 1
        return None
    if any(
        reference.get(key) not in (None, "", resource[key])
        for key in ("app_subtype", "app_metadata_version", *bank._APP_LABEL_FIELDS)
        if key in resource
    ):
        counts["native_label_conflict"] += 1
        return None
    return message


def _capture_shard(local_store, history, database, account, targets, packet_slots, counts):
    context = projection.prepare_source_context(account, database)
    scopes = defaultdict(list)
    anchors = {}
    for candidate, header, witness in targets:
        scopes[header["session_id"]].append((candidate, header, witness))
        for anchor in header["account_binding"]["anchors"]:
            key = (anchor["table"], anchor["local_id"], anchor["archive_message_id"])
            anchors.setdefault(key, anchor)
    learned = sorted(anchors.values(), key=lambda anchor: anchor["direction"] != "sent")
    learned = tuple(learned[:projection.MAX_LEARNED_WITNESSES])
    packets, manifest = [], []
    binding = None
    for session, claims in scopes.items():
        if len(packets) >= packet_slots:
            counts["packet_budget"] += len(claims)
            continue
        exemplar = claims[0][1]
        try:
            scope = projection.resolve_scope(local_store, session)
        except native.RefreshError:
            counts["unavailable_donor_scope"] += len(claims)
            continue
        if (
            scope.account_id != account or scope.kind != "group"
            or scope.peer_id != exemplar["peer_id"]
            or scope.peer_id not in context.peer_rows
            or projection._table(scope.peer_id) not in context.tables
        ):
            counts["donor_scope_conflict"] += len(claims)
            continue
        table = projection._table(scope.peer_id)
        source = dict(database.source, table=table, account_directory=database.account_directory)
        if binding is None:
            binding = projection._global_binding(
                local_store, database.connection, history, scope, source,
                database.account_directory, context.names, context.peer_rows,
                context.tables, lambda: None, learned_witnesses=learned,
            )
        messages, entries = {}, []
        for candidate, header, witness in claims:
            if header["peer_id"] != scope.peer_id or header["source"]["table"] != table:
                counts["donor_scope_conflict"] += 1
                continue
            message = _native_message(context, scope, witness, candidate.reference, counts)
            if message is None:
                continue
            messages.setdefault(message["local_id"], message)
            entries.append({
                "message_id": candidate.row["id"],
                "original_row_sha256": row_sha256(candidate.row),
                "target_conversation_id": candidate.row["conversation_id"],
            })
        if not messages:
            continue
        packet = {
            "schema_version": 2,
            "receipt_id": "refresh-" + source["snapshot_id"],
            "account_id": account,
            "peer_id": scope.peer_id,
            "session_id": session,
            "conversation_kind": scope.kind,
            "peer_proof": projection._name_proof(context.peer_rows[scope.peer_id]),
            "source": source,
            "anchors": [],
            "account_binding": binding,
            "messages": [messages[key] for key in sorted(messages)],
        }
        bank._validate_packet(packet, (account,))
        bank._binding(history, packet)
        bank._validate_account_binding(history, packet)
        packets.append(packet)
        manifest.extend(entries)
    context.require_current(database, account)
    return packets, manifest


def _capture(local_store, history, by_shard, counts):
    registry = native.KeyRegistry()
    references, packets, manifest = {}, [], []
    for (account, directory, path), targets in sorted(by_shard.items()):
        if len(packets) == MAX_PACKETS:
            counts["packet_budget"] += len(targets)
            continue
        if counts["attempted_shards"] == MAX_PACKETS:
            counts["source_budget"] += len(targets)
            continue
        if account not in references:
            try:
                references[account] = {
                    (item.account_directory, item.relative_path): item
                    for item in registry.references(account, load_keys=True)
                }
            except (OSError, ValueError):
                references[account] = {}
        reference = references[account].get((directory, path))
        if reference is None:
            counts["configured_source_unavailable"] += len(targets)
            continue
        counts["attempted_shards"] += 1
        try:
            database = native.read_database(reference)
        except (OSError, sqlite3.Error, ValueError):
            counts["source_authentication_unavailable"] += len(targets)
            continue
        with closing(database):
            counts["authenticated_shards"] += 1
            try:
                captured, entries = _capture_shard(
                    local_store, history, database, account, targets,
                    MAX_PACKETS - len(packets), counts,
                )
            except native.SourceAccountBindingUnavailable:
                counts["source_account_binding_unavailable"] += len(targets)
                continue
            packets.extend(captured)
            manifest.extend(entries)
    manifest.sort(key=lambda value: value["message_id"])
    counts["packets"] = len(packets)
    counts["captured"] = len(manifest)
    counts["uncovered"] = counts["selected"] - len(manifest)
    return packets, manifest


def _private_directory(path):
    path = path.expanduser().absolute()
    if (
        path.parent.resolve() != path.parent
        or not path.parent.is_relative_to(WORKSTATIONS.resolve())
        or path.exists() or path.is_symlink()
    ):
        raise ValueError("Use a new output in the canonical private WeChat workstation.")
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    state = os.fstat(descriptor)
    if state.st_uid != os.getuid() or state.st_mode & 0o077:
        os.close(descriptor)
        raise ValueError("The output directory must be owned and accessible only by the current user.")
    return path, descriptor


def _write(path, directory, value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if len(raw) > min(MAX_PACKET_FILE_BYTES, bank.MAX_PACKET_BYTES):
        raise ValueError("The capture exceeds its packet file budget. Select a smaller batch.")
    descriptor = os.open(
        path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
        dir_fd=directory,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        os.unlink(path.name, dir_fd=directory)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-store", type=Path, required=True)
    parser.add_argument("--target-session", help="Restrict to this exact target archive conversation.")
    parser.add_argument("--after-message-id", default="", help="Resume after the last attempted target ID.")
    parser.add_argument("--message-id", action="append", help="Select an exact target ID. Repeat to select several.")
    parser.add_argument("--limit", type=int, help="Maximum eligible rows to attempt in this capture.")
    parser.add_argument("--output", type=Path, help="New private workstation JSON for captured packets.")
    args = parser.parse_args(argv)
    identifiers = sorted(set(args.message_id or ()))
    if (
        (args.target_session and _SESSION.fullmatch(args.target_session) is None)
        or (args.after_message_id and _ID.fullmatch(args.after_message_id) is None)
        or any(_ID.fullmatch(identifier) is None for identifier in identifiers)
        or len(identifiers) > min(MAX_ENTRIES, MAX_EVENTS, bank.MAX_PACKET_MESSAGES)
        or (identifiers and args.after_message_id)
    ):
        parser.error("Select valid target archive identifiers or a resume cursor.")
    if (args.output is None) != (args.limit is None) or (
        args.limit is not None
        and not 1 <= args.limit <= min(MAX_ENTRIES, MAX_EVENTS, bank.MAX_PACKET_MESSAGES)
    ):
        parser.error("Capture requires both a bounded --limit and an explicit --output.")
    repository = WechatArchiveRepository(args.local_store)
    pins = _inputs(repository)
    retained, _ = load_registry(repository.path)
    retained_ids = {entry["message_id"] for entry in retained}
    if args.limit is not None and args.limit + len(retained_ids) > MAX_ENTRIES:
        parser.error("The requested capture exceeds the remaining resource detail registry budget.")
    counts = Counter()
    path, directory = _private_directory(args.output) if args.output is not None else (None, None)
    try:
        completion_path = repository.path.with_name(completions.FILENAME)
        if not completion_path.exists() and not completion_path.is_symlink():
            if args.output is not None:
                raise ValueError("No retained completion catalog is available to capture.")
            print(json.dumps({
                "mode": "retained_metadata", "counts": {"retained_catalogs": 0},
                "sources": [],
            }))
            return 0
        completions._identity(completion_path)
        with (
            closing(completions._readonly(completion_path)) as derived,
            closing(_readonly(repository.path)) as history,
        ):
            if not completions._current_metadata(
                derived, repository.path, completions._pins(repository.path),
            ):
                raise ValueError("The retained completion catalog needs current archive inputs.")
            selected, sources = _inventory(
                derived, retained_ids, session=args.target_session, after=args.after_message_id,
                identifiers=identifiers, limit=args.limit, counts=counts,
            )
            report = {
                "mode": "read_only_capture" if args.output is not None else "retained_metadata",
                "counts": dict(counts),
                "sources": sources,
            }
            if args.output is not None:
                counts["selected"] = len(selected)
                groups = _active_targets(repository, derived, selected, counts)
                packets, manifest = _capture(args.local_store, history, groups, counts)
                report["counts"] = dict(counts)
                if (
                    not identifiers and len(selected) == args.limit
                    and counts["candidates"] > args.limit
                ):
                    report["next_after_message_id"] = selected[-1].row["id"]
            if _inputs(repository) != pins:
                raise ValueError("Archive inputs changed during inventory or capture.")
            if args.output is not None:
                _write(path, directory, {"packets": packets, "manifest": manifest, **report})
        print(json.dumps(report, sort_keys=True))
    finally:
        if directory is not None:
            os.close(directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
