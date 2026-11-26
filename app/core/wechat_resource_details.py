"""Apply bounded, authenticated resource details without rewriting source catalogs.

Code version: v1.8.1-codex.0
"""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

from . import wechat_macos_recovery_bank as bank
from .owner_only_files import owner_only_problem
from .wechat_completion_json_codec import load_json_cell
from .wechat_reconcile import native_text_metadata
from .wechat_resource_sender_evidence import (
    _HEADER_FIELDS,
    _json,
    match_secondary_native_event,
    recover_native_resource_sender_from_secondary,
    secondary_native_events,
    validate_secondary_native_event,
)
from .wechat_text import native_components_text
from .windows_file_metadata import file_change_time


CODE_VERSION = "v1.8.1-codex.0"
VERSION = "v1.0.0-codex.0"
IS_WINDOWS = os.name == "nt"
FILENAME = "resource_details.json"
MESSAGE_TABLE = "resource_detail_timeline_messages"
MAX_ENTRIES = 20_000
MAX_BYTES = 32 * 1024**2
_TRANSPORT = "shared-native-proof-v1"
COLUMNS = (
    "id", "conversation_id", "ordinal", "sender_id", "sender_label", "direction",
    "text", "time_sort", "timestamp_value", "timestamp_label", "time_basis",
    "provenance", "quality_flags", "search_text",
)
_SCOPE = ("id", "partition_id", "account_key", "peer_id", "kind")
_LABELS = ("app_subtype", "app_metadata_version", *bank._APP_LABEL_FIELDS)
_TABLE = re.compile(r"(?:(?:main|temp)\.)?[A-Za-z_][A-Za-z0-9_]*")
_FTS_MESSAGE_TABLE = re.compile(r"fts5_message_table_[0-9]+_content")
_HEX = re.compile(r"[0-9a-f]{64}")
_MESSAGE_SCHEMA = """
CREATE TEMP TABLE IF NOT EXISTS resource_detail_replacements(
 id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,ordinal INTEGER NOT NULL,
 sender_id TEXT,sender_label TEXT NOT NULL,direction TEXT NOT NULL,text TEXT NOT NULL,
 time_sort REAL NOT NULL,timestamp_value TEXT NOT NULL,timestamp_label TEXT NOT NULL,
 time_basis TEXT NOT NULL,provenance TEXT NOT NULL,quality_flags TEXT NOT NULL,
 search_text TEXT NOT NULL
) WITHOUT ROWID
"""


