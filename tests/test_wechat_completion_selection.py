"""Bounded activity probes preserve full-row and typed notice precedence.

Code version: v1.0.0-codex.0
"""

import json
import sqlite3

import pytest

from app.core import wechat_completion_selection as selection
from app.core import wechat_secondary_completions as common
from app.core.wechat_completion_json_codec import encode_json_cell, register_json_codec


_TYPES = (
    "TEXT", "TEXT", "INTEGER", "TEXT", "TEXT", "TEXT", "TEXT", "REAL",
    "TEXT", "TEXT", "TEXT", "TEXT", "TEXT", "TEXT",
)


def _row(identifier="exact", **changes):
    return {
        "id": identifier, "conversation_id": "group-a", "ordinal": 1,
        "sender_id": None, "sender_label": "Known author", "direction": "received",
        "text": "Retained body", "time_sort": 100.0,
        "timestamp_value": "2023-11-15T06:13:30+08:00", "timestamp_label": "Retained clock",
        "time_basis": "native", "provenance": '{"server_id":"900"}',
        "quality_flags": "[]", "search_text": "retained body", **changes,
    }


def _insert(connection, table, row):
    connection.execute(
        "INSERT INTO " + table + " VALUES(" + ",".join("?" for _ in common.COLUMNS) + ")",
        tuple(row[column] for column in common.COLUMNS),
    )


def _entry(connection, row, *, operation="replace", server_id=None, encoded=False):
    original = common._json(row) if operation == "replace" else None
    plan = common._json({"event": {"server_id": server_id}}) if operation == "append" else None
    if encoded:
        original, plan = encode_json_cell(original), encode_json_cell(plan)
    connection.execute(
        "INSERT INTO entries VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (row["id"], row["conversation_id"], operation, "native_quote", original,
         "original", "{}", "source", "witness", plan, "projected"),
    )


@pytest.fixture
def timeline():
    connection = sqlite3.connect(":memory:")
    register_json_codec(connection)
    connection.executescript(common._SCHEMA)
    columns = ",".join('"' + column + '" ' + kind for column, kind in zip(common.COLUMNS, _TYPES))
    for table in ("primary_rows", "resource_rows", "bank_rows"):
        connection.execute("CREATE TABLE " + table + "(" + columns + ")")
        connection.execute("CREATE INDEX " + table + "_id ON " + table + "(id)")
        connection.execute("CREATE INDEX " + table + "_conv ON " + table + "(conversation_id)")
    connection.execute("CREATE TABLE shadows(id TEXT PRIMARY KEY)")
    expressions = [
        "CASE WHEN text='computed-number' THEN 1 ELSE text END AS text" if column == "text"
        else "CASE WHEN search_text='computed-number' THEN 1 ELSE search_text END AS search_text"
        if column == "search_text" else column for column in common.COLUMNS
    ]
    connection.execute(
        "CREATE TEMP VIEW input_rows AS SELECT " + ",".join(expressions)
        + " FROM primary_rows UNION ALL SELECT * FROM resource_rows"
    )
    connection.execute(
        "CREATE TEMP VIEW upstream AS SELECT b.* FROM input_rows b "
        "WHERE NOT EXISTS(SELECT 1 FROM shadows s WHERE s.id=b.id) "
        "UNION ALL SELECT * FROM bank_rows"
    )
    try:
        yield connection
    finally:
        connection.close()


def _legacy(connection):
    return dict(connection.execute(
        "SELECT e.id,CASE WHEN " + common._active_predicate("temp.upstream")
        + " THEN 1 ELSE 0 END FROM entries e ORDER BY e.id"
    ))


def _bounded(connection):
    bounded = selection.BoundedActiveSelection(connection, "temp.upstream")
    return {row["id"]: row["is_active"] for row in bounded.entries("main.entries")}


@pytest.mark.parametrize("column", common.COLUMNS)
def test_every_original_field_remains_part_of_exact_replacement_precedence(timeline, column):
    original = _row()
    changed = {**original, column: 2 if column in {"ordinal", "time_sort"} else "Different"}
    _insert(timeline, "primary_rows", changed)
    _entry(timeline, original, encoded=True)
    assert _legacy(timeline) == _bounded(timeline) == {"exact": 0}


