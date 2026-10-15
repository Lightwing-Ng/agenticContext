"""Import visible native Grok Build session updates without changing source files.

Code version: v1.0.1-grok-build.0
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa

from .history_rows import history_rows_match, sort_history_rows
from .resource_persistence import (
    GROK_HISTORY_SCHEMA,
    read_parquet_rows,
    write_parquet_rows_atomic,
)


GROK_BUILD_HISTORY_FILENAME = "history.parquet"
GROK_BUILD_HISTORY_SCHEMA_VERSION = 1
GROK_BUILD_HISTORY_SCHEMA = pa.schema(
    [
        *GROK_HISTORY_SCHEMA,
        pa.field("source_file", pa.string(), nullable=False),
        pa.field("source_line", pa.int64(), nullable=False),
        pa.field("source_kind", pa.string(), nullable=False),
    ]
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
_MAX_LINE_BYTES = 16 * 1024 * 1024
_MAX_METADATA_BYTES = 2 * 1024 * 1024


def default_grok_build_directory() -> Path:
    """Reuse the configured native Grok home, including GROK_HOME overrides."""
    from .config import default_grok_build_directory as configured_directory

    return configured_directory()


def grok_build_history_path(local_store_root: Path | str) -> Path:
    """Keep native Grok Build messages separate from browser Grok Chat history."""
    return (
        Path(local_store_root).expanduser()
        / "llm"
        / "grok_build"
        / GROK_BUILD_HISTORY_FILENAME
    )


def _is_directory(path: Path) -> bool:
    absolute = path.absolute()
    return (
        not any(parent.is_symlink() for parent in (absolute, *absolute.parents))
        and path.is_dir()
    )


def _text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return (
        value.replace("\x00", "")
        .encode("utf-8", errors="replace")
        .decode("utf-8")
        .strip()
    )


def _identifier(value: Any) -> str:
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else ""


def _integer(value: Any) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


def _timestamp(value: Any, *, milliseconds: bool = False) -> str:
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            parsed = datetime.fromtimestamp(
                value / 1_000 if milliseconds else value, tz=UTC
            )
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
        else:
            return ""
        return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    except (OSError, OverflowError, ValueError):
        return ""


def _read_metadata(path: Path) -> dict[str, Any] | None:
    if path.is_symlink() or not _is_directory(path.parent):
        return None
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_METADATA_BYTES + 1)
        if len(raw) > _MAX_METADATA_BYTES:
            return None
        payload = json.loads(raw)
    except (OSError, UnicodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _session_directories(source: Path) -> tuple[Path, list[Path]]:
    """Visit only the documented home, sessions, project, or single-session layouts."""
    if not _is_directory(source):
        raise ValueError(
            "Grok Build directory must exist and must not contain symbolic links."
        )
    if _is_directory(source / "sessions"):
        root, levels = source / "sessions", 2
    elif source.name == "sessions":
        root, levels = source, 2
    elif (source / "summary.json").exists() or (source / "updates.jsonl").exists():
        root, levels = source, 0
    else:
        root, levels = source, 1
    candidates: list[Path] = []
    for directory, children, filenames in os.walk(root, followlinks=False):
        parent = Path(directory)
        depth = len(parent.relative_to(root).parts)
        children[:] = sorted(
            name
            for name in children
            if depth < levels
            and name not in {"subagents", "compaction_checkpoints"}
            and not (parent / name).is_symlink()
        )
        if "summary.json" in filenames or "updates.jsonl" in filenames:
            candidates.append(parent)
    if not candidates and root.name != "sessions":
        raise ValueError(
            "Choose the Grok home, its sessions folder, or a project/session folder "
            "containing summary.json and updates.jsonl."
        )
    return root, candidates


def _subagent_ids(directories: list[Path]) -> set[str]:
    """Exclude child sessions even when an older summary omits its session kind."""
    children: set[str] = set()
    for directory in directories:
        root = directory / "subagents"
        if not _is_directory(root):
            continue
        for child in root.iterdir():
            if not _is_directory(child):
                continue
            metadata = _read_metadata(child / "meta.json") or {}
            session_id = _identifier(metadata.get("child_session_id"))
            if session_id:
                children.add(session_id)
    return children


def _records(
    path: Path, should_stop: Callable[[], bool]
) -> Iterator[tuple[int, dict[str, Any] | None]]:
    """Read bounded records; a malformed line cannot discard other visible messages."""
    if path.is_symlink() or not _is_directory(path.parent):
        yield 0, None
        return
    try:
        with path.open("rb") as stream:
            line_number = 0
            while not should_stop() and (raw := stream.readline(_MAX_LINE_BYTES + 1)):
                line_number += 1
                if len(raw) > _MAX_LINE_BYTES:
                    yield line_number, None
                    while raw and not raw.endswith(b"\n") and not should_stop():
                        raw = stream.readline(_MAX_LINE_BYTES + 1)
                    continue
                try:
                    record = json.loads(raw)
                except (UnicodeError, ValueError):
                    record = None
                yield line_number, record if isinstance(record, dict) else None
    except OSError:
        yield 0, None


def _session_messages(
    path: Path,
    summary: dict[str, Any],
    should_stop: Callable[[], bool],
) -> tuple[list[dict[str, Any]], bool]:
    session_id = summary["info"]["id"]
    title = (
        _text(summary.get("generated_title"))
        or _text(summary.get("session_summary"))
        or "Grok Build session"
    )
    model = _text(summary.get("current_model_id"))
    groups: dict[str, dict[str, Any]] = {}
    seen_events: set[str] = set()
    prompt_turns: dict[str, int] = {}
    turn = 0
    failed = False
    for line_number, record in _records(path, should_stop):
        if record is None:
            failed = True
            continue
        if record.get("method") != "session/update":
            continue
        params = record.get("params")
        if not isinstance(params, dict) or params.get("sessionId") != session_id:
            failed = True
            continue
        update = params.get("update")
        if not isinstance(update, dict):
            failed = True
            continue
        role = {"user_message_chunk": "user", "agent_message_chunk": "assistant"}.get(
            _text(update.get("sessionUpdate")),
        )
        if not role:
            continue
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        update_meta = (
            update.get("_meta") if isinstance(update.get("_meta"), dict) else {}
        )
        if meta.get("hideFromScrollback") or update_meta.get("hideFromScrollback"):
            continue
        content = update.get("content")
        if not isinstance(content, dict) or content.get("type") != "text":
            continue
        raw_text = content.get("text")
        if not isinstance(raw_text, str):
            failed = True
            continue
        event_id = _text(meta.get("eventId"))
        if not event_id:
            failed = True
            continue
        if event_id in seen_events:
            continue
        seen_events.add(event_id)
        timestamp = _timestamp(
            meta.get("agentTimestampMs"), milliseconds=True
        ) or _timestamp(record.get("timestamp"))
        if role == "user":
            prompt_index = _integer(update_meta.get("promptIndex"))
            turn = prompt_index + 1 if prompt_index is not None else turn + 1
            model = _text(update_meta.get("modelId")) or model
        prompt_id = _text(meta.get("promptId"))
        stream_start = _integer(meta.get("streamStartMs"))
        identity = (
            f"stream:{prompt_id}:{stream_start}"
            if role == "assistant" and prompt_id and stream_start is not None
            else f"event:{event_id}"
        )
        key = f"{session_id}:{role}:{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:32]}"
        if role == "assistant" and prompt_id:
            message_turn = prompt_turns.setdefault(prompt_id, turn)
        else:
            message_turn = turn
        if key not in groups:
            groups[key] = {
                "schema_version": GROK_BUILD_HISTORY_SCHEMA_VERSION,
                "platform": "grok_build",
                "conversation_id": session_id,
                "conversation_url": "",
                "conversation_title": title,
                "message_key": key,
                "turn_index": message_turn,
                "message_index": len(groups),
                "role": role,
                "author_label": "You" if role == "user" else "Grok Build",
                "content_text": "",
                "content_html": "",
                "content_sha256": "",
                "source_links": [],
                "model_label": model,
                "first_seen_at": timestamp or _timestamp(summary.get("created_at")),
                "last_seen_at": timestamp or _timestamp(summary.get("updated_at")),
                "source_file": str(path),
                "source_line": line_number,
                "source_kind": "session",
                "_chunks": {},
            }
        group = groups[key]
        chunk_index = _integer(meta.get("chunkId"))
        chunk_key = (0, chunk_index) if chunk_index is not None else (1, line_number)
        group["_chunks"][chunk_key] = raw_text
        group["last_seen_at"] = timestamp or group["last_seen_at"]
    messages: list[dict[str, Any]] = []
    for group in groups.values():
        chunks = group.pop("_chunks")
        content_text = _text("".join(chunks[key] for key in sorted(chunks)))
        if not content_text:
            continue
        group["content_text"] = content_text
        group["content_sha256"] = hashlib.sha256(
            content_text.encode("utf-8")
        ).hexdigest()
        messages.append(group)
    return messages, failed


def import_grok_build_history(
    local_store_root: Path | str,
    source_directory: Path | str,
    *,
    apply: bool = True,
    should_stop: Callable[[], bool] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Merge visible native sessions additively, preserving earlier incomplete imports."""
    source = Path(source_directory).expanduser().absolute()
    transcript_root, directories = _session_directories(source)
    excluded = _subagent_ids(directories)
    path = grok_build_history_path(local_store_root)
    previous = read_parquet_rows(path)
    if path.exists() and previous is None:
        raise RuntimeError(f"Grok Build history Parquet is unreadable: {path}")
    rows = {row["message_key"]: row for row in previous or [] if row.get("message_key")}
    stop = should_stop or (lambda: False)
    result: dict[str, Any] = {
        "source_directory": str(source),
        "transcript_directory": str(transcript_root),
        "files_scanned": 0,
        "files_total": len(directories),
        "added_or_changed": 0,
        "unchanged_messages": 0,
        "unchanged_sessions": 0,
        "imported_sessions": 0,
        "imported_messages": 0,
        "skipped_subagents": 0,
        "failed": 0,
        "stopped": False,
        "applied": False,
    }
    imported_sessions: set[str] = set()
    for directory in directories:
        if stop():
            result["stopped"] = True
            break
        summary = _read_metadata(directory / "summary.json")
        info = summary.get("info") if summary else None
        valid = (
            isinstance(info, dict)
            and _identifier(info.get("id")) == directory.name
            and summary.get("chat_format_version") == 1
        )
        messages: list[dict[str, Any]] = []
        malformed = False
        if not valid:
            malformed = True
        elif summary.get("session_kind") == "subagent" or info["id"] in excluded:
            result["skipped_subagents"] += 1
        else:
            messages, malformed = _session_messages(
                directory / "updates.jsonl", summary, stop
            )
        session_stopped = stop()
        changed = 0
        next_index = 1 + max(
            (
                int(row["message_index"])
                for row in rows.values()
                if row["conversation_id"] == directory.name
            ),
            default=-1,
        )
        for row in messages:
            prior = rows.get(row["message_key"])
            if prior:
                row["message_index"] = prior["message_index"]
                row["turn_index"] = prior["turn_index"]
            else:
                row["message_index"] = next_index
                next_index += 1
            if prior:
                prior_text = prior["content_text"]
                next_text = row["content_text"]
                safe_extension = next_text.startswith(prior_text) and len(
                    next_text
                ) > len(prior_text)
                incomplete_reconstruction = (
                    malformed or session_stopped
                ) and not safe_extension
                truncated_prefix = prior_text.startswith(next_text) and len(
                    prior_text
                ) > len(next_text)
                if incomplete_reconstruction or truncated_prefix:
                    result["unchanged_messages"] += 1
                    continue
            if history_rows_match(prior, row, GROK_BUILD_HISTORY_SCHEMA.names):
                result["unchanged_messages"] += 1
            else:
                rows[row["message_key"]] = row
                changed += 1
        if messages:
            imported_sessions.add(directory.name)
        result["files_scanned"] += 1
        result["failed"] += int(malformed)
        result["added_or_changed"] += changed
        result["imported_sessions"] = len(imported_sessions)
        result["imported_messages"] += len(messages)
        result["unchanged_sessions"] += int(bool(messages) and not changed)
        result["stopped"] = session_stopped
        if on_progress:
            on_progress(dict(result))
        if result["stopped"]:
            break
    if apply and result["added_or_changed"]:
        write_parquet_rows_atomic(
            path, sort_history_rows(rows.values()), GROK_BUILD_HISTORY_SCHEMA
        )
        result["applied"] = True
    result["sessions"] = len({row["conversation_id"] for row in rows.values()})
    result["messages"] = len(rows)
    return result
