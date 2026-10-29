"""OpenClaw archive completeness, provenance, and source-safety regressions.

Code version: v1.0.2-codex.0
"""

import hashlib
import json
import sqlite3

import pyarrow as pa
import pytest

from app.core.openclaw_history import (
    OPENCLAW_HISTORY_SCHEMA,
    OPENCLAW_SOURCE_LABELS,
    import_openclaw_history,
    openclaw_history_path,
)
from app.core.resource_persistence import read_parquet_rows, write_parquet_rows_atomic


SESSION = "040f2bd6-cabc-49d3-88aa-efd997e1da64"
OTHER = "095ab562-67e6-4f59-bb48-131d2c775318"


def _time(second):
    return f"2026-10-13T01:00:{second:02d}Z"


def _header(session=SESSION):
    return {"type": "session", "version": 4, "id": session, "timestamp": _time(0)}


def _model(provider, model, second=1):
    return {
        "type": "model_change", "id": f"model-{second}",
        "timestamp": _time(second), "provider": provider, "modelId": model,
    }


def _message(role, text, identity, second=2, **metadata):
    event = {
        "type": "message", "timestamp": _time(second),
        "message": {"role": role, "content": [{"type": "text", "text": text}], **metadata},
    }
    if identity is not None:
        event["id"] = identity
    return event


def _jsonl(records):
    return "".join(json.dumps(record) + "\n" for record in records).encode()


def _zstd(raw):
    output = pa.BufferOutputStream()
    with pa.CompressedOutputStream(output, "zstd") as stream:
        stream.write(raw)
    return output.getvalue().to_pybytes()


def _archive(tmp_path):
    archive = tmp_path / "archive"
    (archive / "state/agents/main/sessions").mkdir(parents=True)
    return archive


def _transcript(archive, records, name="history.jsonl", folder="sessions"):
    path = archive / "state/agents/main" / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = _jsonl(records)
    path.write_bytes(_zstd(raw) if path.suffix == ".zst" else raw)
    return path


def _database(archive, records=(), archives=(), name="openclaw-agent.sqlite"):
    path = archive / "state/agents/main/agent" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE transcript_events (session_id TEXT, seq INTEGER, "
            "event_json TEXT, event_zstd BLOB, event_utf8_bytes INTEGER, navigation_json TEXT)"
        )
        for index, (session, event, compressed) in enumerate(records):
            raw = json.dumps(event).encode()
            connection.execute("INSERT INTO transcript_events VALUES (?, ?, ?, ?, ?, ?)", (
                session, index, None if compressed else raw.decode(),
                _zstd(raw) if compressed else None, len(raw) if compressed else None,
                "{}" if compressed else None,
            ))
        connection.execute(
            "CREATE TABLE session_transcript_archives (session_id TEXT, generation TEXT, "
            "encoding TEXT, archive_blob BLOB, archive_sha256 TEXT, archive_name TEXT)"
        )
        for session, encoding, events in archives:
            raw = _jsonl(events)
            packed = _zstd(raw) if encoding == "zstd" else raw
            connection.execute("INSERT INTO session_transcript_archives VALUES (?, ?, ?, ?, ?, ?)", (
                session, "generation", encoding, packed, hashlib.sha256(packed).hexdigest(),
                f"{session}.jsonl.deleted.2026-10-13T01-00-00Z.zst",
            ))
    return path


def _rows(store, source):
    return read_parquet_rows(openclaw_history_path(store, source)) or []


def _fingerprint(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file()
    }


