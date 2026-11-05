"""Corroborate missing HTML clocks without rewriting native or export observations.

Code version: v1.0.2-codex.0
"""

from __future__ import annotations

from contextlib import closing

from datetime import datetime
import hashlib
import json
import sqlite3

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core import wechat_forwarded_origins as origins
from app.core.wechat_archive import _identifier
from tests import test_wechat_forwarded_original_precedence as fixtures


CID = fixtures.CID
native_html_store = fixtures.native_html_store
statement_store = fixtures.statement_store
forwarded_origin_store = fixtures.forwarded_origin_store


def _missing_html_clocks(fixture, raw):
    def mutate(proof):
        for part in proof["forwarded_components"]:
            if raw is None:
                part.pop("raw_timestamp", None)
            else:
                part["raw_timestamp"] = raw

    fixtures._update_proof(fixture, fixture.html_parent, mutate)


def _remove_originals(fixture):
    with sqlite3.connect(fixture.history) as connection:
        connection.execute(
            "DELETE FROM messages WHERE conversation_id=?",
            (fixture.original_conversation,),
        )


def _native_naive_clocks(fixture):
    with sqlite3.connect(fixture.history) as connection:
        proof = json.loads(
            connection.execute(
                "SELECT provenance FROM messages WHERE id=?", (fixture.html_parent,)
            ).fetchone()[0]
        )
    clocks = {
        part["export_msgid"].split("_", 1)[1]: part["raw_timestamp"]
        for part in proof["forwarded_components"]
    }

    def mutate(proof):
        for part in proof["text_components"]:
            if part["kind"] == "forwarded_text":
                part["source_create_time"] = "0"
                part["source_timestamp"] = clocks[part["source_data_id"]]

    fixtures._update_proof(fixture, fixture.native_parent, mutate)


def _reference_rows(fixture):
    path = analysis.contact_analysis_cache_path(fixture.store, CID)
    with closing(sqlite3.connect(path)) as connection, connection:
        return {
            table: connection.execute(
                "SELECT * FROM " + table + " ORDER BY 1"
            ).fetchall()
            for table in ("refs", "statement_groups", "statement_members")
        }


@pytest.mark.parametrize("raw", [None, "", "   ", "1970-01-01 08:00:00", "0"])
@pytest.mark.parametrize("naive", [False, True])
def test_missing_html_clock_prefers_native_and_preserves_every_observation(
    forwarded_origin_store, raw, naive
):
    fixture = forwarded_origin_store
    if naive:
        _native_naive_clocks(fixture)
    _missing_html_clocks(fixture, raw)
    _remove_originals(fixture)
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()

    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["total_count"] == 6 and page["observation_count"] == 12
    assert all(item["reference_kind"] == "native_forwarded" for item in page["items"])
    for item in page["items"]:
        assert item["timestamp_value"]
        assert item["display_timestamp_value"] == item["timestamp_value"]
        assert item["original_conversation_unknown"]
        assert len(item["observations"]) == 2
        native = next(
            value
            for value in item["observations"]
            if value["source_id"] == fixture.native_source
        )
        html = next(
            value
            for value in item["observations"]
            if value["source_id"] == fixture.html_source
        )
        assert native["original_conversation_unknown"]
        assert html["original_conversation_unknown"]
        assert html["timestamp_value"] == ""
        assert html["source_clock"] == (raw or "")
        assert {native["account_key"], html["account_key"]} == {
            "account:" + fixture.owner
        }
        if naive:
            assert item["time_basis"] == "Source clock; timezone unknown"
            assert datetime.fromisoformat(item["timestamp_value"]).tzinfo is None
    exported = fixtures._export(fixture, page)
    assert len(exported) == 12
    assert {item["source_id"] for item in exported} == {
        fixture.native_source,
        fixture.html_source,
    }
    references = _reference_rows(fixture)
    assert len(references["refs"]) == len(references["statement_members"]) == 12
    with sqlite3.connect(
        analysis.contact_analysis_cache_path(fixture.store, CID)
    ) as connection:
        assert not {"text", "body", "provenance"} & {
            row[1] for row in connection.execute("PRAGMA table_info(refs)")
        }
    analysis._build_index(fixture.store, CID, page["source_fingerprint"])
    assert _reference_rows(fixture) == references
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


