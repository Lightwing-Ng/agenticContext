"""Verify indexed SQLite navigation preserves the complete browser projection."""

# Code version: v1.1.0-codex.0

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
from pathlib import Path

import pytest

from app.core import chat_history_browser as browser
from app.core import llm_sqlite
from app.core.agent_source_cache import AgentSourceCache
from app.core.resource_persistence import CHATGPT_HISTORY_SCHEMA


def _rows(source: str) -> list[dict]:
    rows = []
    for conversation in range(4):
        for index in range(3):
            timestamp = f"2026-10-{10 + conversation:02d}T10:{index:02d}:00Z"
            rows.append({
                "schema_version": 3,
                "platform": source,
                "conversation_id": f"conversation-{conversation}",
                "conversation_url": f"https://{source}.com/c/conversation-{conversation}",
                "conversation_title": f"Title {3 - conversation}",
                "message_key": f"conversation-{conversation}:{index}",
                "turn_index": 1,
                "message_index": index,
                "role": "user" if index == 0 else "assistant",
                "author_label": "You" if index == 0 else source,
                "content_text": f"Body {conversation}/{index}",
                "content_html": "",
                "content_sha256": "fixture",
                "source_links": [f"https://example.com/{index}"],
                "model_label": "Model",
                "first_seen_at": "2026-11-01T10:00:00Z",
                "last_seen_at": timestamp,
                "provider_revision": "2026-10-31T00:00:00Z",
            })
    return rows


def _store(root: Path, source: str, rows: list[dict] | None = None) -> Path:
    path = browser.chat_history_path(root, source)
    llm_sqlite.write_sqlite_rows(path, _rows(source) if rows is None else rows, CHATGPT_HISTORY_SCHEMA)
    return path


@pytest.mark.parametrize("source", ["gemini", "claude", "chatgpt", "codex", "grok_build"])
@pytest.mark.parametrize("sort", ["newest", "oldest", "name"])
@pytest.mark.parametrize("session", ["", "conversation-2", "missing"])
def test_indexed_navigation_equals_full_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str, sort: str, session: str,
) -> None:
    _store(tmp_path, source)
    kwargs = {
        "source": source, "sort": sort, "session_view": True,
        "page_size": 2, "page": 2,
        "session": f"{source}:{session}" if session else "",
    }
    indexed = browser.query_chat_history(tmp_path, **kwargs)
    monkeypatch.setattr(llm_sqlite, "read_sqlite_summaries", lambda path: None)
    full = browser.query_chat_history(tmp_path, **kwargs)
    assert indexed == full


def test_indexed_reads_decode_only_displayed_and_navigation_conversations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store(tmp_path, "gemini")
    read = llm_sqlite.read_sqlite_rows
    read_conversations = llm_sqlite.read_sqlite_conversations
    decoded = []

    def tracked(path, conversation_id=None):
        decoded.append(conversation_id)
        return read(path, conversation_id=conversation_id)

    def tracked_conversations(path, conversation_ids):
        decoded.extend(conversation_ids)
        return read_conversations(path, conversation_ids)

    monkeypatch.setattr(llm_sqlite, "read_sqlite_rows", tracked)
    monkeypatch.setattr(llm_sqlite, "read_sqlite_conversations", tracked_conversations)
    page = browser.query_chat_history(
        tmp_path, source="gemini", session_view=True, sort="name", page_size=1,
    )
    assert decoded == [page.sessions[0].conversation_id]
    decoded.clear()
    detail = browser.query_chat_history(
        tmp_path, source="gemini", session=page.sessions[0].stable_id,
    )
    assert decoded == [
        item.conversation_id for item in (
            detail.previous_session, detail.current_session, detail.next_session,
        ) if item is not None
    ]
    decoded.clear()
    message = browser.find_chat_history_message(
        tmp_path, source="gemini", conversation_id="conversation-2",
        message_key="conversation-2:1",
    )
    assert message is not None and message.content_text == "Body 2/1"
    assert decoded == ["conversation-2"]
    assert detail.current_session == page.sessions[0]


