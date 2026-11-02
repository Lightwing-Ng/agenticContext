"""A stale contact clock index must recover from retained child evidence.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core.wechat_contacts import contact_registry_path
from tests import test_wechat_forwarded_contact_analysis as forwarded


forwarded_activity_store = forwarded.forwarded_activity_store


@pytest.mark.parametrize("previous_version", ["v1.1.1-codex.0", "v1.2.1-codex.0"])
def test_old_unknown_clock_cache_rebuilds_without_mutating_source(
    forwarded_activity_store, previous_version
):
    fixture = forwarded_activity_store
    component = forwarded._component(
        fixture,
        1,
        forwarded.FIRST,
        raw_timestamp="2019-10-28 下午6:34",
        flags=["unrecognized_export_timestamp"],
    )
    forwarded._publish_tree(fixture, [component])
    registry = contact_registry_path(fixture["store"])
    before = fixture["history"].read_bytes(), registry.read_bytes()
    with (
        patch.object(analysis, "INDEX_VERSION", previous_version),
        patch("app.core.wechat_forwarded.normalize_html_clock", return_value=("", "")),
    ):
        old = analysis.query_contact_analysis(fixture["store"], forwarded.CID)
    assert len(old["items"]) == 1
    assert old["items"][0]["timestamp_value"] == ""
    page = analysis.query_contact_analysis(fixture["store"], forwarded.CID)
    assert len(page["items"]) == 1
    recovered = page["items"][0]
    assert recovered["timestamp_value"] == "2019-10-28T18:34:00"
    assert recovered["timestamp_label"] == "28 Oct 2019 18:34:00"
    assert recovered["source_clock"] == "2019-10-28 下午6:34"
    assert recovered["content_text"] == forwarded.FIRST
    assert "precision: minute" in recovered["time_basis"]
    cache = analysis.contact_analysis_cache_path(fixture["store"], forwarded.CID)
    with sqlite3.connect(cache) as connection:
        assert dict(connection.execute("SELECT key,value FROM info"))["version"] == (
            analysis.INDEX_VERSION
        )
    assert before == (fixture["history"].read_bytes(), registry.read_bytes())
