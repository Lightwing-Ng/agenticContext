"""Keep verified media work when a real index continuation fails.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from contextlib import nullcontext
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from PIL import Image

from app.core import chatgpt_downloader as media
from app.core.config import CrawlConfig
from app.core.resource_persistence import read_parquet_rows
from app.core.state import TaskState


PROJECT_URL = "https://chatgpt.com/g/g-p-" + "a" * 32 + "/project"
CONVERSATIONS = [PROJECT_URL.removesuffix("/project") + f"/c/{identity}" for identity in ("known-session", "new-session")]
HEADERS = {"authorization": "Bearer synthetic-test-only"}
KNOWN_IMAGE = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360606060000000050001a5f645400000000049454e44ae426082"
)


def _new_image() -> bytes:
    stream = BytesIO()
    Image.new("RGB", (2, 1), (255, 0, 0)).save(stream, format="PNG")
    return stream.getvalue()


def _item(file_id: str, conversation_id: str) -> dict:
    return {
        "conversation_id": conversation_id,
        "asset_pointer": f"file-service://{file_id}",
        "url": f"https://chatgpt.com/backend-api/estuary/content?id={file_id}",
    }


class _JsonResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.ok = status == 200
        self.headers: dict[str, str] = {}

    def text(self) -> str:
        return json.dumps(self.payload)


class _IndexContext:
    """Serve the actual collector with first-page and continuation responses."""

    def __init__(self, outcome: str = "http500") -> None:
        self.outcome = outcome
        self.urls: list[str] = []
        self.page = object()
        self.request = SimpleNamespace(get=self.get)

    def get(self, url: str, **_kwargs) -> _JsonResponse:
        assert urlsplit(url).path == "/backend-api/my/recent/image_gen"
        self.urls.append(url)
        if self.outcome == "unavailable":
            return _JsonResponse({"error": "Synthetic first-page failure."}, 500)
        if "after" not in parse_qs(urlsplit(url).query):
            return _JsonResponse({
                "items": [_item("file_known", "known-session"), _item("file_new", "new-session")],
                "cursor": "continuation-one",
            })
        if self.outcome == "http500":
            return _JsonResponse({"error": "Synthetic continuation failure."}, 500)
        if self.outcome == "bad-items":
            return _JsonResponse({"items": None, "cursor": None})
        if self.outcome == "cursor-cycle":
            return _JsonResponse({"items": [_item("file_new", "new-session")], "cursor": "continuation-one"})
        return _JsonResponse({"items": [], "cursor": None})


class _ImageContext:
    def __init__(self) -> None:
        self.urls: list[str] = []
        self.request = SimpleNamespace(get=self.get)

    def get(self, url: str, **_kwargs):
        self.urls.append(url)
        assert parse_qs(urlsplit(url).query)["id"] == ["file_new"]
        payload = _new_image()
        return SimpleNamespace(ok=True, status=200, headers={"content-type": "image/png"}, body=lambda: payload)


def _seed_known_image(target: Path) -> Path:
    target.mkdir(parents=True)
    path = target / "img_file_known.png"
    path.write_bytes(KNOWN_IMAGE)
    candidate = media.ChatGPTImageCandidate(
        source_url=_item("file_known", "known-session")["url"],
        file_id="file_known", conversation_url=CONVERSATIONS[0],
    )
    assert media.ChatGPTImageCatalog(target).register_download(
        candidate, path.name, sha256(KNOWN_IMAGE).hexdigest(), len(KNOWN_IMAGE),
        "2026-10-21T00:00:00Z", image_payload=KNOWN_IMAGE,
    )
    return path


def _fake_sync_surfaces(monkeypatch, context: _IndexContext) -> tuple[_ImageContext, Mock]:
    """Replace browser ownership only; preserve the real collector and image writer."""

    descriptor = SimpleNamespace(engine="safari", label="Safari")
    monkeypatch.setattr(media, "browser_descriptors", lambda _config: {"safari": descriptor})
    monkeypatch.setattr(media, "_launch_chatgpt_browser_context", lambda *_args: nullcontext(context))
    monkeypatch.setattr(media, "_chatgpt_context_page", lambda active: active.page)
    monkeypatch.setattr(media, "open_chatgpt_page", Mock())
    monkeypatch.setattr(media, "_load_chatgpt_session_request_headers", lambda *_args, **_kwargs: dict(HEADERS))
    monkeypatch.setattr(media, "wait_for_cache_scan", lambda *_args: False)

    def collect_sessions(_page, _url, _state, _stop, **kwargs):
        kwargs["request_headers"].update(HEADERS)
        return list(CONVERSATIONS)

    monkeypatch.setattr(media, "collect_project_conversation_urls", collect_sessions)
    monkeypatch.setattr(media, "enrich_chatgpt_project_index_prompts", lambda _context, _url, candidates, *_args, **_kwargs: candidates)
    images = _ImageContext()

    def download_results(candidates, _descriptor, catalog, destination, should_stop, *_args, **_kwargs):
        for candidate in candidates:
            if should_stop():
                return
            downloaded = media.download_chatgpt_image(images, catalog, destination, candidate)
            yield media.ChatGPTImageDownloadWorkResult(
                candidate_file_id=candidate.file_id, downloaded=downloaded,
                skipped=not downloaded, already_cached=not downloaded,
            )

    monkeypatch.setattr(media, "_iter_chatgpt_index_image_results", download_results)
    legacy = Mock(side_effect=AssertionError("A partial index must not start legacy session scans."))
    monkeypatch.setattr(media, "_iter_chatgpt_conversation_results", legacy)
    return images, legacy


def _assert_native_page_sizes(urls: list[str]) -> None:
    assert parse_qs(urlsplit(urls[0]).query) == {"limit": ["25"]}
    assert len(urls) > 1
    for url in urls[1:]:
        assert parse_qs(urlsplit(url).query) == {"limit": ["20"], "after": ["continuation-one"]}


@pytest.mark.parametrize("outcome", ["http500", "bad-items", "cursor-cycle", "page-limit"])
def test_real_collector_preserves_verified_candidates_in_incomplete_error(monkeypatch, outcome: str) -> None:
    context = _IndexContext(outcome)
    monkeypatch.setattr(media, "wait_for_cache_scan", lambda *_args: False)
    if outcome == "page-limit":
        monkeypatch.setattr(media, "CHATGPT_API_PAGE_LIMIT", 1)
    with pytest.raises(media.ChatGPTImageIndexIncompleteError) as caught:
        media.collect_chatgpt_project_index_images(context, PROJECT_URL, CONVERSATIONS, dict(HEADERS), TaskState("test"), lambda: False)
    assert isinstance(caught.value, RuntimeError)
    assert {candidate.file_id for candidate in caught.value.candidates} == {"file_known", "file_new"}
    if outcome != "page-limit":
        _assert_native_page_sizes(context.urls)


def test_second_page_http500_still_caches_first_page_and_retry_keeps_saved_files(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "media/chatgpt/synthetic-project"
    known = _seed_known_image(target)
    known_before = (known.read_bytes(), known.stat().st_mtime_ns)
    context = _IndexContext()
    images, legacy = _fake_sync_surfaces(monkeypatch, context)
    state = TaskState("test")
    result = media.sync_chatgpt_images(state, CrawlConfig(chatgpt_browser="safari", chatgpt_project_url=PROJECT_URL), target)
    assert result.incomplete is True and result.stopped is False
    assert result.downloaded_count == 1 and result.cached_count == 2
    assert (known.read_bytes(), known.stat().st_mtime_ns) == known_before
    incoming = target / "img_file_new.png"
    assert incoming.read_bytes() == _new_image()
    assert len(images.urls) == 1
    assert state.snapshot()["discovery_complete"] is False
    assert state.snapshot()["processed_tweets"] == 2
    assert state.snapshot()["downloaded_images"] == 2
    _assert_native_page_sizes(context.urls)
    legacy.assert_not_called()
    rows = read_parquet_rows(target / media.CHATGPT_CATALOG_FILENAME)
    assert rows is not None and {row["file_id"] for row in rows} == {"file_known", "file_new"}

    saved = (incoming.read_bytes(), incoming.stat().st_mtime_ns)
    second_state = TaskState("retry")
    retried = media.sync_chatgpt_images(second_state, CrawlConfig(chatgpt_browser="safari", chatgpt_project_url=PROJECT_URL), target)
    assert retried.incomplete is True and retried.downloaded_count == 0
    assert retried.cached_count == 2 and len(images.urls) == 1
    assert (incoming.read_bytes(), incoming.stat().st_mtime_ns) == saved
    assert (known.read_bytes(), known.stat().st_mtime_ns) == known_before
    assert second_state.snapshot()["discovery_complete"] is False
    legacy.assert_not_called()


def test_partial_index_skips_optional_metadata_context_and_retains_download_progress(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "media/chatgpt/synthetic-project"
    known = _seed_known_image(target)
    before = (known.read_bytes(), known.stat().st_mtime_ns)
    context = _IndexContext()
    images, legacy = _fake_sync_surfaces(monkeypatch, context)
    launch = Mock(side_effect=[
        nullcontext(context),
        AssertionError("Partial discovery must not open a second metadata browser context."),
    ])
    enrich = Mock(side_effect=AssertionError("Partial discovery must not start optional prompt enrichment."))
    monkeypatch.setattr(media, "_launch_chatgpt_browser_context", launch)
    monkeypatch.setattr(media, "enrich_chatgpt_project_index_prompts", enrich)
    state = TaskState("test")

    result = media.sync_chatgpt_images(
        state, CrawlConfig(chatgpt_browser="safari", chatgpt_project_url=PROJECT_URL), target,
    )

    launch.assert_called_once()
    enrich.assert_not_called()
    legacy.assert_not_called()
    assert result.incomplete is True and result.stopped is False
    assert result.downloaded_count == 1 and result.cached_count == 2
    assert len(images.urls) == 1
    assert (known.read_bytes(), known.stat().st_mtime_ns) == before
    assert (target / "img_file_new.png").read_bytes() == _new_image()
    snapshot = state.snapshot()
    assert snapshot["processed_tweets"] == snapshot["queued_tweets"] == 2
    assert snapshot["downloaded_images"] == 2 and snapshot["discovery_complete"] is False


def test_stop_after_first_index_page_never_reports_complete_or_starts_transfers(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "media/chatgpt/synthetic-project"
    known = _seed_known_image(target)
    context = _IndexContext()
    images, legacy = _fake_sync_surfaces(monkeypatch, context)
    state = TaskState("test")

    def stopped() -> bool:
        return state.snapshot()["discovered_images"] >= 2

    result = media.sync_chatgpt_images(
        state, CrawlConfig(chatgpt_browser="safari", chatgpt_project_url=PROJECT_URL), target, should_stop=stopped,
    )
    assert result.stopped is True and state.snapshot()["discovery_complete"] is False
    assert images.urls == [] and len(context.urls) == 1
    assert known.read_bytes() == KNOWN_IMAGE
    assert not (target / "img_file_new.png").exists()
    legacy.assert_not_called()


def test_unrelated_runtime_error_is_not_misclassified_as_partial_index(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "media/chatgpt/synthetic-project"
    known = _seed_known_image(target)
    images, legacy = _fake_sync_surfaces(monkeypatch, _IndexContext())
    monkeypatch.setattr(media, "collect_chatgpt_project_index_images", Mock(side_effect=RuntimeError("Synthetic unrelated collector failure.")))
    with pytest.raises(RuntimeError, match="Synthetic unrelated collector failure"):
        media.sync_chatgpt_images(TaskState("test"), CrawlConfig(chatgpt_browser="safari", chatgpt_project_url=PROJECT_URL), target)
    assert known.read_bytes() == KNOWN_IMAGE and images.urls == []
    legacy.assert_not_called()


def test_first_page_unavailable_retains_existing_session_scan_fallback(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "media/chatgpt/synthetic-project"
    known = _seed_known_image(target)
    images, legacy = _fake_sync_surfaces(monkeypatch, _IndexContext("unavailable"))
    legacy.side_effect = None
    legacy.return_value = iter([
        media.ChatGPTConversationWorkResult(conversation_index=index, conversation_url=url)
        for index, url in enumerate(CONVERSATIONS, 1)
    ])
    result = media.sync_chatgpt_images(TaskState("test"), CrawlConfig(chatgpt_browser="safari", chatgpt_project_url=PROJECT_URL), target)
    assert result.incomplete is False and result.cached_count == 1
    assert known.read_bytes() == KNOWN_IMAGE and images.urls == []
    legacy.assert_called_once()
