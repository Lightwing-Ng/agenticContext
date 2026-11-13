"""Project authenticated Mac message receipts without changing original archives.

Code version: v1.1.3-codex.0
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import tempfile
from threading import RLock

from .owner_only_files import ensure_owner_only
from .wechat_archive import (
    _file_identity,
    _identifier,
    _time_fields,
    wechat_archive_path,
)
from .wechat_reconcile import native_text_metadata
from .wechat_text import native_components_text


VERSION = "v1.0.0-codex.0"
MAX_OVERLAY_BYTES = 4 * 1024**2
_LOCK = RLock()
_VALIDATED_RECOVERY = object()
_HEX = re.compile(r"[0-9a-f]{64}")
_SQL = re.compile(r"(?:(?:main|temp)\.)?[A-Za-z_][A-Za-z0-9_]*")
_PACKET_FIELDS = {
    "schema_version",
    "receipt_id",
    "account_id",
    "session_id",
    "peer_id",
    "source",
    "anchors",
    "messages",
}
_SOURCE_FIELDS = {
    "source_id",
    "snapshot_id",
    "main_sha256",
    "wal_sha256",
    "database_relative_path",
    "table",
    "integrity_check",
    "authenticated_pages",
    "page_count",
    "authenticated_page_count",
}
_LOCK_BYTE_SOURCE_FIELDS = {"lock_byte_page_count", "authenticated_content_pages"}
_LOCK_BYTE_PAGE_NUMBER = 0x40000000 // 4096 + 1
_MESSAGE_FIELDS = {
    "local_id",
    "local_type",
    "server_id",
    "create_time",
    "sender_id",
    "sender_label",
    "direction",
    "text",
    "content_sha256",
    "native_text",
    "resource",
}
_NATIVE_FIELDS = {
    "text",
    "text_extraction_kind",
    "original_message_type",
    "appmsg_subtype",
    "flags",
    "text_components",
    "quoted_reference",
}
_QUOTE_FIELDS = {
    "original_message_type",
    "source_sender_id",
    "source_sender_label",
    "source_chat_id",
    "source_timestamp",
    "source_ids",
}
_CONVERSATION_COLUMNS = (
    "id",
    "partition_id",
    "account_key",
    "peer_id",
    "title",
    "kind",
    "message_count",
    "first_sort",
    "last_sort",
    "first_time",
    "last_time",
    "latest_text",
)


def _json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value):
    if not isinstance(value, str) or _HEX.fullmatch(value) is None:
        raise ValueError("Mac recovery requires exact SHA-256 source receipts.")
    return value


def _string(value, *, maximum=1024):
    if not isinstance(value, str) or not value or "\0" in value or len(value) > maximum:
        raise ValueError("Mac recovery identifiers must be bounded literal strings.")
    return value


def _server(value):
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[1-9][0-9]{0,18}", value) is None
        or int(value) > 2**63 - 1
    ):
        raise ValueError("Mac recovery requires a positive native server ID.")
    return value


def mac_recovery_id(account_id, peer_id, server_id, source_id):
    """Keep a native server event stable within its original Mac source and owner."""
    return _identifier(
        "wechat-macos-recovery-v1",
        _string(account_id),
        _string(peer_id),
        _server(server_id),
        _string(source_id),
    )


def _read(path):
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or getattr(before, "st_flags", 0) & 0x40000000
        or before.st_size > MAX_OVERLAY_BYTES
    ):
        raise ValueError("Mac recovery needs a bounded resident regular sidecar.")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("Mac recovery metadata changed before reading.")
        raw = stream.read(MAX_OVERLAY_BYTES + 1)
    after = path.lstat()
    if len(raw) > MAX_OVERLAY_BYTES or any(
        getattr(before, key) != getattr(after, key)
        for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    ):
        raise ValueError("Mac recovery metadata changed while reading.")
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != _PACKET_FIELDS | {"seal_sha256"}:
        raise ValueError("Unsupported Mac recovery sidecar schema.")
    seal = value.pop("seal_sha256")
    if _digest(seal) != hashlib.sha256(_json(value).encode()).hexdigest():
        raise ValueError("Mac recovery metadata does not match its seal.")
    return value, hashlib.sha256(raw).hexdigest()


def _history_identity(path):
    state = path.lstat()
    if (
        not stat.S_ISREG(state.st_mode)
        or stat.S_ISLNK(state.st_mode)
        or getattr(state, "st_flags", 0) & 0x40000000
        or any(
            Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")
        )
    ):
        raise ValueError("Mac recovery requires a stable resident original archive.")
    return _file_identity(path)


def _validate_quote(native):
    """Accept the parser's simple quote projection without opaque source XML or keys."""
    reference = native.get("quoted_reference")
    components = native.get("text_components")
    if (
        set(native) != _NATIVE_FIELDS
        or not isinstance(reference, dict)
        or set(reference) != _QUOTE_FIELDS
        or not isinstance(reference.get("source_ids"), dict)
        or set(reference["source_ids"]) - {"svrid", "msgid", "localid"}
        or not isinstance(components, list)
        or not 1 <= len(components) <= 2
    ):
        raise ValueError(
            "Mac quoted replies require bounded literal reference metadata."
        )
    for component in components:
        if not isinstance(component, dict) or (
            component.get("kind") == "reply_text"
            and set(component) != {"kind", "text"}
            or component.get("kind") == "quoted_text"
            and set(component) != {"kind", "text"} | _QUOTE_FIELDS
            or component.get("kind") not in {"reply_text", "quoted_text"}
        ):
            raise ValueError(
                "Mac quote components cannot retain opaque source metadata."
            )
    native_text_metadata(native, {})


