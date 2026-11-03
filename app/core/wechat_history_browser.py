"""Adapt indexed human conversations to the shared Local resources page model."""

# Code version: v1.25.0-codex.0

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re

from .wechat_archive import WechatArchiveRepository
from .wechat_contacts import WechatContactRepository, classify_wechat_identifier
from .wechat_context_evidence import WechatContextEvidence
from .wechat_context_links import WechatContextLinks
from .wechat_display_names import WechatDisplayNames
from .wechat_forwarded import (
    project_forwarded_references,
    project_html_forwarded,
    project_native_forwarded,
)
from .wechat_mac_resources import WechatMacResourceResolver
from .wechat_reconcile import native_text_metadata
from .wechat_text_observations import WechatTextObservations
from .wechat_resources import present_resource, resource_reference


_LEGACY_QUOTE_SEPARATOR = re.compile(r"[ \t]*-(?:[ \t]+-){14}[ \t]*")
_SYSTEM_STATUS_ICONS = re.compile(
    r"(?P<space> ?)(?:\$\{wxid_[A-Za-z0-9_]+@textstatusicon\})+"
)


def _system_notice_display_text(text):
    """Omit exporter labels and status icons only from recognized system notices."""
    prefix = "System Message:"
    if text.startswith(prefix):
        notice = text[len(prefix) :].lstrip(" \t")
        if notice.strip():
            text = notice

    def without_icons(match):
        # Remove one redundant separator, but never join words or normalize whitespace.
        return "" if text.startswith(" ", match.end()) else match.group("space")

    return _SYSTEM_STATUS_ICONS.sub(without_icons, text)


def _quoted_envelope_fields(envelope):
    """Read exporter-rendered labels without promoting them to contact identities."""
    if not envelope.startswith("「") or not envelope.endswith("」"):
        return None
    depth = 0
    for position, character in enumerate(envelope):
        if character == "「":
            depth += 1
        elif character == "」":
            depth -= 1
            if depth < 0 or (depth == 0 and position != len(envelope) - 1):
                return None
    if depth:
        return None
    speaker, colon, quoted_text = envelope[1:-1].partition("：")
    if (
        not colon
        or not speaker
        or speaker != speaker.strip()
        or any(character in speaker for character in "\r\n:「」")
        or not quoted_text.strip()
    ):
        return None
    return {"speaker": speaker, "text": quoted_text}


def _rendered_html_quote_fields(envelope):
    """Preserve explicit rendered quotes when a display label cannot be separated."""
    return _quoted_envelope_fields(envelope) or {
        "speaker": "Quoted message",
        "text": envelope,
    }


def _legacy_text_quoted_reply(text):
    """Recognize the complete legacy quote envelope without inferring identities."""
    offset = 0
    for line in text.splitlines(keepends=True):
        separator = line.rstrip("\r\n")
        if _LEGACY_QUOTE_SEPARATOR.fullmatch(separator) is None:
            offset += len(line)
            continue
        quote_end = offset
        if text[:quote_end].endswith("\r\n"):
            quote_end -= 2
        elif text[:quote_end].endswith(("\n", "\r")):
            quote_end -= 1
        envelope = text[:quote_end]
        fields = _quoted_envelope_fields(envelope)
        if fields is None:
            return None
        reply_start = offset + len(line)
        reply = text[reply_start:]
        if not reply.strip():
            return None
        return {
            **fields,
            "source_start": 0,
            "source_end": quote_end,
            "reply_source_start": reply_start,
            "reply_source_end": len(text),
        }
    return None


def _pdf_message_presentation(text, quality_flags):
    """Present legacy PDF quotes and notices without rewriting source records."""
    notices = []
    body_end = len(text)
    if "pdf_text_layer" in quality_flags:
        prefix = "System Message:"
        for line in reversed(text.splitlines(keepends=True)):
            content = line.rstrip("\r\n")
            if not content.startswith(prefix):
                break
            notice = content[len(prefix) :].lstrip(" \t")
            if not notice.strip():
                break
            start = body_end - len(line)
            notices.append(
                {
                    "text": _system_notice_display_text(content),
                    "source_start": start,
                    "source_end": body_end,
                }
            )
            body_end = start
    body = text[:body_end]
    if notices:
        # Remove only the separator before the suffix, preserving body whitespace.
        if body.endswith("\r\n"):
            body = body[:-2]
        elif body.endswith(("\n", "\r")):
            body = body[:-1]
    result = {"display_text": body, "system_notices": tuple(reversed(notices))}
    if "pdf_text_layer" in quality_flags:
        quote = _legacy_text_quoted_reply(body)
        if quote is not None:
            result["quoted_reply"] = quote
            result["display_text"] = body[quote["reply_source_start"] :]
    return result


