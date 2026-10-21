"""Unit tests for Grok downloader canonical asset handling."""

# Code version: v1.4.0-codex.0

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.core.grok_downloader import (
    GrokCatalogEntry,
    GrokMediaCandidate,
    GrokMediaCatalog,
    PlaywrightError,
    build_candidate_from_versions_payload,
    build_destination_filename,
    candidate_is_downloadable,
    compute_sha256,
    derive_identity_from_filename,
    drop_catalog_entry,
    entry_needs_remote_image_upgrade,
    open_grok_page,
    prepare_grok_library_page,
    resolve_candidate_from_versions,
    resolve_candidate_from_file_details_page,
)
from app.core.safari_automation import SafariContext


class BuildCandidateFromVersionsPayloadTests(unittest.TestCase):
    def test_versions_request_failure_keeps_the_discovered_candidate(self) -> None:
        fallback_candidate = GrokMediaCandidate(
            source_url="https://assets.grok.com/users/test/generated/asset-id/preview_image.jpg",
            asset_id="asset-id",
            asset_name="preview-image",
            media_kind="image",
            identity="asset-id/preview-image",
            preview_url="https://assets.grok.com/users/test/generated/asset-id/preview_image.jpg",
        )

        class Request:
            def get(self, _url: str, **_kwargs):
                raise RuntimeError("Safari request failed: Load failed")

        context = type("Context", (), {"request": Request()})()

        resolved = resolve_candidate_from_versions(context, fallback_candidate)

        self.assertEqual(resolved, fallback_candidate)

    def test_details_page_request_failure_keeps_the_discovered_candidate(self) -> None:
        fallback_candidate = GrokMediaCandidate(
            source_url="https://assets.grok.com/users/test/generated/asset-id/preview_image.jpg",
            asset_id="asset-id",
            asset_name="preview-image",
            media_kind="image",
            identity="asset-id/preview-image",
            preview_url="https://assets.grok.com/users/test/generated/asset-id/preview_image.jpg",
        )

        with patch(
            "app.core.grok_downloader.open_grok_page",
            side_effect=RuntimeError("Safari request failed: Load failed"),
        ):
            resolved = resolve_candidate_from_file_details_page(
                object(),
                fallback_candidate,
                details_page=object(),
            )

        self.assertEqual(resolved, fallback_candidate)

    def test_prefers_canonical_asset_key_over_preview_identity(self) -> None:
        fallback_candidate = GrokMediaCandidate(
            source_url="https://assets.grok.com/users/test/generated/asset-id/preview_image.jpg",
            asset_id="asset-id",
            asset_name="preview-image",
            media_kind="image",
            identity="asset-id/preview-image",
            preview_url="https://assets.grok.com/users/test/generated/asset-id/preview_image.jpg",
        )
        payload = {
            "assets": [
                {
                    "assetId": "asset-id",
                    "mimeType": "image/jpeg",
                    "name": "edited-image.jpg",
                    "sizeBytes": 144_048,
                    "createTime": "2026-04-21T05:00:37.331632Z",
                    "previewImageKey": "users/test/generated/asset-id/preview_image.jpg",
                    "key": "users/test/generated/asset-id/image.jpg",
                    "isLatest": True,
                    "width": 832,
                    "height": 1_248,
                }
            ]
        }

        candidate = build_candidate_from_versions_payload(payload, fallback_candidate)

        self.assertEqual(candidate.identity, "asset-id/edited-image")
        self.assertEqual(candidate.asset_name, "edited-image")
        self.assertEqual(candidate.source_url, "https://assets.grok.com/users/test/generated/asset-id/image.jpg")
        self.assertEqual(candidate.preview_url, "https://assets.grok.com/users/test/generated/asset-id/preview_image.jpg")
        self.assertEqual(candidate.expected_width, 832)
        self.assertEqual(candidate.expected_height, 1_248)
        self.assertEqual(candidate.expected_bytes, 144_048)
        self.assertEqual(candidate.created_at, "2026-04-21T05:00:37Z")

    def test_picks_largest_non_preview_asset(self) -> None:
        fallback_candidate = GrokMediaCandidate(
            source_url="https://assets.grok.com/users/test/generated/asset-id/preview_image.jpg",
            asset_id="asset-id",
            asset_name="preview-image",
            media_kind="image",
            identity="asset-id/preview-image",
            preview_url="https://assets.grok.com/users/test/generated/asset-id/preview_image.jpg",
        )
        payload = {
            "assets": [
                {
                    "assetId": "asset-id",
                    "mimeType": "image/jpeg",
                    "sizeBytes": 1_500,
                    "previewImageKey": "users/test/generated/asset-id/preview_image.jpg",
                    "key": "users/test/generated/asset-id/preview_image.jpg",
                    "isLatest": False,
                    "width": 43,
                    "height": 64,
                },
                {
                    "assetId": "asset-id",
                    "mimeType": "image/jpeg",
                    "sizeBytes": 144_048,
                    "previewImageKey": "users/test/generated/asset-id/preview_image.jpg",
                    "key": "users/test/generated/asset-id/image.jpg",
                    "isLatest": True,
                    "width": 832,
                    "height": 1_248,
                },
            ]
        }

        candidate = build_candidate_from_versions_payload(payload, fallback_candidate)

        self.assertEqual(candidate.identity, "asset-id/image")
        self.assertEqual(candidate.source_url, "https://assets.grok.com/users/test/generated/asset-id/image.jpg")

    def test_deleted_asset_does_not_fall_back_to_preview_url(self) -> None:
        fallback_candidate = GrokMediaCandidate(
            source_url="https://assets.grok.com/users/test/generated/asset-id/preview_image.jpg",
            asset_id="asset-id",
            asset_name="preview-image",
            media_kind="image",
            identity="asset-id/preview-image",
            preview_url="https://assets.grok.com/users/test/generated/asset-id/preview_image.jpg",
        )
        payload = {
            "assets": [
                {
                    "assetId": "asset-id",
                    "mimeType": "",
                    "name": "deleted file",
                    "sizeBytes": 0,
                    "createTime": "2026-04-21T05:26:55.751889Z",
                    "previewImageKey": "",
                    "key": "",
                    "isDeleted": True,
                    "isLatest": True,
                    "width": 832,
                    "height": 1_248,
                }
            ]
        }

        candidate = build_candidate_from_versions_payload(payload, fallback_candidate)

        self.assertEqual(candidate.identity, "asset-id/deleted-file")
        self.assertEqual(candidate.source_url, "")
        self.assertFalse(candidate_is_downloadable(candidate))


