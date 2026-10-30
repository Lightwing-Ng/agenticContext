"""Adapt indexed human conversations to the shared Local resources page model."""

# Code version: v1.0.0-codex.0

from __future__ import annotations

import json

from .wechat_archive import WechatArchiveRepository


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

    def context(row):
        return {
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
    items = []
    for row in result["messages"]:
        metadata = {
            **context(result["current"]),
            "direction": row["direction"],
            "sender_id": row["sender_id"] or "",
            "timestamp_label": row["timestamp_label"],
            "time_basis": row["time_basis"],
            "provenance": json.loads(row["provenance"]),
            "quality_flags": tuple(json.loads(row["quality_flags"])),
        }
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
            "accounts": tuple(result["accounts"]),
            "selected_account": account,
            "chat_kind": result["kind"],
            "requested_session": session,
            "message_count": result["message_count"],
        },
    )
