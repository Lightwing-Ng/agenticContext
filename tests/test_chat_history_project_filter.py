"""Verify cached ChatGPT project filtering and session-navigation boundaries."""

# Code version: v1.0.0-codex.0

from datetime import UTC, datetime, timedelta
import os
from pathlib import Path

from app.core.agent_source_cache import AgentSourceCache
from app.core.chat_history_browser import query_chat_history
from app.core.resource_persistence import CHATGPT_HISTORY_SCHEMA, write_parquet_rows_atomic


PROJECT_A = "g-p-" + "a" * 32
PROJECT_B = "g-p-" + "b" * 32


def _project_url(key: str, slug: str = "") -> str:
    return f"https://chatgpt.com/g/{key}{slug}/project"


def _write_history(tmp_path: Path, sessions: list[tuple[str, str]]) -> None:
    rows = [
        {
            "schema_version": 1,
            "platform": "chatgpt",
            "conversation_id": session_id,
            "conversation_url": url,
            "conversation_title": f"Session {session_id}",
            "message_key": f"{session_id}:0",
            "turn_index": 0,
            "message_index": 0,
            "role": "user",
            "author_label": "You",
            "content_text": "Searchable message",
            "content_html": "",
            "content_sha256": "fixture",
            "source_links": [],
            "model_label": "",
            "first_seen_at": "2026-08-15T09:00:00Z",
            "last_seen_at": "2026-08-12T05:00:00Z",
        }
        for session_id, url in sessions
    ]
    write_parquet_rows_atomic(tmp_path / "llm/chatgpt/history.parquet", rows, CHATGPT_HISTORY_SCHEMA)


def _store_projects(cache: AgentSourceCache, *, now: datetime) -> None:
    cache.store(
        platform="chatgpt", browser="edge", source_kind="sources", now=now,
        payload={"projects": [
            {"id": PROJECT_A, "title": "Alpha", "url": _project_url(PROJECT_A)},
            {"id": PROJECT_B, "title": "Beta", "url": _project_url(PROJECT_B)},
        ]},
    )


def _store_sessions(
    cache: AgentSourceCache,
    project: str,
    session_ids: list[str],
    *,
    now: datetime,
    browser: str = "edge",
) -> None:
    cache.store(
        platform="chatgpt", browser=browser, source_kind="project-sessions",
        project_url=_project_url(project), now=now,
        payload={"sessions": [
            {"id": session_id, "url": f"https://chatgpt.com/c/{session_id}"}
            for session_id in session_ids
        ]},
    )


def test_project_filter_scopes_search_pagination_and_session_navigation(tmp_path: Path) -> None:
    _write_history(tmp_path, [
        (session_id, f"https://chatgpt.com/c/{session_id}")
        for session_id in ("alpha-1", "alpha-2", "beta", "unknown")
    ])
    cache = AgentSourceCache(tmp_path)
    now = datetime.now(UTC)
    _store_projects(cache, now=now)
    _store_sessions(cache, PROJECT_A, ["alpha-1", "alpha-2"], now=now)
    _store_sessions(cache, PROJECT_B, ["beta"], now=now)
    history_path = tmp_path / "llm/chatgpt/history.parquet"
    before = history_path.read_bytes()

    page = query_chat_history(
        tmp_path, source="chatgpt", project=PROJECT_A,
        session_view=True, sort="name", page_size=1, page=2,
    )
    assert page.selected_project == PROJECT_A
    assert [(item.key, item.name) for item in page.project_options] == [
        (PROJECT_A, "Alpha"), (PROJECT_B, "Beta"),
    ]
    assert page.project_count == 2
    assert page.total_count == page.conversation_count == 2
    assert page.total_pages == page.current_page == 2
    assert [item.conversation_id for item in page.sessions] == ["alpha-2"]
    assert query_chat_history(
        tmp_path, source="chatgpt", project=PROJECT_A, query="Session beta",
    ).total_count == 0
    assert query_chat_history(
        tmp_path, source="chatgpt", project=PROJECT_A, query="Searchable",
    ).total_count == 2

    detail = query_chat_history(
        tmp_path, source="chatgpt", project=PROJECT_A,
        session="chatgpt:alpha-1", sort="name",
    )
    assert detail.session_detail
    assert detail.selected_project == PROJECT_A
    assert detail.previous_session is None
    assert detail.next_session is not None
    assert detail.next_session.conversation_id == "alpha-2"
    last = query_chat_history(
        tmp_path, source="chatgpt", project=PROJECT_A,
        session="chatgpt:alpha-2", sort="name",
    )
    assert last.next_session is None
    outside = query_chat_history(
        tmp_path, source="chatgpt", project=PROJECT_A,
        session="chatgpt:beta", session_view=True,
    )
    assert not outside.session_detail
    assert {item.conversation_id for item in outside.items} == {"alpha-1", "alpha-2"}
    assert history_path.read_bytes() == before


