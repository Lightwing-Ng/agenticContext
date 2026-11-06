"""Native server identity survives conflicting or unavailable observation clocks.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from app.core import wechat_contact_analysis as analysis
from tests import test_wechat_contact_statement_groups as grouping


CID = grouping.CID
EPOCH = grouping.EPOCH
statement_store = grouping.statement_store


@pytest.mark.parametrize(("field", "value"), [
    ("timestamp_value", "2026-10-30T18:00:44+08:00"),
    ("timestamp_value", "2026-11-01T08:10:12+08:00"),
    ("timestamp_value", "2026-10-30T13:45:23"),
    ("timestamp_value", ""),
    ("timestamp_value", "1970-01-01T00:00:00+00:00"),
    ("timestamp_value", "Unrecognized source clock"),
    ("time_basis", "Unavailable source time"),
    ("quality_flags", '["possible_epoch_zero_placeholder"]'),
    ("unix_timestamp", EPOCH + 1),
    ("unix_timestamp", 0),
    ("unix_timestamp", None),
    ("unix_timestamp", "unavailable"),
    ("unix_timestamp", True),
])
def test_native_server_event_preserves_all_conflicting_clock_observations(
    statement_store, field, value
):
    fixture = statement_store
    with sqlite3.connect(fixture.history) as connection:
        if field == "unix_timestamp":
            provenance = json.loads(connection.execute(
                "SELECT provenance FROM messages WHERE id=?", (fixture.rows[1]["id"],)
            ).fetchone()[0])
            provenance[field] = value
            connection.execute(
                "UPDATE messages SET provenance=? WHERE id=?",
                (json.dumps(provenance), fixture.rows[1]["id"]),
            )
        else:
            connection.execute(
                f"UPDATE messages SET {field}=? WHERE id=?",
                (value, fixture.rows[1]["id"]),
            )
        observed_clocks = dict(connection.execute(
            "SELECT id,timestamp_value FROM messages WHERE sender_id=?",
            (grouping.TARGET,),
        ))
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()

    page = analysis.query_contact_analysis(
        fixture.store, CID, query="Synthetic", page_size=1, page=2
    )

    assert page["total_count"] == 1
    assert page["current_page"] == page["total_pages"] == 1
    assert page["observation_count"] == 2
    item, = page["items"]
    assert item["content_text"] == grouping.BODY
    assert item["observation_count"] == item["source_account_count"] == 2
    assert {observation["id"] for observation in item["observations"]} == (
        set(observed_clocks)
    )
    with sqlite3.connect(analysis.contact_analysis_cache_path(fixture.store, CID)) as cache:
        assert dict(cache.execute(
            "SELECT id,CASE WHEN source_clock!='' THEN source_clock "
            "ELSE timestamp_value END FROM refs"
        )) == (
            observed_clocks
        )
        assert cache.execute("SELECT COUNT(*) FROM statement_members").fetchone()[0] == 2
        assert cache.execute("SELECT COUNT(*) FROM statement_groups").fetchone()[0] == 1
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def test_same_account_native_snapshots_keep_both_clocks_in_one_event(statement_store):
    fixture = statement_store
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        original = dict(connection.execute(
            "SELECT * FROM messages WHERE id=?", (fixture.rows[0]["id"],)
        ).fetchone())
        copied = dict(original)
        copied["id"] = hashlib.sha256(b"same-account-other-native-snapshot").hexdigest()
        copied["timestamp_value"] = "2026-10-30T18:00:44+08:00"
        copied["timestamp_label"] = "30 Oct 2026 18:00:44"
        copied["time_sort"] = EPOCH + 15321
        provenance = json.loads(original["provenance"])
        provenance["unix_timestamp"] = EPOCH + 15321
        provenance["locator"] = {"database": "message_3.sqlite", "row_id": 31180}
        copied["provenance"] = json.dumps(provenance)
        columns = ",".join(copied)
        parameters = ",".join("?" for _ in copied)
        connection.execute(
            f"INSERT INTO messages({columns}) VALUES({parameters})", tuple(copied.values())
        )
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()

    page = analysis.query_contact_analysis(fixture.store, CID)

    assert page["total_count"] == 1
    assert page["observation_count"] == 3
    item, = page["items"]
    assert item["observation_count"] == 3
    assert item["source_account_count"] == 2
    same_account = [
        observation for observation in item["observations"]
        if observation["source_id"] == fixture.rows[0]["source_id"]
    ]
    assert {observation["id"] for observation in same_account} == {
        original["id"], copied["id"]
    }
    assert {observation["timestamp_value"] for observation in same_account} == {
        original["timestamp_value"], copied["timestamp_value"]
    }
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


@pytest.mark.parametrize("changes", [
    {"source_id": "html-unverified"},
    {"identity_status": "Source account unconfirmed"},
    {"container_account_key": "account:wxid_other_owner"},
    {"kind": "unclassified"},
    {"peer_id": "different-group@chatroom"},
])
def test_server_id_cannot_bypass_native_scope_verification(statement_store, changes):
    fixture = statement_store
    with analysis._readonly(fixture.history) as connection:
        row = dict(connection.execute(
            analysis._ROW_SELECT + "WHERE m.id=?", (fixture.rows[0]["id"],)
        ).fetchone())
    proven_thread = analysis._native_conversation_key(row)
    assert analysis._native_statement_key(row, CID, proven_thread)
    assert analysis._native_statement_key({**row, **changes}, CID, proven_thread) is None


def test_private_server_identity_never_crosses_participant_pairs(statement_store):
    fixture = statement_store
    with analysis._readonly(fixture.history) as connection:
        row = dict(connection.execute(
            analysis._ROW_SELECT + "WHERE m.id=?", (fixture.rows[0]["id"],)
        ).fetchone())

    def private(owner, peer, timestamp):
        return {
            **row,
            "account_id": owner,
            "account_key": "account:" + owner,
            "container_account_key": "account:" + owner,
            "source_id": "ios-" + hashlib.md5(owner.encode()).hexdigest(),
            "kind": "direct",
            "peer_id": peer,
            "timestamp_value": timestamp,
        }

    first = private("wxid_first", "wxid_second", "2026-10-30T13:45:23+08:00")
    reverse = private("wxid_second", "wxid_first", "2026-10-30T18:00:44+08:00")
    different = private("wxid_first", "wxid_third", first["timestamp_value"])

    def event(message):
        return analysis._native_statement_key(
            message, CID, analysis._native_conversation_key(message)
        )

    assert event(first) == event(reverse)
    assert event(first) != event(different)
