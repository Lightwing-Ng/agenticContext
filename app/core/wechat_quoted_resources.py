"""Present native nontext quote references without opening attachment payloads.

Code version: v1.1.1-codex.0
"""

from __future__ import annotations

from collections import defaultdict

from .wechat_quote_padding import MAX_PAGE_ITEMS, _HEX, _key, _proof, _server
from .wechat_quoted_contacts import _exact_original_quoted_author, _native_quote
from .wechat_reconcile import native_text_metadata
from .wechat_resource_index import _KINDS
from .wechat_resources import present_resource, resource_reference
from .wechat_text import native_components_text


VERSION = "v1.1.1-codex.0"
REFERENCE_BASIS = "native_nontext_quote_type_v1"
ORIGINAL_BASIS = "exact_native_resource_original_v1"
NESTED_REFERENCE_BASIS = "native_forwarded_nontext_quote_type_v1"
MAX_NESTED_ITEMS = 4_096
MAX_NESTED_DEPTH = 32


def _missing_resource_quote(item, *, embedded=False):
    candidate = _native_quote(item.messaging)
    if candidate is None:
        return None
    quote, reference = candidate
    proof = item.messaging["provenance"]
    component = proof.get("embedded_forwarded_component")
    if embedded:
        if not isinstance(component, dict) or component.get("kind") != "forwarded_quote":
            return None
        proof = {
            "message_type": 49,
            "text_extraction_kind": "quoted_reply",
            "appmsg_subtype": 57,
            "text_components": component.get("text_components"),
            "quoted_reference": component.get("quoted_reference"),
        }
    elif "embedded_forwarded_component" in proof:
        return None
    message_type = reference.get("original_message_type")
    if (
        type(message_type) is not int
        or message_type not in _KINDS
        or quote.get("forwarded_record")
        or quote.get("payment")
    ):
        return None
    try:
        metadata = native_text_metadata(proof, proof.get("locator", {}))
        components = metadata.get("text_components", [])
        if (
            metadata.get("appmsg_subtype") != 57
            or len(components) != 1
            or components[0].get("kind") != "reply_text"
            or native_components_text(components) != item.content_text
        ):
            return None
    except (ValueError, TypeError, KeyError, RecursionError):
        return None
    return quote, reference, _KINDS[message_type]


def _exact_original(item, reference, matches, current, partitions, names, snapshots):
    if len(matches) != 1:
        return None
    original = matches[0]
    if (
        original.stable_id == item.stable_id
        or not isinstance(original.stable_id, str)
        or _HEX.fullmatch(original.stable_id) is None
    ):
        return None
    previous = _proof(original, partitions, names, snapshots)
    if previous is None or previous[0] != current[0]:
        return None
    proof, old_proof = current[2], previous[2]
    message_type = reference["original_message_type"]
    epoch = reference.get("source_timestamp")
    if (
        previous[1] >= current[1]
        or type(old_proof.get("message_type")) is not int
        or old_proof["message_type"] != message_type
        or old_proof.get("text_extraction_kind") != "resource_placeholder"
        or old_proof["unix_timestamp"] > proof["unix_timestamp"]
        or not _server(epoch)
        or int(epoch) != old_proof["unix_timestamp"]
    ):
        return None
    resource = resource_reference(
        old_proof, original.messaging["account_id"], original.messaging["peer_id"]
    )
    if (
        resource is None
        or ("availability" in resource and resource["availability"] != "metadata_only")
        or type(resource.get("unix_timestamp")) is not int
        or resource["unix_timestamp"] != old_proof["unix_timestamp"]
    ):
        return None
    allowed = {"app", "file"} if message_type == 49 else {_KINDS[message_type]}
    if resource["kind"] not in allowed:
        return None
    if not _exact_original_quoted_author(
        reference,
        current[0][1],
        original.messaging.get("sender_id"),
        item.messaging.get("sender_id"),
    ):
        return None
    # Legacy iOS catalogs omit availability and retain a top-level Mac resolver.
    # The independently scoped original still proves this inert quote copy.
    return original, {**resource, "availability": "metadata_only"}


def _originals_by_server(items, originals):
    by_server = defaultdict(list)
    if (
        not isinstance(items, (list, tuple))
        or not isinstance(originals, (list, tuple))
        or len(items) > MAX_PAGE_ITEMS
        or len(originals) > MAX_PAGE_ITEMS
    ):
        return by_server
    for original in [*items, *originals]:
        proof = original.messaging.get("provenance")
        key = (
            _key(original, proof.get("server_id")) if isinstance(proof, dict) else None
        )
        if key is not None and all(value is not original for value in by_server[key]):
            by_server[key].append(original)
    return by_server


def _exact_resource_original(item, by_server, partitions, names, snapshots):
    candidate = _missing_resource_quote(item)
    if candidate is None:
        return None
    _quote, reference, _kind = candidate
    current = _proof(item, partitions, names, snapshots)
    if current is None:
        return None
    key = _key(item, reference.get("source_ids", {}).get("svrid"))
    return _exact_original(
        item, reference, by_server.get(key, ()), current, partitions, names, snapshots
    )