def test_compressed_sqlite_legacy_and_deleted_copies_merge_with_provenance(tmp_path):
    archive = _archive(tmp_path)
    events = [_header(), _model("openai", "gpt-5.5"),
              _message("user", "Question", "question", 2),
              _message("assistant", "Answer", "answer", 3, provider="openai", model="gpt-5.5")]
    _database(archive, [(SESSION, event, index % 2 == 1) for index, event in enumerate(events)],
              [(SESSION, "zstd", events)])
    _transcript(archive, events, f"{SESSION}.jsonl.reset.2026-10-13T01-00-00Z")
    _transcript(archive, events, f"archive-tier.{SESSION}.jsonl.imported-1788500313109",
                "session-sqlite-import-archive")
    _transcript(archive, events, f"{SESSION}.jsonl.deleted.2026-10-13T01-00-00Z.zst")
    before = _fingerprint(archive)
    store = tmp_path / "store"

    result = import_openclaw_history(store, archive)
    rows = _rows(store, "chatgpt_openclaw")

    assert result["failed"] == 0
    assert result["messages"] == 2
    assert [row["content_text"] for row in rows] == ["Question", "Answer"]
    assert {row["original_session_id"] for row in rows} == {SESSION}
    assert all(len(json.loads(row["provenance_json"])) == 5 for row in rows)
    assert _fingerprint(archive) == before
    assert not (store / "llm/chatgpt").exists()


def test_model_switches_keep_user_context_in_the_replying_provider(tmp_path):
    archive = _archive(tmp_path)
    _transcript(archive, [
        _header(), _model("openai", "gpt-5.5"),
        _message("user", "First turn", "question-1", 2),
        _message("assistant", "OpenAI answer", "answer-1", 3, provider="openai", model="gpt-5.5"),
        _message("user", "Switch provider", "question-2", 4),
        _model("google", "gemini-3.1-pro-preview", 5),
        _message("assistant", "Gemini answer", "answer-2", 6,
                 provider="google", model="gemini-3.1-pro-preview"),
    ])
    store = tmp_path / "store"

    result = import_openclaw_history(store, archive)

    assert result["failed"] == 0
    assert [row["content_text"] for row in _rows(store, "chatgpt_openclaw")] == ["First turn", "OpenAI answer"]
    assert [row["content_text"] for row in _rows(store, "gemini_openclaw")] == ["Switch provider", "Gemini answer"]
    assert {row["provider"] for row in _rows(store, "gemini_openclaw")} == {"google"}


def test_infrastructure_and_unsupported_provider_are_not_relabelled_as_current_model(tmp_path):
    archive = _archive(tmp_path)
    _transcript(archive, [
        _header(), _model("google", "gemini-3.1-pro-preview"),
        _message("assistant", "Mirrored delivery", "mirror", 2,
                 provider="openclaw", model="delivery-mirror"),
        _message("assistant", "Gateway notice", "notice", 3,
                 provider="openclaw", model="gateway-injected"),
        _message("assistant", "Doubao answer", "doubao", 4,
                 provider="volcengine", model="doubao-seed-2-0-lite-260215"),
    ])
    store = tmp_path / "store"

    import_openclaw_history(store, archive)

    assert [row["content_text"] for row in _rows(store, "unknown_openclaw")] == ["Mirrored delivery", "Gateway notice"]
    assert _rows(store, "doubao_openclaw")[0]["provider"] == "volcengine"
    assert _rows(store, "other_openclaw") == []
    assert _rows(store, "gemini_openclaw") == []


def test_conflicting_same_event_id_preserves_both_versions(tmp_path):
    archive = _archive(tmp_path)
    _transcript(archive, [_header(), _message("assistant", "Original answer", "shared", 2,
                                             provider="openai", model="gpt-5.5")], "old.jsonl")
    _transcript(archive, [_header(), _message("assistant", "Revised answer", "shared", 2,
                                             provider="openai", model="gpt-5.5")], "new.jsonl")
    store = tmp_path / "store"

    import_openclaw_history(store, archive)
    rows = _rows(store, "chatgpt_openclaw")

    assert len(rows) == 2
    assert {row["content_text"] for row in rows} == {"Original answer", "Revised answer"}
    assert {row["original_event_id"] for row in rows} == {"shared"}
    assert len({row["message_key"] for row in rows}) == 2


