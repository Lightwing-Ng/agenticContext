"""Aggregate one verified contact's text occurrences without merging namesakes.

Code version: v1.9.1-codex.0
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from itertools import groupby
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from threading import Lock

from .wechat_archive import (
    WechatArchiveRepository,
    _file_identity,
    _identifier,
    _time_fields,
    wechat_archive_path,
)
from .wechat_contacts import (
    CANONICAL_AUTHORITIES,
    MAX_FORWARDED_COMPONENTS,
    MAX_FORWARDED_PAGE_COMPONENTS,
    MAX_FORWARDED_PROOF_BYTES,
    MESSAGE_BINDING_AUTHORITY,
    WechatContactRepository,
    canonical_contact_id,
    contact_registry_path,
)
from .wechat_display_names import WechatDisplayNames
from .wechat_contact_embedded import (
    VERSION as NATIVE_EMBEDDED_VERSION,
    native_candidate_ids,
    native_detail,
    native_details,
)
from .wechat_forwarded import project_html_forwarded
from .wechat_forwarded_origins import link_forwarded_origins
from .wechat_statement_reviews import (
    StatementReviewSourceChanged,
    apply_reviewed_packet_groups,
    restore_reviewed_groups,
    reviews_digest,
)


VERSION = "v1.9.1-codex.0"
INDEX_VERSION = "v1.9.0-codex.0"
_LEGACY_REFERENCE_VERSION = "v1.1.1-codex.0"
_CONTACT_ID = re.compile(r"wechat:contact:[0-9a-f]{64}")
_MESSAGE_KEY = re.compile(
    r"([0-9a-f]{64})((?:(?:_quote)?_forwarded_[1-9][0-9]*)*(?:_quoted_[1-9][0-9]*)?)"
)
_WORKER = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="wechat-contact-analysis"
)
_JOBS = {}
_JOBS_LOCK = Lock()
_BUILD_LOCK = Lock()
_REFERENCE_SCHEMA = """
CREATE TABLE refs (
 id TEXT PRIMARY KEY, parent_message_id TEXT NOT NULL,
 component_path TEXT NOT NULL, anchor_id TEXT NOT NULL,
 conversation_id TEXT NOT NULL, source_id TEXT NOT NULL,
 account_key TEXT NOT NULL, time_sort REAL NOT NULL, ordinal INTEGER NOT NULL,
 has_time INTEGER NOT NULL, timestamp_value TEXT NOT NULL,
 timestamp_label TEXT NOT NULL, time_basis TEXT NOT NULL, source_clock TEXT NOT NULL
) WITHOUT ROWID;
CREATE INDEX refs_time ON refs(has_time DESC,time_sort DESC,ordinal DESC,id);
CREATE TABLE statement_groups (
 id TEXT PRIMARY KEY, reference_id TEXT NOT NULL UNIQUE,
 conversation_key TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE statement_members (
 reference_id TEXT PRIMARY KEY, statement_id TEXT NOT NULL
) WITHOUT ROWID;
CREATE INDEX statement_members_group ON statement_members(statement_id,reference_id);
CREATE TABLE info(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
"""
_ROW_SELECT = (
    "SELECT m.*,c.partition_id AS source_id,c.account_key,c.peer_id,"
    "c.title AS conversation_title,c.kind,p.account_id,p.account_label,"
    "p.identity_status,p.source_label,p.source_file,p.account_key AS container_account_key "
    "FROM main.messages m JOIN main.conversations c ON c.id=m.conversation_id "
    "JOIN main.partitions p ON p.id=c.partition_id "
)
_STATEMENT_COLUMNS = (
    "m.*,c.title AS conversation_title,c.kind,c.peer_id,r.source_id,r.account_key,"
    "p.account_id,p.account_label,p.identity_status,p.source_label,p.source_file,"
    "r.id AS reference_id,r.parent_message_id,r.component_path,r.anchor_id,"
    "r.time_sort AS reference_time_sort,r.timestamp_value AS reference_timestamp_value,"
    "r.timestamp_label AS reference_timestamp_label,r.time_basis AS reference_time_basis,"
    "r.source_clock "
)


class ContactAnalysisSourceChanged(RuntimeError):
    """Reject an index built against sources that changed during the build."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _identity(path):
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Contact analysis inputs must be regular local files.")
    value = path.stat()
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _sources(root):
    history = wechat_archive_path(root)
    contacts = contact_registry_path(root)
    if not history.exists() or not contacts.exists():
        return None
    if any(
        Path(str(path) + suffix).exists()
        for path in (history, contacts)
        for suffix in ("-wal", "-journal", "-shm")
    ):
        raise ContactAnalysisSourceChanged(
            "A contact analysis input has a database journal; wait for publication to finish."
        )
    identities = {str(path.resolve()): _identity(path) for path in (history, contacts)}
    fingerprint = hashlib.sha256(
        _json(
            [
                INDEX_VERSION,
                identities,
                reviews_digest(root),
                {"native_embedded_version": NATIVE_EMBEDDED_VERSION},
            ]
        ).encode()
    ).hexdigest()
    return history, contacts, fingerprint


def _readonly(path, *, derived_cache=False):
    _identity(path)
    if derived_cache and getattr(path.stat(), "st_flags", 0) & 0x40000000:
        raise ContactAnalysisSourceChanged(
            "The derived contact reference cache is cloud-only; rebuild from local sources."
        )
    connection = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    return connection


def contact_analysis_cache_path(root, contact_id):
    """Name one compact derived reference index, never a second text archive."""
    if not isinstance(contact_id, str) or _CONTACT_ID.fullmatch(contact_id) is None:
        raise ValueError("A canonical WeChat contact ID is required.")
    return (
        wechat_archive_path(root).parent
        / "contact_activity"
        / (contact_id.rsplit(":", 1)[-1] + ".sqlite3")
    )


def _contact(root, contact_id, contacts):
    with closing(_readonly(contacts)) as connection:
        row = connection.execute(
            "SELECT * FROM contacts WHERE id=?", (contact_id,)
        ).fetchone()
        if row is None or row["identity_status"] != "verified":
            return None
        try:
            expected = canonical_contact_id(
                row["username"],
                authority="native_contact_username",
                namespace=row["namespace"],
                realm=row["realm"],
            )
        except ValueError:
            return None
        if expected != contact_id:
            return None
        observed = WechatContactRepository(root).query_observations(contact_id)
        names = observed["items"]
        display = next(
            (item["value"] for item in names if item["value"]), row["username"]
        )
        profile = {**dict(row), "platform": "wechat"}
        profile["display_name"] = (
            WechatDisplayNames(root).contact_name(profile) or display
        )
        profile["names"] = names
        profile["observation_count"] = observed["total_count"]
        return profile


def _cache_ready(path, fingerprint):
    if not path.exists():
        return False
    try:
        with closing(_readonly(path, derived_cache=True)) as connection:
            info = dict(connection.execute("SELECT key,value FROM info"))
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        return (
            info.get("fingerprint") == fingerprint
            and info.get("version") == INDEX_VERSION
            and info.get("native_embedded_version") == NATIVE_EMBEDDED_VERSION
            and {"refs", "statement_groups", "statement_members"} <= tables
        )
    except (ContactAnalysisSourceChanged, OSError, ValueError, sqlite3.DatabaseError):
        return False


def _candidate_bindings(connection, contact_id):
    allowed = sorted(CANONICAL_AUTHORITIES)
    marks = ",".join("?" for _ in allowed)
    bindings = [
        dict(row)
        for row in connection.execute(
            "SELECT source_id,sender_id FROM sender_bindings WHERE contact_id=? AND authority IN ("
            + marks
            + ")",
            [contact_id, *allowed],
        )
    ]
    proofs = []
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_contact_bindings'"
    ).fetchone():
        proofs = [
            dict(row)
            for row in connection.execute(
                "SELECT source_id,message_id FROM message_contact_bindings WHERE contact_id=? AND authority=?",
                (contact_id, MESSAGE_BINDING_AUTHORITY),
            )
        ]
    return bindings, proofs


def _candidate_batches(connection, bindings, proofs):
    """Filter before loading message text; scan sender metadata at most once."""
    connection.execute("PRAGMA temp_store=MEMORY")
    connection.execute("CREATE TEMP TABLE candidate_senders(value TEXT PRIMARY KEY)")
    connection.execute("CREATE TEMP TABLE candidate_proofs(value TEXT PRIMARY KEY)")
    connection.executemany(
        "INSERT OR IGNORE INTO candidate_senders VALUES(?)",
        [(item["sender_id"],) for item in bindings],
    )
    connection.executemany(
        "INSERT OR IGNORE INTO candidate_proofs VALUES(?)",
        [(item["message_id"],) for item in proofs],
    )
    statements = []
    if bindings:
        statements.append(
            _ROW_SELECT
            + "WHERE m.sender_id IN (SELECT value FROM temp.candidate_senders)"
        )
    if proofs:
        statements.append(
            _ROW_SELECT + "WHERE m.id IN (SELECT value FROM temp.candidate_proofs)"
        )
    for statement in statements:
        cursor = connection.execute(statement)
        while batch := cursor.fetchmany(400):
            yield [dict(row) for row in batch]


def _candidate_forwarded_batches(connection, username):
    """Inspect retained HTML text partitions, never native/media source trees."""
    conversations = connection.execute(
        "SELECT c.id FROM conversations c JOIN partitions p ON p.id=c.partition_id "
        "WHERE p.id GLOB 'html-*' AND p.source_label='HTML export'"
    )
    for conversation in conversations:
        cursor = connection.execute(
            _ROW_SELECT + "WHERE m.conversation_id=? AND instr(m.provenance,?)>0 "
            "AND CASE WHEN json_valid(m.provenance) THEN "
            "json_array_length(m.provenance,'$.forwarded_components')>0 ELSE 0 END",
            (conversation["id"], username),
        )
        while batch := cursor.fetchmany(100):
            yield [dict(row) for row in batch]


def _candidate_native_batches(connection, history, directory, username):
    parents = []
    for identifier in native_candidate_ids(history, directory, username):
        parents.append(identifier)
        if len(parents) == 200:
            marks = ",".join("?" for _ in parents)
            yield [
                dict(row)
                for row in connection.execute(
                    _ROW_SELECT + "WHERE m.id IN (" + marks + ")", parents
                )
            ]
            parents = []
    if parents:
        marks = ",".join("?" for _ in parents)
        yield [
            dict(row)
            for row in connection.execute(
                _ROW_SELECT + "WHERE m.id IN (" + marks + ")", parents
            )
        ]


def _native_embedded_values(connection, rows, username):
    """Require a proven native owner before using literal embedded author IDs."""
    requests = _requests(connection, rows)
    accepted = {request["id"] for request in requests}
    for row in rows:
        owner = row["account_id"]
        if (
            row["id"] not in accepted
            or not owner
            or row["source_id"] != "ios-" + hashlib.md5(owner.encode()).hexdigest()
            or row["identity_status"] != "Verified archive ID"
            or row["account_key"] != "account:" + owner
            or row["container_account_key"] != row["account_key"]
        ):
            continue
        provenance = json.loads(row["provenance"])
        for detail in native_details(
            provenance,
            parent_id=row["id"],
            account_id=owner,
            peer_id=row["peer_id"] or "",
        ):
            if detail["sender_id"] == username:
                yield _reference_values(row, detail=detail)


def _component_text(component):
    """Return only this author's own text, without quote or descendant text."""
    if not isinstance(component, dict):
        return ""
    text = component.get("text")
    kind = component.get("text_kind")
    if (
        not isinstance(text, str)
        or not text.strip()
        or component.get("is_system") is True
        or kind in {"notice", "system_text", "deferred", "resource_placeholder"}
        or isinstance(component.get("resource_reference"), dict)
        or "possible_exporter_media_placeholder" in component.get("flags", [])
        or str(component.get("type", "")) in {"3", "34", "43", "47", "62"}
        or (
            component.get("forwarded_components")
            and kind not in {"plain_text", "forwarded_rendered_text", "quoted_reply"}
        )
    ):
        return ""
    return text


def _own_text(text, provenance, *, account_id="", parent_id=""):
    """Keep a forwarding envelope from assigning other authors' words to its sender."""
    from .wechat_history_browser import (
        _html_quoted_reply_presentation,
        _native_quoted_reply_presentation,
    )

    kind = provenance.get("text_extraction_kind")
    components = provenance.get("forwarded_components", [])
    if components:
        if kind not in {"plain_text", "forwarded_rendered_text", "quoted_reply"}:
            return ""
        record = project_html_forwarded(
            provenance,
            parent_id=parent_id,
            account_id=account_id,
            text=text,
            components=components,
            source_proof_status="source_proof_verified",
        )
        if record is None:
            return ""
        if kind == "quoted_reply":
            return _html_quoted_reply_presentation(
                text, provenance, forwarded_record=record
            ).get("display_text", "")
        if provenance.get("quoted_components"):
            return ""
        return record["title"] if record["title"].strip() else ""
    if kind == "forwarded_text":
        return ""
    if kind == "quoted_reply":
        own = _native_quoted_reply_presentation(provenance) or (
            _html_quoted_reply_presentation(text, provenance)
        )
        if not own:
            return ""
        text = own["display_text"]
    return _component_text(
        {
            "text": text,
            "text_kind": kind,
            "type": provenance.get("message_type", ""),
            "flags": provenance.get("flags", []),
            "resource_reference": provenance.get("resource_reference"),
        }
    )


def _component_at(provenance, path):
    components = provenance.get("forwarded_components", [])
    component = None
    for position in path:
        if (
            type(position) is not int
            or position < 1
            or not isinstance(components, list)
            or position > len(components)
        ):
            return None
        component = components[position - 1]
        if not isinstance(component, dict):
            return None
        components = component.get("forwarded_components", [])
    return component


def _forwarded_records(connection, rows, resolver):
    by_id = {row["id"]: row for row in rows}
    requests = []
    for request in _requests(connection, rows):
        provenance = json.loads(by_id[request["id"]]["provenance"])
        components = provenance.get("forwarded_components")
        if isinstance(components, list) and components:
            requests.append({**request, "forwarded_components": components})
    projections = {}
    batch, count, size = [], 0, 0
    for request in requests:
        components = request["forwarded_components"]
        pending, component_count = [components], 0
        while pending and component_count <= MAX_FORWARDED_COMPONENTS:
            values = pending.pop()
            component_count += len(values)
            pending.extend(
                nested
                for value in values
                if isinstance(value, dict)
                and isinstance(nested := value.get("forwarded_components"), list)
                and nested
            )
        component_size = len(_json(components).encode())
        if batch and (
            count + component_count > MAX_FORWARDED_PAGE_COMPONENTS
            or size + component_size > MAX_FORWARDED_PROOF_BYTES
        ):
            projections.update(resolver.resolve_forwarded_components(batch))
            batch, count, size = [], 0, 0
        if (
            component_count > MAX_FORWARDED_COMPONENTS
            or component_size > MAX_FORWARDED_PROOF_BYTES
        ):
            continue
        batch.append(request)
        count += component_count
        size += component_size
    if batch:
        projections.update(resolver.resolve_forwarded_components(batch))
    result = {}
    for request in requests:
        projection = projections.get(request["id"], {})
        if projection.get("status") != "source_proof_verified":
            continue
        row = by_id[request["id"]]
        record = project_html_forwarded(
            json.loads(row["provenance"]),
            parent_id=row["id"],
            account_id=row["account_id"] or "",
            text=row["text"],
            components=projection.get("components"),
            source_proof_status=projection["status"],
        )
        if record is not None:
            result[row["id"]] = record
    return result


def _forwarded_items(record, parent_id, path=()):
    """Mirror the shared HTML transcript's one-based DOM keys."""
    for position, child in enumerate(record["items"], start=1):
        component_path = (*path, position)
        key = parent_id + "".join(
            "_forwarded_" + str(value) for value in component_path
        )
        yield child, component_path, key
        nested = child.messaging.get("forwarded_record")
        if nested:
            yield from _forwarded_items(nested, parent_id, component_path)


def _reference_values(row, *, child=None, path=(), key=None, detail=None):
    anchor = None
    if detail is not None:
        timestamp = detail["timestamp_value"]
        label = detail["timestamp_label"]
        basis = detail["time_basis"]
        clock = detail["source_clock"]
        path, key, anchor = detail["path"], detail["id"], detail["anchor_id"]
        time_sort, _, _, _ = _time_fields(
            {"sent_at_raw": timestamp, "timeline_timestamp_usable": bool(timestamp)}
        )
    elif child is None:
        timestamp = row["timestamp_value"]
        label = row["timestamp_label"]
        basis = row["time_basis"]
        clock = ""
        time_sort = row["time_sort"]
        if timestamp.startswith("1970-01-01"):
            clock = timestamp
            timestamp, label, basis, time_sort = (
                "",
                "Unknown time",
                "Unavailable source time",
                0,
            )
    else:
        context = child.messaging
        timestamp = child.last_seen_at
        label = context["timestamp_label"]
        basis = context["time_basis"]
        clock = context["provenance"]["source_time"]
        time_sort, _, _, _ = _time_fields(
            {"sent_at_raw": timestamp, "timeline_timestamp_usable": bool(timestamp)}
        )
    key = key or row["id"]
    return (
        key,
        row["id"],
        _json(list(path)),
        anchor or "wechat_message_" + key,
        row["conversation_id"],
        row["source_id"],
        row["account_key"],
        time_sort,
        row["ordinal"],
        int(bool(timestamp)),
        timestamp,
        label,
        basis,
        clock,
    )


def _routed_native_source(row, provenance, snapshot, partition):
    """Prove a native observation routed into another same-owner conversation."""
    metadata = json.loads(snapshot["metadata"])
    owner = partition["account_id"] if partition else ""
    account_hash = hashlib.md5(owner.encode()).hexdigest() if owner else ""
    locator = provenance.get("locator", {})
    record_id = provenance.get("record_id")
    peer = row["peer_id"]
    relative = (
        locator.get("database_relative_path") if isinstance(locator, dict) else None
    )
    valid = (
        isinstance(metadata, dict)
        and owner
        and partition["id"] == "ios-" + account_hash
        and metadata.get("source_id") == partition["id"]
        and metadata.get("source_account_id") == owner
        and metadata.get("account_hash_verified") is True
        and metadata.get("account_hash") == account_hash
        and row["account_id"] == owner
        and row["account_key"] == partition["account_key"] == "account:" + owner
        and row["container_account_key"] == partition["account_key"]
        and row["identity_status"] in {"Verified archive ID", "Corroborated export ID"}
        and partition["identity_status"] == "Verified archive ID"
        and isinstance(peer, str)
        and peer
        and isinstance(relative, str)
        and Path(relative).parts[:3] == ("Documents", account_hash, "DB")
        and len(Path(relative).parts) == 4
        and Path(relative).name not in {".", ".."}
        and Path(relative).name == locator.get("database")
        and locator.get("table") == "Chat_" + hashlib.md5(peer.encode()).hexdigest()
        and isinstance(record_id, str)
        and bool(record_id)
    )
    if valid:
        observation = {
            **provenance,
            "content_sha256": hashlib.sha256(row["text"].encode()).hexdigest(),
            "sender_id": row["sender_id"] or "",
            "sender_label": row["sender_label"],
            "direction": row["direction"],
            "timestamp_value": row["timestamp_value"],
            "time_basis": row["time_basis"],
            "flags": json.loads(row["quality_flags"]),
        }
        source = partition["id"]
        observation_id = _identifier(source, record_id, _json(observation))
        valid = row["id"] in {
            _identifier(source, record_id),
            _identifier(source, record_id, "variant", observation_id),
        }
    if not valid:
        raise ValueError("A message snapshot crosses its source partition.")
    row["conversation_source_id"] = row["source_id"]
    row["source_id"] = partition["id"]
    for key in (
        "account_key",
        "account_id",
        "account_label",
        "identity_status",
        "source_label",
        "source_file",
    ):
        row[key] = partition[key]


def _requests(connection, rows):
    snapshots = {}
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_snapshots'"
    ).fetchone():
        snapshots = {
            row["id"]: dict(row)
            for row in connection.execute(
                "SELECT id,snapshot_id,source_id,metadata FROM source_snapshots"
            )
        }
    requests = []
    for row in rows:
        try:
            provenance = json.loads(row["provenance"])
        except (TypeError, ValueError) as error:
            raise ValueError("A message has invalid source provenance.") from error
        if not isinstance(provenance, dict):
            raise ValueError("A message has invalid source provenance.")
        extraction = provenance.get("text_extraction_kind")
        if extraction in {"system_text", "resource_placeholder"} or isinstance(
            provenance.get("resource_reference"), dict
        ):
            continue
        if not isinstance(row["text"], str) or not row["text"].strip():
            continue
        snapshot = ""
        snapshot_key = provenance.get("source_snapshot")
        if type(snapshot_key) is int and snapshot_key in snapshots:
            candidate = snapshots[snapshot_key]
            if candidate["source_id"] != row["source_id"]:
                partition = connection.execute(
                    "SELECT * FROM partitions WHERE id=?", (candidate["source_id"],)
                ).fetchone()
                _routed_native_source(row, provenance, candidate, partition)
            snapshot = candidate["snapshot_id"]
        record = provenance.get("record_id")
        requests.append(
            {
                "id": row["id"],
                "conversation_id": row["conversation_id"],
                "source_id": row["source_id"],
                "source_snapshot_id": snapshot,
                "account_id": row["account_id"] or "",
                "sender_id": row["sender_id"] or "",
                "sender_label": row["sender_label"],
                "peer_id": row["peer_id"] or "",
                "conversation_kind": row["kind"],
                "text_sha256": hashlib.sha256(row["text"].encode()).hexdigest(),
                "timestamp_value": row["timestamp_value"],
                "source_record_id": record if isinstance(record, str) else "",
                "source_locator": provenance.get("locator")
                if isinstance(provenance.get("locator"), dict)
                else {},
                "is_system": False,
            }
        )
    return requests


