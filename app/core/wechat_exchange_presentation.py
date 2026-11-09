"""Present imported snapshots as external claims without native authority.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import hashlib
import json

from .wechat_contacts import MAX_FORWARDED_COMPONENTS, MAX_FORWARDED_DEPTH


VERSION = "v1.0.0-codex.0"


def _frozen(provenance, context):
    if (
        not isinstance(provenance, dict)
        or context.get("account_id") is not None
        or context.get("identity_status") != "Imported origin claim"
        or not str(context.get("partition_id", "")).startswith("xchg-")
    ):
        return None
    exchange = provenance.get("exchange")
    if (
        not isinstance(exchange, dict)
        or exchange.get("identity_authority") != "external_origin_claim"
    ):
        return None
    frozen = exchange.get("frozen_presentation")
    if (
        not isinstance(frozen, dict)
        or type(frozen.get("schema_version")) is not int
        or frozen["schema_version"] != 1
        or frozen.get("authority") != "frozen_external_presentation"
        or frozen.get("source_message_id") != exchange.get("original_message_id")
        or frozen.get("source_provenance") != exchange.get("original_provenance_raw")
    ):
        return None
    return frozen


def frozen_forwarded_items(provenance, context, *, parent_id):
    """Keep literal child text and clocks while rebinding every presentation ID."""
    frozen = _frozen(provenance, context)
    if frozen is None:
        return ()
    values = frozen.get("forwarded_items", ())
    if not isinstance(values, list) or len(values) > MAX_FORWARDED_COMPONENTS:
        return ()
    result, paths = [], set()
    for value in values:
        if not isinstance(value, dict):
            return ()
        path = value.get("component_path")
        if (
            not isinstance(path, list)
            or not 1 <= len(path) <= MAX_FORWARDED_DEPTH
            or any(
                type(number) is not int or not 1 <= number <= MAX_FORWARDED_COMPONENTS
                for number in path
            )
            or tuple(path) in paths
            or any(
                not isinstance(value.get(key, ""), str)
                for key in (
                    "text",
                    "sender_id",
                    "sender_label",
                    "viewer_display_name",
                    "timestamp_value",
                    "timestamp_label",
                    "time_basis",
                    "source_clock",
                )
            )
        ):
            return ()
        paths.add(tuple(path))
        identifier = hashlib.sha256(
            json.dumps(
                ["exchange-child", parent_id, path], separators=(",", ":")
            ).encode()
        ).hexdigest()
        result.append(
            {
                **{
                    key: value.get(key, "")
                    for key in (
                        "text",
                        "sender_id",
                        "sender_label",
                        "viewer_display_name",
                        "timestamp_value",
                        "timestamp_label",
                        "time_basis",
                        "source_clock",
                    )
                },
                "id": identifier,
                "component_path": tuple(path),
                "provenance": {
                    "exchange": {
                        "identity_authority": "external_origin_claim",
                        "original_child_id": value.get("id", ""),
                        "component_path": path,
                        "source_provenance": value.get("provenance", {}),
                        "local_resources_available": False,
                    }
                },
            }
        )
    return tuple(sorted(result, key=lambda value: value["component_path"]))


def _resource(provenance):
    from .wechat_resources import RESOURCE_LABELS

    reference = (
        provenance.get("resource_reference") if isinstance(provenance, dict) else None
    )
    if (
        not isinstance(reference, dict)
        or not isinstance(reference.get("kind"), str)
        or reference["kind"] not in RESOURCE_LABELS
    ):
        return None
    kind = reference["kind"]
    label = (
        reference.get("filename")
        or reference.get("safe_title")
        or reference.get("label")
        or RESOURCE_LABELS[kind]
    )
    if not isinstance(label, str) or len(label) > 1024:
        label = RESOURCE_LABELS[kind]
    return {
        "kind": kind,
        "kind_label": RESOURCE_LABELS[kind],
        "label": label,
        "status": "metadata_only",
        "status_label": "Metadata only · imported source",
        "action_label": "",
        "can_reveal": False,
    }


def _snapshot_payment(value):
    if not isinstance(value, dict):
        return None
    payment = {
        key: value[key]
        for key in ("kind", "currency", "label", "currency_label", "display_text")
        if isinstance(value.get(key), str)
    }
    parts = value.get("text_parts")
    if isinstance(parts, list) and len(parts) <= 128:
        payment["text_parts"] = [
            {
                key: part[key]
                for key in ("text", "lang", "message_key")
                if isinstance(part.get(key), str)
            }
            for part in parts
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
    return payment


def _snapshot_context(destination, source, parent_id, path, budget):
    """Copy literal presentation roles without contact or filesystem capabilities."""
    if not isinstance(source, dict):
        return
    for key in ("display_text", "viewer_display_name"):
        if isinstance(source.get(key), str):
            destination[key] = source[key]
    if type(source.get("is_system_notice")) is bool:
        destination["is_system_notice"] = source["is_system_notice"]
    resource = _resource({"resource_reference": source.get("resource")})
    if resource is not None:
        destination["resource"] = resource
    payment = _snapshot_payment(source.get("payment"))
    if payment is not None:
        destination["payment"] = payment
    fields = source.get("record_fields")
    if isinstance(fields, list) and len(fields) <= 128:
        destination["record_fields"] = tuple(
            {"label": field["label"], "value": field["value"]}
            for field in fields
            if isinstance(field, dict)
            and isinstance(field.get("label"), str)
            and isinstance(field.get("value"), str)
        )
    notices = source.get("system_notices")
    if isinstance(notices, list) and len(notices) <= 128:
        destination["system_notices"] = tuple(
            {key: notice[key] for key in ("text", "source_start", "source_end")}
            for notice in notices
            if isinstance(notice, dict)
            and isinstance(notice.get("text"), str)
            and type(notice.get("source_start")) is int
            and type(notice.get("source_end")) is int
        )
    quote = source.get("quoted_reply")
    if isinstance(quote, dict):
        projected = {
            key: quote[key]
            for key in (
                "speaker",
                "text",
                "display_text",
                "viewer_display_name",
            )
            if isinstance(quote.get(key), str)
        }
        quoted_resource = _resource({"resource_reference": quote.get("resource")})
        if quoted_resource is not None:
            projected["resource"] = quoted_resource
        quoted_payment = _snapshot_payment(quote.get("payment"))
        if quoted_payment is not None:
            projected["payment"] = quoted_payment
        nested = _snapshot_record(
            quote.get("forwarded_record"), parent_id, (*path, "quote"), budget
        )
        if nested is not None:
            projected["forwarded_record"] = nested
        destination["quoted_reply"] = projected
    nested = _snapshot_record(
        source.get("forwarded_record"), parent_id, (*path, "forward"), budget
    )
    if nested is not None:
        destination["forwarded_record"] = nested


def _snapshot_record(value, parent_id, path, budget):
    from .wechat_forwarded import _child

    if not isinstance(value, dict) or len(path) > MAX_FORWARDED_DEPTH * 2:
        return None
    values = value.get("items")
    if not isinstance(values, list) or len(values) > MAX_FORWARDED_COMPONENTS:
        return None
    children = []
    for ordinal, source in enumerate(values, 1):
        budget[0] += 1
        if budget[0] > MAX_FORWARDED_COMPONENTS or not isinstance(source, dict):
            return None
        metadata = source.get("metadata")
        if not isinstance(metadata, dict):
            return None
        child_path = (*path, ordinal)
        identifier = hashlib.sha256(
            json.dumps(
                ["exchange-snapshot-child", parent_id, child_path],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        direction = metadata.get("direction", "unknown")
        if not isinstance(direction, str) or direction not in {
            "sent",
            "received",
            "unknown",
        }:
            direction = "unknown"

        def literal(mapping, key, default=""):
            result = mapping.get(key)
            return result if isinstance(result, str) else default

        child = _child(
            stable_id=identifier,
            ordinal=ordinal,
            text=literal(source, "content_text"),
            author=literal(source, "author_label"),
            sender=literal(metadata, "sender_id"),
            direction=direction,
            timestamp=literal(source, "timestamp_value"),
            label=literal(metadata, "timestamp_label"),
            basis=literal(metadata, "time_basis", "Unavailable source time"),
            provenance={
                "exchange": {
                    "identity_authority": "external_origin_claim",
                    "original_child_id": literal(source, "stable_id"),
                    "snapshot_path": child_path,
                    "local_resources_available": False,
                }
            },
            flags=("external_origin_claim",),
        )
        child.messaging["contact"]["registry_persisted"] = False
        child.messaging["contact"]["resolution_reason"] = "external_origin_claim"
        _snapshot_context(child.messaging, metadata, parent_id, child_path, budget)
        children.append(child)
    label = value.get("presentation_label")
    return {
        "title": value.get("title")
        if isinstance(value.get("title"), str)
        else "Chat history",
        "source_title": value.get("source_title")
        if isinstance(value.get("source_title"), str)
        else "",
        "items": tuple(children),
        "authority": "external_origin_claim",
        "presentation_label": "Imported source"
        + (" · " + label if isinstance(label, str) and label else ""),
        "preview_text": value.get("preview_text")
        if isinstance(value.get("preview_text"), str)
        else "",
    }


def apply_frozen_presentation(item):
    """Use source-scoped presentation snapshots after local proof projections finish."""
    from .wechat_forwarded import _child

    context = item.messaging
    provenance = context.get("provenance")
    frozen = _frozen(provenance, context)
    if frozen is None:
        return
    exchange = provenance["exchange"]
    name = exchange.get("viewer_display_name")
    if isinstance(name, str) and name:
        context["viewer_display_name"] = name
    context["exchange_presentation_authority"] = "external_origin_claim"
    observations = frozen.get("event_members", ())
    if isinstance(observations, list) and len(observations) <= MAX_FORWARDED_COMPONENTS:
        projected = []
        seen = set()
        for observation in observations:
            if (
                not isinstance(observation, dict)
                or not observation.get("id")
                or any(
                    not isinstance(observation.get(key, ""), str)
                    for key in (
                        "id",
                        "source_label",
                        "source_snapshot_id",
                        "timestamp_value",
                        "source_clock",
                    )
                )
            ):
                projected = []
                break
            if observation["id"] in seen:
                projected = []
                break
            seen.add(observation["id"])
            projected.append(
                {
                    **{
                        key: observation.get(key, "")
                        for key in (
                            "source_label",
                            "source_snapshot_id",
                            "timestamp_value",
                            "source_clock",
                        )
                    },
                    "id": hashlib.sha256(
                        json.dumps(
                            [
                                "exchange-observation",
                                item.stable_id,
                                observation.get("id", ""),
                            ]
                        ).encode()
                    ).hexdigest(),
                    "clock_disagreement": observation.get("clock_disagreement") is True,
                }
            )
        if projected:
            context["source_observations"] = projected
    resource = _resource(exchange.get("original_provenance"))
    if resource is not None:
        context["resource"] = resource
    presentation = frozen.get("presentation")
    if isinstance(presentation, dict):
        _snapshot_context(context, presentation, item.stable_id, (), [0])
        return
    leaves = frozen_forwarded_items(provenance, context, parent_id=item.stable_id)
    if not leaves:
        return
    tree = {}
    for leaf in leaves:
        node = tree
        for number in leaf["component_path"]:
            node = node.setdefault(number, {})
        if "leaf" in node:
            return
        node["leaf"] = leaf

    def record(nodes, prefix=()):
        children = []
        for number in sorted(key for key in nodes if isinstance(key, int)):
            node = nodes[number]
            path = (*prefix, number)
            leaf = node.get("leaf", {})
            identifier = (
                leaf.get("id")
                or hashlib.sha256(
                    json.dumps(["exchange-group", item.stable_id, path]).encode()
                ).hexdigest()
            )
            child = _child(
                stable_id=identifier,
                ordinal=number,
                text=leaf.get("text", ""),
                author=leaf.get("sender_label", ""),
                sender=leaf.get("sender_id", ""),
                direction="unknown",
                timestamp=leaf.get("timestamp_value", ""),
                label=leaf.get("timestamp_label", ""),
                basis=leaf.get("time_basis", "Unavailable source time"),
                provenance=leaf.get(
                    "provenance",
                    {"exchange": {"identity_authority": "external_origin_claim"}},
                ),
                flags=("external_origin_claim",),
                conversation_id=item.conversation_id,
            )
            child.messaging["viewer_display_name"] = leaf.get("viewer_display_name", "")
            child.messaging["contact"]["registry_persisted"] = False
            child.messaging["contact"]["resolution_reason"] = "external_origin_claim"
            child_resource = _resource(
                leaf.get("provenance", {}).get("exchange", {}).get("source_provenance")
            )
            if child_resource is not None:
                child.messaging["resource"] = child_resource
            if any(isinstance(key, int) for key in node):
                child.messaging["forwarded_record"] = record(node, path)
                child.messaging["display_text"] = ""
            children.append(child)
        return {
            "title": "Chat history",
            "source_title": "",
            "items": tuple(children),
            "authority": "external_origin_claim",
            "presentation_label": "Imported source",
        }

    snapshot = record(tree)
    if isinstance(context.get("quoted_reply"), dict):
        context["quoted_reply"]["forwarded_record"] = snapshot
    else:
        context["forwarded_record"] = snapshot
        context["display_text"] = ""
