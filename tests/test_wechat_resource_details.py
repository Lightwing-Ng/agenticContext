"""Bounded resource details preserve canonical identity and exact native evidence.

Code version: v1.4.1-codex.0
"""

from contextlib import closing
from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import owner_only_files
from app.core import wechat_resource_details as details
from app.core.wechat_archive import WechatArchiveRepository, _time_fields
from app.core.wechat_forwarded import project_native_forwarded
from app.core.wechat_history_browser import query_wechat_history
from app.core.wechat_resource_sender_evidence import secondary_native_events
from app.core.wechat_session_export import WechatSessionExport, WechatSessionSourceChanged
from tests import test_wechat_macos_recovery_account_binding as account
from tests import test_wechat_macos_recovery_bank as native
from tests import test_wechat_resource_sender_evidence as sender


mac_archive = native.mac_archive
bank_archive = native.bank_archive
source_binding = account.source_binding
witness = sender.witness


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture
def fixture(source_binding, witness):
    row, _old_conversation, _old_packet = deepcopy(witness)
    owner, peer = native.OWNER, native.ROOM
    packet = deepcopy(source_binding.packet)
    packet.update(
        peer_id=peer,
        conversation_kind="group",
        session_id=source_binding.fixture.group_session,
        peer_proof=native._proof(10, peer, is_session=1),
    )
    packet["source"]["table"] = "Msg_" + hashlib.md5(peer.encode()).hexdigest()
    message = native._message(4, 49, resource="file")
    message.update(local_type=(4 << 32) | 49, text="[App message]")
    message["resource"] = {
        "kind": "app", "label": "[App message]", "native_time": message["create_time"],
        "app_subtype": "4", "app_metadata_version": "1",
        "safe_title": "A source title <b> 😀",
        "app_name": "小红书", "app_name_source": "appinfo/appname",
    }
    packet["messages"] = [message]
    with closing(sqlite3.connect(source_binding.history)) as connection:
        connection.row_factory = sqlite3.Row
        conversation = dict(connection.execute(
            "SELECT * FROM conversations WHERE id=?", (packet["session_id"],),
        ).fetchone())
    stamp = datetime.fromtimestamp(message["create_time"], UTC).isoformat()
    order, value, label, basis = _time_fields({"sent_at_utc_iso": stamp})
    row.update(
        conversation_id=conversation["id"], text="[App message]",
        search_text="[app message]", time_sort=order, timestamp_value=value,
        timestamp_label=label, time_basis=basis,
    )
    proof = json.loads(row["provenance"])
    proof.update(
        message_type=49,
        source_relative_path="AppDomain-com.tencent.xin/Documents/"
        + hashlib.md5(owner.encode()).hexdigest() + "/DB/message_2.sqlite",
    )
    proof["resource_reference"].update(
        account_id=owner, kind="app", label="[App message]",
    )
    row["provenance"] = json.dumps(proof)
    return SimpleNamespace(
        history=source_binding.history, root=source_binding.root,
        row=row, conversation=conversation, packet=packet,
    )


def _event(fixture):
    return secondary_native_events(fixture.packet)[0]


def _registry(fixture, entry=None):
    entry = entry or details.make_entry(fixture.row, fixture.conversation, fixture.packet)
    path = fixture.history.with_name(details.FILENAME)
    path.write_text(json.dumps({"version": details.VERSION, "entries": [entry]}))
    owner_only_files.ensure_owner_only(path)
    return path


