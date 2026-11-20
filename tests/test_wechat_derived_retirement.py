"""Proved export retirement is staged, bounded, reversible, and idempotent.

Code version: v1.1.3-codex.0
"""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import wechat_derived_retirement as retirement
from app.core.wechat_derived_redundancy import plan_derived_redundancy
from tests.test_wechat_derived_redundancy import redundancy_store as redundancy_store


@pytest.fixture
def prepared(redundancy_store, monkeypatch):
    fixture = redundancy_store
    with sqlite3.connect(fixture.history) as connection:
        manifest = {"schema_version": 1, "messages": 9, "unique_original_message_count": None,
                    "sources": [{"source_id": source, "messages": 3, "conversations": 1,
                                 "account_id": None if source == fixture.sources[2] else source}
                                for source in fixture.sources]}
        connection.execute("INSERT INTO archive_info VALUES('manifest',?)", (json.dumps(manifest),))
        connection.execute("CREATE TABLE message_evidence(id TEXT PRIMARY KEY,message_id TEXT,source_id TEXT,snapshot_id TEXT,extraction_record_id TEXT,observation TEXT,match_kind TEXT)")
        for index, identifier in enumerate(fixture.derived_ids):
            connection.execute("INSERT INTO message_evidence VALUES(?,?,?,?,?,?,?)", (str(index), identifier, fixture.sources[2], "synthetic-export", str(index), "{}", "export"))
    from app.core import wechat_export_merge

    monkeypatch.setattr(wechat_export_merge, "_clonefile", shutil.copyfile)
    monkeypatch.setattr(retirement, "_no_writers", lambda path: None)
    return fixture


def _rows(path):
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return {row["id"]: dict(row) for row in connection.execute("SELECT * FROM messages")}


def test_dry_run_preserves_sources_and_creates_no_workstation(prepared, tmp_path):
    fixture = prepared
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    directory = tmp_path / "rollback"
    result = retirement.stage_retirement(fixture.store, plan_derived_redundancy(fixture.store), backup_directory=directory)
    assert result["candidate_path"] is None
    assert result["report"]["matches"] == 3
    assert result["report"]["messages_after"] == 6
    assert not directory.exists()
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def test_exact_export_replay_does_not_reinsert_retired_observations(prepared, tmp_path):
    from app.core.wechat_export_merge import _json, _source_metadata, merge_wechat_export

    fixture = prepared
    metadata = _source_metadata({
        "source_id": fixture.sources[2], "source_label": "Excel export",
        "source_file": "synthetic.xlsx", "source_sha256": "a" * 64,
    })
    with sqlite3.connect(fixture.history) as connection:
        connection.execute(
            "CREATE TABLE export_imports(source_id TEXT PRIMARY KEY,snapshot_id TEXT,"
            "source_sha256 TEXT,metadata TEXT,record_count INTEGER,conversation_count INTEGER,"
            "evidence_count INTEGER,content_fingerprint TEXT)"
        )
        connection.execute(
            "INSERT INTO export_imports VALUES(?,?,?,?,?,?,?,?)",
            (fixture.sources[2], "synthetic-export", "a" * 64, _json(metadata), 3, 1, 3, "b" * 64),
        )
    staged = retirement.stage_retirement(
        fixture.store, plan_derived_redundancy(fixture.store),
        backup_directory=tmp_path / "replay-rollback", apply=True,
    )
    Path(staged["candidate_path"]).replace(fixture.history)
    before = fixture.history.read_bytes()

    class NoSourceRead:
        def __iter__(self):
            raise AssertionError("An exact retired export replay must not read its source again.")

    replay = merge_wechat_export(
        fixture.store, source=metadata, records=NoSourceRead(),
        snapshot_id="synthetic-export", expected_records=3,
    )
    assert replay["replayed"] and not replay["changed"]
    assert replay["inserted_messages"] == 0
    assert fixture.history.read_bytes() == before


