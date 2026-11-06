"""Pinned packet clocks recover child times while retaining independent sources.

Code version: v1.1.2-codex.0
"""

from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
import hashlib
import json
import sqlite3

import pytest

from app.core import wechat_forwarded_clocks as clocks
from app.core.wechat_archive import _identifier
from app.core.wechat_contacts import canonical_contact_id
from tests import test_wechat_forwarded_original_precedence as fixtures


native_html_store = fixtures.native_html_store
statement_store = fixtures.statement_store
forwarded_origin_store = fixtures.forwarded_origin_store
ROOM = "1234567890@chatroom"


def _rows(fixture):
    with closing(sqlite3.connect(fixture.history)) as connection, connection:
        connection.row_factory = sqlite3.Row
        return [dict(connection.execute(
            "SELECT * FROM messages WHERE id=?", (fixture.html_parent,)
        ).fetchone())]


def _pin(fixture):
    link = {"target_message_id": fixture.html_parent, "native_message_id": fixture.native_parent}
    with closing(sqlite3.connect(fixture.history)) as connection, connection:
        for prefix, identifier in (("target", fixture.html_parent), ("native", fixture.native_parent)):
            row = connection.execute("SELECT text,provenance FROM messages WHERE id=?", (identifier,)).fetchone()
            for field, value in zip(("text", "provenance"), row):
                link[prefix + "_" + field + "_sha256"] = hashlib.sha256(value.encode()).hexdigest()
    fixture.clock_registry = fixture.history.parent / "forwarded_clock_links.json"
    fixture.clock_registry.write_text(json.dumps({"schema_version": 1, "links": [link]}))
    return link


def _mutate(fixture, *, native, callback):
    fixtures._update_proof(fixture, fixture.native_parent if native else fixture.html_parent, callback)


@pytest.fixture
def pinned_clock_store(forwarded_origin_store):
    fixture = forwarded_origin_store
    with closing(sqlite3.connect(fixture.history)) as connection, connection:
        connection.row_factory = sqlite3.Row
        for identifier in (fixture.html_parent, fixture.native_parent):
            conversation = connection.execute("SELECT conversation_id FROM messages WHERE id=?", (identifier,)).fetchone()[0]
            connection.execute("UPDATE conversations SET kind='group',peer_id=? WHERE id=?", (ROOM, conversation))
        snapshot = connection.execute(
            "SELECT json_extract(provenance,'$.source_snapshot') FROM messages WHERE id=?", (fixture.html_parent,)
        ).fetchone()[0]
        metadata = json.loads(connection.execute("SELECT metadata FROM source_snapshots WHERE id=?", (snapshot,)).fetchone()[0])
        for entry in metadata["source_conversation_catalog"]:
            entry.update(index_avatar_native_id=ROOM, conversation_kind="group")
        connection.execute("UPDATE source_snapshots SET metadata=? WHERE id=?", (json.dumps(metadata), snapshot))

    def native(proof):
        proof["locator"]["table"] = "Chat_" + hashlib.md5(ROOM.encode()).hexdigest()
        for number, part in enumerate(proof["text_components"], 1):
            part["forwarded_item_path"] = [number]
            if part["kind"] == "forwarded_resource":
                part.update(forwarded_data_type=2, source_data_id=hashlib.md5(f"resource-{number}".encode()).hexdigest())
            else:
                part["source_timestamp"] = datetime.fromtimestamp(
                    int(part["source_create_time"]), UTC
                ).astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None).isoformat(" ")
                part["source_create_time"] = "0"

    def html(proof):
        for part in proof["forwarded_components"]:
            part["raw_timestamp"] = "1970-01-01 08:00:00"

    _mutate(fixture, native=True, callback=native)
    _mutate(fixture, native=False, callback=html)
    _pin(fixture)
    return fixture


