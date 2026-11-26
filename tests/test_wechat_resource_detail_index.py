"""Exercise indexed resource detail publication and source replay.

Code version: v1.0.1-codex.0
"""

import sqlite3

import pytest

from app.core import wechat_resource_detail_index as index
from app.core import wechat_resource_detail_repair as repair
from app.core import wechat_resource_details as details
from app.core.wechat_archive import WechatArchiveRepository, WechatTimelineProjectionError
from tests import test_wechat_resource_detail_repair as fixtures


mac_archive = fixtures.mac_archive
bank_archive = fixtures.bank_archive
source_binding = fixtures.source_binding
witness = fixtures.witness
fixture = fixtures.fixture
repair_fixture = fixtures.repair_fixture


def test_index_publishes_beyond_json_budget_without_rewriting_json(repair_fixture, tmp_path, monkeypatch):
    fixture = repair_fixture
    marker = fixtures.evidence._registry(fixture)
    original_json = marker.read_bytes()
    second, packet = fixtures._second(fixture)
    before = fixtures._source_bytes(fixture)
    monkeypatch.setattr(repair, "MAX_ENTRIES", 1)
    indexed_path = fixture.history.with_name(index.FILENAME)
    assert str(indexed_path) not in details.detail_input_identities(fixture.history)
    report = repair.stage_resource_detail_repair(
        fixture.root, tmp_path / "reviewed.sqlite3", [packet], indexed=True,
        expected_rows={second["id"]: details.row_sha256(second)},
        target_sessions={second["id"]: second["conversation_id"]},
    )
    assert report["total_entries"] == 2
    assert report["preserved_entries"] == report["counts"]["added"] == 1
    assert not indexed_path.exists()
    repair.publish_resource_detail_repair(fixture.root, report, rollback_path=tmp_path / "rollback.sqlite3")
    assert str(indexed_path) in details.detail_input_identities(fixture.history)
    records = list(index.indexed_entries(indexed_path, index.index_identity(indexed_path)))
    assert [record[0]["message_id"] for record in records] == [second["id"]]
    assert not list(index.indexed_entries(indexed_path, index.index_identity(indexed_path), conversation_id="other"))
    result = WechatArchiveRepository(fixture.root).query(
        session=second["conversation_id"], query="second preserved", persist_event_cache=False,
    )
    projected, = result["messages"]
    assert projected["id"] == second["id"]
    assert details.row_sha256(projected) == records[0][1]
    assert marker.read_bytes() == original_json
    assert fixtures._source_bytes(fixture) == before


def test_index_rejects_changed_candidate_and_changed_replayed_row(repair_fixture, tmp_path):
    fixture = repair_fixture
    candidate = tmp_path / "reviewed.sqlite3"
    report = repair.stage_resource_detail_repair(fixture.root, candidate, [fixture.packet], indexed=True)
    rollback = tmp_path / "rollback.sqlite3"
    with sqlite3.connect(candidate) as connection:
        connection.execute("UPDATE entries SET projected_sha256=?", ("0" * 64,))
    with pytest.raises(ValueError, match="changed"):
        repair.publish_resource_detail_repair(fixture.root, report, rollback_path=rollback)
    target = fixture.history.with_name(index.FILENAME)
    candidate.replace(target)
    with fixtures.evidence._connection(fixture) as connection:
        assert fixtures.evidence._attach(connection, fixture)[-1] == "invalid"


def test_index_can_stage_independently_verified_scopes(repair_fixture, tmp_path, monkeypatch):
    fixture = repair_fixture
    pending_id = "a" * 64
    pending_session = "wechat:" + "b" * 64
    timeline = WechatArchiveRepository._timeline_tables

    def scoped_timeline(repository, *args, **kwargs):
        if kwargs.get("conversation_id") == pending_session:
            raise WechatTimelineProjectionError("invalid")
        return timeline(repository, *args, **kwargs)

    monkeypatch.setattr(WechatArchiveRepository, "_timeline_tables", scoped_timeline)
    report = repair.stage_resource_detail_repair(
        fixture.root, tmp_path / "reviewed.sqlite3", [fixture.packet], indexed=True,
        expected_rows={
            fixture.row["id"]: details.row_sha256(fixture.row),
            pending_id: "c" * 64,
        },
        target_sessions={
            fixture.row["id"]: fixture.row["conversation_id"],
            pending_id: pending_session,
        },
    )

    assert report["counts"]["added"] == 1
    assert report["rejected"] == {"incomplete_upstream_scope": 1}
    assert report["incomplete_scopes"] == [
        {"session": pending_session, "status": "invalid", "rows": 1},
    ]


def test_index_cannot_replace_an_id_already_owned_by_the_json_registry(repair_fixture, tmp_path):
    fixture = repair_fixture
    entry = details.make_entry(fixture.row, fixture.conversation, fixture.packet)
    fixtures.evidence._registry(fixture, entry)
    projected = details.project_resource_details(fixture.row, fixture.conversation, fixtures.evidence._event(fixture))
    index.write_index(fixture.history.with_name(index.FILENAME), [(entry, details.row_sha256(projected))])
    with fixtures.evidence._connection(fixture) as connection:
        assert fixtures.evidence._attach(connection, fixture)[-1] == "invalid"


def test_superseded_exact_event_leaves_other_indexed_details_available(repair_fixture, tmp_path):
    fixture = repair_fixture
    second, packet = fixtures._second(fixture)
    report = repair.stage_resource_detail_repair(
        fixture.root, tmp_path / "reviewed.sqlite3", [fixture.packet, packet], indexed=True,
    )
    assert report["counts"]["added"] == 2
    repair.publish_resource_detail_repair(fixture.root, report, rollback_path=tmp_path / "rollback.sqlite3")
    with sqlite3.connect(fixture.history) as connection:
        connection.execute(
            "UPDATE messages SET search_text='New upstream content' WHERE id=?",
            (fixture.row["id"],),
        )
    result = WechatArchiveRepository(fixture.root).query(
        session=second["conversation_id"], query="second preserved", persist_event_cache=False,
    )
    assert result["resource_index_status"] == "ready"
    assert [row["id"] for row in result["messages"]] == [second["id"]]
