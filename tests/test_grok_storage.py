"""Tests for Grok naming, validation, and durable work-queue state.

Code version: v1.1.1-codex.1
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.grok_downloader import (
    GrokMediaCandidate,
    GrokWorkQueue,
    classify_media_kind,
    compute_sha256,
    infer_extension,
    normalize_asset_name,
    normalize_catalog_timestamp,
    sanitize_filename_part,
    validate_media_file,
)
from app.core.resource_persistence import GROK_WORK_QUEUE_FILENAME, read_parquet_rows


class _EmptyCatalog:
    """Minimal catalog boundary used to exercise queue persistence only."""

    def has_recorded_asset_id(self, _asset_id: str) -> bool:
        return False


def _candidate(asset_id: str = "asset-1") -> GrokMediaCandidate:
    return GrokMediaCandidate(
        source_url="https://files.example.test/download/photo",
        asset_id=asset_id,
        asset_name="photo.jpg",
        media_kind="image",
        identity=f"{asset_id}/photo.jpg",
        preview_url="https://files.example.test/preview/photo.jpg",
        expected_bytes=9,
        created_at="2026-07-29T00:00:00Z",
    )


def test_grok_filename_and_content_helpers_normalize_untrusted_inputs() -> None:
    assert normalize_asset_name("  folder\\photo?.jpg  ") == "folder-photo"
    assert sanitize_filename_part("  ../bad:name  ") == "bad-name"
    assert classify_media_kind("clip.mp4", "") == "video"
    assert classify_media_kind("unknown", "video") == "video"
    assert infer_extension("https://example.test/file", "image/png; charset=binary", "image") == ".png"
    assert infer_extension("https://example.test/file.mov", "", "video") == ".mov"
    assert compute_sha256(b"content") == "ed7002b439e9ac845f22357d822bac1444730fbdb6016d3ec9432297b9ec9f73"
    assert normalize_catalog_timestamp("2026-07-29T08:00:00+08:00") == "2026-07-29T00:00:00Z"


@pytest.mark.parametrize(
    ("remote_name", "expected"),
    [
        (r"folder\photo.jpg", "folder-photo"),
        ("folder/photo.jpg", "photo"),
        (r"folder\photo.JPG?download=1#preview", "folder-photo"),
        ("photo.name.jpeg#preview", "photo.name"),
        ("photo.name.jpeg?download=1", "photo.name"),
        ("Photo_--Name.PNG", "photo-name"),
        ("图像-Photo.PNG", "photo"),
        ("", ""),
    ],
)
def test_remote_asset_names_have_host_independent_identity(remote_name: str, expected: str) -> None:
    assert normalize_asset_name(remote_name) == expected


def test_media_validation_uses_signatures_not_only_extensions(tmp_path: Path) -> None:
    image_path = tmp_path / "image.jpg"
    image_path.write_bytes(b"\xff\xd8\xff\xe0payload")
    invalid_path = tmp_path / "invalid.jpg"
    invalid_path.write_bytes(b"not an image")

    assert validate_media_file(image_path, "image", expected_bytes=image_path.stat().st_size)
    assert not validate_media_file(invalid_path, "image")
    assert not validate_media_file(image_path, "image", expected_bytes=image_path.stat().st_size + 1)


def test_work_queue_persists_resolution_and_download_transitions(tmp_path: Path) -> None:
    candidate = _candidate()
    queue = GrokWorkQueue.build(tmp_path, _EmptyCatalog())

    assert queue.register_discovered([candidate]) == 1
    claimed_for_resolution = queue.claim_for_resolution(limit=1)
    assert claimed_for_resolution[0].identity == candidate.identity
    assert claimed_for_resolution[0].asset_name == "photo"

    queue.mark_resolved(candidate.asset_id, candidate)
    claimed_for_download = queue.claim_ready_for_download(limit=1)
    assert claimed_for_download[0].identity == candidate.identity
    queue.mark_download_interrupted(candidate, "network interrupted")
    assert queue.claim_ready_for_download(limit=1)[0].identity == candidate.identity
    queue.mark_completed(candidate)

    reloaded = GrokWorkQueue.build(tmp_path, _EmptyCatalog())
    assert reloaded.total_count() == 1
    assert reloaded.entries_by_asset_id[candidate.asset_id].status == "ready"
    assert reloaded.has_pending_pipeline_work() is True
    rows = read_parquet_rows(tmp_path / GROK_WORK_QUEUE_FILENAME)
    assert rows is not None
    assert rows[0]["asset_id"] == candidate.asset_id
