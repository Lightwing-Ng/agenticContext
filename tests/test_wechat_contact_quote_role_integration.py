"""Ambiguous native quote roles stay out of contact claims without original proof.

Code version: v1.1.1-codex.0
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import sqlite3

import pytest

from app.core import wechat_contact_analysis as analysis
from tests import test_wechat_contact_native_embedded as fixtures


native_activity_store = fixtures.native_activity_store
ROOM = "444111222@chatroom"


def _reverse_quote(fixture):
    parent = fixture.parents["native-reply"]
    with closing(sqlite3.connect(fixture.history)) as source, source:
        raw = source.execute(
            "SELECT provenance FROM messages WHERE id=?", (parent,)
        ).fetchone()[0]
        proof = json.loads(raw)
        for part in (proof["text_components"][1], proof["quoted_reference"]):
            part["source_sender_id"] = ROOM
            part["source_chat_id"] = fixtures.TARGET
        source.execute(
            "UPDATE messages SET provenance=? WHERE id=?", (json.dumps(proof), parent)
        )
    return parent + "_quoted_2"


def _preserved(fixture):
    return fixture.history.read_bytes(), fixture.registry.read_bytes()


def _assert_unverified_quote(fixture, reference_id):
    cache = analysis.contact_analysis_cache_path(fixture.store, fixture.target_id)
    with closing(sqlite3.connect(cache)) as connection, connection:
        assert connection.execute(
            "SELECT id FROM refs WHERE id=?", (reference_id,)
        ).fetchone() is None
        proof = connection.execute(
            "SELECT status,quoted_body_sha256,reference_before_json "
            "FROM quoted_clock_evidence WHERE reference_id=?", (reference_id,),
        ).fetchone()
    assert proof is not None and proof[0] == "unverified_author"
    assert proof[1] == hashlib.sha256(fixtures.QUOTED.encode()).hexdigest()
    preimage = json.loads(proof[2])
    assert preimage["id"] == reference_id
    assert preimage["parent_message_id"] == fixture.parents["native-reply"]
    return preimage


def test_reversed_quote_without_original_proof_is_not_a_contact_claim_but_keeps_source_anchor(
    native_activity_store,
):
    fixture = native_activity_store
    reference_id = _reverse_quote(fixture)
    before = _preserved(fixture)
    page = analysis.query_contact_analysis(
        fixture.store, fixture.target_id, query=fixtures.QUOTED
    )
    assert page["status"] == "ready" and page["total_count"] == 1
    assert reference_id not in {item["id"] for item in page["items"]}
    preimage = _assert_unverified_quote(fixture, reference_id)
    assert preimage["source_clock"] == "0"

    for raw_sources in (False, True):
        exported = list(
            analysis.export_contact_analysis_items(
                fixture.store,
                fixture.target_id,
                query=fixtures.QUOTED,
                source_fingerprint=page["source_fingerprint"],
                source_occurrences=raw_sources,
            )
        )
        assert {item["id"] for item in exported} == {
            item["id"] for item in page["items"]
        }
        assert all(item["sender_id"] == fixtures.TARGET for item in exported)
        assert all(item["content_text"] == fixtures.QUOTED for item in exported)
        assert reference_id not in {item["id"] for item in exported}
    location = analysis.message_source_location(fixture.store, reference_id)
    assert location is not None
    assert location["anchor_id"] == preimage["anchor_id"]
    assert location["conversation_id"] == preimage["conversation_id"]
    replying = analysis.query_contact_analysis(
        fixture.store, fixture.other_id, query=fixtures.QUOTED
    )
    assert replying["total_count"] == 0
    replies = analysis.query_contact_analysis(fixture.store, fixture.other_id)
    assert fixtures.REPLY in {item["content_text"] for item in replies["items"]}
    raw_replies = list(analysis.export_contact_analysis_items(
        fixture.store, fixture.other_id,
        source_fingerprint=replies["source_fingerprint"], source_occurrences=True,
    ))
    assert fixture.parents["native-reply"] in {item["id"] for item in raw_replies}
    assert _preserved(fixture) == before


def test_embedded_projection_version_changes_source_fingerprint(
    native_activity_store, monkeypatch
):
    fixture = native_activity_store
    original = analysis._source_state(fixture.store)
    before = _preserved(fixture)
    monkeypatch.setattr(analysis, "NATIVE_EMBEDDED_VERSION", "v999.0.0-test.0")
    updated = analysis._source_state(fixture.store)
    assert original[:2] == updated[:2]
    assert original[2] != updated[2]
    assert original[3] != updated[3]
    assert _preserved(fixture) == before


@pytest.mark.parametrize("recorded_version", [None, "v1.1.1-codex.0"])
def test_incomplete_same_inputs_cache_rechecks_but_does_not_promote_ambiguous_quote(
    native_activity_store, monkeypatch, recorded_version
):
    fixture = native_activity_store
    reference_id = _reverse_quote(fixture)
    before = _preserved(fixture)
    page = analysis.query_contact_analysis(fixture.store, fixture.target_id)
    cache = analysis.contact_analysis_cache_path(fixture.store, fixture.target_id)
    with closing(sqlite3.connect(cache)) as retained, retained:
        retained.execute("DELETE FROM statement_groups WHERE reference_id=?", (reference_id,))
        retained.execute("DELETE FROM statement_members WHERE reference_id=?", (reference_id,))
        retained.execute("DELETE FROM refs WHERE id=?", (reference_id,))
        if recorded_version is None:
            retained.execute("DELETE FROM info WHERE key='native_embedded_version'")
        else:
            retained.execute(
                "UPDATE info SET value=? WHERE key='native_embedded_version'",
                (recorded_version,),
            )
    assert analysis._cache_ready(cache, page["source_fingerprint"]) is False
    scanned = []
    original_candidates = analysis._candidate_native_batches

    def candidates(*args, **kwargs):
        scanned.append(True)
        yield from original_candidates(*args, **kwargs)

    monkeypatch.setattr(analysis, "_candidate_native_batches", candidates)
    repaired = analysis.query_contact_analysis(fixture.store, fixture.target_id)
    assert scanned == [True]
    assert repaired["status"] == "ready"
    assert reference_id not in {item["id"] for item in repaired["items"]}
    _assert_unverified_quote(fixture, reference_id)
    assert repaired["observation_count"] == page["observation_count"]
    with closing(sqlite3.connect(cache)) as rebuilt, rebuilt:
        assert (
            rebuilt.execute(
                "SELECT value FROM info WHERE key='native_embedded_version'"
            ).fetchone()[0]
            == analysis.NATIVE_EMBEDDED_VERSION
        )
    assert _preserved(fixture) == before


@pytest.mark.parametrize("copy_kind", ["legacy", "complete"])
@pytest.mark.parametrize("recorded_version", [None, "v1.1.1-codex.0"])
def test_cache_copy_paths_reject_incomplete_native_projection_versions(
    native_activity_store, copy_kind, recorded_version
):
    fixture = native_activity_store
    _reverse_quote(fixture)
    analysis.query_contact_analysis(fixture.store, fixture.target_id)
    cache = analysis.contact_analysis_cache_path(fixture.store, fixture.target_id)
    if copy_kind == "legacy":
        version = "v1.5.0-codex.0"
        identities = {
            str(path.resolve()): analysis._identity(path)
            for path in (fixture.history, fixture.registry)
        }
        old_fingerprint = hashlib.sha256(
            analysis._json([version, identities]).encode()
        ).hexdigest()
        with closing(sqlite3.connect(cache)) as retained, retained:
            retained.executemany(
                "UPDATE info SET value=? WHERE key=?",
                [(version, "version"), (old_fingerprint, "fingerprint")],
            )
    with closing(sqlite3.connect(cache)) as retained, retained:
        if recorded_version is None:
            retained.execute("DELETE FROM info WHERE key='native_embedded_version'")
        else:
            retained.execute(
                "UPDATE info SET value=? WHERE key='native_embedded_version'",
                (recorded_version,),
            )
    with closing(sqlite3.connect(":memory:")) as output, output:
        output.executescript(analysis._REFERENCE_SCHEMA)
        copier = (
            analysis._copy_legacy_references
            if copy_kind == "legacy"
            else analysis._copy_complete_event_index
        )
        assert copier(cache, output, fixture.history, fixture.registry) is False
        assert output.execute("SELECT COUNT(*) FROM refs").fetchone()[0] == 0


def test_matching_projection_cache_can_refresh_grouping_without_author_rescan(
    native_activity_store, monkeypatch
):
    fixture = native_activity_store
    reference_id = _reverse_quote(fixture)
    before = _preserved(fixture)
    analysis.query_contact_analysis(fixture.store, fixture.target_id)
    cache = analysis.contact_analysis_cache_path(fixture.store, fixture.target_id)
    with closing(sqlite3.connect(cache)) as retained, retained:
        retained.execute("UPDATE info SET value='stale-grouping' WHERE key='fingerprint'")

    def no_rescan(*args, **kwargs):
        raise AssertionError("Matching source and author rules should retain accepted references.")

    monkeypatch.setattr(analysis, "_candidate_native_batches", no_rescan)
    page = analysis.query_contact_analysis(
        fixture.store, fixture.target_id, query=fixtures.QUOTED
    )
    assert page["total_count"] == 1
    assert reference_id not in {item["id"] for item in page["items"]}
    _assert_unverified_quote(fixture, reference_id)
    assert _preserved(fixture) == before


def test_matching_body_and_server_without_exact_native_source_proof_cannot_keep_reverse_quote(
    native_activity_store,
):
    fixture = native_activity_store
    reference_id = _reverse_quote(fixture)
    primary = fixture.parents["target-primary"]
    with closing(sqlite3.connect(fixture.history)) as source, source:
        source.execute(
            "UPDATE messages SET text=?,search_text=? WHERE id=?",
            (fixtures.QUOTED, fixtures.QUOTED.casefold(), primary),
        )
        proof = json.loads(source.execute(
            "SELECT provenance FROM messages WHERE id=?", (primary,),
        ).fetchone()[0])
        assert proof["locator"]["table"] != "Chat_" + hashlib.md5(ROOM.encode()).hexdigest()
    before = _preserved(fixture)
    page = analysis.query_contact_analysis(
        fixture.store, fixture.target_id, query=fixtures.QUOTED
    )
    assert page["total_count"] == 1
    event = page["items"][0]
    assert event["id"] == primary and event["observation_count"] == 2
    observations = list(
        analysis.export_contact_analysis_items(
            fixture.store,
            fixture.target_id,
            query=fixtures.QUOTED,
            source_fingerprint=page["source_fingerprint"],
            source_occurrences=True,
        )
    )
    assert reference_id not in {item["id"] for item in observations}
    assert primary in {item["id"] for item in observations}
    _assert_unverified_quote(fixture, reference_id)
    assert {item["statement_id"] for item in observations} == {event["statement_id"]}
    assert _preserved(fixture) == before
