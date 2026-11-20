"""Stage source-proven resource coverage with optional bounded sender decoding.

Code version: v1.2.0-codex.0
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3

from .wechat_archive import (
    _identifier,
    wechat_archive_path,
    wechat_history_partition_fingerprint,
)
from .wechat_export_merge import _clonefile
from .wechat_native_media_supplement import _project
from .wechat_native_resource_revalidation import _owner, _relative, _resident
from .wechat_native_resource_senders import (
    MAX_MESSAGE_BYTES,
    resolve_source_bound_resource_sender,
)
from .wechat_resource_index import _KINDS, _json, _readonly


VERSION = "v1.2.0-codex.0"
MAX_CELL_BYTES = 16 * 1024**2
_SHA = re.compile(r"[0-9a-f]{64}")
_CHAT = re.compile(r"Chat_([0-9a-f]{32})")
_PEER = re.compile(r"[A-Za-z0-9_.@-]{1,256}")
_FIELDS = ("MesLocalID", "MesSvrID", "CreateTime", "Type", "Des")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS native_resource_sources (
 source_snapshot_id INTEGER NOT NULL, database_relative_path TEXT NOT NULL,
 database_sha256 TEXT NOT NULL, account_id TEXT NOT NULL, source_id TEXT NOT NULL,
 backup_id TEXT NOT NULL, metadata_sha256 TEXT NOT NULL, file_id TEXT NOT NULL,
 receipt_basis TEXT NOT NULL, proof TEXT NOT NULL,
 PRIMARY KEY(source_snapshot_id,database_relative_path,database_sha256)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS native_resource_source_inputs (
 source_snapshot_id INTEGER NOT NULL, database_relative_path TEXT NOT NULL,
 database_sha256 TEXT NOT NULL, path TEXT NOT NULL, identity TEXT NOT NULL,
 PRIMARY KEY(source_snapshot_id,database_relative_path,database_sha256,path,identity)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS native_resource_occurrences (
 source_snapshot_id INTEGER NOT NULL, database_relative_path TEXT NOT NULL,
 database_sha256 TEXT NOT NULL, chat_table TEXT NOT NULL, local_id INTEGER NOT NULL,
 account_id TEXT NOT NULL, peer_hash TEXT NOT NULL, canonical_store TEXT NOT NULL,
 canonical_id TEXT NOT NULL, native_metadata TEXT NOT NULL,
 PRIMARY KEY(source_snapshot_id,database_relative_path,database_sha256,chat_table,local_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS native_resource_coverage (
 source_snapshot_id INTEGER NOT NULL, database_relative_path TEXT NOT NULL,
 database_sha256 TEXT NOT NULL, chat_table TEXT NOT NULL, account_id TEXT NOT NULL,
 peer_hash TEXT NOT NULL, peer_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
 status TEXT NOT NULL, native_rows INTEGER NOT NULL, resource_rows INTEGER NOT NULL,
 nontext_rows INTEGER NOT NULL, covered_resource_rows INTEGER NOT NULL,
 type_counts TEXT NOT NULL,
 PRIMARY KEY(source_snapshot_id,database_relative_path,database_sha256,chat_table)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS native_resource_direction_scopes (
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL, backup_id TEXT NOT NULL,
 database_relative_path TEXT NOT NULL, database_sha256 TEXT NOT NULL,
 chat_table TEXT NOT NULL, native_des INTEGER NOT NULL, proof TEXT NOT NULL,
 UNIQUE(account_id,backup_id,database_relative_path,database_sha256,chat_table,native_des)
) WITHOUT ROWID;
CREATE TEMP TABLE extension_receipts (
 sid INTEGER NOT NULL,path TEXT NOT NULL,digest TEXT NOT NULL,basis TEXT NOT NULL,
 proof TEXT NOT NULL,PRIMARY KEY(sid,path,digest)
) WITHOUT ROWID;
CREATE TEMP TABLE extension_declared_versions (
 sid INTEGER NOT NULL,path TEXT NOT NULL,digest TEXT NOT NULL,
 PRIMARY KEY(sid,path,digest)
) WITHOUT ROWID;
CREATE TEMP TABLE extension_events (
 owner TEXT NOT NULL,peer_hash TEXT NOT NULL,server TEXT NOT NULL,epoch INTEGER NOT NULL,
 typ INTEGER NOT NULL,direction TEXT NOT NULL,target_store TEXT NOT NULL,target_id TEXT NOT NULL,
 PRIMARY KEY(owner,peer_hash,server,epoch,typ,direction)
) WITHOUT ROWID;
CREATE TEMP TABLE extension_locators (
 owner TEXT NOT NULL,backup TEXT NOT NULL,path TEXT NOT NULL,digest TEXT NOT NULL,
 chat_table TEXT NOT NULL,local_id INTEGER NOT NULL,signature TEXT NOT NULL,
 server_known INTEGER NOT NULL,
 target_store TEXT NOT NULL,target_id TEXT NOT NULL,
 PRIMARY KEY(owner,backup,path,digest,chat_table,local_id)
) WITHOUT ROWID;
CREATE TEMP TABLE extension_primary (
 owner TEXT NOT NULL,sid INTEGER NOT NULL,path TEXT NOT NULL,digest TEXT NOT NULL,
 chat_table TEXT NOT NULL,local_id INTEGER NOT NULL,raw_server TEXT,epoch INTEGER NOT NULL,
 typ INTEGER NOT NULL,direction TEXT NOT NULL,server_known INTEGER NOT NULL,target_id TEXT NOT NULL
);
CREATE TEMP TABLE extension_routes (
 owner TEXT NOT NULL,peer_hash TEXT NOT NULL,peer TEXT NOT NULL,
 conversation TEXT NOT NULL,priority INTEGER NOT NULL,
 PRIMARY KEY(owner,peer_hash)
) WITHOUT ROWID;
"""


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(4 * 1024**2):
            digest.update(chunk)
    return digest.hexdigest()


def _object(raw):
    if not isinstance(raw, str) or len(raw.encode()) > MAX_CELL_BYTES:
        raise ValueError("A native resource receipt exceeds its metadata budget.")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("A native resource receipt needs object metadata.")
    return value


def _batches(cursor, size):
    while rows := cursor.fetchmany(size):
        yield from rows


def _signature(server, epoch, typ, direction):
    if (
        server is not None
        and (not isinstance(server, (str, int)) or isinstance(server, bool))
        or type(epoch) is not int
        or type(typ) is not int
        or direction not in {"sent", "received", "unknown"}
    ):
        raise ValueError("A native resource lost its recorded metadata types.")
    raw = None if server is None else str(server)
    if raw is not None and re.fullmatch(r"-?[0-9]{1,20}", raw) is None:
        raise ValueError("A native resource server identifier is malformed.")
    positive = (
        str(int(raw))
        if raw is not None and 0 < int(raw) <= 9_223_372_036_854_775_807
        else None
    )
    exact_server = raw if raw is not None and int(raw) < 0 else positive
    return raw, positive, _json((exact_server, epoch, typ, direction))