def test_repeat_import_preserves_identical_rows_and_file_bytes(tmp_path):
    archive = _archive(tmp_path)
    _transcript(archive, [_header(), _model("google", "gemini-3.1-pro-preview"),
                          _message("user", "Question", "question", 2),
                          _message("assistant", "Answer", "answer", 3)])
    store = tmp_path / "store"

    first = import_openclaw_history(store, archive)
    path = openclaw_history_path(store, "gemini_openclaw")
    original, modified = path.read_bytes(), path.stat().st_mtime_ns
    second = import_openclaw_history(store, archive)

    assert first["added_or_changed"] == 2
    assert second["added_or_changed"] == 0
    assert second["applied"] is False
    assert second["unchanged_messages"] == 2
    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == modified


def test_embedded_codex_is_separate_and_excludes_reasoning_and_tools(tmp_path):
    archive = _archive(tmp_path)
    path = archive / "state/agents/main/agent/codex-home/sessions/2026/10/13/rollout-example.jsonl"
    path.parent.mkdir(parents=True)
    events = [
        {"type": "session_meta", "timestamp": _time(0), "payload": {
            "id": OTHER, "originator": "openclaw", "model_provider": "openai"}},
        {"type": "turn_context", "timestamp": _time(1), "payload": {"model": "gpt-5.5"}},
    ]
    for second, role, text, extra in [
        (2, "user", "Question", {}), (3, "assistant", "Private analysis", {"channel": "analysis"}),
        (4, "assistant", "Visible answer", {"phase": "final_answer"}),
        (5, "developer", "Private instructions", {}),
    ]:
        events.append({"type": "response_item", "timestamp": _time(second), "payload": {
            "type": "message", "id": f"item-{second}", "role": role,
            "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}], **extra,
        }})
    events.append({"type": "response_item", "timestamp": _time(6),
                   "payload": {"type": "function_call_output", "output": "Private tool output"}})
    path.write_bytes(_jsonl(events))
    store = tmp_path / "store"

    result = import_openclaw_history(store, archive)
    rows = _rows(store, "chatgpt_openclaw")

    assert result["failed"] == 0
    assert [row["content_text"] for row in rows] == ["Question", "Visible answer"]
    assert {row["harness"] for row in rows} == {"codex"}
    assert {row["original_session_id"] for row in rows} == {OTHER}


def test_malformed_file_does_not_remove_previously_cached_or_other_valid_messages(tmp_path):
    archive = _archive(tmp_path)
    path = _transcript(archive, [_header(), _message("assistant", "Preserved answer", "answer", 3,
                                                   provider="openai", model="gpt-5.5")])
    store = tmp_path / "store"
    import_openclaw_history(store, archive)
    path.write_bytes(path.read_bytes() + b'{"type":"message",')
    _transcript(archive, [_header(OTHER), _message("assistant", "Independent answer", "other", 4,
                                                    provider="google", model="gemini-3.1-pro-preview")], "other.jsonl")
    original = _fingerprint(archive)

    result = import_openclaw_history(store, archive)

    assert result["failed"] == 1
    assert _rows(store, "chatgpt_openclaw")[0]["content_text"] == "Preserved answer"
    assert _rows(store, "gemini_openclaw")[0]["content_text"] == "Independent answer"
    assert _fingerprint(archive) == original


def test_json_object_metadata_maps_and_trajectory_context_are_not_parse_failures(tmp_path):
    archive = _archive(tmp_path)
    _transcript(archive, [_header(), _message("assistant", "Visible", "answer", 2,
                                             provider="openai", model="gpt-5.5")])
    mapping = archive / "state/agents/main/sessions" / f"{SESSION}.jsonl.codex-app-server.json.migrated"
    mapping.write_text(json.dumps({"schemaVersion": 1, "threadId": OTHER, "sessionFile": "history.jsonl"}, indent=2))
    _transcript(archive, [
        {"type": "session.started", "schemaVersion": 1, "sessionId": SESSION,
         "provider": "openai", "modelId": "gpt-5.5", "data": {}},
        {"type": "context.compiled", "sessionId": SESSION,
         "data": {"messages": [{"role": "assistant", "content": "Replayed context"}]}},
    ], f"archive-tier.{SESSION}.trajectory.jsonl.imported-1788500313109", "session-sqlite-import-archive")

    result = import_openclaw_history(tmp_path / "store", archive)

    assert result["failed"] == 0
    assert result["messages"] == 1


