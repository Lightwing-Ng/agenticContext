"""Revalidate bounded native metadata scopes without touching original evidence.

Code version: v1.1.2-codex.0
"""

from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import ExitStack, closing
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat

from .owner_only_files import ensure_owner_only
from .wechat_archive import (
    _file_identity,
    _identifier,
    _time_fields,
    wechat_archive_path,
    wechat_history_partition_fingerprint,
)
from .wechat_resource_index import _KINDS, _LABELS, _SCHEMA, _json, _readonly


VERSION = "v1.1.2-codex.0"
_COMPATIBLE_PROOFS = {
    "v1.0.1-codex.0", "v1.0.2-codex.0", "v1.1.0-codex.0", "v1.1.1-codex.0", VERSION,
}
MAX_SCOPES = 128
MAX_ROWS = 100_000
MAX_BYTES = 128 * 1024**2
_HEX = re.compile(r"[0-9a-f]{64}")


def _resident(path):
    state = path.lstat()
    if (
        not stat.S_ISREG(state.st_mode)
        or path.is_symlink()
        or getattr(state, "st_flags", 0) & 0x40000000
        or any(
            Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")
        )
    ):
        raise ValueError("Native metadata inputs must be resident, regular, and idle.")
    return _file_identity(path)


def _owner(partition):
    owner = partition["account_id"]
    if (
        not isinstance(owner, str)
        or not owner
        or partition["account_key"] != "account:" + owner
        or partition["id"]
        != "ios-" + hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest()
    ):
        raise ValueError("A native resource needs its verified account partition.")
    return owner


def _relative(value, owner):
    prefix = "AppDomain-com.tencent.xin/"
    if isinstance(value, str) and value.startswith(prefix):
        value = value[len(prefix) :]
    hashed = hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest()
    if (
        not isinstance(value, str)
        or re.fullmatch(
            r"Documents/" + hashed + r"/DB/(?:message_[0-9]+|MM)\.sqlite", value
        )
        is None
    ):
        raise ValueError(
            "A native database path must retain its original account directory."
        )
    return value


def _receipts(history, partition):
    owner = _owner(partition)
    receipts, snapshots = defaultdict(set), {}
    total = 0
    rows = history.execute(
        "SELECT * FROM source_snapshots WHERE source_id=? LIMIT 4097",
        (partition["id"],),
    ).fetchall()
    if len(rows) > 4096:
        raise ValueError("Native source receipts exceed their metadata budget.")
    for row in rows:
        total += len(row["metadata"].encode())
        if total > MAX_BYTES:
            raise ValueError("Native source receipts exceed their metadata budget.")
        metadata = json.loads(row["metadata"])
        if (
            metadata.get("source_id") != partition["id"]
            or metadata.get("source_account_id") != owner
            or metadata.get("account_hash_verified") is not True
            or metadata.get("account_hash") != partition["id"][4:]
        ):
            continue
        backup = metadata.get("backup_id")
        if not isinstance(backup, str) or backup != row["snapshot_id"]:
            continue
        sources = {}
        for database in metadata.get("input_databases", ()):
            if not isinstance(database, dict):
                continue
            path = database.get("path") or database.get("relative_path")
            try:
                path = _relative(path, owner)
            except ValueError:
                continue
            digest = database.get("sha256")
            if not isinstance(digest, str) or _HEX.fullmatch(digest) is None:
                continue
            receipts[(backup, path)].add(digest)
            sources[path] = digest
        if sources:
            snapshots[row["id"]] = (
                backup,
                sources,
                {
                    "source_snapshot_id": row["id"],
                    "source_id": row["source_id"],
                    "snapshot_id": row["snapshot_id"],
                    "metadata_sha256": hashlib.sha256(
                        row["metadata"].encode()
                    ).hexdigest(),
                },
            )
    if any(len(values) != 1 for values in receipts.values()):
        raise ValueError("One native database has conflicting preserved receipts.")
    return (
        owner,
        {key: next(iter(values)) for key, values in receipts.items()},
        snapshots,
    )


