"""Regression coverage for isolated local Claude Code history imports.

Code version: v1.0.0-claude-code.0
"""

import json
from pathlib import Path

import pytest

from app.core.claude_code_history import (
    claude_code_history_path,
    import_claude_code_history,
)
from app.core.resource_persistence import read_parquet_rows


def _record(role: str, text: str, *, identity: str = "m1", session: str = "session-1") -> dict:
    return {
        "type": role,
        "sessionId": session,
        "uuid": identity,
        "timestamp": "2026-10-13T01:00:00Z",
        "message": {"role": role, "content": text},
    }


def _transcript(directory: Path, records: list[dict], session: str = "session-1") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session}.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
    return path


def _rows(store: Path) -> list[dict]:
    return read_parquet_rows(claude_code_history_path(store)) or []


def test_import_keeps_visible_text_and_stable_identity_without_tool_or_thinking_data(tmp_path):
    project = tmp_path / "source" / "projects" / "project"
    assistant = _record("assistant", "", identity="m2")
    assistant["message"].update({"model": "claude-example", "content": [
        {"type": "thinking", "thinking": "PRIVATE THINKING"},
        {"type": "tool_use", "input": {"token": "SECRET TOOL INPUT"}},
        {"type": "text", "text": "Visible answer"},
    ]})
    tool = _record("user", "", identity="m3")
    tool["message"]["content"] = [{"type": "tool_result", "content": "SECRET TOOL RESULT"}]
    meta = _record("user", "INTERNAL META", identity="m4")
    meta["isMeta"] = True
    transcript = _transcript(project, [_record("user", "Visible question"), assistant, tool, meta])
    _transcript(project / "session-1" / "subagents", [_record("user", "SUBAGENT")])
    source_bytes = transcript.read_bytes()
    store = tmp_path / "cache"

    result = import_claude_code_history(store, tmp_path / "source")
    rows = _rows(store)

    assert result["sessions"] == 1
    assert result["messages"] == 2
    assert [row["content_text"] for row in rows] == ["Visible question", "Visible answer"]
    assert rows[1]["message_key"] == "session-1:m2:assistant"
    assert rows[1]["model_label"] == "claude-example"
    assert rows[0]["platform"] == "claude_code"
    assert rows[0]["conversation_url"] == ""
    assert rows[0]["first_seen_at"] == "2026-10-13T01:00:00Z"
    assert transcript.read_bytes() == source_bytes
    assert not (store / "llm" / "claude").exists()


def test_desktop_directory_imports_only_metadata_linked_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    desktop = tmp_path / "desktop"
    metadata = desktop / "claude-code-sessions" / "account" / "organization"
    metadata.mkdir(parents=True)
    (metadata / "local_desktop.json").write_text(json.dumps({
        "cliSessionId": "session-1", "title": "Desktop title", "model": "desktop-model",
        "promptAppendSnapshot": {"append": "PRIVATE SYSTEM PROMPT"},
    }))
    project = Path.home() / ".claude" / "projects" / "project"
    _transcript(project, [_record("user", "Question")])
    _transcript(project, [_record("user", "CLI only", session="session-2")], "session-2")

    result = import_claude_code_history(tmp_path / "cache", desktop)

    assert result["files_total"] == 1
    assert result["failed"] == 0
    assert _rows(tmp_path / "cache")[0]["conversation_title"] == "Desktop title"
    assert _rows(tmp_path / "cache")[0]["model_label"] == "desktop-model"


def test_repeated_import_is_idempotent_and_does_not_rewrite_store(tmp_path):
    project = tmp_path / "project"
    _transcript(project, [_record("user", "Question"), _record("assistant", "Answer", identity="m2")])
    store = tmp_path / "cache"
    import_claude_code_history(store, project)
    before = claude_code_history_path(store).stat().st_mtime_ns

    result = import_claude_code_history(store, project)

    assert result["added_or_changed"] == 0
    assert result["unchanged_messages"] == 2
    assert result["unchanged_sessions"] == 1
    assert result["applied"] is False
    assert claude_code_history_path(store).stat().st_mtime_ns == before


def test_partial_and_missing_transcripts_do_not_delete_or_truncate_cached_messages(tmp_path):
    project = tmp_path / "project"
    transcript = _transcript(project, [
        _record("user", "Question"), _record("assistant", "Complete answer", identity="m2"),
    ])
    store = tmp_path / "cache"
    import_claude_code_history(store, project)
    _transcript(project, [_record("assistant", "Complete", identity="m2")])
    with transcript.open("a") as stream:
        stream.write('{"type": "assistant",')

    result = import_claude_code_history(store, project)

    assert result["failed"] == 1
    assert result["messages"] == 2
    assert [row["content_text"] for row in _rows(store)] == ["Question", "Complete answer"]


def test_new_records_merge_with_old_sessions_and_dry_run_stays_read_only(tmp_path):
    project = tmp_path / "source" / "projects" / "project"
    _transcript(project, [_record("user", "First")])
    store = tmp_path / "cache"

    preview = import_claude_code_history(store, project, apply=False)

    assert preview["messages"] == 1
    assert not store.exists()
    import_claude_code_history(store, project)
    _transcript(project, [_record("user", "Second", session="session-2")], "session-2")
    result = import_claude_code_history(store, project)
    assert result["sessions"] == 2
    assert result["added_or_changed"] == 1


def test_symlink_transcripts_and_directories_are_not_followed(tmp_path):
    project = tmp_path / "source" / "projects" / "project"
    _transcript(project, [_record("user", "Allowed")])
    outside = _transcript(tmp_path / "outside", [_record("user", "Outside", session="session-2")], "session-2")
    (project / "session-2.jsonl").symlink_to(outside)
    (project.parent / "linked-project").symlink_to(outside.parent, target_is_directory=True)
    source_link = tmp_path / "source-link"
    source_link.symlink_to(project)

    result = import_claude_code_history(tmp_path / "cache", project.parent)

    assert result["messages"] == 1
    with pytest.raises(ValueError, match="symbolic link"):
        import_claude_code_history(tmp_path / "cache", source_link)
    ancestor_link = tmp_path / "ancestor-link"
    ancestor_link.symlink_to(project.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        import_claude_code_history(tmp_path / "cache", ancestor_link / project.name)


def test_unreadable_cache_is_never_replaced_and_stop_preserves_existing_rows(tmp_path):
    project = tmp_path / "project"
    _transcript(project, [_record("user", "Question")])
    store = tmp_path / "cache"
    result = import_claude_code_history(store, project, should_stop=lambda: True)
    assert result["stopped"] is True
    assert not store.exists()
    path = claude_code_history_path(store)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"unreadable existing cache")

    with pytest.raises(RuntimeError, match="unreadable"):
        import_claude_code_history(store, project)

    assert path.read_bytes() == b"unreadable existing cache"


def test_mismatched_session_records_and_non_message_records_are_ignored(tmp_path):
    project = tmp_path / "project"
    _transcript(project, [
        {"type": "custom-title", "customTitle": "Named Code session", "sessionId": "session-1"},
        _record("user", "Question"),
        _record("assistant", "Other session", session="session-2"),
        {"type": "system", "content": "SYSTEM SECRET"},
    ])

    result = import_claude_code_history(tmp_path / "cache", project)

    assert result["failed"] == 1
    assert result["messages"] == 1
    assert _rows(tmp_path / "cache")[0]["conversation_title"] == "Named Code session"