def _connection(fixture):
    connection = sqlite3.connect(fixture.history.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TEMP TABLE upstream_details AS SELECT * FROM main.messages WHERE 0")
    connection.execute(
        "INSERT INTO upstream_details VALUES(" + ",".join("?" for _ in details.COLUMNS) + ")",
        tuple(fixture.row[key] for key in details.COLUMNS),
    )
    return connection


def _attach(connection, fixture, *, session=None):
    return details.attach_resource_details(
        connection, fixture.history, "main.conversations", "temp.upstream_details",
        conversation_id=session,
    )


def test_forwarded_placeholder_restores_native_transcript_and_original_coordinates(fixture):
    raw = (
        "<msg><appmsg><type>19</type><title>Group Chat History</title><recorditem>"
        "<recordinfo><datalist><dataitem datatype='1'><sourcename>Original speaker</sourcename>"
        "<datadesc>Archived text</datadesc></dataitem></datalist></recordinfo>"
        "</recorditem></appmsg></msg>"
    )
    fixture.packet["messages"] = [native._message(4, 49, raw=raw)]
    provenance = json.loads(fixture.row["provenance"])
    provenance["text_extraction_kind"] = "resource_placeholder"
    fixture.row["provenance"] = json.dumps(provenance)
    stored = deepcopy(fixture.row)
    result = details.project_resource_details(fixture.row, fixture.conversation, _event(fixture))
    proof = json.loads(result["provenance"])
    record = project_native_forwarded(proof, parent_id=stored["id"], account_id=native.OWNER)

    assert fixture.row == stored
    assert all(result[key] == stored[key] for key in (
        "id", "conversation_id", "ordinal", "direction", "time_sort",
        "timestamp_value", "timestamp_label", "time_basis",
    ))
    assert "resource_reference" not in proof
    assert result["text"] == "Archived text"
    assert result["search_text"].startswith("archived text")
    assert record["title"] == "Group Chat History"
    item, = record["items"]
    assert item.author_label == "Original speaker"
    assert item.content_text == "Archived text"

    conflict = json.loads(stored["provenance"])
    conflict["resource_reference"]["app_subtype"] = "5"
    stored["provenance"] = json.dumps(conflict)
    with pytest.raises(ValueError, match="conflicts with the retained app card"):
        details.project_resource_details(stored, fixture.conversation, _event(fixture))


def test_direct_app_labels_use_same_account_event_and_preserve_the_original_author(
    fixture, source_binding,
):
    packet = deepcopy(source_binding.packet)
    peer = packet["peer_id"]
    witness = deepcopy(fixture.packet["messages"][0])
    witness.update(
        sender_id=native.OWNER,
        sender_label=native.OWNER,
        direction="sent",
        real_sender_id=2,
        sender_proof=native._proof(2, native.OWNER),
    )
    packet["messages"] = [witness]
    with closing(sqlite3.connect(fixture.history)) as connection:
        connection.row_factory = sqlite3.Row
        conversation = dict(connection.execute(
            "SELECT * FROM conversations WHERE id=?", (packet["session_id"],),
        ).fetchone())
    row = {
        **fixture.row, "conversation_id": packet["session_id"],
        "sender_id": native.OWNER, "sender_label": "Original sender", "direction": "sent",
    }
    proof = json.loads(row["provenance"])
    proof["text_extraction_kind"] = "resource_placeholder"
    proof["resource_reference"]["peer_id"] = peer
    proof["locator"]["table"] = "Chat_" + hashlib.md5(peer.encode(), usedforsecurity=False).hexdigest()
    row["provenance"] = json.dumps(proof)

    event, = secondary_native_events(packet)
    projected = details.project_resource_details(row, conversation, event)

    assert projected["sender_id"] == row["sender_id"]
    assert projected["sender_label"] == row["sender_label"]
    assert projected["direction"] == "sent"
    assert json.loads(projected["provenance"])["resource_reference"]["safe_title"] == "A source title <b> 😀"
    with pytest.raises(ValueError):
        details.project_resource_details({**row, "direction": "received"}, conversation, event)


def test_active_quote_sender_uses_identical_native_body_at_the_archived_clock(fixture):
    raw = (
        "<msg><appmsg><type>57</type><title>Exact reply</title>"
        "<refermsg><type>1</type><fromusr>wxid_original</fromusr>"
        "<svrid>123</svrid><content>Original text</content></refermsg></appmsg></msg>"
    )
    witness = native._message(4, 49, raw=raw)
    fixture.packet["messages"] = [witness]
    row = {**fixture.row, "text": witness["text"], "search_text": witness["text"].casefold()}
    proof = json.loads(row["provenance"])
    proof.pop("resource_reference")
    proof.update(
        text_extraction_kind="quoted_reply",
        secondary_resource_completion={"strategy": "native_quote", "clock_skew_seconds": -1},
    )
    row["provenance"] = json.dumps(proof)
    retained = {**witness, "create_time": witness["create_time"] - 1}

    with closing(sqlite3.connect(":memory:")) as connection:
        connection.executescript(
            "CREATE TEMP TABLE secondary_completion_active(id TEXT PRIMARY KEY);"
            "ATTACH DATABASE ':memory:' AS secondary_completions;"
            "CREATE TABLE secondary_completions.entries("
            "id,conversation_id,operation,strategy,projected_sha256,witness_key,source_key);"
            "CREATE TABLE secondary_completions.witnesses(id,source_key,message,sha256);"
        )
        connection.execute("INSERT INTO secondary_completion_active VALUES(?)", (row["id"],))
        connection.execute(
            "INSERT INTO secondary_completions.entries VALUES(?,?,'replace','native_quote',?,'w','s')",
            (row["id"], row["conversation_id"], details.row_sha256(row)),
        )
        connection.execute(
            "INSERT INTO secondary_completions.witnesses VALUES('w','s',?,?)",
            (json.dumps(retained), details._sha(retained)),
        )
        projected = details.project_resource_details(row, fixture.conversation, _event(fixture), connection=connection)
        assert projected["sender_id"] == native.MEMBER
        assert all(projected[key] == row[key] for key in (
            "id", "ordinal", "conversation_id", "text", "direction",
            "timestamp_value", "timestamp_label", "time_sort", "time_basis",
        ))
        projected_proof = json.loads(projected["provenance"])
        observation = projected_proof.pop("native_text_sender_details")
        assert projected_proof == proof
        assert observation["native_source_create_time"] - observation["retained_source_create_time"] == 1

        retained["content_sha256"] = "a" * 64
        connection.execute(
            "UPDATE secondary_completions.witnesses SET message=?, sha256=?",
            (json.dumps(retained), details._sha(retained)),
        )
        with pytest.raises(ValueError, match="conflicts with retained native evidence"):
            details.project_resource_details(row, fixture.conversation, _event(fixture), connection=connection)


def test_app_labels_and_sender_keep_all_original_event_coordinates(fixture):
    original = deepcopy(fixture.row)
    result = details.project_resource_details(fixture.row, fixture.conversation, _event(fixture))
    assert result["sender_id"] == native.MEMBER
    assert result["sender_label"] == native.MEMBER
    assert fixture.row == original
    for field in original.keys() - {
        "sender_id", "sender_label", "provenance", "quality_flags", "search_text",
    }:
        assert result[field] == original[field]
    proof = json.loads(result["provenance"])
    reference = proof["resource_reference"]
    assert reference["kind"] == "app"
    assert reference["app_subtype"] == "4"
    assert reference["safe_title"] == "A source title <b> 😀"
    assert reference["app_name"] == "小红书"
    assert proof["source_sha256"] == json.loads(original["provenance"])["source_sha256"]
    assert proof["native_resource_details"]["clock_skew_seconds"] == 0
    assert proof["native_resource_details"]["original_row_sha256"] == details.row_sha256(original)
    assert "小红书" in result["search_text"]
    assert "sender_id_unavailable" not in json.loads(result["quality_flags"])


def test_existing_sender_label_and_card_facts_are_preserved(fixture):
    fixture.row.update(sender_id=native.MEMBER, sender_label="Retained original name")
    proof = json.loads(fixture.row["provenance"])
    proof["resource_reference"]["safe_title"] = "A source title <b> 😀"
    fixture.row["provenance"] = json.dumps(proof)
    result = details.project_resource_details(fixture.row, fixture.conversation, _event(fixture))
    assert result["sender_label"] == "Retained original name"
    assert json.loads(result["provenance"])["resource_reference"]["safe_title"] == "A source title <b> 😀"
    proof["resource_reference"]["safe_title"] = "A conflicting source title"
    fixture.row["provenance"] = json.dumps(proof)
    with pytest.raises(ValueError, match="conflict"):
        details.project_resource_details(fixture.row, fixture.conversation, _event(fixture))


@pytest.mark.parametrize("changed", ["sender", "peer", "server", "time", "account", "type"])
def test_unrelated_or_conflicting_native_identity_never_replaces_authorship(fixture, changed):
    event = deepcopy(_event(fixture))
    if changed == "sender":
        fixture.row["sender_id"] = "wxid_known_other_author"
    elif changed == "peer":
        event["peer_id"] = "other@chatroom"
    elif changed == "server":
        event["server_id"] = "987654321"
    elif changed == "time":
        event["create_time"] += 1
        event["resource"]["native_time"] += 1
    elif changed == "account":
        event["account_id"] = "wxid_other_owner"
    elif changed == "type":
        event["local_type"] = 43
    with pytest.raises(ValueError):
        details.project_resource_details(fixture.row, fixture.conversation, event)


def test_video_author_recovers_without_fabricating_attachment_details(fixture):
    proof = json.loads(fixture.row["provenance"])
    proof["message_type"] = 43
    proof["resource_reference"].update(kind="video", label="[Video]")
    fixture.row.update(text="[Video]", search_text="[video]", provenance=json.dumps(proof))
    event = _event(fixture)
    event.update(local_type=43, text="[Video]")
    event["resource"] = {"kind": "video", "label": "[Video]", "native_time": event["create_time"]}
    result = details.project_resource_details(fixture.row, fixture.conversation, event)
    assert result["sender_id"] == native.MEMBER
    assert result["text"] == "[Video]"
    reference = json.loads(result["provenance"])["resource_reference"]
    assert "safe_title" not in reference
    assert "app_name" not in reference


def test_video_media_account_cannot_replace_a_missing_envelope_author(fixture):
    proof = json.loads(fixture.row["provenance"])
    proof["message_type"] = 43
    proof["resource_reference"].update(kind="video", label="[Video]")
    fixture.row.update(text="[Video]", search_text="[video]", provenance=json.dumps(proof))
    original = deepcopy(fixture.row)
    event = _event(fixture)
    event.update(local_type=43, text="[Video]", sender_id="", sender_label="")
    event["sender_proof"] = native._proof(event["real_sender_id"], "")
    event["resource"] = {
        "kind": "video", "label": "[Video]", "native_time": event["create_time"],
        "fromusername": native.MEMBER,
    }
    with pytest.raises(ValueError):
        details.project_resource_details(fixture.row, fixture.conversation, event)
    assert fixture.row == original
    assert not fixture.row["sender_id"]


def test_noop_or_already_projected_rows_cannot_create_override_chains(fixture):
    fixture.row.update(sender_id=native.MEMBER, sender_label="Known")
    packet = deepcopy(fixture.packet)
    packet["messages"][0]["resource"] = {
        "kind": "app", "label": "[App message]",
        "native_time": packet["messages"][0]["create_time"], "app_subtype": "4",
    }
    with pytest.raises(ValueError, match="adds no"):
        details.make_entry(fixture.row, fixture.conversation, packet)
    projected = details.project_resource_details(fixture.row, fixture.conversation, _event(fixture))
    with pytest.raises(ValueError, match="upstream"):
        details.make_entry(projected, fixture.conversation, fixture.packet)


def test_sql_layer_enriches_search_and_keeps_source_files_unchanged(fixture):
    path = _registry(fixture)
    before = {file: file.read_bytes() for file in (fixture.history, path)}
    with closing(_connection(fixture)) as connection:
        conversations, table, status = _attach(connection, fixture)
        assert conversations == "main.conversations"
        assert status == "ready"
        row = dict(connection.execute(
            f"SELECT * FROM {table} WHERE instr(search_text,?)>0", ("小红书",),
        ).fetchone())
        assert row["id"] == fixture.row["id"]
        assert row["sender_id"] == native.MEMBER
        assert row["text"] == fixture.row["text"]
        assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1
    for file, content in before.items():
        assert file.read_bytes() == content


def test_scoped_reader_does_not_apply_another_conversations_entry(fixture):
    _registry(fixture)
    with closing(_connection(fixture)) as connection:
        assert _attach(connection, fixture, session="wechat:" + "a" * 64) == (
            "main.conversations", "temp.upstream_details", "absent",
        )


@pytest.mark.parametrize("field", ["sender_label", "text", "ordinal", "timestamp_value", "search_text"])
def test_full_upstream_row_pin_rejects_changed_source_before_any_replacement(fixture, field):
    _registry(fixture)
    with closing(_connection(fixture)) as connection:
        connection.execute(
            f"UPDATE upstream_details SET {field}=?", (999 if field == "ordinal" else "Changed",),
        )
        assert _attach(connection, fixture)[2] == "invalid"
        assert connection.execute(
            "SELECT 1 FROM sqlite_temp_master WHERE name=?", (details.MESSAGE_TABLE,),
        ).fetchone() is None


def test_changed_primary_account_anchor_rejects_declared_native_proof(fixture):
    _registry(fixture)
    anchor = fixture.packet["account_binding"]["anchors"][0]["archive_message_id"]
    with sqlite3.connect(fixture.history) as primary:
        primary.execute("UPDATE messages SET text='Conflicting archive anchor' WHERE id=?", (anchor,))
    with closing(_connection(fixture)) as connection:
        assert _attach(connection, fixture)[2] == "invalid"


@pytest.mark.parametrize("problem", ["duplicate", "bad_hash", "packet_count", "version", "public", "symlink", "oversize"])
def test_malformed_or_untrusted_registry_fails_closed(fixture, problem):
    path = _registry(fixture)
    payload = json.loads(path.read_text())
    if problem == "duplicate":
        payload["entries"].append(deepcopy(payload["entries"][0]))
    elif problem == "bad_hash":
        payload["entries"][0]["original_row_sha256"] = "0" * 64
    elif problem == "packet_count":
        payload["entries"][0]["packet"]["messages"] *= 2
    elif problem == "version":
        payload["version"] = "unsupported"
    elif problem == "public":
        if os.name == "nt":
            security = owner_only_files._windows_security()
            owner = security.current_user_sid()
            security.set_protected_dacl(str(path), f"D:P(A;;FA;;;{owner})(A;;FR;;;WD)")
        else:
            path.chmod(0o644)
    elif problem == "symlink":
        target = path.with_suffix(".preserved")
        path.rename(target)
        path.symlink_to(target)
    elif problem == "oversize":
        path.write_bytes(b" " * (details.MAX_BYTES + 1))
    if problem not in {"symlink", "oversize"}:
        path.write_text(json.dumps(payload))
    with closing(_connection(fixture)) as connection:
        assert _attach(connection, fixture)[2] == "invalid"


@pytest.mark.parametrize("timestamps_tie", (False, True))
def test_registry_identity_pins_in_place_change_with_restored_size_and_mtime(
    fixture, monkeypatch, timestamps_tie,
):
    if timestamps_tie:
        monkeypatch.setattr(details, "IS_WINDOWS", True)
        monkeypatch.setattr(
            details, "file_change_time",
            lambda _path, *, descriptor=None: 116_444_736_000_000_000,
        )
    path = _registry(fixture)
    before = details.registry_identity(fixture.history)
    raw = path.read_bytes()
    path.write_bytes(raw.replace(b"source title", b"edited title"))
    assert path.stat().st_size == before["size"]
    os.utime(path, ns=(path.stat().st_atime_ns, before["mtime_ns"]))
    after = details.registry_identity(fixture.history)
    assert after != before
    if timestamps_tie:
        assert {key: value for key, value in after.items() if key != "sha256"} == {
            key: value for key, value in before.items() if key != "sha256"
        }


def test_windows_registry_rejects_native_change_during_read(fixture, monkeypatch):
    _registry(fixture)
    monkeypatch.setattr(details, "IS_WINDOWS", True)
    changes = iter((55, 55, 56))
    monkeypatch.setattr(details, "file_change_time", lambda *_args, **_kwargs: next(changes))

    with pytest.raises(ValueError, match="changed while reading"):
        details.load_registry(fixture.history)


def test_absent_registry_is_pinned_without_creating_files(fixture):
    path = fixture.history.with_name(details.FILENAME)
    assert details.detail_input_identities(fixture.history) == {str(path.resolve()): None}
    with closing(_connection(fixture)) as connection:
        assert _attach(connection, fixture)[2] == "absent"
    assert not path.exists()
    assert details.load_registry(fixture.history) == ([], None)


def _compact_registry(fixture, entries=None):
    entries = entries or [details.make_entry(fixture.row, fixture.conversation, fixture.packet)]
    path = fixture.history.with_name(details.FILENAME)
    path.write_bytes(details.encode_registry(entries))
    owner_only_files.ensure_owner_only(path)
    return path


def test_compact_round_trip_retains_the_flat_v1_logical_projection(fixture):
    entry = details.make_entry(fixture.row, fixture.conversation, fixture.packet)
    _registry(fixture, entry)
    legacy, legacy_identity = details.load_registry(fixture.history)
    assert legacy == [entry]
    assert legacy_identity == details.registry_identity(fixture.history)
    with closing(_connection(fixture)) as connection:
        _, table, status = _attach(connection, fixture)
        assert status == "ready"
        legacy_row = dict(connection.execute(f"SELECT * FROM {table}").fetchone())
    path = _compact_registry(fixture, legacy)
    decoded, identity = details.load_registry(fixture.history)
    assert decoded == legacy
    assert identity == details.registry_identity(fixture.history)
    assert details.encode_registry(decoded) == path.read_bytes()
    with closing(_connection(fixture)) as connection:
        _, table, status = _attach(connection, fixture)
        assert status == "ready"
        assert dict(connection.execute(f"SELECT * FROM {table}").fetchone()) == legacy_row


def test_many_entries_share_native_headers_and_scopes_without_truncation(fixture):
    original = details.make_entry(fixture.row, fixture.conversation, fixture.packet)
    entries = [
        dict(original, message_id=_sha(f"synthetic-detail-{index}"))
        for index in range(1_001)
    ]
    path = _compact_registry(fixture, entries)
    compact = json.loads(path.read_bytes())
    assert len(compact["entries"]) == 1_001
    assert len(compact["sources"]) == len(compact["conversations"]) == 1
    flat_bytes = len(json.dumps({"version": details.VERSION, "entries": entries}).encode())
    assert path.stat().st_size < flat_bytes // 2
    decoded, _ = details.load_registry(fixture.history)
    assert len(decoded) == 1_001
    assert {entry["message_id"] for entry in decoded} == {entry["message_id"] for entry in entries}
    assert decoded[0]["packet"]["account_binding"] is decoded[-1]["packet"]["account_binding"]
    assert details.encode_registry(reversed(decoded)) == path.read_bytes()


@pytest.mark.parametrize("problem", [
    "source_digest", "scope_digest", "unknown_source", "unknown_scope",
    "duplicate_id", "extra_header_field", "unused_source", "packet_field", "owner_scope",
])
def test_compact_transport_cannot_change_proof_objects_or_references(fixture, problem):
    path = _compact_registry(fixture)
    value = json.loads(path.read_bytes())
    source_key, = value["sources"]
    scope_key, = value["conversations"]
    if problem == "source_digest":
        value["sources"][source_key]["receipt_id"] = "tampered-receipt"
    elif problem == "scope_digest":
        value["conversations"][scope_key]["peer_id"] = "other@chatroom"
    elif problem == "unknown_source":
        value["entries"][0]["source_key"] = "0" * 64
    elif problem == "unknown_scope":
        value["entries"][0]["conversation_key"] = "0" * 64
    elif problem == "duplicate_id":
        value["entries"].append(deepcopy(value["entries"][0]))
    elif problem == "extra_header_field":
        header = value["sources"].pop(source_key)
        header["raw_xml"] = "opaque input must not enter metadata transport"
        key = details._sha(header)
        value["sources"][key] = header
        value["entries"][0]["source_key"] = key
    elif problem == "unused_source":
        header = deepcopy(value["sources"][source_key])
        header["receipt_id"] = "unused-retained-input"
        value["sources"][details._sha(header)] = header
    elif problem == "packet_field":
        value["entries"][0]["message"]["raw_xml"] = "opaque input is unsupported"
    elif problem == "owner_scope":
        scope = value["conversations"].pop(scope_key)
        owner = "wxid_other_owner"
        scope.update(
            account_key="account:" + owner,
            partition_id="ios-" + hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest(),
        )
        key = details._sha(scope)
        value["conversations"][key] = scope
        value["entries"][0]["conversation_key"] = key
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        details.load_registry(fixture.history)
    with closing(_connection(fixture)) as connection:
        assert _attach(connection, fixture)[2] == "invalid"


def test_compact_encoder_refuses_duplicate_ids_instead_of_selecting_a_witness(fixture):
    entry = details.make_entry(fixture.row, fixture.conversation, fixture.packet)
    with pytest.raises(ValueError, match="repeats"):
        details.encode_registry([entry, deepcopy(entry)])


def test_compact_registry_count_budget_is_explicit_and_never_truncates(fixture, monkeypatch):
    entry = details.make_entry(fixture.row, fixture.conversation, fixture.packet)
    entries = [dict(entry, message_id=_sha(str(index))) for index in range(3)]
    monkeypatch.setattr(details, "MAX_ENTRIES", 2)
    with pytest.raises(ValueError, match="entry budget"):
        details.encode_registry(entries)
    monkeypatch.setattr(details, "MAX_ENTRIES", 3)
    path = _compact_registry(fixture, entries)
    monkeypatch.setattr(details, "MAX_ENTRIES", 2)
    with pytest.raises(ValueError):
        details.load_registry(fixture.history)
    assert len(json.loads(path.read_bytes())["entries"]) == 3


def test_compact_registry_byte_budget_is_explicit_before_publication(fixture, monkeypatch):
    entry = details.make_entry(fixture.row, fixture.conversation, fixture.packet)
    encoded = details.encode_registry([entry])
    monkeypatch.setattr(details, "MAX_BYTES", len(encoded) - 1)
    with pytest.raises(ValueError, match="byte budget"):
        details.encode_registry([entry])


def test_compact_registry_replays_archive_side_account_proof(fixture):
    _compact_registry(fixture)
    anchor = fixture.packet["account_binding"]["anchors"][0]["archive_message_id"]
    with sqlite3.connect(fixture.history) as primary:
        primary.execute("UPDATE messages SET text='Changed after capture' WHERE id=?", (anchor,))
    with closing(_connection(fixture)) as connection:
        assert _attach(connection, fixture)[2] == "invalid"


def _publish_synthetic_upstream(fixture):
    with sqlite3.connect(fixture.history) as connection:
        connection.execute(
            "INSERT INTO messages VALUES(" + ",".join("?" for _ in details.COLUMNS) + ")",
            tuple(fixture.row[key] for key in details.COLUMNS),
        )
        connection.execute(
            "UPDATE conversations SET message_count=message_count+1 WHERE id=?",
            (fixture.conversation["id"],),
        )
    _registry(fixture)


def test_normal_history_query_search_and_complete_export_use_the_same_details(fixture):
    _publish_synthetic_upstream(fixture)
    before = fixture.history.read_bytes()
    page = query_wechat_history(
        fixture.root, session=fixture.conversation["id"], query="小红书",
        persist_event_cache=False,
    )
    assert page.total_count == 1
    item, = page.items
    assert item.stable_id == fixture.row["id"]
    assert item.messaging["sender_id"] == native.MEMBER
    assert item.messaging["resource"]["publisher_name"] == "小红书"
    assert item.messaging["resource"]["label"] == "A source title <b> 😀"
    assert item.content_text == fixture.row["text"]
    exporter = WechatSessionExport(
        fixture.root, fixture.conversation["id"], write_event_cache=False,
    )
    try:
        rows = list(exporter.items())
    finally:
        exporter.close()
    result, = (row for row in rows if row["id"] == fixture.row["id"])
    assert result["sender_id"] == native.MEMBER
    assert result["text"] == fixture.row["text"]
    assert json.loads(result["provenance"])["resource_reference"]["app_name"] == "小红书"
    assert fixture.history.read_bytes() == before


def test_export_detects_registry_change_even_with_restored_modification_time(fixture):
    _publish_synthetic_upstream(fixture)
    exporter = WechatSessionExport(
        fixture.root, fixture.conversation["id"], write_event_cache=False,
    )
    path = fixture.history.with_name(details.FILENAME)
    before = path.stat()
    raw = path.read_bytes()
    path.write_bytes(raw.replace(b"source title", b"edited title"))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    try:
        with pytest.raises(WechatSessionSourceChanged):
            list(exporter.items())
    finally:
        exporter.close()


def test_repository_query_rejects_stale_detail_row_instead_of_returning_false_author(fixture):
    _publish_synthetic_upstream(fixture)
    with sqlite3.connect(fixture.history) as connection:
        connection.execute("UPDATE messages SET sender_label='Source changed' WHERE id=?", (fixture.row["id"],))
    page = WechatArchiveRepository(fixture.root).query(
        session=fixture.conversation["id"], persist_event_cache=False,
    )
    assert page["resource_index_status"] == "invalid"
    retained, = (row for row in page["messages"] if row["id"] == fixture.row["id"])
    assert not retained["sender_id"]
