"""Legacy packet positions require complete corroboration and preserve every source.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core import wechat_forwarded_origins as origins
from app.core.wechat_archive import _identifier
from tests import test_wechat_forwarded_original_precedence as original


CID = original.CID
TARGET = original.TARGET
statement_store = original.statement_store
native_html_store = original.native_html_store
forwarded_origin_store = original.forwarded_origin_store


@pytest.fixture
def legacy_packet_store(forwarded_origin_store):
    fixture = forwarded_origin_store

    def native_positions(proof):
        parts = [
            part
            for part in proof["text_components"]
            if part["kind"] == "forwarded_text"
        ]
        for number, part in enumerate(parts, 1):
            part["source_data_id"] = str(number - 1)
            part["forwarded_item_path"] = [number]
        proof["text_components"] = parts
        fixture.native_paths = [
            ["native", number]
            for number, part in enumerate(parts, 1)
            if part["source_sender_id"] == TARGET
        ]

    def html_positions(proof):
        for number, part in enumerate(proof["forwarded_components"]):
            part["export_msgid"] = "413_" + str(number)

    original._update_proof(fixture, fixture.native_parent, native_positions)
    original._update_proof(fixture, fixture.html_parent, html_positions)
    return fixture


def _packet_pair(fixture):
    with closing(sqlite3.connect(fixture.history)) as connection, connection:
        connection.row_factory = sqlite3.Row
        rows = [
            dict(
                connection.execute(
                    analysis._ROW_SELECT + "WHERE m.id=?", (key,)
                ).fetchone()
            )
            for key in (fixture.html_parent, fixture.native_parent)
        ]
        snapshots = origins._source_snapshots(connection)
    html, native = rows
    html_proof = json.loads(html["provenance"])
    native_proof = json.loads(native["provenance"])
    scope = origins._native_scope(
        native, native_proof, snapshots, analysis._native_conversation_key
    )
    assert scope is not None
    details = origins._native_packet(native_proof, native["id"], scope[2])
    assert details is not None
    return (html, html_proof), (native, native_proof, details, scope), snapshots


def _insert_html_only(fixture):
    """Keep an unmatched ordinary HTML observation beside the complete packet."""
    with closing(sqlite3.connect(fixture.history)) as connection, connection:
        connection.row_factory = sqlite3.Row
        row = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (fixture.html_parent,)
            ).fetchone()
        )
        proof = json.loads(row["provenance"])
        row.update(
            id=_identifier(fixture.html_source, "legacy-html-only"),
            ordinal=100,
            sender_id="synthetic-target-export-id",
            sender_label="Target",
            direction="received",
            text="Synthetic HTML-only statement",
            search_text="synthetic html-only statement",
        )
        proof.update(
            record_id="legacy-html-only",
            sender_native_id=TARGET,
            text_extraction_kind="plain_text",
            forwarded_components=[],
            quoted_components=[],
            primary_text_sha256=hashlib.sha256(row["text"].encode()).hexdigest(),
        )
        proof["locator"].update(export_msgid="414", export_msgtype="1")
        row["provenance"] = json.dumps(proof)
        original._insert(connection, "messages", row)
    with closing(sqlite3.connect(fixture.registry)) as connection, connection:
        connection.execute(
            "INSERT INTO sender_bindings VALUES(?,?,?,?,?,?,?)",
            (
                fixture.html_source,
                "synthetic-target-export-id",
                "",
                "",
                CID,
                "corroborated_export_username",
                "{}",
            ),
        )
    return row["id"]


def _assert_originals_and_receipts(fixture, page, *, extra=None):
    assert page["status"] == "ready"
    assert page["total_count"] == 6 + bool(extra)
    assert page["observation_count"] == 18 + bool(extra)
    selected = {item["id"]: item for item in page["items"]}
    assert set(selected) == set(fixture.original_ids) | ({extra} if extra else set())
    for key in fixture.original_ids:
        assert selected[key]["source_label"] == "iPad archive"
        assert not selected[key]["is_embedded"]
        assert selected[key]["observation_count"] == 3
    exported = original._export(fixture, page)
    assert len(exported) == 18 + bool(extra)
    for index, native_path in enumerate(fixture.native_paths):
        receipts = [
            item
            for item in exported
            if (
                item["id"] == fixture.original_ids[index]
                or item["parent_message_id"] == fixture.native_parent
                and item["component_path"] == native_path
                or item["parent_message_id"] == fixture.html_parent
                and item["component_path"] == fixture.html_paths[index]
            )
        ]
        assert len(receipts) == 3
        assert len({item["statement_id"] for item in receipts}) == 1
        assert len({item["anchor_id"] for item in receipts}) == 3
        assert {item["source_id"] for item in receipts} == {
            fixture.native_source,
            fixture.html_source,
        }
    if extra:
        assert selected[extra]["source_label"] == "HTML export"
        assert selected[extra]["observation_count"] == 1


def test_complete_legacy_packet_keeps_ipad_origins_and_unique_html(legacy_packet_store):
    fixture = legacy_packet_store
    extra = _insert_html_only(fixture)
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    html, native, snapshots = _packet_pair(fixture)
    assert len(origins._packet_bridge(html, native, snapshots)) == 9
    page = analysis.query_contact_analysis(fixture.store, CID)
    _assert_originals_and_receipts(fixture, page, extra=extra)
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


@pytest.mark.parametrize(
    "damage",
    [
        "leading_zero",
        "mixed_ids",
        "gap",
        "partial",
        "nested",
        "trailing_resource",
        "omission_receipt",
        "reordered",
        "export_suffix",
    ],
)
def test_legacy_positions_require_a_complete_flat_packet(legacy_packet_store, damage):
    html, native, snapshots = _packet_pair(legacy_packet_store)
    row, proof, _, scope = native
    parts = proof["text_components"]
    leaves = html[1]["forwarded_components"]
    if damage in {"leading_zero", "mixed_ids", "gap"}:
        value = {"leading_zero": "00", "mixed_ids": "a" * 32, "gap": "10"}[damage]
        parts[0]["source_data_id"] = value
        leaves[0]["export_msgid"] = "413_" + value
    elif damage == "partial":
        proof["text_components"] = parts[1:]
        html[1]["forwarded_components"] = leaves[1:]
    elif damage == "nested":
        for part in parts:
            part.pop("forwarded_item_path")
        proof["text_components"] = [
            {
                "kind": "forwarded_group",
                "text": original.native_components_text(parts),
                "text_components": parts,
            }
        ]
    elif damage == "trailing_resource":
        parts.append(
            {"kind": "forwarded_resource", "resource_kind": "image", "text": ""}
        )
    elif damage == "omission_receipt":
        proof["omitted_forwarded_items"] = 1
    elif damage == "reordered":
        proof["text_components"] = list(reversed(parts))
        html[1]["forwarded_components"] = list(reversed(leaves))
    else:
        leaves[0]["export_msgid"] = "413_999"
    details = origins._native_packet(proof, row["id"], scope[2])
    assert details is not None
    if damage in {"trailing_resource", "nested", "omission_receipt"}:
        assert all(not part["_packet_flat_text_only"] for part in details)
    assert origins._packet_bridge(html, (row, proof, details, scope), snapshots) is None


def test_legacy_bridge_still_rejects_scope_clock_author_and_body_conflicts(
    legacy_packet_store,
):
    baseline = _packet_pair(legacy_packet_store)
    for damage in (
        "owner",
        "peer",
        "forwarder",
        "parent_clock",
        "child_clock",
        "author",
        "body",
    ):
        html, native, snapshots = deepcopy(baseline)
        row, proof = html
        if damage == "owner":
            snapshots[proof["source_snapshot"]]["metadata"]["source_account_id"] = (
                "wxid_other_owner"
            )
        elif damage == "peer":
            row["peer_id"] = "wxid_other_peer"
        elif damage == "forwarder":
            proof["sender_native_id"] = "wxid_other_forwarder"
        elif damage == "parent_clock":
            row["timestamp_value"] = "2021-10-20T16:19:54"
        elif damage == "child_clock":
            proof["forwarded_components"][0]["raw_timestamp"] = "2021-10-19 16:19:53"
        elif damage == "author":
            proof["forwarded_components"][0]["sender_native_id"] = "wxid_other_author"
        else:
            proof["forwarded_components"][0]["text"] += " altered"
        assert origins._packet_bridge(html, native, snapshots) is None, damage


def test_ambiguous_native_legacy_packets_keep_independent_html_receipts(
    legacy_packet_store,
):
    fixture = legacy_packet_store
    with closing(sqlite3.connect(fixture.history)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.executemany(
            "DELETE FROM messages WHERE id=?", [(key,) for key in fixture.original_ids]
        )
        row = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (fixture.native_parent,)
            ).fetchone()
        )
        proof = json.loads(row["provenance"])
        proof.update(record_id="second-legacy-packet", server_id="8123456789012345679")
        proof["locator"]["local_id"] = 408
        row.update(
            id=_identifier(fixture.native_source, "second-legacy-packet"),
            ordinal=2,
            provenance=json.dumps(proof),
        )
        original._insert(connection, "messages", row)
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    page = analysis.query_contact_analysis(fixture.store, CID)
    exported = original._export(fixture, page)
    html = [
        item for item in exported if item["parent_message_id"] == fixture.html_parent
    ]
    native = [
        item for item in exported if item["parent_message_id"] != fixture.html_parent
    ]
    assert len(html) == 6 and len(native) == 12
    assert {item["statement_id"] for item in html}.isdisjoint(
        item["statement_id"] for item in native
    )
    assert page["observation_count"] == 18
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def test_previous_complete_contact_cache_upgrades_without_author_rescan(
    legacy_packet_store, monkeypatch
):
    fixture = legacy_packet_store
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    with monkeypatch.context() as previous:
        previous.setattr(analysis, "INDEX_VERSION", "v1.11.1-codex.0")
        previous.setattr(origins, "_bridge_packet_data_ids", lambda _details: None)
        old = analysis.query_contact_analysis(fixture.store, CID)
    assert old["status"] == "ready"
    assert old["total_count"] == 12 and old["observation_count"] == 18
    cache = analysis.contact_analysis_cache_path(fixture.store, CID)
    with closing(sqlite3.connect(cache)) as connection, connection:
        references = connection.execute("SELECT * FROM refs ORDER BY id").fetchall()

    def no_author_rescan(*_args, **_kwargs):
        raise AssertionError(
            "A complete compatible cache must retain its accepted source references."
        )

    for name in (
        "_candidate_batches",
        "_candidate_forwarded_batches",
        "_candidate_native_batches",
        "_group_statements",
    ):
        monkeypatch.setattr(analysis, name, no_author_rescan)
    page = analysis.query_contact_analysis(fixture.store, CID)
    _assert_originals_and_receipts(fixture, page)
    with closing(sqlite3.connect(cache)) as connection, connection:
        assert (
            connection.execute("SELECT * FROM refs ORDER BY id").fetchall()
            == references
        )
        assert (
            dict(connection.execute("SELECT key,value FROM info"))["version"]
            == analysis.INDEX_VERSION
        )
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())
