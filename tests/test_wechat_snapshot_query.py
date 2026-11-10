"""Persisted overview summaries preserve live filtering and canonical counts.

Code version: v1.0.0-codex.0
"""

import hashlib
import sqlite3
from unittest.mock import patch

import pytest

from app.core import wechat_browser_snapshot as snapshots
from tests import test_wechat_conversation_events as fixtures


event_store = fixtures.event_store


def _extend_conversations(fixture):
    with sqlite3.connect(fixture.repository.path) as connection:
        template = list(connection.execute("SELECT * FROM conversations").fetchone())
        partition = list(connection.execute("SELECT * FROM partitions LIMIT 1").fetchone())
        partition[0:3] = ["other-partition", "account:another-owner", "another-owner"]
        connection.execute("INSERT INTO partitions VALUES(?,?,?,?,?,?,?,?)", partition)
        for number, title in enumerate(("Alpha", "Zulu", "Another owner"), 1):
            row = template.copy()
            row[0] = "wechat:" + hashlib.sha256(title.encode()).hexdigest()
            row[4], row[5], row[6] = title, "group", 0
            row[7], row[8] = number + 10, number + 20
            if number == 3:
                row[1], row[2] = partition[0], partition[1]
            connection.execute("INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", row)


@pytest.mark.parametrize(
    "parameters",
    [
        {},
        {"sort": "oldest"},
        {"sort": "name", "page_size": 2, "page": 2},
        {"account": "account:" + fixtures.OWNER},
        {"account": "account:another-owner"},
        {"kind": "group"},
        {"kind": "direct", "page": 999},
    ],
)
def test_persisted_summary_filters_match_original_timeline(event_store, parameters):
    _extend_conversations(event_store)
    repository = event_store.repository
    with patch.object(snapshots, "load_conversation_snapshot", return_value=None):
        expected = repository._query_uncached(**parameters)
    actual = repository._query_uncached(**parameters)
    assert actual == expected


def test_new_event_proofs_adjust_persisted_raw_counts_without_rebuilding(
    event_store, monkeypatch
):
    repository = event_store.repository
    assert repository._query_uncached()["message_count"] == 6
    connection, _result = fixtures._run(event_store)
    connection.close()

    def unexpected_rebuild(*args, **kwargs):
        pytest.fail("Publishing an event proof must not rebuild the raw overview.")

    monkeypatch.setattr(repository, "_timeline_tables", unexpected_rebuild)
    assert repository._query_uncached()["message_count"] == 3
    fixtures.events._cache_path(repository, fixtures.SESSION).unlink()
    assert repository._query_uncached()["message_count"] == 6


@pytest.mark.parametrize("parameters", [{"query": "Private"}, {"session": fixtures.SESSION}])
def test_search_and_details_keep_their_full_message_timeline(
    event_store, monkeypatch, parameters
):
    def unexpected_snapshot(*args, **kwargs):
        pytest.fail("Search and details must use the complete message timeline.")

    monkeypatch.setattr(snapshots, "load_conversation_snapshot", unexpected_snapshot)
    result = event_store.repository._query_uncached(**parameters)
    assert result["count"] > 0
