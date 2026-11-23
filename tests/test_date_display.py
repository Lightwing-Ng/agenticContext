"""Date presentation and local preference persistence. Code version: v1.0.0-codex.0."""

from datetime import date

import pytest

from app.core.date_display import DateDisplay, format_date, format_datetime
from app.web.date_display import DateDisplayStore


@pytest.mark.parametrize(
    ("style", "label"),
    [
        ("d_mmm_yyyy", "8 Dec 2026"),
        ("dd_mmm_yyyy", "08 Dec 2026"),
        ("yyyy_mmm_d", "2026 Dec 8"),
        ("yyyy_mmm_dd", "2026 Dec 08"),
        ("yyyy_mm_dd_cjk", "2026年12月08日"),
        ("yyyy_mm_dd", "2026/12/08"),
        ("dd_mm_yyyy", "08/12/2026"),
    ],
)
def test_formats_calendar_dates_without_converting_date_only_values(style, label):
    assert format_date(date(2026, 12, 8), style=style) == label
    assert format_datetime("2026-12-08", style=style, time_zone="America/Los_Angeles") == label
    assert format_datetime("2026-12-08T07:02", style=style) == f"{label} 07:02"


def test_timestamp_zone_conversion_retains_precision_and_invalid_literals():
    stamp = "2026-12-07T16:02:03Z"
    assert format_datetime(stamp, time_zone="Asia/Hong_Kong") == "8 Dec 2026 00:02:03"
    assert format_datetime(stamp) == "7 Dec 2026 16:02:03"
    assert format_date("2026-02-30") == "2026-02-30"
    assert format_date(None, fallback="Unknown date") == "Unknown date"


def test_preference_can_be_reopened_and_updated_independently(tmp_path):
    path = tmp_path / "settings.date-display.json"
    store = DateDisplayStore(path)
    assert store.read() == DateDisplay()
    assert not path.exists()
    store.save(full="yyyy_mm_dd_cjk", compact=None)
    reopened = DateDisplayStore(path)
    reopened.save(full=None, compact="yyyy_mm_dd")
    assert store.read() == DateDisplay(full="yyyy_mm_dd_cjk", compact="yyyy_mm_dd")
    path.write_text('{"full": [], "compact": "yyyy_mm_dd"}', encoding="utf-8")
    assert store.read() == DateDisplay(compact="yyyy_mm_dd")


def test_settings_reject_invalid_date_format_without_writing(client, app):
    store = app.extensions["date_display"]
    previous = store.read()
    response = client.post("/settings/date-format", data={"full_date_format": "yyyy_mm_dd"})
    assert response.status_code == 400
    assert store.read() == previous
