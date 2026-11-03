"""Exact HTML completion links enrich views without changing source occurrences.

Code version: v1.0.1-codex.0
"""

from copy import deepcopy
import hashlib
import json
import sqlite3
from unittest.mock import patch

import pytest

from app.core.wechat_archive import _identifier
from app.core.wechat_contacts import WechatContactRepository
from app.core.wechat_context_links import WechatContextLinks
from app.core.wechat_export_merge import merge_wechat_export
from app.core.wechat_reconcile import _json
from tests.test_wechat_contacts import _make_forwarded_html_store


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def context_link_fixture(store):
    fixture = _make_forwarded_html_store(store)
    request = fixture["request"]
    target_id = request["id"]
    title = "Forward history"
    with sqlite3.connect(fixture["history"]) as connection:
        connection.row_factory = sqlite3.Row
        target = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (target_id,)
            ).fetchone()
        )
        provenance = json.loads(target["provenance"])
        provenance.update(
            export_msgid_namespace="MesLocalID",
            primary_text_sha256=_sha(title),
            quoted_components=[],
        )
        connection.execute(
            "UPDATE messages SET provenance=? WHERE id=?",
            (_json(provenance), target_id),
        )
    original = deepcopy(request["forwarded_components"][0])
    added = deepcopy(original)
    added.update(
        export_msgid="42_component_2",
        ordinal=3,
        component_ordinal=2,
        text="Additional complete text 😀 <img src=x>",
        raw_timestamp="2021-08-08 12:35:56",
    )
    added["source_locator"].update(message_ordinal=3, page_message_ordinal=3)
    children = [original, added]
    body = (
        title
        + "\n\n"
        + "\n\n".join(
            f"[Forwarded text: {item['sender_label']} | {item['raw_timestamp']}]\n{item['text']}"
            for item in children
        )
    )
    cross = {
        "kind": "rendered_text_completion_of_source_record",
        "existing_message_id": target_id,
        "existing_source_id": request["source_id"],
        "existing_account_key": "account:" + request["account_id"],
        "existing_conversation_id": request["conversation_id"],
        "existing_body_sha256": _sha(target["text"]),
        "existing_timestamp": target["timestamp_value"],
        "content_sha256": _sha(body),
        "global_message_identity_asserted": False,
        "time_zone_inferred": False,
        "basis": "Same original archive member and local record locator.",
    }
    source = {
        **fixture["source"],
        "source_id": request["source_id"] + "-text-completion-v1",
    }
    merge_wechat_export(
        store,
        source=source,
        snapshot_id=source["source_sha256"],
        records=[
            {
                "extraction_record_id": request["source_record_id"],
                "conversation_key": request["peer_id"],
                "peer_id": request["peer_id"],
                "title": "Synthetic room",
                "kind": "group",
                "sender_id": request["sender_id"],
                "sender_label": request["sender_label"],
                "direction": "received",
                "text": body,
                "sent_at_raw": provenance["source_time"],
                "provenance": {
                    "locator": request["source_locator"],
                    "forwarded_components": children,
                    "quoted_components": [],
                    "export_msgid_namespace": "MesLocalID",
                    "primary_text_sha256": _sha(title),
                    "text_extraction_kind": "rendered_app_text",
                },
                "crossrefs": [cross],
            }
        ],
    )
    source_id = _identifier(source["source_id"], request["source_record_id"])
    with sqlite3.connect(fixture["history"]) as connection:
        evidence_id = connection.execute(
            "SELECT id FROM message_evidence WHERE message_id=?", (source_id,)
        ).fetchone()[0]
    registry = fixture["history"].with_name("context_links.json")
    registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "links": [
                    {
                        "target_message_id": target_id,
                        "source_message_id": source_id,
                        "evidence_id": evidence_id,
                    }
                ],
            }
        )
    )
    return {
        **fixture,
        "target_id": target_id,
        "source_id": source_id,
        "evidence_id": evidence_id,
        "registry": registry,
        "source_body": body,
        "children": children,
    }


def _target(fixture):
    with sqlite3.connect(fixture["history"]) as connection:
        connection.row_factory = sqlite3.Row
        return dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (fixture["target_id"],)
            ).fetchone()
        )


