"""Subtype-5 link labels retain direct source roles and native source pins.

Code version: v1.0.0-codex.0
"""

from copy import deepcopy
import hashlib
import sqlite3

import pytest

from app.core import wechat_mac_refresh_projection as projection
from app.core import wechat_macos_recovery_bank as bank
from app.core.wechat_history_browser import query_wechat_history
from app.core.wechat_resource_index import _APP_LABEL_FIELDS, _app_metadata
from tests import test_wechat_macos_recovery_bank as fixtures


mac_archive = fixtures.mac_archive
bank_archive = fixtures.bank_archive
OWNER = fixtures.OWNER
SENDER = fixtures.MEMBER
BODY = (
    "<msg><appmsg><type>5</type><title>Literal &amp; source title</title>"
    "<sourcedisplayname>Original publisher</sourcedisplayname>"
    "<url>https://example.invalid/private-article</url>"
    "<aeskey>opaque-secret-key</aeskey></appmsg>"
    "<appinfo><appname>Source application</appname></appinfo></msg>"
)


def _project(raw=BODY, *, kind="group", subtype=5):
    return projection._project_message(
        {
            "local_id": 57,
            "local_type": (subtype << 32) | 49,
            "server_id": 557,
            "create_time": 1_700_000_057,
            "real_sender_id": 3,
            "message_content": raw,
            "compression_marker": 0,
        },
        OWNER,
        kind,
        {3: {"rowid": 3, "user_name": SENDER, "is_session": 0}},
    )


@pytest.mark.parametrize("kind", ["direct", "group"])
def test_link_projection_keeps_leaf_labels_without_changing_original_body_pin(kind):
    raw = SENDER + ":\n" + BODY if kind == "group" else BODY
    message = _project(raw, kind=kind)
    assert message["text"] == "[App message]"
    assert message["resource"] == {
        "kind": "app",
        "label": "[App message]",
        "native_time": 1_700_000_057,
        "app_subtype": "5",
        "safe_title": "Literal & source title",
        "publisher_name": "Original publisher",
        "app_name": "Source application",
        "app_metadata_version": "1",
    }
    assert message["content_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert message["sender_id"] == SENDER
    assert "native_text" not in message


@pytest.mark.parametrize(
    "payload",
    [
        "<msg><appmsg><type>5</type><title><b>Nested title</b></title></appmsg></msg>",
        "<msg><appmsg><type>5</type><title>A</title><title>B</title></appmsg></msg>",
        "<msg><appmsg><type>5</type><title>" + "x" * 1025 + "</title></appmsg></msg>",
        "<msg><appmsg><type>19</type><title>Chat history</title>"
        "<sourcedisplayname>Not an article publisher</sourcedisplayname></appmsg></msg>",
        "<msg><appmsg><type>5</type><type>19</type><title>Ambiguous</title></appmsg></msg>",
        "<msg><appmsg><type>5</type><url>https://example.invalid/source</url>"
        "<sourceusername>Not a publisher name</sourceusername></appmsg></msg>",
        "<msg><appmsg><type>5</type></appmsg><appinfo><appname>A</appname>"
        "<appname>B</appname></appinfo></msg>",
        "<!DOCTYPE msg [<!ENTITY source 'untrusted'>]><msg><appmsg><type>5</type>"
        "<title>&source;</title></appmsg></msg>",
        "<msg><appmsg><type>5</type><title>Broken",
    ],
)
def test_ambiguous_non_link_or_unsafe_source_does_not_invent_labels(payload):
    metadata = _app_metadata(payload, False, False)
    assert not set(_APP_LABEL_FIELDS) & metadata.keys()
    assert "app_metadata_version" not in metadata


def test_native_forwarded_conversation_subtype_does_not_acquire_link_labels():
    resource = _project(subtype=19)["resource"]
    assert resource["app_subtype"] == "19"
    assert not set(_APP_LABEL_FIELDS) & resource.keys()
    assert "app_metadata_version" not in resource


def test_link_labels_survive_bank_publication_and_presentation_without_rewriting_history(
    bank_archive,
):
    fixture = bank_archive
    original = fixture.history.read_bytes()
    packet = deepcopy(fixture.group)
    packet["messages"].append(_project())
    fixtures._publish(fixture, packets=[packet], name="link-candidate")
    page = query_wechat_history(
        fixture.root, session=fixture.group_session, persist_event_cache=False
    )
    item = next(
        item
        for item in page.items
        if item.messaging["provenance"].get("macos_recovery", {}).get("local_id") == 57
    )
    assert item.messaging["resource"]["label"] == "Literal & source title"
    assert item.messaging["resource"]["publisher_name"] == "Original publisher"
    assert item.messaging["resource"]["is_link"] is True
    assert item.messaging["resource"]["can_reveal"] is False
    assert item.messaging["resource"]["status"] == "metadata_only"
    assert fixture.history.read_bytes() == original


@pytest.mark.parametrize(
    "damage",
    [
        "nested",
        "oversized",
        "control",
        "empty",
        "version",
        "unversioned",
        "subtype",
        "url",
    ],
)
def test_bank_rejects_unbounded_or_non_link_label_metadata(bank_archive, damage):
    packet = deepcopy(bank_archive.group)
    message = _project()
    resource = message["resource"]
    if damage == "nested":
        resource["publisher_name"] = {"text": "A publisher"}
    elif damage == "oversized":
        resource["publisher_name"] = "x" * 1025
    elif damage == "control":
        resource["publisher_name"] = "bad\x00name"
    elif damage == "empty":
        resource["publisher_name"] = ""
    elif damage == "version":
        resource["app_metadata_version"] = "2"
    elif damage == "unversioned":
        resource.pop("app_metadata_version")
    elif damage == "subtype":
        resource["app_subtype"] = "19"
        message["local_type"] = (19 << 32) | 49
    else:
        resource["url"] = "https://example.invalid/private-resource"
    packet["messages"].append(message)
    with pytest.raises(ValueError):
        bank._validate_packet(packet, {OWNER})


@pytest.mark.parametrize("code_version", ["v1.2.0-codex.0", "v1.2.1-codex.0"])
def test_link_metadata_support_keeps_existing_schema_three_banks_readable(
    bank_archive, code_version
):
    candidate, _receipt = fixtures._stage(bank_archive)
    with sqlite3.connect(candidate) as connection:
        connection.execute(
            "UPDATE archive_info SET value=? WHERE key='code_version'", (code_version,)
        )
    with sqlite3.connect(
        candidate.as_uri() + "?mode=ro&immutable=1", uri=True
    ) as connection:
        metadata = bank._validate_bank(connection, bank_archive.history)
    assert metadata["schema_version"] == "3"
    assert metadata["code_version"] == code_version