def _legacy_html_quoted_reply_presentation(text, provenance):
    """Present the complete legacy text convention without claiming native quote metadata."""
    if (
        not isinstance(text, str)
        or not isinstance(provenance, dict)
        or "message_type" in provenance
        or provenance.get("text_extraction_kind") != "plain_text"
        or provenance.get("export_msgid_namespace") != "MesLocalID"
        or provenance.get("forwarded_components", []) != []
        or provenance.get("quoted_components", []) != []
    ):
        return {}
    locator = provenance.get("locator")
    native_fields = {
        "message_type",
        "appmsg_subtype",
        "quoted_reference",
        "text_components",
        "forwarded_references",
    }
    if (
        not isinstance(locator, dict)
        or locator.get("export_msgtype") != "1"
        or locator.get("export_msgid_namespace") != "MesLocalID"
        or native_fields.intersection(provenance)
        or native_fields.intersection(locator)
        or provenance.get("primary_text_sha256")
        != hashlib.sha256(text.encode("utf-8")).hexdigest()
    ):
        return {}
    quote = _legacy_text_quoted_reply(text)
    if quote is None:
        return {}
    return {
        "display_text": text[quote["reply_source_start"] :],
        "quoted_reply": quote,
    }


def _html_quoted_reply_presentation(text, provenance, *, forwarded_record=None):
    """Present explicit rendered HTML quotes only when their reply hash agrees."""
    if (
        not isinstance(provenance, dict)
        or "message_type" in provenance
        or provenance.get("text_extraction_kind") != "quoted_reply"
        or provenance.get("export_msgid_namespace") != "MesLocalID"
        or (
            provenance.get("forwarded_components", []) != []
            and forwarded_record is None
        )
    ):
        return {}
    locator = provenance.get("locator")
    components = provenance.get("quoted_components")
    if (
        not isinstance(locator, dict)
        or locator.get("export_msgtype") != "49"
        or locator.get("export_msgid_namespace") != "MesLocalID"
        or not isinstance(components, list)
        or len(components) != 1
        or not isinstance(components[0], dict)
        or components[0].get("kind") != "rendered_quote"
        or not isinstance(components[0].get("text"), str)
    ):
        return {}
    if forwarded_record is not None:
        if (
            not isinstance(forwarded_record, dict)
            or forwarded_record.get("authority") != "source_verified_html_export_tree"
            or not isinstance(forwarded_record.get("title"), str)
        ):
            return {}
        text = forwarded_record["title"]
    envelope = components[0]["text"]
    fields = _rendered_html_quote_fields(envelope)
    prefix = envelope + "\n\n"
    if not envelope.strip() or not text.startswith(prefix):
        return {}
    reply = text[len(prefix) :]
    if (
        not reply.strip()
        or provenance.get("primary_text_sha256")
        != hashlib.sha256(reply.encode("utf-8")).hexdigest()
    ):
        return {}
    return {
        "display_text": reply,
        "quoted_reply": {
            **fields,
            "source_start": 0,
            "source_end": len(envelope),
            "reply_source_start": len(prefix),
            "reply_source_end": len(text),
        },
    }


