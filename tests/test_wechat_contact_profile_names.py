"""Contact headings prefer retained readable names over hash-ordered identifiers.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.core.wechat_contact_analysis import _contact
from app.core.wechat_contacts import (
    MAX_OBSERVATIONS,
    canonical_contact_id,
    contact_registry_path,
    initialize_contact_registry,
)
from app.core.wechat_display_names import display_names_path


USERNAME = "wxid_profile_subject"
CONTACT_ID = canonical_contact_id(USERNAME, authority="native_contact_username")


@pytest.fixture
def profile_store(tmp_path):
    store = tmp_path / "store"
    registry = contact_registry_path(store)
    registry.parent.mkdir(parents=True)
    with sqlite3.connect(registry) as connection:
        initialize_contact_registry(connection)
        connection.execute(
            "INSERT INTO contacts VALUES(?,?,?,?,?,?)",
            (CONTACT_ID, "wechat", "", USERNAME, "wechat_account", "verified"),
        )
    return store, registry


def _observe(registry, identifier, kind, value, *, contact_id=CONTACT_ID):
    with sqlite3.connect(registry) as connection:
        connection.execute(
            "INSERT INTO contact_observations VALUES(?,?,?,?,?,?,?,?,?)",
            (identifier, contact_id, "native-fixture", "", "", "", kind, value, "{}"),
        )


def test_heading_uses_nickname_after_raw_identifier_and_keeps_all_evidence(profile_store):
    store, registry = profile_store
    _observe(registry, "a", "source_sender_label", USERNAME)
    _observe(registry, "b", "remark", "A viewer's remark")
    _observe(registry, "c", "alias", "readable-alias")
    _observe(registry, "d", "nickname", "Readable Contact")
    before = registry.read_bytes()

    profile = _contact(store, CONTACT_ID, registry)

    assert profile["display_name"] == "Readable Contact"
    assert profile["id"] == CONTACT_ID and profile["username"] == USERNAME
    assert profile["observation_count"] == 4
    assert {item["value"] for item in profile["names"]} == {
        USERNAME, "A viewer's remark", "readable-alias", "Readable Contact"
    }
    assert registry.read_bytes() == before


def test_heading_finds_nickname_beyond_bounded_observation_page(profile_store):
    store, registry = profile_store
    for index in range(MAX_OBSERVATIONS):
        _observe(registry, f"a{index:03}", "source_sender_label", USERNAME)
    _observe(registry, "z", "nickname", "Readable Contact")

    profile = _contact(store, CONTACT_ID, registry)

    assert len(profile["names"]) == MAX_OBSERVATIONS
    assert profile["observation_count"] == MAX_OBSERVATIONS + 1
    assert profile["display_name"] == "Readable Contact"


def test_nickname_frequency_and_stable_ties_ignore_other_contacts(profile_store):
    store, registry = profile_store
    _observe(registry, "a", "nickname", "Zulu")
    _observe(registry, "b", "nickname", "Zulu")
    _observe(registry, "c", "nickname", "Alpha")
    _observe(registry, "d", "nickname", "A different identity", contact_id="other")
    assert _contact(store, CONTACT_ID, registry)["display_name"] == "Zulu"
    _observe(registry, "e", "nickname", "Alpha")
    assert _contact(store, CONTACT_ID, registry)["display_name"] == "Alpha"


def test_display_preference_still_overrides_retained_nicknames(profile_store):
    store, registry = profile_store
    _observe(registry, "a", "nickname", "Readable Contact")
    display_names_path(store).write_text(
        json.dumps({"schema_version": 1, "contacts": {CONTACT_ID: "My chosen name"}})
    )
    assert _contact(store, CONTACT_ID, registry)["display_name"] == "My chosen name"


@pytest.mark.parametrize("readable", [None, "A retained source label"])
def test_identifier_is_only_a_fallback_when_no_readable_name_exists(profile_store, readable):
    store, registry = profile_store
    _observe(registry, "a", "source_sender_label", USERNAME)
    if readable:
        _observe(registry, "b", "source_sender_label", readable)
    assert _contact(store, CONTACT_ID, registry)["display_name"] == (readable or USERNAME)
