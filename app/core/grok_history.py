"""Grok text history collection and local persistence.

Code version: v1.2.1-codex.1
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlencode

from .cache_timing import wait_for_cache_scan
from .browser_sessions import (
    browser_descriptors,
    goto_with_retry,
    is_grok_security_verification_page,
    launch_chromium_context,
    sync_playwright_or_error,
)
from .config import CrawlConfig
from .resource_persistence import (
    GROK_HISTORY_FILENAME,
    GROK_HISTORY_SCHEMA,
    GROK_HISTORY_SCHEMA_VERSION,
    read_parquet_rows,
    write_parquet_rows_atomic,
)
from .state import TaskSnapshot, TaskState


GROK_HOME_URL = "https://grok.com/"
GROK_API_BASE_URL = "https://grok.com"
GROK_CONVERSATIONS_PAGE_SIZE = 60
GROK_CONVERSATION_PAGE_LIMIT = 500
GROK_RESPONSE_BATCH_SIZE = 50
GROK_API_RETRY_LIMIT = 3
GROK_API_RETRY_DELAY_MS = 1_000
GROK_API_REQUEST_TIMEOUT_MS = 30_000


@dataclass(frozen=True)
class GrokConversation:
    """The metadata needed to identify one Grok conversation."""

    conversation_id: str
    title: str
    created_at: str
    updated_at: str
    url: str


@dataclass(frozen=True)
class GrokConversationSync:
    """The result of replacing one conversation in the local store."""

    message_count: int
    added_or_changed: int
    unchanged: int


@dataclass(frozen=True)
class GrokTextMessage:
    """Normalized text content ready for the local history schema."""

    message_key: str
    platform: str
    conversation_id: str
    conversation_title: str
    conversation_url: str
    role: str
    author_label: str
    content_text: str
    content_html: str
    timestamp: str
    turn_index: int
    message_index: int
    model_label: str
    source_links: tuple[str, ...]
    content_sha256: str


def grok_history_path(local_store_root: Path | str) -> Path:
    """Return the canonical local path for Grok text history."""

    return Path(local_store_root).expanduser() / "llm" / "grok" / GROK_HISTORY_FILENAME


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_http_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def _extract_source_links(value: Any, *, key: str = "") -> list[str]:
    """Extract source URLs without storing arbitrary response payloads."""

    links: list[str] = []
    if isinstance(value, str):
        if _is_http_url(value) and (
            not key
            or key.lower() in {"url", "href", "uri", "sourceurl", "webpageurl", "webpageurls"}
        ):
            links.append(value)
        return links
    if isinstance(value, list):
        for item in value:
            links.extend(_extract_source_links(item, key=key))
        return links
    if isinstance(value, dict):
        for item_key, item in value.items():
            links.extend(_extract_source_links(item, key=str(item_key)))
    return links


def _message_source_links(response: dict[str, Any]) -> list[str]:
    links: list[str] = []
    for field in (
        "webpageUrls",
        "webSearchResults",
        "citedWebSearchResults",
        "citedRagResults",
        "citedXposts",
        "citedConnectorSearchResults",
        "citedCollectionSearchResults",
    ):
        links.extend(_extract_source_links(response.get(field), key=field))
    return list(dict.fromkeys(links))


def _response_text(response: dict[str, Any]) -> str:
    value = response.get("message")
    if value is None:
        value = response.get("query") if str(response.get("sender", "")).lower() == "human" else ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value or "").replace("\x00", "").strip()


def _conversation_from_payload(payload: dict[str, Any]) -> GrokConversation | None:
    conversation_id = str(
        payload.get("conversationId")
        or payload.get("id")
        or payload.get("conversation_id")
        or ""
    ).strip()
    if not conversation_id:
        return None
    title = str(payload.get("title") or "").strip() or f"Grok session {conversation_id}"
    created_at = str(payload.get("createTime") or payload.get("createdAt") or "").strip()
    updated_at = str(
        payload.get("modifyTime")
        or payload.get("updatedAt")
        or payload.get("createTime")
        or ""
    ).strip()
    return GrokConversation(
        conversation_id=conversation_id,
        title=title,
        created_at=created_at,
        updated_at=updated_at,
        url=f"{GROK_API_BASE_URL}/c/{conversation_id}",
    )


def _api_error_message(result: dict[str, Any]) -> str:
    body = result.get("body")
    if isinstance(body, dict):
        detail = body.get("message") or body.get("error") or body.get("detail")
        if detail:
            return str(detail)
    if isinstance(body, str) and body.strip():
        return body.strip()[:300]
    return f"HTTP {result.get('status', 'unknown')}"


def _grok_api_json(
    page: Any,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch one authenticated Grok API response in the browser context."""

    url = f"{GROK_API_BASE_URL}{path}"
    serialized_body = json.dumps(body, ensure_ascii=False) if body is not None else None
    for attempt in range(1, GROK_API_RETRY_LIMIT + 1):
        result = page.evaluate(
            """
            async ({url, method, body, timeoutMs}) => {
              const controller = new AbortController();
              const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
              try {
                const options = {
                  method,
                  credentials: "include",
                  headers: {Accept: "application/json"},
                  signal: controller.signal,
                };
                if (body !== null) {
                  options.headers["Content-Type"] = "application/json";
                  options.body = body;
                }
                const response = await fetch(url, options);
                const text = await response.text();
                let parsed = text;
                try { parsed = text ? JSON.parse(text) : {}; } catch (_) {}
                return {status: response.status, body: parsed};
              } catch (error) {
                if (controller.signal.aborted) {
                  return {
                    status: 408,
                    body: {message: `Request timed out after ${timeoutMs} ms`},
                    timedOut: true,
                  };
                }
                throw error;
              } finally {
                clearTimeout(timeoutId);
              }
            }
            """,
            {
                "url": url,
                "method": method,
                "body": serialized_body,
                "timeoutMs": GROK_API_REQUEST_TIMEOUT_MS,
            },
        )
        payload = result.get("body") if isinstance(result, dict) else None
        if isinstance(payload, str):
            title_match = re.search(r"<title[^>]*>(.*?)</title>", payload, re.I | re.S)
            title = title_match.group(1) if title_match else ""
            if is_grok_security_verification_page(title, payload, payload):
                raise RuntimeError(
                    "Grok requires browser security verification. Open Grok in the selected "
                    "browser and complete any verification, then retry Text caching. "
                    "Existing cached history has been preserved."
                )
        if isinstance(result, dict) and 200 <= int(result.get("status", 0)) < 300:
            payload = result.get("body")
            if isinstance(payload, dict):
                return payload
            raise RuntimeError(f"Unexpected Grok API payload for {path}")
        status = int(result.get("status", 0)) if isinstance(result, dict) else 0
        if status not in {408, 425, 429} and status < 500:
            message = _api_error_message(result if isinstance(result, dict) else {})
            raise RuntimeError(f"Grok API request failed for {path}: {message}")
        if attempt < GROK_API_RETRY_LIMIT:
            page.wait_for_timeout(GROK_API_RETRY_DELAY_MS * attempt)
    raise RuntimeError(f"Grok API request failed after retries: {path}")


