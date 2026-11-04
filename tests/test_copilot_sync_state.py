"""Durable Copilot coverage proof, interruption, and conservative reload checks."""

# Code version: v1.1.0-codex.0

from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.copilot_history_store import CopilotHistoryStore, build_copilot_initial_snapshot
from app.core.copilot_sync_state import (
    COPILOT_SYNC_STATE_SCHEMA,
    copilot_sync_state_path,
    read_copilot_sync_state,
    write_copilot_sync_state,
)
from app.core.resource_persistence import COPILOT_HISTORY_SCHEMA, read_parquet_rows, write_parquet_rows_atomic


def _complete(**changes) -> dict:
    return {
        "schema_version": 1,
        "status": "complete",
        "started_at": "2026-10-21T01:00:00Z",
        "finished_at": "2026-10-21T02:00:00Z",
        "discovery_complete": True,
        "sync_complete": True,
        "expected_sessions": 500,
        "discovered_sessions": 500,
        "processed_sessions": 500,
        "failed_sessions": 0,
        "completion_basis": "Fixture complete index with exhausted continuation.",
        "reason": "",
        **changes,
    }


def _persist_history(root: Path, sessions: int = 2) -> CopilotHistoryStore:
    store = CopilotHistoryStore(root)
    for index in range(sessions):
        store.replace_conversation(
            {"conversation_id": f"fixture-session-{index}"},
            [{"role": "user", "content_text": f"Synthetic fixture message {index}."}],
        )
    return store


def test_legacy_history_has_unknown_coverage_without_inventing_progress(tmp_path: Path) -> None:
    store = CopilotHistoryStore(tmp_path)
    store.replace_conversation(
        {"conversation_id": "fixture-session"},
        [{"role": "user", "content_text": "Synthetic fixture text."}],
    )
    before = store.path.read_bytes()
    coverage = read_copilot_sync_state(tmp_path)
    snapshot = build_copilot_initial_snapshot("test", tmp_path)
    assert coverage["status"] == "unverified"
    assert coverage["expected_sessions"] is None
    assert not coverage["discovery_complete"] and not coverage["sync_complete"]
    assert not copilot_sync_state_path(tmp_path).exists()
    assert snapshot.downloaded_posts == snapshot.downloaded_tweets == 1
    assert snapshot.discovered_tweets == snapshot.queued_tweets == snapshot.processed_tweets == 0
    assert snapshot.performance_metrics["sync_complete"] is False
    assert "not been verified" in snapshot.message
    assert store.path.read_bytes() == before


def test_partial_checkpoint_survives_reload_and_keeps_unknown_total(tmp_path: Path) -> None:
    write_copilot_sync_state(
        tmp_path,
        {
            "status": "incomplete",
            "discovered_sessions": 17,
            "processed_sessions": 17,
            "reason": "Only recent conversations were exposed; the full history total is unknown.",
        },
    )
    coverage = read_copilot_sync_state(tmp_path)
    assert coverage["expected_sessions"] is None
    assert coverage["discovered_sessions"] == coverage["processed_sessions"] == 17
    snapshot = build_copilot_initial_snapshot("reloaded", tmp_path)
    assert snapshot.phase == "failed"
    assert snapshot.queued_tweets == snapshot.processed_tweets == 17
    assert snapshot.performance_metrics["sync_complete"] is False
    assert not snapshot.discovery_complete
    assert "full history total is unknown" in snapshot.message
    rows = read_parquet_rows(copilot_sync_state_path(tmp_path))
    assert len(rows) == 1 and set(rows[0]) == set(COPILOT_SYNC_STATE_SCHEMA.names)


def test_running_checkpoint_reloads_as_interrupted_without_losing_counts(tmp_path: Path) -> None:
    write_copilot_sync_state(
        tmp_path,
        {"status": "running", "discovered_sessions": 500, "processed_sessions": 17},
    )
    before = copilot_sync_state_path(tmp_path).read_bytes()
    snapshot = build_copilot_initial_snapshot("reloaded", tmp_path)
    assert snapshot.phase == "failed" and not snapshot.running
    assert snapshot.discovered_tweets == 500 and snapshot.processed_tweets == 17
    assert snapshot.performance_metrics["status"] == "incomplete"
    assert snapshot.performance_metrics["sync_complete"] is False
    assert "interrupted" in snapshot.message
    assert copilot_sync_state_path(tmp_path).read_bytes() == before


