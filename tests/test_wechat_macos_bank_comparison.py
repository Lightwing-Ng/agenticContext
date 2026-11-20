"""Repair updates use their complete event key as the comparison bank grows.

Code version: v1.0.0-codex.0
"""

from copy import deepcopy
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import wechat_macos_recovery_bank as bank
from tests import test_wechat_macos_recovery_bank as fixtures
from tests import test_wechat_macos_reparse as repairs


mac_archive = fixtures.mac_archive
bank_archive = fixtures.bank_archive


@pytest.fixture(params=("same_occurrence", "new_snapshot"))
def repair_update(bank_archive, monkeypatch, request):
    """Capture the SQL actually executed by either production repair branch."""
    old, new = repairs._packets(bank_archive, "quote")
    fixtures._publish(bank_archive, packets=[old], name="before-keyed-repair")
    with sqlite3.connect(bank_archive.bank_path) as connection:
        identifier = connection.execute("SELECT id FROM messages").fetchone()[0]
    if request.param == "new_snapshot":
        new = deepcopy(new)
        new["source"].update(
            snapshot_id="another-authenticated-snapshot",
            main_sha256="d" * 64,
            wal_sha256="e" * 64,
        )
    observed = []
    connect = sqlite3.connect

    class RecordedConnection(sqlite3.Connection):
        def execute(self, query, parameters=()):
            if query.startswith("UPDATE comparison_events SET signature=?"):
                observed.append((query, tuple(parameters)))
            return super().execute(query, parameters)

    def recorded_connect(*args, **kwargs):
        return connect(*args, **kwargs, factory=RecordedConnection)

    monkeypatch.setattr(bank.sqlite3, "connect", recorded_connect)
    _candidate, receipt = fixtures._stage(
        bank_archive, packets=[new], name="keyed-repair-candidate"
    )
    assert receipt["reparsed"] == 1
    assert len(observed) == 1
    return SimpleNamespace(
        query=observed[0][0], parameters=observed[0][1],
        session=new["session_id"], server=new["messages"][0]["server_id"],
        message=identifier,
    )


def _comparison_connection():
    connection = sqlite3.Connection(":memory:")
    connection.execute(
        "CREATE TABLE comparison_events("
        "conversation_id TEXT,server_id TEXT,message_id TEXT,signature TEXT,"
        "canonical_store TEXT,PRIMARY KEY(conversation_id,server_id,message_id)) "
        "WITHOUT ROWID"
    )
    return connection


def test_both_repair_branches_search_the_full_primary_key(repair_update):
    with _comparison_connection() as connection:
        plans = [
            row[3] for row in connection.execute(
                "EXPLAIN QUERY PLAN " + repair_update.query, repair_update.parameters
            )
        ]

    assert any(
        "SEARCH comparison_events USING PRIMARY KEY" in plan
        and all(column + "=?" in plan for column in (
            "conversation_id", "server_id", "message_id"
        ))
        for plan in plans
    )
    assert not any("SCAN comparison_events" in plan for plan in plans)


def test_repair_work_does_not_grow_with_unrelated_comparison_rows(repair_update):
    work = []
    for size in (1_000, 20_000):
        with _comparison_connection() as connection:
            connection.executemany(
                "INSERT INTO comparison_events VALUES(?,?,?,?,?)",
                (
                    (f"scope-{index // 100}", str(index), f"event-{index}", "old", "bank")
                    for index in range(size)
                ),
            )
            connection.execute(
                "INSERT INTO comparison_events VALUES(?,?,?,?,?)",
                (repair_update.session, repair_update.server, repair_update.message,
                 "old", "bank"),
            )
            steps = 0

            def count_step():
                nonlocal steps
                steps += 1
                return 0

            connection.set_progress_handler(count_step, 1)
            connection.execute(repair_update.query, repair_update.parameters)
            connection.set_progress_handler(None, 0)
            work.append(steps)

    assert work[1] <= work[0] + 16


def test_repair_does_not_update_another_scope_or_server_with_the_same_message_id(
    repair_update,
):
    target = (repair_update.session, repair_update.server, repair_update.message)
    neighbors = (
        ("another-scope", repair_update.server, repair_update.message),
        (repair_update.session, "another-server", repair_update.message),
        (repair_update.session, repair_update.server, "another-message"),
    )
    with _comparison_connection() as connection:
        connection.executemany(
            "INSERT INTO comparison_events VALUES(?,?,?,?,?)",
            ((*key, "old", "bank") for key in (target, *neighbors)),
        )
        connection.execute(repair_update.query, repair_update.parameters)
        rows = {
            row[:3]: row[3]
            for row in connection.execute("SELECT * FROM comparison_events")
        }

    assert rows[target] == repair_update.parameters[0]
    assert all(rows[key] == "old" for key in neighbors)


@pytest.mark.parametrize("schema", ("3", "4"))
def test_previous_v140_banks_remain_readable_after_the_query_fix(bank_archive, schema):
    old, new = repairs._packets(bank_archive, "quote")
    fixtures._publish(bank_archive, packets=[old], name="previous-schema-three")
    if schema == "4":
        fixtures._publish(bank_archive, packets=[new], name="previous-schema-four")
    with sqlite3.connect(bank_archive.bank_path) as connection:
        connection.execute(
            "UPDATE archive_info SET value='v1.4.0-codex.0' WHERE key='code_version'"
        )
    before = bank_archive.bank_path.read_bytes()

    with sqlite3.connect(bank_archive.bank_path) as connection:
        metadata = bank._validate_bank(connection, bank_archive.history)

    assert metadata["schema_version"] == schema
    assert metadata["code_version"] == "v1.4.0-codex.0"
    assert bank_archive.bank_path.read_bytes() == before