def test_recovers_only_native_child_clock_and_preserves_all_source_bytes(pinned_clock_store):
    fixture = pinned_clock_store
    before = fixture.history.read_bytes(), fixture.registry.read_bytes(), fixture.clock_registry.read_bytes()
    rows = _rows(fixture)
    before_rows = deepcopy(rows)
    projected = clocks.WechatForwardedClocks(fixture.store).project(rows)[fixture.html_parent]
    assert len(projected) == 9
    first = projected[(1,)]
    assert first["timestamp_value"] == "2020-08-12T13:46:00"
    assert datetime.fromisoformat(first["timestamp_value"]).tzinfo is None
    assert first["time_basis"] == "Recovered native packet time; Source clock; timezone unknown"
    assert first["source_clock"] == "2020-08-12 13:46:00"
    assert first["proof"]["native_message_id"] == fixture.native_parent
    assert first["proof"]["native_component_path"] == ["native", 1]
    assert first["proof"]["target_source_clock"] == "1970-01-01 08:00:00"
    assert first["proof"]["source_snapshot_id"]
    assert "recovered_pinned_native_forwarded_clock" in first["quality_flags"]
    assert clocks.linked_parent_ids(fixture.store) == (fixture.html_parent,)
    assert len(clocks.registry_digest(fixture.store)) == 64
    assert rows == before_rows
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes(), fixture.clock_registry.read_bytes())


def test_allows_independent_receiving_account_and_parent_delivery_time(pinned_clock_store):
    fixture = pinned_clock_store
    owner = "wxid_independent_html_owner"
    with closing(sqlite3.connect(fixture.history)) as connection, connection:
        connection.execute("UPDATE partitions SET account_id=?,account_key=? WHERE id=?", (owner, "account:" + owner, fixture.html_source))
        connection.execute("UPDATE conversations SET account_key=? WHERE partition_id=?", ("account:" + owner, fixture.html_source))
        snapshot = connection.execute("SELECT json_extract(provenance,'$.source_snapshot') FROM messages WHERE id=?", (fixture.html_parent,)).fetchone()[0]
        metadata = json.loads(connection.execute("SELECT metadata FROM source_snapshots WHERE id=?", (snapshot,)).fetchone()[0])
        metadata.update(source_account_id=owner, owner_evidence={"native_username": owner, "native_contact_id": canonical_contact_id(owner, authority="native_account_owner")})
        connection.execute("UPDATE source_snapshots SET metadata=? WHERE id=?", (json.dumps(metadata), snapshot))
        connection.execute("UPDATE messages SET timestamp_value='2021-10-20T16:20:03',direction='received' WHERE id=?", (fixture.html_parent,))
    _pin(fixture)
    assert len(clocks.WechatForwardedClocks(fixture.store).project(_rows(fixture))[fixture.html_parent]) == 9


def test_valid_html_child_clock_remains_its_own_observation(pinned_clock_store):
    fixture = pinned_clock_store
    _mutate(fixture, native=False, callback=lambda proof: proof["forwarded_components"][0].update(raw_timestamp="2020-08-12 13:46:00"))
    _pin(fixture)
    result = clocks.WechatForwardedClocks(fixture.store).project(_rows(fixture))[fixture.html_parent]
    assert len(result) == 8
    assert (1,) not in result


def test_genuine_numeric_native_clock_retains_its_known_offset(pinned_clock_store):
    fixture = pinned_clock_store

    def numeric(proof):
        for part in proof["text_components"]:
            if part["kind"] == "forwarded_text":
                stamp = datetime.fromisoformat(part["source_timestamp"]).replace(tzinfo=timezone(timedelta(hours=8)))
                part["source_create_time"] = str(int(stamp.timestamp()))
                part["source_timestamp"] = stamp.isoformat()

    _mutate(fixture, native=True, callback=numeric)
    _pin(fixture)
    result = clocks.WechatForwardedClocks(fixture.store).project(_rows(fixture))[fixture.html_parent]
    assert result[(1,)]["timestamp_value"] == "2020-08-12T13:46:00+08:00"
    assert result[(1,)]["time_basis"] == "Recovered native packet time; UTC+08:00"


@pytest.mark.parametrize("fault", ["symlink", "journal"])
def test_archive_boundaries_reject_unpublished_or_indirect_source(pinned_clock_store, fault):
    fixture = pinned_clock_store
    rows = _rows(fixture)
    if fault == "symlink":
        original = fixture.history.with_name("original-history.sqlite3")
        fixture.history.rename(original)
        fixture.history.symlink_to(original)
    else:
        fixture.history.with_name(fixture.history.name + "-journal").write_bytes(b"active")
    assert clocks.WechatForwardedClocks(fixture.store).project(rows) == {}