@pytest.mark.parametrize("raw", ["2020-08-12 13:46:45", "2021-10-20", "invalid clock"])
def test_nonempty_conflicting_or_malformed_html_clock_remains_separate(
    forwarded_origin_store, raw
):
    fixture = forwarded_origin_store
    _missing_html_clocks(fixture, "1970-01-01 08:00:00")
    fixtures._update_proof(
        fixture,
        fixture.html_parent,
        lambda proof: proof["forwarded_components"][0].update(raw_timestamp=raw),
    )
    _remove_originals(fixture)
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["total_count"] == page["observation_count"] == 12
    assert all(item["observation_count"] == 1 for item in page["items"])


@pytest.mark.parametrize("raw", [0, True, [], {}])
def test_non_string_html_clock_is_not_missing_evidence(raw):
    assert not origins._bridge_component_clock(raw, "2021-10-20T12:34:56")


@pytest.mark.parametrize("native_clock", ["", "1970-01-01 08:00:00", "2021-10-20"])
def test_missing_native_child_time_never_uses_the_outer_forward_time(
    forwarded_origin_store, native_clock
):
    fixture = forwarded_origin_store

    def mutate(proof):
        for part in proof["text_components"]:
            if part["kind"] == "forwarded_text":
                part.update(source_create_time="0", source_timestamp=native_clock)

    fixtures._update_proof(fixture, fixture.native_parent, mutate)
    _missing_html_clocks(fixture, "1970-01-01 08:00:00")
    _remove_originals(fixture)
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["total_count"] == page["observation_count"] == 12
    assert all(not item["timestamp_value"] for item in page["items"])
    assert all(item["observation_count"] == 1 for item in page["items"])


def test_partial_missing_html_clocks_keep_genuine_agreeing_clocks(
    forwarded_origin_store,
):
    fixture = forwarded_origin_store
    fixtures._update_proof(
        fixture,
        fixture.html_parent,
        lambda proof: proof["forwarded_components"][0].update(
            raw_timestamp="1970-01-01 08:00:00"
        ),
    )
    _remove_originals(fixture)
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["total_count"] == 6 and page["observation_count"] == 12
    assert all(item["reference_kind"] == "native_forwarded" for item in page["items"])


def test_group_receiving_packets_never_establish_a_private_original_thread(
    forwarded_origin_store,
):
    fixture = forwarded_origin_store
    peer = "1234567890@chatroom"
    with sqlite3.connect(fixture.history) as connection:
        for parent in (fixture.native_parent, fixture.html_parent):
            connection.execute(
                "UPDATE conversations SET kind='group',peer_id=? WHERE id="
                "(SELECT conversation_id FROM messages WHERE id=?)",
                (peer, parent),
            )
        proof = json.loads(
            connection.execute(
                "SELECT provenance FROM messages WHERE id=?", (fixture.html_parent,)
            ).fetchone()[0]
        )
        snapshot = proof["source_snapshot"]
        metadata = json.loads(
            connection.execute(
                "SELECT metadata FROM source_snapshots WHERE id=?", (snapshot,)
            ).fetchone()[0]
        )
        metadata["source_conversation_catalog"][0].update(
            index_avatar_native_id=peer, conversation_kind="group"
        )
        connection.execute(
            "UPDATE source_snapshots SET metadata=? WHERE id=?",
            (json.dumps(metadata), snapshot),
        )
    fixtures._update_proof(
        fixture,
        fixture.native_parent,
        lambda proof: proof["locator"].update(
            table="Chat_"
            + hashlib.md5(peer.encode(), usedforsecurity=False).hexdigest()
        ),
    )
    _missing_html_clocks(fixture, "1970-01-01 08:00:00")
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["total_count"] == 12 and page["observation_count"] == 18
    receiving = [item for item in page["items"] if item["is_embedded"]]
    assert len(receiving) == 6
    assert all(item["reference_kind"] == "native_forwarded" for item in receiving)
    assert all(item["kind"] == "group" for item in receiving)
    assert all(item["original_conversation_unknown"] for item in receiving)
    assert all(item["observation_count"] == 2 for item in receiving)


def test_ambiguous_native_packet_clocks_do_not_choose_an_arbitrary_origin(
    forwarded_origin_store,
):
    fixture = forwarded_origin_store
    _remove_originals(fixture)
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        other = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (fixture.native_parent,)
            ).fetchone()
        )
        proof = json.loads(other["provenance"])
        proof["record_id"] = "independent-native-forward-envelope"
        proof["server_id"] = "8123456789012345679"
        for part in proof["text_components"]:
            if part["kind"] == "forwarded_text":
                part["source_create_time"] = str(int(part["source_create_time"]) + 1)
        other.update(
            id=_identifier(fixture.native_source, proof["record_id"]),
            ordinal=2,
            provenance=json.dumps(proof),
        )
        fixtures._insert(connection, "messages", other)
    _missing_html_clocks(fixture, "1970-01-01 08:00:00")
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["total_count"] == page["observation_count"] == 18
    assert all(item["observation_count"] == 1 for item in page["items"])


