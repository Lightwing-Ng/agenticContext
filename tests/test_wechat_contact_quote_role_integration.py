"""Recovered native quote roles reach search, exports, navigation, and cache rebuilds.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core import wechat_mcp_archive as mcp_archive
from tests import test_wechat_contact_native_embedded as fixtures


native_activity_store = fixtures.native_activity_store
ROOM = "444111222@chatroom"


def _reverse_quote(fixture):
    parent = fixture.parents["native-reply"]
    with sqlite3.connect(fixture.history) as source:
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


def test_reversed_quote_reaches_contact_search_exports_and_source_anchor(
    native_activity_store,
):
    fixture = native_activity_store
    reference_id = _reverse_quote(fixture)
    before = _preserved(fixture)
    page = analysis.query_contact_analysis(
        fixture.store, fixture.target_id, query=fixtures.QUOTED
    )
    assert page["status"] == "ready" and page["total_count"] == 2
    quote = next(item for item in page["items"] if item["id"] == reference_id)
    assert quote["sender_id"] == fixtures.TARGET
    assert quote["content_text"] == fixtures.QUOTED
    assert quote["original_peer_id"] == ROOM
    assert quote["original_conversation_unknown"] is False
    assert quote["timestamp_value"] == ""
    assert quote["provenance"]["source_time"] == "0"
    assert quote["anchor_id"] == "wechat_message_" + fixture.parents["native-reply"]

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
    location = analysis.message_source_location(fixture.store, reference_id)
    assert location is not None
    assert location["anchor_id"] == quote["anchor_id"]
    assert location["conversation_id"] == quote["conversation_id"]
    replying = analysis.query_contact_analysis(
        fixture.store, fixture.other_id, query=fixtures.QUOTED
    )
    assert replying["total_count"] == 0
    assert _preserved(fixture) == before


def test_mcp_quote_keyword_search_pages_original_authors_without_source_writes(
    native_activity_store,
):
    fixture = native_activity_store
    reference_id = _reverse_quote(fixture)
    original = _preserved(fixture)
    target = analysis.query_contact_analysis(
        fixture.store, fixture.target_id, query=fixtures.QUOTED
    )
    other = analysis.query_contact_analysis(
        fixture.store, fixture.other_id, query=fixtures.QUOTED
    )
    assert target["status"] == other["status"] == "ready"
    assert target["total_count"] == 2
    assert other["total_count"] == 0
    expected = {item["id"] for item in target["items"]}
    quote = next(item for item in target["items"] if item["id"] == reference_id)
    assert quote["sender_id"] == fixtures.TARGET
    assert quote["original_peer_id"] == ROOM
    caches = [
        analysis.contact_analysis_cache_path(fixture.store, identifier)
        for identifier in (fixture.target_id, fixture.other_id)
    ]
    published = tuple(path.read_bytes() for path in caches)
    observed = []
    for page in (1, 2):
        result = mcp_archive.search_messages(
            fixture.store,
            contact_id=fixture.target_id,
            query=fixtures.QUOTED,
            sort="oldest",
            page=page,
            page_size=1,
        )
        assert result["ok"] is True
        assert result["total_count"] == result["total_pages"] == 2
        assert result["page"] == page
        assert len(result["messages"]) == 1
        observed.extend(result["messages"])
    assert {item["id"] for item in observed} == expected
    assert len({item["id"] for item in observed}) == 2
    assert all(item["text"] == fixtures.QUOTED for item in observed)
    assert all(item["sender_id"] == fixtures.TARGET for item in observed)
    assert all(item["scope_contact_id"] == fixture.target_id for item in observed)
    quoted = next(item for item in observed if item["id"] == reference_id)
    assert quoted["sender_id"] not in {ROOM, fixtures.OTHER}
    assert quoted["timestamp_value"] == ""
    replying = mcp_archive.search_messages(
        fixture.store,
        contact_id=fixture.other_id,
        query=fixtures.QUOTED,
        page_size=1,
    )
    assert replying["ok"] is True
    assert replying["total_count"] == 0 and replying["messages"] == []
    assert _preserved(fixture) == original
    assert tuple(path.read_bytes() for path in caches) == published


def test_embedded_projection_version_changes_source_fingerprint(
    native_activity_store, monkeypatch
):
    fixture = native_activity_store
    original = analysis._sources(fixture.store)
    before = _preserved(fixture)
    monkeypatch.setattr(analysis, "NATIVE_EMBEDDED_VERSION", "v999.0.0-test.0")
    updated = analysis._sources(fixture.store)
    assert original[:2] == updated[:2]
    assert original[2] != updated[2]
    assert _preserved(fixture) == before


@pytest.mark.parametrize("recorded_version", [None, "v1.1.1-codex.0"])
def test_incomplete_same_inputs_cache_rebuilds_missing_quote_membership(
    native_activity_store, monkeypatch, recorded_version
):
    fixture = native_activity_store
    reference_id = _reverse_quote(fixture)
    before = _preserved(fixture)
    page = analysis.query_contact_analysis(fixture.store, fixture.target_id)
    cache = analysis.contact_analysis_cache_path(fixture.store, fixture.target_id)
    with sqlite3.connect(cache) as retained:
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
    assert reference_id in {item["id"] for item in repaired["items"]}
    assert repaired["observation_count"] == page["observation_count"]
    with sqlite3.connect(cache) as rebuilt:
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
        with sqlite3.connect(cache) as retained:
            retained.executemany(
                "UPDATE info SET value=? WHERE key=?",
                [(version, "version"), (old_fingerprint, "fingerprint")],
            )
    with sqlite3.connect(cache) as retained:
        if recorded_version is None:
            retained.execute("DELETE FROM info WHERE key='native_embedded_version'")
        else:
            retained.execute(
                "UPDATE info SET value=? WHERE key='native_embedded_version'",
                (recorded_version,),
            )
    with sqlite3.connect(":memory:") as output:
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
    with sqlite3.connect(cache) as retained:
        retained.execute("UPDATE info SET value='stale-grouping' WHERE key='fingerprint'")

    def no_rescan(*args, **kwargs):
        raise AssertionError("Matching source and author rules should retain accepted references.")

    monkeypatch.setattr(analysis, "_candidate_native_batches", no_rescan)
    page = analysis.query_contact_analysis(
        fixture.store, fixture.target_id, query=fixtures.QUOTED
    )
    assert page["total_count"] == 2
    assert reference_id in {item["id"] for item in page["items"]}
    assert _preserved(fixture) == before


def test_reversed_quote_uses_proven_room_when_linking_original_native_event(
    native_activity_store,
):
    fixture = native_activity_store
    reference_id = _reverse_quote(fixture)
    primary = fixture.parents["target-primary"]
    with sqlite3.connect(fixture.history) as source:
        source.execute(
            "UPDATE messages SET text=?,search_text=? WHERE id=?",
            (fixtures.QUOTED, fixtures.QUOTED.casefold(), primary),
        )
    before = _preserved(fixture)
    page = analysis.query_contact_analysis(
        fixture.store, fixture.target_id, query=fixtures.QUOTED
    )
    assert page["total_count"] == 1
    event = page["items"][0]
    assert event["id"] == primary and event["observation_count"] == 3
    observations = list(
        analysis.export_contact_analysis_items(
            fixture.store,
            fixture.target_id,
            query=fixtures.QUOTED,
            source_fingerprint=page["source_fingerprint"],
            source_occurrences=True,
        )
    )
    quote = next(item for item in observations if item["id"] == reference_id)
    assert quote["original_peer_id"] == ROOM
    assert quote["timestamp_value"] == ""
    assert {item["statement_id"] for item in observations} == {event["statement_id"]}
    assert _preserved(fixture) == before