def _native_conversation_key(row):
    """Share proven group IDs and private participant pairs across native accounts."""
    owner, peer = row["account_id"], row["peer_id"]
    if (
        not isinstance(owner, str)
        or not owner
        or row["account_key"] != "account:" + owner
        or row["container_account_key"] != row["account_key"]
        or row["identity_status"] != "Verified archive ID"
        or row["source_id"] != "ios-" + hashlib.md5(owner.encode()).hexdigest()
        or not isinstance(peer, str)
        or not peer
        or "\0" in peer
        or any(character.isspace() for character in peer)
    ):
        return None
    if row["kind"] == "group" and peer.endswith(("@chatroom", "@im.chatroom")):
        return _identifier("native-group", peer)
    if row["kind"] == "direct" and not peer.endswith(("@chatroom", "@im.chatroom")):
        return _identifier("native-private", *sorted((owner, peer)))
    return None


def _native_statement_key(row, contact_id, conversation_key):
    """Match proven server events without choosing between conflicting source clocks."""
    if conversation_key is None or conversation_key != _native_conversation_key(row):
        return None
    provenance = json.loads(row["provenance"])
    server_id = provenance.get("server_id")
    if (
        type(server_id) not in {str, int}
        or re.fullmatch(r"-?[1-9][0-9]*", str(server_id)) is None
    ):
        return None
    original_type = provenance.get("message_type")
    if type(original_type) is not int or original_type <= 0:
        return None
    text = _own_text(
        row["text"], provenance, account_id=row["account_id"], parent_id=row["id"]
    )
    if not text:
        return None
    return _identifier(
        "native-statement-v2",
        contact_id,
        conversation_key,
        str(server_id),
        str(original_type),
        hashlib.sha256(text.encode()).hexdigest(),
    )


