"""Synthetic complete packets for reviewed statement-copy tests.

Code version: v1.0.0-codex.0
"""

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core.wechat_contact_embedded import native_details


OWNER = "wxid_transcript_owner"
AUTHOR = "wxid_original_author"
OTHER = "wxid_original_participant"
FORWARDER = "wxid_original_forwarder"
SOURCE = "ios-" + hashlib.md5(OWNER.encode()).hexdigest()
SELECT = "SELECT m.* FROM messages m "


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _part(index, *, author=None):
    return {
        "kind": "forwarded_text",
        "text": "Transcript statement " + str(index),
        "source_sender_id": author or (AUTHOR if index % 2 == 0 else OTHER),
        "source_sender_label": "Literal source label",
        "source_timestamp": (
            datetime(2021, 9, 16, 15, 0, 0) + timedelta(seconds=index)
        ).strftime("%Y-%m-%d %H:%M:%S"),
        "source_create_time": "",
        "source_data_id": "",
        "source_ids": {},
    }


def _noop_prepare(connection, rows):
    return []


def _add_packet(
    fixture,
    name,
    components,
    *,
    snapshot=1,
    sender=FORWARDER,
    conversation="same-receiving-conversation",
    kind="group",
):
    parent = _hash(name)
    clock = datetime.fromisoformat("2021-09-16T18:00:00+08:00")
    proof = {
        "message_type": 49,
        "appmsg_subtype": 19,
        "text_extraction_kind": "forwarded_text",
        "text_components": deepcopy(components),
        "source_snapshot": snapshot,
        "server_id": "8123456789012345678",
        "unix_timestamp": int(clock.timestamp()),
        "source_time": clock.isoformat(),
        "locator": {},
    }
    fixture.source.execute(
        "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            parent,
            SOURCE,
            OWNER,
            "account:" + OWNER,
            "account:" + OWNER,
            "same-receiving@chatroom" if kind == "group" else OTHER,
            conversation,
            kind,
            "Verified archive ID",
            sender,
            "received",
            clock.isoformat(),
            json.dumps(proof),
            1,
            "Source packet",
            0,
        ),
    )
    details = list(native_details(proof, parent_id=parent, account_id=OWNER))
    for detail in details:
        if detail["sender_id"] != AUTHOR:
            continue
        identifier, statement = detail["id"], _hash("statement-" + detail["id"])
        fixture.output.execute(
            "INSERT INTO refs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                identifier,
                parent,
                json.dumps(detail["path"], separators=(",", ":")),
                detail["anchor_id"],
                conversation,
                SOURCE,
                "account:" + OWNER,
                0,
                1,
                bool(detail["timestamp_value"]),
                detail["timestamp_value"],
                detail["timestamp_label"],
                detail["time_basis"],
                detail["source_clock"],
            ),
        )
        fixture.output.execute(
            "INSERT INTO statement_groups VALUES(?,?,?)",
            (statement, identifier, conversation),
        )
        fixture.output.execute(
            "INSERT INTO statement_members VALUES(?,?)", (identifier, statement)
        )
    return parent, details


@pytest.fixture
def overlap_store():
    source = sqlite3.connect(":memory:")
    source.row_factory = sqlite3.Row
    source.executescript(
        "CREATE TABLE source_snapshots(id INTEGER PRIMARY KEY,snapshot_id TEXT,"
        "source_id TEXT,metadata TEXT);"
        "CREATE TABLE messages(id TEXT PRIMARY KEY,source_id TEXT,account_id TEXT,"
        "account_key TEXT,container_account_key TEXT,peer_id TEXT,conversation_id TEXT,"
        "kind TEXT,identity_status TEXT,sender_id TEXT,direction TEXT,timestamp_value TEXT,"
        "provenance TEXT,ordinal INTEGER,text TEXT,time_sort REAL);"
    )
    for number in (1, 2):
        source.execute(
            "INSERT INTO source_snapshots VALUES(?,?,?,?)",
            (
                number,
                "snapshot-" + str(number),
                SOURCE,
                json.dumps(
                    {
                        "source_account_id": OWNER,
                        "account_hash_verified": True,
                        "account_hash": hashlib.md5(OWNER.encode()).hexdigest(),
                    }
                ),
            ),
        )
    output = sqlite3.connect(":memory:")
    output.executescript(analysis._REFERENCE_SCHEMA)
    fixture = SimpleNamespace(source=source, output=output)
    yield fixture
    source.close()
    output.close()
