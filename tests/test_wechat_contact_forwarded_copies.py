"""Compact repeated forwarded assertions while retaining independent source events.

Code version: v1.0.0-codex.0
"""

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import sqlite3

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core import wechat_contact_forwarded_copies as projection
from app.core.wechat_contacts import canonical_contact_id
from tests import test_wechat_forwarded_overlap as packets
from tests import test_wechat_statement_reference_reviews as reference_reviews
from tests import test_wechat_statement_reviews as reviews
from tests import test_wechat_statement_reviews_integration as integration


overlap_store = packets.overlap_store
reviewed_store = reviews.reviewed_store
retained_review_store = integration.retained_review_store
AUTHOR = packets.AUTHOR
CONTACT = canonical_contact_id(AUTHOR, authority="native_message_sender")


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _apply(fixture, **kwargs):
    return projection.fold_contact_forwarded_copies(
        fixture.source,
        fixture.output,
        CONTACT,
        AUTHOR,
        row_select=packets.SELECT,
        prepare_rows=packets._noop_prepare,
        native_thread_key=analysis._native_conversation_key,
        **kwargs,
    )


def _snapshot(output):
    return {
        table: output.execute("SELECT * FROM " + table + " ORDER BY 1").fetchall()
        for table in ("refs", "statement_groups", "statement_members")
    }


def _update_proof(fixture, parent, change):
    proof = reviews._proof(fixture, parent)
    change(proof)
    reviews._write_proof(fixture, parent, proof)


def _refresh_ref(fixture, parent, *, timestamp=None):
    proof = reviews._proof(fixture, parent)
    if timestamp is None:
        timestamp = next(
            analysis.native_details(proof, parent_id=parent, account_id=packets.OWNER)
        )["timestamp_value"]
    fixture.output.execute(
        "UPDATE refs SET timestamp_value=? WHERE parent_message_id=?",
        (timestamp, parent),
    )


@pytest.fixture
def projection_store(overlap_store):
    fixture = overlap_store
    for row in fixture.source.execute("SELECT * FROM source_snapshots").fetchall():
        metadata = json.loads(row["metadata"])
        metadata["source_label"] = (
            "iPad archive" if row["id"] == 1 else "iPhone archive"
        )
        fixture.source.execute(
            "UPDATE source_snapshots SET metadata=? WHERE id=?",
            (json.dumps(metadata), row["id"]),
        )
    part = packets._part(0)
    part["source_create_time"] = str(
        int(datetime.fromisoformat("2021-09-16T15:00:00+08:00").timestamp())
    )
    fixture.first, a = packets._add_packet(fixture, "First native receipt", [part])
    fixture.second, b = packets._add_packet(fixture, "Second native receipt", [part])
    fixture.native_refs = [a[0]["id"], b[0]["id"]]
    later = datetime.fromisoformat("2021-09-16T18:00:17+08:00")
    _update_proof(
        fixture,
        fixture.second,
        lambda proof: proof.update(
            unix_timestamp=int(later.timestamp()),
            source_time=later.isoformat(),
            server_id="7123456789012345678",
        ),
    )
    fixture.source.execute(
        "UPDATE messages SET timestamp_value=?,ordinal=2 WHERE id=?",
        (later.isoformat(), fixture.second),
    )
    fixture.html, fixture.html_refs = reviews._add_html(
        fixture, fixture.first, "HTML forwarded receipt", "113", 3
    )
    proof = reviews._proof(fixture, fixture.html)
    proof["forwarded_components"][0]["export_msgid"] = "113_"
    proof["locator"].update(
        archive_sha256=_hash("Exact archive bytes"),
        member_path="Archive/group.html",
        member={"sha256": _hash("Exact archive member")},
    )
    reviews._write_proof(fixture, fixture.html, proof)
    snapshot = fixture.source.execute(
        "SELECT * FROM source_snapshots WHERE id=3"
    ).fetchone()
    metadata = json.loads(snapshot["metadata"])
    metadata["source_sha256"] = _hash("Exact archive bytes")
    fixture.source.execute(
        "UPDATE source_snapshots SET metadata=? WHERE id=3", (json.dumps(metadata),)
    )
    return fixture


