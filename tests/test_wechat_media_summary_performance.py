"""Layered conversation summaries are evaluated once without expanding messages.

Code version: v1.0.0-codex.0
"""

import sqlite3

import pytest

from app.core.wechat_media_event_index import attach_media_event_index
from tests import test_wechat_media_event_global_timeline as fixtures


global_media_store = fixtures.global_media_store
placeholder_store = fixtures.placeholder_store


@pytest.mark.parametrize("scoped", [False, True])
def test_layered_summary_views_are_not_repeated_for_each_media_conversation(
    global_media_store, scoped
):
    fixture = global_media_store
    connection = sqlite3.connect(
        fixture.history.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            "CREATE TEMP TABLE input_summary_rows AS SELECT * FROM main.conversations WHERE 0"
        )
        seed = list(connection.execute("SELECT * FROM main.conversations LIMIT 1").fetchone())
        unrelated = []
        for number in range(6_000):
            row = seed.copy()
            row[0] = f"unrelated-summary-{number}"
            unrelated.append(row)
        connection.executemany(
            "INSERT INTO input_summary_rows VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            unrelated,
        )
        connection.execute("INSERT INTO input_summary_rows SELECT * FROM main.conversations")
        expected_reads = connection.execute("SELECT COUNT(*) FROM input_summary_rows").fetchone()[0]
        observed = []

        def observe(identifier):
            observed.append(identifier)
            return 1

        connection.create_function("observe_summary", 1, observe)
        # LIMIT preserves the incoming view boundary, as compound overlay
        # summaries do in production, so repeated scope lookups cannot hide
        # behind an optimizer's predicate pushdown in this fixture.
        connection.execute(
            "CREATE TEMP VIEW layered_input_summaries AS "
            "SELECT * FROM input_summary_rows WHERE observe_summary(id) LIMIT -1"
        )
        spec = fixture.specs[0]
        conversations, _messages, status = attach_media_event_index(
            connection,
            fixture.history,
            "layered_input_summaries",
            "main.messages",
            conversation_id=spec.conversation if scoped else None,
        )
        assert status == "ready"
        assert connection.execute(
            f"SELECT message_count FROM {conversations} WHERE id=?",
            (spec.conversation,),
        ).fetchone()[0] == 738
        assert connection.execute(
            f"SELECT COUNT(*) FROM {conversations} WHERE id LIKE 'unrelated-summary-%'"
        ).fetchone()[0] == 6_000
        connection.execute(f"SELECT COUNT(*),SUM(message_count) FROM {conversations}").fetchone()
        assert len(observed) == expected_reads
    finally:
        connection.close()