def list_grok_conversations(page: Any) -> list[GrokConversation]:
    """Enumerate every non-project conversation using Grok's page token."""

    conversations: list[GrokConversation] = []
    seen_ids: set[str] = set()
    page_token = ""
    seen_tokens: set[str] = set()
    for _ in range(GROK_CONVERSATION_PAGE_LIMIT):
        query = {
            "pageSize": str(GROK_CONVERSATIONS_PAGE_SIZE),
            "excludeProjects": "true",
        }
        if page_token:
            query["pageToken"] = page_token
        payload = _grok_api_json(
            page,
            "/rest/app-chat/conversations?" + urlencode(query),
        )
        items = payload.get("conversations")
        if not isinstance(items, list):
            break
        for raw_item in items:
            if not isinstance(raw_item, dict):
                continue
            conversation = _conversation_from_payload(raw_item)
            if conversation is None or conversation.conversation_id in seen_ids:
                continue
            seen_ids.add(conversation.conversation_id)
            conversations.append(conversation)
        next_token = str(payload.get("nextPageToken") or "").strip()
        if not next_token or next_token in seen_tokens or not items:
            break
        seen_tokens.add(next_token)
        page_token = next_token
    return conversations


def _response_nodes(page: Any, conversation_id: str) -> list[dict[str, Any]]:
    payload = _grok_api_json(
        page,
        f"/rest/app-chat/conversations/{conversation_id}/response-node",
    )
    nodes = payload.get("responseNodes")
    return [item for item in nodes if isinstance(item, dict)] if isinstance(nodes, list) else []