def _native_direction(proof, fallback):
    """Compare original native metadata independently of its sender presentation."""
    recorded = proof.get("recorded_native_metadata", {})
    locator = proof.get("locator", {})
    original = recorded.get("Des") if isinstance(recorded, dict) else None
    observed = locator.get("native_direction") if isinstance(locator, dict) else None
    if original is not None and observed is not None and original != observed:
        raise ValueError("The original native direction receipts disagree.")
    raw = original if original is not None else observed
    if raw is not None:
        if type(raw) is not int:
            raise ValueError(
                "A native direction receipt lost its original integer value."
            )
        return {0: "sent", 1: "received"}.get(raw, "unknown")
    return proof.get("direction", fallback)


def _original_relative(value, owner):
    if not isinstance(value, str) or "\0" in value:
        raise ValueError("A native receipt needs its original relative database path.")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.parts.count("Documents") != 1:
        raise ValueError(
            "A native receipt cannot escape its original account directory."
        )
    index = path.parts.index("Documents")
    return _relative("/".join(path.parts[index:]), owner)


def _receipt(output, sid, path, digest, basis, proof):
    if not isinstance(digest, str) or _SHA.fullmatch(digest) is None:
        raise ValueError("A native database lacks its exact original SHA-256.")
    previous = output.execute(
        "SELECT digest FROM temp.extension_receipts WHERE sid=? AND path=?",
        (sid, path),
    ).fetchall()
    for prior in previous:
        if prior[0] != digest and any(
            output.execute(
                "SELECT 1 FROM temp.extension_declared_versions WHERE sid=? AND path=? AND digest=?",
                (sid, path, value),
            ).fetchone()
            is None
            for value in (prior[0], digest)
        ):
            raise ValueError(
                "One native database has conflicting undeclared source versions."
            )
    output.execute(
        "INSERT OR IGNORE INTO temp.extension_receipts VALUES(?,?,?,?,?)",
        (sid, path, digest, basis, _json(proof)),
    )


def _snapshots(history, owners, output):
    snapshots = {}
    for row in history.execute("SELECT * FROM source_snapshots ORDER BY id"):
        metadata = _object(row["metadata"])
        owner = metadata.get("source_account_id")
        if owner not in owners:
            continue
        native = "ios-" + hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest()
        if row["source_id"] != native:
            continue
        restored_bundle = (
            isinstance(metadata.get("native_restore_version"), str)
            and bool(metadata["native_restore_version"])
            and isinstance(metadata.get("source_inventory_sha256"), str)
            and _SHA.fullmatch(metadata["source_inventory_sha256"]) is not None
            and isinstance(metadata.get("native_occurrences"), list)
            and bool(metadata["native_occurrences"])
        )
        backup_frame = metadata.get("backup_id", metadata.get("snapshot_id"))
        if backup_frame is None and restored_bundle:
            backup_frame = row["snapshot_id"]
        if (
            metadata.get("source_id") != native
            or metadata.get("account_hash_verified") is not True
            or metadata.get("account_hash") != native[4:]
            or not isinstance(row["snapshot_id"], str)
            or not row["snapshot_id"]
            or backup_frame != row["snapshot_id"]
        ):
            continue
        identity = metadata.get("identity_evidence", {})
        legacy_identity = False
        member = metadata.get(
            "identity_member_path", metadata.get("native_identity_member_path")
        )
        if isinstance(identity, str) and isinstance(member, str):
            identity_path = PurePosixPath(member)
            parts = identity_path.parts
            legacy_identity = (
                metadata.get("container_kind") == "legacy_ios"
                and identity.startswith("NSKeyedArchiver UsrName ")
                and "MD5 account-directory agreement" in identity
                and not identity_path.is_absolute()
                and ".." not in parts
                and parts.count("Documents") == 1
                and parts[parts.index("Documents") :]
                == ("Documents", native[4:], "mmsetting.archive")
                and all(
                    isinstance(metadata.get(key), str) and _SHA.fullmatch(metadata[key])
                    for key in ("identity_sha256", "archive_sha256")
                )
                and isinstance(metadata.get("input_databases"), list)
                and bool(metadata["input_databases"])
            )
        if not legacy_identity and (
            not isinstance(identity, dict)
            or "directory_hash" in identity
            and identity["directory_hash"] != native[4:]
        ):
            continue
        snapshots[row["id"]] = dict(
            owner=owner,
            source_id=native,
            backup_id=row["snapshot_id"],
            metadata=metadata,
            metadata_sha256=hashlib.sha256(row["metadata"].encode()).hexdigest(),
        )
        declared = {}
        for occurrence in metadata.get("native_occurrences", ()):
            if not isinstance(occurrence, dict) or not re.fullmatch(
                r"DB/(?:message_[0-9]+|MM)\.sqlite", str(occurrence.get("kind", ""))
            ):
                continue
            path = _original_relative(occurrence.get("relative_path"), owner)
            digest = occurrence.get("sha256")
            file_id = hashlib.sha1(
                ("AppDomain-com.tencent.xin-" + path).encode(), usedforsecurity=False
            ).hexdigest()
            if (
                occurrence.get("account_hash") != native[4:]
                or not isinstance(digest, str)
                or _SHA.fullmatch(digest) is None
                or occurrence.get("object") != "objects/" + digest
                or occurrence.get("file_id") != file_id
                or occurrence.get("kind") != "DB/" + PurePosixPath(path).name
                or not isinstance(occurrence.get("manifest_sha256"), str)
                or _SHA.fullmatch(occurrence["manifest_sha256"]) is None
                or type(occurrence.get("bytes")) is not int
                or occurrence["bytes"] < 0
                or not isinstance(occurrence.get("backup_folder"), str)
                or not occurrence["backup_folder"]
            ):
                raise ValueError(
                    "A declared native source version lost its original preservation receipt."
                )
            declared.setdefault((path, digest), []).append(occurrence)
            output.execute(
                "INSERT OR IGNORE INTO temp.extension_declared_versions VALUES(?,?,?)",
                (row["id"], path, digest),
            )
        for (path, digest), occurrences in declared.items():
            _receipt(
                output,
                row["id"],
                path,
                digest,
                "preservation_manifest",
                dict(native_occurrences=occurrences),
            )
        for entry in metadata.get("input_databases", ()):
            if not isinstance(entry, dict):
                continue
            value = (
                entry.get("path")
                or entry.get("relative_path")
                or entry.get("member_path")
            )
            try:
                path = _original_relative(value, owner)
            except ValueError:
                continue
            _receipt(
                output,
                row["id"],
                path,
                entry.get("sha256"),
                "preservation_manifest",
                entry,
            )
    return snapshots


def _route(output, owner, hashed, conversation, priority):
    peer = conversation["peer_id"] or ""
    prior = output.execute(
        "SELECT peer,priority FROM temp.extension_routes WHERE owner=? AND peer_hash=?",
        (owner, hashed),
    ).fetchone()
    if prior and prior[0] and peer and prior[0] != peer:
        raise ValueError("Two peer identifiers claim one native table hash.")
    if prior and prior[1] <= priority:
        return
    output.execute(
        "INSERT OR REPLACE INTO temp.extension_routes VALUES(?,?,?,?,?)",
        (owner, hashed, peer, _json(dict(conversation)), priority),
    )


