"""Stage indexed, source-bound Mac recovery without rewriting the original archive.

Code version: v1.15.4-codex.0
"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from contextlib import closing, contextmanager, nullcontext
import ctypes
from datetime import datetime
import errno
import hashlib
import json
from multiprocessing import get_context
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
from threading import RLock, get_ident

from . import wechat_macos_bank_v4_reader as schema4_reader
from . import wechat_macos_recovery as legacy
from . import wechat_macos_reparse as reparse
from .compute_resources import resolve_cpu_process_workers
from .owner_only_files import ensure_owner_only, owner_only_directory_problem, owner_only_problem
from .platform_lock import lock_file, unlock_file
from .wechat_archive import _identifier, _time_fields, wechat_archive_path
from .wechat_completion_clone import sync_completion_file
from .wechat_group_membership import valid_native_membership
from .wechat_reconcile import _TEXT_METADATA_FIELDS, native_text_metadata
from .wechat_resource_index import (
    CHANNELS_APP_SUBTYPE, CHANNELS_LABEL_PATHS, _KINDS, _LABELS, _app_label,
)
from .wechat_text import is_native_pat_projection, native_components_text
from .windows_file_metadata import file_change_time, has_open_file_handles


CODE_VERSION = "v1.15.4-codex.0"
WRITER_VERSION = "v1.5.1-codex.0"
VERSION = WRITER_VERSION
MAX_PACKET_BYTES = 128 * 1024**2
MAX_PACKET_MESSAGES = 100_000
MAX_SCOPE_MESSAGES = 5_000_000
_LOCK = RLock()
_PUBLICATION_LOCK_NAME = ".macos_recovery.publication.lock"
_STUB_CLOCK_POLICY = "inert_resource_stub_to_authenticated_native_projection_v2"
_STUB_CLOCK_FIELDS = ("time_sort", "timestamp_value", "timestamp_label", "time_basis")
_VALIDATED = {}
_VALIDATING = {}
_MIN_PARALLEL_BANK_BYTES = 16 * 1024**2
_MIN_PARALLEL_SHADOW_ROWS = 1_024
_MAX_VALIDATION_WORKERS = 3
_CLONE_NOOWNERCOPY = 0x0002
_SCHEMA_CONTRACT = {}
_SHADOW_VALIDATED = {}
_SHADOW_VALIDATING = {}
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
    "system_membership",
}
_SIGNATURE_FIELDS = (
    *_TEXT_METADATA_FIELDS,
    "invitation_card_fields",
    "system_message_subtype",
    "system_source_ids",
    "app_card_metadata",
)
_LINK_LABEL_FIELDS = ("safe_title", "publisher_name", "app_name")
_APP_LABEL_FIELDS = (
    "safe_title",
    "app_name",
    "app_name_source",
    "publisher_name",
    "publisher_name_source",
    *CHANNELS_LABEL_PATHS,
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
_SCHEMA_V4 = _SCHEMA + reparse.SCHEMA


def _json(value):
    return legacy._json(value)


def _sha(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _file_sha(path, *, check_cancel=None):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(4 * 1024**2):
            if check_cancel is not None:
                check_cancel()
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
    return _storage_identity(Path(path), value)


def _storage_identity(path, state, *, descriptor=None):
    identity = {
        key: getattr(state, "st_" + key)
        for key in ("dev", "ino", "size", "mtime_ns", "ctime_ns")
    }
    if os.name == "nt":
        change_time = file_change_time(path, descriptor=descriptor)
        # FILE_BASIC_INFO counts 100 ns intervals since 1 Jan 1601.
        identity["ctime_ns"] = (change_time - 116444736000000000) * 100
    return identity


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
        or binding.get("schema_version") not in (1, 2)
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
    owner_row = _name_proof(
        binding["owner_proof"], owner, 2 if binding["schema_version"] == 1 else None
    )
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
                anchor["real_sender_id"] != owner_row["rowid"]
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


def _valid_resource_labels(resource, typ):
    for key in _APP_LABEL_FIELDS:
        if key in resource and (
            not isinstance(resource[key], str)
            or len(resource[key]) > 1024
            or any(
                ord(char) < 32 and char not in "\t\r\n"
                for char in resource[key]
            )
        ):
            return False
    versioned = "app_metadata_version" in resource
    if any(key in resource for key in _APP_LABEL_FIELDS if key != "safe_title"):
        if not versioned:
            return False
    if not versioned:
        return True
    if (
        resource["app_metadata_version"] != "1"
        or typ != 49
        or resource.get("kind") != "app"
        or (
            CHANNELS_LABEL_PATHS.keys() & resource.keys()
            and resource.get("app_subtype") != str(CHANNELS_APP_SUBTYPE)
        )
        or not any(
            resource.get(key, "").strip()
            for key in (*_LINK_LABEL_FIELDS, *CHANNELS_LABEL_PATHS)
        )
    ):
        return False
    # Older link packets sealed the literal labels before saving their source paths.
    if (
        resource.get("app_subtype") == "5"
        and not any(key in resource for key in ("app_name_source", "publisher_name_source"))
    ):
        return all(
            key not in resource or bool(_app_label(resource[key]))
            and _app_label(resource[key]) == resource[key]
            for key in _LINK_LABEL_FIELDS
        )
    for key, paths in (
        ("publisher_name", {"appmsg/sourcedisplayname"}),
        ("app_name", {"appinfo/appname", "appmsg/appinfo/appname"}),
    ):
        if key in resource or key + "_source" in resource:
            if (
                not resource.get(key, "").strip()
                or resource.get(key + "_source") not in paths
            ):
                return False
    return True


def _app_card_metadata(resource):
    if resource.get("app_metadata_version") != "1":
        return None
    return {
        "schema_version": 1,
        **{key: resource[key] for key in _APP_LABEL_FIELDS if key in resource},
    }


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
        legacy._SOURCE_FIELDS | legacy._LOCK_BYTE_SOURCE_FIELDS | {"account_directory"}
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
        pat = is_native_pat_projection(
            message.get("native_text"), message["local_type"], peer_id=peer
        )
        system = typ in {10000, 10002} or pat
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
            if "system_membership" in native and (
                packet["conversation_kind"] != "group"
                or typ not in {10000, 10002}
                or kind != "system_text"
                or native.get("system_message_subtype") != "sysmsgtemplate"
                or not valid_native_membership(native["system_membership"], native["text"])
            ):
                raise ValueError("Mac membership evidence conflicts with its native notice.")
            if (
                typ == 1
                and kind != "plain_text"
                or system
                and kind != "system_text"
                or typ == 49
                and not pat
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
                "app_metadata_version",
                *_APP_LABEL_FIELDS,
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
                or not _valid_resource_labels(resource, typ)
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
            binding_mode="authenticated_source_account_binding_v"
            + str(packet["account_binding"]["schema_version"]),
        )
    labels = _app_card_metadata(message.get("resource", {}))
    if labels is not None:
        # Version only new label projections; historical resource signatures stay valid.
        proof["app_card_metadata"] = labels
    projected[11] = _json(proof)
    return projected


def _rows_digest(connection, *, check_cancel=None):
    digest = hashlib.sha256()
    encode_row = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode
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
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name='projection_revisions' AND type='table'"
    ).fetchone():
        tables.append("projection_revisions")
    for table in tables:
        digest.update(table.encode())
        for index, row in enumerate(connection.execute(f"SELECT * FROM {table} ORDER BY 1,2")):
            if check_cancel is not None and index % 256 == 0:
                check_cancel()
            digest.update(encode_row(tuple(row)).encode() + b"\n")
    return digest.hexdigest()


def _writer_version(connection):
    """Identify the complete provenance policy independently of the Python API version."""
    return WRITER_VERSION


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
                *_APP_LABEL_FIELDS,
                "app_metadata_version",
                "app_card_metadata",
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


def _exact_epoch(value):
    return type(value) is int and 1 <= value <= 253402271999


def _catalog_clock(row):
    proof = json.loads(row["provenance"])
    epoch = proof.get("unix_timestamp")
    if (
        not _exact_epoch(epoch)
        or type(row["time_sort"]) not in {int, float}
        or any(not isinstance(row[key], str) or not row[key] for key in _STUB_CLOCK_FIELDS[1:])
    ):
        return None
    try:
        observed = datetime.fromisoformat(row["timestamp_value"])
        if observed.tzinfo is None or observed.timestamp() != epoch:
            return None
        if tuple(row[key] for key in _STUB_CLOCK_FIELDS) != _time_fields(
            {"sent_at_utc_iso": observed.isoformat()}
        ):
            return None
    except (ValueError, TypeError, OverflowError, OSError):
        return None
    return {"epoch": epoch, **{key: row[key] for key in _STUB_CLOCK_FIELDS}}


def _upgrade_stub(stub, packet, message):
    row = json.loads(stub["original_row"])
    proof = json.loads(row["provenance"])
    reference = proof["resource_reference"]
    native = message.get("native_text")
    if (
        not isinstance(native, dict)
        or message["local_type"] & 0xFFFFFFFF != 49
        or type(native.get("appmsg_subtype")) is not int
        or reference.get("account_id") != packet["account_id"]
        or reference.get("peer_id") != packet["peer_id"]
        or str(proof.get("server_id")) != message["server_id"]
        or proof.get("message_type") != message["local_type"] & 0xFFFFFFFF
    ):
        return None
    catalog_epoch = proof.get("unix_timestamp")
    native_epoch = message["create_time"]
    if not _exact_epoch(catalog_epoch) or not _exact_epoch(native_epoch):
        return None
    delta = native_epoch - catalog_epoch
    pat = is_native_pat_projection(native, message["local_type"], peer_id=packet["peer_id"])
    if pat:
        # The catalog's inert App row lacks actor information. A typed pat
        # supplies its own roles and is admitted only at the exact source time.
        if delta or row["sender_id"] or row["direction"] not in {"received", "unknown"}:
            return None
    elif (
        native["appmsg_subtype"] not in {1, 19, 57, 2000, 2001}
        or row["direction"] != message["direction"]
        or row["sender_id"] not in {None, "", message["sender_id"]}
        or abs(delta) > 1
        or delta and row["sender_id"] != message["sender_id"]
    ):
        return None
    upgrade = {
        "original_id": row["id"],
        "source_file_name": stub["source_file_name"],
        "source_file_sha256": stub["source_file_sha256"],
        "original_text_sha256": hashlib.sha256(row["text"].encode()).hexdigest(),
        "original_provenance_sha256": hashlib.sha256(
            row["provenance"].encode()
        ).hexdigest(),
        "original_row_sha256": _sha(row),
        "original_ordinal": row["ordinal"],
        "sender_identity_filled": not pat and not row["sender_id"],
        "upgrade_basis": "inert_resource_stub_to_authenticated_native_projection_v1",
    }
    if delta == 0:
        return upgrade
    catalog_clock = _catalog_clock(row)
    if catalog_clock is None:
        return None
    return {
        **upgrade, "upgrade_basis": _STUB_CLOCK_POLICY,
        "native_source_key": _sha({key: value for key, value in packet.items() if key != "messages"}),
        "clock_observations": {
            "catalog": catalog_clock, "native": {"epoch": native_epoch},
            "native_minus_catalog_seconds": delta,
        },
    }


def _validate_stub_clock_receipt(connection, row, upgrade, *, original=None, schema="main"):
    """Validate both observed clocks without choosing a preferred source epoch."""
    if upgrade.get("upgrade_basis") != _STUB_CLOCK_POLICY:
        raise ValueError("Unsupported retained-clock stub upgrade policy.")
    clocks = upgrade.get("clock_observations")
    catalog = clocks.get("catalog") if isinstance(clocks, dict) else None
    native = clocks.get("native") if isinstance(clocks, dict) else None
    delta = clocks.get("native_minus_catalog_seconds") if isinstance(clocks, dict) else None
    proof = json.loads(row["provenance"])
    receipt = proof.get("macos_recovery", {})
    if (
        not isinstance(catalog, dict) or set(catalog) != {"epoch", *_STUB_CLOCK_FIELDS}
        or not isinstance(native, dict) or set(native) != {"epoch"}
        or set(clocks) != {"catalog", "native", "native_minus_catalog_seconds"}
        or not _exact_epoch(catalog.get("epoch")) or not _exact_epoch(native.get("epoch"))
        or type(delta) is not int or delta not in {-1, 0, 1}
        or native["epoch"] - catalog["epoch"] != delta
        or type(upgrade.get("original_ordinal")) is not int
        or upgrade["original_id"] != row["id"] or upgrade["original_ordinal"] != row["ordinal"]
        or any(catalog[key] != row[key] for key in _STUB_CLOCK_FIELDS)
        or type(upgrade.get("sender_identity_filled")) is not bool
        or delta and upgrade["sender_identity_filled"]
        or not isinstance(receipt, dict)
        or not _exact_epoch(proof.get("unix_timestamp"))
        or not _exact_epoch(receipt.get("create_time"))
        or proof.get("unix_timestamp") != native["epoch"]
        or receipt.get("create_time") != native["epoch"]
        or type(proof.get("appmsg_subtype")) is not int
        or proof.get("appmsg_subtype") not in {1, 19, 57, 2000, 2001}
    ):
        raise ValueError("A retained-clock stub upgrade has inconsistent observations.")
    catalog_projection = {
        **row, "provenance": _json({**proof, "unix_timestamp": catalog["epoch"]})
    }
    if _catalog_clock(catalog_projection) != catalog:
        raise ValueError("A retained-clock stub upgrade has an invalid catalog clock.")
    source_key = upgrade.get("native_source_key")
    if not isinstance(source_key, str) or re.fullmatch(r"[0-9a-f]{64}", source_key) is None:
        raise ValueError("A retained-clock stub upgrade lost its native source key.")
    source = connection.execute(f"SELECT proof FROM {schema}.sources WHERE id=?", (source_key,)).fetchone()
    observed = connection.execute(
        f"SELECT proof,canonical_id FROM {schema}.occurrences "
        "WHERE source_id=? AND conversation_id=? AND local_id=?",
        (source_key, row["conversation_id"], receipt.get("local_id")),
    ).fetchone()
    if source is None or observed is None or observed[1] != row["id"]:
        raise ValueError("A retained-clock stub upgrade lost its exact native observation.")
    packet, message = json.loads(source[0]), json.loads(observed[0])
    _validate_packet({**packet, "messages": [message]}, (packet["account_id"],))
    if (
        _sha(packet) != source_key
        or packet["session_id"] != row["conversation_id"]
        or packet["account_id"] != receipt.get("account_id")
        or packet["peer_id"] != receipt.get("peer_id")
        or message["create_time"] != native["epoch"]
        or message["server_id"] != str(proof.get("server_id"))
        or message["sender_id"] != row["sender_id"] or message["direction"] != row["direction"]
        or any(message.get(key) != receipt.get(key) for key in (
            "local_id", "local_type", "server_id", "content_sha256", "real_sender_id"
        ))
    ):
        raise ValueError("A retained-clock stub upgrade differs from its native proof.")
    if original is not None:
        original_proof = json.loads(original["provenance"])
        reference = original_proof.get("resource_reference", {})
        if (
            _catalog_clock(original) != catalog
            or original["id"] != row["id"] or original["ordinal"] != row["ordinal"]
            or reference.get("account_id") != packet["account_id"]
            or reference.get("peer_id") != packet["peer_id"]
            or str(original_proof.get("server_id")) != message["server_id"]
            or original_proof.get("message_type") != 49
            or original["direction"] != message["direction"]
            or original["sender_id"] not in {None, "", message["sender_id"]}
            or delta and original["sender_id"] != message["sender_id"]
        ):
            raise ValueError("A retained-clock stub upgrade differs from its original catalog.")


def _validate_stub_clocks(connection, history):
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name='shadow_replacements' AND type='table'"
    ).fetchone():
        return
    if connection.execute(
        "SELECT 1 FROM messages m WHERE instr(m.provenance,?)>0 "
        "AND json_extract(m.provenance,'$.macos_stub_upgrade.upgrade_basis')=? "
        "AND NOT EXISTS(SELECT 1 FROM shadow_replacements s WHERE s.original_id=m.id) LIMIT 1",
        (_STUB_CLOCK_POLICY, _STUB_CLOCK_POLICY),
    ).fetchone():
        raise ValueError("A retained-clock projection lost its original shadow receipt.")
    cursor = connection.execute(
        "SELECT s.proof AS shadow_proof,s.original_id AS shadow_original_id,"
        "s.source_file_name AS shadow_source_file_name,s.source_file_sha256 AS shadow_source_file_sha256,"
        "s.original_text_sha256 AS shadow_original_text_sha256,"
        "s.original_provenance_sha256 AS shadow_original_provenance_sha256,"
        "s.original_row_sha256 AS shadow_original_row_sha256,m.* FROM shadow_replacements s "
        "LEFT JOIN messages m ON m.id=s.original_id "
        "WHERE instr(s.proof,?)>0 OR instr(s.proof,'\"clock_observations\"')>0",
        (_STUB_CLOCK_POLICY,),
    )
    fields = [column[0] for column in cursor.description]
    catalogs = {}
    try:
        for observed in cursor:
            row = dict(zip(fields, observed, strict=True))
            upgrade = json.loads(row.pop("shadow_proof"))
            if (
                row.get("id") is None
                or json.loads(row["provenance"]).get("macos_stub_upgrade") != upgrade
                or any(upgrade.get(key) != row["shadow_" + key] for key in (
                    "original_id", "source_file_name", "source_file_sha256",
                    "original_text_sha256", "original_provenance_sha256", "original_row_sha256",
                ))
            ):
                raise ValueError("A retained-clock stub upgrade lost its canonical receipt.")
            name = upgrade["source_file_name"]
            if name not in {"resource_references.sqlite3", "resource_revalidated.sqlite3"}:
                raise ValueError("A retained-clock upgrade names an unsupported catalog.")
            if name not in catalogs:
                path = Path(history).with_name(name)
                identity = _identity(path)
                catalog = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
                catalog.row_factory = sqlite3.Row
                catalogs[name] = (path, identity, catalog)
            path, identity, catalog = catalogs[name]
            original = catalog.execute("SELECT * FROM messages WHERE id=?", (row["id"],)).fetchone()
            if original is None:
                raise ValueError("A retained-clock upgrade lost its original catalog row.")
            original = dict(original)
            if (
                hashlib.sha256(original["text"].encode()).hexdigest() != upgrade["original_text_sha256"]
                or hashlib.sha256(original["provenance"].encode()).hexdigest() != upgrade["original_provenance_sha256"]
                or _sha(original) != upgrade["original_row_sha256"]
            ):
                raise ValueError("A retained-clock upgrade differs from its exact catalog receipt.")
            _validate_stub_clock_receipt(connection, row, upgrade, original=original)
        for path, identity, _catalog in catalogs.values():
            if _identity(path) != identity:
                raise ValueError("An original clock catalog changed during validation.")
    finally:
        for _path, _identity_value, catalog in catalogs.values():
            catalog.close()


def _private_candidate_workspace(path):
    for parent in (path.parent, path.parent.parent):
        if os.name == "nt":
            if owner_only_directory_problem(parent):
                return False
            continue
        value = parent.lstat()
        if (
            not stat.S_ISDIR(value.st_mode)
            or value.st_uid != os.getuid()
            or stat.S_IMODE(value.st_mode) & 0o077
        ):
            return False
    return True


def _baseline_copy_file(path, target):
    """Admit only an independent resident working file inside private directories."""
    identity = _identity(path)
    observed = path.lstat()
    if (
        observed.st_nlink != 1
        or any(os.path.lexists(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal"))
        or observed.st_dev != target.stat().st_dev
        or (os.name != "nt" and (
            observed.st_uid != os.getuid()
            or stat.S_IMODE(observed.st_mode) not in {0o600, 0o644}
        ))
    ):
        raise ValueError("A reused baseline must be an independently owned local file.")
    if not _private_candidate_workspace(path):
        raise ValueError("A reused baseline requires private owned parent directories.")
    return identity


def _no_baseline_copy_handles(path):
    """Reject every open handle; an unavailable inspection grants no reuse credit."""
    if os.name == "nt":
        try:
            opened = has_open_file_handles(path)
        except OSError:
            raise ValueError("Baseline open-file inspection did not complete.") from None
        if opened:
            raise ValueError("The baseline has an open handle or cannot be inspected safely.")
        return
    executable = shutil.which("lsof")
    if executable is None:
        raise ValueError("Open-file inspection is required before baseline reuse.")
    try:
        result = subprocess.run(
            [executable, "-nP", "-t", "--", str(path)],
            capture_output=True, text=True, check=False, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        raise ValueError("Baseline open-file inspection did not complete.") from None
    if result.returncode != 1 or result.stdout.strip() or result.stderr.strip():
        raise ValueError("The baseline has an open handle or cannot be inspected safely.")


def _baseline_schema(connection):
    return [tuple(row) for row in connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    )]


def _stable_candidate_sha(path, target, *, owner_only=False, check_cancel=None):
    """Hash a closed private candidate while requiring its complete storage pin."""
    if check_cancel is not None:
        check_cancel()
    before = _baseline_copy_file(path, target)
    if owner_only and _candidate_owner_problem(path):
        raise ValueError("The reviewed Mac bank candidate must remain owner-only.")
    _no_baseline_copy_handles(path)
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as stream:
        if _storage_identity(path, os.fstat(stream.fileno()), descriptor=stream.fileno()) != before:
            raise ValueError("The candidate changed before its complete byte proof.")
        while block := stream.read(4 * 1024**2):
            if check_cancel is not None:
                check_cancel()
            digest.update(block)
        if _storage_identity(path, os.fstat(stream.fileno()), descriptor=stream.fileno()) != before:
            raise ValueError("The candidate changed during its complete byte proof.")
    if _baseline_copy_file(path, target) != before:
        raise ValueError("The candidate changed during its complete byte proof.")
    _no_baseline_copy_handles(path)
    if _baseline_copy_file(path, target) != before:
        raise ValueError("The candidate changed after its complete byte proof.")
    return digest.hexdigest(), before


def _candidate_owner_problem(path):
    return owner_only_problem(path) or (
        os.name != "nt" and stat.S_IMODE(path.lstat().st_mode) != 0o600
    )


def _publication_candidate_file(path):
    identity = _identity(path)
    if (
        _candidate_owner_problem(path)
        or any(os.path.lexists(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal"))
    ):
        raise ValueError("The reviewed Mac bank candidate must remain independently owner-only.")
    return identity


def _candidate_storage_rebind(
    candidate, target, expected, file_sha256, *, history, guard, inputs,
    phase, audit, allow_rebind=True, owner_only=False, check_cancel=None,
):
    """Rebind only a closed candidate's ctime through an existing full byte proof."""
    _require_publication_guard(guard, history)
    if _inputs(history) != inputs:
        raise ValueError("Canonical inputs changed before reuse or publication.")
    current = _identity(candidate)
    if current == expected:
        return current
    if (
        not allow_rebind
        or set(current) != set(expected)
        or any(current[key] != value for key, value in expected.items() if key != "ctime_ns")
        or not isinstance(file_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", file_sha256) is None
    ):
        raise ValueError("The verified candidate changed before reuse or publication.")
    digest, rebound = _stable_candidate_sha(
        candidate, target, owner_only=owner_only, check_cancel=check_cancel,
    )
    _require_publication_guard(guard, history)
    if (
        digest != file_sha256 or rebound != current
        or _inputs(history) != inputs or _baseline_copy_file(candidate, target) != rebound
    ):
        raise ValueError("The candidate or canonical inputs changed during storage rebinding.")
    audit.append({
        "phase": phase, "basis": "candidate_ctime_only_exact_file_sha256_v1",
        "before_identity": dict(expected), "after_identity": dict(rebound),
        "file_sha256": file_sha256,
    })
    return rebound


class _BaselineCopyCapability:
    """Authorize one verified existing output under its original publication lease."""

    __slots__ = (
        "path", "history", "guard", "pid", "thread", "inputs", "identity",
        "metadata", "schema", "used", "allocated_bytes", "file_sha256", "storage_rebindings",
    )

    def __init__(self, path, history, guard, inputs, identity, metadata, schema, allocated, file_sha256, rebindings):
        self.path, self.history, self.guard = path, history.resolve(), guard
        self.pid, self.thread = os.getpid(), get_ident()
        self.inputs = json.loads(_json(inputs))
        self.identity = dict(identity)
        self.metadata = dict(metadata)
        self.schema = list(schema)
        self.used = False
        self.allocated_bytes = allocated
        self.file_sha256 = file_sha256
        self.storage_rebindings = json.loads(_json(rebindings))

    @property
    def summary(self):
        baseline = self.inputs["macos_recovery.sqlite3"]
        return {
            "schema_version": 1, "candidate_path": str(self.path),
            "scratch_path": str(self.path),
            "candidate_identity": dict(self.identity),
            "scratch_identity": dict(self.identity),
            "input_identities": json.loads(_json(self.inputs)),
            "baseline_identity": dict(baseline),
            "bank_schema_version": self.metadata["schema_version"],
            "payload_sha256": self.metadata["payload_sha256"],
            "file_sha256": self.file_sha256,
            "storage_rebindings": json.loads(_json(self.storage_rebindings)),
            "allocated_bytes": self.allocated_bytes,
            "reused_clone_credit_bytes": min(baseline["size"], self.allocated_bytes),
        }


def verify_baseline_copy(local_store_root, candidate_path, *, publication_guard):
    """Prove an existing scratch equals today's bank without changing its payload.

    The caller separately validates failed-report authority. This capability does
    not select native sources or resume a previous run's counters or receipts.
    """
    history = wechat_archive_path(local_store_root)
    _require_publication_guard(publication_guard, history)
    target = mac_recovery_bank_path(local_store_root)
    candidate = Path(candidate_path).expanduser().absolute()
    if candidate.resolve() in {history.resolve(), target.resolve()}:
        raise ValueError("A baseline working copy must be separate from canonical inputs.")
    inputs = _inputs(history)
    if inputs[target.name] is None:
        raise ValueError("Baseline reuse requires a current published bank.")
    file_sha256, before = _stable_candidate_sha(candidate, target)
    rebindings = []
    if _inputs(history) != inputs:
        raise ValueError("Canonical inputs changed before baseline verification.")
    with (
        closing(sqlite3.connect(target.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as previous,
        closing(sqlite3.connect(candidate.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as scratch,
    ):
        for connection in (previous, scratch):
            connection.execute("PRAGMA temp_store=MEMORY")
            connection.execute("PRAGMA trusted_schema=OFF")
        metadata = _validate_bank(previous, history, inputs, allow_parallel=True)
        scratch_metadata = _validate_bank(scratch, history, inputs)
        if [row[0] for row in scratch.execute("PRAGMA integrity_check")] != ["ok"]:
            raise ValueError("The baseline working copy failed SQLite integrity checking.")
        schema = _baseline_schema(previous)
        if scratch_metadata != metadata or _baseline_schema(scratch) != schema:
            raise ValueError("The scratch schema and complete metadata must equal the current bank.")
    before = _candidate_storage_rebind(
        candidate, target, before, file_sha256, history=history,
        guard=publication_guard, inputs=inputs, phase="factory_verification", audit=rebindings,
    )
    _no_baseline_copy_handles(candidate)
    for attempt in range(2):
        descriptor = os.open(
            candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            opened = os.fstat(descriptor)
            matched = _storage_identity(candidate, opened, descriptor=descriptor) == before
            if matched:
                if os.name == "nt":
                    ensure_owner_only(candidate)
                elif stat.S_IMODE(opened.st_mode) != 0o600:
                    os.fchmod(descriptor, 0o600)
                sealed = os.fstat(descriptor)
                sealed_identity = _storage_identity(candidate, sealed, descriptor=descriptor)
        finally:
            os.close(descriptor)
        if matched:
            break
        if attempt:
            raise ValueError("The verified scratch changed before its permissions were sealed.")
        before = _candidate_storage_rebind(
            candidate, target, before, file_sha256, history=history,
            guard=publication_guard, inputs=inputs, phase="factory_before_permissions", audit=rebindings,
        )
    identity = _candidate_storage_rebind(
        candidate, target, sealed_identity, file_sha256, history=history,
        guard=publication_guard, inputs=inputs, phase="factory_after_permissions", audit=rebindings,
        owner_only=True,
    )
    if _candidate_owner_problem(candidate):
        raise ValueError("The reviewed Mac bank candidate must remain owner-only.")
    _no_baseline_copy_handles(candidate)
    identity = _candidate_storage_rebind(
        candidate, target, identity, file_sha256, history=history,
        guard=publication_guard, inputs=inputs, phase="factory_before_authorization", audit=rebindings,
        owner_only=True,
    )
    if _baseline_copy_file(candidate, target) != identity:
        raise ValueError("The verified scratch changed before baseline reuse was authorized.")
    allocated = getattr(candidate.lstat(), "st_blocks", 0) * 512
    return _BaselineCopyCapability(
        candidate.resolve(), history, publication_guard, inputs, identity, metadata, schema, allocated,
        file_sha256, rebindings,
    )


def _require_baseline_copy(capability, history, candidate, guard, *, allow_rebind=True):
    if (
        type(capability) is not _BaselineCopyCapability
        or capability.used or capability.pid != os.getpid()
        or capability.thread != get_ident() or capability.guard is not guard
        or capability.history != history.resolve()
        or capability.path != candidate.resolve()
    ):
        raise ValueError("The baseline reuse capability is inactive or out of scope.")
    _require_publication_guard(guard, history)
    target = history.with_name("macos_recovery.sqlite3")
    if _inputs(history) != capability.inputs:
        raise ValueError("The verified baseline or canonical inputs changed before reuse.")
    capability.identity = _candidate_storage_rebind(
        candidate, target, capability.identity, capability.file_sha256,
        history=history, guard=guard, inputs=capability.inputs, phase="baseline_reuse",
        audit=capability.storage_rebindings, allow_rebind=allow_rebind, owner_only=True,
    )
    if (
        _baseline_copy_file(candidate, target) != capability.identity
        or _candidate_owner_problem(candidate)
    ):
        raise ValueError("The verified baseline or canonical inputs changed before reuse.")
    capability.allocated_bytes = getattr(candidate.lstat(), "st_blocks", 0) * 512
    return capability


@contextmanager
def _staging_output(candidate, history, baseline_copy, publication_guard, *, cloned=False):
    if baseline_copy is None:
        if not cloned:
            descriptor = os.open(
                candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600,
            )
            os.close(descriptor)
            try:
                ensure_owner_only(candidate)
            except OSError:
                candidate.unlink(missing_ok=True)
                raise
        path = candidate.absolute().as_uri() + "?mode=rw"
        with closing(sqlite3.connect(path, uri=True)) as output:
            yield output
        return
    cap = _require_baseline_copy(baseline_copy, history, candidate, publication_guard)
    _no_baseline_copy_handles(candidate)
    with closing(sqlite3.connect(
        candidate.resolve().as_uri() + "?mode=rw", uri=True, timeout=0,
    )) as output:
        output.execute("PRAGMA locking_mode=EXCLUSIVE")
        output.execute("PRAGMA synchronous=OFF")
        output.execute("BEGIN EXCLUSIVE")
        _require_baseline_copy(cap, history, candidate, publication_guard, allow_rebind=False)
        cap.used = True
        # Exclusive locking mode retains the file lease across the staging code's
        # existing commits and temporary-schema setup, until this connection closes.
        yield output


def _stage_message_table(repository, connection, history_identity, conversation_id):
    _conversations, messages, status = repository._timeline_tables(
        connection, history_identity, conversation_id=conversation_id, strict=True
    )
    if status not in {"ready", "absent"}:
        raise ValueError("The existing source timeline is incomplete before Mac bank staging.")
    return messages


def _no_stage_cancellation():
    """Keep the existing offline staging path independent of service cancellation."""


@contextmanager
def _cancellable_sqlite(connection, check_cancel):
    interrupted = []

    def checkpoint():
        try:
            check_cancel()
        except Exception as error:
            interrupted.append(error)
            return 1
        return 0

    connection.set_progress_handler(checkpoint, 100_000)
    try:
        yield
    except (sqlite3.OperationalError, ValueError):
        if interrupted:
            raise interrupted[0] from None
        raise
    finally:
        connection.set_progress_handler(None, 0)


@contextmanager
def _scoped_staging_timeline(repository, history_uri, history_identity, session, check_cancel):
    # Overlay attachments create fixed aliases and temporary views. A fresh
    # connection keeps those source proofs pinned to this one conversation.
    with closing(sqlite3.connect(history_uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA temp_store=MEMORY")
        with _cancellable_sqlite(connection, check_cancel):
            messages = _stage_message_table(repository, connection, history_identity, session)
            check_cancel()
            yield connection, messages


def _stage_comparison_scope(connection, output, messages, session, catalog_hashes, check_cancel):
    for index, observed in enumerate(connection.execute(
        f"SELECT * FROM {messages} WHERE conversation_id=?", (session,)
    )):
        if index >= MAX_SCOPE_MESSAGES:
            raise ValueError("Mac comparison scope exceeds its bounded row budget.")
        if index % 256 == 0:
            check_cancel()
        proof = json.loads(observed["provenance"])
        server = proof.get("server_id")
        if server is None or re.fullmatch(r"[1-9][0-9]{0,18}", str(server)) is None:
            continue
        store = (
            "macos_bank"
            if output.execute("SELECT 1 FROM messages WHERE id=?", (observed["id"],)).fetchone()
            else "existing_timeline"
        )
        output.execute(
            "INSERT OR IGNORE INTO comparison_events VALUES(?,?,?,?,?)",
            (session, str(server), observed["id"], _event_signature(observed), store),
        )
        if store != "macos_bank":
            stub_source = _stub_source(connection, observed, catalog_hashes)
            if stub_source is not None:
                output.execute(
                    "INSERT OR IGNORE INTO comparison_stubs VALUES(?,?,?,?)",
                    (observed["id"], *stub_source, _json(dict(observed))),
                )


def _clone_verified_bank(history, target, candidate, inputs, cancel):
    """Clone one verified bank into an independent private file before opening it writable."""
    if sys.platform != "darwin" or not _private_candidate_workspace(candidate):
        return False
    try:
        clone = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True).fclonefileat
    except (AttributeError, OSError):
        return False
    clone.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int)
    clone.restype = ctypes.c_int
    try:
        source_identity = _publication_candidate_file(target)
    except ValueError:
        return False
    if source_identity != inputs[target.name]:
        raise ValueError("The original Mac bank changed before cloning.")

    flags = os.O_RDONLY | os.O_NOFOLLOW
    with os.fdopen(os.open(target, flags), "rb") as source:
        directory_fd = os.open(candidate.parent, flags | os.O_DIRECTORY)
        try:
            directory = os.fstat(directory_fd)
            if directory.st_dev != source_identity["dev"]:
                return False

            def source_unchanged():
                observed = os.fstat(source.fileno())
                return (
                    all(getattr(observed, "st_" + key) == value for key, value in source_identity.items())
                    and _inputs(history) == inputs
                )

            _validated_bank_metadata(target, history, source_identity, inputs)
            cancel()
            if not source_unchanged():
                raise ValueError("The original Mac bank changed before cloning.")
            if clone(source.fileno(), directory_fd, os.fsencode(candidate.name), _CLONE_NOOWNERCOPY) != 0:
                code = ctypes.get_errno()
                if (
                    code in {errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV}
                    and not os.path.lexists(candidate)
                ):
                    return False
                raise OSError(code, "Could not clone the verified Mac recovery bank.")
            result = _publication_candidate_file(candidate)
            opened_result = os.stat(candidate.name, dir_fd=directory_fd, follow_symlinks=False)
            if (
                not source_unchanged()
                or (result["dev"], result["ino"]) != (opened_result.st_dev, opened_result.st_ino)
                or result["ino"] == source_identity["ino"]
                or any(result[key] != source_identity[key] for key in ("dev", "size", "mtime_ns"))
                or not _private_candidate_workspace(candidate)
            ):
                raise ValueError("The cloned Mac bank did not retain its verified baseline.")
            cancel()
            return True
        finally:
            os.close(directory_fd)


def stage_mac_recovery_bank(
    local_store_root, packets, candidate_path, *, account_ids, progress=None,
    baseline_copy=None, publication_guard=None, conversation_id=None,
    scope_comparison=False, check_cancel=None, clone_baseline=False,
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
    history_uri = history.resolve().as_uri() + "?mode=ro&immutable=1"
    inputs = _inputs(history)
    candidate = Path(candidate_path).expanduser()
    target = mac_recovery_bank_path(local_store_root)
    if (
        (baseline_copy is None and candidate.exists())
        or candidate.is_symlink()
        or candidate.resolve() in {history.resolve(), target.resolve()}
    ):
        raise ValueError("Mac recovery bank staging requires a new candidate path.")
    if baseline_copy is not None:
        _require_baseline_copy(baseline_copy, history, candidate, publication_guard)
    if not candidate.parent.is_dir() or candidate.parent.is_symlink():
        raise ValueError(
            "The bank candidate needs an existing local staging directory."
        )
    report = {
        "added": 0,
        "upgraded": 0,
        "reparsed": 0,
        "existing": 0,
        "occurrences_added": 0,
        "variants": 0,
        "conflicts": 0,
        "scopes": 0,
    }
    processed_scopes, source_scopes, types = set(), set(), Counter()
    catalog_hashes = {}
    cancel = check_cancel or _no_stage_cancellation
    cloned = (
        clone_baseline is True and baseline_copy is None and inputs[target.name] is not None
        and _clone_verified_bank(history, target, candidate, inputs, cancel)
    )
    with (
        closing(sqlite3.connect(history_uri, uri=True)) as original,
        _staging_output(candidate, history, baseline_copy, publication_guard, cloned=cloned) as output,
        _cancellable_sqlite(output, check_cancel) if check_cancel is not None else nullcontext(),
    ):
        original.row_factory = sqlite3.Row
        original.execute("PRAGMA temp_store=MEMORY")
        if target.exists():
            if not cloned:
                with closing(
                    sqlite3.connect(
                        target.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
                    )
                ) as previous:
                    if baseline_copy is None:
                        _validated_bank_metadata(target, history, inputs[target.name], inputs)
                        if check_cancel is None:
                            previous.backup(output)
                        else:
                            check_cancel()
                            previous.backup(output, pages=1024, progress=lambda *_args: check_cancel())
                    else:
                        _validate_bank(previous, history, inputs)
                        if (
                            dict(previous.execute("SELECT key,value FROM archive_info")) != baseline_copy.metadata
                            or _baseline_schema(previous) != baseline_copy.schema
                            or _inputs(history) != baseline_copy.inputs
                        ):
                            raise ValueError("The canonical baseline changed before scratch reuse.")
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
        if baseline_copy is None:
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
        history_identity = _file_identity(history)
        if not scope_comparison:
            message_table = _stage_message_table(
                repository, original, history_identity, conversation_id,
            )
        for packet in packets:
            cancel()
            _validate_packet(packet, accounts)
            session = packet["session_id"]
            if conversation_id is not None and session != conversation_id:
                raise ValueError("A Mac recovery packet is outside the selected conversation.")
            conversation = _binding(original, packet)
            if session not in processed_scopes:
                timeline = (
                    _scoped_staging_timeline(repository, history_uri, history_identity, session, cancel)
                    if scope_comparison else nullcontext((original, message_table))
                )
                with timeline as (connection, messages):
                    _stage_comparison_scope(
                        connection, output, messages, session, catalog_hashes, cancel,
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
            for index, message in enumerate(packet["messages"]):
                if index % 256 == 0:
                    cancel()
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
                    "SELECT * FROM occurrences WHERE id=?",
                    (locator_key,),
                ).fetchone()
                if existing_occurrence is not None:
                    if (
                        existing_occurrence["content_sha256"]
                        != message["content_sha256"]
                    ):
                        raise ValueError(
                            "One preserved Mac source row changed after its prior recovery."
                        )
                    if existing_occurrence["signature"] != signature:
                        if reparse.current_projection_replay(
                            output, existing_occurrence, packet, message, signature
                        ):
                            report["existing"] += 1
                            continue
                        repaired = reparse.apply(
                            output,
                            existing_occurrence["canonical_id"],
                            projected,
                            source_key,
                        )
                        if repaired is None or not reparse.historical_occurrence(
                            output, existing_occurrence
                        ):
                            raise ValueError(
                                "One preserved Mac source row changed outside its semantic revision history."
                            )
                        if repaired == "reparsed":
                            report["reparsed"] += 1
                        current_signature = output.execute(
                            "SELECT signature FROM event_keys WHERE message_id=?",
                            (existing_occurrence["canonical_id"],),
                        ).fetchone()[0]
                        output.execute(
                            "UPDATE comparison_events SET signature=? "
                            "WHERE conversation_id=? AND server_id=? AND message_id=?",
                            (
                                current_signature, session, message["server_id"],
                                existing_occurrence["canonical_id"],
                            ),
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
                if (
                    conflict
                    and len(matches) == 1
                    and matches[0]["canonical_store"] == "macos_bank"
                ):
                    canonical_id = matches[0]["message_id"]
                    repaired = reparse.apply(
                        output, canonical_id, projected, source_key
                    )
                    if repaired is not None:
                        current_signature = output.execute(
                            "SELECT signature FROM event_keys WHERE message_id=?",
                            (canonical_id,),
                        ).fetchone()[0]
                        output.execute(
                            "UPDATE comparison_events SET signature=? "
                            "WHERE conversation_id=? AND server_id=? AND message_id=?",
                            (current_signature, session, message["server_id"], canonical_id),
                        )
                        store = (
                            "macos_bank_shadow"
                            if output.execute(
                                "SELECT 1 FROM shadow_replacements WHERE original_id=?",
                                (canonical_id,),
                            ).fetchone()
                            else "macos_bank"
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
                        report["occurrences_added"] += 1
                        report[
                            "reparsed" if repaired == "reparsed" else "existing"
                        ] += 1
                        continue
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
                    if "clock_observations" in upgrade:
                        catalog_clock = upgrade["clock_observations"]["catalog"]
                        for position, key in enumerate(_STUB_CLOCK_FIELDS, 7):
                            projected[position] = catalog_clock[key]
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
        summary_source = "selected_scopes" if scope_comparison else "conversations"
        for session, in output.execute(f"SELECT id FROM {summary_source}"):
            cancel()
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
            "schema_version": "4" if reparse._has_table(output) else "3",
            "code_version": _writer_version(output),
            "input_identities": _json(
                {key: value for key, value in inputs.items() if key != target.name}
            ),
            "account_ids": _json(retained_accounts),
            "payload_sha256": _rows_digest(output, check_cancel=check_cancel),
        }
        output.executemany(
            "INSERT OR REPLACE INTO archive_info VALUES(?,?)", metadata.items()
        )
        output.commit()
        cancel()
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
    cancel()
    if _inputs(history) != inputs:
        raise ValueError("A source archive changed during Mac recovery bank staging.")
    return {
        **report,
        "candidate_path": str(candidate),
        "candidate_sha256": _file_sha(candidate, check_cancel=check_cancel),
        "candidate_identity": _identity(candidate),
        "input_identities": inputs,
        "account_ids": list(accounts),
        **({"baseline_copy": baseline_copy.summary} if baseline_copy is not None else {}),
    }


class _ParallelBankChanged(ValueError):
    """An independent check observed a different file or source identity."""


def _validate_revision_proofs(connection):
    if connection.execute(
        "SELECT 1 FROM projection_revisions WHERE reason='native_pat_projection' LIMIT 1"
    ).fetchone():
        reparse.validate(connection)
    else:
        schema4_reader.validate(connection)


def _validate_bank_job(job, bank_path, history, identity, inputs):
    """Run one independent proof over the same immutable bank."""
    path, history = Path(bank_path), Path(history)
    if _identity(path) != identity or _inputs(history) != inputs:
        raise _ParallelBankChanged("The Mac recovery bank changed before worker validation.")
    with closing(
        sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    ) as connection:
        if job == "payload":
            result = _rows_digest(connection)
        elif job == "reparse":
            result = _validate_revision_proofs(connection)
        elif job == "clocks":
            result = _validate_stub_clocks(connection, history)
        else:
            raise ValueError("Unsupported Mac recovery bank validation job.")
    if _identity(path) != identity or _inputs(history) != inputs:
        raise _ParallelBankChanged("The Mac recovery bank changed during worker validation.")
    return result


def _parallel_bank_checks(
    connection, history, schema_version, inputs, *, candidate_pin=None, check_cancel=None,
):
    """Bound independent proofs to a published bank or a closed, pinned candidate."""
    if connection.in_transaction or (
        check_cancel is not None and not hasattr(ProcessPoolExecutor, "terminate_workers")
    ):
        return None
    if candidate_pin is None:
        path = Path(history).with_name("macos_recovery.sqlite3")
        identity = _identity(path, optional=True)
    else:
        path, identity = candidate_pin
    if identity is None or identity["size"] < _MIN_PARALLEL_BANK_BYTES:
        return None
    database = next(
        (row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main"),
        "",
    )
    if not database or Path(database).resolve() != path.resolve():
        return None
    jobs = ("payload", "reparse", "clocks") if schema_version == "4" else ("payload", "clocks")
    workers = min(
        _MAX_VALIDATION_WORKERS,
        len(jobs),
        resolve_cpu_process_workers(workload_size=len(jobs)),
    )
    if workers <= 1:
        return None
    if _identity(path) != identity or _inputs(Path(history)) != inputs:
        raise _ParallelBankChanged("The Mac recovery inputs changed before parallel validation.")
    if check_cancel is not None:
        check_cancel()
    try:
        executor = ProcessPoolExecutor(max_workers=workers, mp_context=get_context("spawn"))
    except (OSError, BrokenProcessPool):
        return None
    try:
        with executor:
            futures = [
                executor.submit(_validate_bank_job, job, str(path), str(history), identity, inputs)
                for job in jobs
            ]
            if check_cancel is not None:
                pending = set(futures)
                try:
                    while pending:
                        check_cancel()
                        done, pending = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
                        for future in done:
                            future.result()
                    check_cancel()
                except BaseException:
                    executor.terminate_workers()
                    raise
            results = [future.result() for future in futures]
    except BrokenProcessPool:
        # A failed process transport may be retried locally; proof failures propagate.
        return None
    if _identity(path) != identity or _inputs(Path(history)) != inputs:
        raise _ParallelBankChanged("The Mac recovery bank changed during parallel validation.")
    return results[0]


def _validate_bank(
    connection, history, current_inputs=None, *, allow_parallel=False,
    candidate_pin=None, check_cancel=None,
):
    global _SCHEMA_CONTRACT
    metadata = dict(connection.execute("SELECT key,value FROM archive_info"))
    schema_version = metadata.get("schema_version")
    supported = {
        "1": ("v1.0.0-codex.0",),
        "2": ("v1.1.0-codex.0",),
        "3": (
            "v1.2.0-codex.0",
            "v1.2.1-codex.0",
            "v1.2.2-codex.0",
            "v1.3.0-codex.0",
            "v1.4.0-codex.0",
            "v1.4.1-codex.0",
            "v1.4.2-codex.0",
            "v1.4.3-codex.0",
            "v1.5.0-codex.0",
            VERSION,
        ),
        "4": (
            "v1.4.0-codex.0",
            "v1.4.1-codex.0",
            "v1.4.2-codex.0",
            "v1.4.3-codex.0",
            "v1.5.0-codex.0",
            VERSION,
        ),
    }
    if (
        schema_version not in supported
        or metadata.get("code_version") not in supported[schema_version]
    ):
        raise ValueError("Unsupported Mac recovery bank schema.")
    if schema_version not in _SCHEMA_CONTRACT:
        with closing(sqlite3.connect(":memory:")) as expected_schema:
            expected_schema.executescript(
                _SCHEMA_V1
                if schema_version == "1"
                else _SCHEMA_V4
                if schema_version == "4"
                else _SCHEMA
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
    dependencies = {
        key: value for key, value in actual.items() if key != "macos_recovery.sqlite3"
    }
    if expected != dependencies:
        from .wechat_archive_relocation import matches

        database = next(
            (row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main"), ""
        )
        if (
            not database
            or Path(database).resolve() != Path(history).with_name("macos_recovery.sqlite3").resolve()
            or not matches(history, "macos_recovery.sqlite3", expected, dependencies)
        ):
            raise ValueError(
                "The Mac recovery bank is stale against its original timeline inputs."
            )
    payload_digest = (
        _parallel_bank_checks(
            connection, history, schema_version, actual,
            candidate_pin=candidate_pin, check_cancel=check_cancel,
        )
        if allow_parallel
        else None
    )
    parallel = payload_digest is not None
    if not parallel:
        payload_digest = (
            _rows_digest(connection) if check_cancel is None
            else _rows_digest(connection, check_cancel=check_cancel)
        )
    if metadata.get("payload_sha256") != payload_digest:
        raise ValueError(
            "The Mac recovery bank payload differs from its sealed manifest."
        )
    if not parallel and schema_version == "4":
        _validate_revision_proofs(connection)
    if not parallel:
        _validate_stub_clocks(connection, history)
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
            yield verify
        finally:
            unlock_file(handle)


class _PublicationGuard:
    """Keep an active lock capability local to its creating process and thread."""

    __slots__ = ("history", "pid", "thread", "active", "verify")

    def __init__(self, history, verify):
        self.history = Path(history).resolve()
        self.pid = os.getpid()
        self.thread = get_ident()
        self.active = True
        self.verify = verify


def _require_publication_guard(guard, history):
    """Reject a borrowed or expired capability before waiting on the process lock."""
    if (
        type(guard) is not _PublicationGuard
        or not guard.active
        or guard.pid != os.getpid()
        or guard.thread != get_ident()
        or guard.history != Path(history).resolve()
    ):
        raise ValueError("The Mac recovery publication guard is inactive or out of scope.")
    guard.verify()
    return guard


@contextmanager
def _publication_scope(history, guard=None):
    if guard is not None:
        _require_publication_guard(guard, history)
        with _LOCK:
            _require_publication_guard(guard, history)
            yield guard
        return
    with _LOCK, _publication_lock(history) as verify:
        acquired = _PublicationGuard(history, verify)
        try:
            yield acquired
        finally:
            acquired.active = False


@contextmanager
def mac_recovery_publication_guard(local_store_root):
    """Hold publication exclusion across a staged rebase and its final readback."""
    with _publication_scope(wechat_archive_path(local_store_root)) as guard:
        yield guard


def publish_mac_recovery_bank(
    local_store_root, candidate_path, receipt, *, before_replace=None,
    publication_guard=None, reuse_validation=False, check_cancel=None,
):
    """Recheck staged inputs and optional cancellation before the atomic publication."""
    from . import wechat_macos_bank_validation_cache as proof_cache

    history = wechat_archive_path(local_store_root)
    candidate = Path(candidate_path)
    target = mac_recovery_bank_path(local_store_root)
    with _publication_scope(history, publication_guard) as active_guard:
        if str(candidate) != receipt["candidate_path"]:
            raise ValueError(
                "The Mac recovery bank candidate differs from its staging receipt."
            )
        rebindings = []
        storage_target = target if target.exists() else history
        _publication_candidate_file(candidate)
        candidate_identity = _candidate_storage_rebind(
            candidate, storage_target, receipt["candidate_identity"], receipt["candidate_sha256"],
            history=history, guard=active_guard, inputs=receipt["input_identities"],
            phase="publication_before_validation", audit=rebindings, owner_only=True,
            check_cancel=check_cancel,
        )
        _no_baseline_copy_handles(candidate)
        # A recorded rebind already verified the complete candidate under its new pin.
        if not rebindings and _file_sha(candidate, check_cancel=check_cancel) != receipt["candidate_sha256"]:
            raise ValueError("The Mac recovery bank candidate differs from its staging receipt.")
        candidate_identity = _candidate_storage_rebind(
            candidate, storage_target, candidate_identity, receipt["candidate_sha256"],
            history=history, guard=active_guard, inputs=receipt["input_identities"],
            phase="publication_before_validation", audit=rebindings, owner_only=True,
            check_cancel=check_cancel,
        )
        validation_pin = proof_cache.proof_pin(
            target, history, candidate_identity, receipt["input_identities"],
        ) if reuse_validation else None
        for allow_parallel in (True, False):
            try:
                with closing(
                    sqlite3.connect(
                        candidate.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
                    )
                ) as connection:
                    progress = (
                        _cancellable_sqlite(connection, check_cancel)
                        if check_cancel is not None else nullcontext()
                    )
                    with progress:
                        metadata = _validate_bank(
                            connection, history, receipt["input_identities"],
                            allow_parallel=allow_parallel,
                            candidate_pin=(candidate, candidate_identity),
                            check_cancel=check_cancel,
                        )
                break
            except _ParallelBankChanged:
                if not allow_parallel:
                    raise
                rebound = _candidate_storage_rebind(
                    candidate, storage_target, candidate_identity, receipt["candidate_sha256"],
                    history=history, guard=active_guard, inputs=receipt["input_identities"],
                    phase="publication_during_validation", audit=rebindings, owner_only=True,
                    check_cancel=check_cancel,
                )
                if rebound == candidate_identity:
                    raise
                candidate_identity = rebound
        _publication_candidate_file(candidate)
        sync_completion_file(candidate)
        candidate_identity = _candidate_storage_rebind(
            candidate, storage_target, candidate_identity, receipt["candidate_sha256"],
            history=history, guard=active_guard, inputs=receipt["input_identities"],
            phase="publication_after_validation", audit=rebindings, owner_only=True,
            check_cancel=check_cancel,
        )
        if before_replace is not None:
            before_replace()
        _require_publication_guard(active_guard, history)
        _no_baseline_copy_handles(candidate)
        candidate_identity = _candidate_storage_rebind(
            candidate, storage_target, candidate_identity, receipt["candidate_sha256"],
            history=history, guard=active_guard, inputs=receipt["input_identities"],
            phase="publication_before_replace", audit=rebindings, owner_only=True,
            check_cancel=check_cancel,
        )
        if _publication_candidate_file(candidate) != candidate_identity:
            raise ValueError("The candidate changed immediately before publication.")
        os.replace(candidate, target)
        if reuse_validation:
            _remember_published_bank(
                history, target, candidate_identity, receipt, metadata, validation_pin,
            )
        return {
            **receipt,
            "bank_path": str(target),
            "bank_sha256": receipt["candidate_sha256"],
            "candidate_storage_rebindings": rebindings,
            "published_candidate_identity": candidate_identity,
        }


def verify_published_account_refresh(local_store_root, owner, receipt):
    """Verify the just-published bank and one owner receipt without building a timeline."""
    history = wechat_archive_path(local_store_root)
    path = mac_recovery_bank_path(local_store_root)
    inputs = _inputs(history)
    identity = inputs[path.name]
    previous = receipt["input_identities"]
    published = receipt["published_candidate_identity"]
    if (
        identity is None
        or receipt["account_ids"] != [owner]
        or any(identity[key] != published[key] for key in ("dev", "ino", "size", "mtime_ns"))
        or {name: value for name, value in inputs.items() if name != path.name}
        != {name: value for name, value in previous.items() if name != path.name}
    ):
        raise ValueError("The published account refresh does not match its staging receipt.")
    # Reuse the complete validation pinned at publication when possible. A
    # changed file gets fresh verification through the ordinary bank validator.
    _validated_bank_metadata(path, history, identity, inputs)
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        if connection.execute(
            "SELECT 1 FROM sources WHERE account_id=? LIMIT 1", (owner,),
        ).fetchone() is None:
            raise ValueError("The published bank has no source receipt for the selected account.")
    if _inputs(history) != inputs:
        raise ValueError("The published account refresh changed during readback.")


def _validation_key(path, identity, inputs):
    from .wechat_archive_relocation import receipt_identity

    return (
        str(path.resolve()), _json(identity), _json(inputs),
        _json(receipt_identity(path.with_name("history.sqlite3"))),
    )


def _remember_validated_bank(key, metadata):
    with _LOCK:
        if key not in _VALIDATED and len(_VALIDATED) >= 4:
            _VALIDATED.pop(next(iter(_VALIDATED)))
        _VALIDATED[key] = metadata


def _remember_published_bank(history, target, candidate_identity, receipt, metadata, validation_pin):
    """Reuse the completed candidate proof only for the exact published bytes."""
    from . import wechat_macos_bank_validation_cache as proof_cache

    try:
        published = _publication_candidate_file(target)
        if any(published[key] != candidate_identity[key]
               for key in ("dev", "ino", "size", "mtime_ns")):
            return
        inputs = {**receipt["input_identities"], target.name: published}
        if (
            _inputs(history) != inputs
            or _file_sha(target) != receipt["candidate_sha256"]
            or _inputs(history) != inputs
        ):
            return
        key = _validation_key(target, published, inputs)
        _remember_validated_bank(key, metadata)
        if validation_pin is not None:
            pin = proof_cache.proof_pin(target, history, published, inputs)
            if pin == {**validation_pin, "identity": published, "inputs": inputs}:
                proof_cache.write_proof(
                    target.parent / "browser_cache", pin, metadata,
                    lambda: _inputs(history) == inputs and _validation_key(target, published, inputs) == key,
                )
    except (OSError, ValueError):
        # The atomic publication is already complete. A later reader can verify it normally.
        return


def _validated_bank_metadata(path, history, identity, inputs):
    """Share the complete pinned proof between ordinary reads and staging."""
    key = _validation_key(path, identity, inputs)
    with _LOCK:
        metadata = _VALIDATED.get(key)
        pending = _VALIDATING.get(key) if metadata is None else None
        owner = metadata is None and pending is None
        if owner:
            pending = Future()
            _VALIDATING[key] = pending
    if metadata is None:
        if not owner:
            metadata = pending.result()
        else:
            try:
                from . import wechat_macos_bank_validation_cache as proof_cache

                directory = path.parent / "browser_cache"
                pin = proof_cache.proof_pin(path, history, identity, inputs)

                def unchanged():
                    return (
                        _identity(path) == identity and _inputs(Path(history)) == inputs
                        and _validation_key(path, identity, inputs) == key
                    )

                metadata = proof_cache.read_proof(directory, pin, unchanged)
                if metadata is None:
                    with closing(
                        sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
                    ) as verifier:
                        metadata = _validate_bank(verifier, history, inputs, allow_parallel=True)
                    if not unchanged():
                        raise ValueError("The Mac recovery bank changed during validation.")
                    proof_cache.write_proof(directory, pin, metadata, unchanged)
                if not unchanged():
                    raise ValueError("The Mac recovery bank changed during validation.")
                with _LOCK:
                    _remember_validated_bank(key, metadata)
                    _VALIDATING.pop(key, None)
                pending.set_result(metadata)
            except BaseException as error:
                with _LOCK:
                    _VALIDATING.pop(key, None)
                pending.set_exception(error)
                raise
    if (
        _identity(path) != identity or _inputs(Path(history)) != inputs
        or _validation_key(path, identity, inputs) != key
    ):
        raise ValueError("The Mac recovery bank changed during validation.")
    return metadata


def _attach(connection, history):
    history = Path(history)
    path = history.with_name("macos_recovery.sqlite3")
    identity = _identity(path, optional=True)
    if identity is None:
        return None
    inputs = _inputs(history)
    connection.execute(
        "ATTACH DATABASE ? AS macos_recovery_bank",
        (path.resolve().as_uri() + "?mode=ro&immutable=1",),
    )
    return _validated_bank_metadata(path, history, identity, inputs)


def _validate_shadow_rows(connection, alias, name, *, rowid_range=None):
    """Validate exact original rows without publishing any reusable scope state."""
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias) is None:
        raise ValueError("Unsupported original resource catalog alias.")
    row_filter = " AND s.rowid BETWEEN ? AND ?" if rowid_range is not None else ""
    count = 0
    for row in connection.execute(
        "SELECT s.original_text_sha256,s.original_provenance_sha256,s.original_row_sha256,"
        "s.proof AS original_upgrade_proof,b.provenance AS original_bank_provenance,"
        "b.sender_id AS original_bank_sender_id,b.direction AS original_bank_direction,m.* "
        "FROM macos_recovery_bank.shadow_replacements s CROSS JOIN "
        + alias
        + ".messages m JOIN macos_recovery_bank.messages b ON b.id=s.original_id "
        "WHERE s.original_id=m.id AND s.source_file_name=? "
        "AND s.conversation_id IN (SELECT id FROM temp.macos_recovery_bank_scopes)"
        + row_filter,
        (name, *rowid_range) if rowid_range is not None else (name,),
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
        upgrade = json.loads(row["original_upgrade_proof"])
        if upgrade.get("upgrade_basis") == _STUB_CLOCK_POLICY or "clock_observations" in upgrade:
            retained = {
                **original,
                "provenance": row["original_bank_provenance"],
                "sender_id": row["original_bank_sender_id"],
                "direction": row["original_bank_direction"],
            }
            _validate_stub_clock_receipt(
                connection, retained, upgrade, original=original,
                schema="macos_recovery_bank",
            )
    return count


def _validate_shadow_job(history, name, scopes, rowid_range, bank_identity, catalog_identity, inputs):
    """Check one disjoint range with fresh immutable handles and no cache publication."""
    history = Path(history)
    path = history.with_name("macos_recovery.sqlite3")
    if name not in {"resource_references.sqlite3", "resource_revalidated.sqlite3"}:
        raise ValueError("Unsupported original resource catalog.")
    catalog = history.with_name(name)

    def check_inputs():
        if (
            _identity(path) != bank_identity
            or _identity(catalog) != catalog_identity
            or _inputs(history) != inputs
        ):
            raise ValueError("A Mac shadow input changed during worker validation.")

    check_inputs()
    with closing(
        sqlite3.connect(history.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        for alias, database in (("macos_recovery_bank", path), ("shadow_catalog", catalog)):
            connection.execute(
                f"ATTACH DATABASE ? AS {alias}",
                (database.resolve().as_uri() + "?mode=ro&immutable=1",),
            )
        connection.execute(
            "CREATE TEMP TABLE macos_recovery_bank_scopes(id TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        connection.executemany(
            "INSERT INTO temp.macos_recovery_bank_scopes VALUES(?)",
            ((identifier,) for identifier in scopes),
        )
        count = _validate_shadow_rows(connection, "shadow_catalog", name, rowid_range=rowid_range)
    check_inputs()
    return count


def _parallel_shadow_count(connection, history, name, expected_count, catalog_identity, inputs):
    """Partition a large published scope by the closed schema's ordinary row IDs."""
    if expected_count < _MIN_PARALLEL_SHADOW_ROWS:
        return None
    workers = min(_MAX_VALIDATION_WORKERS, resolve_cpu_process_workers(workload_size=expected_count))
    if workers <= 1:
        return None
    history = Path(history)
    path = history.with_name("macos_recovery.sqlite3")
    databases = {row[1]: row[2] for row in connection.execute("PRAGMA database_list")}
    if (
        not databases.get("main")
        or Path(databases["main"]).resolve() != history.resolve()
        or not databases.get("macos_recovery_bank")
        or Path(databases["macos_recovery_bank"]).resolve() != path.resolve()
    ):
        return None
    identity = _identity(path)
    scopes = tuple(row[0] for row in connection.execute("SELECT id FROM temp.macos_recovery_bank_scopes"))
    first, last = connection.execute(
        "SELECT MIN(rowid),MAX(rowid) FROM macos_recovery_bank.shadow_replacements "
        "WHERE source_file_name=? AND conversation_id IN "
        "(SELECT id FROM temp.macos_recovery_bank_scopes)",
        (name,),
    ).fetchone()
    if first is None:
        return 0
    width = (last - first + workers) // workers
    ranges = tuple((start, min(last, start + width - 1)) for start in range(first, last + 1, width))
    try:
        executor = ProcessPoolExecutor(max_workers=workers, mp_context=get_context("spawn"))
    except (OSError, BrokenProcessPool):
        return None
    try:
        with executor:
            futures = [
                executor.submit(
                    _validate_shadow_job, str(history), name, scopes, interval,
                    identity, catalog_identity, inputs,
                )
                for interval in ranges
            ]
            count = sum(future.result() for future in futures)
    except BrokenProcessPool:
        return None
    if _identity(path) != identity or _inputs(history) != inputs:
        raise ValueError("A Mac shadow input changed during parallel validation.")
    return count


def _validate_shadow_sources(connection, history, *, conversation_id=None):
    """Recheck exact original catalog rows before exposing their monotone shadows."""
    path = Path(history).with_name("macos_recovery.sqlite3")
    inputs = _inputs(Path(history))
    base_key = (str(path.resolve()), _json(_identity(path)), _json(inputs))
    key = (*base_key, conversation_id)
    with _LOCK:
        if key in _SHADOW_VALIDATED or (*base_key, None) in _SHADOW_VALIDATED:
            return
        pending = _SHADOW_VALIDATING.get((*base_key, None)) or _SHADOW_VALIDATING.get(key)
        owner = pending is None
        if owner:
            pending = Future()
            _SHADOW_VALIDATING[key] = pending
    if not owner:
        pending.result()
        if _inputs(Path(history)) != inputs:
            raise ValueError("A Mac shadow input changed during source validation.")
        return
    try:
        _validate_shadow_scope(connection, history, inputs)
        if _inputs(Path(history)) != inputs:
            raise ValueError("A Mac shadow input changed during source validation.")
        with _LOCK:
            if len(_SHADOW_VALIDATED) >= 16:
                _SHADOW_VALIDATED.pop(next(iter(_SHADOW_VALIDATED)))
            _SHADOW_VALIDATED[key] = True
            _SHADOW_VALIDATING.pop(key, None)
        pending.set_result(True)
    except BaseException as error:
        with _LOCK:
            _SHADOW_VALIDATING.pop(key, None)
        pending.set_exception(error)
        raise


def _validate_shadow_scope(connection, history, inputs):
    """Retain full-file receipts and membership counts around independent row proofs."""
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
        count = _parallel_shadow_count(
            connection, history, name, expected_count, catalog_identity, inputs
        )
        if count is None:
            count = _validate_shadow_rows(connection, alias, name)
        if count != expected_count or _identity(catalog) != catalog_identity:
            raise ValueError(
                "The original resource stub membership changed during validation."
            )
    if _inputs(Path(history)) != inputs:
        raise ValueError("A Mac shadow input changed during source validation.")


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
        if metadata["schema_version"] in {"2", "3", "4"}:
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
            f"CREATE TEMP TABLE macos_recovery_bank_input_conversations AS SELECT * FROM {conversation_table}"
        )
        connection.execute(
            "CREATE INDEX temp.macos_recovery_bank_input_id ON macos_recovery_bank_input_conversations(id)"
        )
        connection.execute(
            "CREATE TEMP TABLE macos_recovery_bank_conversations AS "
            "SELECT * FROM temp.macos_recovery_bank_input_conversations WHERE 0"
        )
        connection.execute(
            "CREATE INDEX temp.macos_recovery_bank_summary_id ON macos_recovery_bank_conversations(id)"
        )
        for row in rows:
            existing = connection.execute(
                "SELECT * FROM temp.macos_recovery_bank_input_conversations WHERE id=?", (row["id"],)
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
            "CREATE TEMP VIEW macos_recovery_bank_timeline_conversations AS "
            "SELECT c.* FROM temp.macos_recovery_bank_input_conversations c "
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