def test_identity_encoded_database_archives_are_read_without_zstd(tmp_path):
    archive = _archive(tmp_path)
    _database(archive, archives=[(SESSION, "identity", [
        _header(), _model("google", "gemini-3.1-pro-preview"),
        _message("assistant", "Uncompressed archived answer", "answer", 2),
    ])])

    result = import_openclaw_history(tmp_path / "store", archive)

    assert result["failed"] == 0
    assert _rows(tmp_path / "store", "gemini_openclaw")[0]["content_text"] == "Uncompressed archived answer"


def test_database_backup_generations_supply_history_missing_from_current_database(tmp_path):
    archive = _archive(tmp_path)
    _database(archive)
    _database(archive, records=[(SESSION, event, True) for event in [
        _header(), _model("google", "gemini-3.1-pro-preview"),
        _message("assistant", "Older unique answer", "answer", 2),
    ]], name="openclaw-agent.sqlite.pre-startup-migration-5fd7baa2.bak")

    result = import_openclaw_history(tmp_path / "store", archive)

    assert result["failed"] == 0
    assert _rows(tmp_path / "store", "gemini_openclaw")[0]["content_text"] == "Older unique answer"


def test_equal_idless_messages_remain_distinct_with_stable_repeat_import_keys(tmp_path):
    archive = _archive(tmp_path)
    repeat = _message("assistant", "Repeated response", None, 2, provider="openai", model="gpt-5.5")
    _transcript(archive, [_header(), repeat, repeat])
    store = tmp_path / "store"

    first = import_openclaw_history(store, archive)
    keys = [row["message_key"] for row in _rows(store, "chatgpt_openclaw")]
    second = import_openclaw_history(store, archive)

    assert first["messages"] == 2
    assert len(set(keys)) == 2
    assert second["added_or_changed"] == 0
    assert [row["message_key"] for row in _rows(store, "chatgpt_openclaw")] == keys


def test_explicit_source_selection_does_not_write_other_provider_stores(tmp_path):
    archive = _archive(tmp_path)
    _transcript(archive, [_header(),
        _message("assistant", "OpenAI answer", "openai", 2, provider="openai", model="gpt-5.5"),
        _message("assistant", "Gemini answer", "google", 3, provider="google", model="gemini-3.1-pro-preview"),
    ])
    store = tmp_path / "store"

    result = import_openclaw_history(store, archive, source="gemini_openclaw")

    assert result["messages"] == 1
    assert set(result["source_counts"]) == {"gemini_openclaw"}
    assert openclaw_history_path(store, "gemini_openclaw").is_file()
    assert all(not openclaw_history_path(store, key).exists()
               for key in OPENCLAW_SOURCE_LABELS if key != "gemini_openclaw")


def test_unsupported_source_is_rejected_before_any_cache_write(tmp_path):
    with pytest.raises(ValueError, match="Unsupported"):
        import_openclaw_history(tmp_path / "store", tmp_path / "archive", source="chatgpt")
    assert not (tmp_path / "store").exists()


def test_idless_repeats_at_different_times_survive_duplicate_copies_and_reimport(tmp_path):
    archive = _archive(tmp_path)
    events = [_header(),
        _message("assistant", "Repeated answer", None, 2, provider="openai", model="gpt-5.5"),
        _message("assistant", "Repeated answer", None, 4, provider="openai", model="gpt-5.5"),
    ]
    _transcript(archive, events)
    _transcript(archive, events, "history.jsonl.reset.2026-10-13T01-00-00Z")
    store = tmp_path / "store"

    first = import_openclaw_history(store, archive)
    rows = _rows(store, "chatgpt_openclaw")
    original = openclaw_history_path(store, "chatgpt_openclaw").read_bytes()
    second = import_openclaw_history(store, archive)

    assert first["messages"] == 2
    assert {row["first_seen_at"] for row in rows} == {_time(2), _time(4)}
    assert len({row["message_key"] for row in rows}) == 2
    assert all(len(json.loads(row["provenance_json"])) == 2 for row in rows)
    assert second["added_or_changed"] == 0
    assert openclaw_history_path(store, "chatgpt_openclaw").read_bytes() == original


