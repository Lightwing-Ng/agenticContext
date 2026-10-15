"""Regression coverage for independent Grok Chat and local Grok Build resources."""

# Code version: v1.0.0-codex.0

from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.chat_history_browser import (
    build_chat_history_markdown,
    chat_history_path,
    find_chat_history_message,
    load_chat_history_messages,
    query_chat_history,
)
from app.core.config import CrawlConfig
from app.core.local_media_browser import normalize_browser_filters
from app.core.prompt_store import PromptStore
from app.core.resource_persistence import GROK_HISTORY_SCHEMA, write_parquet_rows_atomic
from app.web.app import create_app
from app.web.config_store import SavedConfigStore


def _row(source: str, session: str, text: str, *, index: int = 0, role: str = "user") -> dict:
    """Use matching IDs to expose accidental cross-provider session merging."""
    timestamp = f"2026-10-13T08:00:0{index}Z"
    return {
        "schema_version": 1,
        "platform": source,
        "conversation_id": session,
        "conversation_url": f"https://grok.com/c/{session}",
        "conversation_title": "Local Grok Build session" if source == "grok_build" else "Web chat session",
        "message_key": f"{session}:{role}:{index}",
        "turn_index": 0,
        "message_index": index,
        "role": role,
        "author_label": "You" if role == "user" else "Grok Build",
        "content_text": text,
        "content_html": "",
        "content_sha256": "fixture",
        "source_links": [],
        "model_label": "grok-build",
        "first_seen_at": timestamp,
        "last_seen_at": timestamp,
    }


@pytest.fixture
def history_root(tmp_path: Path) -> Path:
    """Persist fixture messages exclusively under pytest's disposable directory."""
    write_parquet_rows_atomic(
        tmp_path / "llm/grok/history.parquet",
        [_row("grok", "shared-id", "A separate web-only prompt.")],
        GROK_HISTORY_SCHEMA,
    )
    write_parquet_rows_atomic(
        tmp_path / "llm/grok_build/history.parquet",
        [
            _row("grok_build", "shared-id", "Explain the local parser."),
            _row("grok_build", "shared-id", "The parser reads local text.", index=1, role="assistant"),
            _row("grok_build", "second-id", "Inspect another workspace.", index=2),
        ],
        GROK_HISTORY_SCHEMA,
    )
    return tmp_path


@pytest.fixture
def client(history_root: Path):
    """Keep configuration, runtime, histories, and saved prompts isolated."""
    with patch("app.web.app.SavedConfigStore.load", return_value=SavedConfigStore(CrawlConfig())):
        application = create_app(
            history_root,
            computer_use_settings_path=history_root / "settings/computer-use-agent.json",
            computer_use_runtime_root=history_root / "computer-use-runtime",
            agent_external_operations_enabled=False,
        )
    application.config.update(TESTING=True)
    return application.test_client()


@pytest.mark.parametrize(
    ("view", "source", "expected"),
    [
        ("text", "grok_build", "grok_build"),
        ("prompts", "grok_build", "grok_build"),
        ("media", "grok_build", "chatgpt"),
        ("text", "grok", "grok"),
        ("media", "grok", "grok"),
    ],
)
def test_grok_build_filters_keep_local_history_text_only(view: str, source: str, expected: str) -> None:
    filters = normalize_browser_filters(view=view, source=source, project="web-project")
    assert filters["view"] == view
    assert filters["source"] == expected
    if source == "grok_build":
        assert filters["project"] == ""


def test_web_and_local_sessions_remain_separate_with_matching_ids(history_root: Path) -> None:
    messages = load_chat_history_messages(history_root)
    assert len(messages) == 4
    assert len({item.stable_id for item in messages}) == 4
    local = query_chat_history(history_root, source="grok_build", session_view=True)
    web = query_chat_history(history_root, source="grok", session_view=True)
    assert local.conversation_count == 2
    assert local.total_count == 3
    assert web.conversation_count == 1
    assert web.sessions[0].stable_id not in {item.stable_id for item in local.sessions}
    assert all(item.source == "grok_build" for item in local.sessions)
    assert all(item.conversation_url == "" for item in local.sessions)
    assert web.sessions[0].conversation_url == "https://grok.com/c/shared-id"
    assert chat_history_path(history_root, "grok_build") == history_root / "llm/grok_build/history.parquet"


