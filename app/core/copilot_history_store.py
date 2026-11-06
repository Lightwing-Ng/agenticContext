"""Durable, isolated storage for Microsoft Copilot text conversations."""

# Code version: v1.3.0-codex.0

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import LOCAL_STORE_ROOT
from .copilot_legacy_catalog import read_copilot_legacy_catalog
from .copilot_sync_state import read_copilot_sync_state
from .history_rows import (
    history_rows_match,
    partition_conversation_rows,
    sort_history_rows,
)
from .resource_persistence import (
    COPILOT_HISTORY_FILENAME,
    COPILOT_HISTORY_SCHEMA,
    COPILOT_HISTORY_SCHEMA_VERSION,
    read_resource_rows,
    write_resource_rows_atomic,
)
from .state import TaskSnapshot, utc_now


def copilot_history_path(local_store_root: Path | str = LOCAL_STORE_ROOT) -> Path:
    """Return Copilot's canonical cache path, independent of every other provider."""

    return Path(local_store_root) / "llm" / "copilot" / COPILOT_HISTORY_FILENAME


def _text(value: Any) -> str:
    return value.replace("\x00", "").strip() if isinstance(value, str) else ""


def _position(value: Any, fallback: int) -> int:
    if value is None:
        return fallback
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("Copilot session contains an invalid message position.")
    try:
        position = int(value)
    except ValueError as exc:
        raise ValueError("Copilot session contains an invalid message position.") from exc
    if position < 0 or position > 2_147_483_647:
        raise ValueError("Copilot session contains an invalid message position.")
    return position