def _copy_legacy_references(target, output, history, contacts):
    """Upgrade an unchanged compact reference cache without rescanning the archive."""
    if not target.exists():
        return False
    try:
        retained = _readonly(target, derived_cache=True)
    except (ContactAnalysisSourceChanged, OSError, ValueError, sqlite3.DatabaseError):
        return False
    with closing(retained):
        try:
            info = dict(retained.execute("SELECT key,value FROM info"))
            identities = {
                str(path.resolve()): _identity(path) for path in (history, contacts)
            }
            version = info.get("version")
            expected = hashlib.sha256(_json([version, identities]).encode()).hexdigest()
            old_fields = [row[1] for row in retained.execute("PRAGMA table_info(refs)")]
            new_fields = [row[1] for row in output.execute("PRAGMA table_info(refs)")]
            if (
                version
                not in {
                    _LEGACY_REFERENCE_VERSION,
                    "v1.2.0-codex.0",
                    "v1.2.1-codex.0",
                    "v1.3.0-codex.0",
                    "v1.4.0-codex.0",
                    "v1.5.0-codex.0",
                    "v1.5.1-codex.0",
                    "v1.6.0-codex.0",
                    "v1.6.1-codex.0",
                    "v1.6.2-codex.0",
                    "v1.6.3-codex.0",
                    "v1.7.0-codex.0",
                }
                or info.get("fingerprint") != expected
                or info.get("native_embedded_version") != NATIVE_EMBEDDED_VERSION
                or old_fields != new_fields
            ):
                return False
            # Embedded references are replayed below, including unavailable clocks.
        except (ValueError, sqlite3.DatabaseError):
            return False
        initial = _identity(target)
        cursor = retained.execute("SELECT * FROM refs ORDER BY id")
        while rows := cursor.fetchmany(400):
            output.executemany(
                "INSERT INTO refs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [tuple(row) for row in rows],
            )
        if _identity(target) != initial:
            raise ContactAnalysisSourceChanged("The retained reference cache changed.")
    return True


def _copy_complete_event_index(target, output, history, contacts):
    """Keep every accepted reference and existing event link during a grouping upgrade."""
    if not target.exists():
        return False
    try:
        initial = _identity(target)
        retained = _readonly(target, derived_cache=True)
    except (ContactAnalysisSourceChanged, OSError, ValueError, sqlite3.DatabaseError):
        return False
    with closing(retained):
        try:
            info = dict(retained.execute("SELECT key,value FROM info"))
            identities = {
                str(path.resolve()): _identity(path) for path in (history, contacts)
            }
            version = info.get("version")
            expected = hashlib.sha256(_json([version, identities]).encode()).hexdigest()
            retained_inputs = info.get("source_inputs")
            compatible = (
                version in {"v1.7.1-codex.0", "v1.8.0-codex.0"}
                and info.get("fingerprint") == expected
            ) or (
                version == INDEX_VERSION
                and retained_inputs
                == hashlib.sha256(_json(identities).encode()).hexdigest()
            )
            tables = ("refs", "statement_groups", "statement_members")
            if (
                not compatible
                or info.get("native_embedded_version") != NATIVE_EMBEDDED_VERSION
            ):
                return False
            for table in tables:
                old_fields = [
                    row[1] for row in retained.execute(f"PRAGMA table_info({table})")
                ]
                new_fields = [
                    row[1] for row in output.execute(f"PRAGMA table_info({table})")
                ]
                if old_fields != new_fields:
                    return False
        except (ValueError, sqlite3.DatabaseError):
            return False
        for table in tables:
            cursor = retained.execute(f"SELECT * FROM {table}")
            marks = ",".join("?" for _ in cursor.description)
            while rows := cursor.fetchmany(400):
                output.executemany(
                    f"INSERT INTO {table} VALUES({marks})", [tuple(row) for row in rows]
                )
        restore_reviewed_groups(retained, output)
        if _identity(target) != initial:
            raise ContactAnalysisSourceChanged("The retained contact grouping changed.")
    return True


def _apply_conversation_events(root, source, output, contact_id):
    """Union only accepted primary references already proved to be one source event."""
    from .wechat_conversation_events import conversation_events

    sessions = output.execute(
        "SELECT conversation_id FROM refs WHERE component_path='[]' GROUP BY conversation_id "
        "HAVING SUM(source_id GLOB 'html-*')>0 AND SUM(source_id GLOB 'ios-*')>0"
    ).fetchall()
    if not sessions:
        return
    repository = WechatArchiveRepository(root)
    history_identity = _file_identity(repository.path)
    _conversations, message_table, _status = repository._timeline_tables(
        source, history_identity
    )
    for (session,) in sessions:
        current = source.execute(
            "SELECT c.*,p.account_label,p.account_id,p.identity_status,p.source_label,p.source_file "
            "FROM conversations c JOIN partitions p ON p.id=c.partition_id WHERE c.id=?",
            (session,),
        ).fetchone()
        if current is None:
            continue
        events = conversation_events(
            repository, source, session, dict(current), message_table, history_identity
        )
        for member, preferred in events["preferred_by_id"].items():
            observations = events["members_by_preferred"].get(preferred, [])
            if {
                item.get("contact_id")
                for item in observations
                if item.get("id") in {member, preferred}
            } != {contact_id}:
                continue
            groups = dict(
                output.execute(
                    "SELECT r.id,sm.statement_id FROM refs r JOIN statement_members sm "
                    "ON sm.reference_id=r.id WHERE r.component_path='[]' AND r.conversation_id=? "
                    "AND r.id IN (?,?)",
                    (session, member, preferred),
                )
            )
            if (
                member not in groups
                or preferred not in groups
                or groups[member] == groups[preferred]
            ):
                continue
            # Preserve every existing original/forwarded link by moving the whole proven group.
            output.execute(
                "UPDATE statement_members SET statement_id=? WHERE statement_id=?",
                (groups[preferred], groups[member]),
            )
    output.execute(
        "DELETE FROM statement_groups WHERE id NOT IN (SELECT statement_id FROM statement_members)"
    )