def test_fold_keeps_all_sources_clocks_and_exact_reversible_grouping(projection_store):
    f = projection_store
    before, raw = _snapshot(f.output), tuple(f.source.iterdump())
    result = _apply(f)
    assert result == {
        "status": "applied",
        "statements_joined": 2,
        "projection_groups": 1,
        "refs_retained": 3,
        "skipped_reason_counts": {},
    }
    assert _snapshot(f.output)["refs"] == before["refs"]
    assert tuple(f.source.iterdump()) == raw
    assert f.output.execute("SELECT reference_id FROM statement_groups").fetchall() == [
        (f.native_refs[0],)
    ]
    proof = f.output.execute(
        "SELECT basis,evidence_json FROM forwarded_copy_projection_evidence LIMIT 1"
    ).fetchone()
    assert proof[0] == "contact_forwarded_statement_projection"
    assert json.loads(proof[1])["constraint"] == ["", ""]
    applied = tuple(f.output.iterdump())
    assert _apply(f)["status"] == "already_applied"
    assert tuple(f.output.iterdump()) == applied
    restored = reviews._copy_base(f.output)
    try:
        assert projection.restore_forwarded_copy_groups(f.output, restored) == {
            "restored_groups": 3,
            "restored_members": 3,
        }
        assert _snapshot(restored) == before
        assert projection.restore_forwarded_copy_groups(restored, restored) == {
            "restored_groups": 0,
            "restored_members": 0,
        }
    finally:
        restored.close()


def test_copy_evidence_does_not_copy_a_person_union_restore_journal(projection_store):
    f = projection_store
    assert _apply(f)["status"] == "applied"
    target = sqlite3.connect(":memory:")
    try:
        assert projection.copy_forwarded_copy_evidence(f.output, target) == 3
        assert projection.copy_forwarded_copy_evidence(f.output, target) == 3
        assert projection.restore_forwarded_copy_groups(target, target) == {
            "restored_groups": 0,
            "restored_members": 0,
        }
        assert (
            target.execute(
                "SELECT COUNT(*) FROM forwarded_copy_projection_evidence"
            ).fetchone()[0]
            == 3
        )
    finally:
        target.close()


def test_user_selected_root_survives_generic_folding_and_both_journals_restore(
    projection_store, tmp_path
):
    f = projection_store
    f.root = tmp_path / "store"
    f.path = f.root / "messaging/wechat/statement_reviews.json"
    f.path.parent.mkdir(parents=True)
    baseline = _snapshot(f.output)
    review = reference_reviews._review(
        f, [f.native_refs[1], f.html_refs[0]], f.native_refs[1], "native_group"
    )
    reference_reviews._save(f, [review])
    assert reviews._apply(f)["status"] == "applied"
    manual_state = _snapshot(f.output)
    assert _apply(f)["statements_joined"] == 1
    assert (
        f.output.execute("SELECT reference_id FROM statement_groups").fetchone()[0]
        == f.native_refs[1]
    )
    restored = reviews._copy_base(f.output)
    for table in ("reviewed_statement_groups", "reviewed_statement_members"):
        schema = f.output.execute(
            "SELECT sql FROM sqlite_master WHERE name=?", (table,)
        ).fetchone()[0]
        restored.execute(schema)
        rows = f.output.execute("SELECT * FROM " + table).fetchall()
        restored.executemany(
            "INSERT INTO " + table + " VALUES(" + ",".join("?" for _ in rows[0]) + ")",
            rows,
        )
    try:
        projection.restore_forwarded_copy_groups(f.output, restored)
        assert _snapshot(restored) == manual_state
        reviews.reviews.restore_reviewed_groups(restored, restored)
        assert _snapshot(restored) == baseline
    finally:
        restored.close()


