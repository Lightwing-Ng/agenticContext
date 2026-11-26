"""Resource detail batches preserve originals, prior proofs, and publication boundaries.

Code version: v1.3.1-codex.0
"""

from contextlib import closing
from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
import sqlite3

import pytest

from app.core.owner_only_files import owner_only_problem

from app.core import wechat_resource_detail_repair as repair
from app.core import wechat_resource_details as details
from app.core.wechat_archive import WechatArchiveRepository, _file_identity, _identifier, _time_fields
from app.core.wechat_contacts import initialize_contact_registry
from app.core.wechat_history_browser import query_wechat_history
from tests.test_wechat_contacts import bind, insert_contact, observe
from tests import test_wechat_resource_details as evidence
from tests import test_wechat_secondary_completions as completion_evidence


mac_archive = evidence.mac_archive
bank_archive = evidence.bank_archive
source_binding = evidence.source_binding
witness = evidence.witness
fixture = evidence.fixture
completion_fixture = completion_evidence.completion_fixture


def _insert(fixture, row):
    with sqlite3.connect(fixture.history) as connection:
        connection.execute(
            "INSERT INTO messages VALUES(" + ",".join("?" for _ in details.COLUMNS) + ")",
            tuple(row[key] for key in details.COLUMNS),
        )
        connection.execute(
            "UPDATE conversations SET message_count=message_count+1 WHERE id=?",
            (fixture.conversation["id"],),
        )


@pytest.fixture
def repair_fixture(fixture):
    _insert(fixture, fixture.row)
    contacts = fixture.history.with_name("contacts.sqlite3")
    with sqlite3.connect(contacts) as connection:
        initialize_contact_registry(connection)
    return fixture


def _source_bytes(fixture):
    return {
        path: path.read_bytes()
        for path in (
            fixture.history,
            fixture.history.with_name("contacts.sqlite3"),
        )
    }


def _stage(fixture, candidate, *, packet=None, message_ids=None):
    return repair.stage_resource_detail_repair(
        fixture.root, candidate, [packet or fixture.packet], message_ids=message_ids,
    )


def _publish(fixture, report, rollback):
    return repair.publish_resource_detail_repair(
        fixture.root, report, rollback_path=rollback,
    )


def test_two_matching_text_witnesses_keep_the_archive_owners_exact_source(repair_fixture):
    original = repair_fixture.row
    owner = repair_fixture.packet["account_id"]
    native = evidence.native._message(81, 1, raw="Verified common text")
    other = (
        {"account_id": "wxid_other_viewer"}, deepcopy(native),
        {**original, "provenance": "other viewer proof"},
    )
    own = ({"account_id": owner}, deepcopy(native), original)
    selected = repair._same_text_author_witness([other, own], "account:" + owner)
    assert selected is own
    for field, conflict in (
        ("content_sha256", "0" * 64),
        ("sender_id", "wxid_unrelated_member"),
        ("text", "A competing body"),
    ):
        competing = (other[0], {**other[1], field: conflict}, other[2])
        assert repair._same_text_author_witness([competing, own], "account:" + owner) is None
    competing_output = (other[0], other[1], {**other[2], "sender_id": "wxid_different_member"})
    assert repair._same_text_author_witness([competing_output, own], "account:" + owner) is None


def _second(fixture):
    row, packet = deepcopy(fixture.row), deepcopy(fixture.packet)
    message = packet["messages"][0]
    message.update(
        local_id=message["local_id"] + 1,
        server_id=str(int(message["server_id"]) + 1),
        create_time=message["create_time"] + 1,
        content_sha256=hashlib.sha256(b"independent exact native event").hexdigest(),
    )
    message["resource"].update(
        native_time=message["create_time"], safe_title="A second preserved title",
    )
    stamp = datetime.fromtimestamp(message["create_time"], UTC).isoformat()
    order, value, label, basis = _time_fields({"sent_at_utc_iso": stamp})
    row.update(
        id=hashlib.sha256(b"second original resource").hexdigest(),
        ordinal=row["ordinal"] + 1,
        time_sort=order, timestamp_value=value, timestamp_label=label, time_basis=basis,
    )
    proof = json.loads(row["provenance"])
    proof.update(
        record_id=row["id"], server_id=message["server_id"],
        unix_timestamp=message["create_time"],
    )
    proof["resource_reference"]["unix_timestamp"] = message["create_time"]
    proof["locator"]["local_id"] = row["ordinal"]
    row["provenance"] = json.dumps(proof)
    _insert(fixture, row)
    return row, packet