@pytest.mark.parametrize("reverse", [False, True])
def test_same_id_observations_are_not_deduplicated_before_exact_match(timeline, reverse):
    original = _row()
    rows = [original, {**original, "sender_label": "Different upstream observation"}]
    if reverse:
        rows.reverse()
    _insert(timeline, "primary_rows", rows[0])
    _insert(timeline, "resource_rows", rows[1])
    _entry(timeline, original)
    assert _legacy(timeline) == _bounded(timeline) == {"exact": 1}
    assert timeline.execute("SELECT COUNT(*) FROM completion_selection_rows").fetchone() == (2,)


@pytest.mark.parametrize("field,upstream_value,original_value", [
    ("ordinal", 1, "1"), ("time_sort", 100, 100.0),
    ("time_sort", 100.0, "100"), ("sender_id", None, None),
    ("text", b"Retained body", "Retained body"), ("text", None, None),
    ("text", "computed-number", "1"), ("text", "computed-number", 1),
    ("search_text", "computed-number", "1"), ("search_text", "computed-number", 1),
    ("text", "001", 1), ("provenance", b'{"server_id":"900"}', '{"server_id":"900"}'),
])
def test_compound_computed_columns_keep_sqlite_affinity_and_storage_type(
    timeline, field, upstream_value, original_value
):
    original = _row(**{field: original_value})
    _insert(timeline, "primary_rows", {**original, field: upstream_value})
    _entry(timeline, original, encoded=True)
    expected = _legacy(timeline)
    assert _bounded(timeline) == expected


@pytest.mark.parametrize("upstream_server,planned_server,active", [
    (900, 900, 0), ("900", 900, 1), (900, "900", 1), ("900", "900", 0),
    (900.0, 900, 0), (18446744073709551615, 18446744073709551615, 0),
    (None, None, 1), (None, "900", 1), (True, 1, 0),
    ([1, 2], [1, 2], 0), ({"a": 1}, {"a": 1}, 0),
])
def test_append_membership_keeps_json_number_string_null_and_structured_values(
    timeline, upstream_server, planned_server, active
):
    _insert(timeline, "primary_rows", _row("upstream", provenance=json.dumps({"server_id": upstream_server})))
    _entry(timeline, _row("notice"), operation="append", server_id=planned_server, encoded=True)
    assert _legacy(timeline) == _bounded(timeline) == {"notice": active}
    columns = list(timeline.execute("PRAGMA table_info(completion_selection_members)"))
    assert [row[1] for row in columns] == ["id", "conversation_id", "server_id"]
    assert columns[-1][2] == ""


@pytest.mark.parametrize("same_conversation", [False, True])
def test_append_id_membership_stays_inside_exact_conversation(timeline, same_conversation):
    _insert(timeline, "primary_rows", _row("notice", conversation_id="group-a" if same_conversation else "group-b"))
    _entry(timeline, _row("notice"), operation="append", server_id=None)
    assert _legacy(timeline) == _bounded(timeline) == {"notice": int(not same_conversation)}


def test_notice_projection_keeps_only_matching_candidate_keys_and_indexed_scopes(timeline):
    for index in range(100):
        _insert(timeline, "primary_rows", _row(
            "upstream-" + str(index),
            provenance=json.dumps({"server_id": "wanted" if index == 0 else "other-" + str(index), "body": "x" * 2000}),
        ))
    _insert(timeline, "resource_rows", _row("notice", provenance='{"server_id":null}'))
    _entry(timeline, _row("notice"), operation="append", server_id="wanted")
    expected = _legacy(timeline)
    bounded = selection.BoundedActiveSelection(timeline, "temp.upstream")
    assert {row["id"]: row["is_active"] for row in bounded.entries("main.entries")} == expected == {"notice": 0}
    assert timeline.execute("SELECT COUNT(*) FROM completion_selection_members").fetchone() == (2,)
    plan = [row[3] for row in timeline.execute(
        "EXPLAIN QUERY PLAN " + bounded._membership_query(1), ("group-a",)
    )]
    for table in ("primary_rows", "resource_rows", "bank_rows"):
        assert any("SEARCH " in line and table + "_conv" in line for line in plan), plan
    assert not any("AUTOMATIC COVERING INDEX" in line for line in plan), plan


