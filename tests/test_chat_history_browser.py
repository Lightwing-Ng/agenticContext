"""Focused tests for the local text-history browser."""

# Code version: v1.6.0-codex.1

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.core.chat_history_browser import (
    attach_media_references,
    build_chat_history_markdown,
    format_chat_message_timestamp_label,
    query_chat_history,
)
from app.core.local_media_browser import LocalMediaItem
from app.core.resource_persistence import GEMINI_HISTORY_SCHEMA, write_parquet_rows_atomic


def _history_row(
    conversation_id: str,
    message_key: str,
    content_text: str,
    *,
    role: str = "user",
    conversation_title: str = "Demo conversation",
    first_seen_at: str = "2026-08-15T09:38:00Z",
    last_seen_at: str = "2026-08-12T05:00:00Z",
    message_index: int = 0,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "platform": "gemini",
        "conversation_id": conversation_id,
        "conversation_url": f"https://gemini.google.com/app/{conversation_id}",
        "conversation_title": conversation_title,
        "message_key": message_key,
        "turn_index": 0,
        "message_index": message_index,
        "role": role,
        "author_label": "You" if role == "user" else "Gemini",
        "content_text": content_text,
        "content_html": "",
        "content_sha256": "hash",
        "source_links": [],
        "model_label": "",
        "first_seen_at": first_seen_at,
        "last_seen_at": last_seen_at,
    }


def test_query_chat_history_reads_and_filters_typed_messages(tmp_path: Path) -> None:
    history_path = tmp_path / "llm" / "gemini" / "history.parquet"
    write_parquet_rows_atomic(
        history_path,
        [
            _history_row("new", "new:0:user", "Keep this message", last_seen_at="2026-08-12T06:00:00Z"),
            _history_row("old", "old:0:user", "Findable older message", last_seen_at="2026-08-11T06:00:00Z"),
        ],
        GEMINI_HISTORY_SCHEMA,
    )

    page = query_chat_history(tmp_path, query="findable")
    spaced_page = query_chat_history(tmp_path, query="findable older")

    assert page.total_count == 1
    assert page.conversation_count == 1
    assert page.items[0].conversation_id == "old"
    assert page.items[0].content_text == "Findable older message"
    assert [item.conversation_id for item in spaced_page.items] == ["old"]


def test_query_chat_history_can_paginate_one_row_per_session(tmp_path: Path) -> None:
    history_path = tmp_path / "llm" / "gemini" / "history.parquet"
    write_parquet_rows_atomic(
        history_path,
        [
            _history_row("new", "new:0:user", "First", last_seen_at="2026-08-12T06:00:00Z"),
            _history_row("new", "new:1:assistant", "Latest", role="assistant", last_seen_at="2026-08-12T07:00:00Z"),
            _history_row("old", "old:0:user", "Older", last_seen_at="2026-08-11T06:00:00Z"),
        ],
        GEMINI_HISTORY_SCHEMA,
    )

    page = query_chat_history(tmp_path, session_view=True, page_size=1)

    assert page.session_view
    assert page.total_count == 3
    assert page.conversation_count == 2
    assert page.total_pages == 2
    assert len(page.sessions) == 1
    assert page.sessions[0].conversation_id == "new"
    assert page.sessions[0].message_count == 2
    assert page.sessions[0].latest_message == "Latest"
    assert {message.message_key for message in page.items} == {"new:0:user", "new:1:assistant"}


def test_gemini_capture_fallback_is_unknown_and_sorts_after_source_time(tmp_path: Path) -> None:
    history_path = tmp_path / "llm" / "gemini" / "history.parquet"
    write_parquet_rows_atomic(
        history_path,
        [
            _history_row(
                "trusted",
                "trusted:0:user",
                "Trusted source time",
                first_seen_at="2026-08-15T09:38:00Z",
                last_seen_at="2026-06-04T06:40:37Z",
            ),
            _history_row(
                "fallback",
                "fallback:0:user",
                "Capture fallback",
                first_seen_at="2026-08-15T09:37:00Z",
                last_seen_at="2026-08-15T09:37:00Z",
            ),
            _history_row(
                "regressed",
                "regressed:0:user",
                "Later refresh fallback",
                first_seen_at="2026-08-13T09:31:25Z",
                last_seen_at="2026-08-15T09:38:36Z",
            ),
        ],
        GEMINI_HISTORY_SCHEMA,
    )

    newest_page = query_chat_history(tmp_path, source="gemini", session_view=True, sort="newest")
    oldest_page = query_chat_history(tmp_path, source="gemini", session_view=True, sort="oldest")

    assert [session.conversation_id for session in newest_page.sessions] == [
        "trusted",
        "fallback",
        "regressed",
    ]
    assert [session.conversation_id for session in oldest_page.sessions] == [
        "trusted",
        "fallback",
        "regressed",
    ]
    assert newest_page.sessions[0].last_seen_at == "2026-06-04T06:40:37Z"
    assert newest_page.sessions[1].last_seen_at == ""
    assert newest_page.sessions[2].last_seen_at == ""
    assert format_chat_message_timestamp_label(newest_page.sessions[2].last_seen_at) == "Unknown time"