def test_verified_completion_preserves_old_leaves_and_uses_its_own_source_proof(
    tmp_path,
):
    fixture = context_link_fixture(tmp_path)
    supplied = _target(fixture)
    before = fixture["history"].read_bytes()
    original = WechatContactRepository.resolve_forwarded_components
    with patch.object(
        WechatContactRepository,
        "resolve_forwarded_components",
        autospec=True,
        side_effect=original,
    ) as resolver:
        result = WechatContextLinks(tmp_path).project([supplied])
    assert set(result) == {fixture["target_id"]}
    projection = result[fixture["target_id"]]
    record = projection["forwarded_record"]
    assert record["authority"] == "source_verified_html_export_tree"
    assert [child.content_text for child in record["items"]] == [
        item["text"] for item in fixture["children"]
    ]
    assert all(
        child.messaging["provenance"]["parent_message_id"] == fixture["source_id"]
        for child in record["items"]
    )
    request = resolver.call_args.args[1][0]
    assert request["id"] == fixture["source_id"]
    assert request["source_id"].endswith("-text-completion-v1")
    assert projection["proof"]["evidence_id"] == fixture["evidence_id"]
    assert projection["proof"]["source_text_sha256"] == _sha(fixture["source_body"])
    assert not any(
        item["text"] in _json(projection["proof"]) for item in fixture["children"]
    )
    assert fixture["history"].read_bytes() == before
    assert _target(fixture) == supplied


def test_completion_sources_own_quoted_reply_keeps_its_distinct_roles(tmp_path):
    fixture = context_link_fixture(tmp_path)
    envelope = "「Earlier source participant：An exact earlier question」"
    body = envelope + "\n\n" + fixture["source_body"]
    with sqlite3.connect(fixture["history"]) as connection:
        encoded = connection.execute(
            "SELECT provenance FROM messages WHERE id=?", (fixture["source_id"],)
        ).fetchone()[0]
        provenance = json.loads(encoded)
        provenance.update(
            text_extraction_kind="quoted_reply",
            quoted_components=[{"kind": "rendered_quote", "text": envelope}],
        )
        connection.execute(
            "UPDATE messages SET text=?,provenance=? WHERE id=?",
            (body, _json(provenance), fixture["source_id"]),
        )

    def rebind(observation):
        observation["content_sha256"] = _sha(body)
        observation["cross_reference"]["content_sha256"] = _sha(body)

    _edit_crossref(fixture, rebind)
    before = fixture["history"].read_bytes()
    result = WechatContextLinks(tmp_path).project([_target(fixture)])[
        fixture["target_id"]
    ]
    assert result["forwarded_record"]["title"] == "Forward history"
    assert result["quoted_reply"]["speaker"] == "Earlier source participant"
    assert result["quoted_reply"]["text"] == "An exact earlier question"
    assert fixture["history"].read_bytes() == before


@pytest.mark.parametrize("source_quote", ["same", "missing", "changed", "unproven"])
def test_completion_preserves_every_previously_retained_top_level_quote(
    tmp_path, source_quote
):
    fixture = context_link_fixture(tmp_path)
    envelope = "「Previously retained participant：Previously retained question」"
    source_envelope = (
        envelope
        if source_quote != "changed"
        else "「Another participant：A different question」"
    )
    with sqlite3.connect(fixture["history"]) as connection:
        target_text, target_encoded = connection.execute(
            "SELECT text,provenance FROM messages WHERE id=?", (fixture["target_id"],)
        ).fetchone()
        target_body = envelope + "\n\n" + target_text
        target_provenance = json.loads(target_encoded)
        target_provenance.update(
            text_extraction_kind="quoted_reply",
            quoted_components=[{"kind": "rendered_quote", "text": envelope}],
        )
        connection.execute(
            "UPDATE messages SET text=?,provenance=? WHERE id=?",
            (target_body, _json(target_provenance), fixture["target_id"]),
        )
        source_encoded = connection.execute(
            "SELECT provenance FROM messages WHERE id=?", (fixture["source_id"],)
        ).fetchone()[0]
        source_provenance = json.loads(source_encoded)
        source_body = fixture["source_body"]
        if source_quote != "missing":
            source_body = source_envelope + "\n\n" + source_body
            source_provenance.update(
                text_extraction_kind="quoted_reply"
                if source_quote != "unproven"
                else "rendered_app_text",
                quoted_components=[{"kind": "rendered_quote", "text": source_envelope}],
            )
        connection.execute(
            "UPDATE messages SET text=?,provenance=? WHERE id=?",
            (source_body, _json(source_provenance), fixture["source_id"]),
        )

    def rebind(observation):
        observation["content_sha256"] = _sha(source_body)
        observation["cross_reference"].update(
            content_sha256=_sha(source_body), existing_body_sha256=_sha(target_body)
        )

    _edit_crossref(fixture, rebind)
    before = fixture["history"].read_bytes()
    result = WechatContextLinks(tmp_path).project([_target(fixture)])
    if source_quote == "same":
        quote = result[fixture["target_id"]]["quoted_reply"]
        assert quote["speaker"] == "Previously retained participant"
        assert quote["text"] == "Previously retained question"
    else:
        assert result == {}
    assert fixture["history"].read_bytes() == before