class EntryNeedsRemoteImageUpgradeTests(unittest.TestCase):
    def test_detects_remote_size_mismatch_for_mislabeled_small_image(self) -> None:
        entry = GrokCatalogEntry(
            identity="asset-id/image",
            relative_path="asset-id_image.jpg",
            media_kind="image",
            content_sha256="hash",
            content_bytes=1_500,
            source_url="https://assets.grok.com/users/test/generated/asset-id/image.jpg",
            first_seen_at="2026-04-21T05:00:37Z",
            last_seen_at="2026-04-21T05:00:37Z",
        )
        remote_candidate = GrokMediaCandidate(
            source_url="https://assets.grok.com/users/test/generated/asset-id/image.jpg",
            asset_id="asset-id",
            asset_name="image",
            media_kind="image",
            identity="asset-id/image",
            expected_bytes=144_048,
        )

        self.assertTrue(entry_needs_remote_image_upgrade(entry, remote_candidate))


class FilenameConventionTests(unittest.TestCase):
    def test_build_destination_filename_places_timestamp_before_asset_id(self) -> None:
        candidate = GrokMediaCandidate(
            source_url="https://assets.grok.com/users/test/generated/8e5b4bee-7245-4440-b511-8aa73e42ce3f/image.jpg",
            asset_id="8e5b4bee-7245-4440-b511-8aa73e42ce3f",
            asset_name="image",
            media_kind="image",
            identity="8e5b4bee-7245-4440-b511-8aa73e42ce3f/image",
            created_at="2026-04-21T05:00:37.331632Z",
        )

        filename = build_destination_filename(candidate, "image/jpeg")

        self.assertEqual(filename, "img_20260421T050037Z_8e5b4bee-7245-4440-b511-8aa73e42ce3f_image.jpg")

    def test_derive_identity_from_stamped_filename(self) -> None:
        identity = derive_identity_from_filename(
            "vid_20260421T052655Z_8e5b4bee-7245-4440-b511-8aa73e42ce3f_generated-video.mp4"
        )

        self.assertEqual(identity, "8e5b4bee-7245-4440-b511-8aa73e42ce3f/generated-video")


class DropCatalogEntryTests(unittest.TestCase):
    def test_removes_obsolete_preview_file_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            target_dir = Path(tmp_dir)
            catalog = GrokMediaCatalog.build(target_dir)

            preview_bytes = b"preview"
            preview_path = target_dir / "asset-id_preview-image.jpg"
            preview_path.write_bytes(preview_bytes)
            preview_candidate = GrokMediaCandidate(
                source_url="https://assets.grok.com/users/test/generated/asset-id/preview_image.jpg",
                asset_id="asset-id",
                asset_name="preview-image",
                media_kind="image",
                identity="asset-id/preview-image",
            )
            catalog.register_download(
                candidate=preview_candidate,
                relative_path=preview_path.name,
                content_sha256=compute_sha256(preview_bytes),
                content_bytes=len(preview_bytes),
                seen_at="2026-04-21T05:00:37Z",
            )

            image_bytes = b"full-resolution-image"
            image_path = target_dir / "asset-id_image.jpg"
            image_path.write_bytes(image_bytes)
            image_candidate = GrokMediaCandidate(
                source_url="https://assets.grok.com/users/test/generated/asset-id/image.jpg",
                asset_id="asset-id",
                asset_name="image",
                media_kind="image",
                identity="asset-id/image",
            )
            catalog.register_download(
                candidate=image_candidate,
                relative_path=image_path.name,
                content_sha256=compute_sha256(image_bytes),
                content_bytes=len(image_bytes),
                seen_at="2026-04-21T05:00:38Z",
            )

            drop_catalog_entry(catalog, target_dir, "asset-id/preview-image")

            self.assertNotIn("asset-id/preview-image", catalog.entries_by_identity)
            self.assertIn("asset-id/image", catalog.entries_by_identity)
            self.assertFalse(preview_path.exists())
            self.assertTrue(image_path.exists())


