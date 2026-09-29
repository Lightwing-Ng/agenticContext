"""Gemini media discovery and durable removal regression tests.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.local_media_browser import LocalMediaCatalog, normalize_browser_filters


def _write_catalog(root: Path, entries: list[dict[str, object]]) -> None:
    media_root = root / "media" / "gemini"
    media_root.mkdir(parents=True, exist_ok=True)
    (media_root / "catalog.json").write_text(
        json.dumps({"schema_version": 1, "assets": entries}), encoding="utf-8"
    )


def _write_image(root: Path, asset_id: str = "abc") -> dict[str, object]:
    media_root = root / "media" / "gemini"
    media_root.mkdir(parents=True, exist_ok=True)
    filename = f"img_{asset_id}.png"
    content = b"verified-gemini-image"
    (media_root / filename).write_bytes(content)
    return {
        "asset_id": asset_id,
        "relative_path": filename,
        "media_kind": "image",
        "conversation_url": "https://gemini.google.com/app/session-1",
        "conversation_title": "Gemini landscape session",
        "alt_text": "A mountain landscape",
        "cached_at": "2026-09-29T00:00:00Z",
        "content_bytes": len(content),
    }


@pytest.mark.parametrize("view", ["text", "media", "prompts"])
def test_gemini_remains_selected_in_each_supported_view(view: str) -> None:
    filters = normalize_browser_filters(source=" Gemini ", view=view)

    assert filters["source"] == "gemini"
    assert not filters["session_index"]


def test_gemini_catalog_metadata_is_searchable_and_filters_other_media(tmp_path: Path) -> None:
    root = tmp_path / "local_store"
    entry = _write_image(root)
    _write_catalog(root, [entry, entry])
    (root / "media" / "gemini" / "uncatalogued.png").write_bytes(b"uncatalogued")
    other_media = root / "x" / "example" / "post" / "photo.jpg"
    other_media.parent.mkdir(parents=True)
    other_media.write_bytes(b"other-provider")

    catalog = LocalMediaCatalog(root)
    page = catalog.query(source="gemini", query="MOUNTAIN session", force_refresh=True)

    assert page.total_count == 1
    assert page.image_count == 1
    assert page.video_count == 0
    assert page.pagination_unit == "media"
    item = page.items[0]
    assert item.source == "gemini"
    assert item.resource_key == "abc"
    assert item.title == "A mountain landscape"
    assert item.description == "Gemini landscape session"
    assert item.creator == "Gemini"
    assert item.source_url == "https://gemini.google.com/app/session-1"
    assert item.relative_path == "media/gemini/img_abc.png"
    assert catalog.query(source="gemini", media_kind="video").total_count == 0
    assert catalog.query(source="gemini", query="missing").total_count == 0
    assert catalog.query(source="all").total_count == 2


@pytest.mark.parametrize(
    "override",
    [
        {"relative_path": "../outside.png"},
        {"relative_path": "%2e%2e/outside.png"},
        {"relative_path": ".hidden.png"},
        {"relative_path": "missing.png"},
        {"content_bytes": 1},
        {"media_kind": "video"},
        {"conversation_url": "https://example.com/app/session-1"},
        {"conversation_url": "https://gemini.google.com@evil.example/app/session-1"},
        {"conversation_url": "http://gemini.google.com/app/session-1"},
        {"conversation_url": "https://gemini.google.com/settings"},
        {"conversation_url": "https://[invalid/app/session-1"},
    ],
)
def test_gemini_catalog_skips_invalid_records(
    tmp_path: Path, override: dict[str, object]
) -> None:
    root = tmp_path / "local_store"
    entry = _write_image(root)
    invalid = {**entry, "asset_id": "invalid", **override}
    _write_catalog(root, [invalid, entry])

    items = LocalMediaCatalog(root).query(source="gemini", force_refresh=True).items

    assert len(items) == 1
    assert items[0].resource_key == "abc"
    assert items[0].source_url == "https://gemini.google.com/app/session-1"


def test_gemini_catalog_does_not_follow_media_links_outside_store(tmp_path: Path) -> None:
    root = tmp_path / "local_store"
    entry = _write_image(root)
    external = tmp_path / "outside.png"
    external.write_bytes(b"verified-gemini-image")
    media_root = root / "media" / "gemini"
    (media_root / "linked.png").symlink_to(external)
    _write_catalog(root, [{**entry, "relative_path": "linked.png"}])

    assert LocalMediaCatalog(root).query(source="gemini", force_refresh=True).total_count == 0


def test_gemini_deletion_persists_asset_exclusion_and_restore_clears_it(tmp_path: Path) -> None:
    root = tmp_path / "local_store"
    entry = _write_image(root)
    _write_catalog(root, [entry])
    catalog = LocalMediaCatalog(root)
    item = catalog.query(source="gemini", force_refresh=True).items[0]

    deleted = catalog.delete(item.stable_id)
    reloaded = LocalMediaCatalog(root)
    deleted_page = reloaded.query(source="gemini", force_refresh=True)

    assert deleted.is_deleted
    assert deleted_page.total_count == 1
    assert deleted_page.items[0].is_deleted
    assert deleted_page.items[0].source_url == item.source_url
    assert reloaded.is_excluded("gemini", "abc")
    assert not reloaded.is_excluded("claude", "abc")
    assert not (root / item.relative_path).exists()
    assert reloaded.deleted_preview_path(item.stable_id).read_bytes() == b"verified-gemini-image"

    restored = reloaded.restore(item.stable_id)

    assert not restored.is_deleted
    assert not reloaded.is_excluded("gemini", "abc")
    assert (root / item.relative_path).read_bytes() == b"verified-gemini-image"
    assert reloaded.query(source="gemini", force_refresh=True).items[0] == item
