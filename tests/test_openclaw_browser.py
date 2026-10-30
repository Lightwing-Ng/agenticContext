"""Preserved OpenClaw histories keep independent identities and exports.

Code version: v1.0.0-codex.0
"""

from pathlib import Path
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
from app.core.openclaw_history import OPENCLAW_SOURCE_LABELS
from app.core.resource_persistence import CLAUDE_HISTORY_SCHEMA, write_parquet_rows_atomic
from app.web.app import create_app
from app.web.config_store import SavedConfigStore


@pytest.fixture
def archive_root(tmp_path: Path) -> Path:
    for index, source in enumerate(("chatgpt", *OPENCLAW_SOURCE_LABELS)):
        row = {
            "schema_version": 1, "platform": source, "conversation_id": "same-id",
            "conversation_url": "https://chatgpt.com/c/same-id",
            "conversation_title": f"{source} archived fixture",
            "message_key": "same-id:message-1", "turn_index": 1, "message_index": 1,
            "role": "user", "author_label": "You",
            "content_text": "Browser-only fixture" if source == "chatgpt" else f"Unique {source} prompt",
            "content_html": "", "content_sha256": "fixture", "source_links": [],
            "model_label": "fixture-model", "first_seen_at": "2026-10-01T01:00:00Z",
            "last_seen_at": f"2026-10-01T01:00:0{index}Z",
        }
        write_parquet_rows_atomic(chat_history_path(tmp_path, source), [row], CLAUDE_HISTORY_SCHEMA)
    return tmp_path


@pytest.mark.parametrize("source,label", OPENCLAW_SOURCE_LABELS.items())
def test_sources_do_not_merge_and_exports_keep_origin(archive_root, source, label):
    assert chat_history_path(archive_root, source) == archive_root / "llm" / source / "history.parquet"
    detail = query_chat_history(archive_root, source=source, session=f"{source}:same-id", session_view=True)
    assert detail.total_count == 1
    assert detail.session_detail
    assert detail.current_session.source == source
    assert detail.current_session.conversation_url == ""
    assert detail.items[0].content_text == f"Unique {source} prompt"
    assert detail.items[0].model_label == "fixture-model"
    assert f"- Source: {label}" in build_chat_history_markdown(detail)
    assert "- Original:" not in build_chat_history_markdown(detail)
    assert query_chat_history(archive_root, source=source, query="Browser-only").total_count == 0
    filters = normalize_browser_filters(view="media", source=source, project="web")
    assert filters["source"] == "chatgpt"
    filters = normalize_browser_filters(view="text", source=source, project="web")
    assert filters["source"] == source
    assert filters["project"] == ""


def test_all_sources_have_distinct_message_and_session_identities(archive_root):
    messages = load_chat_history_messages(archive_root)
    assert len(messages) == len(OPENCLAW_SOURCE_LABELS) + 1
    assert len({message.stable_id for message in messages}) == len(messages)
    page = query_chat_history(archive_root, session_view=True)
    assert page.conversation_count == len(messages)
    assert len({session.stable_id for session in page.sessions}) == len(messages)


def test_resources_show_origin_hide_web_action_and_preserve_prompt_pointer(archive_root):
    with patch("app.web.app.SavedConfigStore.load", return_value=SavedConfigStore(CrawlConfig())):
        application = create_app(
            archive_root,
            computer_use_settings_path=archive_root / "settings/agent.json",
            computer_use_runtime_root=archive_root / "runtime",
            agent_external_operations_enabled=False,
        )
    application.config.update(TESTING=True)
    client = application.test_client()
    response = client.get("/browser?view=text&source=gemini_openclaw&session=gemini_openclaw:same-id")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'aria-label="Source: Gemini OpenClaw"' in html
    assert "Unique gemini_openclaw prompt" in html
    assert "data-browser-session-open-original" not in html
    store = application.extensions["prompt_store"]
    store.add_pointer(source="gemini_openclaw", conversation_id="same-id", message_key="same-id:message-1")
    saved = store.query(source="gemini_openclaw")
    assert saved.total_count == 1
    assert saved.items[0].source == "gemini_openclaw"
    assert store.query(source="gemini").total_count == 0