def test_staging_is_review_only_and_preserves_all_source_bytes(repair_fixture, tmp_path):
    f = repair_fixture
    before = _source_bytes(f)
    target = f.history.with_name(details.FILENAME)
    report = _stage(f, tmp_path / "candidate.json")
    assert report["changed"] is True and report["published"] is False
    assert report["counts"]["selected"] == report["counts"]["added"] == 1
    assert report["counts"]["authors_filled"] == report["counts"]["cards_enriched"] == 1
    assert report["rejected"] == {}
    assert not target.exists()
    candidate = tmp_path / "candidate.json"
    assert owner_only_problem(candidate) == ""
    assert hashlib.sha256(candidate.read_bytes()).hexdigest() == report["candidate_sha256"]
    assert _source_bytes(f) == before


def test_publication_merges_existing_proof_and_drives_the_real_sql_reader(
    repair_fixture, tmp_path,
):
    f = repair_fixture
    row, packet = _second(f)
    original = details.make_entry(f.row, f.conversation, f.packet)
    target = evidence._registry(f, original)
    before, old_registry = _source_bytes(f), target.read_bytes()
    report = _stage(
        f, tmp_path / "merged.json", packet=packet, message_ids=[row["id"]],
    )
    assert report["preserved_entries"] == report["counts"]["added"] == 1
    assert report["total_entries"] == 2
    rollback = tmp_path / "registry-before.json"
    assert _publish(f, report, rollback)["published"] is True
    entries, _ = details.load_registry(f.history)
    by_id = {entry["message_id"]: entry for entry in entries}
    assert by_id[original["message_id"]] == original
    assert set(by_id) == {f.row["id"], row["id"]}
    assert rollback.read_bytes() == old_registry
    assert owner_only_problem(rollback) == ""
    result = WechatArchiveRepository(f.root).query(
        session=f.conversation["id"], query="second preserved", persist_event_cache=False,
    )
    assert result["resource_index_status"] == "ready" and result["count"] == 1
    projected, = result["messages"]
    assert projected["id"] == row["id"]
    assert projected["sender_id"] == evidence.native.MEMBER
    for field in ("text", "timestamp_value", "ordinal", "direction"):
        assert projected[field] == row[field]
    assert _source_bytes(f) == before


def test_exact_replay_does_not_rewrite_the_registry_or_create_a_rollback(
    repair_fixture, tmp_path,
):
    f = repair_fixture
    target = evidence._registry(f)
    before = target.read_bytes(), details.registry_identity(f.history)
    report = _stage(f, tmp_path / "replay.json")
    assert report["changed"] is False
    assert report["counts"]["existing"] == 1
    assert report["counts"].get("added", 0) == 0
    rollback = tmp_path / "no-replay-rollback.json"
    publication = _publish(f, report, rollback)
    assert publication["published"] is False and publication["unchanged"] is True
    assert (target.read_bytes(), details.registry_identity(f.history)) == before
    assert not rollback.exists()


def test_conflicting_witness_cannot_replace_an_existing_reviewed_entry(
    repair_fixture, tmp_path,
):
    f = repair_fixture
    target = evidence._registry(f)
    before = target.read_bytes()
    packet = deepcopy(f.packet)
    packet["messages"][0]["resource"]["safe_title"] = "Conflicting later projection"
    report = _stage(f, tmp_path / "conflict.json", packet=packet)
    assert report["changed"] is False
    assert report["total_entries"] == report["preserved_entries"] == 1
    assert report["rejected"]["existing_detail_requires_review"] == 1
    _publish(f, report, tmp_path / "no-conflict-rollback.json")
    assert target.read_bytes() == before


