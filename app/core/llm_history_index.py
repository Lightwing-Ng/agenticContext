"""Build disposable browser summaries beside compressed LLM conversation groups."""

# Code version: v1.1.0-codex.0

from __future__ import annotations

from dataclasses import asdict
from typing import Any


def build_history_summary(rows: list[dict[str, Any]], source: str) -> dict[str, Any] | None:
    """Reuse the browser's exact projection without storing another transcript.

    Import at call time because persistence is also imported by provider adapters
    during application startup. The summary is derived state; full provider rows
    remain the authoritative compressed payload.
    """
    from .chat_history_browser import (
        _build_chat_history_sessions,
        _message_from_row,
        _timestamp_value,
    )

    messages = tuple(
        message
        for row in rows
        if (message := _message_from_row(row, source)) is not None
    )
    if not messages:
        return None
    sessions = _build_chat_history_sessions(messages)
    if len(sessions) != 1:
        raise ValueError("A history index group must contain exactly one conversation.")
    observations: dict[str, tuple[float, str, str, int]] = {}
    if source == "chatgpt":
        for ordinal, message in enumerate(messages):
            observed = max(
                _timestamp_value(message.first_seen_at),
                _timestamp_value(message.last_seen_at),
            )
            previous = observations.get(message.conversation_url)
            if previous is None or observed >= previous[0]:
                observations[message.conversation_url] = (
                    observed, message.first_seen_at, message.last_seen_at, ordinal,
                )
    session = asdict(sessions[0])
    session["latest_message"] = ""
    return {
        "version": 2,
        "session": session,
        "project_observations": [
            {"url": url, "first_seen_at": value[1], "last_seen_at": value[2]}
            for url, value in sorted(observations.items(), key=lambda item: item[1][3])
        ],
    }
