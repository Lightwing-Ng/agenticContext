"""Known resource positions cannot hide or duplicate flat transcript text events.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import sqlite3

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core import wechat_forwarded_origins as origins
from app.core.wechat_archive import _identifier
from tests import test_wechat_forwarded_original_precedence as original
from tests import test_wechat_legacy_packet_precedence as legacy


CID, TARGET = original.CID, original.TARGET
statement_store = original.statement_store
native_html_store = original.native_html_store
forwarded_origin_store = original.forwarded_origin_store
legacy_packet_store = legacy.legacy_packet_store


@pytest.fixture
def mixed_packet_store(legacy_packet_store):
    """Retain twenty flat positions: eighteen text leaves and two image receipts."""
    fixture = legacy_packet_store
    html, native, _ = legacy._packet_pair(fixture)
    source_parts = native[1]["text_components"]
    original_positions = dict(zip((1, 2, 4, 9, 11, 17, 19, 20, 3), source_parts))
    components, html_parts = [], []
    templates = {
        (part["sender_native_id"], part["text"]): part
        for part in html[1]["forwarded_components"]
    }
    filler = deepcopy(source_parts[-1])
    filler_clock = datetime.fromisoformat("2021-10-19T16:19:53+08:00")
    for number in range(1, 21):
        if number in {8, 16}:
            part = {
                "kind": "forwarded_resource",
                "resource_kind": "image",
                "forwarded_data_type": 2,
                "text": "",
                "source_sender_id": fixture.owner,
            }
        elif number in original_positions:
            part = deepcopy(original_positions[number])
        else:
            stamp = filler_clock + timedelta(seconds=number)
            part = {
                **deepcopy(filler),
                "text": "Unmatched owner filler " + str(number),
                "source_create_time": str(int(stamp.timestamp())),
                "source_timestamp": stamp.strftime("%Y-%m-%d %H:%M:%S"),
            }
        part.update(source_data_id=str(number - 1), forwarded_item_path=[number])
        if part["kind"] == "forwarded_text":
            part["source_timestamp"] = datetime.fromtimestamp(
                int(part["source_create_time"]),
                tz=datetime.fromisoformat(native[0]["timestamp_value"]).tzinfo,
            ).strftime("%Y-%m-%d %H:%M:%S")
        components.append(part)
        if part["kind"] == "forwarded_resource":
            continue
        index = len(html_parts) + 1
        template = templates.get(
            (part["source_sender_id"], part["text"]),
            html[1]["forwarded_components"][-1],
        )
        rendered = {
            **deepcopy(template),
            "export_msgid": "413_" + str(number - 1),
            "ordinal": index + 1,
            "component_ordinal": index,
            "sender_native_id": part["source_sender_id"],
            "text": part["text"],
            "raw_timestamp": datetime.fromtimestamp(
                int(part["source_create_time"]),
                tz=datetime.fromisoformat(native[0]["timestamp_value"]).tzinfo,
            ).strftime("%Y-%m-%d %H:%M:%S"),
        }
        rendered["source_locator"].update(
            message_ordinal=index + 1, page_message_ordinal=index + 1
        )
        html_parts.append(rendered)
    original._update_proof(
        fixture,
        fixture.native_parent,
        lambda proof: proof.update(
            text_components=components, omitted_forwarded_items=0
        ),
    )
    original._update_proof(
        fixture,
        fixture.html_parent,
        lambda proof: proof.update(forwarded_components=html_parts),
    )
    fixture.native_paths = [
        ["native", number]
        for number, part in enumerate(components, 1)
        if part.get("source_sender_id") == TARGET and part["kind"] == "forwarded_text"
    ]
    fixture.html_paths = [
        [number]
        for number, part in enumerate(html_parts, 1)
        if part["sender_native_id"] == TARGET
    ]
    return fixture


def test_complete_mixed_packet_links_all_eighteen_text_leaves(mixed_packet_store):
    fixture = mixed_packet_store
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    html, native, snapshots = legacy._packet_pair(fixture)
    assert len(native[1]["text_components"]) == 20
    assert len(native[2]) == len(html[1]["forwarded_components"]) == 18
    assert all(not part["_packet_flat_text_only"] for part in native[2])
    links = origins._packet_bridge(html, native, snapshots)
    assert len(links) == 18
    assert [path for path, _ in links] == [(number,) for number in range(1, 19)]
    page = analysis.query_contact_analysis(fixture.store, CID)
    legacy._assert_originals_and_receipts(fixture, page)
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


@pytest.mark.parametrize(
    "damage",
    [
        "unknown_resource",
        "hidden_resource_text",
        "resource_type_mismatch",
        "duplicate_position",
        "noncontiguous_position",
        "missing_resource",
        "resource_id",
        "duplicate_resource_id",
        "changed_text_id",
        "missing_text",
        "omission_receipt",
        "nested",
        "hidden_nested_resource",
        "html_hole_suffix",
        "html_missing_text",
    ],
)
def test_mixed_packet_positions_fail_closed(mixed_packet_store, damage):
    html, native, snapshots = legacy._packet_pair(mixed_packet_store)
    row, proof, _, scope = native
    parts, leaves = proof["text_components"], html[1]["forwarded_components"]
    if damage == "unknown_resource":
        parts[7].update(resource_kind="unknown", forwarded_data_type=999)
    elif damage == "hidden_resource_text":
        parts[7]["text"] = "A resource cannot conceal a text leaf"
    elif damage == "resource_type_mismatch":
        parts[7]["forwarded_data_type"] = 3
    elif damage == "duplicate_position":
        parts[7]["forwarded_item_path"] = [7]
    elif damage == "noncontiguous_position":
        parts[7]["forwarded_item_path"] = [99]
    elif damage == "missing_resource":
        parts.pop(7)
    elif damage == "resource_id":
        parts[7]["source_data_id"] = "07"
    elif damage == "duplicate_resource_id":
        parts[7]["source_data_id"] = "6"
    elif damage == "changed_text_id":
        parts[8]["source_data_id"] = "99"
        leaves[7]["export_msgid"] = "413_99"
    elif damage == "missing_text":
        parts.pop(8)
        leaves.pop(7)
    elif damage == "omission_receipt":
        proof["omitted_forwarded_items"] = 1
    elif damage == "nested":
        proof["text_components"] = [
            {
                "kind": "forwarded_group",
                "text": original.native_components_text(parts),
                "text_components": parts,
            }
        ]
    elif damage == "hidden_nested_resource":
        parts[7]["text_components"] = [deepcopy(parts[0])]
    elif damage == "html_hole_suffix":
        leaves[7]["export_msgid"] = "413_7"
    else:
        leaves.pop(7)
    details = origins._native_packet(proof, row["id"], scope[2])
    assert (
        details is None
        or origins._packet_bridge(html, (row, proof, details, scope), snapshots) is None
    )


def _separate_same_clock_counterpart(fixture, *, origin_kind):
    """Keep a same-body, same-clock event in the opposite thread as independent evidence."""
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        if origin_kind == "group":
            group_peer = "synthetic-original@chatroom"
            connection.execute(
                "UPDATE conversations SET kind='group',peer_id=? WHERE id=?",
                (group_peer, fixture.original_conversation),
            )
            for key, encoded in connection.execute(
                "SELECT id,provenance FROM messages WHERE conversation_id=?",
                (fixture.original_conversation,),
            ).fetchall():
                proof = json.loads(encoded)
                proof["locator"]["table"] = (
                    "Chat_" + hashlib.md5(group_peer.encode()).hexdigest()
                )
                connection.execute(
                    "UPDATE messages SET provenance=? WHERE id=?",
                    (json.dumps(proof), key),
                )
        primary = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (fixture.original_ids[0],)
            ).fetchone()
        )
        conversation = dict(
            connection.execute(
                "SELECT * FROM conversations WHERE id=?",
                (fixture.original_conversation,),
            ).fetchone()
        )
        counterpart_peer = (
            TARGET if origin_kind == "group" else "synthetic-outside@chatroom"
        )
        counterpart_kind = "direct" if origin_kind == "group" else "group"
        conversation.update(
            id="wechat:" + _identifier(fixture.native_source, "opposite-kind-copy"),
            peer_id=counterpart_peer,
            kind=counterpart_kind,
            message_count=1,
        )
        original._insert(connection, "conversations", conversation)
        proof = json.loads(primary["provenance"])
        proof.update(record_id="opposite-kind-copy", server_id="8999999999999999999")
        proof["locator"]["table"] = (
            "Chat_" + hashlib.md5(counterpart_peer.encode()).hexdigest()
        )
        primary.update(
            id=_identifier(fixture.native_source, "opposite-kind-copy"),
            conversation_id=conversation["id"],
            provenance=json.dumps(proof),
        )
        original._insert(connection, "messages", primary)
        servers = {
            body: str(json.loads(encoded)["server_id"])
            for body, encoded in connection.execute(
                "SELECT text,provenance FROM messages WHERE id IN ("
                + ",".join("?" for _ in fixture.original_ids)
                + ")",
                fixture.original_ids,
            )
        }

    def assert_originals(proof):
        for part in proof["text_components"]:
            if part["kind"] == "forwarded_text" and part["source_sender_id"] == TARGET:
                part["source_ids"] = {"svrid": servers[part["text"]]}

    original._update_proof(fixture, fixture.native_parent, assert_originals)
    return primary["id"]


@pytest.mark.parametrize("origin_kind", ["direct", "group"])
def test_mixed_packet_keeps_the_correct_ipad_origin_and_opposite_thread_separate(
    mixed_packet_store, origin_kind
):
    fixture = mixed_packet_store
    counterpart = _separate_same_clock_counterpart(fixture, origin_kind=origin_kind)
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["total_count"] == 7 and page["observation_count"] == 19
    selected = {item["id"]: item for item in page["items"]}
    assert set(selected) == {*fixture.original_ids, counterpart}
    assert selected[counterpart]["observation_count"] == 1
    assert selected[counterpart]["conversation_id"] != fixture.original_conversation
    for key in fixture.original_ids:
        assert selected[key]["source_label"] == "iPad archive"
        assert selected[key]["observation_count"] == 3
        assert selected[key]["kind"] == origin_kind
        assert selected[key]["conversation_id"] == fixture.original_conversation
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def test_mixed_packet_upgrade_reuses_every_accepted_reference(
    mixed_packet_store, monkeypatch
):
    fixture = mixed_packet_store
    counterpart = _separate_same_clock_counterpart(fixture, origin_kind="group")
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    with monkeypatch.context() as previous:
        previous.setattr(analysis, "FORWARDED_ORIGINS_VERSION", "v1.3.1-codex.0")
        previous.setattr(origins, "_flat_packet_ordinal_ids", lambda *_args: None)
        old = analysis.query_contact_analysis(fixture.store, CID)
    assert old["total_count"] == 13 and old["observation_count"] == 19
    cache = analysis.contact_analysis_cache_path(fixture.store, CID)
    with sqlite3.connect(cache) as connection:
        refs = connection.execute("SELECT * FROM refs ORDER BY id").fetchall()

    def no_author_rescan(*_args, **_kwargs):
        pytest.fail("A compatible upgrade must reuse all accepted source references.")

    for name in (
        "_candidate_batches",
        "_candidate_forwarded_batches",
        "_candidate_native_batches",
        "_group_statements",
    ):
        monkeypatch.setattr(analysis, name, no_author_rescan)
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["total_count"] == 7 and page["observation_count"] == 19
    selected = {item["id"]: item for item in page["items"]}
    assert selected[counterpart]["observation_count"] == 1
    assert all(selected[key]["observation_count"] == 3 for key in fixture.original_ids)
    assert all(
        selected[key]["source_label"] == "iPad archive" for key in fixture.original_ids
    )
    with sqlite3.connect(cache) as connection:
        assert connection.execute("SELECT * FROM refs ORDER BY id").fetchall() == refs
        assert (
            dict(connection.execute("SELECT key,value FROM info"))[
                "forwarded_origins_version"
            ]
            == origins.VERSION
        )
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())
