"""Strictly additive, provenance-preserving legacy Copilot recovery regressions."""

# Code version: v1.0.0-codex.0

import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.copilot_history_store import CopilotHistoryStore, build_copilot_initial_snapshot
from app.core.copilot_legacy_recovery import (
    CAPTURE_METHOD,
    import_copilot_legacy_capture,
    normalize_copilot_legacy_url,
    plan_copilot_legacy_import,
    validate_copilot_legacy_capture,
)
from app.core.copilot_sync_state import (
    copilot_sync_state_path,
    read_copilot_sync_state,
    write_copilot_sync_state,
)
from app.core.job_lock import CACHE_TASK_LOCK_NAME, CacheTaskLock
from app.core.resource_persistence import COPILOT_HISTORY_SCHEMA, read_parquet_rows, write_parquet_rows_atomic


def _capture(identity: str = "AbC123") -> dict:
    return {
        "schema_version": 1,
        "conversation_id": identity,
        "conversation_url": f"https://copilot.microsoft.com/chats/{identity}",
        "title": "Shared fixture title",
        "captured_at": "2026-10-21T09:00:00Z",
        "capture_method": CAPTURE_METHOD,
        "catalog_ordinal": 1,
        "raw_ax": "Synthetic accessibility record, including original role controls.",
        "source_dates": ["Synthetic date with unknown timezone"],
        "messages": [
            {"role": "user", "content": "  Preserve this question exactly.\n"},
            {"role": "assistant", "content": "```python\n    keep_indent()\n```\n\n| A | B |\n| - | - |\n| Complete | Cell |\n"},
        ],
    }