def _source_links(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for candidate in value:
        link = _text(candidate)
        try:
            parsed = urlsplit(link)
        except ValueError:
            continue
        if parsed.scheme in {"http", "https"} and parsed.netloc and link not in result:
            result.append(link)
    return result


class CopilotHistoryStore:
    """Replace complete sessions durably without discarding invalid or empty captures."""

    def __init__(self, local_store_root: Path | str = LOCAL_STORE_ROOT) -> None:
        self.path = copilot_history_path(local_store_root)
        rows = read_resource_rows(self.path)
        if (self.path.exists() or self.path.with_suffix(".parquet").exists()) and rows is None:
            raise RuntimeError(f"Copilot history store is unreadable: {self.path}")
        if any(row.get("platform") != "copilot" for row in rows or []):
            raise RuntimeError("Copilot history contains rows from an unexpected provider.")
        self._rows = {
            str(row["message_key"]): dict(row)
            for row in rows or []
            if row.get("message_key")
        }

    @property
    def rows(self) -> list[dict[str, Any]]:
        """Return detached rows in deterministic conversation and message order."""

        return [
            {**row, "source_links": list(row.get("source_links") or [])}
            for row in sort_history_rows(self._rows.values())
        ]

    @property
    def cached_messages(self) -> int:
        return len(self._rows)

    @property
    def cached_conversations(self) -> int:
        return len({str(row.get("conversation_id") or "") for row in self._rows.values()})

    def replace_conversation(
        self,
        conversation: Mapping[str, Any],
        messages: list[dict[str, Any]],
        captured_at: str | None = None,
    ) -> dict[str, int]:
        """Validate the entire capture, then atomically replace exactly one session.

        ``first_seen_at`` records local capture time. ``last_seen_at`` carries only
        an observed provider timestamp; unavailable message dates remain empty.
        """

        if not isinstance(conversation, Mapping):
            raise ValueError("Copilot session metadata is malformed; cached history was preserved.")
        conversation_id = _text(conversation.get("conversation_id"))
        if not conversation_id or conversation.get("complete") is False:
            raise ValueError("Copilot session is incomplete; cached history was preserved.")
        if not isinstance(messages, list) or not messages:
            raise ValueError("Copilot session exposed no cacheable text; cached history was preserved.")
        capture_time = _text(captured_at) or _text(conversation.get("captured_at")) or utc_now()
        previous, next_rows = partition_conversation_rows(self._rows, conversation_id)
        added_or_changed = 0
        unchanged_messages = 0
        seen_keys: set[str] = set()
        turn_index = 0
        for position, message in enumerate(messages):
            if not isinstance(message, Mapping):
                raise ValueError("Copilot session contains malformed messages; cached history was preserved.")
            role = _text(message.get("role")).lower()
            content_text = _text(message.get("content_text"))
            if role not in {"user", "assistant"} or not content_text:
                raise ValueError("Copilot session contains malformed messages; cached history was preserved.")
            message_index = _position(message.get("message_index"), position)
            turn_index += int(role == "user")
            provider_id = _text(message.get("message_id")) or _text(message.get("provider_message_id"))
            message_key = f"{conversation_id}:{provider_id or f'{message_index}:{role}'}"
            if message_key in seen_keys or (
                message_key in next_rows
                and next_rows[message_key].get("conversation_id") != conversation_id
            ):
                raise ValueError("Copilot session contains duplicate message identities; cached history was preserved.")
            seen_keys.add(message_key)
            prior = previous.get(message_key)
            source_timestamp = _text(message.get("message_timestamp"))
            row = {
                "schema_version": COPILOT_HISTORY_SCHEMA_VERSION,
                "platform": "copilot",
                "conversation_id": conversation_id,
                "conversation_url": _text(conversation.get("conversation_url")) or _text(conversation.get("url")),
                "conversation_title": _text(conversation.get("conversation_title")) or _text(conversation.get("title")),
                "message_key": message_key,
                "turn_index": _position(message.get("turn_index"), max(1, turn_index)),
                "message_index": message_index,
                "role": role,
                "author_label": _text(message.get("author_label")) or ("You" if role == "user" else "Copilot"),
                "content_text": content_text,
                "content_html": _text(message.get("content_html")),
                "content_sha256": hashlib.sha256(content_text.encode("utf-8")).hexdigest(),
                "source_links": _source_links(message.get("source_links")),
                "model_label": _text(message.get("model_label")),
                "first_seen_at": _text(prior.get("first_seen_at")) if prior else capture_time,
                "last_seen_at": source_timestamp or (_text(prior.get("last_seen_at")) if prior else ""),
            }
            if history_rows_match(prior, row, COPILOT_HISTORY_SCHEMA.names):
                unchanged_messages += 1
            else:
                added_or_changed += 1
            next_rows[message_key] = row
        unchanged_session = int(
            len(previous) == len(messages) and unchanged_messages == len(messages)
        )
        if not unchanged_session:
            write_resource_rows_atomic(
                self.path,
                sort_history_rows(next_rows.values()),
                COPILOT_HISTORY_SCHEMA,
            )
            self._rows = next_rows
        return {
            "message_count": len(messages),
            "added_or_changed": added_or_changed,
            "unchanged_messages": unchanged_messages,
            "unchanged_sessions": unchanged_session,
        }


def build_copilot_initial_snapshot(
    version: str,
    local_store_root: Path | str = LOCAL_STORE_ROOT,
) -> TaskSnapshot:
    """Hydrate Copilot counters from its own persisted history without creating files."""

    snapshot = TaskSnapshot(
        version=version,
        account_name="Microsoft Copilot",
        output_dir=str(copilot_history_path(local_store_root).parent),
        progress_unit="sessions",
        performance_metrics={"content_mode": "text"},
    )
    try:
        store = CopilotHistoryStore(local_store_root)
    except RuntimeError as exc:
        snapshot.last_error = str(exc)
        snapshot.message = snapshot.last_error
        return snapshot
    snapshot.downloaded_posts = store.cached_conversations
    snapshot.downloaded_tweets = store.cached_messages
    snapshot.discovered_images = store.cached_messages
    coverage = read_copilot_sync_state(local_store_root)
    legacy_catalog = read_copilot_legacy_catalog(local_store_root)
    if legacy_catalog["status"] != "missing":
        legacy_count = legacy_catalog["sessions"] or 0
        reason = (
            f"The preserved Edge legacy directory contains {legacy_count:,} sessions. "
            "Full legacy transcript coverage remains unverified."
            if legacy_catalog["status"] == "valid"
            else "The preserved Edge legacy directory cannot be verified. Full history coverage remains unverified."
        )
        coverage = {
            **coverage,
            "status": "incomplete" if coverage["status"] == "complete" else coverage["status"],
            "discovery_complete": False,
            "sync_complete": False,
            "expected_sessions": None,
            "discovered_sessions": max(coverage["discovered_sessions"], legacy_count),
            "completion_basis": reason,
            "reason": reason,
        }
    if coverage["status"] == "running":
        coverage = {
            **coverage,
            "status": "incomplete",
            "sync_complete": False,
            "reason": "The previous history sync was interrupted before completion was verified.",
        }
    elif (
        coverage["status"] == "complete"
        and coverage["sync_complete"]
        and store.cached_conversations != coverage["discovered_sessions"]
    ):
        coverage = {
            **coverage,
            "status": "incomplete",
            "sync_complete": False,
            "reason": (
                f"Cached history has {store.cached_conversations:,} sessions, but the last "
                f"verified sync recorded {coverage['discovered_sessions']:,}. "
                "Full history completeness is no longer verified."
            ),
        }
    snapshot.performance_metrics.update(coverage)
    snapshot.discovery_complete = coverage["discovery_complete"]
    snapshot.discovered_tweets = coverage["discovered_sessions"]
    snapshot.queued_tweets = coverage["discovered_sessions"]
    snapshot.processed_tweets = coverage["processed_sessions"]
    snapshot.failed_tweets = coverage["failed_sessions"]
    snapshot.started_at = coverage["started_at"]
    snapshot.finished_at = coverage["finished_at"]
    cache_summary = (
        f"Cached Microsoft Copilot history: {store.cached_conversations:,} sessions, "
        f"{store.cached_messages:,} messages."
    )
    if coverage["status"] == "complete" and coverage["sync_complete"]:
        snapshot.phase = "finished"
        snapshot.message = f"{cache_summary} Full history coverage was verified for the last sync."
    elif coverage["status"] == "stopped":
        snapshot.phase = "stopped"
        snapshot.message = f"{cache_summary} {coverage['reason'] or 'The last sync was stopped before completion.'}"
    elif coverage["status"] in {"incomplete", "failed", "invalid"}:
        snapshot.phase = "failed"
        snapshot.last_error = coverage["reason"] or "Full history completeness has not been verified."
        snapshot.message = f"{cache_summary} {snapshot.last_error}"
    else:
        snapshot.message = f"{cache_summary} {coverage['reason']}"
    return snapshot