def _anchor(provenance, direction, locator):
    local = locator.get("local_id")
    if type(local) is not int or local < 1 or locator.get("row_id") != local:
        raise ValueError("A native resource needs its exact positive row locator.")
    epoch = provenance.get("unix_timestamp")
    if type(epoch) is not int:
        raise ValueError("A native event must retain its recorded integer timestamp.")
    server = provenance.get("server_id")
    # Native exports use zero when no server identifier was recorded. The exact
    # database receipt and local row remain required before comparing events.
    if server == "0":
        server = None
    if server is not None and (
        not isinstance(server, str)
        or re.fullmatch(r"[1-9][0-9]{0,18}", server) is None
        or int(server) > 9223372036854775807
    ):
        raise ValueError("A native event must retain its exact server ID string.")
    message_type = provenance.get("message_type")
    if type(message_type) is not int:
        raise ValueError("A native event must retain its recorded message type.")
    return local, (server, epoch, message_type, direction)


def _current_anchors(history, conversation, owner, snapshots):
    result = defaultdict(list)
    total = 0
    rows = history.execute(
        "SELECT id,provenance,direction FROM messages WHERE conversation_id=? LIMIT ?",
        (conversation["id"], MAX_ROWS + 1),
    )
    for number, row in enumerate(rows, 1):
        total += len(row["provenance"].encode())
        if number > MAX_ROWS or total > MAX_BYTES:
            raise ValueError(
                "The selected native conversation exceeds its proof budget."
            )
        proof = json.loads(row["provenance"])
        snapshot = snapshots.get(proof.get("source_snapshot"))
        locator = proof.get("locator", {})
        if snapshot is None or not isinstance(locator, dict):
            continue
        database = locator.get("database")
        if not isinstance(database, str):
            continue
        path = _relative(
            "Documents/" + conversation["partition_id"][4:] + "/DB/" + database, owner
        )
        digest = snapshot[1].get(path)
        if digest is None:
            continue
        local, signature = _anchor(proof, row["direction"], locator)
        key = (snapshot[0], path, digest, locator.get("table"), local)
        result[key].append((row["id"], signature))
    return result


def _validate_row(row, conversation, owner, receipts, snapshots):
    proof = json.loads(row["provenance"])
    locator, reference = proof.get("locator"), proof.get("resource_reference")
    if not isinstance(locator, dict) or not isinstance(reference, dict):
        raise ValueError("Native resource metadata lacks its original reference.")
    path = _relative(proof.get("source_relative_path"), owner)
    backup, digest = proof.get("backup_id"), proof.get("source_sha256")
    basis = proof.get("source_checksum_basis")
    if (
        receipts.get((backup, path)) != digest
        or not digest
        or basis not in {"preservation_manifest", "primary_source_database_receipt"}
    ):
        raise ValueError(
            "A native resource differs from its preserved database receipt."
        )
    matches = [
        value[2]
        for value in snapshots.values()
        if value[0] == backup and value[1].get(path) == digest
    ]
    if not matches:
        raise ValueError("A native resource has no current source snapshot proof.")
    if basis == "primary_source_database_receipt":
        snapshot = snapshots.get(proof.get("source_snapshot"))
        if (
            snapshot is None
            or snapshot[0] != backup
            or snapshot[1].get(path) != digest
            or proof.get("source_snapshot_metadata_sha256")
            != snapshot[2]["metadata_sha256"]
        ):
            raise ValueError("A native resource lost its primary source receipt.")
    local, signature = _anchor(proof, row["direction"], locator)
    epoch = proof["unix_timestamp"]
    try:
        utc_time = datetime.fromtimestamp(epoch, UTC).isoformat() if epoch > 0 else ""
    except (ValueError, OverflowError, OSError):
        utc_time = ""
    expected_time = _time_fields({"sent_at_utc_iso": utc_time})
    if (
        tuple(
            row[key]
            for key in ("time_sort", "timestamp_value", "timestamp_label", "time_basis")
        )
        != expected_time
    ):
        raise ValueError("A native resource changed its recorded timestamp projection.")
    if conversation["peer_id"]:
        table = (
            "Chat_"
            + hashlib.md5(
                conversation["peer_id"].encode(), usedforsecurity=False
            ).hexdigest()
        )
    else:
        peer_hash = proof.get("unresolved_peer_hash")
        if (
            conversation["kind"] != "unclassified"
            or not isinstance(peer_hash, str)
            or re.fullmatch(r"[0-9a-f]{32}", peer_hash) is None
            or conversation["id"]
            != "wechat:" + _identifier(conversation["partition_id"], peer_hash)
        ):
            raise ValueError("An unresolved native resource lost its exact table hash.")
        table = "Chat_" + peer_hash
    scheme = proof.get("record_id_scheme", "wechat-resource-reference-v1")
    if scheme not in {"wechat-resource-reference-v1", "wechat-resource-reference-v2"}:
        raise ValueError("A native resource has an unsupported record identifier scheme.")
    identity = (
        scheme,
        owner,
        backup,
        "AppDomain-com.tencent.xin/" + path,
    )
    if scheme == "wechat-resource-reference-v2":
        identity += (digest,)
    expected_id = _identifier(
        *identity,
        table,
        str(local),
    )
    kind = reference.get("kind")
    message_type = proof["message_type"]
    if (
        row["conversation_id"] != conversation["id"]
        or row["ordinal"] != local
        or row["id"] != expected_id
        or proof.get("record_id") != expected_id
        or locator.get("table") != table
        or locator.get("database") != PurePosixPath(path).name
        or locator.get("text_origin") != "native_resource_metadata"
        or proof.get("text_extraction_kind") != "resource_placeholder"
        or message_type not in _KINDS
        or kind not in _LABELS
        or (message_type != 49 and kind != _KINDS[message_type])
        or reference.get("account_id") != owner
        or reference.get("peer_id") != conversation["peer_id"]
        or reference.get("unix_timestamp") != proof["unix_timestamp"]
        or row["text"] != reference.get("label")
        or row["text"] != _LABELS[kind]
        or row["direction"] not in {"sent", "received", "unknown"}
    ):
        raise ValueError("A native resource changed its source, frame, or scope.")
    expected_sender = owner if row["direction"] == "sent" else conversation["peer_id"]
    if (
        conversation["kind"] == "direct"
        and row["direction"] != "unknown"
        and row["sender_id"] != expected_sender
    ):
        raise ValueError("A native resource has another direct-chat author.")
    return (backup, path, digest, table, local), signature, matches, kind


