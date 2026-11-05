"""Unique contact statements preserve independent observations and native identity.

Code version: v1.2.1-codex.0
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core.wechat_archive import import_wechat_archive, wechat_archive_path
from app.core.wechat_contacts import (
    canonical_contact_id,
    contact_registry_path,
    initialize_contact_registry,
)
from tests.test_wechat_archive import make_archive


TARGET = "wxid_peer"
CID = canonical_contact_id(TARGET, authority="native_contact_username")
BODY = "0元@Synthetic\u2005"
EPOCH = 1793339123


@pytest.fixture
def statement_store(tmp_path):
    archive = make_archive(tmp_path / "archive")
    store = tmp_path / "store"
    import_wechat_archive(archive, store)
    history = wechat_archive_path(store)
    with sqlite3.connect(history) as connection:
        connection.row_factory = sqlite3.Row
        rows = [
            dict(row)
            for row in connection.execute(
                analysis._ROW_SELECT
                + "WHERE c.kind='group' AND c.partition_id LIKE 'ios-%' ORDER BY m.id"
            )
        ]
        assert len(rows) == 2
        connection.execute("UPDATE messages SET sender_id='wxid_other'")
        for row in rows:
            provenance = json.loads(row["provenance"])
            provenance.update(
                server_id="7210500216981401697",
                message_type=1,
                unix_timestamp=EPOCH,
            )
            connection.execute(
                "UPDATE messages SET sender_id=?,text=?,search_text=?,provenance=?,"
                "timestamp_value=?,timestamp_label=?,time_sort=? WHERE id=?",
                (
                    TARGET,
                    BODY,
                    BODY.casefold(),
                    json.dumps(provenance),
                    "2026-10-30T13:45:23+08:00",
                    "30 Oct 2026 13:45:23",
                    EPOCH,
                    row["id"],
                ),
            )
            connection.execute(
                "UPDATE conversations SET peer_id='fixture-shared@chatroom' WHERE id=?",
                (row["conversation_id"],),
            )
    registry = contact_registry_path(store)
    with sqlite3.connect(registry) as connection:
        initialize_contact_registry(connection)
        connection.execute(
            "INSERT INTO contacts VALUES(?,?,?,?,?,?)",
            (CID, "wechat", "", TARGET, "wechat_account", "verified"),
        )
        for row in rows:
            connection.execute(
                "INSERT INTO sender_bindings VALUES(?,?,?,?,?,?,?)",
                (
                    row["source_id"],
                    TARGET,
                    "",
                    "",
                    CID,
                    "native_contact_username",
                    "{}",
                ),
            )
    return SimpleNamespace(store=store, history=history, registry=registry, rows=rows)


@pytest.mark.parametrize("sort", ["newest", "oldest"])
def test_unique_statement_precedes_search_counts_and_pagination(statement_store, sort):
    fixture = statement_store
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    page = analysis.query_contact_analysis(
        fixture.store, CID, query="Synthetic", page=2, page_size=1, sort=sort
    )
    assert page["total_count"] == 1
    assert page["current_page"] == page["total_pages"] == 1
    assert page["counts"] == {
        "messages": 1,
        "conversations": 1,
        "source_accounts": 2,
        "sources": 2,
    }
    assert page["observation_count"] == 2
    (item,) = page["items"]
    assert item["content_text"] == BODY
    assert item["observation_count"] == item["source_account_count"] == 2
    assert {origin["id"] for origin in item["observations"]} == {
        row["id"] for row in fixture.rows
    }
    assert len({origin["account_key"] for origin in item["observations"]}) == 2
    assert all("content_text" not in origin for origin in item["observations"])
    exported = list(
        analysis.export_contact_analysis_items(
            fixture.store,
            CID,
            query="Synthetic",
            sort=sort,
            source_fingerprint=page["source_fingerprint"],
        )
    )
    assert len(exported) == 1
    assert {origin["statement_id"] for origin in exported} == {item["statement_id"]}
    assert {origin["id"] for origin in exported} == {item["id"]}
    assert all(origin["content_text"] == BODY for origin in exported)
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())
    with sqlite3.connect(
        analysis.contact_analysis_cache_path(fixture.store, CID)
    ) as cache:
        assert cache.execute("SELECT COUNT(*) FROM refs").fetchone()[0] == 2
        for table in ("refs", "statement_groups", "statement_members"):
            fields = {row[1] for row in cache.execute(f"PRAGMA table_info({table})")}
            assert not {"text", "content_text", "provenance"} & fields


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("server_id", "7210500216981401698"),
        ("server_id", 0),
        ("server_id", None),
        ("server_id", True),
        ("server_id", 7210500216981401697.0),
        ("server_id", "07210500216981401697"),
        ("server_id", "7.210500216981401697e18"),
        ("message_type", 49),
        ("message_type", True),
        ("message_type", 0),
        ("message_type", -1),
        ("message_type", None),
        ("message_type", "1"),
        ("text", BODY.upper()),
        ("text", BODY.rstrip()),
        ("peer_id", "different-group@chatroom"),
    ],
)
def test_conflicting_or_insufficient_event_evidence_stays_separate(
    statement_store, field, value
):
    fixture = statement_store
    row = fixture.rows[1]
    with sqlite3.connect(fixture.history) as connection:
        if field in {"server_id", "message_type", "unix_timestamp"}:
            provenance = json.loads(
                connection.execute(
                    "SELECT provenance FROM messages WHERE id=?", (row["id"],)
                ).fetchone()[0]
            )
            provenance[field] = value
            connection.execute(
                "UPDATE messages SET provenance=? WHERE id=?",
                (json.dumps(provenance), row["id"]),
            )
        elif field == "peer_id":
            connection.execute(
                "UPDATE conversations SET peer_id=? WHERE id=?",
                (value, row["conversation_id"]),
            )
        else:
            connection.execute(
                f"UPDATE messages SET {field}=? WHERE id=?", (value, row["id"])
            )
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["total_count"] == page["observation_count"] == 2
    assert all(item["observation_count"] == 1 for item in page["items"])


def test_equivalent_timezones_are_the_same_native_event(statement_store):
    fixture = statement_store
    with sqlite3.connect(fixture.history) as connection:
        connection.execute(
            "UPDATE messages SET timestamp_value='2026-10-30T05:45:23Z' WHERE id=?",
            (fixture.rows[1]["id"],),
        )
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["total_count"] == 1


def test_private_identity_uses_participant_pair_not_viewer_direction(statement_store):
    fixture = statement_store
    with analysis._readonly(fixture.history) as connection:
        row = dict(
            connection.execute(
                analysis._ROW_SELECT + "WHERE m.id=?", (fixture.rows[0]["id"],)
            ).fetchone()
        )
    first, second = "wxid_first", "wxid_second"

    def private(owner, peer, direction):
        return {
            **row,
            "account_id": owner,
            "account_key": "account:" + owner,
            "container_account_key": "account:" + owner,
            "source_id": "ios-" + hashlib.md5(owner.encode()).hexdigest(),
            "kind": "direct",
            "peer_id": peer,
            "direction": direction,
        }

    left = private(first, second, "received")
    right = private(second, first, "sent")
    thread = analysis._native_conversation_key(left)
    assert thread == analysis._native_conversation_key(right)
    assert analysis._native_statement_key(left, CID, thread) == (
        analysis._native_statement_key(right, CID, thread)
    )
    assert thread != analysis._native_conversation_key(
        private(first, TARGET, "received")
    )
    assert (
        analysis._native_conversation_key({**left, "source_id": "xlsx-legacy"}) is None
    )


def _legacy_cache(fixture, *, stale=False):
    analysis.query_contact_analysis(fixture.store, CID)
    identities = {
        str(path.resolve()): analysis._identity(path)
        for path in (fixture.history, fixture.registry)
    }
    fingerprint = hashlib.sha256(
        analysis._json([analysis._LEGACY_REFERENCE_VERSION, identities]).encode()
    ).hexdigest()
    with closing(sqlite3.connect(
        analysis.contact_analysis_cache_path(fixture.store, CID)
    )) as cache, cache:
        cache.execute("DROP TABLE statement_members")
        cache.execute("DROP TABLE statement_groups")
        cache.executemany(
            "UPDATE info SET value=? WHERE key=?",
            [
                (analysis._LEGACY_REFERENCE_VERSION, "version"),
                ("stale" if stale else fingerprint, "fingerprint"),
            ],
        )


def test_unchanged_legacy_cache_upgrades_without_full_archive_scan(
    statement_store, monkeypatch
):
    fixture = statement_store
    _legacy_cache(fixture)

    def no_scan(*args, **kwargs):
        raise AssertionError(
            "An unchanged legacy reference cache must avoid a full scan."
        )

    monkeypatch.setattr(analysis, "_candidate_batches", no_scan)
    monkeypatch.setattr(analysis, "_forwarded_records", no_scan)
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["total_count"] == 1
    assert page["observation_count"] == 2


def test_stale_legacy_cache_rebuilds_from_original_sources(
    statement_store, monkeypatch
):
    fixture = statement_store
    _legacy_cache(fixture, stale=True)
    scanned = []
    original = analysis._candidate_batches

    def scan(*args, **kwargs):
        scanned.append(True)
        yield from original(*args, **kwargs)

    monkeypatch.setattr(analysis, "_candidate_batches", scan)
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert scanned
    assert page["total_count"] == 1
    assert page["observation_count"] == 2
