"""Only exact, independently identified native runs justify derived retirement.

Code version: v1.2.1-codex.0
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.core.wechat_archive import _SCHEMA, wechat_archive_path
from app.core.wechat_contacts import (
    canonical_contact_id,
    contact_registry_path,
    initialize_contact_registry,
)
from app.core import wechat_derived_redundancy as redundancy
from app.core.wechat_derived_redundancy import (
    inventory_derived_redundancy,
    message_row_sha256,
    plan_derived_redundancy,
)


ROOM = "123456789@chatroom"
AUTHORS = ("wxid_synthetic_a", "wxid_synthetic_b")
CONTACTS = tuple(canonical_contact_id(author, authority="native_contact_username") for author in AUTHORS)


@pytest.fixture
def redundancy_store(tmp_path):
    store = tmp_path / "store"
    history = wechat_archive_path(store)
    history.parent.mkdir(parents=True)
    registry = contact_registry_path(store)
    native_ids, derived_ids = [], []
    owners = ("wxid_synthetic_iphone", "wxid_synthetic_ipad")
    sources = ["ios-" + hashlib.md5(owner.encode()).hexdigest() for owner in owners]
    sources.append("xlsx-synthetic")
    with closing(sqlite3.connect(history)) as connection, connection:
        connection.executescript(_SCHEMA)
        connection.execute("CREATE TABLE source_snapshots(id INTEGER PRIMARY KEY,snapshot_id TEXT,source_id TEXT,metadata TEXT)")
        for position, source in enumerate(sources):
            owner = owners[position] if position < 2 else None
            label = "iPhone archive" if position == 0 else "iPad archive" if position == 1 else "Excel export"
            account = "account:" + owner if owner else "source:" + source
            connection.execute(
                "INSERT INTO partitions VALUES(?,?,?,?,?,?,?,?)",
                (source, account, owner, label, "Verified archive ID" if owner else "Source account unconfirmed", label, "synthetic", ""),
            )
            connection.execute(
                "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (source + "-room", source, account, ROOM, ROOM, "group" if owner else "unclassified", 3, 0, 0, "", "", ""),
            )
            if owner:
                connection.execute(
                    "INSERT INTO source_snapshots VALUES(?,?,?,?)",
                    (position + 1, "synthetic-snapshot-" + str(position), source, json.dumps({
                        "source_id": source, "source_account_id": owner,
                        "account_hash_verified": True, "account_hash": hashlib.md5(owner.encode()).hexdigest(),
                        "source_label": label,
                    })),
                )
            for index in range(3):
                identifier = hashlib.sha256((source + str(index)).encode()).hexdigest()
                stamp = datetime.fromisoformat("2021-12-18T21:16:23+08:00") + timedelta(seconds=index)
                body = "Synthetic preserved body " + str(index)
                author_index = index % 2
                proof = {"record_id": "row-" + str(index), "locator": {"local_id": index + 1}}
                if owner:
                    proof.update(source_snapshot=position + 1, text_extraction_kind="plain_text", message_type=1, server_id=str(7000000000000000000 + index), unix_timestamp=int(stamp.timestamp()), source_time=None)
                else:
                    proof["source_time"] = stamp.replace(tzinfo=None).isoformat()
                connection.execute(
                    "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (identifier, source + "-room", index + 1,
                     AUTHORS[author_index] if owner else "legacy-" + str(author_index),
                     "Synthetic speaker", "received" if owner else "unknown", body,
                     stamp.timestamp(), stamp.isoformat() if owner else stamp.replace(tzinfo=None).isoformat(),
                     "Synthetic time", "Native Unix timestamp" if owner else "Source clock; timezone unknown",
                     json.dumps(proof), "[]", body.casefold()),
                )
                (native_ids if owner else derived_ids).append(identifier)
    with closing(sqlite3.connect(registry)) as connection, connection:
        initialize_contact_registry(connection)
        for cid, author in zip(CONTACTS, AUTHORS, strict=True):
            connection.execute("INSERT INTO contacts VALUES(?,?,?,?,?,?)", (cid, "wechat", "", author, "wechat_account", "verified"))
        for position, source in enumerate(sources):
            for index, cid in enumerate(CONTACTS):
                connection.execute(
                    "INSERT INTO sender_bindings VALUES(?,?,?,?,?,?,?)",
                    (source, AUTHORS[index] if position < 2 else "legacy-" + str(index), "", "", cid,
                     "native_contact_username" if position < 2 else "corroborated_export_username", "{}"),
                )
    return SimpleNamespace(store=store, history=history, registry=registry, sources=sources, native_ids=native_ids, derived_ids=derived_ids)


def _change_proof(fixture, identifier, change):
    with sqlite3.connect(fixture.history) as connection:
        proof = json.loads(connection.execute("SELECT provenance FROM messages WHERE id=?", (identifier,)).fetchone()[0])
        change(proof)
        connection.execute("UPDATE messages SET provenance=? WHERE id=?", (json.dumps(proof), identifier))


def test_verified_three_anchor_run_prefers_ipad_and_preserves_every_input(redundancy_store):
    fixture = redundancy_store
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    plan = plan_derived_redundancy(fixture.store)
    assert len(plan["matches"]) == 3
    assert {row["derived_id"] for row in plan["matches"]} == set(fixture.derived_ids)
    assert {row["native_id"] for row in plan["matches"]} == set(fixture.native_ids[3:])
    for match in plan["matches"]:
        evidence = match["evidence"]
        assert evidence["anchor_count"] == 3
        assert evidence["author_count"] == 2
        assert evidence["derived_timezone_unknown"]
        assert evidence["derived_owner_unconfirmed"]
        assert evidence["room_id"] == ROOM
        with sqlite3.connect(fixture.history) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM messages WHERE id=?", (match["derived_id"],)).fetchone()
            assert match["derived_sha256"] == message_row_sha256(row)
    assert "Synthetic preserved body" not in json.dumps(plan)
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


@pytest.mark.parametrize("failure", ["body", "wrong-author", "minute", "timezone", "resource", "quote", "ambiguous-native", "ambiguous-derived", "no-native-clock"])
def test_incomplete_or_conflicting_evidence_cannot_retire_rows(redundancy_store, failure):
    fixture = redundancy_store
    target = fixture.derived_ids[0]
    if failure == "body":
        with sqlite3.connect(fixture.history) as connection:
            connection.execute("UPDATE messages SET text='Distinct source body' WHERE id=?", (target,))
    elif failure == "wrong-author":
        with sqlite3.connect(fixture.registry) as connection:
            connection.execute("UPDATE sender_bindings SET contact_id=? WHERE source_id=? AND sender_id='legacy-0'", (CONTACTS[1], fixture.sources[2]))
    elif failure == "minute":
        _change_proof(fixture, target, lambda proof: proof.update(source_time="2021-12-18T21:16"))
    elif failure == "timezone":
        with sqlite3.connect(fixture.history) as connection:
            for identifier in fixture.derived_ids:
                raw = connection.execute("SELECT provenance,timestamp_value FROM messages WHERE id=?", (identifier,)).fetchone()
                proof = json.loads(raw[0])
                proof["source_time"] = raw[1] + "+00:00"
                connection.execute("UPDATE messages SET provenance=?,timestamp_value=? WHERE id=?", (json.dumps(proof), proof["source_time"], identifier))
    elif failure == "resource":
        _change_proof(fixture, target, lambda proof: proof.update(resource_reference={"kind": "image"}))
    elif failure == "quote":
        _change_proof(fixture, target, lambda proof: proof.update(quoted_components=[{"text": "Quoted text"}]))
    elif failure in {"ambiguous-native", "ambiguous-derived"}:
        original = fixture.native_ids[0] if failure == "ambiguous-native" else target
        with sqlite3.connect(fixture.history) as connection:
            connection.row_factory = sqlite3.Row
            row = dict(connection.execute("SELECT * FROM messages WHERE id=?", (original,)).fetchone())
            row["id"] = "f" * 64
            row["ordinal"] = 7
            if failure == "ambiguous-native":
                proof = json.loads(row["provenance"])
                proof["server_id"] = "7000000000000000099"
                row["provenance"] = json.dumps(proof)
            connection.execute("INSERT INTO messages VALUES(" + ",".join("?" for _ in row) + ")", tuple(row.values()))
    else:
        for identifier in (fixture.native_ids[0], fixture.native_ids[3]):
            _change_proof(fixture, identifier, lambda proof: proof.pop("unix_timestamp"))
    assert plan_derived_redundancy(fixture.store)["matches"] == []


@pytest.mark.parametrize("field,value", [("peer_id", "987654321@chatroom"), ("peer_id", "Human group title")])
def test_group_identity_is_literal_and_never_inferred(redundancy_store, field, value):
    fixture = redundancy_store
    with sqlite3.connect(fixture.history) as connection:
        connection.execute("UPDATE conversations SET peer_id=?,title=? WHERE partition_id=?", (value, value, fixture.sources[2]))
    assert plan_derived_redundancy(fixture.store)["matches"] == []


def test_selected_source_and_budgets_are_explicit_and_fail_closed(redundancy_store):
    fixture = redundancy_store
    with pytest.raises(ValueError, match="derived_rows budget"):
        plan_derived_redundancy(fixture.store, max_derived_rows=2)
    with pytest.raises(ValueError, match="native_rows budget"):
        plan_derived_redundancy(fixture.store, max_native_rows=2)
    with pytest.raises(ValueError, match="Excel or HTML"):
        plan_derived_redundancy(fixture.store, source_ids=[fixture.sources[0]])
    assert plan_derived_redundancy(fixture.store, source_ids=[])["matches"] == []


def test_contact_scope_does_not_use_nicknames_or_unverified_people(redundancy_store):
    fixture = redundancy_store
    assert plan_derived_redundancy(fixture.store, contact_id=CONTACTS[0])["matches"] == []
    with pytest.raises(ValueError, match="canonical verified"):
        plan_derived_redundancy(fixture.store, contact_id="Synthetic speaker")
    with pytest.raises(ValueError, match="exact native identity"):
        plan_derived_redundancy(fixture.store, contact_id="wechat:contact:" + "0" * 64)


def test_three_same_author_anchors_can_be_scoped_without_assuming_owner(redundancy_store):
    fixture = redundancy_store
    with sqlite3.connect(fixture.history) as connection:
        connection.execute("UPDATE messages SET sender_id=? WHERE sender_id=?", (AUTHORS[0], AUTHORS[1]))
        connection.execute("UPDATE messages SET sender_id='legacy-0' WHERE sender_id='legacy-1'")
    matches = plan_derived_redundancy(fixture.store, contact_id=CONTACTS[0])["matches"]
    assert len(matches) == 3
    assert all(match["evidence"]["author_count"] == 1 for match in matches)
    assert all(match["evidence"]["derived_owner_unconfirmed"] for match in matches)


def test_unique_derived_content_survives_a_proven_matching_run(redundancy_store):
    fixture = redundancy_store
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        row = dict(connection.execute("SELECT * FROM messages WHERE id=?", (fixture.derived_ids[0],)).fetchone())
        row.update(id="e" * 64, ordinal=4, text="Unique original information")
        connection.execute("INSERT INTO messages VALUES(" + ",".join("?" for _ in row) + ")", tuple(row.values()))
    plan = plan_derived_redundancy(fixture.store)
    assert len(plan["matches"]) == 3
    assert all(match["derived_id"] != "e" * 64 for match in plan["matches"])


@pytest.mark.parametrize("field,value", [("account_hash_verified", False), ("source_account_id", "wxid_wrong_owner"), ("account_hash", "0" * 32)])
def test_native_snapshot_owner_must_be_independently_verified(redundancy_store, field, value):
    fixture = redundancy_store
    with sqlite3.connect(fixture.history) as connection:
        for identifier, raw in connection.execute("SELECT id,metadata FROM source_snapshots").fetchall():
            metadata = json.loads(raw)
            metadata[field] = value
            connection.execute("UPDATE source_snapshots SET metadata=? WHERE id=?", (json.dumps(metadata), identifier))
    assert plan_derived_redundancy(fixture.store)["matches"] == []


def test_unverified_html_group_without_a_frame_is_unmatched(redundancy_store):
    fixture = redundancy_store
    old, source = fixture.sources[2], "html-synthetic"
    with sqlite3.connect(fixture.history) as connection:
        connection.execute("UPDATE partitions SET id=?,source_label='HTML export' WHERE id=?", (source, old))
        connection.execute("UPDATE conversations SET partition_id=?,kind='group' WHERE partition_id=?", (source, old))
    with sqlite3.connect(fixture.registry) as connection:
        connection.execute("UPDATE sender_bindings SET source_id=? WHERE source_id=?", (source, old))
    assert plan_derived_redundancy(fixture.store, source_ids=[source])["matches"] == []


def test_owner_verified_html_uses_the_same_exact_group_anchors(redundancy_store):
    fixture = redundancy_store
    old, source, owner = fixture.sources[2], "html-synthetic", "wxid_synthetic_iphone"
    with sqlite3.connect(fixture.history) as connection:
        connection.execute(
            "UPDATE partitions SET id=?,source_label='HTML export',account_id=?,account_key=?,identity_status='Corroborated export ID' WHERE id=?",
            (source, owner, "account:" + owner, old),
        )
        connection.execute("UPDATE conversations SET partition_id=?,kind='group',account_key=? WHERE partition_id=?", (source, "account:" + owner, old))
        connection.execute("INSERT INTO source_snapshots VALUES(?,?,?,?)", (3, "synthetic-html", source, json.dumps({
            "source_id": source, "source_account_id": owner, "owner_verified": True,
            "owner_evidence": {"native_username": owner},
        })))
        for identifier in fixture.derived_ids:
            proof = json.loads(connection.execute("SELECT provenance FROM messages WHERE id=?", (identifier,)).fetchone()[0])
            proof["source_snapshot"] = 3
            connection.execute("UPDATE messages SET provenance=? WHERE id=?", (json.dumps(proof), identifier))
    with sqlite3.connect(fixture.registry) as connection:
        connection.execute("UPDATE sender_bindings SET source_id=? WHERE source_id=?", (source, old))
    matches = plan_derived_redundancy(fixture.store, source_ids=[source])["matches"]
    assert len(matches) == 3
    assert all(not match["evidence"]["derived_owner_unconfirmed"] for match in matches)


@pytest.mark.parametrize("message_type", [3, 47, 49, True])
def test_explicit_export_resource_types_cannot_be_retired_as_literal_text(
    redundancy_store, message_type
):
    fixture = redundancy_store
    for identifier in fixture.derived_ids:
        _change_proof(
            fixture, identifier,
            lambda proof: proof["locator"].update(export_msgtype=message_type),
        )
    assert plan_derived_redundancy(fixture.store)["matches"] == []


def test_complete_conversation_scope_does_not_scan_an_unselected_source_thread(redundancy_store):
    fixture = redundancy_store
    selected = fixture.sources[2] + "-room"
    with sqlite3.connect(fixture.history) as connection:
        connection.execute(
            "INSERT INTO conversations SELECT ?,partition_id,account_key,peer_id,title,kind,"
            "1,first_sort,last_sort,first_time,last_time,latest_text FROM conversations WHERE id=?",
            ("outside-thread", selected),
        )
        connection.execute(
            "INSERT INTO messages SELECT ?,?,ordinal,sender_id,sender_label,direction,text,"
            "time_sort,timestamp_value,timestamp_label,time_basis,provenance,quality_flags,search_text "
            "FROM messages WHERE id=?",
            ("f" * 64, "outside-thread", fixture.derived_ids[0]),
        )
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    with pytest.raises(ValueError, match="derived_rows budget"):
        plan_derived_redundancy(fixture.store, max_derived_rows=3)
    result = plan_derived_redundancy(
        fixture.store, conversation_ids=[selected], max_derived_rows=3,
    )
    assert len(result["matches"]) == 3
    assert result["counts"]["derived_rows"] == 3
    assert result["scope"]["conversation_ids"] == [selected]
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


@pytest.mark.parametrize("selection", ["not-a-list", [1], ["missing"], ["native"], ["same", "same"]])
def test_unknown_native_and_duplicate_conversation_selectors_fail_closed(redundancy_store, selection):
    fixture = redundancy_store
    if selection == ["native"]:
        selection = [fixture.sources[0] + "-room"]
    elif selection == ["same", "same"]:
        selection = [fixture.sources[2] + "-room"] * 2
    with pytest.raises(ValueError, match="conversation|Conversation"):
        plan_derived_redundancy(fixture.store, conversation_ids=selection)


def test_conversation_selector_cannot_cross_selected_source_boundaries(redundancy_store):
    fixture = redundancy_store
    with pytest.raises(ValueError, match="selected derived sources"):
        plan_derived_redundancy(
            fixture.store, source_ids=[], conversation_ids=[fixture.sources[2] + "-room"],
        )
    assert plan_derived_redundancy(fixture.store, conversation_ids=[])["matches"] == []


def test_metadata_inventory_never_reads_message_bodies(redundancy_store, monkeypatch):
    fixture = redundancy_store
    original = redundancy._readonly

    def metadata_only(path):
        connection = original(path)

        def authorize(action, table, column, database, origin):
            if action == sqlite3.SQLITE_READ and table in {"messages", "message_storage", "shared_message_bodies"}:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection

    monkeypatch.setattr(redundancy, "_readonly", metadata_only)
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    result = inventory_derived_redundancy(fixture.store)
    assert result["counts"]["messages"] == 3
    assert result["counts"]["conversations"] == 1
    assert result["conversations"][0]["estimated_native_rows"] == 6
    assert result["conversations"][0]["proof_family"] == "group_candidate_requires_message_proof"
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def test_unconfirmed_private_inventory_stays_separate_and_never_reclassified(redundancy_store):
    fixture = redundancy_store
    with sqlite3.connect(fixture.history) as connection:
        connection.execute(
            "UPDATE conversations SET kind='direct',peer_id=? WHERE partition_id=?",
            (AUTHORS[0], fixture.sources[2]),
        )
    result = inventory_derived_redundancy(fixture.store)
    assert result["conversations"][0]["kind"] == "direct"
    assert result["counts"]["private_participant_identity_missing_messages"] == 3
    assert plan_derived_redundancy(fixture.store)["matches"] == []


@pytest.fixture
def private_redundancy_store(redundancy_store):
    fixture = redundancy_store
    parties = ("wxid_synthetic_iphone", "wxid_synthetic_ipad")
    source, old = "html-synthetic-private", fixture.sources[2]
    with sqlite3.connect(fixture.history) as connection:
        connection.execute(
            "UPDATE partitions SET id=?,account_id=?,account_key=?,"
            "identity_status='Corroborated export ID',source_label='HTML export' WHERE id=?",
            (source, parties[0], "account:" + parties[0], old),
        )
        connection.execute(
            "UPDATE conversations SET partition_id=?,account_key=? WHERE partition_id=?",
            (source, "account:" + parties[0], old),
        )
        connection.execute(
            "INSERT INTO source_snapshots VALUES(?,?,?,?)",
            (3, "synthetic-private-html", source, json.dumps({
                "source_id": source, "source_account_id": parties[0], "owner_verified": True,
                "owner_evidence": {"native_username": parties[0]},
            })),
        )
        for position, partition in enumerate((fixture.sources[0], fixture.sources[1], source)):
            owner_index = 1 if position == 1 else 0
            connection.execute(
                "UPDATE conversations SET kind='direct',peer_id=?,title='Readable title' WHERE partition_id=?",
                (parties[1-owner_index], partition),
            )
            identifiers = fixture.derived_ids if position == 2 else fixture.native_ids[position*3:position*3+3]
            for index, identifier in enumerate(identifiers):
                author_index = index % 2
                proof = json.loads(connection.execute("SELECT provenance FROM messages WHERE id=?", (identifier,)).fetchone()[0])
                if position == 2:
                    proof.update(source_snapshot=3)
                    proof["locator"]["export_msgtype"] = "1"
                connection.execute(
                    "UPDATE messages SET sender_id=?,direction=?,provenance=? WHERE id=?",
                    ("legacy-" + str(author_index) if position == 2 else parties[author_index],
                     "sent" if author_index == owner_index else "received", json.dumps(proof), identifier),
                )
    with sqlite3.connect(fixture.registry) as connection:
        connection.execute("DELETE FROM sender_bindings")
        connection.execute("DELETE FROM contacts")
        for index, party in enumerate(parties):
            contact = canonical_contact_id(party, authority="native_contact_username")
            connection.execute("INSERT INTO contacts VALUES(?,?,?,?,?,?)", (contact, "wechat", "", party, "wechat_account", "verified"))
            for position, partition in enumerate((fixture.sources[0], fixture.sources[1], source)):
                connection.execute(
                    "INSERT INTO sender_bindings VALUES(?,?,?,?,?,?,?)",
                    (partition, "legacy-" + str(index) if position == 2 else party, "", "", contact,
                     "corroborated_export_username" if position == 2 else "native_contact_username", "{}"),
                )
    fixture.sources[2] = source
    fixture.private_parties = parties
    fixture.derived_conversation_id = old + "-room"
    return fixture


def test_verified_private_pair_matches_opposite_native_directions_without_merging_accounts(private_redundancy_store):
    fixture = private_redundancy_store
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    result = plan_derived_redundancy(fixture.store)
    assert len(result["matches"]) == 3, result["counts"]
    assert all(match["native_id"] in fixture.native_ids[3:] for match in result["matches"])
    for match in result["matches"]:
        proof = match["evidence"]
        assert proof["method"] == redundancy.PRIVATE_METHOD
        assert proof["chat_kind"] == "direct" and proof["room_id"] is None
        assert proof["participant_pair"] == sorted(fixture.private_parties)
        assert proof["derived_owner_id"] != proof["native_owner_id"]
        assert proof["derived_direction"] != proof["native_direction"]
        assert proof["derived_owner_unconfirmed"] is False
        assert proof["derived_timezone_unknown"] is True
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())
    inventory = inventory_derived_redundancy(fixture.store)
    assert inventory["counts"]["private_candidate_requires_message_proof_messages"] == 3
    assert inventory["conversations"][0]["estimated_native_rows"] == 6


@pytest.mark.parametrize("failure", ["owner", "snapshot-source", "peer", "html-direction", "native-direction", "author", "official", "openim", "unverified-contact", "tampered-peer-contact"])
def test_private_participant_or_direction_uncertainty_cannot_authorize_retirement(private_redundancy_store, failure):
    fixture = private_redundancy_store
    with sqlite3.connect(fixture.history) as connection:
        if failure in {"owner", "snapshot-source"}:
            row = connection.execute("SELECT metadata FROM source_snapshots WHERE id=3").fetchone()
            metadata = json.loads(row[0])
            if failure == "owner":
                metadata["owner_evidence"] = {}
            else:
                metadata["source_id"] = "html-another-source"
            connection.execute("UPDATE source_snapshots SET metadata=? WHERE id=3", (json.dumps(metadata),))
        elif failure in {"peer", "official", "openim"}:
            peer = {"peer": "wxid_unrelated", "official": "gh_public_service", "openim": "external@openim"}[failure]
            connection.execute("UPDATE conversations SET peer_id=? WHERE partition_id=?", (peer, fixture.sources[2]))
        elif failure == "html-direction":
            connection.execute("UPDATE messages SET direction='unknown' WHERE conversation_id=?", (fixture.derived_conversation_id,))
        elif failure == "native-direction":
            connection.execute("UPDATE messages SET direction='unknown' WHERE conversation_id IN (?,?)", (fixture.sources[0]+"-room", fixture.sources[1]+"-room"))
        elif failure == "author":
            connection.execute("UPDATE messages SET sender_id='legacy-1' WHERE id=?", (fixture.derived_ids[0],))
    if failure in {"unverified-contact", "tampered-peer-contact"}:
        with sqlite3.connect(fixture.registry) as connection:
            if failure == "unverified-contact":
                connection.execute("UPDATE contacts SET identity_status='unconfirmed' WHERE username=?", (fixture.private_parties[1],))
            else:
                connection.execute("UPDATE contacts SET id=? WHERE username=?", ("wechat:contact:"+"0"*64, fixture.private_parties[1]))
    assert plan_derived_redundancy(fixture.store)["matches"] == []


@pytest.mark.parametrize("peer", ["gh_synthetic_public", "filehelper", "external@openim"])
def test_known_public_service_and_openim_pairs_stay_outside_private_proof(private_redundancy_store, peer):
    fixture = private_redundancy_store
    owner = fixture.private_parties[0]
    with sqlite3.connect(fixture.history) as connection:
        connection.execute(
            "UPDATE conversations SET peer_id=? WHERE partition_id IN (?,?)",
            (peer, fixture.sources[0], fixture.sources[2]),
        )
        for identifier in fixture.native_ids[:3]:
            connection.execute("UPDATE messages SET sender_id=?,direction='sent' WHERE id=?", (owner, identifier))
        for identifier in fixture.derived_ids:
            connection.execute("UPDATE messages SET sender_id='legacy-0',direction='sent' WHERE id=?", (identifier,))
    result = inventory_derived_redundancy(fixture.store)
    assert result["conversations"][0]["unsupported_reason"] == "official_service_openim_or_unresolved_participant"
    assert plan_derived_redundancy(fixture.store)["matches"] == []


@pytest.mark.parametrize("field,value", [("message_type", {}), ("message_type", []), ("text_extraction_kind", {})])
def test_malformed_rich_type_metadata_is_preserved_instead_of_crashing_an_audit(redundancy_store, field, value):
    fixture = redundancy_store
    for identifier in fixture.derived_ids:
        _change_proof(fixture, identifier, lambda proof: proof.update({field: value}))
    assert plan_derived_redundancy(fixture.store)["matches"] == []
