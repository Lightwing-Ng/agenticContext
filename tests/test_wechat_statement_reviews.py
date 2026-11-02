"""Reviewed copies retain every source reference and have reversible membership.

Code version: v1.0.2-codex.0
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import sqlite3

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core import wechat_statement_reviews as reviews
from app.core.wechat_contacts import canonical_contact_id
from tests import wechat_review_fixtures as packet_fixtures


overlap_store = packet_fixtures.overlap_store
OWNER = packet_fixtures.OWNER
AUTHOR = packet_fixtures.AUTHOR
OTHER = packet_fixtures.OTHER
FORWARDER = packet_fixtures.FORWARDER
CONTACT = canonical_contact_id(AUTHOR, authority="native_message_sender")


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _proof(fixture, parent):
    return json.loads(
        fixture.source.execute(
            "SELECT provenance FROM messages WHERE id=?", (parent,)
        ).fetchone()[0]
    )


def _write_proof(fixture, parent, proof):
    fixture.source.execute(
        "UPDATE messages SET provenance=? WHERE id=?", (json.dumps(proof), parent)
    )


def _pins(fixture):
    result = []
    for parent in fixture.parents:
        row = fixture.source.execute(
            "SELECT text,provenance FROM messages WHERE id=?", (parent,)
        ).fetchone()
        result.append(
            {
                "message_id": parent,
                "text_sha256": _hash(row[0]),
                "provenance_sha256": _hash(reviews._json(json.loads(row[1]))),
            }
        )
    return result


def _save_review(fixture):
    declaration = {
        "authority": "user_confirmed_transcript_copy",
        "owner_account_id": OWNER,
        "preferred_parent_message_id": fixture.group,
        "parents": _pins(fixture),
        "evidence_id": _hash("User confirmed complete packet copies"),
    }
    fixture.path.parent.mkdir(parents=True, exist_ok=True)
    fixture.path.write_text(
        json.dumps({"schema_version": 1, "packet_reviews": [declaration]})
    )
    return declaration


def _tables(connection):
    return {
        table: list(connection.execute("SELECT * FROM " + table + " ORDER BY 1"))
        for table in ("refs", "statement_groups", "statement_members")
    }


def _apply(fixture):
    return reviews.apply_reviewed_packet_groups(
        fixture.root,
        fixture.source,
        fixture.output,
        CONTACT,
        row_select=packet_fixtures.SELECT,
        prepare_rows=packet_fixtures._noop_prepare,
        native_thread_key=analysis._native_conversation_key,
    )


def _add_html(fixture, parent, label, local, snapshot):
    native = dict(
        fixture.source.execute(
            "SELECT * FROM messages WHERE id=?", (parent,)
        ).fetchone()
    )
    native_proof = json.loads(native["provenance"])
    parts = []
    identifier = _hash(label)
    source = "html-" + identifier[:24]
    for component in native_proof["text_components"]:
        if component["kind"] != "forwarded_text":
            continue
        stamp = datetime.fromtimestamp(
            int(component["source_create_time"]),
            datetime.fromisoformat(native["timestamp_value"]).tzinfo,
        )
        parts.append(
            {
                "export_msgid": local + "_" + component["source_data_id"],
                "sender_native_id": component["source_sender_id"],
                "sender_label": "Export source label",
                "raw_timestamp": stamp.strftime("%Y-%m-%d %H:%M:%S"),
                "text": component["text"],
                "text_kind": "forwarded_rendered_text",
                "forwarded_components": [],
            }
        )
    proof = {
        "text_extraction_kind": "forwarded_text",
        "source_snapshot": snapshot,
        "sender_native_id": native["sender_id"],
        "export_msgid_namespace": "MesLocalID",
        "locator": {
            "export_msgid": local,
            "export_msgid_namespace": "MesLocalID",
            "export_msgtype": "49",
        },
        "forwarded_components": parts,
    }
    native.update(
        id=identifier,
        source_id=source,
        identity_status="Corroborated export ID",
        sender_id="mutable-parent-export-id",
        provenance=json.dumps(proof),
    )
    fixture.source.execute(
        "INSERT INTO messages VALUES(" + ",".join("?" for _ in native) + ")",
        tuple(native.values()),
    )
    fixture.source.execute(
        "INSERT INTO source_snapshots VALUES(?,?,?,?)",
        (
            snapshot,
            "html-snapshot-" + label,
            source,
            json.dumps(
                {
                    "owner_verified": True,
                    "source_account_id": OWNER,
                    "owner_evidence": {"native_username": OWNER},
                }
            ),
        ),
    )
    references = []
    for position, part in enumerate(parts, 1):
        if part["sender_native_id"] != AUTHOR:
            continue
        reference = identifier + "_forwarded_" + str(position)
        group = _hash("statement-" + reference)
        fixture.output.execute(
            "INSERT INTO refs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                reference,
                identifier,
                json.dumps([position]),
                "wechat_message_" + reference,
                native["conversation_id"],
                source,
                "account:" + OWNER,
                0,
                1,
                1,
                part["raw_timestamp"],
                part["raw_timestamp"],
                "Source clock; timezone unknown",
                part["raw_timestamp"],
            ),
        )
        fixture.output.execute(
            "INSERT INTO statement_groups VALUES(?,?,?)",
            (group, reference, native["conversation_id"]),
        )
        fixture.output.execute(
            "INSERT INTO statement_members VALUES(?,?)", (reference, group)
        )
        references.append(reference)
    return identifier, references


@pytest.fixture
def reviewed_store(overlap_store, tmp_path):
    fixture = overlap_store
    clock = datetime.fromisoformat("2021-09-16T15:00:00+08:00")
    parts = []
    for index in range(8):
        stamp = clock + timedelta(seconds=index * 17)
        part = packet_fixtures._part(index)
        part.update(
            source_timestamp=stamp.strftime("%Y-%m-%d %H:%M"),
            source_create_time=str(int(stamp.timestamp())),
            source_data_id=str(index),
            forwarded_item_path=[index + 1],
        )
        if index == 2:
            part.update(
                kind="forwarded_resource",
                text="",
                resource_kind="image",
                forwarded_data_type=2,
            )
        parts.append(part)
    private, first = packet_fixtures._add_packet(
        fixture,
        "reviewed-private",
        parts,
        sender=FORWARDER,
        kind="direct",
        conversation="private-source",
    )
    fixture.source.execute(
        "UPDATE messages SET peer_id=? WHERE id=?", (FORWARDER, private)
    )
    group, second = packet_fixtures._add_packet(
        fixture,
        "reviewed-group",
        parts,
        sender=OWNER,
        kind="group",
        conversation="group-source",
    )
    later = datetime.fromisoformat("2021-09-16T18:00:08+08:00")
    proof = _proof(fixture, group)
    proof.update(unix_timestamp=int(later.timestamp()), source_time=later.isoformat())
    _write_proof(fixture, group, proof)
    fixture.source.execute(
        "UPDATE messages SET direction='sent',timestamp_value=? WHERE id=?",
        (later.isoformat(), group),
    )
    fixture.root = tmp_path / "store"
    fixture.path = fixture.root / "messaging/wechat/statement_reviews.json"
    fixture.group, fixture.private = group, private
    hp, hp_refs = _add_html(fixture, private, "html-reviewed-private", "113", 3)
    hg, hg_refs = _add_html(fixture, group, "html-reviewed-group", "4928", 4)
    fixture.parents = [private, hp, hg, group]
    fixture.html_private, fixture.html_group = hp, hg
    fixture.group_refs = [part["id"] for part in second if part["sender_id"] == AUTHOR]
    fixture.private_refs = [part["id"] for part in first if part["sender_id"] == AUTHOR]
    fixture.html_refs = [hp_refs, hg_refs]
    _save_review(fixture)
    return fixture


def _copy_base(source):
    output = sqlite3.connect(":memory:")
    output.executescript(analysis._REFERENCE_SCHEMA)
    for table, rows in _tables(source).items():
        output.executemany(
            "INSERT INTO " + table + " VALUES(" + ",".join("?" for _ in rows[0]) + ")",
            rows,
        )
    return output


def test_complete_review_prefers_native_group_and_preserves_every_source(
    reviewed_store,
):
    f = reviewed_store
    original_source = tuple(f.source.iterdump())
    original_refs = _tables(f.output)["refs"]
    count = len(original_refs)
    result = _apply(f)
    assert result == {
        "status": "applied",
        "reviews_applied": 1,
        "statements_joined": 9,
        "members_retained": 12,
    }
    assert _tables(f.output)["refs"] == original_refs
    assert tuple(f.source.iterdump()) == original_source
    assert (
        f.output.execute("SELECT COUNT(*) FROM statement_members").fetchone()[0]
        == count
    )
    assert {
        row[0] for row in f.output.execute("SELECT reference_id FROM statement_groups")
    } == set(f.group_refs)
    assert _apply(f)["status"] == "already_applied"


def test_prejoined_declared_groups_restore_exact_baseline_after_removal(reviewed_store):
    f = reviewed_store
    for private, html in zip(f.private_refs, f.html_refs[0]):
        winner = f.output.execute(
            "SELECT statement_id FROM statement_members WHERE reference_id=?",
            (private,),
        ).fetchone()[0]
        loser = f.output.execute(
            "SELECT statement_id FROM statement_members WHERE reference_id=?", (html,)
        ).fetchone()[0]
        f.output.execute(
            "UPDATE statement_members SET statement_id=? WHERE statement_id=?",
            (winner, loser),
        )
        f.output.execute("DELETE FROM statement_groups WHERE id=?", (loser,))
    baseline = _tables(f.output)
    assert _apply(f)["status"] == "applied"
    upgraded = _copy_base(f.output)
    try:
        restored = reviews.restore_reviewed_groups(f.output, upgraded)
        assert restored == {"restored_groups": 9, "restored_members": 12}
        assert _tables(upgraded) == baseline
        f.path.unlink()
        assert (
            reviews.apply_reviewed_packet_groups(
                f.root,
                f.source,
                upgraded,
                CONTACT,
                row_select=packet_fixtures.SELECT,
                prepare_rows=packet_fixtures._noop_prepare,
                native_thread_key=analysis._native_conversation_key,
            )["status"]
            == "missing"
        )
        assert _tables(upgraded) == baseline
    finally:
        upgraded.close()


@pytest.mark.parametrize(
    "damage",
    [
        "text_pin",
        "proof_pin",
        "owner",
        "preferred_html",
        "wrong_snapshot",
        "missing_seconds",
        "clock_conflict",
        "child_author",
        "child_body",
        "reordered",
        "omitted",
        "data_id",
        "resource",
        "server_id",
        "original_chat",
        "duplicate_text",
        "html_body",
        "html_author",
        "html_clock",
        "html_suffix",
        "html_owner",
        "html_nested",
        "html_quoted",
        "html_parent_clock",
    ],
)
def test_changed_or_incomplete_evidence_rejects_without_partial_membership(
    reviewed_store, damage
):
    f = reviewed_store
    proof = _proof(f, f.group)
    html = _proof(f, f.html_group)
    if damage == "wrong_snapshot":
        proof["source_snapshot"] = 2
    elif damage == "missing_seconds":
        proof["text_components"][0]["source_create_time"] = ""
    elif damage == "clock_conflict":
        proof["text_components"][0]["source_timestamp"] = "2021-09-16 15:59"
    elif damage == "child_author":
        proof["text_components"][0]["source_sender_id"] = "wxid_someone_else"
    elif damage == "child_body":
        proof["text_components"][0]["text"] += " changed"
    elif damage == "reordered":
        proof["text_components"][0], proof["text_components"][1] = (
            proof["text_components"][1],
            proof["text_components"][0],
        )
    elif damage == "omitted":
        proof["omitted_forwarded_items"] = 1
    elif damage == "data_id":
        proof["text_components"][0]["source_data_id"] = "99"
    elif damage == "resource":
        proof["text_components"][2]["resource_kind"] = "video"
    elif damage in {"server_id", "original_chat"}:
        private = _proof(f, f.private)
        if damage == "server_id":
            private["text_components"][0]["source_ids"] = {"svrid": "111"}
            proof["text_components"][0]["source_ids"] = {"svrid": "222"}
        else:
            private["text_components"][0]["source_chat_id"] = "original-one@chatroom"
            proof["text_components"][0]["source_chat_id"] = "original-two@chatroom"
        _write_proof(f, f.private, private)
    elif damage == "duplicate_text":
        proof["text_components"][4] = deepcopy(proof["text_components"][0])
    elif damage == "html_body":
        html["forwarded_components"][0]["text"] += " changed"
    elif damage == "html_author":
        html["forwarded_components"][0]["sender_native_id"] = "wxid_namesake"
    elif damage == "html_clock":
        html["forwarded_components"][0]["raw_timestamp"] = "2021-09-16 15:00:01"
    elif damage == "html_suffix":
        html["forwarded_components"][0]["export_msgid"] = "4928_7"
    elif damage == "html_owner":
        f.source.execute("UPDATE source_snapshots SET metadata='{}' WHERE id=4")
    elif damage == "html_nested":
        html["forwarded_components"][0]["forwarded_components"] = [
            deepcopy(html["forwarded_components"][1])
        ]
    elif damage == "html_quoted":
        html["forwarded_components"][0]["quoted_components"] = [
            {"text": "Unreviewed quote"}
        ]
    elif damage == "html_parent_clock":
        f.source.execute(
            "UPDATE messages SET timestamp_value='2021-09-16T18:01:00+08:00' WHERE id=?",
            (f.html_group,),
        )
    _write_proof(f, f.group, proof)
    _write_proof(f, f.html_group, html)
    declaration = _save_review(f)
    if damage == "text_pin":
        declaration["parents"][0]["text_sha256"] = "0" * 64
    elif damage == "proof_pin":
        declaration["parents"][0]["provenance_sha256"] = "0" * 64
    elif damage == "owner":
        declaration["owner_account_id"] = "wxid_another_owner"
    elif damage == "preferred_html":
        declaration["preferred_parent_message_id"] = f.html_group
    f.path.write_text(
        json.dumps({"schema_version": 1, "packet_reviews": [declaration]})
    )
    baseline, source = _tables(f.output), tuple(f.source.iterdump())
    assert _apply(f)["status"] == "rejected"
    assert _tables(f.output) == baseline
    assert tuple(f.source.iterdump()) == source


@pytest.mark.parametrize(
    "source_ids",
    [
        {"svrid": "111", "dataitemsource/msgid": "222"},
        {"dataitemsource/msgid": "not-a-server-id"},
    ],
)
def test_legacy_server_alias_conflict_is_rejected(reviewed_store, source_ids):
    fixture = reviewed_store
    proof = _proof(fixture, fixture.group)
    proof["text_components"][0]["source_ids"] = source_ids
    _write_proof(fixture, fixture.group, proof)
    _save_review(fixture)
    baseline = _tables(fixture.output)
    assert _apply(fixture)["status"] == "rejected"
    assert _tables(fixture.output) == baseline


def test_undeclared_existing_members_fail_closed_on_apply_and_restore(reviewed_store):
    f = reviewed_store
    assert _apply(f)["status"] == "applied"
    output = _copy_base(f.output)
    try:
        group = output.execute("SELECT id FROM statement_groups LIMIT 1").fetchone()[0]
        output.execute("INSERT INTO statement_members VALUES('unexpected',?)", (group,))
        before = tuple(output.iterdump())
        with pytest.raises(ValueError, match="extra memberships"):
            reviews.restore_reviewed_groups(f.output, output)
        assert tuple(output.iterdump()) == before
    finally:
        output.close()


def test_digest_tracks_missing_invalid_and_present_registry(reviewed_store):
    f = reviewed_store
    present = reviews.reviews_digest(f.root)
    f.path.write_text('{"schema_version":1,"schema_version":1,"packet_reviews":[]}')
    invalid = reviews.reviews_digest(f.root)
    assert invalid != present
    assert _apply(f)["status"] == "rejected"
    f.path.unlink()
    missing = reviews.reviews_digest(f.root)
    assert missing not in {present, invalid}
    assert reviews.reviews_digest(f.root) == missing


def test_source_budget_rejection_is_atomic(reviewed_store, monkeypatch):
    f = reviewed_store
    before = _tables(f.output)
    monkeypatch.setattr(reviews, "MAX_SOURCE_BYTES", 1)
    assert _apply(f)["status"] == "rejected"
    assert _tables(f.output) == before


def test_established_rendered_app_html_role_uses_exact_native_packet(reviewed_store):
    f = reviewed_store
    for parent in (f.html_private, f.html_group):
        proof = _proof(f, parent)
        proof["text_extraction_kind"] = "rendered_app_text"
        _write_proof(f, parent, proof)
    _save_review(f)
    assert _apply(f)["status"] == "applied"
    assert {
        row[0] for row in f.output.execute("SELECT reference_id FROM statement_groups")
    } == set(f.group_refs)


@pytest.mark.parametrize(
    "change",
    [
        "unknown_html_role",
        "html_type",
        "boolean_type",
        "resource_server",
        "reference_id",
        "reference_anchor",
        "extra_member",
    ],
)
def test_unsupported_roles_and_reference_identity_drift_are_rejected(
    reviewed_store, change
):
    f = reviewed_store
    if change in {"unknown_html_role", "html_type"}:
        proof = _proof(f, f.html_group)
        if change == "unknown_html_role":
            proof["text_extraction_kind"] = "plain_text"
        else:
            proof["locator"]["export_msgtype"] = "1"
        _write_proof(f, f.html_group, proof)
    elif change == "boolean_type":
        proof = _proof(f, f.group)
        proof["text_components"][0]["original_message_type"] = True
        _write_proof(f, f.group, proof)
    elif change == "resource_server":
        for parent, server in ((f.group, "1111"), (f.private, "2222")):
            proof = _proof(f, parent)
            proof["text_components"][2]["source_ids"] = {"svrid": server}
            _write_proof(f, parent, proof)
    elif change == "reference_id":
        original = f.group_refs[0]
        changed = original + "_forwarded_1"
        f.output.execute("UPDATE refs SET id=? WHERE id=?", (changed, original))
        f.output.execute(
            "UPDATE statement_members SET reference_id=? WHERE reference_id=?",
            (changed, original),
        )
        f.output.execute(
            "UPDATE statement_groups SET reference_id=? WHERE reference_id=?",
            (changed, original),
        )
    elif change == "reference_anchor":
        f.output.execute(
            "UPDATE refs SET anchor_id='wrong' WHERE id=?", (f.group_refs[0],)
        )
    else:
        group = f.output.execute(
            "SELECT statement_id FROM statement_members WHERE reference_id=?",
            (f.group_refs[0],),
        ).fetchone()[0]
        f.output.execute(
            "INSERT INTO statement_members VALUES('undeclared-member',?)", (group,)
        )
    _save_review(f)
    before = _tables(f.output)
    assert _apply(f)["status"] == "rejected"
    assert _tables(f.output) == before


def test_full_seconds_can_stand_alone_but_minute_copy_needs_own_epoch(reviewed_store):
    f = reviewed_store
    for parent in (f.private, f.group):
        proof = _proof(f, parent)
        for part in proof["text_components"]:
            stamp = datetime.fromtimestamp(
                int(part["source_create_time"]),
                datetime.fromisoformat("2021-09-16T15:00:00+08:00").tzinfo,
            )
            part["source_timestamp"] = stamp.strftime("%Y-%m-%d %H:%M:%S")
            part["source_create_time"] = ""
        _write_proof(f, parent, proof)
    _save_review(f)
    assert _apply(f)["status"] == "applied"


def test_restoration_rejects_extra_members_in_a_previously_removed_group(
    reviewed_store,
):
    f = reviewed_store
    assert _apply(f)["status"] == "applied"
    upgraded = _copy_base(f.output)
    try:
        current = {
            row[0] for row in upgraded.execute("SELECT id FROM statement_groups")
        }
        removed = next(
            row[0]
            for row in f.output.execute("SELECT id FROM reviewed_statement_groups")
            if row[0] not in current
        )
        upgraded.execute(
            "INSERT INTO statement_members VALUES('outside-member',?)", (removed,)
        )
        before = tuple(upgraded.iterdump())
        with pytest.raises(ValueError, match="extra memberships"):
            reviews.restore_reviewed_groups(f.output, upgraded)
        assert tuple(upgraded.iterdump()) == before
    finally:
        upgraded.close()


def test_restoration_rejects_incomplete_or_misassigned_journal(reviewed_store):
    f = reviewed_store
    assert _apply(f)["status"] == "applied"
    upgraded = _copy_base(f.output)
    try:
        groups = list(
            f.output.execute(
                "SELECT id,reference_id FROM reviewed_statement_groups ORDER BY id"
            )
        )
        f.output.execute(
            "UPDATE reviewed_statement_members SET statement_id=? WHERE reference_id=?",
            (groups[1][0], groups[0][1]),
        )
        before = tuple(upgraded.iterdump())
        with pytest.raises(ValueError, match="inconsistent"):
            reviews.restore_reviewed_groups(f.output, upgraded)
        assert tuple(upgraded.iterdump()) == before
        f.output.execute("DROP TABLE reviewed_statement_members")
        with pytest.raises(ValueError, match="incomplete"):
            reviews.restore_reviewed_groups(f.output, upgraded)
        assert tuple(upgraded.iterdump()) == before
    finally:
        upgraded.close()


def test_pinned_digest_rejects_replacement_before_validation(reviewed_store):
    fixture = reviewed_store
    expected = reviews.reviews_digest(fixture.root)
    original = _tables(fixture.output)
    fixture.path.write_text('{"schema_version":1,"packet_reviews":[]}')
    with pytest.raises(reviews.StatementReviewSourceChanged, match="changed"):
        reviews.apply_reviewed_packet_groups(
            fixture.root,
            fixture.source,
            fixture.output,
            CONTACT,
            row_select=packet_fixtures.SELECT,
            prepare_rows=packet_fixtures._noop_prepare,
            native_thread_key=analysis._native_conversation_key,
            expected_digest=expected,
        )
    assert _tables(fixture.output) == original


def test_changed_registry_during_validation_does_not_apply_partial_groups(
    reviewed_store, monkeypatch
):
    fixture = reviewed_store
    original = _tables(fixture.output)
    plan = reviews._packet_plan

    def replace_after_proof(*args, **kwargs):
        result = plan(*args, **kwargs)
        fixture.path.write_text('{"schema_version":1,"packet_reviews":[]}')
        return result

    monkeypatch.setattr(reviews, "_packet_plan", replace_after_proof)
    with pytest.raises(reviews.StatementReviewSourceChanged, match="changed"):
        _apply(fixture)
    assert _tables(fixture.output) == original
