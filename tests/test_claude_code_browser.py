"""Regression tests for separate local Claude Code and Claude Chat resources."""

# Code version: v1.0.0-codex.0

from pathlib import Path

import pytest

from app.core.chat_history_browser import (
    build_chat_history_markdown,
    chat_history_path,
    find_chat_history_message,
    load_chat_history_messages,
    query_chat_history,
)
from app.core.local_media_browser import normalize_browser_filters
from app.core.prompt_store import PromptStore
from app.core.resource_persistence import CLAUDE_HISTORY_SCHEMA, write_parquet_rows_atomic
from app.web.app import create_app


def _row(source: str, session: str, text: str, *, index: int = 0, role: str = "user") -> dict:
    """Build realistic messages sharing IDs across independent source catalogs."""
    timestamp = f"2026-10-13T08:00:0{index}Z"
    return {
        "schema_version": 1,
        "platform": source,
        "conversation_id": session,
        "conversation_url": f"https://claude.ai/chat/{session}",
        "conversation_title": "Local code session" if source == "claude_code" else "Web chat session",
        "message_key": f"{session}:{role}:{index}",
        "turn_index": 0,
        "message_index": index,
        "role": role,
        "author_label": "You" if role == "user" else "Claude",
        "content_text": text,
        "content_html": "",
        "content_sha256": "fixture",
        "source_links": [],
        "model_label": "claude-opus",
        "first_seen_at": timestamp,
        "last_seen_at": timestamp,
    }


@pytest.fixture
def history_root(tmp_path: Path) -> Path:
    write_parquet_rows_atomic(
        tmp_path / "llm/claude/history.parquet",
        [_row("claude", "shared-id", "A separate web-only prompt.")],
        CLAUDE_HISTORY_SCHEMA,
    )
    write_parquet_rows_atomic(
        tmp_path / "llm/claude_code/history.parquet",
        [
            _row("claude_code", "shared-id", "Explain the local parser."),
            _row("claude_code", "shared-id", "The parser reads local text.", index=1, role="assistant"),
            _row("claude_code", "second-id", "Inspect another workspace.", index=2),
        ],
        CLAUDE_HISTORY_SCHEMA,
    )
    return tmp_path


@pytest.mark.parametrize(
    ("view", "source", "expected"),
    [
        ("text", "claude_code", "claude_code"),
        ("prompts", "claude_code", "claude_code"),
        ("media", "claude_code", "chatgpt"),
        ("text", "claude", "claude"),
        ("media", "claude", "claude"),
    ],
)
def test_source_filters_keep_code_text_only(view: str, source: str, expected: str) -> None:
    filters = normalize_browser_filters(view=view, source=source)
    assert filters["view"] == view
    assert filters["source"] == expected


def test_code_and_web_sessions_remain_separate_with_shared_ids(history_root: Path) -> None:
    messages = load_chat_history_messages(history_root)
    assert len(messages) == 4
    assert len({item.stable_id for item in messages}) == 4
    code = query_chat_history(history_root, source="claude_code", session_view=True)
    web = query_chat_history(history_root, source="claude", session_view=True)
    assert code.conversation_count == 2
    assert code.total_count == 3
    assert web.conversation_count == 1
    assert web.sessions[0].stable_id not in {item.stable_id for item in code.sessions}
    assert all(item.source == "claude_code" for item in code.sessions)
    assert all(item.conversation_url == "" for item in code.sessions)
    assert web.sessions[0].conversation_url == "https://claude.ai/chat/shared-id"
    assert chat_history_path(history_root, "claude_code") == history_root / "llm/claude_code/history.parquet"