def test_embedded_codex_repeated_idless_prompts_and_answers_remain_distinct(tmp_path):
    archive = _archive(tmp_path)
    home = archive / "state/agents/main/agent/codex-home"
    events = [{"type": "session_meta", "timestamp": _time(0), "payload": {
        "id": OTHER, "originator": "openclaw", "model_provider": "openai"}},
        {"type": "turn_context", "timestamp": _time(1), "payload": {"model": "gpt-5.5"}},
    ]
    for second in range(2, 6):
        role = "user" if second % 2 == 0 else "assistant"
        events.append({"type": "response_item", "timestamp": _time(second), "payload": {
            "type": "message", "role": role,
            "content": [{"type": "input_text" if role == "user" else "output_text", "text": "Continue"}],
        }})
    for directory in (home / "sessions/2026/10/13", home / "archived_sessions"):
        directory.mkdir(parents=True)
        (directory / "rollout-example.jsonl").write_bytes(_jsonl(events))
    store = tmp_path / "store"

    first = import_openclaw_history(store, archive)
    rows = _rows(store, "chatgpt_openclaw")
    second = import_openclaw_history(store, archive)

    assert first["failed"] == 0
    assert first["messages"] == 4
    assert [row["role"] for row in rows] == ["user", "assistant", "user", "assistant"]
    assert len({row["message_key"] for row in rows}) == 4
    assert all(len(json.loads(row["provenance_json"])) == 2 for row in rows)
    assert second["added_or_changed"] == 0


@pytest.mark.parametrize("record", [None, [], "scalar", {"type": []},
    {"type": "message", "message": None}, {"type": "session_meta", "payload": []},
    {"type": "custom", "customType": "model-snapshot", "data": []},
])
def test_malformed_record_containers_are_counted_without_aborting_valid_files(tmp_path, record):
    archive = _archive(tmp_path)
    _transcript(archive, [_header(), record], "malformed.jsonl")
    _transcript(archive, [_header(OTHER), _message("assistant", "Valid answer", "valid", 2,
                                                  provider="openai", model="gpt-5.5")], "valid.jsonl")

    result = import_openclaw_history(tmp_path / "store", archive)

    assert result["failed"] == 1
    assert result["failures"][0]["file"].endswith("malformed.jsonl")
    assert _rows(tmp_path / "store", "chatgpt_openclaw")[0]["content_text"] == "Valid answer"


@pytest.mark.parametrize("identity", [None, "", 17])
def test_missing_or_invalid_header_identity_cannot_merge_unrelated_sessions(tmp_path, identity):
    archive = _archive(tmp_path)
    _transcript(archive, [{"type": "session", "version": 4, "id": identity},
        _message("assistant", "Unidentified answer", "answer", 2, provider="openai", model="gpt-5.5"),
    ])

    result = import_openclaw_history(tmp_path / "store", archive)

    assert result["failed"] == 1
    assert result["messages"] == 0