def test_chatgpt_capture_fallback_is_unknown_until_source_time_is_refreshed(tmp_path: Path) -> None:
    history_path = tmp_path / "llm" / "chatgpt" / "history.parquet"
    write_parquet_rows_atomic(
        history_path,
        [
            _history_row(
                "legacy-chatgpt",
                "legacy-chatgpt:0",
                "Legacy ChatGPT message",
                first_seen_at="2026-08-14T02:27:07Z",
                last_seen_at="2026-08-14T02:27:07Z",
            )
        ],
        GEMINI_HISTORY_SCHEMA,
    )

    page = query_chat_history(tmp_path, source="chatgpt")

    assert page.items[0].last_seen_at == ""
    assert format_chat_message_timestamp_label(page.items[0].last_seen_at) == "Unknown time"


def test_query_chatgpt_history_lists_sessions_on_the_home_page(tmp_path: Path) -> None:
    history_path = tmp_path / "llm" / "chatgpt" / "history.parquet"
    rows = [
        dict(
            _history_row(
                "chat-new",
                "chat-new:user",
                "ChatGPT first message",
                conversation_title="Newest ChatGPT session",
                last_seen_at="2026-08-12T08:00:00Z",
            ),
            platform="chatgpt",
            conversation_url="https://chatgpt.com/c/chat-new",
        ),
        dict(
            _history_row(
                "chat-new",
                "chat-new:assistant",
                "ChatGPT complete response",
                role="assistant",
                conversation_title="Newest ChatGPT session",
                last_seen_at="2026-08-12T08:01:00Z",
            ),
            platform="chatgpt",
            conversation_url="https://chatgpt.com/c/chat-new",
        ),
        dict(
            _history_row(
                "chat-old",
                "chat-old:user",
                "Older ChatGPT message",
                conversation_title="Older ChatGPT session",
                last_seen_at="2026-08-11T08:00:00Z",
            ),
            platform="chatgpt",
            conversation_url="https://chatgpt.com/c/chat-old",
        ),
    ]
    write_parquet_rows_atomic(history_path, rows, GEMINI_HISTORY_SCHEMA)

    page = query_chat_history(tmp_path, source="chatgpt", session_view=True, page=1)
    second_page = query_chat_history(tmp_path, source="chatgpt", session_view=True, page=2)

    assert page.total_count == 3
    assert page.conversation_count == 2
    assert page.total_pages == 1
    assert len(page.sessions) == 2
    assert page.sessions[0].source == "chatgpt"
    assert page.sessions[0].conversation_id == "chat-new"
    assert page.sessions[0].message_count == 2
    assert {message.message_key for message in page.items} == {
        "chat-new:user",
        "chat-new:assistant",
        "chat-old:user",
    }
    assert second_page.current_page == 1


def test_query_chat_history_opens_one_session_and_searches_all_sessions(tmp_path: Path) -> None:
    history_path = tmp_path / "llm" / "gemini" / "history.parquet"
    rows = [
        _history_row("demo", f"demo:{index}", f"Message {index}", message_index=index)
        for index in range(3)
    ]
    rows.append(
        _history_row(
            "atour",
            "atour:0",
            "A separate cached message",
            conversation_title="亚朵星球：体验驱动的睡眠专家",
            last_seen_at="2026-08-11T05:00:00Z",
        )
    )
    write_parquet_rows_atomic(history_path, rows, GEMINI_HISTORY_SCHEMA)

    index_page = query_chat_history(tmp_path, source="gemini", session_view=True)
    session_id = index_page.sessions[0].stable_id
    detail_page = query_chat_history(
        tmp_path,
        source="gemini",
        session_view=True,
        session=session_id,
        page_size=2,
    )

    assert detail_page.session_detail
    assert detail_page.current_session is not None
    assert detail_page.current_session.conversation_title == "Demo conversation"
    assert detail_page.total_count == 3
    assert detail_page.total_pages == 2
    assert [message.message_index for message in detail_page.items] == [0, 1]
    assert detail_page.pagination_unit == "message"

    scoped_page = query_chat_history(
        tmp_path, source="gemini", query="亚朵", session=session_id, page=6,
    )
    assert scoped_page.session_detail
    assert scoped_page.total_count == 0
    assert scoped_page.current_page == 1
    across_pages = query_chat_history(
        tmp_path, source="gemini", session=session_id, query="2", page_size=2,
    )
    assert across_pages.total_count == 1
    assert across_pages.items[0].message_index == 2

    global_search_page = query_chat_history(
        tmp_path,
        source="gemini",
        query="亚朵",
        session_view=True,
    )
    assert not global_search_page.session_detail
    assert global_search_page.current_session is None
    assert global_search_page.total_count == 1
    assert global_search_page.conversation_count == 1
    assert [session.conversation_id for session in global_search_page.sessions] == ["atour"]
    assert [message.conversation_id for message in global_search_page.items] == ["atour"]


