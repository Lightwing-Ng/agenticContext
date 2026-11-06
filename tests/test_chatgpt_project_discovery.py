"""Require verified exhaustion of ChatGPT project session discovery.

Code version: v1.1.0-codex.0
"""

import hashlib
import io
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image

from app.core import chatgpt_downloader as downloader
from app.core.config import CrawlConfig
from app.core.state import TaskState


PROJECT = "https://chatgpt.com/g/g-p-" + "a" * 32
FIRST = PROJECT + "/c/first"
HEADERS = {"authorization": "Bearer fixture"}


def collect(pages, should_stop=lambda: False):
    with patch.object(downloader, "_get_chatgpt_api_json", side_effect=pages):
        return downloader._collect_project_conversation_urls_via_api(
            object(), PROJECT, HEADERS, TaskState("test"), should_stop,
        )


def test_project_api_accepts_a_terminal_page_with_an_optional_total():
    assert collect([{"items": [{"id": "first"}], "cursor": "", "total": 1}]) == [FIRST]


def test_project_cursor_exhaustion_does_not_require_a_fixed_total():
    assert collect([
        {"items": [{"id": "first"}], "cursor": "next", "total": 101},
        {"items": [{"id": "second"}], "cursor": "", "total": 201},
    ]) == [FIRST, PROJECT + "/c/second"]


@pytest.mark.parametrize("ending", [
    {}, {"items": None}, {"items": [{"id": "first"}], "cursor": ""},
    {"items": [], "cursor": "still-more"},
    {"items": [{"id": "second"}], "cursor": "next"},
    {"items": [{"id": "second"}], "cursor": 2},
    {"items": [{"id": "second"}], "cursor": " "},
    {"items": [], "cursor": "", "total": True},
    {"items": [], "cursor": "", "total": -1},
    RuntimeError("Fixture later page unavailable"),
])
def test_later_project_page_failures_preserve_verified_sessions(ending):
    with pytest.raises(downloader.ChatGPTConversationIndexIncompleteError) as error:
        collect([{"items": [{"id": "first"}], "cursor": "next"}, ending])
    assert error.value.conversation_urls[0] == FIRST


@pytest.mark.parametrize("entry", [None, {}, {"id": "../foreign"}, {"id": 123}])
def test_project_page_keeps_valid_entries_when_an_identity_is_invalid(entry):
    with pytest.raises(downloader.ChatGPTConversationIndexIncompleteError, match="stable identity") as error:
        collect([{"items": [{"id": "first"}, entry], "cursor": ""}])
    assert error.value.conversation_urls == [FIRST]


def test_project_api_limit_does_not_claim_exhaustion(monkeypatch):
    monkeypatch.setattr(downloader, "CHATGPT_API_PAGE_LIMIT", 1)
    with pytest.raises(downloader.ChatGPTConversationIndexIncompleteError, match="page limit") as error:
        collect([{"items": [{"id": "first"}], "cursor": "next"}])
    assert error.value.conversation_urls == [FIRST]


def test_stop_between_project_pages_returns_the_verified_sessions():
    stopped = False
    def first_page(*args, **kwargs):
        nonlocal stopped
        stopped = True
        return {"items": [{"id": "first"}], "cursor": "next"}
    with patch.object(downloader, "_get_chatgpt_api_json", side_effect=first_page) as request:
        result = downloader._collect_project_conversation_urls_via_api(
            object(), PROJECT, HEADERS, TaskState("test"), lambda: stopped,
        )
    assert result == [FIRST]
    assert request.call_count == 1


def test_partial_api_failure_is_not_hidden_by_a_dom_fallback():
    page = SimpleNamespace(context=object())
    with patch.object(downloader, "open_chatgpt_page"), patch.object(
        downloader, "_get_chatgpt_api_json", side_effect=[
            {"items": [{"id": "first"}], "cursor": "next"}, {"items": None},
        ],
    ), patch.object(downloader, "_wait_for_project_conversation_links") as dom, pytest.raises(
        downloader.ChatGPTConversationIndexIncompleteError,
    ):
        downloader.collect_project_conversation_urls(
            page, PROJECT, TaskState("test"), lambda: False, request_headers=HEADERS.copy(),
        )
    dom.assert_not_called()


def test_explicitly_empty_project_api_does_not_fall_back_to_dom_waiting():
    page = SimpleNamespace(context=object())
    with patch.object(downloader, "open_chatgpt_page"), patch.object(
        downloader, "_get_chatgpt_api_json", return_value={"items": [], "cursor": "", "total": 0},
    ), patch.object(downloader, "_wait_for_project_conversation_links") as dom:
        result = downloader.collect_project_conversation_urls(
            page, PROJECT, TaskState("test"), lambda: False, request_headers=HEADERS.copy(),
        )
    assert result == []
    dom.assert_not_called()


