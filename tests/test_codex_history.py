"""Isolated visible-message, identity, archive, and source-safety regressions.

Code version: v1.0.1-codex.0
"""

import json
from pathlib import Path

import pytest

from app.core.codex_history import (
    codex_history_path,
    default_codex_directory,
    import_codex_history,
)
from app.core import codex_history
from app.core.resource_persistence import read_parquet_rows


SESSION = "019c0000-0000-7000-8000-000000000001"
OTHER = "019c0000-0000-7000-8000-000000000002"
TIMESTAMP = "2026-10-13T01:00:00Z"


def _meta(session=SESSION, **overrides):
    return {"type": "session_meta", "payload": {"id": session, "source": "vscode", **overrides}}


def _message(role, text, identity="message-1", **overrides):
    payload = {
        "type": "message", "role": role,
        "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
        **overrides,
    }
    if identity:
        payload["id"] = identity
    return {"type": "response_item", "timestamp": TIMESTAMP, "payload": payload}


def _transcript(directory: Path, records, session=SESSION):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-2026-10-13T01-00-00-{session}.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


def _rows(store):
    return read_parquet_rows(codex_history_path(store)) or []


def test_visible_messages_exclude_tools_reasoning_and_event_duplicates(tmp_path):
    source = tmp_path / "source"
    assistant = _message("assistant", "Visible <answer>", "answer-1", phase="final_answer")
    transcript = _transcript(source / "sessions/2026/10/13", [
        _meta(),
        {"type": "turn_context", "payload": {"model": "codex-test-model", "cwd": "PRIVATE CWD"}},
        _message("developer", "PRIVATE INSTRUCTIONS"),
        _message("user", "Visible question"),
        {"type": "response_item", "payload": {"type": "reasoning", "content": "PRIVATE REASONING"}},
        {"type": "response_item", "payload": {"type": "function_call_output", "output": "PRIVATE TOOL RESULT"}},
        _message("assistant", "PRIVATE ANALYSIS", "analysis-1", channel="analysis"),
        _message("assistant", "PRIVATE REASONING CHANNEL", "analysis-2", channel="reasoning"),
        _message("assistant", "Visible update", "update-1", channel="commentary"),
        assistant,
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "Visible <answer>"}},
        {"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "AgentMessage", "id": "answer-1", "content": ["Visible <answer>"],
        }}},
        {"type": "response_item", "payload": {"type": "agent_message", "author": "child", "content": [
            {"type": "input_text", "text": "PRIVATE AGENT MESSAGE"},
        ]}},
    ])
    before = transcript.read_bytes()
    store = tmp_path / "cache"

    result = import_codex_history(store, source)
    rows = _rows(store)

    assert result["messages"] == 3
    assert [row["content_text"] for row in rows] == ["Visible question", "Visible update", "Visible <answer>"]
    assert {row["conversation_id"] for row in rows} == {SESSION}
    assert rows[-1]["message_key"] == f"{SESSION}:answer-1:assistant"
    assert rows[-1]["model_label"] == "codex-test-model"
    assert rows[-1]["source_file"] == str(transcript)
    assert rows[-1]["source_kind"] == "session"
    assert rows[-1]["first_seen_at"] == TIMESTAMP
    assert rows[-1]["content_html"] == ""
    assert rows[-1]["conversation_url"] == ""
    assert rows[-1]["author_label"] == "Codex"
    assert transcript.read_bytes() == before
    assert not (store / "llm/chatgpt").exists()