def _sha(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def row_sha256(row):
    """Pin the complete upstream SQL row, normalizing its REAL sort coordinate."""
    try:
        normalized = {key: row[key] for key in COLUMNS}
        normalized["time_sort"] = float(normalized["time_sort"])
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError("Resource details need a complete upstream message row.") from None
    return _sha(normalized)


def _conversation_scope(conversation):
    if not isinstance(conversation, dict):
        raise ValueError("Resource details need their exact original conversation.")
    result = {key: conversation[key] for key in _SCOPE if key in conversation}
    if (
        result.keys() != set(_SCOPE)
        or not isinstance(result["id"], str)
        or re.fullmatch(r"wechat:[0-9a-f]{64}", result["id"]) is None
        or result["kind"] not in {"group", "direct"}
        or not isinstance(result["peer_id"], str)
        or not result["peer_id"]
        or (result["kind"] == "group") != result["peer_id"].endswith(("@chatroom", "@im.chatroom"))
        or not isinstance(result["account_key"], str)
        or not result["account_key"].startswith("account:")
    ):
        raise ValueError("Resource details require an original native conversation scope.")
    owner = result["account_key"].removeprefix("account:")
    if result["partition_id"] != "ios-" + hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest():
        raise ValueError("Resource details lost the native source account partition.")
    return result


def _cross_viewer_completion(connection, row, header, matched):
    """Require an active sender completion for the same physical native event."""
    if connection is None:
        raise ValueError("Cross-viewer resource details need an active native sender completion.")
    proof = json.loads(row["provenance"]).get("secondary_resource_completion")
    if (
        not isinstance(proof, dict)
        or proof.get("strategy") != "native_sender"
        or proof.get("clock_skew_seconds") != 0
        or proof.get("primary") != matched["primary"]
        or matched["primary"]["message_type"] != 49
    ):
        raise ValueError("Cross-viewer resource details need an exact native sender completion.")
    secondary = proof.get("secondary_source")
    donor = matched["secondary_source"]
    if (
        not isinstance(secondary, dict)
        or not isinstance(secondary.get("source"), dict)
        or not row.get("sender_id")
        or row["sender_id"] != donor.get("sender_id")
        or any(
            secondary.get(key) != donor.get(key)
            for key in (
                "account_id", "local_id", "local_type", "server_id", "create_time",
                "content_sha256", "sender_id", "real_sender_id", "sender_proof",
            )
        )
        or any(
            secondary["source"].get(key) != donor["source"].get(key)
            for key in ("account_directory", "database_relative_path", "table")
        )
    ):
        raise ValueError("The cross-viewer resource witness differs from its completed native event.")
    try:
        active = connection.execute(
            "SELECT e.original_sha256,e.projected_sha256 "
            "FROM secondary_completions.entries e "
            "JOIN temp.secondary_completion_active a ON a.id=e.id "
            "WHERE e.id=? AND e.conversation_id=? "
            "AND e.operation='replace' AND e.strategy='native_sender' "
            "AND EXISTS (SELECT 1 FROM main.conversations c WHERE c.id=? "
            "AND c.account_key=? AND c.peer_id=? AND c.kind='group')",
            (
                row["id"], row["conversation_id"], header["session_id"],
                "account:" + header["account_id"], header["peer_id"],
            ),
        ).fetchone()
    except sqlite3.OperationalError:
        active = None
    if active is None or tuple(active) != (proof.get("original_row_sha256"), row_sha256(row)):
        raise ValueError("Cross-viewer resource details need an active native sender completion.")


def _save_text_sender(row, proof, header, message, **observations):
    flags = json.loads(row["quality_flags"])
    if not isinstance(flags, list) or any(not isinstance(flag, str) for flag in flags):
        raise ValueError("Text sender details require retained string quality flags.")
    proof["native_text_sender_details"] = {
        "version": VERSION,
        "original_row_sha256": row_sha256(row),
        "source_account_id": header["account_id"],
        "source_session_id": header["session_id"],
        "source": deepcopy(header["source"]),
        "local_id": message["local_id"],
        "content_sha256": message["content_sha256"],
        "sender_proof": deepcopy(message["sender_proof"]),
        **observations,
    }
    result = dict(row)
    sender = message["sender_id"]
    result["sender_id"] = sender
    if not row["sender_label"] or row["sender_label"] == "Unknown sender":
        result["sender_label"] = sender
    result["quality_flags"] = _json([*flags, "authenticated_text_sender_verified"])
    result["provenance"] = _json(proof)
    if sender.casefold() not in row["search_text"]:
        result["search_text"] += "\n" + sender.casefold()
    return result


def _project_text_sender(row, scope, header, message, connection):
    proof = json.loads(row["provenance"])
    locator = proof.get("locator")
    native_text = message.get("native_text")
    peer = scope["peer_id"]
    owner = scope["account_key"].removeprefix("account:")
    if (
        connection is None
        or scope["kind"] != "group"
        or row["conversation_id"] != scope["id"]
        or header["peer_id"] != peer
        or message["local_type"] != 1
        or proof.get("message_type") != 1
        or proof.get("text_extraction_kind") != "plain_text"
        or proof.get("server_id") != message["server_id"]
        or type(proof.get("unix_timestamp")) is not int
        or proof["unix_timestamp"] != message["create_time"]
        or not isinstance(locator, dict)
        or locator.get("table") != "Chat_" + hashlib.md5(peer.encode(), usedforsecurity=False).hexdigest()
        or type(locator.get("local_id")) is not int
        or locator["local_id"] <= 0
        or not isinstance(native_text, dict)
        or native_text.get("text_extraction_kind") != "plain_text"
    ):
        raise ValueError("Text sender details need an exact archived group event.")
    if row.get("sender_id"):
        return dict(row)
    sender = message["sender_id"]
    if (
        not sender or sender.endswith(("@chatroom", "@im.chatroom"))
        or row["direction"] not in {"unknown", "sent" if sender == owner else "received"}
    ):
        raise ValueError("The native text sender conflicts with the archived direction.")
    bank._name_proof(message.get("sender_proof"), sender, message["real_sender_id"])
    donor = connection.execute(
        "SELECT 1 FROM main.conversations WHERE id=? AND account_key=? AND peer_id=? AND kind='group'",
        (header["session_id"], "account:" + header["account_id"], peer),
    ).fetchone()
    if donor is None:
        raise ValueError("Text sender details need the authenticated donor conversation.")
    stamp = datetime.fromisoformat(row["timestamp_value"])
    if stamp.tzinfo is None or stamp.timestamp() != message["create_time"]:
        raise ValueError("The native text sender differs from the archived event clock.")
    mode = "literal_text"
    if row["text"] != message["text"]:
        corroboration = locator.get("corroboration")
        fts_sources = [
            source for source in corroboration
            if isinstance(source, dict) and source.get("database") == "fts/fts_message.db"
        ] if isinstance(corroboration, list) else []
        if locator.get("text_origin") != "corroborated_recovery_index" or not fts_sources:
            raise ValueError("The archived text differs from its exact native sender witness.")
        indexed = row["text"].replace("，", ",")
        native = message["text"].replace("，", ",")
        if indexed == native:
            mode = "corroborated_fts_comma_width"
        elif indexed.replace("？", "?") == native.replace("？", "?") and any(
            isinstance(source.get("table"), str)
            and _FTS_MESSAGE_TABLE.fullmatch(source["table"]) is not None
            and type(source.get("row_id")) is int
            and source["row_id"] > 0
            for source in fts_sources
        ):
            # Match the sender witness while retaining the original indexed punctuation.
            mode = "corroborated_fts_punctuation_width"
        else:
            raise ValueError("The archived text differs from its exact native sender witness.")
    return _save_text_sender(
        row, proof, header, message, basis="authenticated_native_group_text_sender",
        text_match=mode,
    )


def _project_completed_quote_sender(row, scope, header, message, connection):
    """Add the author to an active quote when an exact-clock native witness agrees."""
    proof = json.loads(row["provenance"])
    completion = proof.get("secondary_resource_completion")
    sender = message["sender_id"]
    owner = scope["account_key"].removeprefix("account:")
    if (
        connection is None or scope["kind"] != "group" or row["sender_id"]
        or header["account_id"] != owner
        or row["conversation_id"] != header["session_id"] or header["session_id"] != scope["id"]
        or header["peer_id"] != scope["peer_id"]
        or message["local_type"] != ((57 << 32) | 49)
        or proof.get("message_type") != 49
        or proof.get("server_id") != message["server_id"]
        or type(proof.get("unix_timestamp")) is not int
        or proof["unix_timestamp"] != message["create_time"]
        or row["text"] != message["text"]
        or not sender or sender.endswith(("@chatroom", "@im.chatroom"))
        or row["direction"] not in {"unknown", "sent" if sender == owner else "received"}
        or not isinstance(completion, dict) or completion.get("strategy") != "native_quote"
        or type(completion.get("clock_skew_seconds")) is not int
        or abs(completion["clock_skew_seconds"]) != 1
    ):
        raise ValueError("Quote sender details need an exact-clock witness for the active quote.")
    stamp = datetime.fromisoformat(row["timestamp_value"])
    if stamp.tzinfo is None or stamp.timestamp() != message["create_time"]:
        raise ValueError("The native quote sender differs from the archived event clock.")
    try:
        entry = connection.execute(
            "SELECT e.projected_sha256,w.message,w.sha256 "
            "FROM temp.secondary_completion_active a "
            "JOIN secondary_completions.entries e ON e.id=a.id "
            "JOIN secondary_completions.witnesses w ON w.id=e.witness_key AND w.source_key=e.source_key "
            "WHERE e.id=? AND e.conversation_id=? AND e.operation='replace' AND e.strategy='native_quote'",
            (row["id"], scope["id"]),
        ).fetchone()
    except sqlite3.OperationalError:
        entry = None
    if entry is None or row_sha256(row) != entry[0]:
        raise ValueError("Quote sender details need the verified active completion.")
    retained = load_json_cell(entry[1])
    if (
        not isinstance(retained, dict) or _sha(retained) != entry[2]
        or any(retained.get(key) != message.get(key) for key in (
            "local_type", "server_id", "sender_id", "content_sha256", "text", "native_text",
        ))
        or retained.get("create_time") != message["create_time"] + completion["clock_skew_seconds"]
    ):
        raise ValueError("The exact-clock quote witness conflicts with retained native evidence.")
    return _save_text_sender(
        row, proof, header, message, basis="authenticated_native_completed_quote_sender",
        text_match="identical_retained_quote", retained_source_create_time=retained["create_time"],
        native_source_create_time=message["create_time"],
    )


def project_resource_details(row, conversation, event, *, connection=None):
    """Replay exact native senders, app-card labels, and forwarded transcripts.

    Callers must authenticate the native source and revalidate its account binding
    independently. The registry reader performs the archive-side binding replay.
    """
    scope = _conversation_scope(conversation)
    header, message = validate_secondary_native_event(event)
    owner = scope["account_key"].removeprefix("account:")
    if "account_binding" not in header:
        raise ValueError("Resource details need an authenticated native source account.")
    if message["local_type"] == 1:
        return _project_text_sender(row, scope, header, message, connection)
    if json.loads(row["provenance"]).get("text_extraction_kind") == "quoted_reply":
        return _project_completed_quote_sender(row, scope, header, message, connection)
    if scope["kind"] == "direct":
        from .wechat_resource_sender_evidence import match_same_account_direct_app_event

        matched = match_same_account_direct_app_event(row, scope, event)
    else:
        matched = match_secondary_native_event(row, scope, [event])
    if matched is None:
        raise ValueError("Resource details need an exact native event clock and identity.")
    same_viewer = header["account_id"] == owner
    if scope["kind"] == "direct":
        result = dict(row)
    elif same_viewer:
        result = recover_native_resource_sender_from_secondary(row, scope, [event])["message"]
    else:
        _cross_viewer_completion(connection, row, header, matched)
        result = dict(row)
    proof = json.loads(result["provenance"])
    reference = proof["resource_reference"]
    resource = event.get("resource")
    native = event.get("native_text")
    added = {}
    forwarding = (
        isinstance(native, dict)
        and native.get("text_extraction_kind") == "forwarded_text"
        and native.get("appmsg_subtype") == 19
    )
    if forwarding:
        if (
            matched["primary"]["message_type"] != 49
            or row["text"] != "[App message]"
            or proof.get("text_extraction_kind") != "resource_placeholder"
            or reference.get("kind") != "app"
            or reference.get("app_subtype") not in (None, "", "19")
            or reference.get("safe_title") not in (None, "", native.get("forwarded_record_title"))
        ):
            raise ValueError("The forwarded transcript conflicts with the retained app card.")
        metadata = native_text_metadata(native, {})
        body = native_components_text(metadata.get("text_components", []))
        if not body.strip() or body != message["text"]:
            raise ValueError("The forwarded transcript lost its explicit native body.")
        remainder = result["search_text"]
        original_prefix = row["text"].casefold()
        if remainder.startswith(original_prefix):
            remainder = remainder[len(original_prefix):].lstrip("\n")
        result["text"] = body
        result["search_text"] = body.casefold() + ("\n" + remainder if remainder else "")
        proof.pop("resource_reference")
        proof.update(metadata, text_extraction_kind="forwarded_text")
    elif (
        matched["primary"]["message_type"] == 49
        and reference.get("kind") == "app"
        and isinstance(resource, dict)
        and resource.get("kind") == "app"
        and resource.get("app_metadata_version") == "1"
    ):
        # The existing packet validator owns field bounds and literal source paths.
        if not bank._valid_resource_labels(resource, 49):
            raise ValueError("Resource details contain unsupported native app labels.")
        for key in _LABELS:
            if key not in resource or not resource[key]:
                continue
            value = resource[key]
            if reference.get(key) not in (None, "", value):
                raise ValueError("Native app details conflict with retained card metadata.")
            if reference.get(key) != value:
                added[key] = value
        if added:
            proof["resource_reference"] = {**reference, **added}
    if not added and result == dict(row):
        return dict(row)
    proof["native_resource_details"] = {
        "version": VERSION,
        "basis": (
            "exact_same_account_authenticated_native_event" if same_viewer
            else "exact_native_sender_completion_event"
        ),
        "original_row_sha256": row_sha256(row),
        "primary": matched["primary"],
        "secondary_source": matched["secondary_source"],
        "clock_skew_seconds": 0,
        "app_fields": added,
    }
    flags = json.loads(result["quality_flags"])
    if not isinstance(flags, list) or any(not isinstance(flag, str) for flag in flags):
        raise ValueError("Resource details require retained string quality flags.")
    if forwarding:
        flags = [
            *(flag for flag in flags if flag != "resource_placeholder"),
            *native["flags"],
        ]
    result["quality_flags"] = _json(list(dict.fromkeys([
        *flags, "authenticated_resource_details_verified",
    ])))
    result["provenance"] = _json(proof)
    terms = [
        value.casefold() for key, value in added.items()
        if key in bank._APP_LABEL_FIELDS and not key.endswith("_source")
    ]
    if terms:
        result["search_text"] += "\n" + "\n".join(dict.fromkeys(terms))
    return result


def make_entry(row, conversation, packet, *, connection=None):
    """Prepare one bounded registry entry from an already authenticated packet.

    The packet must contain exactly its selected event and retain its complete
    existing account proof. Staging must use the upstream timeline before this
    detail layer so that repeated publication never forms a chain of overrides.
    """
    if not isinstance(packet, dict) or len(packet.get("messages", ())) != 1:
        raise ValueError("A resource detail entry needs one exact native witness.")
    events = secondary_native_events(packet)
    provenance = json.loads(row["provenance"])
    if "native_resource_details" in provenance or "native_text_sender_details" in provenance:
        raise ValueError("Stage resource details against the original upstream timeline.")
    projected = project_resource_details(row, conversation, events[0], connection=connection)
    if projected == dict(row):
        raise ValueError("The selected native witness adds no resource details.")
    entry = {
        "message_id": row["id"],
        "original_row_sha256": row_sha256(row),
        "conversation": _conversation_scope(conversation),
        "packet": deepcopy(packet),
    }
    if len(_json(entry).encode()) > MAX_BYTES:
        raise ValueError("The selected resource detail proof exceeds its byte budget.")
    return entry


def _metadata_pin(path, state, *, descriptor=None):
    change_time_ns = state.st_ctime_ns
    if IS_WINDOWS:
        # Use the same native mutation timestamp for a pathname and an open file.
        ticks = file_change_time(path, descriptor=descriptor)
        change_time_ns = (ticks - 116_444_736_000_000_000) * 100
    return {
        "dev": state.st_dev, "inode": state.st_ino,
        "size": state.st_size, "mtime_ns": state.st_mtime_ns,
        "ctime_ns": change_time_ns,
    }


def _registry_metadata(path):
    try:
        state = path.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(state.st_mode) or state.st_size > MAX_BYTES
        or owner_only_problem(path)
        or getattr(state, "st_flags", 0) & 0x40000000
        or path.parent.is_symlink()
    ):
        raise ValueError("Resource details need a bounded private resident registry.")
    return _metadata_pin(path, state)


