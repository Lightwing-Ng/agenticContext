"""Copilot storage durability, transcript validation, and source isolation regressions."""

# Code version: v1.3.0-codex.0

from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.copilot_history_store import (
    CopilotHistoryStore,
    build_copilot_initial_snapshot,
    copilot_history_path,
)
from app.core.resource_persistence import COPILOT_HISTORY_SCHEMA, read_resource_rows


def _conversation(identity: str = "session-1") -> dict:
    return {
        "conversation_id": identity,
        "conversation_url": f"https://copilot.com/chat/conversation/{identity}",
        "conversation_title": "Fixture conversation",
    }


def _messages() -> list[dict]:
    return [
        {"role": "user", "content_text": "Fixture question.", "message_id": "question-1"},
        {
            "role": "assistant",
            "content_text": "Fixture answer.",
            "message_id": "answer-1",
            "content_html": "<p>Fixture answer.</p>",
            "source_links": ["https://example.com/source", "javascript:alert(1)", "https://example.com/source"],
        },
    ]


def test_round_trip_uses_independent_path_and_retains_unknown_provider_dates(tmp_path: Path) -> None:
    store = CopilotHistoryStore(tmp_path)
    assert not copilot_history_path(tmp_path).exists()
    result = store.replace_conversation(_conversation(), _messages(), "2026-10-21T01:00:00Z")
    assert store.path == tmp_path / "llm/copilot/history.sqlite3"
    assert result == {
        "message_count": 2,
        "added_or_changed": 2,
        "unchanged_messages": 0,
        "unchanged_sessions": 0,
    }
    reloaded = CopilotHistoryStore(tmp_path)
    assert reloaded.cached_conversations == 1
    assert reloaded.cached_messages == 2
    assert [row["platform"] for row in reloaded.rows] == ["copilot", "copilot"]
    assert [row["last_seen_at"] for row in reloaded.rows] == ["", ""]
    assert {row["first_seen_at"] for row in reloaded.rows} == {"2026-10-21T01:00:00Z"}
    assert reloaded.rows[1]["source_links"] == ["https://example.com/source"]
    assert set(reloaded.rows[0]) == set(COPILOT_HISTORY_SCHEMA.names)


def test_repeat_capture_preserves_bytes_mtime_and_first_seen_metadata(tmp_path: Path) -> None:
    store = CopilotHistoryStore(tmp_path)
    store.replace_conversation(_conversation(), _messages(), "2026-10-21T01:00:00Z")
    before = (store.path.read_bytes(), store.path.stat().st_mtime_ns)
    result = store.replace_conversation(_conversation(), _messages(), "2026-10-21T02:00:00Z")
    assert (store.path.read_bytes(), store.path.stat().st_mtime_ns) == before
    assert result["added_or_changed"] == 0
    assert result["unchanged_sessions"] == 1
    assert result["unchanged_messages"] == 2


def test_provider_dates_and_first_seen_have_independent_meanings(tmp_path: Path) -> None:
    store = CopilotHistoryStore(tmp_path)
    messages = _messages()
    messages[1]["message_timestamp"] = "2026-10-20T22:00:00Z"
    store.replace_conversation(_conversation(), messages, "2026-10-21T01:00:00Z")
    messages[1]["content_text"] = "Edited fixture answer."
    result = store.replace_conversation(_conversation(), messages, "2026-10-21T02:00:00Z")
    assert result["added_or_changed"] == 1
    assert store.rows[1]["last_seen_at"] == "2026-10-20T22:00:00Z"
    assert store.rows[1]["first_seen_at"] == "2026-10-21T01:00:00Z"
    messages[1].pop("message_timestamp")
    store.replace_conversation(_conversation(), messages, "2026-10-21T03:00:00Z")
    assert store.rows[1]["last_seen_at"] == "2026-10-20T22:00:00Z"


@pytest.mark.parametrize(
    "messages",
    [
        [],
        None,
        [None],
        [{"role": "user", "content_text": ""}],
        [{"role": "system", "content_text": "Unsupported role."}],
        [{"role": "user", "content_text": {"unexpected": "object"}}],
        [{"role": "user", "content_text": "Fixture.", "message_index": -1}],
        [{"role": "user", "content_text": "Fixture.", "message_index": 0.5}],
        [{"role": "user", "content_text": "Fixture."}, {"role": "assistant", "content_text": ""}],
        [
            {"role": "user", "content_text": "Fixture.", "message_id": "duplicate"},
            {"role": "assistant", "content_text": "Fixture.", "message_id": "duplicate"},
        ],
    ],
)
def test_empty_or_malformed_capture_cannot_truncate_existing_history(tmp_path: Path, messages) -> None:
    store = CopilotHistoryStore(tmp_path)
    store.replace_conversation(_conversation(), _messages())
    previous_rows = store.rows
    previous_bytes = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.replace_conversation(_conversation(), messages)
    assert store.rows == previous_rows
    assert store.path.read_bytes() == previous_bytes


