"""Verify keyed media catalog updates retain evidence and the separate LLM format."""

# Code version: v1.0.0-codex.0

from __future__ import annotations

from dataclasses import asdict
import json
import sqlite3
from pathlib import Path

import pyarrow as pa
import pytest

from app.core.cache_catalog import CATALOG_FILENAME, LocalTweetCacheIndex
from app.core.chatgpt_downloader import (
    ChatGPTCatalogEntry,
    ChatGPTImageCandidate,
    ChatGPTImageCatalog,
)
from app.core.llm_sqlite import read_sqlite_rows, write_sqlite_rows
from app.core.media_sqlite import MediaSQLiteStore, read_media_sqlite_schema
from app.core.resource_persistence import (
    CHATGPT_CATALOG_FILENAME,
    CHATGPT_CATALOG_SCHEMA,
    CHATGPT_CATALOG_SCHEMA_VERSION,
    X_CACHE_CATALOG_SCHEMA,
    X_CACHE_CATALOG_SCHEMA_VERSION,
    write_resource_rows_atomic,
)


def _entry(identity: str, *, conversation: str | None = None) -> ChatGPTCatalogEntry:
    return ChatGPTCatalogEntry(
        file_id=identity,
        relative_path=f"{identity}.png",
        content_sha256=f"sha256-{identity}",
        content_bytes=16,
        source_url=f"https://chatgpt.com/backend-api/estuary/content?id={identity}",
        conversation_url=f"https://chatgpt.com/c/{conversation or identity}",
        alt_text=f"Original {identity}",
        width=1,
        height=1,
        first_seen_at="2026-10-01T00:00:00Z",
        last_seen_at="2026-10-02T00:00:00Z",
        prompt_markdown=f"Prompt for {identity}",
        conversation_title=f"Session {identity}",
        created_at="2026-09-01T00:00:00Z",
        visual_signature="0" * 64,
    )


def _image(root: Path, entry: ChatGPTCatalogEntry) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / entry.relative_path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 8)


def _row(entry: ChatGPTCatalogEntry) -> dict:
    return {"schema_version": CHATGPT_CATALOG_SCHEMA_VERSION, **asdict(entry)}


def _tweet(root: Path, identity: str) -> Path:
    directory = root / f"post-{identity}"
    directory.mkdir(parents=True)
    (directory / "image.png").write_bytes(b"media")
    return directory


def test_chatgpt_metadata_update_serializes_only_the_changed_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = {identity: _entry(identity) for identity in ("one", "two", "three")}
    catalog = ChatGPTImageCatalog(tmp_path, entries)
    catalog.save()
    before = catalog._store.get("two")
    serialized = []
    serialize = catalog._entry_row

    def tracked(entry):
        serialized.append(entry.file_id)
        return serialize(entry)

    monkeypatch.setattr(catalog, "_entry_row", tracked)
    monkeypatch.setattr(catalog._store, "replace_rows", lambda rows: pytest.fail("A metadata edit cannot replace the catalog."))
    monkeypatch.setattr(catalog._store, "rows", lambda: pytest.fail("A metadata edit cannot scan the store."))
    assert catalog.update_metadata_batch([
        ChatGPTImageCandidate(file_id="one", source_url="", conversation_url="", conversation_title="Renamed"),
    ]) == 1
    assert serialized == ["one"]
    assert catalog._store.get("one")["conversation_title"] == "Renamed"
    assert catalog._store.get("two") == before


def test_chatgpt_failed_delta_keeps_persisted_evidence_and_retries_pending_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry("one")
    catalog = ChatGPTImageCatalog(tmp_path, {entry.file_id: entry})
    catalog.save()
    before = catalog._store.get("one")
    candidate = ChatGPTImageCandidate(file_id="one", source_url="", conversation_url="", conversation_title="Changed")
    with monkeypatch.context() as scoped:
        def fail(**kwargs):
            raise OSError("Simulated durable-write failure")
        scoped.setattr(catalog._store, "apply_changes", fail)
        with pytest.raises(OSError, match="durable-write"):
            catalog.update_metadata(candidate)
    assert catalog._store.get("one") == before
    assert catalog._dirty_file_ids == {"one"}
    catalog.update_metadata(candidate)
    assert catalog._store.get("one")["conversation_title"] == "Changed"
    assert not catalog._dirty_file_ids


def test_chatgpt_readonly_legacy_load_and_first_delta_preserve_original_parquet(tmp_path: Path) -> None:
    original = _entry("one")
    untouched = _entry("two")
    legacy = (tmp_path / CHATGPT_CATALOG_FILENAME).with_suffix(".parquet")
    write_resource_rows_atomic(legacy, [_row(original), _row(untouched)], CHATGPT_CATALOG_SCHEMA)
    before = legacy.read_bytes()
    catalog = ChatGPTImageCatalog.build(tmp_path, repair=False)
    assert not catalog.catalog_path.exists()
    assert asdict(catalog.entries_by_file_id["two"]) == asdict(untouched)
    catalog.update_metadata(ChatGPTImageCandidate(file_id="one", source_url="", conversation_url="", conversation_title="New"))
    assert catalog.catalog_path.read_bytes().startswith(b"SQLite format 3\x00")
    assert legacy.read_bytes() == before
    assert catalog._store.get("two") == _row(untouched)


