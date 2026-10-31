"""Adapt indexed human conversations to the shared Local resources page model."""

# Code version: v1.3.0-codex.0

from __future__ import annotations

import hashlib
import json

from .wechat_archive import WechatArchiveRepository
from .wechat_contacts import WechatContactRepository
from .wechat_display_names import WechatDisplayNames


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

    def context(row):
        return {
            "viewer_account_label": display_names.account_label(row),
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
    contact_requests = []
    items = []
    for row in result["messages"]:
        provenance = json.loads(row["provenance"])
        metadata = {
            **context(result["current"]),
            "direction": row["direction"],
            "sender_id": row["sender_id"] or "",
            "timestamp_label": row["timestamp_label"],
            "time_basis": row["time_basis"],
            "provenance": provenance,
            "quality_flags": tuple(json.loads(row["quality_flags"])),
        }
        source_snapshot = (
            provenance.get("source_snapshot") if isinstance(provenance, dict) else None
        )
        snapshot = (
            source_snapshots.get(source_snapshot)
            if type(source_snapshot) is int
            else None
        )
        if snapshot is not None:
            source = snapshot["metadata"]
            for key in ("source_file", "source_label", "backup_id"):
                if isinstance(source.get(key), str) and source[key]:
                    metadata[key] = source[key]
            metadata["source_snapshot_id"] = snapshot["snapshot_id"]
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
                "is_system": isinstance(provenance, dict)
                and provenance.get("text_extraction_kind") == "system_text",
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
    contacts = WechatContactRepository(local_store_root).resolve_messages(
        contact_requests
    )
    for item in items:
        contact = contacts[item.stable_id]
        name = display_names.contact_name(contact)
        if name:
            item.messaging["viewer_display_name"] = name
            contact = {
                **contact,
                "viewer_display_name": name,
                "viewer_display_name_source": "local_user_preference",
            }
        item.messaging["contact"] = contact
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
            "accounts": tuple(
                {**row, "viewer_name": display_names.account_label(row)}
                for row in result["accounts"]
            ),
            "selected_account": account,
            "chat_kind": result["kind"],
            "requested_session": session,
            "message_count": result["message_count"],
            "source_snapshots": source_snapshots,
        },
    )