def test_incomplete_metadata_cannot_replace_existing_history(tmp_path: Path) -> None:
    store = CopilotHistoryStore(tmp_path)
    store.replace_conversation(_conversation(), _messages())
    before = store.path.read_bytes()
    for metadata in ({}, None, {**_conversation(), "complete": False}):
        with pytest.raises(ValueError):
            store.replace_conversation(metadata, _messages())
    assert store.path.read_bytes() == before


def test_complete_replacement_removes_old_session_rows_but_retains_other_sessions(tmp_path: Path) -> None:
    store = CopilotHistoryStore(tmp_path)
    store.replace_conversation(_conversation(), _messages())
    store.replace_conversation(_conversation("session-2"), _messages())
    second_session = [row for row in store.rows if row["conversation_id"] == "session-2"]
    result = store.replace_conversation(_conversation(), _messages()[:1])
    assert result["unchanged_sessions"] == 0
    assert store.cached_messages == 3
    assert [row for row in store.rows if row["conversation_id"] == "session-2"] == second_session
    assert len(read_resource_rows(store.path)) == 3


def test_failed_atomic_write_does_not_change_in_memory_or_persisted_rows(tmp_path: Path) -> None:
    store = CopilotHistoryStore(tmp_path)
    store.replace_conversation(_conversation(), _messages())
    before = (store.rows, store.path.read_bytes())
    with patch("app.core.copilot_history_store.write_resource_rows_atomic", side_effect=OSError("Fixture write failed.")):
        with pytest.raises(OSError, match="Fixture write failed"):
            store.replace_conversation(_conversation(), _messages()[:1])
    assert (store.rows, store.path.read_bytes()) == before


def test_snapshot_is_isolated_and_corrupt_cache_is_preserved(tmp_path: Path) -> None:
    other = tmp_path / "llm/chatgpt/history.sqlite3"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"Other provider fixture.")
    assert build_copilot_initial_snapshot("test", tmp_path).downloaded_tweets == 0
    store = CopilotHistoryStore(tmp_path)
    store.replace_conversation(_conversation(), _messages())
    snapshot = build_copilot_initial_snapshot("test", tmp_path)
    assert snapshot.downloaded_posts == 1
    assert snapshot.downloaded_tweets == 2
    assert snapshot.discovered_tweets == snapshot.queued_tweets == snapshot.processed_tweets == 0
    assert snapshot.performance_metrics["sync_complete"] is False
    assert "completeness has not been verified" in snapshot.message
    assert snapshot.output_dir == str(tmp_path / "llm/copilot")
    store.path.write_bytes(b"Invalid Copilot fixture.")
    snapshot = build_copilot_initial_snapshot("test", tmp_path)
    assert "unreadable" in snapshot.last_error
    assert store.path.read_bytes() == b"Invalid Copilot fixture."
    assert other.read_bytes() == b"Other provider fixture."


def test_returned_rows_are_detached_from_store(tmp_path: Path) -> None:
    store = CopilotHistoryStore(tmp_path)
    store.replace_conversation(_conversation(), _messages())
    detached = store.rows
    detached[1]["source_links"].clear()
    detached[0]["content_text"] = "Changed outside store."
    assert store.rows[0]["content_text"] == "Fixture question."
    assert store.rows[1]["source_links"] == ["https://example.com/source"]


def test_reload_cannot_claim_full_coverage_when_legacy_directory_remains_unverified(tmp_path: Path) -> None:
    from app.core.copilot_sync_state import read_copilot_sync_state, write_copilot_sync_state

    store = CopilotHistoryStore(tmp_path)
    store.replace_conversation(_conversation(), _messages())
    write_copilot_sync_state(tmp_path, {
        "status": "complete", "discovery_complete": True, "sync_complete": True,
        "discovered_sessions": 1, "processed_sessions": 1, "completion_basis": "Current-web fixture exhausted.",
    })
    before_history = store.path.read_bytes()
    before_coverage = read_copilot_sync_state(tmp_path)
    legacy = {"status": "valid", "sessions": 357, "conversation_ids": [], "catalog_complete": False}
    with patch("app.core.copilot_history_store.read_copilot_legacy_catalog", return_value=legacy):
        snapshot = build_copilot_initial_snapshot("reloaded", tmp_path)
    assert snapshot.downloaded_posts == 1 and snapshot.downloaded_tweets == 2
    assert snapshot.discovered_tweets == snapshot.queued_tweets == 357
    assert not snapshot.discovery_complete and not snapshot.performance_metrics["sync_complete"]
    assert snapshot.performance_metrics["expected_sessions"] is None
    assert snapshot.phase == "failed" and "legacy" in snapshot.message
    assert store.path.read_bytes() == before_history
    assert read_copilot_sync_state(tmp_path) == before_coverage
