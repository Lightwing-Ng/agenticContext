"""Pure, append-only reconciliation of already extracted ChatGPT history rows.

Code version: v1.0.1-codex.0
This module has no application imports, filesystem access, or normalization side effects.
Callers must persist raw source evidence and enforce source eligibility before merging.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from copy import deepcopy
from datetime import datetime
from typing import Any


SCHEMA_VERSION = 3
HISTORY_FIELDS = frozenset({
    "schema_version", "provider_revision", "platform", "conversation_id",
    "conversation_url", "conversation_title", "message_key", "turn_index",
    "message_index", "role", "author_label", "content_text", "content_html",
    "content_sha256", "source_links", "model_label", "first_seen_at", "last_seen_at",
})
_TEXT_FIELDS = HISTORY_FIELDS - {
    "schema_version", "provider_revision", "turn_index", "message_index", "source_links",
}
_SOURCE_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,31}")
_VARIANT_HASH_LENGTHS = (16, 24, 32, 48, 64)
_MAX_INDEX = 2_147_483_647


def _body_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _position(value: object, label: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_INDEX:
        raise ValueError(f"{label} must be a nonnegative int32.")
    return value


def _candidate_row(raw: Mapping[str, Any]) -> dict[str, Any]:
    row = deepcopy(dict(raw))
    missing = HISTORY_FIELDS - row.keys() - {"provider_revision"}
    extra = row.keys() - HISTORY_FIELDS
    if missing or extra:
        raise ValueError("Candidate fields do not match the history schema.")
    for field in _TEXT_FIELDS:
        if not isinstance(row[field], str):
            raise ValueError(f"Candidate {field} must be text.")
    conversation_id = row["conversation_id"]
    if not conversation_id or not row["role"]:
        raise ValueError("Candidate conversation ID and role must be nonempty.")
    if (
        not row["message_key"].startswith(f"{conversation_id}:")
        or row["message_key"] == f"{conversation_id}:"
        or row["platform"] != "chatgpt"
    ):
        raise ValueError("Candidate message identity is inconsistent.")
    _position(row["message_index"], "Candidate message_index")
    _position(row["turn_index"], "Candidate turn_index")
    if not isinstance(row["source_links"], list) or not all(
        isinstance(value, str) for value in row["source_links"]
    ):
        raise ValueError("Candidate source_links must be a list of text values.")
    if row["content_sha256"] != _body_hash(row["content_text"]):
        raise ValueError("Candidate body hash does not match its exact UTF-8 text.")
    if type(row["schema_version"]) is not int or row["schema_version"] != SCHEMA_VERSION:
        raise ValueError("Candidate schema version is unsupported.")
    if row.get("provider_revision") is not None and not isinstance(row["provider_revision"], str):
        raise ValueError("Candidate provider_revision must be text or None.")
    return row


def merge_history_rows(
    existing_rows: Iterable[Mapping[str, Any]],
    candidate_rows: Iterable[Mapping[str, Any]],
    *,
    source: str,
    source_reference: str,
    captured_at: str,
    node_provenance: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Preserve every existing field and append missing or conflicting message bodies.

    Input order is retained. Every new row receives a conversation-local message_index above
    its existing maximum. Source turn indexes remain source indexes, not inferred chronology.
    A conflicting body receives a deterministic local variant key; no text deduplication is
    performed between different original message keys. Repeating an identical source merge
    is row-idempotent. Existing duplicate or empty keys are retained and reported.

    New provider_revision values are deliberately unknown. This does not by itself protect
    against a later destructive provider refresh: the application must enforce its preservation
    guard when replacing sessions and refreshing titles. This function never claims completeness.
    """
    if not isinstance(source, str) or _SOURCE_PATTERN.fullmatch(source) is None:
        raise ValueError("Source must be a stable lowercase identifier.")
    if not isinstance(source_reference, str) or not source_reference.strip():
        raise ValueError("A source evidence reference is required.")
    if not isinstance(captured_at, str):
        raise ValueError("Capture time must include an explicit timezone.")
    try:
        parsed_capture = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Capture time must include an explicit timezone.") from exc
    if parsed_capture.tzinfo is None or parsed_capture.utcoffset() is None:
        raise ValueError("Capture time must include an explicit timezone.")
    evidence = {} if node_provenance is None else node_provenance
    if not isinstance(evidence, Mapping):
        raise ValueError("Node provenance must be a mapping.")

    rows = [deepcopy(dict(row)) for row in existing_rows]
    initial_count = len(rows)
    by_key: dict[str, list[int]] = defaultdict(list)
    maxima: dict[str, int] = {}
    empty_keys = 0
    existing_hash_mismatches = 0
    for index, row in enumerate(rows):
        conversation_id = row.get("conversation_id")
        if not isinstance(conversation_id, str) or not conversation_id:
            raise ValueError("Existing conversation ID must be nonempty text.")
        position = _position(row.get("message_index"), "Existing message_index")
        maxima[conversation_id] = max(maxima.get(conversation_id, -1), position)
        key = row.get("message_key")
        if isinstance(key, str) and key:
            by_key[key].append(index)
        else:
            empty_keys += 1
        text = row.get("content_text")
        if isinstance(text, str):
            try:
                existing_hash_mismatches += row.get("content_sha256") != _body_hash(text)
            except UnicodeEncodeError:
                existing_hash_mismatches += 1
    duplicate_keys = sum(len(positions) - 1 for positions in by_key.values())
    events = []
    counts: Counter[str] = Counter()

    for input_index, raw in enumerate(candidate_rows):
        if not isinstance(raw, Mapping):
            raise ValueError("Each candidate must be a mapping.")
        candidate = _candidate_row(raw)
        original_key = candidate["message_key"]
        conversation_id = candidate["conversation_id"]
        content_text = candidate["content_text"]
        content_hash = candidate["content_sha256"]
        provenance = evidence.get(original_key, {})
        if not isinstance(provenance, Mapping):
            raise ValueError("Each node provenance record must be a mapping.")
        event = {
            "input_position": input_index,
            "conversation_id": conversation_id,
            "source": source,
            "source_reference": source_reference,
            "original_message_key": original_key,
            "original_node_id": original_key[len(conversation_id) + 1:],
            "content_sha256": content_hash,
            "source_message_index": candidate["message_index"],
            "source_turn_index": candidate["turn_index"],
            "source_provider_revision": candidate.get("provider_revision"),
            "node_provenance": deepcopy(dict(provenance)),
        }
        target_key = original_key
        matching = [
            index for index in by_key.get(original_key, [])
            if rows[index].get("conversation_id") == conversation_id
            and rows[index].get("content_text") == content_text
        ]
        action = "keep-existing"
        if not matching:
            if original_key in by_key:
                action = "append-variant"
                for length in _VARIANT_HASH_LENGTHS:
                    target_key = f"{original_key}:recovered:{source}:{content_hash[:length]}"
                    occupied = by_key.get(target_key, [])
                    matching = [
                        index for index in occupied
                        if rows[index].get("conversation_id") == conversation_id
                        and rows[index].get("content_text") == content_text
                    ]
                    if matching:
                        action = "keep-variant"
                        break
                    if not occupied:
                        break
                else:
                    raise ValueError("Deterministic variant keys are occupied by different rows.")
            else:
                action = "append-missing"
            if not matching:
                position = maxima.get(conversation_id, -1) + 1
                _position(position, "Appended message_index")
                appended = {
                    **candidate,
                    "message_key": target_key,
                    "message_index": position,
                    "provider_revision": None,
                }
                matching = [len(rows)]
                rows.append(appended)
                by_key[target_key].append(matching[0])
                maxima[conversation_id] = position
        event.update(
            action=action,
            stored_message_key=target_key,
            stored_row_positions=matching,
            stored_message_indexes=[rows[index]["message_index"] for index in matching],
            candidate_metadata={
                field: deepcopy(value) for field, value in candidate.items()
                if field not in {"content_text", "content_sha256"}
            },
        )
        events.append(event)
        counts[action] += 1

    manifest = {
        "schema_version": 1,
        "module_version": "v1.0.1-codex.0",
        "mode": "preserve-existing",
        "source": source,
        "source_reference": source_reference,
        "captured_at": captured_at,
        "discovery_complete": False,
        "sync_complete": False,
        "requires_preservation_guard": True,
        "before_rows": initial_count,
        "after_rows": len(rows),
        "appended_rows": len(rows) - initial_count,
        "existing_duplicate_keys_preserved": duplicate_keys,
        "existing_empty_keys_preserved": empty_keys,
        "existing_body_hash_mismatches_preserved": existing_hash_mismatches,
        "counts": dict(counts),
        "events": events,
    }
    return rows, manifest