def _routes(history, catalog, output, owners, specs):
    for source, priority in ((catalog, 0), (history, 1)):
        for row in source.execute("SELECT * FROM conversations"):
            owner = (
                row["account_key"][8:]
                if row["account_key"].startswith("account:")
                else ""
            )
            peer = row["peer_id"]
            native = (
                "ios-" + hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest()
            )
            if (
                owner not in owners
                or not isinstance(peer, str)
                or not _PEER.fullmatch(peer)
            ):
                continue
            if source is history and row["partition_id"] != native:
                continue
            _route(
                output,
                owner,
                hashlib.md5(peer.encode(), usedforsecurity=False).hexdigest(),
                row,
                priority,
            )
    for spec in specs:
        owner = spec["owner"]
        native = "ios-" + hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest()
        for peer in spec.get("peer_ids", ()):
            if not isinstance(peer, str) or _PEER.fullmatch(peer) is None:
                raise ValueError(
                    "Native peer hints must retain exact WeChat identifiers."
                )
            hashed = hashlib.md5(peer.encode(), usedforsecurity=False).hexdigest()
            _route(
                output,
                owner,
                hashed,
                dict(
                    id="wechat:" + _identifier(native, peer),
                    partition_id=native,
                    account_key="account:" + owner,
                    peer_id=peer,
                    title=peer,
                    kind="group" if peer.endswith("@chatroom") else "direct",
                ),
                2,
            )


def _table_selected(scopes, owner, table):
    return scopes is None or table in scopes.get(owner, ())


def _select_chat_tables(spec):
    requested = spec.get("chat_tables")
    if "chat_tables" in spec and (
        not isinstance(requested, (list, tuple))
        or not requested
        or any(
            not isinstance(table, str) or _CHAT.fullmatch(table) is None
            for table in requested
        )
        or len(set(requested)) != len(requested)
    ):
        raise ValueError("Select a nonempty unique list of exact native Chat tables.")
    with _readonly(spec["path"]) as source:
        available = {
            row["name"]: row["sql"]
            for row in source.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            if _CHAT.fullmatch(row["name"])
        }
        selected = sorted(available if requested is None else requested)
        if any(table not in available for table in selected):
            raise ValueError("A selected native Chat table does not exist.")
        for table in selected:
            if "VIRTUAL" in str(available[table]).upper() or not set(_FIELDS) <= {
                row[1] for row in source.execute(f'PRAGMA table_info("{table}")')
            }:
                raise ValueError(
                    "A selected native Chat table lacks its ordinary metadata column contract."
                )
    return selected, len(available) - len(selected)


def _native_observation(
    output, proof, row, snapshots, receipt_cache, report, table_scopes=None
):
    sid = proof.get("source_snapshot")
    if type(sid) is not int:
        return
    snapshot = snapshots.get(sid)
    if snapshot is None or row["account_key"] != "account:" + snapshot["owner"]:
        return
    locator = proof.get("locator")
    if not isinstance(locator, dict):
        return
    owner = snapshot["owner"]
    value = (
        locator.get("database_relative_path")
        or locator.get("relative_path")
        or proof.get("source_relative_path")
    )
    if value is None and isinstance(locator.get("database"), str):
        database = locator["database"]
        value = (
            database
            if "/" in database
            else "Documents/" + snapshot["source_id"][4:] + "/DB/" + database
        )
    auxiliary_kind = locator.get("container_kind")
    auxiliary_suffix = {
        "brand": "Brand/BrandMsg.db",
        "chatbot": "ChatBot/msg.sqlite3",
    }.get(auxiliary_kind)
    if auxiliary_suffix:
        expected = "Documents/" + snapshot["source_id"][4:] + "/" + auxiliary_suffix
        file_id = hashlib.sha1(
            ("AppDomain-com.tencent.xin-" + expected).encode(), usedforsecurity=False
        ).hexdigest()
        table, local = locator.get("table"), locator.get("local_id")
        peer = row["peer_id"]
        declarations = [
            entry
            for entry in snapshot["metadata"].get("input_databases", ())
            if isinstance(entry, dict)
            and (
                entry.get("path")
                or entry.get("relative_path")
                or entry.get("member_path")
            )
            == expected
        ]
        if (
            value != expected
            or locator.get("database") != expected
            or not isinstance(locator.get("input_sha256"), str)
            or _SHA.fullmatch(locator["input_sha256"]) is None
            or locator.get("file_id") != file_id
            or len(declarations) != 1
            or declarations[0].get("sha256") != locator.get("input_sha256")
            or declarations[0].get("file_id") != file_id
            or type(local) is not int
            or local < 1
            or locator.get("row_id") != local
            or not isinstance(table, str)
            or _CHAT.fullmatch(table) is None
            or peer
            and table
            != "Chat_" + hashlib.md5(peer.encode(), usedforsecurity=False).hexdigest()
        ):
            raise ValueError(
                "An auxiliary source changed its independently retained account frame."
            )
        report["outside_scope_" + auxiliary_kind + "_observations"] += 1
        return
    path = _original_relative(value, owner)
    digest = locator.get("database_sha256") or proof.get("source_sha256")
    receipts = receipt_cache.setdefault((sid, path), set())
    digest = digest or (next(iter(receipts)) if len(receipts) == 1 else None)
    table, local = locator.get("table"), locator.get("local_id")
    match = _CHAT.fullmatch(table) if isinstance(table, str) else None
    peer = row["peer_id"]
    if (
        not match
        or type(local) is not int
        or local < 1
        or locator.get("row_id", local) != local
        or peer
        and hashlib.md5(peer.encode(), usedforsecurity=False).hexdigest() != match[1]
    ):
        raise ValueError(
            "A native observation changed its exact account or table locator."
        )
    direction = _native_direction(proof, row["direction"])
    recorded = proof.get("recorded_native_metadata", {})
    server_known = (
        isinstance(recorded, dict)
        and "MesSvrID" in recorded
        or proof.get("server_id") is not None
    )
    server = (
        recorded.get("MesSvrID", proof.get("server_id"))
        if isinstance(recorded, dict)
        else proof.get("server_id")
    )
    raw, _, _ = _signature(
        server, proof.get("unix_timestamp"), proof.get("message_type"), direction
    )
    if proof.get("text_extraction_kind") != "resource_placeholder":
        file_id = hashlib.sha1(
            ("AppDomain-com.tencent.xin-" + path).encode(), usedforsecurity=False
        ).hexdigest()
        if (
            locator.get("file_id") != file_id
            or locator.get("database_sha256") != digest
        ):
            if digest not in receipts:
                return
        elif digest not in receipts:
            _receipt(
                output,
                sid,
                path,
                digest,
                "primary_source_database_receipt",
                dict(
                    message_id=row["id"],
                    source_snapshot_id=sid,
                    file_id=file_id,
                    metadata_sha256=snapshot["metadata_sha256"],
                ),
            )
            receipts.add(digest)
    if (
        not isinstance(digest, str)
        or _SHA.fullmatch(digest) is None
        or digest not in receipts
    ):
        return
    if not _table_selected(table_scopes, owner, table):
        report["primary_observations_outside_table_scope"] += 1
        return
    if (
        row["kind"] == "group"
        and direction == "sent"
        and locator.get("native_direction") == 0
        and isinstance(locator.get("native_sender_prefix"), str)
        and bool(locator["native_sender_prefix"])
        and locator["native_sender_prefix"] != owner
        and locator.get("native_sender_prefix_matches_owner") is False
        and locator.get("sender_prefix_identity_status") == "unresolved"
        and row["direction"] == "unknown"
        and proof.get("text_extraction_kind") != "resource_placeholder"
    ):
        identifier = _identifier(
            "wechat-resource-direction-scope-v1",
            owner,
            snapshot["backup_id"],
            path,
            digest,
            table,
            "0",
        )
        witness = dict(
            source_snapshot_id=sid,
            source_metadata_sha256=snapshot["metadata_sha256"],
            primary_message_id=row["id"],
            native_direction=0,
            native_sender_prefix=locator["native_sender_prefix"],
            native_sender_prefix_matches_owner=False,
            sender_prefix_identity_status="unresolved",
        )
        output.execute(
            "INSERT OR IGNORE INTO native_resource_direction_scopes VALUES(?,?,?,?,?,?,?,?)",
            (
                identifier,
                owner,
                snapshot["backup_id"],
                path,
                digest,
                table,
                0,
                _json(witness),
            ),
        )
    if proof["message_type"] not in _KINDS:
        return
    output.execute(
        "INSERT INTO temp.extension_primary VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            owner,
            sid,
            path,
            digest,
            table,
            local,
            raw,
            proof["unix_timestamp"],
            proof["message_type"],
            direction,
            int(server_known),
            row["id"],
        ),
    )