def test_chatgpt_cold_identity_lookup_uses_sqlite_key_without_row_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry("one")
    _image(tmp_path, entry)
    ChatGPTImageCatalog(tmp_path, {entry.file_id: entry}).save()
    cold = ChatGPTImageCatalog(tmp_path)
    monkeypatch.setattr(cold._store, "rows", lambda: pytest.fail("Point lookup must not scan the catalog."))
    assert cold.complete_entry("one") == entry


def test_chatgpt_visual_duplicate_replacement_commits_upsert_and_delete_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _entry("old", conversation="shared")
    _image(tmp_path, original)
    catalog = ChatGPTImageCatalog(tmp_path, {original.file_id: original})
    catalog.save()
    candidate = ChatGPTImageCandidate(
        file_id="new", source_url="https://chatgpt.com/content/new",
        conversation_url=original.conversation_url,
    )
    (tmp_path / "new.png").write_bytes(b"new-image")
    monkeypatch.setattr("app.core.chatgpt_downloader.analyze_image_payload", lambda payload: ("0" * 64, 2, 2))
    calls = []
    apply = catalog._store.apply_changes

    def tracked(*, rows=(), delete_keys=()):
        rows = list(rows)
        delete_keys = list(delete_keys)
        calls.append(([row["file_id"] for row in rows], delete_keys))
        return apply(rows=rows, delete_keys=delete_keys)

    monkeypatch.setattr(catalog._store, "apply_changes", tracked)
    assert catalog.register_download(candidate, "new.png", "new-sha", 9, "2026-11-05T00:00:00Z", image_payload=b"new-image")
    assert calls == [(["new"], ["old"])]
    assert catalog._store.get("old") is None
    assert catalog._store.get("new")["visual_signature"] == "0" * 64


def test_x_register_writes_only_its_directory_and_preserves_url_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "media" / "x"
    first = _tweet(root, "1")
    second = _tweet(root, "2")
    catalog = LocalTweetCacheIndex.build(root)
    catalog.register("https://twitter.com/example/status/1", first, {"title": "Original"})
    catalog.register("https://x.com/example/status/2", second, {"title": "Other"})
    before = catalog._store.get("post-2")
    calls = []
    apply = catalog._store.apply_changes

    def tracked(*, rows=(), delete_keys=()):
        rows = list(rows)
        calls.append([row["relative_tweet_dir"] for row in rows])
        return apply(rows=rows, delete_keys=delete_keys)

    monkeypatch.setattr(catalog._store, "apply_changes", tracked)
    monkeypatch.setattr(catalog._store, "replace_rows", lambda rows: pytest.fail("Post registration cannot replace all rows."))
    catalog.register("https://x.com/renamed/status/1", first, {"title": "Changed"})
    assert calls == [["post-1"]]
    assert catalog._store.get("post-2") == before
    assert catalog._store.get("post-1")["canonical_urls"] == [
        "https://x.com/example/status/1", "https://x.com/renamed/status/1",
    ]
    reloaded = LocalTweetCacheIndex.build(root)
    assert reloaded.lookup_directories("https://x.com/old-name/status/1") == {first}


def test_x_failed_delta_remains_retryable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "media" / "x"
    directory = _tweet(root, "1")
    catalog = LocalTweetCacheIndex.build(root)
    catalog.register("https://x.com/example/status/1", directory, {"title": "Before"})
    before = catalog._store.get("post-1")
    with monkeypatch.context() as scoped:
        def fail(**kwargs):
            raise OSError("Simulated SQLite failure")
        scoped.setattr(catalog._store, "apply_changes", fail)
        with pytest.raises(OSError, match="SQLite"):
            catalog.register("https://x.com/example/status/1", directory, {"title": "After"})
    assert catalog._store.get("post-1") == before
    assert catalog.dirty and catalog._dirty_directories == {directory}
    catalog.flush()
    assert catalog._store.get("post-1")["title"] == "After"
    assert not catalog.dirty