@pytest.mark.parametrize("linked_component", ["agents", "main", "sessions", "transcript", "database_directory", "database"])
def test_linked_source_ancestors_are_rejected_before_reading_outside_archive(tmp_path, linked_component):
    archive = _archive(tmp_path)
    external = _archive(tmp_path / "external")
    events = [_header(), _message("assistant", "External private answer", "answer", 2,
                                  provider="openai", model="gpt-5.5")]
    external_transcript = _transcript(external, events)
    external_database = _database(external, [(SESSION, event, False) for event in events])
    state = archive / "state"
    external_state = external / "state"
    if linked_component == "agents":
        (state / "agents/main/sessions").rmdir()
        (state / "agents/main").rmdir()
        (state / "agents").rmdir()
        (state / "agents").symlink_to(external_state / "agents", target_is_directory=True)
    elif linked_component == "main":
        (state / "agents/main/sessions").rmdir()
        (state / "agents/main").rmdir()
        (state / "agents/main").symlink_to(external_state / "agents/main", target_is_directory=True)
    elif linked_component == "sessions":
        (state / "agents/main/sessions").rmdir()
        (state / "agents/main/sessions").symlink_to(external_transcript.parent, target_is_directory=True)
    elif linked_component == "transcript":
        (state / "agents/main/sessions/history.jsonl").symlink_to(external_transcript)
    elif linked_component == "database_directory":
        (state / "agents/main/agent").symlink_to(external_database.parent, target_is_directory=True)
    else:
        (state / "agents/main/agent").mkdir()
        (state / "agents/main/agent/openclaw-agent.sqlite").symlink_to(external_database)
    before = _fingerprint(external)

    result = import_openclaw_history(tmp_path / "store", archive)

    assert result["failed"] >= 1
    assert result["messages"] == 0
    assert _fingerprint(external) == before


