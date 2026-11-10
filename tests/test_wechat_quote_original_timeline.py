"""Off-page quote lookup uses accepted rows instead of older source projections.

Code version: v1.0.0-codex.0
"""

import sqlite3

import pytest

from app.core.wechat_archive import WechatArchiveRepository, _file_identity
from tests import test_wechat_secondary_completions as completion


mac_archive = completion.mac_archive
bank_archive = completion.bank_archive
source_binding = completion.source_binding
witness = completion.witness
completion_fixture = completion.completion_fixture


def test_final_completion_supersedes_older_same_id_quote_candidate(completion_fixture):
    fixture = completion_fixture
    completion._publish(fixture, completion._stage(fixture))
    repository = WechatArchiveRepository(fixture["root"])
    with completion.completions._readonly(fixture["history"]) as connection:
        _, table, status = repository._timeline_tables(
            connection, _file_identity(fixture["history"]),
            conversation_id=fixture["conversation"]["id"], strict=True,
        )
        assert status == "ready"
        # A legacy projection may retain this ID before its actor was completed.
        connection.execute("CREATE TEMP TABLE macos_recovery_validated(id TEXT)")
        connection.execute("CREATE TEMP TABLE macos_recovery_messages AS SELECT * FROM main.messages WHERE 0")
        connection.execute(
            "INSERT INTO macos_recovery_messages VALUES("
            + ",".join("?" for _ in completion.completions.COLUMNS) + ")",
            tuple(fixture["image"][key] for key in completion.completions.COLUMNS),
        )
        reply = dict(connection.execute(
            "SELECT * FROM " + table + " WHERE id=?", (fixture["quote"]["id"],)
        ).fetchone())
        rows = repository._quoted_resource_originals(
            connection, [reply], fixture["conversation"]["id"], message_table=table,
        )
        assert len(rows) == 1 and rows[0]["id"] == fixture["image"]["id"]
        assert fixture["image"]["sender_id"] is None
        assert rows[0]["sender_id"] == completion.native.MEMBER
        assert "_macos_recovery_validated" not in rows[0]


@pytest.mark.parametrize("size", [1001, 8 * 1024**2 + 1])
def test_accepted_timeline_quote_lookup_retains_existing_budgets(size):
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE accepted(id TEXT,conversation_id TEXT,provenance TEXT,text TEXT)")
        if size == 1001:
            rows = [(str(index), "session", '{"server_id":"5"}', "small") for index in range(size)]
        else:
            rows = [("id", "session", '{"server_id":"5"}', "x" * size)]
        connection.executemany("INSERT INTO accepted VALUES(?,?,?,?)", rows)
        reply = {"provenance": '{"message_type":49,"text_extraction_kind":"quoted_reply","quoted_reference":{"source_ids":{"svrid":"5"}}}'}
        assert WechatArchiveRepository._quoted_resource_originals(
            connection, [reply], "session", message_table="accepted",
        ) == []
