"""Focused tests for browser-rendered Claude history caching.

Code version: v1.1.1-codex.0
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.claude_history import (
    ClaudeConversationLink,
    ClaudeHistoryStore,
    ClaudeNoCacheableMessagesError,
    build_claude_initial_snapshot,
    claude_conversation_id,
    extract_claude_conversation_messages,
    sync_claude_history,
)
from app.core.config import CrawlConfig
from app.core.claude_history_service import ClaudeHistoryService
from app.core.job_lock import CacheTaskLock
from app.core.state import TaskSnapshot, TaskState


class _RenderedClaudePage:
    """Return one deterministic browser-rendered message payload."""

    def evaluate(self, script, *_args):
        assert "user-message" in script
        return {
            "title": "Rendered Claude chat",
            "modelLabel": "Sonnet 5 Medium",
            "messages": [
                {
                    "conversation_title": "Rendered Claude chat",
                    "turn_index": 1,
                    "message_index": 0,
                    "role": "user",
                    "author_label": "You",
                    "content_text": "Please summarize this.",
                    "content_html": "<p>Please summarize this.</p>",
                    "source_links": [],
                    "model_label": "",
                    "message_timestamp": "2026-09-03T01:00:00Z",
                },
                {
                    "conversation_title": "Rendered Claude chat",
                    "turn_index": 1,
                    "message_index": 1,
                    "role": "assistant",
                    "author_label": "Claude",
                    "content_text": "Here is the summary.",
                    "content_html": "<p>Here is the summary.</p>",
                    "source_links": ["https://example.com/source", "javascript:alert(1)"],
                    "model_label": "Sonnet 5 Medium",
                    "message_timestamp": "2026-09-03T01:00:05Z",
                },
            ],
        }


def test_claude_rendered_messages_are_normalized_before_storage() -> None:
    conversation = ClaudeConversationLink(
        "chat-1",
        "https://claude.ai/chat/chat-1",
        "Fallback title",
    )

    messages = extract_claude_conversation_messages(_RenderedClaudePage(), conversation)

    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[0]["content_text"] == "Please summarize this."
    assert messages[1]["model_label"] == "Sonnet 5 Medium"


def test_claude_store_round_trip_preserves_first_seen_metadata(tmp_path: Path) -> None:
    conversation = ClaudeConversationLink(
        "chat-1",
        "https://claude.ai/chat/chat-1",
        "Rendered Claude chat",
    )
    messages = extract_claude_conversation_messages(_RenderedClaudePage(), conversation)
    store = ClaudeHistoryStore(tmp_path / "llm" / "claude" / "history.parquet")

    first = store.replace_conversation(conversation, messages, "2026-09-03T01:01:00Z")
    store.save()
    first_seen = store.rows[0]["first_seen_at"]
    second = store.replace_conversation(conversation, messages, "2026-09-03T02:01:00Z")

    assert first.added_or_changed == 2
    assert first.unchanged_messages == 0
    assert first.unchanged_sessions == 0
    assert second.added_or_changed == 0
    assert second.unchanged_messages == 2
    assert second.unchanged_sessions == 1
    assert store.rows[0]["first_seen_at"] == first_seen
    assert store.cached_conversations == 1
    assert store.cached_messages == 2

    snapshot = build_claude_initial_snapshot("v-test", tmp_path)
    assert snapshot.downloaded_posts == 1
    assert snapshot.downloaded_tweets == 2
    assert "Found existing Claude history" in snapshot.message


def test_claude_urls_use_the_shared_conversation_contract() -> None:
    assert claude_conversation_id("https://claude.ai/chat/chat-1?ignored=1") == "chat-1"
    assert claude_conversation_id("https://claude.ai/project/project-1/chat/chat-1") == "chat-1"
    assert claude_conversation_id("https://example.com/chat/chat-1") == ""


def test_claude_store_rejects_sessions_without_rendered_messages(tmp_path: Path) -> None:
    store = ClaudeHistoryStore(tmp_path / "history.parquet")
    conversation = ClaudeConversationLink("chat-1", "https://claude.ai/chat/chat-1", "Empty")

    with pytest.raises(ClaudeNoCacheableMessagesError):
        store.replace_conversation(conversation, [], "2026-09-03T01:01:00Z")


def test_claude_safari_sync_caches_rendered_text_in_owned_context(
    tmp_path: Path, macos_host
) -> None:
    class _Page:
        def __init__(self) -> None:
            self.visited: list[str] = []

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
            if "a[href], [role=\"link\"]" in script:
                return [{"href": "https://claude.ai/chat/chat-1", "title": "Rendered chat"}]
            if "scrollHeight" in script:
                return {"moved": False, "scrollTop": 0, "scrollHeight": 0}
            if "user-message" in script:
                return _RenderedClaudePage().evaluate(script)
            return None

    page = _Page()

    class _Context:
        primary_page = page

        def __init__(self) -> None:
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            self.closed = True

    context = _Context()
    state = TaskState("test")

    with patch("app.core.claude_history.SafariContext", return_value=context) as safari, patch(
        "app.core.claude_history.sync_playwright_or_error",
        side_effect=AssertionError("Safari sync must not launch Playwright"),
    ):
        result = sync_claude_history(
            state,
            CrawlConfig(claude_browser="safari"),
            lambda: False,
            tmp_path,
        )

    safari.assert_called_once_with("https://claude.ai/new", lock_blocking=False)
    assert context.closed is True
    assert page.visited == [
        "https://claude.ai/new",
        "https://claude.ai/chats",
        "https://claude.ai/chat/chat-1",
    ]
    assert result["sessions"] == 1
    assert result["messages"] == 2
    assert result["failed"] == 0
    assert ClaudeHistoryStore(tmp_path / "llm" / "claude" / "history.parquet").cached_messages == 2


def test_claude_history_service_reports_partial_sync_as_incomplete(tmp_path: Path) -> None:
    class _ImmediateThread:
        def __init__(self, *, target, **_kwargs) -> None:
            self.target = target

        def start(self) -> None:
            self.target()

    state = TaskState("test", snapshot_factory=lambda version: TaskSnapshot(version=version))
    service = ClaudeHistoryService(
        state, local_store_root=tmp_path, task_lock=CacheTaskLock(tmp_path / "cache-task.lock")
    )
    with patch("app.core.claude_history_service.Thread", _ImmediateThread), patch(
        "app.core.claude_history_service.sync_claude_history",
        return_value={
            "sessions": 2, "messages": 1, "added_or_changed": 1,
            "unchanged": 0, "failed": 1, "stopped": False,
        },
    ), patch("app.core.claude_history_service.append_shadow_backup_completion") as backup:
        service.start(CrawlConfig(claude_browser="safari"))

    assert state.snapshot()["phase"] == "failed"
    assert "incomplete" in state.snapshot()["message"]
    assert "1 sessions failed" in state.snapshot()["message"]
    backup.assert_not_called()
