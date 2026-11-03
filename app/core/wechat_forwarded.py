"""Project scoped forwarded references without copying bodies or opening media.

Code version: v1.4.0-codex.0
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .chat_history_browser import ChatHistoryMessage
from .wechat_archive import _identifier, _time_fields, wechat_archive_path
from .wechat_contacts import (
    MAX_FORWARDED_COMPONENTS,
    MAX_FORWARDED_DEPTH,
    MAX_FORWARDED_PROOF_BYTES,
    WechatContactRepository,
)
from .wechat_display_names import WechatDisplayNames
from .wechat_html_export import normalize_html_clock
from .wechat_reconcile import _database_identity, native_text_metadata
from .wechat_resource_index import resource_index_path
from .wechat_resources import RESOURCE_LABELS
from .wechat_text import native_components_text


_ID = re.compile(r"[0-9a-f]{64}")
_MAX_BYTES = 2 * 1024 * 1024
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
    resource=None,
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
    if resource is not None:
        context["resource"] = resource
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


def _epoch_placeholder_clock(raw):
    """Recognize source epoch-zero renderings without assigning them a timezone."""
    if not isinstance(raw, str):
        return False
    return bool(
        re.fullmatch(r"0+", raw.strip())
        or re.match(r"1970-0?1-0?1(?:[ T]|$)", raw.strip())
    )


def forwarded_component_clock(component, *, native=False):
    """Project only a child's own clock, preserving missing and placeholder times."""
    flags = list(dict.fromkeys(component.get("flags", [])))
    values = {"flags": flags}
    precision = ""
    if native:
        epoch = component.get("source_create_time")
        if isinstance(epoch, str) and re.fullmatch(r"0+", epoch.strip()):
            flags.append("possible_epoch_zero_placeholder")
        elif isinstance(epoch, str) and re.fullmatch(r"[1-9][0-9]{0,11}", epoch):
            try:
                values["sent_at_utc_iso"] = datetime.fromtimestamp(
                    int(epoch), UTC
                ).isoformat()
                if _epoch_placeholder_clock(values["sent_at_utc_iso"]):
                    flags.append("possible_epoch_zero_placeholder")
            except (ValueError, OverflowError, OSError):
                pass
        if "sent_at_utc_iso" not in values:
            raw = component.get("source_timestamp", "")
            if _epoch_placeholder_clock(raw):
                flags.append("possible_epoch_zero_placeholder")
            values["sent_at_raw"] = raw
    else:
        raw = component.get("raw_timestamp", "")
        if _epoch_placeholder_clock(raw):
            flags.append("possible_epoch_zero_placeholder")
        normalized, precision = normalize_html_clock(raw)
        values.update(
            sent_at_raw=normalized,
            timeline_timestamp_usable=bool(normalized),
        )
    _sort, timestamp, label, basis = _time_fields(values)
    if not native and timestamp:
        if "unrecognized_export_timestamp" in flags:
            flags.remove("unrecognized_export_timestamp")
            flags.append("recovered_html_source_clock")
        if precision == "minute":
            basis += "; precision: minute (seconds unavailable)"
            flags.append("export_timestamp_precision_minutes")
    return timestamp, label if timestamp else "", basis, tuple(dict.fromkeys(flags))


def _legacy_forwarded_tree(components):
    """Recover only explicit legacy path boundaries, without inventing wrapper metadata."""
    if not components or any(item["kind"] != "forwarded_text" for item in components):
        return components
    paths = [item.get("forwarded_item_path", []) for item in components]
    if (
        any(not path or len(path) > 9 for path in paths)
        or not any(len(path) > 1 for path in paths)
        or paths != sorted(paths)
        or any(right[: len(left)] == left for left, right in zip(paths, paths[1:]))
    ):
        return components

    def group(values, prefix=()):
        result = []
        offset = 0
        while offset < len(values):
            component = values[offset]
            path = tuple(component["forwarded_item_path"])
            current = path[: len(prefix) + 1]
            if path == current:
                result.append(component)
                offset += 1
                continue
            end = offset + 1
            while (
                end < len(values)
                and tuple(values[end]["forwarded_item_path"])[: len(current)] == current
            ):
                end += 1
            children = group(values[offset:end], current)
            result.append(
                {
                    "kind": "forwarded_group",
                    "text": native_components_text(children),
                    "text_components": children,
                    "forwarded_item_path": list(current),
                    "flags": ["legacy_forwarded_boundary_from_item_path"],
                }
            )
            offset = end
        return result

    return group(components)