def _content_digest(connection, *, proof_metadata=None):
    digest = hashlib.sha256()
    total_rows = total_bytes = 0
    tables = [
        ("messages", "id"),
        ("conversations", "id"),
        ("revalidation_source_proofs", "source_snapshot_id"),
    ]
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name='revalidation_native_witnesses'"
    ).fetchone():
        tables.append(("revalidation_native_witnesses", "message_id"))
    for table, order in tables:
        digest.update(table.encode() + b"\0")
        for row in connection.execute(f"SELECT * FROM {table} ORDER BY {order}"):
            encoded = _json(tuple(row)).encode()
            total_rows += 1
            total_bytes += len(encoded)
            if total_rows > MAX_ROWS + MAX_SCOPES + 8192 or total_bytes > 2 * MAX_BYTES:
                raise ValueError(
                    "The native fallback exceeds its sealed metadata budget."
                )
            digest.update(len(encoded).to_bytes(8, "big") + encoded)
    if proof_metadata is None:
        proof_metadata = dict(connection.execute("SELECT key,value FROM archive_info"))
    extra = {
        key: proof_metadata[key]
        for key in (
            "supplemental_metadata",
            "canonical_file_identity",
            "supplement_source_receipts",
        )
        if key in proof_metadata
    }
    if extra:
        digest.update(b"supplemental-native-proof-v1\0" + _json(extra).encode())
    return digest.hexdigest()


def _clock_variant(row, signature, others, report):
    """Keep independently proven native clocks without inventing one timestamp."""
    if any(value[1][2:] != signature[2:] for value in others):
        raise ValueError(
            "A native server ID has conflicting type or direction evidence."
        )
    conflict = {
        "resource_id": row["id"],
        "other_ids": [value[0] for value in others],
        "unix_timestamp": signature[1],
        "other_unix_timestamps": sorted({value[1][1] for value in others}),
        "basis": "same_verified_account_peer_server_with_distinct_native_clocks",
    }
    report.setdefault("source_clock_conflicts", []).append(conflict)
    result = dict(row)
    proof, flags = json.loads(row["provenance"]), json.loads(row["quality_flags"])
    proof["native_resource_clock_conflict"] = conflict
    if "native_resource_source_clock_conflict" not in flags:
        flags.append("native_resource_source_clock_conflict")
    result["provenance"], result["quality_flags"] = _json(proof), _json(flags)
    return result