def test_generated_instruction_blocks_are_excluded_without_discarding_user_task(tmp_path):
    task = _message("user", "", content=[
        {"type": "input_text", "text": "PRIVATE GENERATED GUIDE"},
        {"type": "input_text", "text": "Explain the parser."},
        {"type": "input_text", "text": "PRIVATE ENVIRONMENT"},
        {"type": "input_image", "image_url": "PRIVATE IMAGE"},
    ], internal_chat_message_metadata_passthrough={
        "content_item_kinds": ["agents_md.instructions", "user.text", "environments.environment_context", "user.image"],
    })
    _transcript(tmp_path / "sessions", [
        _meta(), task,
        _message("user", "# AGENTS.md instructions for /test\nPRIVATE GUIDE", "guide"),
        _message("user", "<environment_context>PRIVATE ENVIRONMENT</environment_context>", "environment"),
        _message("user", "Please explain AGENTS.md and environment_context metadata.", "question-2"),
    ])

    result = import_codex_history(tmp_path / "cache", tmp_path / "sessions")

    assert result["messages"] == 2
    assert [row["content_text"] for row in _rows(tmp_path / "cache")] == [
        "Explain the parser.", "Please explain AGENTS.md and environment_context metadata.",
    ]


def test_archives_and_active_copies_merge_once_with_current_provenance(tmp_path):
    source = tmp_path / "source"
    records = [_meta(), _message("user", "Question"), _message("assistant", "Answer", "answer")]
    _transcript(source / "archived_sessions", records)
    active = _transcript(source / "sessions/2026/10/13", records)
    _transcript(source / "archived_sessions", [_meta(OTHER), _message("user", "Archive")], OTHER)
    store = tmp_path / "cache"

    first = import_codex_history(store, source / "sessions")
    before = codex_history_path(store).stat().st_mtime_ns
    second = import_codex_history(store, source)

    assert first["files_scanned"] == 3
    assert first["archived_files"] == 2
    assert first["sessions"] == first["imported_sessions"] == 2
    assert first["messages"] == first["imported_messages"] == 3
    assert first["added_or_changed"] == 3
    assert second["added_or_changed"] == 0
    assert second["unchanged_messages"] == 3
    assert second["unchanged_sessions"] == 2
    assert second["applied"] is False
    assert codex_history_path(store).stat().st_mtime_ns == before
    assert next(row for row in _rows(store) if row["conversation_id"] == SESSION)["source_file"] == str(active)


def test_child_metadata_cannot_be_reopened_by_inherited_parent_metadata(tmp_path):
    source = tmp_path / "sessions"
    _transcript(source, [
        _meta(source={"subagent": {"agent_path": "review"}}, parent_thread_id=OTHER),
        _meta(OTHER), _message("user", "PRIVATE CHILD CONTEXT"),
    ])
    _transcript(source, [_meta(OTHER, forked_from_id=SESSION), _meta(), _message("user", "Parent fork")], OTHER)

    result = import_codex_history(tmp_path / "cache", source)

    assert result["failed"] == 0
    assert result["sessions"] == 1
    assert _rows(tmp_path / "cache")[0]["conversation_id"] == OTHER
    assert _rows(tmp_path / "cache")[0]["content_text"] == "Parent fork"


def test_session_index_title_and_uuid_identity_survive_archive_move(tmp_path):
    source = tmp_path / "source"
    transcript = _transcript(source / "sessions/2026/10/13", [_meta(), _message("user", "Question")])
    (source / "session_index.jsonl").write_text(json.dumps({
        "id": SESSION, "thread_name": "Indexed title", "unrelated": "PRIVATE METADATA",
    }) + "\n")
    store = tmp_path / "cache"
    import_codex_history(store, source)
    archive = source / "archived_sessions"
    archive.mkdir()
    transcript.rename(archive / transcript.name)

    result = import_codex_history(store, source)

    assert result["messages"] == 1
    assert _rows(store)[0]["conversation_title"] == "Indexed title"
    assert _rows(store)[0]["source_kind"] == "archive"


def test_missing_ids_and_repeated_equal_messages_have_distinct_stable_keys(tmp_path):
    _transcript(tmp_path / "sessions", [
        _meta(), _message("user", "Repeat", None), _message("user", "Repeat", None),
        _message("assistant", "Answer", None),
    ])
    store = tmp_path / "cache"

    first = import_codex_history(store, tmp_path / "sessions")
    keys = [row["message_key"] for row in _rows(store)]
    second = import_codex_history(store, tmp_path / "sessions")

    assert first["messages"] == 3
    assert len(set(keys)) == 3
    assert second["added_or_changed"] == 0
    assert [row["message_key"] for row in _rows(store)] == keys


