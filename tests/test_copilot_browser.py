"""Copilot history stays isolated, searchable, and honest about unknown timestamps.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.core.chat_history_browser import (
    build_chat_history_markdown,
    chat_history_path,
    load_chat_history_messages,
    query_chat_history,
)
from app.core.config import CrawlConfig
from app.core.local_media_browser import normalize_browser_filters
from app.core.resource_persistence import COPILOT_HISTORY_SCHEMA, write_parquet_rows_atomic
from app.web.app import create_app
from app.web.config_store import SavedConfigStore


def copilot_row(source="copilot", *, index=0, content="A cached Copilot prompt.", timestamp=""):
    return {
        "schema_version": 1, "platform": source, "conversation_id": "shared-id",
        "conversation_url": "https://copilot.com/chats/shared-id", "conversation_title": "Copilot fixture",
        "message_key": f"shared-id:{index}", "turn_index": 0, "message_index": index,
        "role": "user" if index == 0 else "assistant", "author_label": "You" if index == 0 else "Copilot",
        "content_text": content, "content_html": "", "content_sha256": "fixture",
        "source_links": [], "model_label": "", "first_seen_at": "2026-10-21T01:00:00Z", "last_seen_at": timestamp,
    }


@pytest.fixture
def history_root(tmp_path):
    write_parquet_rows_atomic(
        tmp_path / "llm/copilot/history.parquet",
        [copilot_row(), copilot_row(index=1, content="A cached Copilot answer.")],
        COPILOT_HISTORY_SCHEMA,
    )
    write_parquet_rows_atomic(
        tmp_path / "llm/chatgpt/history.parquet",
        [copilot_row("chatgpt", content="A separate ChatGPT message.")],
        COPILOT_HISTORY_SCHEMA,
    )
    return tmp_path


@pytest.mark.parametrize(("view", "expected"), [("text", "copilot"), ("prompts", "copilot"), ("media", "chatgpt")])
def test_copilot_filters_remain_text_only(view, expected):
    filters = normalize_browser_filters(view=view, source="copilot", project="unrelated-project")
    assert filters["source"] == expected
    assert filters["project"] == ""


def test_copilot_history_and_matching_other_provider_ids_remain_separate(history_root):
    messages = load_chat_history_messages(history_root)
    assert len(messages) == 3
    assert len({item.stable_id for item in messages}) == 3
    page = query_chat_history(history_root, source="copilot", session_view=True)
    assert page.conversation_count == 1
    assert page.total_count == 2
    assert page.sessions[0].source == "copilot"
    assert page.sessions[0].last_seen_at == ""
    assert chat_history_path(history_root, "copilot") == history_root / "llm/copilot/history.sqlite3"


def test_copilot_unknown_message_date_is_not_replaced_by_capture_time(history_root):
    messages = load_chat_history_messages(history_root, "copilot")
    assert all(message.last_seen_at == "" for message in messages)
    page = query_chat_history(history_root, source="copilot", session="copilot:shared-id", session_view=True)
    assert page.session_detail
    exported = build_chat_history_markdown(page)
    assert "Source: Microsoft Copilot" in exported
    assert "A cached Copilot answer." in exported
    assert "21 Oct 2026" not in exported


def test_copilot_local_resources_search_and_source_options(history_root):
    with patch("app.web.app.SavedConfigStore.load", return_value=SavedConfigStore(CrawlConfig())):
        application = create_app(history_root, agent_external_operations_enabled=False)
    response = application.test_client().get("/browser?view=text&source=copilot&q=answer&session_view=0")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Microsoft Copilot" in html
    assert "A cached Copilot answer." in html
    assert "A separate ChatGPT message." not in html
    assert "21 Oct 2026" not in html
    assert "copilot.ico" in html