def stage_native_resource_revalidation(
    local_store_root,
    candidate_path,
    *,
    conversation_ids,
    supplemental_path=None,
    _preserved_path=None,
):
    """Build only explicitly verified scopes; preserve all original sidecar rows."""
    history_path = wechat_archive_path(local_store_root).absolute()
    original = history_path.with_name("resource_references.sqlite3")
    identities = {history_path: _resident(history_path), original: _resident(original)}
    supplemental = Path(supplemental_path).absolute() if supplemental_path else None
    if supplemental is not None:
        if supplemental in {history_path, original}:
            raise ValueError("Select a separate native metadata supplement.")
        identities[supplemental] = _resident(supplemental)
    preserved_path = Path(_preserved_path).absolute() if _preserved_path else None
    if preserved_path is not None:
        identities[preserved_path] = _resident(preserved_path)
    has_extra_sources = supplemental is not None or preserved_path is not None
    selected = tuple(dict.fromkeys(conversation_ids))
    if (
        not selected
        or len(selected) > MAX_SCOPES
        or any(
            not isinstance(cid, str)
            or re.fullmatch(r"wechat:[0-9a-f]{64}", cid) is None
            for cid in selected
        )
    ):
        raise ValueError("Select bounded exact native conversation IDs.")
    candidate = Path(candidate_path).absolute()
    if candidate.exists() or candidate.is_symlink():
        raise FileExistsError("Keep existing native metadata candidates unchanged.")
    if not candidate.parent.is_dir():
        raise ValueError("Select an existing candidate directory.")
    report = {
        "version": VERSION,
        "candidate": str(candidate),
        "source": str(original),
        "source_identity": identities[original],
        "history_identity": identities[history_path],
        "coverage": "explicit_verified_conversations_only",
        "conversations": [],
        "excluded_existing_events": [],
        "messages": 0,
        "media_payload_files_read": 0,
        "cloud_inputs_hydrated": 0,
        "published": False,
    }
    if supplemental is not None:
        report.update(
            supplemental_path=str(supplemental),
            supplemental_identity=identities[supplemental],
        )
    proofs, rows_to_write, summaries, kinds, accounts = {}, [], [], Counter(), Counter()
    receipt_values, witnesses = {}, {}
    total_metadata_bytes = 0
    total_metadata_rows = 0
    with ExitStack() as stack:
        history = stack.enter_context(_readonly(history_path))
        source = stack.enter_context(_readonly(original))
        sources = [source]
        from .wechat_native_resource_receipts import (
            extend_scoped_receipts,
            load_supplement_receipts,
            WITNESS_SCHEMA,
        )

        if preserved_path is not None:
            prior_source = stack.enter_context(_readonly(preserved_path))
            sources.append(prior_source)
            for receipt in load_supplement_receipts(history, prior_source, sealed=True):
                receipt_values[
                    (receipt["source_snapshot_id"], receipt["database_relative_path"])
                ] = receipt
        if supplemental is not None:
            supplement_source = stack.enter_context(_readonly(supplemental))
            sources.append(supplement_source)
            for receipt in load_supplement_receipts(history, supplement_source):
                key = receipt["source_snapshot_id"], receipt["database_relative_path"]
                if key in receipt_values and any(
                    receipt_values[key][field] != receipt[field]
                    for field in (
                        "database_sha256",
                        "metadata_sha256",
                        "manifest_sha256",
                    )
                ):
                    raise ValueError(
                        "A supplemental receipt conflicts with prior sealed metadata."
                    )
                receipt_values[key] = receipt
        fingerprint = wechat_history_partition_fingerprint(history)
        for cid in selected:
            conversation = history.execute(
                "SELECT * FROM conversations WHERE id=?", (cid,)
            ).fetchone()
            priors = [
                prior
                for metadata in sources
                if (
                    prior := metadata.execute(
                        "SELECT * FROM conversations WHERE id=?", (cid,)
                    ).fetchone()
                )
                is not None
            ]
            if conversation is None or not priors or not conversation["peer_id"]:
                raise ValueError("A selected native resource scope is not published.")
            if any(
                conversation[key] != prior[key]
                for prior in priors
                for key in ("partition_id", "account_key", "peer_id", "kind")
            ):
                raise ValueError(
                    "A selected native resource scope changed its account or peer."
                )
            partition = history.execute(
                "SELECT * FROM partitions WHERE id=?", (conversation["partition_id"],)
            ).fetchone()
            if partition is None:
                raise ValueError("A selected resource account is unavailable.")
            owner, receipts, snapshots = _receipts(history, partition)
            if receipt_values:
                (owner, receipts, snapshots), native_witnesses = extend_scoped_receipts(
                    history,
                    conversation,
                    (owner, receipts, snapshots),
                    receipt_values.values(),
                )
                for witness in native_witnesses:
                    witnesses[witness[0]] = witness
            current = _current_anchors(history, conversation, owner, snapshots)
            current_servers = defaultdict(list)
            if has_extra_sources:
                for values in current.values():
                    for identifier, signature in values:
                        if signature[0] is not None:
                            current_servers[signature[0]].append(
                                (identifier, signature)
                            )
            selected_events = {}
            selected_servers = defaultdict(list)
            batch = []
            byte_count = 0
            for number, row in enumerate(
                (
                    row
                    for metadata in sources
                    for row in metadata.execute(
                        "SELECT * FROM messages WHERE conversation_id=? ORDER BY time_sort,ordinal,id LIMIT ?",
                        (cid, MAX_ROWS + 1),
                    )
                ),
                1,
            ):
                byte_count += len(row["provenance"].encode())
                total_metadata_bytes += len(row["provenance"].encode())
                total_metadata_rows += 1
                if number > MAX_ROWS or byte_count > MAX_BYTES:
                    raise ValueError(
                        "The selected resource scope exceeds its metadata budget."
                    )
                if total_metadata_rows > MAX_ROWS or total_metadata_bytes > MAX_BYTES:
                    raise ValueError(
                        "The selected resource scopes exceed their total metadata budget."
                    )
                key, signature, matches, kind = _validate_row(
                    row, conversation, owner, receipts, snapshots
                )
                for proof in matches:
                    proofs[proof["source_snapshot_id"]] = proof
                existing = current.get(key, ())
                basis = (
                    "same_snapshot_database_sha256_table_row_server_time_type_direction"
                )
                if has_extra_sources:
                    server = signature[0]
                    if not existing and server is not None:
                        alternatives = current_servers.get(server, ())
                        existing = [
                            value for value in alternatives if value[1] == signature
                        ]
                        if alternatives and not existing:
                            row = _clock_variant(row, signature, alternatives, report)
                        basis = "same_verified_account_peer_server_time_type_direction"
                    prior_event = selected_events.get(key)
                    if prior_event is not None:
                        if prior_event[1] != signature:
                            raise ValueError(
                                "A shared native locator has conflicting event evidence."
                            )
                    elif server is not None:
                        alternatives = selected_servers.get(server, ())
                        prior_event = next(
                            (value for value in alternatives if value[1] == signature),
                            None,
                        )
                        if alternatives and prior_event is None:
                            row = _clock_variant(row, signature, alternatives, report)
                    if prior_event is not None:
                        report["excluded_existing_events"].append(
                            {
                                "resource_id": row["id"],
                                "primary_ids": [prior_event[0]],
                                "basis": "same_verified_account_peer_server_time_type_direction"
                                if server is not None
                                else basis,
                            }
                        )
                        continue
                if existing:
                    if any(value[1] != signature for value in existing):
                        raise ValueError(
                            "A shared native locator has conflicting event evidence."
                        )
                    report["excluded_existing_events"].append(
                        {
                            "resource_id": row["id"],
                            "primary_ids": [value[0] for value in existing],
                            "basis": basis,
                        }
                    )
                    continue
                if history.execute(
                    "SELECT 1 FROM messages WHERE id=?", (row["id"],)
                ).fetchone():
                    raise ValueError(
                        "A native resource ID already has unrelated primary evidence."
                    )
                batch.append(
                    tuple(row.values()) if isinstance(row, dict) else tuple(row)
                )
                selected_events[key] = (row["id"], signature)
                if signature[0] is not None:
                    selected_servers[signature[0]].append((row["id"], signature))
                kinds[kind] += 1
                accounts[owner] += 1
            if not batch:
                raise ValueError(
                    "The verified native resource scope has no additional events."
                )
            first = min(batch, key=lambda row: (row[7] == 0, row[7], row[2], row[0]))
            last = max(batch, key=lambda row: (row[7], row[2], row[0]))
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
                + (len(batch), first[7], last[7], first[8], last[8], last[6][:300])
            )
            rows_to_write.extend(batch)
            report["conversations"].append({"id": cid, "messages": len(batch)})
    if any(_resident(path) != identity for path, identity in identities.items()):
        raise ValueError("A source changed during native resource verification.")
    report.update(
        messages=len(rows_to_write),
        kinds=dict(kinds),
        accounts=[
            {"account_id": owner, "messages": count}
            for owner, count in accounts.items()
        ],
    )
    created = False
    try:
        with candidate.open("xb"):
            created = True
            ensure_owner_only(candidate)
        with closing(sqlite3.connect(candidate)) as output:
            output.executescript(_SCHEMA)
            output.execute(
                "CREATE TABLE revalidation_source_proofs (source_snapshot_id INTEGER PRIMARY KEY,source_id TEXT NOT NULL,snapshot_id TEXT NOT NULL,metadata_sha256 TEXT NOT NULL)"
            )
            output.executemany(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows_to_write,
            )
            output.executemany(
                "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", summaries
            )
            output.executemany(
                "INSERT INTO revalidation_source_proofs VALUES(?,?,?,?)",
                [tuple(proof.values()) for proof in proofs.values()],
            )
            if witnesses:
                output.executescript(WITNESS_SCHEMA)
                output.executemany(
                    "INSERT INTO revalidation_native_witnesses VALUES(?,?,?,?,?,?,?,?)",
                    witnesses.values(),
                )
            info = {
                "schema_version": "1",
                "proof_version": VERSION,
                "published": "1",
                "history_file_identity": _json(identities[history_path]),
                "history_partitions_sha256": fingerprint,
                "coverage_scope": "explicit_verified_conversations_only",
                "covered_conversations": _json(selected),
                "messages": str(len(rows_to_write)),
                "content_sha256": _content_digest(output),
                "report": _json(report),
            }
            if has_extra_sources:
                info.update(
                    supplemental_metadata="1",
                    canonical_file_identity=_json(identities[original]),
                )
            if receipt_values:
                info["supplement_source_receipts"] = _json(
                    list(receipt_values.values())
                )
            info["content_sha256"] = _content_digest(output, proof_metadata=info)
            output.executemany("INSERT INTO archive_info VALUES(?,?)", info.items())
            output.commit()
            if output.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError(
                    "The native metadata candidate failed its integrity check."
                )
        if any(_resident(path) != identity for path, identity in identities.items()):
            raise ValueError("A source changed before native candidate completion.")
        with candidate.open("rb") as stream:
            os.fsync(stream.fileno())
        report["candidate_bytes"] = candidate.stat().st_size
        return report
    except BaseException:
        if created:
            candidate.unlink(missing_ok=True)
        raise


