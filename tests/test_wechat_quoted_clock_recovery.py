"""Quoted clocks require an exact accepted native event, never a parent clock.

Code version: v1.2.1-codex.0
"""

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core import wechat_quoted_clock_recovery as recovery
from app.core.wechat_archive import _identifier
from app.core.wechat_contact_embedded import native_detail
from app.core.wechat_contacts import canonical_contact_id
from tests import test_wechat_conversation_events as fixtures
from tests import test_wechat_conversation_quote_events as quotes


event_store = fixtures.event_store
ROOM = "123456@chatroom"
CONTACT = canonical_contact_id(fixtures.PEER, authority="native_message_sender")


def _setup(fixture, mutation=None, *, quoted_text=None, original_text=None):
    _, parent_id = quotes._add_quote(fixture)
    original_id = fixture.ids[0][1]
    extra_id = None
    with sqlite3.connect(fixture.repository.path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            "UPDATE conversations SET kind='group',peer_id=? WHERE id=?",
            (ROOM, fixtures.SESSION),
        )
        for raw in connection.execute("SELECT * FROM messages").fetchall():
            row = dict(raw)
            proof = json.loads(row["provenance"])
            if proof["source_snapshot"] != 2:
                continue
            proof["locator"]["table"] = "Chat_" + hashlib.md5(ROOM.encode()).hexdigest()
            if row["id"] == parent_id:
                reference = proof["quoted_reference"]
                reference["source_chat_id"] = ROOM
                if mutation == "different_quoted_author":
                    reference["source_sender_id"] = fixtures.OWNER
                elif mutation == "missing_server":
                    reference["source_ids"] = {}
                elif mutation == "invalid_server":
                    reference["source_ids"] = {"svrid": "0"}
                elif mutation == "conflicting_servers":
                    reference["source_ids"]["srcMsgSvrId"] = "1234567"
                elif mutation == "unknown_server":
                    reference["source_ids"] = {"svrid": "1234567"}
                elif mutation == "source_clock_placeholder":
                    reference["source_timestamp"] = "0"
                elif mutation == "different_claimed_room":
                    reference["source_chat_id"] = "654321@chatroom"
                proof["text_components"][1].update(deepcopy(reference))
                if mutation in {
                    "leading_newline",
                    "trailing_newline",
                    "different_whitespace",
                    "emoticon_difference",
                }:
                    quoted = {
                        "leading_newline": "\n" + quotes.QUOTED,
                        "trailing_newline": quotes.QUOTED + "\n",
                        "different_whitespace": quotes.QUOTED.replace(
                            "two spaces", "two  spaces"
                        ),
                        "emoticon_difference": quotes.QUOTED + "[Smirk]",
                    }[mutation]
                    proof["text_components"][1]["text"] = quoted
                    row["text"] = quotes.REPLY + "\n\n" + quoted
                if quoted_text is not None:
                    proof["text_components"][1]["text"] = quoted_text
                    row["text"] = quotes.REPLY + "\n\n" + quoted_text
            if row["id"] == original_id:
                if mutation == "different_original_author":
                    row["sender_id"] = fixtures.OWNER
                elif mutation == "different_original_body":
                    row["text"] = "Another person's different original body"
                elif mutation == "inconsistent_epoch":
                    proof["unix_timestamp"] += 1
                elif mutation == "naive_original_clock":
                    row["timestamp_value"] = row["timestamp_value"][:-6]
                elif mutation == "future_original":
                    row["timestamp_value"] = "2020-01-01T08:00:05+08:00"
                    proof["unix_timestamp"] = int(
                        datetime.fromisoformat(row["timestamp_value"]).timestamp()
                    )
                elif mutation == "different_snapshot":
                    snapshot = connection.execute(
                        "SELECT source_id,metadata FROM source_snapshots WHERE id=2"
                    ).fetchone()
                    connection.execute(
                        "INSERT INTO source_snapshots VALUES(?,?,?,?)",
                        (3, "another-native-snapshot", *snapshot),
                    )
                    proof["source_snapshot"] = 3
                elif mutation == "wrong_locator":
                    proof["locator"]["table"] = "Chat_wrong_room"
                elif mutation == "different_room":
                    other = "654321@chatroom"
                    conversation = (
                        "wechat:" + hashlib.sha256(other.encode()).hexdigest()
                    )
                    connection.execute(
                        "INSERT INTO conversations SELECT ?,?,account_key,?,title,kind,"
                        "message_count,first_sort,last_sort,first_time,last_time,latest_text "
                        "FROM conversations WHERE id=?",
                        (conversation, fixtures.NATIVE, other, fixtures.SESSION),
                    )
                    row["conversation_id"] = conversation
                    proof["locator"]["table"] = (
                        "Chat_" + hashlib.md5(other.encode()).hexdigest()
                    )
                if original_text is not None:
                    row["text"] = original_text
            row["provenance"] = json.dumps(proof)
            connection.execute(
                "UPDATE messages SET sender_id=?,text=?,timestamp_value=?,"
                "conversation_id=?,provenance=? WHERE id=?",
                (
                    row["sender_id"],
                    row["text"],
                    row["timestamp_value"],
                    row["conversation_id"],
                    row["provenance"],
                    row["id"],
                ),
            )
            if mutation == "ambiguous_original" and row["id"] == original_id:
                extra = dict(row)
                duplicate_proof = deepcopy(proof)
                duplicate_proof["record_id"] = "another-original"
                extra_id = _identifier(fixtures.NATIVE, duplicate_proof["record_id"])
                extra.update(
                    id=extra_id, ordinal=103, provenance=json.dumps(duplicate_proof)
                )
                quotes._insert(connection, extra)
    source = sqlite3.connect(
        fixture.repository.path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
    )
    source.row_factory = sqlite3.Row
    output = sqlite3.connect(":memory:")
    output.executescript(analysis._REFERENCE_SCHEMA)
    identifiers = [original_id, parent_id, *([extra_id] if extra_id else [])]
    rows = {
        row["id"]: dict(row)
        for row in source.execute(
            analysis._ROW_SELECT
            + "WHERE m.id IN ("
            + ",".join("?" for _ in identifiers)
            + ")",
            identifiers,
        )
    }
    # A bad locator is rejected by the recovery layer even if retained references
    # were accepted before this validation was introduced.
    if mutation == "wrong_locator":
        for row in rows.values():
            proof = json.loads(row["provenance"])
            row["source_id"] = fixtures.NATIVE
            row["identity_status"] = "Verified archive ID"
            row["account_id"] = fixtures.OWNER
    else:
        analysis._requests(source, list(rows.values()))
    values = [analysis._reference_values(rows[original_id])]
    if extra_id:
        values.append(analysis._reference_values(rows[extra_id]))
    parent = rows[parent_id]
    detail = native_detail(
        json.loads(parent["provenance"]),
        ["quote", 2],
        parent_id=parent_id,
        account_id=fixtures.OWNER,
        peer_id=ROOM,
    )
    assert detail is not None
    values.append(analysis._reference_values(parent, detail=detail))
    output.executemany("INSERT INTO refs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
    for value in values:
        output.execute(
            "INSERT INTO statement_groups VALUES(?,?,?)",
            ("g-" + value[0], value[0], "thread"),
        )
        output.execute(
            "INSERT INTO statement_members VALUES(?,?)", (value[0], "g-" + value[0])
        )
    reference_id = detail["id"]
    if mutation == "different_reference_account":
        output.execute(
            "UPDATE refs SET account_key='account:other' WHERE id=?", (reference_id,)
        )
    elif mutation == "different_reference_source":
        output.execute(
            "UPDATE refs SET source_id='ios-other' WHERE id=?", (reference_id,)
        )
    return SimpleNamespace(
        source=source,
        output=output,
        original_id=original_id,
        parent_id=parent_id,
        reference_id=reference_id,
    )


def _recover(fixture):
    return recovery.recover_quoted_reference_clocks(
        fixture.source,
        fixture.output,
        CONTACT,
        fixtures.PEER,
        row_select=analysis._ROW_SELECT,
        prepare_rows=analysis._requests,
        native_thread_key=analysis._native_conversation_key,
        validate_native_source=analysis._routed_native_source,
    )


def _members(output):
    return {
        name: output.execute("SELECT * FROM " + name + " ORDER BY 1").fetchall()
        for name in ("statement_groups", "statement_members")
    }


@pytest.mark.parametrize(
    "mutation", [None, "source_clock_placeholder", "leading_newline"]
)
def test_exact_native_original_recovers_quote_clock_and_preserves_sources(
    event_store, mutation
):
    fixture = _setup(event_store, mutation)
    try:
        before = (
            event_store.repository.path.read_bytes(),
            event_store.registry.read_bytes(),
        )
        membership = _members(fixture.output)
        clock_before = fixture.output.execute(
            "SELECT source_clock FROM refs WHERE id=?", (fixture.reference_id,)
        ).fetchone()[0]
        result = _recover(fixture)
        assert result["status"] == "ready"
        assert result["recovered"] == result["verified"] == 1
        assert result["unresolved"] == 0
        assert result["updated_ids"] == [fixture.reference_id]
        assert result["rejected_author_ids"] == []
        row = fixture.output.execute(
            "SELECT has_time,timestamp_value,time_basis,source_clock FROM refs WHERE id=?",
            (fixture.reference_id,),
        ).fetchone()
        assert row == (
            1,
            "2020-01-01T08:00:00+08:00",
            recovery.RECOVERED_TIME_BASIS + "UTC+08:00",
            clock_before,
        )
        assert row[1] != quotes.WALL + "+08:00"
        evidence = fixture.output.execute(
            "SELECT original_message_id,original_server_id FROM quoted_clock_evidence"
        ).fetchone()
        assert evidence == (fixture.original_id, "922337203685477580")
        assert fixture.output.execute(
            "SELECT text_match FROM quoted_clock_evidence"
        ).fetchone()[0] == (
            "single_leading_newline" if mutation == "leading_newline" else "exact"
        )
        assert _members(fixture.output) == membership
        proof_before = fixture.output.execute(
            "SELECT * FROM quoted_clock_evidence"
        ).fetchall()
        assert json.loads(proof_before[0][-1])["timestamp_value"] == ""
        repeat = _recover(fixture)
        assert repeat["recovered"] == 0 and repeat["verified"] == 1
        assert (
            fixture.output.execute("SELECT * FROM quoted_clock_evidence").fetchall()
            == proof_before
        )
        assert (
            fixture.output.execute(
                "SELECT COUNT(*) FROM quoted_clock_evidence"
            ).fetchone()[0]
            == 1
        )
        assert before == (
            event_store.repository.path.read_bytes(),
            event_store.registry.read_bytes(),
        )
    finally:
        fixture.source.close()
        fixture.output.close()


@pytest.mark.parametrize(
    "quoted_text,original_text,text_match",
    [
        ("Literal body[奸笑]", "Literal body[Smirk]", "localized_smirk_token"),
        ("Literal body[Smirk]", "Literal body[奸笑]", "localized_smirk_token"),
        (
            "First[奸笑] and second[Smirk]",
            "First[Smirk] and second[奸笑]",
            "localized_smirk_token",
        ),
        (
            "\nLiteral body[奸笑]",
            "Literal body[Smirk]",
            "single_leading_newline_localized_smirk_token",
        ),
    ],
)
def test_localized_smirk_recovers_only_clock_and_preserves_literal_evidence(
    event_store, quoted_text, original_text, text_match
):
    fixture = _setup(event_store, quoted_text=quoted_text, original_text=original_text)
    try:
        source_before = event_store.repository.path.read_bytes()
        refs_before = fixture.output.execute(
            "SELECT * FROM refs ORDER BY id"
        ).fetchall()
        membership = _members(fixture.output)
        result = _recover(fixture)
        assert result["recovered"] == result["verified"] == 1
        assert result["rejected_author_ids"] == []
        assert fixture.output.execute(
            "SELECT has_time,timestamp_value,source_clock FROM refs WHERE id=?",
            (fixture.reference_id,),
        ).fetchone() == (1, "2020-01-01T08:00:00+08:00", "")
        proof = fixture.output.execute(
            "SELECT text_match,body_sha256,quoted_body_sha256,reference_before_json "
            "FROM quoted_clock_evidence"
        ).fetchone()
        assert proof[:3] == (
            text_match,
            hashlib.sha256(original_text.encode()).hexdigest(),
            hashlib.sha256(quoted_text.encode()).hexdigest(),
        )
        preimage = json.loads(proof[3])
        assert preimage["timestamp_value"] == preimage["source_clock"] == ""
        assert (
            tuple(
                preimage[column[0]]
                for column in fixture.output.execute(
                    "SELECT * FROM refs LIMIT 0"
                ).description
            )
            in refs_before
        )
        assert (
            fixture.source.execute(
                "SELECT text FROM messages WHERE id=?", (fixture.original_id,)
            ).fetchone()[0]
            == original_text
        )
        assert _members(fixture.output) == membership
        assert event_store.repository.path.read_bytes() == source_before
        assert _recover(fixture)["recovered"] == 0
        assert (
            fixture.output.execute(
                "SELECT text_match,body_sha256,quoted_body_sha256,reference_before_json "
                "FROM quoted_clock_evidence"
            ).fetchone()
            == proof
        )
    finally:
        fixture.source.close()
        fixture.output.close()


@pytest.mark.parametrize(
    "quoted_text,original_text",
    [
        ("Literal body[奸笑]", "Literal body"),
        ("Literal body", "Literal body[Smirk]"),
        ("Literal body[奸笑][奸笑]", "Literal body[Smirk]"),
        ("Changed body[奸笑]", "Literal body[Smirk]"),
        ("Literal body[smirk]", "Literal body[奸笑]"),
        ("Literal body[偷笑]", "Literal body[Smirk]"),
        ("Literal body[奸笑]\n", "Literal body[Smirk]"),
        ("Literal body [奸笑]", "Literal body[Smirk]"),
    ],
)
def test_localized_smirk_does_not_hide_body_or_token_count_differences(
    event_store, quoted_text, original_text
):
    fixture = _setup(event_store, quoted_text=quoted_text, original_text=original_text)
    try:
        before = fixture.output.execute("SELECT * FROM refs ORDER BY id").fetchall()
        result = _recover(fixture)
        assert result["recovered"] == result["verified"] == 0
        assert (
            fixture.output.execute("SELECT * FROM refs ORDER BY id").fetchall()
            == before
        )
        assert (
            fixture.output.execute(
                "SELECT COUNT(*) FROM quoted_clock_evidence"
            ).fetchone()[0]
            == 0
        )
    finally:
        fixture.source.close()
        fixture.output.close()


@pytest.mark.parametrize(
    "mutation",
    [
        "different_quoted_author",
        "different_room",
        "inconsistent_epoch",
        "unknown_server",
    ],
)
def test_localized_smirk_never_bypasses_native_identity_or_clock_proof(
    event_store, mutation
):
    fixture = _setup(
        event_store,
        mutation,
        quoted_text="Literal body[奸笑]",
        original_text="Literal body[Smirk]",
    )
    try:
        before = fixture.output.execute("SELECT * FROM refs ORDER BY id").fetchall()
        result = _recover(fixture)
        assert result["recovered"] == result["verified"] == 0
        assert (
            fixture.output.execute("SELECT * FROM refs ORDER BY id").fetchall()
            == before
        )
    finally:
        fixture.source.close()
        fixture.output.close()


def test_localized_smirk_never_assigns_another_native_authors_clock(event_store):
    fixture = _setup(
        event_store,
        "different_original_author",
        quoted_text="Literal body[奸笑]",
        original_text="Literal body[Smirk]",
    )
    try:
        before = fixture.output.execute("SELECT * FROM refs ORDER BY id").fetchall()
        result = _recover(fixture)
        assert result["recovered"] == 0
        assert result["rejected_author_ids"] == [fixture.reference_id]
        assert (
            fixture.output.execute("SELECT * FROM refs ORDER BY id").fetchall()
            == before
        )
        assert fixture.output.execute(
            "SELECT status,text_match FROM quoted_clock_evidence"
        ).fetchone() == ("wrong_author", "localized_smirk_token")
    finally:
        fixture.source.close()
        fixture.output.close()


@pytest.mark.parametrize(
    "mutation",
    [
        "different_quoted_author",
        "missing_server",
        "invalid_server",
        "conflicting_servers",
        "unknown_server",
        "different_original_body",
        "inconsistent_epoch",
        "naive_original_clock",
        "future_original",
        "different_snapshot",
        "different_room",
        "different_reference_account",
        "different_reference_source",
        "ambiguous_original",
        "different_claimed_room",
        "wrong_locator",
        "trailing_newline",
        "different_whitespace",
        "emoticon_difference",
    ],
)
def test_unproved_original_never_borrows_a_clock(event_store, mutation):
    fixture = _setup(event_store, mutation)
    try:
        rows = fixture.output.execute("SELECT * FROM refs ORDER BY id").fetchall()
        membership = _members(fixture.output)
        result = _recover(fixture)
        assert result["recovered"] == result["verified"] == 0
        assert (
            fixture.output.execute("SELECT * FROM refs ORDER BY id").fetchall() == rows
        )
        assert _members(fixture.output) == membership
        assert (
            fixture.output.execute(
                "SELECT COUNT(*) FROM quoted_clock_evidence"
            ).fetchone()[0]
            == 0
        )
    finally:
        fixture.source.close()
        fixture.output.close()


def test_budget_failure_does_not_publish_partial_clock_updates(
    event_store, monkeypatch
):
    fixture = _setup(event_store)
    try:
        before = fixture.output.execute("SELECT * FROM refs ORDER BY id").fetchall()
        monkeypatch.setattr(recovery, "MAX_SOURCE_BYTES", 1)
        assert _recover(fixture)["status"] == "bounded"
        assert (
            fixture.output.execute("SELECT * FROM refs ORDER BY id").fetchall()
            == before
        )
    finally:
        fixture.source.close()
        fixture.output.close()


def test_uncached_original_is_found_only_inside_its_indexed_source_scope(event_store):
    fixture = _setup(event_store)
    try:
        fixture.output.execute("DELETE FROM refs WHERE id=?", (fixture.original_id,))
        queries = []
        fixture.source.set_trace_callback(queries.append)
        assert _recover(fixture)["recovered"] == 1
        lookups = [
            query for query in queries if "SELECT id FROM main.messages WHERE" in query
        ]
        assert lookups and all(
            "conversation_id IN" in query and "$.server_id" in query
            for query in lookups
        )
    finally:
        fixture.source.close()
        fixture.output.close()


def test_exact_original_of_other_author_is_proved_without_changing_membership(
    event_store,
):
    fixture = _setup(event_store, "different_original_author")
    try:
        before = fixture.output.execute("SELECT * FROM refs ORDER BY id").fetchall()
        membership = _members(fixture.output)
        result = _recover(fixture)
        assert result["recovered"] == 0 and result["verified"] == 1
        assert result["rejected_author_ids"] == [fixture.reference_id]
        assert (
            fixture.output.execute("SELECT * FROM refs ORDER BY id").fetchall()
            == before
        )
        assert _members(fixture.output) == membership
        evidence = fixture.output.execute(
            "SELECT status,original_sender_id,reference_before_json FROM quoted_clock_evidence"
        ).fetchone()
        assert evidence[:2] == ("wrong_author", fixtures.OWNER)
        assert json.loads(evidence[2])["id"] == fixture.reference_id
        fixture.output.execute("DELETE FROM refs WHERE id=?", (fixture.reference_id,))
        assert _recover(fixture)["verified"] == 0
        retained = fixture.output.execute(
            "SELECT * FROM quoted_clock_evidence"
        ).fetchall()
        assert len(retained) == 1
        copied = sqlite3.connect(":memory:")
        try:
            assert recovery.copy_clock_evidence(fixture.output, copied) == 1
            assert (
                copied.execute("SELECT * FROM quoted_clock_evidence").fetchall()
                == retained
            )
        finally:
            copied.close()
    finally:
        fixture.source.close()
        fixture.output.close()


def test_budget_failure_preserves_previously_verified_clock_and_evidence(
    event_store, monkeypatch
):
    fixture = _setup(event_store)
    try:
        assert _recover(fixture)["recovered"] == 1
        before = fixture.output.execute("SELECT * FROM refs ORDER BY id").fetchall()
        evidence = fixture.output.execute(
            "SELECT * FROM quoted_clock_evidence"
        ).fetchall()
        monkeypatch.setattr(recovery, "MAX_SOURCE_BYTES", 1)
        assert _recover(fixture)["status"] == "bounded"
        assert (
            fixture.output.execute("SELECT * FROM refs ORDER BY id").fetchall()
            == before
        )
        assert (
            fixture.output.execute("SELECT * FROM quoted_clock_evidence").fetchall()
            == evidence
        )
    finally:
        fixture.source.close()
        fixture.output.close()


def test_scoped_row_budget_rejects_lookup_without_partial_updates(
    event_store, monkeypatch
):
    fixture = _setup(event_store)
    try:
        before = fixture.output.execute("SELECT * FROM refs ORDER BY id").fetchall()
        monkeypatch.setattr(recovery, "MAX_SCOPED_ROWS", 1)
        assert _recover(fixture)["status"] == "bounded"
        assert (
            fixture.output.execute("SELECT * FROM refs ORDER BY id").fetchall()
            == before
        )
    finally:
        fixture.source.close()
        fixture.output.close()
