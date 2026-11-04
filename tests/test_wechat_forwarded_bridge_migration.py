"""Upgrade compact source links without copying text or discarding observations.

Code version: v1.0.2-codex.0
"""

import sqlite3
from pathlib import Path
from unittest.mock import patch

from jinja2 import Environment, FileSystemLoader

from app.core import wechat_contact_analysis as analysis
from app.core import wechat_forwarded_origins as origins
from app.web.presentation import format_wechat_source_clock
from tests import test_wechat_forwarded_original_precedence as fixtures
from tests import test_wechat_html_native_forwarded_bridge as bridge_fixtures


CID = fixtures.CID
forwarded_origin_store = fixtures.forwarded_origin_store
native_html_store = fixtures.native_html_store
statement_store = fixtures.statement_store


def test_existing_reference_cache_adds_native_links_without_rescanning_sources(
    forwarded_origin_store, monkeypatch
):
    fixture = forwarded_origin_store

    def unknown_html_clocks(proof):
        for part in proof["forwarded_components"]:
            part["raw_timestamp"] = "1970-01-01 08:00:00"

    fixtures._update_proof(fixture, fixture.html_parent, unknown_html_clocks)
    before_sources = fixture.history.read_bytes(), fixture.registry.read_bytes()
    with (
        patch.object(analysis, "INDEX_VERSION", "v1.9.0-codex.0"),
        patch.object(origins, "_packet_bridge", return_value=None),
    ):
        previous = analysis.query_contact_analysis(fixture.store, CID)
    cache = analysis.contact_analysis_cache_path(fixture.store, CID)
    with sqlite3.connect(cache) as connection:
        before_refs = connection.execute("SELECT * FROM refs ORDER BY id").fetchall()
        before_members = connection.execute(
            "SELECT reference_id FROM statement_members ORDER BY reference_id"
        ).fetchall()

    def reject_full_rescan(*args, **kwargs):
        raise AssertionError("A compatible reference cache must not rescan sources.")

    monkeypatch.setattr(analysis, "_candidate_batches", reject_full_rescan)
    monkeypatch.setattr(analysis, "_candidate_forwarded_batches", reject_full_rescan)
    monkeypatch.setattr(analysis, "_candidate_native_batches", reject_full_rescan)
    # Revalidating retained references is allowed; scanning authors or rebuilding
    # their base statement groups is not part of a compatible cache upgrade.
    monkeypatch.setattr(analysis, "_group_statements", reject_full_rescan)
    current = analysis.query_contact_analysis(fixture.store, CID)
    assert current["total_count"] == 6 < previous["total_count"]
    assert current["observation_count"] == previous["observation_count"] == 18
    assert {item["id"] for item in current["items"]} == set(fixture.original_ids)
    with sqlite3.connect(cache) as connection:
        assert (
            connection.execute("SELECT * FROM refs ORDER BY id").fetchall()
            == before_refs
        )
        assert (
            connection.execute(
                "SELECT reference_id FROM statement_members ORDER BY reference_id"
            ).fetchall()
            == before_members
        )
        assert not {"text", "body", "provenance"}.intersection(
            row[1] for row in connection.execute("PRAGMA table_info(refs)")
        )
        groups = connection.execute(
            "SELECT * FROM statement_groups ORDER BY id"
        ).fetchall()
        members = connection.execute(
            "SELECT * FROM statement_members ORDER BY reference_id"
        ).fetchall()
    repeated = analysis.query_contact_analysis(fixture.store, CID)
    assert repeated["total_count"] == current["total_count"]
    with sqlite3.connect(cache) as connection:
        assert (
            connection.execute("SELECT * FROM statement_groups ORDER BY id").fetchall()
            == groups
        )
        assert (
            connection.execute(
                "SELECT * FROM statement_members ORDER BY reference_id"
            ).fetchall()
            == members
        )
    assert before_sources == (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
    )


def test_linked_forward_keeps_sources_reachable_and_unknown_timezone_explicit(
    forwarded_origin_store,
):
    fixture = forwarded_origin_store
    bridge_fixtures._native_naive_clocks(fixture)
    bridge_fixtures._missing_html_clocks(fixture, "1970-01-01 08:00:00")
    bridge_fixtures._remove_originals(fixture)
    result = analysis.query_contact_analysis(fixture.store, CID)
    for item in result["items"]:
        item["source_url"] = "/native/" + item["id"]
        for observation in item["observations"]:
            observation["source_url"] = "/observation/" + observation["id"]
    templates = Path(__file__).resolve().parents[1] / "app/web/templates"
    environment = Environment(loader=FileSystemLoader(templates), autoescape=True)
    environment.filters["wechat_source_clock"] = format_wechat_source_clock
    rendered = environment.get_template("_wechat_contact_analysis_content.html").render(
        analysis={
            **result,
            "conversation_count": result["counts"]["conversations"],
            "account_count": result["counts"]["source_accounts"],
        },
        contact_filters={"q": "", "sort": "newest"},
        return_url="",
        pagination_items=[],
        url_for=lambda *_args, **_kwargs: "/synthetic",
    )
    assert rendered.count("data-wechat-contact-timezone-unknown") == 6
    assert rendered.count('aria-label="Show original sources"') == 6
    assert rendered.count("data-wechat-contact-observation=") == 12
    assert "Chat history evidence" not in rendered
    assert "Original conversation unknown" not in rendered
    assert all(item["original_conversation_unknown"] for item in result["items"])
    assert rendered.count('class="wechat-conversation-kind"') == 6
    assert "Source clock; timezone unknown" in rendered
    for item in result["items"]:
        assert 'href="/native/' + item["id"] + '"' in rendered
        assert '<time datetime="' + item["timestamp_value"] + '"' in rendered
        for observation in item["observations"]:
            assert 'href="/observation/' + observation["id"] + '"' in rendered
