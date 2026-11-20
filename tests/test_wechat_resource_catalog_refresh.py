"""Full native catalog refresh reuses source evidence without broadening reads.

Code version: v1.0.2-codex.0
"""

from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import json
import sqlite3

import pytest

from app.core import wechat_resource_catalog_refresh as refresh
from app.core.wechat_archive import WechatArchiveRepository, _file_identity, _identifier
from tests import test_wechat_native_resource_revalidation as fixtures
from tests import test_wechat_primary_source_receipts as primary_fixtures


BACKUP = fixtures.BACKUP
native_metadata = fixtures.native_metadata
primary_receipts = primary_fixtures.primary_receipts


def _stage(fixture, **options):
    candidate = fixture.original.with_name("full-catalog-candidate.sqlite3")
    return candidate, refresh.stage_resource_catalog_refresh(
        fixture.store,
        candidate,
        **options,
    )


def test_legacy_refresh_cannot_drop_extended_source_memberships(native_metadata):
    fixture = native_metadata
    with sqlite3.connect(fixture.original) as source:
        source.execute("CREATE TABLE native_resource_occurrences (receipt TEXT)")
        source.execute("INSERT INTO native_resource_occurrences VALUES ('retained')")
    before = fixture.history.read_bytes(), fixture.original.read_bytes()
    candidate = fixture.original.with_name("full-catalog-candidate.sqlite3")
    with pytest.raises(ValueError, match="memberships and coverage"):
        _stage(fixture)
    assert not candidate.exists()
    assert (fixture.history.read_bytes(), fixture.original.read_bytes()) == before


def test_receipted_v2_identifiers_include_the_native_database_hash(native_metadata):
    from app.core.wechat_native_resource_revalidation import _receipts, _validate_row
    from app.core.wechat_resource_index import _readonly

    fixture = native_metadata
    with _readonly(fixture.history) as history, _readonly(fixture.original) as source:
        conversation = history.execute("SELECT * FROM conversations").fetchone()
        partition = history.execute("SELECT * FROM partitions").fetchone()
        owner, receipts, snapshots = _receipts(history, partition)
        row = dict(
            source.execute(
                "SELECT * FROM messages WHERE ordinal=?",
                (fixture.resources[1][2],),
            ).fetchone()
        )
        proof = json.loads(row["provenance"])
        proof["record_id_scheme"] = "wechat-resource-reference-v2"
        proof["record_id"] = _identifier(
            "wechat-resource-reference-v2",
            owner,
            proof["backup_id"],
            proof["source_relative_path"],
            proof["source_sha256"],
            proof["locator"]["table"],
            str(proof["locator"]["local_id"]),
        )
        row["id"], row["provenance"] = proof["record_id"], json.dumps(proof)
        _validate_row(row, conversation, owner, receipts, snapshots)
        proof["source_sha256"] = "0" * 64
        row["provenance"] = json.dumps(proof)
        with pytest.raises(ValueError, match="preserved database receipt"):
            _validate_row(row, conversation, owner, receipts, snapshots)


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
    with closing(sqlite3.connect(candidate)) as output, output:
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


@pytest.fixture
def supplemental_catalog(primary_receipts):
    fixture = primary_receipts
    with sqlite3.connect(fixture.history) as primary:
        snapshot, metadata = primary.execute(
            "SELECT snapshot_id,metadata FROM source_snapshots WHERE id=1"
        ).fetchone()
    rewritten = []
    for original in fixture.resources:
        row = list(original)
        proof = json.loads(row[11])
        identifier = _identifier(
            "wechat-resource-reference-v1",
            fixtures.OWNER,
            snapshot,
            proof["source_relative_path"],
            proof["locator"]["table"],
            str(row[2]),
        )
        row[0] = identifier
        proof.update(
            backup_id=snapshot,
            record_id=identifier,
            source_snapshot=1,
            source_checksum_basis="primary_source_database_receipt",
            source_snapshot_metadata_sha256=refresh.hashlib.sha256(
                metadata.encode()
            ).hexdigest(),
        )
        row[11] = json.dumps(proof)
        rewritten.append(tuple(row))
    with sqlite3.connect(fixture.original) as source:
        source.execute("DELETE FROM messages")
        source.executemany(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rewritten
        )
    fixture.supplemental_resources = rewritten
    return fixture


@pytest.mark.parametrize("routed_html", [False, True])
def test_future_full_refresh_reproves_supplemental_receipts_and_routed_anchors(
    supplemental_catalog, routed_html
):
    fixture = supplemental_catalog
    if routed_html:
        with sqlite3.connect(fixture.history) as primary:
            conversation = list(
                primary.execute(
                    "SELECT * FROM conversations WHERE id=?",
                    (fixture.conversation["id"],),
                ).fetchone()
            )
            conversation[0] = "wechat:" + "d" * 64
            conversation[1] = "html-" + "e" * 24
            primary.execute(
                "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                conversation,
            )
            primary.execute(
                "UPDATE messages SET conversation_id=? WHERE id=?",
                (conversation[0], fixture.primary_id),
            )
    before = fixture.history.read_bytes(), fixture.original.read_bytes()
    candidate, report = _stage(fixture)
    assert report["messages"] == 2
    assert report["excluded_kinds"] == {"app": 1}
    assert report["primary_receipt_scopes"] == 1
    with sqlite3.connect(candidate) as output:
        assert (
            output.execute("SELECT * FROM messages ORDER BY ordinal").fetchall()
            == fixture.supplemental_resources[1:]
        )
    assert (fixture.history.read_bytes(), fixture.original.read_bytes()) == before


@pytest.mark.parametrize("damage", ["metadata_digest", "placeholder", "account"])
def test_supplemental_catalog_cannot_borrow_or_invent_primary_receipts(
    supplemental_catalog, damage
):
    fixture = supplemental_catalog
    if damage == "metadata_digest":
        with sqlite3.connect(fixture.original) as source:
            proof = json.loads(
                source.execute(
                    "SELECT provenance FROM messages WHERE ordinal=1"
                ).fetchone()[0]
            )
            proof["source_snapshot_metadata_sha256"] = "f" * 64
            source.execute(
                "UPDATE messages SET provenance=? WHERE ordinal=1", (json.dumps(proof),)
            )
    elif damage == "placeholder":
        with sqlite3.connect(fixture.history) as primary:
            proof = deepcopy(fixture.primary_source_proof)
            proof["text_extraction_kind"] = "resource_placeholder"
            primary.execute(
                "UPDATE messages SET provenance=? WHERE id=?",
                (json.dumps(proof), fixture.primary_id),
            )
    elif damage == "account":
        with sqlite3.connect(fixture.history) as primary:
            metadata = deepcopy(fixture.primary_source_metadata)
            metadata["source_account_id"] = "wxid_another_account"
            primary.execute(
                "UPDATE source_snapshots SET metadata=? WHERE id=1",
                (json.dumps(metadata),),
            )
    before = fixture.history.read_bytes(), fixture.original.read_bytes()
    candidate = fixture.original.with_name("full-catalog-candidate.sqlite3")
    with pytest.raises(ValueError):
        _stage(fixture)
    assert not candidate.exists()
    assert (fixture.history.read_bytes(), fixture.original.read_bytes()) == before
