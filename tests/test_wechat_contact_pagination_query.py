"""Bound deep-page body reads while preserving statement order and source fields.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import hashlib
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import wechat_contact_analysis as analysis


def _identifier(value):
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture
def statement_database():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    reads = []

    def body_read(value):
        reads.append(value)
        return value

    connection.create_function("body_read", 1, body_read)
    connection.executescript(analysis._REFERENCE_SCHEMA)
    connection.execute("ATTACH DATABASE ':memory:' AS archive")
    connection.executescript(
        """
        CREATE TABLE archive.message_storage (
            id TEXT PRIMARY KEY, text TEXT NOT NULL,
            provenance TEXT NOT NULL, quality_flags TEXT NOT NULL
        );
        CREATE VIEW archive.messages AS
            SELECT id,body_read(text) AS text,provenance,quality_flags
            FROM message_storage;
        CREATE TABLE archive.conversations (
            id TEXT PRIMARY KEY,title TEXT,kind TEXT,peer_id TEXT
        );
        CREATE TABLE archive.partitions (
            id TEXT PRIMARY KEY,account_id TEXT,account_label TEXT,
            identity_status TEXT,source_label TEXT,source_file TEXT
        );
        """
    )
    for number in range(2):
        connection.execute(
            "INSERT INTO archive.partitions VALUES(?,?,?,?,?,?)",
            (
                f"source-{number}",
                f"owner-{number}",
                f"Account {number}",
                "Verified archive ID",
                "Frozen text fixture",
                f"fixture-{number}",
            ),
        )
    for number in range(6):
        connection.execute(
            "INSERT INTO archive.conversations VALUES(?,?,?,?)",
            (f"conversation-{number}", f"Room {number}", "group", f"peer-{number}"),
        )
    for number in range(6_000):
        statement = _identifier(f"statement-{number}")
        representative = _identifier(f"reference-{number}")
        conversation = f"conversation-{number % 6}"
        connection.execute(
            "INSERT INTO statement_groups VALUES(?,?,?)",
            (statement, representative, f"peer-{number % 6}"),
        )
        members = [representative]
        if number % 10 == 0:
            members.append(_identifier(f"duplicate-{number}"))
        for position, member in enumerate(members):
            known_time = number % 5 != 0
            timestamp = f"2026-01-{number % 28 + 1:02}T00:00:00" if known_time else ""
            connection.execute(
                "INSERT INTO archive.message_storage VALUES(?,?,?,?)",
                (member, f"Original statement {number}", "{}", "[]"),
            )
            connection.execute(
                "INSERT INTO refs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    member,
                    member,
                    "[]",
                    "wechat_message_" + member,
                    conversation,
                    f"source-{(number + position) % 2}",
                    f"account:owner-{(number + position) % 2}",
                    1_000 + number // 7 if known_time else 0,
                    number % 3,
                    int(known_time),
                    timestamp,
                    timestamp or "Unknown time",
                    "Source clock" if known_time else "Unavailable source time",
                    timestamp,
                ),
            )
            connection.execute(
                "INSERT INTO statement_members VALUES(?,?)", (member, statement)
            )
    try:
        yield SimpleNamespace(connection=connection, reads=reads)
    finally:
        connection.close()


@pytest.mark.parametrize("sort", ["newest", "oldest"])
@pytest.mark.parametrize("filtered", [False, True])
def test_paged_rows_preserve_unique_statement_order_and_all_source_fields(
    statement_database, sort, filtered
):
    connection = statement_database.connection
    where = ""
    if filtered:
        connection.execute(
            "CREATE TEMP TABLE contact_statement_matches(id TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        connection.executemany(
            "INSERT INTO contact_statement_matches VALUES(?)",
            [(_identifier(f"statement-{number}"),) for number in range(0, 6_000, 13)],
        )
        where = " WHERE g.id IN (SELECT id FROM contact_statement_matches)"
    expected = [
        dict(row)
        for row in connection.execute(analysis._statement_rows_query(where, sort))
    ]
    assert len(expected) == (462 if filtered else 6_000)
    assert len({row["statement_id"] for row in expected}) == len(expected)
    size = 17
    for offset in (0, 1, 153, len(expected) - size, len(expected) - 3, len(expected)):
        statement_database.reads.clear()
        actual = [
            dict(row)
            for row in connection.execute(
                analysis._statement_page_rows_query(where, sort), (size, offset)
            )
        ]
        assert actual == expected[offset : offset + size]
        assert len(statement_database.reads) == len(actual)


@pytest.mark.parametrize("sort", ["newest", "oldest"])
def test_deep_unknown_time_ties_read_bodies_only_for_the_visible_page(
    statement_database, sort
):
    connection = statement_database.connection
    # An unresolved-clock block must sort stable IDs without loading every body.
    connection.execute("UPDATE refs SET has_time=0,time_sort=0,ordinal=0")
    size, offset = 50, 5_900
    expected = [
        dict(row)
        for row in connection.execute(
            analysis._statement_rows_query("", sort) + " LIMIT ? OFFSET ?",
            (size, offset),
        )
    ]
    assert len(statement_database.reads) > size
    statement_database.reads.clear()
    actual = [
        dict(row)
        for row in connection.execute(
            analysis._statement_page_rows_query("", sort), (size, offset)
        )
    ]
    assert actual == expected
    assert len(actual) == size
    assert len(statement_database.reads) == size