def test_separate_user_reviewed_roots_are_not_overridden(projection_store, tmp_path):
    f = projection_store
    part = reviews._proof(f, f.first)["text_components"][0]
    _, extra = packets._add_packet(f, "Fourth copied receiving packet", [part])
    f.root = tmp_path / "store"
    f.path = f.root / "messaging/wechat/statement_reviews.json"
    f.path.parent.mkdir(parents=True)
    declarations = [
        reference_reviews._review(
            f, [f.native_refs[0], f.html_refs[0]], f.native_refs[0], "native_group"
        ),
        reference_reviews._review(
            f, [f.native_refs[1], extra[0]["id"]], f.native_refs[1], "native_group"
        ),
    ]
    reference_reviews._save(f, declarations)
    assert reviews._apply(f)["status"] == "applied"
    before = tuple(f.output.iterdump())
    result = _apply(f)
    assert (
        result["skipped_reason_counts"]["multiple_user_reviewed_representatives"] == 1
    )
    assert tuple(f.output.iterdump()) == before


def test_global_projection_retains_different_receiving_accounts_and_threads(
    projection_store,
):
    f = projection_store
    owner = "wxid_independent_receiving_account"
    native = "ios-" + hashlib.md5(owner.encode()).hexdigest()
    f.source.execute(
        "UPDATE source_snapshots SET source_id=?,metadata=? WHERE id=2",
        (
            native,
            json.dumps(
                {
                    "source_account_id": owner,
                    "account_hash_verified": True,
                    "account_hash": hashlib.md5(owner.encode()).hexdigest(),
                    "source_label": "iPhone archive",
                }
            ),
        ),
    )
    _update_proof(f, f.second, lambda proof: proof.update(source_snapshot=2))
    f.source.execute(
        "UPDATE messages SET source_id=?,account_id=?,account_key=?,container_account_key=?,"
        "peer_id=?,kind='direct',sender_id=?,conversation_id='another-context' WHERE id=?",
        (
            native,
            owner,
            "account:" + owner,
            "account:" + owner,
            packets.OTHER,
            packets.OTHER,
            f.second,
        ),
    )
    f.output.execute(
        "UPDATE refs SET source_id=?,account_key=?,conversation_id='another-context' "
        "WHERE parent_message_id=?",
        (native, "account:" + owner, f.second),
    )
    before = _snapshot(f.output)["refs"]
    assert _apply(f)["statements_joined"] == 2
    assert _snapshot(f.output)["refs"] == before
    evidence = [
        json.loads(row[0])
        for row in f.output.execute(
            "SELECT evidence_json FROM forwarded_copy_projection_evidence"
        )
    ]
    assert {row["owner"] for row in evidence} == {packets.OWNER, owner}
    assert {row["kind"] for row in evidence} == {"direct", "group"}
    assert (
        f.output.execute("SELECT reference_id FROM statement_groups").fetchone()[0]
        == f.native_refs[0]
    )


@pytest.mark.parametrize(
    "field,values",
    [
        ("source_ids", [{"svrid": "123"}, {"svrid": "456"}]),
        ("source_chat_id", ["first@chatroom", "second@chatroom"]),
    ],
)
def test_explicit_original_identity_conflicts_block_the_whole_bucket(
    projection_store, field, values
):
    f = projection_store
    for parent, value in zip((f.first, f.second), values, strict=True):
        _update_proof(
            f, parent, lambda proof: proof["text_components"][0].update({field: value})
        )
    before = _snapshot(f.output)
    result = _apply(f)
    assert result["status"] == "no_matches"
    assert result["skipped_reason_counts"]["conflicting_original_identity"] == 1
    assert _snapshot(f.output) == before


