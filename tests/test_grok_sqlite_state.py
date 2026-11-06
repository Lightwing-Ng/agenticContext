"""Verify Grok keyed persistence, legacy boundaries, and competing queue claims.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import sqlite3
from threading import Barrier

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.core.grok_downloader import (
    GrokCatalogEntry,
    GrokDownloadManifest,
    GrokManifestEntry,
    GrokMediaCandidate,
    GrokMediaCatalog,
    GrokWorkQueue,
    GrokWorkQueueEntry,
    compute_sha256,
)
from app.core.media_sqlite import media_store_for, read_media_sqlite_schema
from app.core.resource_persistence import (
    GROK_CATALOG_FILENAME,
    GROK_CATALOG_SCHEMA,
    GROK_CATALOG_SCHEMA_VERSION,
    GROK_DOWNLOAD_MANIFEST_FILENAME,
    GROK_DOWNLOAD_MANIFEST_SCHEMA,
    GROK_DOWNLOAD_MANIFEST_SCHEMA_VERSION,
    GROK_WORK_QUEUE_FILENAME,
    GROK_WORK_QUEUE_SCHEMA,
    GROK_WORK_QUEUE_SCHEMA_VERSION,
)


STAMP = "2026-07-29T00:00:00Z"


def _candidate(index=1):
    identity = f"{index:08x}-0000-0000-0000-000000000001"
    return GrokMediaCandidate(
        source_url=f"https://files.example.test/{identity}/photo.jpg",
        asset_id=identity,
        asset_name="photo",
        media_kind="image",
        identity=f"{identity}/photo",
        created_at=STAMP,
    )


def _audit(path, key):
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE changed_keys(operation TEXT,identity TEXT);"
            f"CREATE TRIGGER audit_update AFTER UPDATE ON media_rows BEGIN "
            f"INSERT INTO changed_keys VALUES('update',new.{key}); END;"
            f"CREATE TRIGGER audit_insert AFTER INSERT ON media_rows BEGIN "
            f"INSERT INTO changed_keys VALUES('insert',new.{key}); END;"
            f"CREATE TRIGGER audit_delete AFTER DELETE ON media_rows BEGIN "
            f"INSERT INTO changed_keys VALUES('delete',old.{key}); END;"
        )


def _changes(path):
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT * FROM changed_keys").fetchall()


def test_catalog_changes_only_one_key_and_uses_persisted_point_and_hash_queries(tmp_path, monkeypatch):
    catalog = GrokMediaCatalog(tmp_path)
    candidates = [_candidate(index) for index in range(1, 33)]
    for index, candidate in enumerate(candidates):
        content = b"\xff\xd8\xff\xe0synthetic-" + str(index).encode()
        filename = f"photo-{index}.jpg"
        (tmp_path / filename).write_bytes(content)
        catalog.register_download(candidate, filename, compute_sha256(content), len(content), STAMP)
    catalog.flush()
    _audit(catalog.catalog_path, "identity")
    calls = []
    original_get, original_find = catalog._store.get, catalog._store.find

    def get(key):
        calls.append(("get", key))
        return original_get(key)

    def find(field, value):
        calls.append(("find", field))
        return original_find(field, value)

    monkeypatch.setattr(catalog._store, "get", get)
    monkeypatch.setattr(catalog._store, "find", find)
    entry = catalog.get_entry(candidates[0].identity)
    assert entry is not None
    assert catalog.lookup_relative_path_by_hash(entry.content_sha256) == entry.relative_path
    assert ("get", candidates[0].identity) in calls
    assert ("find", "content_sha256") in calls
    catalog.register_alias(candidates[0], entry.relative_path, entry.content_sha256, "2026-07-30T00:00:00Z")
    catalog.flush()
    assert _changes(catalog.catalog_path) == [("update", candidates[0].identity)]
    catalog.flush()
    assert _changes(catalog.catalog_path) == [("update", candidates[0].identity)]


def test_manifest_transition_updates_only_one_record_and_failed_write_can_retry(tmp_path):
    catalog = GrokMediaCatalog(tmp_path)
    manifest = GrokDownloadManifest.build(tmp_path, catalog)
    candidates = [_candidate(index) for index in range(1, 33)]
    manifest.prepare_candidates(candidates, catalog)
    _audit(manifest.manifest_path, "identity")
    before = manifest._store.rows()
    candidate = candidates[0]
    manifest.mark_in_progress(candidate, ".grok-partial/synthetic.part")
    assert _changes(manifest.manifest_path) == [("update", candidate.identity)]
    after = manifest._store.rows()
    assert before[1:] == after[1:]
    with sqlite3.connect(manifest.manifest_path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_failure BEFORE UPDATE ON media_rows "
            "BEGIN SELECT RAISE(ABORT,'synthetic write failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic write failure"):
        manifest.mark_failed(candidate, ".grok-partial/synthetic.part", "retryable failure")
    assert manifest._store.rows() == after
    assert manifest.dirty is True
    with sqlite3.connect(manifest.manifest_path) as connection:
        connection.execute("DROP TRIGGER reject_failure")
    manifest.flush()
    assert manifest._store.get(candidate.identity)["status"] == "failed"
    assert manifest.dirty is False


@pytest.mark.parametrize("kind", ["resolution", "download"])
def test_two_queue_instances_cannot_claim_the_same_persisted_status(tmp_path, monkeypatch, kind):
    catalog = GrokMediaCatalog(tmp_path)
    first = GrokWorkQueue.build(tmp_path, catalog)
    candidate = _candidate()
    first.register_discovered([candidate])
    if kind == "download":
        first.mark_resolved(candidate.asset_id, candidate)
    second = GrokWorkQueue.build(tmp_path, catalog)
    barrier = Barrier(2)
    for queue in (first, second):
        original = queue._store.compare_and_swap

        def competing(*arguments, operation=original, **options):
            barrier.wait(timeout=10)
            return operation(*arguments, **options)

        monkeypatch.setattr(queue._store, "compare_and_swap", competing)
    claim_name = "claim_for_resolution" if kind == "resolution" else "claim_ready_for_download"
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda queue: getattr(queue, claim_name)(1), (first, second)))
    assert sum(len(result) for result in results) == 1
    row = first._store.get(candidate.asset_id)
    assert row["status"] == ("resolving" if kind == "resolution" else "submitted")
    assert row["resolution_attempts" if kind == "resolution" else "download_attempts"] == 1


def test_queue_transition_preserves_unrelated_unknown_status_and_uses_status_index(tmp_path, monkeypatch):
    catalog = GrokMediaCatalog(tmp_path)
    queue = GrokWorkQueue.build(tmp_path, catalog)
    first, second = _candidate(), _candidate(2)
    queue.register_discovered([first, second])
    unrelated = queue._store.get(second.asset_id)
    unrelated.update(status="future_manual_review", last_error="Preserve source explanation")
    queue._store.upsert_rows([unrelated])
    _audit(queue.queue_path, "asset_id")
    calls = []
    original = queue._store.find

    def find(field, value):
        calls.append((field, value))
        return original(field, value)

    monkeypatch.setattr(queue._store, "find", find)
    claimed = queue.claim_for_resolution(1)
    assert [candidate.asset_id for candidate in claimed] == [first.asset_id]
    assert ("status", "discovered") in calls
    assert _changes(queue.queue_path) == [("update", first.asset_id)]
    assert queue._store.get(second.asset_id) == unrelated


@pytest.mark.parametrize("kind", ["catalog", "manifest", "queue"])
def test_legacy_parquet_load_is_read_only_until_explicit_state_change(tmp_path, kind):
    candidate = _candidate()
    catalog = GrokMediaCatalog(tmp_path)
    if kind == "catalog":
        payload = b"\xff\xd8\xff\xe0synthetic legacy image"
        (tmp_path / "photo.jpg").write_bytes(payload)
        entry = GrokCatalogEntry(candidate.identity, "photo.jpg", "image", compute_sha256(payload), len(payload), candidate.source_url, STAMP, STAMP)
        filename, schema, version = GROK_CATALOG_FILENAME, GROK_CATALOG_SCHEMA, GROK_CATALOG_SCHEMA_VERSION
    elif kind == "manifest":
        entry = GrokManifestEntry(candidate.identity, candidate.asset_id, candidate.asset_name, "image", candidate.source_url, created_at=STAMP, updated_at=STAMP)
        filename, schema, version = GROK_DOWNLOAD_MANIFEST_FILENAME, GROK_DOWNLOAD_MANIFEST_SCHEMA, GROK_DOWNLOAD_MANIFEST_SCHEMA_VERSION
    else:
        entry = GrokWorkQueueEntry(candidate.asset_id, identity=candidate.identity, asset_name=candidate.asset_name, source_url=candidate.source_url, created_at=STAMP, discovered_at=STAMP, updated_at=STAMP)
        filename, schema, version = GROK_WORK_QUEUE_FILENAME, GROK_WORK_QUEUE_SCHEMA, GROK_WORK_QUEUE_SCHEMA_VERSION
    canonical = tmp_path / filename
    legacy = canonical.with_suffix(".parquet")
    pq.write_table(pa.Table.from_pylist([{"schema_version": version, **asdict(entry)}], schema=schema), legacy)
    original, timestamp = legacy.read_bytes(), legacy.stat().st_mtime_ns
    if kind == "catalog":
        state = GrokMediaCatalog.build(tmp_path)
    elif kind == "manifest":
        state = GrokDownloadManifest.build(tmp_path, catalog)
    else:
        state = GrokWorkQueue.build(tmp_path, catalog)
    assert not canonical.exists()
    assert legacy.read_bytes() == original and legacy.stat().st_mtime_ns == timestamp
    if kind == "catalog":
        state.register_alias(candidate, "photo.jpg", entry.content_sha256, "2026-07-30T00:00:00Z")
        state.flush()
    elif kind == "manifest":
        state.mark_in_progress(candidate, ".grok-partial/legacy.part")
    else:
        assert len(state.claim_for_resolution(1)) == 1
    assert canonical.read_bytes().startswith(b"SQLite format 3")
    assert legacy.read_bytes() == original and legacy.stat().st_mtime_ns == timestamp
    stable = canonical.read_bytes()
    state.flush()
    assert canonical.read_bytes() == stable


def test_healthy_manifest_and_queue_reopen_without_rewriting_completed_rows(tmp_path, monkeypatch):
    candidate = _candidate()
    payload = b"\xff\xd8\xff\xe0healthy completed image"
    (tmp_path / "photo.jpg").write_bytes(payload)
    catalog = GrokMediaCatalog(tmp_path)
    catalog.register_download(candidate, "photo.jpg", compute_sha256(payload), len(payload), STAMP)
    catalog.flush()
    manifest = GrokDownloadManifest.build(tmp_path, catalog)
    manifest.mark_completed(candidate, "photo.jpg", compute_sha256(payload), len(payload))
    queue = GrokWorkQueue.build(tmp_path, catalog)
    queue.mark_completed(candidate)
    before = manifest.manifest_path.read_bytes(), queue.queue_path.read_bytes()
    monkeypatch.setattr("app.core.grok_downloader.utc_now", lambda: "2026-08-01T00:00:00Z")
    GrokDownloadManifest.build(tmp_path, catalog)
    GrokWorkQueue.build(tmp_path, catalog)
    assert before == (manifest.manifest_path.read_bytes(), queue.queue_path.read_bytes())


@pytest.mark.parametrize("kind", ["catalog", "manifest", "queue"])
def test_migrated_schema_metadata_survives_runtime_open_and_changed_row(tmp_path, kind):
    candidate = _candidate()
    catalog = GrokMediaCatalog(tmp_path)
    if kind == "catalog":
        payload = b"\xff\xd8\xff\xe0synthetic provenance image"
        (tmp_path / "photo.jpg").write_bytes(payload)
        entry = GrokCatalogEntry(
            candidate.identity, "photo.jpg", "image", compute_sha256(payload),
            len(payload), candidate.source_url, STAMP, STAMP,
        )
        filename, schema, version = GROK_CATALOG_FILENAME, GROK_CATALOG_SCHEMA, GROK_CATALOG_SCHEMA_VERSION
        key = candidate.identity
    elif kind == "manifest":
        entry = GrokManifestEntry(
            candidate.identity, candidate.asset_id, candidate.asset_name, "image",
            candidate.source_url, created_at=STAMP, updated_at=STAMP,
        )
        filename, schema, version = GROK_DOWNLOAD_MANIFEST_FILENAME, GROK_DOWNLOAD_MANIFEST_SCHEMA, GROK_DOWNLOAD_MANIFEST_SCHEMA_VERSION
        key = candidate.identity
    else:
        entry = GrokWorkQueueEntry(
            candidate.asset_id, identity=candidate.identity, asset_name=candidate.asset_name,
            source_url=candidate.source_url, created_at=STAMP, discovered_at=STAMP, updated_at=STAMP,
        )
        filename, schema, version = GROK_WORK_QUEUE_FILENAME, GROK_WORK_QUEUE_SCHEMA, GROK_WORK_QUEUE_SCHEMA_VERSION
        key = candidate.asset_id
    schema = schema.with_metadata({b"source_provenance": b"synthetic legacy metadata"})
    canonical = tmp_path / filename
    store = media_store_for(canonical, schema)
    store.replace_rows([{"schema_version": version, **asdict(entry)}])
    original_rows, original_bytes = store.rows(), canonical.read_bytes()
    if kind == "catalog":
        state = GrokMediaCatalog.build(tmp_path, repair=False)
    elif kind == "manifest":
        state = GrokDownloadManifest.build(tmp_path, catalog)
    else:
        state = GrokWorkQueue.build(tmp_path, catalog)
    assert canonical.read_bytes() == original_bytes
    assert state._store.rows() == original_rows
    assert state._store.schema.equals(schema, check_metadata=True)
    if kind == "catalog":
        state.register_alias(candidate, "photo.jpg", entry.content_sha256, "2026-07-30T00:00:00Z")
        state.flush()
    elif kind == "manifest":
        state.mark_in_progress(candidate, ".grok-partial/provenance.part")
    else:
        assert len(state.claim_for_resolution(1)) == 1
    assert read_media_sqlite_schema(canonical).equals(schema, check_metadata=True)
    assert state._store.get(key) != original_rows[0]