def _verified_coverage(history, path, *, prior_pin=False):
    """Read a sealed scope catalog; stale pins are usable only for fresh reproof."""
    identity, file_identity = _resident(history), _resident(path)
    with _readonly(history) as primary, _readonly(path) as source:
        info = dict(source.execute("SELECT key,value FROM archive_info"))
        covered = json.loads(info.get("covered_conversations", "null"))
        pin = json.loads(info.get("history_file_identity", "null"))
        if (
            info.get("schema_version") != "1"
            or info.get("proof_version") not in _COMPATIBLE_PROOFS
            or info.get("published") != "1"
            or info.get("coverage_scope") != "explicit_verified_conversations_only"
            or not isinstance(pin, dict)
            or set(pin) != set(identity)
            or any(type(value) is not int for value in pin.values())
            or not prior_pin
            and (
                pin != identity
                or info.get("history_partitions_sha256")
                != wechat_history_partition_fingerprint(primary)
                or info.get("supplemental_metadata") == "1"
                and json.loads(info.get("canonical_file_identity", "null"))
                != _resident(history.with_name("resource_references.sqlite3"))
            )
            or not isinstance(covered, list)
            or not 1 <= len(covered) <= MAX_SCOPES
            or any(
                not isinstance(cid, str)
                or re.fullmatch(r"wechat:[0-9a-f]{64}", cid) is None
                for cid in covered
            )
            or len(set(covered)) != len(covered)
            or set(covered)
            != {row[0] for row in source.execute("SELECT id FROM conversations")}
            or info.get("content_sha256") != _content_digest(source)
        ):
            raise ValueError("The sealed native resource coverage is unavailable.")
        proofs = source.execute(
            "SELECT * FROM revalidation_source_proofs LIMIT 4097"
        ).fetchall()
        if not 1 <= len(proofs) <= 4096:
            raise ValueError("Native resource source proofs exceed their budget.")
        for proof in proofs:
            row = primary.execute(
                "SELECT snapshot_id,source_id,metadata FROM source_snapshots WHERE id=?",
                (proof["source_snapshot_id"],),
            ).fetchone()
            if (
                row is None
                or row["snapshot_id"] != proof["snapshot_id"]
                or row["source_id"] != proof["source_id"]
                or hashlib.sha256(row["metadata"].encode()).hexdigest()
                != proof["metadata_sha256"]
            ):
                raise ValueError("A preserved native resource source proof changed.")
        from .wechat_native_resource_receipts import verify_native_witnesses

        verify_native_witnesses(primary, source)
    if _resident(history) != identity or _resident(path) != file_identity:
        raise ValueError("A native coverage input changed during verification.")
    return tuple(covered)