@pytest.mark.parametrize("drift", ["registry", "candidate", "primary"])
def test_publication_rejects_changed_reviewed_inputs_without_overwriting_data(
    repair_fixture, tmp_path, drift,
):
    f = repair_fixture
    candidate = tmp_path / "candidate.json"
    report = _stage(f, candidate)
    target = f.history.with_name(details.FILENAME)
    if drift == "registry":
        evidence._registry(f)
    elif drift == "candidate":
        candidate.write_bytes(candidate.read_bytes() + b"\n")
    else:
        with sqlite3.connect(f.history) as connection:
            connection.execute(
                "UPDATE messages SET sender_label='Concurrent source label' WHERE id=?",
                (f.row["id"],),
            )
    expected = target.read_bytes() if target.exists() else None
    with pytest.raises(ValueError, match="changed"):
        _publish(f, report, tmp_path / "no-drift-rollback.json")
    assert (target.read_bytes() if target.exists() else None) == expected
    assert candidate.is_file()
    assert not (tmp_path / "no-drift-rollback.json").exists()


def test_quarantined_secondary_resource_is_not_promoted_by_a_detail_batch(
    repair_fixture, tmp_path, monkeypatch,
):
    f = repair_fixture
    original = WechatArchiveRepository._timeline_tables

    def upstream_with_quarantine(repository, connection, *args, **kwargs):
        result = original(repository, connection, *args, **kwargs)
        connection.execute("ATTACH DATABASE ':memory:' AS secondary_completions")
        connection.execute("CREATE TABLE secondary_completions.ambiguities(id TEXT PRIMARY KEY)")
        connection.execute(
            "INSERT INTO secondary_completions.ambiguities VALUES(?)", (f.row["id"],),
        )
        return result

    monkeypatch.setattr(WechatArchiveRepository, "_timeline_tables", upstream_with_quarantine)
    report = _stage(f, tmp_path / "quarantined.json")
    assert report["rejected"]["quarantined"] == 1
    assert report["counts"].get("added", 0) == 0
    assert report["changed"] is False and report["total_entries"] == 0
    assert not f.history.with_name(details.FILENAME).exists()


def test_already_complete_native_resource_remains_a_noop(repair_fixture, tmp_path):
    f = repair_fixture
    proof = json.loads(f.row["provenance"])
    proof["resource_reference"].update({
        key: value for key, value in f.packet["messages"][0]["resource"].items()
        if key not in {"kind", "label", "native_time"}
    })
    with sqlite3.connect(f.history) as connection:
        connection.execute(
            "UPDATE messages SET sender_id=?,sender_label=?,provenance=? WHERE id=?",
            (evidence.native.MEMBER, "Known source author", json.dumps(proof), f.row["id"]),
        )
    before = _source_bytes(f)
    report = _stage(f, tmp_path / "complete.json")
    assert report["counts"]["no_change"] == 1
    assert report["changed"] is False and report["total_entries"] == 0
    assert _source_bytes(f) == before


def test_stale_retained_detail_requires_review_without_losing_the_registry(
    repair_fixture, tmp_path,
):
    f = repair_fixture
    target = evidence._registry(f)
    before = target.read_bytes()
    with sqlite3.connect(f.history) as connection:
        connection.execute(
            "UPDATE messages SET search_text='Changed upstream' WHERE id=?", (f.row["id"],),
        )
    candidate = tmp_path / "unaccepted.json"
    with pytest.raises(ValueError, match="revalidation"):
        _stage(f, candidate)
    assert target.read_bytes() == before
    assert not candidate.exists()


