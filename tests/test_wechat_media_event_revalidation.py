"""Revalidate metadata-only timeline candidates without weakening normal reads.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace

import pytest

from app.core.wechat_archive import _file_identity
from app.core import wechat_media_event_revalidation as revalidation
from tests import test_wechat_media_event_global_timeline as fixtures


global_media_store = fixtures.global_media_store
placeholder_store = fixtures.placeholder_store


@pytest.fixture
def stale_media(global_media_store, monkeypatch):
    fixture = global_media_store
    with closing(sqlite3.connect(fixture.history)) as history, history:
        history.execute("INSERT INTO archive_info VALUES('unrelated_revision','new')")
    monkeypatch.setattr(revalidation, "_clonefile", shutil.copyfile)
    return fixture


def test_staged_revalidation_restores_exact_events_without_publishing_or_source_changes(
    stale_media,
):
    fixture = stale_media
    before = fixture.history.read_bytes(), fixture.index.read_bytes()
    connection, result = fixtures._attach(fixture, legacy=False)
    try:
        assert result[-1] == "stale"
    finally:
        connection.close()
    candidate = fixture.index.with_name("verified-candidate.sqlite3")
    report = revalidation.stage_media_event_revalidation(fixture.store, candidate)
    assert report["events"] == 2160
    assert report["conversations"] == 3
    assert report["published"] is False
    assert report["media_payload_files_read"] == report["cloud_inputs_hydrated"] == 0
    assert (fixture.history.read_bytes(), fixture.index.read_bytes()) == before
    with closing(sqlite3.connect(candidate)) as output, output, closing(sqlite3.connect(fixture.index)) as source, source:
        assert output.execute("SELECT * FROM messages ORDER BY id").fetchall() == source.execute(
            "SELECT * FROM messages ORDER BY id"
        ).fetchall()
        assert json.loads(dict(output.execute("SELECT * FROM archive_info"))["history_file_identity"]) == _file_identity(fixture.history)
    # Publication is explicit and separate from candidate construction.
    candidate.replace(fixture.index)
    connection, result = fixtures._attach(fixture, legacy=False)
    try:
        assert result[-1] == "ready"
        assert connection.execute(f"SELECT COUNT(*) FROM {result[1]}").fetchone()[0] == 2214
    finally:
        connection.close()
    assert fixture.history.read_bytes() == before[0]


@pytest.mark.parametrize("defect", ["member", "snapshot", "account", "count", "orphan", "collision"])
def test_revalidation_refuses_broken_or_overlapping_source_evidence(stale_media, defect):
    fixture = stale_media
    with closing(sqlite3.connect(fixture.index)) as index, index:
        if defect == "member":
            index.execute("UPDATE conversation_proofs SET member_sha256=?", ("f" * 64,))
        elif defect == "snapshot":
            index.execute("UPDATE sources SET metadata_sha256=?", ("f" * 64,))
        elif defect == "account":
            index.execute("UPDATE conversations SET account_key='account:wrong'")
        elif defect == "count":
            index.execute("UPDATE conversations SET message_count=message_count+1")
        elif defect == "orphan":
            index.execute("UPDATE messages SET conversation_id='unverified' WHERE id=?", (fixture.specs[0].rows[0][0],))
    if defect == "collision":
        with closing(sqlite3.connect(fixture.history)) as history, history:
            history.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", fixture.specs[0].rows[0])
    before = fixture.history.read_bytes(), fixture.index.read_bytes()
    candidate = fixture.index.with_name("refused-candidate.sqlite3")
    with pytest.raises(ValueError):
        revalidation.stage_media_event_revalidation(fixture.store, candidate)
    assert not candidate.exists()
    assert (fixture.history.read_bytes(), fixture.index.read_bytes()) == before


def test_revalidation_does_not_open_a_cloud_only_metadata_input(stale_media, monkeypatch):
    fixture = stale_media
    original_lstat = Path.lstat
    original_connect = sqlite3.connect

    def cloud(path):
        state = original_lstat(path)
        if path == fixture.index:
            return SimpleNamespace(**{
                key: getattr(state, key)
                for key in dir(state)
                if key.startswith("st_") and key != "st_flags"
            }, st_flags=0x40000000)
        return state

    def no_open(*args, **kwargs):
        raise AssertionError("Cloud metadata must not be opened.")

    monkeypatch.setattr(Path, "lstat", cloud)
    monkeypatch.setattr(sqlite3, "connect", no_open)
    with pytest.raises(ValueError, match="resident"):
        revalidation.stage_media_event_revalidation(
            fixture.store, fixture.index.with_name("never-created.sqlite3")
        )
    monkeypatch.setattr(sqlite3, "connect", original_connect)


def test_revalidation_keeps_an_existing_candidate_unchanged(stale_media):
    fixture = stale_media
    candidate = fixture.index.with_name("keep-candidate.sqlite3")
    candidate.write_text("Keep existing review evidence.")
    with pytest.raises(FileExistsError):
        revalidation.stage_media_event_revalidation(fixture.store, candidate)
    assert candidate.read_text() == "Keep existing review evidence."