def test_concurrent_source_change_discards_the_complete_projection(pinned_clock_store, monkeypatch):
    fixture = pinned_clock_store
    original = clocks._link

    def changed_after_read(*args, **kwargs):
        projected = original(*args, **kwargs)
        with closing(sqlite3.connect(fixture.history)) as connection, connection:
            connection.execute("UPDATE messages SET sender_label=sender_label || ' changed' WHERE id=?", (fixture.native_parent,))
        return projected

    monkeypatch.setattr(clocks, "_link", changed_after_read)
    assert clocks.WechatForwardedClocks(fixture.store).project(_rows(fixture)) == {}


@pytest.mark.parametrize("fault", ["id", "body", "author", "order", "extra_text", "resource", "omitted", "native_clock", "numeric_clock", "html_clock"])
def test_rejects_incomplete_or_contradictory_packet_even_when_repinned(pinned_clock_store, fault):
    fixture = pinned_clock_store

    def mutate(proof):
        parts = proof["forwarded_components"] if fault == "html_clock" else proof["text_components"]
        if fault == "id":
            parts[0]["source_data_id"] = "a" * 32
        elif fault == "body":
            parts[0]["text"] += " changed"
        elif fault == "author":
            parts[0]["source_sender_id"] = "wxid_other_author"
        elif fault == "order":
            parts[0], parts[1] = parts[1], parts[0]
        elif fault == "extra_text":
            parts.append({**parts[0], "source_data_id": "b" * 32, "forwarded_item_path": [len(parts) + 1]})
        elif fault == "resource":
            parts[3].pop("forwarded_data_type")
        elif fault == "omitted":
            proof["omitted_forwarded_items"] = 1
        elif fault == "native_clock":
            parts[0]["source_timestamp"] = "1970-01-01 08:00:00"
        elif fault == "numeric_clock":
            parts[0]["source_create_time"] = "1546300800"
        elif fault == "html_clock":
            parts[0]["raw_timestamp"] = "2020-01-01 12:34:56"

    _mutate(fixture, native=fault != "html_clock", callback=mutate)
    _pin(fixture)
    before = fixture.history.read_bytes()
    assert clocks.WechatForwardedClocks(fixture.store).project(_rows(fixture)) == {}
    assert fixture.history.read_bytes() == before


@pytest.mark.parametrize("fault", ["room", "forwarder", "locator", "record", "snapshot_owner"])
def test_rejects_wrong_source_identity_even_when_repinned(pinned_clock_store, fault):
    fixture = pinned_clock_store
    with closing(sqlite3.connect(fixture.history)) as connection, connection:
        if fault == "room":
            connection.execute("UPDATE conversations SET peer_id='999@chatroom' WHERE id=(SELECT conversation_id FROM messages WHERE id=?)", (fixture.native_parent,))
        elif fault == "forwarder":
            connection.execute("UPDATE messages SET sender_id='wxid_wrong_forwarder' WHERE id=?", (fixture.native_parent,))
        elif fault == "snapshot_owner":
            connection.execute("UPDATE source_snapshots SET metadata=json_set(metadata,'$.source_account_id','wxid_wrong_owner') WHERE id=(SELECT json_extract(provenance,'$.source_snapshot') FROM messages WHERE id=?)", (fixture.native_parent,))
    if fault in {"locator", "record"}:
        def mutate(proof):
            if fault == "locator":
                proof["locator"]["table"] = "Chat_" + "0" * 32
            else:
                proof["record_id"] += "-wrong"
        _mutate(fixture, native=True, callback=mutate)
    _pin(fixture)
    assert clocks.WechatForwardedClocks(fixture.store).project(_rows(fixture)) == {}


@pytest.mark.parametrize("side", ["target", "native", "caller"])
def test_pins_require_exact_published_bytes(pinned_clock_store, side):
    fixture = pinned_clock_store
    rows = _rows(fixture)
    if side == "caller":
        rows[0]["provenance"] += " "
    else:
        _mutate(fixture, native=side == "native", callback=lambda proof: proof.update(extra_evidence="changed"))
        rows = _rows(fixture)
    assert clocks.WechatForwardedClocks(fixture.store).project(rows) == {}