def project_native_forwarded(provenance, *, parent_id, account_id):
    """Reuse literal native forwarding components without inventing original messages."""
    if (
        not isinstance(parent_id, str)
        or _ID.fullmatch(parent_id) is None
        or not isinstance(account_id, str)
        or not account_id
        or not isinstance(provenance, dict)
        or type(provenance.get("message_type")) is not int
        or provenance["message_type"] != 49
    ):
        return None
    kind = provenance.get("text_extraction_kind")
    if kind not in {"forwarded_text", "quoted_reply"}:
        return None
    locator = provenance.get("locator", {})
    if not isinstance(locator, dict):
        return None
    try:
        metadata = native_text_metadata(provenance, locator)
    except (ValueError, TypeError, RecursionError):
        return None
    if "forwarded_references" in metadata:
        # An unresolved explicit reference must not silently become embedded content.
        return None
    components = metadata.get("text_components", [])
    if kind == "quoted_reply":
        bundles = [
            item for item in components if item["kind"] == "quoted_forwarded_text"
        ]
        if len(bundles) != 1 or metadata.get("appmsg_subtype") not in {None, 57}:
            return None
        bundle = bundles[0]
        components = bundle.get("text_components", [])
    else:
        if metadata.get("appmsg_subtype") not in {None, 19}:
            return None
        bundle = metadata
    if not components or any(
        item["kind"]
        not in {
            "forwarded_text",
            "forwarded_resource",
            "forwarded_group",
            "forwarded_quote",
        }
        for item in components
    ):
        return None
    components = _legacy_forwarded_tree(components)

    def record(values, title, path=()):
        children = []
        for ordinal, component in enumerate(values, start=1):
            position = (*path, ordinal)
            sender = component.get("source_sender_id", "")
            author = component.get("source_sender_label") or sender or "Unknown sender"
            timestamp, label, basis, clock_flags = forwarded_component_clock(
                component, native=True
            )
            resource = None
            if component["kind"] == "forwarded_resource":
                resource_kind = component["resource_kind"]
                resource = {
                    "kind": resource_kind,
                    "kind_label": RESOURCE_LABELS[resource_kind],
                    "label": RESOURCE_LABELS[resource_kind],
                    "status": "metadata_only",
                    "status_label": "Metadata only",
                    "action_label": "",
                    "can_reveal": False,
                }
            child = _child(
                stable_id=_identifier(
                    parent_id, "embedded_forward", *map(str, position)
                ),
                ordinal=ordinal,
                text=component["text"],
                author=author,
                sender=sender,
                direction="sent"
                if sender == account_id
                else "received"
                if sender
                else "unknown",
                timestamp=timestamp,
                label=label,
                basis=basis,
                provenance={
                    "embedded_forwarded_component": component,
                    "parent_message_id": parent_id,
                    "source_time": component.get("source_timestamp")
                    or component.get("source_create_time", ""),
                },
                flags=(
                    *clock_flags,
                    "embedded_forwarded_source_assertion",
                ),
                account_id=account_id,
                resource=resource,
            )
            if component["kind"] == "forwarded_group":
                child.messaging["forwarded_record"] = record(
                    component["text_components"],
                    component.get("forwarded_record_title"),
                    position,
                )
                child.messaging["display_text"] = ""
            elif component["kind"] == "forwarded_quote":
                # Reuse the native quote roles; this import avoids a module-load cycle.
                from .wechat_history_browser import _native_quoted_reply_presentation

                quote_evidence = {
                    "message_type": 49,
                    "text_extraction_kind": "quoted_reply",
                    "appmsg_subtype": 57,
                    "text_components": component["text_components"],
                    "quoted_reference": component["quoted_reference"],
                }
                child.messaging.update(
                    _native_quoted_reply_presentation(quote_evidence)
                )
                nested = project_native_forwarded(
                    quote_evidence, parent_id=child.stable_id, account_id=account_id
                )
                if nested is not None and child.messaging.get("quoted_reply"):
                    child.messaging["quoted_reply"]["forwarded_record"] = nested
            children.append(child)
        return {
            "title": title or "Forwarded conversation",
            "items": tuple(children),
            "authority": "native_forwarded_envelope",
            "status": "embedded_text",
        }

    return record(components, bundle.get("forwarded_record_title"))


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
            clock = component.get("raw_timestamp", "")
            timestamp, label, basis, projection_flags = forwarded_component_clock(
                component
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
                flags=(*projection_flags, "html_forwarded_source_assertion"),
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
            "source_title": title or "",
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


def _read_only(path):
    connection = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _message(connection, identifier):
    row = connection.execute(
        "SELECT m.*,c.partition_id,c.account_key,c.peer_id,c.kind,p.account_id,"
        "p.account_key AS partition_account_key,p.identity_status "
        "FROM messages m JOIN conversations c ON c.id=m.conversation_id "
        "JOIN partitions p ON p.id=c.partition_id WHERE m.id=?",
        (identifier,),
    ).fetchone()
    return dict(row) if row is not None else None


def _native_owner(row):
    owner = row.get("account_id")
    return (
        isinstance(owner, str)
        and bool(owner)
        and row["account_key"] == row["partition_account_key"] == "account:" + owner
        and row["partition_id"] == "ios-" + hashlib.md5(owner.encode()).hexdigest()
        and row["identity_status"] == "Verified archive ID"
    )


def _decoded(value):
    if not isinstance(value, str) or len(value.encode()) > _MAX_BYTES:
        raise ValueError("Forwarded source evidence exceeds the supported size.")
    result = json.loads(value)
    if not isinstance(result, dict):
        raise ValueError("Forwarded source evidence must be an object.")
    return result


def project_forwarded_references(
    local_store_root, *, parent_message_id, provenance=None, history_path=None
):
    """Resolve explicit canonical IDs within a published native parent's account.

    An optional provenance preview still requires the exact existing native parent
    identity and scope. Relationships retain their stated user-confirmation authority.
    """
    if (
        not isinstance(parent_message_id, str)
        or _ID.fullmatch(parent_message_id) is None
    ):
        return None
    path = (
        Path(history_path)
        if history_path is not None
        else wechat_archive_path(local_store_root)
    )
    if not path.is_file() or path.is_symlink():
        return None
    connection = resources = None
    try:
        initial = _database_identity(path)
        if any(initial[index] and initial[index][2] for index in (1, 2)):
            return None
        connection = _read_only(path)
        parent = _message(connection, parent_message_id)
        if parent is None or not _native_owner(parent):
            return None
        original = _decoded(parent["provenance"])
        if provenance is None:
            provenance = original
        elif not isinstance(provenance, dict) or any(
            provenance.get(key) != original.get(key)
            for key in (
                "record_id",
                "locator",
                "server_id",
                "unix_timestamp",
                "source_snapshot",
            )
        ):
            return None
        if (
            type(provenance.get("message_type")) is not int
            or provenance["message_type"] != 49
            or provenance.get("text_extraction_kind")
            not in {"forwarded_text", "quoted_reply"}
            or not isinstance(provenance.get("locator", {}), dict)
        ):
            return None
        metadata = native_text_metadata(provenance, provenance.get("locator", {}))
        declaration = metadata.get("forwarded_references")
        if declaration is None:
            return None
        children, requests = [], []
        resource_path = resource_index_path(local_store_root)
        resource_identity = None
        body_bytes = 0
        snapshots = {}
        has_snapshots = (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_snapshots'"
            ).fetchone()
            is not None
        )
        for ordinal, item in enumerate(declaration["items"], start=1):
            if item["account_id"] != parent["account_id"]:
                return None
            resource = None
            if item["kind"] == "message":
                if item["message_id"] == parent_message_id:
                    return None
                row = _message(connection, item["message_id"])
                if row is None or not _native_owner(row):
                    return None
                evidence = _decoded(row["provenance"])
            else:
                if resources is None:
                    if not resource_path.is_file() or resource_path.is_symlink():
                        return None
                    resource_identity = _database_identity(resource_path)
                    if any(
                        resource_identity[index] and resource_identity[index][2]
                        for index in (1, 2)
                    ):
                        return None
                    resources = _read_only(resource_path)
                candidate = resources.execute(
                    "SELECT m.*,c.partition_id,c.account_key,c.peer_id,c.kind "
                    "FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.id=?",
                    (item["resource_id"],),
                ).fetchone()
                if candidate is None:
                    return None
                row = dict(candidate)
                row.update(
                    {
                        key: parent[key]
                        for key in (
                            "account_id",
                            "partition_account_key",
                            "identity_status",
                        )
                    }
                )
                evidence = _decoded(row["provenance"])
                saved_locator = evidence.get("locator", {})
                saved_reference = evidence.get("resource_reference", {})
                if (
                    not _native_owner(row)
                    or evidence.get("message_type") != item["message_type"]
                    or evidence.get("unix_timestamp") != item["unix_timestamp"]
                    or not isinstance(saved_locator, dict)
                    or any(
                        saved_locator.get(key) != value
                        for key, value in item["source_locator"].items()
                    )
                    or not isinstance(saved_reference, dict)
                    or saved_reference.get("kind") != "image"
                    or saved_reference.get("account_id") != item["account_id"]
                    or saved_reference.get("peer_id") != item["peer_id"]
                ):
                    return None
                resource = {
                    "kind": "image",
                    "kind_label": "Image",
                    "label": "Image",
                    "status": "metadata_only",
                    "status_label": "Metadata only",
                    "action_label": "",
                    "can_reveal": False,
                }
            if (
                row["account_id"] != item["account_id"]
                or row["peer_id"] != item["peer_id"]
            ):
                return None
            text = "" if resource is not None else row["text"]
            body_bytes += len(text.encode())
            if body_bytes > _MAX_BYTES:
                return None
            child = _child(
                stable_id=_identifier(
                    parent_message_id, "forwarded_reference", str(ordinal), row["id"]
                ),
                ordinal=ordinal,
                text=text,
                author=row["sender_label"],
                sender=row["sender_id"] or "",
                direction=row["direction"],
                timestamp=row["timestamp_value"],
                label=row["timestamp_label"] if row["timestamp_value"] else "",
                basis=row["time_basis"],
                provenance=evidence,
                flags=json.loads(row["quality_flags"]),
                conversation_id=row["conversation_id"],
                account_id=row["account_id"],
                peer_id=row["peer_id"],
                resource=resource,
            )
            child.messaging["canonical_message_id"] = row["id"]
            child.messaging["forwarded_relationship_authority"] = declaration[
                "authority"
            ]
            snapshot_id = evidence.get("backup_id", "")
            snapshot = evidence.get("source_snapshot")
            if has_snapshots and type(snapshot) is int and 0 < snapshot <= 2**63 - 1:
                key = snapshot, row["partition_id"]
                if key not in snapshots:
                    receipt = connection.execute(
                        "SELECT snapshot_id FROM source_snapshots WHERE id=? AND source_id=?",
                        key,
                    ).fetchone()
                    snapshots[key] = receipt[0] if receipt is not None else ""
                snapshot_id = snapshots[key]
            requests.append(
                {
                    "id": child.stable_id,
                    "conversation_id": row["conversation_id"],
                    "sender_id": row["sender_id"] or "",
                    "sender_label": row["sender_label"],
                    "source_id": row["partition_id"],
                    "source_snapshot_id": snapshot_id,
                    "account_id": row["account_id"],
                    "peer_id": row["peer_id"],
                    "conversation_kind": row["kind"],
                    "source_record_id": str(evidence.get("record_id") or ""),
                    "source_locator": evidence.get("locator", {}),
                    "timestamp_value": row["timestamp_value"],
                    "is_system": False,
                }
            )
            children.append(child)
        contacts = WechatContactRepository(local_store_root).resolve_messages(requests)
        names = WechatDisplayNames(local_store_root)
        for child in children:
            contact = contacts[child.stable_id]
            child.messaging["contact"] = contact
            child.messaging["viewer_display_name"] = names.contact_name(contact)
        if _database_identity(path) != initial or (
            resource_identity is not None
            and _database_identity(resource_path) != resource_identity
        ):
            return None
        return {
            "title": declaration.get("title") or "Forwarded conversation",
            "items": tuple(children),
            "authority": declaration["authority"],
            "status": "canonical_references",
        }
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
        return None
    finally:
        if resources is not None:
            resources.close()
        if connection is not None:
            connection.close()