def test_manifest_populated_after_packet_iteration_limits_the_reviewed_scope(
    repair_fixture, tmp_path,
):
    f = repair_fixture
    _row, second = _second(f)
    combined = deepcopy(f.packet)
    combined["messages"].extend(second["messages"])
    captured = {}

    def packets():
        yield combined
        captured[f.row["id"]] = details.row_sha256(f.row)

    report = repair.stage_resource_detail_repair(
        f.root, tmp_path / "captured-scope.json", packets(), expected_rows=captured,
    )
    assert report["counts"]["selected"] == report["counts"]["added"] == 1
    assert report["total_entries"] == 1 and report["rejected"] == {}
    _publish(f, report, tmp_path / "no-prior-manifest.json")
    entries, _ = details.load_registry(f.history)
    assert {entry["message_id"] for entry in entries} == {f.row["id"]}


def test_cross_viewer_capture_requires_an_active_exact_completion_and_preserves_sender(
    completion_fixture, tmp_path,
):
    f = completion_fixture
    target = f["conversation"]["id"]
    row = f["image"]
    donor = f["packet"]
    with sqlite3.connect(f["history"].with_name("contacts.sqlite3")) as connection:
        initialize_contact_registry(connection)
    with closing(sqlite3.connect(f["history"])) as connection:
        donor["session_id"], = connection.execute(
            "SELECT id FROM conversations WHERE account_key=? AND peer_id=?",
            ("account:" + donor["account_id"], donor["peer_id"]),
        ).fetchone()
    witness = donor["messages"][0]
    witness.update(
        local_type=(51 << 32) | 49,
        text="[App message]",
        resource={
            "kind": "app", "label": "[App message]", "app_subtype": "51",
            "native_time": witness["create_time"],
        },
    )
    row.update(text="[App message]", search_text="[app message]")
    proof = json.loads(row["provenance"])
    proof["message_type"] = 49
    proof["resource_reference"].update(kind="app", label="[App message]")
    row["provenance"] = json.dumps(proof)
    with sqlite3.connect(f["catalog"]) as connection:
        connection.execute(
            "UPDATE messages SET text=?,search_text=?,provenance=? WHERE id=?",
            (row["text"], row["search_text"], row["provenance"], row["id"]),
        )
    rich = deepcopy({**donor, "messages": [witness]})
    rich["messages"][0]["resource"].update(
        app_metadata_version="1", channels_description="Exact Channels share",
    )
    manifest = {"target_sessions": {row["id"]: target}}
    missing_completion = repair.stage_resource_detail_repair(
        f["root"], tmp_path / "missing.json", [rich],
        expected_rows={row["id"]: details.row_sha256(row)}, **manifest,
    )
    assert missing_completion["rejected"] == {"native_evidence_conflict": 1}

    completion_evidence._publish(f, completion_evidence._stage(f))
    repository = WechatArchiveRepository(f["root"])
    with closing(repair._readonly(f["history"])) as connection:
        _conversations, messages, status = repository._timeline_tables(
            connection, _file_identity(f["history"]), conversation_id=target,
            strict=True, include_resource_details=False,
        )
        completed = dict(connection.execute(
            f"SELECT * FROM {messages} WHERE id=?", (row["id"],),
        ).fetchone())
    assert status in {"ready", "absent"}
    assert completed["sender_id"] == witness["sender_id"]
    manifest["expected_rows"] = {row["id"]: details.row_sha256(completed)}
    foreign_event = deepcopy(rich)
    foreign_event["messages"][0]["content_sha256"] = "e" * 64
    conflict = repair.stage_resource_detail_repair(
        f["root"], tmp_path / "conflict.json", [foreign_event], **manifest,
    )
    assert conflict["rejected"] == {"native_evidence_conflict": 1}

    report = repair.stage_resource_detail_repair(
        f["root"], tmp_path / "details.json", [foreign_event, rich], **manifest,
    )
    assert report["rejected"] == {}
    assert report["counts"] == {"selected": 1, "added": 1, "authors_filled": 0, "cards_enriched": 1}
    published = repair.publish_resource_detail_repair(
        f["root"], report, rollback_path=tmp_path / "prior-details.json",
    )
    assert published["published"]
    result = repository.query(session=target, persist_event_cache=False)
    enriched, = (item for item in result["messages"] if item["id"] == row["id"])
    assert enriched["sender_id"] == completed["sender_id"]
    assert enriched["sender_label"] == completed["sender_label"]
    assert json.loads(enriched["provenance"])["resource_reference"]["channels_description"] == "Exact Channels share"
    with closing(repair._readonly(f["history"])) as connection:
        _conversations, upstream, status = repository._timeline_tables(
            connection, _file_identity(f["history"]), conversation_id=target,
            strict=True, include_secondary=False, include_resource_details=False,
        )
        original = dict(connection.execute(
            f"SELECT * FROM {upstream} WHERE id=?", (row["id"],),
        ).fetchone())
    assert status in {"ready", "absent"}
    assert details.row_sha256(original) == details.row_sha256(row)