def _native_quoted_reply_presentation(provenance):
    """Project explicit native quote roles without changing message or contact evidence."""
    if (
        not isinstance(provenance, dict)
        or type(provenance.get("message_type")) is not int
        or provenance.get("message_type") != 49
        or provenance.get("text_extraction_kind") != "quoted_reply"
    ):
        return {}
    locator = provenance.get("locator", {})
    if not isinstance(locator, dict):
        return {}
    try:
        metadata = native_text_metadata(provenance, locator)
    except ValueError:
        # Existing malformed evidence stays literal rather than acquiring inferred roles.
        return {}
    if metadata.get("appmsg_subtype") not in {None, 57}:
        return {}
    components = metadata.get("text_components", [])
    replies = [item for item in components if item["kind"] == "reply_text"]
    quotes = [
        item
        for item in components
        if item["kind"] in {"quoted_text", "quoted_forwarded_text"}
    ]
    if len(replies) > 1 or len(quotes) > 1:
        return {}
    reply_text = replies[0]["text"] if replies else ""
    if not reply_text.strip() and (len(quotes) != 1 or not quotes[0]["text"].strip()):
        return {}
    reference = metadata.get("quoted_reference")
    quoted = quotes[0] if quotes else None
    if reference is None and quoted is not None:
        reference = {
            key: value
            for key, value in quoted.items()
            if key
            not in {
                "kind",
                "text",
                "text_components",
                "flags",
                "omitted_forwarded_items",
            }
        }
    if reference is None:
        return {}
    forwarded = (
        quoted is not None and quoted["kind"] == "quoted_forwarded_text"
    ) or reference.get("relationship") == "forwarded_conversation"
    generic_speaker = "Quoted forwarded conversation" if forwarded else "Quoted message"
    speaker = reference.get("source_sender_label") or generic_speaker
    if reference.get("authority") == "user_confirmation":
        speaker += " (user-confirmed)"
    quoted_text = (
        quoted["text"]
        if quoted is not None
        else "Original quoted content is unavailable."
    )
    if quoted is not None and quoted["kind"] == "quoted_forwarded_text":
        originals = quoted.get("text_components", [])
        if originals:
            quoted_text = "\n\n".join(
                (
                    item.get("source_sender_label")
                    or item.get("source_sender_id")
                    or "Unknown original speaker"
                )
                + ":\n"
                + item["text"]
                for item in originals
                if item["text"].strip()
            )
    return {
        "display_text": reply_text,
        "quoted_reply": {
            "speaker": speaker,
            "text": quoted_text,
            "reference": reference,
            "text_components": components,
        },
    }


def _present_html_forwarded_quotes(record):
    """Keep explicit quoted roles inside an already source-verified HTML tree."""
    if not record or record.get("authority") != "source_verified_html_export_tree":
        return
    for child in record["items"]:
        context = child.messaging
        component = context["provenance"].get("embedded_html_forwarded_component", {})
        quotes = component.get("quoted_components", [])
        if (
            component.get("text_kind") == "quoted_reply"
            and isinstance(quotes, list)
            and len(quotes) == 1
            and isinstance(quotes[0], dict)
            and quotes[0].get("kind") == "rendered_quote"
            and isinstance(quotes[0].get("text"), str)
            and isinstance(component.get("text"), str)
        ):
            fields = _rendered_html_quote_fields(quotes[0]["text"])
            if quotes[0]["text"].strip():
                context["quoted_reply"] = fields
                nested = context.get("forwarded_record")
                if nested:
                    nested["title"] = component["text"] or "Forwarded conversation"
                    nested["source_title"] = component["text"]
                else:
                    context["display_text"] = component["text"]
        _present_html_forwarded_quotes(context.get("forwarded_record"))


def _mark_sender_runs(items, *, filtered=False):
    """Show a native sender once at the start of each adjacent, page-local run."""

    def identity(item):
        context = item.messaging
        sender = context.get("sender_id")
        if (
            context.get("is_system_notice")
            or not context.get("display_text", item.content_text)
            or context.get("sender_identifier_namespace") != "native_user_name"
            or not isinstance(sender, str)
            or not sender.strip()
            or sender != sender.strip()
            or not context.get("account_id")
            or context.get("direction") not in {"sent", "received"}
        ):
            return None
        provenance = context.get("provenance")
        if not isinstance(provenance, dict):
            return None
        timestamp = provenance.get("unix_timestamp")
        if type(timestamp) is not int or timestamp <= 0 or not item.last_seen_at:
            return None
        try:
            displayed_time = datetime.fromisoformat(item.last_seen_at)
        except (TypeError, ValueError):
            return None
        if displayed_time.tzinfo is None:
            return None
        return (
            context["account_id"],
            item.conversation_id,
            sender,
            context["direction"],
        ), timestamp

    for item in items:
        item.messaging["show_sender_name"] = True
    if filtered:
        return
    for current, following in zip(items, items[1:]):
        left, right = identity(current), identity(following)
        if (
            left is not None
            and right is not None
            and not current.messaging.get("system_notices")
            and left[0] == right[0]
            and right[1] >= left[1]
        ):
            following.messaging["show_sender_name"] = False


