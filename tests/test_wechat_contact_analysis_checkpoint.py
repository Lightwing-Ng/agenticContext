"""Finish pinned reference scans when only derived event proofs change.

Code version: v1.0.1-codex.0
"""

from contextlib import closing

import hashlib
import json
import sqlite3

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core import wechat_conversation_events as events
from tests import test_wechat_contact_analysis as fixtures


contact_analysis_store = fixtures.contact_analysis_store
CID = fixtures.CID


def _refs(cache):
    with closing(sqlite3.connect(cache)) as connection, connection:
        return connection.execute("SELECT * FROM refs ORDER BY id").fetchall()


@pytest.mark.parametrize("entry", ["build", "query"])
def test_event_only_drift_keeps_complete_checkpoint_and_next_query_reuses_every_ref(
    contact_analysis_store, monkeypatch, entry
):
    f = contact_analysis_store
    known = {}
    monkeypatch.setattr(events, "cache_only_summaries", lambda _repository: dict(known))
    before = analysis._sources(f.store)[2]
    stable = analysis._source_state(f.store)[3]
    original = analysis._apply_conversation_events

    def event_arrived(*args):
        original(*args)
        known["wechat:" + hashlib.sha256(b"New session proof").hexdigest()] = 2

    monkeypatch.setattr(analysis, "_apply_conversation_events", event_arrived)
    source_bytes = f.history.read_bytes(), f.registry.read_bytes()
    if entry == "build":
        analysis._build_index(f.store, CID, before)
    else:
        with pytest.raises(
            analysis.ContactAnalysisSourceChanged, match="changed while reading"
        ):
            analysis.query_contact_analysis(f.store, CID)
    latest = analysis._sources(f.store)[2]
    assert before != latest
    assert stable == analysis._source_state(f.store)[3]
    cache = analysis.contact_analysis_cache_path(f.store, CID)
    checkpoint_refs = _refs(cache)
    assert checkpoint_refs
    with closing(sqlite3.connect(cache)) as connection, connection:
        info = dict(connection.execute("SELECT key,value FROM info"))
    assert info["fingerprint"] == before
    assert analysis._cache_ready(cache, before)
    assert not analysis._cache_ready(cache, latest)
    assert (
        analysis.contact_analysis_export_context(f.store, CID)["status"] == "indexing"
    )

    def no_rescan(*_args, **_kwargs):
        raise AssertionError(
            "A complete pinned checkpoint must not rescan source authors."
        )

    for name in (
        "_candidate_batches",
        "_candidate_forwarded_batches",
        "_candidate_native_batches",
        "_group_statements",
    ):
        monkeypatch.setattr(analysis, name, no_rescan)
    page = analysis.query_contact_analysis(f.store, CID)
    assert page["status"] == "ready"
    assert page["source_fingerprint"] == latest
    assert analysis._cache_ready(cache, latest)
    assert _refs(cache) == checkpoint_refs
    assert (f.history.read_bytes(), f.registry.read_bytes()) == source_bytes


@pytest.mark.parametrize(
    "change",
    [
        "history",
        "registry",
        "reviews",
        "index",
        "forwarded_clock",
        "forwarded_origins",
        "identified_packets",
        "event_version",
    ],
)
def test_real_source_review_or_rule_drift_never_publishes_a_checkpoint(
    contact_analysis_store, monkeypatch, change
):
    f = contact_analysis_store
    fingerprint = analysis._sources(f.store)[2]
    original = analysis._apply_conversation_events

    def changed(*args):
        original(*args)
        if change == "history":
            with sqlite3.connect(f.history) as connection:
                connection.execute(
                    "UPDATE messages SET text=text || ' changed' WHERE id=?",
                    (f.legacy["id"],),
                )
        elif change == "registry":
            with sqlite3.connect(f.registry) as connection:
                connection.execute(
                    "INSERT INTO contact_registry_info VALUES('checkpoint-drift','true')"
                )
        elif change == "reviews":
            (f.store / "messaging/wechat/statement_reviews.json").write_text(
                json.dumps({"schema_version": 1, "packet_reviews": []})
            )
        elif change == "event_version":
            monkeypatch.setattr(events, "VERSION", events.VERSION + ".drift")
        else:
            field = {
                "index": "INDEX_VERSION",
                "forwarded_clock": "FORWARDED_CLOCK_VERSION",
                "forwarded_origins": "FORWARDED_ORIGINS_VERSION",
                "identified_packets": "IDENTIFIED_PACKET_COPIES_VERSION",
            }[change]
            monkeypatch.setattr(analysis, field, getattr(analysis, field) + ".drift")

    monkeypatch.setattr(analysis, "_apply_conversation_events", changed)
    with pytest.raises(
        analysis.ContactAnalysisSourceChanged, match="changed while indexing"
    ):
        analysis._build_index(f.store, CID, fingerprint)
    cache = analysis.contact_analysis_cache_path(f.store, CID)
    assert not cache.exists()
    assert list(cache.parent.glob(".contact-analysis-*.sqlite3")) == []