def test_build_chat_history_markdown_exports_the_complete_session(tmp_path: Path) -> None:
    history_path = tmp_path / "llm" / "gemini" / "history.parquet"
    rows = [
        _history_row("demo", "demo:0", "A question", message_index=0),
        _history_row(
            "demo",
            "demo:1",
            "A response",
            role="assistant",
            message_index=1,
            last_seen_at="2026-08-12T05:01:00Z",
        ),
    ]
    rows[0]["source_links"] = ["https://example.com/source"]
    write_parquet_rows_atomic(history_path, rows, GEMINI_HISTORY_SCHEMA)

    index_page = query_chat_history(tmp_path, source="gemini", session_view=True)
    detail_page = query_chat_history(
        tmp_path,
        source="gemini",
        session_view=True,
        session=index_page.sessions[0].stable_id,
        page_size=100,
    )

    markdown = build_chat_history_markdown(detail_page)
    assert markdown.startswith("# Demo conversation\n")
    assert "- Messages: 2" in markdown
    assert f"### 1. You · {format_chat_message_timestamp_label('2026-08-12T05:00:00Z')}" in markdown
    assert f"### 2. Gemini · {format_chat_message_timestamp_label('2026-08-12T05:01:00Z')}" in markdown
    assert "Source link 1: https://example.com/source" in markdown
    assert markdown.endswith("\n")


def test_query_chat_history_exposes_adjacent_sessions_in_sorted_order(tmp_path: Path) -> None:
    history_path = tmp_path / "llm" / "gemini" / "history.parquet"
    rows = [
        _history_row("newest", "newest:0", "Newest", last_seen_at="2026-08-12T08:00:00Z"),
        _history_row("middle", "middle:0", "Middle", last_seen_at="2026-08-12T07:00:00Z"),
        _history_row("oldest", "oldest:0", "Oldest", last_seen_at="2026-08-12T06:00:00Z"),
    ]
    write_parquet_rows_atomic(history_path, rows, GEMINI_HISTORY_SCHEMA)

    index_page = query_chat_history(tmp_path, source="gemini", session_view=True)
    middle_id = index_page.sessions[1].stable_id
    detail_page = query_chat_history(
        tmp_path,
        source="gemini",
        session_view=True,
        session=middle_id,
    )

    assert detail_page.previous_session is not None
    assert detail_page.previous_session.conversation_id == "newest"
    assert detail_page.next_session is not None
    assert detail_page.next_session.conversation_id == "oldest"


def test_query_chat_history_session_home_uses_100_sessions_per_page(tmp_path: Path) -> None:
    history_path = tmp_path / "llm" / "gemini" / "history.parquet"
    rows = [
        _history_row(
            f"session-{index}",
            f"session-{index}:0",
            f"Message {index}",
            conversation_title=f"Session {index}",
            last_seen_at=f"2026-08-12T05:{index % 60:02d}:00Z",
        )
        for index in range(101)
    ]
    write_parquet_rows_atomic(history_path, rows, GEMINI_HISTORY_SCHEMA)

    first_page = query_chat_history(tmp_path, source="gemini", session_view=True)
    second_page = query_chat_history(tmp_path, source="gemini", session_view=True, page=2)

    assert first_page.page_size == 100
    assert first_page.total_pages == 2
    assert len(first_page.sessions) == 100
    assert len(second_page.sessions) == 1


def test_format_chat_message_timestamp_label_uses_zero_padded_day_in_display_timezone() -> None:
    expected = datetime.fromisoformat("2026-08-01T01:02:03+00:00").astimezone(
        ZoneInfo("Asia/Hong_Kong")
    )
    assert format_chat_message_timestamp_label("2026-08-01T01:02:03Z") == (
        f"{expected.day:02d}/{expected.month:02d}/{expected.year:04d} "
        f"{expected.hour:02d}:{expected.minute:02d}:{expected.second:02d} "
        "(HKT)"
    )


