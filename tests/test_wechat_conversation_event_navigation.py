"""Preferred conversation events share pagination, source jumps, and CSV rows.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core import wechat_conversation_events as events
from app.core.wechat_archive import import_wechat_archive, wechat_archive_path
from app.core.wechat_contacts import contact_registry_path, initialize_contact_registry
from app.core.wechat_display_names import display_names_path
from app.core.wechat_session_export import WechatSessionExport
from tests import test_wechat_conversation_events as event_fixtures
from tests.test_wechat_archive import make_archive
from tests.test_wechat_contacts import bind


event_store = event_fixtures.event_store


@pytest.fixture
def resolved_event_store(tmp_path, monkeypatch):
    store = tmp_path / "store"
    import_wechat_archive(make_archive(tmp_path / "archive", repeated=104), store)
    history = wechat_archive_path(store)
    with sqlite3.connect(history) as connection:
        connection.row_factory = sqlite3.Row
        conversation = dict(
            connection.execute(
                "SELECT * FROM conversations WHERE kind='group' "
                "AND account_key='account:wxid_account_a'",
            ).fetchone()
        )
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM messages WHERE conversation_id=? "
                "ORDER BY time_sort,ordinal,id",
                (conversation["id"],),
            )
        ]
        hidden, preferred = rows[0]["id"], rows[1]["id"]
        duplicate = rows[1]
        connection.execute(
            "UPDATE messages SET sender_id=?,sender_label=?,direction=?,text=?,"
            "search_text=?,timestamp_value=?,timestamp_label=?,time_sort=? WHERE id=?",
            tuple(
                duplicate[key]
                for key in (
                    "sender_id",
                    "sender_label",
                    "direction",
                    "text",
                    "search_text",
                    "timestamp_value",
                    "timestamp_label",
                    "time_sort",
                )
            )
            + (hidden,),
        )
    registry = contact_registry_path(store)
    with sqlite3.connect(registry) as connection:
        initialize_contact_registry(connection)

    def projection(repository, connection, session, current, message_table, identity, *, persist=True):
        assert session == conversation["id"]
        return {
            "status": "ready",
            "preferred_by_id": {hidden: preferred},
            "members_by_preferred": {preferred: [{"id": hidden}, {"id": preferred}]},
            "original_count": 104,
            "display_count": 103,
            "count_delta": 1,
        }

    monkeypatch.setattr(events, "conversation_events", projection)
    return SimpleNamespace(
        store=store,
        history=history,
        registry=registry,
        conversation=conversation,
        hidden=hidden,
        preferred=preferred,
        boundary=rows[100]["id"],
    )


def test_source_jump_maps_hidden_id_to_preferred_and_uses_display_page(
    resolved_event_store,
):
    fixture = resolved_event_store
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    hidden = analysis.message_source_location(fixture.store, fixture.hidden)
    preferred = analysis.message_source_location(fixture.store, fixture.preferred)
    assert hidden == preferred
    assert hidden["id"] == hidden["parent_message_id"] == fixture.preferred
    assert hidden["anchor_id"] == "wechat_message_" + fixture.preferred
    boundary = analysis.message_source_location(fixture.store, fixture.boundary)
    assert boundary["page"] == boundary["message_page"] == 1
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def test_session_export_uses_the_same_preferred_membership_without_source_writes(
    resolved_event_store,
):
    fixture = resolved_event_store
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    export = WechatSessionExport(fixture.store, fixture.conversation["id"])
    assert export.conversation["message_count"] == 103
    rows = list(export.items())
    identifiers = [row["id"] for row in rows]
    assert len(rows) == 103
    assert fixture.hidden not in identifiers
    assert identifiers.count(fixture.preferred) == 1
    assert identifiers[0] == fixture.preferred
    assert export.connection is None
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def test_session_export_pins_contact_evidence_used_by_event_projection(
    resolved_event_store,
):
    fixture = resolved_event_store
    export = WechatSessionExport(fixture.store, fixture.conversation["id"])
    with sqlite3.connect(fixture.registry) as connection:
        connection.execute(
            "INSERT INTO contact_registry_info(key,value) VALUES('changed','after-preflight')"
        )
    from app.core.wechat_session_export import WechatSessionSourceChanged

    with pytest.raises(WechatSessionSourceChanged):
        next(export.items())
    assert export.connection is None


def test_verified_html_native_events_share_source_page_and_export_rows(event_store):
    fixture = event_store
    before = fixture.repository.path.read_bytes(), fixture.registry.read_bytes()
    old, preferred = fixture.ids[-1]
    location = analysis.message_source_location(fixture.store, old, page_size=2)
    assert location == analysis.message_source_location(
        fixture.store, preferred, page_size=2
    )
    assert location["parent_message_id"] == preferred
    assert location["anchor_id"] == "wechat_message_" + preferred
    assert location["page"] == 2
    export = WechatSessionExport(fixture.store, event_fixtures.SESSION)
    assert export.conversation["message_count"] == 3
    rows = list(export.items())
    assert [row["id"] for row in rows] == [pair[1] for pair in fixture.ids]
    assert before == (
        fixture.repository.path.read_bytes(),
        fixture.registry.read_bytes(),
    )


def test_preferred_routed_native_export_resolves_names_in_its_proven_source(
    event_store,
):
    fixture = event_store
    with sqlite3.connect(fixture.registry) as connection:
        identifier = connection.execute(
            "SELECT id FROM contacts WHERE username=?",
            (event_fixtures.PEER,),
        ).fetchone()[0]
        bind(
            connection,
            identifier,
            source=event_fixtures.NATIVE,
            sender=event_fixtures.PEER,
        )
    preferences = display_names_path(fixture.store)
    preferences.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "contacts": {identifier: "Preferred verified peer"},
            }
        )
    )
    before = fixture.repository.path.read_bytes(), fixture.registry.read_bytes()
    rows = list(WechatSessionExport(fixture.store, event_fixtures.SESSION).items())
    peer_rows = [row for row in rows if row["sender_id"] == event_fixtures.PEER]
    assert len(peer_rows) == 2
    assert all(
        row.get("viewer_display_name") == "Preferred verified peer" for row in peer_rows
    )
    assert before == (
        fixture.repository.path.read_bytes(),
        fixture.registry.read_bytes(),
    )
