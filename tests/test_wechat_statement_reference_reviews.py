"""Explicit reference reviews preserve accepted observations and undo exactly.

Code version: v1.0.1-codex.0
"""

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json

import pytest

from app.core import wechat_statement_reviews as reviews
from tests import test_wechat_forwarded_overlap as packets
from tests import test_wechat_statement_reviews as legacy


overlap_store = packets.overlap_store
reviewed_store = legacy.reviewed_store


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _member(fixture, reference):
    cursor = fixture.output.execute("SELECT * FROM refs WHERE id=?", (reference,))
    ref = dict(
        zip((part[0] for part in cursor.description), cursor.fetchone(), strict=True)
    )
    row = dict(
        fixture.source.execute(
            "SELECT * FROM messages WHERE id=?", (ref["parent_message_id"],)
        ).fetchone()
    )
    proof = json.loads(row["provenance"])
    path = json.loads(ref["component_path"])
    if not path:
        body = row["text"]
    elif path[0] == "native":
        body = proof["text_components"][path[1] - 1]["text"]
    else:
        body = proof["forwarded_components"][path[0] - 1]["text"]
    snapshot = fixture.source.execute(
        "SELECT snapshot_id FROM source_snapshots WHERE id=?",
        (proof["source_snapshot"],),
    ).fetchone()[0]
    return {
        "reference_id": reference,
        "parent_message_id": row["id"],
        "component_path": path,
        "source_id": row["source_id"],
        "account_key": row["account_key"],
        "source_snapshot_id": snapshot,
        "parent_text_sha256": _hash(row["text"]),
        "provenance_sha256": _hash(_json(proof)),
        "body_sha256": _hash(body),
    }


def _review(fixture, references, preferred, policy="earliest_forward"):
    return {
        "authority": "user_confirmed_statement_copy",
        "contact_id": legacy.CONTACT,
        "preferred_reference_id": preferred,
        "selection_policy": policy,
        "members": [_member(fixture, reference) for reference in references],
        "evidence_id": _hash("Explicit reviewed cohort " + preferred),
    }


def _save(fixture, declarations, packet_reviews=()):
    registry = {
        "schema_version": 2,
        "packet_reviews": list(packet_reviews),
        "reference_reviews": declarations,
    }
    fixture.path.write_text(_json(registry))
    return registry


def _cohort(fixture, number=0, *, preferred=None, policy="earliest_forward"):
    refs = [
        fixture.private_refs[number],
        fixture.group_refs[number],
        *(values[number] for values in fixture.html_refs),
    ]
    return _review(fixture, refs, preferred or fixture.private_refs[number], policy)


@pytest.fixture
def reference_store(reviewed_store):
    fixture = reviewed_store
    for row in fixture.source.execute("SELECT * FROM source_snapshots").fetchall():
        metadata = json.loads(row["metadata"])
        metadata["source_label"] = "iPad archive"
        if row["source_id"].startswith("html-"):
            metadata["source_sha256"] = _hash("Pinned HTML archive")
        fixture.source.execute(
            "UPDATE source_snapshots SET metadata=? WHERE id=?",
            (json.dumps(metadata), row["id"]),
        )
    for parent in (fixture.html_private, fixture.html_group):
        proof = legacy._proof(fixture, parent)
        proof["locator"].update(
            archive_sha256=_hash("Pinned HTML archive"),
            member_path="html/verified-conversation.html",
            member={"sha256": _hash("Literal archive member")},
        )
        legacy._write_proof(fixture, parent, proof)
    return fixture


def test_first_forward_and_disjoint_positions_share_parent_pins(reference_store):
    f = reference_store
    baseline, source = legacy._tables(f.output), tuple(f.source.iterdump())
    declarations = [_cohort(f, number) for number in (0, 1)]
    _save(f, declarations)
    assert legacy._apply(f) == {
        "status": "applied",
        "reviews_applied": 2,
        "statements_joined": 6,
        "members_retained": 8,
    }
    assert legacy._tables(f.output)["refs"] == baseline["refs"]
    assert tuple(f.source.iterdump()) == source
    assert legacy._apply(f)["status"] == "already_applied"
    restored = legacy._copy_base(f.output)
    try:
        assert reviews.restore_reviewed_groups(f.output, restored) == {
            "restored_groups": 8,
            "restored_members": 8,
        }
        assert legacy._tables(restored) == baseline
    finally:
        restored.close()


