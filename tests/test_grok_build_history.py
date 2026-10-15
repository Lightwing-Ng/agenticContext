"""Verify native Grok Build imports with isolated ACP session fixtures.

Code version: v1.0.1-grok-build.0
"""

import json
from pathlib import Path

import pytest

from app.core import grok_build_history
from app.core.grok_build_history import (
    grok_build_history_path,
    import_grok_build_history,
)
from app.core.resource_persistence import read_parquet_rows


def _event(
    kind: str,
    text: str,
    event_id: str,
    *,
    session: str = "session-1",
    prompt: str = "prompt-1",
    stream: int = 1_792_000_000_000,
    chunk: int = 0,
    index: int = 0,
    model: str = "grok-native",
) -> dict:
    return {
        "timestamp": 1_792_000_000 + index,
        "method": "session/update",
        "params": {
            "sessionId": session,
            "update": {
                "sessionUpdate": kind,
                "content": {"type": "text", "text": text},
                "_meta": {"promptIndex": index, "modelId": model},
            },
            "_meta": {
                "eventId": event_id,
                "agentTimestampMs": 1_792_000_000_000 + index * 1_000,
                "promptId": prompt,
                "streamStartMs": stream,
                "chunkId": chunk,
            },
        },
    }


def _write_updates(directory: Path, events: list[dict]) -> Path:
    path = directory / "updates.jsonl"
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    return path


