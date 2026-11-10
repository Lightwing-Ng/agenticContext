"""Retain source occurrences while projecting proven same-session text events.

Code version: v1.7.1-codex.0
"""

from __future__ import annotations

import base64
from collections import OrderedDict, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from threading import RLock
import zlib

from .wechat_contacts import WechatContactRepository, canonical_contact_id
from .wechat_conversation_quotes import VERSION as _QUOTE_VERSION
from .wechat_conversation_sequence import VERSION as _SEQUENCE_VERSION
from .wechat_incomplete_quotes import VERSION as _INCOMPLETE_QUOTE_VERSION
from .wechat_reviewed_quote_events import (
    VERSION as _REVIEWED_QUOTE_VERSION,
    bridge_reviewed_quotes,
    reviewed_quotes_digest,
    reviewed_quotes_path,
)
from .wechat_source_precedence import (
    VERSION as _SOURCE_PRECEDENCE_VERSION,
    monotone_pair_runs,
)


# The proof schema is unchanged by the exact-byte transport repair.
VERSION = "v1.6.2-codex.0"
COMPATIBLE_PROOF_VERSIONS = frozenset(
    {
        "v1.0.3-codex.0",
        "v1.1.0-codex.0",
        "v1.2.0-codex.0",
        "v1.3.0-codex.0",
        "v1.4.0-codex.0",
        "v1.4.1-codex.0",
        "v1.5.0-codex.0",
        "v1.6.0-codex.0",
        "v1.6.1-codex.0",
        VERSION,
    }
)
MAX_ROWS = 1_000_000
MAX_SOURCE_BYTES = 512 * 1024**2
MAX_CACHE_BYTES = 32 * 1024**2
MAX_COMPRESSED_CACHE_BYTES = 64 * 1024**2
MAX_DECODED_CACHE_BYTES = 256 * 1024**2
MAX_SUMMARY_SCAN_BYTES = 128 * 1024**2
_COMPRESSION_THRESHOLD_BYTES = 32 * 1024**2
_COMPRESSED_TRANSPORT = "zlib-json-v1"
_CACHE_ENCODING = "source-context-array-v1"
_CACHE_SOURCE_FIELDS = ("source_id", "source_snapshot_id", "source_label")
_CACHE_CONTEXT_FIELDS = (
    "id",
    "source_clock",
    "timestamp_value",
    "contact_id",
    "record_id",
    "sender_id",
    "sender_label",
)
_LOCK = RLock()
_READ_ONLY_PROJECTIONS = {}
_SUMMARY_LOCK = RLock()
_SUMMARY_CACHE = OrderedDict()
_SUMMARY_CACHE_LIMIT = 1_024
_ID = re.compile(r"[0-9a-f]{64}")
_SESSION = re.compile(r"wechat:([0-9a-f]{64})")
_TABLES = {
    "main.messages",
    "timeline_messages",
    "temp.timeline_messages",
    "placeholder_timeline_messages",
    "temp.placeholder_timeline_messages",
    "media_event_timeline_messages",
    "temp.media_event_timeline_messages",
    "macos_recovery_timeline_messages",
    "temp.macos_recovery_timeline_messages",
    "macos_recovery_bank_timeline_messages",
    "temp.macos_recovery_bank_timeline_messages",
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _identity(path, *, optional=False):
    try:
        value = path.stat()
    except FileNotFoundError:
        if optional:
            return None
        raise
    if (
        path.is_symlink()
        or not stat.S_ISREG(value.st_mode)
        or getattr(value, "st_flags", 0) & 0x40000000
        or any(
            Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")
        )
    ):
        raise ValueError("Conversation event inputs must be published local files.")
    return {
        "dev": value.st_dev,
        "inode": value.st_ino,
        "size": value.st_size,
        "mtime_ns": value.st_mtime_ns,
        "ctime_ns": value.st_ctime_ns,
    }


def _inputs(repository, history_identity=None):
    history = repository.path
    actual = _identity(history)
    if history_identity is not None and any(
        actual.get(key) != value for key, value in history_identity.items()
    ):
        raise ValueError("The history changed before event projection.")
    contacts = history.with_name("contacts.sqlite3")
    contact_identity = _identity(contacts)
    # Validate original paths before canonicalizing keys; resolving first hides symlinks.
    result = {str(history.resolve()): actual, str(contacts.resolve()): contact_identity}
    resource = history.with_name("resource_references.sqlite3")
    try:
        resource_identity = _identity(resource, optional=True)
    except ValueError:
        # An unavailable optional index must not hydrate or block plain-text views.
        value = resource.lstat()
        resource_identity = {
            "unavailable": True,
            "size": value.st_size,
            "mtime_ns": value.st_mtime_ns,
        }
    result[str(resource.resolve())] = resource_identity
    fallback = history.with_name("resource_revalidated.sqlite3")
    if fallback.exists() or fallback.is_symlink():
        try:
            fallback_identity = _identity(fallback)
        except ValueError:
            value = fallback.lstat()
            fallback_identity = {
                "unavailable": True,
                "size": value.st_size,
                "mtime_ns": value.st_mtime_ns,
            }
        result[str(fallback.resolve())] = fallback_identity
    placeholders = history.with_name("media_placeholders.json")
    if placeholders.exists():
        try:
            placeholder_identity = _identity(placeholders)
        except ValueError:
            value = placeholders.lstat()
            placeholder_identity = {
                "unavailable": True,
                "size": value.st_size,
                "mtime_ns": value.st_mtime_ns,
            }
        result[str(placeholders.resolve())] = placeholder_identity
    media_events = history.with_name("media_events.sqlite3")
    try:
        media_event_identity = _identity(media_events, optional=True)
    except ValueError:
        value = media_events.lstat()
        media_event_identity = {
            "unavailable": True,
            "size": value.st_size,
            "mtime_ns": value.st_mtime_ns,
        }
    result[str(media_events.resolve())] = media_event_identity
    recovery = history.with_name("macos_recovery.json")
    if recovery.exists() or recovery.is_symlink():
        try:
            recovery_identity = _identity(recovery)
        except ValueError:
            value = recovery.lstat()
            recovery_identity = {
                "unavailable": True,
                "size": value.st_size,
                "mtime_ns": value.st_mtime_ns,
            }
        result[str(recovery.resolve())] = recovery_identity
    bank = history.with_name("macos_recovery.sqlite3")
    if bank.exists() or bank.is_symlink():
        try:
            bank_identity = _identity(bank)
        except ValueError:
            value = bank.lstat()
            bank_identity = {
                "unavailable": True,
                "size": value.st_size,
                "mtime_ns": value.st_mtime_ns,
            }
        result[str(bank.resolve())] = bank_identity
    review_root = history.parent.parent.parent
    review_digest = reviewed_quotes_digest(review_root)
    review_path = reviewed_quotes_path(review_root)
    if review_path.is_symlink():
        raise ValueError("Conversation quote reviews cannot be symbolic links.")
    result[str(review_path.resolve())] = review_digest
    return result


def _fingerprint(inputs):
    payload = _json(
        [
            VERSION,
            _QUOTE_VERSION,
            _SEQUENCE_VERSION,
            _INCOMPLETE_QUOTE_VERSION,
            _REVIEWED_QUOTE_VERSION,
            _SOURCE_PRECEDENCE_VERSION,
            inputs,
        ]
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _directory(repository):
    return repository.path.with_name("conversation_events")


def _cache_path(repository, session):
    match = _SESSION.fullmatch(session) if isinstance(session, str) else None
    if match is None:
        raise ValueError("A canonical conversation is required.")
    return _directory(repository) / (match[1] + ".json")


def _pack_cache(value):
    """Intern repeated source metadata without changing any public proof fields."""
    groups = value.get("members_by_preferred")
    if (
        not isinstance(groups, dict)
        or "cache_encoding" in value
        or "context_sources" in value
    ):
        return value
    required = {*_CACHE_SOURCE_FIELDS, *_CACHE_CONTEXT_FIELDS}
    sources, source_indexes, packed = [], {}, {}
    for preferred, group in groups.items():
        if not isinstance(group, list):
            return value
        rows = []
        for context in group:
            if not isinstance(context, dict) or not required <= context.keys():
                # Legacy callers may supply only IDs. Preserve their original shape.
                return value
            source = [context[field] for field in _CACHE_SOURCE_FIELDS]
            key = _json(source)
            if key not in source_indexes:
                source_indexes[key] = len(sources)
                sources.append(source)
            row = [
                context["id"],
                source_indexes[key],
                *(context[field] for field in _CACHE_CONTEXT_FIELDS[1:]),
            ]
            extra = {key: item for key, item in context.items() if key not in required}
            if extra:
                row.append(extra)
            rows.append(row)
        packed[preferred] = rows
    return {
        **value,
        "cache_encoding": _CACHE_ENCODING,
        "context_sources": sources,
        "members_by_preferred": packed,
    }


def _unpack_cache(value):
    """Read the versioned array transport or an unchanged legacy JSON proof."""
    if not isinstance(value, dict):
        raise ValueError("A conversation event cache must be an object.")
    if "cache_encoding" not in value:
        if "context_sources" in value:
            raise ValueError("A compact event cache needs its encoding version.")
        return value
    sources = value.get("context_sources")
    groups = value.get("members_by_preferred")
    if (
        value["cache_encoding"] != _CACHE_ENCODING
        or not isinstance(sources, list)
        or any(not isinstance(source, list) or len(source) != 3 for source in sources)
        or not isinstance(groups, dict)
    ):
        raise ValueError("Unsupported compact conversation event cache.")
    reserved = {*_CACHE_SOURCE_FIELDS, *_CACHE_CONTEXT_FIELDS}
    unpacked = {}
    for preferred, group in groups.items():
        if not isinstance(group, list):
            raise ValueError("Compact conversation event members must be arrays.")
        contexts = []
        for row in group:
            if (
                not isinstance(row, list)
                or len(row) not in {8, 9}
                or type(row[1]) is not int
                or not 0 <= row[1] < len(sources)
                or len(row) == 9
                and (
                    not isinstance(row[8], dict)
                    or any(
                        not isinstance(key, str) or key in reserved for key in row[8]
                    )
                )
            ):
                raise ValueError("Invalid compact conversation event member.")
            context = {
                **dict(zip(_CACHE_SOURCE_FIELDS, sources[row[1]], strict=True)),
                **dict(zip(_CACHE_CONTEXT_FIELDS, [row[0], *row[2:8]], strict=True)),
                **(row[8] if len(row) == 9 else {}),
            }
            contexts.append(context)
        unpacked[preferred] = contexts
    return {
        **{
            key: item
            for key, item in value.items()
            if key not in {"cache_encoding", "context_sources"}
        },
        "members_by_preferred": unpacked,
    }


def _encode_cache(value):
    """Compress large proofs while preserving the ordinary JSON byte contract."""
    encoded = (_json(_pack_cache(value)) + "\n").encode("utf-8")
    if len(encoded) <= MAX_CACHE_BYTES:
        return encoded.decode("utf-8")
    if (
        len(encoded) <= _COMPRESSION_THRESHOLD_BYTES
        or len(encoded) > MAX_DECODED_CACHE_BYTES
    ):
        return None
    envelope = {
        "cache_transport": _COMPRESSED_TRANSPORT,
        "decoded_bytes": len(encoded),
        "decoded_sha256": hashlib.sha256(encoded).hexdigest(),
        "data": base64.b64encode(zlib.compress(encoded)).decode("ascii"),
    }
    transport = _json(envelope) + "\n"
    return (
        transport
        if len(transport.encode("utf-8")) <= MAX_COMPRESSED_CACHE_BYTES
        else None
    )


def _decode_cache(encoded):
    """Bound transport and expansion before parsing a compressed proof."""
    value = json.loads(encoded)
    if not isinstance(value, dict) or "cache_transport" not in value:
        if len(encoded) > MAX_CACHE_BYTES:
            raise ValueError("An ordinary event cache exceeds its byte budget.")
        return value
    size = value.get("decoded_bytes")
    digest = value.get("decoded_sha256")
    if (
        len(encoded) > MAX_COMPRESSED_CACHE_BYTES
        or set(value) != {"cache_transport", "decoded_bytes", "decoded_sha256", "data"}
        or value["cache_transport"] != _COMPRESSED_TRANSPORT
        or type(size) is not int
        or not 0 < size <= MAX_DECODED_CACHE_BYTES
        or not isinstance(digest, str)
        or _ID.fullmatch(digest) is None
        or not isinstance(value["data"], str)
    ):
        raise ValueError("Invalid compressed conversation event cache.")
    compressed = base64.b64decode(value["data"], validate=True)
    decoder = zlib.decompressobj()
    decoded = decoder.decompress(compressed, size + 1)
    if (
        len(decoded) != size
        or not decoder.eof
        or decoder.unused_data
        or decoder.unconsumed_tail
        or hashlib.sha256(decoded).hexdigest() != digest
    ):
        raise ValueError("Compressed event cache integrity does not agree.")
    result = json.loads(decoded)
    if isinstance(result, dict) and "cache_transport" in result:
        raise ValueError("Nested compressed event caches are unsupported.")
    return result


def _read_cache(path, fingerprint, session=None, *, expected_identity=None):
    try:
        before = _identity(path)
        if expected_identity is not None and before != expected_identity:
            return None
        limit = max(MAX_CACHE_BYTES, MAX_COMPRESSED_CACHE_BYTES)
        if before["size"] > limit:
            return None
        with path.open("rb") as stream:
            encoded = stream.read(before["size"] + 1)
        if len(encoded) != before["size"]:
            return None
        value = _unpack_cache(_decode_cache(encoded))
        if (
            _identity(path) != before
            or not isinstance(value, dict)
            or value.get("version") != VERSION
            or value.get("fingerprint") != fingerprint
            or session is not None
            and value.get("session") != session
            or _SESSION.fullmatch(value.get("session", "")) is None
            or type(value.get("original_count")) is not int
            or not isinstance(value.get("preferred_by_id"), dict)
            or not isinstance(value.get("members_by_preferred"), dict)
        ):
            return None
        mapping = value["preferred_by_id"]
        members = value["members_by_preferred"]
        for member, preferred in mapping.items():
            if (
                _ID.fullmatch(member) is None
                or _ID.fullmatch(preferred) is None
                or member == preferred
            ):
                return None
            group = members.get(preferred)
            if not isinstance(group, list) or {member, preferred} - {
                item.get("id") for item in group
            }:
                return None
        for preferred, group in members.items():
            if (
                _ID.fullmatch(preferred) is None
                or not isinstance(group, list)
                or len(group) < 2
                or len({item.get("id") for item in group}) != len(group)
                or any(
                    item.get("id") != preferred
                    and mapping.get(item.get("id")) != preferred
                    for item in group
                )
            ):
                return None
        if value.get("count_delta") != len(mapping) or value.get(
            "display_count"
        ) != value["original_count"] - len(mapping):
            return None
        return value
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        RecursionError,
        zlib.error,
    ):
        return None


def _empty(status, original_count=0):
    return {
        "status": status,
        "preferred_by_id": {},
        "members_by_preferred": {},
        "count_delta": 0,
        "original_count": original_count,
        "display_count": original_count,
    }


def _sequence_local_id(provenance, *, native):
    """Keep device-local numbers as scoped sequence evidence, never identity."""
    locator = provenance.get("locator", {})
    if not isinstance(locator, dict):
        return None
    if native:
        raw = locator.get("local_id")
    else:
        namespaces = [
            namespace
            for namespace in (
                locator.get("export_msgid_namespace"),
                provenance.get("export_msgid_namespace"),
            )
            if namespace is not None
        ]
        if not namespaces or any(value != "MesLocalID" for value in namespaces):
            return None
        raw = locator.get("export_msgid")
    if type(raw) is int:
        value = raw
    elif isinstance(raw, str) and re.fullmatch(r"[1-9][0-9]{0,18}", raw):
        value = int(raw)
    else:
        return None
    return value if 0 < value < 2**63 else None


def _unidentified_group_sender(row, provenance, local_id, kind):
    """Retain eligible unknown observations for a later independent event proof."""
    avatar = provenance.get("avatar_path_metadata")
    return (
        kind == "group"
        and provenance.get("text_extraction_kind") == "plain_text"
        and row["direction"] == "received"
        and row["sender_id"] in (None, "")
        and provenance.get("sender_export_id") in (None, "")
        and provenance.get("sender_native_id") in (None, "")
        and re.fullmatch(r"wxid_[A-Za-z0-9]+", row["sender_label"]) is not None
        and local_id is not None
        and isinstance(avatar, str)
        and avatar.endswith("/Portrait/DefaultAvatar.png")
    )


def _wall_clock(row, provenance, *, native):
    raw = provenance.get("source_time") or row["timestamp_value"]
    if (
        not isinstance(raw, str)
        or re.search(r"[T ]\d{2}:\d{2}:\d{2}(?:[.+\-Z]|$)", raw) is None
    ):
        return None
    try:
        stamp = datetime.fromisoformat(row["timestamp_value"])
        source = datetime.fromisoformat(raw)
        if (
            stamp.year <= 1970
            or stamp.microsecond
            or source.microsecond
            or stamp.replace(tzinfo=None) != source.replace(tzinfo=None)
            or stamp.tzinfo is not None
            and source.tzinfo is not None
            and stamp != source
        ):
            return None
        if native and (
            stamp.tzinfo is None
            or Decimal(str(provenance.get("unix_timestamp")))
            != Decimal(str(stamp.timestamp()))
        ):
            return None
    except (ValueError, TypeError, OverflowError, InvalidOperation):
        return None
    return stamp.replace(tzinfo=None).isoformat(timespec="seconds")


def _scan(repository, connection, session, current, message_table):
    """Inspect only the requested conversation and batch existing identity proofs."""
    from .wechat_contact_analysis import _routed_native_source
    from .wechat_conversation_quotes import quoted_event

    owner, peer = current.get("account_id"), current.get("peer_id")
    if (
        not isinstance(owner, str)
        or not owner
        or not isinstance(peer, str)
        or not peer
        or any(ch.isspace() for ch in peer)
        or current.get("account_key") != "account:" + owner
        or current.get("kind") not in {"direct", "group"}
    ):
        return []
    container = connection.execute(
        "SELECT * FROM main.partitions WHERE id=?", (current["partition_id"],)
    ).fetchone()
    if (
        container is None
        or container["account_key"] != current["account_key"]
        or container["account_id"] != owner
    ):
        return []
    if (
        not container["id"].startswith("html-")
        or container["identity_status"] != "Corroborated export ID"
    ):
        return []
    resolver = WechatContactRepository(repository.path.parent.parent.parent)
    snapshots, metadata_by_snapshot = {}, {}
    partitions = {container["id"]: dict(container)}
    results, count, size = [], 0, 0
    cursor = connection.execute(
        "SELECT m.* FROM "
        + message_table
        + " m WHERE m.conversation_id=? ORDER BY m.ordinal,m.id",
        (session,),
    )
    while batch := cursor.fetchmany(200):
        html, requests = [], []
        for source_row in batch:
            row = dict(source_row)
            count += 1
            size += len(row["text"].encode()) + len(row["provenance"].encode())
            if count > MAX_ROWS or size > MAX_SOURCE_BYTES:
                raise ValueError("Conversation event proof exceeds its bounded budget.")
            try:
                provenance = json.loads(row["provenance"])
                extraction = provenance.get("text_extraction_kind")
                quoted = extraction == "quoted_reply"
                if (
                    extraction != "plain_text"
                    and not quoted
                    or any(
                        provenance.get(key)
                        for key in (
                            "forwarded_components",
                            "resource_reference",
                        )
                    )
                    or quoted
                    and provenance.get("forwarded_references")
                    or not quoted
                    and any(
                        provenance.get(key)
                        for key in ("text_components", "quoted_components")
                    )
                    or row["direction"] not in {"sent", "received"}
                    or not row["text"].strip()
                ):
                    continue
                snapshot_key = provenance.get("source_snapshot")
                if type(snapshot_key) is not int:
                    continue
                if snapshot_key not in snapshots:
                    found = connection.execute(
                        "SELECT * FROM main.source_snapshots WHERE id=?",
                        (snapshot_key,),
                    ).fetchone()
                    snapshots[snapshot_key] = dict(found) if found else None
                snapshot = snapshots[snapshot_key]
                if snapshot is None:
                    continue
                if snapshot_key not in metadata_by_snapshot:
                    metadata_by_snapshot[snapshot_key] = json.loads(
                        snapshot["metadata"]
                    )
                metadata = metadata_by_snapshot[snapshot_key]
                if not isinstance(metadata, dict):
                    continue
                source_id = snapshot["source_id"]
                if source_id not in partitions:
                    found = connection.execute(
                        "SELECT * FROM main.partitions WHERE id=?", (source_id,)
                    ).fetchone()
                    partitions[source_id] = dict(found) if found else None
                partition = partitions[source_id]
                if partition is None:
                    continue
                native = source_id.startswith("ios-")
                body_hash = hashlib.sha256(row["text"].encode()).hexdigest()
                row.update(
                    peer_id=peer,
                    account_id=owner,
                    account_key=current["account_key"],
                    container_account_key=container["account_key"],
                    identity_status=container["identity_status"],
                    source_id=container["id"],
                    kind=current["kind"],
                )
                if native:
                    _routed_native_source(row, provenance, snapshot, partition)
                    server = provenance.get("server_id")
                    if (
                        type(server) not in {str, int}
                        or re.fullmatch(r"-?[1-9][0-9]*", str(server)) is None
                        or type(provenance.get("message_type")) is not int
                        or provenance["message_type"] != (49 if quoted else 1)
                    ):
                        continue
                    author = canonical_contact_id(
                        row["sender_id"], authority="native_message_sender"
                    )
                    if current["kind"] == "direct" and row["sender_id"] != (
                        owner if row["direction"] == "sent" else peer
                    ):
                        continue
                else:
                    locator = provenance.get("locator", {})
                    if (
                        source_id != container["id"]
                        or not source_id.startswith("html-")
                        or container["identity_status"] != "Corroborated export ID"
                        or metadata.get("owner_verified") is not True
                        or metadata.get("source_account_id") != owner
                        or metadata.get("owner_evidence", {}).get("native_username")
                        != owner
                        or not quoted
                        and provenance.get("primary_text_sha256") != body_hash
                        or not isinstance(locator, dict)
                        or str(locator.get("export_msgtype"))
                        != ("49" if quoted else "1")
                    ):
                        continue
                    server, author = "", ""
                quote = quoted_event(row, provenance, native=native) if quoted else None
                if quoted and quote is None:
                    continue
                event_hash = (
                    ("quoted_reply", quote["reply_hash"], quote["text_hash"])
                    if quote
                    else body_hash
                )
                wall = _wall_clock(row, provenance, native=native)
                if wall is None:
                    continue
                order = provenance.get("source_order", row["ordinal"])
                if type(order) is not int or order <= 0:
                    continue
                local_id = _sequence_local_id(provenance, native=native)
                candidate = {
                    "id": row["id"],
                    "format": "native" if native else "html",
                    "message_type": 49 if quoted else 1,
                    "source_key": (source_id, snapshot["snapshot_id"]),
                    "local_id": local_id,
                    "local_scope": (
                        provenance["locator"].get("database_relative_path"),
                        provenance["locator"].get("table"),
                    )
                    if native
                    else None,
                    "unidentified_native_label": not native
                    and _unidentified_group_sender(
                        row, provenance, local_id, current["kind"]
                    ),
                    "order": order,
                    "key": (event_hash, wall, row["direction"]),
                    "author": author,
                    "server": str(server),
                    "epoch": str(
                        Decimal(
                            str(
                                datetime.fromisoformat(
                                    row["timestamp_value"]
                                ).timestamp()
                            )
                        )
                    )
                    if datetime.fromisoformat(row["timestamp_value"]).tzinfo is not None
                    else None,
                    "rank": 0
                    if native and metadata.get("source_label") == "iPad archive"
                    else 1
                    if native
                    else 2,
                    "context": {
                        "id": row["id"],
                        "source_id": source_id,
                        "source_snapshot_id": snapshot["snapshot_id"],
                        "source_label": metadata.get("source_label")
                        or partition["source_label"],
                        "source_clock": provenance.get("source_time")
                        or row["timestamp_value"],
                        "timestamp_value": row["timestamp_value"],
                        "contact_id": author,
                        "record_id": provenance.get("record_id", ""),
                        "sender_id": row["sender_id"] or "",
                        "sender_label": row["sender_label"],
                    },
                }
                if quote:
                    candidate["quote"] = quote
                if native:
                    results.append(candidate)
                else:
                    html.append(candidate)
                    requests.append(
                        {
                            "id": row["id"],
                            "conversation_id": session,
                            "source_id": source_id,
                            "source_snapshot_id": snapshot["snapshot_id"],
                            "account_id": owner,
                            "sender_id": row["sender_id"] or "",
                            "sender_label": row["sender_label"],
                            "peer_id": peer,
                            "conversation_kind": current["kind"],
                            "text_sha256": body_hash,
                            "timestamp_value": row["timestamp_value"],
                            "source_record_id": provenance.get("record_id", ""),
                            "source_locator": provenance.get("locator", {}),
                            "is_system": False,
                        }
                    )
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
        contacts = resolver.resolve_messages(requests) if requests else {}
        for candidate in html:
            contact = contacts[candidate["id"]]
            if contact.get("identity_status") == "verified":
                candidate["author"] = contact["id"]
                candidate["context"]["contact_id"] = contact["id"]
            # Unknown observations remain negative evidence for source ambiguity.
            # Only verified authors enter ordinary identity-based matching.
            results.append(candidate)
    return results


def _group_candidates(candidates, current):
    native_signatures = defaultdict(set)
    for item in candidates:
        if item["format"] == "native":
            native_signatures[item["server"]].add((*item["key"], item["author"]))
    conflicting_servers = {
        server
        for server, signatures in native_signatures.items()
        if len(signatures) > 1
    }
    by_event = defaultdict(list)
    for item in candidates:
        if item["format"] == "native" and item["server"] in conflicting_servers:
            continue
        by_event[(*item["key"], item["author"])].append(item)
    pairs = defaultdict(list)
    for values in by_event.values():
        native = [item for item in values if item["format"] == "native"]
        html = [item for item in values if item["format"] == "html"]
        if not native or not html or len({item["server"] for item in native}) != 1:
            continue
        sources = defaultdict(list)
        for item in values:
            sources[(item["format"], item["source_key"])].append(item)
        for left in html:
            if len(sources[("html", left["source_key"])]) != 1:
                continue
            for right in native:
                if len(sources[("native", right["source_key"])]) == 1 and (
                    left["epoch"] is None
                    or Decimal(left["epoch"]) == Decimal(right["epoch"])
                ):
                    pairs[(left["source_key"], right["source_key"])].append(
                        (left, right)
                    )
    parent = {}

    def find(value):
        parent.setdefault(value, value)
        while parent[value] != value:
            value = parent[value]
        return value

    owner_id = canonical_contact_id(
        current["account_id"], authority="native_account_owner"
    )
    peer_id = (
        canonical_contact_id(current["peer_id"], authority="native_peer_username")
        if current["kind"] == "direct"
        else None
    )
    for matches in pairs.values():
        runs = monotone_pair_runs(
            matches,
            left_order=lambda pair: pair[0]["order"],
            right_order=lambda pair: pair[1]["order"],
            clock=lambda pair: pair[0]["key"][1],
        )
        for ordered in runs:
            authors = {pair[0]["author"] for pair in ordered}
            if (
                len(ordered) < 3
                or owner_id not in authors
                or peer_id is not None
                and peer_id not in authors
                or len(authors) < 2
            ):
                continue
            for left, right in ordered:
                parent[find(left["id"])] = find(right["id"])
    grouped = defaultdict(list)
    records = {item["id"]: item for item in candidates}
    for identifier in parent:
        grouped[find(identifier)].append(records[identifier])
    mapping, members = {}, {}
    for group in grouped.values():
        preferred = min(group, key=lambda item: (item["rank"], item["id"]))["id"]
        if len(group) < 2:
            continue
        members[preferred] = [
            item["context"]
            for item in sorted(group, key=lambda item: (item["rank"], item["id"]))
        ]
        mapping.update(
            {item["id"]: preferred for item in group if item["id"] != preferred}
        )
    return mapping, members


def _group(candidates, current):
    from .wechat_conversation_quotes import proven_quote_candidates
    from .wechat_conversation_sequence import bridge_unidentified_events
    from .wechat_incomplete_quotes import bridge_incomplete_quotes

    verified = [item for item in candidates if item.get("author")]
    plain = [item for item in verified if "quote" not in item]
    mapping, members = _group_candidates(plain, current)
    quotes = proven_quote_candidates(verified, mapping, members)
    if quotes:
        extended_mapping, extended_members = _group_candidates(plain + quotes, current)
        quote_ids = {item["id"] for item in quotes}
        mapping.update(
            {
                key: value
                for key, value in extended_mapping.items()
                if key in quote_ids and value in quote_ids
            }
        )
        members.update(
            {key: value for key, value in extended_members.items() if key in quote_ids}
        )
    mapping, members = bridge_incomplete_quotes(candidates, current, mapping, members)
    return bridge_unidentified_events(candidates, current, mapping, members)


def conversation_events(
    repository, connection, session, current, message_table, history_identity=None,
    *, persist=True,
):
    """Return a pinned same-session display membership without changing source rows."""
    count = current.get("message_count", 0) if isinstance(current, dict) else 0
    if (
        message_table not in _TABLES
        or not isinstance(current, dict)
        or current.get("id") != session
    ):
        return _empty("unsupported", count)
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM " + message_table + " WHERE conversation_id=?",
            (session,),
        ).fetchone()[0]
        inputs = _inputs(repository, history_identity)
        fingerprint = _fingerprint(inputs)
        target = _cache_path(repository, session)
        if target.parent.is_symlink() or target.is_symlink():
            raise ValueError("The conversation event cache cannot be a symbolic link.")
        with _LOCK:
            transient_key = (session, fingerprint, count)
            if not persist and transient_key in _READ_ONLY_PROJECTIONS:
                cached = _READ_ONLY_PROJECTIONS[transient_key]
                if _inputs(repository, history_identity) == inputs:
                    return {**cached, "status": "ready"}
            cached = _read_cache(target, fingerprint, session)
            if cached is not None and _inputs(repository, history_identity) == inputs:
                return {**cached, "status": "ready"}
            if count > MAX_ROWS:
                return _empty("bounded", count)
            candidates = _scan(repository, connection, session, current, message_table)
            mapping, members = _group(candidates, current) if candidates else ({}, {})
            if candidates:
                mapping, members = bridge_reviewed_quotes(
                    repository,
                    connection,
                    session,
                    current,
                    candidates,
                    mapping,
                    members,
                )
            if _inputs(repository, history_identity) != inputs:
                raise ValueError("The conversation source changed during projection.")
            result = {
                "version": VERSION,
                "fingerprint": fingerprint,
                "session": session,
                "original_count": count,
                "display_count": count - len(mapping),
                "preferred_by_id": mapping,
                "members_by_preferred": members,
                "count_delta": len(mapping),
            }
            encoded = _encode_cache(result)
            if encoded is None:
                return _empty("bounded", count)
            if not persist:
                if len(_READ_ONLY_PROJECTIONS) >= 2:
                    _READ_ONLY_PROJECTIONS.pop(next(iter(_READ_ONLY_PROJECTIONS)))
                _READ_ONLY_PROJECTIONS[transient_key] = result
                return {**result, "status": "ready"}
            target.parent.mkdir(mode=0o700, exist_ok=True)
            descriptor, name = tempfile.mkstemp(
                prefix=".events-", suffix=".json", dir=target.parent
            )
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(encoded.encode("utf-8"))
                if _inputs(repository, history_identity) != inputs:
                    raise ValueError(
                        "The conversation source changed before publication."
                    )
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            return {**result, "status": "ready"}
    except (OSError, ValueError, TypeError, KeyError, sqlite3.DatabaseError):
        return _empty("unavailable", count)