def test_a_packet_repeating_the_same_child_preserves_multiplicity(projection_store):
    f = projection_store
    part = reviews._proof(f, f.first)["text_components"][0]
    packets._add_packet(f, "Two real positions", [deepcopy(part), deepcopy(part)])
    before = _snapshot(f.output)
    result = _apply(f)
    assert result["skipped_reason_counts"]["same_packet_multiplicity"] == 1
    assert _snapshot(f.output) == before


@pytest.mark.parametrize("clock", ["2021-09-16 15:00", "", "1970-01-01 08:00:00"])
def test_unknown_or_minute_children_are_not_invented_as_second_events(
    projection_store, clock
):
    f = projection_store
    for parent in (f.first, f.second):
        _update_proof(
            f,
            parent,
            lambda proof: proof["text_components"][0].update(
                source_timestamp=clock, source_create_time=""
            ),
        )
    before = _snapshot(f.output)
    assert _apply(f)["status"] == "no_matches"
    assert _snapshot(f.output) == before


def test_known_epoch_cannot_hide_a_different_explicit_instant(projection_store):
    f = projection_store
    part = reviews._proof(f, f.second)["text_components"][0]
    stamp = int(part["source_create_time"]) + 3600
    _update_proof(
        f,
        f.second,
        lambda proof: proof["text_components"][0].update(
            source_timestamp="2021-09-16 16:00:00", source_create_time=str(stamp)
        ),
    )
    _refresh_ref(f, f.second)
    before = _snapshot(f.output)["refs"]
    result = _apply(f)
    assert result["statements_joined"] == 1
    assert _snapshot(f.output)["refs"] == before
    assert f.output.execute("SELECT COUNT(*) FROM statement_groups").fetchone()[0] == 2