def test_duplicate_notice_observations_and_missing_json_keys_keep_existence_semantics(timeline):
    _insert(timeline, "primary_rows", _row("notice", provenance="{}"))
    _insert(timeline, "resource_rows", _row("notice", provenance='{"server_id":null}'))
    _entry(timeline, _row("notice"), operation="append", server_id=None)
    assert _legacy(timeline) == _bounded(timeline) == {"notice": 0}
    assert timeline.execute("SELECT COUNT(*) FROM completion_selection_members").fetchone() == (2,)


def test_shadow_precedence_and_novel_append_are_unchanged(timeline):
    original = _row()
    _insert(timeline, "primary_rows", original)
    timeline.execute("INSERT INTO shadows VALUES(?)", (original["id"],))
    _insert(timeline, "bank_rows", {**original, "text": "Newer verified bank body"})
    _entry(timeline, original)
    _entry(timeline, _row("novel"), operation="append", server_id="unseen")
    assert _legacy(timeline) == _bounded(timeline) == {"exact": 0, "novel": 1}


def test_reviewed_original_absent_from_entries_uses_same_bounded_precedence(timeline):
    original = _row("reviewed-prior")
    _insert(timeline, "primary_rows", original)
    bounded = selection.BoundedActiveSelection(timeline, "temp.upstream")
    assert not timeline.execute("SELECT 1 FROM entries").fetchone()
    assert bounded.original_is_active(original) == 1
    timeline.execute("UPDATE primary_rows SET sender_label='Newer verified author'")
    assert bounded.original_is_active(original) == 0


def test_candidate_batches_push_id_filters_into_each_indexed_union_arm(timeline, monkeypatch):
    monkeypatch.setattr(selection, "ENTRY_BATCH_SIZE", 2)
    for index in range(7):
        row = _row(f"entry-{index}")
        _insert(timeline, "primary_rows", row)
        _entry(timeline, row)
    observed = []
    bounded = selection.BoundedActiveSelection(timeline, "temp.upstream")
    scope = bounded._scope

    def measure(*args, **kwargs):
        scope(*args, **kwargs)
        observed.append(timeline.execute("SELECT COUNT(*) FROM completion_selection_batch").fetchone()[0])

    monkeypatch.setattr(bounded, "_scope", measure)
    records = list(bounded.entries("main.entries"))
    assert [row["id"] for row in records] == [f"entry-{index}" for index in range(7)]
    assert all(row["is_active"] == 1 for row in records)
    assert observed == [2, 2, 2, 1]
    plan = [row[3] for row in timeline.execute(
        "EXPLAIN QUERY PLAN " + bounded._replacement_query(2), ("entry-1", "entry-2")
    )]
    for table in ("primary_rows", "resource_rows", "bank_rows"):
        assert any("SEARCH " in line and table + "_id" in line for line in plan), plan
    assert not any("AUTOMATIC COVERING INDEX" in line for line in plan), plan
    assert not any("SCAN " + table in line for line in plan for table in ("primary_rows", "resource_rows", "bank_rows")), plan


def test_operation_batches_limit_notice_scans_and_merge_the_original_id_order(timeline, monkeypatch):
    monkeypatch.setattr(selection, "ENTRY_BATCH_SIZE", 2)
    for index in range(10):
        row = _row(f"entry-{index}")
        if index % 2:
            _entry(timeline, row, operation="append", server_id="novel-" + str(index))
        else:
            _entry(timeline, row)
            _insert(timeline, "primary_rows", row)
    expected = _legacy(timeline)
    bounded = selection.BoundedActiveSelection(timeline, "temp.upstream")
    calls = []
    membership = bounded._membership_query

    def count_scans(size):
        calls.append(size)
        return membership(size)

    monkeypatch.setattr(bounded, "_membership_query", count_scans)
    rows = list(bounded.entries("main.entries"))
    assert [row["id"] for row in rows] == list(expected)
    assert {row["id"]: row["is_active"] for row in rows} == expected
    assert calls == [1, 1, 1]
