#!/usr/bin/env python3
"""Reparse authenticated app-card labels in a staged completion catalog.

Code version: v1.0.0-codex.0
"""

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path
import sqlite3
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import wechat_mac_refresh_projection as projection
from app.core import wechat_mac_refresh_source as native
from app.core import wechat_secondary_completions as completions
from app.core.owner_only_files import (
    ensure_owner_only, owner_only_directory_problem, owner_only_problem,
)
from app.core.wechat_archive import wechat_archive_path
from app.core.wechat_completion_clone import sync_completion_file
from app.core.wechat_completion_json_codec import encode_json_cell, load_json_cell
from scripts import capture_wechat_resource_details as capture
from scripts import repair_wechat_resource_details as repair


VERSION = "v1.0.0-codex.0"
_LABEL_FIELDS = ("app_subtype", "app_metadata_version", *capture.bank._APP_LABEL_FIELDS)
_REFERENCE_TABLES = {
    "append_event_audit": ("kept_witness_key", "dropped_witness_key"),
    "incremental_resource_conflicts": ("prior_witness_key", "witness_key"),
}


def _target_witnesses(output, history, owners, counts):
    sessions = {
        row["id"]: row["peer_id"]
        for owner in owners
        for row in history.execute(
            "SELECT id,peer_id FROM conversations WHERE account_key=? AND kind='group'",
            ("account:" + owner,),
        )
    }
    output.execute("CREATE TEMP TABLE target_sessions(id TEXT PRIMARY KEY) WITHOUT ROWID")
    output.executemany(
        "INSERT INTO target_sessions VALUES(?)", ((session,) for session in sessions),
    )
    output.execute("CREATE TEMP TABLE target_witnesses(id TEXT PRIMARY KEY) WITHOUT ROWID")
    output.execute(
        "INSERT OR IGNORE INTO target_witnesses SELECT e.witness_key "
        "FROM entries e JOIN target_sessions t ON t.id=e.conversation_id "
        "JOIN messages m ON m.id=e.id AND m.conversation_id=e.conversation_id "
        "WHERE e.operation='replace' AND e.strategy='native_sender' "
        "AND m.text='[App message]' "
        "AND NOT EXISTS (SELECT 1 FROM ambiguities a WHERE a.id=e.id)"
    )
    counts["shared_or_quarantined_witnesses"] += output.execute(
        "DELETE FROM target_witnesses WHERE id IN ("
        "SELECT e.witness_key FROM entries e "
        "JOIN target_witnesses w ON w.id=e.witness_key "
        "LEFT JOIN target_sessions t ON t.id=e.conversation_id "
        "WHERE t.id IS NULL OR e.operation!='replace' OR e.strategy!='native_sender' "
        "OR EXISTS (SELECT 1 FROM ambiguities a WHERE a.id=e.id))"
    ).rowcount
    for table, columns in _REFERENCE_TABLES.items():
        if not output.execute(
            "SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name=?", (table,),
        ).fetchone():
            continue
        for column in columns:
            counts["shared_or_quarantined_witnesses"] += output.execute(
                f"DELETE FROM target_witnesses WHERE id IN (SELECT {column} FROM {table})"
            ).rowcount

    groups, headers, bindings = defaultdict(list), {}, set()
    for row in output.execute(
        "SELECT t.id AS witness_key,e.conversation_id,w.message,w.sha256,"
        "e.source_key,s.header,s.sha256 AS source_sha256 "
        "FROM entries e JOIN target_witnesses t ON t.id=e.witness_key "
        "JOIN witnesses w ON w.id=t.id AND w.source_key=e.source_key "
        "JOIN sources s ON s.id=e.source_key"
    ):
        source_key = row["source_key"]
        if source_key not in headers:
            header = load_json_cell(row["header"])
            if row["source_sha256"] != source_key or completions._sha(header) != source_key:
                raise ValueError("A scoped app-card witness lost its retained source receipt.")
            headers[source_key] = header
        header = headers[source_key]
        message = load_json_cell(row["message"])
        resource = message.get("resource", {})
        if resource.get("kind") != "app" or message["local_type"] & 0xFFFFFFFF != 49:
            counts["non_app_witnesses"] += 1
            continue
        if (
            completions._sha(message) != row["sha256"]
            or completions._sha([source_key, message["local_id"], message["content_sha256"]])
            != row["witness_key"]
        ):
            raise ValueError("A scoped app-card witness lost its retained message digest.")
        source = header["source"]
        owner = header["account_id"]
        peer = sessions[row["conversation_id"]]
        if (
            owner not in owners or header["peer_id"] != peer
            or header["conversation_kind"] != "group"
            or source["table"] != projection._table(peer)
            or "account_binding" not in header
        ):
            counts["unavailable_source_scope"] += 1
            continue
        if source_key not in bindings:
            capture.bank._validate_account_binding(history, {**header, "messages": []})
            bindings.add(source_key)
        key = (owner, source["account_directory"], source["database_relative_path"])
        groups[key].append((row["witness_key"], row["sha256"], header["session_id"], peer, message))
    return groups


