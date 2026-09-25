"""Focused tests for Grok text-history persistence and API pagination."""

# Code version: v1.5.1-codex.0

import json
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from app.core.chat_history_browser import query_chat_history
from app.core.grok_history import (
    GROK_API_REQUEST_TIMEOUT_MS,
    GrokConversation,
    GrokHistoryStore,
    GrokTextMessage,
    _grok_api_json,
    extract_grok_inline_citations,
    list_grok_conversations,
    normalize_grok_display_markdown,
    sync_grok_history,
)
from app.core.config import CrawlConfig
from app.core.safari_automation import SafariContext, SafariPage, SafariResponse
from app.core.state import TaskSnapshot, TaskState


def _message(key: str, role: str, index: int, content: str) -> GrokTextMessage:
    return GrokTextMessage(
        message_key=key,
        platform="grok",
        conversation_id="conversation-1",
        conversation_title="Test session",
        conversation_url="https://grok.com/c/conversation-1",
        role=role,
        author_label="You" if role == "user" else "Grok",
        content_text=content,
        content_html="",
        timestamp=f"2026-08-13T00:0{index}:00Z",
        turn_index=index + 1 if role == "user" else index,
        message_index=index,
        model_label="grok-4" if role == "assistant" else "",
        source_links=(),
        content_sha256=f"sha-{key}",
    )


def test_grok_history_store_round_trips_and_preserves_first_seen(tmp_path: Path) -> None:
    """Keep stable rows and update only their latest observation timestamp."""
    path = tmp_path / "llm" / "grok" / "history.parquet"
    conversation = GrokConversation(
        "conversation-1",
        "Test session",
        "2026-08-13T00:00:00Z",
        "2026-08-13T00:01:00Z",
        "https://grok.com/c/conversation-1",
    )
    store = GrokHistoryStore(path)
    first = store.replace_conversation(
        conversation,
        [_message("conversation-1:r1", "user", 0, "hello")],
        "2026-08-13T00:02:00Z",
    )
    second = store.replace_conversation(
        conversation,
        [_message("conversation-1:r1", "user", 0, "hello")],
        "2026-08-13T00:03:00Z",
    )

    assert first.added_or_changed == 1
    assert second.added_or_changed == 0
    assert second.unchanged == 1
    page = query_chat_history(tmp_path, source="grok", session_view=True)
    assert page.total_count == 1
    assert page.sessions[0].conversation_title == "Test session"
    assert page.items[0].content_text == "hello"


def test_grok_history_is_included_in_all_source_queries(tmp_path: Path) -> None:
    """Make the Local resources all-sources view include Grok rows."""
    path = tmp_path / "llm" / "grok" / "history.parquet"
    conversation = GrokConversation(
        "conversation-1",
        "Test session",
        "",
        "",
        "https://grok.com/c/conversation-1",
    )
    GrokHistoryStore(path).replace_conversation(
        conversation,
        [_message("conversation-1:r1", "assistant", 0, "answer")],
        "2026-08-13T00:02:00Z",
    )

    page = query_chat_history(tmp_path, source="all")
    assert page.total_count == 1
    assert page.items[0].source == "grok"


def test_grok_inline_citations_follow_card_identity_and_reject_unsafe_urls() -> None:
    marker = (
        '<grok:render card_id="safe-card" card_type="citation_card" '
        'type="render_inline_citation"><argument name="citation_id">107</argument>'
        '</grok:render>'
    )
    unsafe_marker = marker.replace("safe-card", "unsafe-card").replace("107", "2")
    response = {
        "message": f"Evidence {marker} ignored {unsafe_marker} repeated {marker}",
        "cardAttachmentsJson": [
            '{"id":"unsafe-card","type":"render_inline_citation",'
            '"cardType":"citation_card","url":"javascript:alert(1)"}',
            '{"id":"safe-card","type":"render_inline_citation",'
            '"cardType":"citation_card","url":"https://www.example.com/source"}',
            '{broken',
        ],
    }

    assert extract_grok_inline_citations(response) == [{
        "card_id": "safe-card",
        "url": "https://www.example.com/source",
        "label": "Example",
        "citation_id": "107",
    }]