def test_native_group_chooses_ipad_copy_and_keeps_all_source_clocks(reference_store):
    f = reference_store
    before = legacy._tables(f.output)["refs"]
    review = _cohort(f, preferred=f.group_refs[0], policy="native_group")
    _save(f, [review])
    assert legacy._apply(f)["status"] == "applied"
    assert legacy._tables(f.output)["refs"] == before
    assert f.output.execute(
        "SELECT reference_id FROM statement_groups WHERE reference_id=?",
        (f.group_refs[0],),
    ).fetchone()


@pytest.mark.parametrize(
    "source_clock", ["2021-09-16T15:00:00+08:00", "2021-09-16T07:00:00+00:00"]
)
def test_unsupported_source_clock_is_not_replaced_by_a_positive_child_epoch(
    reference_store, source_clock
):
    f = reference_store
    proof = legacy._proof(f, f.group)
    proof["text_components"][0]["source_timestamp"] = source_clock
    legacy._write_proof(f, f.group, proof)
    _save(f, [_cohort(f)])
    before = legacy._tables(f.output)
    assert legacy._apply(f)["status"] == "rejected"
    assert legacy._tables(f.output) == before


def test_clock_conflict_is_rejected_when_every_accepted_reference_matches_its_own_source(
    reference_store,
):
    f = reference_store
    proof = legacy._proof(f, f.group)
    child = proof["text_components"][0]
    child["source_create_time"] = str(int(child["source_create_time"]) + 1)
    legacy._write_proof(f, f.group, proof)
    f.output.execute(
        "UPDATE refs SET timestamp_value='2021-09-16T15:00:01+08:00' WHERE id=?",
        (f.group_refs[0],),
    )
    _save(f, [_cohort(f)])
    before = legacy._tables(f.output)
    assert legacy._apply(f)["status"] == "rejected"
    assert legacy._tables(f.output) == before


def test_an_undeclared_existing_member_cannot_be_silently_absorbed(reference_store):
    f = reference_store
    group = f.output.execute(
        "SELECT statement_id FROM statement_members WHERE reference_id=?",
        (f.private_refs[0],),
    ).fetchone()[0]
    other_group = f.output.execute(
        "SELECT statement_id FROM statement_members WHERE reference_id=?",
        (f.private_refs[1],),
    ).fetchone()[0]
    f.output.execute(
        "UPDATE statement_members SET statement_id=? WHERE reference_id=?",
        (group, f.private_refs[1]),
    )
    f.output.execute("DELETE FROM statement_groups WHERE id=?", (other_group,))
    _save(f, [_cohort(f)])
    before = legacy._tables(f.output)
    assert legacy._apply(f)["status"] == "rejected"
    assert legacy._tables(f.output) == before


def test_reviewed_short_html_pair_needs_complete_verified_source_frame(reference_store):
    f = reference_store
    review = _review(
        f, [f.group_refs[0], f.html_refs[1][0]], f.group_refs[0], "native_source"
    )
    _save(f, [review])
    assert legacy._apply(f)["status"] == "applied"
    assert (
        f.output.execute("SELECT COUNT(*) FROM statement_members").fetchone()[0] == 12
    )


def test_explicit_review_accepts_a_pinned_empty_legacy_child_id(reference_store):
    f = reference_store
    for parent in (f.html_private, f.html_group):
        proof = legacy._proof(f, parent)
        proof["forwarded_components"][0]["export_msgid"] = (
            proof["locator"]["export_msgid"] + "_"
        )
        legacy._write_proof(f, parent, proof)
    before, raw = legacy._tables(f.output), tuple(f.source.iterdump())
    _save(f, [])
    assert legacy._apply(f)["status"] == "empty"
    assert legacy._tables(f.output) == before
    _save(f, [_cohort(f, preferred=f.group_refs[0], policy="native_group")])
    assert legacy._apply(f) == {
        "status": "applied",
        "reviews_applied": 1,
        "statements_joined": 3,
        "members_retained": 4,
    }
    assert legacy._tables(f.output)["refs"] == before["refs"]
    assert tuple(f.source.iterdump()) == raw
    assert legacy._apply(f)["status"] == "already_applied"
    restored = legacy._copy_base(f.output)
    try:
        reviews.restore_reviewed_groups(f.output, restored)
        assert legacy._tables(restored) == before
    finally:
        restored.close()