@pytest.mark.parametrize(("indexed", "native_text", "text_match"), [
    ("保留,文字,格式", "保留，文字，格式", "corroborated_fts_comma_width"),
    ("保留,文字?", "保留，文字？", "corroborated_fts_punctuation_width"),
])
def test_manifest_can_fill_only_an_exact_ordinary_group_text_sender(
    repair_fixture, tmp_path, indexed, native_text, text_match,
):
    f = repair_fixture
    packet = deepcopy(f.packet)
    packet["messages"] = [evidence.native._message(81, 1, raw=native_text)]
    witness = packet["messages"][0]
    owner = "wxid_second_viewer"
    partition = "ios-" + hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest()
    target = "wechat:" + _identifier(partition, packet["peer_id"])
    conversation = {
        **f.conversation, "id": target, "partition_id": partition, "account_key": "account:" + owner,
        "message_count": 1,
    }
    stamp = datetime.fromtimestamp(witness["create_time"], UTC).isoformat()
    order, value, label, basis = _time_fields({"sent_at_utc_iso": stamp})
    row = dict(f.row)
    row.update(
        id=hashlib.sha256(b"ordinary text awaiting sender").hexdigest(),
        conversation_id=target, ordinal=147, text=indexed, search_text=indexed,
        sender_id=None, sender_label="Unknown sender", direction="received",
        time_sort=order, timestamp_value=value, timestamp_label=label, time_basis=basis,
        quality_flags='["sender_unavailable_in_search_index"]',
    )
    proof = json.loads(row["provenance"])
    proof.pop("resource_reference")
    proof.update(
        message_type=1, text_extraction_kind="plain_text", server_id=witness["server_id"],
        unix_timestamp=witness["create_time"],
    )
    proof["locator"].update(
        text_origin="corroborated_recovery_index",
        corroboration=[{
            "database": "fts/fts_message.db", "table": "fts5_message_table_1_content",
            "row_id": 4,
        }],
    )
    row["provenance"] = json.dumps(proof)
    with sqlite3.connect(f.history) as connection:
        connection.execute(
            "INSERT INTO partitions VALUES(?,?,?,?,?,?,?,?)",
            (partition, "account:" + owner, owner, owner, "Verified archive ID", "iPhone backup", "", ""),
        )
        connection.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(conversation[column] for column in completion_evidence.completions.CONVERSATION_COLUMNS),
        )
        connection.execute(
            "INSERT INTO messages VALUES(" + ",".join("?" for _ in details.COLUMNS) + ")",
            tuple(row[column] for column in details.COLUMNS),
        )
    manifest = {
        "target_sessions": {row["id"]: target},
        "expected_rows": {row["id"]: details.row_sha256(row)},
    }
    different = deepcopy(packet)
    different["messages"] = [evidence.native._message(81, 1, raw="替换，文字，格式")]
    rejected = repair.stage_resource_detail_repair(
        f.root, tmp_path / "different-text.json", [different], **manifest,
    )
    assert rejected["rejected"] == {"native_evidence_conflict": 1}
    report = repair.stage_resource_detail_repair(
        f.root, tmp_path / "text-sender.json", [packet], **manifest,
    )
    assert report["counts"]["authors_filled"] == 1
    assert report["counts"]["cards_enriched"] == 0
    assert report["rejected"] == {}
    assert _publish(f, report, tmp_path / "prior-text-sender.json")["published"]
    page = WechatArchiveRepository(f.root).query(session=target, persist_event_cache=False)
    edited, = (item for item in page["messages"] if item["id"] == row["id"])
    assert edited["sender_id"] == witness["sender_id"]
    assert all(edited[key] == row[key] for key in ("id", "text", "time_sort", "timestamp_value", "ordinal"))
    enriched = json.loads(edited["provenance"])
    assert enriched.pop("native_text_sender_details")["text_match"] == text_match
    assert enriched == proof
    with sqlite3.connect(f.history.with_name("contacts.sqlite3")) as connection:
        identifier = insert_contact(connection, witness["sender_id"])
        bind(
            connection, identifier, source=partition, sender=witness["sender_id"],
            room=packet["peer_id"],
        )
        observe(
            connection, identifier, "nickname", "Native nickname",
            source=partition, snapshot="", owner=owner,
        )
        observe(
            connection, identifier, "room_display_name", "Verified group label",
            source=partition, snapshot="", owner=owner, room=packet["peer_id"],
        )
    result = query_wechat_history(f.root, session=target, persist_event_cache=False)
    repaired, = (item for item in result.items if item.stable_id == row["id"])
    assert repaired.author_label == witness["sender_id"]
    assert repaired.messaging["viewer_display_name"] == "Verified group label"


