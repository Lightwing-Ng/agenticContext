"""Short transcript copies require paired independent native/HTML parent proof.

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
from tests import test_wechat_identified_packet_copies as identified


CID = identified.CID
statement_store = identified.statement_store
native_html_store = identified.native_html_store
forwarded_origin_store = identified.forwarded_origin_store
identified_packet_store = identified.identified_packet_store


def _write_parent(connection, row, proof):
    if "forwarded_components" in proof:
        primary = "\n\n".join(part["text"] for part in proof["forwarded_components"])
        proof["primary_text_sha256"] = hashlib.sha256(primary.encode()).hexdigest()
        text = original._render(
            {
                "text": primary,
                "text_kind": "forwarded_text",
                "quoted_components": [],
                "forwarded_components": proof["forwarded_components"],
            }
        )
    else:
        text = original.native_components_text(proof["text_components"])
    connection.execute(
        "UPDATE messages SET text=?,search_text=?,provenance=? WHERE id=?",
        (text, text.casefold(), json.dumps(proof), row["id"]),
    )


def _mutate(fixture, identifier, change):
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        row = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (identifier,)
            ).fetchone()
        )
        proof = json.loads(row["provenance"])
        change(row, proof)
        columns = [key for key in row if key != "id"]
        connection.execute(
            "UPDATE messages SET "
            + ",".join(key + "=?" for key in columns)
            + " WHERE id=?",
            [row[key] for key in columns] + [identifier],
        )
        _write_parent(connection, row, proof)


@pytest.fixture
def paired_two_item_store(identified_packet_store):
    fixture = identified_packet_store
    native = fixture.parents[:2]
    html = []
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("DELETE FROM messages WHERE id=?", (fixture.parents[2],))
        conversation = connection.execute(
            "SELECT * FROM conversations WHERE id=?", (fixture.receiving_contexts[0],)
        ).fetchone()
        peer = conversation["peer_id"]
        forwarder = "wxid_same_short_packet_forwarder"
        native_rows = []
        for identifier in native:
            row = dict(
                connection.execute(
                    "SELECT * FROM messages WHERE id=?", (identifier,)
                ).fetchone()
            )
            proof = json.loads(row["provenance"])
            proof["text_components"] = proof["text_components"][:2]
            for part in proof["text_components"]:
                # The source's second clock is precise; an epoch is not invented.
                part["source_create_time"] = ""
            proof["locator"]["table"] = "Chat_" + hashlib.md5(peer.encode()).hexdigest()
            row.update(
                conversation_id=fixture.receiving_contexts[0],
                sender_id=forwarder,
                direction="received",
            )
            connection.execute(
                "UPDATE messages SET conversation_id=?,sender_id=?,direction=? WHERE id=?",
                (
                    row["conversation_id"],
                    row["sender_id"],
                    row["direction"],
                    identifier,
                ),
            )
            _write_parent(connection, row, proof)
            native_rows.append(row)
        template = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (fixture.html_parent,)
            ).fetchone()
        )
        html_proof = json.loads(template["provenance"])
        snapshot = html_proof["source_snapshot"]
        metadata = json.loads(
            connection.execute(
                "SELECT metadata FROM source_snapshots WHERE id=?", (snapshot,)
            ).fetchone()[0]
        )
        metadata.update(
            source_account_id=fixture.owner,
            owner_evidence={
                "native_username": fixture.owner,
                "native_contact_id": canonical_contact_id(
                    fixture.owner, authority="native_account_owner"
                ),
            },
        )
        metadata["source_conversation_catalog"][0].update(
            index_avatar_native_id=peer, conversation_kind="group"
        )
        connection.execute(
            "UPDATE source_snapshots SET metadata=? WHERE id=?",
            (json.dumps(metadata), snapshot),
        )
        connection.execute(
            "UPDATE partitions SET account_id=?,account_key=? WHERE id=?",
            (fixture.owner, "account:" + fixture.owner, fixture.html_source),
        )
        connection.execute(
            "UPDATE conversations SET account_key=?,peer_id=?,kind='group' WHERE id=?",
            ("account:" + fixture.owner, peer, template["conversation_id"]),
        )
        for number in range(2):
            row = deepcopy(template)
            row.update(
                ordinal=number + 1,
                timestamp_value=datetime.fromisoformat(
                    native_rows[number]["timestamp_value"]
                )
                .replace(tzinfo=None)
                .isoformat(),
                direction="received",
            )
            proof = deepcopy(html_proof)
            proof["sender_native_id"] = forwarder
            proof["forwarded_components"] = proof["forwarded_components"][:2]
            local = str(413 + number)
            proof["locator"]["export_msgid"] = local
            proof["record_id"] = _identifier(
                "html-text-v1",
                proof["locator"]["archive_sha256"],
                original._json(proof["locator"]),
            )
            identifier = _identifier(fixture.html_source, proof["record_id"])
            row["id"] = identifier
            html.append(identifier)
            for part in proof["forwarded_components"]:
                part["export_msgid"] = (
                    local + "_" + part["export_msgid"].rsplit("_", 1)[-1]
                )
            if number:
                original._insert(connection, "messages", row)
            else:
                connection.execute(
                    "UPDATE messages SET timestamp_value=?,direction=? WHERE id=?",
                    (row["timestamp_value"], row["direction"], identifier),
                )
            _write_parent(connection, row, proof)
    return SimpleNamespace(**vars(fixture), native_parents=native, html_parents=html)


def _read(fixture):
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["status"] == "ready"
    return page, original._export(fixture, page)


@pytest.mark.parametrize("native_epoch", [False, True])
def test_paired_short_copies_prefer_ipad_keep_all_receipts_and_replay_unchanged(
    paired_two_item_store,
    native_epoch,
):
    fixture = paired_two_item_store
    if native_epoch:

        def add_epoch(_row, proof):
            for part in proof["text_components"]:
                part["source_create_time"] = str(
                    int(
                        datetime.fromisoformat(part["source_timestamp"])
                        .replace(tzinfo=timezone(timedelta(hours=8)))
                        .timestamp()
                    )
                )

        for parent in fixture.native_parents:
            _mutate(fixture, parent, add_epoch)
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    page, receipts = _read(fixture)
    assert page["total_count"] == 2 and page["observation_count"] == 8
    assert len(receipts) == len({item["id"] for item in receipts}) == 8
    assert all(item["source_label"] == "iPad archive" for item in page["items"])
    assert all(item["observation_count"] == 4 for item in page["items"])
    assert all(item["original_conversation_unknown"] for item in receipts)
    assert all(
        item["timestamp_value"].endswith("+08:00") == native_epoch
        for item in receipts
        if item["reference_kind"] == "native_forwarded"
    )
    assert all(
        "+" not in item["timestamp_value"]
        for item in receipts
        if item["reference_kind"] == "html_forwarded"
    )
    cache = analysis.contact_analysis_cache_path(fixture.store, CID)
    with sqlite3.connect(cache) as connection:
        baseline = (
            connection.execute("SELECT * FROM refs ORDER BY id").fetchall(),
            connection.execute(
                "SELECT * FROM statement_members ORDER BY reference_id"
            ).fetchall(),
            connection.execute("SELECT * FROM statement_groups ORDER BY id").fetchall(),
        )
        connection.execute(
            "UPDATE info SET value='force-compatible-replay' WHERE key='fingerprint'"
        )
    replay, replay_receipts = _read(fixture)
    assert [item["id"] for item in replay["items"]] == [
        item["id"] for item in page["items"]
    ]
    assert {item["id"] for item in replay_receipts} == {item["id"] for item in receipts}
    with sqlite3.connect(cache) as connection:
        assert baseline == (
            connection.execute("SELECT * FROM refs ORDER BY id").fetchall(),
            connection.execute(
                "SELECT * FROM statement_members ORDER BY reference_id"
            ).fetchall(),
            connection.execute("SELECT * FROM statement_groups ORDER BY id").fetchall(),
        )
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


@pytest.mark.parametrize(
    "damage",
    [
        "missing_html",
        "missing_native",
        "different_forwarder",
        "different_direction",
        "different_group",
        "different_snapshot",
        "same_native_server",
        "same_native_local",
        "wrong_native_table",
        "html_clock",
        "html_forwarder",
        "html_direction",
        "same_html_local",
        "ambiguous_parent_clocks",
        "child_body",
        "child_clock",
        "child_author",
        "child_server",
        "child_chat",
        "decimal",
        "duplicate_ids",
        "partial",
        "nested",
        "resource",
        "omitted",
        "html_timezone",
    ],
)
def test_short_packets_need_the_whole_unique_paired_source_proof(
    paired_two_item_store, damage
):
    fixture = paired_two_item_store
    if damage in {"missing_html", "missing_native"}:
        identifier = (
            fixture.html_parents[-1]
            if damage == "missing_html"
            else fixture.native_parents[-1]
        )
        with sqlite3.connect(fixture.history) as connection:
            connection.execute("DELETE FROM messages WHERE id=?", (identifier,))
    else:
        identifier = (
            fixture.native_parents[-1]
            if damage
            in {
                "different_forwarder",
                "different_direction",
                "different_group",
                "different_snapshot",
                "same_native_server",
                "same_native_local",
                "wrong_native_table",
                "ambiguous_parent_clocks",
            }
            else fixture.html_parents[-1]
        )

        def change(row, proof):
            if damage == "different_forwarder":
                row["sender_id"] = "wxid_other_forwarder"
            elif damage == "different_direction":
                row.update(sender_id=fixture.owner, direction="sent")
            elif damage == "different_group":
                row["conversation_id"] = fixture.receiving_contexts[-1]
            elif damage == "different_snapshot":
                with sqlite3.connect(fixture.history) as connection:
                    connection.row_factory = sqlite3.Row
                    snapshot = dict(
                        connection.execute(
                            "SELECT * FROM source_snapshots WHERE id=?",
                            (proof["source_snapshot"],),
                        ).fetchone()
                    )
                    snapshot["id"] += 100
                    original._insert(connection, "source_snapshots", snapshot)
                proof["source_snapshot"] = snapshot["id"]
            elif damage in {
                "same_native_server",
                "same_native_local",
                "ambiguous_parent_clocks",
            }:
                with sqlite3.connect(fixture.history) as connection:
                    first = connection.execute(
                        "SELECT timestamp_value,provenance FROM messages WHERE id=?",
                        (fixture.native_parents[0],),
                    ).fetchone()
                other = json.loads(first[1])
                if damage == "same_native_server":
                    proof["server_id"] = other["server_id"]
                elif damage == "same_native_local":
                    proof["locator"]["local_id"] = other["locator"]["local_id"]
                else:
                    row["timestamp_value"] = first[0]
                    proof["source_time"] = first[0]
                    proof["unix_timestamp"] = int(
                        datetime.fromisoformat(first[0]).timestamp()
                    )
            elif damage == "wrong_native_table":
                proof["locator"]["table"] = "Chat_wrong_group"
            elif damage == "html_clock":
                row["timestamp_value"] = (
                    datetime.fromisoformat(row["timestamp_value"])
                    + timedelta(seconds=1)
                ).isoformat()
            elif damage == "html_timezone":
                row["timestamp_value"] = row["timestamp_value"] + "+00:00"
            elif damage == "html_forwarder":
                proof["sender_native_id"] = "wxid_other_html_forwarder"
            elif damage == "html_direction":
                row["direction"] = "sent"
                proof["sender_native_id"] = fixture.owner
            elif damage == "same_html_local":
                proof["locator"]["export_msgid"] = "413"
                for part in proof["forwarded_components"]:
                    part["export_msgid"] = (
                        "413_" + part["export_msgid"].rsplit("_", 1)[-1]
                    )
            else:
                part = proof["forwarded_components"][0]
                if damage == "child_body":
                    part["text"] += " conflicting text"
                elif damage == "child_clock":
                    part["raw_timestamp"] = "2020-08-12 13:46:59"
                elif damage == "child_author":
                    part.update(
                        sender_native_id="wxid_other_child_author",
                        avatar_path="Portrait/wxid_other_child_author.jpg",
                    )
                elif damage == "child_server":
                    part["source_ids"] = {"svrid": "7999999999999999991"}
                    proof["forwarded_components"][1]["source_ids"] = {
                        "svrid": "7999999999999999992"
                    }
                elif damage == "child_chat":
                    part.update(
                        source_chat_id="one@chatroom",
                        original_chat_id="another@chatroom",
                    )
                elif damage == "decimal":
                    for number, child in enumerate(proof["forwarded_components"]):
                        child["export_msgid"] = "414_" + str(number)
                elif damage == "duplicate_ids":
                    proof["forwarded_components"][1]["export_msgid"] = part[
                        "export_msgid"
                    ]
                elif damage == "partial":
                    proof["forwarded_components"] = [part]
                elif damage == "nested":
                    part["forwarded_components"] = [deepcopy(part)]
                elif damage == "resource":
                    part.update(
                        text_kind="resource_placeholder",
                        type="3",
                        resource_reference={"kind": "image"},
                    )
                else:
                    proof["omitted_forwarded_items"] = 1

        if damage == "child_server":
            _mutate(
                fixture,
                fixture.html_parents[0],
                lambda _row, proof: proof["forwarded_components"][0].update(
                    source_ids={"svrid": "7999999999999999992"}
                ),
            )
        _mutate(fixture, identifier, change)
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    page, receipts = _read(fixture)
    assert page["total_count"] == page["observation_count"] == len(receipts)
    assert all(item["observation_count"] == 1 for item in page["items"])
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def test_an_extra_unmatched_html_copy_blocks_the_whole_short_cohort(
    paired_two_item_store,
):
    fixture = paired_two_item_store
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        row = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (fixture.html_parents[0],)
            ).fetchone()
        )
        proof = json.loads(row["provenance"])
        proof["locator"]["export_msgid"] = "415"
        proof["record_id"] = _identifier(
            "html-text-v1",
            proof["locator"]["archive_sha256"],
            original._json(proof["locator"]),
        )
        for part in proof["forwarded_components"]:
            part["export_msgid"] = "415_" + part["export_msgid"].rsplit("_", 1)[-1]
        row["id"] = _identifier(fixture.html_source, proof["record_id"])
        row["timestamp_value"] = (
            datetime.fromisoformat(row["timestamp_value"]) + timedelta(seconds=1)
        ).isoformat()
        row["ordinal"] += 20
        original._insert(connection, "messages", row)
        _write_parent(connection, row, proof)
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    page, receipts = _read(fixture)
    assert page["total_count"] == page["observation_count"] == len(receipts) == 10
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def test_a_different_verified_html_owner_cannot_corroborate_short_native_copies(
    paired_two_item_store,
):
    fixture = paired_two_item_store
    owner = fixture.html_owner
    with sqlite3.connect(fixture.history) as connection:
        proof = json.loads(
            connection.execute(
                "SELECT provenance FROM messages WHERE id=?",
                (fixture.html_parents[0],),
            ).fetchone()[0]
        )
        key = proof["source_snapshot"]
        metadata = json.loads(
            connection.execute(
                "SELECT metadata FROM source_snapshots WHERE id=?", (key,)
            ).fetchone()[0]
        )
        metadata.update(
            source_account_id=owner,
            owner_evidence={
                "native_username": owner,
                "native_contact_id": canonical_contact_id(
                    owner, authority="native_account_owner"
                ),
            },
        )
        connection.execute(
            "UPDATE source_snapshots SET metadata=? WHERE id=?",
            (json.dumps(metadata), key),
        )
        connection.execute(
            "UPDATE partitions SET account_id=?,account_key=? WHERE id=?",
            (owner, "account:" + owner, fixture.html_source),
        )
        connection.execute(
            "UPDATE conversations SET account_key=? WHERE partition_id=?",
            ("account:" + owner, fixture.html_source),
        )
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    page, receipts = _read(fixture)
    assert page["total_count"] == page["observation_count"] == len(receipts) == 8
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def test_short_packet_copy_does_not_override_a_whole_group_original_conflict(
    paired_two_item_store,
    monkeypatch,
):
    fixture = paired_two_item_store
    originals = []
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        template = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (fixture.native_parents[0],)
            ).fetchone()
        )
        template_proof = json.loads(template["provenance"])
        part = template_proof["text_components"][0]
        stamp = datetime.fromisoformat(part["source_timestamp"]).replace(
            tzinfo=timezone(timedelta(hours=8))
        )
        for number in range(2):
            record = "short-cohort-conflicting-original-" + str(number)
            row = {
                **template,
                "id": _identifier(fixture.native_source, record),
                "sender_id": identified.TARGET,
                "ordinal": 200 + number,
                "text": part["text"],
                "search_text": part["text"].casefold(),
                "timestamp_value": stamp.isoformat(),
                "time_sort": stamp.timestamp(),
            }
            proof = {
                key: deepcopy(value)
                for key, value in template_proof.items()
                if key not in {"text_components", "appmsg_subtype"}
            }
            proof.update(
                record_id=record,
                server_id=str(7999999999999999990 + number),
                message_type=1,
                text_extraction_kind="plain_text",
                source_time=stamp.isoformat(),
                unix_timestamp=int(stamp.timestamp()),
            )
            row["provenance"] = json.dumps(proof)
            original._insert(connection, "messages", row)
            originals.append(row["id"])
    with monkeypatch.context() as context:
        context.setattr(
            analysis, "link_identified_packet_copies", lambda *_args, **_kwargs: None
        )
        page, _ = _read(fixture)
    assert page["total_count"] == page["observation_count"] == 10
    cache = analysis.contact_analysis_cache_path(fixture.store, CID)
    with sqlite3.connect(cache) as connection:
        for parent, primary in zip(fixture.native_parents, originals):
            reference = parent + "_forwarded_1"
            winner = connection.execute(
                "SELECT statement_id FROM statement_members WHERE reference_id=?",
                (reference,),
            ).fetchone()[0]
            loser = connection.execute(
                "SELECT statement_id FROM statement_members WHERE reference_id=?",
                (primary,),
            ).fetchone()[0]
            connection.execute(
                "UPDATE statement_members SET statement_id=? WHERE statement_id=?",
                (winner, loser),
            )
            connection.execute("DELETE FROM statement_groups WHERE id=?", (loser,))
            connection.execute(
                "UPDATE statement_groups SET reference_id=? WHERE id=?",
                (primary, winner),
            )
        baseline = connection.execute(
            "SELECT * FROM statement_members ORDER BY reference_id"
        ).fetchall()
        connection.execute(
            "UPDATE info SET value='force-whole-group-replay' WHERE key='fingerprint'"
        )
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    page, receipts = _read(fixture)
    assert page["total_count"] == 8 and page["observation_count"] == len(receipts) == 10
    with sqlite3.connect(cache) as connection:
        assert (
            baseline
            == connection.execute(
                "SELECT * FROM statement_members ORDER BY reference_id"
            ).fetchall()
        )
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())
