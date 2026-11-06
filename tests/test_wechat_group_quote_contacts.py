"""Group quote identities follow proved original events, not replying authors.

Code version: v1.0.1-codex.0
"""

from copy import deepcopy
import sqlite3

import pytest

from tests import test_wechat_quoted_contacts as fixtures
from tests.test_wechat_contacts import bind, insert_contact, observe


quote_store = fixtures.quote_store
ORIGINAL = "b" * 64


def _proved_item(fixture, *, original_sender=fixtures.TARGET):
    item = fixtures._item(chat=fixtures.OTHER)
    item.messaging["sender_id"] = fixtures.OTHER
    item.messaging["source_observations"] = [
        {
            "id": item.stable_id,
            "source_id": fixtures.SOURCE,
            "source_snapshot_id": fixtures.SNAPSHOT,
            "sender_id": fixtures.OTHER,
            "event_kind": "quoted_reply",
            "quoted_original_id": ORIGINAL,
            "quoted_server_id": "8591339163006592468",
            "quoted_sender_id": original_sender,
            "quoted_contact_id": fixture.identifiers[original_sender],
        }
    ]
    return item


def _assert_sources_preserved(item, before):
    assert item.content_text == before.content_text
    assert item.messaging["provenance"] == before.messaging["provenance"]
    assert (
        item.messaging["source_observations"] == before.messaging["source_observations"]
    )
    for key in ("reference", "text_components", "speaker", "text"):
        assert (
            item.messaging["quoted_reply"][key] == before.messaging["quoted_reply"][key]
        )


def test_group_quote_uses_proved_original_instead_of_current_reply_author(quote_store):
    item = _proved_item(quote_store)
    before = deepcopy(item)
    registry = quote_store.path.read_bytes()
    fixtures._project(quote_store, [item])
    quote = item.messaging["quoted_reply"]
    assert quote["contact"]["id"] == quote_store.identifiers[fixtures.TARGET]
    assert quote["viewer_display_name"] == fixtures.VIEWER
    assert quote["contact"]["id"] != quote_store.identifiers[fixtures.OTHER]
    _assert_sources_preserved(item, before)
    assert quote_store.path.read_bytes() == registry


def test_proved_original_supports_legacy_native_username(quote_store):
    legacy = "legacy_native_username"
    with sqlite3.connect(quote_store.path) as connection:
        identifier = insert_contact(connection, legacy)
        bind(
            connection,
            identifier,
            source=fixtures.SOURCE,
            sender=legacy,
            snapshot=fixtures.SNAPSHOT,
            room=fixtures.ROOM,
        )
        observe(
            connection,
            identifier,
            "nickname",
            "Original legacy participant",
            source=fixtures.SOURCE,
            snapshot=fixtures.SNAPSHOT,
            owner=fixtures.OWNER,
        )
    quote_store.identifiers[legacy] = identifier
    item = _proved_item(quote_store, original_sender=legacy)
    before = deepcopy(item)
    fixtures._project(quote_store, [item])
    quote = item.messaging["quoted_reply"]
    assert quote["contact"]["id"] == identifier
    assert quote["viewer_display_name"] == "Original legacy participant"
    _assert_sources_preserved(item, before)


@pytest.mark.parametrize("chat", [fixtures.TARGET, fixtures.OTHER])
def test_group_room_role_alone_cannot_identify_a_known_member(quote_store, chat):
    item = fixtures._item(chat=chat)
    item.messaging["sender_id"] = fixtures.OTHER
    before = deepcopy(item.messaging)
    fixtures._project(quote_store, [item])
    assert item.messaging == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "c" * 64),
        ("source_id", "ios-other-source"),
        ("source_snapshot_id", "other-native-snapshot"),
        ("sender_id", fixtures.TARGET),
        ("event_kind", "forwarded_text"),
        ("quoted_original_id", "not-a-message-id"),
        ("quoted_original_id", "a" * 64),
        ("quoted_sender_id", fixtures.ROOM),
        ("quoted_contact_id", "wechat:contact:" + "d" * 64),
        ("quoted_server_id", "900000000000000001"),
    ],
)
def test_group_original_proof_must_match_current_source(quote_store, field, value):
    item = _proved_item(quote_store)
    item.messaging["source_observations"][0][field] = value
    before = deepcopy(item.messaging)
    fixtures._project(quote_store, [item])
    assert item.messaging == before


def test_conflicting_original_authors_do_not_select_one(quote_store):
    item = _proved_item(quote_store)
    second = deepcopy(item.messaging["source_observations"][0])
    second.update(
        quoted_original_id="c" * 64,
        quoted_sender_id=fixtures.OTHER,
        quoted_contact_id=quote_store.identifiers[fixtures.OTHER],
    )
    item.messaging["source_observations"].append(second)
    before = deepcopy(item.messaging)
    fixtures._project(quote_store, [item])
    assert item.messaging == before


def test_unregistered_proved_original_does_not_gain_a_contact(quote_store):
    item = _proved_item(quote_store)
    with sqlite3.connect(quote_store.path) as connection:
        connection.execute(
            "DELETE FROM sender_bindings WHERE contact_id=?",
            (quote_store.identifiers[fixtures.TARGET],),
        )
    before = deepcopy(item.messaging)
    fixtures._project(quote_store, [item])
    assert item.messaging == before
