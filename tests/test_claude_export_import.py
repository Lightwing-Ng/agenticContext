"""Tests for importing an official Claude data export.

Code version: v1.0.0-claude.0
"""

import json
import zipfile
from pathlib import Path

from app.core.claude_export_import import import_claude_export
from app.core.claude_history import ClaudeConversationLink, ClaudeHistoryStore, claude_history_path


def _exported_conversation() -> dict:
    """Return an export entry without a current leaf, including an abandoned edit."""

    return {
        "uuid": "chat-1",
        "name": "Exported chat",
        "updated_at": "2026-09-03T01:02:05Z",
        "chat_messages": [
            {"uuid": "m1", "parent_message_uuid": "root", "sender": "human",
             "created_at": "2026-09-03T01:00:00Z", "text": "", "attachments": [
                 {"file_name": "", "extracted_content": "Pasted notes"},
                 {"file_name": "image.png", "extracted_content": ""},
             ], "content": [{"type": "text", "text": "Please read this"}]},
            {"uuid": "m2-old", "parent_message_uuid": "m1", "sender": "assistant",
             "created_at": "2026-09-03T01:00:05Z", "text": "",
             "content": [{"type": "text", "text": "Abandoned answer"}]},
            {"uuid": "m2", "parent_message_uuid": "m1", "sender": "assistant",
             "created_at": "2026-09-03T01:01:05Z", "text": "",
             "content": [{"type": "text", "text": "Kept answer"}]},
        ],
    }


def _write_export(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("conversations.json", json.dumps([_exported_conversation()]))
    return path


def test_claude_export_dry_run_reports_without_writing(tmp_path: Path) -> None:
    export = _write_export(tmp_path / "conversations-000.zip")

    result = import_claude_export(tmp_path, export)

    assert result["applied"] is False
    assert result["after"] == {"sessions": 1, "messages": 2}
    assert not claude_history_path(tmp_path).exists()
    assert not (tmp_path / "recovery").exists()


def test_claude_export_apply_keeps_other_sessions_and_backs_up_history(tmp_path: Path) -> None:
    history = claude_history_path(tmp_path)
    store = ClaudeHistoryStore(history)
    store.replace_conversation(
        ClaudeConversationLink("chat-0", "https://claude.ai/chat/chat-0", "Older"),
        [{"role": "user", "content_text": "Older question", "message_index": 0}],
        "2026-09-01T00:00:00Z",
    )
    store.save()
    original = history.read_bytes()
    export = _write_export(tmp_path / "conversations-000.zip")

    result = import_claude_export(tmp_path, export, apply=True)

    rows = ClaudeHistoryStore(history).rows
    imported = [row for row in rows if row["conversation_id"] == "chat-1"]
    assert result["applied"] is True
    assert Path(result["backup"]).read_bytes() == original
    assert {row["conversation_id"] for row in rows} == {"chat-0", "chat-1"}
    assert [row["content_text"] for row in imported] == [
        "Please read this\n\n---\n\nAttachment: pasted text\n\nPasted notes",
        "Kept answer",
    ]
    assert imported[0]["conversation_url"] == "https://claude.ai/chat/chat-1"
    assert imported[1]["conversation_title"] == "Exported chat"