def _source_precedence_snapshot(row, provenance, snapshots):
    key = provenance.get("source_snapshot")
    if type(key) is not int or key <= 0:
        return {}
    snapshot = snapshots.get(key)
    if snapshot is None or snapshot["source_id"] != row["source_id"]:
        return {}
    try:
        metadata = json.loads(snapshot["metadata"])
    except (ValueError, TypeError):
        return {}
    return metadata if isinstance(metadata, dict) else {}


def _stage_source_precedence(output, row, reference, group, snapshots, contact_id):
    """Keep compact source preferences and strict, source-local pairing evidence."""
    provenance = json.loads(row["provenance"])
    metadata = _source_precedence_snapshot(row, provenance, snapshots)
    owner = row["account_id"] or ""
    native = _native_conversation_key(row) is not None
    native_metadata = (
        native
        and metadata.get("source_account_id") == owner
        and metadata.get("account_hash_verified") is True
        and metadata.get("account_hash") == hashlib.md5(owner.encode()).hexdigest()
    )
    rank = (
        0
        if native_metadata and metadata.get("source_label") == "iPad archive"
        else 1
        if native
        else 2
    )
    output.execute(
        "INSERT INTO source_preference VALUES(?,?)",
        (reference[0], rank + (10 if reference[2] != "[]" else 0)),
    )
    if (
        reference[2] != "[]"
        or provenance.get("text_extraction_kind") != "plain_text"
        or provenance.get("forwarded_components")
        or provenance.get("quoted_components")
        or provenance.get("resource_reference")
        or row["direction"] not in {"received", "sent"}
        or not row["sender_id"]
    ):
        return
    locator = provenance.get("locator", {})
    if not isinstance(locator, dict):
        return
    body_hash = hashlib.sha256(row["text"].encode()).hexdigest()
    author = row["sender_id"] if native else provenance.get("sender_native_id")
    try:
        author_id = canonical_contact_id(author, authority="native_message_sender")
    except (ValueError, TypeError):
        return
    if author_id != contact_id:
        return
    if native:
        if (
            not native_metadata
            or provenance.get("message_type") != 1
            or _native_statement_key(row, "", _native_conversation_key(row)) is None
        ):
            return
        local_id = locator.get("local_id")
        format_name = "native"
    else:
        if (
            not row["source_id"].startswith("html-")
            or row["identity_status"] != "Corroborated export ID"
            or row["account_key"] != "account:" + owner
            or row["container_account_key"] != row["account_key"]
            or not owner
            or metadata.get("owner_verified") is not True
            or metadata.get("source_account_id") != owner
            or not isinstance(metadata.get("owner_evidence"), dict)
            or metadata["owner_evidence"].get("native_username") != owner
            or provenance.get("primary_text_sha256") != body_hash
            or provenance.get("export_msgid_namespace") != "MesLocalID"
            or locator.get("export_msgid_namespace") != "MesLocalID"
            or str(locator.get("export_msgtype")) != "1"
            or row["kind"] not in {"direct", "group"}
            or not row["peer_id"]
        ):
            return
        local_id = locator.get("export_msgid")
        format_name = "html"
    if (
        type(local_id) not in {str, int}
        or re.fullmatch(r"[1-9][0-9]*", str(local_id)) is None
    ):
        return
    raw_clock = provenance.get("source_time")
    if native and raw_clock is None:
        raw_clock = row["timestamp_value"]
    if (
        not isinstance(raw_clock, str)
        or re.search(r"[T ]\d{2}:\d{2}:\d{2}(?:[.+\-Z]|$)", raw_clock) is None
    ):
        return
    try:
        stamp = datetime.fromisoformat(row["timestamp_value"])
        clock = datetime.fromisoformat(raw_clock)
        if (
            stamp.year <= 1970
            or stamp.microsecond
            or clock.microsecond
            or stamp.replace(tzinfo=None) != clock.replace(tzinfo=None)
            or (
                stamp.tzinfo is not None and clock.tzinfo is not None and stamp != clock
            )
        ):
            return
        epoch = stamp.timestamp() if stamp.tzinfo is not None else None
        if native and (
            epoch is None
            or Decimal(str(provenance["unix_timestamp"])) != Decimal(str(epoch))
        ):
            return
    except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError):
        return
    scope = _identifier("source-pair-scope", owner, row["kind"], row["peer_id"])
    key = _identifier(
        "local-event-corroboration",
        scope,
        author,
        row["direction"],
        str(local_id),
        stamp.replace(tzinfo=None).isoformat(),
        body_hash,
    )
    output.execute(
        "INSERT INTO source_pair_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            reference[0],
            group,
            format_name,
            row["source_id"] + ":" + str(provenance.get("source_snapshot")),
            scope,
            key,
            str(local_id),
            row["ordinal"],
            epoch,
            row["conversation_id"],
        ),
    )


def _apply_source_precedence(output):
    """Require unique local-event matches and a monotonic multi-event source pair."""
    pairs = output.execute(
        "WITH unique_evidence AS (SELECT *,COUNT(*) OVER "
        "(PARTITION BY source_key,event_key) AS occurrences FROM source_pair_evidence) "
        "SELECT h.source_key,n.source_key,h.scope,h.reference_id,n.statement_id,"
        "h.ordinal,n.ordinal,h.local_id FROM unique_evidence h JOIN unique_evidence n "
        "ON h.event_key=n.event_key WHERE h.format='html' AND n.format='native' "
        "AND h.occurrences=1 AND n.occurrences=1 "
        "AND (h.epoch IS NULL OR h.epoch=n.epoch) "
        "ORDER BY h.source_key,n.source_key,h.scope,h.ordinal,h.reference_id"
    )
    targets = {}
    for _, matching in groupby(pairs, lambda pair: pair[:3]):
        rows = list(matching)
        if len({row[7] for row in rows}) < 3 or any(
            a[5] >= b[5] or a[6] >= b[6] for a, b in zip(rows, rows[1:])
        ):
            continue
        for row in rows:
            targets.setdefault(row[3], set()).add(row[4])
    for reference, statements in targets.items():
        if len(statements) == 1:
            output.execute(
                "UPDATE statement_members SET statement_id=? WHERE reference_id=?",
                (next(iter(statements)), reference),
            )
    output.execute(
        "DELETE FROM statement_groups WHERE id NOT IN "
        "(SELECT statement_id FROM statement_members)"
    )
    output.execute(
        "UPDATE statement_groups SET reference_id=(SELECT sm.reference_id "
        "FROM statement_members sm JOIN source_preference p ON p.reference_id=sm.reference_id "
        "WHERE sm.statement_id=statement_groups.id ORDER BY p.rank,sm.reference_id LIMIT 1)"
    )


def _group_statements(source, output, contact_id):
    """Retain every observation, storing only event keys and reference membership."""
    snapshots = (
        {
            row["id"]: dict(row)
            for row in source.execute("SELECT * FROM source_snapshots")
        }
        if source.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_snapshots'"
        ).fetchone()
        else {}
    )
    output.executescript(
        "CREATE TEMP TABLE source_preference(reference_id TEXT PRIMARY KEY,rank INTEGER);"
        "CREATE TEMP TABLE source_pair_evidence(reference_id TEXT PRIMARY KEY,statement_id TEXT,"
        "format TEXT,source_key TEXT,scope TEXT,event_key TEXT,local_id TEXT,ordinal INTEGER,"
        "epoch REAL,conversation_id TEXT);"
    )
    cursor = output.execute(
        "SELECT id,parent_message_id,component_path,source_id,account_key "
        "FROM refs ORDER BY id"
    )
    while references := cursor.fetchmany(400):
        parents = sorted({reference[1] for reference in references})
        marks = ",".join("?" for _ in parents)
        rows = {
            row["id"]: dict(row)
            for row in source.execute(
                _ROW_SELECT + "WHERE m.id IN (" + marks + ")", parents
            )
        }
        groups, members = [], []
        for reference in references:
            parent = rows.get(reference[1])
            if parent is None:
                raise ContactAnalysisSourceChanged(
                    "A retained source message is missing."
                )
            row = dict(parent)
            if row["source_id"] != reference[3]:
                provenance = json.loads(row["provenance"])
                snapshot = source.execute(
                    "SELECT id,snapshot_id,source_id,metadata FROM source_snapshots "
                    "WHERE id=? AND source_id=?",
                    (provenance.get("source_snapshot"), reference[3]),
                ).fetchone()
                if snapshot is None:
                    raise ContactAnalysisSourceChanged(
                        "A retained native source observation is missing."
                    )
                partition = source.execute(
                    "SELECT * FROM partitions WHERE id=?", (reference[3],)
                ).fetchone()
                _routed_native_source(row, provenance, snapshot, partition)
            if row["account_key"] != reference[4]:
                raise ContactAnalysisSourceChanged(
                    "A retained source observation changed its account."
                )
            if reference[2] == "[]" and not _own_text(
                row["text"],
                json.loads(row["provenance"]),
                account_id=row["account_id"] or "",
                parent_id=row["id"],
            ):
                continue
            native_thread = _native_conversation_key(row)
            conversation_key = native_thread or _identifier(
                "source-conversation", row["conversation_id"]
            )
            group = (
                _native_statement_key(row, contact_id, native_thread)
                if reference[2] == "[]"
                else None
            ) or _identifier("source-statement", contact_id, reference[0])
            groups.append((group, reference[0], conversation_key))
            members.append((reference[0], group))
            _stage_source_precedence(
                output, row, reference, group, snapshots, contact_id
            )
        output.executemany(
            "INSERT OR IGNORE INTO statement_groups VALUES(?,?,?)", groups
        )
        output.executemany("INSERT INTO statement_members VALUES(?,?)", members)
    # Ambiguous legacy author roles stay in the original archive, not as claims
    # that the forwarding author wrote all of the quoted words.
    output.execute(
        "DELETE FROM refs WHERE id NOT IN (SELECT reference_id FROM statement_members)"
    )
    _link_native_references(source, output, contact_id)
    link_forwarded_origins(
        source,
        output,
        contact_id,
        row_select=_ROW_SELECT,
        prepare_rows=_requests,
        native_thread_key=_native_conversation_key,
        native_statement_key=_native_statement_key,
    )
    _apply_source_precedence(output)


