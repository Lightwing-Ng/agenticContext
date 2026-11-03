"""Forwarded clocks preserve unknown times instead of promoting epoch placeholders.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from app.core.wechat_forwarded import (
    forwarded_component_clock,
    project_native_forwarded,
)
from tests.test_wechat_html_forwarded import _component, _evidence, _project


def _native(component):
    evidence = {
        "message_type": 49,
        "appmsg_subtype": 19,
        "text_extraction_kind": "forwarded_text",
        "source_time": "2026-10-30 21:22:23",
        "text_components": [component],
    }
    before = deepcopy(evidence)
    record = project_native_forwarded(
        evidence, parent_id="a" * 64, account_id="wxid_owner"
    )
    assert record is not None
    assert evidence == before
    return record["items"][0]


@pytest.mark.parametrize("raw", [
    "1970-01-01 08:00:00", "1970-1-1 8:00 AM",
    "1970-01-01T00:00:00Z", "0",
])
def test_html_unflagged_epoch_clock_stays_unknown_and_keeps_source_evidence(raw):
    evidence = _evidence(children=[_component(raw_timestamp=raw)])
    before = deepcopy(evidence)
    child = _project(evidence)["items"][0]
    assert child.first_seen_at == child.last_seen_at == ""
    assert child.messaging["timestamp_label"] == ""
    assert child.messaging["time_basis"] == "Unavailable source time"
    assert "possible_epoch_zero_placeholder" in child.messaging["quality_flags"]
    assert child.messaging["provenance"]["source_time"] == raw
    assert (
        child.messaging["provenance"]["embedded_html_forwarded_component"][
            "raw_timestamp"
        ] == raw
    )
    assert evidence == before


@pytest.mark.parametrize("raw", ["", "Unknown original time", "2021-04-09"])
def test_missing_html_original_time_never_uses_parent_clock(raw):
    evidence = _evidence(children=[_component(raw_timestamp=raw)])
    child = _project(evidence)["items"][0]
    assert evidence[0]["source_time"] == "2021-08-05 21:42:49"
    assert child.last_seen_at == ""
    assert child.messaging["provenance"]["source_time"] == raw
    assert child.messaging["time_basis"] == "Unavailable source time"


@pytest.mark.parametrize("raw", ["2019-11-03 21:07:53", "2019-11-03 9:07:53 PM"])
def test_html_genuine_original_clock_remains_naive(raw):
    component = _component(raw_timestamp=raw)
    before = deepcopy(component)
    timestamp, label, basis, flags = forwarded_component_clock(component)
    assert timestamp == "2019-11-03T21:07:53"
    assert label == "3 Nov 2019 21:07:53"
    assert basis == "Source clock; timezone unknown"
    assert "possible_epoch_zero_placeholder" not in flags
    assert component == before


@pytest.mark.parametrize("epoch", ["0", "0000000000", " 0 "])
@pytest.mark.parametrize("raw", ["1970-01-01 08:00:00", "2019-11-03 21:07:53", ""])
def test_native_zero_epoch_cannot_be_replaced_with_another_clock(epoch, raw):
    component = {
        "kind": "forwarded_text", "text": "Literal original body",
        "source_sender_id": "wxid_original", "source_create_time": epoch,
        "source_timestamp": raw,
    }
    child = _native(component)
    assert child.last_seen_at == ""
    assert child.messaging["time_basis"] == "Unavailable source time"
    assert "possible_epoch_zero_placeholder" in child.messaging["quality_flags"]
    assert child.content_text == component["text"]
    assert child.messaging["provenance"]["embedded_forwarded_component"] == component
    assert child.messaging["provenance"]["source_time"] == (raw or epoch)


@pytest.mark.parametrize("raw", [
    "1970-01-01 08:00:00", "1970-01-01T00:00:00Z", "1970-1-1 8:00 AM",
])
def test_native_raw_epoch_placeholder_without_epoch_is_unknown(raw):
    child = _native({
        "kind": "forwarded_text", "text": "Literal body", "source_timestamp": raw,
    })
    assert child.last_seen_at == ""
    assert child.messaging["provenance"]["source_time"] == raw
    assert "possible_epoch_zero_placeholder" in child.messaging["quality_flags"]


@pytest.mark.parametrize("epoch", ["1", "28800"])
def test_native_epoch_day_renderings_are_not_valid_original_wechat_times(epoch):
    child = _native({
        "kind": "forwarded_text", "text": "Literal body", "source_create_time": epoch,
    })
    assert child.last_seen_at == ""
    assert child.messaging["provenance"]["source_time"] == epoch
    assert "possible_epoch_zero_placeholder" in child.messaging["quality_flags"]


def test_native_missing_clock_does_not_inherit_wrapper_or_parent_time():
    child = _native({
        "kind": "forwarded_group", "text": "Nested literal body",
        "source_create_time": "1546300800",
        "text_components": [
            {"kind": "forwarded_text", "text": "Nested literal body"},
        ],
    })
    assert child.last_seen_at == "2019-01-01T08:00:00+08:00"
    inner = child.messaging["forwarded_record"]["items"][0]
    assert inner.first_seen_at == inner.last_seen_at == ""
    assert inner.messaging["timestamp_label"] == ""
    assert inner.messaging["time_basis"] == "Unavailable source time"
    assert inner.messaging["provenance"]["source_time"] == ""
    assert inner.messaging["peer_id"] == ""


def test_genuine_native_epoch_precedes_placeholder_rendering_without_rewriting_it():
    component = {
        "kind": "forwarded_text", "text": "Literal original body",
        "source_create_time": "1546300800", "source_timestamp": "1970-01-01 08:00:00",
    }
    child = _native(component)
    assert child.last_seen_at == "2019-01-01T08:00:00+08:00"
    assert child.messaging["time_basis"] == "UTC+08:00"
    assert child.messaging["provenance"]["source_time"] == "1970-01-01 08:00:00"
    assert "possible_epoch_zero_placeholder" not in child.messaging["quality_flags"]


@pytest.mark.parametrize("raw", [
    "2019-11-03T21:07:53+08:00", "2019-11-03T13:07:53Z",
])
def test_native_explicit_source_offset_is_retained_when_epoch_is_missing(raw):
    child = _native({
        "kind": "forwarded_text", "text": "Original body", "source_timestamp": raw,
    })
    assert child.last_seen_at == raw.replace("Z", "+00:00")
    assert child.messaging["provenance"]["source_time"] == raw


@pytest.mark.parametrize("native", [False, True])
def test_explicit_unknown_time_flag_blocks_otherwise_valid_source_clocks(native):
    component = {
        "flags": ["possible_epoch_zero_placeholder"],
        "raw_timestamp": "2019-11-03 21:07:53",
        "source_timestamp": "2019-11-03 21:07:53",
        "source_create_time": "1546300800",
    }
    before = deepcopy(component)
    timestamp, label, basis, flags = forwarded_component_clock(component, native=native)
    assert timestamp == label == ""
    assert basis == "Unavailable source time"
    assert flags == ("possible_epoch_zero_placeholder",)
    assert component == before