def _edit_crossref(fixture, change):
    with sqlite3.connect(fixture["history"]) as connection:
        connection.row_factory = sqlite3.Row
        row = dict(
            connection.execute(
                "SELECT * FROM message_evidence WHERE id=?", (fixture["evidence_id"],)
            ).fetchone()
        )
        observation = json.loads(row["observation"])
        change(observation)
        encoded = _json(observation)
        identifier = _identifier(row["source_id"], row["extraction_record_id"], encoded)
        connection.execute(
            "UPDATE message_evidence SET id=?,observation=? WHERE id=?",
            (identifier, encoded, row["id"]),
        )
    registry = json.loads(fixture["registry"].read_text())
    registry["links"][0]["evidence_id"] = identifier
    fixture["registry"].write_text(json.dumps(registry))


@pytest.mark.parametrize(
    "failure",
    [
        "cross_kind",
        "existing_body",
        "target_link",
        "global_identity",
        "timezone",
        "source_body",
        "target_body",
        "source_owner",
        "sender",
        "kind",
        "source_member",
        "old_leaf",
        "old_sender",
        "source_receipt",
        "missing_evidence",
    ],
)
def test_invalid_completion_binding_never_overrides_a_target(tmp_path, failure):
    fixture = context_link_fixture(tmp_path)
    if failure in {
        "cross_kind",
        "existing_body",
        "target_link",
        "global_identity",
        "timezone",
    }:
        field, value = {
            "cross_kind": ("kind", "unrelated_text_match"),
            "existing_body": ("existing_body_sha256", "f" * 64),
            "target_link": ("existing_message_id", "e" * 64),
            "global_identity": ("global_message_identity_asserted", True),
            "timezone": ("time_zone_inferred", True),
        }[failure]
        _edit_crossref(
            fixture,
            lambda observation: observation["cross_reference"].update({field: value}),
        )
    else:
        with sqlite3.connect(fixture["history"]) as connection:
            if failure in {"source_body", "target_body"}:
                identifier = (
                    fixture["source_id"]
                    if failure == "source_body"
                    else fixture["target_id"]
                )
                connection.execute(
                    "UPDATE messages SET text='Changed body' WHERE id=?", (identifier,)
                )
            elif failure == "source_owner":
                connection.execute(
                    "UPDATE partitions SET account_id='wxid_other' WHERE id=?",
                    (fixture["source"]["source_id"] + "-text-completion-v1",),
                )
            elif failure == "sender":
                connection.execute(
                    "UPDATE messages SET sender_id='different-alias' WHERE id=?",
                    (fixture["target_id"],),
                )
            elif failure == "kind":
                connection.execute(
                    "UPDATE conversations SET kind='direct' WHERE id=(SELECT conversation_id FROM messages WHERE id=?)",
                    (fixture["target_id"],),
                )
            elif failure in {"source_member", "old_leaf", "old_sender"}:
                identifier = (
                    fixture["source_id"]
                    if failure == "source_member"
                    else fixture["target_id"]
                )
                value = json.loads(
                    connection.execute(
                        "SELECT provenance FROM messages WHERE id=?", (identifier,)
                    ).fetchone()[0]
                )
                if failure == "source_member":
                    value["locator"]["page_message_ordinal"] += 1
                elif failure == "old_sender":
                    value["forwarded_components"][0]["sender_label"] = (
                        "Different source remark"
                    )
                else:
                    value["forwarded_components"][0]["text"] = (
                        "Different previously retained text"
                    )
                connection.execute(
                    "UPDATE messages SET provenance=? WHERE id=?",
                    (_json(value), identifier),
                )
            elif failure == "source_receipt":
                source_id = fixture["source"]["source_id"] + "-text-completion-v1"
                number, encoded = connection.execute(
                    "SELECT id,metadata FROM source_snapshots WHERE source_id=?",
                    (source_id,),
                ).fetchone()
                metadata = json.loads(encoded)
                metadata["owner_verified"] = False
                connection.execute(
                    "UPDATE source_snapshots SET metadata=? WHERE id=?",
                    (_json(metadata), number),
                )
            elif failure == "missing_evidence":
                connection.execute(
                    "DELETE FROM message_evidence WHERE id=?", (fixture["evidence_id"],)
                )
    before = fixture["history"].read_bytes()
    assert WechatContextLinks(tmp_path).project([_target(fixture)]) == {}
    assert fixture["history"].read_bytes() == before


