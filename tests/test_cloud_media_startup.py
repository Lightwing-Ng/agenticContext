"""Keep application startup and passive Grok reads independent of media downloads.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from pathlib import Path

from app.core import grok_downloader
from app.core.resource_persistence import GROK_CATALOG_FILENAME
from app.web import app as web_app


def test_startup_and_activity_do_not_hydrate_cache_inventories(monkeypatch, tmp_path) -> None:
    """An unavailable provider inventory must not prevent the server from responding."""
    def unavailable_inventory(*_args, **_kwargs):
        raise AssertionError("Startup attempted to read an optional cache inventory.")

    for name in vars(web_app):
        if name.startswith("build_") and name.endswith("_snapshot"):
            monkeypatch.setattr(web_app, name, unavailable_inventory)

    application = web_app.create_app(
        tmp_path / "local_store", agent_external_operations_enabled=False,
    )
    client = application.test_client()

    assert client.get("/").status_code == 302
    response = client.get("/api/cache/activity")
    assert response.status_code == 200
    assert response.get_json()["tasks"] == []


def test_passive_grok_snapshot_does_not_rebuild_from_cloud_media(monkeypatch, tmp_path) -> None:
    """An absent catalog is pending verification, not a reason to open every image."""
    media_path = tmp_path / "img_20260421T050037Z_8e5b4bee-7245-4440-b511-8aa73e42ce3f_image.jpg"
    media_path.write_bytes(b"\xff\xd8\xff\xe0cloud-only-image")

    def unavailable_media(*_args, **_kwargs):
        raise AssertionError("A passive status read attempted to download media.")

    monkeypatch.setattr(grok_downloader, "validate_media_file", unavailable_media)
    monkeypatch.setattr(grok_downloader, "compute_file_sha256", unavailable_media)
    snapshot = grok_downloader.build_grok_initial_snapshot("test", tmp_path)

    assert snapshot.performance_metrics["cache_inventory_pending"] is True
    assert snapshot.downloaded_images == 1
    assert snapshot.downloaded_posts == 1
    assert "next sync" in snapshot.message
    assert not (tmp_path / GROK_CATALOG_FILENAME).exists()
    assert media_path.exists()


def test_passive_grok_snapshot_uses_saved_counts_without_opening_media(monkeypatch, tmp_path) -> None:
    """Catalog-backed counters survive offloading without media reads or catalog writes."""
    media_path = tmp_path / "image.jpg"
    media_path.write_bytes(b"cloud-only-image")
    candidate = grok_downloader.GrokMediaCandidate(
        source_url="https://example.test/image.jpg",
        asset_id="asset",
        asset_name="image",
        media_kind="image",
        identity="asset/image",
    )
    catalog = grok_downloader.GrokMediaCatalog(tmp_path)
    catalog.register_download(candidate, media_path.name, "saved-hash", media_path.stat().st_size)
    catalog.flush()
    catalog_path = tmp_path / GROK_CATALOG_FILENAME
    original_catalog = catalog_path.read_bytes()
    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if path == media_path:
            raise AssertionError("A passive status read opened cloud-only media.")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    snapshot = grok_downloader.build_grok_initial_snapshot("test", tmp_path)

    assert snapshot.downloaded_images == 1
    assert snapshot.downloaded_tweets == 1
    assert not snapshot.performance_metrics.get("cache_inventory_pending")
    assert catalog_path.read_bytes() == original_catalog