@pytest.mark.parametrize("failure", ["unclickable", "stagnant", "limit"])
def test_dom_fallback_keeps_verified_sessions_but_reports_unfinished_pagination(failure, monkeypatch):
    page = SimpleNamespace(context=object(), wait_for_timeout=lambda milliseconds: None)
    if failure == "limit":
        monkeypatch.setattr(downloader, "CHATGPT_PROJECT_LOAD_ROUNDS", 1)
    with patch.object(downloader, "open_chatgpt_page"), patch.object(
        downloader, "_get_chatgpt_api_json", side_effect=RuntimeError("Fixture API unavailable"),
    ), patch.object(downloader, "_wait_for_project_conversation_links", return_value=[FIRST]), patch.object(
        downloader, "_has_load_more_conversations", return_value=True,
    ), patch.object(downloader, "_click_load_more_conversations", return_value=failure != "unclickable"), patch.object(
        downloader, "_extract_project_links", return_value=[FIRST],
    ), pytest.raises(downloader.ChatGPTConversationIndexIncompleteError) as error:
        downloader.collect_project_conversation_urls(
            page, PROJECT, TaskState("test"), lambda: False, request_headers=HEADERS.copy(),
        )
    assert error.value.conversation_urls == [FIRST]


def test_first_api_failure_can_still_use_an_exhausted_dom_fallback():
    page = SimpleNamespace(context=object())
    with patch.object(downloader, "open_chatgpt_page"), patch.object(
        downloader, "_get_chatgpt_api_json", side_effect=RuntimeError("Fixture API unavailable"),
    ), patch.object(downloader, "_wait_for_project_conversation_links", return_value=[FIRST]), patch.object(
        downloader, "_has_load_more_conversations", return_value=False,
    ):
        result = downloader.collect_project_conversation_urls(
            page, PROJECT, TaskState("test"), lambda: False, request_headers=HEADERS.copy(),
        )
    assert result == [FIRST]


def png(color):
    stream = io.BytesIO()
    Image.new("RGB", (2, 2), color).save(stream, format="PNG")
    return stream.getvalue()


def test_project_media_sync_retains_old_bytes_and_downloads_verified_partial_sessions(tmp_path, macos_host):
    target = tmp_path / "media" / "chatgpt" / "fixture"
    target.mkdir(parents=True)
    old = downloader.ChatGPTImageCandidate(
        "https://chatgpt.com/backend-api/estuary/content?id=file_old", "file_old", PROJECT + "/c/old",
    )
    old_bytes = png("red")
    old_path = target / "img_file_old.png"
    old_path.write_bytes(old_bytes)
    catalog = downloader.ChatGPTImageCatalog(target)
    catalog.register_download(
        old, old_path.name, hashlib.sha256(old_bytes).hexdigest(), len(old_bytes),
        "2026-10-22T00:00:00Z", image_payload=old_bytes,
    )
    before = hashlib.sha256(old_path.read_bytes()).hexdigest()
    context = SimpleNamespace()
    page = SimpleNamespace(context=context)
    context.pages = [page]
    context.new_page = lambda: page
    state = TaskState("test")
    scanned = []

    def observe(page, headers):
        headers.update(HEADERS)

    def scan(urls, descriptor, cache, directory, *args, **kwargs):
        scanned.extend(urls)
        candidate = downloader.ChatGPTImageCandidate(
            "https://chatgpt.com/backend-api/estuary/content?id=file_new", "file_new", FIRST,
        )
        content = png("blue")
        path = directory / "img_file_new.png"
        path.write_bytes(content)
        cache.register_download(
            candidate, path.name, hashlib.sha256(content).hexdigest(), len(content),
            "2026-10-22T00:00:00Z", image_payload=content,
        )
        yield downloader.ChatGPTConversationWorkResult(1, FIRST, (candidate.file_id,), downloaded_count=1)

    with patch.object(downloader, "_launch_chatgpt_browser_context", return_value=nullcontext(context)), patch.object(
        downloader, "open_chatgpt_page",
    ), patch.object(downloader, "_listen_for_chatgpt_request_headers", side_effect=observe), patch.object(
        downloader, "_get_chatgpt_api_json", side_effect=[
            {"items": [{"id": "first"}], "cursor": "next"}, {"items": None},
        ],
    ), patch.object(downloader, "collect_chatgpt_project_index_images", return_value=[]), patch.object(
        downloader, "_iter_chatgpt_conversation_results", side_effect=scan,
    ):
        result = downloader.sync_chatgpt_images(
            state, config=CrawlConfig(chatgpt_browser="safari", chatgpt_project_url=PROJECT),
            target_dir=target, content_mode="media",
        )

    assert result.incomplete
    assert result.downloaded_count == 1
    assert result.cached_count == 2
    assert result.failed_count == 0
    assert state.snapshot()["discovery_complete"] is False
    assert scanned == [FIRST]
    assert hashlib.sha256(old_path.read_bytes()).hexdigest() == before
    assert (target / "img_file_new.png").is_file()
