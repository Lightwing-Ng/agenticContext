"""Verified contact analysis preserves account evidence and rejects name collisions.

Code version: v1.0.2-codex.0
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from threading import Event
from types import SimpleNamespace

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core.wechat_archive import import_wechat_archive, wechat_archive_path
from app.core.wechat_contacts import (
    MESSAGE_BINDING_AUTHORITY,
    canonical_contact_id,
    contact_observation,
    contact_registry_path,
    initialize_contact_registry,
)
from app.core.wechat_display_names import display_names_path
from tests.test_wechat_archive import make_archive


TARGET = "wxid_peer"
OTHER = "wxid_other"
CID = canonical_contact_id(TARGET, authority="native_contact_username")
OTHER_CID = canonical_contact_id(OTHER, authority="native_contact_username")


@pytest.fixture
def contact_analysis_store(tmp_path):
    archive = make_archive(tmp_path / "archive", repeated=4)
    store = tmp_path / "store"
    import_wechat_archive(archive, store)
    history = wechat_archive_path(store)
    with sqlite3.connect(history) as connection:
        connection.row_factory = sqlite3.Row
        native = [
            dict(row)
            for row in connection.execute(
                "SELECT m.*,c.partition_id FROM messages m JOIN conversations c ON c.id=m.conversation_id "
                "WHERE c.partition_id LIKE 'ios-%' AND m.sender_id=? ORDER BY c.partition_id,m.id",
                (TARGET,),
            )
        ]
        expected = {row["id"] for row in native}
        assert len(expected) == 8
        other = native[0]
        expected.remove(other["id"])
        connection.execute(
            "UPDATE messages SET sender_id=?,sender_label='Flora' WHERE id=?",
            (OTHER, other["id"]),
        )
        legacy = dict(
            connection.execute(
                "SELECT m.* FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE c.partition_id='xlsx-2' ORDER BY m.id LIMIT 1"
            ).fetchone()
        )
        connection.execute(
            "UPDATE messages SET sender_id=NULL,sender_label='Flora' WHERE id=?",
            (legacy["id"],),
        )
        expected.add(legacy["id"])
        export = [
            dict(row)
            for row in connection.execute(
                "SELECT m.* FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE c.partition_id='xlsx-3' ORDER BY m.id LIMIT 2"
            )
        ]
        connection.execute(
            "CREATE TABLE source_snapshots(id INTEGER PRIMARY KEY,snapshot_id TEXT,source_id TEXT,metadata TEXT)"
        )
        for number, snapshot in ((1, "export-good"), (2, "export-wrong")):
            connection.execute(
                "INSERT INTO source_snapshots VALUES(?,?,?,?)",
                (
                    number,
                    snapshot,
                    "xlsx-3",
                    json.dumps({"source_file": snapshot + ".html"}),
                ),
            )
            provenance = json.loads(export[number - 1]["provenance"])
            provenance["source_snapshot"] = number
            connection.execute(
                "UPDATE messages SET sender_id='public_alias',sender_label='Flora',provenance=? WHERE id=?",
                (json.dumps(provenance), export[number - 1]["id"]),
            )
        expected.add(export[0]["id"])
    registry = contact_registry_path(store)
    with sqlite3.connect(registry) as connection:
        initialize_contact_registry(connection)
        for cid, username in ((CID, TARGET), (OTHER_CID, OTHER)):
            connection.execute(
                "INSERT INTO contacts VALUES(?,?,?,?,?,?)",
                (cid, "wechat", "", username, "wechat_account", "verified"),
            )
        sources = sorted({row["partition_id"] for row in native})
        for source in sources:
            connection.execute(
                "INSERT INTO sender_bindings VALUES(?,?,?,?,?,?,?)",
                (source, TARGET, "", "", CID, "native_contact_username", "{}"),
            )
        connection.execute(
            "INSERT INTO sender_bindings VALUES(?,?,?,?,?,?,?)",
            (
                other["partition_id"],
                OTHER,
                "",
                "",
                OTHER_CID,
                "native_contact_username",
                "{}",
            ),
        )
        connection.execute(
            "INSERT INTO sender_bindings VALUES(?,?,?,?,?,?,?)",
            (
                "xlsx-3",
                "public_alias",
                "export-good",
                "",
                CID,
                "corroborated_export_username",
                "{}",
            ),
        )
        provenance = json.loads(legacy["provenance"])
        connection.execute(
            "INSERT INTO message_contact_bindings VALUES(?,?,?,?,?,?,?,?)",
            (
                "xlsx-2",
                legacy["id"],
                hashlib.sha256(legacy["text"].encode()).hexdigest(),
                legacy["timestamp_value"],
                provenance["record_id"],
                CID,
                MESSAGE_BINDING_AUTHORITY,
                '{"basis":"synthetic independent source proof"}',
            ),
        )
        observation = contact_observation(
            CID, source_id=sources[0], kind="nickname", value="Old synthetic name"
        )
        columns = tuple(observation)
        connection.execute(
            "INSERT INTO contact_observations("
            + ",".join(columns)
            + ") VALUES("
            + ",".join("?" for _ in columns)
            + ")",
            tuple(observation[column] for column in columns),
        )
    display_names_path(store).write_text(
        json.dumps({"schema_version": 1, "contacts": {CID: "Research Contact"}})
    )
    return SimpleNamespace(
        store=store,
        history=history,
        registry=registry,
        expected=expected,
        legacy=legacy,
        other=other,
        export=export,
    )


def test_all_verified_text_occurrences_keep_sources_and_leave_namesakes_out(
    contact_analysis_store,
):
    fixture = contact_analysis_store
    before = (fixture.history.read_bytes(), fixture.registry.read_bytes())
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["status"] == "ready"
    assert page["contact"]["id"] == CID
    assert page["contact"]["display_name"] == "Research Contact"
    assert {item["id"] for item in page["items"]} == fixture.expected
    assert page["counts"] == {
        "messages": 9,
        "conversations": 6,
        "source_accounts": 4,
        "sources": 4,
    }
    assert {item["contact_id"] for item in page["items"]} == {CID}
    assert fixture.other["id"] not in fixture.expected
    assert fixture.export[1]["id"] not in fixture.expected
    assert fixture.legacy["id"] in fixture.expected
    assert (
        next(item for item in page["items"] if item["id"] == fixture.export[0]["id"])[
            "source_file"
        ]
        == "export-good.html"
    )
    assert all(item["content_text"] for item in page["items"])
    assert {item["source_id"] for item in page["items"]} == {
        item["source_id"] for item in page["accounts"]
    }
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())
    with sqlite3.connect(
        analysis.contact_analysis_cache_path(fixture.store, CID)
    ) as cache:
        fields = {row[1] for row in cache.execute("PRAGMA table_info(refs)")}
        assert "text" not in fields and "content_text" not in fields


def test_search_pagination_and_warm_cache_avoid_repeating_source_scan(
    contact_analysis_store, monkeypatch
):
    fixture = contact_analysis_store
    initial = analysis.query_contact_analysis(fixture.store, CID, page_size=2)

    def scan_must_not_run(*_args):
        raise AssertionError("A warm query must not rescan source messages.")

    monkeypatch.setattr(analysis, "_candidate_batches", scan_must_not_run)
    next_page = analysis.query_contact_analysis(fixture.store, CID, page=2, page_size=2)
    assert initial["total_pages"] == 5
    assert not (
        {item["id"] for item in initial["items"]}
        & {item["id"] for item in next_page["items"]}
    )
    result = analysis.query_contact_analysis(fixture.store, CID, query="unknown")
    assert result["total_count"] == 2
    assert result["counts"]["sources"] == 2
    result = analysis.query_contact_analysis(
        fixture.store, CID, query="no matching body", page=999
    )
    assert result["items"] == [] and result["current_page"] == 1


def _watch_cloud_cache(monkeypatch, cache, enabled):
    original_stat = Path.stat
    original_connect = sqlite3.connect
    cache_uri = cache.resolve().as_uri()
    attempts = []

    class CloudState:
        def __init__(self, state):
            self.state = state
            self.st_flags = getattr(state, "st_flags", 0) | 0x40000000

        def __getattr__(self, name):
            return getattr(self.state, name)

    def inspect(path, *args, **kwargs):
        state = original_stat(path, *args, **kwargs)
        return CloudState(state) if path == cache and enabled() else state

    def connect(database, *args, **kwargs):
        if str(database).split("?", 1)[0] == cache_uri and enabled():
            attempts.append(str(database))
            raise AssertionError("A derived cloud-only cache must not be opened.")
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", inspect)
    monkeypatch.setattr(analysis.sqlite3, "connect", connect)
    return attempts


def test_cloud_only_cache_is_not_read_or_reused_and_rebuilds_from_local_sources(
    contact_analysis_store, monkeypatch
):
    fixture = contact_analysis_store
    initial = analysis.query_contact_analysis(fixture.store, CID)
    cache = analysis.contact_analysis_cache_path(fixture.store, CID)
    original_stat = Path.stat
    old_inode = original_stat(cache).st_ino
    before = (fixture.history.read_bytes(), fixture.registry.read_bytes())
    attempts = _watch_cloud_cache(
        monkeypatch, cache, lambda: original_stat(cache).st_ino == old_inode
    )

    assert not analysis._cache_ready(cache, initial["source_fingerprint"])
    rebuilt = analysis.query_contact_analysis(fixture.store, CID)

    assert attempts == []
    assert original_stat(cache).st_ino != old_inode
    assert rebuilt["status"] == "ready"
    assert rebuilt["source_fingerprint"] == initial["source_fingerprint"]
    assert {item["id"] for item in rebuilt["items"]} == fixture.expected
    assert rebuilt["counts"] == initial["counts"]
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


@pytest.mark.parametrize("operation", ["query", "export"])
def test_cache_evicted_after_readiness_check_is_not_opened(
    contact_analysis_store, monkeypatch, operation
):
    fixture = contact_analysis_store
    initial = analysis.query_contact_analysis(fixture.store, CID)
    cache = analysis.contact_analysis_cache_path(fixture.store, CID)
    cloud_only = [False]
    attempts = _watch_cloud_cache(monkeypatch, cache, lambda: cloud_only[0])
    original_ready = analysis._cache_ready

    def evict_after_ready(*args):
        ready = original_ready(*args)
        assert ready
        cloud_only[0] = True
        return ready

    monkeypatch.setattr(analysis, "_cache_ready", evict_after_ready)
    with pytest.raises(analysis.ContactAnalysisSourceChanged, match="cloud-only"):
        if operation == "query":
            analysis.query_contact_analysis(fixture.store, CID)
        else:
            list(
                analysis.export_contact_analysis_items(
                    fixture.store,
                    CID,
                    source_fingerprint=initial["source_fingerprint"],
                )
            )
    assert attempts == []


def test_new_source_bytes_invalidate_cache_and_tampered_message_proof_is_rejected(
    contact_analysis_store,
):
    fixture = contact_analysis_store
    before = analysis.query_contact_analysis(fixture.store, CID)
    with sqlite3.connect(fixture.history) as connection:
        connection.execute(
            "UPDATE messages SET text='Changed source text' WHERE id=?",
            (fixture.legacy["id"],),
        )
    after = analysis.query_contact_analysis(fixture.store, CID)
    assert before["source_fingerprint"] != after["source_fingerprint"]
    assert after["total_count"] == 8
    assert fixture.legacy["id"] not in {row["id"] for row in after["items"]}


def test_contact_registry_changes_invalidate_cache_and_media_system_rows_are_excluded(
    contact_analysis_store,
):
    fixture = contact_analysis_store
    before = analysis.query_contact_analysis(fixture.store, CID)
    with sqlite3.connect(fixture.registry) as connection:
        connection.execute("DELETE FROM sender_bindings WHERE source_id='xlsx-3'")
    after = analysis.query_contact_analysis(fixture.store, CID)
    assert before["source_fingerprint"] != after["source_fingerprint"]
    assert after["total_count"] == 8
    targets = [
        row["id"] for row in after["items"] if row["source_id"].startswith("ios-")
    ][:2]
    with sqlite3.connect(fixture.history) as connection:
        for identifier, extra in zip(
            targets,
            (
                {"text_extraction_kind": "system_text"},
                {
                    "text_extraction_kind": "resource_placeholder",
                    "resource_reference": {"kind": "image"},
                },
            ),
            strict=True,
        ):
            provenance = json.loads(
                connection.execute(
                    "SELECT provenance FROM messages WHERE id=?", (identifier,)
                ).fetchone()[0]
            )
            provenance.update(extra)
            connection.execute(
                "UPDATE messages SET provenance=? WHERE id=?",
                (json.dumps(provenance), identifier),
            )
    result = analysis.query_contact_analysis(fixture.store, CID)
    assert result["total_count"] == 6
    assert not (set(targets) & {row["id"] for row in result["items"]})


def test_background_first_request_returns_immediately_and_publishes_complete_index(
    contact_analysis_store, monkeypatch
):
    fixture = contact_analysis_store
    entered, release = Event(), Event()
    original = analysis._candidate_batches

    def paused(*args):
        entered.set()
        assert release.wait(10)
        yield from original(*args)

    monkeypatch.setattr(analysis, "_candidate_batches", paused)
    try:
        page = analysis.query_contact_analysis(fixture.store, CID, background=True)
        assert page["status"] == "indexing" and page["items"] == []
        assert entered.wait(10)
    finally:
        release.set()
    job = analysis._JOBS[(str(fixture.store.resolve()), CID)][1]
    job.result(timeout=10)
    page = analysis.query_contact_analysis(fixture.store, CID, background=True)
    assert page["status"] == "ready" and page["total_count"] == 9


def test_changed_source_during_build_is_never_published(
    contact_analysis_store, monkeypatch
):
    fixture = contact_analysis_store
    original = analysis._candidate_batches

    def changed(*args):
        yield from original(*args)
        with sqlite3.connect(fixture.registry) as connection:
            connection.execute(
                "INSERT INTO contact_registry_info VALUES('changed','true')"
            )

    monkeypatch.setattr(analysis, "_candidate_batches", changed)
    with pytest.raises(analysis.ContactAnalysisSourceChanged):
        analysis.query_contact_analysis(fixture.store, CID)
    assert not analysis.contact_analysis_cache_path(fixture.store, CID).exists()


def test_missing_unverified_and_malformed_contacts_do_not_create_an_index(
    contact_analysis_store,
):
    fixture = contact_analysis_store
    assert (
        analysis.query_contact_analysis(fixture.store, "wechat:contact:" + "0" * 64)
        is None
    )
    with pytest.raises(ValueError):
        analysis.query_contact_analysis(fixture.store, "Flora")
    with sqlite3.connect(fixture.registry) as connection:
        connection.execute(
            "UPDATE contacts SET identity_status='source_scoped' WHERE id=?", (CID,)
        )
    assert analysis.query_contact_analysis(fixture.store, CID) is None
    assert not analysis.contact_analysis_cache_path(fixture.store, CID).exists()


def test_source_location_uses_actual_page_and_unknown_times_sort_last(tmp_path):
    archive = make_archive(tmp_path / "archive", repeated=104)
    store = tmp_path / "store"
    import_wechat_archive(archive, store)
    path = wechat_archive_path(store)
    with sqlite3.connect(path) as connection:
        conversation = connection.execute(
            "SELECT id FROM conversations WHERE kind='direct' LIMIT 1"
        ).fetchone()[0]
        ids = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM messages WHERE conversation_id=? ORDER BY time_sort,ordinal",
                (conversation,),
            )
        ]
        connection.execute(
            "UPDATE messages SET time_sort=0,timestamp_value='' WHERE id=?", (ids[0],)
        )
    assert analysis.message_source_location(store, ids[101])["page"] == 2
    assert analysis.message_source_location(store, ids[0])["page"] == 2
    assert analysis.message_source_location(store, ids[1])["page"] == 1
    assert analysis.message_source_location(store, "../outside") is None
    assert analysis.message_source_location(store, "0" * 64) is None


def test_source_location_keeps_equal_time_and_ordinal_ties_on_the_actual_page(tmp_path):
    archive = make_archive(tmp_path / "archive", repeated=103)
    store = tmp_path / "store"
    import_wechat_archive(archive, store)
    path = wechat_archive_path(store)
    with sqlite3.connect(path) as connection:
        conversation = connection.execute(
            "SELECT id FROM conversations WHERE kind='direct' LIMIT 1"
        ).fetchone()[0]
        connection.execute(
            "UPDATE messages SET ordinal=1 WHERE conversation_id=?", (conversation,)
        )
    repository = analysis.WechatArchiveRepository(store)
    first = repository.query(session=conversation, page=1)["messages"]
    second = repository.query(session=conversation, page=2)["messages"]
    assert len(first) == 100 and len(second) == 3
    for message, expected in ((first[-1], 1), (second[0], 2), (second[-1], 2)):
        location = analysis.message_source_location(store, message["id"])
        assert location["page"] == expected
        assert message["id"] in {
            item["id"]
            for item in repository.query(session=conversation, page=location["page"])[
                "messages"
            ]
        }


@pytest.mark.parametrize("suffix", ["-wal", "-journal", "-shm"])
def test_analysis_refuses_immutable_reads_with_a_database_journal(
    contact_analysis_store, suffix
):
    fixture = contact_analysis_store
    journal = fixture.history.with_name(fixture.history.name + suffix)
    journal.write_bytes(b"synthetic pending database publication")
    with pytest.raises(analysis.ContactAnalysisSourceChanged, match="journal"):
        analysis.query_contact_analysis(fixture.store, CID)
    assert not analysis.contact_analysis_cache_path(fixture.store, CID).exists()


def test_export_account_viewing_names_require_the_original_native_owner_anchor(
    contact_analysis_store,
):
    fixture = contact_analysis_store
    owner_a, owner_b = "wxid_account_c", "wxid_account_b"
    with sqlite3.connect(fixture.history) as connection:
        anchor_source = (
            "ios-" + hashlib.md5(owner_a.encode(), usedforsecurity=False).hexdigest()
        )
        connection.execute(
            "INSERT INTO partitions SELECT ?,?,?,account_label,identity_status,source_label,source_file,payload_file "
            "FROM partitions WHERE account_id='wxid_account_a' LIMIT 1",
            (anchor_source, "account:" + owner_a, owner_a),
        )
        for source, owner, status in (
            ("xlsx-3", owner_a, "Corroborated export ID"),
            ("xlsx-2", owner_b, "Source account unconfirmed"),
        ):
            connection.execute(
                "UPDATE partitions SET account_id=?,account_key=?,account_label=?,identity_status=? WHERE id=?",
                (owner, "account:" + owner, "Shared raw account label", status, source),
            )
            connection.execute(
                "UPDATE conversations SET account_key=? WHERE partition_id=?",
                ("account:" + owner, source),
            )
    preferences = {
        "schema_version": 1,
        "contacts": {
            CID: "Research Contact",
            canonical_contact_id(
                owner_a, authority="native_account_owner"
            ): "Viewing owner A",
            canonical_contact_id(
                owner_b, authority="native_account_owner"
            ): "Viewing owner B",
        },
    }
    display_names_path(fixture.store).write_text(json.dumps(preferences))
    page = analysis.query_contact_analysis(fixture.store, CID)
    export = next(row for row in page["items"] if row["source_id"] == "xlsx-3")
    unconfirmed = next(row for row in page["items"] if row["source_id"] == "xlsx-2")
    assert export["viewer_account_label"] == "Viewing owner A · ount_c"
    assert unconfirmed["viewer_account_label"] == "Shared raw account label"
    accounts = {row["source_id"]: row["label"] for row in page["accounts"]}
    assert accounts["xlsx-3"] == "Viewing owner A · ount_c"
    assert accounts["xlsx-2"] == "Shared raw account label"
    assert anchor_source not in accounts