def _link_native_references(source, output, contact_id):
    """Link explicit original server IDs only to one proven, exact native event.

    The receiving conversation stays on each source reference. A missing original
    conversation is never replaced with that receiving conversation.
    """
    output.execute(
        "CREATE TEMP TABLE native_event_links(server_id TEXT,message_type INTEGER,"
        "body_hash TEXT,sender_id TEXT,statement_id TEXT,peer_id TEXT,"
        "PRIMARY KEY(server_id,message_type,body_hash,sender_id,statement_id)) WITHOUT ROWID"
    )
    for embedded in (False, True):
        predicate = (
            "component_path='[]'"
            if not embedded
            else "json_type(component_path,'$[0]')='text'"
        )
        cursor = output.execute(
            "SELECT r.*,sm.statement_id FROM refs r JOIN statement_members sm "
            "ON sm.reference_id=r.id WHERE " + predicate
        )
        cursor.row_factory = sqlite3.Row
        while refs := cursor.fetchmany(200):
            parents = sorted({row["parent_message_id"] for row in refs})
            marks = ",".join("?" for _ in parents)
            rows = [
                dict(row)
                for row in source.execute(
                    _ROW_SELECT + "WHERE m.id IN (" + marks + ")", parents
                )
            ]
            _requests(source, rows)
            by_id = {row["id"]: row for row in rows}
            for reference in refs:
                row = by_id[reference["parent_message_id"]]
                provenance = json.loads(row["provenance"])
                if not embedded:
                    thread = _native_conversation_key(row)
                    if _native_statement_key(row, contact_id, thread) is None:
                        continue
                    body = _own_text(
                        row["text"],
                        provenance,
                        account_id=row["account_id"],
                        parent_id=row["id"],
                    )
                    output.execute(
                        "INSERT OR IGNORE INTO native_event_links VALUES(?,?,?,?,?,?)",
                        (
                            str(provenance["server_id"]),
                            provenance["message_type"],
                            hashlib.sha256(body.encode()).hexdigest(),
                            row["sender_id"],
                            reference["statement_id"],
                            row["peer_id"],
                        ),
                    )
                    continue
                detail = native_detail(
                    provenance,
                    json.loads(reference["component_path"]),
                    parent_id=row["id"],
                    account_id=row["account_id"] or "",
                    peer_id=row["peer_id"] or "",
                )
                if (
                    detail is None
                    or type(detail.get("original_message_type")) is not int
                ):
                    continue
                identifiers = {
                    value
                    for key, value in detail["source_ids"].items()
                    if key in {"svrid", "srcMsgSvrId", "dataitemsource/svrid"}
                    and isinstance(value, str)
                    and re.fullmatch(r"-?[1-9][0-9]*", value)
                }
                if len(identifiers) != 1:
                    continue
                matches = output.execute(
                    "SELECT statement_id,peer_id FROM native_event_links WHERE "
                    "server_id=? AND message_type=? AND body_hash=? AND sender_id=? LIMIT 2",
                    (
                        next(iter(identifiers)),
                        detail["original_message_type"],
                        hashlib.sha256(detail["text"].encode()).hexdigest(),
                        detail["sender_id"],
                    ),
                ).fetchall()
                if len(matches) != 1 or (
                    detail.get("original_chat_id")
                    and detail["original_chat_id"] != matches[0][1]
                ):
                    continue
                output.execute(
                    "UPDATE statement_members SET statement_id=? WHERE reference_id=?",
                    (matches[0][0], reference["id"]),
                )
    output.execute(
        "DELETE FROM statement_groups WHERE id NOT IN (SELECT statement_id FROM statement_members)"
    )


def _build_index(root, contact_id, fingerprint):
    with _BUILD_LOCK:
        paths = _sources(root)
        if paths is None or paths[2] != fingerprint:
            raise ContactAnalysisSourceChanged(
                "The contact sources changed before indexing."
            )
        history, contacts, _ = paths
        review_digest = reviews_digest(root)
        identities = {
            str(path.resolve()): _identity(path) for path in (history, contacts)
        }
        if (
            hashlib.sha256(
                _json(
                    [
                        INDEX_VERSION,
                        identities,
                        review_digest,
                        {"native_embedded_version": NATIVE_EMBEDDED_VERSION},
                    ]
                ).encode()
            ).hexdigest()
            != fingerprint
        ):
            raise ContactAnalysisSourceChanged(
                "The statement-review sources changed before indexing."
            )
        target = contact_analysis_cache_path(root, contact_id)
        if _cache_ready(target, fingerprint):
            return
        if target.parent.is_symlink():
            raise ValueError("The contact analysis cache cannot be a symbolic link.")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, filename = tempfile.mkstemp(
            prefix=".contact-analysis-", suffix=".sqlite3", dir=target.parent
        )
        os.close(descriptor)
        temporary = Path(filename)
        try:
            with closing(_readonly(contacts)) as registry:
                bindings, proofs = _candidate_bindings(registry, contact_id)
                identity = registry.execute(
                    "SELECT username FROM contacts WHERE id=?", (contact_id,)
                ).fetchone()
                username = identity["username"] if identity is not None else ""
            with (
                closing(_readonly(history)) as source,
                closing(sqlite3.connect(temporary)) as output,
            ):
                output.executescript(_REFERENCE_SCHEMA)
                complete = _copy_complete_event_index(target, output, history, contacts)
                if not complete:
                    resolver = WechatContactRepository(root)
                    reused = _copy_legacy_references(target, output, history, contacts)
                    if reused:
                        output.execute("DELETE FROM refs WHERE component_path!='[]'")
                        output.execute(
                            "UPDATE refs SET source_clock=timestamp_value,timestamp_value='',"
                            "timestamp_label='Unknown time',time_basis='Unavailable source time',"
                            "time_sort=0,has_time=0 WHERE timestamp_value LIKE '1970-01-01%'"
                        )
                    for rows in (
                        () if reused else _candidate_batches(source, bindings, proofs)
                    ):
                        requests = _requests(source, rows)
                        resolved = resolver.resolve_messages(requests)
                        by_id = {row["id"]: row for row in rows}
                        values = []
                        for message in requests:
                            contact = resolved[message["id"]]
                            if (
                                contact.get("id") != contact_id
                                or contact.get("identity_status") != "verified"
                            ):
                                continue
                            row = by_id[message["id"]]
                            if not _own_text(
                                row["text"],
                                json.loads(row["provenance"]),
                                account_id=row["account_id"] or "",
                                parent_id=row["id"],
                            ):
                                continue
                            values.append(_reference_values(row))
                        output.executemany(
                            "INSERT OR IGNORE INTO refs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            values,
                        )
                    if username:
                        for rows in _candidate_forwarded_batches(source, username):
                            records = _forwarded_records(source, rows, resolver)
                            values = []
                            for row in rows:
                                record = records.get(row["id"])
                                if record is None:
                                    continue
                                for child, path, key in _forwarded_items(
                                    record, row["id"]
                                ):
                                    context = child.messaging
                                    contact = context.get("contact", {})
                                    component = context["provenance"].get(
                                        "embedded_html_forwarded_component"
                                    )
                                    if (
                                        contact.get("id") != contact_id
                                        or contact.get("identity_status") != "verified"
                                        or context.get("is_system_notice")
                                        or not _component_text(component)
                                    ):
                                        continue
                                    values.append(
                                        _reference_values(
                                            row, child=child, path=path, key=key
                                        )
                                    )
                            output.executemany(
                                "INSERT OR REPLACE INTO refs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                values,
                            )
                        for rows in _candidate_native_batches(
                            source, history, target.parent, username
                        ):
                            output.executemany(
                                "INSERT OR REPLACE INTO refs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                _native_embedded_values(source, rows, username),
                            )
                    _group_statements(source, output, contact_id)
                _apply_conversation_events(root, source, output, contact_id)
                try:
                    reviewed = apply_reviewed_packet_groups(
                        root,
                        source,
                        output,
                        contact_id,
                        row_select=_ROW_SELECT,
                        prepare_rows=_requests,
                        native_thread_key=_native_conversation_key,
                        expected_digest=review_digest,
                    )
                except StatementReviewSourceChanged as error:
                    raise ContactAnalysisSourceChanged(str(error)) from error
                identities = {
                    str(path.resolve()): _identity(path) for path in (history, contacts)
                }
                output.executemany(
                    "INSERT INTO info VALUES(?,?)",
                    [
                        ("version", INDEX_VERSION),
                        ("fingerprint", fingerprint),
                        ("native_embedded_version", NATIVE_EMBEDDED_VERSION),
                        (
                            "source_inputs",
                            hashlib.sha256(_json(identities).encode()).hexdigest(),
                        ),
                        ("statement_reviews_digest", review_digest),
                        ("statement_reviews", _json(reviewed)),
                    ],
                )
                output.commit()
            current = _sources(root)
            if current is None or current[2] != fingerprint:
                raise ContactAnalysisSourceChanged(
                    "The contact sources changed while indexing."
                )
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()


