"""Preserve WeChat list search membership, pagination, and adjusted totals.

Code version: v1.0.0-codex.0
"""

from contextlib import closing
import hashlib
import sqlite3

import pytest

from tests import test_wechat_conversation_events as events


event_store = events.event_store


@pytest.fixture
def search_store(event_store):
    repository = event_store.repository
    owner = "account:" + events.OWNER
    other_owner = "account:other-search-owner"
    sessions = {}
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        conversation = list(connection.execute("SELECT * FROM conversations").fetchone())
        message = list(connection.execute("SELECT * FROM messages LIMIT 1").fetchone())
        partition = list(connection.execute("SELECT * FROM partitions LIMIT 1").fetchone())
        partition[0:3] = ["other-partition", other_owner, "other-search-owner"]
        connection.execute("INSERT INTO partitions VALUES(?,?,?,?,?,?,?,?)", partition)
        for title, account, kind, clock, bodies in (
            ("Synthetic Alpha", other_owner, "group", 20, ("Private in body",)),
            ("Zulu", owner, "group", 10, ("Synthetic term", "Private term")),
            ("Synthetic unmatched", owner, "group", 30, ("Unrelated",)),
            ("Other unmatched", owner, "group", 40, ("Private term",)),
        ):
            identifier = "wechat:" + hashlib.sha256(title.encode()).hexdigest()
            sessions[title] = identifier
            row = conversation.copy()
            row[0] = identifier
            if account == other_owner:
                row[1:3] = partition[:2]
            row[4:9] = [title, kind, len(bodies), clock, clock]
            connection.execute("INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", row)
            for ordinal, body in enumerate(bodies, 1):
                row = message.copy()
                row[0:3] = [
                    hashlib.sha256(f"{identifier}:{ordinal}".encode()).hexdigest(),
                    identifier,
                    ordinal,
                ]
                row[6], row[7], row[-1] = body, clock, body.casefold()
                connection.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
    connection, proof = events._run(event_store)
    with closing(connection):
        assert proof["count_delta"] == 3
    return repository, owner, other_owner, sessions


@pytest.mark.parametrize("sort", ["newest", "oldest", "name"])
def test_search_counts_each_matching_conversation_once(search_store, sort):
    repository, owner, other_owner, sessions = search_store
    expected = {
        "newest": ["Synthetic Alpha", "Zulu", "Synthetic conversation"],
        "oldest": ["Synthetic conversation", "Zulu", "Synthetic Alpha"],
        "name": ["Synthetic Alpha", "Synthetic conversation", "Zulu"],
    }[sort]
    query = "SYNTHETIC PRIVATE"
    all_rows = repository.query(query=query, sort=sort)
    assert [row["title"] for row in all_rows["conversations"]] == expected
    assert all_rows["count"] == all_rows["conversation_count"] == 3
    assert all_rows["message_count"] == 6
    assert {
        row["title"]: row["message_count"] for row in all_rows["conversations"]
    } == {"Synthetic Alpha": 1, "Synthetic conversation": 3, "Zulu": 2}

    last = repository.query(query=query, sort=sort, page_size=1, page=999)
    assert (last["page"], last["pages"], last["count"], last["message_count"]) == (3, 3, 3, 6)
    assert [row["title"] for row in last["conversations"]] == [expected[-1]]

    for account, title, count in ((owner, "Zulu", 2), (other_owner, "Synthetic Alpha", 1)):
        scoped = repository.query(account=account, kind="group", query=query, sort=sort)
        assert (scoped["count"], scoped["message_count"]) == (1, count)
        assert [row["id"] for row in scoped["conversations"]] == [sessions[title]]
    absent = repository.query(account=other_owner, kind="direct", query=query)
    assert (absent["conversations"], absent["count"], absent["message_count"]) == ([], 0, 0)