def _resolve_native_forwarded_contacts(
    items, repository, display_names, account_rows, source_snapshots
):
    """Resolve literal child authors only inside a verified native source envelope."""
    partitions = {row["partition_id"]: row for row in account_rows}
    requests, children = [], {}

    def visit(record):
        if (
            not isinstance(record, dict)
            or record.get("authority") != "native_forwarded_envelope"
        ):
            return
        for child in record["items"]:
            yield child
            yield from visit(child.messaging.get("forwarded_record"))
            quote = child.messaging.get("quoted_reply")
            if isinstance(quote, dict):
                yield from visit(quote.get("forwarded_record"))

    for parent in items:
        context = parent.messaging
        owner = display_names.account_owner(context)
        partition = partitions.get(context.get("partition_id"))
        if (
            not owner
            or partition is None
            or display_names.account_owner(partition) != owner
            or partition["key"] != context.get("account_key")
        ):
            continue
        snapshot_key = context["provenance"].get("source_snapshot")
        snapshot = (
            source_snapshots.get(snapshot_key) if type(snapshot_key) is int else None
        )
        if snapshot is None or snapshot["source_id"] != context["partition_id"]:
            continue
        source = snapshot["metadata"]
        if (
            source.get("source_id") != context["partition_id"]
            or source.get("source_account_id") != owner
            or source.get("account_hash_verified") is not True
            or source.get("account_hash") != hashlib.md5(owner.encode()).hexdigest()
        ):
            continue
        records = [context.get("forwarded_record")]
        quote = context.get("quoted_reply")
        if isinstance(quote, dict):
            records.append(quote.get("forwarded_record"))
        for record in records:
            for child in visit(record):
                metadata = child.messaging
                component = metadata["provenance"].get(
                    "embedded_forwarded_component", {}
                )
                sender = component.get("source_sender_id")
                if (
                    not isinstance(sender, str)
                    or not sender
                    or sender != sender.strip()
                ):
                    continue
                peer = component.get("source_chat_id", "")
                peer = peer if isinstance(peer, str) and peer == peer.strip() else ""
                requests.append(
                    {
                        "id": child.stable_id,
                        # This locates the receiving source, not the original chat.
                        "conversation_id": parent.conversation_id,
                        "source_id": context["partition_id"],
                        "source_snapshot_id": snapshot["snapshot_id"],
                        "account_id": owner,
                        "sender_id": sender,
                        "sender_label": child.author_label,
                        "peer_id": peer,
                        "conversation_kind": "group"
                        if peer.endswith("@chatroom")
                        else "unclassified",
                        "timestamp_value": child.last_seen_at,
                        "is_system": False,
                    }
                )
                children[child.stable_id] = child
                metadata.update(
                    source_file=context.get("source_file", ""),
                    source_label=context.get("source_label", ""),
                    source_snapshot_id=snapshot["snapshot_id"],
                    partition_id=context["partition_id"],
                    peer_id=peer,
                    sender_identifier_namespace="native_user_name",
                )
    for offset in range(0, len(requests), 400):
        resolved = repository.resolve_messages(requests[offset : offset + 400])
        for identifier, contact in resolved.items():
            if contact.get("identity_status") != "verified":
                continue
            metadata = children[identifier].messaging
            name = display_names.contact_name(contact)
            metadata["viewer_display_name"] = name or contact["display_name"]
            metadata["contact"] = {
                **contact,
                "viewer_display_name": metadata["viewer_display_name"],
                "viewer_display_name_source": "local_user_preference"
                if name
                else "canonical_contact",
            }