def test_empty_legacy_child_id_still_needs_the_current_provenance_pin(reference_store):
    f = reference_store
    review = _cohort(f, preferred=f.group_refs[0], policy="native_group")
    proof = legacy._proof(f, f.html_group)
    proof["forwarded_components"][0]["export_msgid"] = (
        proof["locator"]["export_msgid"] + "_"
    )
    legacy._write_proof(f, f.html_group, proof)
    _save(f, [review])
    before = legacy._tables(f.output)
    assert legacy._apply(f)["status"] == "rejected"
    assert legacy._tables(f.output) == before


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_key",
        "null_id",
        "wrong_parent_prefix",
        "whitespace_suffix",
        "nonnumeric_suffix",
        "empty_value",
        "numeric_without_parent",
    ],
)
def test_empty_legacy_id_support_never_accepts_an_unpinned_component_identity(
    reference_store, mutation
):
    f = reference_store
    proof = legacy._proof(f, f.html_group)
    component = proof["forwarded_components"][0]
    prefix = proof["locator"]["export_msgid"] + "_"
    if mutation == "missing_key":
        del component["export_msgid"]
    else:
        component["export_msgid"] = {
            "null_id": None,
            "wrong_parent_prefix": "another-parent_",
            "whitespace_suffix": prefix + " ",
            "nonnumeric_suffix": prefix + "invalid",
            "empty_value": "",
            "numeric_without_parent": "0",
        }[mutation]
    legacy._write_proof(f, f.html_group, proof)
    _save(f, [_cohort(f, preferred=f.group_refs[0], policy="native_group")])
    before, raw = legacy._tables(f.output), tuple(f.source.iterdump())
    assert legacy._apply(f)["status"] == "rejected"
    assert legacy._tables(f.output) == before
    assert tuple(f.source.iterdump()) == raw


@pytest.mark.parametrize(
    "field",
    [
        "archive_sha256",
        "member",
        "member_path",
        "export_msgid_namespace",
        "export_msgtype",
    ],
)
def test_short_html_review_rejects_incomplete_or_unsupported_frames(
    reference_store, field
):
    f = reference_store
    proof = legacy._proof(f, f.html_group)
    del proof["locator"][field]
    legacy._write_proof(f, f.html_group, proof)
    _save(
        f,
        [
            _review(
                f,
                [f.group_refs[0], f.html_refs[1][0]],
                f.group_refs[0],
                "native_source",
            )
        ],
    )
    before = legacy._tables(f.output)
    assert legacy._apply(f)["status"] == "rejected"
    assert legacy._tables(f.output) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("parent_text_sha256", "0" * 64),
        ("provenance_sha256", "0" * 64),
        ("body_sha256", "0" * 64),
        ("source_snapshot_id", "another-snapshot"),
        ("source_id", "ios-" + "0" * 32),
        ("account_key", "account:wxid_another_owner"),
        ("component_path", ["native", 2]),
        ("parent_message_id", "0" * 64),
    ],
)
def test_each_member_source_pin_is_required_and_rejection_is_atomic(
    reference_store, field, value
):
    f = reference_store
    declarations = [_cohort(f, number) for number in (0, 1)]
    declarations[1]["members"][-1][field] = value
    _save(f, declarations)
    before = legacy._tables(f.output)
    assert legacy._apply(f)["status"] == "rejected"
    assert legacy._tables(f.output) == before


