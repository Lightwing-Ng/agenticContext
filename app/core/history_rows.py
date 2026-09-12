"""Pure row operations shared by formal text-history stores."""

# Code version: v1.0.0-codex.1

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def partition_conversation_rows(
    rows_by_key: Mapping[str, dict[str, Any]],
    conversation_id: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Split one conversation from rows that must remain in the store."""

    previous: dict[str, dict[str, Any]] = {}
    retained: dict[str, dict[str, Any]] = {}
    for key, row in rows_by_key.items():
        target = (
            previous
            if str(row.get("conversation_id") or "") == conversation_id
            else retained
        )
        target[key] = row
    return previous, retained


def history_rows_match(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
    field_names: Iterable[str],
) -> bool:
    """Return whether two rows match across every persisted schema field."""

    return previous is not None and all(
        previous.get(name) == current.get(name) for name in field_names
    )


def sort_history_rows(
    rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Order message rows deterministically by conversation and message position."""

    return sorted(
        rows,
        key=lambda row: (
            str(row.get("conversation_id") or ""),
            int(row.get("message_index") or 0),
        ),
    )