def _save(path: Path, capture: dict | None = None) -> Path:
    path.write_text(json.dumps(capture or _capture(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _seed(root: Path, count: int = 17) -> CopilotHistoryStore:
    store = CopilotHistoryStore(root)
    for index in range(count):
        store.replace_conversation(
            {
                "conversation_id": f"modern-{index}",
                "conversation_url": f"https://copilot.com/chat/conversation/modern-{index}",
                "conversation_title": "Shared fixture title",
            },
            [{"role": "user", "content_text": f"Synthetic existing question {index}."}],
            "2026-10-21T01:00:00Z",
        )
    write_copilot_sync_state(root, {
        "status": "stopped", "discovery_complete": True,
        "discovered_sessions": count, "processed_sessions": 2,
        "completion_basis": "Previous source cursor ended.",
    })
    return store


def _files(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_preview_does_not_write_even_lock_or_recovery_files(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    _seed(root)
    source = _save(tmp_path / "capture.json")
    before = _files(tmp_path)
    result = import_copilot_legacy_capture(root, source, known_catalog_sessions=357)
    assert result["added_sessions"] == 1 and result["added_messages"] == 2
    assert result["after_sessions"] == 18 and result["after_messages"] == 19
    assert result["applied"] is False and result["sync_complete"] is False
    assert _files(tmp_path) == before


def test_apply_keeps_existing_17_and_raw_bodies_with_verifiable_provenance(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    existing = _seed(root)
    existing_rows = existing.rows
    original = existing.path.read_bytes()
    original_state = copilot_sync_state_path(root).read_bytes()
    other = root / "llm/chatgpt/history.parquet"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"Protected other-provider fixture.")
    capture = _capture()
    source = _save(tmp_path / "capture.json", capture)
    raw = source.read_bytes()
    result = import_copilot_legacy_capture(root, source, known_catalog_sessions=357, apply=True)
    rows = CopilotHistoryStore(root).rows
    assert [row for row in rows if row["conversation_id"].startswith("modern-")] == existing_rows
    recovered = [row for row in rows if row["conversation_id"] == capture["conversation_id"]]
    assert [row["content_text"] for row in recovered] == [message["content"] for message in capture["messages"]]
    assert [row["message_key"] for row in recovered] == ["AbC123:legacy-copy-position:0:user", "AbC123:legacy-copy-position:1:assistant"]
    assert all(row["last_seen_at"] == "" and row["content_html"] == "" for row in recovered)
    assert all(row["first_seen_at"] == capture["captured_at"] for row in recovered)
    assert all(row["content_sha256"] == hashlib.sha256(row["content_text"].encode()).hexdigest() for row in recovered)
    backup = Path(result["backup_dir"])
    assert (backup / "capture.json").read_bytes() == source.read_bytes() == raw
    assert (backup / "history.parquet").read_bytes() == original
    assert (backup / "sync-state.parquet").read_bytes() == original_state
    audit = json.loads((backup / "import.json").read_bytes())
    assert audit["capture_sha256"] == hashlib.sha256(raw).hexdigest()
    assert audit["conversation_url"] == capture["conversation_url"]
    assert "provider message IDs unavailable" in audit["message_key_basis"]
    assert audit["applied"] is True
    assert other.read_bytes() == b"Protected other-provider fixture."
    coverage = read_copilot_sync_state(root)
    assert coverage["status"] == "incomplete" and not coverage["sync_complete"]
    assert not coverage["discovery_complete"]
    assert coverage["expected_sessions"] == coverage["discovered_sessions"] == 357
    assert coverage["processed_sessions"] == 2
    assert build_copilot_initial_snapshot("test", root).phase == "failed"


def test_repeated_capture_is_idempotent_and_does_not_merge_same_titles(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    source = _save(tmp_path / "capture.json")
    first = import_copilot_legacy_capture(root, source, known_catalog_sessions=357, apply=True)
    path = root / "llm/copilot/history.parquet"
    before = (path.read_bytes(), path.stat().st_mtime_ns, copilot_sync_state_path(root).read_bytes())
    capture = _capture()
    capture["captured_at"] = "2026-10-21T10:00:00+00:00"
    _save(source, capture)
    result = import_copilot_legacy_capture(root, source, known_catalog_sessions=357, apply=True)
    assert result["added_messages"] == 0 and result["duplicate_sessions"] == 1
    assert result["applied"] is False
    assert (path.read_bytes(), path.stat().st_mtime_ns, copilot_sync_state_path(root).read_bytes()) == before
    assert len(list((root / "recovery").iterdir())) == 1
    assert Path(first["backup_dir"]).is_dir()
    _save(source, _capture("Different9"))
    import_copilot_legacy_capture(root, source, known_catalog_sessions=357, apply=True)
    assert CopilotHistoryStore(root).cached_conversations == 2


@pytest.mark.parametrize("change", ["shorter", "changed", "source-url-collision"])
def test_conflicting_or_truncated_capture_never_replaces_existing_rows(tmp_path: Path, change: str) -> None:
    root = tmp_path / "runtime"
    source = _save(tmp_path / "capture.json")
    import_copilot_legacy_capture(root, source, known_catalog_sessions=357, apply=True)
    capture = _capture()
    if change == "shorter":
        capture["messages"] = capture["messages"][:1]
    elif change == "changed":
        capture["messages"][0]["content"] = "A changed copied body."
    else:
        store = CopilotHistoryStore(root)
        rows = store.rows
        rows[0]["conversation_url"] = "https://copilot.com/chat/conversation/AbC123"
        write_parquet_rows_atomic(store.path, rows, COPILOT_HISTORY_SCHEMA)
    _save(source, capture)
    before = _files(root)
    with pytest.raises(ValueError, match="conflicts"):
        import_copilot_legacy_capture(root, source, known_catalog_sessions=357)
    assert _files(root) == before


@pytest.mark.parametrize("messages", [
    [{"role": "assistant", "content": "Assistant-only copied text."}],
    [{"role": "user", "content": "User-only copied text."}],
    [{"role": "user", "content": "First question."}, {"role": "user", "content": "Another question."}],
])
def test_unpaired_roles_are_preserved_and_flagged_pending(messages: list[dict]) -> None:
    capture = _capture()
    capture["messages"] = messages
    rows, result = plan_copilot_legacy_import([], capture)
    assert [row["role"] for row in rows] == [message["role"] for message in messages]
    assert result["role_sequence_requires_review"] is True and result["sync_complete"] is False


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("conversation_id", "different"),
    ("conversation_url", "https://copilot.microsoft.com.evil.example/chats/AbC123"),
    ("conversation_url", "https://copilot.microsoft.com/chats/AbC123?tracking=1"),
    ("capture_method", "unverified"), ("captured_at", "2026-10-21T09:00:00"),
    ("captured_at", "2026-10-21T09:00:00+08:00"), ("raw_ax", ""),
    ("catalog_ordinal", True), ("catalog_ordinal", -1), ("source_dates", "Unknown"),
    ("messages", []), ("messages", [{"role": "system", "content": "Unsupported."}]),
    ("messages", [{"role": [], "content": "Unsupported."}]),
    ("messages", [{"role": "user", "content": "\x00Invalid."}]),
    ("messages", [{"role": "user", "content": " "}]),
    ("messages", [{"role": "user", "content": "Copied.", "message_id": "invented"}]),
])
def test_invalid_capture_is_rejected(field: str, value: object) -> None:
    capture = _capture()
    capture[field] = value
    with pytest.raises(ValueError):
        validate_copilot_legacy_capture(capture)


@pytest.mark.parametrize("url", [
    "http://copilot.microsoft.com/chats/AbC123", "https://user@copilot.microsoft.com/chats/AbC123",
    "https://copilot.microsoft.com:443/chats/AbC123", "https://copilot.microsoft.com/chats/AbC123/",
    "https://copilot.microsoft.com/chats/AbC_123", "https://copilot.microsoft.com/chats/%41bC123",
    "https://copilot.microsoft.com/chats/AbC123#fragment", "https://copilot.com/chat/conversation/AbC123",
])
def test_legacy_url_normalizer_accepts_only_observed_identity_shape(url: str) -> None:
    assert normalize_copilot_legacy_url(url) == ""
    assert normalize_copilot_legacy_url(_capture()["conversation_url"]) == _capture()["conversation_url"]


def test_busy_cache_and_bad_coverage_refuse_apply(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    store = _seed(root)
    source = _save(tmp_path / "capture.json")
    original = store.path.read_bytes()
    gate = CacheTaskLock(root / CACHE_TASK_LOCK_NAME)
    assert gate.acquire("synthetic-active-task")
    try:
        with pytest.raises(RuntimeError, match="cache task is running"):
            import_copilot_legacy_capture(root, source, known_catalog_sessions=357, apply=True)
    finally:
        gate.release()
    assert store.path.read_bytes() == original
    copilot_sync_state_path(root).write_bytes(b"Invalid coverage fixture.")
    with pytest.raises(RuntimeError, match="coverage state is unreadable"):
        import_copilot_legacy_capture(root, source, known_catalog_sessions=357, apply=True)
    assert store.path.read_bytes() == original
    assert not (root / "recovery").exists()


def test_atomic_history_failure_preserves_old_rows_and_keeps_partial_backup(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    store = _seed(root)
    source = _save(tmp_path / "capture.json")
    original = store.path.read_bytes()
    with patch("app.core.copilot_legacy_recovery.write_parquet_rows_atomic", side_effect=OSError("Synthetic write failed.")):
        with pytest.raises(OSError, match="Synthetic write failed"):
            import_copilot_legacy_capture(root, source, known_catalog_sessions=357, apply=True)
    assert store.path.read_bytes() == original
    assert read_copilot_sync_state(root)["status"] == "incomplete"
    backup = next((root / "recovery").iterdir())
    assert (backup / "history.parquet").read_bytes() == original
    assert json.loads((backup / "import.json").read_bytes())["applied"] is False
    assert read_parquet_rows(store.path) == store.rows


def test_previous_complete_status_is_downgraded_before_history_write(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    _seed(root)
    write_copilot_sync_state(root, {
        "status": "complete", "sync_complete": True, "discovery_complete": True,
        "expected_sessions": 17, "discovered_sessions": 17, "processed_sessions": 17,
        "completion_basis": "Previous source cursor ended.",
    })
    source = _save(tmp_path / "capture.json")
    original_writer = write_parquet_rows_atomic

    def checked_write(path, rows, schema):
        state = read_copilot_sync_state(root)
        assert state["status"] == "incomplete" and not state["sync_complete"]
        assert state["expected_sessions"] == 357
        original_writer(path, rows, schema)

    with patch("app.core.copilot_legacy_recovery.write_parquet_rows_atomic", side_effect=checked_write):
        import_copilot_legacy_capture(root, source, known_catalog_sessions=357, apply=True)
    assert read_copilot_sync_state(root)["processed_sessions"] == 17
    assert not build_copilot_initial_snapshot("test", root).discovery_complete


def test_failed_coverage_write_cannot_append_history(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    store = _seed(root)
    source = _save(tmp_path / "capture.json")
    before = (store.path.read_bytes(), copilot_sync_state_path(root).read_bytes())
    with patch("app.core.copilot_legacy_recovery.write_copilot_sync_state", side_effect=OSError("Synthetic coverage failure.")):
        with pytest.raises(OSError, match="Synthetic coverage failure"):
            import_copilot_legacy_capture(root, source, known_catalog_sessions=357, apply=True)
    assert (store.path.read_bytes(), copilot_sync_state_path(root).read_bytes()) == before


def test_coverage_readback_must_confirm_partial_state_before_history_write(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    store = _seed(root)
    source = _save(tmp_path / "capture.json")
    original = store.path.read_bytes()
    with patch("app.core.copilot_legacy_recovery.write_copilot_sync_state"):
        with pytest.raises(RuntimeError, match="coverage readback failed"):
            import_copilot_legacy_capture(root, source, known_catalog_sessions=357, apply=True)
    assert store.path.read_bytes() == original


def test_capture_changed_after_backup_cannot_reach_history_write(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    store = _seed(root)
    source = _save(tmp_path / "capture.json")
    before = (store.path.read_bytes(), copilot_sync_state_path(root).read_bytes())
    original_source = source.read_bytes()
    original_write = Path.write_text

    def change_capture(path, text, *args, **kwargs):
        result = original_write(path, text, *args, **kwargs)
        if path.name == "import.json":
            source.write_bytes(original_source + b"\n")
        return result

    with patch.object(Path, "write_text", new=change_capture):
        with pytest.raises(RuntimeError, match="changed during review"):
            import_copilot_legacy_capture(root, source, known_catalog_sessions=357, apply=True)
    assert (store.path.read_bytes(), copilot_sync_state_path(root).read_bytes()) == before
    backup = next((root / "recovery").iterdir())
    assert (backup / "capture.json").read_bytes() == original_source


def test_corrupt_or_duplicate_existing_history_is_never_rewritten(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    store = _seed(root, 2)
    rows = copy.deepcopy(store.rows)
    rows.append(dict(rows[0]))
    write_parquet_rows_atomic(store.path, rows, COPILOT_HISTORY_SCHEMA)
    source = _save(tmp_path / "capture.json")
    original = store.path.read_bytes()
    with pytest.raises(ValueError, match="malformed"):
        import_copilot_legacy_capture(root, source, known_catalog_sessions=357)
    assert store.path.read_bytes() == original
    store.path.write_bytes(b"Invalid history fixture.")
    with pytest.raises(RuntimeError, match="history is unreadable"):
        import_copilot_legacy_capture(root, source, known_catalog_sessions=357)
    assert store.path.read_bytes() == b"Invalid history fixture."
