"""Persist and bind one date display selection for every rendering surface."""

# Code version: v1.0.1-codex.0

from __future__ import annotations

import json
from dataclasses import asdict
from functools import partial
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock

from flask import Flask, current_app, g, has_request_context

from app.core.foundation import (
    COMPACT_FORMATS,
    EXAMPLE_DATE,
    FULL_FORMATS,
    DateDisplay,
    default_settings_path,
    format_date,
    format_datetime,
    format_time,
)


class DateDisplayStore:
    """Keep date preferences beside the existing local settings."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = Lock()

    def read(self) -> DateDisplay:
        defaults = DateDisplay()
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return defaults
        if not isinstance(saved, dict):
            return defaults
        full, compact = saved.get("full"), saved.get("compact")
        return DateDisplay(
            full if isinstance(full, str) and full in FULL_FORMATS else defaults.full,
            compact if isinstance(compact, str) and compact in COMPACT_FORMATS else defaults.compact,
        )

    def save(self, *, full: str | None, compact: str | None) -> None:
        with self._lock:
            current = self.read()
            updated = DateDisplay(full or current.full, compact or current.compact)
            if current == updated:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.path.parent,
                    prefix=f".{self.path.name}.",
                    delete=False,
                ) as handle:
                    temporary = Path(handle.name)
                    json.dump(asdict(updated), handle)
                temporary.replace(self.path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)


def current_date_display() -> DateDisplay:
    """Use one preference snapshot for the server and the browser on this request."""
    if not has_request_context():
        return DateDisplay()
    if "date_display" not in g:
        store = current_app.extensions.get("date_display")
        g.date_display = store.read() if store is not None else DateDisplay()
    return g.date_display


def display_date(value, compact=False, **options):
    return format_date(value, style=current_date_display().style(compact), **options)


def display_datetime(value, compact=False, **options):
    return format_datetime(value, style=current_date_display().style(compact), **options)


def register_date_display(app: Flask) -> None:
    settings_path = default_settings_path()
    app.extensions["date_display"] = DateDisplayStore(settings_path.with_suffix(".date-display.json"))
    app.jinja_env.filters.update(
        display_date=display_date,
        display_datetime=display_datetime,
        display_time=format_time,
    )
    app.jinja_env.globals.update(
        current_date_display=current_date_display,
        display_date=display_date,
        display_datetime=display_datetime,
        display_time=format_time,
        date_format_choices=(
            ("full", "Full", FULL_FORMATS),
            ("compact", "Compact", COMPACT_FORMATS),
        ),
        date_format_example=partial(format_date, EXAMPLE_DATE),
    )