@contextmanager
def _open_registry(path, expected):
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
    )
    with os.fdopen(descriptor, "rb") as stream:
        if _metadata_pin(path, os.fstat(descriptor), descriptor=descriptor) != expected:
            raise ValueError("The resource detail registry changed before reading.")
        yield stream
        if _metadata_pin(path, os.fstat(descriptor), descriptor=descriptor) != expected:
            raise ValueError("The resource detail registry changed while reading.")
    if _registry_metadata(path) != expected:
        raise ValueError("The resource detail registry changed while reading.")


def registry_identity(history_path):
    """Pin the private resident registry, including its bytes on Windows."""
    path = Path(history_path).with_name(FILENAME)
    identity = _registry_metadata(path)
    if identity is not None and IS_WINDOWS:
        # An in-place overwrite and mtime restore can leave the NTFS change
        # timestamp tied. Hash only this bounded, already-resident registry.
        digest = hashlib.sha256()
        remaining = identity["size"]
        with _open_registry(path, identity) as stream:
            while remaining and (chunk := stream.read(min(remaining, 1024**2))):
                digest.update(chunk)
                remaining -= len(chunk)
            if remaining or stream.read(1):
                raise ValueError("The resource detail registry changed while reading.")
        identity["sha256"] = digest.hexdigest()
    return identity


