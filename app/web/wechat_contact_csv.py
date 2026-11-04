"""Stream contact statements as UTF-8 CSV without an on-disk export.

Code version: v2.3.0-codex.0
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
    """Export distinct wall-clock/body pairs without altering source-event identity."""
    writer = csv.writer(_CsvSink())
    seen = set()
    try:
        yield "\ufeff" + writer.writerow(_COLUMNS)
        for item in chain(() if first is None else (first,), items):
            # The compact contact file omits the optional zone. Native offsets
            # and unresolved source clocks remain independent in the archive.
            clock = format_csv_time(item.get("timestamp_value"))[:19]
            row = (clock, item["content_text"])
            if row not in seen:
                seen.add(row)
                yield writer.writerow(row)
    finally:
        items.close()
