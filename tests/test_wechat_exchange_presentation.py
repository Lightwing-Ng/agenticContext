"""Preserve imported presentation snapshots without granting native authority.

Code version: v1.0.0-codex.0
"""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.core.wechat_exchange_presentation import (
    apply_frozen_presentation,
    frozen_forwarded_items,
)


def _item():
    original = '{ "source_snapshot": 1, "resource_reference":{"kind":"image","filename":"literal.png","path":"/donor-only/file"} }'
    provenance = {
        "exchange": {
            "identity_authority": "external_origin_claim",
            "original_message_id": "original-parent",
            "original_provenance_raw": original,
            "original_provenance": {
                "source_snapshot": 1,
                "resource_reference": {
                    "kind": "image",
                    "filename": "literal.png",
                    "path": "/donor-only/file",
                },
            },
            "viewer_display_name": "Source-only name 2️⃣",
            "frozen_presentation": {
                "schema_version": 1,
                "authority": "frozen_external_presentation",
                "source_message_id": "original-parent",
                "source_provenance": original,
                "forwarded_items": [
                    {
                        "id": "foreign-child-id",
                        "component_path": [1, 2],
                        "text": "  Literal\n\n* text  ",
                        "sender_id": "claimed-child-id",
                        "sender_label": "Source child",
                        "viewer_display_name": "Source child preference",
                        "timestamp_value": "",
                        "timestamp_label": "Unknown time",
                        "time_basis": "Source clock; timezone unknown",
                        "source_clock": "",
                        "provenance": {
                            "authority": "native_account_owner",
                            "path": "/donor-only/secret",
                        },
                    }
                ],
            },
        },
    }
    return SimpleNamespace(
        stable_id="local-parent-namespace",
        conversation_id="local-conversation",
        messaging={
            "account_id": None,
            "identity_status": "Imported origin claim",
            "partition_id": "xchg-source",
            "provenance": provenance,
        },
    )


def test_frozen_snapshot_keeps_literal_fields_and_namespaces_child_ids():
    item = _item()
    source = deepcopy(item.messaging["provenance"])
    rows = frozen_forwarded_items(source, item.messaging, parent_id=item.stable_id)
    assert len(rows) == 1 and rows[0]["id"] != "foreign-child-id"
    assert rows[0]["component_path"] == (1, 2)
    assert rows[0]["text"] == "  Literal\n\n* text  "
    assert rows[0]["timestamp_value"] == ""
    assert rows[0]["time_basis"] == "Source clock; timezone unknown"
    assert "authority" not in rows[0]["provenance"]
    assert source == item.messaging["provenance"]
    other = frozen_forwarded_items(source, item.messaging, parent_id="another-parent")
    assert rows[0]["id"] != other[0]["id"]


def test_imported_tree_and_resource_never_get_local_reveal_or_contact_links():
    item = _item()
    apply_frozen_presentation(item)
    assert item.messaging["viewer_display_name"] == "Source-only name 2️⃣"
    assert item.messaging["resource"]["can_reveal"] is False
    assert "path" not in item.messaging["resource"]
    root = item.messaging["forwarded_record"]
    assert root["authority"] == "external_origin_claim"
    nested = root["items"][0].messaging["forwarded_record"]
    leaf = nested["items"][0]
    assert leaf.content_text == "  Literal\n\n* text  "
    assert leaf.messaging["contact"]["identity_status"] == "unresolved"
    assert leaf.messaging["contact"]["registry_persisted"] is False
    assert leaf.messaging["viewer_display_name"] == "Source child preference"
    assert (
        leaf.messaging["provenance"]["exchange"]["identity_authority"]
        == "external_origin_claim"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"account_id": "wxid_local"},
        {"identity_status": "Verified archive ID"},
        {"partition_id": "ios-local"},
    ],
)
def test_frozen_snapshots_never_override_native_or_other_sources(change):
    item = _item()
    item.messaging.update(change)
    before = deepcopy(item.messaging)
    assert (
        frozen_forwarded_items(
            item.messaging["provenance"], item.messaging, parent_id=item.stable_id
        )
        == ()
    )
    apply_frozen_presentation(item)
    assert item.messaging == before