def cached_conversation_events(repository, session, history_identity=None):
    """Reuse a pinned proof without reading any conversation message bodies."""
    try:
        inputs = _inputs(repository, history_identity)
        target = _cache_path(repository, session)
        if target.parent.is_symlink() or target.is_symlink():
            return None
        value = _read_cache(target, _fingerprint(inputs), session)
        if value is not None and _inputs(repository, history_identity) == inputs:
            return {**value, "status": "ready"}
    except (OSError, ValueError, TypeError):
        pass
    return None


def _cached_summary(path, fingerprint, identity):
    """Retain only validated counts, with one decode for concurrent readers."""
    key = (
        str(path.absolute()),
        fingerprint,
        tuple(sorted(identity.items())),
        MAX_CACHE_BYTES,
        MAX_COMPRESSED_CACHE_BYTES,
        MAX_DECODED_CACHE_BYTES,
    )
    with _SUMMARY_LOCK:
        if _identity(path) != identity:
            return None
        if key in _SUMMARY_CACHE:
            summary = _SUMMARY_CACHE.pop(key)
            _SUMMARY_CACHE[key] = summary
            return summary
        value = _read_cache(path, fingerprint, expected_identity=identity)
        summary = (value["session"], value["count_delta"]) if value is not None else None
        if _identity(path) != identity:
            return None
        _SUMMARY_CACHE[key] = summary
        while len(_SUMMARY_CACHE) > _SUMMARY_CACHE_LIMIT:
            _SUMMARY_CACHE.popitem(last=False)
        return summary


