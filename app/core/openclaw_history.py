"""Import preserved OpenClaw transcripts with explicit provider provenance.

Code version: v1.0.2-codex.0
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa

from .resource_persistence import (
    CLAUDE_HISTORY_SCHEMA,
    read_parquet_rows,
    write_parquet_rows_atomic,
)


OPENCLAW_HISTORY_FILENAME = "history.parquet"
OPENCLAW_SOURCE_LABELS = {
    "chatgpt_openclaw": "ChatGPT OpenClaw",
    "gemini_openclaw": "Gemini OpenClaw",
    "claude_openclaw": "Claude OpenClaw",
    "grok_openclaw": "Grok OpenClaw",
    "zhipu_openclaw": "Zhipu OpenClaw",
    "doubao_openclaw": "Doubao OpenClaw",
    "other_openclaw": "Other OpenClaw",
    "unknown_openclaw": "Unknown OpenClaw",
}
OPENCLAW_HISTORY_SCHEMA = pa.schema([
    *CLAUDE_HISTORY_SCHEMA,
    pa.field("original_session_id", pa.string()),
    pa.field("original_event_id", pa.string()),
    pa.field("provider", pa.string()),
    pa.field("harness", pa.string()),
    pa.field("attribution", pa.string()),
    pa.field("provenance_json", pa.string()),
    pa.field("archive_directory", pa.string()),
])


def openclaw_history_path(local_store_root: Path | str, source: str) -> Path:
    """Keep each OpenClaw provider separate from every native chat source."""
    if source not in OPENCLAW_SOURCE_LABELS:
        raise ValueError("Unsupported OpenClaw source.")
    return Path(local_store_root) / "llm" / source / OPENCLAW_HISTORY_FILENAME


def _decompress(raw: bytes) -> bytes:
    """Use the existing Arrow codec on every supported Python version."""
    with pa.CompressedInputStream(pa.BufferReader(raw), "zstd") as stream:
        return stream.read()


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _timestamp(value: Any) -> str:
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            parsed = datetime.fromtimestamp(value / 1000 if value > 100_000_000_000 else value, UTC)
        elif isinstance(value, str) and value:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return ""
        else:
            return ""
        return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return ""


def _source(provider: str, model: str) -> str:
    """Attribute only explicit provider or unambiguous model-family evidence."""
    provider, model = provider.lower(), model.lower()
    if provider == "openclaw":
        return "unknown_openclaw"
    if provider in {"openai", "openai-codex", "azure-openai"}:
        return "chatgpt_openclaw"
    if provider in {"google", "google-gemini-cli", "google-antigravity", "gemini"}:
        return "gemini_openclaw"
    if provider in {"anthropic", "claude"}:
        return "claude_openclaw"
    if provider in {"xai", "x-ai"}:
        return "grok_openclaw"
    if provider in {"zhipu", "zai", "z-ai"}:
        return "zhipu_openclaw"
    if provider in {"volcengine", "doubao"}:
        return "doubao_openclaw"
    if provider:
        return "other_openclaw"
    for prefix, source in (("gemini-", "gemini_openclaw"), ("gpt-", "chatgpt_openclaw"),
                           ("claude-", "claude_openclaw"), ("grok-", "grok_openclaw"),
                           ("glm-", "zhipu_openclaw")):
        if model.startswith(prefix):
            return source
    return "unknown_openclaw"


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content.replace("\x00", "").strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in ("text", "input_text", "output_text"):
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        elif block.get("type") in ("image", "input_image", "audio", "input_audio", "file"):
            parts.append(f"[Archived {block['type']} attachment; original payload retained in the private archive.]")
    return "\n\n".join(parts).replace("\x00", "")


def _safe_path(path: Path, boundary: Path | None = None) -> Path:
    """Reject symbolic-link ancestors before opening archive content."""
    path = path.expanduser().absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("OpenClaw archive paths must not contain symbolic links.")
    resolved = path.resolve()
    if boundary is not None and not resolved.is_relative_to(boundary):
        raise ValueError("OpenClaw transcript path escapes the archive state directory.")
    return resolved


def _event_object(event: Any) -> dict:
    """Validate record containers before identity and message parsing."""
    if not isinstance(event, dict):
        raise ValueError("Transcript event must be an object.")
    kind = event.get("type")
    if not isinstance(kind, str):
        raise ValueError("Transcript event type must be a string.")
    if kind == "message" and not isinstance(event.get("message"), dict):
        raise ValueError("Transcript message must be an object.")
    if kind in {"session_meta", "turn_context", "response_item"} and not isinstance(event.get("payload"), dict):
        raise ValueError("Codex transcript payload must be an object.")
    if kind == "custom" and event.get("customType") == "model-snapshot" and not isinstance(event.get("data"), dict):
        raise ValueError("Transcript model snapshot must be an object.")
    return event


def _state_directory(root: Path, source_directory: Path | str | None) -> tuple[Path, Path]:
    if source_directory is None:
        pointer_path = _safe_path(root / "openclaw_archive" / "latest.json")
        pointer = json.loads(pointer_path.read_text())
        archive = _safe_path(Path(pointer["archive_directory"]))
    else:
        archive = _safe_path(Path(source_directory))
    state = archive / "state" if not (archive / "agents").is_dir() and (archive / "state").is_dir() else archive
    state = _safe_path(state, archive)
    if not state.is_dir():
        raise ValueError("OpenClaw archive state directory is unavailable.")
    return archive, state


def _collect(state: Path, stop, progress) -> tuple[dict, dict]:
    """Collect current SQLite, historical archives, and embedded Codex rollouts."""
    sessions: dict[tuple[str, str, str], dict] = {}
    occurrences: dict[tuple, int] = defaultdict(int)
    stats = {"files_scanned": 0, "files_total": 0, "failed": 0, "failures": [],
             "events_read": 0, "duplicate_events": 0, "stopped": False}

    def safe_candidate(path):
        try:
            _safe_path(path, state)
        except (OSError, ValueError) as error:
            stats["failed"] += 1
            stats["failures"].append({"file": str(path.relative_to(state)), "error_type": type(error).__name__})
            return False
        return True

    def add(agent, session_id, harness, event, location):
        event = _event_object(event)
        stats["events_read"] += 1
        bucket = sessions.setdefault((agent, str(session_id), harness), {})
        fingerprint = _digest(event)
        identity = str(event.get("id") or "")
        if not identity:
            scope = (agent, str(session_id), harness, location.get("file"),
                     location.get("table"), location.get("generation"), fingerprint)
            occurrence = occurrences[scope]
            occurrences[scope] += 1
            identity = f"event:{fingerprint}:occurrence:{occurrence}"
        # Exact copies across database generations share one event. Differing
        # versions retain separate hashes and therefore cannot overwrite history.
        key = (identity, fingerprint)
        if key in bucket:
            bucket[key]["locations"].append(location)
            stats["duplicate_events"] += 1
        else:
            bucket[key] = {"event": event, "locations": [location], "order": len(bucket),
                           "identity": identity}

    paths = []
    agents = state / "agents"
    if safe_candidate(agents) and agents.is_dir():
        for agent in sorted(agents.iterdir()):
            if not safe_candidate(agent) or not agent.is_dir():
                continue
            database_directory = agent / "agent"
            if safe_candidate(database_directory) and database_directory.is_dir():
                for db in sorted(database_directory.glob("openclaw-agent.sqlite*")):
                    if safe_candidate(db) and db.is_file() and not db.name.endswith(("-wal", "-shm", "-journal")):
                        with db.open("rb") as stream:
                            if stream.read(16) == b"SQLite format 3\x00":
                                paths.append((agent.name, db, "sqlite"))
            for directory in (agent / "sessions", agent / "session-sqlite-import-archive"):
                if safe_candidate(directory) and directory.is_dir():
                    paths.extend((agent.name, p, "jsonl") for p in sorted(directory.rglob("*"))
                                 if (".jsonl" in p.name or p.suffix == ".zst") and safe_candidate(p) and p.is_file()
                                 and ".codex-app-server.json" not in p.name)
            home = agent / "agent" / "codex-home"
            for directory in (home / "sessions", home / "archived_sessions"):
                if safe_candidate(directory) and directory.is_dir():
                    paths.extend((agent.name, p, "codex") for p in sorted(directory.rglob("rollout*.jsonl"))
                                 if safe_candidate(p) and p.is_file())
    stats["files_total"] = len(paths) + stats["failed"]
    for agent, path, kind in paths:
        if stop():
            stats["stopped"] = True
            break
        relative = str(path.relative_to(state))
        try:
            _safe_path(path, state)
            if kind == "sqlite":
                with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
                    columns = {r[1] for r in connection.execute("pragma table_info(transcript_events)")}
                    if not columns:
                        stats["files_scanned"] += 1
                        continue
                    compressed = "event_zstd" if "event_zstd" in columns else "NULL"
                    for sid, seq, raw, packed in connection.execute(
                        f"select session_id,seq,event_json,{compressed} from transcript_events order by session_id,seq"
                    ):
                        event = json.loads(raw if raw is not None else _decompress(packed))
                        add(agent, sid, "openclaw", event, {"file": relative, "table": "transcript_events", "seq": seq})
                    tables = {r[0] for r in connection.execute("select name from sqlite_master where type='table'")}
                    for table in ("session_transcript_archives", "session_transcript_cold_archives"):
                        if table not in tables:
                            continue
                        archive_columns = {r[1] for r in connection.execute(f"pragma table_info({table})")}
                        encoding = "encoding" if "encoding" in archive_columns else "'zstd'"
                        for sid, generation, blob, codec in connection.execute(
                            f"select session_id,generation,archive_blob,{encoding} from {table} where archive_blob is not null"
                        ):
                            if codec not in {"zstd", "identity", "utf8", "jsonl"}:
                                raise ValueError("Unsupported transcript archive encoding.")
                            decoded = _decompress(blob) if codec == "zstd" else bytes(blob)
                            for number, raw in enumerate(decoded.splitlines(), 1):
                                if raw.strip():
                                    add(agent, sid, "openclaw", json.loads(raw),
                                        {"file": relative, "table": table, "generation": generation, "line": number})
            else:
                raw = path.read_bytes()
                if path.suffix == ".zst":
                    raw = _decompress(raw)
                records = [(number, _event_object(json.loads(line)))
                           for number, line in enumerate(raw.splitlines(), 1) if line.strip()]
                session_id = next((event["id"] for _, event in records if event.get("type") == "session"
                                   and isinstance(event.get("id"), str) and event["id"].strip()), "")
                if kind == "codex":
                    session_id = next((event["payload"]["id"] for _, event in records
                                       if event.get("type") == "session_meta"
                                       and isinstance(event["payload"].get("id"), str)
                                       and event["payload"]["id"].strip()), "")
                if not session_id:
                    # Trajectory-only records are retained in the archive and not
                    # silently recast as user-facing messages.
                    if all(event.get("type") not in {"message", "response_item"} for _, event in records):
                        stats["files_scanned"] += 1
                        continue
                    raise ValueError("Transcript session identity is missing.")
                for number, event in records:
                    add(agent, session_id, "codex" if kind == "codex" else "openclaw", event,
                        {"file": relative, "line": number})
        except (OSError, ValueError, sqlite3.Error, pa.ArrowException) as error:
            stats["failed"] += 1
            stats["failures"].append({"file": relative, "error_type": type(error).__name__})
        stats["files_scanned"] += 1
        if progress:
            progress(dict(stats))
    return sessions, stats


def _rows(sessions: dict, archive: Path) -> list[dict]:
    rows = []
    for (agent, sid, harness), bucket in sessions.items():
        events = sorted(bucket.values(), key=lambda item: (
            _timestamp(item["event"].get("timestamp")) or "", item["order"]))
        provider = model = ""
        visible = []
        for item in events:
            event = item["event"]
            kind = event.get("type")
            if kind == "model_change":
                provider = str(event.get("provider") or "")
                model = str(event.get("modelId") or "")
            if kind == "custom" and event.get("customType") == "model-snapshot":
                data = event.get("data") or {}
                provider = str(data.get("provider") or "")
                model = str(data.get("modelId") or "")
            if harness == "codex":
                payload = event.get("payload") or {}
                if kind == "session_meta":
                    provider = str(payload.get("model_provider") or "")
                if kind == "turn_context":
                    model = str(payload.get("model") or model)
                if kind != "response_item" or payload.get("type") != "message":
                    continue
                if payload.get("channel") in ("analysis", "reasoning") or payload.get("phase") in ("analysis", "reasoning"):
                    continue
                message = payload
            else:
                if kind != "message":
                    continue
                message = event.get("message") or {}
            role = message.get("role")
            if role not in ("user", "assistant"):
                continue
            content = _text(message.get("content"))
            if not content:
                continue
            explicit_provider = str(message.get("provider") or provider)
            explicit_model = str(message.get("model") or model)
            # Delivery mirrors and gateway injections are retained as unknown
            # infrastructure, never as output from the current model.
            visible.append({"item": item, "message": message, "content": content,
                            "provider": explicit_provider, "model": explicit_model,
                            "source": _source(explicit_provider, explicit_model), "role": role})
        title = next((v["content"].splitlines()[0][:100] for v in visible if v["role"] == "user"),
                     f"OpenClaw session {sid[:12]}")
        for index, value in enumerate(visible):
            attribution = "message_provider" if value["message"].get("provider") else "temporal_model_evidence"
            targets = {(value["source"], value["provider"], value["model"])}
            if value["role"] == "user":
                following = []
                for later in visible[index + 1:]:
                    if later["role"] == "user":
                        break
                    if later["source"] != "unknown_openclaw":
                        following.append((later["source"], later["provider"], later["model"]))
                if following:
                    by_source = {}
                    for target in following:
                        by_source.setdefault(target[0], target)
                    targets = set(by_source.values())
                    attribution = "following_assistant_in_same_turn"
            event = value["item"]["event"]
            timestamp = _timestamp(event.get("timestamp") or value["message"].get("timestamp"))
            event_id = str(event.get("id") or value["message"].get("id") or value["item"]["identity"])
            conversation_id = f"{agent}:{harness}:{sid}"
            for source, row_provider, row_model in sorted(targets):
                identity = _digest([conversation_id, event_id, value["role"], value["content"], row_provider, row_model])
                rows.append({
                    "schema_version": 1, "platform": source,
                    "conversation_id": conversation_id, "conversation_url": "",
                    "conversation_title": title, "message_key": identity,
                    "turn_index": index, "message_index": index, "role": value["role"],
                    "author_label": "You" if value["role"] == "user" else OPENCLAW_SOURCE_LABELS[source],
                    "content_text": value["content"], "content_html": "",
                    "content_sha256": hashlib.sha256(value["content"].encode()).hexdigest(),
                    "source_links": [], "model_label": row_model,
                    "first_seen_at": timestamp, "last_seen_at": timestamp,
                    "original_session_id": sid, "original_event_id": event_id,
                    "provider": row_provider, "harness": harness, "attribution": attribution,
                    "provenance_json": json.dumps(value["item"]["locations"], sort_keys=True),
                    "archive_directory": str(archive),
                })
    return rows


def _legacy_synthetic_keys(previous: dict, incoming: dict) -> set[str]:
    """Retire old derived identities only when corrected records cover their evidence."""
    fields = ("platform", "conversation_id", "original_session_id", "harness", "role",
              "content_text", "content_sha256", "provider", "model_label", "archive_directory")

    def provenance(row):
        try:
            locations = json.loads(row.get("provenance_json") or "null")
            if not isinstance(locations, list) or not locations:
                return set()
            if any(not isinstance(value, dict) or not isinstance(value.get("file"), str)
                   or not value["file"] for value in locations):
                return set()
            return {json.dumps(value, sort_keys=True) for value in locations}
        except (TypeError, ValueError):
            return set()

    evidence = {}
    for row in incoming.values():
        if not re.fullmatch(r"event:[0-9a-f]{64}:occurrence:[0-9]+", row.get("original_event_id") or ""):
            continue
        locations = provenance(row)
        if not locations:
            continue
        signature = tuple(row.get(field) for field in fields)
        covered, timestamps = evidence.setdefault(signature, (set(), set()))
        covered.update(locations)
        timestamps.add((row.get("first_seen_at"), row.get("last_seen_at")))

    retired = set()
    for key, row in previous.items():
        if key in incoming or not re.fullmatch(r"occurrence:[0-9]+", row.get("original_event_id") or ""):
            continue
        signature = tuple(row.get(field) for field in fields)
        covered, timestamps = evidence.get(signature, (set(), set()))
        locations = provenance(row)
        if (locations and locations <= covered
                and (row.get("first_seen_at"), row.get("last_seen_at")) in timestamps):
            retired.add(key)
    return retired


def import_openclaw_history(
    local_store_root: Path | str,
    source_directory: Path | str | None = None,
    *,
    source: str = "all",
    should_stop=None,
    on_progress=None,
) -> dict[str, Any]:
    """Rebuild searchable providers from a standalone archive without live access."""
    if source != "all" and source not in OPENCLAW_SOURCE_LABELS:
        raise ValueError("Unsupported OpenClaw source.")
    root = _safe_path(Path(local_store_root))
    archive, state = _state_directory(root, source_directory)
    sessions, result = _collect(state, should_stop or (lambda: False), on_progress)
    incoming = defaultdict(dict)
    for row in _rows(sessions, archive):
        prior = incoming[row["platform"]].get(row["message_key"])
        if prior:
            locations = json.loads(prior["provenance_json"]) + json.loads(row["provenance_json"])
            row["provenance_json"] = json.dumps(
                list({json.dumps(value, sort_keys=True): value for value in locations}.values()), sort_keys=True,
            )
        incoming[row["platform"]][row["message_key"]] = row
    selected = tuple(OPENCLAW_SOURCE_LABELS) if source == "all" else (source,)
    result.update({"sessions": 0, "messages": 0, "imported_sessions": 0,
                   "imported_messages": 0, "added_or_changed": 0,
                   "unchanged_messages": 0, "unchanged_sessions": 0, "applied": False,
                   "migrated_legacy_keys": 0, "archive_directory": str(archive), "source_counts": {}})
    for key in selected:
        path = _safe_path(openclaw_history_path(root, key), root)
        previous = read_parquet_rows(path)
        if path.exists() and previous is None:
            raise RuntimeError("Existing OpenClaw history is unreadable; import refused.")
        merged = {row["message_key"]: row for row in previous or []}
        retired = _legacy_synthetic_keys(merged, incoming[key])
        for identity in retired:
            del merged[identity]
        changed = sum(merged.get(identity) != row for identity, row in incoming[key].items())
        merged.update(incoming[key])
        values = sorted(merged.values(), key=lambda row: (row["conversation_id"], row["message_index"], row["message_key"]))
        if changed or retired:
            write_parquet_rows_atomic(path, values, OPENCLAW_HISTORY_SCHEMA)
            path.chmod(0o600)
            result["applied"] = True
        count = len({row["conversation_id"] for row in values})
        result["source_counts"][key] = {"sessions": count, "messages": len(values)}
        result["sessions"] += count
        result["messages"] += len(values)
        result["imported_sessions"] += len({row["conversation_id"] for row in incoming[key].values()})
        result["imported_messages"] += len(incoming[key])
        result["added_or_changed"] += changed
        result["migrated_legacy_keys"] += len(retired)
        result["unchanged_messages"] += len(incoming[key]) - changed
    if on_progress:
        on_progress(dict(result))
    return result