def _primary(history, output, snapshots, owners, batch, report, table_scopes=None):
    receipt_cache = {}
    for row in output.execute("SELECT sid,path,digest FROM temp.extension_receipts"):
        receipt_cache.setdefault((row[0], row[1]), set()).add(row[2])
    marks = ",".join("?" for _ in owners)
    query = (
        "SELECT m.id,m.direction,CASE WHEN length(CAST(m.provenance AS BLOB))<=? "
        "THEN m.provenance END AS provenance,c.account_key,c.peer_id,c.kind FROM messages m "
        "JOIN conversations c ON c.id=m.conversation_id WHERE c.account_key IN ("
        + marks
        + ")"
    )
    parameters = (MAX_CELL_BYTES, *("account:" + owner for owner in sorted(owners)))
    for row in _batches(history.execute(query, parameters), batch):
        proof = _object(row["provenance"])
        _native_observation(
            output, proof, row, snapshots, receipt_cache, report, table_scopes
        )
        report["primary_observations_checked"] += 1
    if history.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_evidence'"
    ).fetchone():
        envelopes = {}
        for sid, snapshot in snapshots.items():
            key = snapshot["source_id"], snapshot["backup_id"]
            envelopes.setdefault(key, []).append(sid)
        query = (
            "SELECT m.id,m.direction,CASE WHEN length(CAST(e.observation AS BLOB))<=? "
            "THEN e.observation END AS provenance,c.account_key,c.peer_id,c.kind,e.source_id,e.snapshot_id "
            "FROM message_evidence e JOIN messages m ON m.id=e.message_id "
            "JOIN conversations c ON c.id=m.conversation_id WHERE c.account_key IN ("
            + marks
            + ")"
        )
        for row in _batches(history.execute(query, parameters), batch):
            proof = _object(row["provenance"])
            choices = envelopes.get((row["source_id"], row["snapshot_id"]), ())
            if "source_snapshot" in proof:
                if (
                    proof["source_snapshot"] in snapshots
                    and proof["source_snapshot"] not in choices
                ):
                    raise ValueError(
                        "An independent native observation changed its source envelope."
                    )
                _native_observation(
                    output, proof, row, snapshots, receipt_cache, report, table_scopes
                )
            else:
                for sid in choices:
                    _native_observation(
                        output,
                        {**proof, "source_snapshot": sid},
                        row,
                        snapshots,
                        receipt_cache,
                        report,
                        table_scopes,
                    )
            report["primary_observations_checked"] += 1


def _keys(
    output,
    owner,
    backup,
    path,
    digest,
    table,
    local,
    server,
    epoch,
    typ,
    direction,
    target_store,
    target_id,
    *,
    server_observed=True,
):
    raw, positive, signature = _signature(server, epoch, typ, direction)
    exact = (owner, backup, path, digest, table, local)
    prior = output.execute(
        "SELECT signature,target_store,target_id,server_known FROM temp.extension_locators WHERE owner=? AND backup=? AND path=? AND digest=? AND chat_table=? AND local_id=?",
        exact,
    ).fetchone()
    if prior and prior[0] != signature:
        old = json.loads(prior[0])
        new = json.loads(signature)
        if old[1:] != new[1:] or prior[3] and server_observed:
            raise ValueError(
                "One exact native source locator has conflicting metadata."
            )
    if prior and not prior[3] and server_observed:
        output.execute(
            "UPDATE temp.extension_locators SET signature=?,server_known=1 WHERE owner=? AND backup=? AND path=? AND digest=? AND chat_table=? AND local_id=?",
            (signature, *exact),
        )
    match = (prior[1], prior[2]) if prior else None
    hashed = _CHAT.fullmatch(table)[1]
    positive_key = (owner, hashed, positive, epoch, typ, direction)
    if positive:
        found = output.execute(
            "SELECT target_store,target_id FROM temp.extension_events WHERE owner=? AND peer_hash=? AND server=? AND epoch=? AND typ=? AND direction=?",
            positive_key,
        ).fetchone()
        if found:
            match = tuple(found)
    canonical = match or (target_store, target_id)
    output.execute(
        "INSERT OR IGNORE INTO temp.extension_locators VALUES(?,?,?,?,?,?,?,?,?,?)",
        (*exact, signature, int(server_observed), *canonical),
    )
    if positive:
        output.execute(
            "INSERT OR IGNORE INTO temp.extension_events VALUES(?,?,?,?,?,?,?,?)",
            (*positive_key, *canonical),
        )
    return canonical, match is not None