@pytest.mark.parametrize("shared", [False, True])
def test_staged_aliases_keep_native_rows_and_complete_small_rollback(prepared, tmp_path, shared):
    fixture = prepared
    if shared:
        from app.core.wechat_shared_storage import _install

        with sqlite3.connect(fixture.history) as connection:
            _install(connection)
    original = _rows(fixture.history)
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    result = retirement.stage_retirement(fixture.store, plan_derived_redundancy(fixture.store), backup_directory=tmp_path / "rollback", apply=True)
    candidate, report = Path(result["candidate_path"]), result["report"]
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())
    assert not report["published"] and report["filesystem_bytes_reclaimed"] == 0
    assert report["evidence_rows_moved"] == 3
    assert _rows(candidate) == {key: row for key, row in original.items() if key not in fixture.derived_ids}
    with sqlite3.connect(candidate) as connection:
        aliases = connection.execute("SELECT old_id,native_id,metadata,proof FROM derived_message_aliases").fetchall()
        assert {row[0] for row in aliases} == set(fixture.derived_ids)
        for _, native_id, metadata, proof in aliases:
            assert native_id in fixture.native_ids[3:]
            assert "Synthetic preserved body" not in metadata + proof
            assert json.loads(metadata)["row"]["time_basis"] == "Source clock; timezone unknown"
            assert json.loads(metadata)["source"]["account_id"] is None
        assert connection.execute("SELECT message_count,first_time,last_time,latest_text FROM conversations WHERE partition_id=?", (fixture.sources[2],)).fetchone() == (0, "", "", "")
        assert json.loads(connection.execute("SELECT value FROM archive_info WHERE key='manifest'").fetchone()[0])["messages"] == 6
        manifest = json.loads(connection.execute("SELECT value FROM archive_info WHERE key='manifest'").fetchone()[0])
        assert manifest["unique_original_message_count"] is None
        assert [(row["messages"], row["conversations"]) for row in manifest["sources"]] == [(3, 1), (3, 1), (0, 1)]
        assert manifest["sources"][2]["account_id"] is None
        assert {row[0] for row in connection.execute("SELECT message_id FROM message_evidence")} == set(fixture.native_ids[3:])
    with gzip.open(report["rollback_path"], "rt", encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream]
    assert records[0]["manifest"]["messages"] == 9
    assert {row["row"]["id"]: row["row"] for row in records if row["kind"] == "message"} == {key: original[key] for key in fixture.derived_ids}
    assert len([row for row in records if row["kind"] == "message_evidence"]) == 3
    assert len(list((tmp_path / "rollback").glob("*.sqlite3"))) == 0


def test_idempotent_existing_aliases_never_recreate_body_copies(prepared, tmp_path):
    fixture = prepared
    plan = plan_derived_redundancy(fixture.store)
    staged = retirement.stage_retirement(fixture.store, plan, backup_directory=tmp_path / "first", apply=True)
    os.replace(staged["candidate_path"], fixture.history)
    plan["history_identity"] = retirement.file_identity(fixture.history)
    again = retirement.stage_retirement(fixture.store, plan, backup_directory=tmp_path / "second", apply=True)
    assert again["candidate_path"] is None
    assert again["report"]["matches"] == 0 and again["report"]["already_retired"] == 3
    assert not (tmp_path / "second").exists()


@pytest.mark.parametrize("failure", ["identity", "derived-hash", "native-hash", "wrong-source", "wrong-author"])
def test_stale_or_unproved_plan_never_stages_or_mutates(prepared, tmp_path, failure):
    fixture = prepared
    plan = plan_derived_redundancy(fixture.store)
    if failure == "identity":
        plan["history_identity"]["size"] += 1
    elif failure in {"derived-hash", "native-hash"}:
        plan["matches"][0][failure.replace("-hash", "_sha256")] = "0" * 64
    elif failure == "wrong-author":
        plan["matches"][0]["evidence"]["contact_id"] = "wechat:contact:" + "f" * 64
    else:
        with sqlite3.connect(fixture.history) as connection:
            connection.execute("UPDATE partitions SET source_label='iPad archive' WHERE id=?", (fixture.sources[2],))
        plan["history_identity"] = retirement.file_identity(fixture.history)
    before = fixture.history.read_bytes()
    with pytest.raises(ValueError):
        retirement.stage_retirement(fixture.store, plan, backup_directory=tmp_path / "rejected", apply=True)
    assert fixture.history.read_bytes() == before
    assert not (tmp_path / "rejected").exists()


