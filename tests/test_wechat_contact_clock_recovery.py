"""A stale contact clock index must recover from retained child evidence.

Code version: v1.2.1-codex.0
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
from unittest.mock import patch

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core import wechat_forwarded as clock_projection
from app.core.wechat_contacts import contact_registry_path
from tests import test_wechat_contact_native_embedded as native
from tests import test_wechat_forwarded_contact_analysis as forwarded


forwarded_activity_store = forwarded.forwarded_activity_store
native_activity_store = native.native_activity_store


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


def _prepare_old_native_clock_cache(
    fixture,
    *,
    first_clock="2021-09-17 11:12:56",
    second_clock="2020-09-17 10:11:12",
    clock_version="v1.0.0-codex.0",
):
    proof = native._tree()
    first = proof["text_components"][0]
    first["source_timestamp"] = first_clock
    second = {
        **first,
        "text": "A second original statement with its own earlier clock",
        "source_timestamp": second_clock,
    }
    proof["text_components"].append(second)
    native._publish_native_forwarded_proof(fixture, proof)
    original_clock = clock_projection.forwarded_component_clock

    def legacy_clock(component, *, native=False):
        if (
            native
            and component.get("source_create_time") == "0"
            and component.get("source_timestamp")
            in {first["source_timestamp"], second["source_timestamp"]}
        ):
            return (
                "",
                "",
                "Unavailable source time",
                ("possible_epoch_zero_placeholder",),
            )
        return original_clock(component, native=native)

    with (
        patch.object(analysis, "FORWARDED_CLOCK_VERSION", clock_version),
        patch.object(clock_projection, "forwarded_component_clock", legacy_clock),
    ):
        old = analysis.query_contact_analysis(fixture.store, fixture.target_id)
    parent = fixture.parents["native-forward"]
    return old, parent + "_forwarded_1", parent + "_forwarded_5"


def _retained_reference_identity(cache):
    with closing(sqlite3.connect(cache)) as connection:
        return {
            "references": connection.execute(
                "SELECT id,parent_message_id,component_path,anchor_id,conversation_id,"
                "source_id,account_key,ordinal FROM refs ORDER BY id"
            ).fetchall(),
            "groups": connection.execute(
                "SELECT * FROM statement_groups ORDER BY id"
            ).fetchall(),
            "members": connection.execute(
                "SELECT * FROM statement_members ORDER BY reference_id"
            ).fetchall(),
        }


def test_current_complete_cache_recovers_native_clocks_without_source_or_group_rescan(
    native_activity_store, monkeypatch
):
    fixture = native_activity_store
    old, first_id, second_id = _prepare_old_native_clock_cache(fixture)
    old_items = {row["id"]: row for row in old["items"]}
    assert old_items[first_id]["timestamp_value"] == ""
    assert old_items[second_id]["timestamp_value"] == ""
    assert old_items[first_id]["source_clock"] == "2021-09-17 11:12:56"
    cache = analysis.contact_analysis_cache_path(fixture.store, fixture.target_id)
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    membership = _retained_reference_identity(cache)

    def no_rescan(*args, **kwargs):
        raise AssertionError(
            "An unchanged complete index must reuse its accepted references."
        )

    for name in (
        "_candidate_batches",
        "_candidate_forwarded_batches",
        "_candidate_native_batches",
        "_group_statements",
    ):
        monkeypatch.setattr(analysis, name, no_rescan)
    newest = analysis.query_contact_analysis(
        fixture.store, fixture.target_id, sort="newest"
    )
    items = {row["id"]: row for row in newest["items"]}
    assert items[first_id]["timestamp_value"] == "2021-09-17T11:12:56"
    assert items[first_id]["timestamp_label"] == "17 Sep 2021 11:12:56"
    assert items[first_id]["time_basis"] == "Source clock; timezone unknown"
    assert items[first_id]["source_clock"] == old_items[first_id]["source_clock"]
    assert items[first_id]["content_text"] == native.FIRST
    assert items[second_id]["timestamp_value"] == "2020-09-17T10:11:12"
    assert "recovered_native_source_clock" in items[first_id]["quality_flags"]
    assert "possible_epoch_zero_placeholder" not in items[first_id]["quality_flags"]
    order = [row["id"] for row in newest["items"]]
    assert order.index(first_id) < order.index(second_id)
    oldest = analysis.query_contact_analysis(
        fixture.store, fixture.target_id, sort="oldest"
    )
    order = [row["id"] for row in oldest["items"]]
    assert order.index(second_id) < order.index(first_id)
    unknown_ids = {
        row["id"]
        for row in old["items"]
        if not row["timestamp_value"] and row["id"] not in {first_id, second_id}
    }
    assert unknown_ids
    assert all(items[key]["timestamp_value"] == "" for key in unknown_ids)
    assert _retained_reference_identity(cache) == membership
    with sqlite3.connect(cache) as connection:
        info = dict(connection.execute("SELECT key,value FROM info"))
    assert info["version"] == analysis.INDEX_VERSION
    assert info["forwarded_clock_version"] == analysis.FORWARDED_CLOCK_VERSION
    assert newest["source_fingerprint"] != old["source_fingerprint"]
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def test_complete_cache_recovers_localized_native_minutes_without_rescanning(
    native_activity_store, monkeypatch
):
    fixture = native_activity_store
    old, first_id, second_id = _prepare_old_native_clock_cache(
        fixture,
        first_clock="2019-9-4 下午4:05",
        second_clock="2021-7-11 15:01",
        clock_version="v1.1.0-codex.0",
    )
    old_items = {row["id"]: row for row in old["items"]}
    assert old_items[first_id]["timestamp_value"] == ""
    assert old_items[second_id]["timestamp_value"] == ""
    cache = analysis.contact_analysis_cache_path(fixture.store, fixture.target_id)
    membership = _retained_reference_identity(cache)
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()

    def no_rescan(*args, **kwargs):
        raise AssertionError("Localized clock recovery must reuse accepted references.")

    for name in (
        "_candidate_batches",
        "_candidate_forwarded_batches",
        "_candidate_native_batches",
        "_group_statements",
    ):
        monkeypatch.setattr(analysis, name, no_rescan)
    page = analysis.query_contact_analysis(fixture.store, fixture.target_id)
    items = {row["id"]: row for row in page["items"]}
    for identifier, timestamp in (
        (first_id, "2019-09-04T16:05:00"),
        (second_id, "2021-07-11T15:01:00"),
    ):
        item = items[identifier]
        assert item["timestamp_value"] == timestamp
        assert item["source_clock"] == old_items[identifier]["source_clock"]
        assert item["time_basis"] == (
            "Source clock; timezone unknown; precision: minute (seconds unavailable)"
        )
        assert "native_timestamp_precision_minutes" in item["quality_flags"]
        assert "recovered_native_source_clock" in item["quality_flags"]
    assert _retained_reference_identity(cache) == membership
    assert page["source_fingerprint"] != old["source_fingerprint"]
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_key", "account:wxid_other_owner"),
        ("source_id", "ios-unrelated-source"),
        ("conversation_id", "wechat:unrelated-conversation"),
        ("parent_message_id", "f" * 64),
        ("component_path", '["native",99]'),
        ("anchor_id", "wechat_message_unrelated"),
    ],
)
def test_native_clock_upgrade_rejects_cross_source_or_changed_reference(
    native_activity_store, field, value
):
    fixture = native_activity_store
    _, first_id, _ = _prepare_old_native_clock_cache(fixture)
    cache = analysis.contact_analysis_cache_path(fixture.store, fixture.target_id)
    with sqlite3.connect(cache) as connection:
        connection.execute(f"UPDATE refs SET {field}=? WHERE id=?", (value, first_id))
    before = (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
        cache.read_bytes(),
    )
    with pytest.raises(analysis.ContactAnalysisSourceChanged):
        analysis.query_contact_analysis(fixture.store, fixture.target_id)
    assert before == (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
        cache.read_bytes(),
    )
    assert not list(cache.parent.glob(".contact-analysis-*.sqlite3"))