def test_linked_archive_parent_is_rejected_before_import(tmp_path):
    archive = _archive(tmp_path)
    parent = tmp_path / "linked-parent"
    parent.symlink_to(archive.parent, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic"):
        import_openclaw_history(tmp_path / "store", parent / archive.name)


def test_linked_destination_parent_cannot_replace_external_cache(tmp_path):
    archive = _archive(tmp_path)
    _transcript(archive, [_header(), _message("assistant", "Answer", "answer", 2,
                                             provider="openai", model="gpt-5.5")])
    external = tmp_path / "external"
    external.mkdir()
    target = external / "history.parquet"
    target.write_bytes(b"protected external bytes")
    store = tmp_path / "store"
    (store / "llm").mkdir(parents=True)
    (store / "llm/chatgpt_openclaw").symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic"):
        import_openclaw_history(store, archive)
    assert target.read_bytes() == b"protected external bytes"


def test_stopping_before_next_file_preserves_cache_and_completed_file_messages(tmp_path):
    archive = _archive(tmp_path)
    _transcript(archive, [_header(), _message("assistant", "First answer", "first", 2,
                                             provider="openai", model="gpt-5.5")], "a-first.jsonl")
    _transcript(archive, [_header(OTHER), _message("assistant", "Second answer", "second", 3,
                                                  provider="openai", model="gpt-5.5")], "b-second.jsonl")
    stopped = False

    def progress(result):
        nonlocal stopped
        if result["files_scanned"] == 1:
            stopped = True

    store = tmp_path / "store"
    result = import_openclaw_history(store, archive, should_stop=lambda: stopped, on_progress=progress)

    assert result["stopped"] is True
    assert result["files_scanned"] == 1
    assert [row["content_text"] for row in _rows(store, "chatgpt_openclaw")] == ["First answer"]
    resumed = import_openclaw_history(store, archive)
    assert resumed["stopped"] is False
    assert resumed["messages"] == 2


def _legacy_synthetic_row(rows, occurrence=0):
    """Reproduce the identity and combined provenance emitted by version 1.0.0."""
    row = dict(rows[-1])
    row["original_event_id"] = f"occurrence:{occurrence}"
    identity = [row["conversation_id"], row["original_event_id"], row["role"],
                row["content_text"], row["provider"], row["model_label"]]
    row["message_key"] = hashlib.sha256(json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    locations = [location for item in rows for location in json.loads(item["provenance_json"])]
    row["provenance_json"] = json.dumps(
        list({json.dumps(value, sort_keys=True): value for value in locations}.values()), sort_keys=True,
    )
    return row


def _repeated_legacy_fixture(tmp_path):
    archive = _archive(tmp_path)
    paths = []
    for second, filename in ((2, "first.jsonl"), (4, "second.jsonl")):
        paths.append(_transcript(archive, [_header(),
            _message("assistant", "Repeated answer", None, second, provider="openai", model="gpt-5.5"),
        ], filename))
    store = tmp_path / "store"
    import_openclaw_history(store, archive)
    return archive, store, paths, _rows(store, "chatgpt_openclaw")


def test_old_collapsed_synthetic_cache_migrates_only_fully_represented_evidence(tmp_path):
    archive, store, _paths, corrected = _repeated_legacy_fixture(tmp_path)
    legacy = _legacy_synthetic_row(corrected)
    orphan = _legacy_synthetic_row(corrected, occurrence=7)
    orphan["provenance_json"] = json.dumps([{"file": "agents/main/sessions/missing.jsonl", "line": 2}])
    path = openclaw_history_path(store, "chatgpt_openclaw")
    write_parquet_rows_atomic(path, [legacy, orphan], OPENCLAW_HISTORY_SCHEMA)

    result = import_openclaw_history(store, archive)
    rows = _rows(store, "chatgpt_openclaw")
    original = path.read_bytes()
    repeated = import_openclaw_history(store, archive)

    assert result["migrated_legacy_keys"] == 1
    assert result["messages"] == 3
    assert {row["message_key"] for row in rows} == {
        *(row["message_key"] for row in corrected), orphan["message_key"],
    }
    assert repeated["migrated_legacy_keys"] == 0
    assert repeated["added_or_changed"] == 0
    assert repeated["applied"] is False
    assert path.read_bytes() == original


def test_legacy_retirement_rewrites_cache_when_corrected_rows_are_already_unchanged(tmp_path):
    archive, store, _paths, corrected = _repeated_legacy_fixture(tmp_path)
    legacy = _legacy_synthetic_row(corrected)
    path = openclaw_history_path(store, "chatgpt_openclaw")
    write_parquet_rows_atomic(path, [*corrected, legacy], OPENCLAW_HISTORY_SCHEMA)

    result = import_openclaw_history(store, archive)

    assert result["migrated_legacy_keys"] == 1
    assert result["added_or_changed"] == 0
    assert result["applied"] is True
    assert result["messages"] == 2
    assert {row["message_key"] for row in _rows(store, "chatgpt_openclaw")} == {
        row["message_key"] for row in corrected
    }


def test_partial_archive_failure_retains_unproven_legacy_cache_until_evidence_recovers(tmp_path):
    archive, store, paths, corrected = _repeated_legacy_fixture(tmp_path)
    legacy = _legacy_synthetic_row(corrected)
    write_parquet_rows_atomic(openclaw_history_path(store, "chatgpt_openclaw"), [legacy], OPENCLAW_HISTORY_SCHEMA)
    intact = paths[1].read_bytes()
    paths[1].write_bytes(intact + b'{"type":"message",')

    partial = import_openclaw_history(store, archive)
    partial_keys = {row["message_key"] for row in _rows(store, "chatgpt_openclaw")}
    paths[1].write_bytes(intact)
    recovered = import_openclaw_history(store, archive)

    assert partial["failed"] == 1
    assert partial["migrated_legacy_keys"] == 0
    assert legacy["message_key"] in partial_keys
    assert recovered["failed"] == 0
    assert recovered["migrated_legacy_keys"] == 1
    assert recovered["messages"] == 2


@pytest.mark.parametrize("change", ["different_archive", "empty_provenance", "malformed_provenance"])
def test_uncertain_legacy_evidence_is_retained_even_when_message_content_matches(tmp_path, change):
    archive, store, _paths, corrected = _repeated_legacy_fixture(tmp_path)
    legacy = _legacy_synthetic_row(corrected)
    if change == "different_archive":
        legacy["archive_directory"] = str(tmp_path / "another-archive")
    elif change == "empty_provenance":
        legacy["provenance_json"] = "[]"
    else:
        legacy["provenance_json"] = '{"file":"not-a-provenance-list"}'
    write_parquet_rows_atomic(openclaw_history_path(store, "chatgpt_openclaw"), [legacy], OPENCLAW_HISTORY_SCHEMA)

    result = import_openclaw_history(store, archive)

    assert result["migrated_legacy_keys"] == 0
    assert result["messages"] == 3
    assert legacy["message_key"] in {row["message_key"] for row in _rows(store, "chatgpt_openclaw")}
