"""Stage indexed, source-bound Mac recovery without rewriting the original archive.

Code version: v1.2.1-codex.0
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing, contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from threading import RLock

from . import wechat_macos_recovery as legacy
from .platform_lock import lock_file, unlock_file
from .wechat_archive import _identifier, wechat_archive_path
from .wechat_reconcile import _TEXT_METADATA_FIELDS, native_text_metadata
from .wechat_resource_index import _KINDS, _LABELS
from .wechat_text import native_components_text


VERSION = "v1.2.1-codex.0"
MAX_PACKET_BYTES = 128 * 1024**2
MAX_PACKET_MESSAGES = 100_000
MAX_SCOPE_MESSAGES = 5_000_000
_LOCK = RLock()
_PUBLICATION_LOCK_NAME = ".macos_recovery.publication.lock"
_VALIDATED = {}
_SCHEMA_CONTRACT = {}
_SHADOW_VALIDATED = {}
_CATALOG_HASHES = {}
_ACCOUNT_BINDINGS = {}
_ACCOUNT_BINDING_SHAPES = {}
_NATIVE_FIELDS = {
    "text",
    "text_extraction_kind",
    "original_message_type",
    "appmsg_subtype",
    "flags",
    *_TEXT_METADATA_FIELDS,
    "invitation_card_fields",
    "system_message_subtype",
    "system_source_ids",
}
_SIGNATURE_FIELDS = (
    *_TEXT_METADATA_FIELDS,
    "invitation_card_fields",
    "system_message_subtype",
    "system_source_ids",
)
_SOURCE_INPUTS = (
    "history.sqlite3",
    "macos_recovery.json",
    "macos_recovery.sqlite3",
    "resource_references.sqlite3",
    "resource_revalidated.sqlite3",
    "media_placeholders.json",
    "media_events.sqlite3",
)
_SCHEMA_V1 = """
CREATE TABLE messages (
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
 sender_id TEXT, sender_label TEXT NOT NULL, direction TEXT NOT NULL,
 text TEXT NOT NULL, time_sort REAL NOT NULL, timestamp_value TEXT NOT NULL,
 timestamp_label TEXT NOT NULL, time_basis TEXT NOT NULL,
 provenance TEXT NOT NULL, quality_flags TEXT NOT NULL, search_text TEXT NOT NULL
);
CREATE INDEX message_conversation_time ON messages(conversation_id,time_sort,ordinal,id);
CREATE TABLE conversations (
 id TEXT PRIMARY KEY, partition_id TEXT NOT NULL, account_key TEXT NOT NULL,
 peer_id TEXT, title TEXT NOT NULL, kind TEXT NOT NULL,
 message_count INTEGER NOT NULL, first_sort REAL NOT NULL, last_sort REAL NOT NULL,
 first_time TEXT NOT NULL, last_time TEXT NOT NULL, latest_text TEXT NOT NULL
);
CREATE TABLE sources (
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL, peer_id TEXT NOT NULL,
 source_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, proof TEXT NOT NULL
);
CREATE TABLE conversation_proofs (
 conversation_id TEXT NOT NULL, source_id TEXT NOT NULL, proof TEXT NOT NULL,
 PRIMARY KEY(conversation_id,source_id)
) WITHOUT ROWID;
CREATE TABLE event_keys (
 message_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, peer_id TEXT NOT NULL,
 server_id TEXT NOT NULL, signature TEXT NOT NULL,
 UNIQUE(account_id,peer_id,server_id)
);
CREATE INDEX event_server_scope ON event_keys(account_id,peer_id,server_id);
CREATE TABLE occurrences (
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
 local_id INTEGER NOT NULL, server_id TEXT NOT NULL, content_sha256 TEXT NOT NULL,
 canonical_id TEXT NOT NULL, canonical_store TEXT NOT NULL, signature TEXT NOT NULL,
 proof TEXT NOT NULL,
 UNIQUE(source_id,conversation_id,local_id)
);
CREATE INDEX occurrence_conversation ON occurrences(conversation_id,source_id);
CREATE TABLE gaps (
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, source_id TEXT NOT NULL,
 local_id INTEGER NOT NULL, server_id TEXT NOT NULL, reason TEXT NOT NULL,
 content_sha256 TEXT NOT NULL
);
CREATE TABLE archive_info (key TEXT PRIMARY KEY,value TEXT NOT NULL);
"""
_SHADOW_SCHEMA = """
CREATE TABLE shadow_replacements (
 original_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
 source_file_name TEXT NOT NULL, source_file_sha256 TEXT NOT NULL,
 original_text_sha256 TEXT NOT NULL, original_provenance_sha256 TEXT NOT NULL,
 original_row_sha256 TEXT NOT NULL, proof TEXT NOT NULL
);
CREATE INDEX shadow_conversation ON shadow_replacements(conversation_id,original_id);
"""
_SCHEMA = _SCHEMA_V1 + _SHADOW_SCHEMA


def _json(value):
    return legacy._json(value)


def _sha(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(4 * 1024**2):
            digest.update(block)
    return digest.hexdigest()


def mac_recovery_bank_path(local_store_root):
    return wechat_archive_path(local_store_root).with_name("macos_recovery.sqlite3")


def _identity(path, *, optional=False):
    try:
        value = Path(path).lstat()
    except FileNotFoundError:
        if optional:
            return None
        raise
    if (
        not stat.S_ISREG(value.st_mode)
        or stat.S_ISLNK(value.st_mode)
        or getattr(value, "st_flags", 0) & 0x40000000
        or any(
            Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")
        )
    ):
        raise ValueError(
            "Mac recovery bank inputs must be stable resident regular files."
        )
    return {
        key: getattr(value, "st_" + key)
        for key in ("dev", "ino", "size", "mtime_ns", "ctime_ns")
    }


def _inputs(history):
    result = {}
    for name in _SOURCE_INPUTS:
        path = history.with_name(name)
        try:
            result[name] = _identity(path, optional=name != "history.sqlite3")
        except ValueError:
            if name in {
                "history.sqlite3",
                "macos_recovery.json",
                "macos_recovery.sqlite3",
            }:
                raise
            value = path.lstat()
            result[name] = {
                "unavailable": True,
                "size": value.st_size,
                "mtime_ns": value.st_mtime_ns,
            }
    return result


def _name_proof(value, expected_user=None, expected_row=None):
    if not isinstance(value, dict) or set(value) != {"row", "row_sha256"}:
        raise ValueError("Mac recovery requires an exact authenticated Name2Id row.")
    row = value["row"]
    if (
        not isinstance(row, dict)
        or set(row) != {"rowid", "user_name", "is_session"}
        or type(row["rowid"]) is not int
        or row["rowid"] < 1
        or not isinstance(row["user_name"], str)
        or not row["user_name"]
        or type(row["is_session"]) is not int
        or value["row_sha256"] != _sha(row)
        or expected_user is not None
        and row["user_name"] != expected_user
        or expected_row is not None
        and row["rowid"] != expected_row
    ):
        raise ValueError(
            "A Mac sender or peer differs from its authenticated Name2Id row."
        )
    return row


def _account_binding_shape(packet):
    """Validate an explicit whole-source owner proof without local-anchor invention."""
    binding = packet.get("account_binding")
    fields = {
        "schema_version",
        "account_id",
        "account_directory",
        "owner_proof",
        "source",
        "anchors",
    }
    source_fields = {
        "source_id",
        "snapshot_id",
        "main_sha256",
        "wal_sha256",
        "database_relative_path",
    }
    source = packet.get("source")
    if (
        not isinstance(binding, dict)
        or set(binding) != fields
        or binding.get("schema_version") != 1
        or type(binding["schema_version"]) is not int
        or binding.get("account_id") != packet["account_id"]
        or not isinstance(source, dict)
        or not isinstance(binding.get("source"), dict)
        or set(binding["source"]) != source_fields
        or any(binding["source"][key] != source.get(key) for key in source_fields)
        or binding.get("account_directory") != source.get("account_directory")
    ):
        raise ValueError(
            "The global Mac account proof crosses its exact captured source."
        )
    directory = binding["account_directory"]
    owner = binding["account_id"]
    if (
        not isinstance(directory, str)
        or len(directory) > 256
        or re.fullmatch(r"[A-Za-z0-9_.-]+", directory) is None
        or directory != owner
        and not directory.startswith(owner + "_")
    ):
        raise ValueError(
            "The captured Mac account directory conflicts with its native owner."
        )
    _name_proof(binding["owner_proof"], owner, 2)
    digest = _sha(binding)
    with _LOCK:
        if digest in _ACCOUNT_BINDING_SHAPES:
            return digest
    anchors = binding["anchors"]
    if not isinstance(anchors, list) or not 2 <= len(anchors) <= 32:
        raise ValueError(
            "A global Mac owner proof needs at least two independent native anchors."
        )
    anchor_fields = {
        "archive_message_id",
        "peer_id",
        "peer_proof",
        "server_id",
        "message_type",
        "create_time",
        "direction",
        "sender_id",
        "text_sha256",
        "table",
        "local_id",
        "content_sha256",
        "real_sender_id",
        "sender_proof",
    }
    peers, identifiers, sent = set(), set(), 0
    for anchor in anchors:
        if not isinstance(anchor, dict) or set(anchor) != anchor_fields:
            raise ValueError(
                "Global Mac anchors require exact archive and native row coordinates."
            )
        peer = legacy._string(anchor["peer_id"])
        _name_proof(anchor["peer_proof"], peer)
        _name_proof(
            anchor["sender_proof"], anchor["sender_id"], anchor["real_sender_id"]
        )
        legacy._server(anchor["server_id"])
        for field in ("archive_message_id", "text_sha256", "content_sha256"):
            legacy._digest(anchor[field])
        if (
            anchor["message_type"] != 1
            or type(anchor["message_type"]) is not int
            or type(anchor["create_time"]) is not int
            or not 1 <= anchor["create_time"] <= 253402271999
            or type(anchor["local_id"]) is not int
            or not 1 <= anchor["local_id"] <= 2**63 - 1
            or type(anchor["real_sender_id"]) is not int
            or anchor["real_sender_id"] < 1
            or anchor["table"]
            != "Msg_" + hashlib.md5(peer.encode(), usedforsecurity=False).hexdigest()
            or anchor["direction"] not in {"sent", "received"}
            or anchor["direction"]
            != ("sent" if anchor["sender_id"] == owner else "received")
            or anchor["direction"] == "sent"
            and (
                anchor["real_sender_id"] != 2
                or anchor["sender_proof"] != binding["owner_proof"]
            )
            or not peer.endswith(("@chatroom", "@im.chatroom"))
            and anchor["sender_id"] not in {owner, peer}
            or anchor["sender_id"].endswith(("@chatroom", "@im.chatroom"))
            or anchor["archive_message_id"] in identifiers
        ):
            raise ValueError(
                "A global Mac anchor conflicts with its native sender, type or clock."
            )
        peers.add(peer)
        identifiers.add(anchor["archive_message_id"])
        sent += anchor["direction"] == "sent"
    if len(peers) < 2 or sent < 1:
        raise ValueError(
            "Global Mac owner anchors must span distinct peers and include a sent-owner message."
        )
    with _LOCK:
        if len(_ACCOUNT_BINDING_SHAPES) >= 32:
            _ACCOUNT_BINDING_SHAPES.pop(next(iter(_ACCOUNT_BINDING_SHAPES)))
        _ACCOUNT_BINDING_SHAPES[digest] = True
    return digest


def _validate_account_binding(connection, packet):
    digest = _account_binding_shape(packet)
    history = next(
        row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main"
    )
    identity = _identity(Path(history))
    # An immutable SQLite connection can retain old pages after an external write.
    # A new filesystem identity must never be paired with those cached old pages.
    connection.execute(
        "CREATE TEMP TABLE IF NOT EXISTS macos_account_binding_context("
        "path TEXT PRIMARY KEY,identity TEXT NOT NULL) WITHOUT ROWID"
    )
    pinned = connection.execute(
        "SELECT identity FROM temp.macos_account_binding_context WHERE path=?",
        (history,),
    ).fetchone()
    if pinned is None:
        connection.execute(
            "INSERT INTO temp.macos_account_binding_context VALUES(?,?)",
            (history, _json(identity)),
        )
    elif pinned[0] != _json(identity):
        raise ValueError(
            "The archive changed behind an immutable account-binding connection."
        )
    key = (str(Path(history).resolve()), _json(identity), digest)
    with _LOCK:
        if key in _ACCOUNT_BINDINGS:
            return digest
    binding = packet["account_binding"]
    owner = binding["account_id"]
    native_partition = (
        "ios-" + hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest()
    )
    for anchor in binding["anchors"]:
        row = connection.execute(
            "SELECT m.*,c.peer_id,c.account_key,p.account_id,p.account_key AS partition_account_key,"
            "p.id AS source_partition,p.identity_status FROM main.messages m "
            "JOIN main.conversations c ON c.id=m.conversation_id JOIN main.partitions p ON p.id=c.partition_id "
            "WHERE m.id=?",
            (anchor["archive_message_id"],),
        ).fetchone()
        if row is None:
            raise ValueError(
                "A global Mac account anchor is absent from the original archive."
            )
        proof = json.loads(row["provenance"])
        try:
            stamp = datetime.fromisoformat(row["timestamp_value"])
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "A global account anchor needs its native archive clock."
            ) from exc
        if (
            row["peer_id"] != anchor["peer_id"]
            or row["account_id"] != owner
            or row["source_partition"] != native_partition
            or row["identity_status"] != "Verified archive ID"
            or row["account_key"] != "account:" + owner
            or row["partition_account_key"] != row["account_key"]
            or row["sender_id"] != anchor["sender_id"]
            or row["direction"] != anchor["direction"]
            or proof.get("message_type") != 1
            or str(proof.get("server_id")) != anchor["server_id"]
            or type(proof.get("unix_timestamp")) is not int
            or proof["unix_timestamp"] != anchor["create_time"]
            or stamp.tzinfo is None
            or stamp.timestamp() != anchor["create_time"]
            or hashlib.sha256(row["text"].encode()).hexdigest() != anchor["text_sha256"]
        ):
            raise ValueError(
                "A global Mac owner anchor differs from its exact native archive identity."
            )
    if _identity(Path(history)) != identity:
        raise ValueError(
            "The original archive changed during global account binding validation."
        )
    with _LOCK:
        if len(_ACCOUNT_BINDINGS) >= 32:
            _ACCOUNT_BINDINGS.pop(next(iter(_ACCOUNT_BINDINGS)))
        _ACCOUNT_BINDINGS[key] = True
    return digest


def _safe_metadata(value, depth=0):
    if depth > 40:
        raise ValueError("Mac parser metadata exceeds its nesting budget.")
    if isinstance(value, dict):
        if len(value) > 128:
            raise ValueError("Mac parser metadata has too many fields.")
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or len(key) > 256
                or key.lower().replace("_", "")
                in {
                    "aeskey",
                    "rawxml",
                    "messagecontent",
                    "encryptedpayload",
                    "mediakey",
                    "decryptionkey",
                    "encryptionkey",
                    "key",
                    "raw",
                    "payload",
                }
            ):
                raise ValueError(
                    "Mac recovery cannot retain opaque payloads or media keys."
                )
            _safe_metadata(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > 1000:
            raise ValueError("Mac parser metadata has too many components.")
        for item in value:
            _safe_metadata(item, depth + 1)
    elif isinstance(value, str):
        if len(value.encode()) > 2 * 1024**2:
            raise ValueError("Mac parser metadata exceeds its cell budget.")
    elif value is not None and type(value) not in {bool, int}:
        raise ValueError("Mac parser metadata must contain only bounded JSON values.")


def _validate_packet(packet, accounts):
    required = legacy._PACKET_FIELDS | {"conversation_kind", "peer_proof"}
    if (
        not isinstance(packet, dict)
        or set(packet) - (required | {"account_binding"})
        or required - set(packet)
        or packet.get("schema_version") != 2
        or type(packet["schema_version"]) is not int
        or packet.get("account_id") not in accounts
        or packet.get("conversation_kind") not in {"direct", "group"}
        or not isinstance(packet.get("messages"), list)
        or not 1 <= len(packet["messages"]) <= MAX_PACKET_MESSAGES
    ):
        raise ValueError("Unsupported or unauthorized Mac recovery bank packet.")
    owner, peer = packet["account_id"], packet["peer_id"]
    legacy._string(owner)
    legacy._string(peer)
    legacy._string(packet["receipt_id"])
    if (
        not isinstance(packet["session_id"], str)
        or re.fullmatch(r"wechat:[0-9a-f]{64}", packet["session_id"]) is None
    ):
        raise ValueError("Mac recovery bank needs an existing canonical conversation.")
    _name_proof(packet["peer_proof"], peer)
    source = packet["source"]
    if not isinstance(source, dict) or set(source) - (
        legacy._SOURCE_FIELDS | {"account_directory"}
    ):
        raise ValueError("Unsupported Mac account source receipt fields.")
    legacy._validate_source(
        {key: value for key, value in source.items() if key != "account_directory"}
    )
    bound = "account_binding" in packet
    legacy._validate_anchors(packet["anchors"], minimum=0 if bound else 2)
    if bound:
        _account_binding_shape(packet)
    elif "account_directory" in source:
        raise ValueError(
            "A captured account directory needs an explicit source-level account proof."
        )
    if (
        source["table"]
        != "Msg_" + hashlib.md5(peer.encode(), usedforsecurity=False).hexdigest()
    ):
        raise ValueError(
            "A Mac table must reproduce its authenticated peer's UTF-8 MD5."
        )
    if packet["conversation_kind"] != (
        "group" if peer.endswith(("@chatroom", "@im.chatroom")) else "direct"
    ):
        raise ValueError(
            "The Mac conversation kind conflicts with its native peer namespace."
        )
    seen = {}
    for message in packet["messages"]:
        required_message = legacy._MESSAGE_FIELDS - {
            "sender_label",
            "native_text",
            "resource",
        }
        allowed = legacy._MESSAGE_FIELDS | {"real_sender_id", "sender_proof"}
        if (
            not isinstance(message, dict)
            or set(message) - allowed
            or (required_message | {"real_sender_id"}) - set(message)
        ):
            raise ValueError("Unsupported Mac recovery bank message fields.")
        local = message["local_id"]
        if (
            type(local) is not int
            or not 1 <= local <= 2**63 - 1
            or type(message["local_type"]) is not int
            or not 0 <= message["local_type"] <= 2**64 - 1
            or type(message["create_time"]) is not int
            or not 1 <= message["create_time"] <= 253402271999
            or type(message["real_sender_id"]) is not int
            or message["real_sender_id"] < 0
            or not isinstance(message["text"], str)
            or len(message["text"].encode()) > 2 * 1024**2
        ):
            raise ValueError(
                "Mac native row coordinates and clocks must retain exact bounded types."
            )
        legacy._server(message["server_id"])
        legacy._digest(message["content_sha256"])
        if local in seen and seen[local] != message:
            raise ValueError("One Mac source row has conflicting message metadata.")
        seen[local] = message
        typ = message["local_type"] & 0xFFFFFFFF
        system = typ in {10000, 10002}
        proof = message.get("sender_proof")
        if system:
            if (
                message["sender_id"] != ""
                or message["direction"] != "unknown"
                or message.get("sender_label") != "System Message"
            ):
                raise ValueError(
                    "Mac system notices must retain a neutral author and unknown direction."
                )
            if proof is not None:
                _name_proof(proof, expected_row=message["real_sender_id"])
        else:
            _name_proof(proof, message["sender_id"], message["real_sender_id"])
            if message["direction"] != (
                "sent" if message["sender_id"] == owner else "received"
            ):
                raise ValueError(
                    "Mac direction conflicts with its authenticated native sender."
                )
            if packet["conversation_kind"] == "direct" and message["sender_id"] not in {
                owner,
                peer,
            }:
                raise ValueError("A direct Mac message has an unrelated native sender.")
            if packet["conversation_kind"] == "group" and message["sender_id"].endswith(
                ("@chatroom", "@im.chatroom")
            ):
                raise ValueError("A group room cannot be its received message author.")
        native, resource = message.get("native_text"), message.get("resource")
        if native is not None:
            if (
                resource is not None
                or not isinstance(native, dict)
                or set(native) - _NATIVE_FIELDS
                or native.get("original_message_type") != typ
                or native.get("text") != message["text"]
                or not isinstance(native.get("flags"), list)
                or any(not isinstance(flag, str) for flag in native["flags"])
                or typ not in {1, 49, 10000, 10002}
            ):
                raise ValueError(
                    "Mac text recovery requires the exact supported parser projection."
                )
            _safe_metadata(native)
            native_text_metadata(native, {})
            kind = native.get("text_extraction_kind")
            if (
                typ == 1
                and kind != "plain_text"
                or system
                and kind != "system_text"
                or typ == 49
                and kind
                not in {"app_text", "text_appmsg", "quoted_reply", "forwarded_text"}
                or "text_components" in native
                and native_components_text(native["text_components"]) != message["text"]
                or typ == 49
                and message["local_type"] >> 32
                and native.get("appmsg_subtype") != message["local_type"] >> 32
            ):
                raise ValueError(
                    "Mac parser roles or components conflict with their native type and text."
                )
        else:
            allowed_resource = {
                "kind",
                "label",
                "native_time",
                "app_subtype",
                "safe_title",
            }
            if (
                system
                or not isinstance(resource, dict)
                or set(resource) - allowed_resource
            ):
                raise ValueError("A Mac resource needs body-free typed metadata.")
            kind = resource.get("kind")
            expected = _KINDS.get(typ, "unknown")
            if typ == 49 and "app_subtype" in resource and kind != "app":
                expected = {2: "image", 3: "audio", 4: "video", 6: "file"}.get(
                    message["local_type"] >> 32, "app"
                )
            if (
                kind != expected
                or resource.get("native_time") != message["create_time"]
                or resource.get("label") != _LABELS[kind]
                or message["text"] != resource["label"]
                or "app_subtype" in resource
                and resource["app_subtype"] != str(message["local_type"] >> 32)
                or "safe_title" in resource
                and (
                    not isinstance(resource["safe_title"], str)
                    or len(resource["safe_title"]) > 1024
                )
            ):
                raise ValueError(
                    "Mac resource kind, label or native subtype conflicts with its receipt."
                )
    if len(_json(packet).encode()) > MAX_PACKET_BYTES:
        raise ValueError("Mac recovery bank packet exceeds its bounded staging budget.")


def _binding(connection, packet):
    _name_proof(packet["peer_proof"], packet["peer_id"])
    source = packet["source"]
    legacy._validate_source(
        {key: value for key, value in source.items() if key != "account_directory"}
    )
    if (
        source["table"]
        != "Msg_"
        + hashlib.md5(packet["peer_id"].encode(), usedforsecurity=False).hexdigest()
    ):
        raise ValueError("A bound Mac scope differs from its authenticated peer table.")
    row = connection.execute(
        "SELECT c.*,p.account_id,p.account_key AS partition_account_key FROM main.conversations c "
        "JOIN main.partitions p ON p.id=c.partition_id WHERE c.id=?",
        (packet["session_id"],),
    ).fetchone()
    if row is None:
        raise ValueError("Mac recovery bank cannot create an unproved conversation.")
    value = dict(row)
    if (
        value["peer_id"] != packet["peer_id"]
        or value["kind"] != packet["conversation_kind"]
        or value["account_id"] != packet["account_id"]
        or value["account_key"] != "account:" + packet["account_id"]
        or value["partition_account_key"] != value["account_key"]
    ):
        raise ValueError(
            "The Mac recovery target crosses its exact existing account/peer scope."
        )
    bound = "account_binding" in packet
    legacy._validate_anchors(packet["anchors"], minimum=0 if bound else 2)
    if bound:
        _validate_account_binding(connection, packet)
    # The legacy binder's anchor checks are shared, while group kind is checked above.
    for anchor in packet["anchors"]:
        original = connection.execute(
            "SELECT conversation_id,text,provenance FROM main.messages WHERE id=?",
            (anchor["archive_message_id"],),
        ).fetchone()
        if (
            original is None
            or original[0] != packet["session_id"]
            or hashlib.sha256(original[1].encode()).hexdigest() != anchor["text_sha256"]
            or str(json.loads(original[2]).get("server_id")) != anchor["server_id"]
        ):
            raise ValueError(
                "A Mac recovery bank anchor differs from its original archive row."
            )
    return {key: value[key] for key in legacy._CONVERSATION_COLUMNS}


def _event_signature(row):
    proof = json.loads(row["provenance"])
    epoch = proof.get("unix_timestamp")
    if type(epoch) is not int:
        try:
            epoch = int(datetime.fromisoformat(row["timestamp_value"]).timestamp())
        except (ValueError, TypeError, OverflowError):
            epoch = row["timestamp_value"]
    return _sha(
        {
            "message_type": proof.get("message_type"),
            "epoch": epoch,
            "direction": row["direction"],
            "sender_id": row["sender_id"],
            "text_sha256": hashlib.sha256(row["text"].encode()).hexdigest(),
            "native_text": {
                key: proof[key] for key in _SIGNATURE_FIELDS if key in proof
            },
        }
    )


def _row(packet, message):
    projected = list(legacy._project(packet, message))
    proof = json.loads(projected[11])
    proof["macos_recovery"].update(
        conversation_kind=packet["conversation_kind"],
        peer_proof=packet["peer_proof"],
        real_sender_id=message["real_sender_id"],
        sender_proof=message.get("sender_proof"),
    )
    if "account_binding" in packet:
        proof["macos_recovery"].update(
            account_binding_sha256=_sha(packet["account_binding"]),
            binding_mode="authenticated_source_account_binding_v1",
        )
    projected[11] = _json(proof)
    return projected


def _rows_digest(connection):
    digest = hashlib.sha256()
    tables = [
        "sources",
        "conversation_proofs",
        "event_keys",
        "occurrences",
        "gaps",
        "messages",
        "conversations",
    ]
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name='shadow_replacements' AND type='table'"
    ).fetchone():
        tables.append("shadow_replacements")
    for table in tables:
        digest.update(table.encode())
        for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1,2"):
            digest.update(_json(tuple(row)).encode())
            digest.update(b"\n")
    return digest.hexdigest()


def _empty_metadata(value):
    return value is None or value == "" or value == {} or value == []


def _stub_source(connection, row, catalog_hashes):
    """Identify only a visible, inert catalog stub; primary text is never shadowed."""
    proof = json.loads(row["provenance"])
    reference = proof.get("resource_reference")
    if (
        row["text"] != "[App message]"
        or proof.get("message_type") != 49
        or proof.get("text_extraction_kind") != "resource_placeholder"
        or not isinstance(reference, dict)
        or reference.get("kind") != "app"
        or connection.execute(
            "SELECT 1 FROM main.messages WHERE id=?", (row["id"],)
        ).fetchone()
    ):
        return None
    for container in (proof, proof.get("locator", {}), reference):
        if not isinstance(container, dict) or any(
            not _empty_metadata(container.get(key))
            for key in (
                *_TEXT_METADATA_FIELDS,
                "native_metadata",
                "safe_title",
                "app_subtype",
            )
        ):
            return None
    database = next(
        (
            item[2]
            for item in connection.execute("PRAGMA database_list")
            if item[1] == "resource_index"
        ),
        None,
    )
    if database is None or Path(database).name not in {
        "resource_references.sqlite3",
        "resource_revalidated.sqlite3",
    }:
        return None
    original = connection.execute(
        "SELECT * FROM resource_index.messages WHERE id=?", (row["id"],)
    ).fetchone()
    if original is None or dict(original) != dict(row):
        return None
    path = Path(database)
    if str(path) not in catalog_hashes:
        before = _identity(path)
        digest = _file_sha(path)
        if _identity(path) != before:
            raise ValueError(
                "The original resource catalog changed while its receipt was sealed."
            )
        catalog_hashes[str(path)] = digest
    return path.name, catalog_hashes[str(path)]


def _upgrade_stub(stub, packet, message):
    row = json.loads(stub["original_row"])
    proof = json.loads(row["provenance"])
    reference = proof["resource_reference"]
    native = message.get("native_text")
    if (
        not isinstance(native, dict)
        or message["local_type"] & 0xFFFFFFFF != 49
        or native.get("appmsg_subtype") not in {1, 19, 57, 2000, 2001}
        or reference.get("account_id") != packet["account_id"]
        or reference.get("peer_id") != packet["peer_id"]
        or str(proof.get("server_id")) != message["server_id"]
        or proof.get("message_type") != message["local_type"] & 0xFFFFFFFF
        or proof.get("unix_timestamp") != message["create_time"]
        or row["direction"] != message["direction"]
        or row["sender_id"] not in {None, "", message["sender_id"]}
    ):
        return None
    return {
        "original_id": row["id"],
        "source_file_name": stub["source_file_name"],
        "source_file_sha256": stub["source_file_sha256"],
        "original_text_sha256": hashlib.sha256(row["text"].encode()).hexdigest(),
        "original_provenance_sha256": hashlib.sha256(
            row["provenance"].encode()
        ).hexdigest(),
        "original_row_sha256": _sha(row),
        "original_ordinal": row["ordinal"],
        "sender_identity_filled": not row["sender_id"],
        "upgrade_basis": "inert_resource_stub_to_authenticated_native_projection_v1",
    }


def stage_mac_recovery_bank(
    local_store_root, packets, candidate_path, *, account_ids, progress=None
):
    """Build a new indexed candidate from explicit authenticated per-peer packets."""
    from .wechat_archive import WechatArchiveRepository, _file_identity

    accounts = tuple(sorted(set(account_ids)))
    if not accounts or any(
        not isinstance(account, str) or not account for account in accounts
    ):
        raise ValueError(
            "Mac bank staging needs an explicit nonempty account allowlist."
        )
    history = wechat_archive_path(local_store_root)
    inputs = _inputs(history)
    candidate = Path(candidate_path).expanduser()
    target = mac_recovery_bank_path(local_store_root)
    if (
        candidate.exists()
        or candidate.is_symlink()
        or candidate.resolve() in {history.resolve(), target.resolve()}
    ):
        raise ValueError("Mac recovery bank staging requires a new candidate path.")
    if not candidate.parent.is_dir() or candidate.parent.is_symlink():
        raise ValueError(
            "The bank candidate needs an existing local staging directory."
        )
    report = {
        "added": 0,
        "upgraded": 0,
        "existing": 0,
        "occurrences_added": 0,
        "variants": 0,
        "conflicts": 0,
        "scopes": 0,
    }
    processed_scopes, source_scopes, types = set(), set(), Counter()
    catalog_hashes = {}
    with (
        closing(
            sqlite3.connect(
                history.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
            )
        ) as original,
        closing(sqlite3.connect(candidate)) as output,
    ):
        original.row_factory = sqlite3.Row
        original.execute("PRAGMA temp_store=MEMORY")
        if target.exists():
            with closing(
                sqlite3.connect(
                    target.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
                )
            ) as previous:
                _validate_bank(previous, history, inputs)
                previous.backup(output)
            if (
                output.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='shadow_replacements'"
                ).fetchone()
                is None
            ):
                output.executescript(_SHADOW_SCHEMA)
        else:
            output.executescript(_SCHEMA)
        output.row_factory = sqlite3.Row
        output.execute("PRAGMA synchronous=OFF")
        message_columns = [
            column[1] for column in output.execute("PRAGMA table_info(messages)")
        ]
        output.executescript(
            "CREATE TEMP TABLE comparison_events(conversation_id TEXT,server_id TEXT,message_id TEXT,signature TEXT,"
            "canonical_store TEXT,PRIMARY KEY(conversation_id,server_id,message_id)) WITHOUT ROWID;"
            "CREATE TEMP TABLE selected_scopes(id TEXT PRIMARY KEY) WITHOUT ROWID;"
            "CREATE TEMP TABLE comparison_stubs(original_id TEXT PRIMARY KEY,source_file_name TEXT,"
            "source_file_sha256 TEXT,original_row TEXT) WITHOUT ROWID;"
        )
        repository = WechatArchiveRepository(local_store_root)
        _conversations, message_table, status = repository._timeline_tables(
            original, _file_identity(history), strict=True
        )
        if status not in {"ready", "absent"}:
            raise ValueError(
                "The existing source timeline is incomplete before Mac bank staging."
            )
        for packet in packets:
            _validate_packet(packet, accounts)
            conversation = _binding(original, packet)
            session = packet["session_id"]
            if session not in processed_scopes:
                count = 0
                for observed in original.execute(
                    f"SELECT * FROM {message_table} WHERE conversation_id=?", (session,)
                ):
                    count += 1
                    if count > MAX_SCOPE_MESSAGES:
                        raise ValueError(
                            "Mac comparison scope exceeds its bounded row budget."
                        )
                    proof = json.loads(observed["provenance"])
                    server = proof.get("server_id")
                    if (
                        server is None
                        or re.fullmatch(r"[1-9][0-9]{0,18}", str(server)) is None
                    ):
                        continue
                    store = (
                        "macos_bank"
                        if output.execute(
                            "SELECT 1 FROM messages WHERE id=?", (observed["id"],)
                        ).fetchone()
                        else "existing_timeline"
                    )
                    output.execute(
                        "INSERT OR IGNORE INTO comparison_events VALUES(?,?,?,?,?)",
                        (
                            session,
                            str(server),
                            observed["id"],
                            _event_signature(observed),
                            store,
                        ),
                    )
                    if store != "macos_bank":
                        stub_source = _stub_source(original, observed, catalog_hashes)
                        if stub_source is not None:
                            output.execute(
                                "INSERT OR IGNORE INTO comparison_stubs VALUES(?,?,?,?)",
                                (observed["id"], *stub_source, _json(dict(observed))),
                            )
                processed_scopes.add(session)
            source = {key: value for key, value in packet.items() if key != "messages"}
            source_key = _sha(source)
            output.execute(
                "INSERT OR IGNORE INTO sources VALUES(?,?,?,?,?,?)",
                (
                    source_key,
                    packet["account_id"],
                    packet["peer_id"],
                    packet["source"]["source_id"],
                    packet["source"]["snapshot_id"],
                    _json(source),
                ),
            )
            output.execute(
                "INSERT OR IGNORE INTO conversation_proofs VALUES(?,?,?)",
                (session, source_key, _json(source)),
            )
            source_scopes.add((session, source_key))
            output.execute(
                "INSERT OR IGNORE INTO selected_scopes VALUES(?)", (session,)
            )
            for message in packet["messages"]:
                projected = _row(packet, message)
                canonical = dict(zip(message_columns, projected, strict=True))
                signature = _event_signature(canonical)
                locator_key = _identifier(
                    "mac-bank-source-row-v1",
                    packet["account_id"],
                    packet["source"]["snapshot_id"],
                    packet["source"]["main_sha256"],
                    packet["source"]["wal_sha256"],
                    packet["source"]["database_relative_path"],
                    packet["source"]["table"],
                    str(message["local_id"]),
                )
                existing_occurrence = output.execute(
                    "SELECT content_sha256,signature FROM occurrences WHERE id=?",
                    (locator_key,),
                ).fetchone()
                if existing_occurrence is not None:
                    if tuple(existing_occurrence) != (
                        message["content_sha256"],
                        signature,
                    ):
                        raise ValueError(
                            "One preserved Mac source row changed after its prior recovery."
                        )
                    report["existing"] += 1
                    continue
                matches = list(
                    output.execute(
                        "SELECT message_id,signature,canonical_store FROM comparison_events WHERE conversation_id=? AND server_id=?",
                        (session, message["server_id"]),
                    )
                )
                conflict = matches and any(
                    row["signature"] != signature for row in matches
                )
                upgrade = None
                if conflict and len(matches) == 1:
                    stub = output.execute(
                        "SELECT * FROM comparison_stubs WHERE original_id=?",
                        (matches[0]["message_id"],),
                    ).fetchone()
                    if stub is not None:
                        upgrade = _upgrade_stub(stub, packet, message)
                if upgrade is not None:
                    projected[0] = upgrade["original_id"]
                    projected[2] = upgrade["original_ordinal"]
                    provenance = json.loads(projected[11])
                    provenance["macos_stub_upgrade"] = upgrade
                    projected[11] = _json(provenance)
                    canonical_id, store = projected[0], "macos_bank_shadow"
                    output.execute(
                        "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        projected,
                    )
                    output.execute(
                        "INSERT INTO event_keys VALUES(?,?,?,?,?)",
                        (
                            canonical_id,
                            packet["account_id"],
                            packet["peer_id"],
                            message["server_id"],
                            signature,
                        ),
                    )
                    output.execute(
                        "INSERT INTO shadow_replacements VALUES(?,?,?,?,?,?,?,?)",
                        (
                            canonical_id,
                            session,
                            upgrade["source_file_name"],
                            upgrade["source_file_sha256"],
                            upgrade["original_text_sha256"],
                            upgrade["original_provenance_sha256"],
                            upgrade["original_row_sha256"],
                            _json(upgrade),
                        ),
                    )
                    output.execute(
                        "UPDATE comparison_events SET signature=?,canonical_store=? "
                        "WHERE conversation_id=? AND server_id=? AND message_id=?",
                        (signature, store, session, message["server_id"], canonical_id),
                    )
                    output.execute(
                        "INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            locator_key,
                            source_key,
                            session,
                            message["local_id"],
                            message["server_id"],
                            message["content_sha256"],
                            canonical_id,
                            store,
                            signature,
                            _json(message),
                        ),
                    )
                    report["upgraded"] += 1
                    report["occurrences_added"] += 1
                    continue
                if conflict:
                    reason = "native_or_projection_conflict"
                    output.execute(
                        "INSERT OR IGNORE INTO gaps VALUES(?,?,?,?,?,?,?)",
                        (
                            locator_key,
                            session,
                            source_key,
                            message["local_id"],
                            message["server_id"],
                            reason,
                            message["content_sha256"],
                        ),
                    )
                    report["conflicts"] += 1
                    continue
                if matches:
                    canonical_id, _signature, store = matches[0]
                    report["existing"] += 1
                else:
                    canonical_id, store = projected[0], "macos_bank"
                    output.execute(
                        "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        projected,
                    )
                    output.execute(
                        "INSERT INTO event_keys VALUES(?,?,?,?,?)",
                        (
                            canonical_id,
                            packet["account_id"],
                            packet["peer_id"],
                            message["server_id"],
                            signature,
                        ),
                    )
                    output.execute(
                        "INSERT INTO comparison_events VALUES(?,?,?,?,?)",
                        (session, message["server_id"], canonical_id, signature, store),
                    )
                    report["added"] += 1
                    types[str(message["local_type"])] += 1
                output.execute(
                    "INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        locator_key,
                        source_key,
                        session,
                        message["local_id"],
                        message["server_id"],
                        message["content_sha256"],
                        canonical_id,
                        store,
                        signature,
                        _json(message),
                    ),
                )
                report["occurrences_added"] += 1
            output.execute(
                "INSERT OR IGNORE INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(conversation[key] for key in legacy._CONVERSATION_COLUMNS),
            )
            if progress is not None:
                progress(
                    {
                        **report,
                        "scope": session,
                        "processed_scopes": len(processed_scopes),
                    }
                )
        if not processed_scopes:
            raise ValueError(
                "Mac recovery bank staging received no proved conversation packets."
            )
        for session in (
            row[0] for row in output.execute("SELECT id FROM conversations")
        ):
            count = output.execute(
                "SELECT COUNT(*) FROM messages m WHERE conversation_id=? AND NOT EXISTS("
                "SELECT 1 FROM shadow_replacements s WHERE s.original_id=m.id)",
                (session,),
            ).fetchone()[0]
            first = output.execute(
                "SELECT time_sort,timestamp_value FROM messages WHERE conversation_id=? ORDER BY time_sort,ordinal,id LIMIT 1",
                (session,),
            ).fetchone()
            last = output.execute(
                "SELECT time_sort,timestamp_value,text FROM messages WHERE conversation_id=? ORDER BY time_sort DESC,ordinal DESC,id DESC LIMIT 1",
                (session,),
            ).fetchone()
            if first is not None:
                output.execute(
                    "UPDATE conversations SET message_count=?,first_sort=?,last_sort=?,first_time=?,last_time=?,latest_text=? WHERE id=?",
                    (
                        count,
                        first[0],
                        last[0],
                        first[1],
                        last[1],
                        last[2][:300],
                        session,
                    ),
                )
            else:
                output.execute("DELETE FROM conversations WHERE id=?", (session,))
        retained_accounts = [
            row[0]
            for row in output.execute(
                "SELECT DISTINCT account_id FROM sources ORDER BY account_id"
            )
        ]
        metadata = {
            "schema_version": "3",
            "code_version": VERSION,
            "input_identities": _json(
                {key: value for key, value in inputs.items() if key != target.name}
            ),
            "account_ids": _json(retained_accounts),
            "payload_sha256": _rows_digest(output),
        }
        output.executemany(
            "INSERT OR REPLACE INTO archive_info VALUES(?,?)", metadata.items()
        )
        output.commit()
        if output.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            # sqlite3.Row equality differs from tuple equality.
            if [row[0] for row in output.execute("PRAGMA integrity_check")] != ["ok"]:
                raise ValueError(
                    "The staged Mac recovery bank failed full integrity checking."
                )
        report.update(
            total=output.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            scopes=output.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
            type_counts=dict(types),
            upgraded_total=output.execute(
                "SELECT COUNT(*) FROM shadow_replacements"
            ).fetchone()[0],
        )
        report["added_total"] = report["total"] - report["upgraded_total"]
    os.chmod(candidate, 0o600)
    if _inputs(history) != inputs:
        raise ValueError("A source archive changed during Mac recovery bank staging.")
    return {
        **report,
        "candidate_path": str(candidate),
        "candidate_sha256": _file_sha(candidate),
        "candidate_identity": _identity(candidate),
        "input_identities": inputs,
        "account_ids": list(accounts),
    }


def _validate_bank(connection, history, current_inputs=None):
    global _SCHEMA_CONTRACT
    metadata = dict(connection.execute("SELECT key,value FROM archive_info"))
    schema_version = metadata.get("schema_version")
    supported = {
        "1": ("v1.0.0-codex.0",),
        "2": ("v1.1.0-codex.0",),
        "3": ("v1.2.0-codex.0", VERSION),
    }
    if (
        schema_version not in supported
        or metadata.get("code_version") not in supported[schema_version]
    ):
        raise ValueError("Unsupported Mac recovery bank schema.")
    if schema_version not in _SCHEMA_CONTRACT:
        with closing(sqlite3.connect(":memory:")) as expected_schema:
            expected_schema.executescript(
                _SCHEMA_V1 if schema_version == "1" else _SCHEMA
            )
            _SCHEMA_CONTRACT[schema_version] = list(
                expected_schema.execute(
                    "SELECT type,name,tbl_name,sql FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
                )
            )
    actual_schema = [
        tuple(row)
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    ]
    if actual_schema != _SCHEMA_CONTRACT[schema_version]:
        raise ValueError(
            "The Mac recovery bank schema differs from its closed table contract."
        )
    expected = json.loads(metadata["input_identities"])
    actual = current_inputs or _inputs(Path(history))
    if expected != {
        key: value for key, value in actual.items() if key != "macos_recovery.sqlite3"
    }:
        raise ValueError(
            "The Mac recovery bank is stale against its original timeline inputs."
        )
    if metadata.get("payload_sha256") != _rows_digest(connection):
        raise ValueError(
            "The Mac recovery bank payload differs from its sealed manifest."
        )
    return metadata


@contextmanager
def _publication_lock(history):
    """Keep one persistent lock inode shared by every bank publisher."""
    path = Path(history).with_name(_PUBLICATION_LOCK_NAME)
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        try:
            descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            descriptor = os.open(path, flags)
    except OSError:
        raise ValueError("The Mac recovery publication lock is unavailable.") from None
    with os.fdopen(descriptor, "r+", encoding="ascii") as handle:

        def verify():
            opened = os.fstat(handle.fileno())
            named = path.lstat()
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(named.st_mode)
                or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
                or os.name != "nt"
                and (
                    stat.S_IMODE(opened.st_mode) & 0o077 or opened.st_uid != os.getuid()
                )
            ):
                raise ValueError(
                    "The Mac recovery publication lock must be owner-private and unchanged."
                )

        verify()
        try:
            lock_file(handle, blocking=False)
        except BlockingIOError:
            raise ValueError(
                "Another process is publishing the Mac recovery bank."
            ) from None
        try:
            verify()
            yield
        finally:
            unlock_file(handle)


def publish_mac_recovery_bank(
    local_store_root, candidate_path, receipt, *, before_replace=None
):
    """Recheck staged inputs and optional cancellation before the atomic publication."""
    history = wechat_archive_path(local_store_root)
    candidate = Path(candidate_path)
    target = mac_recovery_bank_path(local_store_root)
    with _LOCK, _publication_lock(history):
        candidate_identity = _identity(candidate)
        if (
            str(candidate) != receipt["candidate_path"]
            or candidate_identity != receipt["candidate_identity"]
            or _file_sha(candidate) != receipt["candidate_sha256"]
        ):
            raise ValueError(
                "The Mac recovery bank candidate differs from its staging receipt."
            )
        if _inputs(history) != receipt["input_identities"]:
            raise ValueError(
                "Mac recovery bank source identities changed before publication."
            )
        with closing(
            sqlite3.connect(
                candidate.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
            )
        ) as connection:
            _validate_bank(connection, history)
        if stat.S_IMODE(candidate.stat().st_mode) != 0o600:
            raise ValueError("The reviewed Mac bank candidate must remain owner-only.")
        with candidate.open("rb") as stream:
            os.fsync(stream.fileno())
        if (
            _identity(candidate) != candidate_identity
            or _inputs(history) != receipt["input_identities"]
        ):
            raise ValueError(
                "A staged Mac bank input changed immediately before publication."
            )
        if before_replace is not None:
            before_replace()
        os.replace(candidate, target)
        return {
            **receipt,
            "bank_path": str(target),
            "bank_sha256": receipt["candidate_sha256"],
        }


def _attach(connection, history):
    path = Path(history).with_name("macos_recovery.sqlite3")
    identity = _identity(path, optional=True)
    if identity is None:
        return None
    inputs = _inputs(Path(history))
    connection.execute(
        "ATTACH DATABASE ? AS macos_recovery_bank",
        (path.resolve().as_uri() + "?mode=ro&immutable=1",),
    )
    key = (str(path.resolve()), _json(identity), _json(inputs))
    with _LOCK:
        metadata = _VALIDATED.get(key)
    if metadata is None:
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        ) as verifier:
            metadata = _validate_bank(verifier, history, inputs)
        with _LOCK:
            if len(_VALIDATED) >= 4:
                _VALIDATED.pop(next(iter(_VALIDATED)))
            _VALIDATED[key] = metadata
    if _identity(path) != identity or _inputs(Path(history)) != inputs:
        raise ValueError("The Mac recovery bank changed during validation.")
    return metadata


def _validate_shadow_sources(connection, history, *, conversation_id=None):
    """Recheck exact original catalog rows before exposing their monotone shadows."""
    path = Path(history).with_name("macos_recovery.sqlite3")
    inputs = _inputs(Path(history))
    base_key = (str(path.resolve()), _json(_identity(path)), _json(inputs))
    key = (*base_key, conversation_id)
    with _LOCK:
        if key in _SHADOW_VALIDATED or (*base_key, None) in _SHADOW_VALIDATED:
            return
    source_files = connection.execute(
        "SELECT DISTINCT source_file_name,source_file_sha256 FROM macos_recovery_bank.shadow_replacements "
        "WHERE conversation_id IN (SELECT id FROM temp.macos_recovery_bank_scopes)"
    ).fetchall()
    databases = {
        Path(row[2]).name: row[1]
        for row in connection.execute("PRAGMA database_list")
        if row[2]
    }
    for index, source in enumerate(source_files):
        name, digest = source
        if name not in {"resource_references.sqlite3", "resource_revalidated.sqlite3"}:
            raise ValueError(
                "A Mac shadow requires its exact original resource catalog."
            )
        catalog = Path(history).with_name(name)
        catalog_identity = _identity(catalog)
        hash_key = (str(catalog.resolve()), _json(catalog_identity))
        with _LOCK:
            actual_digest = _CATALOG_HASHES.get(hash_key)
        if actual_digest is None:
            actual_digest = _file_sha(catalog)
            if _identity(catalog) != catalog_identity:
                raise ValueError(
                    "An original stub catalog changed during hash validation."
                )
            with _LOCK:
                if len(_CATALOG_HASHES) >= 4:
                    _CATALOG_HASHES.pop(next(iter(_CATALOG_HASHES)))
                _CATALOG_HASHES[hash_key] = actual_digest
        if actual_digest != digest:
            raise ValueError(
                "A Mac shadow's original resource catalog no longer matches its full-file receipt."
            )
        alias = databases.get(name)
        if alias is None:
            alias = f"macos_stub_catalog_{index}"
            connection.execute(
                f"ATTACH DATABASE ? AS {alias}",
                (catalog.resolve().as_uri() + "?mode=ro&immutable=1",),
            )
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias) is None:
            raise ValueError("Unsupported original resource catalog alias.")
        expected_count = connection.execute(
            "SELECT COUNT(*) FROM macos_recovery_bank.shadow_replacements s WHERE source_file_name=? "
            "AND conversation_id IN (SELECT id FROM temp.macos_recovery_bank_scopes)",
            (name,),
        ).fetchone()[0]
        count = 0
        for row in connection.execute(
            "SELECT s.original_text_sha256,s.original_provenance_sha256,s.original_row_sha256,m.* "
            "FROM macos_recovery_bank.shadow_replacements s CROSS JOIN "
            + alias
            + ".messages m "
            "WHERE s.original_id=m.id AND s.source_file_name=? "
            "AND s.conversation_id IN (SELECT id FROM temp.macos_recovery_bank_scopes)",
            (name,),
        ):
            count += 1
            original = {
                key: row[key] for key in row.keys() if not key.startswith("original_")
            }
            if (
                hashlib.sha256(original["text"].encode()).hexdigest()
                != row["original_text_sha256"]
                or hashlib.sha256(original["provenance"].encode()).hexdigest()
                != row["original_provenance_sha256"]
                or _sha(original) != row["original_row_sha256"]
            ):
                raise ValueError(
                    "An original resource stub differs from its exact shadow receipt."
                )
        if count != expected_count or _identity(catalog) != catalog_identity:
            raise ValueError(
                "The original resource stub membership changed during validation."
            )
    if _inputs(Path(history)) != inputs:
        raise ValueError("A Mac shadow input changed during source validation.")
    with _LOCK:
        if len(_SHADOW_VALIDATED) >= 16:
            _SHADOW_VALIDATED.pop(next(iter(_SHADOW_VALIDATED)))
        _SHADOW_VALIDATED[key] = True


def attach_mac_recovery_bank(
    connection, history_path, conversation_table, message_table, *, conversation_id=None
):
    """Attach an indexed bank while retaining existing JSON and native timeline rows."""
    default = (conversation_table, message_table)
    try:
        if any(
            not isinstance(value, str) or legacy._SQL.fullmatch(value) is None
            for value in default
        ):
            raise ValueError("Unsupported Mac bank input timeline tables.")
        metadata = _attach(connection, history_path)
        if metadata is None:
            return *default, "absent"
        rows = connection.execute(
            "SELECT * FROM macos_recovery_bank.conversations"
            + (" WHERE id=?" if conversation_id else ""),
            (conversation_id,) if conversation_id else (),
        ).fetchall()
        if not rows:
            return *default, "absent"
        connection.execute(
            "CREATE TEMP TABLE macos_recovery_bank_scopes(id TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        connection.executemany(
            "INSERT INTO macos_recovery_bank_scopes VALUES(?)",
            [(row["id"],) for row in rows],
        )
        for row in rows:
            for record in connection.execute(
                "SELECT proof FROM macos_recovery_bank.conversation_proofs WHERE conversation_id=?",
                (row["id"],),
            ):
                _binding(connection, json.loads(record[0]))
        shadow_filter = ""
        if metadata["schema_version"] in {"2", "3"}:
            _validate_shadow_sources(
                connection, history_path, conversation_id=conversation_id
            )
            shadow_filter = (
                " WHERE NOT EXISTS(SELECT 1 FROM macos_recovery_bank.shadow_replacements s "
                "WHERE s.original_id=m.id AND s.conversation_id IN "
                "(SELECT id FROM temp.macos_recovery_bank_scopes))"
            )
        connection.execute(
            f"CREATE TEMP VIEW macos_recovery_bank_timeline_messages AS SELECT m.* FROM {message_table} m"
            + shadow_filter
            + " UNION ALL "
            "SELECT b.* FROM macos_recovery_bank.messages b WHERE b.conversation_id IN (SELECT id FROM temp.macos_recovery_bank_scopes)"
        )
        connection.execute(
            f"CREATE TEMP TABLE macos_recovery_bank_conversations AS SELECT * FROM {conversation_table} WHERE 0"
        )
        for row in rows:
            existing = connection.execute(
                f"SELECT * FROM {conversation_table} WHERE id=?", (row["id"],)
            ).fetchone()
            if existing is None:
                raise ValueError(
                    "A Mac bank cannot create an unproved source conversation."
                )
            value = dict(existing)
            first_new = row["first_sort"] > 0 and (
                value["first_sort"] == 0 or row["first_sort"] < value["first_sort"]
            )
            last_new = row["last_sort"] >= value["last_sort"]
            value.update(
                message_count=value["message_count"] + row["message_count"],
                first_sort=row["first_sort"] if first_new else value["first_sort"],
                first_time=row["first_time"] if first_new else value["first_time"],
                last_sort=row["last_sort"] if last_new else value["last_sort"],
                last_time=row["last_time"] if last_new else value["last_time"],
                latest_text=row["latest_text"] if last_new else value["latest_text"],
            )
            connection.execute(
                "INSERT INTO macos_recovery_bank_conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(value[key] for key in legacy._CONVERSATION_COLUMNS),
            )
        connection.execute(
            f"CREATE TEMP VIEW macos_recovery_bank_timeline_conversations AS SELECT c.* FROM {conversation_table} c "
            "WHERE NOT EXISTS(SELECT 1 FROM temp.macos_recovery_bank_conversations b WHERE b.id=c.id) UNION ALL "
            "SELECT * FROM temp.macos_recovery_bank_conversations"
        )
        connection.execute(
            "CREATE TEMP TABLE macos_recovery_bank_validated(ready INTEGER)"
        )
        return (
            "macos_recovery_bank_timeline_conversations",
            "macos_recovery_bank_timeline_messages",
            "ready",
        )
    except (
        OSError,
        sqlite3.Error,
        ValueError,
        TypeError,
        KeyError,
        RecursionError,
        OverflowError,
    ):
        return *default, "invalid"


def mark_bank_rows(connection, rows, sentinel):
    if (
        connection.execute(
            "SELECT 1 FROM temp.sqlite_master WHERE type='table' AND name='macos_recovery_bank_validated'"
        ).fetchone()
        is None
    ):
        return rows
    for row in rows:
        stored = connection.execute(
            "SELECT provenance,text FROM macos_recovery_bank.messages WHERE id=?",
            (row["id"],),
        ).fetchone()
        if stored is not None and tuple(stored) == (row["provenance"], row["text"]):
            row["_macos_recovery_validated"] = sentinel
    return rows


def bank_message_conversation(history_path, message_id):
    path = Path(history_path).with_name("macos_recovery.sqlite3")
    if _identity(path, optional=True) is None:
        return None
    with closing(
        sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    ) as connection:
        row = connection.execute(
            "SELECT conversation_id FROM messages WHERE id=?", (message_id,)
        ).fetchone()
    return row[0] if row else None