def _optional_resident(path):
    return _resident(path) if os.path.lexists(path) else None


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024**2), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stage_extended_native_resource_revalidation(
    local_store_root, candidate_path, *, conversation_ids, supplemental_path=None
):
    """Reprove requested scopes together with every previously sealed scope.

    Prior rows are not copied into the candidate. Their catalog selects scopes
    that the original metadata and current native receipts must independently
    prove again. Corrupt coverage or a combined budget overflow refuses the
    extension without dropping any previously published scope.
    """
    history = wechat_archive_path(local_store_root).absolute()
    fallback = history.with_name("resource_revalidated.sqlite3")
    identity = _resident(history)
    fallback_identity = _optional_resident(fallback)
    preserved = (
        _verified_coverage(history, fallback, prior_pin=True)
        if fallback_identity is not None
        else ()
    )
    requested = tuple(dict.fromkeys(conversation_ids))
    if not requested:
        raise ValueError("Select at least one native resource scope to extend.")
    selected = tuple(dict.fromkeys((*preserved, *requested)))
    report = stage_native_resource_revalidation(
        local_store_root,
        candidate_path,
        conversation_ids=selected,
        supplemental_path=supplemental_path,
        _preserved_path=fallback if fallback_identity is not None else None,
    )
    candidate = Path(candidate_path).absolute()
    candidate_identity = _resident(candidate)
    if (
        _resident(history) != identity
        or _optional_resident(fallback) != fallback_identity
    ):
        if _optional_resident(candidate) == candidate_identity:
            candidate.unlink()
        raise ValueError("Native coverage changed while staging its extension.")
    candidate_digest = _sha256(candidate)
    if _resident(candidate) != candidate_identity:
        raise ValueError("The staged native candidate changed before review.")
    return {
        **report,
        "requested_conversations": list(requested),
        "preserved_conversations": list(preserved),
        "fallback_identity": fallback_identity,
        "candidate_identity": candidate_identity,
        "candidate_sha256": candidate_digest,
    }


