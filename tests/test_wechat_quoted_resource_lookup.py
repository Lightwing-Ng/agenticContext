"""Exercise bounded native quote lookups without opening source or media files.

Code version: v1.0.0-codex.0
"""

import json
import sqlite3

import pytest

from app.core.wechat_archive import WechatArchiveRepository
from app.core.wechat_resources import present_resource


@pytest.fixture
def catalog():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("ATTACH DATABASE ':memory:' AS resource_index")
    connection.execute(
        "CREATE TABLE resource_index.messages(id TEXT,conversation_id TEXT,provenance TEXT)"
    )
    yield connection
    connection.close()


def _reply(server="42"):
    return {
        "provenance": json.dumps(
            {
                "message_type": 49,
                "text_extraction_kind": "quoted_reply",
                "quoted_reference": {"source_ids": {"svrid": server}},
            }
        )
    }


def _accepted(connection):
    connection.execute("CREATE TEMP VIEW timeline_messages AS SELECT 1")


def _row(connection, identifier, server="42", conversation="room", **proof):
    connection.execute(
        "INSERT INTO resource_index.messages VALUES(?,?,?)",
        (identifier, conversation, json.dumps({"server_id": server, **proof})),
    )


def test_attached_catalog_without_accepted_timeline_is_not_read(catalog):
    _row(catalog, "original")
    assert WechatArchiveRepository._quoted_resource_originals(
        catalog, [_reply()], "room"
    ) == []


def test_lookup_is_exactly_conversation_and_server_scoped(catalog):
    _accepted(catalog)
    _row(catalog, "original")
    _row(catalog, "another-room", conversation="other")
    _row(catalog, "another-server", server="43")
    before = catalog.total_changes
    result = WechatArchiveRepository._quoted_resource_originals(
        catalog, [_reply()], "room"
    )
    assert [row["id"] for row in result] == ["original"]
    assert catalog.total_changes == before


def test_conflicting_candidates_remain_distinct_for_reference_proof(catalog):
    _accepted(catalog)
    _row(catalog, "first", message_type=3)
    _row(catalog, "second", message_type=49)
    result = WechatArchiveRepository._quoted_resource_originals(
        catalog, [_reply()], "room"
    )
    assert [row["id"] for row in result] == ["first", "second"]


@pytest.mark.parametrize("server", [None, True, 42, "0", "-42", "042", "9223372036854775808"])
def test_unusable_reference_identifiers_are_not_coerced(catalog, server):
    _accepted(catalog)
    _row(catalog, "original", server=server)
    assert WechatArchiveRepository._quoted_resource_originals(
        catalog, [_reply(server)], "room"
    ) == []


def test_malformed_or_ordinary_records_do_not_request_a_quote(catalog):
    _accepted(catalog)
    _row(catalog, "original")
    messages = [
        {"provenance": "null"},
        {"provenance": "{"},
        {"provenance": json.dumps({"message_type": 1})},
        {"provenance": json.dumps({"quoted_reference": {"source_ids": None}})},
    ]
    assert WechatArchiveRepository._quoted_resource_originals(
        catalog, messages, "room"
    ) == []


def test_oversized_page_is_refused(catalog):
    _accepted(catalog)
    assert WechatArchiveRepository._quoted_resource_originals(
        catalog, [_reply()] * 1001, "room"
    ) == []


def test_oversized_candidate_set_is_refused_without_choosing_one(catalog):
    _accepted(catalog)
    for number in range(1001):
        _row(catalog, str(number))
    assert WechatArchiveRepository._quoted_resource_originals(
        catalog, [_reply()], "room"
    ) == []


def test_oversized_metadata_is_not_returned(catalog):
    _accepted(catalog)
    _row(catalog, "large", title="x" * (8 * 1024**2))
    assert WechatArchiveRepository._quoted_resource_originals(
        catalog, [_reply()], "room"
    ) == []


def test_source_app_title_is_literal_metadata_without_a_resolver():
    title = 'A <script>literal</script> title\nSecond line'
    resource = present_resource(
        {"kind": "app", "availability": "metadata_only", "safe_title": title}, None
    )
    assert resource["label"] == title
    assert resource["kind_label"] == "App message"
    assert resource["can_reveal"] is False
    assert resource["status"] == "metadata_only"


@pytest.mark.parametrize("title", ["", "   ", "x\x00y", "x" * 1025, None, 42])
def test_unusable_app_title_keeps_the_known_type(title):
    resource = present_resource(
        {"kind": "app", "availability": "metadata_only", "safe_title": title}, None
    )
    assert resource["label"] == "App message"


def test_confirmed_native_forward_card_uses_chat_history_caption():
    resource = present_resource(
        {
            "kind": "app",
            "availability": "metadata_only",
            "safe_title": "An original card title",
            "app_subtype": "19",
        },
        None,
    )
    assert resource["label"] == "Chat history"