def _validate_source(source):
    """Validate a closed authenticated source receipt independently of scope mode."""
    if not isinstance(source, dict) or set(source) not in (
        _SOURCE_FIELDS,
        _SOURCE_FIELDS | _LOCK_BYTE_SOURCE_FIELDS,
    ):
        raise ValueError("Mac recovery requires a complete source proof.")
    lock_byte_page = "lock_byte_page_count" in source
    if re.fullmatch(r"macos-[A-Za-z0-9_-]{1,120}", source.get("source_id", "")) is None:
        raise ValueError("A recovered Mac source cannot impersonate an iOS source.")
    _string(source["snapshot_id"])
    _digest(source["main_sha256"])
    _digest(source["wal_sha256"])
    relative = _string(source["database_relative_path"], maximum=4096)
    if (
        PurePosixPath(relative).is_absolute()
        or "\\" in relative
        or ":" in relative
        or any(part in {"", ".", ".."} for part in relative.split("/"))
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", source.get("table", ""))
        is None
        or source["integrity_check"] != "ok"
        or source["authenticated_pages"] is not (not lock_byte_page)
        or type(source["page_count"]) is not int
        or source["page_count"] < 1
        or type(source["authenticated_page_count"]) is not int
        or source["authenticated_page_count"]
        != source["page_count"] - int(lock_byte_page)
        or lock_byte_page
        and (
            type(source["lock_byte_page_count"]) is not int
            or source["lock_byte_page_count"] != 1
            or source["authenticated_content_pages"] is not True
            or source["page_count"] < _LOCK_BYTE_PAGE_NUMBER
        )
    ):
        raise ValueError("Mac recovery requires complete authenticated content pages.")


def _validate_anchors(anchors, *, minimum=2):
    """Validate only declared local anchors without manufacturing substitutes."""
    if not isinstance(anchors, list) or not minimum <= len(anchors) <= 32:
        raise ValueError(
            "Mac recovery needs at least two existing independent anchors."
        )
    seen = set()
    for anchor in anchors:
        if not isinstance(anchor, dict) or set(anchor) != {
            "server_id",
            "archive_message_id",
            "text_sha256",
        }:
            raise ValueError(
                "Mac recovery anchors require exact original IDs and hashes."
            )
        server = _server(anchor["server_id"])
        _digest(anchor["archive_message_id"])
        _digest(anchor["text_sha256"])
        if server in seen:
            raise ValueError(
                "Mac recovery anchors must identify distinct native events."
            )
        seen.add(server)


