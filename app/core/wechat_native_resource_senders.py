"""Resolve resource authors from exact, independently proven native messages.

Code version: v1.1.2-codex.0

This read-only helper enriches one catalog row for a caller's reversible stage.
It does not publish a catalog, open media files, or infer an author from direction,
nearby messages, display names, or an unverified server-ID match.
"""

from __future__ import annotations

from copy import deepcopy
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3

from . import wechat_wcdb
from .wechat_archive import wechat_history_partition_fingerprint
from .wechat_export_merge import _clonefile
from .wechat_native_resource_revalidation import _relative, _resident, _validate_row
from .wechat_primary_source_receipts import primary_source_database_receipts
from .wechat_resource_index import _json, _readonly


VERSION = "v1.1.1-codex.0"
MAX_MESSAGE_BYTES = 2 * 1024**2
_PREFIX = re.compile(r"\A([A-Za-z0-9_.@-]{1,256}):\r?\n")
_FIELDS = ("MesLocalID", "MesSvrID", "CreateTime", "Type", "Des")
_RESOLUTION_FLAGS = {
    "native_sender_dictionary_unavailable",
    "native_sender_backend_unavailable",
    "native_sender_payload_invalid",
    "native_sender_prefix_unavailable",
}


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(4 * 1024**2):
            digest.update(chunk)
    return digest.hexdigest()


def _object(raw):
    if not isinstance(raw, str) or len(raw.encode()) > 16 * 1024**2:
        raise ValueError("A resource sender needs bounded object provenance.")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("A resource sender needs object provenance.")
    return value


def _signature(row):
    server = row["MesSvrID"]
    if server is not None and (
        isinstance(server, bool) or not isinstance(server, (str, int))
    ):
        raise ValueError("A native sender lost its recorded server ID.")
    server = str(server) if server is not None else None
    if server is not None and re.fullmatch(r"-?[0-9]{1,20}", server) is None:
        raise ValueError("A native sender has an invalid server ID.")
    if any(type(row[key]) is not int for key in _FIELDS if key != "MesSvrID"):
        raise ValueError("A native sender changed its ordinary metadata types.")
    return tuple(server if key == "MesSvrID" else row[key] for key in _FIELDS)


def _project(row, proof, resolution, sender=None):
    result = dict(row)
    proof = deepcopy(proof)
    prior_resolution = proof.get("native_resource_sender_resolution")
    retain_resolved = (
        resolution["status"] != "resolved"
        and isinstance(prior_resolution, dict)
        and prior_resolution.get("status") == "resolved"
        and prior_resolution.get("sender_id") == result.get("sender_id")
        and bool(result.get("sender_id"))
        and all(
            prior_resolution.get(key) == resolution.get(key)
            for key in (
                "source_snapshot",
                "database_relative_path",
                "database_sha256",
                "chat_table",
                "local_id",
                "server_id",
                "message_type",
                "compression_marker",
                "native_message_sha256",
            )
        )
    )
    if retain_resolved:
        proof["native_resource_sender_latest_attempt"] = resolution
    else:
        proof["native_resource_sender_resolution"] = resolution
        proof.pop("native_resource_sender_latest_attempt", None)
    flags = json.loads(result["quality_flags"])
    if not isinstance(flags, list) or any(not isinstance(flag, str) for flag in flags):
        raise ValueError("A resource sender needs its recorded quality flags.")
    flags = [flag for flag in flags if flag not in _RESOLUTION_FLAGS]
    if sender is not None:
        prior = result.get("sender_id")
        if prior and prior != sender:
            raise ValueError("The decoded native author conflicts with the catalog.")
        result["sender_id"] = sender
        if not prior or result["sender_label"] == "Unknown sender":
            result["sender_label"] = sender
        flags = [flag for flag in flags if flag != "sender_id_unavailable"]
        flags.append("native_resource_sender_verified")
        search = result["search_text"]
        if sender.casefold() not in search:
            result["search_text"] = search + "\n" + sender.casefold()
    elif resolution["status"] != "resolved":
        flag = {
            "missing_dictionary": "native_sender_dictionary_unavailable",
            "backend_unavailable": "native_sender_backend_unavailable",
            "invalid_payload": "native_sender_payload_invalid",
            "prefix_unavailable": "native_sender_prefix_unavailable",
        }[resolution["status"]]
        flags.append(flag)
    result["quality_flags"] = _json(list(dict.fromkeys(flags)))
    result["provenance"] = _json(proof)
    return result


