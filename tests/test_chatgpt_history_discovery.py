"""Require explicit account-history exhaustion without discarding verified sessions."""

# Code version: v1.1.0-codex.0

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest

from app.core.chatgpt_downloader import (
    ChatGPTHistoryStore,
    ChatGPTConversationIndexIncompleteError,
    _collect_all_chatgpt_conversation_urls_via_api,
    chatgpt_history_path_for_target,
    sync_chatgpt_images,
)
from app.core.config import CrawlConfig
from app.core.resource_persistence import read_parquet_rows
from app.core.state import TaskState


def _collect(pages):
    with patch("app.core.chatgpt_downloader._get_chatgpt_api_json", side_effect=pages):
        return _collect_all_chatgpt_conversation_urls_via_api(
            object(), "https://chatgpt.com/", {"authorization": "Bearer fixture"},
            TaskState("test"), lambda: False,
        )


@pytest.mark.parametrize("pages,expected", [
    ([{"items": [], "total": 0}], []),
    ([{"items": [{"id": "first"}], "total": 1}, {"items": [], "total": 1}],
     ["https://chatgpt.com/c/first"]),
])
def test_history_discovery_accepts_only_explicit_consistent_empty_page(pages, expected) -> None:
    assert _collect(pages) == expected


@pytest.mark.parametrize("ending", [
    {},
    {"items": None},
    {"items": [{"id": "first"}]},
    {"items": [], "total": 2},
    {"items": [], "total": True},
    RuntimeError("Fixture provider connection failed"),
])
def test_history_discovery_keeps_verified_urls_when_continuation_is_unproven(ending) -> None:
    with pytest.raises(ChatGPTConversationIndexIncompleteError) as error:
        _collect([{"items": [{"id": "first"}]}, ending])
    assert error.value.conversation_urls == ["https://chatgpt.com/c/first"]


@pytest.mark.parametrize("invalid_entry", [None, {}, {"id": "../foreign"}, {"id": 123}])
def test_history_discovery_retains_valid_rows_without_accepting_malformed_identity(invalid_entry) -> None:
    with pytest.raises(ChatGPTConversationIndexIncompleteError, match="stable identity") as error:
        _collect([{"items": [{"id": "first"}, invalid_entry]}])
    assert error.value.conversation_urls == ["https://chatgpt.com/c/first"]


def test_history_discovery_does_not_call_a_bounded_page_limit_complete() -> None:
    with patch("app.core.chatgpt_downloader.CHATGPT_API_PAGE_LIMIT", 1), pytest.raises(
        ChatGPTConversationIndexIncompleteError, match="page limit",
    ) as error:
        _collect([{"items": [{"id": "first"}]}])
    assert error.value.conversation_urls == ["https://chatgpt.com/c/first"]


@pytest.mark.parametrize("terminal_total", [0, 1, None])
def test_history_discovery_does_not_treat_total_hints_as_a_fixed_catalog_count(terminal_total) -> None:
    assert _collect([
        {"items": [{"id": "first"}], "total": 2},
        {"items": [], "total": terminal_total},
    ]) == ["https://chatgpt.com/c/first"]


def test_real_page_relative_totals_continue_until_an_explicit_empty_page() -> None:
    pages = [
        {"items": [{"id": f"session-{offset + index}"} for index in range(100)],
         "total": offset + 101, "offset": offset, "limit": 100}
        for offset in (0, 100, 200)
    ]
    pages.append({"items": [], "total": 300, "offset": 300, "limit": 100})
    with patch("app.core.chatgpt_downloader._get_chatgpt_api_json", side_effect=pages) as request:
        result = _collect_all_chatgpt_conversation_urls_via_api(
            object(), "https://chatgpt.com/", {"authorization": "Bearer fixture"},
            TaskState("test"), lambda: False,
        )
    assert len(result) == 300
    assert len(set(result)) == 300
    assert [int(parse_qs(urlsplit(call.args[1]).query)["offset"][0]) for call in request.call_args_list] == [
        0, 100, 200, 300,
    ]


def test_empty_page_cannot_complete_a_catalog_that_still_advertises_more_sessions() -> None:
    pages = [
        {"items": [{"id": f"session-{offset + index}"} for index in range(100)],
         "total": offset + 101, "offset": offset, "limit": 100}
        for offset in (0, 100, 200)
    ]
    pages.append({"items": [], "total": 301, "offset": 300, "limit": 100})
    with pytest.raises(ChatGPTConversationIndexIncompleteError, match="more sessions") as error:
        _collect(pages)
    assert len(error.value.conversation_urls) == 300


