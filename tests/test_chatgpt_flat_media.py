"""Read-only all-session media expansion and exact-content deduplication.

Code version: v1.1.2-codex.0
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
from threading import Event, Lock, current_thread
from time import monotonic
from types import SimpleNamespace

import pytest

from app.core.local_media_browser import LocalMediaCatalog, normalize_browser_filters
from app.core import local_media_browser


def _write_project(
    root: Path,
    records: tuple[tuple[str, str, bytes], ...],
    *,
    project: str = "demo",
    catalog_hashes: dict[str, str] | None = None,
) -> Path:
    """Write independent files with deterministic catalog times and session identities."""
    directory = root / "media" / "chatgpt" / project
    directory.mkdir(parents=True, exist_ok=True)
    entries = {}
    for index, (filename, session, content) in enumerate(records, start=1):
        (directory / filename).write_bytes(content)
        entry = {
            "file_id": f"file-{filename}",
            "relative_path": filename,
            "conversation_url": f"https://chatgpt.com/c/{session}",
            "conversation_title": f"Session {session}",
            "created_at": f"2026-08-01T00:{index // 60:02d}:{index % 60:02d}Z",
        }
        if catalog_hashes is not None and filename in catalog_hashes:
            entry["content_sha256"] = catalog_hashes[filename]
        entries[filename] = entry
    (directory / ".chatgpt_catalog.json").write_text(
        json.dumps({"entries": entries}), encoding="utf-8",
    )
    return directory


@pytest.mark.parametrize(
    ("view", "source", "session_index", "media_view", "expected"),
    (
        ("media", "chatgpt", "1", "all", "all"),
        ("media", "chatgpt", "TRUE", " ALL ", "all"),
        ("media", "chatgpt", "on", "all", "all"),
        ("media", "chatgpt", None, "all", ""),
        ("media", "chatgpt", "0", "all", ""),
        ("media", "chatgpt", "1", "grid", ""),
        ("media", "chatgpt", "1", None, ""),
        ("media", "all", "1", "all", ""),
        ("media", "x", "1", "all", ""),
        ("text", "chatgpt", "1", "all", ""),
        ("prompts", "chatgpt", "1", "all", ""),
    ),
)
def test_all_media_filter_is_scoped_to_chatgpt_session_index(
    view: str,
    source: str,
    session_index: str | None,
    media_view: str | None,
    expected: str,
) -> None:
    filters = normalize_browser_filters(
        view=view, source=source, session_index=session_index, media_view=media_view,
    )
    assert filters["media_view"] == expected


def test_all_media_expands_every_work_without_changing_existing_session_modes(tmp_path: Path) -> None:
    _write_project(tmp_path, (
        ("alpha-old.png", "alpha", b"alpha-old"),
        ("alpha-new.png", "alpha", b"alpha-new"),
        ("beta.png", "beta", b"beta"),
    ))
    catalog = LocalMediaCatalog(tmp_path)

    expanded = catalog.query(source="chatgpt", chatgpt_session_index=True, chatgpt_all_media=True)
    index = catalog.query(source="chatgpt", chatgpt_session_index=True)
    detail = catalog.query(source="chatgpt", chatgpt_session_key="alpha")

    assert expanded.pagination_unit == "media"
    assert expanded.session_count == 2
    assert expanded.total_count == expanded.image_count == 3
    assert [item.filename for item in expanded.items] == ["beta.png", "alpha-new.png", "alpha-old.png"]
    assert expanded.sessions == ()
    assert index.pagination_unit == "session_index"
    assert len(index.items) == 2
    assert detail.pagination_unit == "session"
    assert {item.filename for item in detail.items} == {"alpha-old.png", "alpha-new.png"}


def test_all_media_deduplicates_actual_bytes_across_sessions_and_projects(tmp_path: Path) -> None:
    _write_project(tmp_path, (
        ("one.png", "alpha", b"same-content"),
        ("renamed.png", "beta", b"same-content"),
        ("distinct.png", "beta", b"distinct-content"),
    ))
    _write_project(tmp_path, (("another.png", "gamma", b"same-content"),), project="other")
    catalog = LocalMediaCatalog(tmp_path)

    expanded = catalog.query(source="chatgpt", chatgpt_all_media=True)

    assert expanded.session_count == 3
    assert expanded.total_count == 2
    assert {item.filename for item in expanded.items} == {"one.png", "distinct.png"}
    assert len(catalog.snapshot()) == 4
    assert len(catalog.query(source="chatgpt", chatgpt_session_view=False).items) == 4


def test_actual_bytes_override_missing_invalid_and_stale_catalog_hashes(tmp_path: Path) -> None:
    fake_digest = "a" * 64
    _write_project(tmp_path, (
        ("missing.png", "alpha", b"duplicate"),
        ("invalid.png", "beta", b"duplicate"),
        ("stale.png", "gamma", b"first-distinct"),
        ("stale-other.png", "delta", b"other-distinct"),
    ), catalog_hashes={"invalid.png": "invalid", "stale.png": fake_digest, "stale-other.png": fake_digest})

    expanded = LocalMediaCatalog(tmp_path).query(source="chatgpt", chatgpt_all_media=True)

    assert expanded.session_count == 4
    assert expanded.total_count == 3
    assert {item.filename for item in expanded.items} == {"invalid.png", "stale.png", "stale-other.png"}


def test_deduplication_precedes_absolute_ordering_and_pagination(tmp_path: Path) -> None:
    records = tuple(
        (f"work-{index:02d}.png", f"session-{index % 3}", f"unique-{index}".encode())
        for index in range(25)
    ) + (("duplicate.png", "other-session", b"unique-24"),)
    _write_project(tmp_path, records)
    catalog = LocalMediaCatalog(tmp_path)

    first = catalog.query(source="chatgpt", chatgpt_all_media=True)
    last = catalog.query(source="chatgpt", chatgpt_all_media=True, page=99)
    oldest = catalog.query(source="chatgpt", chatgpt_all_media=True, sort="oldest")
    by_name = catalog.query(source="chatgpt", chatgpt_all_media=True, sort="name")

    assert first.total_count == 25
    assert first.total_pages == 2
    assert first.session_count == 4
    assert len(first.items) == 24
    assert last.current_page == 2
    assert len(last.items) == 1
    assert {item.stable_id for item in first.items}.isdisjoint(item.stable_id for item in last.items)
    assert len({item.stable_id for item in (*first.items, *last.items)}) == 25
    assert oldest.items[0].filename == "work-00.png"
    assert by_name.items[0].filename == "duplicate.png"


def test_search_and_kind_filters_are_applied_before_deduplication(tmp_path: Path) -> None:
    _write_project(tmp_path, (
        ("first.png", "alpha", b"duplicate"),
        ("matching.png", "beta", b"duplicate"),
        ("movie.mp4", "beta", b"video"),
    ))
    catalog = LocalMediaCatalog(tmp_path)

    search = catalog.query(source="chatgpt", chatgpt_all_media=True, query="matching")
    videos = catalog.query(source="chatgpt", chatgpt_all_media=True, media_kind="video")
    empty = catalog.query(source="chatgpt", chatgpt_all_media=True, query="absent", page=99)

    assert [item.filename for item in search.items] == ["matching.png"]
    assert search.session_count == 1
    assert videos.total_count == videos.video_count == 1
    assert videos.image_count == 0
    assert videos.items[0].filename == "movie.mp4"
    assert (empty.items, empty.session_count, empty.current_page, empty.total_pages) == ((), 0, 1, 1)


def test_active_copy_wins_over_identical_deleted_media_without_removing_tombstone(tmp_path: Path) -> None:
    _write_project(tmp_path, (
        ("a-deleted.png", "alpha", b"duplicate"),
        ("z-active.png", "beta", b"duplicate"),
        ("deleted-only.png", "gamma", b"only-deleted"),
    ))
    catalog = LocalMediaCatalog(tmp_path)
    originals = {item.filename: item for item in catalog.snapshot()}
    for filename in ("a-deleted.png", "deleted-only.png"):
        catalog.delete(originals[filename].stable_id)
    before = tuple(catalog.snapshot())

    expanded = catalog.query(source="chatgpt", chatgpt_all_media=True)

    assert expanded.session_count == 3
    assert expanded.total_count == 2
    assert {item.filename for item in expanded.items} == {"z-active.png", "deleted-only.png"}
    assert next(item for item in expanded.items if item.filename == "z-active.png").is_deleted is False
    assert next(item for item in expanded.items if item.filename == "deleted-only.png").is_deleted is True
    assert catalog.snapshot() == before
    assert len(catalog._deletion_catalog.deleted_items()) == 2


def test_unknown_or_unreadable_content_is_preserved_individually(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _write_project(tmp_path, (("readable.png", "alpha", b"readable"),))
    catalog = LocalMediaCatalog(tmp_path)
    original = catalog.snapshot()[0]
    missing = replace(
        original, stable_id="missing", relative_path="media/chatgpt/demo/missing.png", filename="missing.png",
    )
    unreadable = replace(
        original, stable_id="unreadable", relative_path="media/chatgpt/demo/unreadable.png", filename="unreadable.png",
    )
    (directory / "unreadable.png").write_bytes(b"readable")
    original_open = Path.open

    def guarded_open(path: Path, *args, **kwargs):
        if path.name == "unreadable.png":
            raise PermissionError("Synthetic read failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(catalog, "snapshot", lambda force_refresh=False: (original, missing, unreadable))
    monkeypatch.setattr(Path, "open", guarded_open)

    expanded = catalog.query(source="chatgpt", chatgpt_all_media=True)

    assert expanded.total_count == 3
    assert {item.stable_id for item in expanded.items} == {original.stable_id, "missing", "unreadable"}


def test_hash_cache_reuses_unchanged_bytes_and_invalidates_modified_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _write_project(tmp_path, (
        ("one.png", "alpha", b"same"),
        ("two.png", "beta", b"same"),
    ))
    catalog = LocalMediaCatalog(tmp_path)
    real_file_digest = hashlib.file_digest
    reads = []

    def observed_digest(handle, algorithm):
        reads.append(handle.name)
        return real_file_digest(handle, algorithm)

    monkeypatch.setattr(hashlib, "file_digest", observed_digest)
    assert catalog.query(source="chatgpt", chatgpt_all_media=True).total_count == 1
    assert len(reads) == 2
    assert catalog.query(source="chatgpt", chatgpt_all_media=True).total_count == 1
    assert len(reads) == 2

    modified_path = directory / "two.png"
    old_stat = modified_path.stat()
    modified_path.write_bytes(b"else")
    os.utime(modified_path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))

    assert catalog.query(source="chatgpt", chatgpt_all_media=True).total_count == 2
    assert len(reads) == 3


def test_all_media_does_not_change_other_sources_or_write_cached_records(tmp_path: Path) -> None:
    directory = _write_project(tmp_path, (
        ("one.png", "alpha", b"same"),
        ("two.png", "beta", b"same"),
    ))
    catalog = LocalMediaCatalog(tmp_path)
    before = {path: path.read_bytes() for path in directory.iterdir()}

    expanded = catalog.query(source="chatgpt", chatgpt_all_media=True)
    all_sources = catalog.query(source="all", chatgpt_all_media=True)

    assert expanded.total_count == 1
    assert all_sources.total_count == 2
    assert {path: path.read_bytes() for path in directory.iterdir()} == before


def test_unique_actual_sizes_need_no_content_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_project(tmp_path, (
        ("small.png", "alpha", b"a"),
        ("medium.png", "beta", b"ab"),
        ("large.png", "gamma", b"abc"),
    ))
    reads = []

    def unexpected_digest(handle, algorithm):
        reads.append(handle.name)
        raise AssertionError("Distinct actual lengths cannot be exact duplicates")

    monkeypatch.setattr(hashlib, "file_digest", unexpected_digest)

    expanded = LocalMediaCatalog(tmp_path).query(source="chatgpt", chatgpt_all_media=True)

    assert expanded.total_count == 3
    assert reads == []


def test_known_dataless_files_do_not_trigger_content_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_project(tmp_path, (
        ("cloud.png", "alpha", b"same"),
        ("local.png", "beta", b"same"),
    ))
    original_stat = Path.stat
    original_open = Path.open
    reads = []
    dataless_flag = 0x40000000

    def flagged_stat(path: Path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if path.name != "cloud.png":
            return result
        values = {name: getattr(result, name) for name in dir(result) if name.startswith("st_")}
        values["st_flags"] = dataless_flag
        return SimpleNamespace(**values)

    def observed_open(path: Path, *args, **kwargs):
        if path.suffix == ".png":
            reads.append(path.name)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(local_media_browser.stat, "SF_DATALESS", dataless_flag, raising=False)
    monkeypatch.setattr(Path, "stat", flagged_stat)
    monkeypatch.setattr(Path, "open", observed_open)

    expanded = LocalMediaCatalog(tmp_path).query(source="chatgpt", chatgpt_all_media=True)

    assert expanded.total_count == 2
    assert reads == []


def test_blocked_reads_have_one_total_budget_and_no_duplicate_pending_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_project(tmp_path, tuple((f"image-{index}.png", str(index), b"same") for index in range(6)))
    catalog = LocalMediaCatalog(tmp_path)
    real_file_digest = hashlib.file_digest
    release = Event()
    calls = []
    calls_lock = Lock()

    def blocked_digest(handle, algorithm):
        with calls_lock:
            calls.append((handle.name, current_thread().daemon))
        release.wait(2)
        return real_file_digest(handle, algorithm)

    monkeypatch.setattr(hashlib, "file_digest", blocked_digest)
    monkeypatch.setattr(local_media_browser, "CONTENT_HASH_WAIT_SECONDS", 0.05)
    try:
        started = monotonic()
        expanded = catalog.query(source="chatgpt", chatgpt_all_media=True)
        assert monotonic() - started < 0.5
        assert expanded.total_count == 6
        assert len(calls) == 4
        assert all(is_daemon for _name, is_daemon in calls)

        again = catalog.query(source="chatgpt", chatgpt_all_media=True)
        assert again.total_count == 6
        assert len(calls) == 4
        assert len(catalog._content_hash_pending) == 6
    finally:
        release.set()
        with catalog._content_hash_condition:
            catalog._content_hash_condition.wait_for(lambda: not catalog._content_hash_pending, timeout=2)

    assert not catalog._content_hash_pending
    assert len(calls) == 6
    assert catalog.query(source="chatgpt", chatgpt_all_media=True).total_count == 1


def test_blocked_tombstones_leave_a_reader_for_new_active_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _write_project(tmp_path, tuple(
        (f"deleted-{index}.png", str(index), b"same") for index in range(4)
    ))
    catalog = LocalMediaCatalog(tmp_path)
    for item in catalog.snapshot():
        catalog.delete(item.stable_id)
    real_file_digest = hashlib.file_digest
    release = Event()
    deleted_reads = []

    def blocked_deleted_digest(handle, algorithm):
        if ".browser-trash" in Path(handle.name).parts:
            deleted_reads.append(handle.name)
            release.wait(2)
        return real_file_digest(handle, algorithm)

    monkeypatch.setattr(hashlib, "file_digest", blocked_deleted_digest)
    monkeypatch.setattr(local_media_browser, "CONTENT_HASH_WAIT_SECONDS", 0.05)
    try:
        assert catalog.query(source="chatgpt", chatgpt_all_media=True).total_count == 4
        assert len(deleted_reads) == 3
        (directory / "new-one.png").write_bytes(b"same")
        (directory / "new-two.png").write_bytes(b"same")

        expanded = catalog.query(source="chatgpt", chatgpt_all_media=True, force_refresh=True)

        assert len([item for item in expanded.items if not item.is_deleted]) == 1
        assert expanded.total_count == 5
        assert len(deleted_reads) == 3
    finally:
        release.set()
        with catalog._content_hash_condition:
            catalog._content_hash_condition.wait_for(lambda: not catalog._content_hash_pending, timeout=2)

    assert not catalog._content_hash_pending
    assert catalog.query(source="chatgpt", chatgpt_all_media=True).total_count == 1


def test_pending_cap_reserves_admission_for_active_files_before_tombstones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _write_project(tmp_path, tuple(
        (f"a-deleted-{index}.png", str(index), b"same") for index in range(6)
    ))
    catalog = LocalMediaCatalog(tmp_path)
    for item in catalog.snapshot():
        catalog.delete(item.stable_id)
    (directory / "z-active-one.png").write_bytes(b"same")
    (directory / "z-active-two.png").write_bytes(b"same")
    catalog.snapshot(force_refresh=True)
    real_file_digest = hashlib.file_digest
    release = Event()
    deleted_reads = []

    def blocked_deleted_digest(handle, algorithm):
        if ".browser-trash" in Path(handle.name).parts:
            deleted_reads.append(handle.name)
            release.wait(2)
        return real_file_digest(handle, algorithm)

    monkeypatch.setattr(hashlib, "file_digest", blocked_deleted_digest)
    monkeypatch.setattr(local_media_browser, "CONTENT_HASH_MAX_PENDING", 4)
    monkeypatch.setattr(local_media_browser, "CONTENT_HASH_WAIT_SECONDS", 0.05)
    try:
        expanded = catalog.query(source="chatgpt", chatgpt_all_media=True)

        # Tombstones precede active paths in the snapshot and exceed the admission cap.
        # Both healthy active copies still enter the queue first and collapse to one.
        assert len([item for item in expanded.items if not item.is_deleted]) == 1
        assert len([item for item in expanded.items if item.is_deleted]) == 6
        assert len(deleted_reads) == 2
    finally:
        release.set()
        with catalog._content_hash_condition:
            catalog._content_hash_condition.wait_for(lambda: not catalog._content_hash_pending, timeout=2)

    assert not catalog._content_hash_pending


def test_blocked_tombstone_admission_keeps_capacity_for_later_active_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _write_project(tmp_path, tuple(
        (f"deleted-{index}.png", str(index), b"same") for index in range(4)
    ))
    catalog = LocalMediaCatalog(tmp_path)
    for item in catalog.snapshot():
        catalog.delete(item.stable_id)
    real_file_digest = hashlib.file_digest
    release = Event()

    def blocked_deleted_digest(handle, algorithm):
        if ".browser-trash" in Path(handle.name).parts:
            release.wait(2)
        return real_file_digest(handle, algorithm)

    monkeypatch.setattr(hashlib, "file_digest", blocked_deleted_digest)
    monkeypatch.setattr(local_media_browser, "CONTENT_HASH_MAX_PENDING", 4)
    monkeypatch.setattr(local_media_browser, "CONTENT_HASH_WAIT_SECONDS", 0.05)
    try:
        # First fill the deleted-media allowance while no active files are present.
        assert catalog.query(source="chatgpt", chatgpt_all_media=True).total_count == 4
        (directory / "later-one.png").write_bytes(b"same")
        (directory / "later-two.png").write_bytes(b"same")

        expanded = catalog.query(source="chatgpt", chatgpt_all_media=True, force_refresh=True)

        # New active work must enter even when the previous tombstone reads remain blocked.
        assert len([item for item in expanded.items if not item.is_deleted]) == 1
        assert len([item for item in expanded.items if item.is_deleted]) == 4
        assert expanded.total_count == 5
    finally:
        release.set()
        with catalog._content_hash_condition:
            catalog._content_hash_condition.wait_for(lambda: not catalog._content_hash_pending, timeout=2)

    assert not catalog._content_hash_pending
