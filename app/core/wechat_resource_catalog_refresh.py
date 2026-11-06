"""Stage a streaming, source-proven refresh of an existing native resource catalog.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3

from .wechat_archive import (
    _identifier,
    wechat_archive_path,
    wechat_history_partition_fingerprint,
)
from .wechat_native_resource_revalidation import (
    _anchor,
    _owner,
    _receipts,
    _relative,
    _resident,
    _validate_row,
)
from .wechat_resource_index import _SCHEMA, _json, _readonly


VERSION = "v1.0.1-codex.0"
MAX_CONVERSATIONS = 10_000
MAX_RESOURCE_ROWS = 20_000_000
MAX_PRIMARY_OBSERVATIONS = 50_000_000
MAX_METADATA_BYTES = 64 * 1024**3
MAX_CELL_BYTES = 16 * 1024**2
_COLUMNS = (
    "id",
    "conversation_id",
    "ordinal",
    "sender_id",
    "sender_label",
    "direction",
    "text",
    "time_sort",
    "timestamp_value",
    "timestamp_label",
    "time_basis",
    "provenance",
    "quality_flags",
    "search_text",
)
_SCOPE = ("partition_id", "account_key", "peer_id", "kind")


def _bounded_json(raw):
    if not isinstance(raw, str) or len(raw.encode()) > MAX_CELL_BYTES:
        raise ValueError("One native metadata cell exceeds the refresh budget.")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("A native observation must retain an object provenance.")
    return value


def _budget(report, raw, *, primary=False):
    count = "primary_observations" if primary else "checked_resources"
    maximum = MAX_PRIMARY_OBSERVATIONS if primary else MAX_RESOURCE_ROWS
    report[count] += 1
    report["checked_metadata_bytes"] += len(raw.encode())
    if report[count] > maximum or report["checked_metadata_bytes"] > MAX_METADATA_BYTES:
        raise ValueError(
            "The full native refresh exceeds its explicit metadata budget."
        )


def _save_anchor(output, proof, direction, primary_id, conversation, snapshots):
    snapshot = snapshots.get(proof.get("source_snapshot"))
    if snapshot is None:
        return
    locator = proof.get("locator")
    if not isinstance(locator, dict) or not isinstance(locator.get("database"), str):
        raise ValueError("A proven native observation lost its database locator.")
    owner = conversation["account_key"][8:]
    try:
        path = _relative(
            "Documents/"
            + conversation["partition_id"][4:]
            + "/DB/"
            + locator["database"],
            owner,
        )
    except ValueError:
        # The preserved snapshot can also contain unrelated native databases.
        return
    digest = snapshot[1].get(path)
    if digest is None:
        return
    table = (
        "Chat_"
        + hashlib.md5(
            conversation["peer_id"].encode(),
            usedforsecurity=False,
        ).hexdigest()
    )
    if locator.get("table") != table or direction not in {
        "sent",
        "received",
        "unknown",
    }:
        raise ValueError("A current native anchor crosses its conversation scope.")
    local, signature = _anchor(proof, direction, locator)
    key = (snapshot[0], path, digest, table, local)
    encoded = _json(signature)
    existing = output.execute(
        "SELECT signature FROM temp.current_native_anchors WHERE "
        "backup=? AND path=? AND digest=? AND chat_table=? AND local_id=?",
        key,
    ).fetchone()
    if existing and existing[0] != encoded:
        raise ValueError("One current native locator has conflicting event evidence.")
    output.execute(
        "INSERT OR IGNORE INTO temp.current_native_anchors VALUES(?,?,?,?,?,?,?)",
        (*key, encoded, primary_id),
    )


def _current_anchors(history, output, conversation, snapshots, report):
    output.execute("DELETE FROM temp.current_native_anchors")
    query = (
        "SELECT id,direction,CASE WHEN length(CAST(provenance AS BLOB))<=? "
        "THEN provenance END AS provenance FROM messages WHERE conversation_id=?"
    )
    for row in history.execute(query, (MAX_CELL_BYTES, conversation["id"])):
        proof = _bounded_json(row["provenance"])
        _budget(report, row["provenance"], primary=True)
        _save_anchor(
            output, proof, row["direction"], row["id"], conversation, snapshots
        )
    if (
        history.execute(
            "SELECT 1 FROM sqlite_master WHERE name='message_evidence' AND type='table'"
        ).fetchone()
        is None
    ):
        return
    query = (
        "SELECT m.id,e.snapshot_id,CASE WHEN length(CAST(e.observation AS BLOB))<=? "
        "THEN e.observation END AS observation FROM messages m CROSS JOIN "
        "message_evidence e ON e.message_id=m.id WHERE m.conversation_id=? "
        "AND e.source_id=?"
    )
    for row in history.execute(
        query,
        (MAX_CELL_BYTES, conversation["id"], conversation["partition_id"]),
    ):
        proof = _bounded_json(row["observation"])
        _budget(report, row["observation"], primary=True)
        relevant = [
            (identifier, value)
            for identifier, value in snapshots.items()
            if value[0] == row["snapshot_id"]
        ]
        if not relevant:
            continue
        if "source_snapshot" not in proof:
            locator = proof.get("locator")
            if not isinstance(locator, dict) or not isinstance(
                locator.get("database"), str
            ):
                raise ValueError(
                    "A native evidence observation lost its database locator."
                )
            try:
                path = _relative(
                    "Documents/"
                    + conversation["partition_id"][4:]
                    + "/DB/"
                    + locator["database"],
                    conversation["account_key"][8:],
                )
            except ValueError:
                continue
            matching = [
                identifier for identifier, value in relevant if path in value[1]
            ]
            if not matching:
                continue
            # The envelope's source ID and snapshot string select an existing
            # preserved receipt, not a timestamp or a nearby message guess.
            proof = {**proof, "source_snapshot": matching[0]}
        snapshot = snapshots.get(proof.get("source_snapshot"))
        if snapshot is None or snapshot[0] != row["snapshot_id"]:
            raise ValueError("A native evidence observation changed its snapshot.")
        _save_anchor(
            output, proof, proof.get("direction"), row["id"], conversation, snapshots
        )


def _resource_rows(source, conversation_id):
    columns = ",".join(
        "CASE WHEN length(CAST("
        + column
        + " AS BLOB))<=:maximum THEN "
        + column
        + " END AS "
        + column
        for column in _COLUMNS
    )
    return source.execute(
        "SELECT " + columns + " FROM messages WHERE conversation_id=:cid "
        "ORDER BY time_sort,ordinal,id",
        {"maximum": MAX_CELL_BYTES, "cid": conversation_id},
    )


def _summary(conversation, count, first, last):
    return tuple(
        conversation[key]
        for key in (
            "id",
            "partition_id",
            "account_key",
            "peer_id",
            "title",
            "kind",
        )
    ) + (count, first[7], last[7], first[8], last[8], last[6][:300])


def stage_resource_catalog_refresh(local_store_root, candidate_path, *, progress=None):
    """Stage a complete refresh of existing metadata; publication remains explicit.

    Source databases, media payloads, and cloud-only files are never opened. Only
    source-proven events absent from the current primary survive in the candidate.
    """
    history_path = wechat_archive_path(local_store_root).absolute()
    original = history_path.with_name("resource_references.sqlite3")
    identities = {path: _resident(path) for path in (history_path, original)}
    candidate = Path(candidate_path).absolute()
    if candidate.exists() or candidate.is_symlink():
        raise FileExistsError("Keep existing resource refresh candidates unchanged.")
    if not candidate.parent.is_dir() or candidate.parent.is_symlink():
        raise ValueError("Select an existing regular candidate directory.")
    if candidate in identities:
        raise ValueError("A resource refresh candidate cannot replace either input.")
    if (
        shutil.disk_usage(candidate.parent).free
        < identities[original]["size"] * 2 + 64 * 1024**2
    ):
        raise ValueError("Insufficient space for a streamed native metadata candidate.")
    report = {
        "version": VERSION,
        "source": str(original),
        "candidate": str(candidate),
        "source_identity": identities[original],
        "history_identity": identities[history_path],
        "coverage": "existing_catalog_only",
        "checked_resources": 0,
        "primary_observations": 0,
        "checked_metadata_bytes": 0,
        "source_conversations": 0,
        "conversations": 0,
        "messages": 0,
        "excluded_existing_records": 0,
        "kinds": {},
        "excluded_kinds": {},
        "media_payload_files_read": 0,
        "cloud_inputs_hydrated": 0,
        "published": False,
    }
    counts, excluded = Counter(), Counter()
    account_counts, source_cache = Counter(), {}
    try:
        with (
            _readonly(history_path) as history,
            _readonly(original) as source,
            closing(
                sqlite3.connect(candidate),
            ) as output,
        ):
            candidate.chmod(0o600)
            output.executescript(_SCHEMA)
            output.execute("PRAGMA temp_store=FILE")
            output.execute(
                "CREATE TEMP TABLE current_native_anchors (backup TEXT,path TEXT,"
                "digest TEXT,chat_table TEXT,local_id INTEGER,signature TEXT,"
                "primary_id TEXT,PRIMARY KEY(backup,path,digest,chat_table,local_id)) "
                "WITHOUT ROWID"
            )
            fingerprint = wechat_history_partition_fingerprint(history)
            info = dict(source.execute("SELECT key,value FROM archive_info"))
            if info.get("schema_version") != "1":
                raise ValueError(
                    "The existing native catalog has an unsupported schema."
                )
            for table in ("messages", "conversations"):
                if (
                    source.execute(
                        "SELECT 1 FROM sqlite_master WHERE name=? AND type='table' "
                        "AND upper(sql) NOT LIKE '%VIRTUAL%'",
                        (table,),
                    ).fetchone()
                    is None
                ):
                    raise ValueError(
                        "Refresh requires ordinary published catalog tables."
                    )
            if source.execute(
                "SELECT 1 FROM messages m WHERE NOT EXISTS "
                "(SELECT 1 FROM conversations c WHERE c.id=m.conversation_id) LIMIT 1"
            ).fetchone():
                raise ValueError(
                    "The source resource catalog contains an orphan event."
                )
            conversations = source.execute("SELECT * FROM conversations ORDER BY id")
            for prior in conversations:
                report["source_conversations"] += 1
                if report["source_conversations"] > MAX_CONVERSATIONS:
                    raise ValueError(
                        "The source resource scopes exceed the refresh budget."
                    )
                current = history.execute(
                    "SELECT * FROM conversations WHERE id=?",
                    (prior["id"],),
                ).fetchone()
                if current is not None and any(
                    current[key] != prior[key] for key in _SCOPE
                ):
                    raise ValueError(
                        "A resource conversation changed account or peer scope."
                    )
                partition = history.execute(
                    "SELECT * FROM partitions WHERE id=?",
                    (prior["partition_id"],),
                ).fetchone()
                if partition is None or not prior["peer_id"]:
                    raise ValueError(
                        "A resource conversation has no verified native owner."
                    )
                owner = _owner(partition)
                if prior["account_key"] != partition["account_key"] or (
                    current is None
                    and prior["id"]
                    != "wechat:" + _identifier(prior["partition_id"], prior["peer_id"])
                ):
                    raise ValueError(
                        "A resource-only conversation has no exact source scope."
                    )
                conversation = current or prior
                if partition["id"] not in source_cache:
                    source_cache[partition["id"]] = _receipts(history, partition)
                receipt_owner, receipts, snapshots = source_cache[partition["id"]]
                if owner != receipt_owner:
                    raise ValueError("The native source receipt has another account.")
                _current_anchors(history, output, conversation, snapshots, report)
                source_count = kept = 0
                first = last = None
                batch = []
                for row in _resource_rows(source, prior["id"]):
                    if row["id"] is None or row["provenance"] is None:
                        raise ValueError(
                            "One resource event exceeds its metadata budget."
                        )
                    if (
                        any(
                            row[column] is None
                            for column in _COLUMNS
                            if column != "sender_id"
                        )
                        or sum(
                            len(value.encode())
                            for value in row
                            if isinstance(value, str)
                        )
                        > MAX_CELL_BYTES
                    ):
                        raise ValueError(
                            "One resource event exceeds its metadata budget."
                        )
                    _budget(report, row["provenance"])
                    source_count += 1
                    key, signature, _, kind = _validate_row(
                        row,
                        conversation,
                        owner,
                        receipts,
                        snapshots,
                    )
                    anchor = output.execute(
                        "SELECT signature FROM temp.current_native_anchors WHERE "
                        "backup=? AND path=? AND digest=? AND chat_table=? AND local_id=?",
                        key,
                    ).fetchone()
                    if anchor:
                        if anchor[0] != _json(signature):
                            raise ValueError(
                                "A shared native locator has conflicting event evidence."
                            )
                        excluded[kind] += 1
                        report["excluded_existing_records"] += 1
                    else:
                        if history.execute(
                            "SELECT 1 FROM messages WHERE id=?", (row["id"],)
                        ).fetchone():
                            raise ValueError(
                                "A resource ID has unrelated current primary evidence."
                            )
                        values = tuple(row)
                        batch.append(values)
                        kept += 1
                        counts[kind] += 1
                        account_counts[owner] += 1
                        first_key = (values[7] == 0, values[7], values[2], values[0])
                        last_key = (values[7], values[2], values[0])
                        if first is None or first_key < (
                            first[7] == 0,
                            first[7],
                            first[2],
                            first[0],
                        ):
                            first = values
                        if last is None or last_key > (last[7], last[2], last[0]):
                            last = values
                        if len(batch) >= 1000:
                            output.executemany(
                                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                batch,
                            )
                            batch.clear()
                    if progress and report["checked_resources"] % 100_000 == 0:
                        progress(
                            {
                                **report,
                                "phase": "resources",
                                "conversation": prior["id"],
                            }
                        )
                if source_count != prior["message_count"]:
                    raise ValueError(
                        "A source conversation count differs from its event rows."
                    )
                if batch:
                    output.executemany(
                        "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        batch,
                    )
                if kept:
                    output.execute(
                        "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        _summary(conversation, kept, first, last),
                    )
                    report["conversations"] += 1
                report["messages"] += kept
                if progress:
                    progress(
                        {**report, "phase": "conversation", "conversation": prior["id"]}
                    )
            report["kinds"] = dict(sorted(counts.items()))
            report["excluded_kinds"] = dict(sorted(excluded.items()))
            report["accounts"] = [
                {"account_id": owner, "messages": count}
                for owner, count in sorted(account_counts.items())
            ]
            if (
                "messages" in info
                and int(info["messages"]) != report["checked_resources"]
            ):
                raise ValueError(
                    "The source catalog total differs from its checked event rows."
                )
            metadata = {
                "schema_version": "1",
                "proof_version": VERSION,
                "history_file_identity": _json(identities[history_path]),
                "history_partitions_sha256": fingerprint,
                "messages": str(report["messages"]),
                "backup_id": info.get("backup_id", ""),
                "report": _json(report),
            }
            output.executemany("INSERT INTO archive_info VALUES(?,?)", metadata.items())
            output.commit()
            if output.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError(
                    "The refreshed resource candidate failed integrity checks."
                )
        if any(_resident(path) != identity for path, identity in identities.items()):
            raise ValueError("A resource input changed before refresh completion.")
        with candidate.open("r+b") as stream:
            os.fsync(stream.fileno())
        report["candidate_bytes"] = candidate.stat().st_size
        return report
    except BaseException:
        candidate.unlink(missing_ok=True)
        raise
