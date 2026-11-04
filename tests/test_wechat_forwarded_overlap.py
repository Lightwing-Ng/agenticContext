"""Repeated transcript excerpts retain source observations and unknown original threads.

Code version: v1.0.0-codex.0
"""

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core import wechat_forwarded_origins as origins
from app.core.wechat_contact_embedded import native_details
from app.core.wechat_forwarded_origins import link_forwarded_copies


OWNER = "wxid_transcript_owner"
AUTHOR = "wxid_original_author"
OTHER = "wxid_original_participant"
FORWARDER = "wxid_original_forwarder"
SOURCE = "ios-" + hashlib.md5(OWNER.encode()).hexdigest()
SELECT = "SELECT m.* FROM messages m "


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _part(index, *, author=None):
    return {
        "kind": "forwarded_text",
        "text": "Transcript statement " + str(index),
        "source_sender_id": author or (AUTHOR if index % 2 == 0 else OTHER),
        "source_sender_label": "Literal source label",
        "source_timestamp": (
            datetime(2021, 9, 16, 15, 0, 0) + timedelta(seconds=index)
        ).strftime("%Y-%m-%d %H:%M:%S"),
        "source_create_time": "",
        "source_data_id": "",
        "source_ids": {},
    }


def _noop_prepare(connection, rows):
    return []


def _add_packet(
    fixture,
    name,
    components,
    *,
    snapshot=1,
    sender=FORWARDER,
    conversation="same-receiving-conversation",
    kind="group",
):
    parent = _hash(name)
    clock = datetime.fromisoformat("2021-09-16T18:00:00+08:00")
    proof = {
        "message_type": 49,
        "appmsg_subtype": 19,
        "text_extraction_kind": "forwarded_text",
        "text_components": deepcopy(components),
        "source_snapshot": snapshot,
        "server_id": "8123456789012345678",
        "unix_timestamp": int(clock.timestamp()),
        "source_time": clock.isoformat(),
        "locator": {},
    }
    fixture.source.execute(
        "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            parent,
            SOURCE,
            OWNER,
            "account:" + OWNER,
            "account:" + OWNER,
            "same-receiving@chatroom" if kind == "group" else OTHER,
            conversation,
            kind,
            "Verified archive ID",
            sender,
            "received",
            clock.isoformat(),
            json.dumps(proof),
            1,
            "Source packet",
            0,
        ),
    )
    details = list(native_details(proof, parent_id=parent, account_id=OWNER))
    for detail in details:
        if detail["sender_id"] != AUTHOR:
            continue
        identifier, statement = detail["id"], _hash("statement-" + detail["id"])
        fixture.output.execute(
            "INSERT INTO refs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                identifier,
                parent,
                json.dumps(detail["path"], separators=(",", ":")),
                detail["anchor_id"],
                conversation,
                SOURCE,
                "account:" + OWNER,
                0,
                1,
                bool(detail["timestamp_value"]),
                detail["timestamp_value"],
                detail["timestamp_label"],
                detail["time_basis"],
                detail["source_clock"],
            ),
        )
        fixture.output.execute(
            "INSERT INTO statement_groups VALUES(?,?,?)",
            (statement, identifier, conversation),
        )
        fixture.output.execute(
            "INSERT INTO statement_members VALUES(?,?)", (identifier, statement)
        )
    return parent, details


@pytest.fixture
def overlap_store():
    source = sqlite3.connect(":memory:")
    source.row_factory = sqlite3.Row
    source.executescript(
        "CREATE TABLE source_snapshots(id INTEGER PRIMARY KEY,snapshot_id TEXT,"
        "source_id TEXT,metadata TEXT);"
        "CREATE TABLE messages(id TEXT PRIMARY KEY,source_id TEXT,account_id TEXT,"
        "account_key TEXT,container_account_key TEXT,peer_id TEXT,conversation_id TEXT,"
        "kind TEXT,identity_status TEXT,sender_id TEXT,direction TEXT,timestamp_value TEXT,"
        "provenance TEXT,ordinal INTEGER,text TEXT,time_sort REAL);"
    )
    for number in (1, 2):
        source.execute(
            "INSERT INTO source_snapshots VALUES(?,?,?,?)",
            (
                number,
                "snapshot-" + str(number),
                SOURCE,
                json.dumps(
                    {
                        "source_account_id": OWNER,
                        "account_hash_verified": True,
                        "account_hash": hashlib.md5(OWNER.encode()).hexdigest(),
                    }
                ),
            ),
        )
    output = sqlite3.connect(":memory:")
    output.executescript(analysis._REFERENCE_SCHEMA)
    fixture = SimpleNamespace(source=source, output=output)
    yield fixture
    source.close()
    output.close()