def test_verified_complete_checkpoint_survives_reload(tmp_path: Path) -> None:
    store = _persist_history(tmp_path)
    write_copilot_sync_state(tmp_path, _complete(expected_sessions=2, discovered_sessions=2, processed_sessions=2))
    before = (store.path.read_bytes(), copilot_sync_state_path(tmp_path).read_bytes())
    snapshot = build_copilot_initial_snapshot("reloaded", tmp_path)
    assert snapshot.phase == "finished"
    assert snapshot.discovery_complete
    assert snapshot.performance_metrics["sync_complete"] is True
    assert snapshot.queued_tweets == snapshot.processed_tweets == 2
    assert snapshot.downloaded_posts == snapshot.downloaded_tweets == 2
    assert "coverage was verified" in snapshot.message
    assert (store.path.read_bytes(), copilot_sync_state_path(tmp_path).read_bytes()) == before


@pytest.mark.parametrize("remaining_sessions", [0, 1])
def test_missing_or_truncated_history_invalidates_complete_readback_without_rewriting(
    tmp_path: Path, remaining_sessions: int
) -> None:
    store = _persist_history(tmp_path)
    write_copilot_sync_state(tmp_path, _complete(expected_sessions=2, discovered_sessions=2, processed_sessions=2))
    manifest_path = copilot_sync_state_path(tmp_path)
    before_manifest = (manifest_path.read_bytes(), manifest_path.stat().st_mtime_ns)
    if remaining_sessions:
        write_parquet_rows_atomic(store.path, store.rows[:remaining_sessions], COPILOT_HISTORY_SCHEMA)
        before_history = (store.path.read_bytes(), store.path.stat().st_mtime_ns)
    else:
        store.path.unlink()
        before_history = None
    snapshot = build_copilot_initial_snapshot("reloaded", tmp_path)
    assert snapshot.phase == "failed"
    assert snapshot.performance_metrics["status"] == "incomplete"
    assert snapshot.performance_metrics["sync_complete"] is False
    assert snapshot.downloaded_posts == remaining_sessions
    assert snapshot.discovered_tweets == snapshot.processed_tweets == 2
    assert f"Cached history has {remaining_sessions} sessions" in snapshot.message
    assert "no longer verified" in snapshot.last_error
    assert (manifest_path.read_bytes(), manifest_path.stat().st_mtime_ns) == before_manifest
    assert read_copilot_sync_state(tmp_path)["sync_complete"] is True
    if before_history is None:
        assert not store.path.exists()
    else:
        assert (store.path.read_bytes(), store.path.stat().st_mtime_ns) == before_history


@pytest.mark.parametrize(
    "changes",
    [
        {"discovery_complete": False},
        {"sync_complete": False},
        {"processed_sessions": 17},
        {"failed_sessions": 1},
        {"expected_sessions": 501},
        {"completion_basis": ""},
        {"status": "running"},
        {"discovered_sessions": True},
        {"processed_sessions": -1},
        {"expected_sessions": "500"},
    ],
)
def test_invalid_completion_never_replaces_a_prior_checkpoint(tmp_path: Path, changes: dict) -> None:
    write_copilot_sync_state(tmp_path, {"status": "incomplete", "reason": "Fixture partial history."})
    before = copilot_sync_state_path(tmp_path).read_bytes()
    with pytest.raises(ValueError):
        write_copilot_sync_state(tmp_path, _complete(**changes))
    assert copilot_sync_state_path(tmp_path).read_bytes() == before
    assert not read_copilot_sync_state(tmp_path)["sync_complete"]


def test_corrupt_or_contradictory_checkpoint_cannot_hydrate_as_success(tmp_path: Path) -> None:
    path = copilot_sync_state_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"Invalid fixture checkpoint.")
    assert read_copilot_sync_state(tmp_path)["status"] == "invalid"
    assert build_copilot_initial_snapshot("test", tmp_path).phase == "failed"
    assert path.read_bytes() == b"Invalid fixture checkpoint."
    write_parquet_rows_atomic(path, [_complete(discovery_complete=False)], COPILOT_SYNC_STATE_SCHEMA)
    assert read_copilot_sync_state(tmp_path)["status"] == "invalid"
    assert build_copilot_initial_snapshot("test", tmp_path).performance_metrics["sync_complete"] is False


def test_atomic_checkpoint_write_failure_preserves_prior_bytes(tmp_path: Path) -> None:
    write_copilot_sync_state(tmp_path, {"status": "incomplete"})
    before = copilot_sync_state_path(tmp_path).read_bytes()
    with patch("app.core.copilot_sync_state.write_parquet_rows_atomic", side_effect=OSError("Fixture write failed.")):
        with pytest.raises(OSError, match="Fixture write failed"):
            write_copilot_sync_state(tmp_path, _complete())
    assert copilot_sync_state_path(tmp_path).read_bytes() == before
