"""Conservatively identify legacy ChatGPT tool traces and repair local history."""

# Code version: v1.1.0-codex.1

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from .config import LOCAL_STORE_ROOT
from .job_lock import CacheTaskLock
from .resource_persistence import CHATGPT_HISTORY_SCHEMA, read_parquet_rows, write_parquet_rows_atomic


def _is_web_tool_trace(text: str) -> bool:
    """Require every line to follow the compact web-tool protocol."""
    lines = text.splitlines()
    reference = r"(?:turn\d+[a-z]+\d+|https?://[^\s|]+)"
    commands = 0
    for index, line in enumerate(lines):
        line = line.strip()
        if re.fullmatch(r"(?:fast|slow)\|\S.*", line):
            commands += 1
        elif re.fullmatch(rf"open\|{reference}(?:\|\d+)?", line):
            commands += 1
        elif re.fullmatch(rf"find\|{reference}\|\S.*", line):
            commands += 1
        elif re.fullmatch(r"length\|(?:short|medium|long)", line) and index == len(lines) - 1:
            continue
        else:
            return False
    return commands > 0


def is_chatgpt_tool_trace(text: str) -> bool:
    """Recognize standalone tool syntax, never execute it or classify ordinary prose."""
    text = text.strip()
    if _is_web_tool_trace(text):
        return True
    if text.startswith("search("):
        try:
            call = ast.parse(text, mode="eval").body
        except (SyntaxError, ValueError, RecursionError):
            return False
        return (
            isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
            and call.func.id == "search" and len(call.args) == 1 and not call.keywords
            and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str)
        )
    if not text.startswith("{"):
        return False
    try:
        payload = json.loads(text)
    except (ValueError, RecursionError):
        return False
    if not isinstance(payload, dict):
        return False
    if set(payload) == {"path", "args"}:
        return (
            isinstance(payload["path"], str) and payload["path"].startswith("/connector_")
            and isinstance(payload["args"], dict)
        )
    return (
        bool(payload) and set(payload) <= {"search_query", "response_length"}
        and isinstance(payload.get("search_query"), list) and bool(payload["search_query"])
        and all(isinstance(item, dict) and isinstance(item.get("q"), str)
                for item in payload["search_query"])
    )


def plan_history_cleanup(rows: list[dict], reviewed_ids: set[str]) -> tuple[list[dict], list[dict]]:
    """Remove reviewed rows and legacy traces followed by a reply in the same turn."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[str(row["conversation_id"])].append(row)
    removed = []
    removal_keys = set()
    for group in groups.values():
        later_reply = False
        for row in sorted(group, key=lambda item: int(item["message_index"]), reverse=True):
            stable_id = "chat-" + hashlib.sha256(f"chatgpt:{row['message_key']}".encode()).hexdigest()[:24]
            if row["role"] != "assistant":
                later_reply = False
                continue
            trace = is_chatgpt_tool_trace(str(row.get("content_text") or ""))
            reviewed = stable_id in reviewed_ids
            legacy = int(row.get("schema_version") or 0) < 3
            if reviewed or (legacy and trace and later_reply):
                removal_keys.add(row["message_key"])
                removed.append({"stable_id": stable_id, "message_key": row["message_key"],
                                "reason": "reviewed" if reviewed else "legacy-tool-before-reply"})
            elif not trace:
                later_reply = True
    kept = [dict(row) for row in rows if row["message_key"] not in removal_keys]
    affected = {str(row["conversation_id"]) for row in rows if row["message_key"] in removal_keys}
    for conversation_id in affected:
        for index, row in enumerate(sorted((row for row in kept if row["conversation_id"] == conversation_id),
                                           key=lambda item: int(item["message_index"]))):
            row["message_index"] = index
    return kept, removed


def clean_history(root: Path, reviewed_ids: set[str], apply: bool = False) -> dict:
    """Back up and atomically repair history while excluding concurrent cache writers."""
    lock = CacheTaskLock(root / ".cache_task.lock")
    if not lock.acquire("chatgpt-history-cleanup"):
        raise RuntimeError("A cache task is running; history was not modified.")
    try:
        path = root / "llm" / "chatgpt" / "history.parquet"
        original = path.read_bytes()
        rows = read_parquet_rows(path)
        if rows is None:
            raise RuntimeError("ChatGPT history is unreadable; history was not modified.")
        kept, removed = plan_history_cleanup(rows, reviewed_ids)
        result = {"before": len(rows), "after": len(kept), "removed": len(removed), "applied": False}
        if apply and removed:
            if path.read_bytes() != original:
                raise RuntimeError("History changed during review; history was not modified.")
            backup_dir = root / "recovery" / ("chatgpt-history-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ"))
            backup_dir.mkdir(parents=True, exist_ok=False)
            backup = backup_dir / "history.parquet"
            backup.write_bytes(original)
            (backup_dir / "cleanup.json").write_text(json.dumps({**result, "rows": removed}, indent=2))
            write_parquet_rows_atomic(path, kept, CHATGPT_HISTORY_SCHEMA)
            result.update(applied=True, backup=str(backup))
        return result
    finally:
        lock.release()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=LOCAL_STORE_ROOT)
    parser.add_argument("--remove-message", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(clean_history(args.root, set(args.remove_message), args.apply), indent=2))


if __name__ == "__main__":
    main()