def test_code_history_keeps_source_times_and_session_navigation(history_root: Path) -> None:
    detail = query_chat_history(
        history_root, source="claude_code", session="claude_code:shared-id", session_view=True,
    )
    assert detail.session_detail
    assert [item.role for item in detail.items] == ["user", "assistant"]
    assert [item.last_seen_at for item in detail.items] == [
        "2026-10-13T08:00:00Z", "2026-10-13T08:00:01Z",
    ]
    assert detail.current_session.last_seen_at == "2026-10-13T08:00:01Z"
    assert detail.previous_session.conversation_id == "second-id"
    assert detail.next_session is None
    assert query_chat_history(history_root, source="claude_code", query="web-only").total_count == 0
    assert query_chat_history(history_root, source="claude", query="parser").total_count == 0
    assert query_chat_history(history_root, source="claude_code", query="parser").total_count == 2


def test_export_and_prompt_bookmarks_resolve_the_code_source(history_root: Path) -> None:
    page = query_chat_history(history_root, source="claude_code", session="claude_code:shared-id")
    markdown = build_chat_history_markdown(page)
    assert "- Source: Claude Code" in markdown
    assert "- Messages: 2" in markdown
    assert "- Original:" not in markdown
    assert "The parser reads local text." in markdown
    web_markdown = build_chat_history_markdown(
        query_chat_history(history_root, source="claude", session="claude:shared-id")
    )
    assert "- Source: Claude Chat" in web_markdown
    store = PromptStore(history_root)
    saved, created = store.add_pointer(
        source="claude_code", conversation_id="shared-id", message_key="shared-id:user:0",
    )
    assert created
    assert saved.content_text == "Explain the local parser."
    assert saved.conversation_url == ""
    assert store.query(source="claude_code").total_count == 1
    assert store.query(source="claude").total_count == 0
    resolved = find_chat_history_message(
        history_root, source="claude_code", conversation_id="shared-id", message_key="shared-id:user:0",
    )
    assert resolved.source == "claude_code"
    assert resolved.content_text == saved.content_text


def test_browser_renders_distinct_filters_and_local_session_actions(history_root: Path) -> None:
    client = create_app(history_root).test_client()
    response = client.get("/browser?view=text&source=claude_code&session_view=1")
    assert response.status_code == 200
    index_html = response.get_data(as_text=True)
    assert 'data-browser-source-filter-option="claude"' in index_html
    assert 'data-browser-source-filter-label="Claude Chat"' in index_html
    assert 'data-browser-source-filter-option="claude_code"' in index_html
    assert 'data-browser-source-filter-label="Claude Code"' in index_html
    assert 'aria-label="Cached Claude Code sessions"' in index_html
    assert "Local code session" in index_html
    assert "Web chat session" not in index_html
    detail = client.get(
        "/browser?view=text&source=claude_code&session=claude_code:shared-id&session_view=1"
    )
    assert detail.status_code == 200
    detail_html = detail.get_data(as_text=True)
    assert 'aria-label="Cached Claude Code session messages"' in detail_html
    assert "Explain the local parser." in detail_html
    assert "data-browser-session-open-original" not in detail_html
    assert "data-browser-session-refresh-url" in detail_html
    assert "data-browser-session-download-url" in detail_html
    assert "--browser-session-action-count: 2;" in detail_html
    assert 'data-prompt-source="claude_code"' in detail_html
    media_html = client.get("/browser?view=media&source=claude_code").get_data(as_text=True)
    assert 'data-browser-source-filter-option="claude_code"' not in media_html
    assert 'name="source" value="chatgpt"' in media_html


def test_browser_saves_and_exports_local_code_content(history_root: Path) -> None:
    client = create_app(history_root).test_client()
    response = client.post(
        "/api/browser/prompts",
        json={"source": "claude_code", "conversation_id": "shared-id", "message_key": "shared-id:user:0"},
    )
    assert response.status_code == 200
    assert response.get_json()["created"]
    prompts_html = client.get("/browser?view=prompts&source=claude_code").get_data(as_text=True)
    assert "Explain the local parser." in prompts_html
    assert "Claude Code" in prompts_html
    export = client.get("/browser/session/claude_code:shared-id/export?source=claude_code")
    assert export.status_code == 200
    assert "- Source: Claude Code" in export.get_data(as_text=True)
