"""Pinned native copies recover missing authors without nickname inference.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from app.core import wechat_forwarded_authors as authors
from app.core.wechat_archive import _identifier
from app.core.wechat_text import native_components_text
from tests import test_wechat_forwarded_original_precedence as fixtures


native_html_store = fixtures.native_html_store
statement_store = fixtures.statement_store
forwarded_origin_store = fixtures.forwarded_origin_store
CID = fixtures.CID
TARGET = fixtures.TARGET


def _rows(fixture):
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(connection.execute(
            authors._SELECT, (fixture.target_parent,)
        ).fetchone())]


def _pin(fixture):
    link = {
        "target_message_id": fixture.target_parent,
        "native_message_id": fixture.native_parent,
    }
    with sqlite3.connect(fixture.history) as connection:
        for prefix, identifier in (
            ("target", fixture.target_parent), ("native", fixture.native_parent)
        ):
            row = connection.execute(
                "SELECT text,provenance FROM messages WHERE id=?", (identifier,)
            ).fetchone()
            for field, value in zip(("text", "provenance"), row):
                link[prefix + "_" + field + "_sha256"] = hashlib.sha256(
                    value.encode()
                ).hexdigest()
    fixture.author_registry = fixture.history.parent / "forwarded_author_links.json"
    fixture.author_registry.write_text(json.dumps({"schema_version": 1, "links": [link]}))
    fixture.author_link = link
    return link


def _mutate(fixture, *, target, callback):
    identifier = fixture.target_parent if target else fixture.native_parent
    with sqlite3.connect(fixture.history) as connection:
        raw = connection.execute(
            "SELECT provenance FROM messages WHERE id=?", (identifier,)
        ).fetchone()[0]
        proof = json.loads(raw)
        callback(proof)
        text = native_components_text(proof["text_components"])
        connection.execute(
            "UPDATE messages SET text=?,search_text=?,provenance=? WHERE id=?",
            (text, text.casefold(), json.dumps(proof), identifier),
        )


@pytest.fixture
def native_author_store(forwarded_origin_store):
    fixture = forwarded_origin_store

    def flat(proof):
        proof["text_components"] = [
            part for part in proof["text_components"] if part["kind"] == "forwarded_text"
        ]
        for number, part in enumerate(proof["text_components"], 1):
            part["forwarded_item_path"] = [number]

    fixtures._update_proof(fixture, fixture.native_parent, flat)
    record = "native-missing-author-forward-envelope"
    fixture.target_parent = _identifier(fixture.native_source, record)
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        row = dict(connection.execute(
            "SELECT * FROM messages WHERE id=?", (fixture.native_parent,)
        ).fetchone())
        proof = json.loads(row["provenance"])
        proof["record_id"] = record
        proof["server_id"] = "8123456789012345679"
        for part in proof["text_components"]:
            part["source_sender_id"] = ""
            part["source_sender_label"] = "Fixture speaker"
        row.update(
            id=fixture.target_parent,
            ordinal=row["ordinal"] + 1,
            provenance=json.dumps(proof),
            text=native_components_text(proof["text_components"]),
        )
        row["search_text"] = row["text"].casefold()
        fixtures._insert(connection, "messages", row)
    _pin(fixture)
    return fixture


def test_recovers_whole_packet_authors_with_immutable_raw_sources(native_author_store):
    fixture = native_author_store
    before = fixture.history.read_bytes(), fixture.author_registry.read_bytes()
    rows = _rows(fixture)
    caller = deepcopy(rows)
    resolver = authors.WechatForwardedAuthors(fixture.store)
    result = resolver.project(rows)[fixture.target_parent]
    with sqlite3.connect(fixture.history) as connection:
        proof = json.loads(connection.execute(
            "SELECT provenance FROM messages WHERE id=?", (fixture.native_parent,)
        ).fetchone()[0])
    assert len(result) == len(proof["text_components"])
    for number, part in enumerate(proof["text_components"], 1):
        resolved = result[(number,)]
        assert resolved["sender_id"] == part["source_sender_id"]
        assert resolved["identity_basis"] == "verified_complete_native_transcript_copy"
        assert resolved["evidence"]["native_reference_id"] == (
            fixture.native_parent + "_forwarded_" + str(number)
        )
        assert resolved["evidence"]["source_data_id"] == part["source_data_id"]
        assert resolved["evidence"]["target_source_sender_id"] == ""
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        traced = []
        connection.set_trace_callback(traced.append)
        assert resolver.linked_rows(connection) == rows
        assert len(traced) == 1
        assert "WHERE m.id=" in traced[0]
    assert rows == caller
    assert before == (fixture.history.read_bytes(), fixture.author_registry.read_bytes())
    assert len(authors.registry_digest(fixture.store)) == 64


def test_known_matching_authors_stay_literal_and_other_names_receive_no_alias(native_author_store):
    fixture = native_author_store
    with sqlite3.connect(fixture.history) as connection:
        proof = json.loads(connection.execute(
            "SELECT provenance FROM messages WHERE id=?", (fixture.native_parent,)
        ).fetchone()[0])
    sender = proof["text_components"][0]["source_sender_id"]
    _mutate(fixture, target=True, callback=lambda value: value["text_components"][0].update(
        source_sender_id=sender, source_sender_label="Entirely different remark"
    ))
    _pin(fixture)
    result = authors.WechatForwardedAuthors(fixture.store).project(_rows(fixture))
    assert (1,) not in result[fixture.target_parent]
    unrelated = {**_rows(fixture)[0], "id": "e" * 64}
    assert authors.WechatForwardedAuthors(fixture.store).project([unrelated]) == {}


def test_independent_receiving_accounts_keep_their_separate_native_envelopes(native_author_store):
    fixture = native_author_store
    owner = "wxid_independent_receiver"
    account_hash = hashlib.md5(owner.encode()).hexdigest()
    source_id = "ios-" + account_hash
    record = "independent-missing-author-envelope"
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        partition = dict(connection.execute(
            "SELECT * FROM partitions WHERE id=?", (fixture.native_source,)
        ).fetchone())
        partition.update(id=source_id, account_id=owner, account_key="account:" + owner)
        fixtures._insert(connection, "partitions", partition)
        row = dict(connection.execute(
            "SELECT * FROM messages WHERE id=?", (fixture.target_parent,)
        ).fetchone())
        conversation = dict(connection.execute(
            "SELECT * FROM conversations WHERE id=?", (row["conversation_id"],)
        ).fetchone())
        conversation.update(
            id="wechat:" + _identifier(source_id, fixture.owner), partition_id=source_id,
            account_key="account:" + owner, peer_id=fixture.owner,
        )
        fixtures._insert(connection, "conversations", conversation)
        proof = json.loads(row["provenance"])
        snapshot = dict(connection.execute(
            "SELECT * FROM source_snapshots WHERE id=?", (proof["source_snapshot"],)
        ).fetchone())
        metadata = json.loads(snapshot["metadata"])
        metadata.update(
            source_id=source_id, source_account_id=owner,
            account_hash=account_hash, account_hash_verified=True,
        )
        proof["source_snapshot"] = connection.execute(
            "INSERT INTO source_snapshots(snapshot_id,source_id,metadata) VALUES(?,?,?)",
            ("independent-author-receiver", source_id, json.dumps(metadata)),
        ).lastrowid
        proof["record_id"] = record
        proof["locator"]["database_relative_path"] = (
            "Documents/" + account_hash + "/DB/" + proof["locator"]["database"]
        )
        proof["locator"]["table"] = "Chat_" + hashlib.md5(fixture.owner.encode()).hexdigest()
        identifier = _identifier(source_id, record)
        connection.execute(
            "UPDATE messages SET id=?,conversation_id=?,sender_id=?,direction='received',provenance=? WHERE id=?",
            (identifier, conversation["id"], fixture.owner, json.dumps(proof), fixture.target_parent),
        )
        fixture.target_parent = identifier
    _pin(fixture)
    result = authors.WechatForwardedAuthors(fixture.store).project(_rows(fixture))
    first = result[fixture.target_parent][(1,)]
    assert first["evidence"]["target_source_id"] == source_id
    assert first["evidence"]["native_source_id"] == fixture.native_source
    assert source_id != fixture.native_source


@pytest.mark.parametrize("fault", [
    "opaque_id", "body", "known_author", "order", "omitted", "nested", "resource",
    "ordinal_ids", "missing_clock", "conflicting_clock", "numeric_clock", "timezone", "server_id", "chat_id",
])
def test_repinned_incomplete_or_conflicting_packet_is_rejected(native_author_store, fault):
    fixture = native_author_store

    def mutate(proof):
        parts = proof["text_components"]
        if fault == "opaque_id":
            parts[0]["source_data_id"] = "a" * 32
        elif fault == "body":
            parts[0]["text"] += " changed"
        elif fault == "known_author":
            parts[0]["source_sender_id"] = "wxid_contradictory_author"
        elif fault == "order":
            parts[0], parts[1] = parts[1], parts[0]
            for number, part in enumerate(parts, 1):
                part["forwarded_item_path"] = [number]
        elif fault == "omitted":
            proof["omitted_forwarded_items"] = 1
        elif fault == "nested":
            parts[0]["text_components"] = [deepcopy(parts[1])]
        elif fault == "resource":
            parts[0].update(kind="forwarded_resource", text="", resource_kind="image", forwarded_data_type=2)
        elif fault == "ordinal_ids":
            for number, part in enumerate(parts):
                part["source_data_id"] = str(number)
        elif fault == "missing_clock":
            parts[0].update(source_create_time="0", source_timestamp="1970-01-01 08:00:00")
        elif fault == "conflicting_clock":
            parts[0]["source_timestamp"] = "2020-01-01 12:00:00"
        elif fault == "numeric_clock":
            parts[0]["source_create_time"] = str(int(parts[0]["source_create_time"]) + 1)
        elif fault == "timezone":
            parts[0]["source_timestamp"] = "2020-08-12T13:46:00+09:00"
        elif fault == "server_id":
            parts[0]["source_ids"] = {"svrid": "9234567890123456789"}
        elif fault == "chat_id":
            parts[0]["source_chat_id"] = "999999@chatroom"

    if fault in {"server_id", "chat_id"}:
        _mutate(fixture, target=False, callback=lambda proof: proof["text_components"][0].update(
            source_ids={"svrid": "8234567890123456789"}, source_chat_id="888888@chatroom"
        ))
    _mutate(fixture, target=True, callback=mutate)
    _pin(fixture)
    assert authors.WechatForwardedAuthors(fixture.store).project(_rows(fixture)) == {}


@pytest.mark.parametrize("fault", ["unknown_source_author", "single_source_author", "record", "locator", "source_snapshot"])
def test_invalid_donor_identity_is_rejected_even_when_repinned(native_author_store, fault):
    fixture = native_author_store

    def mutate(proof):
        if fault == "unknown_source_author":
            proof["text_components"][0]["source_sender_id"] = ""
        elif fault == "single_source_author":
            for part in proof["text_components"]:
                part["source_sender_id"] = TARGET
        elif fault == "record":
            proof["record_id"] = "contradictory-source-record"
        elif fault == "locator":
            proof["locator"]["table"] = "Chat_" + "f" * 32
        else:
            proof["source_snapshot"] = 999999

    _mutate(fixture, target=False, callback=mutate)
    _pin(fixture)
    assert authors.WechatForwardedAuthors(fixture.store).project(_rows(fixture)) == {}


@pytest.mark.parametrize("fault", ["hash", "registry_removed", "duplicate", "malformed", "symlink", "journal"])
def test_evidence_revocation_and_source_boundaries_fail_closed(native_author_store, fault):
    fixture = native_author_store
    rows = _rows(fixture)
    if fault == "hash":
        _mutate(fixture, target=False, callback=lambda proof: proof["text_components"][0].update(
            source_sender_label="Changed source bytes"
        ))
    elif fault == "registry_removed":
        fixture.author_registry.unlink()
    elif fault == "duplicate":
        fixture.author_registry.write_text(json.dumps({"schema_version": 1, "links": [fixture.author_link] * 2}))
    elif fault == "malformed":
        fixture.author_registry.write_text("{}")
    elif fault == "symlink":
        original = fixture.author_registry.with_name("original-author-links.json")
        fixture.author_registry.rename(original)
        fixture.author_registry.symlink_to(original)
    else:
        fixture.history.with_name(fixture.history.name + "-journal").write_bytes(b"active")
    assert authors.WechatForwardedAuthors(fixture.store).project(rows) == {}


def test_concurrent_source_change_discards_projection(native_author_store, monkeypatch):
    fixture = native_author_store
    original = authors._link

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        with sqlite3.connect(fixture.history) as connection:
            connection.execute("UPDATE messages SET sender_label='Changed' WHERE id=?", (fixture.native_parent,))
        return result

    monkeypatch.setattr(authors, "_link", changed)
    assert authors.WechatForwardedAuthors(fixture.store).project(_rows(fixture)) == {}


@pytest.mark.parametrize("fault", ["registry_bytes", "link_count", "page_count", "snapshot_owner", "direction", "caller_bytes"])
def test_bounded_inputs_and_envelope_claims_fail_closed(native_author_store, monkeypatch, fault):
    fixture = native_author_store
    rows = _rows(fixture)
    if fault == "registry_bytes":
        monkeypatch.setattr(authors, "MAX_REGISTRY_BYTES", 1)
    elif fault == "link_count":
        monkeypatch.setattr(authors, "MAX_LINKS", 0)
    elif fault == "page_count":
        monkeypatch.setattr(authors, "MAX_ROWS", 0)
    elif fault == "caller_bytes":
        rows[0]["text"] += " stale caller"
    elif fault == "snapshot_owner":
        with sqlite3.connect(fixture.history) as connection:
            proof = json.loads(rows[0]["provenance"])
            raw = connection.execute(
                "SELECT metadata FROM source_snapshots WHERE id=?", (proof["source_snapshot"],)
            ).fetchone()[0]
            metadata = json.loads(raw)
            metadata["source_account_id"] = "wxid_wrong_owner"
            connection.execute(
                "UPDATE source_snapshots SET metadata=? WHERE id=?",
                (json.dumps(metadata), proof["source_snapshot"]),
            )
    else:
        with sqlite3.connect(fixture.history) as connection:
            connection.execute("UPDATE messages SET direction='received' WHERE id=?", (fixture.native_parent,))
    assert authors.WechatForwardedAuthors(fixture.store).project(rows) == {}