class CatalogRebuildTests(unittest.TestCase):
    def test_rebuild_uses_timestamp_embedded_in_stamped_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            target_dir = Path(tmp_dir)
            image_path = (
                target_dir
                / "img_20260421T050037Z_8e5b4bee-7245-4440-b511-8aa73e42ce3f_image.jpg"
            )
            image_path.write_bytes(b"\xff\xd8\xff\xe0full-resolution-image")

            catalog = GrokMediaCatalog.build(target_dir)
            entry = catalog.entries_by_identity["8e5b4bee-7245-4440-b511-8aa73e42ce3f/image"]

            self.assertEqual(entry.first_seen_at, "2026-04-21T05:00:37Z")
            self.assertEqual(entry.relative_path, image_path.name)


class OpenGrokPageTests(unittest.TestCase):
    def test_safari_requires_dom_navigation_without_requesting_networkidle(self) -> None:
        url = "https://grok.com/files?sort=&fileType=&createdBy="
        page = Mock(context=SafariContext(url))
        page.wait_for_load_state.side_effect = RuntimeError(
            "Safari did not reach load state 'networkidle' within 5,000 ms."
        )

        with patch("app.core.grok_downloader.time.sleep") as sleep:
            open_grok_page(page, url, settle_seconds=0.0)

        page.goto.assert_called_once_with(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_load_state.assert_not_called()
        sleep.assert_not_called()

    def test_safari_navigation_failure_propagates_before_optional_waits(self) -> None:
        url = "https://grok.com/files?sort=&fileType=&createdBy="
        page = Mock(context=SafariContext(url))
        failure = RuntimeError("Safari navigation failed.")
        page.goto.side_effect = failure

        with self.assertRaises(RuntimeError) as raised, patch(
            "app.core.grok_downloader.time.sleep",
        ) as sleep:
            open_grok_page(page, url)

        self.assertIs(raised.exception, failure)
        page.goto.assert_called_once_with(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_load_state.assert_not_called()
        sleep.assert_not_called()

    def test_uses_domcontentloaded_and_tolerates_networkidle_timeout(self) -> None:
        class FakePage:
            def __init__(self) -> None:
                self.goto_calls: list[tuple[str, str, int]] = []
                self.wait_calls: list[tuple[str, int]] = []

            def goto(self, url: str, wait_until: str, timeout: int) -> None:
                self.goto_calls.append((url, wait_until, timeout))

            def wait_for_load_state(self, state: str, timeout: int) -> None:
                self.wait_calls.append((state, timeout))
                raise PlaywrightError("network still busy")

        fake_page = FakePage()

        open_grok_page(fake_page, "https://grok.com/files?sort=&fileType=&createdBy=", settle_seconds=0.0)

        self.assertEqual(
            fake_page.goto_calls,
            [("https://grok.com/files?sort=&fileType=&createdBy=", "domcontentloaded", 60_000)],
        )
        self.assertEqual(fake_page.wait_calls, [("networkidle", 5_000)])


class PrepareGrokLibraryPageTests(unittest.TestCase):
    def test_prefers_existing_grok_page_and_closes_blank_tabs(self) -> None:
        class FakePage:
            def __init__(self, url: str) -> None:
                self.url = url
                self.brought_to_front = False
                self.closed = False

            def bring_to_front(self) -> None:
                self.brought_to_front = True

            def close(self) -> None:
                self.closed = True

        class FakeContext:
            def __init__(self) -> None:
                self.pages = [
                    FakePage("about:blank"),
                    FakePage("https://grok.com/files?sort=&fileType=&createdBy="),
                ]

            def new_page(self) -> FakePage:
                raise AssertionError("new_page should not be called when a Grok page already exists")

        context = FakeContext()

        page = prepare_grok_library_page(context)

        self.assertEqual(page.url, "https://grok.com/files?sort=&fileType=&createdBy=")
        self.assertFalse(page.brought_to_front)
        self.assertTrue(context.pages[0].closed)


if __name__ == "__main__":
    unittest.main()