def detail_input_identities(history_path):
    """Pin absence as well as publication for readers, exports and derived caches."""
    from .wechat_resource_detail_index import FILENAME as INDEX_FILENAME, index_identity

    path = Path(history_path).with_name(FILENAME)
    indexed = path.with_name(INDEX_FILENAME)
    identities = {str(path.resolve()): registry_identity(history_path)}
    if index_pin := index_identity(indexed):
        identities[str(indexed.absolute())] = index_pin
    return identities


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate resource detail registry field.")
        value[key] = item
    return value


def _entry_shape(entry):
    if (
        not isinstance(entry, dict)
        or set(entry) != {"message_id", "original_row_sha256", "conversation", "packet"}
        or not isinstance(entry["message_id"], str)
        or _HEX.fullmatch(entry["message_id"]) is None
        or not isinstance(entry["original_row_sha256"], str)
        or _HEX.fullmatch(entry["original_row_sha256"]) is None
    ):
        raise ValueError("Resource detail entries need unique exact source pins.")
    return _conversation_scope(entry["conversation"])


def _transport_entry(entry):
    scope = _entry_shape(entry)
    packet = entry["packet"]
    if (
        not isinstance(packet, dict) or not isinstance(packet.get("messages"), list)
        or len(packet["messages"]) != 1 or "account_binding" not in packet
        or packet.get("peer_id") != scope["peer_id"]
        or (
            packet.get("session_id") == scope["id"]
            and packet.get("account_id") != scope["account_key"].removeprefix("account:")
        )
    ):
        raise ValueError("A resource detail entry lost its authenticated native room event.")
    bank._validate_packet(packet, (packet["account_id"],))
    return scope, packet


