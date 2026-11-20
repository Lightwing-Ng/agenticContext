"""Cache visible local Codex conversations without changing their source files.

Code version: v1.1.0-codex.0
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
from uuid import UUID

import pyarrow as pa

from .history_rows import history_rows_match, sort_history_rows
from .resource_persistence import (
    CLAUDE_HISTORY_SCHEMA,
    read_resource_rows,
    write_resource_rows_atomic,
)


CODEX_HISTORY_FILENAME = "history.sqlite3"
CODEX_HISTORY_SCHEMA_VERSION = 1
CODEX_HISTORY_SCHEMA = pa.schema([
    *CLAUDE_HISTORY_SCHEMA,
    pa.field("source_file", pa.string(), nullable=False),
    pa.field("source_line", pa.int64(), nullable=False),
    pa.field("source_kind", pa.string(), nullable=False),
])
_MAX_LINE_BYTES = 16 * 1024 * 1024
_ITEM_ID = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
_FILENAME_ID = re.compile(r"([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})$")
_FILENAME_IDS = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_RECORD_HEADER = re.compile(
    rb'^\s*\{\s*(?:(?:"timestamp"\s*:\s*"(?:[^"\\]|\\.)*"|'
    rb'"ordinal"\s*:\s*-?[0-9]+)\s*,\s*)*'
    rb'"type"\s*:\s*"(?P<kind>[a-z_]+)"'
)
_PAYLOAD_HEADER = re.compile(
    rb'^\s*,\s*(?:(?:"timestamp"\s*:\s*"(?:[^"\\]|\\.)*"|'
    rb'"ordinal"\s*:\s*-?[0-9]+)\s*,\s*)*'
    rb'"payload"\s*:\s*\{\s*"type"\s*:\s*"(?P<kind>[a-z_]+)"'
)
_EXCLUDED_RECORD_KINDS = {
    b"compacted", b"event_msg", b"world_state", b"token_usage_record",
    b"inter_agent_communication_metadata",
}
_EXCLUDED_ITEM_KINDS = {
    b"reasoning", b"function_call", b"function_call_output", b"custom_tool_call",
    b"custom_tool_call_output", b"agent_message", b"web_search_call",
    b"local_shell_call", b"image_generation_call", b"computer_call",
    b"computer_call_output",
}


def default_codex_directory() -> Path:
    """Use the configured local Codex home, including a custom CODEX_HOME."""
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def codex_history_path(local_store_root: Path | str) -> Path:
    """Keep local Codex messages separate from browser ChatGPT conversations."""
    return Path(local_store_root).expanduser() / "llm" / "codex" / CODEX_HISTORY_FILENAME


def _is_directory(path: Path) -> bool:
    absolute = path.absolute()
    return not any(parent.is_symlink() for parent in (absolute, *absolute.parents)) and path.is_dir()


def _files(directory: Path, *, levels: int) -> Iterator[Path]:
    """Read bounded rollout layouts without following symbolic links."""
    if not _is_directory(directory):
        return
    for root, directories, filenames in os.walk(directory, followlinks=False):
        parent = Path(root)
        depth = len(parent.relative_to(directory).parts)
        directories[:] = sorted(
            name for name in directories
            if depth < levels and not (parent / name).is_symlink()
        )
        for name in sorted(filenames):
            path = parent / name
            if name.startswith("rollout-") and path.suffix == ".jsonl" and (
                not path.is_symlink() and path.is_file()
            ):
                yield path


def _source_files(source: Path) -> tuple[list[tuple[Path, str]], list[Path], Path]:
    if not _is_directory(source):
        raise ValueError("Codex directory must be an existing directory, not a symbolic link.")
    if _is_directory(source / "sessions") or _is_directory(source / "archived_sessions"):
        home = source
        directories = [(source / "archived_sessions", "archive"), (source / "sessions", "session")]
    elif source.name == "sessions":
        home = source.parent
        directories = [(home / "archived_sessions", "archive"), (source, "session")]
    else:
        home = source.parent if source.name == "archived_sessions" else source
        kind = "archive" if source.name == "archived_sessions" else "session"
        directories = [(source, kind)]
    existing = [(directory, kind) for directory, kind in directories if _is_directory(directory)]
    files = [(path, kind) for directory, kind in existing for path in _files(directory, levels=3)]
    if not files and not any(directory.name in {"sessions", "archived_sessions"} for directory, _ in existing):
        raise ValueError(
            "Choose the Codex home, sessions, archived_sessions, or a date folder containing rollout JSONL files."
        )
    return files, [directory for directory, _ in existing], home


def _uuid(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    try:
        return str(UUID(value))
    except ValueError:
        return ""


def _text(value: Any) -> str:
    return value.replace("\x00", "").strip() if isinstance(value, str) else ""


def _timestamp(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _excluded_record_prefix(prefix: bytes) -> bool:
    """Recognize excluded record headers without parsing large private payloads."""
    header = _RECORD_HEADER.match(prefix)
    if not header:
        return False
    kind = header.group("kind")
    if kind in _EXCLUDED_RECORD_KINDS:
        return True
    if kind == b"response_item":
        payload = _PAYLOAD_HEADER.match(prefix[header.end():])
        return bool(payload and payload.group("kind") in _EXCLUDED_ITEM_KINDS)
    return False


def _records(path: Path, should_stop: Callable[[], bool]) -> Iterator[tuple[int, dict | None]]:
    """Bound each record and expose malformed lines without their private contents."""
    if any(parent.is_symlink() for parent in (path, *path.absolute().parents)):
        raise OSError("Symbolic-link transcript paths are not supported.")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        line_index = 0
        while not should_stop() and (raw := stream.readline(_MAX_LINE_BYTES + 1)):
            line_index += 1
            excluded = _excluded_record_prefix(raw[:4096])
            if len(raw) > _MAX_LINE_BYTES:
                while raw and not raw.endswith(b"\n"):
                    raw = stream.readline(_MAX_LINE_BYTES + 1)
                if not excluded:
                    yield line_index, None
                continue
            if excluded:
                continue
            try:
                record = json.loads(raw)
            except (UnicodeError, ValueError):
                record = None
            yield line_index, record if isinstance(record, dict) else None


def _titles(home: Path, should_stop: Callable[[], bool]) -> dict[str, str]:
    """Read only explicitly indexed thread titles from the optional local sidecar."""
    path = home / "session_index.jsonl"
    titles: dict[str, str] = {}
    if not path.is_file() or path.is_symlink():
        return titles
    try:
        for _, record in _records(path, should_stop):
            if record and (identity := _uuid(record.get("id"))):
                title = _text(record.get("thread_name"))
                if title:
                    titles[identity] = title
    except OSError:
        pass
    return titles


def _message_text(message: dict[str, Any]) -> str:
    """Exclude reasoning, tool payloads, attachments, and generated instruction blocks."""
    role = message.get("role")
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    metadata = message.get("internal_chat_message_metadata_passthrough")
    kinds = metadata.get("content_item_kinds") if isinstance(metadata, dict) else None
    indexed_kinds = kinds if isinstance(kinds, list) and len(kinds) == len(content) else []
    parts = []
    for index, block in enumerate(content):
        if not isinstance(block, dict) or block.get("type") not in ("input_text", "output_text", "text"):
            continue
        if indexed_kinds and indexed_kinds[index] not in ("user.text", "unknown"):
            continue
        text = _text(block.get("text"))
        if role == "user" and (
            text.startswith("# AGENTS.md instructions for ")
            or text.startswith("<environment_context>")
        ):
            continue
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def _read_transcript(
    path: Path, source_kind: str, titles: dict[str, str], should_stop: Callable[[], bool],
) -> tuple[list[dict[str, Any]], bool, bool]:
    """Read canonical response items once, including visible commentary and final answers."""
    filename_match = _FILENAME_ID.search(path.stem)
    session_id = _uuid(filename_match.group(1)) if filename_match else ""
    filename_ids = {_uuid(identity) for identity in _FILENAME_IDS.findall(path.stem)}
    messages: dict[str, dict[str, Any]] = {}
    failed = False
    found_metadata = False
    model = ""
    title = ""
    turn_index = 0
    fallback_counts: dict[tuple[str, str], int] = {}
    try:
        for line_index, record in _records(path, should_stop):
            if record is None:
                failed = True
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            if record.get("type") == "session_meta":
                identity = _uuid(payload.get("id"))
                if not identity:
                    failed = True
                    continue
                if not session_id:
                    session_id = identity
                history_base = payload.get("history_base")
                if not found_metadata and len(filename_ids) > 1 and identity in filename_ids and (
                    isinstance(history_base, dict)
                    and _uuid(history_base.get("thread_id")) == identity
                    and all(type(history_base.get(field)) is int and history_base[field] >= 0 for field in (
                        "end_ordinal_exclusive", "end_byte_offset",
                    ))
                ):
                    session_id = identity
                if identity != session_id:
                    continue
                found_metadata = True
                origin = payload.get("source")
                if payload.get("parent_thread_id") or (
                    isinstance(origin, dict) and "subagent" in origin
                ):
                    return [], failed, False
                title = _text(payload.get("title") or payload.get("thread_name")) or title
                continue
            if record.get("type") == "turn_context":
                model = _text(payload.get("model")) or model
                continue
            if record.get("type") != "response_item" or payload.get("type") != "message":
                continue
            role = payload.get("role")
            if role not in ("user", "assistant") or any(
                payload.get(field) in ("analysis", "reasoning") for field in ("phase", "channel")
            ):
                continue
            content = _message_text(payload)
            if not content:
                continue
            if not session_id:
                failed = True
                continue
            timestamp = _timestamp(record.get("timestamp"))
            identity = _text(payload.get("id"))
            if not _ITEM_ID.fullmatch(identity):
                bucket = (role, timestamp)
                ordinal = fallback_counts.get(bucket, 0)
                fallback_counts[bucket] = ordinal + 1
                fallback = f"{role}:{timestamp}:{ordinal}" if timestamp else f"line:{line_index}:{role}"
                identity = "fallback-" + hashlib.sha256(fallback.encode("utf-8")).hexdigest()[:32]
            key = f"{session_id}:{identity}:{role}"
            prior = messages.get(key)
            if role == "user" and prior is None:
                turn_index += 1
            if prior and len(prior["content_text"]) > len(content) and prior["content_text"].startswith(content):
                continue
            messages[key] = {
                "schema_version": CODEX_HISTORY_SCHEMA_VERSION,
                "platform": "codex",
                "conversation_id": session_id,
                "conversation_url": "",
                "conversation_title": "",
                "message_key": key,
                "turn_index": prior["turn_index"] if prior else turn_index,
                "message_index": prior["message_index"] if prior else len(messages),
                "role": role,
                "author_label": "You" if role == "user" else "Codex",
                "content_text": content,
                "content_html": "",
                "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "source_links": [],
                "model_label": _text(payload.get("model")) or model,
                "first_seen_at": prior["first_seen_at"] if prior else timestamp,
                "last_seen_at": timestamp,
                "source_file": str(path),
                "source_line": line_index,
                "source_kind": source_kind,
            }
    except OSError:
        failed = True
    if not found_metadata:
        return [], True, should_stop()
    title = titles.get(session_id) or title or next(
        (row["content_text"].splitlines()[0][:120] for row in messages.values() if row["role"] == "user"),
        "Codex session",
    )
    for row in messages.values():
        row["conversation_title"] = title
    return list(messages.values()), failed, should_stop()


def import_codex_history(
    local_store_root: Path | str,
    source_directory: Path | str,
    *,
    apply: bool = True,
    should_stop: Callable[[], bool] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Merge active and archived sessions without deleting missing or partial history."""
    source = Path(source_directory).expanduser().absolute()
    files, directories, home = _source_files(source)
    cache_path = codex_history_path(local_store_root)
    previous_rows = read_resource_rows(cache_path)
    if (cache_path.exists() or cache_path.with_suffix(".parquet").exists()) and previous_rows is None:
        raise RuntimeError(f"Codex history store is unreadable: {cache_path}")
    previous = {row["message_key"]: row for row in previous_rows or [] if row.get("message_key")}
    for row in previous.values():
        row.setdefault("source_file", "")
        row.setdefault("source_line", 0)
        row.setdefault("source_kind", "unknown")
    rows = dict(previous)
    cached_sessions = {row["conversation_id"] for row in rows.values()}
    stop = should_stop or (lambda: False)
    titles = _titles(home, stop)
    captured_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    imported_keys: set[str] = set()
    imported_sessions: set[str] = set()
    changed_keys: set[str] = set()
    changed_session_counts: dict[str, int] = {}
    next_indexes: dict[str, int] = {}
    for row in rows.values():
        session_id = row["conversation_id"]
        next_indexes[session_id] = max(next_indexes.get(session_id, 0), int(row["message_index"]) + 1)
    result: dict[str, Any] = {
        "source_directory": str(source),
        "transcript_directory": str(directories[-1] if directories else source),
        "transcript_directories": [str(directory) for directory in directories],
        "archived_files": sum(kind == "archive" for _, kind in files),
        "files_scanned": 0,
        "files_total": len(files),
        "added_or_changed": 0,
        "unchanged_messages": 0,
        "unchanged_sessions": 0,
        "imported_sessions": 0,
        "imported_messages": 0,
        "failed": 0,
        "stopped": False,
        "applied": False,
    }

    def update_totals() -> None:
        result.update(
            added_or_changed=len(changed_keys),
            unchanged_messages=len(imported_keys - changed_keys),
            unchanged_sessions=len(imported_sessions) - len(changed_session_counts),
            imported_sessions=len(imported_sessions),
            imported_messages=len(imported_keys),
            sessions=len(cached_sessions),
            messages=len(rows),
        )

    for transcript, source_kind in files:
        if stop():
            result["stopped"] = True
            break
        messages, malformed, stopped = _read_transcript(transcript, source_kind, titles, stop)
        for row in messages:
            key = row["message_key"]
            prior = rows.get(key)
            imported_keys.add(key)
            imported_sessions.add(row["conversation_id"])
            cached_sessions.add(row["conversation_id"])
            if prior and len(prior["content_text"]) > len(row["content_text"]) and (
                prior["content_text"].startswith(row["content_text"])
            ):
                continue
            if prior:
                row["message_index"] = prior["message_index"]
                row["turn_index"] = prior["turn_index"]
                row["first_seen_at"] = str(prior.get("first_seen_at") or row["first_seen_at"] or captured_at)
            else:
                row["message_index"] = next_indexes.get(row["conversation_id"], 0)
                next_indexes[row["conversation_id"]] = row["message_index"] + 1
                row["first_seen_at"] = row["first_seen_at"] or captured_at
            row["last_seen_at"] = row["last_seen_at"] or row["first_seen_at"]
            rows[key] = row
            session_id = row["conversation_id"]
            if history_rows_match(previous.get(key), row, CODEX_HISTORY_SCHEMA.names):
                if key in changed_keys:
                    changed_keys.remove(key)
                    remaining = changed_session_counts[session_id] - 1
                    if remaining:
                        changed_session_counts[session_id] = remaining
                    else:
                        del changed_session_counts[session_id]
            elif key not in changed_keys:
                changed_keys.add(key)
                changed_session_counts[session_id] = changed_session_counts.get(session_id, 0) + 1
        result["files_scanned"] += 1
        result["failed"] += int(malformed)
        result["stopped"] = stopped
        update_totals()
        if on_progress:
            on_progress(dict(result))
        if stopped:
            break
    update_totals()
    if apply and result["added_or_changed"]:
        write_resource_rows_atomic(cache_path, sort_history_rows(rows.values()), CODEX_HISTORY_SCHEMA)
        result["applied"] = True
    return result
