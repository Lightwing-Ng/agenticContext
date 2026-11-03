"""Scoped MCP timelines preserve evidence without projecting unrelated conversations.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from contextlib import closing
import json
import sqlite3

import pytest

from app.core.wechat_archive import (
    WechatArchiveRepository,
    WechatTimelineProjectionError,
    _file_identity,
    wechat_archive_path,
)
from app.core import wechat_media_placeholders
from tests.wechat_media_placeholder_fixtures import (
    CONVERSATION,
    placeholder_store as placeholder_store,
)
from tests.test_wechat_resource_timeline import (
    _reference,
    _session,
    _sidecar,
    store as store,
)


def _connection(history):
    connection = sqlite3.connect(history.as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA temp_store=MEMORY")
    return connection


def test_ordinary_conversation_never_reads_unrelated_media_witnesses(placeholder_store):
    fixture = placeholder_store
    repository = WechatArchiveRepository(fixture.store)
    reads = []

    def deny_message_reads(action, table, column, database, source):
        if action == sqlite3.SQLITE_READ and table == "messages":
            reads.append(column)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with closing(_connection(fixture.history)) as connection:
        connection.set_authorizer(deny_message_reads)
        result = repository._timeline_tables(
            connection,
            _file_identity(fixture.history),
            conversation_id="wechat:ordinary-conversation",
            strict=True,
        )
    assert result == ("main.conversations", "main.messages", "absent")
    assert reads == []


def test_scoped_projection_keeps_audited_media_and_original_database_bytes(placeholder_store):
    fixture = placeholder_store
    before = fixture.history.read_bytes(), fixture.overlay.read_bytes()
    with closing(_connection(fixture.history)) as connection:
        conversations, messages, status = WechatArchiveRepository(fixture.store)._timeline_tables(
            connection,
            _file_identity(fixture.history),
            conversation_id=CONVERSATION,
            strict=True,
        )
        rows = connection.execute(
            f"SELECT ordinal,text FROM {messages} WHERE conversation_id=? ORDER BY ordinal",
            (CONVERSATION,),
        ).fetchall()
        count = connection.execute(
            f"SELECT message_count FROM {conversations} WHERE id=?", (CONVERSATION,)
        ).fetchone()[0]
    assert status == "absent"
    assert count == len(rows) == 20
    assert [row[0] for row in rows if row[1] == "[Image]"] == [8, 13]
    assert (fixture.history.read_bytes(), fixture.overlay.read_bytes()) == before


@pytest.mark.parametrize("budget", ["rows", "bytes"])
def test_scoped_proof_budget_is_explicit_and_does_not_change_browser_projection(
    placeholder_store, monkeypatch, budget
):
    fixture = placeholder_store
    setting = "MAX_SCOPED_WITNESS_ROWS" if budget == "rows" else "MAX_SCOPED_WITNESS_BYTES"
    monkeypatch.setattr(wechat_media_placeholders, setting, 1)
    repository = WechatArchiveRepository(fixture.store)
    with closing(_connection(fixture.history)) as connection:
        with pytest.raises(WechatTimelineProjectionError) as failure:
            repository._timeline_tables(
                connection,
                _file_identity(fixture.history),
                conversation_id=CONVERSATION,
                strict=True,
            )
    assert failure.value.status == "incomplete"
    with closing(_connection(fixture.history)) as connection:
        _conversations, messages, _status = repository._timeline_tables(
            connection, _file_identity(fixture.history)
        )
        assert connection.execute(f"SELECT COUNT(*) FROM {messages}").fetchone()[0] == 20


def test_invalid_scoped_media_proof_is_not_silently_omitted(placeholder_store):
    fixture = placeholder_store
    entries = json.loads(fixture.overlay.read_text())
    entries["entries"][0]["member_sha256"] = "0" * 64
    fixture.overlay.write_text(json.dumps(entries))
    with closing(_connection(fixture.history)) as connection:
        with pytest.raises(WechatTimelineProjectionError) as failure:
            WechatArchiveRepository(fixture.store)._timeline_tables(
                connection,
                _file_identity(fixture.history),
                conversation_id=CONVERSATION,
                strict=True,
            )
    assert failure.value.status == "invalid"


def test_placeholder_id_locates_its_scope_without_scanning_message_provenance(placeholder_store):
    fixture = placeholder_store
    identifier = wechat_media_placeholders.media_placeholder_id(fixture.entries[0])

    def deny_provenance(action, table, column, database, source):
        if action == sqlite3.SQLITE_READ and table == "messages" and column == "provenance":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with closing(_connection(fixture.history)) as connection:
        connection.set_authorizer(deny_provenance)
        repository = WechatArchiveRepository(fixture.store)
        assert repository._timeline_conversation_id(connection, identifier) == CONVERSATION
        assert repository._timeline_conversation_id(connection, "0" * 64) is None


def test_scoped_resource_projection_validates_only_requested_conversation(store):
    native_store = store
    first = _session(native_store)
    second = _session(native_store, owner="wxid_account_b")
    first_message = _reference(first, "Scoped resource", 1)
    second_message = _reference(second, "Unrelated resource", 2)
    resource = _sidecar(native_store, [(first, first_message), (second, second_message)])
    with closing(sqlite3.connect(resource)) as connection:
        connection.execute(
            "UPDATE messages SET provenance='invalid-unrelated-proof' WHERE conversation_id=?",
            (second["id"],),
        )
        connection.commit()
    history = wechat_archive_path(native_store)
    before = history.read_bytes(), resource.read_bytes()
    repository = WechatArchiveRepository(native_store)
    with closing(_connection(history)) as connection:
        assert repository._timeline_conversation_id(connection, first_message[0]) == first["id"]
    with closing(_connection(history)) as connection:
        _conversations, messages, status = repository._timeline_tables(
            connection,
            _file_identity(history),
            conversation_id=first["id"],
            strict=True,
        )
        assert status == "ready"
        assert connection.execute(
            f"SELECT COUNT(*) FROM {messages} WHERE id=?", (first_message[0],)
        ).fetchone()[0] == 1
        assert connection.execute(
            f"SELECT COUNT(*) FROM {messages} WHERE id=?", (second_message[0],)
        ).fetchone()[0] == 0
    with closing(_connection(history)) as connection:
        assert repository._resource_timeline_tables(connection, _file_identity(history))[2] == "invalid"
    assert (history.read_bytes(), resource.read_bytes()) == before
