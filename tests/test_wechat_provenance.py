"""Per-message snapshot origins remain accurate without rewriting legacy partitions.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.core.chat_history_browser import (
    build_chat_history_markdown,
    query_chat_history,
)
from app.core.wechat_archive import import_wechat_archive, wechat_archive_path
from tests.test_wechat_archive import make_archive


@pytest.fixture
def archive_store(tmp_path):
    archive = make_archive(tmp_path / "archive", repeated=3)
    store = tmp_path / "store"
    import_wechat_archive(archive, store)
    return store


def _session(store, account="account:wxid_account_a"):
    page = query_chat_history(store, source="wechat", account=account)
    return next(
        session for session in page.sessions if session.messaging["kind"] == "direct"
    )


def _rows(connection, table):
    return connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()


def _snapshot_table(connection):
    connection.execute(
        "CREATE TABLE source_snapshots (id INTEGER PRIMARY KEY, snapshot_id TEXT, "
        "source_id TEXT, metadata TEXT)"
    )


def _tag_message(connection, message_id, snapshot_id):
    provenance = json.loads(
        connection.execute(
            "SELECT provenance FROM messages WHERE id=?", (message_id,)
        ).fetchone()[0]
    )
    provenance["source_snapshot"] = snapshot_id
    connection.execute(
        "UPDATE messages SET provenance=? WHERE id=?",
        (json.dumps(provenance), message_id),
    )


def _message_ids(connection, session_id):
    return [
        row[0]
        for row in connection.execute(
            "SELECT id FROM messages WHERE conversation_id=? ORDER BY ordinal",
            (session_id,),
        )
    ]


def test_legacy_archive_without_snapshot_table_keeps_its_original_source(archive_store):
    selected = _session(archive_store)
    path = wechat_archive_path(archive_store)
    before = path.read_bytes()
    page = query_chat_history(
        archive_store, source="wechat", session=selected.stable_id
    )
    assert page.messaging["source_snapshots"] == {}
    assert page.current_session.messaging["source_file"] == "fixture.xlsx"
    assert {message.messaging["source_file"] for message in page.items} == {
        "fixture.xlsx"
    }
    assert all("source_snapshot_id" not in message.messaging for message in page.items)
    assert path.read_bytes() == before
    original_export = build_chat_history_markdown(page)
    with sqlite3.connect(path) as connection:
        _snapshot_table(connection)
        connection.execute(
            "INSERT INTO source_snapshots VALUES(?,?,?,?)",
            (
                99,
                "unused",
                selected.messaging["partition_id"],
                json.dumps({"source_file": "Unused backup"}),
            ),
        )
    legacy_page = query_chat_history(
        archive_store, source="wechat", session=selected.stable_id
    )
    assert build_chat_history_markdown(legacy_page) == original_export


def test_mixed_snapshot_messages_use_their_own_source_without_changing_partition(
    archive_store,
):
    selected = _session(archive_store)
    path = wechat_archive_path(archive_store)
    partition = selected.messaging["partition_id"]
    with sqlite3.connect(path) as connection:
        before_partitions = _rows(connection, "partitions")
        _snapshot_table(connection)
        ids = _message_ids(connection, selected.stable_id)
        first = {
            "source_id": partition,
            "source_file": "Finder snapshot one",
            "source_label": "Finder backup",
            "backup_id": "device-backup-one",
            "input_databases": [
                {"path": f"DB/message_{index}.sqlite", "sha256": str(index) * 64}
                for index in range(7)
            ],
        }
        second = {
            **first,
            "source_file": "Finder snapshot two",
            "backup_id": "device-backup-two",
        }
        connection.executemany(
            "INSERT INTO source_snapshots VALUES(?,?,?,?)",
            [
                (17, "snapshot-one", partition, json.dumps(first)),
                (23, "snapshot-two", partition, json.dumps(second)),
            ],
        )
        _tag_message(connection, ids[1], 17)
        _tag_message(connection, ids[2], 23)
    before = path.read_bytes()
    page = query_chat_history(
        archive_store, source="wechat", session=selected.stable_id
    )
    contexts = [message.messaging for message in page.items]
    assert [context["source_file"] for context in contexts] == [
        "fixture.xlsx",
        "Finder snapshot one",
        "Finder snapshot two",
    ]
    assert [context.get("backup_id") for context in contexts] == [
        None,
        "device-backup-one",
        "device-backup-two",
    ]
    assert [context.get("source_snapshot_id") for context in contexts] == [
        None,
        "snapshot-one",
        "snapshot-two",
    ]
    assert contexts[1]["source_label"] == "Finder backup"
    assert contexts[1]["provenance"]["source_snapshot"] == 17
    assert contexts[2]["provenance"]["source_snapshot"] == 23
    assert all(
        "input_databases" not in context and "source_snapshot_metadata" not in context
        for context in contexts
    )
    assert page.messaging["source_snapshots"][17]["metadata"] == first
    assert page.messaging["source_snapshots"][23]["metadata"] == second
    assert page.current_session.messaging["source_file"] == "fixture.xlsx"
    exported = build_chat_history_markdown(page)
    messages = exported.split("\n### ")[1:]
    assert len(messages) == 3
    assert "- Source snapshot:" not in messages[0] and "- Backup ID:" not in messages[0]
    assert "- Archive: Finder backup / Finder snapshot one" in messages[1]
    assert "- Source snapshot: snapshot-one" in messages[1]
    assert "- Backup ID: device-backup-one" in messages[1]
    assert "device-backup-two" not in messages[1]
    assert "- Archive: Finder backup / Finder snapshot two" in messages[2]
    assert "- Source snapshot: snapshot-two" in messages[2]
    assert "- Backup ID: device-backup-two" in messages[2]
    assert "device-backup-one" not in messages[2]
    assert "input_databases" not in exported
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        assert _rows(connection, "partitions") == before_partitions
    assert path.read_bytes() == before


def test_snapshot_pointer_cannot_expose_another_account_source(archive_store):
    selected = _session(archive_store)
    other = _session(archive_store, "account:wxid_account_b")
    path = wechat_archive_path(archive_store)
    with sqlite3.connect(path) as connection:
        _snapshot_table(connection)
        metadata = {
            "source_id": other.messaging["partition_id"],
            "source_file": "Other account private source",
            "backup_id": "other-backup",
        }
        connection.execute(
            "INSERT INTO source_snapshots VALUES(?,?,?,?)",
            (
                31,
                "other-snapshot",
                other.messaging["partition_id"],
                json.dumps(metadata),
            ),
        )
        _tag_message(connection, _message_ids(connection, selected.stable_id)[0], 31)
    page = query_chat_history(
        archive_store, source="wechat", session=selected.stable_id
    )
    assert page.messaging["source_snapshots"] == {}
    assert all(
        message.messaging["source_file"] == "fixture.xlsx" for message in page.items
    )
    blocked = query_chat_history(
        archive_store,
        source="wechat",
        account="account:wxid_account_b",
        session=selected.stable_id,
    )
    assert not blocked.items
    assert blocked.messaging["source_snapshots"] == {}


@pytest.mark.parametrize("pointer", [True, "17", 17.0, -17, 0, 1 << 64])
def test_only_positive_integer_snapshot_pointers_are_resolved(archive_store, pointer):
    selected = _session(archive_store)
    partition = selected.messaging["partition_id"]
    path = wechat_archive_path(archive_store)
    with sqlite3.connect(path) as connection:
        _snapshot_table(connection)
        connection.executemany(
            "INSERT INTO source_snapshots VALUES(?,?,?,?)",
            [
                (
                    number,
                    "must-not-match",
                    partition,
                    json.dumps(
                        {"source_id": partition, "source_file": "incorrect source"}
                    ),
                )
                for number in (0, 1, 17)
            ],
        )
        _tag_message(
            connection, _message_ids(connection, selected.stable_id)[0], pointer
        )
    page = query_chat_history(
        archive_store, source="wechat", session=selected.stable_id
    )
    assert page.messaging["source_snapshots"] == {}
    assert all(
        message.messaging["source_file"] == "fixture.xlsx" for message in page.items
    )


@pytest.mark.parametrize(
    "metadata",
    [
        "not JSON",
        "[]",
        '{"source_id":"different-partition","source_file":"incorrect source"}',
    ],
)
def test_invalid_snapshot_metadata_falls_back_without_mutating_legacy_evidence(
    archive_store, metadata
):
    selected = _session(archive_store)
    partition = selected.messaging["partition_id"]
    path = wechat_archive_path(archive_store)
    with sqlite3.connect(path) as connection:
        _snapshot_table(connection)
        connection.execute(
            "INSERT INTO source_snapshots VALUES(?,?,?,?)",
            (17, "snapshot", partition, metadata),
        )
        _tag_message(connection, _message_ids(connection, selected.stable_id)[0], 17)
    before = path.read_bytes()
    page = query_chat_history(
        archive_store, source="wechat", session=selected.stable_id
    )
    assert page.messaging["source_snapshots"] == {}
    assert all(
        message.messaging["source_file"] == "fixture.xlsx" for message in page.items
    )
    assert path.read_bytes() == before


def test_missing_snapshot_table_does_not_break_a_referenced_message(archive_store):
    selected = _session(archive_store)
    path = wechat_archive_path(archive_store)
    with sqlite3.connect(path) as connection:
        _tag_message(connection, _message_ids(connection, selected.stable_id)[0], 17)
    page = query_chat_history(
        archive_store, source="wechat", session=selected.stable_id
    )
    assert page.messaging["source_snapshots"] == {}
    assert all(
        message.messaging["source_file"] == "fixture.xlsx" for message in page.items
    )