def encode_registry(entries):
    """Encode bounded logical entries with shared immutable proof headers.

    Canonical ordering makes exact replay deterministic. Repeated message IDs are
    refused, including equal duplicates; a publisher must merge retries explicitly
    rather than accidentally publishing competing proof entries.
    """
    if isinstance(entries, (str, bytes, dict)):
        raise ValueError("A resource detail registry needs an entry sequence.")
    sources, conversations, compact, seen = {}, {}, [], set()
    estimated_bytes = 256
    for entry in entries:
        if len(compact) >= MAX_ENTRIES:
            raise ValueError("The resource detail registry exceeds its entry budget.")
        scope, packet = _transport_entry(entry)
        identifier = entry["message_id"]
        if identifier in seen:
            raise ValueError("A resource detail registry repeats a message ID.")
        seen.add(identifier)
        header = {key: packet[key] for key in _HEADER_FIELDS if key in packet}
        source_key, conversation_key = _sha(header), _sha(scope)
        if source_key not in sources:
            estimated_bytes += len(_json(header).encode()) + len(source_key) + 4
            sources[source_key] = header
        if conversation_key not in conversations:
            estimated_bytes += len(_json(scope).encode()) + len(conversation_key) + 4
            conversations[conversation_key] = scope
        item = {
            "message_id": identifier,
            "original_row_sha256": entry["original_row_sha256"],
            "conversation_key": conversation_key,
            "source_key": source_key,
            "message": packet["messages"][0],
        }
        estimated_bytes += len(_json(item).encode()) + 1
        if estimated_bytes > MAX_BYTES:
            raise ValueError("The resource detail registry exceeds its byte budget.")
        compact.append(item)
    value = {
        "version": VERSION, "transport": _TRANSPORT,
        "sources": sources, "conversations": conversations,
        "entries": sorted(compact, key=lambda entry: entry["message_id"]),
    }
    encoded = _json(value).encode()
    if len(encoded) > MAX_BYTES:
        raise ValueError("The resource detail registry exceeds its byte budget.")
    return encoded


