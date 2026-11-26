"""Stage preserved native metadata scopes without reading attachment payloads.

Code version: v1.2.1-codex.0
"""

from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from .wechat_archive import (
    _identifier,
    _time_fields,
    wechat_archive_path,
    wechat_history_partition_fingerprint,
)
from .wechat_native_resource_revalidation import (
    VERSION as PROOF_VERSION,
    _content_digest,
    _owner,
    _resident,
    _sha256,
    _verified_coverage,
)
from .wechat_native_resource_receipts import WITNESS_SCHEMA
from .wechat_resource_index import (
    _APP_LABEL_FIELDS,
    _KINDS,
    _LABELS,
    _SCHEMA,
    _app_metadata,
    _app_labels as _app_labels,
    _group_sender_prefix,
    _json,
    _readonly,
    _table_rows,
)


VERSION = "v1.2.0-codex.0"
MAX_PRIMARY_ROWS = 200_000
MAX_METADATA_BYTES = 512 * 1024**2
MAX_EVENTS = 100_000
_HEX = re.compile(r"[0-9a-f]{64}")
_FILE_ID = re.compile(r"[0-9a-f]{40}")
_USER = re.compile(r"[A-Za-z0-9_.@-]{1,256}")
_PRESERVED_PROOF_KEYS = (
    "supplemental_metadata",
    "canonical_file_identity",
    "supplement_source_receipts",
)


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate preserved metadata key.")
        value[key] = item
    return value