def test_changed_captured_upstream_is_skipped_without_losing_prior_repairs(
    repair_fixture, tmp_path,
):
    f = repair_fixture
    row, packet = _second(f)
    captured = {row["id"]: details.row_sha256(row)}
    target = evidence._registry(f)
    before = target.read_bytes(), details.registry_identity(f.history)
    with sqlite3.connect(f.history) as connection:
        connection.execute(
            "UPDATE messages SET sender_label='Concurrent source correction' WHERE id=?",
            (row["id"],),
        )
    source_bytes = _source_bytes(f)
    report = repair.stage_resource_detail_repair(
        f.root, tmp_path / "changed-capture.json", [packet], expected_rows=captured,
    )
    assert report["rejected"]["upstream_changed_since_capture"] == 1
    assert report["counts"].get("added", 0) == 0
    assert report["preserved_entries"] == report["total_entries"] == 1
    assert report["changed"] is False
    receipt = _publish(f, report, tmp_path / "no-changed-capture-rollback.json")
    assert receipt["unchanged"] is True
    assert (target.read_bytes(), details.registry_identity(f.history)) == before
    assert _source_bytes(f) == source_bytes


@pytest.mark.parametrize("invalidity", ["message_id", "row_sha256", "selected_scope", "empty"])
def test_invalid_capture_manifest_cannot_create_an_unbounded_repair(
    repair_fixture, tmp_path, invalidity,
):
    f = repair_fixture
    captured = {f.row["id"]: details.row_sha256(f.row)}
    selected = None
    if invalidity == "message_id":
        captured = {"not-a-canonical-message": details.row_sha256(f.row)}
    elif invalidity == "row_sha256":
        captured[f.row["id"]] = "not-a-complete-row-digest"
    elif invalidity == "selected_scope":
        selected = [hashlib.sha256(b"not-in-reviewed-capture").hexdigest()]
    else:
        captured = {}
    before = _source_bytes(f)
    candidate = tmp_path / "unaccepted-manifest.json"
    with pytest.raises(ValueError):
        repair.stage_resource_detail_repair(
            f.root, candidate, [f.packet], message_ids=selected, expected_rows=captured,
        )
    assert not candidate.exists()
    assert not f.history.with_name(details.FILENAME).exists()
    assert _source_bytes(f) == before