@pytest.mark.parametrize("fault", ["missing", "invalid", "duplicate", "oversized", "symlink", "journal"])
def test_registry_boundaries_are_fail_closed(pinned_clock_store, fault):
    fixture = pinned_clock_store
    path = fixture.clock_registry
    if fault == "missing":
        path.unlink()
    elif fault == "invalid":
        path.write_text("[]")
    elif fault == "duplicate":
        value = json.loads(path.read_text())
        value["links"] *= 2
        path.write_text(json.dumps(value))
    elif fault == "oversized":
        path.write_bytes(b" " * (clocks.MAX_REGISTRY_BYTES + 1))
    elif fault == "symlink":
        other = path.with_name("original-links.json")
        path.rename(other)
        path.symlink_to(other)
    else:
        path.with_name(path.name + "-journal").write_bytes(b"active")
    assert bool(clocks.registry_digest(fixture.store)) == (fault == "duplicate")
    assert clocks.linked_parent_ids(fixture.store) == ()
    assert clocks.WechatForwardedClocks(fixture.store).project(_rows(fixture)) == {}


@pytest.mark.parametrize("limit", ["MAX_ROWS", "MAX_ROW_BYTES", "MAX_SOURCE_BYTES", "MAX_COMPONENTS", "MAX_SNAPSHOT_BYTES"])
def test_runtime_budgets_fail_closed(pinned_clock_store, monkeypatch, limit):
    fixture = pinned_clock_store
    monkeypatch.setattr(clocks, limit, 0)
    assert clocks.WechatForwardedClocks(fixture.store).project(_rows(fixture)) == {}


def _links(fixture, targets):
    result = []
    with closing(sqlite3.connect(fixture.history)) as connection, connection:
        for target in targets:
            link = {"target_message_id": target, "native_message_id": fixture.native_parent}
            for prefix, identifier in (("target", target), ("native", fixture.native_parent)):
                values = connection.execute("SELECT text,provenance FROM messages WHERE id=?", (identifier,)).fetchone()
                for field, value in zip(("text", "provenance"), values):
                    link[prefix + "_" + field + "_sha256"] = hashlib.sha256(value.encode()).hexdigest()
            result.append(link)
    return result


def _add_html_target(fixture):
    with closing(sqlite3.connect(fixture.history)) as connection, connection:
        connection.row_factory = sqlite3.Row
        row = dict(connection.execute("SELECT * FROM messages WHERE id=?", (fixture.html_parent,)).fetchone())
        proof = json.loads(row["provenance"])
        proof["locator"]["export_msgid"] = "414"
        for leaf in proof["forwarded_components"]:
            leaf["export_msgid"] = "414_" + leaf["export_msgid"].split("_", 1)[1]
        proof["record_id"] = _identifier("html-text-v1", proof["locator"]["archive_sha256"], fixtures._json(proof["locator"]))
        row.update(id=_identifier(fixture.html_source, proof["record_id"]), provenance=json.dumps(proof), ordinal=row["ordinal"] + 1)
        fixtures._insert(connection, "messages", row)
    return row


def _json_links(fixture, links, info=None):
    fixture.clock_registry.write_text(json.dumps({"schema_version": 1, "links": links, "info": info or {}}))


def _indexed_links(fixture, links, info=None):
    path = fixture.clock_registry.with_suffix(".sqlite3")
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA user_version=1")
        connection.execute(
            "CREATE TABLE links(target_message_id TEXT PRIMARY KEY,native_message_id TEXT,"
            "target_text_sha256 TEXT,target_provenance_sha256 TEXT,native_text_sha256 TEXT,"
            "native_provenance_sha256 TEXT) WITHOUT ROWID"
        )
        connection.executemany("INSERT INTO links VALUES(?,?,?,?,?,?)", [tuple(link[key] for key in clocks._LINK_COLUMNS) for link in links])
        connection.execute("CREATE TABLE info(key TEXT PRIMARY KEY,value TEXT)")
        connection.executemany("INSERT INTO info VALUES(?,?)", (info or {}).items())
    return path


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("fault", ["pin", "caller", "missing_native", "malformed", "duplicate"])
def test_bad_link_does_not_erase_independent_valid_target(pinned_clock_store, reverse, fault):
    fixture = pinned_clock_store
    second = _add_html_target(fixture)
    rows = [*_rows(fixture), second]
    links = _links(fixture, [row["id"] for row in rows])
    if fault == "pin":
        links[1]["target_provenance_sha256"] = "0" * 64
    elif fault == "caller":
        rows[1]["provenance"] += " "
    elif fault == "missing_native":
        links[1]["native_message_id"] = "0" * 64
    elif fault == "malformed":
        links[1]["native_text_sha256"] = "invalid"
    else:
        links.append(dict(links[1]))
    _json_links(fixture, list(reversed(links)) if reverse else links)
    result = clocks.WechatForwardedClocks(fixture.store).project(list(reversed(rows)) if reverse else rows)
    assert set(result) == {fixture.html_parent}
    assert len(result[fixture.html_parent]) == 9


