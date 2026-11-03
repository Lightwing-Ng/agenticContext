"""Synthetic audited media metadata for scoped read-only archive tests.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.core.wechat_archive import _SCHEMA, _identifier


OWNER = "wxid_placeholder_owner"



PEER = "wxid_placeholder_peer"



ARCHIVE_SHA = hashlib.sha256(b"Synthetic immutable HTML source").hexdigest()



MEMBER_SHA = hashlib.sha256(b"Synthetic audited HTML conversation").hexdigest()



AUDIT_SHA = hashlib.sha256(b"Synthetic complete node disposition audit").hexdigest()



SOURCE = "html-" + ARCHIVE_SHA[:24]



MEMBER = "Synthetic/Chat.html"



CONVERSATION = "wechat:" + _identifier(SOURCE, MEMBER)



MEDIA_ORDINALS = (8, 13)



def _clock(ordinal):
    return f"2023-12-29 13:00:{ordinal:02d}"



def _entry(ordinal):
    return {
        "source_snapshot_id": 1,
        "source_id": SOURCE,
        "archive_sha256": ARCHIVE_SHA,
        "member_sha256": MEMBER_SHA,
        "audit_sha256": AUDIT_SHA,
        "conversation_id": CONVERSATION,
        "node": {
            "role": "top_level",
            "status": "deferred_top_level",
            "source_locator": {
                "member_path": MEMBER,
                "message_ordinal": ordinal,
                "page": 0,
                "page_message_ordinal": ordinal,
            },
            "export_msgid": str(ordinal),
            "type": "image",
            "text_kind": "deferred",
            "classes": ["media", "msg", "raw-img", "right"],
            "flags": ["export_msgid_is_meslocalid", "export_time_zone_unavailable"],
            "text_length": 0,
            "text_sha256": hashlib.sha256(b"").hexdigest(),
            "direction": "sent",
            "raw_timestamp": _clock(ordinal),
            "sender_export_id": "synthetic.owner.alias",
            "sender_native_id": OWNER,
            "avatar_path": f"Chat_files/Portrait/{OWNER}.jpg",
            "sender_label": "Synthetic owner",
        },
    }



@pytest.fixture
def placeholder_store(tmp_path):
    store = tmp_path / "store"
    history = store / "messaging/wechat/history.sqlite3"
    history.parent.mkdir(parents=True)
    member = {"member_path": MEMBER, "sha256": MEMBER_SHA, "size": 4096}
    metadata = {
        "source_id": SOURCE,
        "source_sha256": ARCHIVE_SHA,
        "source_account_id": OWNER,
        "owner_verified": True,
        "owner_evidence": {"native_username": OWNER},
        "source_file": "Synthetic HTML source.zip",
        "source_label": "HTML export",
        "selected_text_members": [member],
        "source_conversation_catalog": [
            {
                "member_path": MEMBER,
                "index_avatar_native_id": PEER,
                "conversation_kind": "direct",
                "reviewed_resolution": {"main_html_sha256": MEMBER_SHA},
                "counts": {
                    "top_level_nodes": 20,
                    "all_message_nodes": 20,
                    "retained_top_level_text": 18,
                    "deferred_top_level": 2,
                },
                "declared_async_counts": {"numberOfPages": 0},
            }
        ],
    }
    text_rows = []
    for ordinal in range(1, 21):
        if ordinal in MEDIA_ORDINALS:
            continue
        stamp = datetime.fromisoformat(_clock(ordinal))
        record = f"synthetic-text-frame-{ordinal}"
        text = f"Retained text {ordinal}"
        provenance = {
            "record_id": record,
            "source_snapshot": 1,
            "source_time": _clock(ordinal),
            "text_extraction_kind": "plain_text",
            "export_msgid_namespace": "MesLocalID",
            "sender_native_id": OWNER,
            "sender_export_id": "synthetic.owner.alias",
            "original_timezone": "unknown",
            "locator": {
                "member_path": MEMBER,
                "archive_sha256": ARCHIVE_SHA,
                "member": member,
                "message_ordinal": ordinal,
                "page_message_ordinal": ordinal,
                "page": 0,
                "export_msgid": str(ordinal),
                "export_msgtype": "1",
                "export_msgid_namespace": "MesLocalID",
            },
        }
        text_rows.append(
            (
                _identifier(SOURCE, record),
                CONVERSATION,
                ordinal,
                OWNER,
                "Synthetic owner",
                "sent",
                text,
                stamp.replace(tzinfo=UTC).timestamp(),
                stamp.isoformat(),
                f"29 Dec 2023 13:00:{ordinal:02d}",
                "Source clock; timezone unknown",
                json.dumps(provenance),
                "[]",
                text.casefold(),
            )
        )
    with sqlite3.connect(history) as connection:
        connection.executescript(_SCHEMA)
        connection.execute(
            "CREATE TABLE source_snapshots(id INTEGER PRIMARY KEY,snapshot_id TEXT,"
            "source_id TEXT,metadata TEXT)"
        )
        connection.execute(
            "INSERT INTO partitions VALUES(?,?,?,?,?,?,?,?)",
            (
                SOURCE,
                "account:" + OWNER,
                OWNER,
                "Synthetic owner",
                "Corroborated export ID",
                "HTML export",
                "Synthetic HTML source.zip",
                "",
            ),
        )
        connection.execute(
            "INSERT INTO source_snapshots VALUES(1,?,?,?)",
            (ARCHIVE_SHA, SOURCE, json.dumps(metadata)),
        )
        first, last = text_rows[0], text_rows[-1]
        connection.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                CONVERSATION,
                SOURCE,
                "account:" + OWNER,
                PEER,
                "Synthetic conversation",
                "direct",
                18,
                first[7],
                last[7],
                first[8],
                last[8],
                last[6],
            ),
        )
        connection.executemany(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", text_rows
        )
        connection.execute(
            "INSERT INTO archive_info VALUES('manifest',?)",
            (json.dumps({"schema_version": 1, "messages": 18, "sources": []}),),
        )
    entries = [_entry(ordinal) for ordinal in MEDIA_ORDINALS]
    overlay = history.with_name("media_placeholders.json")
    overlay.write_text(json.dumps({"schema_version": 1, "entries": entries}))
    return SimpleNamespace(
        store=store,
        history=history,
        overlay=overlay,
        entries=entries,
        metadata=metadata,
        conversation=CONVERSATION,
        text_rows=text_rows,
    )
