"""Match unidentified HTML occurrences through bounded verified native neighbors.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from itertools import combinations
import re

from .wechat_contacts import canonical_contact_id


VERSION = "v1.0.1-codex.0"
MATCH_BASIS = "verified_neighbor_sequence_v1"
_CLOCK = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_LABEL = re.compile(r"wxid_[A-Za-z0-9]+")


def _positive(value):
    return type(value) is int and value > 0


def _key(item):
    value = item.get("key")
    if (
        "quote" in item
        or not isinstance(value, tuple)
        or len(value) != 3
        or not isinstance(value[0], str)
        or not value[0]
        or not isinstance(value[1], str)
        or _CLOCK.fullmatch(value[1]) is None
        or value[2] not in {"sent", "received"}
    ):
        return None
    try:
        if datetime.fromisoformat(value[1]).year <= 1970:
            return None
    except ValueError:
        return None
    return value


def _source(item):
    value = item.get("source_key")
    return (
        value
        if isinstance(value, tuple)
        and len(value) == 2
        and all(isinstance(part, str) and part for part in value)
        else None
    )


def _epoch(item):
    try:
        value = Decimal(str(item["epoch"]))
        return value if value.is_finite() else None
    except (ValueError, KeyError, InvalidOperation):
        return None


def _same_epoch(left, right):
    if left.get("epoch") is None:
        return True
    first, second = _epoch(left), _epoch(right)
    return first is not None and second is not None and first == second


def _native_scope(item):
    value = item.get("local_scope")
    return (
        value
        if isinstance(value, tuple)
        and len(value) == 2
        and all(isinstance(part, str) and part for part in value)
        else None
    )


def _native(item):
    sender = item.get("context", {}).get("sender_id")
    try:
        return (
            item.get("format") == "native"
            and _key(item) is not None
            and _source(item) is not None
            and _native_scope(item) is not None
            and _epoch(item) is not None
            and _positive(item.get("local_id"))
            and _positive(item.get("order"))
            and isinstance(item.get("server"), str)
            and re.fullmatch(r"-?[1-9][0-9]*", item["server"]) is not None
            and item.get("author")
            == canonical_contact_id(sender, authority="native_message_sender")
        )
    except (ValueError, TypeError):
        return False


def _anchor_proof(anchors, left, right, owner):
    nearby = []
    for local in range(max(1, left["local_id"] - 4), left["local_id"] + 5):
        values = anchors.get(local, ())
        if len(values) > 1:
            return None
        nearby.extend(values)
    nearby.sort(key=lambda pair: (pair[0]["order"], pair[0]["id"]))
    if len(nearby) < 3 or any(
        first[0]["order"] >= second[0]["order"]
        or first[1]["order"] >= second[1]["order"]
        for first, second in zip(nearby, nearby[1:])
    ):
        return None
    # The target itself must fit every verified neighbor's relative ordering.
    if any(
        (pair[0]["order"] < left["order"]) != (pair[1]["order"] < right["order"])
        or pair[0]["order"] == left["order"]
        or pair[1]["order"] == right["order"]
        for pair in nearby
    ):
        return None
    for proof in combinations(nearby, 3):
        authors = {pair[0]["author"] for pair in proof}
        if (
            owner in authors
            and len(authors) >= 2
            and proof[0][0]["order"] < left["order"] < proof[-1][0]["order"]
            and proof[0][1]["order"] < right["order"] < proof[-1][1]["order"]
        ):
            return [pair[2] for pair in proof]
    return None


def bridge_unidentified_events(candidates, current, mapping, members):
    """Join occurrences without promoting their unverified source display labels."""
    updated_mapping, updated_members = dict(mapping), dict(members)
    if current.get("kind") != "group":
        return updated_mapping, updated_members
    try:
        owner = canonical_contact_id(
            current.get("account_id"), authority="native_account_owner"
        )
    except (ValueError, TypeError):
        return updated_mapping, updated_members
    records = {item["id"]: item for item in candidates}
    if len(records) != len(candidates):
        return updated_mapping, updated_members
    counts = Counter(
        (item.get("format"), _source(item), _key(item)) for item in candidates
    )
    signatures = defaultdict(set)
    for item in candidates:
        if item.get("format") == "native":
            signatures[item.get("server")].add(
                (repr(item.get("key")), item.get("author"), _epoch(item))
            )
    conflicts = {server for server, values in signatures.items() if len(values) != 1}
    natives = defaultdict(list)
    for item in candidates:
        if _native(item) and item["server"] not in conflicts:
            natives[_key(item)].append(item)

    anchors = defaultdict(lambda: defaultdict(list))
    for preferred, group in members.items():
        proven = [
            records[context["id"]]
            for context in group
            if context.get("id") in records
            and mapping.get(context["id"], context["id"]) == preferred
        ]
        for left in proven:
            if (
                left.get("format") != "html"
                or not left.get("author")
                or not _positive(left.get("local_id"))
                or not _positive(left.get("order"))
                or _key(left) is None
                or _source(left) is None
                or counts[("html", left["source_key"], left["key"])] != 1
            ):
                continue
            for right in proven:
                if (
                    _native(right)
                    and right["server"] not in conflicts
                    and left["key"] == right["key"]
                    and left["author"] == right["author"]
                    and left["local_id"] == right["local_id"]
                    and counts[("native", right["source_key"], right["key"])] == 1
                    and _same_epoch(left, right)
                ):
                    scope = (
                        left["source_key"],
                        right["source_key"],
                        right["local_scope"],
                    )
                    anchors[scope][left["local_id"]].append((left, right, preferred))

    existing = {context["id"] for group in members.values() for context in group}
    for left in candidates:
        context = left.get("context", {})
        label = context.get("sender_label")
        if (
            left.get("unidentified_native_label") is not True
            or left.get("format") != "html"
            or left.get("author") != ""
            or context.get("contact_id") != ""
            or left["id"] in existing
            or left["id"] in updated_mapping
            or not isinstance(label, str)
            or _LABEL.fullmatch(label) is None
            or _key(left) is None
            or left["key"][2] != "received"
            or _source(left) is None
            or not _positive(left.get("local_id"))
            or not _positive(left.get("order"))
            or counts[("html", left["source_key"], left["key"])] != 1
        ):
            continue
        matches = []
        for right in natives[left["key"]]:
            if (
                right["context"].get("sender_id") != label
                or right["local_id"] != left["local_id"]
                or counts[("native", right["source_key"], right["key"])] != 1
                or not _same_epoch(left, right)
            ):
                continue
            proof = _anchor_proof(
                anchors[
                    (left["source_key"], right["source_key"], right["local_scope"])
                ],
                left,
                right,
                owner,
            )
            if proof:
                matches.append((right, proof))
        if (
            not matches
            or len({(right["server"], right["author"]) for right, _ in matches}) != 1
        ):
            continue
        group_contexts = {left["id"]: dict(context)}
        old_groups = set()
        for right, _ in matches:
            preferred = updated_mapping.get(right["id"], right["id"])
            if preferred in updated_members:
                old_groups.add(preferred)
                group_contexts.update(
                    (row["id"], row) for row in updated_members[preferred]
                )
            else:
                group_contexts[right["id"]] = right["context"]
        native_members = [
            records[identifier]
            for identifier in group_contexts
            if identifier in records and records[identifier].get("format") == "native"
        ]
        preferred = min(native_members, key=lambda item: (item["rank"], item["id"]))[
            "id"
        ]
        proof = min(matches, key=lambda pair: (pair[0]["rank"], pair[0]["id"]))[1]
        group_contexts[left["id"]].update(
            event_match_basis=MATCH_BASIS,
            event_match_anchor_ids=proof,
        )
        for identifier in old_groups:
            updated_members.pop(identifier)
        updated_members[preferred] = [
            group_contexts[identifier]
            for identifier in sorted(
                group_contexts,
                key=lambda identifier: (records[identifier]["rank"], identifier),
            )
        ]
        updated_mapping.pop(preferred, None)
        updated_mapping.update(
            (identifier, preferred)
            for identifier in group_contexts
            if identifier != preferred
        )
    return updated_mapping, updated_members
