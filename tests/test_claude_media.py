"""Focused tests for authenticated Claude rendered-image caching.

Code version: v1.2.0-codex.0
"""

from __future__ import annotations

from contextlib import contextmanager
import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from app.core.claude_media import (
    ClaudeMediaCatalog,
    ClaudeMediaSizeLimitError,
    ClaudeMediaSyncResult,
    _candidate_from_rendered_image,
    _download_claude_image,
    _validated_image_extension,
    build_claude_media_initial_snapshot,
    claude_media_dir,
    sync_claude_media,
)
from app.core.claude_history import ClaudeConversationLink
from app.core.claude_history_service import ClaudeHistoryService
from app.core.config import CrawlConfig
from app.core.job_lock import CacheTaskLock
from app.core.local_media_browser import LocalMediaCatalog
from app.core.state import TaskState


def _png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (2, 2), (0, 85, 204)).save(output, format="PNG")
    return output.getvalue()


def _rendered_candidate(image_index: int = 0):
    candidate = _candidate_from_rendered_image(
        {
            "source_url": f"https://claude.ai/images/image-{image_index}.png",
            "message_index": 0,
            "image_index": image_index,
            "role": "assistant",
        },
        ClaudeConversationLink("chat-1", "https://claude.ai/chat/chat-1", "Chat"),
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
    with patch("app.core.claude_media.SafariContext", return_value=context), patch(
        "app.core.claude_media.goto_with_retry"
    ), patch("app.core.claude_media._wait_for_claude_ready"), patch(
        "app.core.claude_media.discover_claude_conversations",
        return_value=[ClaudeConversationLink("chat-1", "https://claude.ai/chat/chat-1", "Chat")],
    ), patch("app.core.claude_media._prepare_claude_conversation_for_rendering"), patch(
        "app.core.claude_media.discover_claude_rendered_images", return_value=(candidates, 0),
    ):
        yield page
    context.__exit__.assert_called_once()


def test_claude_media_accepts_only_first_party_rendered_image_urls() -> None:
    conversation = ClaudeConversationLink("chat-1", "https://claude.ai/chat/chat-1", "Chat")
    raw = {"message_index": 1, "image_index": 0, "role": "assistant", "alt_text": "Plot"}

    assert _candidate_from_rendered_image(
        {**raw, "source_url": "https://assets.claude.ai/images/a.png?token=secret"},
        conversation,
    )
    for source_url in (
        "http://claude.ai/images/a.png",
        "https://claude.ai.evil.test/images/a.png",
        "https://evil.test/images/a.png",
        "https://user:pass@claude.ai/images/a.png",
        "data:image/png;base64,AAAA",
        "blob:https://claude.ai/id",
    ):
        assert _candidate_from_rendered_image(
            {**raw, "source_url": source_url},
            conversation,
        ) is None


def test_claude_safari_media_sync_saves_real_images_and_skips_known_bytes(
    tmp_path: Path, macos_host
) -> None:
    class _Page:
        def __init__(self) -> None:
            self.visited: list[str] = []
            self.downloads: list[str] = []

        def goto(self, url: str, **_kwargs) -> None:
            self.visited.append(url)

        def wait_for_timeout(self, _milliseconds: int) -> None:
            pass

        def title(self) -> str:
            return "Claude"

        def evaluate(self, script: str, *_args):
            if "document.body" in script:
                return "New chat"
            if "composerSelector" in script:
                return {"count": 1}
            if 'a[href], [role="link"]' in script:
                return [{"href": "https://claude.ai/chat/chat-1", "title": "Rendered chat"}]
            if "scrollHeight" in script:
                return {"moved": False, "scrollTop": 0, "scrollHeight": 0}
            if 'main [role="article"]' in script:
                return [
                    {
                        "source_url": "https://assets.claude.ai/images/image-1.png?token=private",
                        "message_index": 1,
                        "image_index": 0,
                        "role": "assistant",
                        "alt_text": "Blue plot",
                    },
                    {
                        "source_url": "https://external.example/image.png",
                        "message_index": 1,
                        "image_index": 1,
                        "role": "assistant",
                        "alt_text": "External",
                    },
                ]
            return None

        def download_to_path(
            self, source_url: str, destination: Path, _should_stop, *, max_bytes: int
        ) -> tuple[str, bool]:
            self.downloads.append(source_url)
            assert max_bytes == 1024 * 1024
            destination.write_bytes(_png_bytes())
            return "image/png", False

    page = _Page()

    class _Context:
        primary_page = page

        def __init__(self) -> None:
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            self.closed = True

    contexts: list[_Context] = []

    def new_context(*_args, **_kwargs) -> _Context:
        context = _Context()
        contexts.append(context)
        return context

    config = CrawlConfig(claude_browser="safari", max_media_file_size_mib=1)
    with patch("app.core.claude_media.SafariContext", side_effect=new_context) as safari:
        first = sync_claude_media(TaskState("test"), config, lambda: False, tmp_path)
        second = sync_claude_media(TaskState("test"), config, lambda: False, tmp_path)

    assert safari.call_count == 2
    assert all(context.closed for context in contexts)
    assert first.discovered_images == 1
    assert first.downloaded_images == 1
    assert first.cached_images == 1
    assert first.skipped_unsupported == 1
    assert first.incomplete is False
    assert second.downloaded_images == 0
    assert second.skipped_known == 1
    assert len(page.downloads) == 1
    assert page.visited[:3] == [
        "https://claude.ai/new",
        "https://claude.ai/chats",
        "https://claude.ai/chat/chat-1",
    ]
    catalog_root = claude_media_dir(tmp_path)
    manifest = json.loads((catalog_root / "catalog.json").read_text())
    assert manifest["schema_version"] == 1
    assert len(manifest["assets"]) == 1
    record = manifest["assets"][0]
    assert record["conversation_url"] == "https://claude.ai/chat/chat-1"
    assert record["alt_text"] == "Blue plot"
    assert record["content_bytes"] == len(_png_bytes())
    assert "private" not in json.dumps(manifest)
    assert (catalog_root / record["relative_path"]).read_bytes() == _png_bytes()
    assert build_claude_media_initial_snapshot("v-test", tmp_path).downloaded_images == 1
    media_path = catalog_root / record["relative_path"]
    media_path.write_bytes(b"x" * record["content_bytes"])
    with patch("app.core.claude_media.hashlib.sha256", side_effect=AssertionError("Status hashed an image")):
        assert build_claude_media_initial_snapshot("v-test", tmp_path).downloaded_images == 1
    candidate = _candidate_from_rendered_image(
        {
            "source_url": "https://assets.claude.ai/images/image-1.png?token=new",
            "message_index": 1,
            "image_index": 0,
            "role": "assistant",
        },
        ClaudeConversationLink("chat-1", "https://claude.ai/chat/chat-1", "Rendered chat"),
    )
    assert candidate is not None
    assert not ClaudeMediaCatalog(catalog_root).contains(candidate)
    media_path.write_bytes(_png_bytes())

    local_media = LocalMediaCatalog(tmp_path)
    active_items = [item for item in local_media.snapshot(force_refresh=True) if item.source == "claude"]
    assert len(active_items) == 1
    assert active_items[0].resource_key == record["asset_id"]
    local_media.delete(active_items[0].stable_id)
    assert not media_path.exists()
    with patch("app.core.claude_media.SafariContext", side_effect=new_context):
        after_delete = sync_claude_media(TaskState("test"), config, lambda: False, tmp_path)
    assert after_delete.downloaded_images == 0
    assert after_delete.skipped_excluded == 1
    assert len(page.downloads) == 1
    assert not media_path.exists()
    assert build_claude_media_initial_snapshot("v-test", tmp_path).downloaded_images == 0


@pytest.mark.parametrize("bridge_error", (
    None,
    "Safari media exceeds the configured cache limit.",
    "Safari media request failed: Safari media exceeds the configured cache limit.",
    "Safari media exceeds the 8-byte cache limit.",
    "Safari media request failed: Safari media exceeds the 8-byte cache limit.",
))
def test_claude_media_rejects_oversized_bytes_without_catalog_entry(
    tmp_path: Path, bridge_error: str | None,
) -> None:
    conversation = ClaudeConversationLink("chat-1", "https://claude.ai/chat/chat-1", "Chat")
    candidate = _candidate_from_rendered_image(
        {
            "source_url": "https://claude.ai/images/image-1.png",
            "message_index": 0,
            "image_index": 0,
            "role": "user",
        },
        conversation,
    )
    assert candidate is not None
    catalog = ClaudeMediaCatalog(claude_media_dir(tmp_path))

    class _Page:
        def download_to_path(
            self, _source_url: str, destination: Path, _should_stop, *, max_bytes: int
        ) -> tuple[str, bool]:
            assert max_bytes == 8
            if bridge_error:
                raise RuntimeError(bridge_error)
            destination.write_bytes(_png_bytes())
            return "image/png", False

    with pytest.raises(ClaudeMediaSizeLimitError, match="size limit|cache limit"):
        _download_claude_image(_Page(), catalog, candidate, lambda: False, 8)

    assert catalog.cached_count == 0
    assert not list(catalog.root.glob("img_*"))
    assert not list((catalog.root / ".partial").glob("*.part"))


def test_claude_media_accepts_exactly_the_configured_byte_limit(tmp_path: Path) -> None:
    payload = _png_bytes()
    catalog = ClaudeMediaCatalog(claude_media_dir(tmp_path))
    page = MagicMock()

    def transfer(_url, destination, _stop, *, max_bytes):
        assert max_bytes == len(payload)
        destination.write_bytes(payload)
        return "image/png", False

    page.download_to_path.side_effect = transfer
    assert _download_claude_image(page, catalog, _rendered_candidate(), lambda: False, len(payload))
    assert catalog.cached_count == 1
    assert [path.read_bytes() for path in catalog.root.glob("img_*")] == [payload]


@pytest.mark.parametrize("boundary", ("declared_length", "range_metadata", "stream", "measured_bytes"))
def test_claude_media_size_skip_does_not_retry_or_fail_the_sync(
    tmp_path: Path, macos_host, boundary: str,
) -> None:
    candidate = _rendered_candidate()
    state = TaskState("test")
    config = CrawlConfig(claude_browser="safari", max_media_file_size_mib=1)

    def transfer(_url, destination, _stop, *, max_bytes):
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
        result = sync_claude_media(state, config, lambda: False, tmp_path)
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
    assert not list(claude_media_dir(tmp_path).glob("img_*"))
    assert not list((claude_media_dir(tmp_path) / ".partial").glob("*.part"))


@pytest.mark.parametrize("failure", ("stream_error", "empty_response"))
def test_claude_media_real_failures_still_retry_and_mark_sync_incomplete(
    tmp_path: Path, macos_host, failure: str,
) -> None:
    state = TaskState("test")

    def transfer(_url, destination, _stop, *, max_bytes):
        if failure == "stream_error":
            destination.write_bytes(b"partial image")
            raise RuntimeError("Safari media request failed: stream interrupted before cache limit.")
        destination.write_bytes(b"")
        return "image/png", False

    with _rendered_media_session([_rendered_candidate()], transfer) as page:
        result = sync_claude_media(
            state, CrawlConfig(claude_browser="safari", max_media_file_size_mib=1),
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
    assert not list(claude_media_dir(tmp_path).glob("img_*"))
    assert not list((claude_media_dir(tmp_path) / ".partial").glob("*.part"))


def test_claude_size_skip_progress_preserves_cached_image_above_a_lowered_limit(
    tmp_path: Path, macos_host,
) -> None:
    output = io.BytesIO()
    Image.new("RGB", (800, 600), (0, 85, 204)).save(output, format="PNG", compress_level=0)
    payload = output.getvalue()
    assert 1_048_576 < len(payload) < 2_097_152
    catalog = ClaudeMediaCatalog(claude_media_dir(tmp_path))
    known = _rendered_candidate(1)
    seed_page = MagicMock()

    def seed_transfer(_url, destination, _stop, *, max_bytes):
        destination.write_bytes(payload)
        return "image/png", False

    seed_page.download_to_path.side_effect = seed_transfer
    assert _download_claude_image(seed_page, catalog, known, lambda: False, 2_097_152)
    manifest_before = catalog.path.read_bytes()
    state = TaskState("test")

    def reject_oversized(url, _destination, _stop, *, max_bytes):
        assert url != known.source_url
        raise RuntimeError(f"Safari media exceeds the {max_bytes:,}-byte cache limit.")

    with _rendered_media_session([_rendered_candidate(), known], reject_oversized) as page:
        result = sync_claude_media(
            state, CrawlConfig(claude_browser="safari", max_media_file_size_mib=1),
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


def test_claude_media_rejects_images_above_pixel_limit() -> None:
    with patch("app.core.claude_media.CLAUDE_MAX_IMAGE_PIXELS", 3):
        assert _validated_image_extension(_png_bytes()) == ""


@pytest.mark.parametrize("linked_component", ["claude", ".partial"])
def test_claude_media_refuses_symlinked_storage_directories(
    tmp_path: Path, linked_component: str
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    media_root = claude_media_dir(tmp_path / "local_store")
    media_root.parent.mkdir(parents=True)
    if linked_component == "claude":
        media_root.symlink_to(outside, target_is_directory=True)
        with pytest.raises(RuntimeError, match="symbolic link"):
            ClaudeMediaCatalog(media_root)
    else:
        media_root.mkdir()
        (media_root / ".partial").symlink_to(outside, target_is_directory=True)
        catalog = ClaudeMediaCatalog(media_root)
        candidate = _candidate_from_rendered_image(
            {
                "source_url": "https://claude.ai/images/image-1.png",
                "message_index": 0,
                "image_index": 0,
                "role": "user",
            },
            ClaudeConversationLink("chat-1", "https://claude.ai/chat/chat-1", "Chat"),
        )
        assert candidate is not None

        class _Page:
            def download_to_path(self, *_args, **_kwargs) -> None:
                raise AssertionError("Unsafe storage reached the browser download")

        with pytest.raises(RuntimeError, match="symbolic link"):
            _download_claude_image(_Page(), catalog, candidate, lambda: False, 1024)
    assert list(outside.iterdir()) == []


def test_claude_service_dispatches_media_mode_without_text_sync(tmp_path: Path) -> None:
    class _ImmediateThread:
        def __init__(self, *, target, **_kwargs) -> None:
            self.target = target

        def start(self) -> None:
            self.target()

    state = TaskState("test")
    service = ClaudeHistoryService(
        state,
        local_store_root=tmp_path,
        task_lock=CacheTaskLock(tmp_path / "cache-task.lock"),
    )
    result = ClaudeMediaSyncResult(
        sessions=1,
        discovered_images=1,
        cached_images=1,
        downloaded_images=1,
    )
    with patch("app.core.claude_history_service.Thread", _ImmediateThread), patch(
        "app.core.claude_history_service.sync_claude_media",
        return_value=result,
    ) as media_sync, patch(
        "app.core.claude_history_service.sync_claude_history",
        side_effect=AssertionError("Media must not launch the text pipeline"),
    ):
        service.start(CrawlConfig(claude_browser="safari"), content_mode="media")

    media_sync.assert_called_once()
    assert state.snapshot()["phase"] == "finished"
    assert state.snapshot()["performance_metrics"]["content_mode"] == "media"
    assert "1 local image files present" in state.snapshot()["message"]