def test_x_legacy_json_list_columns_migrate_once_without_rewriting_parquet(tmp_path: Path) -> None:
    root = tmp_path / "media" / "x"
    directory = _tweet(root, "1")
    legacy = (root / CATALOG_FILENAME).with_suffix(".parquet")
    schema = pa.schema([
        pa.field("schema_version", pa.int16()), pa.field("relative_tweet_dir", pa.string()),
        pa.field("canonical_urls_json", pa.string()), pa.field("status_ids_json", pa.string()),
        pa.field("image_count", pa.int32()), pa.field("video_count", pa.int32()),
    ])
    write_resource_rows_atomic(legacy, [{
        "schema_version": 1, "relative_tweet_dir": "post-1",
        "canonical_urls_json": json.dumps(["https://x.com/example/status/1"]),
        "status_ids_json": json.dumps(["1"]), "image_count": 1, "video_count": 0,
    }], schema)
    before = legacy.read_bytes()
    catalog = LocalTweetCacheIndex.build(root)
    assert catalog.lookup_directories("https://x.com/new-name/status/1") == {directory}
    assert legacy.read_bytes() == before
    row = catalog._store.get("post-1")
    assert row["canonical_urls"] == ["https://x.com/example/status/1"]
    assert row["schema_version"] == X_CACHE_CATALOG_SCHEMA_VERSION


def test_x_directory_metadata_can_use_a_primary_key_on_a_cold_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "media" / "x"
    directory = _tweet(root, "1")
    catalog = LocalTweetCacheIndex.build(root)
    catalog.register("https://x.com/example/status/1", directory, {"title": "Retained"})
    cold = LocalTweetCacheIndex(root, root / CATALOG_FILENAME)
    monkeypatch.setattr(cold._store, "rows", lambda: pytest.fail("Metadata lookup must use its directory key."))
    assert cold.metadata_for_directory(directory)["title"] == "Retained"


def test_x_keyed_updates_preserve_imported_arrow_metadata_and_list_child_names(tmp_path: Path) -> None:
    root = tmp_path / "media" / "x"
    directory = _tweet(root, "1")
    schema = pa.schema([
        pa.field(field.name, pa.list_(pa.field("element", pa.string())), nullable=field.nullable)
        if field.name in {"canonical_urls", "status_ids"} else field
        for field in X_CACHE_CATALOG_SCHEMA
    ], metadata={b"source-contract": b"retained-test-metadata"})
    row = {field.name: "" for field in schema}
    row.update(
        schema_version=X_CACHE_CATALOG_SCHEMA_VERSION, relative_tweet_dir="post-1",
        canonical_urls=["https://x.com/example/status/1"], status_ids=["1"],
        image_count=1, video_count=0, timestamp=None,
    )
    legacy = (root / CATALOG_FILENAME).with_suffix(".parquet")
    write_resource_rows_atomic(legacy, [row], schema)
    catalog = LocalTweetCacheIndex.build(root)
    catalog.register("https://x.com/example/status/1", directory, {"title": "Changed"})
    assert read_media_sqlite_schema(catalog.catalog_path).equals(schema, check_metadata=True)
    assert catalog._store.get("post-1")["timestamp"] is None


def test_x_llm_root_catalog_keeps_its_existing_distinct_store_format(tmp_path: Path) -> None:
    root = tmp_path / "llm"
    directory = _tweet(root, "1")
    path = root / ".cache_catalog.sqlite3"
    row = {field.name: "" for field in X_CACHE_CATALOG_SCHEMA}
    row.update(
        schema_version=X_CACHE_CATALOG_SCHEMA_VERSION, relative_tweet_dir="post-1",
        canonical_urls=["https://x.com/example/status/1"], status_ids=["1"],
        image_count=1, video_count=0, timestamp=None,
    )
    write_sqlite_rows(path, [row], X_CACHE_CATALOG_SCHEMA)
    catalog = LocalTweetCacheIndex.build(root)
    assert catalog._store is None
    catalog.register("https://x.com/renamed/status/1", directory, {"title": "Kept"})
    persisted = read_sqlite_rows(path)
    assert persisted[0]["title"] == "Kept"
    assert persisted[0]["status_ids"] == ["1"]


def test_catalog_builds_fail_closed_on_corrupt_canonical_databases(tmp_path: Path) -> None:
    images = tmp_path / "images"
    images.mkdir()
    (images / CHATGPT_CATALOG_FILENAME).write_bytes(b"broken sqlite")
    with pytest.raises((sqlite3.Error, ValueError)):
        ChatGPTImageCatalog.build(images, repair=False)
    posts = tmp_path / "media" / "x"
    posts.mkdir(parents=True)
    (posts / CATALOG_FILENAME).write_bytes(b"broken sqlite")
    with pytest.raises((sqlite3.Error, ValueError)):
        LocalTweetCacheIndex.build(posts)


def test_missing_catalog_read_does_not_create_a_database(tmp_path: Path) -> None:
    path = tmp_path / CHATGPT_CATALOG_FILENAME
    store = MediaSQLiteStore(path, CHATGPT_CATALOG_SCHEMA, ("file_id",))
    assert store.get("missing") is None
    assert store.rows() == []
    assert not path.exists()
