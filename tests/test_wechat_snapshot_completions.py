"""Persisted overviews retain newly merged secondary completion semantics.

Code version: v1.0.0-codex.0
"""

from collections import OrderedDict
from pathlib import Path
import sqlite3

import pytest

from app.core import wechat_browser_cache as pages
from app.core import wechat_browser_snapshot as snapshots
from app.core import wechat_secondary_completions as completions
from app.core.wechat_archive import WechatArchiveRepository
from tests import test_wechat_secondary_completions as fixtures


mac_archive = fixtures.mac_archive
bank_archive = fixtures.bank_archive
source_binding = fixtures.source_binding
witness = fixtures.witness
completion_fixture = fixtures.completion_fixture


@pytest.fixture
def completion_overview(completion_fixture, monkeypatch):
    fixture = completion_fixture
    contacts = fixture["history"].with_name("contacts.sqlite3")
    with sqlite3.connect(contacts):
        pass
    repository = WechatArchiveRepository(fixture["root"])
    calls = []
    timeline = repository._timeline_tables

    def tracked_timeline(*args, **kwargs):
        calls.append(kwargs.get("strict", False))
        return timeline(*args, **kwargs)

    monkeypatch.setattr(repository, "_timeline_tables", tracked_timeline)
    monkeypatch.setattr(snapshots, "_SNAPSHOTS", OrderedDict())
    monkeypatch.setattr(snapshots, "_PENDING", {})
    monkeypatch.setattr(snapshots, "_code_fingerprint", lambda: "merged-secondary-reader-v1")
    monkeypatch.setattr(pages, "_PAGES", OrderedDict())
    monkeypatch.setattr(pages, "_PENDING", {})
    return fixture, repository, calls


def _conversation(fixture, result):
    return next(
        row for row in result["conversations"]
        if row["id"] == fixture["conversation"]["id"]
    )


def _publish(fixture, *, encoded=False):
    report = fixtures._stage(fixture)
    if encoded:
        report = completions.compact_secondary_message_completions(
            fixture["root"], fixture["candidate"],
            fixture["candidate"].with_name("encoded-snapshot-source.sqlite3"),
            candidate_sha256=report["candidate_sha256"],
        )
        fixture = {**fixture, "candidate": Path(report["candidate"])}
        with sqlite3.connect(fixture["candidate"]) as connection:
            assert connection.execute(
                "SELECT typeof(provenance) FROM messages WHERE id=?",
                (fixture["quote"]["id"],),
            ).fetchone()[0] == "blob"
    publication = fixtures._publish(fixture, report)
    assert publication["published"] is True


@pytest.mark.parametrize("encoded", [False, True])
def test_signed_disk_overview_keeps_secondary_counts_and_latest_text(
    completion_overview, encoded
):
    fixture, repository, calls = completion_overview
    _publish(fixture, encoded=encoded)
    expected = repository.query()
    conversation = _conversation(fixture, expected)
    assert conversation["message_count"] == 2
    assert conversation["latest_text"] == "Verified reply"
    assert calls == [True]
    snapshot = fixture["history"].parent / "browser_cache/conversations.snapshot"
    assert snapshot.is_file()

    # A fresh process must recover the verified summary without dropping the overlay.
    snapshots._SNAPSHOTS.clear()
    pages._PAGES.clear()
    assert repository.query() == expected
    assert calls == [True]


def test_completion_publication_and_removal_invalidate_warm_overviews(
    completion_overview,
):
    fixture, repository, calls = completion_overview
    original = repository.query()
    assert _conversation(fixture, original)["latest_text"] == "[App message]"
    assert calls == [True]

    _publish(fixture)
    completed = repository.query()
    assert _conversation(fixture, completed)["latest_text"] == "Verified reply"
    assert _conversation(fixture, completed)["message_count"] == 2
    assert calls == [True, True]

    completions.completion_path(fixture["root"]).unlink()
    restored = repository.query()
    assert restored == original


def test_invalid_completion_cannot_reuse_a_previously_verified_snapshot(
    completion_overview,
):
    fixture, repository, calls = completion_overview
    _publish(fixture)
    assert _conversation(fixture, repository.query())["latest_text"] == "Verified reply"
    target = completions.completion_path(fixture["root"])
    with sqlite3.connect(target) as connection:
        connection.execute(
            "UPDATE messages SET text='Unverified replacement' WHERE id=?",
            (fixture["quote"]["id"],),
        )
    result = repository.query()
    conversation = _conversation(fixture, result)
    assert result["resource_index_status"] == "invalid"
    assert conversation["latest_text"] == "[App message]"
    assert conversation["message_count"] == 2
    assert calls == [True, True, False]