def test_partial_missing_and_malformed_transcripts_preserve_cached_messages(tmp_path):
    source = tmp_path / "sessions"
    transcript = _transcript(source, [_meta(), _message("user", "Question"), _message("assistant", "Complete answer", "answer")])
    store = tmp_path / "cache"
    import_codex_history(store, source)
    _transcript(source, [_meta(), _message("assistant", "Complete", "answer")])
    with transcript.open("a") as stream:
        stream.write('{"type":"response_item",')

    result = import_codex_history(store, source)

    assert result["failed"] == 1
    assert result["messages"] == 2
    assert [row["content_text"] for row in _rows(store)] == ["Question", "Complete answer"]
    transcript.unlink()
    assert import_codex_history(store, source)["messages"] == 2


def test_append_import_extends_instead_of_duplicating_or_deleting(tmp_path):
    source = tmp_path / "sessions"
    records = [_meta(), _message("user", "Question"), _message("assistant", "Answer", "answer")]
    transcript = _transcript(source, records)
    store = tmp_path / "cache"
    import_codex_history(store, source)
    with transcript.open("a") as stream:
        stream.write(json.dumps(_message("user", "Next question", "question-2")) + "\n")

    result = import_codex_history(store, source)

    assert result["added_or_changed"] == 1
    assert result["messages"] == 3
    assert [row["message_index"] for row in _rows(store)] == [0, 1, 2]
    assert [row["turn_index"] for row in _rows(store)] == [1, 1, 2]


def test_dry_run_and_stop_do_not_create_cache_or_change_sources(tmp_path):
    source = tmp_path / "sessions"
    transcript = _transcript(source, [_meta(), _message("user", "Question")])
    before = transcript.read_bytes()
    store = tmp_path / "cache"

    preview = import_codex_history(store, source, apply=False)
    stopped = import_codex_history(store, source, should_stop=lambda: True)

    assert preview["messages"] == 1
    assert preview["applied"] is False
    assert stopped["stopped"] is True
    assert not store.exists()
    assert transcript.read_bytes() == before


def test_unreadable_cache_is_never_replaced(tmp_path):
    source = tmp_path / "sessions"
    _transcript(source, [_meta(), _message("user", "Question")])
    path = codex_history_path(tmp_path / "cache")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"corrupt existing cache")

    with pytest.raises(RuntimeError, match="unreadable"):
        import_codex_history(tmp_path / "cache", source)

    assert path.read_bytes() == b"corrupt existing cache"


def test_symlink_files_directories_and_titles_are_not_followed(tmp_path):
    source = tmp_path / "source"
    allowed = _transcript(source / "sessions", [_meta(), _message("user", "Allowed")])
    outside = _transcript(tmp_path / "outside", [_meta(OTHER), _message("user", "Outside")], OTHER)
    (source / "sessions" / outside.name).symlink_to(outside)
    (source / "archived_sessions").symlink_to(outside.parent, target_is_directory=True)
    outside_title = tmp_path / "private-index.jsonl"
    outside_title.write_text(json.dumps({"id": SESSION, "thread_name": "PRIVATE TITLE"}))
    (source / "session_index.jsonl").symlink_to(outside_title)
    link = tmp_path / "link"
    link.symlink_to(source, target_is_directory=True)

    result = import_codex_history(tmp_path / "cache", source)

    assert result["messages"] == 1
    assert _rows(tmp_path / "cache")[0]["conversation_title"] == "Allowed"
    assert allowed.exists()
    with pytest.raises(ValueError, match="symbolic link"):
        import_codex_history(tmp_path / "cache", link)
    with pytest.raises(ValueError, match="symbolic link"):
        import_codex_history(tmp_path / "cache", link / "sessions")