def test_registry_preview_is_bounded_body_free_and_does_not_require_publication(
    tmp_path,
):
    fixture = context_link_fixture(tmp_path)
    audit = fixture["registry"].parent / "repair_audits" / "preview.json"
    audit.parent.mkdir()
    audit.write_bytes(fixture["registry"].read_bytes())
    fixture["registry"].unlink()
    assert WechatContextLinks(tmp_path).project([_target(fixture)]) == {}
    assert WechatContextLinks(tmp_path, registry_path=audit).project([_target(fixture)])
    value = json.loads(audit.read_text())
    value["links"][0]["body"] = "Forbidden embedded text"
    audit.write_text(json.dumps(value))
    assert (
        WechatContextLinks(tmp_path, registry_path=audit).project([_target(fixture)])
        == {}
    )
    with pytest.raises(ValueError):
        WechatContextLinks(tmp_path, registry_path=tmp_path.parent / "outside.json")


def test_unregistered_current_page_does_not_open_or_scan_history(tmp_path):
    context_link_fixture(tmp_path)
    with patch(
        "app.core.wechat_context_links._read_only",
        side_effect=AssertionError("History must stay unopened"),
    ):
        assert (
            WechatContextLinks(tmp_path).project(
                [{"id": "f" * 64, "text": "Other source"}]
            )
            == {}
        )


def test_changed_supplied_row_and_conflicting_registry_entries_fail_closed(tmp_path):
    fixture = context_link_fixture(tmp_path)
    supplied = _target(fixture)
    supplied["text"] = "Stale current-page body"
    assert WechatContextLinks(tmp_path).project([supplied]) == {}
    registry = json.loads(fixture["registry"].read_text())
    registry["links"].append(deepcopy(registry["links"][0]))
    fixture["registry"].write_text(json.dumps(registry))
    assert WechatContextLinks(tmp_path).project([_target(fixture)]) == {}


@pytest.mark.parametrize("changed", ["registry", "history"])
def test_source_proof_cannot_publish_a_projection_after_its_inputs_change(
    tmp_path, changed
):
    fixture = context_link_fixture(tmp_path)
    supplied = _target(fixture)
    original = WechatContactRepository.resolve_forwarded_components

    def resolve_then_change(repository, requests):
        result = original(repository, requests)
        if changed == "registry":
            fixture["registry"].write_bytes(fixture["registry"].read_bytes() + b"\n")
        else:
            with sqlite3.connect(fixture["history"]) as connection:
                connection.execute(
                    "UPDATE messages SET sender_label='Concurrent source change' WHERE id=?",
                    (fixture["source_id"],),
                )
        return result

    with patch.object(
        WechatContactRepository,
        "resolve_forwarded_components",
        autospec=True,
        side_effect=resolve_then_change,
    ):
        assert WechatContextLinks(tmp_path).project([supplied]) == {}
