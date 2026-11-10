"""Recovery summary joins evaluate a compound incoming catalog only once.

Code version: v1.0.0-codex.0
"""

import sqlite3

import pytest

from app.core import wechat_macos_recovery_bank as bank
from app.core.wechat_archive import _SCHEMA


@pytest.mark.parametrize("scoped", [False, True])
def test_bank_summary_lookup_stays_bounded_for_materialized_input_catalog(
    tmp_path, monkeypatch, scoped
):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(_SCHEMA)
    connection.execute("ATTACH DATABASE ':memory:' AS macos_recovery_bank")
    connection.execute(
        "CREATE TABLE macos_recovery_bank.conversations AS SELECT * FROM main.conversations WHERE 0"
    )
    connection.execute(
        "CREATE TABLE macos_recovery_bank.messages AS SELECT * FROM main.messages WHERE 0"
    )
    connection.execute(
        "CREATE TABLE macos_recovery_bank.conversation_proofs(conversation_id TEXT,proof TEXT)"
    )
    main_count, bank_count = 10_222, 222
    rows = [
        (
            f"conversation-{number:05d}", "source", "owner", f"peer-{number}",
            f"Conversation {number}", "direct", 2, 1, 2, "first", "last", "Original latest",
        )
        for number in range(main_count)
    ]
    connection.executemany("INSERT INTO main.conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    connection.executemany(
        "INSERT INTO macos_recovery_bank.conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        [(*row[:6], 3, 0, 3, "", "newest", "Bank latest") for row in rows[:bank_count]],
    )
    # A MATERIALIZED CTE models SQLite's compound overlay plans. Repeating an
    # ID lookup against this view otherwise reconstructs every unrelated row.
    connection.execute(
        "CREATE TEMP VIEW compound_conversation_input AS "
        "WITH incoming AS MATERIALIZED(SELECT * FROM main.conversations) SELECT * FROM incoming"
    )
    monkeypatch.setattr(bank, "_attach", lambda connection, history: {"schema_version": "1"})
    ticks = []
    connection.set_progress_handler(lambda: ticks.append(1) or int(len(ticks) > 4_000), 1_000)
    try:
        conversations, _messages, status = bank.attach_mac_recovery_bank(
            connection, tmp_path / "history.sqlite3", "compound_conversation_input", "main.messages",
            conversation_id=rows[0][0] if scoped else None,
        )
        assert status == "ready"
        actual = connection.execute(f"SELECT * FROM {conversations} ORDER BY id").fetchall()
        assert len(actual) == main_count
        expected_updates = 1 if scoped else bank_count
        assert sum(row["message_count"] for row in actual) == main_count * 2 + expected_updates * 3
        assert actual[0]["first_sort"] == 1
        assert actual[0]["last_sort"] == 3
        assert actual[0]["last_time"] == "newest"
        assert actual[0]["latest_text"] == "Bank latest"
        assert actual[-1]["latest_text"] == "Original latest"
        assert len(ticks) <= 4_000
    finally:
        connection.set_progress_handler(None, 0)
        connection.close()
