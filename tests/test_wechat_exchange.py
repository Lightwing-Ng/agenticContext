"""Exercise portable WeChat source boundaries, typed bytes, and hostile containers.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile

import pytest

from app.core.wechat_archive import _SCHEMA, _file_identity, wechat_history_partition_fingerprint
from app.core.wechat_exchange import (
    _package_id, _write_table, export_conversation, logical_row_sha256, read_package,
    validate_preserved_closure,
)
from app.core.wechat_export_merge import _EXPORT_SCHEMA
from tests.test_wechat_conversation_events import event_store as _event_store_fixture
from tests.test_wechat_resource_index import resources as _resource_store_fixture
from tests.test_wechat_forwarded_previews import previews as _preview_store_fixture


event_store = _event_store_fixture
resources = _resource_store_fixture
previews = _preview_store_fixture


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture
def archive(tmp_path):
    root = tmp_path / "store"
    base = root / "messaging" / "wechat"
    base.mkdir(parents=True)
    path = base / "history.sqlite3"
    expected = []
    sessions = []
    with sqlite3.connect(path) as connection:
        connection.executescript(_SCHEMA + _EXPORT_SCHEMA)
        for number, verified in enumerate((True, True, False)):
            owner = f"wxid_synthetic_{number}"
            partition = "ios-" + hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest()
            session = "wechat:" + _sha(partition + "room")
            sessions.append(session)
            connection.execute("INSERT INTO partitions VALUES(?,?,?,?,?,?,?,?)", (
                partition, "account:" + owner, owner, "Synthetic account",
                "Verified archive ID" if verified else "Source account unconfirmed",
                "iPad archive", "/untrusted/source-path", "/untrusted/payload-path"))
            connection.execute("INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                session, partition, "account:" + owner, "synthetic@chatroom", "Same title",
                "group", 1, 1.0, 1.0, "time", "time", "Synthetic body"))
            provenance = '{ "source_snapshot": ' + str(number + 1) + ', "record_id":"same-local-id", "native_xml":"<msg>2️⃣</msg>" }'
            row = (_sha(str(number)), session, 7, "wxid_sender", "Source label", "received",
                   "  Literal * text\n\n2️⃣\n", 1.0, "2026-11-08T00:00:01Z", "Source time",
                   "Native source epoch", provenance, ' ["source flag"] ', "literal search")
            connection.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
            connection.execute("INSERT INTO source_snapshots VALUES(?,?,?,?)", (
                number + 1, "snapshot-same-string", partition,
                '{ "source_account_id": "' + owner + '", "version": 1 }'))
            connection.execute("INSERT INTO message_evidence VALUES(?,?,?,?,?,?,?)", (
                _sha("evidence" + str(number)), row[0], partition, "snapshot-same-string",
                "same-local-id", '{ "exact": "original  observation" }', "native"))
            if verified:
                expected.append(dict(zip((r[1] for r in connection.execute("PRAGMA table_info(messages)")), row)))
        connection.execute("INSERT INTO archive_info VALUES(?,?)", ("opaque", b"\x00\xff\x80original"))
    return root, sessions, expected


def test_source_family_roundtrip_preserves_observations_and_exact_bytes(archive, tmp_path):
    root, sessions, expected = archive
    path = root / "messaging" / "wechat" / "history.sqlite3"
    before = path.read_bytes()
    output = tmp_path / "exchange.wechatx"
    manifest = export_conversation(root, sessions[0], output, account="account:wxid_synthetic_0", kind="group")
    package = read_package(output)
    assert package.package_id == manifest["package_id"]
    assert manifest["scope"]["conversation_ids"] == sorted(sessions[:2])
    assert list(package.iter_rows("history.messages")) == expected
    assert len(list(package.iter_rows("history.message_evidence"))) == 2
    assert list(package.iter_rows("history.archive_info"))[0]["value"] == b"\x00\xff\x80original"
    assert len(list(package.iter_rows("exchange.timeline"))) == 2
    assert len(list(package.iter_rows("history.source_snapshots"))) == 2
    assert list(package.iter_rows("contacts.contacts")) == []
    assert path.read_bytes() == before
    assert (root / "messaging" / "wechat" / "export_origin.json").is_file()
    with zipfile.ZipFile(output) as compressed:
        assert all(item.compress_type == zipfile.ZIP_STORED for item in compressed.infolist())


def _rewrite(source, output, mutation=None, extra=None):
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(output, "w") as rewritten:
        for item in original.infolist():
            raw = original.read(item.filename)
            if item.filename == "manifest.json" and mutation is not None:
                manifest = json.loads(raw)
                mutation(manifest)
                manifest["package_id"] = _package_id(manifest)
                raw = json.dumps(manifest).encode()
            rewritten.writestr(item.filename, raw)
        if extra:
            rewritten.writestr(*extra)


def test_rejects_undeclared_members_without_extraction(archive, tmp_path):
    root, sessions, _ = archive
    output, damaged = tmp_path / "good.wechatx", tmp_path / "bad.wechatx"
    export_conversation(root, sessions[0], output)
    _rewrite(output, damaged, extra=("../../escape.txt", b"untrusted"))
    with pytest.raises(ValueError, match="undeclared"):
        read_package(damaged)
    assert not (tmp_path.parent / "escape.txt").exists()


@pytest.mark.parametrize("change", [
    lambda m: m["tables"][0].update(row_count=1_000_001),
    lambda m: m["tables"][0].update(sha256="0" * 64),
    lambda m: m["tables"][0].update(path="../../untrusted.parquet"),
    lambda m: m["tables"][0].update(name="history.arbitrary_sql"),
])
def test_rejects_bad_bounds_hashes_and_table_contract(archive, tmp_path, change):
    root, sessions, _ = archive
    output, damaged = tmp_path / "good.wechatx", tmp_path / "bad.wechatx"
    export_conversation(root, sessions[0], output)
    _rewrite(output, damaged, change)
    with pytest.raises(ValueError):
        read_package(damaged)


def test_verified_handle_rejects_changed_file(archive, tmp_path):
    root, sessions, _ = archive
    output = tmp_path / "good.wechatx"
    export_conversation(root, sessions[0], output)
    package = read_package(output)
    with output.open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ValueError, match="changed"):
        list(package.iter_rows("history.messages"))


def test_measures_both_codecs_and_rejects_mixed_storage_types(tmp_path):
    rows = [{"id": i, "text": "Synthetic repeated body " * 10, "payload": b"\x00\xff", "clock": 1.0} for i in range(400)]
    chosen, codec, raw_bytes, columns = _write_table(tmp_path / "payload", rows, [(key, "") for key in rows[0]])
    assert chosen.stat().st_size == min((tmp_path / "payload.parquet").stat().st_size,
                                       (tmp_path / "payload.jsonl.zst").stat().st_size)
    assert codec in {"parquet-zstd", "jsonl-zstd"}
    assert raw_bytes > chosen.stat().st_size
    assert next(column for column in columns if column["name"] == "payload")["type"] == "blob"
    with pytest.raises(ValueError, match="Mixed"):
        _write_table(tmp_path / "mixed", [{"value": 1}, {"value": "1"}], [("value", "")])


def test_rejects_another_account_scope(archive, tmp_path):
    root, sessions, _ = archive
    with pytest.raises(ValueError, match="scope"):
        export_conversation(root, sessions[0], tmp_path / "invalid.wechatx", account="account:someone_else")


def test_stale_sidecar_is_preserved_without_promoting_it_to_valid_timeline(archive, tmp_path):
    root, sessions, _ = archive
    base = root / "messaging" / "wechat"
    with sqlite3.connect(base / "history.sqlite3") as history:
        conversation = history.execute("SELECT * FROM conversations WHERE id=?", (sessions[0],)).fetchone()
        row = list(history.execute("SELECT * FROM messages WHERE conversation_id=?", (sessions[0],)).fetchone())
        fingerprint = wechat_history_partition_fingerprint(history)
    with sqlite3.connect(base / "resource_references.sqlite3") as resource:
        from app.core.wechat_resource_index import _SCHEMA as resource_schema
        resource.executescript(resource_schema)
        resource.execute("INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", conversation)
        row[0], row[6] = _sha("stale-resource"), "Stale event must remain a declared source claim"
        resource.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
        pin = _file_identity(base / "history.sqlite3")
        pin["size"] -= 1
        resource.executemany("INSERT INTO archive_info VALUES(?,?)", (
            ("schema_version", "1"), ("history_file_identity", json.dumps(pin)),
            ("history_partitions_sha256", fingerprint)))
    output = tmp_path / "stale.wechatx"
    manifest = export_conversation(root, sessions[0], output)
    package = read_package(output)
    assert len(list(package.iter_rows("resource.messages"))) == 1
    assert row[0] not in {item["id"] for item in package.iter_rows("exchange.timeline")}
    assert manifest["scope"]["timeline_status"][sessions[0]]["resources"] == "stale"


def test_shared_body_layout_and_logical_messages_both_roundtrip(archive, tmp_path):
    root, sessions, expected = archive
    from app.core.wechat_shared_storage import _install
    with sqlite3.connect(root / "messaging" / "wechat" / "history.sqlite3") as connection:
        connection.execute("DELETE FROM archive_info")
        _install(connection)
        connection.execute("INSERT INTO shared_message_bodies VALUES(?,?,?)", (1, expected[0]["text"], "literal"))
        connection.execute("UPDATE message_storage SET text='',search_text=' search',content_ref=1")
    output = tmp_path / "shared.wechatx"
    export_conversation(root, sessions[0], output)
    package = read_package(output)
    assert list(package.iter_rows("history.messages")) == expected
    assert len(list(package.iter_rows("history.shared_message_bodies"))) == 1
    assert {row["text"] for row in package.iter_rows("history.message_storage")} == {""}
    assert {row["content_ref"] for row in package.iter_rows("history.message_storage")} == {1}


@pytest.mark.parametrize("filename", ["exclusions.json", "source_exclusions.json"])
def test_source_exclusion_overrides_reachable_family(archive, tmp_path, filename):
    root, sessions, _ = archive
    owner = "wxid_synthetic_1"
    source = "ios-" + hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest()
    (root / "messaging" / "wechat" / filename).write_text(json.dumps({
        "schema_version": 1, "sources": [{"source_id": source}]}))
    output = tmp_path / "excluded.wechatx"
    with pytest.raises(ValueError, match="explicitly excluded"):
        export_conversation(root, sessions[0], output)
    assert not output.exists()


def test_rejects_manifest_hiding_another_conversation_in_verified_rows(archive, tmp_path):
    root, sessions, _ = archive
    original, damaged = tmp_path / "family.wechatx", tmp_path / "hidden.wechatx"
    export_conversation(root, sessions[0], original)
    def hide(manifest):
        manifest["scope"]["conversation_ids"] = [sessions[0]]
        manifest["scope"]["dependency_conversation_ids"] = []
        manifest["counts"]["conversations"] = 1
        manifest["counts"]["dependency_conversations"] = 0
    _rewrite(original, damaged, hide)
    with pytest.raises(ValueError, match="declared conversation closure"):
        read_package(damaged)


def test_frozen_presentation_keeps_literal_clock_name_and_raw_occurrences(archive, tmp_path, monkeypatch):
    root, sessions, expected = archive
    import app.core.wechat_session_export as exports
    leaf = {"id": _sha("child"), "component_path": [1, 2], "sender_id": "source-sender",
            "sender_label": "Source child", "viewer_display_name": "Viewing child",
            "text": "  Literal child\n\n2️⃣", "timestamp_value": "2026-11-08T00:00:00Z",
            "timestamp_label": "8 Nov 2026 00:00:00", "time_basis": "Proved child source epoch",
            "source_clock": "00:00", "provenance": {"native_forwarded_clock_recovery": {"proof": "synthetic"}}}
    class FrozenExport:
        def __init__(self, local_store_root, session_id, *, write_event_cache):
            assert write_event_cache is False
            self.session = session_id
            self.sources = {}
            self.resource_status = "absent"
            self.events = {"status": "ready", "members_by_preferred": {expected[0]["id"]: [expected[1]["id"]]}}
        def close(self):
            pass
        def items(self):
            if self.session == sessions[0]:
                yield {**expected[0], "viewer_display_name": "Frozen viewer", "forwarded_items": [leaf]}
    monkeypatch.setattr(exports, "WechatSessionExport", FrozenExport)
    monkeypatch.setattr("app.core.wechat_exchange._full_presentations", lambda *args: {
        expected[0]["id"]: {"viewer_display_name": "Frozen viewer", "display_text": expected[0]["text"]}})
    output = tmp_path / "frozen.wechatx"
    export_conversation(root, sessions[0], output)
    package = read_package(output)
    assert len(list(package.iter_rows("history.messages"))) == 2
    assert len(list(package.iter_rows("exchange.timeline"))) == 1
    snapshot = list(package.iter_rows("exchange.presentations"))[0]
    assert snapshot["viewer_display_name"] == "Frozen viewer"
    assert json.loads(snapshot["metadata_json"])["forwarded_items"] == [leaf]
    flat = list(package.iter_rows("exchange.forwarded_items"))[0]
    assert flat["component_path"] == "[1,2]"
    assert flat["text"] == leaf["text"]


def test_output_cannot_replace_source_directly_or_through_hardlink(archive, tmp_path):
    root, sessions, _ = archive
    source = root / "messaging" / "wechat" / "history.sqlite3"
    before = source.read_bytes()
    with pytest.raises(ValueError, match="extension"):
        export_conversation(root, sessions[0], source)
    alias = tmp_path / "alias.wechatx"
    alias.hardlink_to(source)
    with pytest.raises(ValueError, match="never replace"):
        export_conversation(root, sessions[0], alias)
    assert source.read_bytes() == before
    assert alias.read_bytes() == before
    assert not (source.parent / "export_origin.json").exists()


def test_nested_original_rows_cannot_hide_an_unrelated_conversation(archive, tmp_path):
    root, sessions, _ = archive
    output = tmp_path / "nested.wechatx"
    export_conversation(root, sessions[0], output)
    package = read_package(output)
    assert validate_preserved_closure(package.manifest, package.iter_rows)["messages"] == 2
    def changed_rows(name):
        for row in package.iter_rows(name):
            if name == "history.messages":
                row["conversation_id"] = "wechat:" + "f" * 64
            yield row
    with pytest.raises(ValueError, match="logical rows"):
        validate_preserved_closure(package.manifest, changed_rows)
    altered = json.loads(json.dumps(package.manifest))
    table = next(row for row in altered["tables"] if row["name"] == "history.messages")
    table["rows_sha256"] = hashlib.sha256("".join(sorted(logical_row_sha256(row) for row in changed_rows("history.messages"))).encode()).hexdigest()
    altered["package_id"] = _package_id(altered)
    with pytest.raises(ValueError, match="declared conversation closure"):
        validate_preserved_closure(altered, changed_rows)


def test_routed_native_event_retains_its_original_source_partition(event_store, tmp_path):
    fixture = event_store
    from tests.test_wechat_conversation_events import HTML, NATIVE, SESSION
    before = {path: path.read_bytes() for path in (fixture.repository.path, fixture.registry)}
    output = tmp_path / "routed.wechatx"
    export_conversation(fixture.store, SESSION, output)
    package = read_package(output)
    assert {row["id"] for row in package.iter_rows("history.partitions")} == {HTML, NATIVE}
    assert len(list(package.iter_rows("history.messages"))) == 6
    assert len(list(package.iter_rows("exchange.timeline"))) == 3
    assert len(list(package.iter_rows("history.source_snapshots"))) == 2
    assert all(path.read_bytes() == raw for path, raw in before.items())
    assert not (fixture.repository.path.parent / "conversation_events").exists()


def test_full_projection_freezes_manual_quote_and_exact_pinned_artifact(tmp_path):
    from tests.test_wechat_text_observations import make_observation_fixture
    fixture = make_observation_fixture(tmp_path / "manual")
    before = {path: path.read_bytes() for path in (
        fixture["history_path"], fixture["resource_path"], fixture["registry_path"], fixture["artifact_path"])}
    output = tmp_path / "manual.wechatx"
    manifest = export_conversation(fixture["store"], fixture["session_id"], output)
    package = read_package(output)
    presentation = next(package.iter_rows("exchange.presentations"))
    metadata = json.loads(presentation["metadata_json"])["presentation"]
    assert metadata["display_text"] == fixture["new_reply"]
    children = metadata["quoted_reply"]["forwarded_record"]["items"]
    assert [row["content_text"] for row in children[1:]] == [fixture["excerpt_text"], fixture["canonical_text"]]
    artifact = next(package.iter_rows("registries.artifacts"))
    assert artifact["content"] == before[fixture["artifact_path"]]
    assert len(list(package.iter_rows("history.messages"))) == 2
    assert len(list(package.iter_rows("exchange.timeline"))) == 1
    assert manifest["counts"]["dependency_conversations"] == 1
    assert all(path.read_bytes() == raw for path, raw in before.items())
    from app.core.wechat_exchange_import import import_package
    from app.core.wechat_history_browser import query_wechat_history
    target = tmp_path / "restored"
    restored = import_package(target, output, apply=True, minimum_free_bytes=0)
    assert restored["inserted_messages"] == 1
    with sqlite3.connect(target / "messaging" / "wechat" / "history.sqlite3") as connection:
        session = connection.execute("SELECT conversation_id FROM messages").fetchone()[0]
    page = query_wechat_history(target, session=session, persist_event_cache=False)
    context = page.items[0].messaging
    assert context["display_text"] == fixture["new_reply"]
    restored_children = context["quoted_reply"]["forwarded_record"]["items"]
    assert [item.content_text for item in restored_children[1:]] == [fixture["excerpt_text"], fixture["canonical_text"]]
    assert {item.stable_id for item in restored_children}.isdisjoint({item["stable_id"] for item in children})
    forwarded_path = tmp_path / "forwarded.wechatx"
    forwarded_manifest = export_conversation(target, session, forwarded_path)
    forwarded_package = read_package(forwarded_path)
    assert forwarded_manifest["origin_id"] == manifest["origin_id"]
    assert not any(name.startswith("preserved.") for name in forwarded_package.tables)
    assert forwarded_manifest["counts"] == manifest["counts"]
    assert {name: row["rows_sha256"] for name, row in forwarded_package.tables.items()} == {
        name: row["rows_sha256"] for name, row in package.tables.items()}
    with sqlite3.connect(target / "messaging" / "wechat" / "history.sqlite3") as connection:
        local_source = connection.execute("SELECT partition_id FROM conversations WHERE id=?", (session,)).fetchone()[0]
    (target / "messaging" / "wechat" / "source_exclusions.json").write_text(json.dumps({
        "schema_version": 1, "sources": [{"source_id": local_source}]}))
    with pytest.raises(ValueError, match="excluded local source"):
        export_conversation(target, session, tmp_path / "forbidden.wechatx")


def test_verified_resource_only_conversation_exports_without_primary_text(resources, tmp_path):
    from app.core.wechat_resource_index import build_resource_index, resource_index_path
    build_resource_index(resources.store, resources.snapshot)
    with sqlite3.connect(resource_index_path(resources.store)) as connection:
        session = connection.execute("SELECT id FROM conversations WHERE peer_id='attachments@chatroom'").fetchone()[0]
    output = tmp_path / "resources.wechatx"
    export_conversation(resources.store, session, output)
    package = read_package(output)
    assert list(package.iter_rows("history.messages")) == []
    assert len(list(package.iter_rows("exchange.timeline"))) == 1
    assert len(list(package.iter_rows("exchange.presentations"))) == 1
    metadata = json.loads(next(package.iter_rows("exchange.presentations"))["metadata_json"])
    assert metadata["presentation"]["resource"]["kind"] == "video"


def test_preview_only_packet_keeps_its_literal_excerpt(previews, tmp_path):
    fixture = previews
    with sqlite3.connect(fixture["history"]) as connection:
        session = connection.execute("SELECT conversation_id FROM messages WHERE id=?", (fixture["target_id"],)).fetchone()[0]
    output = tmp_path / "preview.wechatx"
    export_conversation(fixture["store"], session, output)
    package = read_package(output)
    row = next(row for row in package.iter_rows("exchange.presentations") if row["message_id"] == fixture["target_id"])
    record = json.loads(row["metadata_json"])["presentation"]["forwarded_record"]
    unresolved = record["items"][8]["metadata"]["forwarded_record"]
    assert unresolved["presentation_label"] == "Preview only"
    assert unresolved["preview_text"] == "An unresolved label: literal preview\n  • Keep spacing  "
    assert unresolved["items"] == []


def test_canonical_typed_row_limit_is_shared_with_importer_before_export(archive, tmp_path):
    from app.core.wechat_exchange_import import _encoded_row
    from app.core.wechat_exchange_rows import encode_typed_row
    small = {"null": None, "integer": 2**63 - 1, "real": -0.0, "text": "  Literal\n2️⃣", "blob": b"\x00\xff"}
    assert encode_typed_row(small, small) == _encoded_row(small, small)
    large = {"text": "x" * (8 * 1024**2), "search": "x" * (8 * 1024**2)}
    for encoder in (encode_typed_row, _encoded_row):
        with pytest.raises(ValueError, match="row exceeds"):
            encoder(large, large)
    root, sessions, _ = archive
    path = root / "messaging" / "wechat" / "history.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE messages SET text=?,search_text=? WHERE conversation_id=?",
                           (large["text"], large["search"], sessions[0]))
    before = hashlib.sha256(path.read_bytes()).digest()
    output = tmp_path / "oversized.wechatx"
    with pytest.raises(ValueError, match="row exceeds"):
        export_conversation(root, sessions[0], output)
    assert not output.exists()
    assert not (path.parent / "export_origin.json").exists()
    assert hashlib.sha256(path.read_bytes()).digest() == before


def test_unrelated_native_preferences_allow_fast_forward_but_selected_binding_does_not(archive, tmp_path):
    from contextlib import closing
    from app.core.wechat_contacts import initialize_contact_registry
    from app.core.wechat_exchange_import import import_package
    from app.core.wechat_history_browser import query_wechat_history
    from tests.test_wechat_contacts import bind, insert_contact
    root, sessions, _ = archive
    first = tmp_path / "first.wechatx"
    original = export_conversation(root, sessions[0], first)
    target = tmp_path / "receiver"
    import_package(target, first, apply=True, minimum_free_bytes=0)
    base = target / "messaging" / "wechat"
    with closing(sqlite3.connect(base / "history.sqlite3")) as connection:
        session, partition = connection.execute("SELECT id,partition_id FROM conversations ORDER BY id LIMIT 1").fetchone()
        local_message = connection.execute("SELECT id FROM messages WHERE conversation_id=? LIMIT 1", (session,)).fetchone()[0]
    with closing(sqlite3.connect(base / "contacts.sqlite3")) as contacts:
        initialize_contact_registry(contacts)
        identifier = insert_contact(contacts, "wxid_sender")
        bind(contacts, identifier, source="ios-unrelated-native-source", sender="wxid_sender")
        contacts.commit()
    (base / "display_names.json").write_text(json.dumps({
        "schema_version": 1, "contacts": {identifier: "Unrelated native alias"},
        "hidden_account_sources": ["ios-unrelated-native-source"]}))
    (base / "context_links.json").write_text(json.dumps({
        "schema_version": 1, "links": [{"target_message_id": "e" * 64,
            "source_message_id": local_message, "evidence_id": "c" * 64}]}))
    before = query_wechat_history(target, session=session, persist_event_cache=False)
    assert before.items[0].messaging.get("viewer_display_name") != "Unrelated native alias"
    forwarded = tmp_path / "unrelated-prefs.wechatx"
    manifest = export_conversation(target, session, forwarded)
    package = read_package(forwarded)
    assert manifest["origin_id"] == original["origin_id"]
    assert not any(name.startswith("preserved.") for name in package.tables)
    with closing(sqlite3.connect(base / "contacts.sqlite3")) as contacts:
        bind(contacts, identifier, source=partition, sender="wxid_sender", room="synthetic@chatroom")
        contacts.commit()
    after = query_wechat_history(target, session=session, persist_event_cache=False)
    assert after.items[0].messaging["viewer_display_name"] == "Unrelated native alias"
    changed = tmp_path / "selected-prefs.wechatx"
    manifest = export_conversation(target, session, changed)
    package = read_package(changed)
    assert manifest["origin_id"] != original["origin_id"]
    assert "contacts.sender_bindings" in package.tables
    assert "preserved.original_rows" in package.tables


def test_reader_rejects_many_valid_cells_whose_typed_row_exceeds_import_limit(archive, tmp_path):
    import pyarrow as pa
    root, sessions, _ = archive
    original_path, damaged = tmp_path / "small.wechatx", tmp_path / "large-row.wechatx"
    export_conversation(root, sessions[0], original_path)
    package = read_package(original_path)
    rows = list(package.iter_rows("history.messages"))
    rows[0]["text"] = rows[0]["search_text"] = "x" * (8 * 1024**2)
    raw = b"".join((json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode() for row in rows)
    compressed = bytes(pa.Codec("zstd", compression_level=1).compress(raw))
    manifest = json.loads(json.dumps(package.manifest))
    descriptor = next(item for item in manifest["tables"] if item["name"] == "history.messages")
    old_path = descriptor["path"]
    descriptor.update(codec="jsonl-zstd", path="tables/history/messages.jsonl.zst", bytes=len(compressed),
                      sha256=hashlib.sha256(compressed).hexdigest(), uncompressed_bytes=len(raw))
    manifest["package_id"] = _package_id(manifest)
    with zipfile.ZipFile(original_path) as source, zipfile.ZipFile(damaged, "w") as target:
        for item in source.infolist():
            if item.filename not in {"manifest.json", old_path}:
                target.writestr(item.filename, source.read(item.filename))
        target.writestr(descriptor["path"], compressed)
        target.writestr("manifest.json", json.dumps(manifest).encode())
    with pytest.raises(ValueError, match="row exceeds"):
        read_package(damaged)
