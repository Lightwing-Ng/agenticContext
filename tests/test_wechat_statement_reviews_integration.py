"""Reviewed packet preferences upgrade retained references without rescanning sources.

Code version: v1.0.2-codex.0
"""

from __future__ import annotations

import csv
from datetime import datetime
import hashlib
import io
import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import wechat_archive as archive
from app.core import wechat_contact_analysis as analysis
from app.core import wechat_statement_reviews as reviews
from app.core.wechat_contacts import contact_registry_path, initialize_contact_registry
from app.web.wechat_contact_csv import contact_csv_chunks
from tests import wechat_review_fixtures as packet_fixtures
from tests import test_wechat_statement_reviews as review_fixtures


overlap_store = packet_fixtures.overlap_store
reviewed_store = review_fixtures.reviewed_store
CONTACT = review_fixtures.CONTACT


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _snapshot(cache):
    with sqlite3.connect(cache) as connection:
        return {
            table: connection.execute(
                "SELECT * FROM " + table + " ORDER BY 1"
            ).fetchall()
            for table in ("refs", "statement_groups", "statement_members")
        }


@pytest.fixture
def retained_review_store(reviewed_store):
    """Materialize accepted references as a complete pre-review v1.8.0 cache."""
    fixture = reviewed_store
    store = fixture.root
    history = archive.wechat_archive_path(store)
    history.parent.mkdir(parents=True, exist_ok=True)
    flat_rows = [dict(row) for row in fixture.source.execute("SELECT * FROM messages")]
    conversations = {
        (row["source_id"], row["conversation_id"]): "wechat:"
        + _hash(row["source_id"] + ":" + row["conversation_id"])
        for row in flat_rows
    }
    with sqlite3.connect(history) as connection:
        connection.executescript(archive._SCHEMA)
        connection.execute(
            "CREATE TABLE source_snapshots(id INTEGER PRIMARY KEY,snapshot_id TEXT,"
            "source_id TEXT,metadata TEXT)"
        )
        for snapshot in fixture.source.execute("SELECT * FROM source_snapshots"):
            metadata = json.loads(snapshot["metadata"])
            metadata.update(
                source_id=snapshot["source_id"],
                source_label="iPad archive"
                if snapshot["source_id"].startswith("ios-")
                else "HTML export",
            )
            connection.execute(
                "INSERT INTO source_snapshots VALUES(?,?,?,?)",
                (
                    snapshot["id"],
                    snapshot["snapshot_id"],
                    snapshot["source_id"],
                    json.dumps(metadata),
                ),
            )
        for row in flat_rows:
            source = row["source_id"]
            native = source.startswith("ios-")
            connection.execute(
                "INSERT OR IGNORE INTO partitions VALUES(?,?,?,?,?,?,?,?)",
                (
                    source,
                    row["account_key"],
                    row["account_id"],
                    "Verified receiving account",
                    row["identity_status"],
                    "iPhone archive" if native else "HTML export",
                    "Synthetic source metadata",
                    "",
                ),
            )
            conversation = conversations[(source, row["conversation_id"])]
            title = "Reviewed receiving group" if row["kind"] == "group" else "Private"
            connection.execute(
                "INSERT OR IGNORE INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    conversation,
                    source,
                    row["account_key"],
                    row["peer_id"],
                    title,
                    row["kind"],
                    1,
                    row["time_sort"],
                    row["time_sort"],
                    row["timestamp_value"],
                    row["timestamp_value"],
                    "",
                ),
            )
            connection.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["id"],
                    conversation,
                    row["ordinal"],
                    row["sender_id"],
                    "Literal parent author",
                    row["direction"],
                    row["text"],
                    row["time_sort"],
                    row["timestamp_value"],
                    row["timestamp_value"],
                    "UTC+08:00" if native else "Source clock; timezone unknown",
                    row["provenance"],
                    "[]",
                    row["text"].casefold(),
                ),
            )
    registry = contact_registry_path(store)
    with sqlite3.connect(registry) as connection:
        initialize_contact_registry(connection)
        connection.execute(
            "INSERT INTO contacts VALUES(?,?,?,?,?,?)",
            (
                CONTACT,
                "wechat",
                "",
                review_fixtures.AUTHOR,
                "wechat_account",
                "verified",
            ),
        )
    cache = analysis.contact_analysis_cache_path(store, CONTACT)
    cache.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(cache) as connection:
        connection.executescript(analysis._REFERENCE_SCHEMA)
        for table in ("refs", "statement_groups", "statement_members"):
            for raw in fixture.output.execute("SELECT * FROM " + table):
                values = list(raw)
                if table == "refs":
                    old = dict(
                        zip(
                            (
                                item[1]
                                for item in fixture.output.execute(
                                    "PRAGMA table_info(refs)"
                                )
                            ),
                            raw,
                        )
                    )
                    values[4] = conversations[
                        (old["source_id"], old["conversation_id"])
                    ]
                    values[7] = datetime.fromisoformat(
                        old["timestamp_value"]
                    ).timestamp()
                elif table == "statement_groups":
                    values[2] = "pre-review-source-context-" + raw[0]
                connection.execute(
                    "INSERT INTO "
                    + table
                    + " VALUES("
                    + ",".join("?" for _ in values)
                    + ")",
                    values,
                )
        for native, html in (
            (fixture.private_refs, fixture.html_refs[0]),
            (fixture.group_refs, fixture.html_refs[1]),
        ):
            for preferred, alternate in zip(native, html):
                groups = dict(
                    connection.execute(
                        "SELECT reference_id,statement_id FROM statement_members "
                        "WHERE reference_id IN (?,?)",
                        (preferred, alternate),
                    )
                )
                connection.execute(
                    "UPDATE statement_members SET statement_id=? WHERE statement_id=?",
                    (groups[preferred], groups[alternate]),
                )
                connection.execute(
                    "DELETE FROM statement_groups WHERE id=?", (groups[alternate],)
                )
        identities = {
            str(path.resolve()): analysis._identity(path)
            for path in (history, registry)
        }
        connection.executemany(
            "INSERT INTO info VALUES(?,?)",
            [
                ("version", "v1.8.0-codex.0"),
                ("fingerprint", _hash(analysis._json(["v1.8.0-codex.0", identities]))),
                ("source_inputs", _hash(analysis._json(identities))),
            ],
        )
    return SimpleNamespace(
        store=store,
        history=history,
        registry=registry,
        cache=cache,
        review_path=fixture.path,
        review_bytes=fixture.path.read_bytes(),
        native_group=fixture.group,
        group_refs=fixture.group_refs,
        native_private=fixture.private,
        baseline=_snapshot(cache),
        preserved=(history.read_bytes(), registry.read_bytes()),
    )