def _seed(history, catalog, output, snapshots, owners, batch, report, table_scopes=None):
    for row in _batches(
        output.execute(
            "SELECT p.* FROM temp.extension_primary p JOIN temp.extension_receipts r ON r.sid=p.sid AND r.path=p.path AND r.digest=p.digest"
        ),
        batch,
    ):
        _keys(
            output,
            row["owner"],
            snapshots[row["sid"]]["backup_id"],
            *tuple(row)[2:10],
            "primary",
            row["target_id"],
            server_observed=bool(row["server_known"]),
        )
    query = "SELECT m.id,m.conversation_id,m.direction,m.provenance,c.* FROM messages m JOIN conversations c ON c.id=m.conversation_id"
    for row in _batches(catalog.execute(query), batch):
        owner = (
            row["account_key"][8:] if row["account_key"].startswith("account:") else ""
        )
        if owner not in owners:
            continue
        proof = _object(row["provenance"])
        locator = proof.get("locator", {})
        table = locator.get("table")
        match = _CHAT.fullmatch(table) if isinstance(table, str) else None
        if match is None:
            continue
        if not _table_selected(table_scopes, owner, table):
            report["catalog_rows_outside_table_scope"] += 1
            continue
        conversation = {
            key: row[key]
            for key in ("partition_id", "account_key", "peer_id", "title", "kind")
        }
        conversation["id"] = row["conversation_id"]
        _route(output, owner, match[1], conversation, 0)
        path = _relative(proof.get("source_relative_path"), owner)
        digest, backup = proof.get("source_sha256"), proof.get("backup_id")
        choices = [
            sid
            for sid, value in snapshots.items()
            if value["owner"] == owner and value["backup_id"] == backup
        ]
        for sid in choices:
            receipt = output.execute(
                "SELECT 1 FROM temp.extension_receipts WHERE sid=? AND path=? AND digest=?",
                (sid, path, digest),
            ).fetchone()
            if receipt:
                recorded = proof.get("recorded_native_metadata", {})
                server_known = (
                    isinstance(recorded, dict)
                    and "MesSvrID" in recorded
                    or proof.get("server_id") is not None
                )
                server = (
                    recorded.get("MesSvrID", proof.get("server_id"))
                    if isinstance(recorded, dict)
                    else proof.get("server_id")
                )
                _keys(
                    output,
                    owner,
                    snapshots[sid]["backup_id"],
                    path,
                    digest,
                    table,
                    locator["local_id"],
                    server,
                    proof["unix_timestamp"],
                    proof["message_type"],
                    _native_direction(proof, row["direction"]),
                    "catalog",
                    row["id"],
                    server_observed=server_known,
                )
    if catalog.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_resource_occurrences'"
    ).fetchone():
        for row in _batches(
            catalog.execute("SELECT * FROM native_resource_occurrences"), batch
        ):
            sid, owner = row["source_snapshot_id"], row["account_id"]
            snapshot = snapshots.get(sid)
            if owner not in owners or snapshot is None or snapshot["owner"] != owner:
                continue
            if not _table_selected(table_scopes, owner, row["chat_table"]):
                report["retained_occurrences_outside_table_scope"] += 1
                continue
            receipt = output.execute(
                "SELECT 1 FROM temp.extension_receipts WHERE sid=? AND path=? AND digest=?",
                (sid, row["database_relative_path"], row["database_sha256"]),
            ).fetchone()
            if receipt is None:
                continue
            target = history if row["canonical_store"] == "primary" else catalog
            if (
                row["canonical_store"] not in {"primary", "catalog"}
                or target.execute(
                    "SELECT 1 FROM messages WHERE id=?", (row["canonical_id"],)
                ).fetchone()
                is None
            ):
                raise ValueError(
                    "A retained native occurrence lost its canonical event."
                )
            raw = _object(row["native_metadata"])
            direction = {0: "sent", 1: "received"}.get(raw["Des"], "unknown")
            _keys(
                output,
                owner,
                snapshot["backup_id"],
                row["database_relative_path"],
                row["database_sha256"],
                row["chat_table"],
                row["local_id"],
                raw["MesSvrID"],
                raw["CreateTime"],
                raw["Type"],
                direction,
                row["canonical_store"],
                row["canonical_id"],
            )


def _preservation(output, spec, snapshot, identities):
    report_path, sources_path = (
        spec.get("preservation_report_path"),
        spec.get("sources_path"),
    )
    if report_path is None and sources_path is None:
        return
    if report_path is None or sources_path is None:
        raise ValueError(
            "Preserved native sources require both immutable receipt files."
        )
    paths = [Path(path).absolute() for path in (report_path, sources_path)]
    values = []
    for path in paths:
        identities[path] = _resident(path)
        if identities[path]["size"] > MAX_CELL_BYTES:
            raise ValueError("A preservation receipt exceeds its metadata budget.")
        values.append(json.loads(path.read_text()))
    report, sources = values
    if (
        not isinstance(report, dict)
        or not isinstance(sources, list)
        or report.get("snapshot_id") != snapshot["backup_id"]
    ):
        raise ValueError("The preservation report changed its native snapshot.")
    selected = [
        item
        for item in sources
        if isinstance(item, dict) and item.get("source_id") == snapshot["source_id"]
    ]
    if (
        len(selected) != 1
        or any(
            snapshot["metadata"].get(key) != value for key, value in selected[0].items()
        )
        or set(snapshot["metadata"]) - set(selected[0]) - {"cross_validation"}
    ):
        raise ValueError(
            "The preservation sources differ from the current snapshot receipt."
        )
    entries = [
        entry
        for entry in report.get("selected_files", ())
        if isinstance(entry, dict)
        and entry.get("domain") == "AppDomain-com.tencent.xin"
        and entry.get("relative_path") == spec["relative"]
    ]
    if len(entries) != 1:
        raise ValueError(
            "The preservation report must select one exact message database."
        )
    entry = entries[0]
    digest = entry.get("sha256")
    file_id = hashlib.sha1(
        ("AppDomain-com.tencent.xin-" + spec["relative"]).encode(),
        usedforsecurity=False,
    ).hexdigest()
    if (
        entry.get("snapshot_sha256") != digest
        or entry.get("source_sha256") != digest
        or entry.get("bytes") != identities[spec["path"]]["size"]
        or entry.get("file_id") != file_id
        or entry.get("input_relative_path") != "inputs/" + spec["relative"]
    ):
        raise ValueError(
            "The preserved message database lost its independent file receipt."
        )
    if spec["path"] != paths[0].parent / "inputs" / spec["relative"]:
        raise ValueError(
            "The preserved database escaped its immutable workstation mapping."
        )
    _receipt(
        output,
        spec["source_snapshot_id"],
        spec["relative"],
        digest,
        "preservation_manifest",
        dict(
            entry=entry,
            report=str(paths[0]),
            report_sha256=_sha256(paths[0]),
            sources=str(paths[1]),
            sources_sha256=_sha256(paths[1]),
        ),
    )


def _conversation(output, spec, hashed):
    row = output.execute(
        "SELECT conversation FROM temp.extension_routes WHERE owner=? AND peer_hash=?",
        (spec["owner"], hashed),
    ).fetchone()
    if row:
        return json.loads(row[0])
    native = spec["snapshot"]["source_id"]
    return dict(
        id="wechat:" + _identifier(native, hashed),
        partition_id=native,
        account_key="account:" + spec["owner"],
        peer_id="",
        title="Unidentified conversation · " + hashed[-8:],
        kind="unclassified",
    )


def _coverage(output, values):
    key = values[:4]
    prior = output.execute(
        "SELECT * FROM native_resource_coverage WHERE source_snapshot_id=? AND database_relative_path=? AND database_sha256=? AND chat_table=?",
        key,
    ).fetchone()
    if prior and tuple(prior) != tuple(values):
        raise ValueError("One sealed native table changed its resource coverage.")
    output.execute(
        "INSERT OR IGNORE INTO native_resource_coverage VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        values,
    )


