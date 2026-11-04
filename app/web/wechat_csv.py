"""Shared compact WeChat CSV value formatting.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from datetime import datetime
import re


_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?"
)


def format_csv_time(timestamp_value):
    """Preserve the archived wall clock and its known offset, to whole seconds."""
    if not isinstance(timestamp_value, str):
        return ""
    value = timestamp_value.strip()
    if _TIMESTAMP.fullmatch(value) is None:
        return ""
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    formatted = (
        f"{stamp.year:04d}-{stamp.month:02d}-{stamp.day:02d} "
        f"{stamp.hour:02d}:{stamp.minute:02d}:{stamp.second:02d}"
    )
    offset = stamp.utcoffset()
    if offset is not None:
        minutes = int(offset.total_seconds() / 60)
        hours, remainder = divmod(abs(minutes), 60)
        formatted += f" UTC{'+' if minutes >= 0 else '-'}{hours:02d}:{remainder:02d}"
    return formatted


def speaker_csv_id(sender_label, sender_id):
    """Keep the viewing label and complete source identifier in one CSV cell."""
    label = str(sender_label or "").strip()
    identifier = str(sender_id or "").strip()
    if label and identifier and label != identifier:
        return f"{label} ({identifier})"
    return label or identifier