def test_grok_display_markdown_removes_only_native_hidden_session_update_prelude() -> None:
    source = "核对证据。**「研究」Session 更新 — 结论**\n\n正文"

    assert normalize_grok_display_markdown(source) == "**「研究」Session 更新 — 结论**\n\n正文"
    assert normalize_grok_display_markdown("正常引言。\n\n**「研究」Session 更新 — 结论**") == (
        "正常引言。\n\n**「研究」Session 更新 — 结论**"
    )


def test_grok_security_challenge_is_actionable_and_is_not_retried() -> None:
    page = Mock()
    page.evaluate.return_value = {
        "status": 403,
        "body": '<!DOCTYPE html><html><head><title>Just a moment...</title></head></html>',
    }
    with pytest.raises(RuntimeError, match="Grok requires browser security verification") as error:
        _grok_api_json(page, "/rest/app-chat/conversations")
    assert "<html>" not in str(error.value)
    assert "preserved" in str(error.value)
    page.evaluate.assert_called_once()
    page.wait_for_timeout.assert_not_called()


def test_grok_text_status_rehydrates_persisted_messages(tmp_path: Path) -> None:
    from app.web.app import create_app

    conversation = GrokConversation("conversation-1", "Test session", "", "", "https://grok.com/c/conversation-1")
    GrokHistoryStore(tmp_path / "llm/grok/history.parquet").replace_conversation(
        conversation, [_message("conversation-1:r1", "user", 0, "cached text")],
        "2026-09-06T00:00:00Z",
    )
    for _ in range(2):
        client = create_app(tmp_path).test_client()
        status = client.get("/api/cache/grok/status?content_mode=text").get_json()
        assert status["downloaded_posts"] == 1
        assert status["downloaded_tweets"] == 1
        assert status["downloaded_images"] == 0
        assert status == client.get("/api/cache/grok/text/status").get_json()


def test_grok_conversation_pagination_uses_page_token(monkeypatch) -> None:
    """Follow Grok's nextPageToken with the pageToken request parameter."""
    calls: list[str] = []

    def fake_api(_page, path: str, **_kwargs):
        calls.append(path)
        if "pageToken=next-token" in path:
            return {
                "conversations": [
                    {
                        "conversationId": "conversation-2",
                        "title": "Second",
                    }
                ]
            }
        return {
            "conversations": [
                {
                    "conversationId": "conversation-1",
                    "title": "First",
                }
            ],
            "nextPageToken": "next-token",
        }

    monkeypatch.setattr("app.core.grok_history._grok_api_json", fake_api)
    conversations = list_grok_conversations(object())

    assert [item.conversation_id for item in conversations] == [
        "conversation-1",
        "conversation-2",
    ]
    assert "pageToken=next-token" in calls[1]


def test_grok_api_fetch_has_a_bounded_abort_controller_timeout() -> None:
    """Bound browser-context API work and always clear its timer."""

    class FakePage:
        def __init__(self) -> None:
            self.script = ""
            self.arguments = {}

        def evaluate(self, script, arguments):
            self.script = script
            self.arguments = arguments
            return {"status": 200, "body": {"ok": True}}

    page = FakePage()

    assert _grok_api_json(page, "/rest/test") == {"ok": True}
    assert page.arguments["timeoutMs"] == GROK_API_REQUEST_TIMEOUT_MS
    assert "new AbortController()" in page.script
    assert "signal: controller.signal" in page.script
    assert "setTimeout(() => controller.abort(), timeoutMs)" in page.script
    assert "clearTimeout(timeoutId)" in page.script


