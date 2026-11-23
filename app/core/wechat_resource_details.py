"""Apply bounded, authenticated resource details without rewriting source catalogs.

Code version: v1.3.2-codex.0
"""

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
from .wechat_resource_sender_evidence import (
    _HEADER_FIELDS,
    _json,
    match_secondary_native_event,
    recover_native_resource_sender_from_secondary,
    secondary_native_events,
    validate_secondary_native_event,
)
from .windows_file_metadata import file_change_time


CODE_VERSION = "v1.3.2-codex.0"
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
        or result["kind"] != "group"
        or not isinstance(result["account_key"], str)
        or not result["account_key"].startswith("account:")
    ):
        raise ValueError("Resource details require an original native group scope.")
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


def _project_text_sender(row, scope, header, message, connection):
    proof = json.loads(row["provenance"])
    locator = proof.get("locator")
    native_text = message.get("native_text")
    peer = scope["peer_id"]
    owner = scope["account_key"].removeprefix("account:")
    if (
        connection is None
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
        if (
            locator.get("text_origin") != "corroborated_recovery_index"
            or not isinstance(corroboration, list)
            or not any(
                isinstance(source, dict) and source.get("database") == "fts/fts_message.db"
                for source in corroboration
            )
            or row["text"].replace("，", ",") != message["text"].replace("，", ",")
        ):
            raise ValueError("The archived text differs from its exact native sender witness.")
        mode = "corroborated_fts_comma_width"
    flags = json.loads(row["quality_flags"])
    if not isinstance(flags, list) or any(not isinstance(flag, str) for flag in flags):
        raise ValueError("Text sender details require retained string quality flags.")
    proof["native_text_sender_details"] = {
        "version": VERSION,
        "basis": "authenticated_native_group_text_sender",
        "text_match": mode,
        "original_row_sha256": row_sha256(row),
        "source_account_id": header["account_id"],
        "source_session_id": header["session_id"],
        "source": deepcopy(header["source"]),
        "local_id": message["local_id"],
        "content_sha256": message["content_sha256"],
        "sender_proof": deepcopy(message["sender_proof"]),
    }
    result = dict(row)
    result["sender_id"] = sender
    if not row["sender_label"] or row["sender_label"] == "Unknown sender":
        result["sender_label"] = sender
    result["quality_flags"] = _json([*flags, "authenticated_text_sender_verified"])
    result["provenance"] = _json(proof)
    if sender.casefold() not in row["search_text"]:
        result["search_text"] += "\n" + sender.casefold()
    return result


def project_resource_details(row, conversation, event, *, connection=None):
    """Project an exactly witnessed text or resource sender and inert app-card labels.

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
    matched = match_secondary_native_event(row, scope, [event])
    if matched is None:
        raise ValueError("Resource details need an exact native event clock and identity.")
    same_viewer = header["account_id"] == owner
    if same_viewer:
        result = recover_native_resource_sender_from_secondary(row, scope, [event])["message"]
    else:
        _cross_viewer_completion(connection, row, header, matched)
        result = dict(row)
    proof = json.loads(result["provenance"])
    reference = proof["resource_reference"]
    resource = event.get("resource")
    added = {}
    if (
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


def _resource_detail_file_pin(path, state, *, descriptor=None):
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


def registry_identity(history_path):
    """Return a resident private-file pin without hydrating or following a link."""
    path = Path(history_path).with_name(FILENAME)
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
    return _resource_detail_file_pin(path, state)


def detail_input_identities(history_path):
    """Pin absence as well as publication for readers, exports and derived caches."""
    path = Path(history_path).with_name(FILENAME)
    return {str(path.resolve()): registry_identity(history_path)}


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


def _read_registry(history_path, expected):
    path = Path(history_path).with_name(FILENAME)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        actual = _resource_detail_file_pin(path, os.fstat(descriptor), descriptor=descriptor)
        if actual != expected:
            raise ValueError("The resource detail registry changed before reading.")
        content = stream.read(MAX_BYTES + 1)
        if _resource_detail_file_pin(path, os.fstat(descriptor), descriptor=descriptor) != expected:
            raise ValueError("The resource detail registry changed while reading.")
    if len(content) > MAX_BYTES or registry_identity(history_path) != expected:
        raise ValueError("The resource detail registry changed while reading.")
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
    return entries


def load_registry(history_path):
    """Load flat or compact logical entries and their immutable publication pin."""
    identity = registry_identity(history_path)
    if identity is None:
        return [], None
    entries = _read_registry(history_path, identity)
    if registry_identity(history_path) != identity:
        raise ValueError("Resource detail inputs changed during loading.")
    return entries, identity


def _row(connection, sql, values):
    cursor = connection.execute(sql, values)
    rows = cursor.fetchmany(2)
    if len(rows) != 1:
        raise ValueError("A resource detail source row is absent or ambiguous.")
    return dict(zip((column[0] for column in cursor.description), rows[0], strict=True))


def attach_resource_details(
    connection, history_path, conversation_table, message_table, *, conversation_id=None,
):
    """Apply verified replacements before SQL paging, search and session exports."""
    if (
        _TABLE.fullmatch(conversation_table) is None or _TABLE.fullmatch(message_table) is None
        or message_table in {MESSAGE_TABLE, "temp." + MESSAGE_TABLE}
    ):
        raise ValueError("Resource details need validated upstream timeline tables.")
    try:
        entries, identity = load_registry(history_path)
        if identity is None:
            return conversation_table, message_table, "absent"
        replacements = []
        for entry in entries:
            if conversation_id is not None and entry["conversation"]["id"] != conversation_id:
                continue
            current = _row(
                connection, f"SELECT * FROM {message_table} WHERE id=?",
                (entry["message_id"],),
            )
            if row_sha256(current) != entry["original_row_sha256"]:
                raise ValueError("The resource detail upstream row changed.")
            conversation = _row(
                connection, f"SELECT * FROM {conversation_table} WHERE id=?",
                (entry["conversation"]["id"],),
            )
            if _conversation_scope(conversation) != entry["conversation"]:
                raise ValueError("The resource detail conversation source changed.")
            packet = entry["packet"]
            if not isinstance(packet, dict) or len(packet.get("messages", ())) != 1:
                raise ValueError("A resource detail entry lost its exact native event.")
            events = secondary_native_events(packet)
            bank._validate_account_binding(connection, packet)
            projected = project_resource_details(current, conversation, events[0], connection=connection)
            if projected == current:
                raise ValueError("The stored native proof adds no resource details.")
            replacements.append(projected)
        if registry_identity(history_path) != identity:
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