@pytest.mark.parametrize(
    "damage", ["wrong_owner", "missing_archive_hash", "wrong_parent_id"]
)
def test_invalid_html_frame_never_enters_the_projection(projection_store, damage):
    f = projection_store
    if damage == "wrong_owner":
        value = f.source.execute(
            "SELECT metadata FROM source_snapshots WHERE id=3"
        ).fetchone()[0]
        metadata = json.loads(value)
        metadata["source_account_id"] = "wxid_another_owner"
        f.source.execute(
            "UPDATE source_snapshots SET metadata=? WHERE id=3", (json.dumps(metadata),)
        )
    else:

        def mutate(proof):
            if damage == "missing_archive_hash":
                del proof["locator"]["archive_sha256"]
            else:
                proof["forwarded_components"][0]["export_msgid"] = "wrong_"

        _update_proof(f, f.html, mutate)
    result = _apply(f)
    assert result["statements_joined"] == 1
    assert (
        f.output.execute(
            "SELECT COUNT(*) FROM forwarded_copy_projection_evidence WHERE reference_id=?",
            (f.html_refs[0],),
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize("mutation", ["body", "author", "source", "missing_parent"])
def test_changed_applied_sources_fail_closed_without_losing_the_journal(
    projection_store, mutation
):
    f = projection_store
    assert _apply(f)["status"] == "applied"
    before = tuple(f.output.iterdump())
    if mutation == "missing_parent":
        f.source.execute("DELETE FROM messages WHERE id=?", (f.first,))
    elif mutation == "source":
        f.output.execute(
            "UPDATE refs SET source_id='html-foreign' WHERE id=?", (f.native_refs[0],)
        )
        before = tuple(f.output.iterdump())
    else:
        _update_proof(
            f,
            f.first,
            lambda proof: proof["text_components"][0].update(
                {"text" if mutation == "body" else "source_sender_id": "changed"}
            ),
        )
    assert _apply(f)["status"] == "rejected"
    assert tuple(f.output.iterdump()) == before


def _add_primary(f, name, server="8999999999999999999"):
    parent = _hash(name)
    row = dict(
        f.source.execute("SELECT * FROM messages WHERE id=?", (f.first,)).fetchone()
    )
    proof = json.loads(row["provenance"])
    part = proof["text_components"][0]
    stamp = datetime.fromtimestamp(
        int(part["source_create_time"]),
        datetime.fromisoformat("2021-09-16T15:00:00+08:00").tzinfo,
    )
    proof.update(
        message_type=1,
        text_extraction_kind="plain_text",
        unix_timestamp=int(stamp.timestamp()),
        source_time=stamp.isoformat(),
        server_id=server,
    )
    proof.pop("text_components")
    proof.pop("appmsg_subtype", None)
    row.update(
        id=parent,
        sender_id=AUTHOR,
        text=part["text"],
        timestamp_value=stamp.isoformat(),
        provenance=json.dumps(proof),
    )
    f.source.execute(
        "INSERT INTO messages VALUES(" + ",".join("?" for _ in row) + ")",
        tuple(row.values()),
    )
    old = list(
        f.output.execute(
            "SELECT * FROM refs WHERE id=?", (f.native_refs[0],)
        ).fetchone()
    )
    old[0] = old[1] = parent
    old[2] = "[]"
    old[3] = "wechat_message_" + parent
    group = _hash("Native primary " + name)
    f.output.execute("INSERT INTO refs VALUES(" + ",".join("?" for _ in old) + ")", old)
    f.output.execute(
        "INSERT INTO statement_groups VALUES(?,?,?)",
        (group, parent, "Native original conversation"),
    )
    f.output.execute("INSERT INTO statement_members VALUES(?,?)", (parent, group))
    return parent


def test_one_proven_primary_remains_the_representative_without_source_rewriting(
    projection_store,
):
    f = projection_store
    primary = _add_primary(f, "Native original")
    before = _snapshot(f.output)["refs"]
    result = _apply(f)
    assert result["statements_joined"] == 3
    assert (
        f.output.execute("SELECT reference_id FROM statement_groups").fetchone()[0]
        == primary
    )
    assert _snapshot(f.output)["refs"] == before


def test_distinct_native_original_events_are_never_collapsed(projection_store):
    f = projection_store
    _add_primary(f, "Native first")
    _add_primary(f, "Native second", "7999999999999999999")
    before = _snapshot(f.output)
    assert _apply(f)["status"] == "no_matches"
    assert _snapshot(f.output) == before


def test_an_explicit_offset_conflict_does_not_acquire_the_native_forward_clock(
    projection_store,
):
    f = projection_store
    primary = _add_primary(f, "Original with a different explicit offset")
    stamp = datetime.fromisoformat("2021-09-16T15:00:00+09:00")
    _update_proof(
        f,
        primary,
        lambda proof: proof.update(
            unix_timestamp=int(stamp.timestamp()), source_time=stamp.isoformat()
        ),
    )
    f.source.execute(
        "UPDATE messages SET timestamp_value=? WHERE id=?", (stamp.isoformat(), primary)
    )
    f.output.execute(
        "UPDATE refs SET timestamp_value=? WHERE id=?", (stamp.isoformat(), primary)
    )
    before = _snapshot(f.output)
    result = _apply(f)
    assert result["skipped_reason_counts"]["conflicting_explicit_instants"] == 1
    assert _snapshot(f.output) == before


def test_a_standalone_html_primary_is_not_promoted_into_a_native_original(
    projection_store,
):
    f = projection_store
    row = dict(
        f.source.execute("SELECT * FROM messages WHERE id=?", (f.html,)).fetchone()
    )
    proof = json.loads(row["provenance"])
    body = proof["forwarded_components"][0]["text"]
    proof.update(
        text_extraction_kind="plain_text",
        sender_native_id=AUTHOR,
        primary_text_sha256=_hash(body),
        source_time="2021-09-16 15:00:00",
    )
    proof["locator"]["export_msgtype"] = "1"
    proof.pop("forwarded_components")
    parent = _hash("A standalone HTML primary assertion")
    row.update(
        id=parent,
        text=body,
        timestamp_value="2021-09-16T15:00:00",
        provenance=json.dumps(proof),
    )
    f.source.execute(
        "INSERT INTO messages VALUES(" + ",".join("?" for _ in row) + ")",
        tuple(row.values()),
    )
    ref = list(
        f.output.execute("SELECT * FROM refs WHERE id=?", (f.html_refs[0],)).fetchone()
    )
    ref[0] = ref[1] = parent
    ref[2] = "[]"
    ref[3] = "wechat_message_" + parent
    group = _hash("Standalone HTML assertion group")
    f.output.execute("INSERT INTO refs VALUES(" + ",".join("?" for _ in ref) + ")", ref)
    f.output.execute(
        "INSERT INTO statement_groups VALUES(?,?,?)",
        (group, parent, "HTML source context"),
    )
    f.output.execute("INSERT INTO statement_members VALUES(?,?)", (parent, group))
    before = _snapshot(f.output)
    result = _apply(f)
    assert result["skipped_reason_counts"].get("unproven_html_primary") == 1, result
    assert _snapshot(f.output) == before


def test_an_extra_group_member_cannot_hide_behind_an_existing_projection(
    projection_store,
):
    f = projection_store
    assert _apply(f)["status"] == "applied"
    existing = f.output.execute("SELECT id FROM statement_groups").fetchone()[0]
    part = packets._part(8, author=AUTHOR)
    _, values = packets._add_packet(f, "An unrelated later assertion", [part])
    other = packets._statement(f, values[0]["id"])
    f.output.execute(
        "UPDATE statement_members SET statement_id=? WHERE statement_id=?",
        (existing, other),
    )
    f.output.execute("DELETE FROM statement_groups WHERE id=?", (other,))
    before = tuple(f.output.iterdump())
    assert _apply(f)["status"] == "rejected"
    with pytest.raises(ValueError, match="undeclared members"):
        projection.restore_forwarded_copy_groups(f.output, f.output)
    assert tuple(f.output.iterdump()) == before


def test_restoration_rejects_changed_reference_fields_before_any_writes(
    projection_store,
):
    f = projection_store
    assert _apply(f)["status"] == "applied"
    f.output.execute(
        "UPDATE refs SET source_clock=source_clock||' altered' WHERE id=?",
        (f.native_refs[0],),
    )
    before = tuple(f.output.iterdump())
    with pytest.raises(ValueError, match="source reference changed"):
        projection.restore_forwarded_copy_groups(f.output, f.output)
    assert tuple(f.output.iterdump()) == before


def test_primary_only_groups_are_untouched(projection_store):
    f = projection_store
    _add_primary(f, "Native first")
    _add_primary(f, "Native second", "7999999999999999999")
    f.output.execute(
        "DELETE FROM statement_groups WHERE reference_id LIKE '%_forwarded_%'"
    )
    f.output.execute(
        "DELETE FROM statement_members WHERE reference_id LIKE '%_forwarded_%'"
    )
    f.output.execute("DELETE FROM refs WHERE id LIKE '%_forwarded_%'")
    before = _snapshot(f.output)
    assert _apply(f)["status"] == "no_matches"
    assert _snapshot(f.output) == before


@pytest.mark.parametrize(
    "field,value",
    [("MAX_REFERENCES", 2), ("MAX_PACKET_BYTES", 2), ("MAX_SOURCE_BYTES", 20)],
)
def test_metadata_bounds_leave_the_output_exactly_unchanged(
    projection_store, monkeypatch, field, value
):
    f = projection_store
    before = tuple(f.output.iterdump())
    monkeypatch.setattr(projection, field, value)
    assert _apply(f)["status"] == "bounded"
    assert tuple(f.output.iterdump()) == before


def test_cancellation_rolls_back_a_partially_started_projection(projection_store):
    f = projection_store
    before = tuple(f.output.iterdump())

    def cancel():
        if projection._TABLES[0] in projection._tables(f.output):
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        _apply(f, check_cancelled=cancel)
    assert tuple(f.output.iterdump()) == before


@pytest.fixture
def projection_archive_store(retained_review_store):
    """Expose an actual archive/cache pair with one extra native group receipt."""
    f = retained_review_store
    f.review_path.write_text(
        json.dumps({"schema_version": 2, "packet_reviews": [], "reference_reviews": []})
    )
    with sqlite3.connect(f.history) as source, sqlite3.connect(f.cache) as output:
        source.row_factory = sqlite3.Row
        for snapshot in source.execute(
            "SELECT * FROM source_snapshots WHERE source_id LIKE 'html-%'"
        ).fetchall():
            metadata = json.loads(snapshot["metadata"])
            metadata["source_sha256"] = _hash("Pinned archive " + snapshot["source_id"])
            source.execute(
                "UPDATE source_snapshots SET metadata=? WHERE id=?",
                (json.dumps(metadata), snapshot["id"]),
            )
            for parent in source.execute(
                analysis._ROW_SELECT + "WHERE p.id=?", (snapshot["source_id"],)
            ).fetchall():
                proof = json.loads(parent["provenance"])
                proof["locator"].update(
                    archive_sha256=metadata["source_sha256"],
                    member_path="Synthetic/verified-group.html",
                    member={"sha256": _hash("Pinned member " + parent["id"])},
                )
                source.execute(
                    "UPDATE messages SET provenance=? WHERE id=?",
                    (json.dumps(proof), parent["id"]),
                )
        full = dict(
            source.execute(
                analysis._ROW_SELECT
                + "WHERE p.id LIKE 'ios-%' AND c.kind='group' LIMIT 1"
            ).fetchone()
        )
        row = dict(
            source.execute(
                "SELECT * FROM messages WHERE id=?", (full["id"],)
            ).fetchone()
        )
        parent = _hash("Another native group receipt in the actual archive schema")
        row["id"] = parent
        row["ordinal"] += 100
        source.execute(
            "INSERT INTO messages VALUES(" + ",".join("?" for _ in row) + ")",
            tuple(row.values()),
        )
        raw = output.execute(
            "SELECT * FROM refs WHERE parent_message_id=? ORDER BY id", (full["id"],)
        ).fetchall()
        for old in raw:
            ref = list(old)
            ref[0] = ref[0].replace(full["id"], parent)
            ref[1] = parent
            ref[3] = "wechat_message_" + ref[0]
            group = _hash("Actual archive extra group " + ref[0])
            output.execute(
                "INSERT INTO refs VALUES(" + ",".join("?" for _ in ref) + ")", ref
            )
            output.execute(
                "INSERT INTO statement_groups VALUES(?,?,?)",
                (group, ref[0], full["conversation_id"]),
            )
            output.execute("INSERT INTO statement_members VALUES(?,?)", (ref[0], group))
    with sqlite3.connect(f.cache) as output:
        identities = {
            str(p.resolve()): analysis._identity(p) for p in (f.history, f.registry)
        }
        output.execute(
            "UPDATE info SET value=? WHERE key='source_inputs'",
            (_hash(analysis._json(identities)),),
        )
    return f


def test_real_archive_schema_folds_without_changing_any_raw_record(
    projection_archive_store,
):
    f = projection_archive_store
    raw = f.history.read_bytes()
    with sqlite3.connect(f.history) as source, sqlite3.connect(f.cache) as output:
        source.row_factory = sqlite3.Row
        refs = output.execute("SELECT * FROM refs ORDER BY id").fetchall()
        result = projection.fold_contact_forwarded_copies(
            source,
            output,
            CONTACT,
            AUTHOR,
            row_select=analysis._ROW_SELECT,
            prepare_rows=analysis._requests,
            native_thread_key=analysis._native_conversation_key,
        )
        assert result["status"] == "applied", result
        assert result["statements_joined"] == 6
        assert output.execute("SELECT * FROM refs ORDER BY id").fetchall() == refs
    assert f.history.read_bytes() == raw
