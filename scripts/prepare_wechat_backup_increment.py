"""Project an exact retained native delta into replayable append-only inputs.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.wechat_native_delta import project_native_text
from app.core.wechat_wcdb import MissingDictionaryError
from scripts.inspect_wechat_backup_increment import digest, encoded, identity, readonly, write_new


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workstation", type=Path, required=True)
    parser.add_argument("--local-store", type=Path, required=True)
    parser.add_argument("--check-text-file", type=Path)
    args = parser.parse_args()
    work = args.workstation.absolute()
    operation = work / "package-v1.0.0"
    operation.mkdir(mode=0o700)
    result = {"version": "v1.0.1-codex.0", "canonical_written": False,
              "runtime_media_payloads_copied": False}
    try:
        sources = json.loads((work / "evidence/sources-v1.0.0.json").read_bytes())
        preservation = json.loads((work / "evidence/preservation-v1.0.0.json").read_bytes())
        comparison = json.loads((work / "evidence/comparison-v1.0.0.json").read_bytes())
        if comparison["state"] != "preserved_and_compared_not_imported":
            raise ValueError("Select a complete, unchanged, retained source comparison.")
        copied = preservation["copies"]
        by_source = {s["source_id"]: s for s in sources}
        anchors = json.loads(args.check_text_file.read_bytes()) if args.check_text_file else {}
        pending = defaultdict(list)
        delta_path = work / "evidence/native-row-deltas-v1.0.0.jsonl"
        if identity(delta_path)[2] > 128 * 1024**2:
            raise ValueError("Use a separately reviewed streaming package for a delta above 128 MiB.")
        for number, line in enumerate(delta_path.open(), 1):
            if number > 100_000:
                raise ValueError("The bounded incremental package exceeds 100,000 changed rows.")
            row = json.loads(line)
            pending[row["database_relative_path"]].append(row)
        history = args.local_store.absolute() / "messaging/wechat/history.sqlite3"
        baseline = identity(history, database=True)
        result["canonical_history"] = {"path": str(history), "identity": baseline, "sha256": digest(history)}
        peers = defaultdict(lambda: defaultdict(set))
        with readonly(history) as canonical:
            for sid, peer in canonical.execute("SELECT partition_id,peer_id FROM conversations"):
                if isinstance(peer, str) and peer:
                    peers[sid]["Chat_" + hashlib.md5(peer.encode(), usedforsecurity=False).hexdigest()].add(peer)
        for source in sources:
            sid = source["source_id"]
            for proof in source["native_peer_evidence"]:
                entry = copied[proof["database_relative_path"]]
                path = Path(entry["local_path"])
                if digest(path) != proof["sha256"]:
                    raise ValueError("A fresh native peer database changed.")
                with readonly(path) as native:
                    for table, column in (("Friend", "userName"), ("OpenIMContact", "userName"), ("MessageOfAppExtTable", "chatUsername")):
                        if not native.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                            continue
                        if column not in {r[1] for r in native.execute(f'PRAGMA table_info("{table}")')}:
                            continue
                        for (peer,) in native.execute(f'SELECT DISTINCT "{column}" FROM "{table}" WHERE typeof("{column}")=\'text\''):
                            if peer:
                                peers[sid]["Chat_" + hashlib.md5(peer.encode(), usedforsecurity=False).hexdigest()].add(peer)
        counts, gaps, found, specs = Counter(), [], {key: [] for key in anchors}, []
        with (operation / "records-v1.0.0.jsonl").open("x") as output:
            for database in comparison["databases"]:
                relative, sid = database["path"], database["source_id"]
                selected = [table["table"] for table in database["tables"] if table["changes"]]
                if not selected:
                    continue
                entry = copied[relative]
                path = Path(entry["local_path"])
                if digest(path) != entry["sha256"]:
                    raise ValueError("A preserved changed message database failed its complete hash.")
                specs.append({"source_id": sid, "database_path": str(path),
                              "database_relative_path": relative,
                              "source_database_sha256": entry["sha256"],
                              "chat_tables": selected,
                              "peer_ids": sorted({peer for values in peers[sid].values() for peer in values})})
                with readonly(path) as native:
                    native.row_factory = sqlite3.Row
                    for change in pending[relative]:
                        if change["kind"] == "deleted":
                            counts["deleted_native_observations_retained_in_prior_snapshot"] += 1
                            continue
                        table = change["chat_table"]
                        row = native.execute(f'SELECT rowid AS source_rowid,* FROM "{table}" WHERE rowid=?', (change["native"]["source_rowid"],)).fetchone()
                        if row is None:
                            raise ValueError("An exact source row disappeared after comparison.")
                        row = dict(row)
                        raw = row["Message"]
                        blob = raw.encode() if isinstance(raw, str) else bytes(raw or b"")
                        if hashlib.sha256(blob).hexdigest() != change["message_sha256"]:
                            raise ValueError("A source body differs from its exact delta observation.")
                        if row["Type"] not in {1, 49, 10000, 10002}:
                            counts["metadata_only"] += 1
                            continue
                        candidates = peers[sid].get(table, set())
                        if len(candidates) != 1:
                            gaps.append({**change, "reason": "unresolved_native_peer_preimage"})
                            counts["peer_gap"] += 1
                            continue
                        peer = next(iter(candidates))
                        kind = "group" if peer.endswith(("@chatroom", "@im.chatroom")) else "direct"
                        try:
                            record = project_native_text(row, source=by_source[sid], database=entry,
                                                         table=table, peer=peer, kind=kind)
                        except (ValueError, RuntimeError, TypeError, UnicodeError) as error:
                            gap = {**change, "reason": type(error).__name__}
                            if isinstance(error, MissingDictionaryError):
                                gap["dictionary_id"] = error.dictionary_id
                            gaps.append(gap)
                            counts["decode_or_projector_gap"] += 1
                            continue
                        output.write(encoded(record) + "\n")
                        counts["records"] += 1
                        counts["native_type:" + str(row["Type"]) ] += 1
                        for key, phrase in anchors.items():
                            if phrase in record["text"] or phrase in encoded(record.get("source_locator", {})):
                                found[key].append({"record_id": record["extraction_record_id"],
                                                   "conversation_id": peer, "source_id": sid,
                                                   "body_sha256": hashlib.sha256(record["text"].encode()).hexdigest(),
                                                   "message_type": row["Type"],
                                                   "text_extraction_kind": record["text_extraction_kind"]})
                print(encoded({"event": "packaged", "database": relative, "counts": dict(counts)}), flush=True)
        if identity(history, database=True) != baseline:
            raise ValueError("The canonical baseline changed during source preparation.")
        write_new(operation / "gaps-v1.0.0.json", gaps)
        write_new(operation / "resource-specs-v1.0.0.json", specs)
        result.update(state="prepared_not_imported", counts=dict(counts), anchor_hits=found,
                      resource_specifications=len(specs), complete_changed_chat_tables=sum(len(s["chat_tables"]) for s in specs),
                      records_sha256=digest(operation / "records-v1.0.0.jsonl"),
                      gaps_sha256=digest(operation / "gaps-v1.0.0.json"),
                      source_metadata_sha256=digest(work / "evidence/sources-v1.0.0.json"))
        write_new(operation / "report-v1.0.0.json", result)
        print(encoded({"state": result["state"], "counts": dict(counts),
                       "anchor_hits": {k: len(v) for k, v in found.items()}}), flush=True)
    except BaseException as error:
        write_new(operation / "stopped-v1.0.0.json", {**result, "error_type": type(error).__name__, "error": str(error)})
        raise


if __name__ == "__main__":
    main()