def resolve_source_bound_resource_sender(
    row, proof, native_row, *, conversation, owner, dictionaries=None
):
    """Project a bounded native prefix after the caller verifies its exact source.

    The caller must independently validate the source account, snapshot receipt,
    full database SHA-256, and resident input identity before reading this row.
    This pure helper rechecks the group, account, table, local row, retained event
    metadata, and published signature before decoding. It performs no file I/O.
    """
    recorded = proof.get("recorded_native_metadata")
    if not isinstance(recorded, dict) or _signature(recorded) != _signature(native_row):
        raise ValueError(
            "The native sender row differs from its recorded event metadata."
        )
    reference, locator = proof.get("resource_reference"), proof.get("locator")
    if not isinstance(reference, dict) or not isinstance(locator, dict):
        raise ValueError("The native sender lacks its retained source binding.")
    relative = _relative(proof.get("source_relative_path"), owner)
    peer = conversation.get("peer_id")
    digest = proof.get("source_sha256")
    if (
        conversation.get("kind") != "group"
        or conversation.get("account_key") != "account:" + owner
        or not isinstance(peer, str)
        or not peer.endswith(("@chatroom", "@im.chatroom"))
        or reference.get("account_id") != owner
        or reference.get("peer_id") != peer
        or row["conversation_id"] != conversation.get("id")
        or proof.get("record_id") != row["id"]
        or row["ordinal"] != native_row["MesLocalID"]
        or locator.get("local_id") != native_row["MesLocalID"]
        or locator.get("table")
        != "Chat_" + hashlib.md5(peer.encode(), usedforsecurity=False).hexdigest()
        or locator.get("database_relative_path") != relative
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or locator.get("database_sha256") != digest
        or type(proof.get("source_snapshot")) is not int
        or proof["source_snapshot"] <= 0
    ):
        raise ValueError("The native sender changed its account, peer, or source row.")
    raw_server = int(native_row["MesSvrID"] or 0)
    native_server = str(raw_server) if 0 < raw_server <= 9223372036854775807 else None
    if (native_server, native_row["CreateTime"], native_row["Type"]) != (
        proof.get("server_id"),
        proof.get("unix_timestamp"),
        proof.get("message_type"),
    ):
        raise ValueError(
            "The native sender row differs from its published event signature."
        )
    raw, marker = native_row.get("Message"), native_row.get("compression_marker", 0)
    resolution = {
        "version": VERSION,
        "attempted_method": "exact_native_group_message_prefix",
        "basis": "source_bound_native_message_decode_attempt",
        "source_snapshot": proof["source_snapshot"],
        "database_relative_path": relative,
        "database_sha256": digest,
        "chat_table": locator["table"],
        "local_id": native_row["MesLocalID"],
        "server_id": native_row["MesSvrID"],
        "message_type": native_row["Type"],
        "compression_marker": marker,
        "media_payload_files_read": 0,
    }
    if isinstance(raw, (str, bytes)):
        encoded = raw.encode() if isinstance(raw, str) else raw
        resolution.update(
            native_message_sha256=hashlib.sha256(encoded).hexdigest(),
            native_message_bytes=len(encoded),
            native_storage_class="text" if isinstance(raw, str) else "blob",
        )
    try:
        if raw is None:
            raise wechat_wcdb.WCDBDecodeError(
                "Native message is absent or exceeds the sender byte limit"
            )
        decoded = wechat_wcdb.decode_wcdb_text(
            raw, marker, dictionaries=dictionaries, limit=MAX_MESSAGE_BYTES
        )
        if isinstance(raw, bytes) and marker in (2, 3):
            dictionary_id = wechat_wcdb.get_wcdb_dictionary_id(raw)
            dictionary = dictionaries[dictionary_id]
            content = (
                bytes(dictionary)
                if isinstance(dictionary, (bytes, bytearray, memoryview))
                else dictionary.dict_content
            )
            resolution.update(
                dictionary_id=dictionary_id,
                dictionary_sha256=hashlib.sha256(content).hexdigest(),
            )
    except wechat_wcdb.MissingDictionaryError as error:
        resolution.update(
            status="missing_dictionary",
            dictionary_id=error.dictionary_id,
            basis="native_compression_frame_dictionary_requirement",
        )
    except wechat_wcdb.WCDBBackendUnavailableError:
        resolution.update(status="backend_unavailable")
    except (wechat_wcdb.WCDBDecodeError, TypeError):
        resolution.update(status="invalid_payload")
    else:
        prefix = _PREFIX.match(decoded)
        sender = prefix[1] if prefix else None
        if sender is None or sender.endswith(("@chatroom", "@im.chatroom")):
            resolution.update(status="prefix_unavailable")
        else:
            resolution.update(
                status="resolved",
                sender_id=sender,
                basis="exact_native_group_message_prefix",
            )
    return (_project(row, proof, resolution, resolution.get("sender_id")), resolution)