def test_free_space_budget_refuses_before_creating_any_candidate(prepared, tmp_path, monkeypatch):
    fixture = prepared
    monkeypatch.setattr(retirement.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    with pytest.raises(ValueError, match="free-space budget"):
        retirement.stage_retirement(fixture.store, plan_derived_redundancy(fixture.store), backup_directory=tmp_path / "no-space", apply=True)
    assert not (tmp_path / "no-space").exists()
    assert not list(fixture.history.parent.glob(".wechat-derived-retire-*"))


def test_unmatched_export_and_negative_native_server_ids_are_preserved(prepared, tmp_path):
    fixture = prepared
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        row = dict(connection.execute("SELECT * FROM messages WHERE id=?", (fixture.derived_ids[0],)).fetchone())
        row.update(id="a" * 64, ordinal=4, text="Unique information survives", search_text="unique")
        connection.execute("INSERT INTO messages VALUES(" + ",".join("?" for _ in row) + ")", tuple(row.values()))
        manifest = json.loads(connection.execute("SELECT value FROM archive_info WHERE key='manifest'").fetchone()[0])
        manifest["messages"] = 10
        manifest["sources"][2]["messages"] = 4
        connection.execute("UPDATE archive_info SET value=? WHERE key='manifest'", (json.dumps(manifest),))
        for identifier in fixture.native_ids:
            encoded = connection.execute("SELECT provenance FROM messages WHERE id=?", (identifier,)).fetchone()[0]
            proof = json.loads(encoded)
            proof["server_id"] = "-" + proof["server_id"]
            connection.execute("UPDATE messages SET provenance=? WHERE id=?", (json.dumps(proof), identifier))
    original = _rows(fixture.history)
    result = retirement.stage_retirement(fixture.store, plan_derived_redundancy(fixture.store), backup_directory=tmp_path / "retained", apply=True)
    with sqlite3.connect(result["candidate_path"]) as connection:
        assert connection.execute("SELECT message_count,latest_text FROM conversations WHERE partition_id=?", (fixture.sources[2],)).fetchone() == (1, "Unique information survives")
    assert _rows(result["candidate_path"]) == {key: value for key, value in original.items() if key not in fixture.derived_ids}


def test_sql_guards_reject_any_native_or_unmatched_update(prepared, tmp_path, monkeypatch):
    fixture = prepared
    before = fixture.history.read_bytes()

    def corrupt_native(connection, affected):
        connection.execute("UPDATE messages SET text='Corruption' WHERE id=?", (fixture.native_ids[0],))

    monkeypatch.setattr(retirement, "_counters", corrupt_native)
    with pytest.raises(sqlite3.IntegrityError, match="unauthorized"):
        retirement.stage_retirement(fixture.store, plan_derived_redundancy(fixture.store), backup_directory=tmp_path / "blocked", apply=True)
    assert fixture.history.read_bytes() == before
    assert not list(fixture.history.parent.glob(".wechat-derived-retire-*.sqlite3"))


def test_multiple_bounded_batches_keep_guards_and_report_actual_space(prepared, tmp_path, monkeypatch):
    fixture = prepared
    monkeypatch.setattr(retirement, "WRITE_BATCH_SIZE", 2)
    before = fixture.history.read_bytes()
    result = retirement.stage_retirement(fixture.store, plan_derived_redundancy(fixture.store), backup_directory=tmp_path / "batches", apply=True)
    report = result["report"]
    assert report["message_write_batches"] == 2 and report["write_batch_size"] == 2
    assert report["worst_case_incremental_bytes"] > 0
    assert report["required_free_bytes"] == 1024**3 + report["peak_write_batch_bytes"] + report["metadata_write_reserve_bytes"]
    assert [row["phase"] for row in report["free_space_observations"]].count("message-batch-committed") == 2
    assert report["observed_free_bytes_minimum"] <= report["observed_free_bytes_before"]
    assert _rows(result["candidate_path"]).keys() == set(fixture.native_ids)
    assert before == fixture.history.read_bytes()


def test_space_drop_after_a_committed_batch_discards_entire_owned_candidate(prepared, tmp_path, monkeypatch):
    fixture = prepared
    monkeypatch.setattr(retirement, "WRITE_BATCH_SIZE", 2)
    observed = []

    def capacity(path):
        for candidate in fixture.history.parent.glob(".wechat-derived-retire-*.sqlite3"):
            with sqlite3.connect(candidate) as connection:
                count = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            if count == 7:
                observed.append(count)
                return SimpleNamespace(free=0)
        return SimpleNamespace(free=10**12)

    monkeypatch.setattr(retirement.shutil, "disk_usage", capacity)
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    with pytest.raises(ValueError, match="free-space budget during message-batch-committed"):
        retirement.stage_retirement(fixture.store, plan_derived_redundancy(fixture.store), backup_directory=tmp_path / "space-fall", apply=True)
    assert observed == [7]
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())
    assert not list(fixture.history.parent.glob(".wechat-derived-retire-*"))
    assert (tmp_path / "space-fall" / "removed-observations.jsonl.gz").is_file()


