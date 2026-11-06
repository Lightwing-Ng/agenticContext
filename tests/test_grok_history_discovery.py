"""Prove Grok catalog exhaustion before reporting complete history coverage."""

# Code version: v1.0.0-codex.0

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.core.config import CrawlConfig
from app.core.grok_history import (
    GrokConversation,
    GrokConversationDiscoveryIncompleteError,
    GrokHistoryStore,
    _normalized_messages,
    grok_history_path,
    list_grok_conversations,
    sync_grok_history,
)
from app.core.grok_history_service import GrokHistoryService
from app.core.job_lock import CacheTaskLock
from app.core.resource_persistence import read_parquet_rows
from app.core.state import TaskSnapshot, TaskState


def _page(identity="first", token=None):
    result = {"conversations": [{"conversationId": identity, "title": "Fixture session"}]}
    if token is not None:
        result["nextPageToken"] = token
    return result


@pytest.mark.parametrize("ending", [
    {"conversations": []},
    {"conversations": [], "nextPageToken": ""},
    {"conversations": [], "nextPageToken": None},
])
def test_grok_discovery_accepts_provider_terminal_token_forms(ending) -> None:
    with patch("app.core.grok_history._grok_api_json", side_effect=[_page(token="next"), ending]):
        conversations = list_grok_conversations(object())
    assert [item.conversation_id for item in conversations] == ["first"]


@pytest.mark.parametrize("ending", [
    {},
    {"conversations": [], "nextPageToken": "more"},
    {"conversations": [{"conversationId": "second"}], "nextPageToken": "next"},
    {"conversations": [{"conversationId": "first"}]},
    {"conversations": [{"conversationId": "second"}], "nextPageToken": 123},
    RuntimeError("Fixture provider connection failed"),
])
def test_grok_discovery_retains_verified_sessions_when_exhaustion_is_unproven(ending) -> None:
    with patch("app.core.grok_history._grok_api_json", side_effect=[_page(token="next"), ending]), pytest.raises(
        GrokConversationDiscoveryIncompleteError,
    ) as error:
        list_grok_conversations(object())
    assert error.value.conversations[0].conversation_id == "first"
    if isinstance(ending, dict) and ending.get("conversations") == [{"conversationId": "second"}]:
        assert [item.conversation_id for item in error.value.conversations] == ["first", "second"]


@pytest.mark.parametrize("invalid_entry", [None, {}, {"conversationId": "../foreign"}, {"conversationId": 123}])
def test_grok_discovery_retains_valid_entries_but_rejects_malformed_identity(invalid_entry) -> None:
    with patch("app.core.grok_history._grok_api_json", return_value={
        "conversations": [{"conversationId": "first"}, invalid_entry],
    }), pytest.raises(GrokConversationDiscoveryIncompleteError, match="stable identity") as error:
        list_grok_conversations(object())
    assert [item.conversation_id for item in error.value.conversations] == ["first"]


def test_grok_page_limit_cannot_be_reported_as_exhaustion() -> None:
    with patch("app.core.grok_history.GROK_CONVERSATION_PAGE_LIMIT", 1), patch(
        "app.core.grok_history._grok_api_json", return_value=_page(token="next"),
    ), pytest.raises(GrokConversationDiscoveryIncompleteError, match="page limit") as error:
        list_grok_conversations(object())
    assert [item.conversation_id for item in error.value.conversations] == ["first"]


def test_grok_discovery_checks_stop_before_another_page() -> None:
    calls = 0

    def fetch(*args):
        nonlocal calls
        calls += 1
        return _page(token="next")

    with patch("app.core.grok_history._grok_api_json", side_effect=fetch):
        conversations = list_grok_conversations(object(), should_stop=lambda: calls == 1)
    assert calls == 1
    assert [item.conversation_id for item in conversations] == ["first"]


def test_grok_partial_discovery_persists_verified_messages_and_preserves_old_sessions(
    tmp_path: Path, macos_host,
) -> None:
    path = grok_history_path(tmp_path)
    old = GrokConversation("old", "Old fixture", "", "", "https://grok.com/c/old")
    messages = _normalized_messages(
        old, [{"responseId": "r-old"}],
        {"r-old": {"responseId": "r-old", "sender": "human", "message": "Previously cached content"}},
        "2026-10-22T00:00:00Z",
    )
    GrokHistoryStore(path).replace_conversation(old, messages, "2026-10-22T00:00:00Z")
    prior = read_parquet_rows(path)
    state = TaskState("test", snapshot_factory=TaskSnapshot)
    page = SimpleNamespace(wait_for_timeout=Mock())
    context = SimpleNamespace(primary_page=page)
    list_calls = 0

    def fetch(_page, url, **kwargs):
        nonlocal list_calls
        if "conversations?" in url:
            list_calls += 1
            return _page_payload if list_calls == 1 else {"conversations": [], "nextPageToken": "unread"}
        if url.endswith("/response-node"):
            return {"responseNodes": [{"responseId": "r-new"}]}
        return {"responses": [{"responseId": "r-new", "sender": "assistant", "message": "New verified content"}]}

    _page_payload = _page("new", "next")
    with patch("app.core.grok_history.SafariContext", return_value=nullcontext(context)), patch(
        "app.core.grok_history._grok_api_json", side_effect=fetch,
    ):
        result = sync_grok_history(
            state, CrawlConfig(grok_browser="safari"), lambda: False, local_store_root=str(tmp_path),
        )

    assert result["incomplete"] and not result["stopped"]
    assert result["failed"] == 0 and result["messages"] == 1
    assert state.snapshot()["phase"] == "failed"
    assert state.snapshot()["discovery_complete"] is False
    rows = read_parquet_rows(path)
    assert [row for row in rows if row["conversation_id"] == "old"] == prior
    assert any(row["content_text"] == "New verified content" for row in rows)


def test_grok_service_rejects_incomplete_discovery_without_failed_sessions(tmp_path: Path) -> None:
    class ImmediateThread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    state = TaskState("test", snapshot_factory=TaskSnapshot)
    service = GrokHistoryService(
        state, local_store_root=tmp_path, task_lock=CacheTaskLock(tmp_path / "cache-task.lock"),
    )
    with patch("app.core.grok_history_service.Thread", ImmediateThread), patch(
        "app.core.grok_history_service.sync_grok_history",
        return_value={"sessions": 1, "messages": 1, "added_or_changed": 1, "unchanged": 0,
                      "failed": 0, "stopped": False, "incomplete": True},
    ), patch("app.core.grok_history_service.append_shadow_backup_completion") as backup:
        service.start(CrawlConfig(grok_browser="safari"))
    assert state.snapshot()["phase"] == "failed"
    assert "catalog exhaustion" in state.snapshot()["message"]
    backup.assert_not_called()
