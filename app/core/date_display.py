"""Shared date formats for application-owned display labels."""

# Code version: v1.0.0-codex.0

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, tzinfo
from zoneinfo import ZoneInfo


@dataclass(frozen=True, slots=True)
class DateFormat:
    label: str
    pattern: str


FULL_FORMATS = {
    "d_mmm_yyyy": DateFormat("D Mmm yyyy", "{d} {mmm} {yyyy}"),
    "dd_mmm_yyyy": DateFormat("DD Mmm yyyy", "{dd} {mmm} {yyyy}"),
    "yyyy_mmm_d": DateFormat("yyyy Mmm D", "{yyyy} {mmm} {d}"),
    "yyyy_mmm_dd": DateFormat("yyyy Mmm DD", "{yyyy} {mmm} {dd}"),
    "yyyy_mm_dd_cjk": DateFormat("yyyy年mm月dd日", "{yyyy}年{mm}月{dd}日"),
}
COMPACT_FORMATS = {
    "yyyy_mm_dd": DateFormat("yyyy/mm/dd", "{yyyy}/{mm}/{dd}"),
    "dd_mm_yyyy": DateFormat("dd/mm/yyyy", "{dd}/{mm}/{yyyy}"),
}
FORMATS = FULL_FORMATS | COMPACT_FORMATS
DEFAULT_FULL_STYLE = "d_mmm_yyyy"
DEFAULT_COMPACT_STYLE = "dd_mm_yyyy"
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
EXAMPLE_DATE = date(2026, 12, 8)


@dataclass(frozen=True, slots=True)
class DateDisplay:
    full: str = DEFAULT_FULL_STYLE
    compact: str = DEFAULT_COMPACT_STYLE

    def style(self, compact: bool = False) -> str:
        return self.compact if compact else self.full

    def browser_state(self) -> dict:
        return {
            "full": FULL_FORMATS[self.full].pattern,
            "compact": COMPACT_FORMATS[self.compact].pattern,
            "months": MONTHS,
        }


def _parse(value: object, time_zone: str | tzinfo | None) -> date | datetime | None:
    if isinstance(value, date):
        parsed = value
    elif isinstance(value, str):
        source = value.strip()
        try:
            parsed = date.fromisoformat(source)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(source)
            except ValueError:
                return None
    else:
        return None

    if isinstance(parsed, datetime) and time_zone is not None:
        zone = ZoneInfo(time_zone) if isinstance(time_zone, str) else time_zone
        parsed = (parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC))
        return parsed.astimezone(zone)
    return parsed


def _fallback(value: object, fallback: str | None) -> str:
    if fallback is not None:
        return fallback
    return value if isinstance(value, str) else ""


def _render_date(day: date, style: str) -> str:
    return FORMATS[style].pattern.format(
        d=day.day,
        dd=f"{day.day:02d}",
        mmm=MONTHS[day.month - 1],
        mm=f"{day.month:02d}",
        yyyy=f"{day.year:04d}",
    )


def _render_time(stamp: datetime, source: object, seconds: bool | None) -> str:
    if seconds is None:
        seconds = not isinstance(source, str) or source.strip()[16:17] == ":"
    clock = f"{stamp.hour:02d}:{stamp.minute:02d}"
    return f"{clock}:{stamp.second:02d}" if seconds else clock


def format_date(
    value: object,
    *,
    style: str = DEFAULT_FULL_STYLE,
    time_zone: str | tzinfo | None = None,
    fallback: str | None = None,
) -> str:
    """Format the calendar date, preserving literal ISO dates and the source's zone by default."""
    parsed = _parse(value, time_zone)
    return _render_date(parsed, style) if parsed is not None else _fallback(value, fallback)


def format_datetime(
    value: object,
    *,
    style: str = DEFAULT_FULL_STYLE,
    time_zone: str | tzinfo | None = None,
    seconds: bool | None = None,
    fallback: str | None = None,
) -> str:
    """Format a date and available clock with the selected date style."""
    parsed = _parse(value, time_zone)
    if parsed is None:
        return _fallback(value, fallback)
    label = _render_date(parsed, style)
    if isinstance(parsed, datetime):
        return f"{label} {_render_time(parsed, value, seconds)}"
    return label


def format_time(
    value: object,
    *,
    time_zone: str | tzinfo | None = None,
    seconds: bool | None = None,
    fallback: str | None = None,
) -> str:
    """Format the clock alone."""
    parsed = _parse(value, time_zone)
    if isinstance(parsed, datetime):
        return _render_time(parsed, value, seconds)
    return _fallback(value, fallback)
