"""Exported original clocks retain their literal value and source precision.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

from copy import deepcopy
import json

import pytest

from app.core.wechat_html_export import normalize_html_clock, parse_html_export
from app.core.wechat_export_merge import _record
from tests.test_wechat_export_merge import record as export_record
from tests.test_wechat_html_export import fixture_zip, message
from tests.test_wechat_html_forwarded import _component, _evidence, _project


@pytest.mark.parametrize(
    ("raw", "normalized", "precision"),
    [
        ("2020-1-8 1:03 PM", "2020-01-08T13:03:00", "minute"),
        ("2020-1-19 12:02 AM", "2020-01-19T00:02:00", "minute"),
        ("2020-1-19 12:02 PM", "2020-01-19T12:02:00", "minute"),
        ("2020-1-8 1:03:12 pm", "2020-01-08T13:03:12", "second"),
        ("2019-10-28 下午6:34", "2019-10-28T18:34:00", "minute"),
        ("2021-4-9 下午12:38", "2021-04-09T12:38:00", "minute"),
        ("2019-10-28 凌晨0:18", "2019-10-28T00:18:00", "minute"),
        ("2019-10-28 凌晨12:18", "2019-10-28T00:18:00", "minute"),
        ("2019-10-28 上午11:30", "2019-10-28T11:30:00", "minute"),
        ("2019-10-28 中午12:00", "2019-10-28T12:00:00", "minute"),
        ("2022-2-13 15:12", "2022-02-13T15:12:00", "minute"),
        ("2021-08-05 21:37:44", "2021-08-05T21:37:44", "second"),
    ],
)
def test_explicit_original_clock_formats(raw, normalized, precision):
    assert normalize_html_clock(raw) == (normalized, precision)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        None,
        "Unknown original time",
        "2021-08-05",
        "昨天 1:03 PM",
        "0",
        "1970-01-01 00:00:00",
        "1970-1-1 8:00 AM",
        "2021-02-29 13:03",
        "2021-8-5 25:03",
        "2021-8-5 13:60",
        "2021-8-5 13:03:60",
        "2021-8-5 0:03 PM",
        "2021-8-5 13:03 PM",
        "2021-8-5 下午0:03",
        "2021-8-5 下午18:03",
        "2021-8-5 下午6:03 AM",
        "2021-8-5 凌晨11:03",
        "2021-8-5 中午1:03",
    ],
)
def test_incomplete_invalid_or_epoch_placeholder_clocks_stay_unknown(raw):
    assert normalize_html_clock(raw) == ("", "")


def test_parser_retains_original_clock_and_marks_minute_precision(tmp_path):
    raw = "2020-1-8 1:03 PM"
    node = message().replace("2021-08-07 10:59:03", raw)
    record = parse_html_export(fixture_zip(tmp_path, nodes=[node]))["conversations"][0][
        "messages"
    ][0]
    assert record["raw_timestamp"] == raw
    assert "export_timestamp_precision_minutes" in record["flags"]
    assert "unrecognized_export_timestamp" not in record["flags"]


def test_parser_identifies_epoch_zero_placeholder(tmp_path):
    node = message().replace("2021-08-07 10:59:03", "1970-1-1 8:00 AM")
    record = parse_html_export(fixture_zip(tmp_path, nodes=[node]))["conversations"][0][
        "messages"
    ][0]
    assert "possible_epoch_zero_placeholder" in record["flags"]


@pytest.mark.parametrize("raw", ["2020-1-8 1:03 PM", "2020-1-8 下午1:03"])
def test_retained_legacy_component_recovers_its_own_clock_without_source_rewrite(raw):
    component = _component(
        raw_timestamp=raw,
        flags=["export_time_zone_unavailable", "unrecognized_export_timestamp"],
    )
    evidence = _evidence(children=[component])
    original = deepcopy(evidence)
    child = _project(evidence)["items"][0]
    assert child.last_seen_at == "2020-01-08T13:03:00"
    assert child.messaging["timestamp_label"] == "8 Jan 2020 13:03:00"
    assert child.messaging["time_basis"] == (
        "Source clock; timezone unknown; precision: minute (seconds unavailable)"
    )
    assert "export_timestamp_precision_minutes" in child.messaging["quality_flags"]
    assert "unrecognized_export_timestamp" not in child.messaging["quality_flags"]
    assert "recovered_html_source_clock" in child.messaging["quality_flags"]
    assert child.messaging["provenance"]["source_time"] == raw
    assert (
        child.messaging["provenance"]["embedded_html_forwarded_component"][
            "raw_timestamp"
        ]
        == raw
    )
    assert evidence == original


def test_explicit_epoch_placeholder_flag_blocks_clock_recovery():
    component = _component(
        raw_timestamp="2020-1-8 1:03 PM",
        flags=["possible_epoch_zero_placeholder"],
    )
    child = _project(_evidence(children=[component]))["items"][0]
    assert child.last_seen_at == ""
    assert child.messaging["time_basis"] == "Unavailable source time"


@pytest.mark.parametrize(
    ("clock", "timestamp", "minute_precision"),
    [
        ("2020-1-8 1:03 PM", "2020-01-08T13:03:00", True),
        ("2020-1-8 下午1:03", "2020-01-08T13:03:00", True),
        ("2020-01-08T13:03:12Z", "2020-01-08T13:03:12+00:00", False),
        ("2020-01-08T13:03:12+08:00", "2020-01-08T13:03:12+08:00", False),
    ],
)
def test_future_html_import_uses_its_own_clock_without_rewriting_raw_provenance(
    clock, timestamp, minute_precision
):
    raw = export_record(
        sent_at_raw=clock,
        provenance={"retained_metadata": "untouched", "source_time": "original field"},
    )
    before = deepcopy(raw)
    row = _record(raw, "html-fixture", 17, 1)[0]
    assert row[8] == timestamp
    assert ("precision: minute (seconds unavailable)" in row[10]) is minute_precision
    provenance = json.loads(row[11])
    assert provenance["source_time"] == clock
    assert provenance["raw_export_provenance"] == raw["provenance"]
    assert provenance["retained_metadata"] == "untouched"
    assert raw == before


@pytest.mark.parametrize(
    ("source", "clock", "flags"),
    [
        ("html-fixture", "Unknown original time", []),
        ("html-fixture", "2020-01-08", []),
        ("html-fixture", "1970-1-1 8:00 AM", []),
        ("html-fixture", "1970-01-01T00:00:00Z", []),
        ("html-fixture", "2020-1-8 1:03 PM", ["possible_epoch_zero_placeholder"]),
        ("xlsx-fixture", "2020-1-8 1:03 PM", []),
    ],
)
def test_future_import_does_not_invent_incomplete_epoch_or_non_html_clocks(
    source, clock, flags
):
    raw = export_record(sent_at_raw=clock, flags=flags)
    before = deepcopy(raw)
    row = _record(raw, source, 17, 1)[0]
    assert row[8] == ""
    assert row[10] == "Unavailable source time"
    assert json.loads(row[11])["source_time"] == clock
    assert raw == before
