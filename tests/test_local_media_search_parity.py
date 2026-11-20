"""Characterization tests for media search and snapshot isolation.

Code version: v1.0.0-codex.0
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from sys import getsizeof
from threading import Event

import pytest

from app.core import local_media_browser as media


def _item(name, description, *, source="x", stable_id="media-synthetic"):
    return media.LocalMediaItem(
        stable_id=stable_id,
        source=source,
        media_kind="image",
        relative_path=f"media/{source}/{name}.png",
        filename=f"{name}.png",
        title="Straße Σίσυφος",
        description=description,
        creator="Orion",
        source_url="",
        captured_at="2026-11-01T00:00:00Z",
        captured_at_label="1 Nov 2026",
        content_bytes=12,
        project_name="Synthetic",
        prompt_markdown="Cafe\u0301 中文🙂 \ud800",
    )


def test_catalog_search_reuses_results_without_changing_item_identity(tmp_path, monkeypatch):
    second = _item("zebra", "needle amber")
    first = _item("apple", "NEEDLE amber")
    other = _item("banana", "needle bronze")
    catalog = media.LocalMediaCatalog(tmp_path)
    monkeypatch.setattr(catalog, "_build_snapshot", lambda: (second, first, other))

    query = "  STRASSE\u00a0σίσυφοσ\u3000中文🙂 amber \ud800  "
    for _ in range(2):
        page = catalog.query(query=query, sort="name")
        assert page.total_count == 2
        assert page.items[0] is first
        assert page.items[1] is second

    assert catalog.query(query="needle bronze", sort="name").items[0] is other
    assert catalog.query(query="café").items == ()
    assert catalog.query(query="needle" + " " * 114 + "ignored-suffix").total_count == 3


def test_filter_preserves_order_and_checks_only_eligible_search_fields():
    first = _item("zebra", "needle amber")
    second = _item("apple", "needle amber")
    malformed = _item("malformed", 17, source="grok", stable_id="media-malformed")

    filtered = media.filter_media_items(iter((first, malformed, second)), source="x", query="needle amber")
    assert len(filtered) == 2
    assert filtered[0] is first
    assert filtered[1] is second
    assert media.filter_media_items((malformed,), media_id="media-other", query="needle") == ()
    with pytest.raises(TypeError, match="sequence item 2"):
        media.filter_media_items((malformed,), query="needle")


def test_catalog_search_preserves_custom_query_coercion(tmp_path, monkeypatch):
    truthiness_checks = []

    class Query(str):
        def __bool__(self):
            truthiness_checks.append(self)
            return True

        def __str__(self):
            return self

        def strip(self):
            return self

        def __getitem__(self, index):
            return self

    item = _item("result", "needle")
    catalog = media.LocalMediaCatalog(tmp_path)
    monkeypatch.setattr(catalog, "_build_snapshot", lambda: (item,))

    assert catalog.query(query=Query("needle")).items[0] is item
    assert truthiness_checks == ["needle", "needle"]


@pytest.mark.parametrize("refresh", ["force", "invalidate", "expiry"])
def test_catalog_search_respects_replacement_snapshot(tmp_path, monkeypatch, refresh):
    old = _item("same", "older phrase")
    new = replace(old, description="newer phrase")
    snapshots = iter(((old,), (new,)))
    now = 10.0
    monkeypatch.setattr(media, "monotonic", lambda: now)
    catalog = media.LocalMediaCatalog(tmp_path, ttl_seconds=5)
    monkeypatch.setattr(catalog, "_build_snapshot", lambda: next(snapshots))

    assert catalog.query(query="older").items[0] is old
    assert catalog.query(query="older").items[0] is old
    if refresh == "invalidate":
        catalog.invalidate()
    if refresh == "expiry":
        now += 5

    assert catalog.query(query="older", force_refresh=refresh == "force").items == ()
    assert catalog.query(query="newer").items[0] is new


def test_in_flight_query_keeps_its_snapshot_during_refresh(tmp_path, monkeypatch):
    old = _item("same", "older phrase")
    new = replace(old, description="newer phrase")
    snapshots = iter(((old,), (new,)))
    catalog = media.LocalMediaCatalog(tmp_path, ttl_seconds=60)
    monkeypatch.setattr(catalog, "_build_snapshot", lambda: next(snapshots))
    assert catalog.query(query="older").items[0] is old

    acquired_old = Event()
    release_old = Event()
    snapshot = catalog.snapshot

    def pause_old_query(force_refresh=False):
        items = snapshot(force_refresh=force_refresh)
        if items[0] is old:
            acquired_old.set()
            assert release_old.wait(5)
        return items

    monkeypatch.setattr(catalog, "snapshot", pause_old_query)
    with ThreadPoolExecutor(max_workers=1) as workers:
        in_flight = workers.submit(catalog.query, query="older")
        try:
            assert acquired_old.wait(5)
            assert catalog.query(query="newer", force_refresh=True).items[0] is new
        finally:
            release_old.set()
        assert in_flight.result(timeout=5).items[0] is old

    assert catalog.query(query="older").items == ()
    assert catalog.query(query="newer").items[0] is new


@pytest.mark.parametrize("limit", ["text bytes", "item count"])
def test_search_cache_preserves_results_when_full(tmp_path, monkeypatch, limit):
    oversized = _item("large", "needle " * 512)
    fits = _item("small", "needle")
    uncached = _item("other", "needle")
    search_text = media._searchable_media_text
    if limit == "text bytes":
        monkeypatch.setattr(media, "_MEDIA_SEARCH_CACHE_MAX_TEXT_BYTES", getsizeof(search_text(fits)))
        items = oversized, fits, uncached
    else:
        monkeypatch.setattr(media, "_MEDIA_SEARCH_CACHE_MAX_ITEMS", 1)
        items = fits, uncached

    searched = []

    def record(item):
        searched.append(item)
        return search_text(item)

    monkeypatch.setattr(media, "_searchable_media_text", record)
    catalog = media.LocalMediaCatalog(tmp_path)
    monkeypatch.setattr(catalog, "_build_snapshot", lambda: items)

    first = catalog.query(query="needle", sort="name")
    assert first.total_count == len(items)
    assert searched == list(items)
    searched.clear()

    second = catalog.query(query="needle", sort="name")
    assert all(a is b for a, b in zip(first.items, second.items, strict=True))
    assert searched == [item for item in items if item is not fits]