def recover_native_resource_sender(
    history_path,
    catalog_path,
    message_id,
    database_path,
    *,
    dictionaries=None,
):
    """Read one source-bound group author and return a staged row plus its proof.

    Independent primary receipts must prove the exact database and source account.
    The materialized database must match that receipt byte-for-byte, and its local
    row must match all retained ordinary metadata. Only a fully decoded native
    sender prefix can resolve the author. Missing dictionaries remain explicit.
    The caller owns cloning, publication, rollback, and source-preservation policy.
    """
    history_path, catalog_path, database_path = (
        Path(value).absolute() for value in (history_path, catalog_path, database_path)
    )
    if (
        not isinstance(message_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", message_id) is None
    ):
        raise ValueError("Select one exact native resource message ID.")
    paths = (history_path, catalog_path, database_path)
    identities = {path: _resident(path) for path in paths}
    with _readonly(history_path) as history, _readonly(catalog_path) as catalog:
        info = dict(catalog.execute("SELECT key,value FROM archive_info"))
        if (
            info.get("schema_version") != "1"
            or json.loads(info.get("history_file_identity", "null"))
            != identities[history_path]
            or info.get("history_partitions_sha256")
            != wechat_history_partition_fingerprint(history)
        ):
            raise ValueError(
                "The resource sender catalog is not pinned to the current primary."
            )
        row = catalog.execute(
            "SELECT * FROM messages WHERE id=?", (message_id,)
        ).fetchone()
        if row is None:
            raise ValueError("The selected native resource is not published.")
        row = dict(row)
        conversation = catalog.execute(
            "SELECT * FROM conversations WHERE id=?", (row["conversation_id"],)
        ).fetchone()
        if conversation is None or conversation["kind"] != "group":
            raise ValueError(
                "Native prefix recovery requires a verified group conversation."
            )
        conversation = dict(conversation)
        proof = _object(row["provenance"])
        receipt = primary_source_database_receipts(
            history, conversation, proof.get("source_snapshot")
        )
        key, _, _, _ = _validate_row(
            row,
            conversation,
            receipt["owner"],
            receipt["receipts"],
            receipt["snapshots"],
        )
        _, relative, expected_digest, table, local = key
        if _sha256(database_path) != expected_digest:
            raise ValueError(
                "The native sender database differs from its independent receipt."
            )
        if proof.get("locator", {}).get("file_id") != receipt["file_ids"][relative]:
            raise ValueError("The native sender lost its exact backup file locator.")
        with _readonly(database_path) as native:
            columns = {
                item[1] for item in native.execute(f'PRAGMA table_info("{table}")')
            }
            if not {*_FIELDS, "Message"} <= columns:
                raise ValueError(
                    "The native sender table lacks its ordinary message columns."
                )
            marker = '"WCDB_CT_Message"' if "WCDB_CT_Message" in columns else "0"
            value = native.execute(
                f"SELECT MesLocalID,CAST(MesSvrID AS TEXT) AS MesSvrID,CreateTime,Type,Des,"
                f"CASE WHEN length(CAST(Message AS BLOB))<=? THEN Message END AS Message,"
                f'{marker} AS compression_marker FROM "{table}" WHERE MesLocalID=?',
                (MAX_MESSAGE_BYTES, local),
            ).fetchone()
            if value is None:
                raise ValueError(
                    "The native sender row is absent from its exact source."
                )
            value = dict(value)
            updated, resolution = resolve_source_bound_resource_sender(
                row,
                proof,
                dict(value),
                conversation=conversation,
                owner=receipt["owner"],
                dictionaries=dictionaries,
            )
    if any(_resident(path) != identity for path, identity in identities.items()):
        raise ValueError("A native sender input changed during verification.")
    return {
        "version": VERSION,
        "message": updated,
        "resolution": resolution,
        "published": False,
    }


def stage_native_resource_senders(
    history_path,
    catalog_path,
    candidate_path,
    database_path,
    *,
    message_ids,
    dictionaries=None,
    progress=None,
):
    """Stage explicit sender resolutions in an APFS clone of the resource catalog.

    The history is never cloned or modified. Every membership table and unrelated
    catalog record remains in the source's copy-on-write clone. This entry point
    performs no publication; callers retain their existing maintenance boundary,
    reversible rollback, and explicit publication checks.
    """
    history_path, catalog_path, candidate_path, database_path = (
        Path(value).absolute()
        for value in (history_path, catalog_path, candidate_path, database_path)
    )
    selected = tuple(message_ids)
    if not selected or len(selected) > 100 or len(set(selected)) != len(selected):
        raise ValueError("Select between one and 100 distinct native resource IDs.")
    if (
        candidate_path.exists()
        or candidate_path.is_symlink()
        or not candidate_path.parent.is_dir()
        or candidate_path.parent.is_symlink()
        or candidate_path in {history_path, catalog_path, database_path}
    ):
        raise ValueError(
            "Select a new sender candidate in an existing regular directory."
        )
    if shutil.disk_usage(candidate_path.parent).free < 16 * 1024**2:
        raise ValueError("Insufficient space for a copy-on-write sender update.")
    paths = (history_path, catalog_path, database_path)
    identities = {path: _resident(path) for path in paths}
    recovered = []
    for identifier in selected:
        result = recover_native_resource_sender(
            history_path,
            catalog_path,
            identifier,
            database_path,
            dictionaries=dictionaries,
        )
        recovered.append(result)
        if progress:
            progress(
                {
                    "phase": "sender",
                    "checked": len(recovered),
                    "resolution": result["resolution"],
                }
            )
    if any(_resident(path) != identity for path, identity in identities.items()):
        raise ValueError("A sender source changed before candidate staging.")
    base_digest = _sha256(catalog_path)
    statuses = dict(Counter(result["resolution"]["status"] for result in recovered))
    report = {
        "version": VERSION,
        "candidate": str(candidate_path),
        "base_catalog": str(catalog_path),
        "base_sha256": base_digest,
        "history_identity": identities[history_path],
        "catalog_identity": identities[catalog_path],
        "native_database_identity": identities[database_path],
        "messages": len(recovered),
        "statuses": statuses,
        "message_payload_columns_read": len(recovered),
        "media_payload_files_read": 0,
        "history_cloned": False,
        "published": False,
        "resolutions": [result["resolution"] for result in recovered],
    }
    started = False
    try:
        _clonefile(catalog_path, candidate_path)
        started = True
        candidate_path.chmod(0o600)
        if _sha256(candidate_path) != base_digest:
            raise ValueError(
                "The sender candidate is not an exact resource catalog clone."
            )
        with sqlite3.connect(candidate_path) as output:
            output.execute("PRAGMA journal_mode=DELETE")
            output.execute("PRAGMA synchronous=FULL")
            for result in recovered:
                row = result["message"]
                output.execute(
                    "UPDATE messages SET sender_id=?,sender_label=?,provenance=?,quality_flags=?,search_text=? WHERE id=?",
                    tuple(
                        row[key]
                        for key in (
                            "sender_id",
                            "sender_label",
                            "provenance",
                            "quality_flags",
                            "search_text",
                            "id",
                        )
                    ),
                )
                if output.execute("SELECT changes()").fetchone()[0] != 1:
                    raise ValueError(
                        "The sender candidate lost a selected resource record."
                    )
            output.execute(
                "INSERT OR REPLACE INTO archive_info VALUES('native_resource_sender_recovery',?)",
                (
                    _json(
                        {
                            key: report[key]
                            for key in (
                                "version",
                                "messages",
                                "statuses",
                                "message_payload_columns_read",
                                "media_payload_files_read",
                            )
                        }
                    ),
                ),
            )
            output.commit()
            if output.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("The sender candidate failed its integrity check.")
        if any(_resident(path) != identity for path, identity in identities.items()):
            raise ValueError("A sender input changed before candidate completion.")
        with candidate_path.open("r+b" if os.name == "nt" else "rb") as stream:
            os.fsync(stream.fileno())
        report.update(
            candidate_identity=_resident(candidate_path),
            candidate_sha256=_sha256(candidate_path),
            candidate_bytes=candidate_path.stat().st_size,
        )
        return report
    except BaseException:
        if started:
            candidate_path.unlink(missing_ok=True)
        raise