def test_indexed_chatgpt_projects_and_new_discovery_metadata_match_full_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store(tmp_path, "chatgpt")
    now = datetime.now(UTC) + timedelta(hours=1)
    project = "g-p-" + "a" * 32
    cache = AgentSourceCache(tmp_path)
    cache.store(
        platform="chatgpt", browser="edge", source_kind="sources", now=now,
        payload={"projects": [{"id": project, "title": "Project"}]},
    )
    cache.store(
        platform="chatgpt", browser="edge", source_kind="project-sessions", now=now,
        project_url=f"https://chatgpt.com/g/{project}/project",
        payload={"sessions": [
            {"id": "conversation-1", "url": "https://chatgpt.com/c/conversation-1"},
            {"id": "conversation-2", "url": "https://chatgpt.com/c/conversation-2"},
        ]},
    )
    cache.store(
        platform="chatgpt", browser="edge", source_kind="session-history", now=now,
        payload={"conversation_url": "https://chatgpt.com/c/conversation-1", "title": "Renamed"},
    )
    for session in ("", "chatgpt:conversation-1", "chatgpt:conversation-0"):
        kwargs = {
            "source": "chatgpt", "project": project, "session_view": True,
            "sort": "name", "session": session,
        }
        indexed = browser.query_chat_history(tmp_path, **kwargs)
        with monkeypatch.context() as scoped:
            scoped.setattr(llm_sqlite, "read_sqlite_summaries", lambda path: None)
            full = browser.query_chat_history(tmp_path, **kwargs)
        assert indexed == full


def test_indexed_summary_updates_after_changed_capture(tmp_path: Path) -> None:
    path = _store(tmp_path, "gemini")
    before = browser.query_chat_history(tmp_path, source="gemini", session_view=True)
    rows = _rows("gemini")
    rows = [row for row in rows if row["conversation_id"] != "conversation-3"]
    rows[-1]["content_text"] = "Updated last message"
    llm_sqlite.write_sqlite_rows(path, rows, CHATGPT_HISTORY_SCHEMA)
    after = browser.query_chat_history(tmp_path, source="gemini", session_view=True)
    assert before.conversation_count == 4
    assert after.conversation_count == 3
    assert after.total_count == 9
    assert after.sessions[0].latest_message == "Updated last message"


def test_empty_sqlite_browser_index(tmp_path: Path) -> None:
    _store(tmp_path, "gemini", rows=[])
    page = browser.query_chat_history(tmp_path, source="gemini", session_view=True)
    assert page.total_count == page.conversation_count == 0
    assert page.total_pages == page.current_page == 1
    assert not page.items and not page.sessions


@pytest.mark.parametrize("sort", ["newest", "oldest", "name"])
def test_combined_provider_index_preserves_global_paging_and_navigation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sort: str,
) -> None:
    for source in ("chatgpt", "claude", "gemini", "codex", "zhihu"):
        _store(tmp_path, source)
    for session in ("", "gemini:conversation-2", "zhihu:conversation-1"):
        kwargs = {
            "source": "all", "sort": sort, "session_view": True,
            "page_size": 3, "page": 2, "session": session,
        }
        indexed = browser.query_chat_history(tmp_path, **kwargs)
        with monkeypatch.context() as scoped:
            scoped.setattr(llm_sqlite, "read_sqlite_summaries", lambda path: None)
            full = browser.query_chat_history(tmp_path, **kwargs)
        assert indexed == full


def test_indexed_read_falls_back_when_a_writer_changes_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store(tmp_path, "gemini")
    read_conversations = llm_sqlite.read_sqlite_conversations
    load_messages = browser.load_chat_history_messages
    full_reads = []

    def changed_during_read(path, conversation_ids):
        rows = read_conversations(path, conversation_ids)
        status = path.stat()
        os.utime(path, ns=(status.st_atime_ns, status.st_mtime_ns + 1_000_000))
        return rows

    def tracked_full_read(root, source="all"):
        full_reads.append(source)
        return load_messages(root, source)

    monkeypatch.setattr(llm_sqlite, "read_sqlite_conversations", changed_during_read)
    monkeypatch.setattr(browser, "load_chat_history_messages", tracked_full_read)
    page = browser.query_chat_history(tmp_path, source="gemini", session_view=True)
    assert page.total_count == 12
    assert full_reads == ["gemini"]


def test_indexed_chatgpt_membership_preserves_equal_time_observation_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = "g-p-" + "a" * 32
    second = "g-p-" + "b" * 32
    rows = _rows("chatgpt")[:3]
    for row, project in zip(rows, (first, second, first), strict=True):
        row["conversation_url"] = f"https://chatgpt.com/g/{project}/c/conversation-0"
    _store(tmp_path, "chatgpt", rows)
    cache = AgentSourceCache(tmp_path)
    cache.store(
        platform="chatgpt", browser="edge", source_kind="sources",
        payload={"projects": [{"id": first, "title": "A"}, {"id": second, "title": "B"}]},
    )
    indexed = browser.query_chat_history(
        tmp_path, source="chatgpt", session_view=True, project=first,
    )
    monkeypatch.setattr(llm_sqlite, "read_sqlite_summaries", lambda path: None)
    full = browser.query_chat_history(
        tmp_path, source="chatgpt", session_view=True, project=first,
    )
    assert indexed == full
    assert indexed.total_count == 3