def _decode_compact(value):
    if (
        set(value) != {"version", "transport", "sources", "conversations", "entries"}
        or value.get("transport") != _TRANSPORT
        or not isinstance(value["sources"], dict)
        or not isinstance(value["conversations"], dict)
        or max(len(value["sources"]), len(value["conversations"])) > MAX_ENTRIES
    ):
        raise ValueError("Unsupported compact resource detail registry.")
    sources, conversations = value["sources"], value["conversations"]
    for key, header in sources.items():
        if (
            not isinstance(key, str) or _HEX.fullmatch(key) is None
            or not isinstance(header, dict) or set(header) - _HEADER_FIELDS
            or _sha(header) != key
        ):
            raise ValueError("A shared native detail header lost its digest.")
    for key, conversation in conversations.items():
        scope = _conversation_scope(conversation)
        if (
            not isinstance(key, str) or _HEX.fullmatch(key) is None
            or conversation != scope or _sha(scope) != key
        ):
            raise ValueError("A shared resource detail scope lost its digest.")
    entries, used_sources, used_conversations = [], set(), set()
    for item in value["entries"]:
        if (
            not isinstance(item, dict)
            or set(item) != {
                "message_id", "original_row_sha256", "conversation_key", "source_key", "message",
            }
            or not isinstance(item["source_key"], str)
            or not isinstance(item["conversation_key"], str)
            or item["source_key"] not in sources
            or item["conversation_key"] not in conversations
        ):
            raise ValueError("A compact resource detail entry lost its proof references.")
        header = sources[item["source_key"]]
        # Nested immutable proof objects are shared to keep large batches bounded.
        entry = {
            "message_id": item["message_id"],
            "original_row_sha256": item["original_row_sha256"],
            "conversation": conversations[item["conversation_key"]],
            "packet": {**header, "messages": [item["message"]]},
        }
        _transport_entry(entry)
        entries.append(entry)
        used_sources.add(item["source_key"])
        used_conversations.add(item["conversation_key"])
    if used_sources != sources.keys() or used_conversations != conversations.keys():
        raise ValueError("A compact resource detail registry contains unused proof objects.")
    return entries