def test_shared_native_and_snapshot_bytes_are_charged_only_once(pinned_clock_store, monkeypatch):
    fixture = pinned_clock_store
    second = _add_html_target(fixture)
    rows = [*_rows(fixture), second]
    _json_links(fixture, _links(fixture, [row["id"] for row in rows]))
    ids = [fixture.html_parent, second["id"], fixture.native_parent]
    with closing(sqlite3.connect(fixture.history)) as connection, connection:
        marks = ",".join("?" for _ in ids)
        source_bytes = connection.execute("SELECT SUM(length(CAST(text AS BLOB))+length(CAST(provenance AS BLOB))) FROM messages WHERE id IN (" + marks + ")", ids).fetchone()[0]
        snapshot_bytes = connection.execute("SELECT SUM(length(CAST(metadata AS BLOB))) FROM source_snapshots WHERE id IN (SELECT json_extract(provenance,'$.source_snapshot') FROM messages WHERE id IN (" + marks + "))", ids).fetchone()[0]
    monkeypatch.setattr(clocks, "MAX_SOURCE_BYTES", source_bytes + snapshot_bytes)
    statements, original = [], clocks._readonly

    def traced(path):
        connection = original(path)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(clocks, "_readonly", traced)
    result = clocks.WechatForwardedClocks(fixture.store).project(rows)
    assert set(result) == {fixture.html_parent, second["id"]}
    assert sum("SELECT * FROM source_snapshots WHERE id=" in sql for sql in statements) == 2


def test_indexed_registry_selects_requested_targets_and_retains_receipt(pinned_clock_store, monkeypatch):
    fixture = pinned_clock_store
    primary = _links(fixture, [fixture.html_parent])[0]
    links = [primary, *[{**primary, "target_message_id": hashlib.sha256(f"unrequested-{number}".encode()).hexdigest()} for number in range(300)]]
    receipt = {"candidate_agreement_receipt": "offline-audit.json", "candidate_agreement_receipt_sha256": "a" * 64}
    path = _indexed_links(fixture, links, receipt)
    statements, original = [], clocks._readonly

    def traced(target):
        connection = original(target)
        if target == path:
            connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(clocks, "_readonly", traced)
    assert clocks.linked_parent_ids(fixture.store, [fixture.html_parent, "f" * 64]) == (fixture.html_parent,)
    projected = clocks.WechatForwardedClocks(fixture.store).project(_rows(fixture))[fixture.html_parent]
    assert projected[(1,)]["proof"]["candidate_agreement_receipt_sha256"] == "a" * 64
    assert all("ORDER BY target_message_id" not in sql for sql in statements)
    assert any("WHERE target_message_id IN" in sql for sql in statements)
    with closing(sqlite3.connect(path)) as connection, connection:
        plan = connection.execute("EXPLAIN QUERY PLAN SELECT * FROM links WHERE target_message_id=?", (fixture.html_parent,)).fetchall()
    assert any("SEARCH links USING PRIMARY KEY" in row[3] for row in plan)


@pytest.mark.parametrize("fault", ["pin", "malformed", "oversized_field"])
def test_indexed_bad_link_is_isolated_before_loading_other_targets(pinned_clock_store, fault):
    fixture = pinned_clock_store
    second = _add_html_target(fixture)
    rows = [*_rows(fixture), second]
    links = _links(fixture, [row["id"] for row in rows])
    links[1]["native_text_sha256"] = "0" * 64 if fault == "pin" else "invalid" if fault == "malformed" else "f" * 1_000_000
    _indexed_links(fixture, links)
    result = clocks.WechatForwardedClocks(fixture.store).project(rows)
    assert set(result) == {fixture.html_parent}
    assert len(result[fixture.html_parent]) == 9