def _json_input(path, inputs, *, limit=4 * 1024**2):
    path = Path(path).absolute()
    identity = _resident(path)
    if identity["size"] > limit:
        raise ValueError("A preserved metadata receipt exceeds its byte budget.")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit or _resident(path) != identity:
        raise ValueError("A preserved metadata receipt changed while reading.")
    inputs[str(path)] = {
        "identity": identity,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    return json.loads(raw, object_pairs_hook=_unique)


def _current(path):
    return _resident(Path(path))


def _unchanged(inputs):
    if any(_current(path) != item["identity"] for path, item in inputs.items()):
        raise ValueError("A preserved metadata input changed during staging.")


def _time(epoch):
    if type(epoch) is not int or epoch <= 0:
        raise ValueError("A native metadata event needs its recorded positive epoch.")
    try:
        iso = datetime.fromtimestamp(epoch, UTC).isoformat()
    except (ValueError, OverflowError, OSError) as error:
        raise ValueError(
            "A native metadata timestamp is outside its supported range."
        ) from error
    return _time_fields({"sent_at_utc_iso": iso})


def _signature(server, epoch, kind, direction):
    if (
        type(epoch) is not int
        or type(kind) is not int
        or direction not in {"sent", "received", "unknown"}
    ):
        raise ValueError("A native source anchor is incomplete.")
    if (
        server is None
        or type(server) is int
        and server == 0
        or type(server) is str
        and server in {"", "0"}
    ):
        normalized = None
    elif type(server) is int and 0 < server <= 9_223_372_036_854_775_807:
        normalized = str(server)
    elif (
        type(server) is str
        and re.fullmatch(r"[1-9][0-9]{0,18}", server)
        and int(server) <= 9_223_372_036_854_775_807
    ):
        normalized = server
    else:
        raise ValueError("A native source server identifier is malformed.")
    return normalized, epoch, kind, direction


def _binding(history, cid, snapshot_id, report, sources, database, inputs):
    if type(snapshot_id) is not int or snapshot_id <= 0:
        raise ValueError("Select an exact preserved native source snapshot.")
    conversation = history.execute(
        "SELECT * FROM conversations WHERE id=?", (cid,)
    ).fetchone()
    if conversation is None:
        raise ValueError("The selected native conversation is unavailable.")
    partition = history.execute(
        "SELECT * FROM partitions WHERE id=?", (conversation["partition_id"],)
    ).fetchone()
    if partition is None:
        raise ValueError("The selected native account is unavailable.")
    owner = _owner(partition)
    peer = conversation["peer_id"]
    if (
        conversation["account_key"] != partition["account_key"]
        or not isinstance(peer, str)
        or _USER.fullmatch(peer) is None
        or conversation["kind"] != ("group" if peer.endswith("@chatroom") else "direct")
    ):
        raise ValueError("The native conversation lost its exact account or peer.")
    snapshot = history.execute(
        "SELECT * FROM source_snapshots WHERE id=?", (snapshot_id,)
    ).fetchone()
    if snapshot is None or snapshot["source_id"] != partition["id"]:
        raise ValueError("The preserved source belongs to another native account.")
    metadata = json.loads(snapshot["metadata"])
    if (
        metadata.get("source_id") != partition["id"]
        or metadata.get("source_account_id") != owner
        or metadata.get("account_hash_verified") is not True
        or metadata.get("account_hash") != partition["id"][4:]
        or not isinstance(report, dict)
        or report.get("snapshot_id") != snapshot["snapshot_id"]
        or not isinstance(sources, list)
        or len(sources) > 128
    ):
        raise ValueError("The preserved source lacks its verified account receipt.")
    declared_sources = [
        value
        for value in sources
        if isinstance(value, dict) and value.get("source_id") == partition["id"]
    ]
    if (
        len(declared_sources) != 1
        or any(metadata.get(key) != value for key, value in declared_sources[0].items())
        or set(metadata) - set(declared_sources[0]) - {"cross_validation"}
    ):
        raise ValueError(
            "The preserved source metadata differs from its current snapshot."
        )
    relative = "Documents/" + partition["id"][4:] + "/DB/" + database.name
    if re.fullmatch(r"message_[0-9]+\.sqlite|MM\.sqlite", database.name) is None:
        raise ValueError("Select one native message database.")
    entries = report.get("selected_files")
    if not isinstance(entries, list) or len(entries) > 100_000:
        raise ValueError("The preservation report lacks a bounded file inventory.")
    selected = [
        value
        for value in entries
        if isinstance(value, dict)
        and value.get("domain") == "AppDomain-com.tencent.xin"
        and value.get("relative_path") == relative
    ]
    if len(selected) != 1:
        raise ValueError(
            "The preservation report must select one exact native database."
        )
    entry = selected[0]
    digest = entry.get("sha256")
    if (
        not isinstance(digest, str)
        or _HEX.fullmatch(digest) is None
        or entry.get("snapshot_sha256") != digest
        or entry.get("source_sha256") != digest
        or entry.get("bytes") != inputs[str(database)]["identity"]["size"]
        or not isinstance(entry.get("file_id"), str)
        or _FILE_ID.fullmatch(entry["file_id"]) is None
        or entry["file_id"]
        != hashlib.sha1(
            ("AppDomain-com.tencent.xin-" + relative).encode(), usedforsecurity=False
        ).hexdigest()
        or entry.get("input_relative_path") != "inputs/" + relative
    ):
        raise ValueError("The native database differs from its preserved file receipt.")
    return dict(conversation), owner, dict(snapshot), relative, digest, entry["file_id"]


def _anchors(history, native, conversation, source_id, relative, digest, table):
    verified_snapshots = set()
    snapshots = history.execute(
        "SELECT id,metadata FROM source_snapshots WHERE source_id=? LIMIT 4097",
        (source_id,),
    ).fetchall()
    if len(snapshots) > 4096:
        raise ValueError("Native source snapshots exceed their proof budget.")
    for row in snapshots:
        value = json.loads(row["metadata"])
        if (
            value.get("source_id") == source_id
            and value.get("source_account_id") == conversation["account_key"][8:]
            and value.get("account_hash_verified") is True
            and value.get("account_hash") == source_id[4:]
        ):
            verified_snapshots.add(row["id"])
    anchors, clocks, count, total = defaultdict(list), 0, 0, 0
    for row in history.execute(
        "SELECT id,provenance,direction,time_sort,timestamp_value,timestamp_label,time_basis FROM messages WHERE conversation_id=? LIMIT ?",
        (conversation["id"], MAX_PRIMARY_ROWS + 1),
    ):
        count += 1
        total += len(row["provenance"].encode())
        if count > MAX_PRIMARY_ROWS or total > MAX_METADATA_BYTES:
            raise ValueError("The native primary scope exceeds its metadata budget.")
        proof = json.loads(row["provenance"])
        locator = proof.get("locator", {})
        if (
            not isinstance(locator, dict)
            or proof.get("source_snapshot") not in verified_snapshots
            or locator.get("database_relative_path") != relative
            or locator.get("database_sha256") != digest
            or locator.get("table") != table
        ):
            continue
        local = locator.get("local_id")
        if type(local) is not int or local <= 0 or locator.get("row_id") != local:
            raise ValueError("A retained native source row has a conflicting locator.")
        signature = _signature(
            proof.get("server_id"),
            proof.get("unix_timestamp"),
            proof.get("message_type"),
            row["direction"],
        )
        anchors[local].append((row["id"], signature))
        if clocks < 3 and row["direction"] in {"sent", "received"}:
            originals = native.execute(
                f'SELECT MesSvrID,CreateTime,Type,Des FROM "{table}" WHERE MesLocalID=? LIMIT 2',
                (local,),
            ).fetchall()
            if len(originals) != 1:
                raise ValueError(
                    "A retained native clock anchor is missing or ambiguous."
                )
            original = originals[0]
            direction = {0: "sent", 1: "received"}.get(original[3], "unknown")
            if signature != _signature(*original[:3], direction) or tuple(
                row[key]
                for key in (
                    "time_sort",
                    "timestamp_value",
                    "timestamp_label",
                    "time_basis",
                )
            ) != _time(original[1]):
                raise ValueError(
                    "Native metadata clocks disagree with retained source events."
                )
            clocks += 1
    if clocks < 2:
        raise ValueError(
            "Two retained native source clocks are required before staging."
        )
    return anchors


def _sender_prefix(native, table, local, marker):
    row = native.execute(
        f"SELECT CASE WHEN typeof(Message)='text' OR (typeof(Message)='blob' AND COALESCE({marker},0) IN (0,1)) THEN substr(Message,1,300) ELSE NULL END FROM \"{table}\" WHERE MesLocalID=? LIMIT 2",
        (local,),
    ).fetchall()
    if len(row) != 1:
        raise ValueError("A native metadata sender has an ambiguous source row.")
    sender, _content = _group_sender_prefix(row[0][0])
    return sender


def _write(
    candidate,
    history,
    rows,
    conversations,
    proofs,
    report,
    *,
    witnesses=(),
    proof_metadata=None,
):
    if (
        os.path.lexists(candidate)
        or not candidate.parent.is_dir()
        or candidate.parent.is_symlink()
    ):
        raise ValueError("Select a new candidate in a regular existing directory.")
    descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    created = os.fstat(descriptor)
    os.close(descriptor)
    try:
        with closing(sqlite3.connect(candidate)) as output:
            candidate.chmod(0o600)
            output.executescript(_SCHEMA)
            output.execute(
                "CREATE TABLE revalidation_source_proofs (source_snapshot_id INTEGER PRIMARY KEY,source_id TEXT NOT NULL,snapshot_id TEXT NOT NULL,metadata_sha256 TEXT NOT NULL)"
            )
            output.executemany(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
            )
            summaries = []
            for cid, conversation in conversations.items():
                selected = [row for row in rows if row[1] == cid]
                if not selected:
                    raise ValueError(
                        "A metadata scope has no additional source events."
                    )
                first = min(selected, key=lambda row: (row[7], row[2], row[0]))
                last = max(selected, key=lambda row: (row[7], row[2], row[0]))
                summaries.append(
                    tuple(
                        conversation[key]
                        for key in (
                            "id",
                            "partition_id",
                            "account_key",
                            "peer_id",
                            "title",
                            "kind",
                        )
                    )
                    + (
                        len(selected),
                        first[7],
                        last[7],
                        first[8],
                        last[8],
                        last[6][:300],
                    )
                )
            output.executemany(
                "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", summaries
            )
            output.executemany(
                "INSERT INTO revalidation_source_proofs VALUES(?,?,?,?)",
                proofs.values(),
            )
            if witnesses:
                output.executescript(WITNESS_SCHEMA)
                output.executemany(
                    "INSERT INTO revalidation_native_witnesses VALUES(?,?,?,?,?,?,?,?)",
                    witnesses,
                )
            info = {
                "schema_version": "1",
                "proof_version": PROOF_VERSION,
                "published": "1",
                "history_file_identity": _json(
                    _resident(
                        Path(history.execute("PRAGMA database_list").fetchone()[2])
                    )
                ),
                "history_partitions_sha256": wechat_history_partition_fingerprint(
                    history
                ),
                "coverage_scope": "explicit_verified_conversations_only",
                "covered_conversations": _json(tuple(conversations)),
                "messages": str(len(rows)),
                "report": _json(report),
                "source_receipts": _json(report.get("source_receipts", [])),
            }
            if proof_metadata:
                info.update(proof_metadata)
            output.executemany("INSERT INTO archive_info VALUES(?,?)", info.items())
            output.execute(
                "INSERT INTO archive_info VALUES('content_sha256',?)",
                (_content_digest(output),),
            )
            output.commit()
            if output.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("The metadata candidate failed its integrity check.")
        with candidate.open("r+b" if os.name == "nt" else "rb") as stream:
            os.fsync(stream.fileno())
    except BaseException:
        if (
            candidate.exists()
            and not candidate.is_symlink()
            and candidate.stat().st_ino == created.st_ino
        ):
            candidate.unlink()
        raise


def stage_preserved_native_resource_scope(
    local_store_root,
    candidate_path,
    *,
    conversation_id,
    source_snapshot_id,
    database_path,
    preservation_report_path,
    sources_path,
):
    """Build one unpublished scope from exactly one resident preserved database."""
    history_path = wechat_archive_path(local_store_root).absolute()
    database = Path(database_path).absolute()
    candidate = Path(candidate_path).absolute()
    inputs = {str(history_path): {"identity": _resident(history_path)}}
    inputs[str(database)] = {"identity": _resident(database)}
    if inputs[str(database)]["identity"]["size"] > 512 * 1024**2:
        raise ValueError("The selected native database exceeds the source byte budget.")
    report = _json_input(preservation_report_path, inputs)
    sources = _json_input(sources_path, inputs)
    if (
        not isinstance(report, dict)
        or report.get("sources_sha256")
        != inputs[str(Path(sources_path).absolute())]["sha256"]
    ):
        raise ValueError("The source list differs from its preservation receipt.")
    if (
        not isinstance(conversation_id, str)
        or re.fullmatch(r"wechat:[0-9a-f]{64}", conversation_id) is None
    ):
        raise ValueError("Select an exact native conversation ID.")
    rows, excluded, kinds = [], [], Counter()
    with _readonly(history_path) as history:
        conversation, owner, snapshot, relative, digest, file_id = _binding(
            history,
            conversation_id,
            source_snapshot_id,
            report,
            sources,
            database,
            inputs,
        )
        expected = (
            Path(preservation_report_path).absolute().parent / "inputs" / relative
        )
        if expected != database or any(
            parent.is_symlink() for parent in (database, *database.parents)
        ):
            raise ValueError("The selected database escapes its preserved source path.")
        actual_digest = _sha256(database)
        if actual_digest != digest:
            raise ValueError(
                "The preserved native database failed its SHA-256 receipt."
            )
        inputs[str(database)]["sha256"] = actual_digest
        table = (
            "Chat_"
            + hashlib.md5(
                conversation["peer_id"].encode(), usedforsecurity=False
            ).hexdigest()
        )
        with _readonly(database) as native:
            columns = {
                row[1] for row in native.execute(f'PRAGMA table_info("{table}")')
            }
            marker = '"WCDB_CT_Message"' if "WCDB_CT_Message" in columns else "0"
            anchors = _anchors(
                history,
                native,
                conversation,
                conversation["partition_id"],
                relative,
                digest,
                table,
            )
            seen = set()
            for number, row in enumerate(_table_rows(native, table), 1):
                if number > MAX_EVENTS:
                    raise ValueError(
                        "Native resource events exceed their metadata budget."
                    )
                local, server, epoch, typ, des, payload = row
                if (
                    type(local) is not int
                    or local <= 0
                    or local in seen
                    or type(typ) is not int
                    or type(des) is not int
                ):
                    raise ValueError(
                        "A native metadata row has an ambiguous source locator."
                    )
                seen.add(local)
                direction = {0: "sent", 1: "received"}.get(des, "unknown")
                signature = _signature(server, epoch, typ, direction)
                existing = anchors.get(local, ())
                if existing:
                    if any(item[1] != signature for item in existing):
                        raise ValueError(
                            "A native metadata event conflicts with retained source text."
                        )
                    excluded.append(
                        {
                            "local_id": local,
                            "primary_ids": [item[0] for item in existing],
                        }
                    )
                    continue
                reference = {
                    "kind": _KINDS[typ],
                    "filename": "",
                    "file_extension": "",
                    "checksum_md5": "",
                    "byte_size": None,
                    "account_id": owner,
                    "peer_id": conversation["peer_id"],
                    "unix_timestamp": epoch,
                    "availability": "metadata_only",
                }
                app = (
                    _app_metadata(
                        payload,
                        conversation["kind"] == "group",
                        direction == "received",
                    )
                    if typ == 49 and payload is not None
                    else {}
                )
                reference.update(
                    {
                        key: value
                        for key, value in app.items()
                        if key
                        in {
                            "kind",
                            "filename",
                            "file_extension",
                            "checksum_md5",
                            "byte_size",
                            *_APP_LABEL_FIELDS,
                        }
                    }
                )
                if typ == 49:
                    subtype = app.get("subtype", "")
                    if (
                        subtype.isascii()
                        and subtype.isdecimal()
                        and 1 <= len(subtype) <= 6
                    ):
                        reference["app_subtype"] = subtype
                reference["label"] = _LABELS[reference["kind"]]
                sender = (
                    owner
                    if direction == "sent"
                    else conversation["peer_id"]
                    if direction == "received" and conversation["kind"] == "direct"
                    else app.get("sender_id")
                    or _sender_prefix(native, table, local, marker)
                )
                identifier = _identifier(
                    "wechat-resource-reference-v1",
                    owner,
                    snapshot["snapshot_id"],
                    "AppDomain-com.tencent.xin/" + relative,
                    table,
                    str(local),
                )
                if history.execute(
                    "SELECT 1 FROM messages WHERE id=?", (identifier,)
                ).fetchone():
                    raise ValueError(
                        "A metadata identifier collides with another retained source event."
                    )
                proof = {
                    "record_id": identifier,
                    "source_snapshot": source_snapshot_id,
                    "backup_id": snapshot["snapshot_id"],
                    "unix_timestamp": epoch,
                    "source_time": None,
                    "server_id": signature[0],
                    "message_type": typ,
                    "text_extraction_kind": "resource_placeholder",
                    "resource_reference": reference,
                    "source_relative_path": "AppDomain-com.tencent.xin/" + relative,
                    "source_sha256": digest,
                    "source_checksum_basis": "preservation_manifest",
                    "locator": {
                        "database": database.name,
                        "database_relative_path": relative,
                        "database_sha256": digest,
                        "table": table,
                        "local_id": local,
                        "row_id": local,
                        "file_id": file_id,
                        "native_direction": des,
                        "text_origin": "native_resource_metadata",
                    },
                }
                if "app_subtype" in reference:
                    proof["native_appmsg_subtype"] = reference["app_subtype"]
                flags = ["resource_placeholder", "media_payload_not_read"]
                if not sender:
                    flags.append("sender_id_unavailable")
                rows.append(
                    (
                        identifier,
                        conversation_id,
                        local,
                        sender or None,
                        sender or "Unknown sender",
                        direction,
                        reference["label"],
                        *_time(epoch),
                        _json(proof),
                        _json(flags),
                        "\n".join(
                            (
                                reference["label"],
                                reference["filename"],
                                sender or "",
                                conversation["title"],
                                conversation["peer_id"],
                            )
                        ).casefold(),
                    )
                )
                kinds[reference["kind"]] += 1
        proof = (
            source_snapshot_id,
            snapshot["source_id"],
            snapshot["snapshot_id"],
            hashlib.sha256(snapshot["metadata"].encode()).hexdigest(),
        )
        _unchanged(inputs)
        source_receipt = {
            "source_snapshot_id": source_snapshot_id,
            "source_id": snapshot["source_id"],
            "snapshot_id": snapshot["snapshot_id"],
            "metadata_sha256": proof[3],
            "database_relative_path": relative,
            "database_sha256": digest,
            "database_bytes": inputs[str(database)]["identity"]["size"],
            "file_id": file_id,
            "manifest_path": str(Path(preservation_report_path).absolute()),
            "manifest_sha256": inputs[str(Path(preservation_report_path).absolute())][
                "sha256"
            ],
            "original_source_inventory_file": json.loads(snapshot["metadata"]).get(
                "source_inventory_file"
            ),
        }
        result = {
            "version": VERSION,
            "candidate": str(candidate),
            "published": False,
            "messages": len(rows),
            "excluded_existing_events": excluded,
            "kinds": dict(kinds),
            "covered_conversations": [conversation_id],
            "inputs": inputs,
            "history_identity": inputs[str(history_path)]["identity"],
            "source_identity": inputs[str(database)]["identity"],
            "source_sha256": digest,
            "source_receipts": [source_receipt],
            "media_payload_files_read": 0,
            "cloud_inputs_hydrated": 0,
        }
        _write(
            candidate,
            history,
            rows,
            {conversation_id: conversation},
            {source_snapshot_id: proof},
            result,
        )
        _unchanged(inputs)
        result.update(
            candidate_identity=_resident(candidate), candidate_sha256=_sha256(candidate)
        )
        return result


def merge_native_resource_scopes(local_store_root, candidate_paths, output_path):
    """Union independently sealed current scopes without replacing source rows."""
    history_path = wechat_archive_path(local_store_root).absolute()
    output = Path(output_path).absolute()
    paths = [Path(path).absolute() for path in candidate_paths]
    if not paths or len(paths) > 128 or len(set(paths)) != len(paths):
        raise ValueError("Select bounded distinct native metadata candidates.")
    inputs = {str(history_path): {"identity": _resident(history_path)}}
    rows, conversations, proofs, receipts, witnesses, proof_metadata = (
        {},
        {},
        {},
        {},
        {},
        {},
    )
    with _readonly(history_path) as history:
        for path in paths:
            inputs[str(path)] = {"identity": _resident(path), "sha256": _sha256(path)}
            _verified_coverage(history_path, path)
            with _readonly(path) as source:
                info = dict(source.execute("SELECT key,value FROM archive_info"))
                for key in _PRESERVED_PROOF_KEYS:
                    if key in info:
                        if key in proof_metadata and proof_metadata[key] != info[key]:
                            raise ValueError(
                                "Sealed resource metadata proofs conflict."
                            )
                        proof_metadata[key] = info[key]
                if source.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='revalidation_native_witnesses'"
                ).fetchone():
                    for row in source.execute(
                        "SELECT * FROM revalidation_native_witnesses LIMIT 4097"
                    ):
                        value = tuple(row)
                        if (
                            row["message_id"] in witnesses
                            and witnesses[row["message_id"]] != value
                        ):
                            raise ValueError(
                                "Sealed native resource witnesses conflict."
                            )
                        witnesses[row["message_id"]] = value
                        if len(witnesses) > 4096:
                            raise ValueError(
                                "Merged native resource witnesses exceed their budget."
                            )
                prior_report = json.loads(info.get("report", "{}"))
                if isinstance(prior_report, dict):
                    for filename, pin in prior_report.get("inputs", {}).items():
                        if filename in inputs and inputs[filename] != pin:
                            raise ValueError(
                                "Preserved resource inputs have conflicting pins."
                            )
                        if not isinstance(pin, dict) or "identity" not in pin:
                            raise ValueError(
                                "A preserved resource input pin is incomplete."
                            )
                        inputs[filename] = pin
                source_receipts = json.loads(info.get("source_receipts", "[]"))
                if not isinstance(source_receipts, list) or len(source_receipts) > 4096:
                    raise ValueError(
                        "Native resource source receipts exceed their budget."
                    )
                for receipt in source_receipts:
                    key = (
                        receipt["source_snapshot_id"],
                        receipt["database_relative_path"],
                    )
                    if key in receipts and receipts[key] != receipt:
                        raise ValueError("Preserved resource source receipts conflict.")
                    receipts[key] = receipt
                for row in source.execute("SELECT * FROM conversations"):
                    current = history.execute(
                        "SELECT * FROM conversations WHERE id=?", (row["id"],)
                    ).fetchone()
                    if current is None or any(
                        row[key] != current[key]
                        for key in ("partition_id", "account_key", "peer_id", "kind")
                    ):
                        raise ValueError(
                            "A sealed resource scope changed its account or conversation."
                        )
                    conversations[row["id"]] = dict(current)
                for row in source.execute("SELECT * FROM messages"):
                    value = tuple(row)
                    if row["id"] in rows and rows[row["id"]] != value:
                        raise ValueError(
                            "Two sealed resource candidates disagree about an event."
                        )
                    rows[row["id"]] = value
                    if len(rows) > MAX_EVENTS:
                        raise ValueError(
                            "Merged resource metadata exceeds its event budget."
                        )
                for row in source.execute("SELECT * FROM revalidation_source_proofs"):
                    value = tuple(row)
                    if (
                        row["source_snapshot_id"] in proofs
                        and proofs[row["source_snapshot_id"]] != value
                    ):
                        raise ValueError("Sealed resource source proofs conflict.")
                    proofs[row["source_snapshot_id"]] = value
        _unchanged(inputs)
        report = {
            "version": VERSION,
            "candidate": str(output),
            "published": False,
            "messages": len(rows),
            "covered_conversations": list(conversations),
            "preserved_candidates": [str(path) for path in paths],
            "inputs": inputs,
            "history_identity": inputs[str(history_path)]["identity"],
            "source_receipts": list(receipts.values()),
            "preserved_native_witnesses": len(witnesses),
            "preserved_proof_metadata": proof_metadata,
        }
        _write(
            output,
            history,
            list(rows.values()),
            conversations,
            proofs,
            report,
            witnesses=witnesses.values(),
            proof_metadata=proof_metadata,
        )
        _verified_coverage(history_path, output)
        _unchanged(inputs)
        report.update(
            candidate_identity=_resident(output), candidate_sha256=_sha256(output)
        )
        return report
