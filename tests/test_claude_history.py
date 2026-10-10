"""Focused tests for browser-rendered Claude history caching.

Code version: v1.3.0-claude.0
"""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from app.core.claude_history import (
    ClaudeConversationLink,
    ClaudeConversationPayloadRecorder,
    ClaudeHistoryStore,
    ClaudeNoCacheableMessagesError,
    build_claude_initial_snapshot,
    claude_conversation_id,
    extract_claude_conversation_messages,
    messages_from_claude_payload,
    render_complete_claude_transcript,
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
            if "loginRequired" in script:
                return {"loading": False}
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


def test_claude_text_sync_stop_during_discovery_preserves_cached_history(tmp_path: Path) -> None:
    history_path = tmp_path / "llm/claude/history.parquet"
    store = ClaudeHistoryStore(history_path)
    conversation = ClaudeConversationLink("chat-1", "https://claude.ai/chat/chat-1", "Cached")
    store.replace_conversation(
        conversation, extract_claude_conversation_messages(_RenderedClaudePage(), conversation),
        "2026-09-03T01:01:00Z",
    )
    store.save()
    before = history_path.read_bytes()
    stop_requested = False

    def cancel_discovery(_page, *, should_stop):
        nonlocal stop_requested
        assert should_stop() is False
        stop_requested = True
        return []

    state = TaskState("test")
    with patch("app.core.claude_history._open_claude_history_page") as context, patch(
        "app.core.claude_history.goto_with_retry",
    ), patch("app.core.claude_history._wait_for_claude_ready"), patch(
        "app.core.claude_history.discover_claude_conversations", side_effect=cancel_discovery,
    ):
        context.return_value.__enter__.return_value = Mock()
        result = sync_claude_history(
            state, CrawlConfig(), lambda: stop_requested, tmp_path,
        )

    assert result["stopped"] is True
    assert result["failed"] == result["sessions"] == 0
    assert state.snapshot()["phase"] == "stopped"
    assert state.snapshot()["discovery_complete"] is False
    assert history_path.read_bytes() == before


_ROOT_UUID = "00000000-0000-4000-8000-000000000000"
_PAYLOAD_URL = (
    "https://claude.ai/api/organizations/org-1/chat_conversations/chat-1"
    "?tree=True&rendering_mode=messages"
)


def _claude_payload() -> dict:
    """Return a two-turn conversation with one abandoned edit branch."""

    return {
        "name": "Payload chat",
        "model": "claude-opus-5-5",
        "current_leaf_message_uuid": "m4",
        "chat_messages": [
            {"uuid": "m1", "parent_message_uuid": _ROOT_UUID, "sender": "human", "index": 0,
             "created_at": "2026-09-03T01:00:00Z", "text": "",
             "content": [{"type": "text", "text": "First question"}]},
            {"uuid": "m2", "parent_message_uuid": "m1", "sender": "assistant", "index": 1,
             "created_at": "2026-09-03T01:00:05Z", "text": "",
             "content": [
                 {"type": "thinking", "thinking": "private reasoning"},
                 {"type": "tool_use", "name": "web_search", "input": {}},
                 {"type": "text", "text": "**\u200bAnswer\u200b** part one", "citations": [
                     {"url": "https://example.com/a",
                      "sources": [{"url": "https://example.com/b"}, {"url": "javascript:x"}]},
                 ]},
                 {"type": "text", "text": "part two", "citations": []},
             ]},
            {"uuid": "m3-old", "parent_message_uuid": "m2", "sender": "human", "index": 2,
             "created_at": "2026-09-03T01:01:00Z", "text": "",
             "content": [{"type": "text", "text": "Abandoned edit"}]},
            {"uuid": "m3", "parent_message_uuid": "m2", "sender": "human", "index": 2,
             "created_at": "2026-09-03T01:02:00Z", "text": "Legacy text body", "content": []},
            {"uuid": "m4", "parent_message_uuid": "m3", "sender": "assistant", "index": 3,
             "created_at": "2026-09-03T01:02:05Z", "text": "",
             "content": [{"type": "text", "text": "Final answer"}]},
        ],
    }


def test_claude_payload_keeps_the_displayed_branch_as_markdown_text() -> None:
    conversation = ClaudeConversationLink("chat-1", "https://claude.ai/chat/chat-1", "Fallback")

    messages = messages_from_claude_payload(_claude_payload(), conversation)

    assert [message["content_text"] for message in messages] == [
        "First question",
        "**Answer** part one\n\npart two",
        "Legacy text body",
        "Final answer",
    ]
    assert [message["role"] for message in messages] == ["user", "assistant", "user", "assistant"]
    assert [message["message_index"] for message in messages] == [0, 1, 2, 3]
    assert [message["turn_index"] for message in messages] == [1, 1, 2, 2]
    assert messages[1]["source_links"] == ["https://example.com/a", "https://example.com/b"]
    assert messages[1]["model_label"] == "claude-opus-5-5"
    assert messages[0]["model_label"] == ""
    assert messages[3]["message_timestamp"] == "2026-09-03T01:02:05Z"
    assert {message["conversation_title"] for message in messages} == {"Payload chat"}


class _Response:
    def __init__(self, url: str, payload, *, status: int = 200, method: str = "GET") -> None:
        self.url = url
        self.status = status
        self.request = Mock(method=method)
        self._payload = payload

    def json(self):
        return self._payload


class _EventPage:
    """Emit recorded responses while waiting, like a Playwright page."""

    def __init__(self, responses_by_url: dict[str, list[_Response]] | None = None) -> None:
        self.handlers = []
        self.responses_by_url = responses_by_url or {}
        self.visited: list[str] = []

    def on(self, event: str, handler) -> None:
        assert event == "response"
        self.handlers.append(handler)

    def emit(self, response: _Response) -> None:
        for handler in self.handlers:
            handler(response)

    def goto(self, url: str, **_kwargs) -> None:
        self.visited.append(url)
        for response in self.responses_by_url.get(url, []):
            self.emit(response)

    def wait_for_timeout(self, _milliseconds: int) -> None:
        pass


def test_claude_payload_recorder_only_keeps_matching_conversation_responses() -> None:
    page = _EventPage()
    recorder = ClaudeConversationPayloadRecorder(page)
    page.emit(_Response(_PAYLOAD_URL, {"chat_messages": ["wrong method"]}, method="POST"))
    page.emit(_Response(_PAYLOAD_URL.replace("claude.ai", "example.com"), {"chat_messages": []}))
    page.emit(_Response(_PAYLOAD_URL.replace("chat-1?", "chat-1/composer_notices?"), {}))
    page.emit(_Response(_PAYLOAD_URL, {"chat_messages": []}, status=403))

    assert recorder.active is True
    assert recorder.wait_for_payload(page, "chat-1", lambda: False, timeout_seconds=0) is None

    page.emit(_Response(_PAYLOAD_URL, _claude_payload()))
    assert recorder.wait_for_payload(page, "chat-1", lambda: False)["name"] == "Payload chat"
    recorder.forget("chat-1")
    assert recorder.wait_for_payload(page, "chat-1", lambda: False, timeout_seconds=0) is None
    assert ClaudeConversationPayloadRecorder(object()).active is False


class _VirtualTranscriptPage:
    """Mount three rows around the scroll position after older rows are loaded."""

    def __init__(self, total: int, *, hidden_until_loaded: int) -> None:
        self.total = total
        self.first_loaded = hidden_until_loaded
        self.position = total - 3

    def wait_for_timeout(self, _milliseconds: int) -> None:
        pass

    def evaluate(self, script: str, arg=None):
        if "load earlier messages" in script:
            if self.first_loaded == 0:
                return False
            self.first_loaded = max(0, self.first_loaded - 4)
            return True
        if "toTop" in script:
            before = self.position
            self.position = self.first_loaded if arg["toTop"] else min(self.total - 3, before + 2)
            return {"moved": self.position != before, "atBottom": self.position >= self.total - 3}
        assert "user-message" in script
        start = max(self.position, self.first_loaded)
        rows = range(start, min(self.total, start + 3))
        return {"messages": [
            {"row_index": row - self.first_loaded, "role": "user" if row % 2 == 0 else "assistant",
             "content_text": f"message {row}", "message_index": 99, "turn_index": 99}
            for row in rows
        ]}


def test_claude_rendered_fallback_merges_every_virtualized_row() -> None:
    conversation = ClaudeConversationLink("chat-1", "https://claude.ai/chat/chat-1", "Long")
    page = _VirtualTranscriptPage(14, hidden_until_loaded=6)

    messages = render_complete_claude_transcript(page, conversation, lambda: False)

    assert [message["content_text"] for message in messages] == [
        f"message {row}" for row in range(14)
    ]
    assert [message["message_index"] for message in messages] == list(range(14))
    assert messages[-1]["turn_index"] == 7


def test_claude_rendered_fallback_fails_instead_of_skipping_an_unrendered_session() -> None:
    class _BlankPage:
        def wait_for_timeout(self, _milliseconds: int) -> None:
            pass

        def evaluate(self, _script: str, _arg=None):
            return {"messages": []}

    conversation = ClaudeConversationLink("chat-1", "https://claude.ai/chat/chat-1", "Blank")
    with pytest.raises(RuntimeError, match="did not render any messages"):
        render_complete_claude_transcript(_BlankPage(), conversation, lambda: False, timeout_seconds=0)


def test_claude_chromium_sync_caches_the_complete_recorded_conversation(tmp_path: Path) -> None:
    page = _EventPage({"https://claude.ai/chat/chat-1": [_Response(_PAYLOAD_URL, _claude_payload())]})
    page.evaluate = Mock(side_effect=AssertionError("the recorded payload must be used"))
    conversation = ClaudeConversationLink("chat-1", "https://claude.ai/chat/chat-1", "Listed")
    state = TaskState("test")

    with patch("app.core.claude_history._open_claude_history_page") as context, patch(
        "app.core.claude_history._wait_for_claude_ready",
    ), patch(
        "app.core.claude_history.discover_claude_conversations", return_value=[conversation],
    ):
        context.return_value.__enter__.return_value = page
        result = sync_claude_history(state, CrawlConfig(), lambda: False, tmp_path)

    store = ClaudeHistoryStore(tmp_path / "llm" / "claude" / "history.parquet")
    assert result["messages"] == 4
    assert result["failed"] == 0
    assert store.cached_messages == 4
    assert [row["content_text"] for row in store.rows][-1] == "Final answer"


def test_claude_sync_stops_on_a_verification_challenge_without_retrying(tmp_path: Path) -> None:
    page = _EventPage()
    page.evaluate = Mock(side_effect=AssertionError("a challenge page must not be evaluated"))
    conversations = [
        ClaudeConversationLink("chat-1", "https://claude.ai/chat/chat-1", "First"),
        ClaudeConversationLink("chat-2", "https://claude.ai/chat/chat-2", "Second"),
    ]
    state = TaskState("test")

    with patch("app.core.claude_history._open_claude_history_page") as context, patch(
        "app.core.claude_history._wait_for_claude_ready",
    ), patch(
        "app.core.claude_history.discover_claude_conversations", return_value=conversations,
    ), patch(
        "app.core.claude_history.page_shows_security_verification", return_value=True,
    ), pytest.raises(RuntimeError, match="human verification challenge"):
        context.return_value.__enter__.return_value = page
        sync_claude_history(state, CrawlConfig(), lambda: False, tmp_path)

    assert page.visited == ["https://claude.ai/new", "https://claude.ai/chat/chat-1"]