def _enrich_witnesses(output, groups, counts):
    registry = native.KeyRegistry()
    sources = {}
    for (owner, directory, path), targets in sorted(groups.items()):
        if owner not in sources:
            sources[owner] = {
                (item.account_directory, item.relative_path): item
                for item in registry.references(owner, load_keys=True)
            }
        reference = sources[owner].get((directory, path))
        if reference is None:
            counts["configured_source_unavailable"] += len(targets)
            continue
        try:
            database = native.read_database(reference)
        except (OSError, sqlite3.Error, native.RefreshError):
            counts["native_source_unavailable"] += len(targets)
            continue
        updated, seen = 0, set()
        with closing(database), output:
            context = projection.prepare_source_context(owner, database)
            for witness_key, prior_sha, session, peer, witness in targets:
                if witness_key in seen:
                    continue
                seen.add(witness_key)
                scope = projection.ConversationScope(session, owner, peer, "group")
                prior = witness["resource"]
                current = capture._native_message(context, scope, witness, prior, counts)
                if current is None:
                    continue
                resource = dict(prior)
                resource.update({
                    key: current["resource"][key] for key in _LABEL_FIELDS
                    if key in current["resource"]
                })
                if resource == prior:
                    counts["unchanged"] += 1
                    continue
                enriched = {**witness, "resource": resource}
                changed = output.execute(
                    "UPDATE witnesses SET message=?,sha256=? WHERE id=? AND sha256=?",
                    (
                        encode_json_cell(completions._json(enriched)),
                        completions._sha(enriched), witness_key, prior_sha,
                    ),
                ).rowcount
                if changed != 1:
                    raise ValueError("A private app-card witness changed while being reparsed.")
                updated += 1
            context.require_current(database, owner)
        counts["enriched_witnesses"] += updated
        print(f"{owner} {path}: {updated:,} card witnesses enriched", file=sys.stderr, flush=True)


def _stage(local_store, candidate, owners):
    history_path = wechat_archive_path(local_store).absolute()
    active = history_path.with_name(completions.FILENAME)
    pins = completions._pins(history_path)
    prior = completions._identity(active)
    if owner_only_problem(active):
        raise ValueError("The active completion catalog must remain owner-only.")
    with closing(completions._readonly(active)) as old:
        if not completions._matches_read_pins(old, history_path, pins):
            raise ValueError("The completion catalog needs its exact archive inputs.")
    completions._clonefile(active, candidate)
    ensure_owner_only(candidate)
    counts = Counter()
    with (
        closing(sqlite3.connect(candidate)) as output,
        closing(completions._readonly(history_path)) as history,
        closing(completions._readonly(history_path.with_name("resource_references.sqlite3"))) as catalog,
    ):
        output.row_factory = sqlite3.Row
        output.execute("PRAGMA journal_mode=DELETE")
        output.execute("PRAGMA synchronous=FULL")
        output.execute("PRAGMA temp_store=MEMORY")
        with output:
            output.execute("INSERT OR REPLACE INTO archive_info VALUES('staging_state','incomplete')")
        groups = _target_witnesses(output, history, owners, counts)
        output.commit()
        _enrich_witnesses(output, groups, counts)
        if [row[0] for row in output.execute("PRAGMA quick_check")] != ["ok"]:
            raise ValueError("The private completion candidate failed SQLite verification.")
        completions._validate_entries(
            output, history, catalog,
            progress=lambda count: print(
                f"Verified {count:,} completion entries", file=sys.stderr, flush=True,
            ),
        )
        if completions._pins(history_path) != pins or completions._identity(active) != prior:
            raise ValueError("A completion input changed while staging app-card labels.")
        with output:
            output.execute(
                "UPDATE OR REPLACE archive_info SET key='card_reparse_prior_compaction_inputs' "
                "WHERE key='compaction_input_identities'"
            )
            output.executemany("INSERT OR REPLACE INTO archive_info VALUES(?,?)", (
                ("expected_existing_catalog", completions._json(prior)),
                ("input_identities", completions._json(pins)),
                ("app_card_label_reparse", completions._json({
                    "version": VERSION, "accounts": sorted(owners), "counts": dict(counts),
                })),
                ("staging_state", "complete"),
            ))
    sync_completion_file(candidate)
    identity = completions._identity(candidate)
    digest = completions._file_sha(candidate)
    if completions._identity(candidate) != identity:
        raise ValueError("The verified completion candidate changed while hashing.")
    completions._remember_validation(candidate, identity, pins)
    return {
        "version": VERSION, "candidate": str(candidate), "candidate_sha256": digest,
        "candidate_identity": identity, "counts": dict(counts), "published": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-store", type=Path, required=True)
    parser.add_argument("--target-account", action="append", required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args(argv)
    owners = frozenset(args.target_account)
    if any(capture._ACCOUNT.fullmatch(owner) is None for owner in owners):
        parser.error("Select raw target account identifiers.")
    candidate, report = repair._output(args.candidate), repair._output(args.report)
    if (
        candidate == report
        or owner_only_directory_problem(candidate.parent)
        or owner_only_directory_problem(report.parent)
    ):
        parser.error("Use distinct candidate and report paths in an owner-only workstation.")
    receipt = _stage(args.local_store, candidate, owners)
    repair._write(report, receipt)
    if args.publish and receipt["counts"].get("enriched_witnesses"):
        published = completions.publish_secondary_message_completions(
            args.local_store, candidate, candidate_sha256=receipt["candidate_sha256"],
        )
        print(completions._json(published), flush=True)
    else:
        print(completions._json(receipt), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