def _prevent_author_rescan(monkeypatch):
    def no_rescan(*_args, **_kwargs):
        raise AssertionError(
            "A retained complete index must not rescan source authors."
        )

    for name in (
        "_candidate_batches",
        "_candidate_forwarded_batches",
        "_candidate_native_batches",
        "_group_statements",
    ):
        monkeypatch.setattr(analysis, name, no_rescan)


def test_review_upgrade_prefers_native_group_in_table_and_csv_without_rescan(
    retained_review_store, monkeypatch
):
    fixture = retained_review_store
    _prevent_author_rescan(monkeypatch)
    before_fingerprint = analysis._sources(fixture.store)[2]
    page = analysis.query_contact_analysis(fixture.store, CONTACT)
    assert page["status"] == "ready"
    assert page["total_count"] == 3
    assert page["observation_count"] == 12
    assert {item["id"] for item in page["items"]} == set(fixture.group_refs)
    assert all(item["source_label"] == "iPad archive" for item in page["items"])
    assert all(item["kind"] == "group" for item in page["items"])
    assert all(item["original_conversation_unknown"] for item in page["items"])
    assert all(item["observation_count"] == 4 for item in page["items"])
    assert all(len(item["observations"]) == 4 for item in page["items"])
    assert page["source_fingerprint"] == before_fingerprint
    retained = _snapshot(fixture.cache)
    assert retained["refs"] == fixture.baseline["refs"]
    assert len(retained["statement_members"]) == 12
    exported = analysis.export_contact_analysis_items(
        fixture.store, CONTACT, source_fingerprint=page["source_fingerprint"]
    )
    first = next(exported)
    assert first["source_label"] == "iPad archive"
    assert first["parent_message_id"] == fixture.native_group
    text = "".join(contact_csv_chunks(exported, first=first))
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    assert rows[0] == ["Time", "Statement"]
    assert len(rows) == 4
    assert {row[1] for row in rows[1:]} == {
        item["content_text"] for item in page["items"]
    }
    raw_sources = list(
        analysis.export_contact_analysis_items(
            fixture.store,
            CONTACT,
            source_fingerprint=page["source_fingerprint"],
            source_occurrences=True,
        )
    )
    assert len(raw_sources) == 12
    assert fixture.preserved == (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
    )