def _sync_directory(path):
    if os.name == "nt":
        # The Windows CRT cannot open directory descriptors for fsync.
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_native_resource_revalidation(local_store_root, report, *, rollback_path):
    """Publish a reviewed extension atomically, retaining an exact rollback file."""
    from .job_lock import CACHE_TASK_LOCK_NAME, CacheMaintenanceLock, CacheTaskLock
    from .wechat_export_merge import _clonefile

    history = wechat_archive_path(local_store_root).absolute()
    original = history.with_name("resource_references.sqlite3")
    fallback = history.with_name("resource_revalidated.sqlite3")
    candidate = Path(report["candidate"]).absolute()
    rollback = Path(rollback_path).absolute()
    if (
        report.get("version") != VERSION
        or report.get("published") is not False
        or "fallback_identity" not in report
        or candidate.parent.resolve() != fallback.parent.resolve()
        or candidate in {history, original, fallback}
        or rollback.resolve().is_relative_to(Path(local_store_root).resolve())
        or not rollback.parent.is_dir()
        or rollback.parent.is_symlink()
        or os.path.lexists(rollback)
    ):
        raise ValueError(
            "Select an untouched local candidate and external rollback path."
        )
    maintenance = CacheMaintenanceLock(
        CacheTaskLock(Path(local_store_root) / CACHE_TASK_LOCK_NAME)
    )
    if not maintenance.acquire("wechat-native-resource-publication"):
        raise RuntimeError("Finish the active cache task before publishing metadata.")
    swapped = False
    try:

        def unchanged():
            if (
                _resident(history) != report["history_identity"]
                or _resident(original) != report["source_identity"]
                or _optional_resident(fallback) != report["fallback_identity"]
                or _resident(candidate) != report["candidate_identity"]
                or _sha256(candidate) != report["candidate_sha256"]
                or "supplemental_path" in report
                and _resident(Path(report["supplemental_path"]))
                != report["supplemental_identity"]
            ):
                raise ValueError("A reviewed native metadata input changed.")

        unchanged()
        covered = _verified_coverage(history, candidate)
        if set(covered) != {item["id"] for item in report["conversations"]} or not set(
            report["preserved_conversations"]
        ).issubset(covered):
            raise ValueError("The candidate would discard reviewed native coverage.")
        if report["fallback_identity"] is not None:
            _clonefile(fallback, rollback)
            ensure_owner_only(rollback)
            if _sha256(rollback) != _sha256(fallback):
                raise ValueError("The fallback rollback differs from its original bytes.")
            with rollback.open("r+b" if os.name == "nt" else "rb") as stream:
                os.fsync(stream.fileno())
            _sync_directory(rollback.parent)
        unchanged()
        os.replace(candidate, fallback)
        swapped = True
        _sync_directory(fallback.parent)
        if (
            _verified_coverage(history, fallback) != covered
            or _resident(fallback) != report["candidate_identity"]
            or _resident(history) != report["history_identity"]
            or _resident(original) != report["source_identity"]
        ):
            raise ValueError("Published native metadata failed its source readback.")
        return {
            **report,
            "published": True,
            "fallback": str(fallback),
            "rollback": str(rollback) if rollback.exists() else None,
        }
    except BaseException:
        if swapped:
            if _resident(fallback) != report["candidate_identity"]:
                raise RuntimeError("Keep changed metadata and its rollback for review.")
            if report["fallback_identity"] is None:
                os.replace(fallback, candidate)
            else:
                _clonefile(rollback, candidate)
                os.replace(candidate, fallback)
            _sync_directory(fallback.parent)
        raise
    finally:
        maintenance.release()


