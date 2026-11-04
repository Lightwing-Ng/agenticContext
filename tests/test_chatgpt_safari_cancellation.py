"""Verify Safari image cancellation preserves partial bytes and releases claims."""

# Code version: v1.0.0-codex.0

from collections import deque
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core.chatgpt_downloader import (
    ChatGPTImageCandidate,
    ChatGPTImageCatalog,
    _chatgpt_file_download_url,
    _drain_chatgpt_index_candidates,
    download_chatgpt_image,
)
from app.core.safari_automation import SafariContext, SafariPage
from app.core.compute_queue import BoundedWorkQueue


def _image_download(tmp_path: Path):
    candidate = ChatGPTImageCandidate(
        source_url="https://chatgpt.com/backend-api/estuary/content?id=file_cancel",
        file_id="file_cancel",
        conversation_url="https://chatgpt.com/c/cancel",
        request_headers={"authorization": "Bearer fixture"},
    )
    context = SafariContext(candidate.conversation_url)
    page = SafariPage(context, window_id=123)
    context.pages.append(page)
    target = tmp_path / "media" / "chatgpt" / "fixture"
    return candidate, context, page, target, ChatGPTImageCatalog.build(target)


@pytest.mark.parametrize("refresh", [False, True])
def test_safari_stream_stop_preserves_partial_and_does_not_retry_or_register(tmp_path: Path, refresh: bool) -> None:
    candidate, context, page, target, catalog = _image_download(tmp_path)
    stop = Event()
    should_stop = stop.is_set
    callbacks = []
    partials = []

    def stream(url, destination, callback, **kwargs):
        callbacks.append(callback)
        if refresh and url == candidate.source_url:
            raise RuntimeError("Safari media request returned HTTP 403 with 0 bytes.")
        destination.write_bytes(b"partial image bytes")
        partials.append(destination)
        stop.set()
        if callback():
            raise RuntimeError("Stop requested while downloading browser media.")
        pytest.fail("The Safari byte stream did not receive the active Stop callback.")

    metadata = SimpleNamespace(ok=True, status=200, text=lambda: '{"download_url":"https://storage.example/original.png"}')
    with patch.object(page, "download_to_path", side_effect=stream) as transfer, patch.object(
        context.request, "get_from_page", return_value=metadata,
    ) as metadata_get, patch.object(catalog, "register_download") as register, patch(
        "app.core.chatgpt_downloader.time.sleep",
    ) as sleep, pytest.raises(RuntimeError, match="interrupted"):
        download_chatgpt_image(context, catalog, target, candidate, should_stop=should_stop)

    assert transfer.call_count == (2 if refresh else 1)
    assert all(callback is should_stop for callback in callbacks)
    assert partials[0].read_bytes() == b"partial image bytes"
    assert list(target.glob("img_*")) == []
    if refresh:
        assert metadata_get.call_args.kwargs["should_stop"] is should_stop
        assert metadata_get.call_args.args[0] is page
    register.assert_not_called()
    sleep.assert_not_called()
    assert catalog.claim_download(candidate.file_id)
    catalog.release_download(candidate.file_id)


def test_safari_metadata_stop_does_not_start_a_byte_stream(tmp_path: Path) -> None:
    candidate, context, page, target, catalog = _image_download(tmp_path)
    candidate.source_url = _chatgpt_file_download_url(candidate.file_id)
    stop = Event()
    should_stop = stop.is_set

    def metadata_get(_page, _url, **kwargs):
        stop.set()
        assert kwargs["should_stop"]()
        raise RuntimeError("Safari request interrupted.")

    with patch.object(context.request, "get_from_page", side_effect=metadata_get) as metadata, patch.object(
        page, "download_to_path",
    ) as transfer, pytest.raises(RuntimeError, match="interrupted"):
        download_chatgpt_image(context, catalog, target, candidate, should_stop=should_stop)

    assert metadata.call_args.kwargs["should_stop"] is should_stop
    transfer.assert_not_called()
    assert catalog.claim_download(candidate.file_id)
    catalog.release_download(candidate.file_id)


def test_safari_retry_cooldown_can_stop_before_another_transfer(tmp_path: Path) -> None:
    candidate, context, page, target, catalog = _image_download(tmp_path)
    stop = Event()
    should_stop = stop.is_set

    def cooldown(_delay, callback):
        assert callback is should_stop
        stop.set()
        return True

    with patch.object(page, "download_to_path", side_effect=RuntimeError("Load failed")) as transfer, patch(
        "app.core.chatgpt_downloader.wait_for_cache_scan", side_effect=cooldown,
    ) as wait, pytest.raises(RuntimeError, match="interrupted"):
        download_chatgpt_image(context, catalog, target, candidate, should_stop=should_stop)

    transfer.assert_called_once()
    wait.assert_called_once()
    assert catalog.claim_download(candidate.file_id)
    catalog.release_download(candidate.file_id)


def test_safari_index_worker_stops_without_processing_the_next_image(tmp_path: Path) -> None:
    candidate, context, page, target, catalog = _image_download(tmp_path)
    pending = deque([candidate, candidate])
    results = BoundedWorkQueue(4)
    stop = Event()

    def stream(_url, destination, callback, **kwargs):
        destination.write_bytes(b"partial")
        stop.set()
        assert callback()
        raise RuntimeError("Stop requested while downloading browser media.")

    with patch.object(page, "download_to_path", side_effect=stream) as transfer:
        _drain_chatgpt_index_candidates(
            context, pending, catalog, target, stop.is_set, results, {}, 0,
        )

    transfer.assert_called_once()
    assert len(pending) == 1
    assert results.empty()
    assert catalog.claim_download(candidate.file_id)
    catalog.release_download(candidate.file_id)