def _load_responses(
    page: Any,
    conversation_id: str,
    response_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    responses: dict[str, dict[str, Any]] = {}
    ids = [str(response_id).strip() for response_id in response_ids if str(response_id).strip()]
    for offset in range(0, len(ids), GROK_RESPONSE_BATCH_SIZE):
        batch = ids[offset : offset + GROK_RESPONSE_BATCH_SIZE]
        payload = _grok_api_json(
            page,
            f"/rest/app-chat/conversations/{conversation_id}/load-responses",
            method="POST",
            body={"responseIds": batch},
        )
        items = payload.get("responses")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            response_id = str(item.get("responseId") or item.get("id") or "").strip()
            if response_id:
                responses[response_id] = item
    return responses


def _normalized_messages(
    conversation: GrokConversation,
    nodes: list[dict[str, Any]],
    responses: dict[str, dict[str, Any]],
    captured_at: str,
) -> list[GrokTextMessage]:
    ordered_responses: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for node in nodes:
        response_id = str(node.get("responseId") or node.get("id") or "").strip()
        response = responses.get(response_id)
        if response is not None:
            ordered_responses.append(response)
            seen_ids.add(response_id)
    extras = [
        response
        for response_id, response in responses.items()
        if response_id not in seen_ids
    ]
    extras.sort(key=lambda item: (str(item.get("createTime") or ""), str(item.get("responseId") or "")))
    ordered_responses.extend(extras)

    messages: list[GrokTextMessage] = []
    turn_index = 0
    for response in ordered_responses:
        sender = str(response.get("sender") or "").strip().lower()
        if sender not in {"human", "user", "assistant"}:
            continue
        content = _response_text(response)
        if not content:
            continue
        role = "user" if sender in {"human", "user"} else "assistant"
        if role == "user":
            turn_index += 1
        response_id = str(response.get("responseId") or response.get("id") or "").strip()
        if not response_id:
            continue
        timestamp = str(response.get("createTime") or "").strip() or captured_at
        model_label = str(response.get("model") or "").strip()
        messages.append(
            GrokTextMessage(
                message_key=f"{conversation.conversation_id}:{response_id}",
                platform="grok",
                conversation_id=conversation.conversation_id,
                conversation_title=conversation.title,
                conversation_url=conversation.url,
                role=role,
                author_label="You" if role == "user" else "Grok",
                content_text=content,
                content_html="",
                timestamp=timestamp,
                turn_index=turn_index,
                message_index=len(messages),
                model_label=model_label,
                source_links=_message_source_links(response),
                content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            )
        )
    return messages


class GrokHistoryStore:
    """Replace conversations atomically while preserving first-seen timestamps."""

    def __init__(self, path: Path | str) -> None:
        self.path = path
        rows = read_parquet_rows(path)
        if Path(path).exists() and rows is None:
            raise RuntimeError(f"Grok history Parquet is unreadable: {path}")
        self._rows: dict[str, dict[str, Any]] = {
            str(row.get("message_key")): row
            for row in rows or []
            if row.get("message_key")
        }

    @property
    def cached_messages(self) -> int:
        return len(self._rows)

    @property
    def cached_conversations(self) -> int:
        return len({str(row.get("conversation_id")) for row in self._rows.values()})

    def replace_conversation(
        self,
        conversation: GrokConversation,
        messages: list[GrokTextMessage],
        captured_at: str,
    ) -> GrokConversationSync:
        conversation_id = conversation.conversation_id
        previous = {
            key: row
            for key, row in self._rows.items()
            if str(row.get("conversation_id")) == conversation_id
        }
        next_rows: dict[str, dict[str, Any]] = {
            key: row
            for key, row in self._rows.items()
            if str(row.get("conversation_id")) != conversation_id
        }
        added_or_changed = 0
        unchanged = 0
        for message in messages:
            row = {
                "message_key": message.message_key,
                "schema_version": GROK_HISTORY_SCHEMA_VERSION,
                "platform": message.platform,
                "conversation_id": message.conversation_id,
                "conversation_title": message.conversation_title,
                "conversation_url": message.conversation_url,
                "role": message.role,
                "author_label": message.author_label,
                "content_text": message.content_text,
                "content_html": message.content_html,
                "turn_index": message.turn_index,
                "message_index": message.message_index,
                "model_label": message.model_label,
                "source_links": list(message.source_links),
                "first_seen_at": str(previous.get(message.message_key, {}).get("first_seen_at") or captured_at),
                "last_seen_at": message.timestamp or captured_at,
                "content_sha256": message.content_sha256,
            }
            prior = previous.get(message.message_key)
            if prior is not None and all(prior.get(key) == row.get(key) for key in GROK_HISTORY_SCHEMA.names):
                unchanged += 1
            else:
                added_or_changed += 1
            next_rows[message.message_key] = row
        self._rows = next_rows
        write_parquet_rows_atomic(self.path, list(self._rows.values()), GROK_HISTORY_SCHEMA)
        return GrokConversationSync(
            message_count=len(messages),
            added_or_changed=added_or_changed,
            unchanged=unchanged,
        )


def build_grok_history_snapshot(
    *,
    version: str,
    local_store_root: str,
) -> TaskSnapshot:
    """Build the initial UI snapshot for the Grok text runtime."""

    store = GrokHistoryStore(grok_history_path(local_store_root))
    snapshot = TaskSnapshot(
        version=version,
        account_name="Grok",
        output_dir=str(grok_history_path(local_store_root).parent),
        progress_unit="sessions",
        discovered_tweets=store.cached_conversations,
        queued_tweets=store.cached_conversations,
        processed_tweets=store.cached_conversations,
        downloaded_posts=store.cached_conversations,
        downloaded_tweets=store.cached_messages,
        discovered_images=store.cached_messages,
        message=(
            f"Ready. Found existing Grok history: {store.cached_conversations:,} sessions, "
            f"{store.cached_messages:,} messages."
        ),
    )
    return snapshot


def sync_grok_history(
    state: TaskState,
    config: CrawlConfig,
    should_stop: Callable[[], bool],
    *,
    local_store_root: str,
) -> dict[str, Any]:
    """Cache all text conversations available to the authenticated Edge profile."""

    descriptor = browser_descriptors(config).get(config.grok_browser)
    if descriptor is None:
        raise RuntimeError(f"Unsupported Grok browser: {config.grok_browser}")
    if descriptor.engine != "chromium":
        raise RuntimeError("Grok text history currently requires a Chromium browser such as Edge")

    captured_at = utc_now_iso()
    store = GrokHistoryStore(grok_history_path(local_store_root))
    state.update(
        phase="collecting",
        progress_unit="sessions",
        account_name="Grok",
        output_dir=str(grok_history_path(local_store_root).parent),
        downloaded_posts=store.cached_conversations,
        downloaded_tweets=store.cached_messages,
        discovered_images=store.cached_messages,
        downloaded_images=0,
        message="Opening authenticated Grok history in Edge...",
    )
    state.append_event("Opening authenticated Grok history in the selected Edge profile.")

    processed_sessions = 0
    discovered_messages = 0
    added_or_changed = 0
    unchanged_messages = 0
    failed_sessions = 0
    stopped = False

    with sync_playwright_or_error() as playwright:
        with launch_chromium_context(
            playwright,
            descriptor,
            headless=True,
            clone_profile_first=True,
            background_window=True,
        ) as context:
            page = context.pages[0] if context.pages else context.new_page()
            goto_with_retry(page, GROK_HOME_URL, attempts=3)
            page.wait_for_timeout(500)
            conversations = list_grok_conversations(page)
            state.update(
                phase="downloading",
                discovered_tweets=len(conversations),
                queued_tweets=len(conversations),
                discovery_complete=True,
                message=f"Found {len(conversations):,} Grok sessions; loading text messages...",
            )
            state.append_event(f"Found {len(conversations):,} Grok sessions across all API pages.")

            for index, conversation in enumerate(conversations):
                if should_stop():
                    stopped = True
                    break
                scan_wait = config.cache_scan_wait("grok", "text")
                if index and scan_wait > 0:
                    if wait_for_cache_scan(scan_wait, should_stop):
                        stopped = True
                        break
                try:
                    nodes = _response_nodes(page, conversation.conversation_id)
                    response_ids = [
                        str(node.get("responseId") or node.get("id") or "").strip()
                        for node in nodes
                    ]
                    responses = _load_responses(page, conversation.conversation_id, response_ids)
                    messages = _normalized_messages(conversation, nodes, responses, captured_at)
                    result = store.replace_conversation(conversation, messages, captured_at)
                    discovered_messages += result.message_count
                    added_or_changed += result.added_or_changed
                    unchanged_messages += result.unchanged
                except Exception as exc:
                    failed_sessions += 1
                    state.append_event(
                        f"Failed Grok session {conversation.conversation_id}: {str(exc)[:240]}"
                    )
                processed_sessions += 1
                state.update(
                    processed_tweets=processed_sessions,
                    downloaded_posts=store.cached_conversations,
                    downloaded_tweets=store.cached_messages,
                    discovered_images=discovered_messages,
                    failed_tweets=failed_sessions,
                    message=(
                        f"Loaded {processed_sessions:,}/{len(conversations):,} sessions; "
                        f"{store.cached_messages:,} messages cached."
                    ),
                )

    if stopped:
        phase = "stopped"
        message = (
            f"Stopped Grok history sync after {processed_sessions:,}/{len(conversations):,} sessions; "
            f"{store.cached_messages:,} messages remain cached."
        )
    else:
        phase = "completed"
        message = (
            f"Finished Grok history sync. Inspected {processed_sessions:,} sessions, "
            f"found {discovered_messages:,} messages, added or changed {added_or_changed:,}, "
            f"unchanged {unchanged_messages:,}, failed {failed_sessions:,}; "
            f"{store.cached_conversations:,} sessions and {store.cached_messages:,} messages cached."
        )
    state.update(
        phase=phase,
        discovered_tweets=len(conversations),
        queued_tweets=len(conversations),
        processed_tweets=processed_sessions,
        downloaded_posts=store.cached_conversations,
        downloaded_tweets=store.cached_messages,
        discovered_images=discovered_messages,
        downloaded_images=0,
        failed_tweets=failed_sessions,
        message=message,
    )
    state.append_event(message)
    return {
        "sessions": processed_sessions,
        "messages": discovered_messages,
        "added_or_changed": added_or_changed,
        "unchanged": unchanged_messages,
        "failed": failed_sessions,
        "stopped": stopped,
    }