def test_stable_index_digest_is_hashed_once_per_file_identity(pinned_clock_store, monkeypatch):
    fixture = pinned_clock_store
    path = _indexed_links(fixture, _links(fixture, [fixture.html_parent]))
    original, reads = type(path).open, []

    def counted(target, *args, **kwargs):
        if target == path:
            reads.append(args[0] if args else kwargs.get("mode", "r"))
        return original(target, *args, **kwargs)

    monkeypatch.setattr(type(path), "open", counted)
    assert clocks.registry_digest(fixture.store)
    assert clocks.linked_parent_ids(fixture.store, [fixture.html_parent]) == (fixture.html_parent,)
    assert clocks.WechatForwardedClocks(fixture.store).project(_rows(fixture))
    assert reads == ["rb"]


@pytest.mark.parametrize("fault", ["corrupt", "version", "symlink", "journal", "size", "count"])
def test_indexed_authority_never_falls_back_to_stale_json(pinned_clock_store, monkeypatch, fault):
    fixture = pinned_clock_store
    path = _indexed_links(fixture, _links(fixture, [fixture.html_parent]))
    if fault == "corrupt":
        path.write_bytes(b"invalid database")
    elif fault == "version":
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("PRAGMA user_version=2")
    elif fault == "symlink":
        original = path.with_name("original-index.sqlite3")
        path.rename(original)
        path.symlink_to(original)
    elif fault == "journal":
        path.with_name(path.name + "-journal").write_bytes(b"active")
    elif fault == "size":
        monkeypatch.setattr(clocks, "MAX_INDEXED_REGISTRY_BYTES", 0)
    else:
        monkeypatch.setattr(clocks, "MAX_INDEXED_LINKS", 0)
    assert clocks.registry_digest(fixture.store) == ""
    assert clocks.linked_parent_ids(fixture.store) == ()
    assert clocks.WechatForwardedClocks(fixture.store).project(_rows(fixture)) == {}


def test_registry_digest_tracks_atomic_replacement(pinned_clock_store):
    fixture = pinned_clock_store
    before = clocks.registry_digest(fixture.store)
    replacement = fixture.clock_registry.with_name("replacement.json")
    replacement.write_text(json.dumps({"schema_version": 1, "links": [], "info": {"publication_id": "withdrawn"}}))
    replacement.replace(fixture.clock_registry)
    assert clocks.registry_digest(fixture.store) != before
    assert clocks.linked_parent_ids(fixture.store, [fixture.html_parent]) == ()


@pytest.mark.parametrize("indexed", [False, True])
def test_global_agreement_receipt_requires_the_unchanged_source_identity(pinned_clock_store, indexed):
    fixture = pinned_clock_store
    identity = json.dumps(clocks._identity(fixture.history))
    links = _links(fixture, [fixture.html_parent])
    info = {"source_identity": identity, "candidate_agreement_receipt": "all-candidates-reviewed.json"}
    (_indexed_links if indexed else _json_links)(fixture, links, info)
    assert clocks.WechatForwardedClocks(fixture.store).project(_rows(fixture))
    with closing(sqlite3.connect(fixture.history)) as connection, connection:
        connection.execute("UPDATE messages SET sender_label=sender_label || ' new source observation' WHERE id=?", (fixture.native_parent,))
    assert clocks.WechatForwardedClocks(fixture.store).project(_rows(fixture)) == {}


def test_cached_html_snapshot_cannot_cross_its_stored_source_partition(pinned_clock_store):
    fixture = pinned_clock_store
    with closing(sqlite3.connect(fixture.history)) as connection, connection:
        connection.execute("UPDATE source_snapshots SET source_id='html-other-source' WHERE id=(SELECT json_extract(provenance,'$.source_snapshot') FROM messages WHERE id=?)", (fixture.html_parent,))
    assert clocks.WechatForwardedClocks(fixture.store).project(_rows(fixture)) == {}


