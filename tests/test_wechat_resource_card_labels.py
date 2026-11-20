"""Resource cards retain explicit source labels independently of local payloads.

Code version: v1.0.0-codex.0
"""

from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from unittest.mock import Mock

import pytest

from app.core.wechat_completion_json_codec import encode_json_cell
from app.core.wechat_resources import present_resource, resource_reference
from app.core.wechat_secondary_completions import present_retained_app_cards
from tests.test_wechat_resources import (
    _RenderedMarkup,
    _text,
    resource_macros as resource_macros,
    resource_template as resource_template,
)


@pytest.mark.parametrize("embedded", [False, True])
def test_channels_uses_link_card_layout_with_literal_description_and_publisher(
    resource_macros, embedded
):
    description = "原帖第一行\n<em>原样显示</em>"
    reference = {
        "kind": "app",
        "app_subtype": "51",
        "availability": "metadata_only",
        "safe_title": "当前微信版本不支持展示该内容，请升级至最新版本。",
        "channels_description": description,
        "channels_nickname": "示例发布者",
    }
    resource = present_resource(reference, None)
    parsed = _RenderedMarkup(str(resource_macros.resource_card(resource, embedded=embedded)))
    card = parsed.find(css_class="wechat-resource")
    assert card.attrs["data-wechat-resource-link"] == "true"
    assert card.attrs["data-wechat-resource-platform"] == "WeChat Channels"
    assert _text(parsed.find(css_class="wechat-resource-label wechat-message-text")) == description
    assert _text(parsed.find(css_class="wechat-resource-status")) == "示例发布者 · WeChat Channels"
    assert all(parsed.find(tag=tag) is None for tag in ("a", "em", "img", "button", "iframe"))
    fallback = present_resource(
        {
            key: value for key, value in reference.items()
            if key not in {"channels_description", "channels_nickname"}
        },
        None,
    )
    assert fallback["label"] == "WeChat Channels"


@pytest.mark.parametrize("status", ["file", "folder", "missing"])
def test_indexed_app_card_labels_do_not_change_local_resolution_or_actions(status):
    reference = {
        "kind": "app",
        "app_subtype": "5",
        "safe_title": "A preserved article title",
        "publisher_name": "An explicit public account",
        "app_name": "A separate application",
    }
    before = deepcopy(reference)
    resolver = Mock()
    resolver.resolve.return_value = {
        "status": status,
        "path": Path("/private/synthetic-resource") if status != "missing" else None,
        "label": "Show the preserved resource in Finder",
    }
    resource = present_resource(reference, resolver)
    assert resource["label"] == reference["safe_title"]
    assert resource["publisher_name"] == reference["publisher_name"]
    assert resource["status"] == status
    assert resource["can_reveal"] is (status != "missing")
    assert not resource.get("is_link", False)
    assert reference == before
    resolver.resolve.assert_called_once_with(reference)


def test_indexed_app_forwarding_keeps_its_chat_history_role():
    resolver = Mock()
    resolver.resolve.return_value = {"status": "missing", "path": None, "label": ""}
    resource = present_resource(
        {
            "kind": "app",
            "app_subtype": "19",
            "safe_title": "A forwarding title",
            "publisher_name": "A forwarding application",
        },
        resolver,
    )
    assert resource["label"] == "Chat history"
    assert resource["publisher_name"] == ""


@pytest.fixture
def retained_card_connection():
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.executescript(
            "ATTACH DATABASE ':memory:' AS secondary_completions;"
            "CREATE TABLE secondary_completions.entries("
            "id TEXT PRIMARY KEY, conversation_id TEXT, operation TEXT, "
            "strategy TEXT, source_key TEXT, witness_key TEXT);"
            "CREATE TABLE secondary_completions.witnesses("
            "id TEXT PRIMARY KEY, source_key TEXT, message TEXT);"
            "CREATE TEMP TABLE secondary_completion_active(id TEXT PRIMARY KEY);"
        )
        yield connection