def _validate_packet(packet):
    if (
        not isinstance(packet, dict)
        or set(packet) != _PACKET_FIELDS
        or type(packet.get("schema_version")) is not int
        or packet["schema_version"] != 1
    ):
        raise ValueError("Unsupported Mac recovery packet schema.")
    for key in ("receipt_id", "account_id", "peer_id"):
        _string(packet[key])
    if re.fullmatch(r"wechat:[0-9a-f]{64}", packet.get("session_id", "")) is None:
        raise ValueError("Mac recovery requires an existing canonical session.")
    _validate_source(packet["source"])
    _validate_anchors(packet["anchors"])
    messages = packet["messages"]
    if not isinstance(messages, list) or not 1 <= len(messages) <= 2048:
        raise ValueError("Mac recovery requires bounded nonempty message metadata.")
    seen, local_ids = {}, set()
    for message in messages:
        required = _MESSAGE_FIELDS - {"sender_label", "native_text", "resource"}
        if (
            not isinstance(message, dict)
            or set(message) - _MESSAGE_FIELDS
            or required - set(message)
        ):
            raise ValueError("Unsupported Mac recovery message metadata.")
        server = _server(message["server_id"])
        if server in seen and seen[server] != message:
            raise ValueError(
                "A recovered server event has conflicting source metadata."
            )
        if message["local_id"] in local_ids and server not in seen:
            raise ValueError("A Mac source row cannot identify several server events.")
        seen[server] = message
        local_ids.add(message["local_id"])
        if (
            type(message["local_id"]) is not int
            or not 1 <= message["local_id"] <= 2**63 - 1
            or type(message["local_type"]) is not int
            or not 0 <= message["local_type"] <= 2**64 - 1
            or type(message["create_time"]) is not int
            or not 1 <= message["create_time"] <= 253402271999
            or message["direction"] not in {"sent", "received"}
            or message["sender_id"]
            != packet["account_id" if message["direction"] == "sent" else "peer_id"]
            or not isinstance(message["text"], str)
            or not message["text"].strip()
            or len(message["text"].encode()) > 2 * 1024**2
        ):
            raise ValueError(
                "Mac recovery message identity, direction, type or time conflicts."
            )
        _digest(message["content_sha256"])
        if "sender_label" in message:
            _string(message["sender_label"])
        native = message.get("native_text")
        resource = message.get("resource")
        if message["local_type"] & 0xFFFFFFFF == 49:
            if (
                resource is not None
                or not isinstance(native, dict)
                or set(native) - _NATIVE_FIELDS
                or native.get("text") != message["text"]
                or native.get("original_message_type") != 49
                or native.get("appmsg_subtype") != 57
                or native.get("text_extraction_kind") != "quoted_reply"
                or native_components_text(native.get("text_components", []))
                != message["text"]
                or not isinstance(native.get("flags"), list)
                or any(not isinstance(flag, str) for flag in native["flags"])
            ):
                raise ValueError(
                    "Mac quoted replies require their exact parsed native projection."
                )
            _validate_quote(native)
        else:
            kind = {3: "image", 43: "video", 62: "video"}.get(message["local_type"])
            label = {"image": "[Image]", "video": "[Video]"}.get(kind)
            if (
                native is not None
                or kind is None
                or not isinstance(resource, dict)
                or set(resource) != {"kind", "label", "native_time"}
                or resource
                != {"kind": kind, "label": label, "native_time": message["create_time"]}
                or message["text"] != label
            ):
                raise ValueError(
                    "Recovered resources require exact body-free typed metadata."
                )
    if len(_json(packet).encode()) > MAX_OVERLAY_BYTES - 1024:
        raise ValueError("Mac recovery exceeds its sealed metadata budget.")


def _validate_binding(connection, packet):
    row = connection.execute(
        "SELECT c.peer_id,c.kind,c.account_key,p.account_id,p.account_key "
        "FROM main.conversations c JOIN main.partitions p ON p.id=c.partition_id WHERE c.id=?",
        (packet["session_id"],),
    ).fetchone()
    owner = packet["account_id"]
    if row is None or tuple(row) != (
        packet["peer_id"],
        "direct",
        "account:" + owner,
        owner,
        "account:" + owner,
    ):
        raise ValueError(
            "Mac recovery cannot cross its existing account and peer binding."
        )
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
                "A Mac recovery anchor differs from its published original."
            )