def select_resource_index(history_path, *, conversation_id=None):
    """Prefer a current full index, then an independently proven bounded fallback."""
    history = Path(history_path)
    canonical = history.with_name("resource_references.sqlite3")
    fallback = history.with_name("resource_revalidated.sqlite3")
    if conversation_id is None:
        return canonical
    try:
        identity = _resident(history)
        # A supplemental publication contains the entire selected canonical
        # scope plus independently verified newer native observations. Prefer
        # that union only while its exact canonical input remains unchanged.
        if os.path.lexists(fallback) and os.path.lexists(canonical):
            try:
                _resident(fallback)
                with _readonly(fallback) as source:
                    info = dict(source.execute("SELECT key,value FROM archive_info"))
                if (
                    info.get("supplemental_metadata") == "1"
                    and json.loads(info.get("canonical_file_identity", "null"))
                    == _resident(canonical)
                    and conversation_id in _verified_coverage(history, fallback)
                ):
                    return fallback
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
                pass
        with _readonly(history) as primary:
            fingerprint = wechat_history_partition_fingerprint(primary)
            if canonical.exists():
                try:
                    _resident(canonical)
                except ValueError:
                    if not getattr(canonical.lstat(), "st_flags", 0) & 0x40000000:
                        return canonical
                else:
                    with _readonly(canonical) as source:
                        info = dict(
                            source.execute("SELECT key,value FROM archive_info")
                        )
                    pin = json.loads(info.get("history_file_identity", "null"))
                    if (
                        not isinstance(pin, dict)
                        or set(pin) != set(identity)
                        or any(type(value) is not int for value in pin.values())
                    ):
                        return canonical
                    if (
                        pin == identity
                        and info.get("history_partitions_sha256") == fingerprint
                    ):
                        return canonical
            fallback_identity = _resident(fallback)
            covered = _verified_coverage(history, fallback)
            if conversation_id not in covered:
                return canonical
            if (
                _resident(history) == identity
                and _resident(fallback) == fallback_identity
            ):
                return fallback
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
        pass
    return canonical


def locate_revalidated_resource(history_path, message_id):
    """Resolve one sealed fallback ID before revalidating its explicit scope."""
    if not isinstance(message_id, str) or _HEX.fullmatch(message_id) is None:
        return None
    path = Path(history_path).with_name("resource_revalidated.sqlite3")
    try:
        identity = _resident(path)
        with _readonly(path) as source:
            row = source.execute(
                "SELECT conversation_id FROM messages WHERE id=?", (message_id,)
            ).fetchone()
        if (
            row is not None
            and select_resource_index(history_path, conversation_id=row[0]) == path
            and _resident(path) == identity
        ):
            return row[0]
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        pass
    return None