def test_project_options_use_latest_catalog_and_deduplicate_slug_aliases(tmp_path: Path) -> None:
    cache = AgentSourceCache(tmp_path)
    now = datetime.now(UTC) - timedelta(days=1)
    cache.store(
        platform="chatgpt", browser="chrome", source_kind="sources", now=now,
        payload={"projects": [{"id": "removed", "title": "Old project"}]},
    )
    cache.store(
        platform="chatgpt", browser="edge", source_kind="browser-session",
        now=now + timedelta(seconds=1),
        payload={"agent_sources": {"projects": [
            {"id": PROJECT_A, "title": "Same name", "url": _project_url(PROJECT_A, "-old-name")},
            {"id": PROJECT_A, "title": "Alias", "url": _project_url(PROJECT_A, "-new-name")},
            {"id": PROJECT_B, "title": "Same name"},
            {"title": "Missing identity"}, None,
        ]}},
    )
    page = query_chat_history(tmp_path, source="chatgpt")
    assert [(item.key, item.name) for item in page.project_options] == [
        (PROJECT_A, "Same name"), (PROJECT_B, "Same name"),
    ]
    normalized = query_chat_history(
        tmp_path, source="chatgpt", project=_project_url(PROJECT_A, "-renamed"),
    )
    assert normalized.selected_project == PROJECT_A
    assert normalized.project_options == page.project_options
    cache.store(
        platform="chatgpt", browser="edge", source_kind="sources",
        now=now + timedelta(seconds=2), payload={"projects": []},
    )
    assert query_chat_history(tmp_path, source="chatgpt").project_options == ()


def test_stale_membership_reads_latest_snapshot_and_explicit_history_urls(tmp_path: Path) -> None:
    _write_history(tmp_path, [
        ("direct", f"https://chatgpt.com/g/{PROJECT_A}-slug/c/direct"),
        ("catalog", "https://chatgpt.com/c/catalog"),
        ("removed", "https://chatgpt.com/c/removed"),
        ("unknown", "https://chatgpt.com/c/unknown"),
    ])
    cache = AgentSourceCache(tmp_path)
    now = datetime.now(UTC) - timedelta(days=1)
    _store_projects(cache, now=now)
    _store_sessions(cache, PROJECT_A, ["catalog", "removed"], now=now, browser="chrome")
    _store_sessions(cache, PROJECT_A, ["catalog"], now=now + timedelta(seconds=1))
    page = query_chat_history(tmp_path, source="chatgpt", project=PROJECT_A)
    assert {item.conversation_id for item in page.items} == {"direct", "catalog"}
    _store_sessions(cache, PROJECT_B, ["catalog"], now=now + timedelta(seconds=2))
    assert {item.conversation_id for item in query_chat_history(
        tmp_path, source="chatgpt", project=PROJECT_A,
    ).items} == {"direct"}
    assert {item.conversation_id for item in query_chat_history(
        tmp_path, source="chatgpt", project=PROJECT_B,
    ).items} == {"catalog"}


def test_missing_or_invalid_project_fails_closed_and_other_sources_clear_filter(tmp_path: Path) -> None:
    _write_history(tmp_path, [("alpha", f"https://chatgpt.com/g/{PROJECT_A}/c/alpha")])
    for requested in (PROJECT_A, "missing-project", "https://untrusted.example/g/project"):
        page = query_chat_history(tmp_path, source="chatgpt", project=requested)
        assert page.selected_project == requested
        assert page.total_count == 0
        assert len(page.project_options) == 1
        assert page.project_options[0].name == "Unavailable project"
    assert query_chat_history(tmp_path, source="chatgpt").total_count == 1
    all_sources = query_chat_history(tmp_path, source="all", project=PROJECT_A)
    assert all_sources.selected_project == ""
    assert all_sources.project_options == ()
    assert all_sources.total_count == 1


def test_project_moves_use_observations_instead_of_history_file_mtime(tmp_path: Path) -> None:
    _write_history(tmp_path, [("moved", f"https://chatgpt.com/g/{PROJECT_A}/c/moved")])
    history_path = tmp_path / "llm/chatgpt/history.parquet"
    now = datetime.now(UTC) - timedelta(days=1)
    newer_file_time = (now + timedelta(days=2)).timestamp()
    os.utime(history_path, (newer_file_time, newer_file_time))
    cache = AgentSourceCache(tmp_path)
    _store_projects(cache, now=now)
    _store_sessions(cache, PROJECT_B, ["moved"], now=now)
    assert query_chat_history(tmp_path, source="chatgpt", project=PROJECT_A).total_count == 0
    assert query_chat_history(tmp_path, source="chatgpt", project=PROJECT_B).total_count == 1
    cache.store(
        platform="chatgpt", browser="chrome", source_kind="sources",
        now=now + timedelta(seconds=1),
        payload={"recent_sessions": [
            {"id": "moved", "url": f"https://chatgpt.com/g/{PROJECT_A}-renamed/c/moved"},
        ]},
    )
    assert query_chat_history(tmp_path, source="chatgpt", project=PROJECT_A).total_count == 1
    assert query_chat_history(tmp_path, source="chatgpt", project=PROJECT_B).total_count == 0