def test_routed_native_packet_keeps_its_native_source_and_html_container(
    forwarded_origin_store,
):
    fixture = forwarded_origin_store
    _native_naive_clocks(fixture)
    _missing_html_clocks(fixture, "1970-01-01 08:00:00")
    _remove_originals(fixture)
    with sqlite3.connect(fixture.history) as connection:
        receiving = connection.execute(
            "SELECT conversation_id FROM messages WHERE id=?", (fixture.html_parent,)
        ).fetchone()[0]
        connection.execute(
            "UPDATE messages SET conversation_id=? WHERE id=?",
            (receiving, fixture.native_parent),
        )
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["total_count"] == 6 and page["observation_count"] == 12
    for item in page["items"]:
        assert item["source_id"] == fixture.native_source
        assert item["conversation_id"] == receiving
        assert item["time_basis"] == "Source clock; timezone unknown"
        assert item["original_conversation_unknown"]
        assert {origin["source_id"] for origin in item["observations"]} == {
            fixture.native_source,
            fixture.html_source,
        }
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def test_bridge_only_replay_keeps_proven_originals_without_scanning_primary_refs(
    forwarded_origin_store,
):
    fixture = forwarded_origin_store
    _missing_html_clocks(fixture, "1970-01-01 08:00:00")
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["total_count"] == 6 and page["observation_count"] == 18
    expected = _reference_rows(fixture)
    path = analysis.contact_analysis_cache_path(fixture.store, CID)
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    queries = []

    def reject_new_original(*args):
        raise AssertionError("A bridge-only replay must preserve existing original links.")

    with closing(analysis._readonly(fixture.history)) as source, source, closing(sqlite3.connect(path)) as output, output:
        for reference, statement in output.execute(
            "SELECT sm.reference_id,sm.statement_id FROM statement_members sm "
            "JOIN refs r ON r.id=sm.reference_id WHERE r.source_id=?",
            (fixture.html_source,),
        ).fetchall():
            detached = hashlib.sha256(("detached-" + reference).encode()).hexdigest()
            conversation = output.execute(
                "SELECT conversation_key FROM statement_groups WHERE id=?", (statement,)
            ).fetchone()[0]
            output.execute(
                "INSERT INTO statement_groups VALUES(?,?,?)",
                (detached, reference, conversation),
            )
            output.execute(
                "UPDATE statement_members SET statement_id=? WHERE reference_id=?",
                (detached, reference),
            )
        source.set_trace_callback(queries.append)
        output.set_trace_callback(queries.append)
        origins.link_forwarded_origins(
            source,
            output,
            CID,
            row_select=analysis._ROW_SELECT,
            prepare_rows=analysis._requests,
            native_thread_key=analysis._native_conversation_key,
            native_statement_key=reject_new_original,
            link_originals=False,
        )
    assert not any("WHERE component_path='[]'" in sql for sql in queries)
    assert not any("WHERE m.conversation_id=" in sql for sql in queries)
    assert _reference_rows(fixture) == expected
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def test_source_metadata_decodes_once_per_pass_and_keeps_snapshot_scope(monkeypatch):
    metadata = json.dumps({"source_account_id": "wxid_synthetic_owner", "catalog": [
        {"member": "Synthetic/Conversation" + str(number) + ".html"}
        for number in range(200)
    ]})
    decoded = []
    parse = origins._object

    def tracked(value):
        if value == metadata:
            decoded.append(value)
        return parse(value)

    monkeypatch.setattr(origins, "_object", tracked)
    with sqlite3.connect(":memory:") as source:
        source.row_factory = sqlite3.Row
        source.execute(
            "CREATE TABLE source_snapshots(id INTEGER,source_id TEXT,metadata TEXT)"
        )
        source.execute(
            "INSERT INTO source_snapshots VALUES(?,?,?)", (1, "synthetic-source", metadata)
        )
        snapshots = origins._source_snapshots(source)
        for _ in range(100):
            assert origins._metadata(
                {"source_id": "synthetic-source"}, {"source_snapshot": 1}, snapshots
            ) is snapshots[1]["metadata"]
        assert len(decoded) == 1
        assert origins._metadata(
            {"source_id": "different-source"}, {"source_snapshot": 1}, snapshots
        ) == {}
        assert origins._metadata(
            {"source_id": "synthetic-source"}, {"source_snapshot": True}, snapshots
        ) == {}
