"""Stream contact statements as UTF-8 CSV without an on-disk export.

Code version: v2.1.0-codex.0
"""

from __future__ import annotations

import csv
from itertools import chain

from .wechat_csv import format_csv_time

_COLUMNS = ("Time", "Statement")


class _CsvSink:
    def write(self, value):
        return value


def contact_csv_filename(contact):
    """Use a bounded Unicode name with a stable identity suffix."""
    name = "".join(
        character if character.isalnum() or character in {"-", "_", " "} else "_"
        for character in contact["display_name"]
    ).strip()[:80]
    return f"{name or 'contact'}-{contact['id'][-12:]}.csv"


def contact_csv_chunks(items, *, first, contact=None, source_url=None):
    """Export a standard clock and original statement in the current order."""
    writer = csv.writer(_CsvSink())
    try:
        yield "\ufeff" + writer.writerow(_COLUMNS)
        for item in chain(() if first is None else (first,), items):
            yield writer.writerow(
                (format_csv_time(item.get("timestamp_value")), item["content_text"])
            )
    finally:
        items.close()