@pytest.mark.parametrize(
    "mutation",
    [
        "later",
        "html_preference",
        "wrong_contact",
        "overlap",
        "duplicate",
        "wrong_owner",
        "wrong_body",
        "resource",
        "server",
        "chat",
        "numeric_clock",
        "second_clock",
        "frame_hash",
        "not_ipad",
    ],
)
def test_semantic_conflicts_fail_even_after_provenance_is_repinned(
    reference_store, mutation
):
    f = reference_store
    if mutation in {
        "wrong_owner",
        "wrong_body",
        "resource",
        "server",
        "chat",
        "numeric_clock",
        "second_clock",
    }:
        proof = legacy._proof(f, f.group)
        child = proof["text_components"][0]
        if mutation == "wrong_owner":
            child["source_sender_id"] = legacy.OTHER
        elif mutation == "wrong_body":
            child["text"] += " changed"
        elif mutation == "resource":
            child["kind"] = "forwarded_resource"
        elif mutation == "server":
            child["source_ids"] = {"svrid": "123"}
            first = legacy._proof(f, f.private)
            first["text_components"][0]["source_ids"] = {"svrid": "456"}
            legacy._write_proof(f, f.private, first)
        elif mutation == "chat":
            child["source_chat_id"] = "different@chatroom"
            first = legacy._proof(f, f.private)
            first["text_components"][0]["source_chat_id"] = "original@chatroom"
            legacy._write_proof(f, f.private, first)
        elif mutation == "numeric_clock":
            child["source_create_time"] = str(int(child["source_create_time"]) + 60)
        else:
            child["source_create_time"] = str(int(child["source_create_time"]) + 1)
        legacy._write_proof(f, f.group, proof)
    if mutation in {"frame_hash", "not_ipad"}:
        snapshot = 4 if mutation == "frame_hash" else 1
        row = f.source.execute(
            "SELECT metadata FROM source_snapshots WHERE id=?", (snapshot,)
        ).fetchone()
        metadata = json.loads(row[0])
        metadata["source_sha256" if mutation == "frame_hash" else "source_label"] = (
            "invalid"
        )
        f.source.execute(
            "UPDATE source_snapshots SET metadata=? WHERE id=?",
            (json.dumps(metadata), snapshot),
        )
    review = _cohort(f)
    if mutation == "later":
        review["preferred_reference_id"] = f.group_refs[0]
    elif mutation == "html_preference":
        review["preferred_reference_id"] = f.html_refs[0][0]
    elif mutation == "wrong_contact":
        review["contact_id"] = "wechat:contact:" + "0" * 64
    elif mutation == "not_ipad":
        review["selection_policy"] = "native_source"
    elif mutation == "duplicate":
        review["members"].append(deepcopy(review["members"][0]))
    declarations = [review]
    if mutation == "overlap":
        duplicate = deepcopy(review)
        duplicate["evidence_id"] = _hash(
            "Another review of the same accepted references"
        )
        declarations.append(duplicate)
    _save(f, declarations)
    before = legacy._tables(f.output)
    expected = "no_contact_members" if mutation == "wrong_contact" else "rejected"
    assert legacy._apply(f)["status"] == expected
    assert legacy._tables(f.output) == before


def test_cross_owner_minute_clock_review_keeps_unknown_seconds(reference_store):
    f = reference_store
    owner = "wxid_second_verified_owner"
    source = "ios-" + hashlib.md5(owner.encode()).hexdigest()
    f.source.execute(
        "INSERT INTO source_snapshots VALUES(?,?,?,?)",
        (
            5,
            "second-owner-ipad",
            source,
            _json(
                {
                    "source_account_id": owner,
                    "account_hash_verified": True,
                    "account_hash": hashlib.md5(owner.encode()).hexdigest(),
                    "source_label": "iPad archive",
                }
            ),
        ),
    )
    for parent, snapshot, owner_id, source_id in [
        (f.private, 1, legacy.OWNER, packets.SOURCE),
        (f.group, 5, owner, source),
    ]:
        proof = legacy._proof(f, parent)
        proof["source_snapshot"] = snapshot
        proof["text_components"][0].update(
            source_timestamp="2021-09-16 15:00", source_create_time="0"
        )
        legacy._write_proof(f, parent, proof)
        f.source.execute(
            "UPDATE messages SET source_id=?,account_id=?,account_key=?,container_account_key=?,sender_id=?,direction='received' WHERE id=?",
            (
                source_id,
                owner_id,
                "account:" + owner_id,
                "account:" + owner_id,
                legacy.FORWARDER,
                parent,
            ),
        )
        f.output.execute(
            "UPDATE refs SET source_id=?,account_key=? WHERE parent_message_id=?",
            (source_id, "account:" + owner_id, parent),
        )
    for reference in (f.private_refs[0], f.group_refs[0]):
        f.output.execute(
            "UPDATE refs SET timestamp_value='2021-09-16T15:00:00',time_basis='Source clock; precision: minute (seconds unavailable)',source_clock='2021-09-16 15:00' WHERE id=?",
            (reference,),
        )
    review = _review(
        f, [f.private_refs[0], f.group_refs[0]], f.group_refs[0], "native_group"
    )
    _save(f, [review])
    before, raw = legacy._tables(f.output)["refs"], tuple(f.source.iterdump())
    assert legacy._apply(f)["status"] == "applied"
    assert legacy._tables(f.output)["refs"] == before
    assert tuple(f.source.iterdump()) == raw


