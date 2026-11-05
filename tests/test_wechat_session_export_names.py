"""Session export aliases retain verified source identity and bounded snapshots.

Code version: v1.0.3-codex.0
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
from app.core.wechat_session_export import (
    WechatSessionExport,
    WechatSessionSourceChanged,
)
from app.web.wechat_session_csv import session_csv_chunks
from tests.test_wechat_archive import make_archive


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
