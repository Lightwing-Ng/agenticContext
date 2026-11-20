"""Preserve and compare a finished iOS WeChat snapshot with a prior local one.

Code version: v1.0.2-codex.0

This preparation command never writes the active archive. Failed operations retain
their plan, source clones and journal; an existing workstation is never resumed
or overwritten automatically. Media payloads and cloud files are not opened.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import sqlite3
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.wechat_export_merge import _clonefile
from app.core.wechat_identity import parse_login_info
from app.core.wechat_native_delta import iter_native_row_deltas


VERSION = "v1.0.2-codex.0"
DOMAIN = "AppDomain-com.tencent.xin"
DATABASE = re.compile(
    r"Documents/([0-9a-f]{32})/(?:DB/(?:message_[0-9]+|MM|WCDB_Contact)\.sqlite"
    r"|DB/FTS[^/]*\.db|Brand/BrandMsg\.db|ChatBot/msg\.sqlite3|fts/fts_message\.db"
    r"|(?:FTS/)?FTS[^/]*\.db)"
)
CHAT = re.compile(r"Chat_[0-9a-f]{32}")


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write_new(path, value):
    with path.open("xb") as stream:
        stream.write((encoded(value) + "\n").encode())
        stream.flush()
        os.fsync(stream.fileno())


def identity(path, *, database=False):
    path = Path(path)
    state = path.lstat()
    if (
        path.resolve() != path.absolute()
        or not stat.S_ISREG(state.st_mode)
        or state.st_nlink != 1
        or getattr(state, "st_flags", 0) & 0x40000000
    ):
        raise ValueError("Input must be a resident, regular, single-link local file.")
    if database and any(Path(str(path) + s).exists() for s in ("-wal", "-shm", "-journal")):
        raise ValueError("Native SQLite sidecars require separate preservation.")
    return [state.st_dev, state.st_ino, state.st_size, state.st_mtime_ns]


def digest(path):
    before = identity(path)
    value = hashlib.sha256()
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if [opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns] != before:
            raise ValueError("The input changed before hashing.")
        while chunk := stream.read(8 * 1024**2):
            value.update(chunk)
    if identity(path) != before:
        raise ValueError("The input changed during hashing.")
    return value.hexdigest()


def readonly(path):
    identity(path, database=True)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA cache_size=-16384")
    return connection


def reject_manifest_sidecars(connection, relative_paths):
    """Check original manifest paths, not misleading hashed-file siblings."""
    wanted = {path + suffix for path in relative_paths for suffix in ("-wal", "-shm", "-journal")}
    for (relative,) in connection.execute(
        "SELECT relativePath FROM Files WHERE domain=? AND "
        "(relativePath GLOB '*-wal' OR relativePath GLOB '*-shm' OR relativePath GLOB '*-journal')",
        (DOMAIN,),
    ):
        if relative in wanted:
            raise ValueError("The original native manifest contains a selected SQLite sidecar.")


def require_previous_publication(previous):
    """Never skip unchanged source bytes relative to an unpublished attempt."""
    legacy = previous / "evidence/ipad-restoration-handoff-v1.0.0.json"
    candidates = [legacy] if legacy.exists() else []
    candidates.extend(sorted(previous.glob("coordinated-increment-*/final-v1.0.0.json")))
    accepted = {
        "published_and_bounded_live_acceptance_verified_with_preserved_gaps",
        "canonical_increment_published_and_readback_verified_clock_pending",
    }
    for path in candidates:
        if json.loads(path.read_bytes()).get("state") in accepted:
            return {"path": str(path), "sha256": digest(path)}
    raise ValueError("The prior workstation has no verified primary publication boundary.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--previous-workstation", type=Path, required=True)
    parser.add_argument("--workstation", type=Path, required=True)
    parser.add_argument("--minimum-free-bytes", type=int, default=4 * 1024**3)
    parser.add_argument("--check-text-file", type=Path)
    args = parser.parse_args()
    backup, previous, work = (p.absolute() for p in (
        args.backup, args.previous_workstation, args.workstation
    ))
    if args.minimum_free_bytes < 2 * 1024**3:
        raise ValueError("The actual free-space reserve must be at least 2 GiB.")
    if work.exists() or work.is_relative_to(backup) or work.is_relative_to(previous):
        raise ValueError("Select a new independent workstation; automatic resume is forbidden.")
    work.mkdir(mode=0o700, parents=True)
    (work / "raw").mkdir(mode=0o700)
    (work / "evidence").mkdir(mode=0o700)
    journal = work / "evidence/progress-v1.0.0.jsonl"

    def guard():
        free = os.statvfs(work)
        value = free.f_bavail * free.f_frsize
        if value < args.minimum_free_bytes:
            raise ValueError("The deliberate actual-free-space reserve was reached.")
        return value

    def emit(event, **fields):
        value = {"event": event, "at_utc": datetime.now(UTC).isoformat(),
                 "free_bytes": guard(), **fields}
        with journal.open("ab") as stream:
            stream.write((encoded(value) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        print(encoded(value), flush=True)

    result = {"version": VERSION, "canonical_written": False,
              "OneDrive_backup_deferred": True, "media_payloads_opened": 0,
              "backup_root": str(backup), "previous_workstation": str(previous)}
    try:
        anchors = {}
        if args.check_text_file is not None:
            if identity(args.check_text_file)[2] > 64 * 1024:
                raise ValueError("Text acceptance inputs exceed their explicit budget.")
            anchors = json.loads(args.check_text_file.read_bytes())
            if not isinstance(anchors, dict) or not all(
                isinstance(k, str) and isinstance(v, str) and v
                for k, v in anchors.items()
            ):
                raise ValueError("Text checks must map labels to nonempty literals.")
            result["check_text_file_sha256"] = digest(args.check_text_file)
        metadata = {}
        for name in ("Info.plist", "Status.plist", "Manifest.plist", "Manifest.db"):
            path = backup / name
            metadata[name] = {"identity": identity(path, database=name.endswith(".db")),
                              "sha256": digest(path)}
        info = plistlib.loads((backup / "Info.plist").read_bytes())
        status = plistlib.loads((backup / "Status.plist").read_bytes())
        manifest = plistlib.loads((backup / "Manifest.plist").read_bytes())
        if status.get("SnapshotState") != "finished" or manifest.get("IsEncrypted") is not False:
            raise ValueError("A finished, unencrypted, stable device snapshot is required.")
        stamp = info["Last Backup Date"].replace(tzinfo=UTC)
        snapshot = (f"ipad-{backup.name}-{stamp:%Y%m%dT%H%M%SZ}-"
                    + metadata["Manifest.db"]["sha256"][:16])
        result.update(snapshot_id=snapshot, metadata=metadata,
                      backup_date_utc=stamp.isoformat(), product_type=info.get("Product Type"))
        old_sources = json.loads((previous / "evidence/sources-v1.0.0.json").read_bytes())
        result["previous_verified_publication"] = require_previous_publication(previous)
        old_by_path = {d["path"]: d for source in old_sources for d in source["input_databases"]}
        old_raw = previous / "raw" / ("ipad-" + backup.name)
        prior_preservation = previous / "evidence/preservation-v1.0.0.json"
        prior_copies = (
            json.loads(prior_preservation.read_bytes())["copies"]
            if prior_preservation.exists() else {}
        )
        originals = {}
        with readonly(backup / "Manifest.db") as database:
            for fid, relative in database.execute(
                "SELECT fileID,relativePath FROM Files WHERE domain=? AND flags=1 ORDER BY relativePath",
                (DOMAIN,),
            ):
                if not (DATABASE.fullmatch(relative) or relative == "Documents/LoginInfo2.dat"
                        or re.fullmatch(r"Documents/MMappedKV/mmkv_name_edge_computing_run_record__[A-Za-z0-9_.@-]+", relative)):
                    continue
                if fid != hashlib.sha1((DOMAIN + "-" + relative).encode(), usedforsecurity=False).hexdigest():
                    raise ValueError("A manifest file ID lost its domain/path preimage.")
                originals[relative] = {"file_id": fid, "domain": DOMAIN, "path": relative}
            reject_manifest_sidecars(database, originals)
        if not originals:
            raise ValueError("The backup contains no selected WeChat source files.")
        write_new(work / "evidence/discovery-plan-v1.0.0.json", {
            **result, "files": originals, "minimum_free_bytes": args.minimum_free_bytes,
            "script_sha256": digest(Path(__file__).absolute()),
        })
        copies = {}
        for name, proof in metadata.items():
            guard()
            target = work / "evidence" / name
            _clonefile(backup / name, target)
            if digest(target) != proof["sha256"]:
                raise ValueError("A backup metadata clone differs from its source.")
        for number, (relative, entry) in enumerate(originals.items(), 1):
            guard()
            fid = entry["file_id"]
            source, target = backup / fid[:2] / fid, work / "raw" / fid
            before = identity(source, database=bool(DATABASE.fullmatch(relative)))
            hashed = digest(source)
            _clonefile(source, target)
            if digest(target) != hashed or identity(source) != before:
                raise ValueError("A preserved native file changed while cloning.")
            copies[relative] = {**entry, "sha256": hashed, "bytes": before[2],
                                "local_path": str(target), "source_identity": before}
            emit("source_preserved", ordinal=number, total=len(originals),
                 relative_path=relative, bytes=before[2])
        write_new(work / "evidence/preservation-v1.0.0.json", {
            **result, "copies": copies, "status": "all_selected_sources_full_sha_verified",
        })
        owners = {}
        prefix = "Documents/MMappedKV/mmkv_name_edge_computing_run_record__"
        for relative in copies:
            if relative.startswith(prefix) and not relative.endswith(".crc"):
                owner = relative[len(prefix):]
                account_hash = hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest()
                if account_hash in owners and owners[account_hash] != owner:
                    raise ValueError("Native account filename evidence is ambiguous.")
                owners[account_hash] = owner
        login = copies["Documents/LoginInfo2.dat"]
        fresh = {row["source_account_id"] for row in parse_login_info(Path(login["local_path"]).read_bytes())}
        sources, databases, totals = [], [], Counter()
        anchor_hits = {key: [] for key in anchors}
        with (work / "evidence/native-row-deltas-v1.0.0.jsonl").open("x") as changes:
            for account_hash, owner in sorted(owners.items()):
                shards = [p for p in copies if re.fullmatch(
                    "Documents/" + account_hash + r"/DB/message_[0-9]+\.sqlite", p)]
                if not shards:
                    continue
                mmkv = copies[prefix + owner]
                source = {"source_id": "ios-" + account_hash, "source_account_id": owner,
                          "account_hash": account_hash, "account_hash_verified": True,
                          "snapshot_id": snapshot, "backup_id": snapshot,
                          "source_file": f"iPad backup · {stamp.day} {stamp:%b %Y}",
                          "source_label": "iPad archive", "source_backup": str(backup),
                          "source_backup_identifier": backup.name,
                          "backup_manifest_sha256": metadata["Manifest.db"]["sha256"],
                          "backup_info_last_backup_date_utc": stamp.isoformat(),
                          "backup_snapshot_state": "finished", "backup_is_encrypted": False,
                          "backup_is_full": status.get("IsFullBackup"),
                          "checkpointed_only": True, "fresh_login_info_contains_owner": owner in fresh,
                          "profile_kind": "native_cached_profile", "message_history_completeness": "not_proven",
                          "identity_evidence": {"kind": "native_mmkv_filename_with_directory_md5",
                              "directory_hash": account_hash, "native_filename_username": owner,
                              "fresh_manifest_path_verified": True, "mmkv_domain": DOMAIN,
                              "mmkv_filename": prefix + owner, "mmkv_file_id": mmkv["file_id"],
                              "mmkv_sha256": mmkv["sha256"], "nickname_inference": False},
                          "input_databases": [{k: copies[p][k] for k in ("path", "file_id", "domain", "sha256", "bytes")} for p in shards],
                          "native_peer_evidence": [{"database_relative_path": p,
                              **{k: copies[p][k] for k in ("file_id", "sha256", "bytes")}}
                              for p in copies if p in (f"Documents/{account_hash}/DB/MM.sqlite", f"Documents/{account_hash}/DB/WCDB_Contact.sqlite")]}
                sources.append(source)
                for relative in shards:
                    guard()
                    entry = copies[relative]
                    prior = old_by_path.get(relative)
                    record = {"source_id": source["source_id"], **entry, "tables": []}
                    databases.append(record)
                    if prior and prior["sha256"] == entry["sha256"]:
                        record["state"] = "exact_database_bytes_unchanged"
                        totals["unchanged_databases"] += 1
                        continue
                    old_path = (
                        Path(prior_copies[relative]["local_path"])
                        if relative in prior_copies
                        else old_raw / prior["file_id"] if prior else None
                    )
                    if old_path is not None and digest(old_path) != prior["sha256"]:
                        raise ValueError("The retained prior native database failed its full hash.")
                    old = readonly(old_path) if old_path else None
                    new = readonly(Path(entry["local_path"]))
                    try:
                        old_tables = {row[0] for row in old.execute("SELECT name FROM sqlite_master WHERE type='table'") if CHAT.fullmatch(row[0])} if old else set()
                        new_tables = {row[0] for row in new.execute("SELECT name FROM sqlite_master WHERE type='table'") if CHAT.fullmatch(row[0])}
                        if old_tables - new_tables:
                            record["removed_tables_held"] = sorted(old_tables - new_tables)
                        for table in sorted(new_tables):
                            counts = Counter()
                            for delta in iter_native_row_deltas(old if table in old_tables else None, new, table):
                                counts[delta.kind] += 1
                                row = delta.after or delta.before
                                raw = row.get("Message")
                                blob = raw.encode() if isinstance(raw, str) else bytes(raw or b"")
                                metadata_row = {k: v for k, v in row.items() if k != "Message"}
                                change = {"source_id": source["source_id"], "database_relative_path": relative,
                                          "database_sha256": entry["sha256"], "chat_table": table,
                                          "kind": delta.kind, "key": delta.key, "native": metadata_row,
                                          "message_sha256": hashlib.sha256(blob).hexdigest(),
                                          "sqlite_storage_class": type(raw).__name__}
                                changes.write(encoded(change) + "\n")
                                if isinstance(raw, str):
                                    for key, phrase in anchors.items():
                                        if phrase in raw:
                                            anchor_hits[key].append(change)
                            record["tables"].append({"table": table, "changes": dict(counts)})
                            totals.update(counts)
                        record["state"] = "complete_primary_key_comparison"
                    finally:
                        if old:
                            old.close()
                        new.close()
                    emit("database_compared", path=relative, changed_tables=sum(bool(t["changes"]) for t in record["tables"]), totals=dict(totals))
        for name, proof in metadata.items():
            if identity(backup / name) != proof["identity"] or digest(backup / name) != proof["sha256"]:
                raise ValueError("Backup metadata changed before the snapshot comparison completed.")
        write_new(work / "evidence/sources-v1.0.0.json", sources)
        result.update(state="preserved_and_compared_not_imported", sources=len(sources),
                      databases=databases, counts=dict(totals), plaintext_delta_anchor_hits=anchor_hits)
        write_new(work / "evidence/comparison-v1.0.0.json", result)
        emit("comparison_verified", counts=dict(totals))
    except BaseException as error:
        result.update(state="stopped_artifacts_preserved", error_type=type(error).__name__, error=str(error))
        write_new(work / "evidence/stopped-v1.0.0.json", result)
        raise


if __name__ == "__main__":
    main()
