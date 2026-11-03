"""Retain source occurrences while projecting proven same-session text events.

Code version: v1.0.4-codex.0
"""

from __future__ import annotations

from collections import defaultdict
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

from .wechat_contacts import WechatContactRepository, canonical_contact_id


VERSION = "v1.0.3-codex.0"
MAX_ROWS = 1_000_000
MAX_SOURCE_BYTES = 512 * 1024**2
MAX_CACHE_BYTES = 32 * 1024**2
_LOCK = RLock()
_ID = re.compile(r"[0-9a-f]{64}")
_SESSION = re.compile(r"wechat:([0-9a-f]{64})")
_TABLES = {"main.messages", "timeline_messages", "temp.timeline_messages"}


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
    result = {
        str(history): actual,
        str(history.with_name("contacts.sqlite3")): _identity(
            history.with_name("contacts.sqlite3")
        ),
    }
    resource = history.with_name("resource_references.sqlite3")
    try:
        result[str(resource)] = _identity(resource, optional=True)
    except ValueError:
        # An unavailable optional index must not hydrate or block plain-text views.
        value = resource.lstat()
        result[str(resource)] = {
            "unavailable": True,
            "size": value.st_size,
            "mtime_ns": value.st_mtime_ns,
        }
    # Validate each original path before canonicalizing its cache identity.
    return {str(Path(path).resolve()): identity for path, identity in result.items()}


def _fingerprint(inputs):
    return hashlib.sha256(_json([VERSION, inputs]).encode()).hexdigest()


def _directory(repository):
    return repository.path.with_name("conversation_events")


def _cache_path(repository, session):
    match = _SESSION.fullmatch(session) if isinstance(session, str) else None
    if match is None:
        raise ValueError("A canonical conversation is required.")
    return _directory(repository) / (match[1] + ".json")


def _read_cache(path, fingerprint, session=None):
    try:
        before = _identity(path)
        if before["size"] > MAX_CACHE_BYTES:
            return None
        value = json.loads(path.read_text())
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
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
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
    snapshots, partitions = {}, {container["id"]: dict(container)}
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
                if (
                    provenance.get("text_extraction_kind") != "plain_text"
                    or any(
                        provenance.get(key)
                        for key in (
                            "text_components",
                            "forwarded_components",
                            "quoted_components",
                            "resource_reference",
                        )
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
                metadata = json.loads(snapshot["metadata"])
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
                        or provenance["message_type"] != 1
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
                        or provenance.get("primary_text_sha256") != body_hash
                        or not isinstance(locator, dict)
                        or str(locator.get("export_msgtype")) != "1"
                    ):
                        continue
                    server, author = "", ""
                wall = _wall_clock(row, provenance, native=native)
                if wall is None:
                    continue
                order = provenance.get("source_order", row["ordinal"])
                if type(order) is not int or order <= 0:
                    continue
                candidate = {
                    "id": row["id"],
                    "format": "native" if native else "html",
                    "source_key": (source_id, snapshot["snapshot_id"]),
                    "order": order,
                    "key": (body_hash, wall, row["direction"]),
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
                results.append(candidate)
    return results


def _group(candidates, current):
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
        ordered = sorted(matches, key=lambda pair: pair[0]["order"])
        authors = {pair[0]["author"] for pair in ordered}
        if (
            len(ordered) < 3
            or owner_id not in authors
            or peer_id is not None
            and peer_id not in authors
            or len(authors) < 2
            or any(
                a[0]["order"] >= b[0]["order"] or a[1]["order"] >= b[1]["order"]
                for a, b in zip(ordered, ordered[1:])
            )
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


def conversation_events(
    repository, connection, session, current, message_table, history_identity=None
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
            cached = _read_cache(target, fingerprint, session)
            if cached is not None and _inputs(repository, history_identity) == inputs:
                return {**cached, "status": "ready"}
            if count > MAX_ROWS:
                return _empty("bounded", count)
            candidates = _scan(repository, connection, session, current, message_table)
            mapping, members = _group(candidates, current) if candidates else ({}, {})
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
            encoded = _json(result)
            if len(encoded.encode()) > MAX_CACHE_BYTES:
                return _empty("bounded", count)
            target.parent.mkdir(mode=0o700, exist_ok=True)
            descriptor, name = tempfile.mkstemp(
                prefix=".events-", suffix=".json", dir=target.parent
            )
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, "w") as stream:
                    stream.write(encoded + "\n")
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
            scanned_bytes += path.lstat().st_size
            if scanned_bytes > 64 * 1024**2:
                break
            value = _read_cache(path, fingerprint)
            if value is not None:
                result[value["session"]] = value["count_delta"]
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
