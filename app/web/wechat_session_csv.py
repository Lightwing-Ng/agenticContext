"""Serialize source-scoped WeChat messages without an on-disk export.

Code version: v2.0.0-codex.0
"""

from __future__ import annotations

import csv

from .wechat_csv import format_csv_time, speaker_csv_id


_COLUMNS = ("Time", "Speaker ID", "Statement")


class _CsvSink:
    def write(self, value):
        return value


def session_csv_filename(conversation):
    """Bound the Unicode title and retain an unambiguous conversation suffix."""
    title = "".join(
        character if character.isalnum() or character in {"-", "_", " "} else "_"
        for character in conversation["title"]
    ).strip()[:80]
    return f"{title or 'conversation'}-{conversation['id'][-12:]}.csv"


def session_csv_chunks(export):
    """Export only the clock, viewing label with source ID, and original body."""
    writer = csv.writer(_CsvSink())
    items = export.items()
    try:
        yield "\ufeff" + writer.writerow(_COLUMNS)
        for item in items:
            yield writer.writerow(
                (
                    format_csv_time(item["timestamp_value"]),
                    speaker_csv_id(
                        item.get("viewer_display_name") or item["sender_label"],
                        item["sender_id"],
                    ),
                    item["text"],
                )
            )
    finally:
        items.close()
        export.close()