def _link(fixture):
    link_forwarded_copies(
        fixture.source,
        fixture.output,
        row_select=SELECT,
        prepare_rows=_noop_prepare,
        native_thread_key=analysis._native_conversation_key,
    )


def _statement(fixture, identifier):
    return fixture.output.execute(
        "SELECT statement_id FROM statement_members WHERE reference_id=?", (identifier,)
    ).fetchone()[0]


def _source_dump(fixture):
    return tuple(fixture.source.iterdump())


@pytest.mark.parametrize("kind", ["group", "direct"])
def test_multi_author_overlap_keeps_each_parent_anchor_and_source_clock(
    overlap_store, kind
):
    fixture = overlap_store
    first, a = _add_packet(fixture, "first", [_part(i) for i in range(10)], kind=kind)
    second, b = _add_packet(
        fixture, "second", [_part(i) for i in range(4, 16)], kind=kind
    )
    before, count = (
        _source_dump(fixture),
        fixture.output.execute("SELECT count(*) FROM refs").fetchone()[0],
    )
    _link(fixture)
    for number in (4, 6, 8):
        left, right = a[number], b[number - 4]
        assert _statement(fixture, left["id"]) == _statement(fixture, right["id"])
    assert _statement(fixture, a[0]["id"]) != _statement(fixture, b[-2]["id"])
    assert fixture.output.execute("SELECT count(*) FROM refs").fetchone()[0] == count
    rows = list(
        fixture.output.execute(
            "SELECT parent_message_id,anchor_id,time_basis FROM refs"
        )
    )
    assert {row[0] for row in rows} == {first, second}
    assert all("_forwarded_" in row[1] for row in rows)
    assert all(row[2] == "Source clock; timezone unknown" for row in rows)
    assert _source_dump(fixture) == before
    after = tuple(fixture.output.iterdump())
    _link(fixture)
    assert tuple(fixture.output.iterdump()) == after