def test_review_removal_restores_prior_membership_and_reapplication_is_idempotent(
    retained_review_store, monkeypatch
):
    fixture = retained_review_store
    _prevent_author_rescan(monkeypatch)
    reviewed = analysis.query_contact_analysis(fixture.store, CONTACT)
    first_applied = _snapshot(fixture.cache)
    fixture.review_path.unlink()
    removed_fingerprint = analysis._sources(fixture.store)[2]
    assert removed_fingerprint != reviewed["source_fingerprint"]
    restored = analysis.query_contact_analysis(fixture.store, CONTACT)
    assert restored["total_count"] == 6
    assert restored["observation_count"] == 12
    assert _snapshot(fixture.cache) == fixture.baseline
    fixture.review_path.write_bytes(fixture.review_bytes)
    reapplied = analysis.query_contact_analysis(fixture.store, CONTACT)
    assert reapplied["total_count"] == 3
    assert _snapshot(fixture.cache) == first_applied
    again = analysis.query_contact_analysis(fixture.store, CONTACT)
    assert again["source_fingerprint"] == reapplied["source_fingerprint"]
    assert _snapshot(fixture.cache) == first_applied
    assert fixture.preserved == (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
    )


def test_changed_review_pin_restores_sources_instead_of_retaining_old_union(
    retained_review_store, monkeypatch
):
    fixture = retained_review_store
    _prevent_author_rescan(monkeypatch)
    page = analysis.query_contact_analysis(fixture.store, CONTACT)
    declaration = json.loads(fixture.review_bytes)
    declaration["packet_reviews"][0]["parents"][0]["text_sha256"] = "0" * 64
    fixture.review_path.write_text(json.dumps(declaration))
    assert reviews.reviews_digest(fixture.store)
    invalid = analysis.query_contact_analysis(fixture.store, CONTACT)
    assert invalid["source_fingerprint"] != page["source_fingerprint"]
    assert invalid["total_count"] == 6
    assert _snapshot(fixture.cache) == fixture.baseline
    assert fixture.preserved == (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
    )


def test_registry_change_during_apply_cannot_publish_an_aba_fingerprint(
    retained_review_store, monkeypatch
):
    fixture = retained_review_store
    _prevent_author_rescan(monkeypatch)
    before = fixture.cache.read_bytes()
    original_apply = analysis.apply_reviewed_packet_groups

    def transient_other_registry(*args, **kwargs):
        fixture.review_path.write_text('{"schema_version":1,"packet_reviews":[]}')
        try:
            return original_apply(*args, **kwargs)
        finally:
            fixture.review_path.write_bytes(fixture.review_bytes)

    with monkeypatch.context() as during_apply:
        during_apply.setattr(
            analysis, "apply_reviewed_packet_groups", transient_other_registry
        )
        with pytest.raises(
            analysis.ContactAnalysisSourceChanged, match="review|source|changed"
        ):
            analysis.query_contact_analysis(fixture.store, CONTACT)

    assert fixture.cache.read_bytes() == before
    assert fixture.review_path.read_bytes() == fixture.review_bytes
    recovered = analysis.query_contact_analysis(fixture.store, CONTACT)
    assert recovered["total_count"] == 3
    assert {item["id"] for item in recovered["items"]} == set(fixture.group_refs)
    assert fixture.preserved == (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
    )