def _missing(connection, packet, message_table="main.messages"):
    result = []
    for message in packet["messages"]:
        candidates = connection.execute(
            f"SELECT text,direction,sender_id,provenance FROM {message_table} "
            "WHERE conversation_id=? AND CAST(json_extract(provenance,'$.server_id') AS TEXT)=?",
            (packet["session_id"], message["server_id"]),
        ).fetchall()
        if candidates:
            for row in candidates:
                if tuple(row[:3]) != (
                    message["text"],
                    message["direction"],
                    message["sender_id"],
                ):
                    raise ValueError(
                        "A native server ID conflicts with an existing archive event."
                    )
                proof = json.loads(row[3])
                if (
                    "unix_timestamp" in proof
                    and proof["unix_timestamp"] != message["create_time"]
                    or "message_type" in proof
                    and proof["message_type"] != message["local_type"] & 0xFFFFFFFF
                ):
                    raise ValueError(
                        "A native server ID conflicts with its source type or clock."
                    )
            continue
        if message not in result:
            result.append(message)
    return result


def stage_mac_recovery(local_store_root, packet):
    """Return a sealed review candidate; never open a Mac database or write history."""
    packet = json.loads(_json(packet))
    _validate_packet(packet)
    history = wechat_archive_path(local_store_root)
    identity = _history_identity(history)
    path = history.with_name("macos_recovery.json")
    base, base_digest = (
        _read(path) if path.exists() or path.is_symlink() else (None, None)
    )
    with closing(
        sqlite3.connect(history.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    ) as connection:
        _validate_binding(connection, packet)
        if base is not None:
            _validate_packet(base)
            _validate_binding(connection, base)
            if {k: v for k, v in base.items() if k != "messages"} != {
                k: v for k, v in packet.items() if k != "messages"
            }:
                raise ValueError(
                    "Mac recovery cannot replace a different source receipt or binding."
                )
        original = {m["server_id"]: m for m in base["messages"]} if base else {}
        added, existing = 0, 0
        missing = _missing(connection, packet)
        for message in missing:
            server = message["server_id"]
            if server in original:
                if original[server] != message:
                    raise ValueError(
                        "A recovered Mac source event has conflicting content."
                    )
                existing += 1
            else:
                original[server] = message
                added += 1
        existing += len(packet["messages"]) - len(missing)
    value = {
        **packet,
        "messages": sorted(
            original.values(),
            key=lambda m: (m["create_time"], m["local_id"], m["server_id"]),
        ),
    }
    if not value["messages"]:
        raise ValueError("Mac recovery has no missing source events to publish.")
    _validate_packet(value)
    value["seal_sha256"] = hashlib.sha256(_json(value).encode()).hexdigest()
    if _history_identity(history) != identity:
        raise ValueError("The original archive changed during Mac recovery staging.")
    return {
        "sidecar": value,
        "base_sha256": base_digest,
        "history_identity": identity,
        "added": added,
        "existing": existing,
        "total": len(original),
    }


def publish_mac_recovery(local_store_root, staged):
    """Atomically publish a reviewed candidate after rechecking original anchors."""
    with _LOCK:
        history = wechat_archive_path(local_store_root)
        path = history.with_name("macos_recovery.json")
        value = json.loads(_json(staged["sidecar"]))
        seal = value.pop("seal_sha256")
        if seal != hashlib.sha256(_json(value).encode()).hexdigest():
            raise ValueError("The staged Mac recovery seal is invalid.")
        current = stage_mac_recovery(local_store_root, value)
        if (
            current["base_sha256"] != staged["base_sha256"]
            or current["history_identity"] != staged["history_identity"]
            or current["sidecar"] != staged["sidecar"]
        ):
            raise ValueError("Mac recovery inputs changed after staging.")
        raw = (_json(staged["sidecar"]) + "\n").encode()
        if len(raw) > MAX_OVERLAY_BYTES:
            raise ValueError("Mac recovery exceeds its sealed metadata budget.")
        if hashlib.sha256(raw).hexdigest() == staged["base_sha256"]:
            return {key: staged[key] for key in ("added", "existing", "total")} | {
                "sidecar_path": str(path),
                "sidecar_sha256": staged["base_sha256"],
            }
        descriptor, temporary = tempfile.mkstemp(
            prefix=".macos-recovery-", suffix=".json", dir=path.parent
        )
        temporary = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                ensure_owner_only(temporary)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            latest_base = _read(path)[1] if path.exists() or path.is_symlink() else None
            if (
                latest_base != staged["base_sha256"]
                or _history_identity(history) != staged["history_identity"]
            ):
                raise ValueError("Mac recovery inputs changed before publication.")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return {key: staged[key] for key in ("added", "existing", "total")} | {
            "sidecar_path": str(path),
            "sidecar_sha256": hashlib.sha256(raw).hexdigest(),
        }


def mac_recovery_conversation(history_path, message_id):
    """Locate a recovered message before the normal scoped timeline validation."""
    path = Path(history_path).with_name("macos_recovery.json")
    if not path.exists() and not path.is_symlink():
        return None
    packet, _ = _read(path)
    _validate_packet(packet)
    return (
        packet["session_id"]
        if any(
            mac_recovery_id(
                packet["account_id"],
                packet["peer_id"],
                m["server_id"],
                packet["source"]["source_id"],
            )
            == message_id
            for m in packet["messages"]
        )
        else None
    )


def mark_mac_recovery_rows(connection, rows):
    """Attach runtime authority only to rows from this verified temporary overlay."""
    from .wechat_macos_recovery_bank import mark_bank_rows

    mark_bank_rows(connection, rows, _VALIDATED_RECOVERY)
    if (
        connection.execute(
            "SELECT 1 FROM temp.sqlite_master WHERE type='table' AND name='macos_recovery_validated'"
        ).fetchone()
        is None
    ):
        return rows
    for row in rows:
        stored = connection.execute(
            "SELECT provenance,text FROM temp.macos_recovery_messages WHERE id=?",
            (row["id"],),
        ).fetchone()
        if stored is not None and tuple(stored) == (row["provenance"], row["text"]):
            row["_macos_recovery_validated"] = _VALIDATED_RECOVERY
    return rows


def validated_recovery_receipt(row):
    """Return Mac authority only when the validated read path stamped this exact row."""
    if row.get("_macos_recovery_validated") is not _VALIDATED_RECOVERY:
        return None
    try:
        receipt = json.loads(row["provenance"])["macos_recovery"]
        return dict(receipt) if isinstance(receipt, dict) else None
    except (KeyError, TypeError, ValueError):
        return None


def _project(packet, message):
    source = packet["source"]
    native = message.get("native_text", {})
    stamp = datetime.fromtimestamp(message["create_time"], UTC).isoformat()
    ordering, value, label, basis = _time_fields({"sent_at_utc_iso": stamp})
    proof = {
        **source,
        "account_id": packet["account_id"],
        "peer_id": packet["peer_id"],
        "receipt_id": packet["receipt_id"],
        "session_id": packet["session_id"],
        **{
            key: message[key]
            for key in (
                "local_id",
                "local_type",
                "server_id",
                "content_sha256",
                "create_time",
            )
        },
    }
    provenance = {
        "macos_recovery": proof,
        "server_id": message["server_id"],
        "message_type": message["local_type"] & 0xFFFFFFFF,
        "unix_timestamp": message["create_time"],
        "source_time": stamp,
        "locator": {
            "database_relative_path": source["database_relative_path"],
            "table": source["table"],
            "local_id": message["local_id"],
            "local_type": message["local_type"],
        },
        "text_extraction_kind": native.get(
            "text_extraction_kind", "resource_placeholder"
        ),
        **{key: item for key, item in native.items() if key not in {"text", "flags"}},
    }
    if "resource" in message:
        provenance["resource_reference"] = {
            **message["resource"],
            "account_id": packet["account_id"],
            "peer_id": packet["peer_id"],
            "availability": "metadata_only",
        }
    return (
        mac_recovery_id(
            packet["account_id"],
            packet["peer_id"],
            message["server_id"],
            source["source_id"],
        ),
        packet["session_id"],
        message["local_id"],
        message["sender_id"],
        message.get("sender_label", message["sender_id"]),
        message["direction"],
        message["text"],
        ordering,
        value,
        label,
        basis,
        _json(provenance),
        _json(native.get("flags", [])),
        message["text"].casefold(),
    )


def attach_mac_recovery(
    connection, history_path, conversation_table, message_table, *, conversation_id=None
):
    """Expose one validated additive timeline for browsing, search and exports."""
    default = (conversation_table, message_table)
    path = Path(history_path).with_name("macos_recovery.json")
    if not path.exists() and not path.is_symlink():
        return *default, "absent"
    try:
        if any(not isinstance(v, str) or _SQL.fullmatch(v) is None for v in default):
            raise ValueError("Unsupported Mac recovery timeline table.")
        history_identity = _history_identity(Path(history_path))
        packet, sidecar_digest = _read(path)
        _validate_packet(packet)
        if conversation_id is not None and conversation_id != packet["session_id"]:
            return *default, "absent"
        _validate_binding(connection, packet)
        messages = _missing(connection, packet, message_table)
        rows = [_project(packet, message) for message in messages]
        connection.execute(
            "CREATE TEMP TABLE macos_recovery_messages AS SELECT * FROM main.messages WHERE 0"
        )
        connection.executemany(
            "INSERT INTO temp.macos_recovery_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        connection.execute(
            f"CREATE TEMP VIEW macos_recovery_timeline_messages AS SELECT * FROM {message_table} UNION ALL SELECT * FROM temp.macos_recovery_messages"
        )
        connection.execute(
            f"CREATE TEMP TABLE macos_recovery_conversations AS SELECT * FROM {conversation_table} WHERE 0"
        )
        if rows:
            cursor = connection.execute(
                f"SELECT * FROM {conversation_table} WHERE id=?",
                (packet["session_id"],),
            )
            conversation = dict(
                zip(
                    (col[0] for col in cursor.description),
                    cursor.fetchone(),
                    strict=True,
                )
            )
            summary = connection.execute(
                "SELECT COUNT(*) FROM macos_recovery_timeline_messages WHERE conversation_id=?",
                (packet["session_id"],),
            ).fetchone()[0]
            first = connection.execute(
                "SELECT time_sort,timestamp_value FROM macos_recovery_timeline_messages WHERE conversation_id=? ORDER BY (time_sort=0),time_sort,ordinal,id LIMIT 1",
                (packet["session_id"],),
            ).fetchone()
            last = connection.execute(
                "SELECT time_sort,timestamp_value,text FROM macos_recovery_timeline_messages WHERE conversation_id=? ORDER BY (time_sort=0),time_sort DESC,ordinal DESC,id DESC LIMIT 1",
                (packet["session_id"],),
            ).fetchone()
            conversation.update(
                message_count=summary,
                first_sort=first[0],
                first_time=first[1],
                last_sort=last[0],
                last_time=last[1],
                latest_text=last[2][:300],
            )
            connection.execute(
                "INSERT INTO temp.macos_recovery_conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(conversation[key] for key in _CONVERSATION_COLUMNS),
            )
        connection.execute(
            f"CREATE TEMP VIEW macos_recovery_timeline_conversations AS SELECT c.* FROM {conversation_table} c WHERE NOT EXISTS(SELECT 1 FROM temp.macos_recovery_conversations r WHERE r.id=c.id) UNION ALL SELECT * FROM temp.macos_recovery_conversations"
        )
        if (
            _file_identity(Path(history_path)) != history_identity
            or _read(path)[1] != sidecar_digest
        ):
            return *default, "invalid"
        connection.execute("CREATE TEMP TABLE macos_recovery_validated(ready INTEGER)")
        return (
            "macos_recovery_timeline_conversations",
            "macos_recovery_timeline_messages",
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
