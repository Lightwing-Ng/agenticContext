"""Native author recovery augments existing statements without rewriting sources.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from contextlib import closing
import csv
from io import StringIO
import json
import sqlite3

import pytest

from app.core import wechat_contact_analysis as analysis
from app.web.wechat_contact_csv import contact_csv_chunks
from tests import test_wechat_forwarded_authors as fixtures


native_html_store = fixtures.native_html_store
statement_store = fixtures.statement_store
forwarded_origin_store = fixtures.forwarded_origin_store
native_author_store = fixtures.native_author_store
CID = fixtures.CID
TARGET = fixtures.TARGET


def _parts(fixture, identifier):
    with closing(sqlite3.connect(fixture.history)) as connection, connection:
        raw = connection.execute(
            "SELECT provenance FROM messages WHERE id=?", (identifier,)
        ).fetchone()[0]
    return json.loads(raw)["text_components"]


def _members(fixture):
    path = analysis.contact_analysis_cache_path(fixture.store, CID)
    with closing(sqlite3.connect(path)) as connection, connection:
        return dict(connection.execute(
            "SELECT reference_id,statement_id FROM statement_members"
        ))


def _export(fixture, page, **options):
    return list(analysis.export_contact_analysis_items(
        fixture.store,
        CID,
        source_fingerprint=page["source_fingerprint"],
        source_occurrences=True,
        **options,
    ))


def _disable_source_rescans(monkeypatch):
    def no_rescan(*_args, **_kwargs):
        raise AssertionError("A complete unchanged contact cache must be reused.")

    for name in (
        "_candidate_batches", "_candidate_native_batches", "_candidate_forwarded_batches"
    ):
        monkeypatch.setattr(analysis, name, no_rescan)


def _without_links(fixture):
    fixture.author_registry.write_text(json.dumps({"schema_version": 1, "links": []}))


def test_existing_cache_recovers_sources_search_and_csv_without_new_statements(
    native_author_store, monkeypatch
):
    fixture = native_author_store
    pinned = fixture.author_registry.read_bytes()
    _without_links(fixture)
    source_bytes = fixture.history.read_bytes(), fixture.registry.read_bytes()
    initial = analysis.query_contact_analysis(fixture.store, CID, page_size=200)
    baseline_members = _members(fixture)
    donor_parts = _parts(fixture, fixture.native_parent)
    expected = {
        fixture.target_parent + "_forwarded_" + str(number): (number, part)
        for number, part in enumerate(donor_parts, 1)
        if part["source_sender_id"] == TARGET
    }
    assert expected
    assert not expected.keys() & baseline_members.keys()

    _disable_source_rescans(monkeypatch)
    fixture.author_registry.write_bytes(pinned)
    recovered = analysis.query_contact_analysis(fixture.store, CID, page_size=200)
    assert recovered["total_count"] == initial["total_count"]
    assert recovered["observation_count"] == initial["observation_count"] + len(expected)
    assert recovered["source_fingerprint"] != initial["source_fingerprint"]
    members = _members(fixture)
    assert members.keys() == baseline_members.keys() | expected.keys()
    assert set(members.values()) == set(baseline_members.values())
    assert all(members[key] == value for key, value in baseline_members.items())

    exported = {item["id"]: item for item in _export(fixture, recovered)}
    for identifier, (number, part) in expected.items():
        native_reference = fixture.native_parent + "_forwarded_" + str(number)
        assert members[identifier] == baseline_members[native_reference]
        item = exported[identifier]
        assert item["sender_id"] == TARGET
        assert item["content_text"] == part["text"]
        assert item["source_clock"] == part["source_timestamp"]
        assert item["timestamp_value"] == exported[native_reference]["timestamp_value"]
        assert item["provenance"]["identity_basis"] == (
            "verified_complete_native_transcript_copy"
        )
        evidence = item["provenance"]["native_forwarded_author_recovery"]
        assert evidence["native_reference_id"] == native_reference
        assert evidence["target_source_sender_id"] == ""
        assert evidence["source_data_id"] == part["source_data_id"]
        assert "recovered_native_forwarded_author" in item["quality_flags"]
        grouped = next(
            row for row in recovered["items"]
            if row["statement_id"] == members[identifier]
        )
        assert identifier in {origin["id"] for origin in grouped["observations"]}
        searched = analysis.query_contact_analysis(fixture.store, CID, query=part["text"])
        matching_sources = _export(fixture, searched, query=part["text"])
        assert identifier in {row["id"] for row in matching_sources}

    unique = analysis.export_contact_analysis_items(
        fixture.store, CID, source_fingerprint=recovered["source_fingerprint"]
    )
    first = next(unique, None)
    csv_rows = list(csv.reader(StringIO("".join(contact_csv_chunks(unique, first=first)))))
    assert csv_rows[0] == ["\ufeffTime", "Statement"]
    assert all(part["text"] in {row[1] for row in csv_rows[1:]} for _, part in expected.values())
    assert len(csv_rows[1:]) == len({tuple(row) for row in csv_rows[1:]})
    assert source_bytes == (fixture.history.read_bytes(), fixture.registry.read_bytes())


@pytest.mark.parametrize("revocation", ["removed_registry", "empty_registry", "invalid_pin"])
def test_revocation_removes_only_recovered_members_and_retains_native_sources(
    native_author_store, monkeypatch, revocation
):
    fixture = native_author_store
    pinned = fixture.author_registry.read_bytes()
    _without_links(fixture)
    initial = analysis.query_contact_analysis(fixture.store, CID)
    baseline_members = _members(fixture)
    source_bytes = fixture.history.read_bytes(), fixture.registry.read_bytes()
    _disable_source_rescans(monkeypatch)
    fixture.author_registry.write_bytes(pinned)
    recovered = analysis.query_contact_analysis(fixture.store, CID)
    assert recovered["observation_count"] > initial["observation_count"]
    if revocation == "removed_registry":
        fixture.author_registry.unlink()
    elif revocation == "empty_registry":
        _without_links(fixture)
    else:
        registry = json.loads(pinned)
        registry["links"][0]["target_provenance_sha256"] = "f" * 64
        fixture.author_registry.write_text(json.dumps(registry))

    revoked = analysis.query_contact_analysis(fixture.store, CID)
    assert revoked["total_count"] == initial["total_count"]
    assert revoked["observation_count"] == initial["observation_count"]
    assert _members(fixture) == baseline_members
    exported = _export(fixture, revoked)
    assert not any(item["parent_message_id"] == fixture.target_parent for item in exported)
    assert any(item["parent_message_id"] == fixture.native_parent for item in exported)
    assert source_bytes == (fixture.history.read_bytes(), fixture.registry.read_bytes())
    assert analysis.query_contact_analysis(fixture.store, CID)["observation_count"] == (
        initial["observation_count"]
    )


def test_mixed_literal_and_missing_authors_keep_literal_source_identity(
    native_author_store, monkeypatch
):
    fixture = native_author_store
    donor_parts = _parts(fixture, fixture.native_parent)
    number = next(
        index for index, part in enumerate(donor_parts, 1)
        if part["source_sender_id"] == TARGET
    )

    def restore_literal(proof):
        proof["text_components"][number - 1].update(
            source_sender_id=TARGET, source_sender_label="A literal source remark"
        )

    fixtures._mutate(fixture, target=True, callback=restore_literal)
    fixtures._pin(fixture)
    pinned = fixture.author_registry.read_bytes()
    _without_links(fixture)
    source_bytes = fixture.history.read_bytes(), fixture.registry.read_bytes()
    initial = analysis.query_contact_analysis(fixture.store, CID)
    baseline_members = _members(fixture)
    literal_id = fixture.target_parent + "_forwarded_" + str(number)
    assert literal_id in baseline_members
    _disable_source_rescans(monkeypatch)
    fixture.author_registry.write_bytes(pinned)
    recovered = analysis.query_contact_analysis(fixture.store, CID)
    expected_missing = sum(part["source_sender_id"] == TARGET for part in donor_parts) - 1
    assert recovered["total_count"] == initial["total_count"]
    assert recovered["observation_count"] == initial["observation_count"] + expected_missing
    literal = next(item for item in _export(fixture, recovered) if item["id"] == literal_id)
    assert literal["sender_id"] == TARGET
    assert literal["sender_label"] == "A literal source remark"
    assert literal["provenance"]["identity_basis"] == "literal_native_envelope_sender_id"
    assert "native_forwarded_author_recovery" not in literal["provenance"]
    assert _members(fixture)[literal_id] == baseline_members[literal_id]
    assert source_bytes == (fixture.history.read_bytes(), fixture.registry.read_bytes())