@pytest.mark.parametrize(
    "damage",
    [
        "snapshot",
        "conversation",
        "forwarder",
        "owner_proof",
        "parent_clock",
        "body",
        "child_clock",
        "epoch",
        "type",
        "server_id",
        "data_id",
        "chat_id",
        "single_author",
        "repeated_sequence",
        "one_match",
        "raw_clock_format",
    ],
)
def test_weak_or_contradictory_overlap_remains_independent(overlap_store, damage):
    fixture = overlap_store
    left, right = [_part(i) for i in range(8)], [_part(i) for i in range(8)]
    options = {}
    if damage == "snapshot":
        options["snapshot"] = 2
    elif damage == "conversation":
        options["conversation"] = "another-receiving-conversation"
    elif damage == "forwarder":
        options["sender"] = "wxid_another_forwarder"
    elif damage == "body":
        right[4]["text"] += " changed"
    elif damage == "child_clock":
        right[4]["source_timestamp"] = "2021-09-16 15:02:04"
    elif damage == "epoch":
        right[4].update(source_create_time="0", source_timestamp="1970-01-01 08:00:00")
    elif damage == "type":
        right[4]["original_message_type"] = 49
    elif damage == "server_id":
        left[4]["source_ids"] = {"svrid": "1234567890123456789"}
        right[4]["source_ids"] = {"svrid": "2234567890123456789"}
    elif damage == "data_id":
        left[4]["source_data_id"], right[4]["source_data_id"] = "a" * 32, "b" * 32
    elif damage == "chat_id":
        left[4]["source_chat_id"], right[4]["source_chat_id"] = (
            "first@chatroom",
            "second@chatroom",
        )
    elif damage == "single_author":
        for part in [*left, *right]:
            part["source_sender_id"] = AUTHOR
    elif damage == "repeated_sequence":
        right += deepcopy(right)
    elif damage == "one_match":
        for index, part in enumerate(right):
            if index != 4:
                part["text"] += " unrelated"
    elif damage == "raw_clock_format":
        right[4]["source_timestamp"] = right[4]["source_timestamp"].replace(" ", "T")
    _add_packet(fixture, "first", left)
    parent, _ = _add_packet(fixture, "second", right, **options)
    if damage in {"owner_proof", "parent_clock"}:
        if damage == "owner_proof":
            fixture.source.execute("UPDATE source_snapshots SET metadata='{}'")
        else:
            proof = json.loads(
                fixture.source.execute(
                    "SELECT provenance FROM messages WHERE id=?", (parent,)
                ).fetchone()[0]
            )
            proof["unix_timestamp"] = 0
            fixture.source.execute(
                "UPDATE messages SET provenance=? WHERE id=?",
                (json.dumps(proof), parent),
            )
    before = tuple(
        fixture.output.execute("SELECT * FROM statement_members ORDER BY reference_id")
    )
    _link(fixture)
    assert (
        tuple(
            fixture.output.execute(
                "SELECT * FROM statement_members ORDER BY reference_id"
            )
        )
        == before
    )


def test_transitive_overlap_does_not_merge_conflicting_server_claims(overlap_store):
    fixture = overlap_store
    claims = []
    for name, server in (
        ("first", "1234567890123456789"),
        ("bridge", ""),
        ("last", "2234567890123456789"),
    ):
        parts = [_part(i) for i in range(8)]
        if server:
            parts[4]["source_ids"] = {"svrid": server}
        _, details = _add_packet(fixture, name, parts)
        claims.append(details[4]["id"])
    _link(fixture)
    assert _statement(fixture, claims[0]) != _statement(fixture, claims[2])
    assert len({_statement(fixture, identifier) for identifier in claims}) == 2


def test_existing_native_primary_stays_preferred_when_whole_groups_move(overlap_store):
    fixture = overlap_store
    _, a = _add_packet(fixture, "first", [_part(i) for i in range(8)])
    _, b = _add_packet(fixture, "second", [_part(i) for i in range(8)])
    original = _hash("primary-original")
    row = dict(fixture.source.execute("SELECT * FROM messages LIMIT 1").fetchone())
    proof = json.loads(row["provenance"])
    proof.update(
        message_type=1,
        text_extraction_kind="plain_text",
        server_id="1234567890123456789",
    )
    row.update(
        id=original,
        sender_id=AUTHOR,
        text="Transcript statement 4",
        provenance=json.dumps(proof),
    )
    fixture.source.execute(
        "INSERT INTO messages VALUES(" + ",".join("?" for _ in row) + ")",
        tuple(row.values()),
    )
    fixture.output.execute(
        "INSERT INTO refs SELECT ?,?, '[]',?, conversation_id,source_id,account_key,time_sort,ordinal,has_time,timestamp_value,timestamp_label,time_basis,source_clock FROM refs WHERE id=?",
        (original, original, "wechat_message_" + original, a[4]["id"]),
    )
    left_group = _statement(fixture, a[4]["id"])
    fixture.output.execute(
        "INSERT INTO statement_members VALUES(?,?)", (original, left_group)
    )
    fixture.output.execute(
        "UPDATE statement_groups SET reference_id=? WHERE id=?", (original, left_group)
    )
    _link(fixture)
    assert _statement(fixture, b[4]["id"]) == left_group
    assert (
        fixture.output.execute(
            "SELECT reference_id FROM statement_groups WHERE id=?", (left_group,)
        ).fetchone()[0]
        == original
    )


