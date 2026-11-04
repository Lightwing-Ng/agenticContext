"""Opaque packet identities link complete copies while preserving receiving evidence.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core.wechat_archive import _identifier
from app.core.wechat_contacts import canonical_contact_id
from tests import test_wechat_forwarded_original_precedence as original


CID, TARGET = original.CID, original.TARGET
statement_store = original.statement_store
native_html_store = original.native_html_store
forwarded_origin_store = original.forwarded_origin_store


@pytest.fixture
def identified_packet_store(forwarded_origin_store):
    fixture = forwarded_origin_store
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        parent = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (fixture.native_parent,)
            ).fetchone()
        )
        native_proof = json.loads(parent["provenance"])
        parts = [
            part
            for part in native_proof["text_components"]
            if part.get("source_sender_id") == TARGET
            and part.get("kind") == "forwarded_text"
        ][:4]
        assert len(parts) == 4 and len({part["source_data_id"] for part in parts}) == 4
        for part in parts:
            part["original_message_type"] = 1
            part["source_timestamp"] = datetime.fromtimestamp(
                int(part["source_create_time"]), timezone(timedelta(hours=8))
            ).strftime("%Y-%m-%d %H:%M:%S")
        template_conversation = dict(
            connection.execute(
                "SELECT * FROM conversations WHERE id=?", (parent["conversation_id"],)
            ).fetchone()
        )
        connection.executemany(
            "DELETE FROM messages WHERE id=?", [(key,) for key in fixture.original_ids]
        )
        connection.execute("DELETE FROM messages WHERE id=?", (fixture.native_parent,))
        contexts, parents = [], []
        for number in range(3):
            peer = "identified-receiving-" + str(number % 2) + "@chatroom"
            conversation = "wechat:" + _identifier(fixture.native_source, peer)
            if conversation not in contexts:
                original._insert(
                    connection,
                    "conversations",
                    {
                        **template_conversation,
                        "id": conversation,
                        "peer_id": peer,
                        "title": "Synthetic receiving group " + str(number % 2),
                        "kind": "group",
                    },
                )
                contexts.append(conversation)
            stamp = datetime.fromisoformat(parent["timestamp_value"]) + timedelta(
                days=number
            )
            proof = deepcopy(native_proof)
            proof.update(
                record_id="identified-parent-" + str(number),
                server_id=str(8123456789012345600 + number),
                text_components=deepcopy(parts),
                source_time=stamp.isoformat(),
                unix_timestamp=int(stamp.timestamp()),
            )
            proof["locator"].update(
                local_id=500 + number,
                table="Chat_" + hashlib.md5(peer.encode()).hexdigest(),
            )
            for position, part in enumerate(proof["text_components"], 1):
                part["forwarded_item_path"] = [position]
            row = {
                **parent,
                "id": _identifier(fixture.native_source, proof["record_id"]),
                "conversation_id": conversation,
                "ordinal": number + 1,
                "sender_id": fixture.owner
                if not number
                else "wxid_forwarder_" + str(number),
                "direction": "sent" if not number else "received",
                "timestamp_value": stamp.isoformat(),
                "time_sort": stamp.timestamp(),
                "provenance": json.dumps(proof),
                "text": original.native_components_text(proof["text_components"]),
            }
            row["search_text"] = row["text"].casefold()
            original._insert(connection, "messages", row)
            parents.append(row["id"])
        html = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (fixture.html_parent,)
            ).fetchone()
        )
        proof = json.loads(html["provenance"])
        leaves = [
            part
            for part in proof["forwarded_components"]
            if part.get("sender_native_id") == TARGET
        ][:4]
        original._reindex_html(leaves)
        for leaf in leaves:
            leaf["type"] = "1"
        proof["forwarded_components"] = leaves
        other_owner = "wxid_other_receiving_owner"
        proof["sender_native_id"] = "wxid_html_forwarder"
        connection.execute(
            "UPDATE messages SET direction='received',provenance=? WHERE id=?",
            (json.dumps(proof), fixture.html_parent),
        )
        html_peer = "other-owner-receiving@chatroom"
        connection.execute(
            "UPDATE partitions SET account_id=?,account_key=? WHERE id=?",
            (other_owner, "account:" + other_owner, fixture.html_source),
        )
        connection.execute(
            "UPDATE conversations SET account_key=?,peer_id=?,kind='group',title='Other owner receiving group' WHERE id=?",
            ("account:" + other_owner, html_peer, html["conversation_id"]),
        )
        snapshot = proof["source_snapshot"]
        metadata = json.loads(
            connection.execute(
                "SELECT metadata FROM source_snapshots WHERE id=?", (snapshot,)
            ).fetchone()[0]
        )
        metadata.update(
            source_account_id=other_owner,
            owner_evidence={
                "native_username": other_owner,
                "native_contact_id": canonical_contact_id(
                    other_owner, authority="native_account_owner"
                ),
            },
        )
        metadata["source_conversation_catalog"][0].update(
            index_avatar_native_id=html_peer, conversation_kind="group"
        )
        connection.execute(
            "UPDATE source_snapshots SET metadata=? WHERE id=?",
            (json.dumps(metadata), snapshot),
        )
    original._update_proof(fixture, fixture.html_parent, lambda _proof: None)
    return SimpleNamespace(
        **vars(fixture),
        parents=parents,
        receiving_contexts=contexts,
        html_owner=other_owner,
    )


def _observations(fixture):
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["status"] == "ready"
    exported = original._export(fixture, page)
    return page, exported


def _separate_html(fixture, expected_html=4):
    page, exported = _observations(fixture)
    html = [
        item for item in exported if item["parent_message_id"] == fixture.html_parent
    ]
    native = [item for item in exported if item["parent_message_id"] in fixture.parents]
    assert len(html) == expected_html and len(native) == 12
    assert {item["statement_id"] for item in html}.isdisjoint(
        item["statement_id"] for item in native
    )
    assert page["observation_count"] == 12 + expected_html


def test_complete_identified_copies_across_owners_keep_four_ipad_statements_and_all_receipts(
    identified_packet_store,
):
    fixture = identified_packet_store
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    page, exported = _observations(fixture)
    assert page["total_count"] == 4 and page["observation_count"] == 16
    assert len(exported) == len({item["id"] for item in exported}) == 16
    assert {item["source_label"] for item in page["items"]} == {"iPad archive"}
    assert all(item["reference_kind"] == "native_forwarded" for item in page["items"])
    assert all(item["observation_count"] == 4 for item in page["items"])
    assert {item["account_key"] for item in exported} == {
        "account:" + fixture.owner,
        "account:" + fixture.html_owner,
    }
    assert {
        item["conversation_id"]
        for item in exported
        if item["parent_message_id"] in fixture.parents
    } == set(fixture.receiving_contexts)
    for item in page["items"]:
        receipts = [
            row for row in exported if row["statement_id"] == item["statement_id"]
        ]
        assert {row["parent_message_id"] for row in receipts} == {
            *fixture.parents,
            fixture.html_parent,
        }
        assert len({row["anchor_id"] for row in receipts}) == 4
        assert all(row["original_conversation_unknown"] for row in receipts)
        assert all(
            row["timestamp_value"].endswith("+08:00")
            for row in receipts
            if row["reference_kind"] == "native_forwarded"
        )
        assert all(
            "+" not in row["timestamp_value"]
            for row in receipts
            if row["reference_kind"] == "html_forwarded"
        )
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


@pytest.mark.parametrize(
    "damage",
    [
        "resource",
        "nested",
        "omitted",
        "partial",
        "decimal",
        "mixed",
        "duplicate",
        "reordered",
    ],
)
def test_noncomplete_or_nonopaque_html_packet_does_not_acquire_native_statements(
    identified_packet_store, damage
):
    fixture = identified_packet_store

    def mutate(proof):
        parts = proof["forwarded_components"]
        if damage == "resource":
            parts.append(
                {
                    **deepcopy(parts[-1]),
                    "text": "[Image]",
                    "text_kind": "rendered_app_text",
                    "type": "3",
                    "sender_native_id": "wxid_resource_sender",
                    "avatar_path": "Portrait/wxid_resource_sender.jpg",
                    "export_msgid": "413_" + hashlib.md5(b"Resource frame").hexdigest(),
                    "forwarded_components": [],
                }
            )
        elif damage == "nested":
            proof["forwarded_components"] = [
                {
                    **deepcopy(parts[0]),
                    "text_kind": "forwarded_text",
                    "text": "Nested packet",
                    "forwarded_components": parts,
                }
            ]
        elif damage == "omitted":
            proof["omitted_forwarded_items"] = 1
        elif damage == "partial":
            proof["forwarded_components"] = parts[:-1]
        elif damage in {"decimal", "mixed"}:
            for number, part in enumerate(parts):
                if damage == "decimal" or number == 0:
                    part["export_msgid"] = "413_" + str(number)
        elif damage == "duplicate":
            parts[-1]["export_msgid"] = parts[0]["export_msgid"]
        else:
            proof["forwarded_components"] = list(reversed(parts))
        original._reindex_html(proof["forwarded_components"])

    original._update_proof(fixture, fixture.html_parent, mutate)
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    _separate_html(fixture, 3 if damage == "partial" else 4)
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


@pytest.mark.parametrize("conflict", ["body", "clock", "author"])
def test_one_conflicting_id_poisons_the_whole_new_cross_owner_cohort(
    identified_packet_store, conflict
):
    fixture = identified_packet_store

    def mutate(proof):
        child = proof["forwarded_components"][0]
        if conflict == "body":
            child["text"] += " conflicting source text"
        elif conflict == "clock":
            child["raw_timestamp"] = "2020-08-12 13:46:59"
        else:
            child.update(
                sender_native_id="wxid_other_child_author",
                avatar_path="Portrait/wxid_other_child_author.jpg",
            )

    original._update_proof(fixture, fixture.html_parent, mutate)
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    _separate_html(fixture, 3 if conflict == "author" else 4)
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def test_same_body_and_clock_without_the_same_packet_ids_remains_independent(
    identified_packet_store,
):
    fixture = identified_packet_store

    def mutate(proof):
        for number, part in enumerate(proof["forwarded_components"]):
            part["export_msgid"] = (
                "413_"
                + hashlib.md5(("Unrelated packet " + str(number)).encode()).hexdigest()
            )

    original._update_proof(fixture, fixture.html_parent, mutate)
    _separate_html(fixture)


@pytest.mark.parametrize("constraint", ["server", "chat"])
def test_packet_ids_do_not_override_asserted_original_server_or_chat_conflicts(
    identified_packet_store, constraint
):
    fixture = identified_packet_store

    def native_assertion(proof):
        child = proof["text_components"][0]
        if constraint == "server":
            child["source_ids"] = {"svrid": "7999999999999999991"}
        else:
            child["source_chat_id"] = "original-one@chatroom"

    def html_assertion(proof):
        child = proof["forwarded_components"][0]
        if constraint == "server":
            child["source_ids"] = {"svrid": "7999999999999999992"}
        else:
            child["source_chat_id"] = "original-two@chatroom"

    original._update_proof(fixture, fixture.parents[0], native_assertion)
    original._update_proof(fixture, fixture.html_parent, html_assertion)
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    _separate_html(fixture)
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())
