"""Session export aliases retain verified source identity and bounded snapshots.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

from contextlib import closing
import csv
import hashlib
import io
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core.wechat_archive import import_wechat_archive, wechat_archive_path
from app.core.wechat_contacts import (
    canonical_contact_id,
    contact_registry_path,
    initialize_contact_registry,
)
from app.core.wechat_display_names import display_names_path
from app.core.wechat_history_browser import query_wechat_history
from app.core.wechat_session_export import (
    WechatSessionExport,
    WechatSessionSourceChanged,
)
from app.web.wechat_session_csv import session_csv_chunks
from tests.test_wechat_archive import make_archive
from tests.test_wechat_resource_timeline import _reference, _sidecar


ALIAS = "Local viewing name"
OWNER = "wxid_account_a"
SENDER = "wxid_peer"


@pytest.fixture
def named_export(tmp_path):
    store = tmp_path / "store"
    import_wechat_archive(make_archive(tmp_path / "archive", repeated=215), store)
    history = wechat_archive_path(store)
    source = "ios-" + hashlib.md5(OWNER.encode()).hexdigest()
    with closing(sqlite3.connect(history)) as connection, connection:
        connection.row_factory = sqlite3.Row
        conversation = dict(
            connection.execute(
                "SELECT * FROM conversations WHERE partition_id=? AND kind='group'",
                (source,),
            ).fetchone()
        )
        unknown = connection.execute(
            "SELECT id FROM conversations WHERE partition_id='xlsx-2'"
        ).fetchone()[0]
    registry = contact_registry_path(store)
    identifier = canonical_contact_id(SENDER, authority="native_message_sender")
    with closing(sqlite3.connect(registry)) as connection, connection:
        initialize_contact_registry(connection)
        connection.execute(
            "INSERT INTO contacts VALUES(?,?,?,?,?,?)",
            (identifier, "wechat", "", SENDER, "wechat_account", "verified"),
        )
        connection.execute(
            "INSERT INTO sender_bindings VALUES(?,?,?,?,?,?,?)",
            (source, SENDER, "", "", identifier, "native_message_sender", "{}"),
        )
    preferences = display_names_path(store)
    preferences.write_text(
        json.dumps({"schema_version": 1, "contacts": {identifier: ALIAS}})
    )
    return SimpleNamespace(
        store=store,
        history=history,
        registry=registry,
        preferences=preferences,
        conversation=conversation,
        unknown=unknown,
        contact_id=identifier,
    )


def test_fixture_releases_sqlite_handles_before_exposing_export_inputs(
    tmp_path, monkeypatch,
):
    connections = []
    connect = sqlite3.connect

    def retained_connection(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", retained_connection)
    named_export.__wrapped__(tmp_path)
    # Strong references prevent garbage collection from hiding an unclosed handle.
    assert connections
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def _items(fixture, session=None):
    export = WechatSessionExport(fixture.store, session or fixture.conversation["id"])
    items = list(export.items())
    assert export.connection is None
    return items


def test_verified_alias_preserves_original_sender_body_and_source_bytes(named_export):
    fixture = named_export
    before = {
        path: path.read_bytes()
        for path in (fixture.history, fixture.registry, fixture.preferences)
    }
    with closing(sqlite3.connect(fixture.history)) as connection, connection:
        connection.row_factory = sqlite3.Row
        expected = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM messages WHERE conversation_id=? "
                "ORDER BY time_sort,ordinal,id",
                (fixture.conversation["id"],),
            )
        ]
    items = _items(fixture)
    assert len(items) == 215
    for item, original in zip(items, expected, strict=True):
        assert {key: item[key] for key in original} == original
        assert item.get("viewer_display_name") == (
            ALIAS if item["sender_id"] == SENDER else None
        )
    body = "".join(
        session_csv_chunks(
            WechatSessionExport(fixture.store, fixture.conversation["id"])
        )
    )
    reader = csv.DictReader(io.StringIO(body.removeprefix("\ufeff"), newline=""))
    assert reader.fieldnames == ["Time", "Speaker ID", "Statement"]
    assert any(row["Speaker ID"] == f"{ALIAS} ({SENDER})" for row in reader)
    assert all(path.read_bytes() == body for path, body in before.items())


def test_same_sender_string_in_unverified_source_does_not_gain_alias(named_export):
    items = _items(named_export, named_export.unknown)
    assert any(item["sender_id"] == SENDER for item in items)
    assert all("viewer_display_name" not in item for item in items)


def test_missing_sender_id_keeps_source_label_without_guessing_alias(named_export):
    fixture = named_export
    with closing(sqlite3.connect(fixture.history)) as connection, connection:
        identifier = connection.execute(
            "SELECT id FROM messages WHERE conversation_id=? ORDER BY ordinal LIMIT 1",
            (fixture.conversation["id"],),
        ).fetchone()[0]
        connection.execute(
            "UPDATE messages SET sender_id=NULL WHERE id=?", (identifier,)
        )
    item = next(item for item in _items(fixture) if item["id"] == identifier)
    assert item["sender_id"] is None
    assert item["sender_label"] == "Same display name"
    assert "viewer_display_name" not in item


def test_empty_preferences_skip_contact_resolution(named_export):
    fixture = named_export
    fixture.preferences.write_text(json.dumps({"schema_version": 1, "contacts": {}}))
    with patch(
        "app.core.wechat_session_export.WechatContactRepository.resolve_messages"
    ) as resolve:
        items = _items(fixture)
    resolve.assert_not_called()
    assert all("viewer_display_name" not in item for item in items)


def test_missing_registry_falls_back_to_source_labels(named_export):
    fixture = named_export
    fixture.registry.unlink()
    assert all("viewer_display_name" not in item for item in _items(fixture))


@pytest.mark.parametrize("changed_source", ["preferences", "registry"])
def test_alias_source_change_stops_before_next_batch(named_export, changed_source):
    fixture = named_export
    export = WechatSessionExport(fixture.store, fixture.conversation["id"])
    items = export.items()
    assert len([next(items) for _ in range(200)]) == 200
    if changed_source == "preferences":
        fixture.preferences.write_text(
            json.dumps(
                {"schema_version": 1, "contacts": {fixture.contact_id: "New name"}}
            )
        )
    else:
        with closing(sqlite3.connect(fixture.registry)) as connection, connection:
            connection.execute(
                "UPDATE contacts SET username='wxid_different' WHERE id=?",
                (fixture.contact_id,),
            )
    with pytest.raises(WechatSessionSourceChanged):
        next(items)
    assert export.connection is None


def test_snapshot_and_room_specific_binding_needs_exact_message_context(named_export):
    fixture = named_export
    with closing(sqlite3.connect(fixture.history)) as connection, connection:
        identifier, raw = connection.execute(
            "SELECT id,provenance FROM messages WHERE conversation_id=? AND sender_id=? "
            "ORDER BY ordinal LIMIT 1",
            (fixture.conversation["id"], SENDER),
        ).fetchone()
        connection.execute(
            "CREATE TABLE source_snapshots(id INTEGER PRIMARY KEY,snapshot_id TEXT,"
            "source_id TEXT,metadata TEXT)"
        )
        connection.execute(
            "INSERT INTO source_snapshots VALUES(17,?,?,?)",
            ("source-snapshot", fixture.conversation["partition_id"], "{}"),
        )
        provenance = json.loads(raw)
        provenance["source_snapshot"] = 17
        connection.execute(
            "UPDATE messages SET provenance=? WHERE id=?",
            (json.dumps(provenance), identifier),
        )
    with closing(sqlite3.connect(fixture.registry)) as connection, connection:
        connection.execute(
            "UPDATE sender_bindings SET source_snapshot='source-snapshot',room_id=?",
            (fixture.conversation["peer_id"],),
        )
    items = _items(fixture)
    assert [item["id"] for item in items if item.get("viewer_display_name")] == [
        identifier
    ]


def _preferences(fixture, state):
    if state == "missing":
        fixture.preferences.unlink()
    elif state == "empty":
        fixture.preferences.write_text(json.dumps({"schema_version": 1, "contacts": {}}))
    else:
        assert state == "alias"


def _profile(fixture, *, remark="Scoped source remark", nickname="Global profile name"):
    with sqlite3.connect(fixture.registry) as connection:
        if remark is not None:
            connection.execute(
                "INSERT INTO contact_observations VALUES(?,?,?,?,?,?,?,?,?)",
                ("scoped-remark", fixture.contact_id,
                 fixture.conversation["partition_id"], "", OWNER,
                 fixture.conversation["peer_id"], "remark", remark, "{}"),
            )
        if nickname is not None:
            connection.execute(
                "INSERT INTO contact_observations VALUES(?,?,?,?,?,?,?,?,?)",
                ("global-nickname", fixture.contact_id, "another-native-source",
                 "retained-snapshot", OWNER, "", "nickname", nickname, "{}"),
            )


def _video_resource(fixture, *, label=SENDER):
    row = list(_reference(fixture.conversation, "[Video]", 17))
    row[3], row[4] = SENDER, label
    provenance = json.loads(row[11])
    provenance["message_type"] = 43
    provenance["resource_reference"]["kind"] = "video"
    row[11] = json.dumps(provenance)
    sidecar = _sidecar(fixture.store, [(fixture.conversation, tuple(row))])
    return row[0], sidecar


def _raw_primary(fixture, *, sender=SENDER, label=SENDER, system=False):
    with sqlite3.connect(fixture.history) as connection:
        identifier, raw = connection.execute(
            "SELECT id,provenance FROM messages WHERE conversation_id=? "
            "AND sender_id=? ORDER BY ordinal LIMIT 1",
            (fixture.conversation["id"], SENDER),
        ).fetchone()
        provenance = json.loads(raw)
        if system:
            provenance.update(message_type=10000, text_extraction_kind="system_text")
        connection.execute(
            "UPDATE messages SET sender_id=?,sender_label=?,provenance=? WHERE id=?",
            (sender, label, json.dumps(provenance), identifier),
        )
    return identifier


@pytest.mark.parametrize("preference_state", ["missing", "empty"])
@pytest.mark.parametrize("source_label", ["", SENDER])
def test_native_resource_name_matches_page_without_local_alias_and_preserves_bytes(
    named_export, preference_state, source_label,
):
    fixture = named_export
    _preferences(fixture, preference_state)
    _profile(fixture)
    identifier, sidecar = _video_resource(fixture, label=source_label)
    paths = [fixture.history, fixture.registry, sidecar]
    if fixture.preferences.exists():
        paths.append(fixture.preferences)
    before = {path: path.read_bytes() for path in paths}
    with sqlite3.connect(sidecar) as connection:
        connection.row_factory = sqlite3.Row
        original = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (identifier,),
            ).fetchone()
        )

    item = next(item for item in _items(fixture) if item["id"] == identifier)
    assert {key: item[key] for key in original} == original
    assert item["viewer_display_name"] == "Scoped source remark"
    page = query_wechat_history(
        fixture.store, session=fixture.conversation["id"], page_size=1000,
    )
    visible = next(item for item in page.items if item.stable_id == identifier)
    assert visible.messaging["viewer_display_name"] == item["viewer_display_name"]
    assert visible.content_text == item["text"] == "[Video]"
    assert visible.last_seen_at == item["timestamp_value"]
    with patch("builtins.open", side_effect=AssertionError("CSV must not read media payloads")):
        body = "".join(
            session_csv_chunks(
                WechatSessionExport(fixture.store, fixture.conversation["id"])
            )
        )
    rows = list(csv.DictReader(io.StringIO(body.removeprefix("\ufeff"), newline="")))
    assert next(row for row in rows if row["Statement"] == "[Video]") == {
        "Time": "2021-08-07 19:00:17 UTC+08:00",
        "Speaker ID": f"Scoped source remark ({SENDER})",
        "Statement": "[Video]",
    }
    assert all(path.read_bytes() == content for path, content in before.items())
    if preference_state == "missing":
        assert not fixture.preferences.exists()


@pytest.mark.parametrize("remark", [None, SENDER])
def test_native_resource_uses_global_display_only_when_preferred_is_missing_or_raw(
    named_export, remark,
):
    fixture = named_export
    _preferences(fixture, "empty")
    _profile(fixture, remark=remark)
    identifier, _ = _video_resource(fixture)
    item = next(item for item in _items(fixture) if item["id"] == identifier)
    assert item["viewer_display_name"] == "Global profile name"
    assert item["sender_label"] == item["sender_id"] == SENDER


def test_explicit_alias_wins_over_scoped_native_remark_and_global_profile(named_export):
    fixture = named_export
    _profile(fixture)
    identifier, _ = _video_resource(fixture)
    item = next(item for item in _items(fixture) if item["id"] == identifier)
    assert item["viewer_display_name"] == ALIAS


@pytest.mark.parametrize(
    "boundary",
    [
        "system", "human_label", "missing_sender", "unregistered_sender",
        "unverified_contact", "raw_names", "owner_partition_mismatch",
    ],
)
def test_native_profile_fallback_requires_original_source_and_verified_identity(
    named_export, boundary,
):
    fixture = named_export
    _preferences(fixture, "empty")
    _profile(
        fixture,
        remark=SENDER if boundary == "raw_names" else "Scoped source remark",
        nickname=SENDER if boundary == "raw_names" else "Global profile name",
    )
    identifier = _raw_primary(
        fixture,
        sender=None if boundary == "missing_sender" else (
            "wxid_unregistered" if boundary == "unregistered_sender" else SENDER
        ),
        label="Original source label" if boundary == "human_label" else (
            "wxid_unregistered" if boundary == "unregistered_sender" else SENDER
        ),
        system=boundary == "system",
    )
    if boundary == "unverified_contact":
        with sqlite3.connect(fixture.registry) as connection:
            connection.execute(
                "UPDATE contacts SET identity_status='unresolved' WHERE id=?",
                (fixture.contact_id,),
            )
    if boundary == "owner_partition_mismatch":
        with sqlite3.connect(fixture.history) as connection:
            connection.execute(
                "UPDATE partitions SET account_id='wxid_other_owner' WHERE id=?",
                (fixture.conversation["partition_id"],),
            )
    item = next(item for item in _items(fixture) if item["id"] == identifier)
    assert "viewer_display_name" not in item


def test_non_native_source_cannot_borrow_same_sender_verified_profile(named_export):
    fixture = named_export
    _preferences(fixture, "missing")
    _profile(fixture)
    with sqlite3.connect(fixture.history) as connection:
        connection.execute(
            "UPDATE messages SET sender_label=sender_id WHERE conversation_id=?",
            (fixture.unknown,),
        )
    with sqlite3.connect(fixture.registry) as connection:
        connection.execute(
            "INSERT INTO sender_bindings VALUES(?,?,?,?,?,?,?)",
            ("xlsx-2", SENDER, "", "", fixture.contact_id,
             "native_message_sender", "{}"),
        )
    with patch(
        "app.core.wechat_session_export.WechatContactRepository.resolve_messages"
    ) as resolve:
        items = _items(fixture, fixture.unknown)
    resolve.assert_not_called()
    assert any(item["sender_id"] == SENDER for item in items)
    assert all("viewer_display_name" not in item for item in items)


@pytest.mark.parametrize("preference_state", ["missing", "empty"])
def test_native_profile_registry_change_stops_next_batch_without_any_alias(
    named_export, preference_state,
):
    fixture = named_export
    _preferences(fixture, preference_state)
    _profile(fixture)
    _video_resource(fixture)
    export = WechatSessionExport(fixture.store, fixture.conversation["id"])
    items = export.items()
    assert len([next(items) for _ in range(200)]) == 200
    with sqlite3.connect(fixture.registry) as connection:
        connection.execute(
            "UPDATE contact_observations SET value='Changed source remark' "
            "WHERE id='scoped-remark'"
        )
    with pytest.raises(WechatSessionSourceChanged):
        next(items)
    assert export.connection is None
