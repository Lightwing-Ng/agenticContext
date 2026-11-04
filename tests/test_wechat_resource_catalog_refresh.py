"""Full native catalog refresh reuses source evidence without broadening reads.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from copy import deepcopy
import json
import sqlite3

import pytest

from app.core import wechat_resource_catalog_refresh as refresh
from app.core.wechat_archive import WechatArchiveRepository, _file_identity
from tests import test_wechat_native_resource_revalidation as fixtures


BACKUP = fixtures.BACKUP
native_metadata = fixtures.native_metadata


def _stage(fixture, **options):
    candidate = fixture.original.with_name("full-catalog-candidate.sqlite3")
    return candidate, refresh.stage_resource_catalog_refresh(
        fixture.store,
        candidate,
        **options,
    )


def test_full_candidate_reproves_events_and_preserves_both_inputs(native_metadata):
    fixture = native_metadata
    before = fixture.history.read_bytes(), fixture.original.read_bytes()
    updates = []
    candidate, report = _stage(fixture, progress=updates.append)
    assert report["checked_resources"] == 3
    assert report["messages"] == 2
    assert report["source_conversations"] == report["conversations"] == 1
    assert report["excluded_kinds"] == {"app": 1}
    assert report["kinds"] == {"file": 1, "image": 1}
    assert report["coverage"] == "existing_catalog_only"
    assert report["cloud_inputs_hydrated"] == report["media_payload_files_read"] == 0
    assert report["published"] is False
    assert updates[-1]["phase"] == "conversation"
    assert updates[-1]["checked_resources"] == 3
    with sqlite3.connect(candidate) as output:
        assert (
            output.execute("SELECT * FROM messages ORDER BY ordinal").fetchall()
            == fixture.resources[1:]
        )
        metadata = dict(output.execute("SELECT key,value FROM archive_info"))
        assert json.loads(metadata["history_file_identity"]) == _file_identity(
            fixture.history
        )
        assert (
            output.execute(
                "SELECT name FROM sqlite_master WHERE name='current_native_anchors'"
            ).fetchall()
            == []
        )
    assert (fixture.history.read_bytes(), fixture.original.read_bytes()) == before
    # Publication is a separate caller action against disposable test data.
    candidate.replace(fixture.original)
    page = WechatArchiveRepository(fixture.store).query(
        session=fixture.conversation["id"]
    )
    assert page["resource_index_status"] == "ready"
    assert page["count"] == 3
    assert fixture.history.read_bytes() == before[0]


@pytest.mark.parametrize("embedded_snapshot", [True, False])
def test_additional_native_evidence_prevents_duplicate_placeholder(
    native_metadata, embedded_snapshot
):
    fixture = native_metadata
    observation = deepcopy(json.loads(fixture.resources[1][11]))
    observation.update(source_snapshot=1, direction="received")
    if not embedded_snapshot:
        del observation["source_snapshot"]
    with sqlite3.connect(fixture.history) as primary:
        primary.execute(
            "CREATE TABLE message_evidence(id TEXT PRIMARY KEY,message_id TEXT,"
            "source_id TEXT,snapshot_id TEXT,extraction_record_id TEXT,"
            "observation TEXT,match_kind TEXT)"
        )
        primary.execute("CREATE INDEX evidence_message ON message_evidence(message_id)")
        primary.execute(
            "INSERT INTO message_evidence VALUES(?,?,?,?,?,?,?)",
            (
                "evidence",
                fixture.primary_id,
                fixture.conversation["partition_id"],
                BACKUP,
                "source-image",
                json.dumps(observation),
                "server_id",
            ),
        )
    candidate, report = _stage(fixture)
    assert report["messages"] == 1
    assert report["excluded_kinds"] == {"app": 1, "image": 1}
    with sqlite3.connect(candidate) as output:
        assert output.execute("SELECT id FROM messages").fetchall() == [
            (fixture.resources[2][0],)
        ]


def test_resource_only_conversation_retains_its_derived_source_scope(native_metadata):
    fixture = native_metadata
    with sqlite3.connect(fixture.history) as primary:
        primary.execute(
            "DELETE FROM messages WHERE conversation_id=?",
            (fixture.conversation["id"],),
        )
        primary.execute(
            "DELETE FROM conversations WHERE id=?", (fixture.conversation["id"],)
        )
    candidate, report = _stage(fixture)
    assert report["messages"] == 3
    assert report["excluded_existing_records"] == 0
    with sqlite3.connect(candidate) as output:
        assert output.execute(
            "SELECT id,message_count FROM conversations"
        ).fetchall() == [(fixture.conversation["id"], 3)]


@pytest.mark.parametrize(
    "damage", ["account", "receipt", "source_count", "event_conflict", "orphan"]
)
def test_invalid_source_or_current_event_evidence_refuses_candidate(
    native_metadata, damage
):
    fixture = native_metadata
    if damage == "account":
        with sqlite3.connect(fixture.original) as source:
            source.execute("UPDATE conversations SET account_key='account:other'")
    elif damage == "receipt":
        with sqlite3.connect(fixture.history) as primary:
            changed = deepcopy(fixture.metadata)
            changed["input_databases"][0]["sha256"] = "f" * 64
            primary.execute(
                "UPDATE source_snapshots SET metadata=?", (json.dumps(changed),)
            )
    elif damage == "source_count":
        with sqlite3.connect(fixture.original) as source:
            source.execute("UPDATE conversations SET message_count=message_count+1")
    elif damage == "event_conflict":
        with sqlite3.connect(fixture.history) as primary:
            raw = primary.execute(
                "SELECT provenance FROM messages WHERE id=?", (fixture.primary_id,)
            ).fetchone()[0]
            changed = json.loads(raw)
            changed["server_id"] = "999999"
            primary.execute(
                "UPDATE messages SET provenance=? WHERE id=?",
                (json.dumps(changed), fixture.primary_id),
            )
    elif damage == "orphan":
        with sqlite3.connect(fixture.original) as source:
            source.execute(
                "UPDATE messages SET conversation_id='unpublished' WHERE ordinal=2"
            )
    before = fixture.history.read_bytes(), fixture.original.read_bytes()
    candidate = fixture.original.with_name("full-catalog-candidate.sqlite3")
    with pytest.raises(ValueError):
        _stage(fixture)
    assert not candidate.exists()
    assert (fixture.history.read_bytes(), fixture.original.read_bytes()) == before


def test_empty_remainder_is_a_valid_fully_deduplicated_catalog(native_metadata):
    fixture = native_metadata
    with sqlite3.connect(fixture.history) as primary:
        for row in fixture.resources[1:]:
            values = list(row)
            proof = json.loads(values[11])
            proof["source_snapshot"] = 1
            values[11] = json.dumps(proof)
            primary.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values
            )
    candidate, report = _stage(fixture)
    assert report["messages"] == report["conversations"] == 0
    assert report["excluded_existing_records"] == 3
    with sqlite3.connect(candidate) as output:
        assert output.execute("SELECT COUNT(*) FROM messages").fetchone() == (0,)
        assert output.execute("SELECT COUNT(*) FROM conversations").fetchone() == (0,)


def test_changed_source_before_completion_discards_only_candidate(native_metadata):
    fixture = native_metadata
    before = fixture.history.read_bytes()

    def change_source(update):
        if update["phase"] == "conversation":
            with sqlite3.connect(fixture.original) as source:
                source.execute(
                    "INSERT INTO archive_info VALUES('concurrent_change','preserved')"
                )

    candidate = fixture.original.with_name("full-catalog-candidate.sqlite3")
    with pytest.raises(ValueError, match="changed"):
        _stage(fixture, progress=change_source)
    assert not candidate.exists()
    assert fixture.history.read_bytes() == before
    with sqlite3.connect(fixture.original) as source:
        assert (
            dict(source.execute("SELECT * FROM archive_info"))["concurrent_change"]
            == "preserved"
        )


def test_explicit_metadata_budget_is_enforced_without_changing_inputs(
    native_metadata, monkeypatch
):
    fixture = native_metadata
    before = fixture.history.read_bytes(), fixture.original.read_bytes()
    monkeypatch.setattr(refresh, "MAX_RESOURCE_ROWS", 2)
    candidate = fixture.original.with_name("full-catalog-candidate.sqlite3")
    with pytest.raises(ValueError, match="budget"):
        _stage(fixture)
    assert not candidate.exists()
    assert (fixture.history.read_bytes(), fixture.original.read_bytes()) == before


def test_existing_candidate_is_never_overwritten(native_metadata):
    fixture = native_metadata
    candidate = fixture.original.with_name("full-catalog-candidate.sqlite3")
    candidate.write_bytes(b"Keep the previous review result.")
    with pytest.raises(FileExistsError):
        _stage(fixture)
    assert candidate.read_bytes() == b"Keep the previous review result."
