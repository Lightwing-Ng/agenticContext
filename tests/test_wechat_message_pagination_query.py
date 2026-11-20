"""Bound full-body reads to visible WeChat messages without changing page membership.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

import hashlib
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import wechat_archive as archive


@pytest.fixture
def message_database():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    reads = []

    def body_read(identifier, value):
        reads.append(identifier)
        return value

    connection.create_function("body_read", 2, body_read)
    connection.executescript(archive._SCHEMA)
    connection.execute("ALTER TABLE messages RENAME TO message_storage")
    connection.execute("ATTACH DATABASE ':memory:' AS native")
    connection.execute(
        "CREATE TABLE native.message_storage AS SELECT * FROM main.message_storage WHERE 0"
    )
    connection.execute("CREATE UNIQUE INDEX native.message_id ON message_storage(id)")
    connection.execute(
        "CREATE INDEX native.message_scope ON message_storage(conversation_id,time_sort,ordinal)"
    )
    columns = (
        "id,conversation_id,ordinal,sender_id,sender_label,direction,"
        "body_read(id,text) AS text,time_sort,timestamp_value,timestamp_label,"
        "time_basis,provenance,quality_flags,search_text"
    )
    for database in ("main", "native"):
        connection.execute(
            "CREATE VIEW "
            + database
            + ".messages AS SELECT "
            + columns
            + " FROM message_storage"
        )
    batches = {"main": [], "native": []}
    suppressed = []
    for number in range(6_000):
        identifier = hashlib.sha256(f"message-{number}".encode()).hexdigest()
        conversation = "selected" if number < 3_000 else "unrelated"
        database = "main" if number % 2 else "native"
        known = number % 7 != 0
        batches[database].append(
            (
                identifier,
                conversation,
                number % 3,
                "sender",
                "Source name",
                "received",
                f"Original message {number}",
                1_000 + number // 11 if known else 0,
                "2026-01-01T00:00:00" if known else "",
                "Source timestamp",
                "Source clock",
                '{"source":"' + database + '","row":' + str(number) + "}",
                '["preserved_flag"]',
                "needle" if number % 13 == 0 else "other",
            )
        )
        if number % 19 == 0:
            suppressed.append((identifier,))
    for database, rows in batches.items():
        connection.executemany(
            "INSERT INTO "
            + database
            + ".message_storage VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    connection.executescript(
        "CREATE TEMP TABLE suppressed(id TEXT PRIMARY KEY) WITHOUT ROWID;"
        "CREATE TEMP VIEW timeline_messages AS SELECT * FROM main.messages "
        "UNION ALL SELECT * FROM native.messages;"
        "CREATE TEMP VIEW canonical_conversation_messages AS SELECT m.* FROM timeline_messages m "
        "WHERE NOT EXISTS(SELECT 1 FROM suppressed s WHERE s.id=m.id);"
    )
    connection.executemany("INSERT INTO suppressed VALUES(?)", suppressed)
    try:
        yield SimpleNamespace(connection=connection, reads=reads)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "table", ["main.messages", "temp.canonical_conversation_messages"]
)
@pytest.mark.parametrize("filtered", [False, True])
@pytest.mark.parametrize("unknown_time", [False, True])
def test_compact_message_pages_preserve_fields_order_search_and_suppression(
    message_database, table, filtered, unknown_time
):
    fixture = message_database
    connection = fixture.connection
    if unknown_time:
        for database in ("main", "native"):
            connection.execute(
                "UPDATE " + database + ".message_storage SET time_sort=0,ordinal=0"
            )
    where = " WHERE m.conversation_id=?"
    parameters = ["selected"]
    if filtered:
        where += " AND instr(m.search_text,?)>0"
        parameters.append("needle")
    expected = [
        dict(row)
        for row in connection.execute(
            "SELECT m.* FROM "
            + table
            + " m"
            + where
            + " ORDER BY (m.time_sort=0),m.time_sort,m.ordinal,m.id",
            parameters,
        )
    ]
    assert expected
    size = 41
    for offset in (0, 1, 100, len(expected) - size, len(expected) - 3, len(expected)):
        fixture.reads.clear()
        actual = [
            dict(row)
            for row in archive._message_page_rows(
                connection,
                table,
                where,
                parameters,
                conversation_id="selected",
                page_size=size,
                offset=offset,
            )
        ]
        assert actual == expected[offset : offset + size]
        assert len(fixture.reads) == len(actual)
        assert set(fixture.reads) == {row["id"] for row in actual}


def test_large_message_page_keeps_order_across_bounded_identity_batches(
    message_database,
):
    fixture = message_database
    table = "temp.canonical_conversation_messages"
    expected = [
        dict(row)
        for row in fixture.connection.execute(
            "SELECT m.* FROM " + table + " m WHERE m.conversation_id=? "
            "ORDER BY (m.time_sort=0),m.time_sort,m.ordinal,m.id LIMIT 601 OFFSET 1000",
            ("selected",),
        )
    ]
    fixture.reads.clear()
    actual = [
        dict(row)
        for row in archive._message_page_rows(
            fixture.connection,
            table,
            " WHERE m.conversation_id=?",
            ["selected"],
            conversation_id="selected",
            page_size=601,
            offset=1000,
        )
    ]
    assert len(actual) == 601
    assert actual == expected
    assert len(fixture.reads) == len(actual)


@pytest.mark.parametrize(
    ("page_size", "status"),
    [(2, "duplicate_page_identity"), (1, "duplicate_page_source_row")],
)
def test_duplicate_source_identities_fail_closed(message_database, page_size, status):
    fixture = message_database
    row = list(
        fixture.connection.execute(
            "SELECT * FROM main.message_storage WHERE conversation_id='selected' "
            "AND id NOT IN (SELECT id FROM suppressed) LIMIT 1"
        ).fetchone()
    )
    row[6] = "A conflicting source body"
    fixture.connection.execute(
        "INSERT INTO native.message_storage VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row
    )
    fixture.reads.clear()
    with pytest.raises(archive.WechatTimelineProjectionError) as error:
        archive._message_page_rows(
            fixture.connection,
            "temp.canonical_conversation_messages",
            " WHERE m.conversation_id=? AND m.id=?",
            ["selected", row[0]],
            conversation_id="selected",
            page_size=page_size,
            offset=0,
        )
    assert error.value.status == status
    if page_size == 2:
        assert fixture.reads == []


def test_missing_selected_source_row_fails_closed(message_database):
    fixture = message_database
    identifier = fixture.connection.execute(
        "SELECT id FROM main.message_storage WHERE conversation_id='selected' LIMIT 1"
    ).fetchone()[0]

    class MissingRowConnection:
        """Simulate a broken source boundary only inside the disposable database."""

        def __init__(self):
            self.calls = 0

        def execute(self, statement, parameters):
            self.calls += 1
            if self.calls == 2:
                fixture.connection.execute(
                    "DELETE FROM main.message_storage WHERE id=?", (identifier,)
                )
            return fixture.connection.execute(statement, parameters)

    with pytest.raises(archive.WechatTimelineProjectionError) as error:
        archive._message_page_rows(
            MissingRowConnection(),
            "main.messages",
            " WHERE m.conversation_id=? AND m.id=?",
            ["selected", identifier],
            conversation_id="selected",
            page_size=1,
            offset=0,
        )
    assert error.value.status == "missing_page_source_row"