def _empty(contact, fingerprint, *, status, error=""):
    return {
        "status": status,
        "contact": contact,
        "items": [],
        "total_count": 0,
        "current_page": 1,
        "total_pages": 1,
        "page_size": 50,
        "counts": {
            "messages": 0,
            "conversations": 0,
            "source_accounts": 0,
            "sources": 0,
        },
        "accounts": [],
        "source_fingerprint": fingerprint,
        "error": error,
    }


def _reference_text(text, raw_provenance, raw_path, account_id, parent_id, peer_id=""):
    try:
        provenance = json.loads(raw_provenance)
        path = json.loads(raw_path)
        if not isinstance(provenance, dict) or not isinstance(path, list):
            return ""
        if path and isinstance(path[0], str):
            detail = native_detail(
                provenance,
                path,
                parent_id=parent_id,
                account_id=account_id or "",
                peer_id=peer_id or "",
            )
            return detail["text"] if detail else ""
        if path:
            return _component_text(_component_at(provenance, path))
        return _own_text(
            text, provenance, account_id=account_id or "", parent_id=parent_id
        )
    except (ValueError, TypeError, RecursionError, KeyError):
        return ""


def _attach_reference_archive(connection, history):
    connection.execute(
        "ATTACH DATABASE ? AS archive",
        (history.resolve().as_uri() + "?mode=ro&immutable=1",),
    )
    connection.create_function(
        "reference_text",
        6,
        lambda *values: _reference_text(*values).casefold(),
        deterministic=True,
    )
    return [dict(row) for row in connection.execute("SELECT * FROM archive.partitions")]


def _reference_query_parts(query):
    where = (
        " WHERE instr(reference_text(m.text,m.provenance,r.component_path,"
        "p.account_id,r.parent_message_id,c.peer_id),?)>0"
        if query
        else ""
    )
    parameters = [query.casefold()] if query else []
    joined = " FROM refs r CROSS JOIN archive.messages m ON m.id=r.parent_message_id"
    partitions = " JOIN archive.partitions p ON p.id=r.source_id"
    return joined, partitions, where, parameters


def _reference_rows_query(query, sort, *, has_time=None):
    # CSV projection version: v1.1.0-codex.0. Keep every source member addressable.
    joined, partitions, where, parameters = _reference_query_parts(query)
    if has_time is not None:
        where += (" AND " if where else " WHERE ") + "r.has_time=?"
        parameters.append(has_time)
    direction = "ASC" if sort == "oldest" else "DESC"
    sql = (
        "SELECT COALESCE(sm.statement_id,r.id) AS statement_id,"
        "m.*,c.title AS conversation_title,c.kind,c.peer_id,r.source_id,r.account_key,"
        "p.account_id,p.account_label,p.identity_status,p.source_label,p.source_file,"
        "r.id AS reference_id,r.parent_message_id,r.component_path,r.anchor_id,"
        "r.time_sort AS reference_time_sort,r.timestamp_value AS reference_timestamp_value,"
        "r.timestamp_label AS reference_timestamp_label,r.time_basis AS reference_time_basis,"
        "r.source_clock "
        + joined
        + " LEFT JOIN statement_members sm ON sm.reference_id=r.id"
        + " JOIN archive.conversations c ON c.id=r.conversation_id"
        + partitions
        + where
        + " ORDER BY r.has_time DESC,r.time_sort "
        + direction
        + ",r.ordinal "
        + direction
        + ",r.id "
        + direction
    )
    return sql, parameters


def _project_reference_rows(connection, rows, contact, names, verified_accounts):
    """Project one bounded batch identically for the table and CSV transport."""
    snapshots = {}
    if connection.execute(
        "SELECT 1 FROM archive.sqlite_master WHERE type='table' AND name='source_snapshots'"
    ).fetchone():
        snapshot_ids = {
            value
            for raw in rows
            if type(value := json.loads(raw["provenance"]).get("source_snapshot"))
            is int
        }
        if snapshot_ids:
            placeholders = ",".join("?" for _ in snapshot_ids)
            snapshots = {
                raw["id"]: dict(raw)
                for raw in connection.execute(
                    "SELECT * FROM archive.source_snapshots WHERE id IN ("
                    + placeholders
                    + ")",
                    list(snapshot_ids),
                )
            }
    items = []
    for raw in rows:
        row = dict(raw)
        provenance = json.loads(row["provenance"])
        component_path = json.loads(row["component_path"])
        row["content_text"] = _reference_text(
            row.pop("text"),
            row["provenance"],
            row["component_path"],
            row["account_id"],
            row["parent_message_id"],
            row["peer_id"],
        )
        row["id"] = row.pop("reference_id")
        row["component_path"] = component_path
        row["is_embedded"] = bool(component_path)
        row["time_sort"] = row.pop("reference_time_sort")
        row["timestamp_value"] = row.pop("reference_timestamp_value")
        row["timestamp_label"] = row.pop("reference_timestamp_label")
        row["time_basis"] = row.pop("reference_time_basis")
        row["contact_id"] = contact["id"]
        row["quality_flags"] = json.loads(row["quality_flags"])
        if component_path and isinstance(component_path[0], str):
            detail = native_detail(
                provenance,
                component_path,
                parent_id=row["parent_message_id"],
                account_id=row["account_id"] or "",
                peer_id=row["peer_id"] or "",
            )
            if detail is None:
                raise ContactAnalysisSourceChanged(
                    "A retained native text reference no longer exists."
                )
            row.update(
                sender_id=detail["sender_id"],
                sender_export_id="",
                sender_label=detail["sender_label"],
                direction=detail["direction"],
                quality_flags=detail["quality_flags"],
                parent_ordinal=row["ordinal"],
                reference_kind=detail["reference_kind"],
                original_conversation_unknown=detail["original_conversation_unknown"],
                original_peer_id=detail.get("original_chat_id", ""),
            )
            row["provenance"] = {
                key: value
                for key, value in provenance.items()
                if key
                not in {"text_components", "forwarded_components", "quoted_components"}
            }
            row["provenance"].update(
                parent_message_id=row["parent_message_id"],
                component_path=component_path,
                source_time=row["source_clock"],
                original_source_ids=detail["source_ids"],
                identity_basis="literal_native_envelope_sender_id",
            )
        elif component_path:
            component = _component_at(provenance, component_path)
            if component is None:
                raise ContactAnalysisSourceChanged(
                    "A retained forwarded reference no longer exists."
                )
            row["sender_id"] = component.get("sender_native_id", "")
            row["sender_export_id"] = component.get("sender_export_id", "")
            row["sender_label"] = component.get("sender_label", "")
            row["direction"] = component.get("direction", "unknown")
            row["parent_ordinal"] = row["ordinal"]
            row["ordinal"] = component.get("ordinal", row["ordinal"])
            row["quality_flags"] = component.get("flags", [])
            row["provenance"] = {
                key: value
                for key, value in provenance.items()
                if key not in {"forwarded_components", "quoted_components"}
            }
            row["provenance"].update(
                parent_message_id=row["parent_message_id"],
                component_path=component_path,
                component_locator=component.get("source_locator", {}),
                component_export_msgid=component.get("export_msgid", ""),
                source_time=row["source_clock"],
            )
            row["reference_kind"] = "html_forwarded"
            row["original_conversation_unknown"] = True
        else:
            row["provenance"] = provenance
            row["reference_kind"] = "primary"
        snapshot = snapshots.get(provenance.get("source_snapshot"))
        if snapshot is not None and snapshot["source_id"] == row["source_id"]:
            metadata = json.loads(snapshot["metadata"])
            row["source_snapshot_id"] = snapshot["snapshot_id"]
            for key in ("source_file", "source_label"):
                if isinstance(metadata.get(key), str) and metadata[key]:
                    row[key] = metadata[key]
        row["viewer_account_label"] = (
            names.account_label(
                {**row, "partition_id": row["source_id"]},
                verified_accounts=verified_accounts,
            )
            or row["account_label"]
        )
        items.append(row)
    return items


def _check_reference_sources(root, fingerprint):
    paths = _sources(root)
    if paths is None or paths[2] != fingerprint:
        raise ContactAnalysisSourceChanged("The contact sources changed while reading.")
    return paths


def _statement_matches(connection, query):
    """Search unique statements once, retaining only compact matching IDs."""
    if not query:
        return "", ""
    connection.execute(
        "CREATE TEMP TABLE contact_statement_matches(id TEXT PRIMARY KEY) WITHOUT ROWID"
    )
    connection.execute(
        "INSERT INTO contact_statement_matches SELECT g.id FROM statement_groups g "
        "JOIN refs r ON r.id=g.reference_id "
        "CROSS JOIN archive.messages m ON m.id=r.parent_message_id "
        "JOIN archive.conversations c ON c.id=r.conversation_id "
        "JOIN archive.partitions p ON p.id=r.source_id "
        "WHERE instr(reference_text(m.text,m.provenance,r.component_path,"
        "p.account_id,r.parent_message_id,c.peer_id),?)>0",
        (query.casefold(),),
    )
    return (
        " WHERE g.id IN (SELECT id FROM contact_statement_matches)",
        " WHERE sm.statement_id IN (SELECT id FROM contact_statement_matches)",
    )