@pytest.mark.parametrize("field,value", [
    ("offset", 0), ("offset", -1), ("offset", "1"), ("offset", True), ("offset", None),
    ("limit", 1), ("limit", "100"), ("limit", True), ("limit", None),
])
def test_history_discovery_rejects_mismatched_pagination_readback(field, value) -> None:
    with pytest.raises(ChatGPTConversationIndexIncompleteError, match=field) as error:
        _collect([
            {"items": [{"id": "first"}], "offset": 0, "limit": 100},
            {"items": [{"id": "unverified"}], "offset": 1, "limit": 100, field: value},
        ])
    assert error.value.conversation_urls == ["https://chatgpt.com/c/first"]


def _payload(content: str):
    return {
        "title": "Fixture session", "current_node": "user",
        "mapping": {"user": {"parent": None, "message": {
            "id": "user", "author": {"role": "user"}, "content": {"parts": [content]},
        }}},
    }


@pytest.mark.parametrize("mode", ["text", "media"])
def test_sync_preserves_verified_partial_history_and_reports_incomplete(
    tmp_path: Path, macos_host, mode: str,
) -> None:
    target = tmp_path / "media" / "chatgpt" / "fixture"
    path = chatgpt_history_path_for_target(target)
    old_url = "https://chatgpt.com/c/old"
    store = ChatGPTHistoryStore(path)
    store.replace_conversation(old_url, _payload("Previously cached content"), "2026-10-22T00:00:00Z")
    store.save()
    prior_rows = read_parquet_rows(path)
    state = TaskState("test")
    context = SimpleNamespace(new_page=lambda: object())
    with patch(
        "app.core.chatgpt_downloader._launch_chatgpt_browser_context", return_value=nullcontext(context),
    ), patch("app.core.chatgpt_downloader.open_chatgpt_page"), patch(
        "app.core.chatgpt_downloader._load_chatgpt_session_request_headers",
        return_value={"authorization": "Bearer fixture"},
    ), patch("app.core.chatgpt_downloader._get_chatgpt_api_json", side_effect=[
        {"items": [{"id": "new"}]}, {},
    ]), patch(
        "app.core.chatgpt_downloader._get_chatgpt_api_json_via_page", return_value=_payload("New verified content"),
    ), patch(
        "app.core.chatgpt_downloader._iter_chatgpt_conversation_results", return_value=iter(()),
    ) as media_scan:
        result = sync_chatgpt_images(
            state, config=CrawlConfig(chatgpt_browser="safari", chatgpt_project_url=""),
            target_dir=target, content_mode=mode,
        )

    assert result.incomplete and not result.stopped
    assert result.discovered_conversations == 1
    assert state.snapshot()["discovery_complete"] is False
    rows = read_parquet_rows(path)
    assert [row for row in rows if row["conversation_url"] == old_url] == prior_rows
    if mode == "text":
        assert result.cached_messages == 2
        assert any(row["content_text"] == "New verified content" for row in rows)
    else:
        assert media_scan.call_args.args[0] == ["https://chatgpt.com/c/new"]


def test_explicitly_empty_text_history_is_a_complete_sync(tmp_path: Path, macos_host) -> None:
    state = TaskState("test")
    context = SimpleNamespace(new_page=lambda: object())
    with patch(
        "app.core.chatgpt_downloader._launch_chatgpt_browser_context", return_value=nullcontext(context),
    ), patch("app.core.chatgpt_downloader.open_chatgpt_page"), patch(
        "app.core.chatgpt_downloader._load_chatgpt_session_request_headers",
        return_value={"authorization": "Bearer fixture"},
    ), patch("app.core.chatgpt_downloader._get_chatgpt_api_json", return_value={"items": [], "total": 0}):
        result = sync_chatgpt_images(
            state, config=CrawlConfig(chatgpt_browser="safari"),
            target_dir=tmp_path / "media" / "chatgpt" / "empty", content_mode="text",
        )
    assert not result.incomplete and result.cached_messages == 0
    assert state.snapshot()["discovery_complete"] is True