def _project_nested_resources(items, partitions, names, snapshots):
    remaining = MAX_NESTED_ITEMS
    source_remaining = MAX_NESTED_ITEMS

    def source_quotes(parent):
        nonlocal source_remaining
        if _proof(parent, partitions, names, snapshots) is None:
            return None
        proof = parent.messaging["provenance"]
        if proof.get("message_type") != 49 or proof.get("text_extraction_kind") not in {
            "forwarded_text", "quoted_reply"
        }:
            return None
        try:
            metadata = native_text_metadata(proof, proof.get("locator", {}))
            components = metadata.get("text_components", [])
            if native_components_text(components) != parent.content_text:
                return None
        except (ValueError, TypeError, KeyError, RecursionError):
            return None
        found = []
        stack = [(components, 0)]
        while stack and source_remaining > 0:
            values, depth = stack.pop()
            if depth > MAX_NESTED_DEPTH or not isinstance(values, (list, tuple)):
                continue
            for value in values:
                if source_remaining <= 0:
                    break
                source_remaining -= 1
                if not isinstance(value, dict):
                    continue
                if value.get("kind") == "forwarded_quote":
                    found.append(value)
                stack.append((value.get("text_components", []), depth + 1))
        return found

    def visit(record, components, *, trusted=True, depth=0, seen=None):
        nonlocal remaining
        if (
            depth > MAX_NESTED_DEPTH
            or remaining <= 0
            or not isinstance(record, dict)
            or not isinstance(record.get("items"), (list, tuple))
        ):
            return
        seen = set() if seen is None else seen
        if id(record) in seen:
            return
        seen.add(id(record))
        trusted = trusted and record.get("authority") == "native_forwarded_envelope"
        for child in record["items"]:
            if remaining <= 0:
                break
            remaining -= 1
            context = getattr(child, "messaging", None)
            if not isinstance(context, dict):
                continue
            quote = context.get("quoted_reply")
            if isinstance(quote, dict) and quote.get("resource_basis") == NESTED_REFERENCE_BASIS:
                for field in ("resource", "resource_basis", "resource_original"):
                    quote.pop(field, None)
            candidate = _missing_resource_quote(child, embedded=True)
            proof = context.get("provenance")
            component = proof.get("embedded_forwarded_component") if isinstance(proof, dict) else None
            if trusted and components is not None and candidate is not None and component in components:
                quote, _reference, kind = candidate
                if "resource" not in quote:
                    quote["resource_basis"] = NESTED_REFERENCE_BASIS
                    quote["resource"] = present_resource(
                        {"kind": kind, "availability": "metadata_only"}, None
                    )
            visit(
                context.get("forwarded_record"), components,
                trusted=trusted, depth=depth + 1, seen=seen,
            )
            if isinstance(quote, dict):
                visit(
                    quote.get("forwarded_record"), components,
                    trusted=trusted, depth=depth + 1, seen=seen,
                )

    for parent in items:
        if remaining <= 0:
            break
        context = parent.messaging
        quote = context.get("quoted_reply")
        records = [context.get("forwarded_record")]
        if isinstance(quote, dict):
            records.append(quote.get("forwarded_record"))
        if any(isinstance(record, dict) for record in records):
            components = source_quotes(parent)
            seen = set()
            for record in records:
                visit(record, components, seen=seen)


def project_native_quoted_resources(
    items, display_names, account_rows, source_snapshots, *, originals=()
):
    """Add typed display metadata; optionally link an exact retained original.

    The caller supplies already accepted, source-scoped resource rows. This
    bounded projection performs no database, file, cloud, or payload reads.
    A type-only placeholder does not assert that the original was recovered.
    Original rows, quote text, native references, and all clocks stay unchanged.
    """
    if (
        not isinstance(items, (list, tuple))
        or not isinstance(originals, (list, tuple))
        or len(items) > MAX_PAGE_ITEMS
        or len(originals) > MAX_PAGE_ITEMS
    ):
        return
    partitions = {row["partition_id"]: row for row in account_rows}
    by_server = _originals_by_server(items, originals)
    for item in items:
        quote = item.messaging.get("quoted_reply")
        if isinstance(quote, dict) and quote.get("resource_basis") in {
            REFERENCE_BASIS,
            ORIGINAL_BASIS,
        }:
            for field in ("resource", "resource_basis", "resource_original"):
                quote.pop(field, None)
        candidate = _missing_resource_quote(item)
        if candidate is None:
            continue
        quote, reference, kind = candidate
        current = _proof(item, partitions, display_names, source_snapshots)
        if current is None:
            continue
        resource = {
            "kind": kind,
            "availability": "metadata_only",
            "account_id": item.messaging["account_id"],
            "peer_id": item.messaging["peer_id"],
        }
        quote["resource_basis"] = REFERENCE_BASIS
        key = _key(item, reference.get("source_ids", {}).get("svrid"))
        original = _exact_original(
            item,
            reference,
            by_server.get(key, ()),
            current,
            partitions,
            display_names,
            source_snapshots,
        )
        if original is not None:
            original_item, resource = original
            quote["resource_basis"] = ORIGINAL_BASIS
            quote["resource_original"] = {
                "message_id": original_item.stable_id,
                "server_id": original_item.messaging["provenance"]["server_id"],
                "source_snapshot": original_item.messaging["provenance"][
                    "source_snapshot"
                ],
            }
        # Metadata-only presentation returns before consulting any resolver.
        quote["resource"] = present_resource(resource, None)
    _project_nested_resources(items, partitions, display_names, source_snapshots)
