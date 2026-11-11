"""Stage and read evidence-checked message completions in one derived SQL catalog.

Code version: v1.0.8-codex.0

Original history, resource catalogs, account boundaries, and recovery-bank pins
remain unchanged. Strategies produce either exact resource-row replacements or
independently anchored system-notice additions through the same SQL timeline.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
from copy import deepcopy
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from threading import RLock

from . import wechat_macos_recovery_bank as bank
from .wechat_archive import wechat_archive_path
from .wechat_completion_json_codec import (
    decode_json_cell,
    encode_json_cell,
    load_json_cell,
    register_json_codec,
)
from .wechat_completion_clone import (
    clone_completion_catalog,
    sync_completion_file,
    sync_completion_directory,
)
from .wechat_global_guard import global_refresh_guard
from .wechat_reconcile import native_text_metadata
from .wechat_resource_sender_evidence import (
    _HEADER_FIELDS,
    _MESSAGE_FIELDS,
    match_secondary_native_event,
    recover_native_resource_sender_from_secondary,
    iter_secondary_native_events,
)
from .wechat_text import native_components_text


CODE_VERSION = "v1.0.8-codex.0"
PROOF_VERSION = "v1.0.0-codex.0"
VERSION = PROOF_VERSION
FILENAME = "secondary_message_completions.sqlite3"
MESSAGE_TABLE = "secondary_completion_timeline_messages"
CONVERSATION_TABLE = "secondary_completion_timeline_conversations"
COLUMNS = (
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
CONVERSATION_COLUMNS = tuple(bank.legacy._CONVERSATION_COLUMNS)
_SQL = re.compile(r"(?:(?:main|temp)\.)?[A-Za-z_][A-Za-z0-9_]*")
_CACHE = {}
_LOCK = RLock()
_SCHEMA = """
CREATE TABLE messages (
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
 sender_id TEXT, sender_label TEXT NOT NULL, direction TEXT NOT NULL,
 text TEXT NOT NULL, time_sort REAL NOT NULL, timestamp_value TEXT NOT NULL,
 timestamp_label TEXT NOT NULL, time_basis TEXT NOT NULL,
 provenance TEXT NOT NULL, quality_flags TEXT NOT NULL, search_text TEXT NOT NULL
);
CREATE INDEX completion_message_conversation ON messages(conversation_id,time_sort,ordinal,id);
CREATE TABLE entries (
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, operation TEXT NOT NULL,
 strategy TEXT NOT NULL, original_row TEXT, original_sha256 TEXT,
 conversation_row TEXT NOT NULL, source_key TEXT NOT NULL, witness_key TEXT NOT NULL,
 plan TEXT, projected_sha256 TEXT NOT NULL
);
CREATE INDEX completion_entry_conversation ON entries(conversation_id,id);
CREATE TABLE sources (id TEXT PRIMARY KEY,header TEXT NOT NULL,sha256 TEXT NOT NULL);
CREATE TABLE witnesses (
 id TEXT PRIMARY KEY,source_key TEXT NOT NULL,message TEXT NOT NULL,sha256 TEXT NOT NULL
);
CREATE TABLE ambiguities (id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE archive_info (key TEXT PRIMARY KEY,value TEXT NOT NULL);
"""


def _clonefile(source, destination):
    clone_completion_catalog(
        source, destination, expected_identity=_identity(source)
    )


def _json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def completion_path(local_store_root):
    return wechat_archive_path(local_store_root).with_name(FILENAME)


def _identity(path, *, optional=False):
    path = Path(path)
    try:
        value = path.lstat()
    except FileNotFoundError:
        if optional:
            return None
        raise
    if (
        not stat.S_ISREG(value.st_mode)
        or path.is_symlink()
        or getattr(value, "st_flags", 0) & 0x40000000
        or any(
            os.path.lexists(str(path) + suffix)
            for suffix in ("-wal", "-shm", "-journal")
        )
    ):
        raise ValueError("Completion inputs must be stable resident regular files.")
    return {
        key: getattr(value, "st_" + key)
        for key in ("dev", "ino", "size", "mtime_ns", "ctime_ns")
    }


def completion_input_identities(history_path):
    """Expose metadata-only cache inputs without hydrating a cloud placeholder."""
    path = Path(history_path).with_name(FILENAME)
    try:
        value = _identity(path, optional=True)
    except ValueError:
        state = path.lstat()
        value = {
            "unavailable": True,
            "size": state.st_size,
            "mtime_ns": state.st_mtime_ns,
        }
    return {} if value is None else {str(path.absolute()): value}


def _readonly(path):
    connection = sqlite3.connect(
        Path(path).absolute().as_uri() + "?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    register_json_codec(connection)
    connection.execute("PRAGMA temp_store=MEMORY")
    connection.execute("PRAGMA cache_size=-32768")
    return connection


def _pins(history):
    return {
        name: _identity(history.with_name(name))
        for name in ("history.sqlite3", "resource_references.sqlite3")
    }


def _file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(8 * 1024**2):
            digest.update(block)
    return digest.hexdigest()


def _metadata(connection):
    info = dict(connection.execute("SELECT key,value FROM archive_info"))
    if info.get("staging_state") not in {None, "complete"}:
        raise ValueError("The completion candidate has not completed staging.")
    if info.get("schema_version") != "1" or info.get("version") != VERSION:
        raise ValueError("Unsupported completion catalog contract.")
    return json.loads(info["input_identities"])


def _decoded_message(row):
    """Restore only the derived JSON transport; every canonical row field stays exact."""
    value = dict(row)
    value["provenance"] = decode_json_cell(value["provenance"])
    return value


def _header_event(event):
    from .wechat_resource_sender_evidence import _HEADER_FIELDS, _MESSAGE_FIELDS

    header = {key: deepcopy(event[key]) for key in _HEADER_FIELDS if key in event}
    message = {key: deepcopy(event[key]) for key in _MESSAGE_FIELDS if key in event}
    return header, message


def _snapshot_index(history):
    result = defaultdict(list)
    for row in history.execute(
        "SELECT id,snapshot_id,source_id,metadata FROM source_snapshots"
    ):
        metadata = json.loads(row["metadata"])
        owner = metadata.get("source_account_id")
        if (
            not isinstance(owner, str)
            or metadata.get("account_hash_verified") is not True
        ):
            continue
        hashed = hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest()
        if (
            metadata.get("account_hash") != hashed
            or row["source_id"] != "ios-" + hashed
        ):
            continue
        for item in metadata.get("input_databases", []):
            if not isinstance(item, dict):
                continue
            relative = item.get(
                "path", item.get("relative_path", item.get("database_relative_path"))
            )
            if isinstance(relative, str) and relative.startswith(
                "AppDomain-com.tencent.xin/"
            ):
                relative = relative[len("AppDomain-com.tencent.xin/") :]
            result[(owner, row["snapshot_id"], relative, item.get("sha256"))].append(
                row["id"]
            )
    return result


def _snapshot_resolver(history, catalog):
    """Use only exact primary declarations or pinned, previously validated catalog receipts."""
    declared = _snapshot_index(history)
    if (
        catalog.execute(
            "SELECT 1 FROM sqlite_master WHERE name='native_resource_sources' AND type='table'"
        ).fetchone()
        is None
    ):
        return declared
    from .wechat_resource_snapshot_adapter import ResourceSnapshotAdapter

    snapshots = [
        dict(row)
        for row in history.execute(
            "SELECT id,snapshot_id,source_id,metadata FROM source_snapshots"
        )
    ]
    receipts = [
        dict(row) for row in catalog.execute("SELECT * FROM native_resource_sources")
    ]
    return ResourceSnapshotAdapter(declared, snapshots, receipts)


def _complete_resource(row, conversation, events, snapshots):
    """Reuse typed native projections while retaining every original clock field."""
    exact = match_secondary_native_event(row, conversation, events)
    matched = exact or match_secondary_native_event(
        row, conversation, events, max_clock_skew=1
    )
    if matched is None:
        return None
    original = dict(row)
    result = (
        recover_native_resource_sender_from_secondary(row, conversation, events)[
            "message"
        ]
        if exact
        else dict(row)
    )
    event = matched["event"]
    native = event.get("native_text")
    quote = (
        isinstance(native, dict)
        and native.get("text_extraction_kind") == "quoted_reply"
        and native.get("appmsg_subtype") == 57
    )
    if quote:
        if row.get("sender_id") and row["sender_id"] != event["sender_id"]:
            raise ValueError("A quoted completion conflicts with the retained author.")
        expected_direction = (
            "sent"
            if event["sender_id"] == matched["primary"]["account_id"]
            else "received"
        )
        if row["direction"] not in {"unknown", expected_direction}:
            raise ValueError(
                "A quoted completion conflicts with the retained direction."
            )
        metadata = native_text_metadata(native, {})
        body = native_components_text(metadata.get("text_components", []))
        if not body.strip() or body != event["text"]:
            raise ValueError("The quoted completion lost its explicit native body.")
        proof = json.loads(result["provenance"])
        proof.pop("resource_reference", None)
        proof.update(metadata, text_extraction_kind="quoted_reply", message_type=49)
        result["text"] = body
        original_prefix = original["text"].casefold()
        remainder = result["search_text"]
        if remainder.startswith(original_prefix):
            remainder = remainder[len(original_prefix) :].lstrip("\n")
        result["search_text"] = body.casefold() + (
            "\n" + remainder if remainder else ""
        )
        flags = json.loads(result["quality_flags"])
        result["quality_flags"] = _json(
            list(
                dict.fromkeys(
                    [
                        *[flag for flag in flags if flag != "resource_placeholder"],
                        *native["flags"],
                        "secondary_native_quote_completed",
                    ]
                )
            )
        )
        result["provenance"] = _json(proof)
    if all(result[key] == original[key] for key in COLUMNS):
        return None
    proof = json.loads(result["provenance"])
    primary = matched["primary"]
    key = (
        primary["account_id"],
        primary["backup_id"],
        primary["source_relative_path"],
        primary["source_sha256"],
    )
    receipt_proof = None
    if hasattr(snapshots, "resolve"):
        resolved = snapshots.resolve(
            key, existing_source_snapshot=proof.get("source_snapshot")
        )
        snapshot = [resolved["source_snapshot_id"]]
        receipt_proof = resolved["receipt_proof"]
    else:
        snapshot = list(dict.fromkeys(snapshots.get(key, ())))
        if len(snapshot) != 1:
            raise ValueError(
                "A completion needs one independently declared original source snapshot."
            )
        if proof.get("source_snapshot", snapshot[0]) != snapshot[0]:
            raise ValueError("A completion cannot change its original source snapshot.")
    proof["source_snapshot"] = snapshot[0]
    proof["locator"] = {
        **proof["locator"],
        "database_relative_path": primary["source_relative_path"],
        "database_sha256": primary["source_sha256"],
    }
    proof["secondary_resource_completion"] = {
        "version": VERSION,
        "strategy": "native_quote" if quote else "native_sender",
        "original_row_sha256": _sha(original),
        "original_provenance_sha256": hashlib.sha256(
            original["provenance"].encode()
        ).hexdigest(),
        "original_resource_reference": json.loads(original["provenance"])[
            "resource_reference"
        ],
        "primary": primary,
        "secondary_source": matched["secondary_source"],
        "clock_skew_seconds": matched["clock_skew_seconds"],
    }
    if receipt_proof is not None:
        proof["secondary_resource_completion"]["snapshot_receipt_adapter"] = (
            receipt_proof
        )
    result["provenance"] = _json(proof)
    return result, matched, "native_quote" if quote else "native_sender"


def _store_entry(
    output, message, conversation, event, *, original=None, strategy, plan=None
):
    message = {**message, "time_sort": float(message["time_sort"])}
    header, native = _header_event(event)
    source_key = _sha(header)
    witness_key = _sha([source_key, native["local_id"], native["content_sha256"]])
    output.execute(
        "INSERT OR IGNORE INTO sources VALUES(?,?,?)",
        (source_key, _json(header), source_key),
    )
    output.execute(
        "INSERT OR IGNORE INTO witnesses VALUES(?,?,?,?)",
        (witness_key, source_key, _json(native), _sha(native)),
    )
    digest = _sha(message)
    if original is None and not _retain_append_event(
        output, message, conversation, event, strategy, source_key, witness_key, digest
    ):
        return
    if output.execute(
        "SELECT 1 FROM ambiguities WHERE id=?", (message["id"],)
    ).fetchone():
        return
    prior = output.execute(
        "SELECT projected_sha256,source_key,witness_key FROM entries WHERE id=?",
        (message["id"],),
    ).fetchone()
    if prior is not None:
        if prior[0] == digest:
            return
        prior_header = load_json_cell(
            output.execute(
                "SELECT header FROM sources WHERE id=?", (prior[1],)
            ).fetchone()[0]
        )
        prior_native = load_json_cell(
            output.execute(
                "SELECT message FROM witnesses WHERE id=?", (prior[2],)
            ).fetchone()[0]
        )

        def coordinates(h, m):
            return (
                h["account_id"],
                h["source"]["source_id"],
                h["source"]["snapshot_id"],
                h["source"]["table"],
                m["local_id"],
            )

        if coordinates(header, native) == coordinates(prior_header, prior_native):
            raise ValueError("One physical source has conflicting completion evidence.")
        _reject_ambiguous(
            output,
            message["id"],
            message["conversation_id"],
            "Multiple physical native witnesses match the original event.",
        )
        return
    output.execute(
        "INSERT OR REPLACE INTO messages VALUES("
        + ",".join("?" for _ in COLUMNS)
        + ")",
        tuple(message[key] for key in COLUMNS),
    )
    output.execute(
        "INSERT OR REPLACE INTO entries VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            message["id"],
            message["conversation_id"],
            "replace" if original is not None else "append",
            strategy,
            _json(original) if original is not None else None,
            _sha(original) if original is not None else None,
            _json(conversation),
            source_key,
            witness_key,
            _json(plan) if plan is not None else None,
            digest,
        ),
    )


def _reject_ambiguous(output, identifier, conversation_id, reason):
    output.execute("DELETE FROM messages WHERE id=?", (identifier,))
    output.execute("DELETE FROM entries WHERE id=?", (identifier,))
    output.execute(
        "INSERT OR IGNORE INTO ambiguities VALUES(?,?,?)",
        (identifier, conversation_id, reason),
    )


def _append_tables(output):
    for statement in (
        "CREATE INDEX IF NOT EXISTS completion_entry_witness_lookup ON entries(witness_key)",
        "CREATE INDEX IF NOT EXISTS completion_entry_source_lookup ON entries(source_key)",
        "CREATE INDEX IF NOT EXISTS completion_append_entry_lookup ON entries(id) WHERE operation='append'",
        "CREATE TABLE IF NOT EXISTS incremental_resource_conflicts(id INTEGER PRIMARY KEY,message_id TEXT NOT NULL,"
        "prior_row_sha256 TEXT NOT NULL,prior_source_key TEXT NOT NULL,prior_witness_key TEXT NOT NULL,"
        "source_key TEXT NOT NULL,witness_key TEXT NOT NULL,reason TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS incremental_conflict_source ON incremental_resource_conflicts(source_key)",
        "CREATE INDEX IF NOT EXISTS incremental_conflict_witness ON incremental_resource_conflicts(witness_key)",
        "CREATE INDEX IF NOT EXISTS incremental_conflict_prior_source ON incremental_resource_conflicts(prior_source_key)",
        "CREATE INDEX IF NOT EXISTS incremental_conflict_prior_witness ON incremental_resource_conflicts(prior_witness_key)",
        "CREATE TABLE IF NOT EXISTS append_event_heads(event_key TEXT PRIMARY KEY,"
        "rank INTEGER NOT NULL,state TEXT NOT NULL,message_id TEXT,business_sha256 TEXT)",
        "CREATE TABLE IF NOT EXISTS append_event_audit(id INTEGER PRIMARY KEY,event_key TEXT NOT NULL,"
        "decision TEXT NOT NULL,kept_id TEXT,kept_row_sha256 TEXT,kept_source_key TEXT,kept_witness_key TEXT,"
        "dropped_id TEXT,dropped_row_sha256 TEXT,dropped_source_key TEXT,dropped_witness_key TEXT,detail TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS append_audit_kept_witness ON append_event_audit(kept_witness_key)",
        "CREATE INDEX IF NOT EXISTS append_audit_dropped_witness ON append_event_audit(dropped_witness_key)",
        "CREATE INDEX IF NOT EXISTS append_audit_kept_source ON append_event_audit(kept_source_key)",
        "CREATE INDEX IF NOT EXISTS append_audit_dropped_source ON append_event_audit(dropped_source_key)",
    ):
        output.execute(statement)


def _known_quoted_reference_facts(native):
    """Compare explicit quoted facts without turning viewer labels into identity."""
    if not isinstance(native, dict):
        return {}
    result = {}
    outer_subtype = native.get("appmsg_subtype")
    if type(outer_subtype) is int and outer_subtype > 0:
        result["outer_appmsg_subtype"] = outer_subtype
    reference = native.get("quoted_reference")
    if not isinstance(reference, dict):
        return result
    for name in ("source_sender_id", "source_chat_id"):
        value = reference.get(name)
        if isinstance(value, str) and value:
            result[name] = value
    kind = reference.get("original_message_type")
    if type(kind) is int and kind > 0:
        result["original_message_type"] = kind
    identifiers = reference.get("source_ids")
    if isinstance(identifiers, dict):
        server = identifiers.get("svrid")
        if (
            isinstance(server, str)
            and len(server) <= 20
            and server.isascii()
            and server.isdecimal()
            and 0 < int(server) < 2**64
        ):
            result["svrid"] = int(server)
    card = reference.get("app_card_metadata")
    if isinstance(card, dict):
        subtype = card.get("appmsg_subtype")
        if type(subtype) is int and subtype > 0:
            result["appmsg_subtype"] = subtype
        title = card.get("safe_title")
        if isinstance(title, str) and title:
            result["safe_title"] = title
    return result


def _quoted_literal_facts(native):
    """Keep explicit reply and plain quoted literals separate from omitted content."""
    if not isinstance(native, dict):
        return {}
    components = native.get("text_components")
    if not isinstance(components, list):
        return {}
    result = {}
    for kind in ("reply_text", "quoted_text"):
        values = [
            item.get("text")
            for item in components
            if isinstance(item, dict)
            and item.get("kind") == kind
            and isinstance(item.get("text"), str)
            and item["text"]
        ]
        if len(values) == 1 and (
            kind == "reply_text"
            or isinstance(native.get("quoted_reference"), dict)
            and native["quoted_reference"].get("original_message_type") == 1
        ):
            result[kind] = values[0]
    return result


def _quoted_literal_conflicts(retained, incoming):
    previous = _quoted_literal_facts(retained)
    novel = _quoted_literal_facts(incoming)
    return any(previous[key] != novel[key] for key in previous.keys() & novel.keys())


def _quoted_reference_conflicts(retained, incoming):
    previous = _known_quoted_reference_facts(retained)
    novel = _known_quoted_reference_facts(incoming)
    return any(previous[key] != novel[key] for key in previous.keys() & novel.keys())


def _record_incremental_conflict(output, row, prior, event, reason):
    """Retain both source coordinates without choosing between contradictory facts."""
    header, native = _header_event(event)
    source_key = _sha(header)
    witness_key = _sha([source_key, native["local_id"], native["content_sha256"]])
    native_sha = _sha(native)
    stored = output.execute(
        "SELECT sha256 FROM witnesses WHERE id=?", (witness_key,)
    ).fetchone()
    if stored is not None and stored[0] != native_sha:
        witness_key = _sha(
            [
                "incremental_conflicting_record",
                source_key,
                native["local_id"],
                native["content_sha256"],
                native_sha,
            ]
        )
    output.execute(
        "INSERT OR IGNORE INTO sources VALUES(?,?,?)",
        (source_key, _json(header), source_key),
    )
    output.execute(
        "INSERT OR IGNORE INTO witnesses VALUES(?,?,?,?)",
        (witness_key, source_key, _json(native), _sha(native)),
    )
    output.execute(
        "INSERT INTO incremental_resource_conflicts(message_id,prior_row_sha256,prior_source_key,prior_witness_key,source_key,witness_key,reason) VALUES(?,?,?,?,?,?,?)",
        (row["id"], prior[3], prior[1], prior[2], source_key, witness_key, reason),
    )
    _reject_ambiguous(output, row["id"], row["conversation_id"], reason)


def _append_context(message, conversation, event, strategy):
    owner = conversation["account_key"][8:]
    if (
        message["conversation_id"] != conversation["id"]
        or conversation["kind"] != "group"
        or event["peer_id"] != conversation["peer_id"]
        or event["local_type"] & 0xFFFFFFFF not in {10000, 10002}
    ):
        raise ValueError("An append decision cannot cross its validated target event.")
    if strategy == "native_owner_system_notice_v1":
        if event["account_id"] != owner or event["session_id"] != conversation["id"]:
            raise ValueError(
                "Native-own precedence requires the exact target viewer and session."
            )
        rank = 2
    elif strategy == "secondary_group_invitation_v1":
        if event["account_id"] == owner:
            raise ValueError(
                "Foreign notice precedence requires an independently different viewer."
            )
        rank = 1
    else:
        raise ValueError("Unsupported closed append evidence strategy.")
    key = _sha(
        {
            "conversation_id": conversation["id"],
            "peer_id": conversation["peer_id"],
            "server_id": event["server_id"],
            "message_type": event["local_type"] & 0xFFFFFFFF,
        }
    )
    business = _sha(
        {
            column: message[column]
            for column in COLUMNS
            if column
            not in {"id", "ordinal", "provenance", "quality_flags", "search_text"}
        }
    )
    return key, rank, business


def _append_stored(output, identifier):
    found = output.execute(
        "SELECT e.source_key,e.witness_key,e.projected_sha256,m.* FROM entries e "
        "JOIN messages m ON m.id=e.id WHERE e.id=?",
        (identifier,),
    ).fetchone()
    if found is None:
        return None
    return {
        "id": identifier,
        "source_key": found[0],
        "witness_key": found[1],
        "row_sha256": found[2],
        "message": _decoded_message(dict(zip(COLUMNS, found[3:]))),
    }


def _append_audit(output, key, decision, kept, dropped, **detail):
    def fields(value):
        return (
            tuple(
                value.get(field)
                for field in ("id", "row_sha256", "source_key", "witness_key")
            )
            if value
            else (None,) * 4
        )

    output.execute(
        "INSERT INTO append_event_audit(event_key,decision,kept_id,kept_row_sha256,kept_source_key,kept_witness_key,"
        "dropped_id,dropped_row_sha256,dropped_source_key,dropped_witness_key,detail) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (key, decision, *fields(kept), *fields(dropped), _json(detail)),
    )


def _delete_completion(output, identifier):
    output.execute("DELETE FROM messages WHERE id=?", (identifier,))
    output.execute("DELETE FROM entries WHERE id=?", (identifier,))


def _retain_append_event(
    output, message, conversation, event, strategy, source_key, witness_key, digest
):
    """Choose only a closed highest-rank witness; preserve every discarded proof key."""
    key, rank, business = _append_context(message, conversation, event, strategy)
    incoming = {
        "id": message["id"],
        "row_sha256": digest,
        "source_key": source_key,
        "witness_key": witness_key,
    }
    head = output.execute(
        "SELECT rank,state,message_id,business_sha256 FROM append_event_heads WHERE event_key=?",
        (key,),
    ).fetchone()
    if head is not None and head[1] == "ambiguous":
        if rank <= head[0]:
            _append_audit(
                output, key, "quarantined_duplicate", None, incoming, rank=rank
            )
            _delete_completion(output, message["id"])
            return False
        _append_audit(
            output,
            key,
            "native_owner_resolves_foreign_ambiguity",
            incoming,
            None,
            prior_rank=head[0],
        )
    elif head is not None:
        prior = _append_stored(output, head[2])
        if prior is not None:
            if prior["id"] == incoming["id"] and all(
                prior[field] == incoming[field]
                for field in ("row_sha256", "source_key", "witness_key")
            ):
                return False
            if rank < head[0]:
                _append_audit(
                    output, key, "lower_rank_duplicate", prior, incoming, rank=rank
                )
                if message["id"] != prior["id"]:
                    _delete_completion(output, message["id"])
                return False
            if rank == head[0]:
                if business == head[3]:
                    _append_audit(
                        output,
                        key,
                        "same_rank_agreeing_duplicate",
                        prior,
                        incoming,
                        rank=rank,
                    )
                    if message["id"] != prior["id"]:
                        _delete_completion(output, message["id"])
                    return False
                _append_audit(
                    output, key, "conflicting_same_rank", None, prior, rank=rank
                )
                _append_audit(
                    output, key, "conflicting_same_rank", None, incoming, rank=rank
                )
                _delete_completion(output, prior["id"])
                _delete_completion(output, message["id"])
                output.execute(
                    "UPDATE append_event_heads SET state='ambiguous',message_id=NULL,business_sha256=NULL WHERE event_key=?",
                    (key,),
                )
                return False
            _append_audit(
                output,
                key,
                "native_owner_precedence",
                incoming,
                prior,
                prior_rank=head[0],
            )
            _delete_completion(output, prior["id"])
    output.execute(
        "INSERT OR REPLACE INTO append_event_heads VALUES(?,?,'active',?,?)",
        (key, rank, message["id"], business),
    )
    return True


def _index_existing_appends(output, history, catalog):
    """Upgrade only complete, individually revalidated append rows from an older base."""
    identifiers = [
        row[0]
        for row in output.execute(
            "SELECT id FROM entries WHERE operation='append' ORDER BY id"
        )
    ]
    if not identifiers:
        return
    factory = output.row_factory
    try:
        output.row_factory = sqlite3.Row
        _validate_entries(
            output,
            history,
            catalog,
            operation="append",
            validate_append_uniqueness=False,
        )
    finally:
        output.row_factory = factory
    for identifier in identifiers:
        row = output.execute(
            "SELECT conversation_row,source_key,witness_key,strategy FROM entries WHERE id=?",
            (identifier,),
        ).fetchone()
        if row is None:
            continue
        stored = _append_stored(output, identifier)
        header = load_json_cell(
            output.execute(
                "SELECT header FROM sources WHERE id=?", (row[1],)
            ).fetchone()[0]
        )
        native = load_json_cell(
            output.execute(
                "SELECT message FROM witnesses WHERE id=?", (row[2],)
            ).fetchone()[0]
        )
        _retain_append_event(
            output,
            stored["message"],
            load_json_cell(row[0]),
            {**header, **native},
            row[3],
            row[1],
            row[2],
            stored["row_sha256"],
        )


def _stage_notice(output, history, catalog, plan):
    from .wechat_native_system_notice_strategy import project_notice_plan

    header, message = _header_event(plan["event"])
    bank._validate_account_binding(history, {**header, "messages": [message]})
    conversation_id = plan["message"]["conversation_id"]
    found = history.execute(
        "SELECT * FROM conversations WHERE id=?", (conversation_id,)
    ).fetchone()
    if found is None:
        found = catalog.execute(
            "SELECT * FROM conversations WHERE id=?", (conversation_id,)
        ).fetchone()
    if found is None:
        raise ValueError("A system completion cannot create a new conversation.")
    conversation = dict(found)
    anchors = []
    for anchor in plan["anchors"]:
        found = history.execute(
            "SELECT * FROM messages WHERE id=?", (anchor["id"],)
        ).fetchone()
        if found is None:
            raise ValueError("A system completion lost its retained primary anchor.")
        anchors.append(dict(found))
    projected = project_notice_plan(plan, conversation, anchors, plan["event"])
    _store_entry(
        output,
        projected["message"],
        conversation,
        plan["event"],
        strategy=plan["strategy"],
        plan=plan,
    )


def stage_secondary_message_completions(
    local_store_root,
    candidate_path,
    packets,
    *,
    notice_plans=None,
    native_system_roles=(),
    base_candidate_path=None,
    base_candidate_sha256=None,
    only_uncompleted=False,
):
    """Prepare a reviewable derived catalog without changing any published file."""
    history_path = wechat_archive_path(local_store_root).absolute()
    target = history_path.with_name(FILENAME)
    candidate = Path(candidate_path).absolute()
    if (
        candidate.exists()
        or candidate.is_symlink()
        or candidate
        in {target, history_path, history_path.with_name("resource_references.sqlite3")}
    ):
        raise ValueError("Select a new completion candidate outside published files.")
    if not candidate.parent.is_dir() or candidate.parent.is_symlink():
        raise ValueError(
            "The completion workstation must already be a regular directory."
        )
    pins = _pins(history_path)
    prior_target = _identity(target, optional=True)
    base = None
    base_identity = None
    base_receipt = None
    if (base_candidate_path is None) != (base_candidate_sha256 is None):
        raise ValueError("A private base candidate needs its reviewed SHA-256.")
    if base_candidate_path is not None:
        base = Path(base_candidate_path).absolute()
        if base in {
            candidate,
            target,
            history_path,
            history_path.with_name("resource_references.sqlite3"),
        }:
            raise ValueError("Use a separate completed private base candidate.")
        base_identity = _identity(base)
        if _file_sha(base) != base_candidate_sha256:
            raise ValueError("The reviewed private base candidate changed.")
        with closing(_readonly(base)) as prior:
            if _metadata(prior) != pins:
                raise ValueError("The private base candidate has stale primary inputs.")
            base_info = dict(prior.execute("SELECT key,value FROM archive_info"))
            if json.loads(base_info["expected_existing_catalog"]) != prior_target:
                raise ValueError(
                    "The private base candidate publication target changed."
                )
        base_receipt = {
            "path": str(base),
            "sha256": base_candidate_sha256,
            "identity": base_identity,
            "report": json.loads(base_info.get("report", "{}")),
        }
    source_hash = _file_sha(history_path.with_name("resource_references.sqlite3"))
    if base is not None and base_info.get("primary_catalog_sha256") != source_hash:
        raise ValueError(
            "The snapshot adapter catalog differs from the reviewed base source hash."
        )
    counts = Counter()
    rejected = Counter()
    ignored = Counter()
    incremental = Counter()
    with (
        closing(_readonly(history_path)) as history,
        closing(
            _readonly(history_path.with_name("resource_references.sqlite3"))
        ) as catalog,
    ):
        snapshots = _snapshot_resolver(history, catalog)
        if base is not None:
            _clonefile(base, candidate)
        elif prior_target is not None:
            with closing(_readonly(target)) as previous:
                if _metadata(previous) != pins:
                    raise ValueError(
                        "Rebuild a stale completion generation from its complete retained evidence."
                    )
            _clonefile(target, candidate)
        else:
            with closing(sqlite3.connect(candidate)) as output, output:
                output.executescript(_SCHEMA)
        candidate.chmod(0o600)
        with closing(sqlite3.connect(candidate)) as output, output:
            register_json_codec(output)
            output.execute("PRAGMA journal_mode=DELETE")
            output.execute("PRAGMA synchronous=FULL")
            output.execute("PRAGMA cache_size=-131072")
            output.execute(
                "INSERT OR REPLACE INTO archive_info VALUES('staging_state','in_progress')"
            )
            output.commit()
            _append_tables(output)
            output.execute(
                "CREATE TABLE IF NOT EXISTS ambiguities (id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,reason TEXT NOT NULL)"
            )
            output.commit()
            _index_existing_appends(output, history, catalog)
            output.commit()
            packet_count = 0
            for packet in packets:
                packet_count += 1
                if "account_binding" not in packet:
                    raise ValueError(
                        "Secondary completions require an independent source-level account binding."
                    )
                bank._validate_account_binding(history, packet)
                events = iter_secondary_native_events(packet)
                if packet.get("conversation_kind") != "group":
                    ignored["authenticated_non_group_packets"] += 1
                    continue
                primary_groups = catalog.execute(
                    "SELECT * FROM conversations WHERE kind='group' AND peer_id=?",
                    (packet["peer_id"],),
                ).fetchall()
                if (
                    not primary_groups
                    and history.execute(
                        "SELECT 1 FROM conversations WHERE kind='group' AND peer_id=? LIMIT 1",
                        (packet["peer_id"],),
                    ).fetchone()
                    is None
                ):
                    ignored["authenticated_groups_without_primary_scope"] += 1
                    continue
                packet_header = {
                    key: packet[key] for key in _HEADER_FIELDS if key in packet
                }
                packet_source_key = _sha(packet_header)
                native_hashes = {}
                lookup = defaultdict(list)
                for event in events:
                    lookup[
                        (event["server_id"], event["local_type"] & 0xFFFFFFFF)
                    ].append(event)
                for conversation_row in primary_groups:
                    conversation = dict(conversation_row)
                    owner = conversation["account_key"][8:]
                    partition = history.execute(
                        "SELECT * FROM partitions WHERE id=?",
                        (conversation["partition_id"],),
                    ).fetchone()
                    if (
                        partition is None
                        or partition["account_id"] != owner
                        or partition["account_key"] != conversation["account_key"]
                    ):
                        raise ValueError(
                            "A completion conversation lost its primary account partition."
                        )
                    for native_row in catalog.execute(
                        "SELECT * FROM messages WHERE conversation_id=?",
                        (conversation["id"],),
                    ):
                        row = dict(native_row)
                        proof = json.loads(row["provenance"])
                        candidates = lookup.get(
                            (proof.get("server_id"), proof.get("message_type")), ()
                        )
                        if not candidates:
                            continue
                        if only_uncompleted:
                            if output.execute(
                                "SELECT 1 FROM ambiguities WHERE id=?", (row["id"],)
                            ).fetchone():
                                incremental["quarantined_rows_skipped"] += 1
                                continue
                            completed_before = output.execute(
                                "SELECT e.strategy,e.source_key,e.witness_key,e.projected_sha256,w.sha256 FROM entries e JOIN witnesses w ON w.id=e.witness_key WHERE e.id=? AND e.operation='replace'",
                                (row["id"],),
                            ).fetchone()
                            if completed_before is not None:
                                incremental["completed_rows_not_reprojected"] += 1
                                stamp = proof.get("unix_timestamp")
                                relevant = [
                                    event
                                    for event in candidates
                                    if type(stamp) is int
                                    and abs(event["create_time"] - stamp)
                                    <= (
                                        1
                                        if completed_before[0] == "native_quote"
                                        else 0
                                    )
                                ]
                                novel = []
                                for event in relevant:
                                    witness_key = _sha(
                                        [
                                            packet_source_key,
                                            event["local_id"],
                                            event["content_sha256"],
                                        ]
                                    )
                                    if witness_key not in native_hashes:
                                        native_hashes[witness_key] = _sha(
                                            {
                                                key: event[key]
                                                for key in _MESSAGE_FIELDS
                                                if key in event
                                            }
                                        )
                                    if (
                                        packet_source_key == completed_before[1]
                                        and witness_key == completed_before[2]
                                        and native_hashes[witness_key]
                                        == completed_before[4]
                                    ):
                                        incremental["same_stored_witness_skipped"] += 1
                                    else:
                                        novel.append(event)
                                if novel:
                                    known = output.execute(
                                        "SELECT sender_id,text FROM messages WHERE id=?",
                                        (row["id"],),
                                    ).fetchone()
                                    retained_native = None
                                    if completed_before[0] == "native_quote":
                                        retained_native = load_json_cell(
                                            output.execute(
                                                "SELECT message FROM witnesses WHERE id=?",
                                                (completed_before[2],),
                                            ).fetchone()[0]
                                        ).get("native_text")
                                    for event in novel:
                                        sender = event.get("sender_id")
                                        sender_conflict = bool(
                                            sender
                                            and not sender.endswith(
                                                ("@chatroom", "@im.chatroom")
                                            )
                                            and known[0]
                                            and sender != known[0]
                                        )
                                        native = event.get("native_text")
                                        quote_conflict = (
                                            completed_before[0] == "native_quote"
                                            and isinstance(native, dict)
                                            and native.get("text_extraction_kind")
                                            == "quoted_reply"
                                            and native.get("appmsg_subtype") == 57
                                            and _quoted_literal_conflicts(
                                                retained_native, native
                                            )
                                        )
                                        reference_conflict = (
                                            completed_before[0] == "native_quote"
                                            and isinstance(native, dict)
                                            and _quoted_reference_conflicts(
                                                retained_native, native
                                            )
                                        )
                                        if (
                                            sender_conflict
                                            or quote_conflict
                                            or reference_conflict
                                        ):
                                            reason = (
                                                "New authenticated native sender conflicts with a completed resource."
                                                if sender_conflict
                                                else "New authenticated native quote body conflicts with a completed resource."
                                                if quote_conflict
                                                else "New authenticated native quoted-reference facts conflict with a completed resource."
                                            )
                                            _record_incremental_conflict(
                                                output,
                                                row,
                                                completed_before,
                                                event,
                                                reason,
                                            )
                                            incremental[
                                                "new_sender_conflicts"
                                                if sender_conflict
                                                else "new_quote_body_conflicts"
                                                if quote_conflict
                                                else "new_quote_reference_conflicts"
                                            ] += 1
                                            break
                                        incremental[
                                            "new_nonconflicting_witness_observations"
                                        ] += 1
                                continue
                        try:
                            completed = _complete_resource(
                                row, conversation, candidates, snapshots
                            )
                        except ValueError as error:
                            rejected[str(error)] += 1
                            if output.execute(
                                "SELECT 1 FROM entries WHERE id=?", (row["id"],)
                            ).fetchone():
                                _reject_ambiguous(
                                    output,
                                    row["id"],
                                    row["conversation_id"],
                                    str(error),
                                )
                            continue
                        if completed is None:
                            continue
                        message, matched, strategy = completed
                        _store_entry(
                            output,
                            message,
                            conversation,
                            matched["event"],
                            original=row,
                            strategy=strategy,
                        )
                if notice_plans is None:
                    from .wechat_secondary_system_notices import build_notice_plans

                    for plan in build_notice_plans(
                        history,
                        catalog,
                        packet,
                        native_system_roles=native_system_roles,
                    ):
                        _stage_notice(output, history, catalog, plan)
                lookup.clear()
                output.commit()
            if notice_plans is not None:
                for plan in notice_plans:
                    _stage_notice(output, history, catalog, plan)
                    output.commit()
            counts = Counter(
                dict(
                    output.execute(
                        "SELECT CASE WHEN operation='append' THEN 'system_notice' ELSE strategy END,COUNT(*) FROM entries GROUP BY CASE WHEN operation='append' THEN 'system_notice' ELSE strategy END"
                    )
                )
            )
            rejected["ambiguous_original_events"] = output.execute(
                "SELECT COUNT(*) FROM ambiguities"
            ).fetchone()[0]
            if not rejected["ambiguous_original_events"]:
                del rejected["ambiguous_original_events"]
            append_decisions = dict(
                output.execute(
                    "SELECT decision,COUNT(*) FROM append_event_audit GROUP BY decision"
                )
            )
            ambiguous_appends = output.execute(
                "SELECT COUNT(*) FROM append_event_heads WHERE state='ambiguous'"
            ).fetchone()[0]
            if ambiguous_appends:
                rejected["ambiguous_append_events"] = ambiguous_appends
            output.execute(
                "DELETE FROM archive_info WHERE key='json_cell_transport_stats'"
            )
            output.executemany(
                "INSERT OR REPLACE INTO archive_info VALUES(?,?)",
                [
                    ("schema_version", "1"),
                    ("version", VERSION),
                    ("input_identities", _json(pins)),
                    ("primary_catalog_sha256", source_hash),
                    ("expected_existing_catalog", _json(prior_target)),
                    ("base_candidate", _json(base_receipt)),
                    (
                        "report",
                        _json(
                            {
                                "counts": dict(counts),
                                "rejected": dict(rejected),
                                "ignored": dict(ignored),
                                "packets": packet_count,
                                "base_candidate": base_receipt,
                                "append_decisions": append_decisions,
                                "incremental": dict(incremental),
                                "snapshot_adapter": {
                                    "rejected": dict(
                                        getattr(snapshots, "rejected", {})
                                    ),
                                    "ignored": dict(getattr(snapshots, "ignored", {})),
                                },
                            }
                        ),
                    ),
                ],
            )
            output.commit()
            if output.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError(
                    "The derived completion candidate failed SQLite verification."
                )
    if (
        _pins(history_path) != pins
        or _identity(target, optional=True) != prior_target
        or base is not None
        and _identity(base) != base_identity
    ):
        raise ValueError("A published input changed while staging completions.")
    with closing(sqlite3.connect(candidate)) as output, output:
        output.execute(
            "UPDATE archive_info SET value='complete' WHERE key='staging_state'"
        )
    sync_completion_file(candidate)
    return {
        "version": VERSION,
        "published": False,
        "candidate": str(candidate),
        "candidate_identity": _identity(candidate),
        "candidate_sha256": _file_sha(candidate),
        "candidate_bytes": candidate.stat().st_size,
        "input_identities": pins,
        "primary_catalog_sha256": source_hash,
        "expected_existing_catalog": prior_target,
        "counts": dict(counts),
        "rejected": dict(rejected),
        "ignored": dict(ignored),
        "packets": packet_count,
        "base_candidate": base_receipt,
        "append_decisions": append_decisions,
        "incremental": dict(incremental),
        "snapshot_adapter": {
            "rejected": dict(getattr(snapshots, "rejected", {})),
            "ignored": dict(getattr(snapshots, "ignored", {})),
        },
    }


def _validate_entries(
    derived,
    history,
    catalog,
    *,
    conversation_id=None,
    progress=None,
    operation=None,
    validate_append_uniqueness=True,
):
    register_json_codec(derived)
    snapshots = _snapshot_resolver(history, catalog)
    filters, parameters = [], []
    if conversation_id is not None:
        filters.append("conversation_id=?")
        parameters.append(conversation_id)
    if operation is not None:
        if operation not in {"append", "replace"}:
            raise ValueError("Unsupported completion validation operation.")
        filters.append("operation=?")
        parameters.append(operation)
    condition = " WHERE " + " AND ".join(filters) if filters else ""
    if validate_append_uniqueness:
        duplicate = derived.execute(
            "SELECT 1 FROM entries WHERE operation='append'"
            + (" AND conversation_id=?" if conversation_id is not None else "")
            + " GROUP BY conversation_id,json_extract(_wechat_completion_json_v1(plan),'$.event.peer_id'),json_extract(_wechat_completion_json_v1(plan),'$.event.server_id'),"
            "(json_extract(_wechat_completion_json_v1(plan),'$.event.local_type') & 4294967295) HAVING COUNT(*)>1 LIMIT 1",
            (conversation_id,) if conversation_id is not None else (),
        ).fetchone()
        if duplicate is not None:
            raise ValueError(
                "A completion catalog contains duplicate append event keys."
            )
    sources = {}
    validated_count = 0
    for entry in derived.execute("SELECT * FROM entries" + condition, parameters):
        validated_count += 1
        if progress is not None and validated_count % 10_000 == 0:
            progress(validated_count)
        entry = dict(entry)
        stored = derived.execute(
            "SELECT * FROM messages WHERE id=?", (entry["id"],)
        ).fetchone()
        if (
            stored is None
            or _sha(_decoded_message(stored)) != entry["projected_sha256"]
        ):
            raise ValueError("A derived completion changed its projected message.")
        if entry["source_key"] not in sources:
            source = derived.execute(
                "SELECT header,sha256 FROM sources WHERE id=?", (entry["source_key"],)
            ).fetchone()
            if (
                source is None
                or _sha(load_json_cell(source["header"])) != source["sha256"]
                or source["sha256"] != entry["source_key"]
            ):
                raise ValueError("A derived completion lost its source receipt.")
            header = load_json_cell(source["header"])
            bank._validate_account_binding(history, {**header, "messages": []})
            sources[entry["source_key"]] = header
        witness = derived.execute(
            "SELECT * FROM witnesses WHERE id=?", (entry["witness_key"],)
        ).fetchone()
        if witness is None or witness["source_key"] != entry["source_key"]:
            raise ValueError("A derived completion lost its exact native witness.")
        message = load_json_cell(witness["message"])
        if (
            _sha(message) != witness["sha256"]
            or _sha(
                [entry["source_key"], message["local_id"], message["content_sha256"]]
            )
            != entry["witness_key"]
        ):
            raise ValueError("A derived native witness changed.")
        event = {**sources[entry["source_key"]], **message}
        conversation = load_json_cell(entry["conversation_row"])
        if entry["operation"] == "replace":
            original = load_json_cell(entry["original_row"])
            actual = catalog.execute(
                "SELECT * FROM messages WHERE id=?", (entry["id"],)
            ).fetchone()
            current_conversation = catalog.execute(
                "SELECT * FROM conversations WHERE id=?", (entry["conversation_id"],)
            ).fetchone()
            if (
                actual is None
                or dict(actual) != original
                or _sha(original) != entry["original_sha256"]
                or current_conversation is None
                or dict(current_conversation) != conversation
            ):
                raise ValueError(
                    "A derived completion's original resource or conversation changed."
                )
            completed = _complete_resource(original, conversation, [event], snapshots)
            if (
                completed is None
                or completed[0] != _decoded_message(stored)
                or completed[2] != entry["strategy"]
            ):
                raise ValueError(
                    "A derived completion no longer follows its source evidence."
                )
        elif entry["operation"] == "append":
            from .wechat_native_system_notice_strategy import project_notice_plan

            plan = load_json_cell(entry["plan"])
            anchors = []
            for anchor in plan["anchors"]:
                actual = history.execute(
                    "SELECT * FROM messages WHERE id=?", (anchor["id"],)
                ).fetchone()
                if actual is None:
                    raise ValueError("A derived system notice lost an original anchor.")
                anchors.append(dict(actual))
            expected = project_notice_plan(plan, conversation, anchors, event)
            if (
                expected["message"] != _decoded_message(stored)
                or entry["strategy"] != plan["strategy"]
            ):
                raise ValueError(
                    "A derived system notice changed its verified strategy."
                )
        else:
            raise ValueError("Unsupported derived message completion operation.")

    if progress is not None:
        progress(validated_count)


def publish_secondary_message_completions(
    local_store_root, candidate_path, *, candidate_sha256
):
    """Publish only a verified candidate while holding existing writer boundaries."""
    history_path = wechat_archive_path(local_store_root).absolute()
    target = history_path.with_name(FILENAME)
    candidate = Path(candidate_path).absolute()
    before = _identity(candidate)
    if _file_sha(candidate) != candidate_sha256:
        raise ValueError("The reviewed completion candidate changed.")
    with (
        global_refresh_guard(local_store_root),
        bank.mac_recovery_publication_guard(local_store_root),
    ):
        with (
            closing(_readonly(candidate)) as derived,
            closing(_readonly(history_path)) as history,
            closing(
                _readonly(history_path.with_name("resource_references.sqlite3"))
            ) as catalog,
        ):
            pins = _metadata(derived)
            info = dict(derived.execute("SELECT key,value FROM archive_info"))
            expected = json.loads(info["expected_existing_catalog"])
            compaction_inputs = json.loads(
                info.get("compaction_input_identities", "null")
            )
            if (
                _pins(history_path) != pins
                or _identity(target, optional=True) != expected
                or compaction_inputs is not None
                and _compaction_inputs(history_path) != compaction_inputs
            ):
                raise ValueError(
                    "Completion publication compare-and-swap inputs changed."
                )
            validation_key = _validation_cache_key(candidate, before, pins)
            with _LOCK:
                reused_validation = validation_key in _CACHE
            if not reused_validation:
                _validate_entries(derived, history, catalog)
        if (
            _identity(candidate) != before
            or _pins(history_path) != pins
            or _identity(target, optional=True) != expected
            or compaction_inputs is not None
            and _compaction_inputs(history_path) != compaction_inputs
        ):
            raise ValueError("A completion publication input changed after validation.")
        previous = None
        if expected is not None:
            previous = candidate.parent / (candidate.stem + ".previous.sqlite3")
            if previous.exists() or previous.is_symlink():
                raise ValueError(
                    "The prior completion preservation path already exists."
                )
            _clonefile(target, previous)
            if _identity(target, optional=True) != expected:
                raise ValueError("The completion target changed during preservation.")
        os.replace(candidate, target)
        sync_completion_directory(target.parent)
        published_identity = _identity(target)
        if any(
            published_identity[field] != before[field]
            for field in ("dev", "ino", "size", "mtime_ns")
        ):
            raise ValueError(
                "The published completion differs from the verified candidate identity."
            )
        _remember_validation(target, published_identity, pins)
    return {
        "version": VERSION,
        "published": True,
        "path": str(target),
        "identity": published_identity,
        "validation_cache_reused": reused_validation,
        "previous_catalog": str(previous) if previous else None,
    }


def _validation_cache_key(path, identity, pins, conversation_id=None):
    return _json([str(Path(path).absolute()), identity, pins, conversation_id])


def _remember_validation(path, identity, pins):
    with _LOCK:
        if len(_CACHE) > 128:
            _CACHE.clear()
        _CACHE[_validation_cache_key(path, identity, pins)] = True


def _compaction_inputs(history_path):
    """Pin every input consumed by the established upstream timeline layers."""
    return bank._inputs(Path(history_path))


def _encode_derived_json(output, progress):
    """Encode bounded batches in a private incomplete clone, retaining exact text."""
    columns = {
        "messages": ("provenance",),
        "entries": ("original_row", "conversation_row", "plan"),
        "sources": ("header",),
        "witnesses": ("message",),
    }
    counts = Counter()
    for table, fields in columns.items():
        previous = 0
        field_sql = ",".join('"' + field + '"' for field in fields)
        while rows := output.execute(
            "SELECT rowid,"
            + field_sql
            + " FROM "
            + table
            + " WHERE rowid>? ORDER BY rowid LIMIT 256",
            (previous,),
        ).fetchall():
            for row in rows:
                values = []
                for cell in row[1:]:
                    text = decode_json_cell(cell)
                    encoded = encode_json_cell(text)
                    if decode_json_cell(encoded) != text:
                        raise ValueError(
                            "A derived JSON cell changed during private compaction."
                        )
                    values.append(encoded)
                    if text is not None:
                        counts["uncompressed_json_bytes"] += len(text.encode("utf-8"))
                        counts["stored_json_bytes"] += (
                            len(encoded)
                            if isinstance(encoded, bytes)
                            else len(encoded.encode("utf-8"))
                        )
                        counts["encoded_json_cells"] += int(isinstance(encoded, bytes))
                if tuple(values) != row[1:]:
                    output.execute(
                        "UPDATE "
                        + table
                        + " SET "
                        + ",".join('"' + field + '"=?' for field in fields)
                        + " WHERE rowid=?",
                        (*values, row[0]),
                    )
            previous = rows[-1][0]
            counts["processed_json_rows"] += len(rows)
            output.commit()
            if counts["processed_json_rows"] % 10_000 < len(rows):
                progress(**dict(counts))
    progress(**dict(counts))
    return dict(counts)


def compact_secondary_message_completions(
    local_store_root,
    candidate_path,
    compacted_candidate_path=None,
    *,
    candidate_sha256,
    dry_run=False,
    progress=None,
    stream_compaction=False,
    quote_enrichment_audit_path=None,
    quote_enrichment_audit_sha256=None,
    quote_enrichment_source_sha256=None,
):
    """Review or compact inactive derived rows without changing any published file.

    The completed source candidate and sealed native streams remain immutable.
    Retained messages keep byte-identical fields and proof hashes. A private
    gzip manifest identifies every discarded derived row by its original and
    projected hashes, allowing replay from the retained original candidate.
    """
    from .wechat_archive import WechatArchiveRepository, _file_identity

    started = time.monotonic()
    phase_started = started
    active_phase = None
    phase_seconds = {}

    def notify(phase, **counts):
        nonlocal phase_started, active_phase
        now = time.monotonic()
        if phase != active_phase:
            if active_phase is not None:
                phase_seconds[active_phase] = (
                    phase_seconds.get(active_phase, 0) + now - phase_started
                )
            active_phase = phase
            phase_started = now
        if progress is not None:
            progress(
                {"phase": phase, "elapsed_seconds": round(now - started, 3), **counts}
            )

    notify("checking_source_candidate")
    replay_options = (
        quote_enrichment_audit_path,
        quote_enrichment_audit_sha256,
        quote_enrichment_source_sha256,
    )
    if any(value is not None for value in replay_options) and (
        not all(value is not None for value in replay_options)
        or not stream_compaction
        or dry_run
    ):
        raise ValueError(
            "Reviewed quote reconciliation requires all audit pins and a new streaming candidate."
        )
    history_path = wechat_archive_path(local_store_root).absolute()
    target = history_path.with_name(FILENAME)
    source = Path(candidate_path).absolute()
    source_identity = _identity(source)
    if _file_sha(source) != candidate_sha256:
        raise ValueError("The reviewed completion source candidate changed.")
    destination = (
        Path(compacted_candidate_path).absolute() if compacted_candidate_path else None
    )
    manifest = destination.with_suffix(".pruned.jsonl.gz") if destination else None
    restoration_manifest = (
        destination.with_suffix(".quote-enrichment.jsonl.gz")
        if destination and quote_enrichment_audit_path is not None
        else None
    )
    if not dry_run and destination is None:
        raise ValueError("Select a new private compacted candidate path.")
    for path in (destination, manifest, restoration_manifest):
        if path is not None and (
            path
            in {
                source,
                target,
                history_path,
                history_path.with_name("resource_references.sqlite3"),
            }
            or path.exists()
            or path.is_symlink()
            or not path.parent.is_dir()
            or path.parent.is_symlink()
        ):
            raise ValueError(
                "Compaction needs new paths in an existing private workstation."
            )
    inputs = _compaction_inputs(history_path)
    expected_target = _identity(target, optional=True)
    pins = _pins(history_path)
    reasons = Counter()
    pruned_counts = Counter()
    manifest_digest = hashlib.sha256()
    manifest_bytes = 0
    output = None
    stream_owner = None
    enrichment_audit = None
    enrichment_result = None
    raw_manifest = None
    compressed_manifest = None

    def record(value):
        nonlocal manifest_bytes
        payload = (_json(value) + "\n").encode()
        manifest_digest.update(payload)
        manifest_bytes += len(payload)
        if compressed_manifest is not None:
            compressed_manifest.write(payload)

    try:
        with (
            closing(_readonly(source)) as derived,
            closing(_readonly(history_path)) as history,
            closing(
                _readonly(history_path.with_name("resource_references.sqlite3"))
            ) as catalog,
        ):
            if _metadata(derived) != pins:
                raise ValueError(
                    "The completion source candidate has stale primary inputs."
                )
            info = dict(derived.execute("SELECT key,value FROM archive_info"))
            if json.loads(info["expected_existing_catalog"]) != expected_target:
                raise ValueError(
                    "The completion target changed before private compaction."
                )
            if quote_enrichment_audit_path is not None:
                from .wechat_quote_enrichment_reconcile import prepare_reviewed_audit

                enrichment_audit = prepare_reviewed_audit(
                    info,
                    candidate_sha256,
                    quote_enrichment_audit_path,
                    quote_enrichment_audit_sha256,
                    quote_enrichment_source_sha256,
                )
            notify("validating_source_entries")
            if (
                _identity(source) != source_identity
                or _pins(history_path) != pins
                or _compaction_inputs(history_path) != inputs
                or _identity(target, optional=True) != expected_target
            ):
                raise ValueError("A compaction source changed before proof validation.")
            with _LOCK:
                source_validation_reused = (
                    _validation_cache_key(source, source_identity, pins) in _CACHE
                )
            if not source_validation_reused:
                _validate_entries(
                    derived,
                    history,
                    catalog,
                    progress=lambda count: notify(
                        "validating_source_entries", validated_entries=count
                    ),
                )
            if (
                _identity(source) != source_identity
                or _pins(history_path) != pins
                or _compaction_inputs(history_path) != inputs
                or _identity(target, optional=True) != expected_target
            ):
                raise ValueError("A compaction source changed after proof validation.")
            if not source_validation_reused:
                _remember_validation(source, source_identity, pins)
            notify("validating_upstream_timeline")
            repository = WechatArchiveRepository(local_store_root)
            _conversations, upstream, status = repository._timeline_tables(
                history,
                _file_identity(history_path),
                strict=True,
                include_secondary=False,
            )
            if status not in {"ready", "absent"}:
                raise ValueError(
                    "Compaction requires a fully validated upstream timeline."
                )
            history.execute(
                "ATTACH DATABASE ? AS completion_source",
                (source.as_uri() + "?mode=ro&immutable=1",),
            )
            before_counts = dict(
                derived.execute(
                    "SELECT CASE WHEN operation='append' THEN 'system_notice' ELSE strategy END,COUNT(*) FROM entries "
                    "GROUP BY CASE WHEN operation='append' THEN 'system_notice' ELSE strategy END"
                )
            )
            stored_before_counts = dict(before_counts)
            if not dry_run:
                notify(
                    "creating_stream_candidate"
                    if stream_compaction
                    else "cloning_candidate"
                )
                if stream_compaction:
                    from .wechat_completion_stream_copy import create_stream_output

                    output, stream_owner = create_stream_output(destination)
                    output.executescript(_SCHEMA)
                else:
                    _clonefile(source, destination)
                    destination.chmod(0o600)
                    output = sqlite3.connect(destination)
                output.execute("PRAGMA journal_mode=DELETE")
                output.execute("PRAGMA synchronous=FULL")
                output.execute(
                    "INSERT OR REPLACE INTO archive_info VALUES('staging_state','in_progress')"
                )
                output.commit()
                _append_tables(output)
                output.execute(
                    "CREATE TEMP TABLE "
                    + ("retained_ids" if stream_compaction else "prune_ids")
                    + "(id TEXT PRIMARY KEY)"
                )
                descriptor = os.open(
                    manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                raw_manifest = os.fdopen(descriptor, "wb")
                compressed_manifest = gzip.GzipFile(
                    filename="", fileobj=raw_manifest, mode="wb", mtime=0
                )
            record(
                {
                    "schema_version": 1,
                    "kind": "inactive_secondary_completions",
                    "source_candidate_sha256": candidate_sha256,
                    "source_candidate_identity": source_identity,
                    "upstream_input_identities": inputs,
                }
            )
            notify(
                "selecting_active_entries"
                if stream_compaction
                else "selecting_inactive_entries"
            )
            from .wechat_completion_selection import BoundedActiveSelection

            active_selection = BoundedActiveSelection(history, upstream)
            retained_selected = 0
            for entry in active_selection.entries("completion_source.entries"):
                if entry.pop("is_active"):
                    if stream_compaction:
                        retained_selected += 1
                        if output is not None:
                            output.execute(
                                "INSERT INTO retained_ids VALUES(?)", (entry["id"],)
                            )
                        if retained_selected % 1000 == 0:
                            notify(
                                "selecting_active_entries",
                                retained_entries=retained_selected,
                                pruned_entries=sum(reasons.values()),
                            )
                    continue
                reason = (
                    "upstream_precedence"
                    if entry["operation"] == "replace"
                    else "upstream_event_already_present"
                )
                reasons[reason] += 1
                pruned_counts[
                    "system_notice"
                    if entry["operation"] == "append"
                    else entry["strategy"]
                ] += 1
                record({**entry, "reason": reason})
                if output is not None and not stream_compaction:
                    output.execute("INSERT INTO prune_ids VALUES(?)", (entry["id"],))
                if sum(reasons.values()) % 1_000 == 0:
                    notify(
                        "selecting_inactive_entries",
                        pruned_entries=sum(reasons.values()),
                    )
            if output is not None:
                if stream_compaction:
                    from .wechat_completion_stream_copy import stream_retained_catalog

                    output.commit()
                    notify("streaming_retained_rows")
                    stream_stats = stream_retained_catalog(
                        output,
                        source,
                        lambda **counts: notify("streaming_retained_rows", **counts),
                    )
                    codec_stats = stream_stats["json_cell_transport"]
                else:
                    notify("pruning_unreferenced_proofs")
                    previous = ""
                    while identifiers := output.execute(
                        "SELECT id FROM prune_ids WHERE id>? ORDER BY id LIMIT 1000",
                        (previous,),
                    ).fetchall():
                        last = identifiers[-1][0]
                        for table in ("messages", "entries"):
                            output.execute(
                                "DELETE FROM "
                                + table
                                + " WHERE id IN (SELECT id FROM prune_ids WHERE id>? AND id<=?)",
                                (previous, last),
                            )
                        output.commit()
                        previous = last
                    audit_present = (
                        output.execute(
                            "SELECT 1 FROM sqlite_master WHERE name='append_event_audit' AND type='table'"
                        ).fetchone()
                        is not None
                    )
                    incremental_audit = (
                        output.execute(
                            "SELECT 1 FROM sqlite_master WHERE name='incremental_resource_conflicts' AND type='table'"
                        ).fetchone()
                        is not None
                    )
                    output.execute(
                        "DELETE FROM witnesses WHERE NOT EXISTS(SELECT 1 FROM entries e WHERE e.witness_key=witnesses.id)"
                        + (
                            " AND NOT EXISTS(SELECT 1 FROM incremental_resource_conflicts a WHERE a.witness_key=witnesses.id OR a.prior_witness_key=witnesses.id)"
                            if incremental_audit
                            else ""
                        )
                        + (
                            " AND NOT EXISTS(SELECT 1 FROM append_event_audit a WHERE a.kept_witness_key=witnesses.id OR a.dropped_witness_key=witnesses.id)"
                            if audit_present
                            else ""
                        )
                    )
                    output.execute(
                        "DELETE FROM sources WHERE NOT EXISTS(SELECT 1 FROM entries e WHERE e.source_key=sources.id)"
                        + (
                            " AND NOT EXISTS(SELECT 1 FROM incremental_resource_conflicts a WHERE a.source_key=sources.id OR a.prior_source_key=sources.id)"
                            if incremental_audit
                            else ""
                        )
                        + (
                            " AND NOT EXISTS(SELECT 1 FROM append_event_audit a WHERE a.kept_source_key=sources.id OR a.dropped_source_key=sources.id)"
                            if audit_present
                            else ""
                        )
                    )
                    if audit_present:
                        output.execute(
                            "DELETE FROM append_event_heads WHERE state='active' AND NOT EXISTS(SELECT 1 FROM entries e WHERE e.id=append_event_heads.message_id)"
                        )
                    output.commit()
                    notify("encoding_json_cells")
                    codec_stats = _encode_derived_json(
                        output, lambda **counts: notify("encoding_json_cells", **counts)
                    )
                if enrichment_audit is not None:
                    from .wechat_quote_enrichment_reconcile import (
                        reconcile_reviewed_quotes,
                    )

                    notify("replaying_reviewed_quote_enrichments")
                    enrichment_result = reconcile_reviewed_quotes(
                        derived,
                        history,
                        catalog,
                        output,
                        audit=enrichment_audit,
                        manifest_path=restoration_manifest,
                        upstream=upstream,
                        active_selection=active_selection,
                        record_pruned=record,
                        codec_stats=codec_stats,
                        progress=lambda **counts: notify(
                            "replaying_reviewed_quote_enrichments", **counts
                        ),
                    )
                    restored = enrichment_result["counts"]
                    before_counts["native_quote"] = before_counts.get(
                        "native_quote", 0
                    ) + restored.get("verified_prior_projections", 0)
                    if restored.get("pruned_prior_upstream", 0):
                        pruned_counts["native_quote"] += restored[
                            "pruned_prior_upstream"
                        ]
                        reasons["upstream_precedence"] += restored[
                            "pruned_prior_upstream"
                        ]
                    codec_stats = enrichment_result["json_cell_transport"]
                    output.execute(
                        "INSERT OR REPLACE INTO archive_info VALUES('quote_enrichment_reconciliation',?)",
                        (_json(enrichment_result),),
                    )
                original_report = json.loads(info.get("report", "{}"))
                retained = {
                    key: count - pruned_counts[key]
                    for key, count in before_counts.items()
                    if count > pruned_counts[key]
                }
                original_report["counts"] = retained
                if enrichment_result is not None:
                    original_report["quote_enrichment_reconciliation"] = (
                        enrichment_result
                    )
                    rejected_report = original_report.setdefault("rejected", {})
                    remaining = enrichment_result["counts"].get(
                        "remaining_quarantines", 0
                    )
                    if remaining:
                        rejected_report["ambiguous_original_events"] = remaining
                    else:
                        rejected_report.pop("ambiguous_original_events", None)
                output.executemany(
                    "INSERT OR REPLACE INTO archive_info VALUES(?,?)",
                    [
                        ("report", _json(original_report)),
                        ("compaction_input_identities", _json(inputs)),
                        ("compaction_source_sha256", candidate_sha256),
                        ("compaction_pruned_counts", _json(dict(pruned_counts))),
                        ("json_cell_codec", "ACJ1"),
                        ("json_cell_transport_stats", _json(codec_stats)),
                        (
                            "compaction_manifest_uncompressed_sha256",
                            manifest_digest.hexdigest(),
                        ),
                    ],
                )
                output.commit()
                if not stream_compaction:
                    notify("vacuuming_candidate")
                    output.execute("VACUUM")
                if output.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise ValueError(
                        "The private compacted candidate failed SQLite verification."
                    )
        if compressed_manifest is not None:
            notify("verifying_manifest")
            compressed_manifest.close()
            compressed_manifest = None
            raw_manifest.flush()
            os.fsync(raw_manifest.fileno())
            raw_manifest.close()
            raw_manifest = None
            verified = hashlib.sha256()
            verified_bytes = 0
            with gzip.open(manifest, "rb") as stream:
                while block := stream.read(1024**2):
                    verified.update(block)
                    verified_bytes += len(block)
            if (
                verified.hexdigest() != manifest_digest.hexdigest()
                or verified_bytes != manifest_bytes
            ):
                raise ValueError(
                    "The private compaction manifest failed round-trip verification."
                )
        notify("checking_input_pins")
        if (
            _identity(source) != source_identity
            or _pins(history_path) != pins
            or _compaction_inputs(history_path) != inputs
            or _identity(target, optional=True) != expected_target
            or enrichment_audit is not None
            and _identity(Path(enrichment_audit["path"]))
            != enrichment_audit["identity"]
        ):
            raise ValueError("A completion compaction compare-and-swap input changed.")
        if output is not None:
            if not stream_compaction:
                output.execute(
                    "UPDATE archive_info SET value='complete' WHERE key='staging_state'"
                )
                output.commit()
            output.close()
            output = None
            verification_path = stream_owner["path"] if stream_owner else destination
            if stream_owner:
                from .wechat_completion_stream_copy import stream_file_owned

                if not stream_file_owned(stream_owner, destination):
                    raise ValueError(
                        "The streamed candidate lost its reserved public inode."
                    )
            if stream_owner:
                from .wechat_completion_stream_copy import sync_stream_file

                sync_stream_file(stream_owner)
            else:
                sync_completion_file(verification_path)
            notify("validating_compacted_candidate")
            final_identity = _identity(verification_path)
            try:
                with (
                    closing(_readonly(verification_path)) as final,
                    closing(_readonly(history_path)) as current_history,
                    closing(
                        _readonly(history_path.with_name("resource_references.sqlite3"))
                    ) as current_catalog,
                ):
                    _validate_entries(
                        final,
                        current_history,
                        current_catalog,
                        progress=lambda count: notify(
                            "validating_compacted_candidate", validated_entries=count
                        ),
                    )
                if stream_owner:
                    if (
                        _identity(verification_path) != final_identity
                        or _identity(destination) != final_identity
                    ):
                        raise ValueError(
                            "The streamed output changed during final proof validation."
                        )
                    with closing(
                        sqlite3.connect(
                            verification_path.as_uri() + "?mode=rw", uri=True
                        )
                    ) as complete:
                        if not stream_file_owned(stream_owner, verification_path):
                            raise ValueError(
                                "The private streamed inode changed before completion."
                            )
                        complete.execute(
                            "UPDATE archive_info SET value='complete' WHERE key='staging_state'"
                        )
                        complete.commit()
                    sync_stream_file(stream_owner)
                    final_identity = _identity(verification_path)
                notify("hashing_compacted_candidate")
                final_sha256 = _file_sha(verification_path)
                if (
                    _identity(verification_path) != final_identity
                    or _identity(destination) != final_identity
                    or _pins(history_path) != pins
                    or _compaction_inputs(history_path) != inputs
                    or _identity(target, optional=True) != expected_target
                    or enrichment_audit is not None
                    and _identity(Path(enrichment_audit["path"]))
                    != enrichment_audit["identity"]
                ):
                    raise ValueError("A final compacted validation input changed.")
                if stream_owner:
                    from .wechat_completion_stream_copy import release_stream_link

                    release_stream_link(stream_owner, destination)
                    stream_owner = None
                    after_release = _identity(destination)
                    if any(
                        after_release[field] != final_identity[field]
                        for field in ("dev", "ino", "size", "mtime_ns")
                    ):
                        raise ValueError(
                            "The verified streamed inode changed after releasing its private link."
                        )
                    final_identity = after_release
                _remember_validation(destination, final_identity, pins)
            except BaseException:
                reset_path = verification_path
                if (
                    not stream_compaction
                    or stream_owner
                    and stream_file_owned(stream_owner, reset_path)
                ):
                    with closing(
                        sqlite3.connect(reset_path.as_uri() + "?mode=rw", uri=True)
                    ) as incomplete:
                        if stream_owner and not stream_file_owned(
                            stream_owner, reset_path
                        ):
                            raise ValueError(
                                "The private streamed inode changed before its failure marker."
                            )
                        incomplete.execute(
                            "UPDATE archive_info SET value='in_progress' WHERE key='staging_state'"
                        )
                        incomplete.commit()
                raise
        report = {
            "version": CODE_VERSION,
            "published": False,
            "dry_run": bool(dry_run),
            "compaction_mode": "stream" if stream_compaction else "clone",
            "quote_enrichment_reconciliation": enrichment_result,
            "source_candidate": str(source),
            "source_candidate_sha256": candidate_sha256,
            "source_candidate_bytes": source_identity["size"],
            "source_validation_cache_reused": source_validation_reused,
            "input_identities": pins,
            "upstream_input_identities": inputs,
            "expected_existing_catalog": expected_target,
            "before_counts": before_counts,
            "stored_before_counts": stored_before_counts,
            "pruned_counts": dict(pruned_counts),
            "pruned_reasons": dict(reasons),
            "retained_counts": {
                key: count - pruned_counts[key]
                for key, count in before_counts.items()
                if count > pruned_counts[key]
            },
            "manifest_uncompressed_sha256": manifest_digest.hexdigest(),
            "manifest_uncompressed_bytes": manifest_bytes,
        }
        if not dry_run:
            notify("hashing_compacted_candidate")
            report.update(
                candidate=str(destination),
                candidate_identity=final_identity,
                candidate_sha256=final_sha256,
                validation_cache_seeded=True,
                candidate_bytes=destination.stat().st_size,
                manifest=str(manifest),
                manifest_sha256=_file_sha(manifest),
                manifest_bytes=manifest.stat().st_size,
                json_cell_transport=codec_stats,
                stream_copy_rows=stream_stats["copied_rows"]
                if stream_compaction
                else None,
                removed_catalog_bytes=source_identity["size"]
                - destination.stat().st_size,
            )
        notify("complete", pruned_entries=sum(reasons.values()))
        report.update(
            elapsed_seconds=time.monotonic() - started, phase_seconds=phase_seconds
        )
        return report
    finally:
        if compressed_manifest is not None:
            compressed_manifest.close()
        if raw_manifest is not None:
            raw_manifest.close()
        if output is not None:
            output.rollback()
            output.close()
        if stream_owner is not None:
            from .wechat_completion_stream_copy import (
                release_stream_link,
                stream_file_owned,
            )

            if stream_file_owned(stream_owner, destination):
                release_stream_link(stream_owner, destination)
            else:
                notify(
                    "private_stream_artifact_retained", path=str(stream_owner["path"])
                )


def completion_message_conversation(history_path, message_id):
    """Locate a derived ID; callers still validate its complete SQL scope."""
    history_path = Path(history_path)
    target = history_path.with_name(FILENAME)
    identity = _identity(target, optional=True)
    if identity is None:
        return None
    with closing(_readonly(target)) as derived:
        if _metadata(derived) != _pins(history_path):
            raise ValueError("The derived message locator is stale.")
        row = derived.execute(
            "SELECT conversation_id FROM entries WHERE id=?", (message_id,)
        ).fetchone()
    if _identity(target) != identity:
        raise ValueError("The derived message locator changed.")
    return row[0] if row is not None else None


def _active_predicate(message_table, *, membership_table=None):
    """Share the exact upstream precedence predicate with private compaction."""
    if not _SQL.fullmatch(message_table):
        raise ValueError("Unsafe completion timeline table name.")
    if membership_table is not None and _SQL.fullmatch(membership_table) is None:
        raise ValueError("Unsafe completion membership table name.")
    membership = membership_table or message_table
    server = "b.server_id" if membership_table else "json_extract(b.provenance,'$.server_id')"
    equality = " AND ".join(
        'b."'
        + column
        + "\" IS json_extract(_wechat_completion_json_v1(e.original_row),'$."
        + '"'
        + column
        + '"'
        + "')"
        for column in COLUMNS
    )
    return (
        "((e.operation='replace' AND EXISTS(SELECT 1 FROM "
        + message_table
        + " b WHERE b.id=e.id AND "
        + equality
        + ")) OR (e.operation='append' AND NOT EXISTS(SELECT 1 FROM "
        + membership
        + " b WHERE b.conversation_id=e.conversation_id AND "
        "(b.id=e.id OR " + server + "=json_extract(_wechat_completion_json_v1(e.plan),'$.event.server_id')))))"
    )


def attach_secondary_message_completions(
    connection, history_path, conversation_table, message_table, *, conversation_id=None
):
    """Apply validated rows in SQL before paging, search, export, or event grouping."""
    history_path = Path(history_path)
    target = history_path.with_name(FILENAME)
    if not target.exists() and not target.is_symlink():
        return conversation_table, message_table, "absent"
    if not _SQL.fullmatch(conversation_table) or not _SQL.fullmatch(message_table):
        raise ValueError("Unsafe completion timeline table name.")
    try:
        register_json_codec(connection)
        target_identity = _identity(target)
        pins = _pins(history_path)
        key = _validation_cache_key(target, target_identity, pins, conversation_id)
        with (
            closing(_readonly(target)) as derived,
            closing(_readonly(history_path)) as history,
            closing(
                _readonly(history_path.with_name("resource_references.sqlite3"))
            ) as catalog,
        ):
            if _metadata(derived) != pins:
                return conversation_table, message_table, "stale"
            with _LOCK:
                verified = (
                    key in _CACHE
                    or _validation_cache_key(target, target_identity, pins) in _CACHE
                )
            if not verified:
                _validate_entries(
                    derived, history, catalog, conversation_id=conversation_id
                )
                with _LOCK:
                    if len(_CACHE) > 128:
                        _CACHE.clear()
                    _CACHE[key] = True
        attached = {
            row[1]: row[2] for row in connection.execute("PRAGMA database_list")
        }
        if "secondary_completions" in attached:
            if Path(attached["secondary_completions"]).absolute() != target.absolute():
                raise ValueError(
                    "Another completion catalog already owns the SQL alias."
                )
        else:
            connection.execute(
                "ATTACH DATABASE ? AS secondary_completions",
                (target.absolute().as_uri() + "?mode=ro&immutable=1",),
            )
        connection.execute("DROP TABLE IF EXISTS temp.secondary_completion_active")
        connection.execute(
            "CREATE TEMP TABLE secondary_completion_active(id TEXT PRIMARY KEY)"
        )
        from .wechat_completion_selection import BoundedActiveSelection

        with BoundedActiveSelection(connection, message_table) as active_selection:
            for entry in active_selection.entries(
                "secondary_completions.entries", conversation_id=conversation_id or None
            ):
                if entry["is_active"]:
                    connection.execute(
                        "INSERT INTO secondary_completion_active VALUES(?)", (entry["id"],)
                    )
        connection.execute("DROP VIEW IF EXISTS temp." + MESSAGE_TABLE)
        columns = ",".join('b."' + column + '"' for column in COLUMNS)
        projected = ",".join(
            '_wechat_completion_json_v1(m."provenance") AS provenance'
            if column == "provenance"
            else 'm."' + column + '"'
            for column in COLUMNS
        )
        connection.execute(
            "CREATE TEMP VIEW "
            + MESSAGE_TABLE
            + " AS SELECT "
            + columns
            + " FROM "
            + message_table
            + " b WHERE NOT EXISTS(SELECT 1 FROM secondary_completion_active a WHERE a.id=b.id) UNION ALL SELECT "
            + projected
            + " FROM secondary_completions.messages m JOIN secondary_completion_active a ON a.id=m.id"
        )
        connection.execute("DROP VIEW IF EXISTS temp." + CONVERSATION_TABLE)
        connection.execute(
            "DROP TABLE IF EXISTS temp.secondary_completion_conversations"
        )
        connection.execute(
            "CREATE TEMP TABLE secondary_completion_conversations AS SELECT * FROM "
            + conversation_table
            + " WHERE 0"
        )
        affected = [
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT m.conversation_id FROM secondary_completions.messages m JOIN secondary_completion_active a ON a.id=m.id"
            )
        ]
        for identifier in affected:
            original = connection.execute(
                "SELECT * FROM " + conversation_table + " WHERE id=?", (identifier,)
            ).fetchone()
            if original is None:
                raise ValueError(
                    "A completion cannot create an unverified conversation."
                )
            values = dict(original)
            values["message_count"] = connection.execute(
                "SELECT COUNT(*) FROM " + MESSAGE_TABLE + " WHERE conversation_id=?",
                (identifier,),
            ).fetchone()[0]
            first = connection.execute(
                "SELECT time_sort,timestamp_value FROM "
                + MESSAGE_TABLE
                + " WHERE conversation_id=? ORDER BY (time_sort>0) DESC,time_sort,ordinal,id LIMIT 1",
                (identifier,),
            ).fetchone()
            last = connection.execute(
                "SELECT time_sort,timestamp_value,text FROM "
                + MESSAGE_TABLE
                + " WHERE conversation_id=? ORDER BY (time_sort>0) DESC,time_sort DESC,ordinal DESC,id DESC LIMIT 1",
                (identifier,),
            ).fetchone()
            values.update(
                first_sort=first[0],
                first_time=first[1],
                last_sort=last[0],
                last_time=last[1],
                latest_text=last[2],
            )
            connection.execute(
                "INSERT INTO secondary_completion_conversations VALUES("
                + ",".join("?" for _ in CONVERSATION_COLUMNS)
                + ")",
                tuple(values[column] for column in CONVERSATION_COLUMNS),
            )
        connection.execute(
            "CREATE TEMP VIEW "
            + CONVERSATION_TABLE
            + " AS SELECT c.* FROM "
            + conversation_table
            + " c WHERE NOT EXISTS(SELECT 1 FROM secondary_completion_conversations d WHERE d.id=c.id) UNION ALL SELECT * FROM secondary_completion_conversations"
        )
        if _identity(target) != target_identity or _pins(history_path) != pins:
            raise ValueError("A completion source changed while building the SQL view.")
        return CONVERSATION_TABLE, MESSAGE_TABLE, "ready"
    except (
        OSError,
        sqlite3.Error,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
    ):
        return conversation_table, message_table, "invalid"
