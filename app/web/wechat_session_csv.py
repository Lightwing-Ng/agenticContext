"""Serialize source-scoped WeChat messages without an on-disk export.

Code version: v2.1.0-codex.0
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
    """Keep source parents and attach proved packet leaves in the same three columns."""
    writer = csv.writer(_CsvSink())
    items = export.items()
    try:
        yield "\ufeff" + writer.writerow(_COLUMNS)
        for item in items:
            # Source parents stay intact; proved packet leaves follow their container.
            for observation in (item, *item.get("forwarded_items", ())):
                yield writer.writerow(
                    (
                        format_csv_time(observation["timestamp_value"]),
                        speaker_csv_id(
                            observation.get("viewer_display_name")
                            or observation["sender_label"],
                            observation["sender_id"],
                        ),
                        observation["text"],
                    )
                )
    finally:
        items.close()
        export.close()
