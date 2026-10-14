"""Import local Claude Code transcripts without accessing browser or account state.

Code version: v1.0.0-claude-code.0
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

from .history_rows import history_rows_match, sort_history_rows
from .resource_persistence import (
    CLAUDE_HISTORY_FILENAME,
    CLAUDE_HISTORY_SCHEMA,
    CLAUDE_HISTORY_SCHEMA_VERSION,
    read_parquet_rows,
    write_parquet_rows_atomic,
)


_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
_MAX_LINE_BYTES = 16 * 1024 * 1024
_MAX_METADATA_BYTES = 2 * 1024 * 1024


def default_claude_code_directory() -> Path:
    """Reuse the application's platform-specific default source directory."""

    from .config import default_claude_code_directory as configured_directory

    return configured_directory()


def claude_code_history_path(local_store_root: Path | str) -> Path:
    """Return the separate typed cache for local Claude Code sessions."""

    return Path(local_store_root).expanduser() / "llm" / "claude_code" / CLAUDE_HISTORY_FILENAME


def _is_directory(path: Path) -> bool:
    absolute = path.absolute()
    return not any(parent.is_symlink() for parent in (absolute, *absolute.parents)) and path.is_dir()


def _files(directory: Path, suffix: str, *, levels: int) -> Iterator[Path]:
    """Visit a bounded directory layout without following symbolic links."""

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
            if path.suffix == suffix and not path.is_symlink() and path.is_file():
                yield path


def _identifier(value: Any) -> str:
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else ""


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


def _desktop_metadata(directory: Path) -> tuple[dict[str, dict[str, str]], int]:
    """Read only title, model, and transcript identity from desktop session records."""

    metadata: dict[str, dict[str, str]] = {}
    failed = 0
    for path in _files(directory, ".json", levels=2):
        if not path.name.startswith("local_"):
            continue
        try:
            if path.stat().st_size > _MAX_METADATA_BYTES:
                failed += 1
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            failed += 1
            continue
        if not isinstance(payload, dict):
            failed += 1
            continue
        session_id = _identifier(payload.get("cliSessionId"))
        if not session_id:
            failed += 1
            continue
        metadata[session_id] = {
            "title": _text(payload.get("title")),
            "model": _text(payload.get("model")),
        }
    return metadata, failed


def _source_files(
    source_directory: Path,
) -> tuple[Path, list[Path], dict[str, dict[str, str]], int]:
    """Resolve an explicit desktop, CLI home, projects, or single-project directory."""

    if not _is_directory(source_directory):
        raise ValueError("Claude Code directory must be an existing directory, not a symbolic link.")
    metadata_directory = (
        source_directory if source_directory.name == "claude-code-sessions"
        else source_directory / "claude-code-sessions"
    )
    if _is_directory(metadata_directory):
        metadata, failed = _desktop_metadata(metadata_directory)
        transcripts = Path.home() / ".claude" / "projects"
        if not _is_directory(transcripts):
            raise ValueError("Claude desktop session metadata requires local ~/.claude/projects transcripts.")
        paths = [
            path for path in _files(transcripts, ".jsonl", levels=1)
            if path.stem in metadata
        ]
        found = {path.stem for path in paths}
        return transcripts, paths, metadata, failed + len(metadata.keys() - found)
    if _is_directory(source_directory / "projects"):
        transcripts = source_directory / "projects"
        paths = list(_files(transcripts, ".jsonl", levels=1))
    elif source_directory.name == "projects":
        transcripts = source_directory
        paths = list(_files(transcripts, ".jsonl", levels=1))
    else:
        transcripts = source_directory
        paths = list(_files(transcripts, ".jsonl", levels=0))
        if not paths:
            raise ValueError(
                "Choose the Claude desktop directory, ~/.claude, ~/.claude/projects, "
                "or a project directory containing Claude Code JSONL transcripts."
            )
    return transcripts, [path for path in paths if _identifier(path.stem)], {}, 0


def _message_text(content: Any) -> str:
    """Keep visible text only; tool results, thinking, and attachments stay in the source."""

    if isinstance(content, str):
        return _text(content)
    if not isinstance(content, list):
        return ""
    return "\n\n".join(
        text for block in content
        if isinstance(block, dict) and block.get("type") == "text"
        if (text := _text(block.get("text")))
    )


