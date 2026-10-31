"""Synthetic published HTML evidence shared by integrity and browser checks.

Code version: v1.0.0-codex.0
"""

import hashlib
import json
import sqlite3

from app.core.wechat_archive import (
    _SCHEMA,
    _identifier,
    _time_fields,
    wechat_archive_path,
)
from app.core.wechat_contacts import canonical_contact_id


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def make_forwarded_html_store(registry):
    sha = hashlib.sha256(b"Synthetic reviewed HTML archive").hexdigest()
    source_id = "html-" + sha[:24]
    member = {
        "member_path": "Export/Room.html",
        "sha256": hashlib.sha256(b"synthetic member").hexdigest(),
        "size": 1000,
        "crc32": "12345678",
        "zip_filename": "Export/Room.html",
        "header_offset": 0,
        "compressed_size": 200,
        "filename_utf8_recovered": False,
    }
    base_locator = {
        "member_path": member["member_path"],
        "page": 0,
        "message_ordinal": 1,
        "page_message_ordinal": 1,
    }
    locator = {
        **base_locator,
        "archive_sha256": sha,
        "member": member,
        "export_msgid": "42",
        "export_msgid_namespace": "MesLocalID",
        "export_msgtype": "49",
    }
    child = {
        "export_msgid": "42_component",
        "type": "",
        "ordinal": 2,
        "component_ordinal": 1,
        "sender_export_id": "",
        "sender_native_id": "wxid_forwarded",
        "sender_label": "Original room-specific author",
        "avatar_path": "Room_files/Portrait/wxid_forwarded.jpg",
        "raw_timestamp": "2021-08-08 12:34:56",
        "text": "Original child text",
        "text_kind": "forwarded_rendered_text",
        "source_locator": {
            **base_locator,
            "message_ordinal": 2,
            "page_message_ordinal": 2,
        },
        "flags": ["original_nested_parent_unknown"],
        "text_completion_parent_context": {"immediate_parent_known": False},
        "forwarded_components": [],
    }
    source = {
        "source_id": source_id,
        "source_sha256": sha,
        "source_file": "Synthetic.zip",
        "source_label": "HTML export",
        "source_account_id": "wxid_owner_a",
        "owner_verified": True,
        "owner_evidence": {
            "native_username": "wxid_owner_a",
            "native_contact_id": canonical_contact_id(
                "wxid_owner_a", authority="native_account_owner"
            ),
        },
        "selected_text_members": [member],
        "source_conversation_catalog": [
            {
                "member_path": member["member_path"],
                "index_avatar_native_id": "room-synthetic@chatroom",
                "conversation_kind": "group",
                "declared_async_counts": {"data_path": "Room_files/Data"},
            }
        ],
    }
    record_id = hashlib.sha256(
        ("html-text-v1\0" + sha + "\0" + _json(locator)).encode()
    ).hexdigest()
    text = "Forward history\n\n[Forwarded text: Original room-specific author | 2021-08-08 12:34:56]\nOriginal child text"
    record = {
        "extraction_record_id": record_id,
        "conversation_key": "room-synthetic@chatroom",
        "peer_id": "room-synthetic@chatroom",
        "title": "Synthetic room",
        "kind": "group",
        "sender_id": "owner-alias",
        "sender_label": "Original parent",
        "direction": "received",
        "text": text,
        "sent_at_raw": "2021-08-08 13:00:00",
        "provenance": {
            "locator": locator,
            "forwarded_components": [child],
            "text_extraction_kind": "rendered_app_text",
        },
        "flags": [],
    }
    # Model an already-published archive; no importer, ZIP, or real source is read.
    message_id = _identifier(source_id, record_id)
    conversation_id = "wechat:" + _identifier(source_id, record["conversation_key"])
    clock, timestamp, label, basis = _time_fields(
        {"sent_at_raw": record["sent_at_raw"]}
    )
    path = wechat_archive_path(registry)
    path.parent.mkdir(parents=True, exist_ok=True)
    provenance = {
        **record["provenance"],
        "record_id": record_id,
        "source_snapshot": 1,
        "source_time": record["sent_at_raw"],
    }
    with sqlite3.connect(path) as connection:
        connection.executescript(
            _SCHEMA
            + """
            CREATE TABLE source_snapshots (
                id INTEGER PRIMARY KEY, snapshot_id TEXT NOT NULL,
                source_id TEXT NOT NULL, metadata TEXT NOT NULL
            );
        """
        )
        connection.execute(
            "INSERT INTO partitions VALUES(?,?,?,?,?,?,?,?)",
            (
                source_id,
                "account:wxid_owner_a",
                "wxid_owner_a",
                "Synthetic owner",
                "Corroborated export ID",
                "HTML export",
                "Synthetic.zip",
                "",
            ),
        )
        connection.execute(
            "INSERT INTO source_snapshots VALUES(?,?,?,?)",
            (1, sha, source_id, _json(source)),
        )
        connection.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                conversation_id,
                source_id,
                "account:wxid_owner_a",
                "room-synthetic@chatroom",
                "Synthetic room",
                "group",
                1,
                clock,
                clock,
                timestamp,
                timestamp,
                text[:300],
            ),
        )
        connection.execute(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                message_id,
                conversation_id,
                1,
                "owner-alias",
                "Original parent",
                "received",
                text,
                clock,
                timestamp,
                label,
                basis,
                _json(provenance),
                "[]",
                text.casefold(),
            ),
        )
        connection.execute(
            "INSERT INTO archive_info VALUES(?,?)",
            (
                "manifest",
                _json(
                    {
                        "schema_version": 1,
                        "sources": [
                            {
                                "source_id": source_id,
                                "account_id": "wxid_owner_a",
                                "messages": 1,
                                "conversations": 1,
                            }
                        ],
                        "messages": 1,
                    }
                ),
            ),
        )
    row = (conversation_id, timestamp)
    request = {
        "id": message_id,
        "source_id": source_id,
        "source_snapshot_id": sha,
        "conversation_id": row[0],
        "account_id": "wxid_owner_a",
        "peer_id": "room-synthetic@chatroom",
        "conversation_kind": "group",
        "sender_id": "owner-alias",
        "sender_label": "Original parent",
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "timestamp_value": row[1],
        "source_record_id": record_id,
        "source_locator": locator,
        "forwarded_components": [child],
        "is_system": False,
    }
    return {"store": registry, "request": request, "source": source, "history": path}
