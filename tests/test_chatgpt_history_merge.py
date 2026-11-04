"""Isolated fixtures for the recovery-only, pure ChatGPT row merge.

Code version: v1.0.1-codex.0
"""

from copy import deepcopy
from hashlib import sha256

import pytest

from app.core.chatgpt_history_merge import merge_history_rows


def row(node="node", text="Original body", conversation="session", index=0, **fields):
    value = {
        "schema_version": 3, "provider_revision": "original-revision", "platform": "chatgpt",
        "conversation_id": conversation, "conversation_url": f"https://chatgpt.com/c/{conversation}",
        "conversation_title": "Synthetic title", "message_key": f"{conversation}:{node}",
        "turn_index": 0, "message_index": index, "role": "assistant", "author_label": "ChatGPT",
        "content_text": text, "content_html": "", "content_sha256": sha256(text.encode()).hexdigest(),
        "source_links": [], "model_label": "", "first_seen_at": "2026-10-01T00:00:00Z",
        "last_seen_at": "2026-09-01T00:00:00Z",
    }
    value.update(fields)
    return value


def merge(existing, candidates, **options):
    return merge_history_rows(
        existing, candidates, source="online", source_reference="synthetic/source.json",
        captured_at="2026-10-22T00:00:00Z", **options,
    )


def test_existing_rows_are_preserved_field_for_field_without_aliasing():
    existing = [row(schema_version=1, provider_revision=None, last_seen_at="", source_links=["old"])]
    existing[0]["opaque_legacy_field"] = {"nested": ["retained"]}
    before = deepcopy(existing)
    candidate = row(conversation_title="Changed title", role="user", first_seen_at="new")
    candidates_before = deepcopy(candidate)

    result, manifest = merge(existing, [candidate])

    assert result == before
    assert existing == before
    assert candidate == candidates_before
    assert manifest["counts"] == {"keep-existing": 1}
    assert manifest["events"][0]["candidate_metadata"]["role"] == "user"
    result[0]["opaque_legacy_field"]["nested"].append("independent")
    assert existing == before


def test_missing_keys_append_per_conversation_without_body_deduplication():
    existing = [row(index=7), row(conversation="other", index=90)]
    candidates = [row(node="second", index=0), row(node="third", index=1), row(conversation="new")]

    result, manifest = merge(existing, candidates)

    assert result[:2] == existing
    assert [item["message_index"] for item in result[2:]] == [8, 9, 0]
    assert [item["message_key"] for item in result[2:]] == ["session:second", "session:third", "new:node"]
    assert all(item["provider_revision"] is None for item in result[2:])
    assert manifest["appended_rows"] == 3
    assert manifest["discovery_complete"] is False
    assert manifest["sync_complete"] is False


def test_conflicting_body_is_a_deterministic_local_variant_with_provenance():
    existing = [row(index=4)]
    candidate = row(text="Recovered body", index=2, turn_index=1)
    provenance = {"session:node": {"parent": "parent", "source_order": 12, "children": ["child"]}}
    before = deepcopy(provenance)

    result, manifest = merge(existing, [candidate], node_provenance=provenance)

    assert result[0] == existing[0]
    assert result[1]["content_text"] == "Recovered body"
    assert result[1]["message_index"] == 5
    assert result[1]["turn_index"] == 1
    assert result[1]["message_key"] == f"session:node:recovered:online:{candidate['content_sha256'][:16]}"
    event = manifest["events"][0]
    assert event["original_message_key"] == "session:node"
    assert event["original_node_id"] == "node"
    assert event["source_message_index"] == 2
    assert event["node_provenance"] == before["session:node"]
    event["node_provenance"]["children"].append("independent")
    assert provenance == before
    repeated, repeated_manifest = merge(result, [candidate], node_provenance=provenance)
    assert repeated == result
    assert repeated_manifest["appended_rows"] == 0
    assert repeated_manifest["counts"] == {"keep-variant": 1}


def test_variant_prefix_collision_expands_without_overwriting_existing_rows():
    candidate = row(text="Recovered body")
    occupied_key = f"session:node:recovered:online:{candidate['content_sha256'][:16]}"
    existing = [row(), row(node="occupied", text="Different event", index=3, message_key=occupied_key)]

    result, _ = merge(existing, [candidate])

    assert result[:2] == existing
    assert result[-1]["message_key"].endswith(candidate["content_sha256"][:24])
    assert result[-1]["message_index"] == 4


def test_existing_duplicate_and_empty_keys_are_not_dropped_or_rewritten():
    existing = [row(index=0), row(index=1, text="Legacy conflict"), row(index=2, message_key="")]
    result, manifest = merge(existing, [row(), row(node="new")])

    assert result[:3] == existing
    assert len(result) == 4
    assert result[-1]["message_index"] == 3
    assert manifest["existing_duplicate_keys_preserved"] == 1
    assert manifest["existing_empty_keys_preserved"] == 1


def test_existing_bad_hash_is_retained_but_not_trusted_for_body_equality():
    existing = [row(content_sha256="bad-hash")]
    result, manifest = merge(existing, [row()])

    assert result == existing
    assert manifest["existing_body_hash_mismatches_preserved"] == 1
    assert manifest["appended_rows"] == 0


def test_repeated_candidate_identity_is_idempotent_but_distinct_ids_are_preserved():
    first = row()
    result, manifest = merge([], [first, deepcopy(first), row(node="different")])

    assert len(result) == 2
    assert manifest["counts"] == {"append-missing": 2, "keep-existing": 1}
    assert [event["input_position"] for event in manifest["events"]] == [0, 1, 2]


def test_missing_provider_revision_and_group_rows_are_supported():
    candidate = row(conversation="group-synthetic", conversation_url="", role="participant")
    candidate.pop("provider_revision")
    result, _ = merge([], [candidate])

    assert result[0]["conversation_url"] == ""
    assert result[0]["role"] == "participant"
    assert result[0]["provider_revision"] is None


@pytest.mark.parametrize("field,value", [
    ("content_sha256", "wrong"), ("message_index", True), ("turn_index", -1),
    ("schema_version", 1), ("message_key", "another:node"), ("source_links", [None]),
    ("content_text", None), ("role", ""), ("parent", "not-a-schema-field"),
])
def test_invalid_candidates_fail_without_mutating_inputs(field, value):
    existing = [row()]
    candidate = row(node="new")
    candidate[field] = value
    before = deepcopy(existing)
    candidate_before = deepcopy(candidate)

    with pytest.raises(ValueError):
        merge(existing, [candidate])

    assert existing == before
    assert candidate == candidate_before


def test_index_overflow_fails_instead_of_reusing_an_existing_position():
    existing = [row(index=2_147_483_647)]
    with pytest.raises(ValueError, match="int32"):
        merge(existing, [row(node="new")])


def test_capture_time_requires_an_explicit_timezone():
    with pytest.raises(ValueError, match="timezone"):
        merge_history_rows([], [], source="online", source_reference="fixture", captured_at="2026-10-22")


@pytest.mark.parametrize("provenance", [[], "", False])
def test_invalid_empty_provenance_is_not_silently_discarded(provenance):
    with pytest.raises(ValueError, match="provenance"):
        merge([], [], node_provenance=provenance)


@pytest.mark.parametrize("revision", [[], {}, False, object()])
def test_invalid_revision_does_not_enter_the_recovery_manifest(revision):
    with pytest.raises(ValueError, match="provider_revision"):
        merge([], [row(provider_revision=revision)])