def test_default_directory_respects_custom_codex_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "custom-home"))
    assert default_codex_directory() == tmp_path / "custom-home"


def test_expanded_duplicate_item_retains_earliest_source_timestamp(tmp_path):
    first = _message("assistant", "Partial", "streamed")
    final = _message("assistant", "Partial answer complete", "streamed")
    final["timestamp"] = "2026-10-13T01:01:00Z"
    _transcript(tmp_path / "sessions", [_meta(), _message("user", "Question"), first, final])

    result = import_codex_history(tmp_path / "cache", tmp_path / "sessions")

    assert result["messages"] == 2
    row = _rows(tmp_path / "cache")[-1]
    assert row["content_text"] == "Partial answer complete"
    assert row["first_seen_at"] == TIMESTAMP
    assert row["last_seen_at"] == final["timestamp"]


def test_mismatched_or_missing_own_metadata_cannot_attribute_messages(tmp_path):
    source = tmp_path / "sessions"
    _transcript(source, [_meta(OTHER), _message("user", "Wrong session")])
    _transcript(source, [_message("user", "Missing metadata")], OTHER)

    result = import_codex_history(tmp_path / "cache", source)

    assert result["failed"] == 2
    assert result["messages"] == 0
    assert not (tmp_path / "cache").exists()


def test_linked_history_filename_uses_explicit_metadata_identity(tmp_path):
    source = tmp_path / "sessions"
    _transcript(source, [_meta(), _message("user", "Original question")])
    linked = _transcript(source, [
        _meta(history_base={"thread_id": SESSION, "end_ordinal_exclusive": 10, "end_byte_offset": 100}),
        _message("user", "Original question"),
        _message("assistant", "Linked answer", "answer"),
    ])
    linked.rename(linked.with_name(f"{linked.stem}_{OTHER}.jsonl"))
    _transcript(source, [_meta(), _message("user", "Original question")])
    result = import_codex_history(tmp_path / "cache", source)

    assert result["failed"] == 0
    assert result["sessions"] == 1
    assert result["messages"] == 2
    assert {row["conversation_id"] for row in _rows(tmp_path / "cache")} == {SESSION}


def test_oversized_excluded_records_do_not_fail_but_visible_records_do(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_history, "_MAX_LINE_BYTES", 512)
    source = tmp_path / "sessions"
    records = [
        _meta(), _message("user", "Question"),
        {"timestamp": TIMESTAMP, "ordinal": 17, "type": "compacted", "payload": {
            "message": "PRIVATE CONTEXT " * 100,
        }},
        {"type": "response_item", "ordinal": 18, "timestamp": TIMESTAMP, "payload": {
            "type": "function_call_output", "output": "PRIVATE TOOL " * 100,
        }},
        _message("assistant", "Answer", "answer"),
    ]
    transcript = _transcript(source, records)
    store = tmp_path / "cache"

    result = import_codex_history(store, source)

    assert result["failed"] == 0
    assert result["messages"] == 2
    _transcript(source, [*records, _message("user", "Visible oversized text " * 100, "oversized")])
    failed = import_codex_history(store, source)
    assert failed["failed"] == 1
    assert failed["messages"] == 2
    assert transcript.exists()


@pytest.mark.parametrize("invalid_bound", [True, -1])
def test_linked_history_requires_nonnegative_integer_bounds(tmp_path, invalid_bound):
    source = tmp_path / "sessions"
    transcript = _transcript(source, [
        _meta(history_base={
            "thread_id": SESSION, "end_ordinal_exclusive": invalid_bound, "end_byte_offset": 100,
        }),
        _message("user", "Unverified linked message"),
    ])
    transcript.rename(transcript.with_name(f"{transcript.stem}_{OTHER}.jsonl"))

    result = import_codex_history(tmp_path / "cache", source)

    assert result["failed"] == 1
    assert result["messages"] == 0