def stage_native_resource_catalog_extension(
    local_store_root,
    base_catalog_path,
    candidate_path,
    *,
    database_specs,
    batch_size=1000,
    resolve_sender_prefixes=False,
    sender_dictionaries=None,
    progress=None,
):
    """Extend a pinned catalog from resident exact databases; never publish or hydrate.

    Each database spec selects ``source_snapshot_id``, an absolute ``database_path``
    and its original ``database_relative_path``. Optional ``peer_ids`` supply exact
    identifier preimages for resource-only tables, never display-name matches.
    ``preservation_report_path`` and ``sources_path`` can prove preserved shards
    without primary text witnesses. Every resource occurrence receives a retained
    source membership, including events already represented by another snapshot.
    Optional ``chat_tables`` selects complete ordinary Chat tables, never row slices.
    Deduplication seeds stay inside the union of those account/table scopes while
    out-of-scope primary observations may still prove a selected full database.
    ``resolve_sender_prefixes`` reads only the bounded native Message column for
    new received group resources after exact source and occurrence verification.
    It never opens media files. The default retains metadata-only coverage.
    """
    if type(batch_size) is not int or not 1 <= batch_size <= 10_000:
        raise ValueError("Select a bounded metadata fetch batch.")
    if type(resolve_sender_prefixes) is not bool:
        raise ValueError("Select an explicit native sender resolution policy.")
    if sender_dictionaries is not None and not resolve_sender_prefixes:
        raise ValueError("Native dictionaries require sender resolution to be enabled.")
    specs = [dict(value) for value in database_specs]
    if not specs or len(specs) > 4096:
        raise ValueError("Select an explicit bounded native database inventory.")
    history_path = wechat_archive_path(local_store_root).absolute()
    base, candidate = (
        Path(base_catalog_path).absolute(),
        Path(candidate_path).absolute(),
    )
    if (
        candidate.exists()
        or candidate.is_symlink()
        or not candidate.parent.is_dir()
        or candidate.parent.is_symlink()
    ):
        raise ValueError("Select a new candidate in an existing regular directory.")
    identities = {path: _resident(path) for path in (history_path, base)}
    base_digest = _sha256(base)
    report = dict(
        version=VERSION,
        candidate=str(candidate),
        base_catalog=str(base),
        base_sha256=base_digest,
        history_identity=identities[history_path],
        added=0,
        source_databases=0,
        tables_checked=0,
        resource_rows=0,
        primary_observations_checked=0,
        primary_duplicates=0,
        catalog_duplicates=0,
        repeated_occurrences=0,
        occurrences_added=0,
        table_scope=(
            "explicit_chat_tables"
            if any("chat_tables" in spec for spec in specs)
            else "all_chat_tables_in_selected_databases"
        ),
        selected_table_scopes=[],
        outside_scope_tables=0,
        primary_observations_outside_table_scope=0,
        catalog_rows_outside_table_scope=0,
        retained_occurrences_outside_table_scope=0,
        unknown_peer_tables=0,
        outside_scope_brand_observations=0,
        outside_scope_chatbot_observations=0,
        presentation_unknown_resources_added=0,
        media_payload_files_read=0,
        message_payload_columns_read=0,
        resolve_sender_prefixes=resolve_sender_prefixes,
        native_sender_statuses={},
        cloud_inputs_hydrated=0,
        published=False,
    )
    started = False
    try:
        with _readonly(history_path) as history, _readonly(base) as catalog:
            info = dict(catalog.execute("SELECT key,value FROM archive_info"))
            if (
                info.get("schema_version") != "1"
                or json.loads(info.get("history_file_identity", "null"))
                != identities[history_path]
                or info.get("history_partitions_sha256")
                != wechat_history_partition_fingerprint(history)
            ):
                raise ValueError(
                    "The base resource catalog is not pinned to the current primary."
                )
            owners = {}
            for row in history.execute("SELECT * FROM partitions"):
                try:
                    owners[_owner(row)] = dict(row)
                except ValueError:
                    continue
            selected = set()
            for spec in specs:
                sid = spec.get("source_snapshot_id")
                snapshot = history.execute(
                    "SELECT metadata FROM source_snapshots WHERE id=?", (sid,)
                ).fetchone()
                if type(sid) is not int or sid < 1 or snapshot is None:
                    raise ValueError(
                        "Select an exact independently retained native snapshot."
                    )
                owner = _object(snapshot[0]).get("source_account_id")
                if owner not in owners:
                    raise ValueError(
                        "The selected resource source is not a verified native owner."
                    )
                spec.update(
                    owner=owner,
                    relative=_original_relative(
                        spec.get("database_relative_path"), owner
                    ),
                )
                path = Path(spec.get("database_path", ""))
                if not path.is_absolute():
                    raise ValueError(
                        "Select an absolute resident native database path."
                    )
                spec["path"] = path.absolute()
                identities[spec["path"]] = _resident(spec["path"])
                key = (sid, spec["relative"], str(spec["path"]))
                if key in selected:
                    raise ValueError(
                        "Select each physical native source database only once."
                    )
                selected.add(key)
            owners = {spec["owner"] for spec in specs}
            table_scopes = {} if any("chat_tables" in spec for spec in specs) else None
            for spec in specs:
                tables, outside = _select_chat_tables(spec)
                spec["selected_tables"] = tables
                if table_scopes is not None:
                    table_scopes.setdefault(spec["owner"], set()).update(tables)
                report["outside_scope_tables"] += outside
                report["selected_table_scopes"].append(
                    dict(
                        source_snapshot_id=spec["source_snapshot_id"],
                        database_relative_path=spec["relative"],
                        chat_tables=tables,
                        outside_scope_tables=outside,
                    )
                )
            _clonefile(base, candidate)
            started = True
            candidate.chmod(0o600)
            if _sha256(candidate) != base_digest:
                raise ValueError("The resource candidate is not an exact base clone.")
            with closing(sqlite3.connect(candidate)) as output:
                output.row_factory = sqlite3.Row
                output.execute("PRAGMA journal_mode=DELETE")
                output.execute("PRAGMA synchronous=FULL")
                output.execute("PRAGMA temp_store=FILE")
                output.executescript(_SCHEMA)
                snapshots = _snapshots(history, owners, output)
                for spec in specs:
                    snapshot = snapshots.get(spec["source_snapshot_id"])
                    if snapshot is None:
                        raise ValueError(
                            "The selected snapshot lacks independent native account evidence."
                        )
                    spec["snapshot"] = snapshot
                    _preservation(output, spec, snapshot, identities)
                _primary(
                    history, output, snapshots, owners, batch_size, report, table_scopes
                )
                _routes(history, catalog, output, owners, specs)
                _seed(
                    history, catalog, output, snapshots, owners, batch_size, report,
                    table_scopes,
                )
                report["seed_primary_rows"] = output.execute(
                    "SELECT COUNT(*) FROM temp.extension_primary"
                ).fetchone()[0]
                report["seed_event_keys"] = output.execute(
                    "SELECT COUNT(*) FROM temp.extension_events"
                ).fetchone()[0]
                report["seed_locator_keys"] = output.execute(
                    "SELECT COUNT(*) FROM temp.extension_locators"
                ).fetchone()[0]
                before = catalog.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                prior_report = _object(info.get("report", "{}"))
                kinds = Counter(prior_report.get("kinds", {}))
                accounts = Counter(
                    {
                        row["account_id"]: row["messages"]
                        for row in prior_report.get("accounts", ())
                    }
                )
                if sum(kinds.values()) != before or sum(accounts.values()) != before:
                    raise ValueError(
                        "The base catalog lacks exact kind and account count receipts."
                    )
                affected = {}
                message_columns = tuple(
                    row[1] for row in output.execute("PRAGMA table_info(messages)")
                )
                for spec in specs:
                    sid, path, relative, snapshot = (
                        spec["source_snapshot_id"],
                        spec["path"],
                        spec["relative"],
                        spec["snapshot"],
                    )
                    digest = _sha256(path)
                    receipt = output.execute(
                        "SELECT * FROM temp.extension_receipts WHERE sid=? AND path=? AND digest=?",
                        (sid, relative, digest),
                    ).fetchone()
                    if receipt is None:
                        if output.execute(
                            "SELECT 1 FROM temp.extension_receipts WHERE sid=? AND path=?",
                            (sid, relative),
                        ).fetchone():
                            raise ValueError(
                                "The resident native database failed its full original SHA-256 receipt."
                            )
                        raise ValueError(
                            "A native database has no independent primary or preservation receipt."
                        )
                    if (
                        spec.get("source_database_sha256", digest) != digest
                        or _resident(path) != identities[path]
                    ):
                        raise ValueError(
                            "The resident native database failed its full original SHA-256 receipt."
                        )
                    file_id = hashlib.sha1(
                        ("AppDomain-com.tencent.xin-" + relative).encode(),
                        usedforsecurity=False,
                    ).hexdigest()
                    output.execute(
                        "INSERT OR IGNORE INTO native_resource_sources VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            sid,
                            relative,
                            digest,
                            spec["owner"],
                            snapshot["source_id"],
                            snapshot["backup_id"],
                            snapshot["metadata_sha256"],
                            file_id,
                            receipt["basis"],
                            receipt["proof"],
                        ),
                    )
                    output.execute(
                        "INSERT OR IGNORE INTO native_resource_source_inputs VALUES(?,?,?,?,?)",
                        (sid, relative, digest, str(path), _json(identities[path])),
                    )
                    with _readonly(path) as source:
                        tables = source.execute(
                            "SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"
                        ).fetchall()
                        for entry in tables:
                            table = entry["name"]
                            match = _CHAT.fullmatch(table)
                            if match is None or table not in spec["selected_tables"]:
                                continue
                            columns = {
                                row[1]
                                for row in source.execute(
                                    f'PRAGMA table_info("{table}")'
                                )
                            }
                            if "VIRTUAL" in str(entry["sql"]).upper() or not set(
                                _FIELDS
                            ) <= columns:
                                raise ValueError(
                                    "A native chat table lacks its ordinary metadata column contract."
                                )
                            conversation = _conversation(output, spec, match[1])
                            counts, total, resources, covered, nontext = (
                                Counter(),
                                0,
                                0,
                                0,
                                0,
                            )
                            previous_local = 0
                            evidence = dict(
                                account_id=spec["owner"],
                                peer_id=conversation["peer_id"],
                                source_snapshot_id=sid,
                                source_metadata_sha256=snapshot["metadata_sha256"],
                                source_database_sha256=digest,
                            )
                            project_receipt = dict(
                                owner=spec["owner"],
                                backup_id=snapshot["backup_id"],
                                basis=receipt["basis"],
                            )
                            query = f'SELECT MesLocalID,CAST(MesSvrID AS TEXT) AS MesSvrID,CreateTime,Type,Des FROM "{table}" ORDER BY MesLocalID'
                            for raw_row in _batches(source.execute(query), batch_size):
                                raw = dict(raw_row)
                                local, epoch, typ = (
                                    raw["MesLocalID"],
                                    raw["CreateTime"],
                                    raw["Type"],
                                )
                                direction = {0: "sent", 1: "received"}.get(
                                    raw["Des"], "unknown"
                                )
                                if (
                                    type(local) is not int
                                    or local <= previous_local
                                    or type(raw["Des"]) is not int
                                ):
                                    raise ValueError(
                                        "A native metadata row lost its exact local ID or direction value."
                                    )
                                previous_local = local
                                _signature(raw["MesSvrID"], epoch, typ, direction)
                                total += 1
                                nontext += typ != 1
                                counts[str(typ)] += 1
                                if progress and total % batch_size == 0:
                                    progress(
                                        {
                                            **report,
                                            "phase": "rows",
                                            "table": table,
                                            "table_native_rows": total,
                                            "temp_sqlite_bytes": output.execute(
                                                "PRAGMA temp.page_count"
                                            ).fetchone()[0]
                                            * output.execute(
                                                "PRAGMA temp.page_size"
                                            ).fetchone()[0],
                                        }
                                    )
                                if typ not in _KINDS:
                                    continue
                                resources += 1
                                projected = {
                                    **raw,
                                    "MesSvrID": raw["MesSvrID"]
                                    if raw["MesSvrID"] is not None
                                    else "0",
                                }
                                row, proof = _project(
                                    evidence,
                                    projected,
                                    project_receipt,
                                    relative,
                                    table,
                                    conversation,
                                )
                                direction_scope = None
                                if raw["Des"] == 0 and conversation["kind"] == "group":
                                    direction_scope = output.execute(
                                        "SELECT id FROM native_resource_direction_scopes WHERE account_id=? AND backup_id=? AND database_relative_path=? AND database_sha256=? AND chat_table=? AND native_des=0",
                                        (
                                            spec["owner"],
                                            snapshot["backup_id"],
                                            relative,
                                            digest,
                                            table,
                                        ),
                                    ).fetchone()
                                if direction_scope:
                                    proof.update(
                                        presentation_direction_basis="unresolved_native_des0_sender_prefix",
                                        direction_scope_id=direction_scope[0],
                                    )
                                    flags = json.loads(row[12])
                                    for flag in (
                                        "native_des0_sender_unresolved",
                                        "direction_unavailable",
                                        "sender_id_unavailable",
                                    ):
                                        if flag not in flags:
                                            flags.append(flag)
                                    search = "\n".join(
                                        (
                                            row[6],
                                            "",
                                            conversation["title"],
                                            conversation["peer_id"],
                                        )
                                    ).casefold()
                                    row = (
                                        *row[:3],
                                        None,
                                        "Unknown sender",
                                        "unknown",
                                        *row[6:12],
                                        _json(flags),
                                        search,
                                    )
                                identifier = _identifier(
                                    "wechat-resource-reference-v2",
                                    spec["owner"],
                                    snapshot["backup_id"],
                                    "AppDomain-com.tencent.xin/" + relative,
                                    digest,
                                    table,
                                    str(local),
                                )
                                proof.update(
                                    record_id=identifier,
                                    record_id_scheme="wechat-resource-reference-v2",
                                )
                                proof["recorded_native_metadata"] = raw
                                proof["locator"].update(
                                    database_relative_path=relative,
                                    database_sha256=digest,
                                    file_id=file_id,
                                )
                                if not conversation["peer_id"]:
                                    proof["unresolved_peer_hash"] = match[1]
                                row = (identifier, *row[1:11], _json(proof), *row[12:])
                                canonical, matched = _keys(
                                    output,
                                    spec["owner"],
                                    snapshot["backup_id"],
                                    relative,
                                    digest,
                                    table,
                                    local,
                                    raw["MesSvrID"],
                                    epoch,
                                    typ,
                                    direction,
                                    "catalog",
                                    row[0],
                                )
                                occurrence = (
                                    sid,
                                    relative,
                                    digest,
                                    table,
                                    local,
                                    spec["owner"],
                                    match[1],
                                    *canonical,
                                    _json(raw),
                                )
                                prior = output.execute(
                                    "SELECT * FROM native_resource_occurrences WHERE source_snapshot_id=? AND database_relative_path=? AND database_sha256=? AND chat_table=? AND local_id=?",
                                    occurrence[:5],
                                ).fetchone()
                                if prior and tuple(prior) != occurrence:
                                    raise ValueError(
                                        "One native resource occurrence changed its retained membership."
                                    )
                                existing = output.execute(
                                    "SELECT 1 FROM messages WHERE id=?", (row[0],)
                                ).fetchone()
                                if existing and not matched:
                                    raise ValueError(
                                        "A native resource ID has unrelated existing evidence."
                                    )
                                if canonical == ("catalog", row[0]) and not existing:
                                    if (
                                        resolve_sender_prefixes
                                        and conversation["kind"] == "group"
                                        and direction == "received"
                                    ):
                                        payload = (
                                            "CASE WHEN length(CAST(Message AS BLOB))<=? "
                                            "THEN Message END"
                                            if "Message" in columns
                                            else "NULL"
                                        )
                                        marker = (
                                            '"WCDB_CT_Message"'
                                            if "WCDB_CT_Message" in columns
                                            else "0"
                                        )
                                        parameters = (
                                            (MAX_MESSAGE_BYTES, local)
                                            if "Message" in columns
                                            else (local,)
                                        )
                                        native = source.execute(
                                            "SELECT MesLocalID,CAST(MesSvrID AS TEXT) AS MesSvrID,"
                                            "CreateTime,Type,Des,"
                                            f"{payload} AS Message,{marker} AS compression_marker "
                                            f'FROM "{table}" WHERE MesLocalID=?',
                                            parameters,
                                        ).fetchone()
                                        if native is None:
                                            raise ValueError(
                                                "The verified native sender row disappeared."
                                            )
                                        enriched, resolution = resolve_source_bound_resource_sender(
                                            dict(zip(message_columns, row, strict=True)),
                                            proof,
                                            dict(native),
                                            conversation=conversation,
                                            owner=spec["owner"],
                                            dictionaries=sender_dictionaries,
                                        )
                                        row = tuple(enriched[key] for key in message_columns)
                                        report["message_payload_columns_read"] += (
                                            "Message" in columns
                                        )
                                        statuses = report["native_sender_statuses"]
                                        status = resolution["status"]
                                        statuses[status] = statuses.get(status, 0) + 1
                                    output.execute(
                                        "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                        row,
                                    )
                                    affected[conversation["id"]] = conversation
                                    report["added"] += 1
                                    report["presentation_unknown_resources_added"] += (
                                        bool(direction_scope)
                                    )
                                    kinds[proof["resource_reference"]["kind"]] += 1
                                    accounts[spec["owner"]] += 1
                                else:
                                    report[
                                        "primary_duplicates"
                                        if canonical[0] == "primary"
                                        else "catalog_duplicates"
                                    ] += 1
                                if prior:
                                    report["repeated_occurrences"] += 1
                                else:
                                    report["occurrences_added"] += 1
                                output.execute(
                                    "INSERT OR IGNORE INTO native_resource_occurrences VALUES(?,?,?,?,?,?,?,?,?,?)",
                                    occurrence,
                                )
                                covered += 1
                            status = (
                                "covered"
                                if conversation["peer_id"]
                                else "unresolved_peer_hash"
                            )
                            _coverage(
                                output,
                                (
                                    sid,
                                    relative,
                                    digest,
                                    table,
                                    spec["owner"],
                                    match[1],
                                    conversation["peer_id"],
                                    conversation["id"],
                                    status,
                                    total,
                                    resources,
                                    nontext,
                                    covered,
                                    _json(dict(counts)),
                                ),
                            )
                            report["tables_checked"] += 1
                            report["unknown_peer_tables"] += not bool(
                                conversation["peer_id"]
                            )
                            report["resource_rows"] += resources
                            if progress:
                                progress({**report, "phase": "table", "table": table})
                    report["source_databases"] += 1
                for cid, conversation in affected.items():
                    count = output.execute(
                        "SELECT COUNT(*) FROM messages WHERE conversation_id=?", (cid,)
                    ).fetchone()[0]
                    first = output.execute(
                        "SELECT time_sort,timestamp_value FROM messages WHERE conversation_id=? ORDER BY (time_sort=0),time_sort,ordinal,id LIMIT 1",
                        (cid,),
                    ).fetchone()
                    last = output.execute(
                        "SELECT time_sort,timestamp_value,text FROM messages WHERE conversation_id=? ORDER BY time_sort DESC,ordinal DESC,id DESC LIMIT 1",
                        (cid,),
                    ).fetchone()
                    output.execute(
                        "INSERT OR REPLACE INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
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
                        + (count, first[0], last[0], first[1], last[1], last[2][:300]),
                    )
                total = output.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                report["unresolved_des0_scopes"] = output.execute(
                    "SELECT COUNT(*) FROM native_resource_direction_scopes"
                ).fetchone()[0]
                if total != before + report["added"]:
                    raise ValueError(
                        "The resource extension changed unrelated message counts."
                    )
                metadata = {
                    **prior_report,
                    "version": VERSION,
                    "messages": total,
                    "conversations": output.execute(
                        "SELECT COUNT(*) FROM conversations"
                    ).fetchone()[0],
                    "kinds": dict(kinds),
                    "accounts": [
                        dict(account_id=owner, messages=count)
                        for owner, count in sorted(accounts.items())
                    ],
                    "native_resource_extension": report,
                }
                output.execute(
                    "INSERT OR REPLACE INTO archive_info VALUES('report',?)",
                    (_json(metadata),),
                )
                output.execute(
                    "INSERT OR REPLACE INTO archive_info VALUES('messages',?)",
                    (str(total),),
                )
                output.execute(
                    "INSERT OR REPLACE INTO archive_info VALUES('native_resource_coverage_scope','explicit_verified_database_tables')"
                )
                output.commit()
                if [tuple(row) for row in output.execute("PRAGMA quick_check")] != [
                    ("ok",)
                ]:
                    raise ValueError(
                        "The extended resource candidate failed integrity checks."
                    )
            if any(
                _resident(path) != identity for path, identity in identities.items()
            ):
                raise ValueError("A native resource input changed during staging.")
            with candidate.open("rb") as stream:
                os.fsync(stream.fileno())
            report.update(
                messages=total,
                candidate_sha256=_sha256(candidate),
                candidate_identity=_resident(candidate),
            )
            return report
    except BaseException:
        if started:
            candidate.unlink(missing_ok=True)
        raise