def test_native_primary_is_supported_without_relabeling_a_forwarded_copy(
    reference_store,
):
    f = reference_store
    original = dict(
        f.source.execute("SELECT * FROM messages WHERE id=?", (f.group,)).fetchone()
    )
    proof = json.loads(original["provenance"])
    stamp = datetime.fromisoformat("2021-09-16T15:00:00+08:00")
    parent = _hash("Original native statement")
    proof.update(
        message_type=1,
        text_extraction_kind="plain_text",
        server_id="0",
        unix_timestamp=int(stamp.timestamp()),
        source_time=stamp.isoformat(),
    )
    original.update(
        id=parent,
        sender_id=legacy.AUTHOR,
        direction="received",
        text=proof["text_components"][0]["text"],
        timestamp_value=stamp.isoformat(),
        provenance=json.dumps(proof),
    )
    f.source.execute(
        "INSERT INTO messages VALUES(" + ",".join("?" for _ in original) + ")",
        tuple(original.values()),
    )
    group = _hash("Statement for original native text")
    f.output.execute(
        "INSERT INTO refs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            parent,
            parent,
            "[]",
            "wechat_message_" + parent,
            original["conversation_id"],
            original["source_id"],
            original["account_key"],
            0,
            1,
            1,
            stamp.isoformat(),
            "Source seconds",
            "UTC+08:00",
            stamp.isoformat(),
        ),
    )
    f.output.execute(
        "INSERT INTO statement_groups VALUES(?,?,?)",
        (group, parent, original["conversation_id"]),
    )
    f.output.execute("INSERT INTO statement_members VALUES(?,?)", (parent, group))
    _save(f, [_review(f, [parent, f.group_refs[0]], parent, "native_group")])
    before = legacy._tables(f.output)["refs"]
    assert legacy._apply(f)["status"] == "applied"
    assert legacy._tables(f.output)["refs"] == before


def test_legacy_packet_and_new_reference_reviews_apply_together(reference_store):
    f = reference_store
    packet = legacy._save_review(f)
    parts = [packets._part(0)]
    first, a = packets._add_packet(f, "Separate short first forward", parts)
    later, b = packets._add_packet(f, "Separate short second forward", parts)
    stamp = datetime.fromisoformat("2021-09-16T18:00:00+08:00") + timedelta(seconds=1)
    proof = legacy._proof(f, later)
    proof.update(unix_timestamp=int(stamp.timestamp()), source_time=stamp.isoformat())
    legacy._write_proof(f, later, proof)
    f.source.execute(
        "UPDATE messages SET timestamp_value=? WHERE id=?", (stamp.isoformat(), later)
    )
    assert first != later
    review = _review(f, [a[0]["id"], b[0]["id"]], a[0]["id"])
    _save(f, [review], [packet])
    before = legacy._tables(f.output)
    assert legacy._apply(f) == {
        "status": "applied",
        "reviews_applied": 2,
        "statements_joined": 10,
        "members_retained": 14,
    }
    restored = legacy._copy_base(f.output)
    try:
        reviews.restore_reviewed_groups(f.output, restored)
        assert legacy._tables(restored) == before
    finally:
        restored.close()
