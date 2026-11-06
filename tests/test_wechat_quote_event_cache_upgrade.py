"""Quote proof upgrades refresh derived views without rescanning accepted sources.

Code version: v1.0.1-codex.0
"""

from contextlib import closing
import sqlite3

from app.core import wechat_contact_analysis as analysis
from app.core import wechat_conversation_events as events
from tests import test_wechat_contact_analysis as contact_fixtures
from tests import test_wechat_conversation_events as event_fixtures


event_store = event_fixtures.event_store
contact_analysis_store = contact_fixtures.contact_analysis_store
PREVIOUS_VERSION = "v1.1.0-codex.0"


def test_previous_quote_event_cache_is_recomputed(event_store, monkeypatch):
    fixture = event_store
    before = fixture.repository.path.read_bytes(), fixture.registry.read_bytes()
    current_version = events.VERSION
    assert current_version != PREVIOUS_VERSION
    with monkeypatch.context() as prior:
        prior.setattr(events, "VERSION", PREVIOUS_VERSION)
        connection, old = event_fixtures._run(fixture)
        connection.close()
    calls = []
    scan = events._scan

    def tracked_scan(*args):
        calls.append(args[2])
        return scan(*args)

    monkeypatch.setattr(events, "_scan", tracked_scan)
    connection, updated = event_fixtures._run(fixture)
    connection.close()
    assert calls == [event_fixtures.SESSION]
    assert old["version"] == PREVIOUS_VERSION
    assert updated["version"] == current_version
    assert updated["fingerprint"] != old["fingerprint"]
    assert updated["preferred_by_id"] == old["preferred_by_id"]
    assert updated["status"] == "ready"
    assert before == (
        fixture.repository.path.read_bytes(),
        fixture.registry.read_bytes(),
    )


def _accepted_rows(path):
    with closing(sqlite3.connect(path)) as connection:
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in ("refs", "statement_groups", "statement_members")
        }


def test_additive_quote_proof_reuses_complete_contact_index(
    contact_analysis_store, monkeypatch
):
    fixture = contact_analysis_store
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    current_version = events.VERSION
    with monkeypatch.context() as prior:
        prior.setattr(events, "VERSION", PREVIOUS_VERSION)
        original = analysis.query_contact_analysis(fixture.store, contact_fixtures.CID)
    assert original["status"] == "ready"
    cache = analysis.contact_analysis_cache_path(fixture.store, contact_fixtures.CID)
    accepted = _accepted_rows(cache)
    assert accepted["refs"]

    def no_rescan(*_args, **_kwargs):
        raise AssertionError(
            "An additive quote proof must retain accepted source rows."
        )

    for name in (
        "_candidate_batches",
        "_candidate_forwarded_batches",
        "_candidate_native_batches",
        "_group_statements",
    ):
        monkeypatch.setattr(analysis, name, no_rescan)
    updated = analysis.query_contact_analysis(fixture.store, contact_fixtures.CID)
    assert updated["status"] == "ready"
    assert updated["source_fingerprint"] != original["source_fingerprint"]
    assert _accepted_rows(cache) == accepted
    with closing(sqlite3.connect(cache)) as connection:
        info = dict(connection.execute("SELECT key,value FROM info"))
    assert info["event_proof_version"] == current_version
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())