def test_chat_history_points_to_existing_media_without_copying_payload(tmp_path: Path) -> None:
    history_path = tmp_path / "llm" / "gemini" / "history.parquet"
    conversation_url = "https://gemini.google.com/app/demo"
    write_parquet_rows_atomic(
        history_path,
        [_history_row("demo", "demo:0:user", "A message")],
        GEMINI_HISTORY_SCHEMA,
    )
    media_item = LocalMediaItem(
        stable_id="media-demo",
        source="chatgpt",
        media_kind="image",
        relative_path="chatgpt/demo/image.png",
        filename="image.png",
        title="image.png",
        description="",
        creator="Demo",
        source_url=conversation_url,
        captured_at="2026-08-12T05:00:00Z",
        captured_at_label="12 Aug 2026",
        content_bytes=12,
        project_name="Demo",
    )

    page = attach_media_references(
        query_chat_history(tmp_path),
        [media_item],
        lambda stable_id: f"/browser?view=media&media_id={stable_id}",
        lambda item: f"/browser/media/{item.relative_path}",
    )

    assert page.items[0].media_refs[0].stable_id == "media-demo"
    assert page.items[0].media_refs[0].href == "/browser?view=media&media_id=media-demo"
    assert page.items[0].media_refs[0].media_url == "/browser/media/chatgpt/demo/image.png"
    assert page.items[0].media_refs[0].media_kind == "image"
    assert not hasattr(page.items[0], "media_payload")


def test_session_metadata_uses_newer_catalog_and_preserves_identity(tmp_path: Path) -> None:
    from datetime import UTC, timedelta
    from app.core.agent_source_cache import AgentSourceCache
    from app.core.resource_persistence import CHATGPT_HISTORY_SCHEMA

    row = _history_row("move-demo", "move-demo:0", "Original message")
    row.update(platform="chatgpt", conversation_url="https://chatgpt.com/c/move-demo")
    path = tmp_path / "llm/chatgpt/history.parquet"
    write_parquet_rows_atomic(path, [row], CHATGPT_HISTORY_SCHEMA)
    original_bytes = path.read_bytes()
    before = query_chat_history(tmp_path, source="chatgpt", session_view=True).sessions[0]
    cache = AgentSourceCache(tmp_path)
    payload = {"sessions": [{"title": "Renamed session", "url": "https://chatgpt.com/g/project/c/move-demo"}]}
    cache.store(platform="chatgpt", browser="edge", source_kind="project-sessions", payload=payload,
                now=datetime.now(UTC) - timedelta(days=1))
    assert query_chat_history(tmp_path).items[0].conversation_title == "Demo conversation"
    cache.store(platform="chatgpt", browser="edge", source_kind="project-sessions", payload=payload,
                now=datetime.now(UTC) + timedelta(seconds=1))
    after = query_chat_history(tmp_path, source="chatgpt", session_view=True).sessions[0]
    assert after.stable_id == before.stable_id
    assert after.conversation_title == "Renamed session"
    assert after.conversation_url == payload["sessions"][0]["url"]
    assert path.read_bytes() == original_bytes


def test_projects_use_latest_provider_catalog_and_stable_identity(tmp_path: Path) -> None:
    """Count containers once across browsers, retaining same-title distinct projects."""
    from datetime import UTC, timedelta
    from app.core.agent_source_cache import AgentSourceCache

    cache = AgentSourceCache(tmp_path)
    now = datetime.now(UTC)
    cache.store(platform="chatgpt", browser="chrome", source_kind="sources",
                payload={"projects": [{"id": "removed"}]}, now=now - timedelta(days=1))
    cache.store(platform="chatgpt", browser="edge", source_kind="browser-session",
                payload={"agent_sources": {"projects": [
                    {"id": "first", "title": "Same", "url": "https://chatgpt.com/g/first/project"},
                    {"id": "alias", "url": "https://chatgpt.com/g/first/project/?tracking=1"},
                    {"id": "second", "title": "Same"},
                    {"title": "No stable identity"}, None,
                ]}}, now=now)
    for platform in ("gemini", "grok", "claude"):
        cache.store(platform=platform, browser="edge", source_kind="sources",
                    payload={"projects": [{"id": "second"}]}, now=now)
    assert query_chat_history(tmp_path).project_count == 5
    assert query_chat_history(tmp_path, source="chatgpt", query="no matches", page=9).project_count == 2
    assert query_chat_history(tmp_path, source="gemini").project_count == 1
    cache.store(platform="gemini", browser="chrome", source_kind="sources",
                payload={"projects": []}, now=now + timedelta(seconds=1))
    assert query_chat_history(tmp_path).project_count == 4
    cache.store(platform="chatgpt", browser="edge", source_kind="sources",
                payload={"projects": "invalid"}, now=now + timedelta(seconds=2))
    assert query_chat_history(tmp_path).project_count == 4