def query_wechat_history(
    local_store_root,
    *,
    account="",
    chat_kind="all",
    query="",
    sort="newest",
    page=1,
    page_size=100,
    session="",
):
    """Reuse page and session contracts while retaining human-message semantics."""
    from .chat_history_browser import (
        ChatHistoryMessage,
        ChatHistoryPage,
        ChatHistorySession,
    )

    try:
        safe_page = max(1, int(page))
        safe_size = min(1_000_000, max(1, int(page_size)))
    except (TypeError, ValueError):
        safe_page, safe_size = 1, 100
    result = WechatArchiveRepository(local_store_root).query(
        account=account,
        kind=chat_kind,
        query=query,
        sort=sort,
        page=safe_page,
        page_size=safe_size,
        session=session,
    )

    display_names = WechatDisplayNames(local_store_root)
    text_observations = WechatTextObservations(local_store_root)
    mac_resources = WechatMacResourceResolver(local_store_root)
    account_rows = result["accounts"]
    projected_accounts, account_options = display_names.project_accounts(account_rows)

    def context(row):
        kind = row.get("kind", "unclassified")
        kind_label = {
            "direct": "Private chat",
            "group": "Group chat",
            "unclassified": "Unclassified chat",
        }.get(kind, "Unclassified chat")
        if (
            kind != "group"
            and classify_wechat_identifier(row.get("peer_id"))["kind"]
            == "official_account"
        ):
            kind_label = "Official account"
        return {
            "conversation_kind_label": kind_label,
            "viewer_account_label": display_names.account_label(
                row, verified_accounts=account_rows
            ),
            "mac_account_mapping": mac_resources.account_mapping(
                row.get("account_id") or ""
            ),
            **{
                key: row.get(key, "")
                for key in (
                    "account_key",
                    "account_id",
                    "account_label",
                    "identity_status",
                    "partition_id",
                    "source_label",
                    "source_file",
                    "kind",
                    "peer_id",
                )
            },
        }

    def conversation(row):
        return ChatHistorySession(
            stable_id=row["id"],
            source="wechat",
            conversation_id=row["id"],
            conversation_url="",
            conversation_title=row["title"],
            message_count=row["message_count"],
            first_seen_at=row["first_time"],
            last_seen_at=row["last_time"],
            latest_message=row["latest_text"],
            latest_role="",
            latest_author_label="",
            model_label="",
            source_links=(),
            messaging=context(row),
        )

    current = conversation(result["current"]) if result["current"] else None
    source_snapshots = result.get("source_snapshots", {})
    context_evidence = WechatContextEvidence(local_store_root).project(
        result["messages"]
    )
    context_links = WechatContextLinks(local_store_root).project(result["messages"])
    contact_requests = []
    items = []
    for row in result["messages"]:
        provenance = json.loads(row["provenance"])
        supplement = context_evidence.get(row["id"])
        effective_provenance = supplement["provenance"] if supplement else provenance
        metadata = {
            **context(result["current"]),
            "direction": row["direction"],
            "sender_id": row["sender_id"] or "",
            "timestamp_label": row["timestamp_label"],
            "time_basis": row["time_basis"],
            "provenance": provenance,
            "quality_flags": tuple(json.loads(row["quality_flags"])),
            "is_system_notice": isinstance(provenance, dict)
            and provenance.get("text_extraction_kind") == "system_text",
        }
        metadata.update(
            _pdf_message_presentation(row["text"], metadata["quality_flags"])
        )
        metadata.update(_legacy_html_quoted_reply_presentation(row["text"], provenance))
        metadata.update(_html_quoted_reply_presentation(row["text"], provenance))
        metadata.update(_native_quoted_reply_presentation(effective_provenance))
        if supplement:
            metadata["context_evidence"] = {
                key: value for key, value in supplement.items() if key != "provenance"
            }
        locator = effective_provenance.get("locator", {})
        has_forwarded_references = "forwarded_references" in effective_provenance or (
            isinstance(locator, dict) and "forwarded_references" in locator
        )
        forwarded_record = (
            project_forwarded_references(
                local_store_root,
                parent_message_id=row["id"],
                provenance=effective_provenance,
            )
            if has_forwarded_references
            else project_native_forwarded(
                effective_provenance,
                parent_id=row["id"],
                account_id=metadata["account_id"],
            )
        )
        if forwarded_record is not None:
            if effective_provenance.get("text_extraction_kind") == "quoted_reply":
                if metadata.get("quoted_reply"):
                    metadata["quoted_reply"]["forwarded_record"] = forwarded_record
            else:
                metadata["forwarded_record"] = forwarded_record
                metadata["display_text"] = ""
        if metadata["is_system_notice"]:
            metadata["display_text"] = _system_notice_display_text(
                metadata["display_text"]
            )
        reference = resource_reference(
            provenance, metadata["account_id"], metadata["peer_id"]
        )
        if reference is not None:
            metadata["resource"] = present_resource(reference, mac_resources)
        supplemental_text = text_observations.project(
            message_id=row["id"],
            account_id=metadata["account_id"],
            peer_id=metadata["peer_id"],
            text=row["text"],
            provenance=provenance,
        )
        if supplemental_text is not None:
            metadata.update(supplemental_text)
        source_snapshot = (
            provenance.get("source_snapshot") if isinstance(provenance, dict) else None
        )
        snapshot = (
            source_snapshots.get(source_snapshot)
            if type(source_snapshot) is int
            else None
        )
        if snapshot is not None and "routed_messages" in snapshot:
            routed = snapshot["routed_messages"].get(row["id"])
            if routed is None:
                snapshot = None
            else:
                metadata.update(routed)
        if snapshot is not None:
            source = snapshot["metadata"]
            for key in ("source_file", "source_label", "backup_id"):
                if isinstance(source.get(key), str) and source[key]:
                    metadata[key] = source[key]
            metadata["source_snapshot_id"] = snapshot["snapshot_id"]
        observations = (
            result.get("event_projection", {})
            .get("members_by_preferred", {})
            .get(row["id"], [])
        )
        if observations:
            metadata["source_observations"] = observations
        owner = metadata["account_id"]
        native_owner = (
            isinstance(owner, str)
            and bool(owner)
            and metadata["partition_id"]
            == "ios-" + hashlib.md5(owner.encode()).hexdigest()
        )
        metadata["sender_identifier_namespace"] = (
            "native_user_name" if native_owner else "unverified_export_field"
        )
        source_record_id = (
            provenance.get("record_id") if isinstance(provenance, dict) else None
        )
        source_locator = (
            provenance.get("locator") if isinstance(provenance, dict) else None
        )
        contact_requests.append(
            {
                "id": row["id"],
                "conversation_id": row["conversation_id"],
                "sender_id": metadata["sender_id"],
                "sender_label": row["sender_label"],
                "source_id": metadata["partition_id"],
                "source_snapshot_id": metadata.get("source_snapshot_id", ""),
                "account_id": metadata["account_id"],
                "peer_id": metadata["peer_id"],
                "conversation_kind": metadata["kind"],
                "text_sha256": hashlib.sha256(row["text"].encode()).hexdigest(),
                "timestamp_value": row["timestamp_value"],
                "source_record_id": source_record_id
                if isinstance(source_record_id, str)
                else "",
                "source_locator": source_locator
                if isinstance(source_locator, dict)
                else {},
                "is_system": metadata["is_system_notice"],
            }
        )
        items.append(
            ChatHistoryMessage(
                stable_id=row["id"],
                source="wechat",
                conversation_id=current.conversation_id,
                conversation_url="",
                conversation_title=current.conversation_title,
                message_key=row["id"],
                message_index=row["ordinal"],
                role=row["direction"],
                author_label=row["sender_label"],
                content_text=row["text"],
                content_html="",
                source_links=(),
                model_label="",
                first_seen_at=row["timestamp_value"],
                last_seen_at=row["timestamp_value"],
                messaging=metadata,
            )
        )
    contact_repository = WechatContactRepository(local_store_root)
    contacts = contact_repository.resolve_messages(contact_requests)
    requests_by_id = {request["id"]: request for request in contact_requests}
    observation_requests = [
        {
            **requests_by_id[item.stable_id],
            "id": observation["id"],
            "source_id": observation["source_id"],
            "source_snapshot_id": observation["source_snapshot_id"],
            "sender_id": observation["sender_id"],
            "sender_label": observation["sender_label"],
            "timestamp_value": observation["timestamp_value"],
            "source_record_id": observation["record_id"],
            "source_locator": {},
        }
        for item in items
        for observation in item.messaging.get("source_observations", [])
        if observation["id"] != item.stable_id and "timestamp_value" in observation
    ]
    observation_contacts = {}
    for offset in range(0, len(observation_requests), 400):
        observation_contacts.update(
            contact_repository.resolve_messages(
                observation_requests[offset : offset + 400]
            )
        )
    forwarded_requests = [
        {
            **requests_by_id[item.stable_id],
            "forwarded_components": item.messaging["provenance"][
                "forwarded_components"
            ],
        }
        for item in items
        if isinstance(item.messaging["provenance"].get("forwarded_components"), list)
        and item.messaging["provenance"]["forwarded_components"]
    ]
    forwarded = {}
    for offset in range(0, len(forwarded_requests), 400):
        forwarded.update(
            contact_repository.resolve_forwarded_components(
                forwarded_requests[offset : offset + 400]
            )
        )
    for item in items:
        contact = contacts[item.stable_id]
        if contact.get("identity_status") != "verified":
            for observation in item.messaging.get("source_observations", []):
                candidate = observation_contacts.get(observation["id"], {})
                if candidate.get("identity_status") == "verified" and candidate.get(
                    "id"
                ) == observation.get("contact_id"):
                    contact = {
                        **candidate,
                        "source_display_name": item.author_label,
                        "resolution_reason": "verified_conversation_event_sources",
                    }
                    break
        name = display_names.contact_name(contact)
        name_source = "local_user_preference"
        if (
            not name
            and item.messaging.get("source_observations")
            and contact.get("identity_status") == "verified"
        ):
            name = contact.get("preferred_name") or contact["display_name"]
            if name == item.messaging["sender_id"]:
                for observation in item.messaging["source_observations"]:
                    candidate = observation_contacts.get(observation["id"], {})
                    label = candidate.get("preferred_name", "")
                    if (
                        candidate.get("identity_status") == "verified"
                        and candidate.get("id") == contact["id"]
                        and label
                        and label != observation["sender_id"]
                    ):
                        name = label
                        break
            name_source = "verified_contact_observation"
        if name:
            item.messaging["viewer_display_name"] = name
            contact = {
                **contact,
                "viewer_display_name": name,
                "viewer_display_name_source": name_source,
            }
        item.messaging["contact"] = contact
        if item.stable_id in forwarded:
            projection = forwarded[item.stable_id]
            item.messaging["forwarded_components"] = projection["components"]
            item.messaging["forwarded_contact_resolution_status"] = projection["status"]
            if not item.messaging.get("forwarded_record"):
                record = project_html_forwarded(
                    item.messaging["provenance"],
                    parent_id=item.stable_id,
                    account_id=item.messaging["account_id"],
                    text=item.content_text,
                    components=projection["components"],
                    source_proof_status=projection["status"],
                    display_names=display_names,
                )
                if record is not None:
                    _present_html_forwarded_quotes(record)
                    quote = _html_quoted_reply_presentation(
                        item.content_text,
                        item.messaging["provenance"],
                        forwarded_record=record,
                    )
                    if quote:
                        item.messaging.update(quote)
                        record["title"] = quote["display_text"]
                        record["source_title"] = quote["display_text"]
                    item.messaging["forwarded_record"] = record
                    item.messaging["display_text"] = ""
        completion = context_links.get(item.stable_id)
        if completion:
            record = completion["forwarded_record"]
            _present_html_forwarded_quotes(record)
            item.messaging["forwarded_record"] = record
            item.messaging["display_text"] = ""
            item.messaging["context_link_proof"] = completion["proof"]
            if completion.get("quoted_reply"):
                item.messaging["quoted_reply"] = completion["quoted_reply"]
    _resolve_native_forwarded_contacts(
        items, contact_repository, display_names, account_rows, source_snapshots
    )
    _mark_sender_runs(items, filtered=bool(str(query or "").strip()))
    return ChatHistoryPage(
        items=tuple(items),
        total_count=result["count"],
        conversation_count=result["conversation_count"],
        current_page=result["page"],
        total_pages=result["pages"],
        page_size=result["page_size"],
        session_view=True,
        sessions=tuple(conversation(row) for row in result["conversations"]),
        session_id=session if current else "",
        current_session=current,
        messaging={
            "accounts": projected_accounts,
            "account_options": tuple(account_options),
            "selected_account": account,
            "chat_kind": result["kind"],
            "event_projection": result.get("event_projection", {}),
            "requested_session": session,
            "message_count": result["message_count"],
            "source_snapshots": source_snapshots,
            "resource_index_status": result.get("resource_index_status", "absent"),
            "resource_index_accounts": result.get("resource_index_accounts"),
        },
    )
