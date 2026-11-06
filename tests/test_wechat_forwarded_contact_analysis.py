"""Analyze verified embedded authors without attributing their words to forwarders.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core.wechat_contacts import (
    WechatContactRepository,
    canonical_contact_id,
    contact_registry_path,
    initialize_contact_registry,
)
from app.core.wechat_export_merge import _json, _record
from tests.test_wechat_contacts import _make_forwarded_html_store, bind, insert_contact
from tests.test_wechat_html_forwarded import _render


TARGET = "wxid_forwarded"
OTHER = "wxid_forwarded_namesake"
OUTER = "wxid_forwarding_contact"
CID = canonical_contact_id(TARGET, authority="native_contact_username")
OTHER_CID = canonical_contact_id(OTHER, authority="native_contact_username")
OUTER_CID = canonical_contact_id(OUTER, authority="native_contact_username")
FIRST = "TARGET-FIRST-unique original statement"
NESTED = "TARGET-NESTED-own reply only"
UNKNOWN = "TARGET-UNKNOWN-own original clock unavailable"
QUOTED = "QUOTED-ONLY-other person's words"
DESCENDANT = "DESCENDANT-ONLY-another person's nested words"
NAMESAKE = "NAMESAKE-ONLY-same label different native identity"


def _component(fixture, number, text, *, native=TARGET, **updates):
    value = deepcopy(fixture["request"]["forwarded_components"][0])
    value.update(
        export_msgid=f"42_component_{number}",
        component_ordinal=number,
        ordinal=number + 1,
        sender_native_id=native,
        sender_label="Shared exported name",
        avatar_path=f"Room_files/Portrait/{native}.jpg",
        text=text,
        text_kind="forwarded_rendered_text",
        forwarded_components=[],
        source_locator={
            **value["source_locator"],
            "message_ordinal": number + 1,
            "page_message_ordinal": number + 1,
        },
    )
    value.update(updates)
    return value


def _publish_tree(fixture, components, *, primary=None, kind="forwarded_text"):
    """Publish the documented export envelope, retaining real source-proof receipts."""
    if primary is None:
        primary = "\n\n".join(child["text"] for child in components)
    source = {
        "text": primary,
        "text_kind": kind,
        "quoted_components": [],
        "forwarded_components": components,
    }
    text = _render(source)
    request = fixture["request"]
    with sqlite3.connect(fixture["history"]) as connection:
        provenance = json.loads(
            connection.execute(
                "SELECT provenance FROM messages WHERE id=?", (request["id"],)
            ).fetchone()[0]
        )
        provenance.update(
            forwarded_components=components,
            quoted_components=[],
            export_msgid_namespace="MesLocalID",
            text_extraction_kind=kind,
            primary_text_sha256=hashlib.sha256(primary.encode()).hexdigest(),
        )
        connection.execute(
            "UPDATE messages SET text=?,search_text=?,provenance=?,sender_id=? WHERE id=?",
            (text, text.casefold(), json.dumps(provenance), OUTER, request["id"]),
        )
    request.update(
        forwarded_components=components,
        sender_id=OUTER,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )


@pytest.fixture
def forwarded_activity_store(tmp_path):
    registry = contact_registry_path(tmp_path)
    registry.parent.mkdir(parents=True)
    with sqlite3.connect(registry) as connection:
        initialize_contact_registry(connection)
        for native in (TARGET, OTHER, OUTER):
            insert_contact(connection, native)
    fixture = _make_forwarded_html_store(tmp_path)
    with sqlite3.connect(registry) as connection:
        bind(
            connection,
            OUTER_CID,
            source=fixture["request"]["source_id"],
            sender=OUTER,
            authority="native_contact_username",
        )
    descendant = _component(fixture, 6, DESCENDANT, native=OTHER)
    nested = _component(
        fixture,
        4,
        NESTED,
        text_kind="quoted_reply",
        raw_timestamp="2020-01-02 03:04:05",
        quoted_components=[{"text": QUOTED}],
        forwarded_components=[descendant],
    )
    unknown = _component(
        fixture,
        5,
        UNKNOWN,
        raw_timestamp="Unknown original time",
        flags=["unrecognized_export_timestamp"],
    )
    system = _component(
        fixture, 7, "SYSTEM-ONLY-tickled a contact", text_kind="system_text"
    )
    wrapper = _component(
        fixture,
        3,
        "Forwarded conversation container",
        native=OTHER,
        text_kind="forwarded_text",
        forwarded_components=[nested, unknown, system],
    )
    _publish_tree(
        fixture,
        [
            _component(fixture, 1, FIRST),
            _component(fixture, 2, NAMESAKE, native=OTHER),
            wrapper,
        ],
    )
    return fixture


def _by_path(page):
    return {tuple(item["component_path"]): item for item in page["items"]}


def test_verified_embedded_authors_keep_nested_scope_and_original_source_bytes(
    forwarded_activity_store,
):
    fixture = forwarded_activity_store
    registry = contact_registry_path(fixture["store"])
    before = (fixture["history"].read_bytes(), registry.read_bytes())
    proof = WechatContactRepository(fixture["store"]).resolve_forwarded_components(
        [fixture["request"]]
    )[fixture["request"]["id"]]
    assert proof["status"] == "source_proof_verified"
    assert proof["components"][0]["contact"]["id"] == CID
    page = analysis.query_contact_analysis(fixture["store"], CID)
    assert page["status"] == "ready"
    assert page["counts"] == {
        "messages": 3,
        "conversations": 1,
        "source_accounts": 1,
        "sources": 1,
    }
    rows = _by_path(page)
    assert {path: row["content_text"] for path, row in rows.items()} == {
        (1,): FIRST,
        (3, 1): NESTED,
        (3, 2): UNKNOWN,
    }
    parent = fixture["request"]
    for path, row in rows.items():
        key = parent["id"] + "".join(f"_forwarded_{position}" for position in path)
        assert row["id"] == key and row["anchor_id"] == "wechat_message_" + key
        assert row["parent_message_id"] == parent["id"]
        assert row["is_embedded"] is True and row["contact_id"] == CID
        assert row["source_id"] == parent["source_id"]
        assert row["conversation_id"] == parent["conversation_id"]
        assert row["account_id"] == parent["account_id"]
        assert row["source_file"] == "Synthetic.zip"
        assert row["source_snapshot_id"] == parent["source_snapshot_id"]
    assert before == (fixture["history"].read_bytes(), registry.read_bytes())


def test_other_forwarder_and_same_label_contact_do_not_inherit_target_words(
    forwarded_activity_store,
):
    fixture = forwarded_activity_store
    resolver = WechatContactRepository(fixture["store"])
    parent = fixture["request"]
    assert resolver.resolve_messages([parent])[parent["id"]]["id"] == OUTER_CID
    outer = analysis.query_contact_analysis(fixture["store"], OUTER_CID)
    assert outer["total_count"] == 0 and outer["items"] == []
    namesake = analysis.query_contact_analysis(fixture["store"], OTHER_CID)
    assert {row["content_text"] for row in namesake["items"]} == {
        NAMESAKE,
        DESCENDANT,
    }
    assert set(_by_path(namesake)) == {(2,), (3, 1, 1)}


def test_embedded_clock_is_original_and_unknown_clock_never_borrows_outer_time(
    forwarded_activity_store,
):
    fixture = forwarded_activity_store
    rows = _by_path(analysis.query_contact_analysis(fixture["store"], CID))
    assert rows[(1,)]["timestamp_value"] == "2021-08-08T12:34:56"
    assert rows[(1,)]["timestamp_label"] == "8 Aug 2021 12:34:56"
    assert rows[(3, 1)]["timestamp_value"] == "2020-01-02T03:04:05"
    assert rows[(3, 1)]["source_clock"] == "2020-01-02 03:04:05"
    assert rows[(3, 1)]["time_basis"] == "Source clock; timezone unknown"
    unknown = rows[(3, 2)]
    assert unknown["source_clock"] == "Unknown original time"
    assert unknown["timestamp_value"] == unknown["timestamp_label"] == ""
    assert unknown["timestamp_value"] != fixture["request"]["timestamp_value"]
    assert unknown["time_basis"] != "Source clock; timezone unknown"


def test_warm_queries_search_only_own_words_without_rescanning_or_copying_bodies(
    forwarded_activity_store, monkeypatch
):
    fixture = forwarded_activity_store
    initial = analysis.query_contact_analysis(fixture["store"], CID, page_size=1)
    assert initial["total_count"] == initial["total_pages"] == 3

    def fail_scan(*_args, **_kwargs):
        raise AssertionError("Warm contact queries must use parent PK references.")

    monkeypatch.setattr(analysis, "_candidate_batches", fail_scan)
    monkeypatch.setattr(analysis, "_candidate_forwarded_batches", fail_scan)
    monkeypatch.setattr(
        WechatContactRepository, "resolve_forwarded_components", fail_scan
    )
    second = analysis.query_contact_analysis(fixture["store"], CID, page=2, page_size=1)
    assert initial["items"][0]["id"] != second["items"][0]["id"]
    own = analysis.query_contact_analysis(fixture["store"], CID, query="target-nested")
    assert [row["content_text"] for row in own["items"]] == [NESTED]
    for query in (QUOTED, DESCENDANT, NAMESAKE, "Forwarded conversation container"):
        empty = analysis.query_contact_analysis(fixture["store"], CID, query=query)
        assert empty["total_count"] == 0 and empty["items"] == []
    cache_path = analysis.contact_analysis_cache_path(fixture["store"], CID)
    with sqlite3.connect(cache_path) as cache:
        fields = {row[1] for row in cache.execute("PRAGMA table_info(refs)")}
        assert not {"text", "content_text", "search_text", "provenance"} & fields
        assert {"parent_message_id", "component_path", "anchor_id"} <= fields
        assert cache.execute("SELECT COUNT(*) FROM refs").fetchone()[0] == 3
        dump = "\n".join(cache.iterdump())
    for body in (FIRST, NESTED, UNKNOWN, QUOTED, DESCENDANT, NAMESAKE):
        assert body not in dump and body.encode() not in cache_path.read_bytes()


@pytest.mark.parametrize(
    "change", ["unverified_owner", "wrong_archive", "wrong_snapshot"]
)
def test_changed_source_receipt_invalidates_cached_embedded_author_proof(
    forwarded_activity_store, change
):
    fixture = forwarded_activity_store
    before = analysis.query_contact_analysis(fixture["store"], CID)
    assert before["total_count"] == 3
    with sqlite3.connect(fixture["history"]) as connection:
        metadata = json.loads(
            connection.execute(
                "SELECT metadata FROM source_snapshots LIMIT 1"
            ).fetchone()[0]
        )
        if change == "unverified_owner":
            metadata["owner_verified"] = False
        elif change == "wrong_archive":
            metadata["source_sha256"] = "0" * 64
        else:
            connection.execute("UPDATE source_snapshots SET snapshot_id=?", ("0" * 64,))
        connection.execute(
            "UPDATE source_snapshots SET metadata=?", (json.dumps(metadata),)
        )
    after = analysis.query_contact_analysis(fixture["store"], CID)
    assert after["source_fingerprint"] != before["source_fingerprint"]
    assert after["total_count"] == 0 and after["items"] == []
    assert (
        analysis.message_source_location(fixture["store"], before["items"][0]["id"])
        is None
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"text_kind": "system_text"},
        {"text_kind": "notice"},
        {"text_kind": "resource_placeholder"},
        {"text_kind": "rendered_app_text", "resource_reference": {"kind": "image"}},
        {"flags": ["possible_exporter_media_placeholder"]},
        {"type": "3"},
    ],
)
def test_system_and_media_placeholders_are_not_contact_statements(
    forwarded_activity_store, updates
):
    fixture = forwarded_activity_store
    _publish_tree(
        fixture, [_component(fixture, 1, "Not an authored text statement", **updates)]
    )
    page = analysis.query_contact_analysis(fixture["store"], CID)
    assert page["total_count"] == 0 and page["items"] == []


def test_plain_text_photo_token_remains_an_authored_statement_and_search_hit(
    forwarded_activity_store,
):
    fixture = forwarded_activity_store
    _publish_tree(
        fixture,
        [_component(fixture, 1, "[Photo]", type="1", text_kind="plain_text")],
    )
    page = analysis.query_contact_analysis(fixture["store"], CID)
    assert page["total_count"] == 1
    assert page["items"][0]["content_text"] == "[Photo]"
    found = analysis.query_contact_analysis(fixture["store"], CID, query="[photo]")
    assert found["total_count"] == 1
    assert found["items"][0]["id"] == page["items"][0]["id"]


@pytest.mark.parametrize(
    "kind,authored",
    [
        ("plain_text", True),
        ("forwarded_rendered_text", True),
        ("rendered_app_text", False),
        ("forwarded_text", False),
    ],
)
def test_nested_own_caption_is_distinct_from_a_forwarding_container_title(
    forwarded_activity_store, kind, authored
):
    fixture = forwarded_activity_store
    caption = "CAPTION-ONLY-target's own words"
    child = _component(fixture, 2, DESCENDANT, native=OTHER)
    _publish_tree(
        fixture,
        [_component(fixture, 1, caption, text_kind=kind, forwarded_components=[child])],
    )
    page = analysis.query_contact_analysis(fixture["store"], CID)
    assert page["total_count"] == int(authored)
    assert [row["content_text"] for row in page["items"]] == (
        [caption] if authored else []
    )
    assert (
        analysis.query_contact_analysis(fixture["store"], CID, query=DESCENDANT)[
            "total_count"
        ]
        == 0
    )


@pytest.mark.parametrize("kind", ["plain_text", "forwarded_rendered_text"])
def test_outer_own_caption_keeps_its_author_without_acquiring_embedded_words(
    forwarded_activity_store, kind
):
    fixture = forwarded_activity_store
    caption = "OUTER-CAPTION-original forwarding contact's own words"
    _publish_tree(
        fixture,
        [_component(fixture, 1, FIRST)],
        primary=caption,
        kind=kind,
    )
    outer = analysis.query_contact_analysis(fixture["store"], OUTER_CID)
    assert [row["content_text"] for row in outer["items"]] == [caption]
    assert outer["items"][0]["is_embedded"] is False
    assert (
        analysis.query_contact_analysis(fixture["store"], OUTER_CID, query=FIRST)[
            "total_count"
        ]
        == 0
    )
    target = analysis.query_contact_analysis(fixture["store"], CID)
    assert [row["content_text"] for row in target["items"]] == [FIRST]


def test_source_location_uses_parent_page_and_exact_nested_anchor(
    forwarded_activity_store,
):
    fixture = forwarded_activity_store
    request = fixture["request"]
    source = fixture["source"]
    records = []
    for position in range(1, 4):
        locator = {
            **request["source_locator"],
            "message_ordinal": position + 10,
            "page_message_ordinal": position + 10,
            "export_msgid": str(position + 10),
        }
        record_id = hashlib.sha256(
            (
                "html-text-v1\0" + source["source_sha256"] + "\0" + _json(locator)
            ).encode()
        ).hexdigest()
        records.append(
            {
                "extraction_record_id": record_id,
                "conversation_key": request["peer_id"],
                "peer_id": request["peer_id"],
                "title": "Synthetic room",
                "kind": "group",
                "sender_id": OUTER,
                "sender_label": "Forwarding contact",
                "direction": "received",
                "text": f"Source pagination padding {position}",
                "sent_at_raw": f"2021-08-08 12:00:0{position}",
                "provenance": {
                    "locator": locator,
                    "text_extraction_kind": "plain_text",
                },
                "flags": [],
            }
        )
    with sqlite3.connect(fixture["history"]) as connection:
        snapshot = connection.execute(
            "SELECT id FROM source_snapshots LIMIT 1"
        ).fetchone()[0]
        for position, record in enumerate(records, start=2):
            row, _, _, _ = _record(record, request["source_id"], snapshot, position)
            connection.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row
            )
    page = analysis.query_contact_analysis(fixture["store"], CID)
    child = _by_path(page)[(3, 1)]
    location = analysis.message_source_location(
        fixture["store"], child["id"], page_size=2
    )
    parent = analysis.message_source_location(
        fixture["store"], request["id"], page_size=2
    )
    assert parent["page"] == location["page"] == location["message_page"] == 2
    assert location["id"] == request["id"]
    assert location["conversation_id"] == request["conversation_id"]
    assert location["anchor_id"] == child["anchor_id"]
    assert location["component_path"] == [3, 1]
    for invalid in (
        request["id"] + "_forwarded_999",
        request["id"] + "_forwarded_3_forwarded_999",
        request["id"] + "_quote_forwarded_1",
    ):
        assert analysis.message_source_location(fixture["store"], invalid) is None
