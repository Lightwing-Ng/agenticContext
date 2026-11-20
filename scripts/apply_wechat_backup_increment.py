"""Stage and verify a prepared native increment before coordinated publication.

Code version: v1.1.0-codex.0

The caller selects a new operation. Every stop retains private candidates and
rollback clones. Publication has a durable intent and per-file actual-state
journal; interruption never triggers an implicit retry or rollback.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import wechat_archive as archive, wechat_reconcile as reconcile
from app.core import wechat_contacts as contacts
from app.core.wechat_derived_publication import _rebind
from app.core.wechat_export_merge import _clonefile
from app.core.job_lock import CACHE_TASK_LOCK_NAME, CacheMaintenanceLock, CacheTaskLock
from app.core.wechat_media_event_revalidation import stage_media_event_revalidation
from app.core.wechat_native_delta import prove_preserved_tables
from app.core.wechat_native_resource_extension import stage_native_resource_catalog_extension
from app.core.wechat_native_restore import restore_wechat_text_subset
from app.core import wechat_native_restore as native_restore
from scripts.inspect_wechat_backup_increment import digest, encoded, identity, readonly, write_new


MARKER = "native_contact_snapshot_identity_binding_v1"


def check_free_space(path):
    value = os.statvfs(path)
    if value.f_bavail * value.f_frsize < 4 * 1024**3:
        raise ValueError("The deliberate actual 4 GiB free-space reserve was reached.")


def guarded_text_restore(*args, check, progress_steps=20_000, **kwargs):
    """Interrupt native SQLite work and check again before private replacement."""
    original_sqlite, original_os = native_restore.sqlite3, native_restore.os
    interrupted = []

    def sqlite_progress():
        try:
            check()
            return 0
        except ValueError as error:
            interrupted.append(error)
            return 1

    class SQLiteProxy:
        def __getattr__(self, name):
            return getattr(original_sqlite, name)

        def connect(self, *values, **options):
            connection = original_sqlite.connect(*values, **options)
            connection.set_progress_handler(sqlite_progress, progress_steps)
            return connection

    class OSProxy:
        def __getattr__(self, name):
            return getattr(original_os, name)

        def replace(self, source, destination):
            check()
            return original_os.replace(source, destination)

    try:
        native_restore.sqlite3, native_restore.os = SQLiteProxy(), OSProxy()
        check()
        return restore_wechat_text_subset(*args, **kwargs)
    except sqlite3.OperationalError as error:
        if interrupted:
            raise ValueError("Native SQLite work stopped at its actual disk reserve.") from error
        raise
    finally:
        native_restore.sqlite3, native_restore.os = original_sqlite, original_os


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def resource_validation(history, catalog):
    with readonly(history) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("ATTACH DATABASE ? AS resource_index", (catalog.as_uri() + "?mode=ro&immutable=1",))
        info = dict(connection.execute("SELECT key,value FROM resource_index.archive_info"))
        if json.loads(info["history_file_identity"]) != archive._file_identity(history):
            raise ValueError("The resource index has a stale primary identity.")
        if info["history_partitions_sha256"] != archive.wechat_history_partition_fingerprint(connection):
            raise ValueError("The resource account partitions changed.")
        corrected = archive.WechatArchiveRepository._validate_resource_conversations(connection)
        count = connection.execute("SELECT COUNT(*) FROM resource_index.messages").fetchone()[0]
        if count != int(info["messages"]) or sum(row[6] for row in corrected) != count:
            raise ValueError("Resource event counts disagree with complete primary validation.")
        return {"events": count, "conversations": len(corrected)}


def name_requests(path):
    requests = []
    with readonly(path) as connection:
        connection.row_factory = sqlite3.Row
        for row in connection.execute("SELECT * FROM sender_bindings"):
            proof = json.loads(row["evidence"])
            if not isinstance(proof, dict) or proof.get("kind") != MARKER:
                continue
            requests.append({"id": hashlib.sha256(encoded(dict(row)).encode()).hexdigest(),
                             "source_id": row["source_id"], "source_snapshot_id": row["source_snapshot"],
                             "account_id": proof["owner_account_id"], "sender_id": row["sender_id"],
                             "sender_label": row["sender_id"], "conversation_id": "retained-name-proof",
                             "peer_id": row["sender_id"], "conversation_kind": "direct"})
    return requests


def resolve_names(store, requests):
    repository = contacts.WechatContactRepository(store)
    result = {}
    for offset in range(0, len(requests), 200):
        for key, value in repository.resolve_messages(requests[offset:offset + 200]).items():
            result[key] = hashlib.sha256(encoded(value).encode()).hexdigest()
    return result


def bind_contacts(store, sources, copies, records, sources_sha):
    """Append exact native Friend bindings; original names and anchors stay put."""
    path = contacts.contact_registry_path(store)
    wanted = {(r["source_id"], r["sender_id"]) for r in records if r.get("sender_id")}
    requests = name_requests(path)
    before = resolve_names(store, requests)
    counts = Counter()
    with sqlite3.connect(path) as output:
        output.row_factory = sqlite3.Row
        output.execute("PRAGMA journal_mode=DELETE")
        output.execute("PRAGMA synchronous=FULL")
        tables = [r[0] for r in output.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        old_counts = {table: output.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables}
        for source in sources:
            sid, owner = source["source_id"], source["source_account_id"]
            relative = f"Documents/{source['account_hash']}/DB/WCDB_Contact.sqlite"
            entry = copies[relative]
            if digest(Path(entry["local_path"])) != entry["sha256"]:
                raise ValueError("A native contact input changed.")
            with readonly(Path(entry["local_path"])) as native:
                rows = {}
                for rowid, username, appid in native.execute("SELECT rowid,userName,openIMAppid FROM Friend"):
                    rows.setdefault(username, []).append((rowid, appid))
            for source_id, sender in sorted(wanted):
                if source_id != sid:
                    continue
                native_rows = rows.get(sender, [])
                if len(native_rows) != 1:
                    counts["held_missing_or_ambiguous_native_identity"] += 1
                    continue
                namespace = "openim" if sender.endswith("@openim") else "wechat"
                realm = native_rows[0][1] if namespace == "openim" else ""
                if not isinstance(realm, str):
                    counts["held_unknown_realm"] += 1
                    continue
                cid = contacts.canonical_contact_id(sender, authority="native_contact_username", namespace=namespace, realm=realm)
                registered = output.execute("SELECT * FROM contacts WHERE id=?", (cid,)).fetchone()
                bindings = [dict(row) for row in output.execute("SELECT * FROM sender_bindings WHERE source_id=? AND sender_id=? ORDER BY source_snapshot,room_id", (sid, sender))]
                if (registered is None or registered["identity_status"] != "verified"
                        or registered["username"] != sender or registered["namespace"] != namespace
                        or registered["realm"] != realm or not bindings
                        or any(row["contact_id"] != cid or row["authority"] not in contacts.CANONICAL_AUTHORITIES for row in bindings)):
                    counts["held_unproved_canonical_binding"] += 1
                    continue
                if any(row["source_snapshot"] in {"", source["snapshot_id"]} and not row["room_id"] for row in bindings):
                    counts["already_usable"] += 1
                    continue
                generic = [row for row in bindings if not row["room_id"]
                           and json.loads(row["evidence"]).get("kind") != MARKER]
                if not generic:
                    counts["held_no_original_generic_anchor"] += 1
                    continue
                retained = {json.loads(row["evidence"])["prior_name_context_snapshot"] for row in bindings
                            if json.loads(row["evidence"]).get("kind") == MARKER}
                candidates = [row for row in generic if row["source_snapshot"] in retained] if retained else generic
                if len(candidates) != 1:
                    counts["held_ambiguous_original_name_context"] += 1
                    continue
                original = candidates[0]
                proof = {"kind": MARKER, "source_metadata_sha256": sources_sha,
                         "source_id": sid, "snapshot_id": source["snapshot_id"],
                         "owner_account_id": owner, "account_hash": source["account_hash"],
                         "database_relative_path": relative, "database_sha256": entry["sha256"],
                         "file_id": entry["file_id"], "native_table": "Friend", "native_column": "userName",
                         "native_rowid": native_rows[0][0], "native_username": sender, "native_openim_appid": realm,
                         "generic_anchor_sha256": hashlib.sha256(encoded(generic).encode()).hexdigest(),
                         "prior_name_context_snapshot": original["source_snapshot"],
                         "names_imported_or_ranked": False, "backup_time_is_name_update_time": False}
                output.execute("INSERT INTO sender_bindings VALUES(?,?,?,?,?,?,?)", (
                    sid, sender, source["snapshot_id"], "", cid, "native_contact_username", encoded(proof)))
                counts["added"] += 1
        output.commit()
        for table in tables:
            expected = old_counts[table] + (counts["added"] if table == "sender_bindings" else 0)
            if output.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] != expected:
                raise ValueError("An unexpected contact table cardinality changed.")
    if resolve_names(store, requests) != before:
        raise ValueError("A later snapshot changed an earlier resolved name or source context.")
    return {"counts": dict(counts), "old_full_resolution_objects_unchanged": len(requests)}


def exact_readback(path, sources, records):
    metadata, omitted = reconcile._normalize_sources(sources, {})
    if omitted:
        raise ValueError("A selected source was excluded unexpectedly.")
    result = []
    with readonly(path) as connection:
        connection.row_factory = sqlite3.Row
        for raw in records:
            snapshot = connection.execute("SELECT id FROM source_snapshots WHERE source_id=? AND snapshot_id=?", (raw["source_id"], metadata[raw["source_id"]]["metadata"]["snapshot_id"])).fetchall()
            if len(snapshot) != 1:
                raise ValueError("An incoming source snapshot is ambiguous.")
            record = reconcile._normalize_record(raw, metadata[raw["source_id"]], snapshot[0][0])
            variant = archive._identifier(raw["source_id"], record["record_id"], "variant", record["observation_id"])
            found = []
            for key in (record["base_id"], variant):
                row = connection.execute("SELECT * FROM messages WHERE id=?", (key,)).fetchone()
                if row is not None and reconcile._compatible(row, record) and json.loads(row["provenance"]) == record["provenance"]:
                    found.append(row["id"])
            if not found:
                row = connection.execute("SELECT * FROM message_evidence WHERE id=?", (record["observation_id"],)).fetchone()
                if row is None or row["observation"] != record["observation_json"] or row["source_id"] != raw["source_id"]:
                    raise ValueError("An exact incoming observation is missing.")
                parent = connection.execute("SELECT * FROM messages WHERE id=?", (row["message_id"],)).fetchone()
                if parent is None or not reconcile._compatible(parent, record):
                    raise ValueError("An evidence target differs from its native text or clock.")
                found.append(parent["id"])
            if len(set(found)) != 1:
                raise ValueError("An incoming observation resolved ambiguously.")
            result.append({"message_id": found[0], "record_id": record["record_id"],
                           "source_id": raw["source_id"], "native_time": raw["unix_timestamp"],
                           "text_sha256": hashlib.sha256(raw["text"].encode()).hexdigest()})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workstation", type=Path, required=True)
    parser.add_argument("--local-store", type=Path, required=True)
    parser.add_argument("--operation", default="coordinated-increment-v1.1.0")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    work, store = args.workstation.absolute(), args.local_store.absolute()
    operation = work / args.operation
    if operation.parent != work:
        raise ValueError("An operation must remain directly inside its workstation.")
    operation.mkdir(mode=0o700)
    private = operation / "private-store"
    home = private / "messaging/wechat"
    home.mkdir(parents=True, mode=0o700)
    rollback = operation / "rollback"
    rollback.mkdir(mode=0o700)
    canonical = store / "messaging/wechat"
    report = {"version": "v1.1.0-codex.0", "state": "not_started", "publication": {}}
    lock = CacheMaintenanceLock(CacheTaskLock(store / CACHE_TASK_LOCK_NAME))
    journal = operation / "journal-v1.0.0.jsonl"

    def progress(value):
        check_free_space(work)
        with journal.open("ab") as stream:
            stream.write((encoded(value) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        if value.get("phase") != "rows":
            print(encoded(value), flush=True)

    acquired = False
    try:
        if not lock.acquire("wechat-native-increment-publication"):
            raise ValueError("Another task owns the archive maintenance boundary.")
        acquired = True
        code_root = Path(__file__).resolve().parents[1]
        code_paths = [Path(__file__).absolute(), *(
            code_root / "app/core" / name for name in (
                "wechat_native_delta.py", "wechat_contacts.py", "wechat_native_restore.py",
                "wechat_native_resource_extension.py", "wechat_native_resource_senders.py",
                "wechat_wcdb.py", "wechat_reconcile.py", "wechat_text.py",
                "wechat_retained_evidence.py", "wechat_media_event_revalidation.py"))]
        code_pins = {str(path): digest(path) for path in code_paths}
        report["code_sha256"] = code_pins
        with (operation / "operator-source-v1.1.0.py").open("xb") as frozen:
            frozen.write(Path(__file__).read_bytes())
            frozen.flush()
            os.fsync(frozen.fileno())
        sources_path = work / "evidence/sources-v1.0.0.json"
        sources = json.loads(sources_path.read_bytes())
        source_sha = digest(sources_path)
        preparation = json.loads((work / "package-v1.0.0/report-v1.0.0.json").read_bytes())
        records_path = work / "package-v1.0.0/records-v1.0.0.jsonl"
        if identity(records_path)[2] > 128 * 1024**2 or preparation["counts"]["records"] > 100_000:
            raise ValueError("This bounded increment requires at most 128 MiB and 100,000 records.")
        if digest(records_path) != preparation["records_sha256"] or source_sha != preparation["source_metadata_sha256"]:
            raise ValueError("The reviewed source package changed.")
        records = [json.loads(line) for line in records_path.open()]
        if len(records) != preparation["counts"]["records"]:
            raise ValueError("The frozen package count changed.")
        snapshot = sources[0]["snapshot_id"]
        copies = json.loads((work / "evidence/preservation-v1.0.0.json").read_bytes())["copies"]
        for entry in copies.values():
            if digest(Path(entry["local_path"])) != entry["sha256"]:
                raise ValueError("An original retained source changed.")
        files = ("history.sqlite3", "resource_references.sqlite3", "media_events.sqlite3", "contacts.sqlite3")
        baseline = {}
        for name in (*files, "exclusions.json"):
            path = canonical / name
            baseline[name] = {"identity": identity(path, database=name.endswith(".sqlite3")), "sha256": digest(path)}
            _clonefile(path, home / name)
            _clonefile(path, rollback / name)
        if baseline["history.sqlite3"]["sha256"] != preparation["canonical_history"]["sha256"]:
            raise ValueError("The source package's canonical baseline changed.")
        report["baseline"] = baseline
        report["initial_resource_validation"] = resource_validation(canonical / "history.sqlite3", canonical / "resource_references.sqlite3")
        write_new(operation / "baseline-v1.0.0.json", baseline)
        def restore_text(apply):
            return guarded_text_restore(private, sources=sources, records=records,
                snapshot_id=snapshot, apply=apply, check=lambda: check_free_space(work))

        preview = restore_text(False)
        write_new(operation / "text-preview-v1.0.0.json", preview)
        if preview["identity_conflicts"] or preview["added_partitions"]:
            raise ValueError("The official preview needs separate identity review.")
        applied = restore_text(True)
        counters = ("input_records", "inserted_messages", "matched_messages", "new_evidence", "conflict_variants", "added_conversations", "added_source_snapshots")
        if any(preview[k] != applied[k] for k in counters):
            raise ValueError("Private application differs from the exact official preview.")
        report["text_apply"] = applied
        report["source_readback"] = exact_readback(home / "history.sqlite3", sources, records)
        write_new(operation / "private-text-checkpoint-v1.0.0.json", report)
        replay = restore_text(False)
        if any(replay[k] for k in ("inserted_messages", "new_evidence", "added_conversations", "added_source_snapshots")):
            raise ValueError("Replaying an identical source package changed the archive.")
        report["text_replay"] = replay
        proof = prove_preserved_tables(rollback / "history.sqlite3", home / "history.sqlite3", mutable_statistics=True, allowed_info=("manifest",), progress=progress)
        write_new(operation / "retained-primary-proof-v1.0.0.json", proof)
        pin = archive._file_identity(home / "history.sqlite3")
        with readonly(home / "resource_references.sqlite3") as source:
            prior_info = dict(source.execute("SELECT key,value FROM archive_info"))
        with readonly(rollback / "history.sqlite3") as old, readonly(home / "history.sqlite3") as new:
            if archive.wechat_history_partition_fingerprint(old) != archive.wechat_history_partition_fingerprint(new):
                raise ValueError("A native partition changed before sidecar revalidation.")
        _rebind(home / "resource_references.sqlite3", prior_info, pin)
        specs = json.loads((work / "package-v1.0.0/resource-specs-v1.0.0.json").read_bytes())
        with readonly(home / "history.sqlite3") as source:
            ids = {sid: rowid for rowid, sid in source.execute("SELECT id,source_id FROM source_snapshots WHERE snapshot_id=?", (snapshot,))}
        specs = [{**{k: v for k, v in spec.items() if k != "source_id"}, "source_snapshot_id": ids[spec["source_id"]]} for spec in specs]
        candidate, repeated = home / ".resource-stage.sqlite3", home / ".resource-replay.sqlite3"
        resource = stage_native_resource_catalog_extension(private, home / "resource_references.sqlite3", candidate, database_specs=specs, resolve_sender_prefixes=True, progress=progress)
        write_new(operation / "resource-stage-v1.0.0.json", resource)
        replay_resource = stage_native_resource_catalog_extension(private, candidate, repeated, database_specs=specs, resolve_sender_prefixes=True, progress=progress)
        if replay_resource["added"] or replay_resource["occurrences_added"]:
            raise ValueError("An identical native resource inventory was not idempotent.")
        write_new(operation / "resource-replay-v1.0.0.json", replay_resource)
        replay_proof = prove_preserved_tables(candidate, repeated, allowed_info=("report",), progress=progress)
        with readonly(candidate) as before, readonly(repeated) as after:
            for table, checked in replay_proof.items():
                if before.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] != after.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]:
                    raise ValueError("Resource replay added an unexpected metadata row.")
        write_new(operation / "resource-replay-row-proof-v1.0.0.json", replay_proof)
        proof = prove_preserved_tables(rollback / "resource_references.sqlite3", candidate, mutable_statistics=True,
            allowed_info=("history_file_identity", "report", "messages", "native_resource_coverage_scope"), progress=progress)
        write_new(operation / "retained-resource-proof-v1.0.0.json", proof)
        os.replace(candidate, home / "resource_references.sqlite3")
        report["candidate_resource_validation"] = resource_validation(home / "history.sqlite3", home / "resource_references.sqlite3")
        media = stage_media_event_revalidation(private, home / ".media-stage.sqlite3")
        write_new(operation / "media-stage-v1.0.0.json", media)
        proof = prove_preserved_tables(rollback / "media_events.sqlite3", home / ".media-stage.sqlite3", allowed_info=("history_file_identity",), progress=progress)
        write_new(operation / "retained-media-proof-v1.0.0.json", proof)
        os.replace(home / ".media-stage.sqlite3", home / "media_events.sqlite3")
        binding = bind_contacts(private, sources, copies, records, source_sha)
        write_new(operation / "contact-bindings-v1.0.0.json", binding)
        contact_replay = bind_contacts(private, sources, copies, records, source_sha)
        if contact_replay["counts"].get("added", 0):
            raise ValueError("A repeated native contact binding was not idempotent.")
        write_new(operation / "contact-binding-replay-v1.0.0.json", contact_replay)
        proof = prove_preserved_tables(rollback / "contacts.sqlite3", home / "contacts.sqlite3", progress=progress)
        write_new(operation / "retained-contact-proof-v1.0.0.json", proof)
        report.update(resource=resource, contacts=binding)
        for name in files:
            with readonly(home / name) as candidate_db:
                if candidate_db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("A prepared candidate failed integrity checking.")
        report["candidates"] = {name: {"path": str(home / name), "identity": identity(home / name), "sha256": digest(home / name)} for name in files}
        report["state"] = "all_candidates_verified_not_published"
        write_new(operation / "prepared-v1.0.0.json", report)
        if args.publish:
            for path, hashed in code_pins.items():
                if digest(Path(path)) != hashed:
                    raise ValueError("Reviewed operator or projection source changed.")
            for entry in copies.values():
                if digest(Path(entry["local_path"])) != entry["sha256"]:
                    raise ValueError("A preserved native source changed before publication.")
            if digest(records_path) != preparation["records_sha256"] or digest(sources_path) != source_sha:
                raise ValueError("Frozen source inputs changed before publication.")
            for name, proof in baseline.items():
                if identity(canonical / name) != proof["identity"] or digest(canonical / name) != proof["sha256"]:
                    raise ValueError("A canonical baseline changed before publication.")
            write_new(operation / "publication-intent-v1.0.0.json", {"baseline": baseline, "candidates": report["candidates"]})
            sync_directory(operation)
            for name in files:
                if identity(canonical / name) != baseline[name]["identity"] or identity(home / name) != report["candidates"][name]["identity"]:
                    raise ValueError("A fully verified publication inode changed.")
                progress({"event": "canonical_replace_prepared", "name": name})
                os.replace(home / name, canonical / name)
                sync_directory(canonical)
                report["publication"][name] = "replaced_full_sha_readback_pending"
                write_new(operation / ("publication-" + name + ".json"), {"state": report["publication"][name], "candidate": report["candidates"][name]})
                progress({"event": "canonical_replaced", "name": name})
            for name in files:
                if digest(canonical / name) != report["candidates"][name]["sha256"]:
                    raise ValueError("A published candidate differs from its prepared complete hash.")
                report["publication"][name] = "full_sha_verified"
            if exact_readback(canonical / "history.sqlite3", sources, records) != report["source_readback"]:
                raise ValueError("Published and private source readback differ.")
            if resource_validation(canonical / "history.sqlite3", canonical / "resource_references.sqlite3") != report["candidate_resource_validation"]:
                raise ValueError("Published resource membership differs from its candidate.")
            report["state"] = "canonical_increment_published_and_readback_verified_clock_pending"
        write_new(operation / "final-v1.0.0.json", report)
    except BaseException as error:
        write_new(operation / "stopped-v1.0.0.json", {**report, "error_type": type(error).__name__, "error": str(error), "artifacts_preserved": True, "automatic_resume": False})
        raise
    finally:
        if acquired:
            lock.release()


if __name__ == "__main__":
    main()