def _read_registry(path, expected):
    with _open_registry(path, expected) as stream:
        content = stream.read(MAX_BYTES + 1)
    if len(content) != expected["size"]:
        raise ValueError("The resource detail registry changed while reading.")
    if IS_WINDOWS:
        expected = {**expected, "sha256": hashlib.sha256(content).hexdigest()}
    try:
        value = json.loads(content, object_pairs_hook=_unique)
    except (UnicodeError, RecursionError):
        raise ValueError("Invalid resource detail registry JSON.") from None
    if (
        not isinstance(value, dict) or value.get("version") != VERSION
        or not isinstance(value.get("entries"), list)
        or len(value["entries"]) > MAX_ENTRIES
    ):
        raise ValueError("Unsupported resource detail registry.")
    if "transport" in value:
        entries = _decode_compact(value)
    elif set(value) == {"version", "entries"}:
        entries = value["entries"]
    else:
        raise ValueError("Unsupported resource detail registry.")
    seen = set()
    for entry in entries:
        _entry_shape(entry)
        if entry["message_id"] in seen:
            raise ValueError("Resource detail entries need unique exact source pins.")
        seen.add(entry["message_id"])
    return entries, expected


def load_registry(history_path):
    """Load flat or compact logical entries and their immutable publication pin."""
    path = Path(history_path).with_name(FILENAME)
    metadata = _registry_metadata(path)
    if metadata is None:
        return [], None
    entries, identity = _read_registry(path, metadata)
    if registry_identity(history_path) != identity:
        raise ValueError("Resource detail inputs changed during loading.")
    return entries, identity


def _row(connection, sql, values):
    cursor = connection.execute(sql, values)
    rows = cursor.fetchmany(2)
    if len(rows) != 1:
        raise ValueError("A resource detail source row is absent or ambiguous.")
    return dict(zip((column[0] for column in cursor.description), rows[0], strict=True))


def _indexed_detail_superseded(current, entry):
    """Leave changed upstream details in place for the same native event."""
    native = entry["packet"]["messages"][0]
    proof = json.loads(current["provenance"])
    return (
        current["conversation_id"] == entry["conversation"]["id"]
        and proof.get("server_id") == native.get("server_id")
        and type(native.get("local_type")) is int
        and proof.get("message_type") == native["local_type"] & 0xFFFFFFFF
        and proof.get("unix_timestamp") == native.get("create_time")
    )


