"""Native packet clock links survive projection, export, and reversible cache upgrades.

Code version: v1.0.0-codex.0
"""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import json
import sqlite3

from app.core import wechat_contact_analysis as analysis
from app.core.chat_history_browser import query_chat_history
from app.core.wechat_forwarded import apply_forwarded_clock_evidence, project_html_forwarded
from app.core.wechat_forwarded_clocks import WechatForwardedClocks
from tests import test_wechat_forwarded_clocks as fixtures
from tests.test_wechat_forwarded_original_precedence import CID


native_html_store = fixtures.native_html_store
statement_store = fixtures.statement_store
forwarded_origin_store = fixtures.forwarded_origin_store
pinned_clock_store = fixtures.pinned_clock_store


def _record(fixture):
    row = fixtures._rows(fixture)[0]
    proof = json.loads(row["provenance"])
    return project_html_forwarded(
        proof,
        parent_id=row["id"],
        account_id=fixture.owner,
        text=row["text"],
        components=proof["forwarded_components"],
        source_proof_status="source_proof_verified",
    )


def test_model_uses_native_child_clock_and_preserves_html_parent_and_raw_clock(pinned_clock_store):
    fixture = pinned_clock_store
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        parent = dict(connection.execute("SELECT * FROM messages WHERE id=?", (fixture.html_parent,)).fetchone())
        stamp = datetime.fromisoformat(parent["timestamp_value"]) + timedelta(seconds=10)
        proof = json.loads(parent["provenance"])
        proof["source_time"] = stamp.replace(tzinfo=None).isoformat(" ")
        connection.execute(
            "UPDATE messages SET timestamp_value=?,time_sort=?,provenance=? WHERE id=?",
            (stamp.replace(tzinfo=None).isoformat(), stamp.timestamp(), json.dumps(proof), fixture.html_parent),
        )
        session, account = connection.execute(
            "SELECT c.id,c.account_key FROM conversations c JOIN messages m ON m.conversation_id=c.id WHERE m.id=?",
            (fixture.html_parent,),
        ).fetchone()
    fixtures._pin(fixture)
    preserved = fixture.history.read_bytes(), fixture.registry.read_bytes()
    page = query_chat_history(fixture.store, source="wechat", account=account, session=session)
    parent = next(item for item in page.items if item.stable_id == fixture.html_parent)
    first = parent.messaging["forwarded_record"]["items"][0]
    assert first.last_seen_at == "2020-08-12T13:46:00"
    assert first.messaging["time_basis"].startswith("Recovered native packet time;")
    assert first.messaging["provenance"]["source_time"] == "1970-01-01 08:00:00"
    assert first.messaging["provenance"]["native_forwarded_clock_recovery"]["native_message_id"] == fixture.native_parent
    assert parent.last_seen_at != first.last_seen_at
    assert parent.last_seen_at == stamp.replace(tzinfo=None).isoformat()
    assert (fixture.history.read_bytes(), fixture.registry.read_bytes()) == preserved


def test_completed_tree_cannot_apply_a_clock_to_a_different_child_at_the_same_position(pinned_clock_store):
    fixture = pinned_clock_store
    recovered = WechatForwardedClocks(fixture.store).project(fixtures._rows(fixture))[fixture.html_parent]
    record = _record(fixture)
    first = record["items"][0]
    inserted = replace(first, stable_id="inserted-completion-child", content_text="Another source's own words", messaging=deepcopy(first.messaging))
    inserted.messaging["provenance"]["embedded_html_forwarded_component"].update(
        text=inserted.content_text, export_msgid="413_" + "f" * 32,
    )
    record["items"] = (inserted, *record["items"])
    apply_forwarded_clock_evidence(record, recovered)
    assert not record["items"][0].last_seen_at
    assert not record["items"][1].last_seen_at
    assert "native_forwarded_clock_recovery" not in record["items"][0].messaging["provenance"]


def test_existing_cache_and_csv_recover_then_revoke_only_derived_html_child_times(pinned_clock_store, monkeypatch):
    fixture = pinned_clock_store
    link = fixture.clock_registry.read_bytes()
    fixture.clock_registry.write_text(json.dumps({"schema_version": 1, "links": []}))
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    initial = analysis.query_contact_analysis(fixture.store, CID)
    path = analysis.contact_analysis_cache_path(fixture.store, CID)
    identifier = fixture.html_parent + "_forwarded_" + str(fixture.html_paths[0][0])
    with sqlite3.connect(path) as cache:
        assert cache.execute("SELECT has_time,source_clock FROM refs WHERE id=?", (identifier,)).fetchone() == (0, "1970-01-01 08:00:00")

    def no_rescan(*_args, **_kwargs):
        raise AssertionError("Unchanged accepted source references must be reused.")

    monkeypatch.setattr(analysis, "_candidate_native_batches", no_rescan)
    monkeypatch.setattr(analysis, "_candidate_batches", no_rescan)
    fixture.clock_registry.write_bytes(link)
    recovered = analysis.query_contact_analysis(fixture.store, CID)
    assert recovered["source_fingerprint"] != initial["source_fingerprint"]
    exported = list(analysis.export_contact_analysis_items(fixture.store, CID, source_fingerprint=recovered["source_fingerprint"], source_occurrences=True))
    item = next(item for item in exported if item["id"] == identifier)
    assert item["timestamp_value"] and not item["timestamp_value"].startswith("1970")
    assert item["source_clock"] == "1970-01-01 08:00:00"
    assert item["provenance"]["native_forwarded_clock_recovery"]["native_message_id"] == fixture.native_parent

    fixture.clock_registry.write_text(json.dumps({"schema_version": 1, "links": []}))
    revoked = analysis.query_contact_analysis(fixture.store, CID)
    assert revoked["source_fingerprint"] != recovered["source_fingerprint"]
    with sqlite3.connect(path) as cache:
        assert cache.execute("SELECT has_time,timestamp_value,source_clock FROM refs WHERE id=?", (identifier,)).fetchone() == (0, "", "1970-01-01 08:00:00")
        assert cache.execute("SELECT count(*) FROM forwarded_clock_evidence").fetchone()[0] == 0
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())