def _session(
    home: Path,
    events: list[dict],
    *,
    session: str = "session-1",
    project: str = "workspace",
    summary_extra: dict | None = None,
) -> Path:
    directory = home / "sessions" / project / session
    directory.mkdir(parents=True)
    summary = {
        "info": {"id": session, "cwd": "/workspace"},
        "generated_title": "Native Grok session",
        "session_summary": "Fallback title",
        "created_at": "2026-10-15T00:00:00Z",
        "updated_at": "2026-10-15T00:01:00Z",
        "current_model_id": "grok-native",
        "chat_format_version": 1,
        **(summary_extra or {}),
    }
    (directory / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    _write_updates(directory, events)
    return directory


def _rows(store: Path) -> list[dict]:
    return read_parquet_rows(grok_build_history_path(store)) or []


def test_import_uses_visible_authoritative_updates_and_preserves_source_bytes(tmp_path):
    home = tmp_path / "native"
    hidden = _event("user_message_chunk", "HIDDEN CONTEXT", "hidden")
    hidden["params"]["update"]["_meta"]["hideFromScrollback"] = True
    session = _session(
        home,
        [
            _event("user_message_chunk", "Question", "user-1"),
            _event("agent_thought_chunk", "PRIVATE THINKING", "thought-1"),
            _event("tool_call_update", "PRIVATE TOOL OUTPUT", "tool-1"),
            hidden,
            _event("agent_message_chunk", "Answer", "answer-1"),
        ],
    )
    (session / "chat_history.jsonl").write_text(
        '{"type":"user","content":"RAW CONTEXT INJECTION"}\n'
    )
    before = {path.name: path.read_bytes() for path in session.iterdir()}
    store = tmp_path / "cache"

    result = import_grok_build_history(store, home)

    assert result["sessions"] == 1
    assert result["messages"] == 2
    assert result["failed"] == 0
    assert [row["content_text"] for row in _rows(store)] == ["Question", "Answer"]
    assert _rows(store)[0]["source_file"] == str(session / "updates.jsonl")
    assert _rows(store)[0]["source_line"] == 1
    assert _rows(store)[1]["author_label"] == "Grok Build"
    assert _rows(store)[1]["platform"] == "grok_build"
    assert _rows(store)[1]["conversation_url"] == ""
    assert {path.name: path.read_bytes() for path in session.iterdir()} == before
    assert not (store / "llm" / "grok").exists()


def test_stream_chunks_merge_across_tool_and_thought_events_and_deduplicate(tmp_path):
    first = _event("agent_message_chunk", "Hello ", "part-1", chunk=2)
    directory = _session(
        tmp_path / "native",
        [
            _event("user_message_chunk", "Question", "user-1"),
            first,
            _event("tool_call", "PRIVATE CALL", "tool-1"),
            _event("agent_thought_chunk", "PRIVATE THINKING", "thought-1"),
            _event("agent_message_chunk", "world", "part-2", chunk=4),
            first,
            _event("agent_message_chunk", "world", "part-2-repeat", chunk=4),
            _event(
                "agent_message_chunk",
                "Another response",
                "part-3",
                stream=1_792_000_001_000,
            ),
        ],
    )

    import_grok_build_history(tmp_path / "cache", directory)

    rows = _rows(tmp_path / "cache")
    assert [row["content_text"] for row in rows] == [
        "Question",
        "Hello world",
        "Another response",
    ]
    assert [row["turn_index"] for row in rows] == [1, 1, 1]


def test_incomplete_stream_extends_the_same_message_and_repeat_import_is_idempotent(
    tmp_path,
):
    home = tmp_path / "native"
    events = [
        _event("user_message_chunk", "Question", "user-1"),
        _event("agent_message_chunk", "Hello ", "part-1", chunk=0),
    ]
    directory = _session(home, events)
    with (directory / "updates.jsonl").open("a") as stream:
        stream.write('{"timestamp":')
    store = tmp_path / "cache"

    first = import_grok_build_history(store, home)
    message_key = _rows(store)[1]["message_key"]
    events.append(_event("agent_message_chunk", "world", "part-2", chunk=1, index=1))
    _write_updates(directory, events)
    second = import_grok_build_history(store, home)
    rows = _rows(store)
    original_bytes = grok_build_history_path(store).read_bytes()
    original_mtime = grok_build_history_path(store).stat().st_mtime_ns
    repeated = import_grok_build_history(store, home)

    assert first["failed"] == 1
    assert second["failed"] == 0
    assert second["messages"] == 2
    assert second["added_or_changed"] == 1
    assert rows[1]["message_key"] == message_key
    assert rows[1]["content_text"] == "Hello world"
    assert rows[1]["first_seen_at"] < rows[1]["last_seen_at"]
    assert repeated["added_or_changed"] == 0
    assert repeated["unchanged_messages"] == 2
    assert repeated["applied"] is False
    assert grok_build_history_path(store).read_bytes() == original_bytes
    assert grok_build_history_path(store).stat().st_mtime_ns == original_mtime


def test_multiple_turns_keep_separate_streams_and_their_source_model(tmp_path):
    directory = _session(
        tmp_path / "native",
        [
            _event(
                "user_message_chunk", "First question", "user-1", model="model-first"
            ),
            _event("agent_message_chunk", "First answer", "answer-1"),
            _event(
                "user_message_chunk",
                "Second question",
                "user-2",
                index=1,
                model="model-second",
            ),
            _event(
                "agent_message_chunk",
                "Second answer",
                "answer-2",
                prompt="prompt-2",
                index=1,
            ),
        ],
    )

    import_grok_build_history(tmp_path / "cache", directory)

    rows = _rows(tmp_path / "cache")
    assert [row["turn_index"] for row in rows] == [1, 1, 2, 2]
    assert [row["model_label"] for row in rows] == [
        "model-first",
        "model-first",
        "model-second",
        "model-second",
    ]
    assert len({row["message_key"] for row in rows}) == 4


def test_child_sessions_are_excluded_by_summary_and_parent_metadata(tmp_path):
    home = tmp_path / "native"
    parent = _session(home, [_event("user_message_chunk", "Main", "user-1")])
    _session(
        home,
        [_event("user_message_chunk", "CHILD ONE", "child-1", session="child-1")],
        session="child-1",
        summary_extra={"session_kind": "subagent"},
    )
    _session(
        home,
        [_event("user_message_chunk", "CHILD TWO", "child-2", session="child-2")],
        session="child-2",
    )
    metadata = parent / "subagents" / "worker"
    metadata.mkdir(parents=True)
    (metadata / "meta.json").write_text(
        json.dumps({"child_session_id": "child-2", "prompt": "PRIVATE SUBAGENT PROMPT"})
    )

    result = import_grok_build_history(tmp_path / "cache", home)

    assert result["skipped_subagents"] == 2
    assert result["messages"] == 1
    assert result["sessions"] == 1


@pytest.mark.parametrize("scope", ["home", "sessions", "project", "session"])
def test_explicit_source_scopes_are_supported_without_reading_other_projects(
    tmp_path, scope
):
    home = tmp_path / "native"
    directory = _session(home, [_event("user_message_chunk", "First", "user-1")])
    _session(
        home,
        [_event("user_message_chunk", "Second", "user-2", session="session-2")],
        session="session-2",
        project="other",
    )
    source = {
        "home": home,
        "sessions": home / "sessions",
        "project": directory.parent,
        "session": directory,
    }[scope]

    result = import_grok_build_history(tmp_path / "cache", source, apply=False)

    assert result["sessions"] == (2 if scope in {"home", "sessions"} else 1)
    assert not (tmp_path / "cache").exists()


def test_corrupt_metadata_and_truncated_logs_preserve_existing_cached_messages(
    tmp_path,
):
    home = tmp_path / "native"
    directory = _session(
        home,
        [
            _event("user_message_chunk", "Question", "user-1"),
            _event("agent_message_chunk", "Complete answer", "answer-1"),
        ],
    )
    store = tmp_path / "cache"
    import_grok_build_history(store, home)
    original = grok_build_history_path(store).read_bytes()
    _write_updates(directory, [_event("agent_message_chunk", "Complete", "answer-1")])
    truncated = import_grok_build_history(store, home)
    (directory / "summary.json").write_text('{"info":')
    corrupted = import_grok_build_history(store, home)

    assert truncated["messages"] == 2
    assert corrupted["failed"] == 1
    assert corrupted["messages"] == 2
    assert grok_build_history_path(store).read_bytes() == original


def test_symlink_roots_ancestors_and_session_files_are_not_followed(tmp_path):
    home = tmp_path / "native"
    directory = _session(home, [_event("user_message_chunk", "Allowed", "user-1")])
    other = _session(
        tmp_path / "outside",
        [_event("user_message_chunk", "OUTSIDE", "outside", session="session-2")],
        session="session-2",
    )
    (directory.parent / "linked-session").symlink_to(other, target_is_directory=True)
    alias = tmp_path / "alias"
    alias.symlink_to(home, target_is_directory=True)

    assert import_grok_build_history(tmp_path / "cache", home)["messages"] == 1
    with pytest.raises(ValueError, match="symbolic links"):
        import_grok_build_history(tmp_path / "cache", alias / "sessions")
    source = directory / "updates.jsonl"
    source.unlink()
    source.symlink_to(other / "updates.jsonl")
    result = import_grok_build_history(tmp_path / "cache", home)
    assert result["failed"] == 1
    assert [row["content_text"] for row in _rows(tmp_path / "cache")] == ["Allowed"]


def test_missing_identity_mismatched_sessions_and_unknown_format_fail_closed(tmp_path):
    invalid = _event("user_message_chunk", "NO IDENTITY", "")
    directory = _session(
        tmp_path / "native",
        [
            invalid,
            _event("user_message_chunk", "OTHER SESSION", "foreign", session="foreign"),
            _event("user_message_chunk", "Allowed", "user-1"),
        ],
    )
    store = tmp_path / "cache"

    result = import_grok_build_history(store, directory)
    summary = json.loads((directory / "summary.json").read_text())
    summary["chat_format_version"] = 99
    (directory / "summary.json").write_text(json.dumps(summary))
    unknown = import_grok_build_history(store, directory)

    assert result["failed"] == 1
    assert result["messages"] == 1
    assert unknown["failed"] == 1
    assert unknown["messages"] == 1


def test_oversized_record_does_not_hide_following_messages(tmp_path, monkeypatch):
    directory = _session(
        tmp_path / "native",
        [
            _event("tool_call_update", "x" * 2_000, "tool"),
            _event("user_message_chunk", "Allowed", "user-1"),
        ],
    )
    monkeypatch.setattr(grok_build_history, "_MAX_LINE_BYTES", 1_000)

    result = import_grok_build_history(tmp_path / "cache", directory)

    assert result["failed"] == 1
    assert result["messages"] == 1


def test_stop_after_progress_keeps_completed_import_and_unreadable_cache_is_not_replaced(
    tmp_path,
):
    home = tmp_path / "native"
    _session(home, [_event("user_message_chunk", "First", "user-1")])
    _session(
        home,
        [_event("user_message_chunk", "Second", "user-2", session="session-2")],
        session="session-2",
    )
    progress = []
    store = tmp_path / "cache"

    result = import_grok_build_history(
        store, home, should_stop=lambda: bool(progress), on_progress=progress.append
    )

    assert result["stopped"] is True
    assert result["files_scanned"] == 1
    assert result["messages"] == 1
    assert progress[0]["files_total"] == 2
    path = grok_build_history_path(store)
    path.write_bytes(b"UNREADABLE CACHE")
    with pytest.raises(RuntimeError, match="unreadable"):
        import_grok_build_history(store, home)
    assert path.read_bytes() == b"UNREADABLE CACHE"


@pytest.mark.parametrize("new_tail", ["", " and a much longer newly appended tail"])
def test_malformed_middle_chunk_never_replaces_cached_content_but_new_messages_are_kept(
    tmp_path, new_tail
):
    chunks = [
        _event("agent_message_chunk", "alpha ", "part-0", chunk=0),
        _event("agent_message_chunk", "beta ", "part-1", chunk=1),
        _event("agent_message_chunk", "gamma", "part-2", chunk=2),
    ]
    directory = _session(tmp_path / "native", chunks)
    store = tmp_path / "cache"
    import_grok_build_history(store, directory)
    original = _rows(store)[0]
    lines = [json.dumps(chunks[0]), '{"malformed":', json.dumps(chunks[2])]
    if new_tail:
        lines.append(
            json.dumps(_event("agent_message_chunk", new_tail, "part-3", chunk=3))
        )
    lines.append(
        json.dumps(
            _event("user_message_chunk", "New valid question", "new-user", index=1)
        )
    )
    (directory / "updates.jsonl").write_text("\n".join(lines) + "\n")

    result = import_grok_build_history(store, directory)

    assert result["failed"] == 1
    assert result["added_or_changed"] == 1
    assert result["messages"] == 2
    assert _rows(store)[0] == original
    assert _rows(store)[1]["content_text"] == "New valid question"


def test_stop_before_late_middle_chunk_preserves_previously_complete_stream(
    tmp_path, monkeypatch
):
    question = _event("user_message_chunk", "Question", "user-1")
    chunks = [
        _event("agent_message_chunk", "alpha ", "part-0", chunk=0),
        _event("agent_message_chunk", "beta ", "part-1", chunk=1),
        _event("agent_message_chunk", "gamma", "part-2", chunk=2),
    ]
    directory = _session(tmp_path / "native", [question, *chunks])
    store = tmp_path / "cache"
    import_grok_build_history(store, directory)
    original_bytes = grok_build_history_path(store).read_bytes()
    _write_updates(directory, [question, chunks[0], chunks[2], chunks[1]])
    original_records = grok_build_history._records
    stopped = False

    def stop_after_third_record(path, should_stop):
        nonlocal stopped
        for line_number, record in original_records(path, should_stop):
            yield line_number, record
            if line_number == 3:
                stopped = True

    monkeypatch.setattr(grok_build_history, "_records", stop_after_third_record)

    result = import_grok_build_history(store, directory, should_stop=lambda: stopped)

    assert result["stopped"] is True
    assert result["added_or_changed"] == 0
    assert grok_build_history_path(store).read_bytes() == original_bytes


def test_malformed_later_record_allows_verified_extension_of_existing_message(tmp_path):
    first = _event("agent_message_chunk", "alpha", "part-0", chunk=0)
    directory = _session(tmp_path / "native", [first])
    store = tmp_path / "cache"
    import_grok_build_history(store, directory)
    _write_updates(
        directory, [first, _event("agent_message_chunk", " beta", "part-1", chunk=1)]
    )
    with (directory / "updates.jsonl").open("a") as stream:
        stream.write('{"incomplete":')

    result = import_grok_build_history(store, directory)

    assert result["failed"] == 1
    assert result["added_or_changed"] == 1
    assert result["messages"] == 1
    assert _rows(store)[0]["content_text"] == "alpha beta"
