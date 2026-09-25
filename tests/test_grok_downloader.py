"""Focused regression tests for Grok media sync dedupe."""

# Code version: v1.7.4-codex.0

from __future__ import annotations

import tempfile
import unittest
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

from app.core.grok_downloader import (
    GROK_CATALOG_FILENAME,
    GROK_DOWNLOAD_WORKERS,
    GrokMediaCatalog,
    GrokCatalogEntry,
    GrokDownloadAuth,
    GrokAuthenticationRequiredError,
    GrokDownloadManifest,
    GrokManifestEntry,
    GrokMediaCandidate,
    DownloadSizeLimitError,
    _stream_grok_candidate_via_safari,
    build_grok_initial_snapshot,
    build_candidate_from_versions_payload,
    candidate_from_url,
    compare_seen_at,
    compute_sha256,
    download_candidate,
    entry_needs_remote_image_upgrade,
    mark_grok_authentication_required,
    resolve_grok_download_worker_count,
    run_download_worker,
    stream_candidate_download,
)
from app.core.config import CrawlConfig
from app.core.resource_persistence import LEGACY_GROK_CATALOG_FILENAME
from app.core.state import TaskSnapshot, TaskState


_JPEG_BYTES = b"\xff\xd8\xff\xe0test-image"
_MP4_BYTES = b"\x00\x00\x00\x18ftypisomtest-video"


class _StreamingResponse:
    def __init__(self, payload: bytes, content_type: str) -> None:
        self.status = 200
        self.headers = {
            "Content-Length": str(len(payload)),
            "Content-Type": content_type,
        }
        self._payload = payload
        self._offset = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False

    def getcode(self) -> int:
        return self.status

    def read(self, chunk_size: int) -> bytes:
        chunk = self._payload[self._offset : self._offset + chunk_size]
        self._offset += len(chunk)
        return chunk


class _FakeResponse:
    def __init__(self, payload: bytes, content_type: str, text: str = "", status: int = 200) -> None:
        self.ok = True
        self.status = status
        self._payload = payload
        self._content_type = content_type
        self._text = text

    def body(self) -> bytes:
        return self._payload

    def header_value(self, name: str) -> str:
        if name.lower() == "content-type":
            return self._content_type
        return ""

    def text(self) -> str:
        return self._text


class _FakeRequestClient:
    def __init__(self, response: _FakeResponse | dict[str, _FakeResponse]) -> None:
        self._response = response

    def get(self, url: str, timeout: int) -> _FakeResponse:
        self.timeout = timeout
        if isinstance(self._response, dict):
            return self._response[url]
        return self._response


class _FakeContext:
    def __init__(self, response: _FakeResponse | dict[str, _FakeResponse]) -> None:
        self.request = _FakeRequestClient(response)


