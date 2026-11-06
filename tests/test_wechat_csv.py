"""CSV clocks retain source offsets and never synthesize missing timestamps.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import pytest

from app.web.wechat_csv import format_csv_time, speaker_csv_id


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-11-01T12:34:56+08:00", "2026-11-01 12:34:56 UTC+08:00"),
        ("2026-11-01T12:34:56Z", "2026-11-01 12:34:56 UTC+00:00"),
        ("2026-11-01T12:34:56-04:00", "2026-11-01 12:34:56 UTC-04:00"),
        ("2026-11-01T12:34:56+05:30", "2026-11-01 12:34:56 UTC+05:30"),
        ("2026-11-01T12:34:56", "2026-11-01 12:34:56"),
        ("2026-11-01 12:34:56.987654", "2026-11-01 12:34:56"),
        ("0001-01-02T03:04:05+00:00", "0001-01-02 03:04:05 UTC+00:00"),
        (None, ""),
        ("", ""),
        ("Unknown time", ""),
        ("2026-11-01", ""),
        ("2026-11-01T12:34", ""),
        ("2026-02-30T12:34:56", ""),
        ("1 Nov 2026 12:34:56", ""),
    ],
)
def test_csv_time_uses_full_numeric_clock_and_retains_known_offset(value, expected):
    assert format_csv_time(value) == expected


@pytest.mark.parametrize(
    ("label", "identifier", "expected"),
    [
        ("备注", "wxid_sender", "备注 (wxid_sender)"),
        ("wxid_sender", "wxid_sender", "wxid_sender"),
        ("备注", None, "备注"),
        (None, "wxid_sender", "wxid_sender"),
        (None, None, ""),
        ('备注, "quoted"', "wxid_sender", '备注, "quoted" (wxid_sender)'),
    ],
)
def test_speaker_id_preserves_name_and_native_identifier(label, identifier, expected):
    assert speaker_csv_id(label, identifier) == expected