def test_duplicate_or_ambiguous_child_paths_fail_closed():
    item = _item()
    values = item.messaging["provenance"]["exchange"]["frozen_presentation"][
        "forwarded_items"
    ]
    values.append(deepcopy(values[0]))
    assert (
        frozen_forwarded_items(
            item.messaging["provenance"], item.messaging, parent_id=item.stable_id
        )
        == ()
    )


@pytest.mark.parametrize("kind", [[], {}, None, 3])
def test_malformed_external_resource_kinds_cannot_break_the_conversation(kind):
    item = _item()
    exchange = item.messaging["provenance"]["exchange"]
    exchange["original_provenance"]["resource_reference"]["kind"] = kind
    leaf = exchange["frozen_presentation"]["forwarded_items"][0]
    leaf["provenance"]["resource_reference"] = {"kind": kind}
    apply_frozen_presentation(item)
    assert "resource" not in item.messaging
    assert item.messaging["forwarded_record"]["authority"] == "external_origin_claim"


def test_imported_event_observations_retain_clocks_without_local_dom_aliases():
    item = _item()
    item.messaging["provenance"]["exchange"]["frozen_presentation"]["event_members"] = [
        {
            "id": "a-local-message-id-on-another-computer",
            "source_label": "Source iPad",
            "source_snapshot_id": "Original snapshot",
            "timestamp_value": "2026-08-18T14:43:10+08:00",
            "source_clock": "2026-08-18 14:43:10",
            "clock_disagreement": True,
        }
    ]
    apply_frozen_presentation(item)
    observation = item.messaging["source_observations"][0]
    assert observation["id"] != "a-local-message-id-on-another-computer"
    assert (
        observation["source_clock"] == "2026-08-18 14:43:10"
        and observation["clock_disagreement"]
    )


@pytest.mark.parametrize("identifiers", [["", ""], ["same", "same"], [None]])
def test_ambiguous_observation_ids_never_create_duplicate_dom_aliases(identifiers):
    item = _item()
    frozen = item.messaging["provenance"]["exchange"]["frozen_presentation"]
    frozen["event_members"] = [
        {"id": identifier} if identifier is not None else {}
        for identifier in identifiers
    ]
    apply_frozen_presentation(item)
    assert "source_observations" not in item.messaging


def test_full_snapshot_restores_manual_quote_nested_forward_and_metadata_cards():
    item = _item()
    metadata = {
        "display_text": "  Literal manual reply\n\n* keep  ",
        "viewer_display_name": "Scoped display",
        "quoted_reply": {
            "speaker": "Original source speaker",
            "text": "  Quoted source\nline  ",
            "forwarded_record": {
                "title": "Nested source title",
                "source_title": "  Literal title  ",
                "items": [
                    {
                        "stable_id": "foreign-child-id",
                        "author_label": "Original source author",
                        "content_text": "Child original",
                        "timestamp_value": "",
                        "metadata": {
                            "sender_id": "claimed-id",
                            "direction": "received",
                            "timestamp_label": "Unknown time",
                            "time_basis": "Source clock; timezone unknown",
                            "viewer_display_name": "Child preference",
                            "resource": {
                                "kind": "image",
                                "label": "Source image",
                                "can_reveal": True,
                                "path": "/donor-only/private",
                            },
                        },
                    }
                ],
            },
        },
        "record_fields": [{"label": "Original field", "value": "  literal value  "}],
    }
    item.messaging["provenance"]["exchange"]["frozen_presentation"]["presentation"] = (
        metadata
    )
    apply_frozen_presentation(item)
    context = item.messaging
    assert context["display_text"] == metadata["display_text"]
    assert context["quoted_reply"]["text"] == metadata["quoted_reply"]["text"]
    record = context["quoted_reply"]["forwarded_record"]
    assert (
        record["source_title"] == "  Literal title  "
        and record["authority"] == "external_origin_claim"
    )
    child = record["items"][0]
    assert child.stable_id != "foreign-child-id"
    assert child.messaging["viewer_display_name"] == "Child preference"
    assert (
        child.messaging["resource"]["label"] == "Source image"
        and not child.messaging["resource"]["can_reveal"]
    )
    assert "path" not in child.messaging["resource"]
    assert child.messaging["contact"]["identity_status"] == "unresolved"
    assert context["record_fields"][0]["value"] == "  literal value  "