class GrokDownloaderTests(unittest.TestCase):
    """Validate Grok flat-file compatibility and content-level dedupe."""

    def test_grok_catalog_refuses_symlinked_media_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside"
            outside.mkdir()
            media_dir = root / "media"
            media_dir.mkdir()
            (media_dir / "grok").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(RuntimeError, "symbolic link"):
                GrokMediaCatalog.build(media_dir / "grok")
            self.assertEqual(list(outside.iterdir()), [])

    def test_grok_catalog_refuses_symlinked_ancestor_of_media_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside"
            outside.mkdir()
            (root / "cache-link").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(RuntimeError, "symbolic link"):
                GrokMediaCatalog.build(root / "cache-link" / "media" / "grok")
            self.assertEqual(list(outside.iterdir()), [])

    def test_grok_catalog_refuses_persisted_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target_dir = root / "media" / "grok"
            target_dir.mkdir(parents=True)
            outside = root / "outside.jpg"
            outside.write_bytes(_JPEG_BYTES)
            legacy_catalog = target_dir / LEGACY_GROK_CATALOG_FILENAME
            legacy_catalog.write_text(json.dumps({"entries": [{
                "identity": "asset/image",
                "relative_path": "../../outside.jpg",
                "content_sha256": compute_sha256(_JPEG_BYTES),
                "media_kind": "image",
                "content_bytes": len(_JPEG_BYTES),
            }]}), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "escapes its cache directory"):
                GrokMediaCatalog.build(target_dir)
            self.assertEqual(outside.read_bytes(), _JPEG_BYTES)

    def test_grok_download_refuses_symlinked_existing_catalog_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target_dir = root / "media" / "grok"
            target_dir.mkdir(parents=True)
            cached_path = target_dir / "image.jpg"
            cached_path.write_bytes(_JPEG_BYTES)
            candidate = GrokMediaCandidate(
                source_url="https://assets.grok.com/users/demo/generated/cebf0764-8ee5-44f5-9653-753701bfdb96/image.jpg",
                asset_id="cebf0764-8ee5-44f5-9653-753701bfdb96",
                asset_name="image",
                media_kind="image",
                identity="cebf0764-8ee5-44f5-9653-753701bfdb96/image",
            )
            catalog = GrokMediaCatalog.build(target_dir)
            catalog.register_download(candidate, cached_path.name, compute_sha256(_JPEG_BYTES), len(_JPEG_BYTES))
            manifest = GrokDownloadManifest.build(target_dir, catalog)
            outside = root / "outside.jpg"
            outside.write_bytes(_JPEG_BYTES)
            cached_path.unlink()
            cached_path.symlink_to(outside)

            with patch("app.core.grok_downloader.apply_preserved_file_timestamp", side_effect=AssertionError("timestamp write")):
                with self.assertRaisesRegex(RuntimeError, "symbolic link"):
                    download_candidate(catalog, manifest, target_dir, candidate, GrokDownloadAuth(), lambda: False)
            self.assertEqual(outside.read_bytes(), _JPEG_BYTES)

    def test_grok_manifest_refuses_traversal_and_symlinked_partial_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target_dir = Path(temp_dir) / "media" / "grok"
            target_dir.mkdir(parents=True)
            catalog = GrokMediaCatalog.build(target_dir)
            manifest = GrokDownloadManifest.build(target_dir, catalog)
            candidate = GrokMediaCandidate(
                source_url="https://assets.grok.com/users/demo/generated/cebf0764-8ee5-44f5-9653-753701bfdb96/image.jpg",
                asset_id="cebf0764-8ee5-44f5-9653-753701bfdb96",
                asset_name="generated-image",
                media_kind="image",
                identity="cebf0764-8ee5-44f5-9653-753701bfdb96/generated-image",
            )
            manifest.entries_by_identity[candidate.identity] = GrokManifestEntry(
                identity=candidate.identity,
                asset_id=candidate.asset_id,
                asset_name=candidate.asset_name,
                media_kind=candidate.media_kind,
                source_url=candidate.source_url,
                temp_relative_path="../../outside.part",
            )
            manifest.dirty = True
            manifest.flush()
            with self.assertRaisesRegex(RuntimeError, "escapes its cache directory"):
                GrokDownloadManifest.build(target_dir, catalog)
            self.assertFalse((target_dir.parent.parent / "outside.part").exists())

            manifest.entries_by_identity[candidate.identity].temp_relative_path = ""
            outside = Path(temp_dir) / "outside"
            outside.mkdir()
            (target_dir / ".grok-partial").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "symbolic link"):
                manifest.temp_path_for(candidate)
            self.assertEqual(list(outside.iterdir()), [])

    def test_grok_candidate_rejects_lookalike_and_insecure_asset_origins(self) -> None:
        path = "/users/demo/generated/cebf0764-8ee5-44f5-9653-753701bfdb96/image.jpg"
        self.assertIsNotNone(candidate_from_url(f"https://assets.grok.com{path}", "img"))
        for source_url in (
            f"https://assets.grok.com.evil.example{path}",
            f"https://evil.example/assets.grok.com{path}",
            f"https://assets.grok.com@evil.example{path}",
            f"https://user:pass@assets.grok.com{path}",
            f"http://assets.grok.com{path}",
            f"https://assets.grok.com:8443{path}",
        ):
            with self.subTest(source_url=source_url):
                self.assertIsNone(candidate_from_url(source_url, "img"))

    def test_grok_download_rechecks_a_persisted_candidate_origin(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target_dir = Path(temp_dir) / "media" / "grok"
            target_dir.mkdir(parents=True)
            catalog = GrokMediaCatalog.build(target_dir)
            manifest = GrokDownloadManifest.build(target_dir, catalog)
            candidate = GrokMediaCandidate(
                source_url=(
                    "https://assets.grok.com.evil.example/users/demo/generated/"
                    "cebf0764-8ee5-44f5-9653-753701bfdb96/image.jpg"
                ),
                asset_id="cebf0764-8ee5-44f5-9653-753701bfdb96",
                asset_name="generated-image",
                media_kind="image",
                identity="cebf0764-8ee5-44f5-9653-753701bfdb96/generated-image",
            )
            with self.assertRaisesRegex(RuntimeError, "unsafe download URL"):
                download_candidate(
                    catalog, manifest, target_dir, candidate, GrokDownloadAuth(), lambda: False
                )
            self.assertEqual(catalog.summarize(), (0, 0, 0))

    def test_safari_stream_passes_the_media_byte_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target_dir = Path(temp_dir)
            candidate = GrokMediaCandidate(
                source_url="https://assets.grok.com/users/demo/generated/cebf0764-8ee5-44f5-9653-753701bfdb96/image.jpg",
                asset_id="cebf0764-8ee5-44f5-9653-753701bfdb96",
                asset_name="generated-image",
                media_kind="image",
                identity="cebf0764-8ee5-44f5-9653-753701bfdb96/generated-image",
            )

            class _Page:
                def download_to_path(self, url, path, _should_stop, **kwargs):
                    self.assertions.append((url, kwargs))
                    path.write_bytes(_JPEG_BYTES)
                    return "image/jpeg", False

                def __init__(self):
                    self.assertions = []

            page = _Page()
            result = _stream_grok_candidate_via_safari(
                page, candidate, target_dir / "image.part", lambda: False, 5
            )
            self.assertEqual(result, ("image/jpeg", False))
            self.assertEqual(page.assertions[0][1]["max_bytes"], 5)
            self.assertEqual(page.assertions[0][1]["expected_bytes"], 0)

    def test_safari_stream_over_limit_is_classified_as_size_skip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target_dir = Path(temp_dir) / "media" / "grok"
            target_dir.mkdir(parents=True)
            candidate = GrokMediaCandidate(
                source_url="https://assets.grok.com/users/demo/generated/cebf0764-8ee5-44f5-9653-753701bfdb96/image.jpg",
                asset_id="cebf0764-8ee5-44f5-9653-753701bfdb96",
                asset_name="generated-image",
                media_kind="image",
                identity="cebf0764-8ee5-44f5-9653-753701bfdb96/generated-image",
            )
            catalog = GrokMediaCatalog.build(target_dir)
            manifest = GrokDownloadManifest.build(target_dir, catalog)

            class _Page:
                def download_to_path(self, _url, _path, _should_stop, **kwargs):
                    assert kwargs["max_bytes"] == 5
                    raise RuntimeError("Safari media exceeds the 5-byte cache limit.")

            outcome = run_download_worker(
                catalog,
                manifest,
                target_dir,
                candidate,
                GrokDownloadAuth(),
                lambda: False,
                browser_streamer=lambda item, path, stop: _stream_grok_candidate_via_safari(
                    _Page(), item, path, stop, 5
                ),
                max_file_size_bytes=5,
            )
            self.assertTrue(outcome.skipped_size)
            self.assertFalse(outcome.failed)
            self.assertEqual(catalog.summarize(), (0, 0, 0))
            self.assertFalse(list(target_dir.rglob("*.part")))

    def test_grok_download_workers_reuse_the_shared_cache_setting(self) -> None:
        self.assertEqual(
            resolve_grok_download_worker_count(CrawlConfig(download_workers=2), "chromium"),
            2,
        )
        self.assertEqual(
            resolve_grok_download_worker_count(CrawlConfig(download_workers=100), "chromium"),
            GROK_DOWNLOAD_WORKERS,
        )
        self.assertEqual(
            resolve_grok_download_worker_count(CrawlConfig(download_workers=3), "safari"),
            1,
        )

    def test_catalog_rebuild_recovers_existing_flat_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target_dir = Path(temp_dir) / "media" / "grok"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "a8db63dd-1a37-42c3-b804-17869ca83f8e_preview_image.jpg").write_bytes(_JPEG_BYTES)
            (target_dir / "cebf0764-8ee5-44f5-9653-753701bfdb96_generated_video.mp4").write_bytes(_MP4_BYTES)
            (target_dir / "3c8eed6b-d3c7-431a-81ec-592234aefa3c_profile-picture.webp").write_bytes(b"RIFF\x00\x00\x00\x00WEBP")

            catalog = GrokMediaCatalog.build(target_dir)

            self.assertTrue((target_dir / GROK_CATALOG_FILENAME).exists())
            self.assertTrue(catalog.contains_identity("a8db63dd-1a37-42c3-b804-17869ca83f8e/preview-image"))
            self.assertTrue(catalog.contains_identity("cebf0764-8ee5-44f5-9653-753701bfdb96/generated-video"))
            self.assertEqual(catalog.summarize(), (2, 1, 1))

    def test_download_candidate_reuses_existing_file_when_content_matches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target_dir = Path(temp_dir) / "media" / "grok"
            target_dir.mkdir(parents=True, exist_ok=True)
            existing_path = target_dir / "a8db63dd-1a37-42c3-b804-17869ca83f8e_preview-image.jpg"
            existing_path.write_bytes(_JPEG_BYTES)

            catalog = GrokMediaCatalog.build(target_dir)
            candidate = GrokMediaCandidate(
                source_url="https://assets.grok.com/users/demo/generated/cebf0764-8ee5-44f5-9653-753701bfdb96/image.jpg",
                asset_id="cebf0764-8ee5-44f5-9653-753701bfdb96",
                asset_name="edited-image",
                media_kind="image",
                identity="cebf0764-8ee5-44f5-9653-753701bfdb96/preview-image",
            )
            manifest = GrokDownloadManifest.build(target_dir, catalog)

            def stream_candidate(_candidate, _auth, temp_path, _should_stop):
                temp_path.parent.mkdir(parents=True, exist_ok=True)
                temp_path.write_bytes(_JPEG_BYTES)
                return "image/jpeg", False

            with patch("app.core.grok_downloader.stream_candidate_download", side_effect=stream_candidate):
                downloaded, deduped, _resumed = download_candidate(
                    catalog,
                    manifest,
                    target_dir,
                    candidate,
                    GrokDownloadAuth(),
                    lambda: False,
                )

            self.assertFalse(downloaded)
            self.assertTrue(deduped)
            self.assertEqual(catalog.summarize(), (1, 1, 0))
            self.assertTrue(catalog.contains_identity(candidate.identity))
            self.assertEqual(len([path for path in target_dir.iterdir() if path.is_file() and not path.name.startswith(".")]), 1)

    def test_download_candidate_skips_known_oversized_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target_dir = Path(temp_dir) / "media" / "grok"
            target_dir.mkdir(parents=True, exist_ok=True)
            candidate = GrokMediaCandidate(
                source_url="https://assets.grok.com/users/demo/generated/cebf0764-8ee5-44f5-9653-753701bfdb96/image.jpg",
                asset_id="cebf0764-8ee5-44f5-9653-753701bfdb96",
                asset_name="oversized-image",
                media_kind="image",
                identity="cebf0764-8ee5-44f5-9653-753701bfdb96/oversized-image",
                expected_bytes=10,
            )
            catalog = GrokMediaCatalog.build(target_dir)
            manifest = GrokDownloadManifest.build(target_dir, catalog)

            with self.assertRaisesRegex(DownloadSizeLimitError, "cache limit"):
                download_candidate(
                    catalog,
                    manifest,
                    target_dir,
                    candidate,
                    GrokDownloadAuth(),
                    lambda: False,
                    max_file_size_bytes=5,
                )

            self.assertEqual(catalog.summarize(), (0, 0, 0))
            self.assertFalse(list(target_dir.glob("*.part")))

    def test_stream_retries_an_http_successful_html_response_before_committing_media(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir) / "asset.part"
            candidate = GrokMediaCandidate(
                source_url="https://assets.grok.com/users/demo/generated/cebf0764-8ee5-44f5-9653-753701bfdb96/image.jpg",
                asset_id="cebf0764-8ee5-44f5-9653-753701bfdb96",
                asset_name="image",
                media_kind="image",
                identity="cebf0764-8ee5-44f5-9653-753701bfdb96/image",
                expected_bytes=len(_JPEG_BYTES),
            )
            responses = [
                _StreamingResponse(b"<html>rate limited</html>", "text/html"),
                _StreamingResponse(_JPEG_BYTES, "image/jpeg"),
            ]

            with patch("app.core.grok_downloader.urlopen", side_effect=responses) as mock_urlopen, patch(
                "app.core.grok_downloader.time.sleep"
            ):
                content_type, resumed = stream_candidate_download(
                    candidate,
                    GrokDownloadAuth(),
                    temp_path,
                    lambda: False,
                )

            self.assertEqual(content_type, "image/jpeg")
            self.assertFalse(resumed)
            self.assertEqual(temp_path.read_bytes(), _JPEG_BYTES)
            self.assertEqual(mock_urlopen.call_count, 2)

    def test_stream_fails_fast_when_grok_rejects_authentication(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir) / "asset.part"
            candidate = GrokMediaCandidate(
                source_url="https://assets.grok.com/users/demo/generated/cebf0764-8ee5-44f5-9653-753701bfdb96/image.jpg",
                asset_id="cebf0764-8ee5-44f5-9653-753701bfdb96",
                asset_name="image",
                media_kind="image",
                identity="cebf0764-8ee5-44f5-9653-753701bfdb96/image",
            )
            unauthorized = HTTPError(candidate.source_url, 401, "Unauthorized", {}, None)

            with patch(
                "app.core.grok_downloader.urlopen",
                side_effect=unauthorized,
            ) as mock_urlopen, patch("app.core.grok_downloader.time.sleep") as sleep:
                with self.assertRaises(GrokAuthenticationRequiredError) as error:
                    stream_candidate_download(
                        candidate,
                        GrokDownloadAuth(),
                        temp_path,
                        lambda: False,
                    )

            self.assertEqual(error.exception.status, 401)
            self.assertIn("Sign in to Grok again", str(error.exception))
            self.assertEqual(mock_urlopen.call_count, 1)
            sleep.assert_not_called()

    def test_download_worker_preserves_authentication_required_outcome(self) -> None:
        candidate = GrokMediaCandidate(
            source_url="https://assets.grok.com/example/image.jpg",
            asset_id="cebf0764-8ee5-44f5-9653-753701bfdb96",
            asset_name="image",
            media_kind="image",
            identity="cebf0764-8ee5-44f5-9653-753701bfdb96/image",
        )

        with patch(
            "app.core.grok_downloader.download_candidate",
            side_effect=GrokAuthenticationRequiredError(403),
        ) as download:
            outcome = run_download_worker(
                catalog=None,
                manifest=None,
                target_dir=Path(tempfile.gettempdir()) / "grok-auth-test",
                candidate=candidate,
                auth=GrokDownloadAuth(),
                should_stop=lambda: False,
            )

        self.assertEqual(download.call_count, 1)
        self.assertTrue(outcome.authentication_required)
        self.assertEqual(outcome.authentication_status, 403)
        self.assertFalse(outcome.failed)

    def test_authentication_failure_marks_status_for_login(self) -> None:
        state = TaskState(
            "test",
            snapshot_factory=lambda version: TaskSnapshot(version=version),
        )
        error = GrokAuthenticationRequiredError(401)

        mark_grok_authentication_required(state, error)

        snapshot = state.snapshot()
        self.assertEqual(snapshot["phase"], "failed")
        self.assertEqual(snapshot["last_error"], str(error))
        self.assertEqual(snapshot["message"], str(error))
        self.assertTrue(snapshot["performance_metrics"]["authentication_required"])

    def test_catalog_contains_asset_id_requires_a_valid_local_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target_dir = Path(temp_dir) / "media" / "grok"
            target_dir.mkdir(parents=True, exist_ok=True)
            asset_id = "cebf0764-8ee5-44f5-9653-753701bfdb96"
            media_path = target_dir / "image.jpg"
            media_path.write_bytes(_JPEG_BYTES)
            candidate = GrokMediaCandidate(
                source_url="https://assets.grok.com/users/demo/generated/cebf0764-8ee5-44f5-9653-753701bfdb96/image.jpg",
                asset_id=asset_id,
                asset_name="image",
                media_kind="image",
                identity=f"{asset_id}/image",
            )
            catalog = GrokMediaCatalog.build(target_dir)
            catalog.register_download(
                candidate,
                media_path.name,
                compute_sha256(_JPEG_BYTES),
                len(_JPEG_BYTES),
            )

            self.assertTrue(catalog.contains_asset_id(asset_id))
            media_path.unlink()
            self.assertFalse(catalog.contains_asset_id(asset_id))

    def test_build_grok_initial_snapshot_hydrates_cached_totals(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target_dir = Path(temp_dir) / "media" / "grok"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "a8db63dd-1a37-42c3-b804-17869ca83f8e_preview_image.jpg").write_bytes(_JPEG_BYTES)

            snapshot = build_grok_initial_snapshot("test", target_dir=target_dir)
            state = TaskState(version="test", snapshot_factory=lambda _version: snapshot)

            self.assertEqual(state.snapshot()["downloaded_posts"], 1)
            self.assertIn("Found existing Grok cache", state.snapshot()["message"])

    def test_catalog_rebuild_prefers_earlier_file_for_duplicate_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target_dir = Path(temp_dir) / "media" / "grok"
            target_dir.mkdir(parents=True, exist_ok=True)
            earlier = target_dir / "11111111-1111-1111-1111-111111111111_content.jpg"
            later = target_dir / "22222222-2222-2222-2222-222222222222_content.jpg"
            earlier.write_bytes(_JPEG_BYTES)
            later.write_bytes(_JPEG_BYTES)

            earlier_stat = earlier.stat()
            later_stat = later.stat()
            os.utime(earlier, (earlier_stat.st_atime, earlier_stat.st_mtime - 10))
            os.utime(later, (later_stat.st_atime, later_stat.st_mtime))

            catalog = GrokMediaCatalog.build(target_dir)

            self.assertEqual(
                catalog.lookup_relative_path_by_hash(catalog.entries_by_identity["11111111-1111-1111-1111-111111111111/content"].content_sha256),
                earlier.name,
            )
            self.assertEqual(
                catalog.entries_by_identity["22222222-2222-2222-2222-222222222222/content"].relative_path,
                earlier.name,
            )

    def test_catalog_load_prefers_earlier_first_seen_path_for_duplicate_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target_dir = Path(temp_dir) / "media" / "grok"
            target_dir.mkdir(parents=True, exist_ok=True)
            earlier = target_dir / "11111111-1111-1111-1111-111111111111_content.jpg"
            later = target_dir / "22222222-2222-2222-2222-222222222222_content.jpg"
            earlier.write_bytes(_JPEG_BYTES)
            later.write_bytes(_JPEG_BYTES)

            payload = {
                "schema_version": 1,
                "entries": [
                    {
                        "identity": "11111111-1111-1111-1111-111111111111/content",
                        "relative_path": earlier.name,
                        "media_kind": "image",
                        "content_sha256": "sha",
                        "content_bytes": len(_JPEG_BYTES),
                        "source_url": "",
                        "first_seen_at": "2026-04-21T03:24:53Z",
                        "last_seen_at": "2026-04-21T03:24:53Z",
                    },
                    {
                        "identity": "22222222-2222-2222-2222-222222222222/content",
                        "relative_path": later.name,
                        "media_kind": "image",
                        "content_sha256": "sha",
                        "content_bytes": len(_JPEG_BYTES),
                        "source_url": "",
                        "first_seen_at": "2026-04-21T03:24:57Z",
                        "last_seen_at": "2026-04-21T03:24:57Z",
                    },
                ],
            }
            (target_dir / LEGACY_GROK_CATALOG_FILENAME).write_text(json.dumps(payload))

            catalog = GrokMediaCatalog.build(target_dir)

            self.assertEqual(catalog.lookup_relative_path_by_hash("sha"), earlier.name)
            self.assertEqual(
                catalog.entries_by_identity["22222222-2222-2222-2222-222222222222/content"].relative_path,
                earlier.name,
            )

    def test_compare_seen_at_orders_earlier_timestamps_first(self) -> None:
        self.assertLess(compare_seen_at("2026-04-21T03:24:53Z", "2026-04-21T03:24:57Z"), 0)
        self.assertGreater(compare_seen_at("2026-04-21T03:24:57Z", "2026-04-21T03:24:53Z"), 0)
        self.assertEqual(compare_seen_at("2026-04-21T03:24:53Z", "2026-04-21T03:24:53Z"), 0)

    def test_versions_payload_upgrades_preview_image_to_original_asset(self) -> None:
        fallback_candidate = GrokMediaCandidate(
            source_url="https://assets.grok.com/users/demo/generated/585da42d-eaff-45c4-9ec5-d1159df2bee8/preview_image.jpg",
            asset_id="585da42d-eaff-45c4-9ec5-d1159df2bee8",
            asset_name="preview-image",
            media_kind="image",
            identity="585da42d-eaff-45c4-9ec5-d1159df2bee8/preview-image",
            preview_url="https://assets.grok.com/users/demo/generated/585da42d-eaff-45c4-9ec5-d1159df2bee8/preview_image.jpg",
        )
        payload = {
            "assets": [
                {
                    "assetId": "585da42d-eaff-45c4-9ec5-d1159df2bee8",
                    "mimeType": "image/jpeg",
                    "name": "edited-image.jpg",
                    "sizeBytes": 148679,
                    "createTime": "2026-04-21T03:30:50.008066Z",
                    "previewImageKey": "users/demo/generated/585da42d-eaff-45c4-9ec5-d1159df2bee8/preview_image.jpg",
                    "key": "users/demo/generated/585da42d-eaff-45c4-9ec5-d1159df2bee8/image.jpg",
                    "width": 832,
                    "height": 1248,
                }
            ]
        }

        resolved = build_candidate_from_versions_payload(payload, fallback_candidate)

        self.assertEqual(
            resolved.source_url,
            "https://assets.grok.com/users/demo/generated/585da42d-eaff-45c4-9ec5-d1159df2bee8/image.jpg",
        )
        self.assertEqual(resolved.asset_name, "edited-image")
        self.assertEqual(resolved.expected_width, 832)
        self.assertEqual(resolved.expected_height, 1248)

    def test_entry_needs_remote_image_upgrade_detects_preview_cache(self) -> None:
        self.assertTrue(
            entry_needs_remote_image_upgrade(
                GrokCatalogEntry(
                    identity="585da42d-eaff-45c4-9ec5-d1159df2bee8/preview-image",
                    relative_path="585da42d-eaff-45c4-9ec5-d1159df2bee8_preview-image.jpg",
                    media_kind="image",
                    content_sha256="sha",
                    content_bytes=1,
                    source_url="https://assets.grok.com/users/demo/generated/585da42d-eaff-45c4-9ec5-d1159df2bee8/preview_image.jpg",
                    first_seen_at="2026-04-21T03:30:50Z",
                    last_seen_at="2026-04-21T03:30:50Z",
                )
            )
        )

    def test_download_candidate_aliases_matching_content_without_duplicate_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target_dir = Path(temp_dir) / "media" / "grok"
            target_dir.mkdir(parents=True, exist_ok=True)
            preview_path = target_dir / "585da42d-eaff-45c4-9ec5-d1159df2bee8_preview-image.jpg"
            preview_path.write_bytes(_JPEG_BYTES)

            catalog = GrokMediaCatalog.build(target_dir)
            candidate = GrokMediaCandidate(
                source_url="https://assets.grok.com/users/demo/generated/585da42d-eaff-45c4-9ec5-d1159df2bee8/image.jpg",
                asset_id="585da42d-eaff-45c4-9ec5-d1159df2bee8",
                asset_name="edited-image",
                media_kind="image",
                identity="585da42d-eaff-45c4-9ec5-d1159df2bee8/edited-image",
            )
            manifest = GrokDownloadManifest.build(target_dir, catalog)

            def stream_candidate(_candidate, _auth, temp_path, _should_stop):
                temp_path.parent.mkdir(parents=True, exist_ok=True)
                temp_path.write_bytes(_JPEG_BYTES)
                return "image/jpeg", False

            with patch("app.core.grok_downloader.stream_candidate_download", side_effect=stream_candidate):
                downloaded, deduped, _resumed = download_candidate(
                    catalog,
                    manifest,
                    target_dir,
                    candidate,
                    GrokDownloadAuth(),
                    lambda: False,
                )

            self.assertFalse(downloaded)
            self.assertTrue(deduped)
            self.assertTrue(preview_path.exists())
            saved_files = [
                path.name
                for path in target_dir.iterdir()
                if path.is_file() and not path.name.startswith(".")
            ]
            self.assertEqual(len(saved_files), 1)
            self.assertEqual(saved_files, [preview_path.name])
            self.assertTrue(catalog.contains_identity(candidate.identity))


if __name__ == "__main__":
    unittest.main()