@pytest.mark.parametrize(
    "budget",
    [
        "MAX_COPY_SEEDS",
        "MAX_COPY_COMPARISONS",
        "MAX_COPY_ALIGNMENTS",
        "MAX_COPY_PARENTS",
        "MAX_COPY_PACKET_BYTES",
        "MAX_COPY_SOURCE_BYTES",
        "MAX_COPY_DETAILS",
    ],
)
def test_copy_proof_budgets_fail_closed_before_any_memberships_change(
    overlap_store, monkeypatch, budget
):
    fixture = overlap_store
    for name in ("first", "second", "third"):
        _add_packet(fixture, name, [_part(i) for i in range(8)])
    before = tuple(fixture.output.iterdump())
    monkeypatch.setattr(origins, budget, 0)
    _link(fixture)
    assert tuple(fixture.output.iterdump()) == before


def test_source_byte_budget_is_checked_before_payload_projection(
    overlap_store, monkeypatch
):
    fixture = overlap_store
    for name in ("first", "second"):
        _add_packet(fixture, name, [_part(i) for i in range(8)])
    before = tuple(fixture.output.iterdump())
    monkeypatch.setattr(origins, "MAX_COPY_SOURCE_BYTES", 1)

    def unexpected_projection(connection, rows):
        raise AssertionError(
            "The budget must reject the input before payload projection."
        )

    link_forwarded_copies(
        fixture.source,
        fixture.output,
        row_select=SELECT,
        prepare_rows=unexpected_projection,
        native_thread_key=analysis._native_conversation_key,
    )
    assert tuple(fixture.output.iterdump()) == before


def test_aggregate_detail_budget_rejects_after_bounded_packet_parsing(
    overlap_store, monkeypatch
):
    fixture = overlap_store
    for name in ("first", "second"):
        _add_packet(fixture, name, [_part(i) for i in range(8)])
    before = tuple(fixture.output.iterdump())
    # Each packet has eight literal leaves but only four contact references.
    # This exceeds the cumulative parsing budget without hitting the ref guard.
    monkeypatch.setattr(origins, "MAX_COPY_DETAILS", 12)
    calls = []
    original = origins._native_packet

    def tracked_packet(*args):
        calls.append(args[1])
        return original(*args)

    monkeypatch.setattr(origins, "_native_packet", tracked_packet)
    _link(fixture)
    assert len(calls) == 2
    assert tuple(fixture.output.iterdump()) == before


def test_two_established_native_original_events_cannot_be_unioned_by_a_copy(
    overlap_store,
):
    fixture = overlap_store
    references = []
    for number, name in enumerate(("first", "second"), 1):
        parent, details = _add_packet(fixture, name, [_part(i) for i in range(8)])
        reference = details[4]["id"]
        references.append(reference)
        row = dict(
            fixture.source.execute(
                "SELECT * FROM messages WHERE id=?", (parent,)
            ).fetchone()
        )
        proof = json.loads(row["provenance"])
        proof.update(
            message_type=1,
            text_extraction_kind="plain_text",
            server_id=str(1234567890123456700 + number),
        )
        original = _hash("original-event-" + name)
        row.update(
            id=original,
            sender_id=AUTHOR,
            text="Transcript statement 4",
            provenance=json.dumps(proof),
        )
        fixture.source.execute(
            "INSERT INTO messages VALUES(" + ",".join("?" for _ in row) + ")",
            tuple(row.values()),
        )
        fixture.output.execute(
            "INSERT INTO refs SELECT ?,?, '[]',?, conversation_id,source_id,account_key,time_sort,ordinal,has_time,timestamp_value,timestamp_label,time_basis,source_clock FROM refs WHERE id=?",
            (original, original, "wechat_message_" + original, reference),
        )
        group = _statement(fixture, reference)
        fixture.output.execute(
            "INSERT INTO statement_members VALUES(?,?)", (original, group)
        )
        fixture.output.execute(
            "UPDATE statement_groups SET reference_id=? WHERE id=?", (original, group)
        )
    _link(fixture)
    assert _statement(fixture, references[0]) != _statement(fixture, references[1])