def cache_only_summaries(repository, history_identity=None):
    """Read already-published counts only; never scan another conversation."""
    try:
        inputs = _inputs(repository, history_identity)
        fingerprint = _fingerprint(inputs)
        directory = _directory(repository)
        if directory.is_symlink() or not directory.is_dir():
            return {}
        result = {}
        scanned_bytes = 0
        for path in directory.glob("*.json"):
            try:
                identity = _identity(path)
            except (OSError, ValueError):
                continue
            size = identity["size"]
            if (
                size > max(MAX_CACHE_BYTES, MAX_COMPRESSED_CACHE_BYTES)
                or scanned_bytes + size + 1 > MAX_SUMMARY_SCAN_BYTES
            ):
                continue
            scanned_bytes += size + 1
            try:
                summary = _cached_summary(path, fingerprint, identity)
            except (OSError, ValueError):
                continue
            if summary is not None:
                result[summary[0]] = summary[1]
        return result if _inputs(repository, history_identity) == inputs else {}
    except (OSError, ValueError, TypeError):
        return {}


def canonical_message_table(connection, message_table, events):
    """Use one temporary membership filter for paging, source jumps, and CSV."""
    if message_table not in _TABLES:
        raise ValueError("Unsupported conversation timeline.")
    if not events["preferred_by_id"]:
        return message_table
    connection.execute(
        "CREATE TEMP TABLE IF NOT EXISTS conversation_event_suppressed(id TEXT PRIMARY KEY) WITHOUT ROWID"
    )
    connection.execute("DELETE FROM temp.conversation_event_suppressed")
    connection.executemany(
        "INSERT INTO temp.conversation_event_suppressed VALUES(?)",
        [(value,) for value in events["preferred_by_id"]],
    )
    connection.execute("DROP VIEW IF EXISTS temp.canonical_conversation_messages")
    connection.execute(
        "CREATE TEMP VIEW canonical_conversation_messages AS SELECT m.* FROM "
        + message_table
        + " m WHERE NOT EXISTS(SELECT 1 FROM temp.conversation_event_suppressed s WHERE s.id=m.id)"
    )
    return "temp.canonical_conversation_messages"