def _short_packet(fixture, count, *, single=False, ordinal=False):
    def native(proof):
        parts = [part for part in proof["text_components"] if part["kind"] == "forwarded_text"][:count]
        for number, part in enumerate(parts, 1):
            part["forwarded_item_path"] = [number]
            if single:
                part["source_sender_id"] = fixture.owner
            if ordinal:
                part["source_data_id"] = str(number - 1)
        proof["text_components"] = parts

    _mutate(fixture, native=True, callback=native)
    with closing(sqlite3.connect(fixture.history)) as connection, connection:
        parts = json.loads(connection.execute("SELECT provenance FROM messages WHERE id=?", (fixture.native_parent,)).fetchone()[0])["text_components"]

    def html(proof):
        proof["forwarded_components"] = proof["forwarded_components"][:count]
        for leaf, part in zip(proof["forwarded_components"], parts):
            leaf.update(sender_native_id=part["source_sender_id"], export_msgid="413_" + part["source_data_id"])

    _mutate(fixture, native=False, callback=html)
    _pin(fixture)


@pytest.mark.parametrize("count", [1, 2, 3])
@pytest.mark.parametrize("single", [False, True])
def test_complete_opaque_packets_do_not_require_multiple_speakers_or_three_leaves(pinned_clock_store, count, single):
    fixture = pinned_clock_store
    _short_packet(fixture, count, single=single)
    result = clocks.WechatForwardedClocks(fixture.store).project(_rows(fixture))
    assert len(result[fixture.html_parent]) == count


@pytest.mark.parametrize("fault", [None, "single", "short", "delivery", "owner"])
def test_ordinal_packets_require_one_verified_receiving_event(pinned_clock_store, fault):
    fixture = pinned_clock_store
    _short_packet(fixture, 2 if fault == "short" else 3, single=fault == "single", ordinal=True)
    if fault == "delivery":
        with closing(sqlite3.connect(fixture.history)) as connection, connection:
            connection.execute("UPDATE messages SET timestamp_value='2021-10-20T16:20:03' WHERE id=?", (fixture.html_parent,))
    elif fault == "owner":
        _change_html_owner(fixture, "wxid_independent_html_owner")
    _pin(fixture)
    result = clocks.WechatForwardedClocks(fixture.store).project(_rows(fixture))
    assert bool(result) == (fault is None)


def _change_html_owner(fixture, owner):
    with closing(sqlite3.connect(fixture.history)) as connection, connection:
        connection.execute("UPDATE partitions SET account_id=?,account_key=? WHERE id=?", (owner, "account:" + owner, fixture.html_source))
        connection.execute("UPDATE conversations SET account_key=? WHERE partition_id=?", ("account:" + owner, fixture.html_source))
        snapshot = connection.execute("SELECT json_extract(provenance,'$.source_snapshot') FROM messages WHERE id=?", (fixture.html_parent,)).fetchone()[0]
        metadata = json.loads(connection.execute("SELECT metadata FROM source_snapshots WHERE id=?", (snapshot,)).fetchone()[0])
        metadata.update(source_account_id=owner, owner_evidence={"native_username": owner, "native_contact_id": canonical_contact_id(owner, authority="native_account_owner")})
        connection.execute("UPDATE source_snapshots SET metadata=? WHERE id=?", (json.dumps(metadata), snapshot))


@pytest.mark.parametrize("reciprocal", [False, True])
@pytest.mark.parametrize("fault", [None, "peer", "direction"])
def test_direct_packets_require_the_same_verified_participant_pair(pinned_clock_store, reciprocal, fault):
    fixture = pinned_clock_store
    _short_packet(fixture, 1, single=True)
    native_peer = fixtures.RECEIVER
    html_peer = fixture.owner if reciprocal else native_peer
    if reciprocal:
        _change_html_owner(fixture, native_peer)
    if fault == "peer":
        html_peer = "wxid_unrelated_recipient"
    with closing(sqlite3.connect(fixture.history)) as connection, connection:
        for identifier, peer in ((fixture.native_parent, native_peer), (fixture.html_parent, html_peer)):
            connection.execute("UPDATE conversations SET kind='direct',peer_id=? WHERE id=(SELECT conversation_id FROM messages WHERE id=?)", (peer, identifier))
        direction = "received" if reciprocal else "sent"
        connection.execute("UPDATE messages SET direction=? WHERE id=?", ("unknown" if fault == "direction" else direction, fixture.html_parent))
        snapshot = connection.execute("SELECT json_extract(provenance,'$.source_snapshot') FROM messages WHERE id=?", (fixture.html_parent,)).fetchone()[0]
        metadata = json.loads(connection.execute("SELECT metadata FROM source_snapshots WHERE id=?", (snapshot,)).fetchone()[0])
        for entry in metadata["source_conversation_catalog"]:
            entry.update(index_avatar_native_id=html_peer, conversation_kind="direct")
        connection.execute("UPDATE source_snapshots SET metadata=? WHERE id=?", (json.dumps(metadata), snapshot))
    _mutate(fixture, native=True, callback=lambda proof: proof["locator"].update(table="Chat_" + hashlib.md5(native_peer.encode()).hexdigest()))
    _pin(fixture)
    before = fixture.history.read_bytes()
    result = clocks.WechatForwardedClocks(fixture.store).project(_rows(fixture))
    assert bool(result) == (fault is None)
    assert before == fixture.history.read_bytes()


