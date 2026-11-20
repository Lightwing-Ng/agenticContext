"""Source-bound nested resource recovery never invents a forwarding parent.

Code version: v1.0.2-codex.0
"""

from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path

import pytest

from app.core import wechat_html_forwarded_resources as resources


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _node(ordinal, text="", *, role="forwarded", kind="", status=None):
    return {
        "source_locator": {
            "member_path": "Archive/Chat.html",
            "page": 0,
            "message_ordinal": ordinal,
            "page_message_ordinal": ordinal,
        },
        "text_sha256": _sha(text),
        "text_length": len(text),
        "role": role,
        "status": status
        or (
            "forward_boundary"
            if role == "boundary"
            else "omitted_forwarded_non_text"
            if kind
            else "retained_forwarded_text"
        ),
        "type": kind,
        "text_kind": "deferred" if kind else "plain_text",
        "sender_label": "Original speaker",
        "sender_export_id": "export-local-author",
        "sender_native_id": "",
        "direction": "unknown",
        "raw_timestamp": "",
        "flags": ["export_time_zone_unavailable"],
    }


def _component(node, text, children=()):
    return {
        "source_locator": node["source_locator"],
        "text": text,
        "text_kind": "forwarded_rendered_text",
        "sender_label": "Original speaker",
        "sender_export_id": "export-local-author",
        "direction": "unknown",
        "raw_timestamp": "",
        "flags": [],
        "forwarded_components": list(children),
    }


def _fixture(*, nested=False, account="account:owner_a"):
    title = "Literal archived conversation"
    nodes = [
        _node(1, title, role="top_level"),
        _node(2, "<< " + title, role="boundary"),
    ]
    if nested:
        nested_title = "Nested original title"
        nodes.extend(
            [
                _node(3, nested_title),
                _node(4, "<< " + nested_title, role="boundary"),
                _node(5, kind="47"),
                _node(6, "Nested child"),
                _node(7, nested_title + " Ends >>", role="boundary"),
            ]
        )
        children = [
            _component(nodes[2], nested_title, [_component(nodes[5], "Nested child")])
        ]
        nodes.append(_node(8, title + " Ends >>", role="boundary"))
    else:
        nodes.extend(
            [
                _node(3, kind="image"),
                _node(4, "Retained child"),
                _node(5, title + " Ends >>", role="boundary"),
            ]
        )
        children = [_component(nodes[3], "Retained child")]
    provenance = {
        "locator": nodes[0]["source_locator"],
        "primary_text_sha256": _sha(title),
        "forwarded_components": children,
    }
    parent = {
        "id": _sha(account),
        "account_key": account,
        "conversation_id": "source-conversation",
        "partition_id": "html-source",
        "text": title + "\n\n[Forwarded text: original]",
        "provenance": json.dumps(provenance),
    }
    return nodes, parent


def _entry(recovered):
    item = recovered[0]
    node, binding = item["node"], item["binding"]
    return {
        "id": _sha(
            binding["root"]["id"] + str(node["source_locator"]["message_ordinal"])
        ),
        "parent_id": binding["root"]["id"],
        "ancestor_ordinals": list(binding["path"]),
        "ordinal": node["source_locator"]["message_ordinal"],
        "node": node,
        "proof": {
            "member_path": "Archive/Chat.html",
            "boundaries": item["boundaries"],
            "audit_records": 100,
            "forwarding_end_unavailable": item.get("end_unavailable", False),
        },
    }


def test_exact_parent_caption_hash_recovers_resource_before_first_text_child():
    nodes, parent = _fixture()
    before = deepcopy((nodes, parent))
    recovered = resources.recover_nodes(nodes, [parent])
    assert len(recovered) == 1
    assert recovered[0]["binding"]["root"]["id"] == parent["id"]
    assert recovered[0]["binding"]["path"] == ()
    assert resources._validate_boundaries(_entry(recovered), parent)
    assert (nodes, parent) == before


@pytest.mark.parametrize("defect", ["primary", "frame", "unrelated_parent"])
def test_missing_or_ambiguous_structure_never_attaches_a_resource(defect):
    nodes, parent = _fixture()
    if defect == "primary":
        parent["text"] = "A changed primary"
    elif defect == "frame":
        proof = json.loads(parent["provenance"])
        proof["locator"]["member_path"] = "Another/Chat.html"
        parent["provenance"] = json.dumps(proof)
    else:
        parent["provenance"] = json.dumps({"locator": {}, "forwarded_components": []})
    assert resources.recover_nodes(nodes, [parent]) == []


def test_nested_resource_requires_both_original_parent_boundaries():
    nodes, parent = _fixture(nested=True)
    recovered = resources.recover_nodes(nodes, [parent])
    assert recovered[0]["binding"]["path"] == (3,)
    entry = _entry(recovered)
    assert len(entry["proof"]["boundaries"]) == 2
    assert resources._validate_boundaries(entry, parent)
    entry["ancestor_ordinals"] = []
    assert not resources._validate_boundaries(entry, parent)
    nodes[-1]["text_sha256"] = _sha("Unproven outer close")
    recovered = resources.recover_nodes(nodes, [parent])
    assert len(recovered) == 1 and recovered[0]["end_unavailable"]
    assert resources._validate_boundaries(_entry(recovered), parent)