def _statement_rows_query(where, sort):
    direction = "ASC" if sort == "oldest" else "DESC"
    return (
        "SELECT g.id AS statement_id,"
        + _STATEMENT_COLUMNS
        + "FROM statement_groups g JOIN refs r ON r.id=g.reference_id "
        "CROSS JOIN archive.messages m ON m.id=r.parent_message_id "
        "JOIN archive.conversations c ON c.id=r.conversation_id "
        "JOIN archive.partitions p ON p.id=r.source_id"
        + where
        + " ORDER BY r.has_time DESC,r.time_sort "
        + direction
        + ",r.ordinal "
        + direction
        + ",r.id "
        + direction
    )


def _statement_export_rows_query(where, sort, *, has_time=None):
    """Stream preferred references in index order before reading source bodies."""
    if has_time is not None:
        where += (" AND " if where else " WHERE ") + "r.has_time=?"
    direction = "ASC" if sort == "oldest" else "DESC"
    return (
        "SELECT g.id AS statement_id,"
        + _STATEMENT_COLUMNS
        + "FROM refs r INDEXED BY refs_time "
        "CROSS JOIN statement_groups g ON g.reference_id=r.id "
        "CROSS JOIN archive.messages m ON m.id=r.parent_message_id "
        "JOIN archive.conversations c ON c.id=r.conversation_id "
        "JOIN archive.partitions p ON p.id=r.source_id"
        + where
        + " ORDER BY r.has_time DESC,r.time_sort "
        + direction
        + ",r.ordinal "
        + direction
        + ",r.id "
        + direction
    )


def _statement_page_rows_query(where, sort):
    """Limit compact statement references before reading original message bodies."""
    direction = "ASC" if sort == "oldest" else "DESC"
    order = (
        " ORDER BY r.has_time DESC,r.time_sort "
        + direction
        + ",r.ordinal "
        + direction
        + ",r.id "
        + direction
    )
    return (
        "WITH contact_page AS MATERIALIZED ("
        "SELECT g.id AS statement_id,g.reference_id "
        "FROM statement_groups g JOIN refs r ON r.id=g.reference_id"
        + where
        + order
        + " LIMIT ? OFFSET ?) "
        "SELECT contact_page.statement_id,"
        + _STATEMENT_COLUMNS
        + "FROM contact_page CROSS JOIN refs r ON r.id=contact_page.reference_id "
        "CROSS JOIN archive.messages m ON m.id=r.parent_message_id "
        "JOIN archive.conversations c ON c.id=r.conversation_id "
        "JOIN archive.partitions p ON p.id=r.source_id" + order
    )


def _statement_observations(connection, items, contact, names, verified_accounts):
    """Keep source clocks distinct without promoting a representative to event time."""
    if not items:
        return

    def observed_time(value, basis):
        if not value or basis == "Unavailable source time":
            return ("unknown",)
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.year <= 1970:
                return ("unknown",)
            if stamp.tzinfo is None:
                return ("source_clock", stamp.isoformat(timespec="microseconds"))
            delta = stamp.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
            seconds = (
                Decimal(delta.days * 86400 + delta.seconds)
                + Decimal(delta.microseconds) / 1_000_000
            )
            return ("aware", seconds)
        except (ValueError, TypeError, OverflowError, AttributeError):
            return ("unknown",)

    def native_clock_status(value, time_key, present):
        if not present or value is None or value == "":
            return False, False
        if type(value) not in {str, int}:
            return False, True
        try:
            if len(str(value)) > 80:
                return False, True
            epoch = Decimal(str(value))
            if (
                not epoch.is_finite()
                or epoch <= 0
                or epoch > Decimal("253402300799.999999")
            ):
                return False, True
            if time_key[0] != "aware":
                return False, True
            return epoch != time_key[1], False
        except InvalidOperation:
            return False, True

    groups = {item["statement_id"]: item for item in items}
    marks = ",".join("?" for _ in groups)
    rows = connection.execute(
        "SELECT sm.statement_id,"
        + _STATEMENT_COLUMNS
        + "FROM statement_members sm CROSS JOIN refs r ON r.id=sm.reference_id "
        "CROSS JOIN archive.messages m ON m.id=r.parent_message_id "
        "JOIN archive.conversations c ON c.id=r.conversation_id "
        "JOIN archive.partitions p ON p.id=r.source_id "
        "WHERE sm.statement_id IN (" + marks + ") "
        "ORDER BY r.account_key,r.source_id,r.id",
        list(groups),
    ).fetchall()
    fields = (
        "id",
        "parent_message_id",
        "anchor_id",
        "conversation_id",
        "conversation_title",
        "kind",
        "peer_id",
        "account_key",
        "account_id",
        "account_label",
        "viewer_account_label",
        "source_id",
        "source_label",
        "source_file",
        "is_embedded",
        "timestamp_value",
        "timestamp_label",
        "time_basis",
        "reference_kind",
        "original_conversation_unknown",
    )
    retained = {row["reference_id"]: row for row in rows}
    native_time_origins = {
        row["reference_id"]
        for row in rows
        if row["account_id"]
        and row["source_id"]
        == "ios-" + hashlib.md5(row["account_id"].encode()).hexdigest()
        and row["account_key"] == "account:" + row["account_id"]
        and row["identity_status"] == "Verified archive ID"
    }
    for item in items:
        item["observations"] = []
    for observation in _project_reference_rows(
        connection, rows, contact, names, verified_accounts
    ):
        original = retained[observation["id"]]
        provenance = json.loads(original["provenance"])
        embedded = original["component_path"] != "[]"
        origin = {key: observation.get(key) for key in fields}
        origin.update(
            raw_timestamp_value=original[
                "reference_timestamp_value" if embedded else "timestamp_value"
            ],
            raw_timestamp_label=original[
                "reference_timestamp_label" if embedded else "timestamp_label"
            ],
            source_clock=original["source_clock"],
            native_unix_timestamp=None
            if embedded
            else provenance.get("unix_timestamp"),
            native_clock_present=not embedded and "unix_timestamp" in provenance,
        )
        time_key = observed_time(origin["timestamp_value"], origin["time_basis"])
        origin["display_timestamp_value"] = (
            origin["timestamp_value"] if time_key[0] != "unknown" else ""
        )
        origin["display_timestamp_label"] = (
            origin["timestamp_label"] or origin["timestamp_value"]
            if time_key[0] != "unknown"
            else "Unknown time"
        )
        origin["clock_inconsistent"], origin["clock_uncertain"] = native_clock_status(
            origin["native_unix_timestamp"], time_key, origin["native_clock_present"]
        )
        groups[observation["statement_id"]]["observations"].append(origin)
    for item in items:
        observations = item["observations"]
        item["observation_count"] = len(observations)
        item["source_account_count"] = len({row["account_key"] for row in observations})
        # A corroborated HTML copy keeps its literal source clock in the disclosure.
        # The verified native observations govern the displayed event clock.
        clock_observations = [
            origin for origin in observations if origin["id"] in native_time_origins
        ] or observations
        times = {
            observed_time(origin["timestamp_value"], origin["time_basis"])
            for origin in clock_observations
        }
        item["time_conflict"] = len(
            {key for key in times if key[0] == "aware"}
        ) > 1 or any(origin["clock_inconsistent"] for origin in clock_observations)
        item["time_uncertain"] = (
            len(times) > 1 and any(key[0] != "aware" for key in times)
        ) or any(origin["clock_uncertain"] for origin in clock_observations)
        item["has_observed_time"] = any(key[0] != "unknown" for key in times)
        primary = next(origin for origin in observations if origin["id"] == item["id"])
        item["display_timestamp_value"] = primary["display_timestamp_value"]
        item["display_timestamp_label"] = primary["display_timestamp_label"]