def _cross_context(fixture, change):
    peer = fixtures.RECEIVER if change == "private" else "999999@chatroom" if change == "group" else ROOM
    with closing(sqlite3.connect(fixture.history)) as connection, connection:
        connection.execute("UPDATE conversations SET kind=?,peer_id=? WHERE id=(SELECT conversation_id FROM messages WHERE id=?)", ("direct" if change == "private" else "group", peer, fixture.native_parent))
        if change == "forwarder":
            connection.execute("UPDATE messages SET sender_id='wxid_another_forwarder',direction='received' WHERE id=?", (fixture.native_parent,))
    _mutate(fixture, native=True, callback=lambda proof: proof["locator"].update(table="Chat_" + hashlib.md5(peer.encode()).hexdigest()))


def _agreement_info(fixture):
    return {
        "source_identity": json.dumps(clocks._identity(fixture.history)),
        "candidate_agreement_receipt": "all-independent-copies-agree.json",
        "candidate_agreement_receipt_sha256": "a" * 64,
    }


@pytest.mark.parametrize("change", ["group", "forwarder", "private"])
@pytest.mark.parametrize("receipt", ["valid", "missing_receipt", "missing_hash", "invalid_hash", "missing_identity"])
def test_strong_opaque_copy_crosses_receiving_context_only_with_complete_agreement_receipt(pinned_clock_store, change, receipt):
    fixture = pinned_clock_store
    _cross_context(fixture, change)
    info = _agreement_info(fixture)
    if receipt == "missing_receipt":
        info.pop("candidate_agreement_receipt")
    elif receipt == "missing_hash":
        info.pop("candidate_agreement_receipt_sha256")
    elif receipt == "invalid_hash":
        info["candidate_agreement_receipt_sha256"] = "z" * 64
    elif receipt == "missing_identity":
        info.pop("source_identity")
    _json_links(fixture, _links(fixture, [fixture.html_parent]), info)
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    result = clocks.WechatForwardedClocks(fixture.store).project(_rows(fixture))
    assert bool(result) == (receipt == "valid")
    if result:
        assert len(result[fixture.html_parent]) == 9
        for value in result[fixture.html_parent].values():
            assert value["proof"]["match_basis"] == "complete_opaque_packet_copy"
            assert "original_chat_id" not in value["proof"]
            assert value["time_basis"] == "Recovered native packet time; Source clock; timezone unknown"
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


@pytest.mark.parametrize("count,single,ordinal", [(1, False, False), (2, False, False), (3, True, False), (3, False, True)])
def test_agreement_receipt_never_relaxes_weak_or_ordinal_receiving_context(pinned_clock_store, count, single, ordinal):
    fixture = pinned_clock_store
    _short_packet(fixture, count, single=single, ordinal=ordinal)
    _cross_context(fixture, "group")
    _json_links(fixture, _links(fixture, [fixture.html_parent]), _agreement_info(fixture))
    assert clocks.WechatForwardedClocks(fixture.store).project(_rows(fixture)) == {}


def test_new_source_observation_invalidates_cross_context_agreement_globally(pinned_clock_store):
    fixture = pinned_clock_store
    _cross_context(fixture, "private")
    _indexed_links(fixture, _links(fixture, [fixture.html_parent]), _agreement_info(fixture))
    assert clocks.WechatForwardedClocks(fixture.store).project(_rows(fixture))
    with closing(sqlite3.connect(fixture.history)) as connection, connection:
        connection.execute("UPDATE messages SET sender_label=sender_label || ' added native observation' WHERE id=?", (fixture.native_parent,))
    assert clocks.WechatForwardedClocks(fixture.store).project(_rows(fixture)) == {}