@pytest.mark.parametrize("empty", [[], {}, None], ids=["array", "object", "null"])
def test_empty_optional_metadata_does_not_invent_rich_content(prepared, tmp_path, empty):
    fixture = prepared
    plan = plan_derived_redundancy(fixture.store)
    with sqlite3.connect(fixture.history) as connection:
        for identifier in [*fixture.native_ids, *fixture.derived_ids]:
            encoded = connection.execute("SELECT provenance FROM messages WHERE id=?", (identifier,)).fetchone()[0]
            proof = json.loads(encoded)
            proof.update(forwarded_components=empty, quoted_components=empty,
                         text_components=empty, resource_reference=None,
                         payment_card_fields=empty)
            connection.execute("UPDATE messages SET provenance=? WHERE id=?", (json.dumps(proof), identifier))
    before = fixture.history.read_bytes()
    plan["history_identity"] = retirement.file_identity(fixture.history)
    rows = _rows(fixture.history)
    for match in plan["matches"]:
        for kind in ("derived", "native"):
            match[kind+"_sha256"] = retirement.logical_message_sha256(rows[match[kind+"_id"]])
    result = retirement.stage_retirement(fixture.store, plan, backup_directory=tmp_path / "unused")
    assert result["report"]["matches"] == 3
    assert result["candidate_path"] is None and before == fixture.history.read_bytes()


@pytest.mark.parametrize("content", [
    {"forwarded_components": [{"text": "Forwarded content"}]},
    {"quoted_components": [{"text": "Quoted content"}]},
    {"resource_reference": {"kind": "image"}},
    {"resource_reference": {}},
    {"message_type": 49},
    {"locator": {"export_msgtype": "3"}},
    {"nested": {"payment_card_fields": {"amount": "1.00"}}},
], ids=["forwarded", "quoted", "resource", "unresolved-resource", "app", "image-type", "nested-payment"])
def test_nonempty_rich_metadata_refuses_even_a_rehashed_plan(prepared, tmp_path, content):
    fixture = prepared
    plan = plan_derived_redundancy(fixture.store)
    match = plan["matches"][0]
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        row = dict(connection.execute("SELECT * FROM messages WHERE id=?", (match["derived_id"],)).fetchone())
        proof = json.loads(row["provenance"])
        proof.update(content)
        row["provenance"] = json.dumps(proof)
        connection.execute("UPDATE messages SET provenance=? WHERE id=?", (row["provenance"], row["id"]))
    plan["history_identity"] = retirement.file_identity(fixture.history)
    match["derived_sha256"] = retirement.logical_message_sha256(row)
    before = fixture.history.read_bytes()
    with pytest.raises(ValueError, match="top-level plain text"):
        retirement.stage_retirement(fixture.store, plan, backup_directory=tmp_path / "rejected-rich", apply=True)
    assert before == fixture.history.read_bytes()
    assert not (tmp_path / "rejected-rich").exists()
