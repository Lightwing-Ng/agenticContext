"""Upgrade complete contact caches without losing source events or review history.

Code version: v1.1.2-codex.0
"""

from contextlib import closing
import csv
import io
import json
from pathlib import Path
import sqlite3

from jinja2 import Environment, FileSystemLoader
import pytest

from app.core import wechat_contact_analysis as analysis
from app.web.presentation import format_wechat_source_clock
from app.web.wechat_contact_csv import contact_csv_chunks
from tests import test_wechat_contact_forwarded_copies as cases
from tests import test_wechat_statement_reviews_integration as review_integration


overlap_store = cases.overlap_store
reviewed_store = cases.reviewed_store
retained_review_store = cases.retained_review_store
projection_archive_store = cases.projection_archive_store


def _snapshot(path):
    with closing(sqlite3.connect(path)) as connection, connection:
        return {
            name: connection.execute("SELECT * FROM " + name + " ORDER BY 1").fetchall()
            for name in ("refs", "statement_groups", "statement_members")
        }


def test_complete_cache_upgrade_folds_table_and_export_without_author_rescan(
    projection_archive_store, monkeypatch
):
    f = projection_archive_store
    before = _snapshot(f.cache)
    raw_sources = f.history.read_bytes(), f.registry.read_bytes()
    review_integration._prevent_author_rescan(monkeypatch)

    page = analysis.query_contact_analysis(f.store, cases.CONTACT, sort="oldest")

    assert page["status"] == "ready"
    assert page["total_count"] == 3
    assert page["observation_count"] == 15
    assert page["counts"]["conversations"] == len(
        {row[2] for row in before["statement_groups"]}
    )
    assert page["forwarded_statement_copies_collapsed"] is True
    assert all(item["source_label"] == "iPad archive" for item in page["items"])
    assert all(item["original_conversation_unknown"] for item in page["items"])
    after = _snapshot(f.cache)
    assert before["refs"] == after["refs"]
    assert len(before["statement_groups"]) - len(after["statement_groups"]) == 6
    assert raw_sources == (f.history.read_bytes(), f.registry.read_bytes())
    exported = analysis.export_contact_analysis_items(
        f.store, cases.CONTACT, source_fingerprint=page["source_fingerprint"]
    )
    first = next(exported)
    rows = list(
        csv.reader(
            io.StringIO(
                "".join(contact_csv_chunks(exported, first=first)).lstrip("\ufeff")
            )
        )
    )
    assert rows[0] == ["Time", "Statement"]
    assert len(rows) == 4
    occurrences = list(
        analysis.export_contact_analysis_items(
            f.store,
            cases.CONTACT,
            source_fingerprint=page["source_fingerprint"],
            source_occurrences=True,
        )
    )
    assert {row["id"] for row in occurrences} == {row[0] for row in before["refs"]}
    original_groups = dict(before["statement_members"])
    assert {item["id"]: item["statement_id"] for item in occurrences} == original_groups
    display_groups = dict(after["statement_members"])
    assert {
        item["id"]: item["display_statement_id"] for item in occurrences
    } == display_groups
    assert set(display_groups.values()).isdisjoint(original_groups.values())


def test_removing_contact_policy_restores_original_event_view(
    projection_archive_store, monkeypatch
):
    f = projection_archive_store
    baseline = _snapshot(f.cache)
    review_integration._prevent_author_rescan(monkeypatch)
    first = analysis.query_contact_analysis(f.store, cases.CONTACT)
    assert first["total_count"] == 3
    cases._policy_file(f.store).unlink()

    restored = analysis.query_contact_analysis(f.store, cases.CONTACT)

    assert restored["source_fingerprint"] != first["source_fingerprint"]
    assert restored["total_count"] == len(baseline["statement_groups"])
    assert _snapshot(f.cache) == baseline


def test_contact_projection_requires_an_explicit_policy(
    projection_archive_store, monkeypatch
):
    f = projection_archive_store
    baseline = _snapshot(f.cache)
    cases._policy_file(f.store).unlink()
    review_integration._prevent_author_rescan(monkeypatch)

    page = analysis.query_contact_analysis(f.store, cases.CONTACT)

    assert page["total_count"] == len(baseline["statement_groups"])
    assert page["forwarded_statement_copies_collapsed"] is False
    assert _snapshot(f.cache) == baseline


def test_rule_upgrade_restores_then_reproves_the_complete_projection(
    projection_archive_store, monkeypatch
):
    f = projection_archive_store
    review_integration._prevent_author_rescan(monkeypatch)
    first = analysis.query_contact_analysis(f.store, cases.CONTACT)
    snapshot = _snapshot(f.cache)
    monkeypatch.setattr(
        analysis, "FORWARDED_COPY_PROJECTION_VERSION", "synthetic-next-rule"
    )

    upgraded = analysis.query_contact_analysis(f.store, cases.CONTACT)

    assert upgraded["source_fingerprint"] != first["source_fingerprint"]
    assert upgraded["total_count"] == 3
    assert _snapshot(f.cache) == snapshot
    with closing(sqlite3.connect(f.cache)) as connection, connection:
        evidence = connection.execute(
            "SELECT basis,COUNT(*) FROM forwarded_copy_projection_evidence GROUP BY basis"
        ).fetchall()
        info = dict(connection.execute("SELECT key,value FROM info"))
    assert evidence == [("contact_forwarded_statement_projection", 15)]
    assert json.loads(info["forwarded_copy_projection"])["status"] == "applied"


def test_disabling_display_rule_restores_every_pre_projection_reference_group(
    projection_archive_store, monkeypatch
):
    f = projection_archive_store
    baseline = _snapshot(f.cache)
    review_integration._prevent_author_rescan(monkeypatch)
    assert analysis.query_contact_analysis(f.store, cases.CONTACT)["total_count"] == 3
    monkeypatch.setattr(
        analysis, "FORWARDED_COPY_PROJECTION_VERSION", "synthetic-disabled-rule"
    )
    monkeypatch.setattr(
        analysis,
        "fold_contact_forwarded_copies",
        lambda *_args, **_kwargs: {"status": "no_matches", "statements_joined": 0},
    )

    restored = analysis.query_contact_analysis(f.store, cases.CONTACT)

    assert restored["total_count"] == len(baseline["statement_groups"])
    assert restored["forwarded_statement_copies_collapsed"] is False
    assert _snapshot(f.cache) == baseline


@pytest.mark.parametrize("collapsed", [False, True])
def test_display_omits_duplicate_copy_note(
    collapsed,
):
    templates = Path(__file__).resolve().parents[1] / "app" / "web" / "templates"
    environment = Environment(loader=FileSystemLoader(templates), autoescape=True)
    environment.filters["wechat_source_clock"] = format_wechat_source_clock
    rendered = environment.get_template("_wechat_contact_analysis_content.html").render(
        analysis={
            "status": "ready",
            "contact": {"id": "synthetic-contact"},
            "total_count": 0,
            "conversation_count": 0,
            "account_count": 0,
            "items": [],
            "forwarded_statement_copies_collapsed": collapsed,
        },
        contact_filters={"q": "", "sort": "oldest"},
        return_url="",
        pagination_items=[],
        url_for=lambda *_args, **_kwargs: "/synthetic",
    )
    assert "Verified copies of the same" not in rendered
    assert "wechat-contact-source-note" not in rendered
