"""Focused tests for authenticated Gemini rendered-image caching.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from contextlib import contextmanager
import io
import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image
from playwright.sync_api import Browser

import test_sidebar_e2e

from app.core.gemini_media import (
    GeminiMediaCatalog,
    GeminiMediaSizeLimitError,
    _candidate_from_rendered_image,
    _download_gemini_image,
    _validated_image_extension,
    build_gemini_media_initial_snapshot,
    discover_gemini_rendered_images,
    gemini_media_dir,
    sync_gemini_media,
)
from app.core.gemini_downloader import GeminiConversationLink
from app.core.config import CrawlConfig
from app.core.state import TaskState
from app.core.safari_automation import SafariContext, SafariPage


disposable_browser = test_sidebar_e2e.disposable_browser


def _png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (2, 2), (0, 85, 204)).save(output, format="PNG")
    return output.getvalue()


def _rendered_candidate(image_index: int = 0):
    candidate = _candidate_from_rendered_image(
        {
            "source_url": f"https://lh3.googleusercontent.com/gg-dl/image-{image_index}.png",
            "message_index": 0,
            "image_index": image_index,
            "role": "assistant",
        },
        GeminiConversationLink("chat-1", "https://gemini.google.com/app/chat-1", "Chat"),
    )
    assert candidate is not None
    return candidate


@contextmanager
def _rendered_media_session(candidates, transfer):
    """Replace browser I/O while exercising the real sync and media downloader."""
    page = MagicMock()
    page.download_to_path.side_effect = transfer
    context = MagicMock()
    context.primary_page = page
    context.__enter__.return_value = context
    with patch("app.core.gemini_media.SafariContext", return_value=context), patch(
        "app.core.gemini_media.goto_with_retry"
    ), patch("app.core.gemini_media._wait_for_gemini_ready"), patch(
        "app.core.gemini_media.collect_gemini_conversation_links",
        return_value=[GeminiConversationLink("chat-1", "https://gemini.google.com/app/chat-1", "Chat")],
    ), patch("app.core.gemini_media.wait_for_gemini_bot_check_clear", return_value=True), patch(
        "app.core.gemini_media._prepare_gemini_page_for_rendering"
    ), patch(
        "app.core.gemini_media.discover_gemini_rendered_images", return_value=(candidates, 0),
    ):
        yield page
    context.__exit__.assert_called_once()


def test_gemini_media_accepts_only_first_party_rendered_image_urls() -> None:
    conversation = GeminiConversationLink("chat-1", "https://gemini.google.com/app/chat-1", "Chat")
    raw = {"message_index": 1, "image_index": 0, "role": "assistant", "alt_text": "Plot"}

    assert _candidate_from_rendered_image(
        {**raw, "source_url": "https://lh3.googleusercontent.com/gg-dl/a.png?token=secret"},
        conversation,
    )
    for source_url in (
        "http://lh3.googleusercontent.com/gg-dl/a.png",
        "https://lh3.googleusercontent.com.evil.test/gg-dl/a.png",
        "https://www.gstatic.com/avatar.png",
        "https://i.ytimg.com/video-thumbnail.jpg",
        "https://lh3.googleusercontent.com:444/gg-dl/a.png",
        "https://evil.test/images/a.png",
        "https://user:pass@lh3.googleusercontent.com/gg-dl/a.png",
        "data:image/png;base64,AAAA",
        "blob:https://gemini.google.com/id",
    ):
        assert _candidate_from_rendered_image(
            {**raw, "source_url": source_url},
            conversation,
        ) is None


@pytest.mark.parametrize("discovered_count", (0, 1))
def test_gemini_media_cancelled_discovery_stops_without_session_failure(
    tmp_path: Path, macos_host, discovered_count: int,
) -> None:
    state = TaskState("test")
    stop_requested = False

    def should_stop() -> bool:
        return stop_requested

    def cancel_discovery(_page, _config, should_stop, _state):
        nonlocal stop_requested
        assert should_stop() is False
        stop_requested = True
        assert should_stop() is True
        return [
            GeminiConversationLink("chat-1", "https://gemini.google.com/app/chat-1", "Chat")
        ][:discovered_count]

    with _rendered_media_session([], None) as page, patch(
        "app.core.gemini_media.collect_gemini_conversation_links", side_effect=cancel_discovery,
    ) as discover, patch(
        "app.core.gemini_media._prepare_gemini_page_for_rendering",
    ) as prepare:
        result = sync_gemini_media(
            state, CrawlConfig(gemini_browser="safari"), should_stop, tmp_path,
        )
        assert discover.call_args.args[0] is page
        assert discover.call_args.args[2] is should_stop
        prepare.assert_not_called()
        page.download_to_path.assert_not_called()

    assert result.stopped is True
    assert result.incomplete is False
    assert result.sessions == result.failed_sessions == result.failed_images == 0
    snapshot = state.snapshot()
    assert snapshot["phase"] == "stopped"
    assert snapshot["discovery_complete"] is False
    assert snapshot["discovered_tweets"] == discovered_count
    assert snapshot["failed_tweets"] == 0
    assert not list(gemini_media_dir(tmp_path).glob("img_*"))


def test_gemini_media_uncancelled_empty_discovery_remains_a_failure(
    tmp_path: Path, macos_host,
) -> None:
    with _rendered_media_session([], None) as page, patch(
        "app.core.gemini_media.collect_gemini_conversation_links", return_value=[],
    ):
        with pytest.raises(RuntimeError, match="Gemini media discovery returned no rendered sessions"):
            sync_gemini_media(
                TaskState("test"), CrawlConfig(gemini_browser="safari"), lambda: False, tmp_path,
            )
        page.download_to_path.assert_not_called()


@pytest.mark.parametrize("bridge_error", (
    None,
    "Safari media exceeds the configured cache limit.",
    "Safari media request failed: Safari media exceeds the configured cache limit.",
    "Safari media exceeds the 8-byte cache limit.",
    "Safari media request failed: Safari media exceeds the 8-byte cache limit.",
))
def test_gemini_media_rejects_oversized_bytes_without_catalog_entry(
    tmp_path: Path, bridge_error: str | None,
) -> None:
    conversation = GeminiConversationLink("chat-1", "https://gemini.google.com/app/chat-1", "Chat")
    candidate = _candidate_from_rendered_image(
        {
            "source_url": "https://lh3.googleusercontent.com/gg-dl/image-1.png",
            "message_index": 0,
            "image_index": 0,
            "role": "user",
        },
        conversation,
    )
    assert candidate is not None
    catalog = GeminiMediaCatalog(gemini_media_dir(tmp_path))

    class _Page:
        def download_to_path(
            self, _source_url: str, destination: Path, _should_stop, *, max_bytes: int, reject_redirects: bool
        ) -> tuple[str, bool]:
            assert max_bytes == 8
            if bridge_error:
                raise RuntimeError(bridge_error)
            destination.write_bytes(_png_bytes())
            return "image/png", False

    with pytest.raises(GeminiMediaSizeLimitError, match="size limit|cache limit"):
        _download_gemini_image(_Page(), catalog, candidate, lambda: False, 8)

    assert catalog.cached_count == 0
    assert not list(catalog.root.glob("img_*"))
    assert not list((catalog.root / ".partial").glob("*.part"))


def test_gemini_media_accepts_exactly_the_configured_byte_limit(tmp_path: Path) -> None:
    payload = _png_bytes()
    catalog = GeminiMediaCatalog(gemini_media_dir(tmp_path))
    page = MagicMock()

    def transfer(_url, destination, _stop, *, max_bytes, reject_redirects):
        assert max_bytes == len(payload)
        destination.write_bytes(payload)
        return "image/png", False

    page.download_to_path.side_effect = transfer
    assert _download_gemini_image(page, catalog, _rendered_candidate(), lambda: False, len(payload))
    assert catalog.cached_count == 1
    assert [path.read_bytes() for path in catalog.root.glob("img_*")] == [payload]


@pytest.mark.parametrize("boundary", ("declared_length", "range_metadata", "stream", "measured_bytes"))
def test_gemini_media_size_skip_does_not_retry_or_fail_the_sync(
    tmp_path: Path, macos_host, boundary: str,
) -> None:
    candidate = _rendered_candidate()
    state = TaskState("test")
    config = CrawlConfig(gemini_browser="safari", max_media_file_size_mib=1)

    def transfer(_url, destination, _stop, *, max_bytes, reject_redirects):
        assert max_bytes == 1_048_576
        if boundary == "measured_bytes":
            destination.write_bytes(b"x" * (max_bytes + 1))
            return "image/png", False
        if boundary == "range_metadata":
            raise RuntimeError(f"Safari media exceeds the {max_bytes:,}-byte cache limit.")
        if boundary == "stream":
            destination.write_bytes(b"partial image")
        raise RuntimeError("Safari media request failed: Safari media exceeds the configured cache limit.")

    with _rendered_media_session([candidate], transfer) as page:
        result = sync_gemini_media(state, config, lambda: False, tmp_path)
        page.download_to_path.assert_called_once()

    assert result.skipped_size == 1
    assert result.failed_images == 0
    assert result.incomplete is False
    assert result.downloaded_images == 0
    assert result.cached_images == 0
    snapshot = state.snapshot()
    assert snapshot["phase"] == "completed"
    assert snapshot["processed_tweets"] == snapshot["queued_tweets"] == 1
    assert snapshot["skipped_tweets"] == 1
    assert snapshot["failed_tweets"] == 0
    assert not list(gemini_media_dir(tmp_path).glob("img_*"))
    assert not list((gemini_media_dir(tmp_path) / ".partial").glob("*.part"))


@pytest.mark.parametrize("failure", ("stream_error", "empty_response"))
def test_gemini_media_real_failures_still_retry_and_mark_sync_incomplete(
    tmp_path: Path, macos_host, failure: str,
) -> None:
    state = TaskState("test")

    def transfer(_url, destination, _stop, *, max_bytes, reject_redirects):
        if failure == "stream_error":
            destination.write_bytes(b"partial image")
            raise RuntimeError("Safari media request failed: stream interrupted before cache limit.")
        destination.write_bytes(b"")
        return "image/png", False

    with _rendered_media_session([_rendered_candidate()], transfer) as page:
        result = sync_gemini_media(
            state, CrawlConfig(gemini_browser="safari", max_media_file_size_mib=1),
            lambda: False, tmp_path,
        )
        assert page.download_to_path.call_count == 2

    assert result.skipped_size == 0
    assert result.failed_images == 1
    assert result.incomplete is True
    snapshot = state.snapshot()
    assert snapshot["phase"] == "failed"
    assert snapshot["processed_tweets"] == snapshot["queued_tweets"] == 1
    assert snapshot["skipped_tweets"] == 0
    assert snapshot["failed_tweets"] == 1
    assert not list(gemini_media_dir(tmp_path).glob("img_*"))
    assert not list((gemini_media_dir(tmp_path) / ".partial").glob("*.part"))


def test_gemini_size_skip_progress_preserves_cached_image_above_a_lowered_limit(
    tmp_path: Path, macos_host,
) -> None:
    output = io.BytesIO()
    Image.new("RGB", (800, 600), (0, 85, 204)).save(output, format="PNG", compress_level=0)
    payload = output.getvalue()
    assert 1_048_576 < len(payload) < 2_097_152
    catalog = GeminiMediaCatalog(gemini_media_dir(tmp_path))
    known = _rendered_candidate(1)
    seed_page = MagicMock()

    def seed_transfer(_url, destination, _stop, *, max_bytes, reject_redirects):
        destination.write_bytes(payload)
        return "image/png", False

    seed_page.download_to_path.side_effect = seed_transfer
    assert _download_gemini_image(seed_page, catalog, known, lambda: False, 2_097_152)
    manifest_before = catalog.path.read_bytes()
    state = TaskState("test")

    def reject_oversized(url, _destination, _stop, *, max_bytes, reject_redirects):
        assert url != known.source_url
        raise RuntimeError(f"Safari media exceeds the {max_bytes:,}-byte cache limit.")

    with _rendered_media_session([_rendered_candidate(), known], reject_oversized) as page:
        result = sync_gemini_media(
            state, CrawlConfig(gemini_browser="safari", max_media_file_size_mib=1),
            lambda: False, tmp_path,
        )
        page.download_to_path.assert_called_once()

    assert result.skipped_size == result.skipped_known == 1
    assert result.downloaded_images == result.failed_images == 0
    assert result.cached_images == 1
    assert result.incomplete is False
    assert state.snapshot()["skipped_tweets"] == 2
    assert state.snapshot()["processed_tweets"] == state.snapshot()["queued_tweets"] == 2
    assert [path.read_bytes() for path in catalog.root.glob("img_*")] == [payload]
    assert catalog.path.read_bytes() == manifest_before


def test_gemini_media_rejects_images_above_pixel_limit() -> None:
    with patch("app.core.gemini_media.GEMINI_MAX_IMAGE_PIXELS", 3):
        assert _validated_image_extension(_png_bytes()) == ""


@pytest.mark.parametrize("linked_component", ["gemini", ".partial"])
def test_gemini_media_refuses_symlinked_storage_directories(
    tmp_path: Path, linked_component: str
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    media_root = gemini_media_dir(tmp_path / "local_store")
    media_root.parent.mkdir(parents=True)
    if linked_component == "gemini":
        media_root.symlink_to(outside, target_is_directory=True)
        with pytest.raises(RuntimeError, match="symbolic link"):
            GeminiMediaCatalog(media_root)
    else:
        media_root.mkdir()
        (media_root / ".partial").symlink_to(outside, target_is_directory=True)
        catalog = GeminiMediaCatalog(media_root)
        candidate = _candidate_from_rendered_image(
            {
                "source_url": "https://lh3.googleusercontent.com/gg-dl/image-1.png",
                "message_index": 0,
                "image_index": 0,
                "role": "user",
            },
            GeminiConversationLink("chat-1", "https://gemini.google.com/app/chat-1", "Chat"),
        )
        assert candidate is not None

        class _Page:
            def download_to_path(self, *_args, **_kwargs) -> None:
                raise AssertionError("Unsafe storage reached the browser download")

        with pytest.raises(RuntimeError, match="symbolic link"):
            _download_gemini_image(_Page(), catalog, candidate, lambda: False, 1024)
    assert list(outside.iterdir()) == []


def test_gemini_media_caches_real_bytes_and_repairs_corruption_without_saving_signed_urls(
    tmp_path: Path, macos_host,
) -> None:
    payload = _png_bytes()
    conversation = GeminiConversationLink("chat-1", "https://gemini.google.com/app/chat-1", "Chat")
    raw = {
        "source_url": "https://lh3.googleusercontent.com/gg-dl/image.png?token=private",
        "message_index": 0, "image_index": 0, "role": "assistant", "alt_text": "Plot",
    }
    candidate = _candidate_from_rendered_image(raw, conversation)
    renewed = _candidate_from_rendered_image(
        {**raw, "source_url": raw["source_url"].replace("private", "renewed")}, conversation,
    )
    assert candidate is not None and renewed is not None
    assert candidate.asset_id == renewed.asset_id

    def transfer(_url, destination, _stop, *, max_bytes, reject_redirects):
        destination.write_bytes(payload)
        return "image/png", False

    config = CrawlConfig(gemini_browser="safari", max_media_file_size_mib=1)
    with _rendered_media_session([candidate], transfer) as page:
        result = sync_gemini_media(TaskState("test"), config, lambda: False, tmp_path)
        assert result.downloaded_images == result.cached_images == result.sessions == 1
        page.download_to_path.assert_called_once()
    catalog_root = gemini_media_dir(tmp_path)
    manifest = json.loads((catalog_root / "catalog.json").read_text())
    assert manifest["schema_version"] == 1
    record = manifest["assets"][0]
    assert record["conversation_url"] == conversation.url
    assert record["media_kind"] == "image"
    assert record["content_bytes"] == len(payload)
    assert "private" not in json.dumps(manifest)
    assert "source_url" not in record
    media_path = catalog_root / record["relative_path"]
    assert media_path.read_bytes() == payload
    assert build_gemini_media_initial_snapshot("test", tmp_path).downloaded_images == 1
    with _rendered_media_session([renewed], transfer) as page:
        result = sync_gemini_media(TaskState("test"), config, lambda: False, tmp_path)
        assert result.skipped_known == 1 and result.downloaded_images == 0
        page.download_to_path.assert_not_called()
    media_path.write_bytes(b"x" * len(payload))
    with _rendered_media_session([renewed], transfer) as page:
        result = sync_gemini_media(TaskState("test"), config, lambda: False, tmp_path)
        assert result.downloaded_images == 1 and result.skipped_known == 0
    assert media_path.read_bytes() == payload


def test_gemini_media_excluded_assets_never_redownload(tmp_path: Path, macos_host) -> None:
    candidate = _rendered_candidate()
    with _rendered_media_session([candidate], None) as page, patch(
        "app.core.gemini_media.BrowserDeletionCatalog.is_excluded", return_value=True,
    ) as excluded:
        result = sync_gemini_media(
            TaskState("test"), CrawlConfig(gemini_browser="safari"), lambda: False, tmp_path,
        )
        excluded.assert_called_once_with("gemini", candidate.asset_id)
        page.download_to_path.assert_not_called()
    assert result.skipped_excluded == 1
    assert result.cached_images == result.downloaded_images == 0


def test_gemini_media_discovery_rejects_unrendered_sessions_and_external_references() -> None:
    conversation = GeminiConversationLink("chat-1", "https://gemini.google.com/app/chat-1", "Chat")
    raw = {
        "source_url": "https://lh3.googleusercontent.com/gg-dl/image.png",
        "message_index": 1, "image_index": 0, "role": "assistant",
    }
    page = MagicMock()
    page.evaluate.return_value = {
        "message_count": 2,
        "images": [raw, dict(raw), {**raw, "source_url": "https://www.gstatic.com/avatar.png"}],
    }
    candidates, unsupported = discover_gemini_rendered_images(page, conversation)
    assert len(candidates) == unsupported == 1
    page.evaluate.return_value = {"message_count": 0, "images": []}
    with pytest.raises(RuntimeError, match="rendered message containers"):
        discover_gemini_rendered_images(page, conversation)


@pytest.mark.parametrize("payload, content_type", (
    (b"<html>Sign in</html>", "text/html"),
    (b"<html>Sign in</html>", "image/png"),
    (_png_bytes()[:30], "image/png"),
))
def test_gemini_media_never_catalogs_html_or_truncated_images(
    tmp_path: Path, payload: bytes, content_type: str,
) -> None:
    catalog = GeminiMediaCatalog(gemini_media_dir(tmp_path))
    page = MagicMock()

    def transfer(_url, destination, _stop, *, max_bytes, reject_redirects):
        destination.write_bytes(payload)
        return content_type, False

    page.download_to_path.side_effect = transfer
    with pytest.raises(RuntimeError, match="non-image|unsupported or incomplete"):
        _download_gemini_image(page, catalog, _rendered_candidate(), lambda: False, 1_048_576)
    assert catalog.cached_count == 0
    assert not catalog.path.exists()
    assert not list(catalog.root.glob("img_*"))
    assert not list((catalog.root / ".partial").glob("*.part"))


def test_gemini_media_session_failures_do_not_complete_successfully(tmp_path: Path, macos_host) -> None:
    state = TaskState("test")
    with _rendered_media_session([], None) as page, patch(
        "app.core.gemini_media.discover_gemini_rendered_images", side_effect=RuntimeError("not rendered"),
    ):
        result = sync_gemini_media(
            state, CrawlConfig(gemini_browser="safari"), lambda: False, tmp_path,
        )
        page.download_to_path.assert_not_called()
    assert result.failed_sessions == 1 and result.sessions == 0
    assert result.incomplete is True
    assert state.snapshot()["phase"] == "failed"


def test_gemini_media_cancelled_transfer_is_stopped_without_retry(tmp_path: Path, macos_host) -> None:
    stopped = False
    state = TaskState("test")

    def transfer(_url, destination, _stop, *, max_bytes, reject_redirects):
        nonlocal stopped
        destination.write_bytes(b"partial")
        stopped = True
        raise RuntimeError("Stop requested while downloading browser media.")

    with _rendered_media_session([_rendered_candidate()], transfer) as page:
        result = sync_gemini_media(
            state, CrawlConfig(gemini_browser="safari"), lambda: stopped, tmp_path,
        )
        page.download_to_path.assert_called_once()
    assert result.stopped is True
    assert result.failed_images == result.failed_sessions == result.cached_images == 0
    assert state.snapshot()["phase"] == "stopped"
    assert not list((gemini_media_dir(tmp_path) / ".partial").glob("*.part"))


@pytest.mark.parametrize("compact_for_safari", (False, True))
def test_gemini_media_discovers_attached_and_generated_images_in_observed_dom_shapes(
    disposable_browser: Browser, compact_for_safari: bool,
) -> None:
    context = disposable_browser.new_context()
    context.route("**/*", lambda route: route.abort())
    page = context.new_page()
    try:
        page.set_content("""
            <img src="https://lh3.googleusercontent.com/a/account-avatar">
            <user-query>
                <div class="avatar"><img src="https://lh3.googleusercontent.com/a/user"></div>
                <div class="attachment-container"><full-width-image>
                    <div class="full-width-image-container replace-fife-images-at-export">
                        <img class="image expandable ng-star-inserted"
                             src="https://lh3.googleusercontent.com/gg/attachment" alt="Attachment">
                    </div>
                </full-width-image></div>
            </user-query>
            <model-response>
                <div class="markdown markdown-main-panel md-content">
                    <div class="attachment-container unknown"><response-element class="no-md">
                        <full-width-image><div class="full-width-image-container replace-fife-images-at-export">
                            <img class="image expandable ng-star-inserted"
                                 src="https://lh3.googleusercontent.com/gg-dl/full-width" alt="Full width">
                        </div></full-width-image>
                    </response-element></div>
                    <generated-image class="luminous-layout">
                        <single-image class="generated-image large luminous-layout">
                            <div class="image-container replace-fife-images-at-export"><div class="overlay-container">
                                <button class="image-button"><img class="image animate"
                                    src="https://lh3.googleusercontent.com/gg-dl/generated" alt="Generated"></button>
                            </div></div>
                        </single-image>
                    </generated-image>
                    <img src="https://www.gstatic.com/logo.png">
                    <img src="https://i.ytimg.com/video-thumbnail.jpg">
                </div>
            </model-response>
        """)
        class RenderedPage:
            def evaluate(self, expression):
                if compact_for_safari:
                    expression = re.sub(r"\s+", " ", str(expression or "").strip())
                return page.evaluate(expression)

        candidates, unsupported = discover_gemini_rendered_images(
            RenderedPage(), GeminiConversationLink("chat-1", "https://gemini.google.com/app/chat-1", "Chat"),
        )
        assert [candidate.alt_text for candidate in candidates] == ["Attachment", "Full width", "Generated"]
        assert [candidate.role for candidate in candidates] == ["user", "assistant", "assistant"]
        assert unsupported == 2
    finally:
        context.close()


@pytest.mark.parametrize("reject_redirects", (False, True))
def test_safari_media_optional_redirect_policy_is_enforced_by_fetch(
    tmp_path: Path, disposable_browser: Browser, reject_redirects: bool,
) -> None:
    context = disposable_browser.new_context()
    context.route("**/*", lambda route: route.abort())
    browser_page = context.new_page()
    try:
        browser_page.set_content("<main>Isolated download bridge fixture</main>")
        browser_page.evaluate("""() => {
            window.fetch = async (_url, options) => {
                window.lastRedirectPolicy = options.redirect;
                if (options.redirect === 'error') throw new TypeError('Redirect rejected');
                return new Response(new Uint8Array([1, 2, 3]), {
                    status: 200, headers: { 'Content-Type': 'image/png' },
                });
            };
        }""")
        page = SafariPage(SafariContext("https://gemini.google.com/app"), window_id=123)
        destination = tmp_path / "redirect.part"
        with patch.object(page, "evaluate", side_effect=browser_page.evaluate):
            if reject_redirects:
                with pytest.raises(RuntimeError, match="Redirect rejected"):
                    page.download_to_path(
                        "https://lh3.googleusercontent.com/gg-dl/image", destination, lambda: False,
                        max_bytes=1024, reject_redirects=True,
                    )
                assert not destination.exists()
            else:
                page.download_to_path(
                    "https://lh3.googleusercontent.com/gg-dl/image", destination, lambda: False,
                    max_bytes=1024,
                )
                assert destination.read_bytes() == bytes([1, 2, 3])
        assert browser_page.evaluate("window.lastRedirectPolicy") == ("error" if reject_redirects else "follow")
    finally:
        context.close()
