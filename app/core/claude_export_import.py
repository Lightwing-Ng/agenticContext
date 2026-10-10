"""Import an official Claude data export into the local Claude history.

The export zip from claude.ai Settings > Privacy contains `conversations.json`. Each exported
conversation replaces the cached rows of the same session, using the same conversion as the
recorded-payload sync path. Sessions that are not in the export are kept.
"""

# Code version: v1.0.0-claude.0

from __future__ import annotations

import argparse
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .agent_session_sources import normalize_claude_conversation_url
from .claude_history import (
    ClaudeConversationLink,
    ClaudeHistoryStore,
    ClaudeNoCacheableMessagesError,
    claude_history_path,
    messages_from_claude_payload,
    utc_now_iso,
)
from .config import LOCAL_STORE_ROOT
from .job_lock import CACHE_TASK_LOCK_NAME, CacheMaintenanceLock, CacheTaskLock


CLAUDE_EXPORT_CONVERSATIONS_FILENAME = "conversations.json"


def read_claude_export_conversations(export_path: Path) -> list[dict[str, Any]]:
    """Read exported conversations from an export zip or an extracted JSON file."""

    if zipfile.is_zipfile(export_path):
        with zipfile.ZipFile(export_path) as archive:
            members = [
                name for name in archive.namelist()
                if Path(name).name == CLAUDE_EXPORT_CONVERSATIONS_FILENAME
            ]
            if len(members) != 1:
                raise RuntimeError(f"{export_path} does not contain exactly one {CLAUDE_EXPORT_CONVERSATIONS_FILENAME}.")
            payload = json.loads(archive.read(members[0]))
    else:
        payload = json.loads(export_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise RuntimeError("Claude export conversations must be a JSON list.")
    return [item for item in payload if isinstance(item, dict) and item.get("uuid")]


def import_claude_export(root: Path, export_path: Path, apply: bool = False) -> dict[str, Any]:
    """Merge one export into Claude history, backing up the previous file when applied."""

    conversations = read_claude_export_conversations(export_path)
    lock = CacheMaintenanceLock(CacheTaskLock(root / CACHE_TASK_LOCK_NAME), root)
    if not lock.acquire("claude-export-import"):
        raise RuntimeError("A cache task is running; history was not modified.")
    try:
        path = claude_history_path(root)
        original = path.read_bytes() if path.exists() else None
        store = ClaudeHistoryStore(path)
        before = {"sessions": store.cached_conversations, "messages": store.cached_messages}
        captured_at = utc_now_iso()
        imported: list[dict[str, Any]] = []
        skipped: list[str] = []
        added_or_changed = 0
        for conversation in conversations:
            conversation_id = str(conversation["uuid"])
            title = str(conversation.get("name") or "").strip() or f"Claude session {conversation_id}"
            link = ClaudeConversationLink(
                conversation_id=conversation_id,
                url=normalize_claude_conversation_url(f"https://claude.ai/chat/{conversation_id}"),
                title=title,
                updated_at=str(conversation.get("updated_at") or ""),
            )
            try:
                sync = store.replace_conversation(
                    link, messages_from_claude_payload(conversation, link), captured_at,
                )
            except ClaudeNoCacheableMessagesError:
                skipped.append(conversation_id)
                continue
            added_or_changed += sync.added_or_changed
            imported.append({"conversation_id": conversation_id, "title": title, "messages": sync.message_count})
        result: dict[str, Any] = {
            "export": str(export_path),
            "exported_sessions": len(conversations),
            "imported_sessions": len(imported),
            "skipped_sessions": skipped,
            "added_or_changed_messages": added_or_changed,
            "before": before,
            "after": {"sessions": store.cached_conversations, "messages": store.cached_messages},
            "applied": False,
        }
        if apply:
            if (path.read_bytes() if path.exists() else None) != original:
                raise RuntimeError("History changed during review; history was not modified.")
            backup_dir = root / "recovery" / ("claude-export-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ"))
            backup_dir.mkdir(parents=True, exist_ok=False)
            if original is not None:
                backup = backup_dir / "history.parquet"
                backup.write_bytes(original)
                result["backup"] = str(backup)
            store.save()
            # The manifest is written after the replacement so it records the outcome that happened.
            result["applied"] = True
            (backup_dir / "import.json").write_text(
                json.dumps({**result, "sessions": imported}, indent=2, ensure_ascii=False),
            )
        return result
    finally:
        lock.release()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path, help="Export zip or extracted conversations.json.")
    parser.add_argument("--root", type=Path, default=LOCAL_STORE_ROOT)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(import_claude_export(args.root, args.export, args.apply), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
