"""OpenClaw archive completeness, provenance, and source-safety regressions.

Code version: v1.0.0-codex.0
"""

import hashlib
import json
import sqlite3

import pyarrow as pa
import pytest

from app.core.openclaw_history import (
    OPENCLAW_SOURCE_LABELS,
    import_openclaw_history,
    openclaw_history_path,
)
from app.core.resource_persistence import read_parquet_rows


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