def test_grok_api_uses_safari_same_origin_request_for_get_and_post() -> None:
    context = SafariContext("https://grok.com/")
    page = SafariPage(context, window_id=123)
    context.pages.append(page)
    responses = [
        SafariResponse(status=200, body_text='{"conversations":[]}'),
        SafariResponse(status=200, body_text='{"responses":[]}'),
    ]

    with patch.object(
        context.request,
        "request_from_page",
        side_effect=responses,
    ) as request_from_page, patch.object(
        page,
        "evaluate",
        side_effect=AssertionError("Safari Grok API must not await a page.evaluate promise."),
    ):
        assert _grok_api_json(page, "/rest/app-chat/conversations") == {
            "conversations": []
        }
        assert _grok_api_json(
            page,
            "/rest/app-chat/conversations/demo/load-responses",
            method="POST",
            body={"responseIds": ["response-1"]},
        ) == {"responses": []}

    assert request_from_page.call_args_list[0].kwargs == {
        "method": "GET",
        "body": None,
    }
    assert request_from_page.call_args_list[1].kwargs == {
        "method": "POST",
        "body": '{"responseIds": ["response-1"]}',
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed to execute Safari's wrapper")
def test_safari_page_wrapper_cannot_await_a_promise_result() -> None:
    """Execute SafariPage's real synchronous wrapper around an async callback."""

    page = SafariPage(SafariContext("https://grok.com/"), window_id=123)

    def run_window(statement: str, **_kwargs: object) -> str:
        match = re.search(r'do JavaScript "((?:\\.|[^"\\])*)" in targetTab', statement)
        assert match is not None
        wrapper = json.loads('"' + match.group(1) + '"')
        result = subprocess.run(
            ["node", "-e", "process.stdout.write(String(eval(process.argv[1])))", wrapper],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    with patch.object(page, "_run_in_window", side_effect=run_window):
        assert page.evaluate("async () => ({status: 200, body: {ok: true}})") == {}


def test_grok_safari_request_fails_closed_when_bridge_is_missing() -> None:
    context = SafariContext("https://grok.com/")
    page = SafariPage(context, window_id=123)
    context.request = None

    with patch.object(page, "evaluate") as evaluate, pytest.raises(
        RuntimeError, match="request bridge is unavailable"
    ):
        _grok_api_json(page, "/rest/app-chat/conversations")

    evaluate.assert_not_called()


def test_grok_text_sync_uses_owned_safari_page_and_caches_messages(
    tmp_path: Path, macos_host
) -> None:
    context = MagicMock()
    context.initial_url = "https://grok.com/"
    context.pages = []
    page = SafariPage(context, window_id=123)
    context.pages.append(page)
    context.primary_page = page
    context.__enter__.return_value = context
    context.request.request_from_page.side_effect = [
        SafariResponse(
            status=200,
            body_text=json.dumps({
                "conversations": [{"conversationId": "conversation-1", "title": "Safari session"}],
            }),
        ),
        SafariResponse(
            status=200,
            body_text=json.dumps({"responseNodes": [{"responseId": "response-1"}]}),
        ),
        SafariResponse(
            status=200,
            body_text=json.dumps({
                "responses": [{
                    "responseId": "response-1",
                    "sender": "assistant",
                    "message": "Cached through Safari",
                }],
            }),
        ),
    ]
    state = TaskState("test", snapshot_factory=lambda version: TaskSnapshot(version=version))

    with patch("app.core.grok_history.SafariContext", return_value=context) as open_safari, patch(
        "app.core.grok_history.sync_playwright_or_error",
        side_effect=AssertionError("Safari must not start Playwright"),
    ), patch.object(page, "wait_for_timeout"):
        result = sync_grok_history(
            state,
            CrawlConfig(grok_browser="safari"),
            lambda: False,
            local_store_root=str(tmp_path),
        )

    open_safari.assert_called_once_with("https://grok.com/", lock_blocking=False)
    context.__exit__.assert_called_once()
    assert result == {
        "sessions": 1,
        "messages": 1,
        "added_or_changed": 1,
        "unchanged": 0,
        "failed": 0,
        "stopped": False,
    }
    assert context.request.request_from_page.call_count == 3
    cached = query_chat_history(tmp_path, source="grok", session_view=True)
    assert cached.total_count == 1
    assert cached.items[0].content_text == "Cached through Safari"


def test_grok_api_timeout_uses_existing_retry_backoff() -> None:
    """Retry a browser-aborted request through the existing 408 path."""

    class FakePage:
        def __init__(self) -> None:
            self.results = [
                {
                    "status": 408,
                    "body": {"message": "Request timed out after 30000 ms"},
                    "timedOut": True,
                },
                {"status": 200, "body": {"ok": True}},
            ]
            self.waits = []

        def evaluate(self, _script, _arguments):
            return self.results.pop(0)

        def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)

    page = FakePage()

    assert _grok_api_json(page, "/rest/test") == {"ok": True}
    assert page.waits == [1_000]