def _query_index(
    root, path, history, contact, fingerprint, query, sort, page, page_size
):
    names = WechatDisplayNames(root)
    with closing(_readonly(path, derived_cache=True)) as connection:
        verified_accounts = _attach_reference_archive(connection, history)
        group_where, member_where = _statement_matches(connection, query)
        statements = connection.execute(
            "SELECT COUNT(*),COUNT(DISTINCT g.conversation_key) "
            "FROM statement_groups g" + group_where,
        ).fetchone()
        observations = connection.execute(
            "SELECT COUNT(DISTINCT r.account_key),COUNT(DISTINCT r.source_id),COUNT(*) "
            "FROM statement_members sm CROSS JOIN refs r ON r.id=sm.reference_id"
            + member_where,
        ).fetchone()
        aggregate = (*statements, observations[0], observations[1])
        total = aggregate[0]
        pages = max(1, (total + page_size - 1) // page_size)
        current = min(page, pages)
        rows = connection.execute(
            _statement_page_rows_query(group_where, sort),
            [page_size, (current - 1) * page_size],
        ).fetchall()
        items = _project_reference_rows(
            connection, rows, contact, names, verified_accounts
        )
        _statement_observations(connection, items, contact, names, verified_accounts)
        accounts = []
        for raw in connection.execute(
            "SELECT DISTINCT p.* FROM refs r JOIN archive.partitions p ON p.id=r.source_id ORDER BY p.account_key,p.id"
        ):
            row = dict(raw)
            accounts.append(
                {
                    "key": row["account_key"],
                    "id": row["account_id"] or "",
                    "source_id": row["id"],
                    "label": names.account_label(
                        row, verified_accounts=verified_accounts
                    )
                    or row["account_label"],
                }
            )
    _check_reference_sources(root, fingerprint)
    return {
        "status": "ready",
        "contact": contact,
        "items": items,
        "total_count": total,
        "observation_count": observations[2],
        "current_page": current,
        "total_pages": pages,
        "page_size": page_size,
        "counts": dict(
            zip(
                ("messages", "conversations", "source_accounts", "sources"),
                aggregate,
                strict=True,
            )
        ),
        "accounts": accounts,
        "source_fingerprint": fingerprint,
        "error": "",
    }


def query_contact_analysis(
    local_store_root,
    contact_id,
    *,
    query="",
    sort="newest",
    page=1,
    page_size=50,
    background=False,
):
    """Read verified text occurrences; optionally queue the first metadata scan."""
    cache = contact_analysis_cache_path(local_store_root, contact_id)
    root = Path(local_store_root).expanduser()
    paths = _sources(root)
    if paths is None:
        return None
    history, registry, fingerprint = paths
    contact = _contact(root, contact_id, registry)
    if contact is None:
        return None
    try:
        selected_page = max(1, int(page))
        size = min(200, max(1, int(page_size)))
    except (ValueError, TypeError):
        selected_page, size = 1, 50
    text = str(query or "").strip()[:1024]
    if not _cache_ready(cache, fingerprint):
        if background:
            key = (str(root.resolve()), contact_id)
            with _JOBS_LOCK:
                job = _JOBS.get(key)
                if job is not None and job[0] == fingerprint and job[1].done():
                    error = job[1].exception()
                    if error is not None:
                        _JOBS.pop(key, None)
                        return _empty(
                            contact, fingerprint, status="error", error=str(error)
                        )
                    job = None
                if job is None or (job[0] != fingerprint and job[1].done()):
                    if sum(not future.done() for _, future in _JOBS.values()) >= 4:
                        return _empty(contact, fingerprint, status="indexing")
                    job = (
                        fingerprint,
                        _WORKER.submit(_build_index, root, contact_id, fingerprint),
                    )
                    _JOBS[key] = job
            return _empty(contact, fingerprint, status="indexing")
        _build_index(root, contact_id, fingerprint)
    return _query_index(
        root, cache, history, contact, fingerprint, text, sort, selected_page, size
    )


def contact_analysis_export_context(local_store_root, contact_id):
    """Validate a ready export without querying or sorting any message bodies.

    CSV preflight version: v1.0.0-codex.0.
    """
    cache = contact_analysis_cache_path(local_store_root, contact_id)
    root = Path(local_store_root).expanduser()
    paths = _sources(root)
    if paths is None:
        return None
    _, registry, fingerprint = paths
    contact = _contact(root, contact_id, registry)
    if contact is None:
        return None
    ready = _cache_ready(cache, fingerprint)
    _check_reference_sources(root, fingerprint)
    return {
        "contact": contact,
        "source_fingerprint": fingerprint,
        "status": "ready" if ready else "indexing",
    }


def export_contact_analysis_items(
    local_store_root,
    contact_id,
    *,
    query="",
    sort="newest",
    source_fingerprint,
    source_occurrences=False,
):
    """Stream preferred statements; explicit source inspection retains every reference."""
    cache = contact_analysis_cache_path(local_store_root, contact_id)
    root = Path(local_store_root).expanduser()
    history, registry, _ = _check_reference_sources(root, source_fingerprint)
    contact = _contact(root, contact_id, registry)
    if contact is None:
        raise ValueError("A verified WeChat contact is required.")
    if not _cache_ready(cache, source_fingerprint):
        raise ContactAnalysisSourceChanged("The contact reference index is not ready.")
    text = str(query or "").strip()[:1024]
    names = WechatDisplayNames(root)
    with closing(_readonly(cache, derived_cache=True)) as connection:
        verified_accounts = _attach_reference_archive(connection, history)
        group_where = (
            "" if source_occurrences else _statement_matches(connection, text)[0]
        )
        # Pin the leading index key so oldest exports reverse-scan time and ordinal.
        groups = (
            [
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT has_time FROM refs ORDER BY has_time DESC"
                )
            ]
            if sort == "oldest"
            else [None]
        )
        for has_time in groups:
            _check_reference_sources(root, source_fingerprint)
            if source_occurrences:
                sql, parameters = _reference_rows_query(text, sort, has_time=has_time)
            else:
                sql = _statement_export_rows_query(group_where, sort, has_time=has_time)
                parameters = [] if has_time is None else [has_time]
            with closing(connection.execute(sql, parameters)) as cursor:
                while True:
                    _check_reference_sources(root, source_fingerprint)
                    rows = cursor.fetchmany(200)
                    if not rows:
                        break
                    items = _project_reference_rows(
                        connection, rows, contact, names, verified_accounts
                    )
                    _check_reference_sources(root, source_fingerprint)
                    yield from items
    _check_reference_sources(root, source_fingerprint)


def message_source_location(local_store_root, message_id, *, page_size=100):
    """Locate an occurrence through the browser's preferred chronological event."""
    from .wechat_conversation_events import (
        canonical_message_table,
        conversation_events,
    )

    match = _MESSAGE_KEY.fullmatch(message_id) if isinstance(message_id, str) else None
    if match is None:
        return None
    parent_id = match[1]
    embedded = bool(match[2])
    fingerprint = None
    if embedded:
        paths = _sources(local_store_root)
        if paths is None:
            return None
        fingerprint = paths[2]
    path = wechat_archive_path(local_store_root)
    if not path.is_file():
        return None
    history_identity = _file_identity(path)
    repository = WechatArchiveRepository(local_store_root)
    with closing(_readonly(path)) as connection:
        row = connection.execute(
            _ROW_SELECT + "WHERE m.id=?",
            (parent_id,),
        ).fetchone()
        if row is None:
            return None
        row = dict(row)
        component_path = []
        anchor_id = "wechat_message_" + message_id
        if embedded:
            provenance = json.loads(row["provenance"])
            detail = next(
                (
                    value
                    for value in native_details(
                        provenance,
                        parent_id=parent_id,
                        account_id=row["account_id"] or "",
                        peer_id=row["peer_id"] or "",
                    )
                    if value["id"] == message_id
                ),
                None,
            )
            native_reference = (
                next(
                    (
                        value
                        for value in _native_embedded_values(
                            connection, [dict(row)], detail["sender_id"]
                        )
                        if value[0] == message_id
                    ),
                    None,
                )
                if detail
                else None
            )
            if native_reference is not None:
                component_path = detail["path"]
                anchor_id = detail["anchor_id"]
            record = _forwarded_records(
                connection,
                [dict(row)],
                WechatContactRepository(local_store_root),
            ).get(parent_id)
            if record is None and not component_path:
                return None
            for child, positions, key in (
                _forwarded_items(record, parent_id) if record else ()
            ):
                if key == message_id and (
                    child.messaging.get("display_text", child.content_text)
                    or child.messaging.get("forwarded_record")
                ):
                    component_path = list(positions)
                    break
            if not component_path:
                return None
        _, table, _ = repository._timeline_tables(connection, history_identity)
        current = connection.execute(
            "SELECT c.*,p.account_label,p.account_id,p.identity_status,"
            "p.source_label,p.source_file FROM main.conversations c "
            "JOIN main.partitions p ON p.id=c.partition_id WHERE c.id=?",
            (row["conversation_id"],),
        ).fetchone()
        if current is None:
            return None
        events = conversation_events(
            repository,
            connection,
            row["conversation_id"],
            dict(current),
            table,
            history_identity,
        )
        preferred_id = events["preferred_by_id"].get(parent_id, parent_id)
        if preferred_id != parent_id:
            # Only primary plain-text events can be collapsed; child paths stay literal.
            if embedded:
                return None
            preferred = connection.execute(
                _ROW_SELECT + "WHERE m.id=? AND m.conversation_id=?",
                (preferred_id, row["conversation_id"]),
            ).fetchone()
            if preferred is None:
                return None
            row = dict(preferred)
            parent_id = preferred_id
            anchor_id = "wechat_message_" + preferred_id
        table = canonical_message_table(connection, table, events)
        before = "(m.time_sort!=0 OR (m.time_sort=0 AND m.ordinal<?))"
        values = [row["ordinal"]]
        if row["time_sort"] != 0:
            before = "(m.time_sort!=0 AND (m.time_sort<? OR (m.time_sort=? AND m.ordinal<?)))"
            values = [row["time_sort"], row["time_sort"], row["ordinal"]]
        rank = connection.execute(
            f"SELECT COUNT(*) FROM {table} m WHERE m.conversation_id=? AND " + before,
            [row["conversation_id"], *values],
        ).fetchone()[0]
        ties = [
            item[0]
            for item in connection.execute(
                f"SELECT m.id FROM {table} m WHERE m.conversation_id=? AND m.time_sort=? AND m.ordinal=? "
                "ORDER BY (m.time_sort=0),m.time_sort,m.ordinal,m.id",
                (row["conversation_id"], row["time_sort"], row["ordinal"]),
            )
        ]
        if parent_id not in ties or _file_identity(path) != history_identity:
            return None
        if fingerprint is not None:
            current = _sources(local_store_root)
            if current is None or current[2] != fingerprint:
                return None
        page = (rank + ties.index(parent_id)) // max(1, int(page_size)) + 1
        return {
            **{
                key: row[key]
                for key in (
                    "id",
                    "conversation_id",
                    "time_sort",
                    "ordinal",
                    "account_key",
                    "kind",
                )
            },
            "parent_message_id": parent_id,
            "component_path": component_path,
            "anchor_id": anchor_id,
            "page": page,
            "message_page": page,
        }
