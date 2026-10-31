"""Present verified HTML forwarding trees without modifying archive records.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import hashlib
import json
import re

from .chat_history_browser import ChatHistoryMessage
from .wechat_archive import _identifier, _time_fields
from .wechat_contacts import (
    MAX_FORWARDED_COMPONENTS,
    MAX_FORWARDED_DEPTH,
    MAX_FORWARDED_PROOF_BYTES,
)


_ID = re.compile(r"[0-9a-f]{64}")
_MAX_BYTES = 2 * 1024 * 1024
_HTML_CLOCK = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}")
_HTML_TEXT_KINDS = frozenset(
    {
        "plain_text",
        "rendered_app_text",
        "rendered_location_metadata",
        "rendered_channel_metadata",
        "forwarded_rendered_text",
        "rendered_forwarded_app_metadata",
        "forwarded_text",
        "quoted_reply",
        "notice",
        "system_text",
    }
)


def _child(
    *,
    stable_id,
    ordinal,
    text,
    author,
    sender,
    direction,
    timestamp,
    label,
    basis,
    provenance,
    flags=(),
    conversation_id="",
    account_id="",
    peer_id="",
):
    context = {
        "direction": direction,
        "sender_id": sender,
        "viewer_display_name": "",
        "timestamp_label": label,
        "time_basis": basis,
        "provenance": provenance,
        "quality_flags": tuple(flags),
        "display_text": text,
        "show_sender_name": True,
        "is_system_notice": False,
        "account_id": account_id,
        "peer_id": peer_id,
        "contact": {
            "platform": "wechat",
            "identity_status": "unresolved",
            "display_name": author,
            "source_display_name": author,
        },
    }
    return ChatHistoryMessage(
        stable_id=stable_id,
        source="wechat",
        conversation_id=conversation_id,
        conversation_url="",
        conversation_title="Forwarded conversation",
        message_key=stable_id,
        message_index=ordinal,
        role=direction,
        author_label=author,
        content_text=text,
        content_html="",
        source_links=(),
        model_label="",
        first_seen_at=timestamp,
        last_seen_at=timestamp,
        messaging=context,
    )


def project_html_forwarded(
    provenance,
    *,
    parent_id,
    account_id,
    text,
    components,
    source_proof_status,
    display_names=None,
):
    """Present an exact, source-verified HTML tree through the shared transcript.

    Component paths identify presentation rows, not inferred original messages or
    immediate forwarding parents. The existing contact resolver owns source and
    identity verification; this adapter additionally verifies the stored rendering.
    """
    locator = provenance.get("locator") if isinstance(provenance, dict) else None
    native_fields = {
        "message_type",
        "text_components",
        "forwarded_references",
        "appmsg_subtype",
        "unix_timestamp",
        "server_id",
    }
    if (
        source_proof_status != "source_proof_verified"
        or not isinstance(parent_id, str)
        or _ID.fullmatch(parent_id) is None
        or not isinstance(account_id, str)
        or not account_id
        or not isinstance(text, str)
        or not isinstance(provenance, dict)
        or not isinstance(locator, dict)
        or any(key in provenance or key in locator for key in native_fields)
        or provenance.get("export_msgid_namespace") != "MesLocalID"
        or locator.get("export_msgid_namespace") != "MesLocalID"
        or provenance.get("text_extraction_kind") not in _HTML_TEXT_KINDS
        or provenance.get("text_extraction_kind") in {"notice", "system_text"}
        or not isinstance(components, list)
        or not components
    ):
        return None

    def source_tree(values, depth=1, count=None):
        count = [0] if count is None else count
        if not isinstance(values, list) or depth > MAX_FORWARDED_DEPTH:
            raise ValueError("HTML forwarded structure exceeds the supported bounds.")
        result = []
        for value in values:
            count[0] += 1
            if not isinstance(value, dict) or count[0] > MAX_FORWARDED_COMPONENTS:
                raise ValueError(
                    "HTML forwarded structure exceeds the supported bounds."
                )
            if (
                not isinstance(value.get("text"), str)
                or value.get("text_kind") not in _HTML_TEXT_KINDS
                or not isinstance(value.get("sender_label", ""), str)
                or not isinstance(value.get("raw_timestamp", ""), str)
                or not isinstance(value.get("sender_export_id", ""), str)
                or value.get("direction", "unknown")
                not in {"sent", "received", "unknown"}
                or not isinstance(value.get("flags", []), list)
                or any(not isinstance(flag, str) for flag in value.get("flags", []))
            ):
                raise ValueError("Invalid HTML forwarded text component.")
            normalized = {
                key: field
                for key, field in value.items()
                if key not in {"contact", "forwarded_components"}
            }
            normalized["forwarded_components"] = (
                source_tree(value.get("forwarded_components", []), depth + 1, count)
                if value.get("forwarded_components", [])
                else []
            )
            if not isinstance(value.get("forwarded_components", []), list):
                raise ValueError("HTML forwarded children must remain a list.")
            result.append(normalized)
        return result

    def quote_texts(node):
        values = node.get("quoted_components", [])
        if not isinstance(values, list) or any(
            not isinstance(value, dict) or not isinstance(value.get("text"), str)
            for value in values
        ):
            raise ValueError("Invalid HTML quoted text components.")
        return [value["text"] for value in values if value["text"]]

    def render(node):
        children = node.get("forwarded_components", [])
        parts = quote_texts(node)
        if node["text"] and not (node["text_kind"] == "forwarded_text" and children):
            parts.append(node["text"])
        for child in children:
            speaker = child.get("sender_label") or "Unknown original sender"
            clock = child.get("raw_timestamp") or "Unknown original time"
            parts.append(f"[Forwarded text: {speaker} | {clock}]\n{render(child)}")
        return "\n\n".join(parts)

    def record(values, title, path=()):
        children = []
        for ordinal, component in enumerate(values, start=1):
            position = (*path, ordinal)
            flags = component.get("flags", [])
            clock = component.get("raw_timestamp", "")
            usable = (
                _HTML_CLOCK.fullmatch(clock) is not None
                and "unrecognized_export_timestamp" not in flags
            )
            _sort, timestamp, label, basis = _time_fields(
                {"sent_at_raw": clock, "timeline_timestamp_usable": usable}
            )
            raw = {key: value for key, value in component.items() if key != "contact"}
            child = _child(
                stable_id=_identifier(parent_id, "html_forwarded", *map(str, position)),
                ordinal=ordinal,
                text=render(component),
                author=component.get("sender_label") or "Unknown original sender",
                sender=component.get("sender_export_id", ""),
                direction=component.get("direction", "unknown"),
                timestamp=timestamp,
                label=label if timestamp else "",
                basis=basis,
                provenance={
                    "embedded_html_forwarded_component": raw,
                    "parent_message_id": parent_id,
                    "locator": component.get("source_locator", {}),
                    "source_time": clock,
                },
                flags=(*flags, "html_forwarded_source_assertion"),
                account_id=account_id,
            )
            contact = component.get("contact")
            if isinstance(contact, dict):
                child.messaging["contact"] = contact
                if display_names is not None:
                    child.messaging["viewer_display_name"] = display_names.contact_name(
                        contact
                    )
            child.messaging["sender_identifier_namespace"] = "unverified_export_field"
            child.messaging["is_system_notice"] = component["text_kind"] in {
                "notice",
                "system_text",
            }
            nested = component.get("forwarded_components", [])
            if nested:
                heading = quote_texts(component)
                if component["text_kind"] != "forwarded_text" and component["text"]:
                    heading.append(component["text"])
                child.messaging["forwarded_record"] = record(
                    nested, "\n\n".join(heading), position
                )
                child.messaging["display_text"] = ""
            children.append(child)
        return {
            "title": title or "Forwarded conversation",
            "items": tuple(children),
            "authority": "source_verified_html_export_tree",
            "status": "embedded_html_text",
        }

    try:
        if (
            len(text.encode()) > _MAX_BYTES
            or len(json.dumps(provenance, ensure_ascii=False, allow_nan=False).encode())
            > MAX_FORWARDED_PROOF_BYTES
        ):
            return None
        raw = source_tree(provenance.get("forwarded_components", []))
        projected = source_tree(components)
        if not raw or raw != projected:
            return None
        suffix = "\n\n".join(
            f"[Forwarded text: {child.get('sender_label') or 'Unknown original sender'}"
            f" | {child.get('raw_timestamp') or 'Unknown original time'}]\n{render(child)}"
            for child in raw
        )
        if text == suffix:
            heading = ""
        elif text.endswith("\n\n" + suffix):
            heading = text[: -len("\n\n" + suffix)]
        else:
            return None
        quoted = "\n\n".join(quote_texts(provenance))
        if quoted:
            if heading == quoted:
                primary = ""
            elif heading.startswith(quoted + "\n\n"):
                primary = heading[len(quoted) + 2 :]
            else:
                return None
        else:
            primary = heading
        if provenance["text_extraction_kind"] == "forwarded_text":
            if primary:
                return None
            primary = "\n\n".join(child["text"] for child in raw)
        if hashlib.sha256(primary.encode()).hexdigest() != provenance.get(
            "primary_text_sha256"
        ):
            return None
        return record(components, heading)
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
        return None