def attach_resource_details(
    connection, history_path, conversation_table, message_table, *, conversation_id=None,
):
    """Apply verified replacements before SQL paging, search and session exports."""
    if (
        _TABLE.fullmatch(conversation_table) is None or _TABLE.fullmatch(message_table) is None
        or message_table in {MESSAGE_TABLE, "temp." + MESSAGE_TABLE}
    ):
        raise ValueError("Resource details need validated upstream timeline tables.")
    from .wechat_resource_detail_index import FILENAME as INDEX_FILENAME, index_identity, indexed_entries

    try:
        entries, identity = load_registry(history_path)
        indexed = Path(history_path).with_name(INDEX_FILENAME)
        index_pin = index_identity(indexed)
        if identity is None and index_pin is None:
            return conversation_table, message_table, "absent"
        replacements = []
        verified_sources, seen, scopes = set(), set(), {}

        def replay(entry, projected_sha=None, source_key=None):
            if entry["message_id"] in seen:
                raise ValueError("A resource detail ID appears in both registries.")
            seen.add(entry["message_id"])
            current = _row(
                connection, f"SELECT * FROM {message_table} WHERE id=?",
                (entry["message_id"],),
            )
            if row_sha256(current) != entry["original_row_sha256"]:
                if source_key is not None and _indexed_detail_superseded(current, entry):
                    return
                raise ValueError("The resource detail upstream row changed.")
            scope_id = entry["conversation"]["id"]
            if scope_id not in scopes:
                scopes[scope_id] = _conversation_scope(_row(
                    connection, f"SELECT * FROM {conversation_table} WHERE id=?", (scope_id,),
                ))
            if scopes[scope_id] != entry["conversation"]:
                raise ValueError("The resource detail conversation source changed.")
            packet = entry["packet"]
            if not isinstance(packet, dict) or len(packet.get("messages", ())) != 1:
                raise ValueError("A resource detail entry lost its exact native event.")
            events = secondary_native_events(packet)
            if source_key is None or source_key not in verified_sources:
                bank._validate_account_binding(connection, packet)
                if source_key is not None:
                    verified_sources.add(source_key)
            projected = project_resource_details(current, scopes[scope_id], events[0], connection=connection)
            if projected == current or projected_sha is not None and row_sha256(projected) != projected_sha:
                raise ValueError("The stored native proof no longer yields its reviewed details.")
            replacements.append(projected)

        for entry in entries:
            if conversation_id is None or entry["conversation"]["id"] == conversation_id:
                replay(entry)
        for entry, projected_sha, source_key in indexed_entries(indexed, index_pin, conversation_id=conversation_id):
            replay(entry, projected_sha, source_key)
        if registry_identity(history_path) != identity or index_identity(indexed) != index_pin:
            raise ValueError("Resource detail inputs changed during validation.")
        if not replacements:
            return conversation_table, message_table, "absent"
        connection.execute(_MESSAGE_SCHEMA)
        connection.execute("DELETE FROM temp.resource_detail_replacements")
        connection.executemany(
            "INSERT INTO temp.resource_detail_replacements VALUES("
            + ",".join("?" for _ in COLUMNS) + ")",
            [tuple(row[column] for column in COLUMNS) for row in replacements],
        )
        connection.execute("DROP VIEW IF EXISTS temp." + MESSAGE_TABLE)
        columns = ",".join("m." + column for column in COLUMNS)
        connection.execute(
            f"CREATE TEMP VIEW {MESSAGE_TABLE} AS SELECT * FROM temp.resource_detail_replacements "
            f"UNION ALL SELECT {columns} FROM {message_table} m WHERE NOT EXISTS("
            "SELECT 1 FROM temp.resource_detail_replacements d WHERE d.id=m.id)",
        )
        return conversation_table, "temp." + MESSAGE_TABLE, "ready"
    except (OSError, ValueError, TypeError, KeyError, sqlite3.DatabaseError):
        return conversation_table, message_table, "invalid"