def _read_transcript(
    path: Path, metadata: dict[str, str], should_stop: Callable[[], bool],
) -> tuple[list[dict[str, Any]], bool, bool]:
    """Parse visible main-session records while tolerating an unfinished final line."""

    messages: dict[str, dict[str, Any]] = {}
    title = metadata.get("title", "")
    failed = False
    stopped = False
    turn_index = 0
    try:
        with path.open("rb") as stream:
            line_index = 0
            while raw := stream.readline(_MAX_LINE_BYTES + 1):
                if should_stop():
                    stopped = True
                    break
                line_index += 1
                if len(raw) > _MAX_LINE_BYTES:
                    failed = True
                    while raw and not raw.endswith(b"\n"):
                        raw = stream.readline(_MAX_LINE_BYTES + 1)
                    continue
                try:
                    record = json.loads(raw)
                except (UnicodeError, ValueError):
                    failed = True
                    continue
                if not isinstance(record, dict):
                    failed = True
                    continue
                record_session = _identifier(record.get("sessionId"))
                if record_session and record_session != path.stem:
                    failed = True
                    continue
                if record.get("type") == "custom-title" and not metadata.get("title"):
                    title = _text(record.get("customTitle")) or title
                role = _text(record.get("type"))
                if role not in {"user", "assistant"} or any(
                    record.get(key) for key in ("isMeta", "isSidechain", "isCompactSummary")
                ):
                    continue
                message = record.get("message")
                if not isinstance(message, dict) or message.get("role", role) != role:
                    continue
                content = _message_text(message.get("content"))
                if not content:
                    continue
                timestamp = _timestamp(record.get("timestamp"))
                identity = _identifier(record.get("uuid"))
                if not identity:
                    identity = "fallback-" + hashlib.sha256(
                        f"{line_index}:{role}:{timestamp}:{content}".encode("utf-8")
                    ).hexdigest()[:32]
                key = f"{path.stem}:{identity}:{role}"
                if role == "user" and key not in messages:
                    turn_index += 1
                prior = messages.get(key)
                messages[key] = {
                    "schema_version": CLAUDE_HISTORY_SCHEMA_VERSION,
                    "platform": "claude_code",
                    "conversation_id": path.stem,
                    "conversation_url": "",
                    "conversation_title": "",
                    "message_key": key,
                    "turn_index": prior["turn_index"] if prior else turn_index,
                    "message_index": prior["message_index"] if prior else len(messages),
                    "role": role,
                    "author_label": "You" if role == "user" else "Claude Code",
                    "content_text": content,
                    "content_html": "",
                    "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "source_links": [],
                    "model_label": _text(message.get("model")) or metadata.get("model", ""),
                    "first_seen_at": timestamp,
                    "last_seen_at": timestamp,
                }
    except OSError:
        failed = True
    if not title:
        title = next(
            (row["content_text"].splitlines()[0][:120] for row in messages.values() if row["role"] == "user"),
            "Claude Code session",
        )
    for row in messages.values():
        row["conversation_title"] = title
    return list(messages.values()), failed, stopped


def import_claude_code_history(
    local_store_root: Path | str,
    source_directory: Path | str,
    *,
    apply: bool = True,
    should_stop: Callable[[], bool] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Add local Code messages without removing cached data missing from partial sources."""

    source = Path(source_directory).expanduser().absolute()
    transcripts, files, metadata, failed = _source_files(source)
    path = claude_code_history_path(local_store_root)
    previous = read_parquet_rows(path)
    if path.exists() and previous is None:
        raise RuntimeError(f"Claude Code history Parquet is unreadable: {path}")
    rows = {row["message_key"]: row for row in previous or [] if row.get("message_key")}
    stop = should_stop or (lambda: False)
    captured_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    result: dict[str, Any] = {
        "source_directory": str(source),
        "transcript_directory": str(transcripts),
        "files_scanned": 0,
        "files_total": len(files),
        "added_or_changed": 0,
        "unchanged_messages": 0,
        "unchanged_sessions": 0,
        "imported_sessions": 0,
        "imported_messages": 0,
        "failed": failed,
        "stopped": False,
        "applied": False,
    }
    for transcript in files:
        if stop():
            result["stopped"] = True
            break
        messages, malformed, stopped = _read_transcript(
            transcript, metadata.get(transcript.stem, {}), stop,
        )
        changed = 0
        next_index = 1 + max(
            (int(row["message_index"]) for row in rows.values() if row["conversation_id"] == transcript.stem),
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
            if prior and prior["content_text"].startswith(row["content_text"]) and (
                len(prior["content_text"]) > len(row["content_text"])
            ):
                result["unchanged_messages"] += 1
                continue
            row["first_seen_at"] = row["first_seen_at"] or (
                str(prior.get("first_seen_at") or captured_at) if prior else captured_at
            )
            if history_rows_match(prior, row, CLAUDE_HISTORY_SCHEMA.names):
                result["unchanged_messages"] += 1
            else:
                rows[row["message_key"]] = row
                changed += 1
        result["files_scanned"] += 1
        result["failed"] += int(malformed)
        result["added_or_changed"] += changed
        result["imported_sessions"] += int(bool(messages))
        result["imported_messages"] += len(messages)
        result["unchanged_sessions"] += int(bool(messages) and not changed)
        result["stopped"] = stopped
        if on_progress:
            on_progress(dict(result))
        if stopped:
            break
    if apply and result["added_or_changed"]:
        write_parquet_rows_atomic(path, sort_history_rows(rows.values()), CLAUDE_HISTORY_SCHEMA)
        result["applied"] = True
    result["sessions"] = len({row["conversation_id"] for row in rows.values()})
    result["messages"] = len(rows)
    return result