def test_unknown_later_boundary_keeps_prior_proven_child_and_stops_parent_carry():
    nodes, parent = _fixture()
    nodes[-1]["text_sha256"] = _sha("An unknown closing control")
    nodes.append(_node(6, kind="file"))
    recovered = resources.recover_nodes(nodes, [parent])
    assert [e["node"]["source_locator"]["message_ordinal"] for e in recovered] == [3]
    entry = _entry(recovered)
    assert entry["proof"]["forwarding_end_unavailable"]
    assert (
        entry["proof"]["boundaries"][0]["cut"]["source_locator"]["message_ordinal"] == 5
    )
    assert resources._validate_boundaries(entry, parent)
    entry["ordinal"] = 6
    assert not resources._validate_boundaries(entry, parent)


def test_unproven_nested_opening_never_attaches_the_following_resource():
    nodes, parent = _fixture(nested=True)
    nodes[3]["text_sha256"] = _sha("An unknown nested opener")
    assert resources.recover_nodes(nodes, [parent]) == []


def test_source_audit_end_retains_proven_child_with_explicit_missing_end_receipt():
    nodes, parent = _fixture()
    nodes.pop()
    recovered = resources.recover_nodes(nodes, [parent])
    entry = _entry(recovered)
    assert entry["proof"]["boundaries"][0]["cut_reason"] == "end_of_audit"
    assert entry["proof"]["boundaries"][0]["cut"] is None
    assert resources._validate_boundaries(entry, parent)


def test_async_page_resource_keeps_main_parent_and_separate_exact_member_receipts():
    nodes, parent = _fixture()
    page = "Archive/Chat_files/messages_1.js"
    for node in nodes[1:]:
        node["source_locator"]["member_path"] = page
    provenance = json.loads(parent["provenance"])
    provenance["forwarded_components"][0]["source_locator"]["member_path"] = page
    parent["provenance"] = json.dumps(provenance)
    recovered = resources.recover_nodes(nodes, [parent])
    assert len(recovered) == 1
    entry = _entry(recovered)
    entry["proof"]["member_path"] = page
    entry["proof"]["frame_member_receipts"] = {
        "Archive/Chat.html": _sha("Main HTML receipt"),
        page: _sha("Async page receipt"),
    }
    assert resources._validate_boundaries(entry, parent)
    del entry["proof"]["frame_member_receipts"][page]
    assert not resources._validate_boundaries(entry, parent)


def test_two_source_owners_keep_separate_parent_ids():
    first, owner_a = _fixture(account="account:owner_a")
    second, owner_b = _fixture(account="account:owner_b")
    a = _entry(resources.recover_nodes(first, [owner_a]))
    b = _entry(resources.recover_nodes(second, [owner_b]))
    assert a["parent_id"] != b["parent_id"]
    assert a["id"] != b["id"]


def test_resource_projection_is_idempotent_keeps_missing_clock_and_has_no_payload_action():
    nodes, parent = _fixture()
    entry = _entry(resources.recover_nodes(nodes, [parent]))
    record = {"items": (), "authority": "source_verified_html_export_tree"}
    resources._merge(record, [entry], account_id="owner_a")
    resources._merge(record, [entry], account_id="owner_a")
    assert len(record["items"]) == 1
    child = record["items"][0]
    assert child.messaging["timestamp_label"] == ""
    assert child.first_seen_at == ""
    assert child.messaging["direction"] == "unknown"
    assert child.messaging["sender_identifier_namespace"] == "unverified_export_field"
    assert child.messaging["resource"]["kind"] == "image"
    assert child.messaging["resource"]["status"] == "metadata_only"
    assert child.messaging["resource"]["can_reveal"] is False
    assert child.messaging["provenance"]["parent_message_id"] == parent["id"]


def test_resources_never_enter_an_enriched_transcript_owned_by_another_parent():
    nodes, parent = _fixture(account="account:owner_a")
    first = _entry(resources.recover_nodes(nodes, [parent]))
    other_nodes, other_parent = _fixture(account="account:owner_b")
    second = _entry(resources.recover_nodes(other_nodes, [other_parent]))
    record = {"items": (), "authority": "source_verified_html_export_tree"}
    assert resources._merge(record, [first], account_id="owner_a") == 1
    assert resources._merge(record, [second], account_id="owner_b") == 0
    assert [child.stable_id for child in record["items"]] == [first["id"]]


def test_audit_reader_requires_exact_hash_and_never_opens_payload_files(
    tmp_path, monkeypatch
):
    nodes, _parent = _fixture()
    folder = tmp_path / "audit_nodes"
    folder.mkdir()
    audit = folder / "conversation-000.jsonl.gz"
    header = {"member_path": "Archive/Chat.html", "counts": {}}
    raw = gzip.compress(
        (
            "\n".join(json.dumps(value) for value in [{"conversation": header}, *nodes])
            + "\n"
        ).encode()
    )
    audit.write_bytes(raw)
    expected = {
        "expected_header": header,
        "expected_records": len(nodes),
        "expected_audit_sha256": hashlib.sha256(raw).hexdigest(),
    }
    original = Path.open

    def metadata_only(path, *args, **kwargs):
        assert path.name.endswith(".jsonl.gz")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", metadata_only)
    actual, _receipt = resources._read_audit(audit, expected, allow_hydration=False)
    assert actual == nodes
    expected["expected_audit_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="digest"):
        resources._read_audit(audit, expected, allow_hydration=False)


def test_absent_catalog_never_opens_a_source_database(tmp_path, monkeypatch):
    def no_connect(*args, **kwargs):
        raise AssertionError("An absent or cloud-only catalog must stay unopened.")

    monkeypatch.setattr(resources.sqlite3, "connect", no_connect)
    assert resources.WechatHtmlForwardedResources(tmp_path).project([{"id": "x"}]) == {}