def _retained_app_card(connection, fields, *, active=True):
    row = {
        "id": "retained-app-message",
        "conversation_id": "retained-conversation",
        "sender_id": "wxid_verified_member",
        "text": "[App message]",
        "provenance": json.dumps(
            {
                "server_id": "123456789",
                "unix_timestamp": 1700000000,
                "resource_reference": {
                    "account_id": "wxid_retained_account",
                    "peer_id": "1234@chatroom",
                    "kind": "app",
                    "availability": "metadata_only",
                },
            }
        ),
    }
    witness = {
        "server_id": "123456789",
        "create_time": 1700000000,
        "sender_id": "wxid_verified_member",
        "local_type": (int(fields["app_subtype"]) << 32) | 49,
        "resource": {"kind": "app", **fields},
    }
    connection.execute(
        "INSERT INTO secondary_completions.witnesses VALUES(?,?,?)",
        ("retained-witness", "verified-source", encode_json_cell(json.dumps(witness))),
    )
    connection.execute(
        "INSERT INTO secondary_completions.entries VALUES(?,?,?,?,?,?)",
        (
            row["id"], row["conversation_id"], "replace", "native_sender",
            "verified-source", "retained-witness",
        ),
    )
    if active:
        connection.execute(
            "INSERT INTO temp.secondary_completion_active VALUES(?)", (row["id"],)
        )
    return row


@pytest.mark.parametrize(
    ("fields", "label", "publisher", "platform"),
    [
        (
            {
                "app_subtype": "51",
                "app_metadata_version": "1",
                "safe_title": "Update WeChat to view this card",
                "channels_description": "第一行\n<em>原帖正文</em>",
                "channels_nickname": "原始发布者",
            },
            "第一行\n<em>原帖正文</em>",
            "原始发布者",
            "WeChat Channels",
        ),
        (
            {
                "app_subtype": "5",
                "app_metadata_version": "1",
                "safe_title": "Original article & title",
                "publisher_name": "Original publisher",
                "publisher_name_source": "appmsg/sourcedisplayname",
            },
            "Original article & title",
            "Original publisher",
            "",
        ),
    ],
    ids=["channels", "link"],
)
def test_active_retained_app_details_feed_the_normal_resource_card(
    retained_card_connection, fields, label, publisher, platform
):
    connection = retained_card_connection
    row = _retained_app_card(connection, fields)
    before = deepcopy(row)
    changes = connection.total_changes

    present_retained_app_cards(connection, [row])

    assert row == {**before, "retained_app_card_fields": fields}
    assert connection.total_changes == changes
    proof = json.loads(row["provenance"])
    reference = resource_reference(proof, "wxid_retained_account", "1234@chatroom")
    resource = present_resource({**reference, **row["retained_app_card_fields"]}, None)
    assert resource["label"] == label
    assert resource["publisher_name"] == publisher
    assert resource.get("platform_label", "") == platform
    assert resource["is_link"]
    assert resource["status"] == "metadata_only"
    assert resource["can_reveal"] is False


@pytest.mark.parametrize(
    ("row_fields", "proof_fields", "reference_fields", "active"),
    [
        ({}, {}, {}, False),
        ({"conversation_id": "another-conversation"}, {}, {}, True),
        ({"sender_id": "wxid_other_member"}, {}, {}, True),
        ({}, {"server_id": "234567890"}, {}, True),
        ({}, {"unix_timestamp": 1700000001}, {}, True),
        ({"text": "[Video]"}, {}, {}, True),
        ({}, {}, {"kind": "video"}, True),
        ({}, {}, {"app_subtype": "19"}, True),
        ({}, {}, {"safe_title": "Conflicting original title"}, True),
    ],
    ids=[
        "inactive", "conversation", "sender", "server", "timestamp", "non-app-body",
        "non-app-reference", "subtype-conflict", "label-conflict",
    ],
)
def test_retained_app_details_leave_inactive_or_conflicting_rows_untouched(
    retained_card_connection, row_fields, proof_fields, reference_fields, active
):
    connection = retained_card_connection
    row = _retained_app_card(
        connection,
        {"app_subtype": "5", "safe_title": "A verified title"},
        active=active,
    )
    row.update(row_fields)
    proof = json.loads(row["provenance"])
    proof.update(proof_fields)
    proof["resource_reference"].update(reference_fields)
    row["provenance"] = json.dumps(proof)
    before = deepcopy(row)

    present_retained_app_cards(connection, [row])

    assert row == before