def test_grok_build_detail_keeps_source_times_search_and_navigation(history_root: Path) -> None:
    detail = query_chat_history(history_root, source="grok_build", session="grok_build:shared-id", session_view=True)
    assert detail.session_detail
    assert [item.role for item in detail.items] == ["user", "assistant"]
    assert [item.last_seen_at for item in detail.items] == [
        "2026-10-13T08:00:00Z", "2026-10-13T08:00:01Z",
    ]
    assert detail.current_session.last_seen_at == "2026-10-13T08:00:01Z"
    assert detail.previous_session.conversation_id == "second-id"
    assert detail.next_session is None
    assert query_chat_history(history_root, source="grok_build", query="web-only").total_count == 0
    assert query_chat_history(history_root, source="grok", query="parser").total_count == 0
    assert query_chat_history(history_root, source="grok_build", query="parser").total_count == 2


def test_grok_build_export_and_prompt_pointers_resolve_independent_history(history_root: Path) -> None:
    page = query_chat_history(history_root, source="grok_build", session="grok_build:shared-id")
    markdown = build_chat_history_markdown(page)
    assert "- Source: Grok Build" in markdown
    assert "- Messages: 2" in markdown
    assert "- Original:" not in markdown
    assert "The parser reads local text." in markdown
    web_markdown = build_chat_history_markdown(
        query_chat_history(history_root, source="grok", session="grok:shared-id")
    )
    assert "- Source: Grok Chat" in web_markdown
    store = PromptStore(history_root)
    saved, created = store.add_pointer(
        source="grok_build", conversation_id="shared-id", message_key="shared-id:user:0",
    )
    assert created
    assert saved.content_text == "Explain the local parser."
    assert saved.conversation_url == ""
    assert store.query(source="grok_build").total_count == 1
    assert store.query(source="grok").total_count == 0
    resolved = find_chat_history_message(
        history_root, source="grok_build", conversation_id="shared-id", message_key="shared-id:user:0",
    )
    assert resolved.source == "grok_build"
    assert resolved.content_text == saved.content_text


def test_browser_exposes_distinct_sources_and_safe_local_actions(client) -> None:
    response = client.get("/browser?view=text&source=grok_build&session_view=1")
    assert response.status_code == 200
    index_html = response.get_data(as_text=True)
    assert 'data-browser-source-filter-option="grok"' in index_html
    assert 'data-browser-source-filter-label="Grok Chat"' in index_html
    assert 'data-browser-source-filter-option="grok_build"' in index_html
    assert 'data-browser-source-filter-label="Grok Build"' in index_html
    assert 'aria-label="Cached Grok Build sessions"' in index_html
    assert "Local Grok Build session" in index_html
    assert "Web chat session" not in index_html
    detail = client.get("/browser?view=text&source=grok_build&session=grok_build:shared-id&session_view=1")
    assert detail.status_code == 200
    detail_html = detail.get_data(as_text=True)
    assert 'aria-label="Cached Grok Build session messages"' in detail_html
    assert "Explain the local parser." in detail_html
    assert "data-browser-session-open-original" not in detail_html
    assert "data-browser-session-refresh-url" in detail_html
    assert "data-browser-session-download-url" in detail_html
    assert "--browser-session-action-count: 2;" in detail_html
    assert 'data-prompt-source="grok_build"' in detail_html
    media_html = client.get("/browser?view=media&source=grok_build").get_data(as_text=True)
    assert 'data-browser-source-filter-option="grok_build"' not in media_html
    assert 'name="source" value="chatgpt"' in media_html


def test_browser_saves_and_exports_local_grok_build_content(client) -> None:
    response = client.post(
        "/api/browser/prompts",
        json={"source": "grok_build", "conversation_id": "shared-id", "message_key": "shared-id:user:0"},
    )
    assert response.status_code == 200
    assert response.get_json()["created"]
    prompts_html = client.get("/browser?view=prompts&source=grok_build").get_data(as_text=True)
    assert "Explain the local parser." in prompts_html
    assert "Grok Build" in prompts_html
    export = client.get("/browser/session/grok_build:shared-id/export?source=grok_build")
    assert export.status_code == 200
    assert "- Source: Grok Build" in export.get_data(as_text=True)
