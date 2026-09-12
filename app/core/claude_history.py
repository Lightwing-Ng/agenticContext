"""Browser-rendered Claude history collection and local persistence.

Code version: v1.1.1-codex.1
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .cache_timing import wait_for_cache_scan
from .agent_session_sources import normalize_claude_conversation_url
from .browser_sessions import (
    CLAUDE_HOME_URL,
    browser_descriptors,
    goto_with_retry,
    launch_chromium_context,
    sync_playwright_or_error,
    visible_claude_composer_selector,
)
from .config import LOCAL_STORE_ROOT, CrawlConfig
from .history_rows import (
    history_rows_match,
    partition_conversation_rows,
    sort_history_rows,
)
from .resource_persistence import (
    CLAUDE_HISTORY_FILENAME,
    CLAUDE_HISTORY_SCHEMA,
    CLAUDE_HISTORY_SCHEMA_VERSION,
    read_parquet_rows,
    write_parquet_rows_atomic,
)
from .state import TaskSnapshot, TaskState


CLAUDE_CHATS_URL = "https://claude.ai/chats"
CLAUDE_HISTORY_RELATIVE_DIR = Path("llm") / "claude"
CLAUDE_READY_TIMEOUT_SECONDS = 45.0
CLAUDE_RENDER_SETTLE_MILLISECONDS = 1_000
CLAUDE_DISCOVERY_ROUND_LIMIT = 12
CLAUDE_CONVERSATION_RETRY_LIMIT = 3
CLAUDE_HOSTS = frozenset({"claude.ai", "www.claude.ai"})
CLAUDE_RESTRICTED_MARKERS = (
    "account suspended",
    "account disabled",
    "account banned",
    "account deactivated",
    "access restricted",
    "usage policy",
    "terms of service",
)


@dataclass(frozen=True, slots=True)
class ClaudeConversationLink:
    """Identify one Claude conversation discovered from rendered navigation."""

    conversation_id: str
    url: str
    title: str
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class ClaudeConversationSync:
    """Summarize one Claude conversation replacement."""

    message_count: int
    added_or_changed: int
    unchanged_messages: int
    unchanged_sessions: int


class ClaudeNoCacheableMessagesError(RuntimeError):
    """Indicate that a rendered Claude session contains no cacheable messages."""


def claude_history_path(local_store_root: Path | str = LOCAL_STORE_ROOT) -> Path:
    """Return the typed Parquet file used for Claude chat history."""

    return Path(local_store_root).expanduser() / CLAUDE_HISTORY_RELATIVE_DIR / CLAUDE_HISTORY_FILENAME


def utc_now_iso() -> str:
    """Return a compact UTC timestamp for local cache metadata."""

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def claude_conversation_id(value: str) -> str:
    """Extract the stable final path component from a canonical Claude URL."""

    normalized = normalize_claude_conversation_url(value)
    if not normalized:
        return ""
    return urlsplit(normalized).path.rstrip("/").rsplit("/", maxsplit=1)[-1]


def _safe_http_links(values: Any) -> list[str]:
    """Keep only absolute HTTP(S) links from rendered message content."""

    links: list[str] = []
    for value in values if isinstance(values, list) else []:
        candidate = str(value or "").strip()
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            continue
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            links.append(candidate)
    return list(dict.fromkeys(links))


def _discover_claude_links_from_dom(page: Any) -> list[ClaudeConversationLink]:
    """Read conversation links from the visible Claude navigation DOM."""

    payload = page.evaluate(
        r"""() => {
            const textOf = (element) => String(
                element?.innerText
                || element?.textContent
                || element?.getAttribute?.('aria-label')
                || element?.getAttribute?.('title')
                || ''
            ).replace(/\s+/g, ' ').trim();
            const rows = [];
            for (const element of document.querySelectorAll('a[href], [role="link"]')) {
                const rawHref = element.href || element.getAttribute('href') || '';
                let url;
                try { url = new URL(rawHref, location.href); } catch (_) { continue; }
                if (url.protocol !== 'https:' || !['claude.ai', 'www.claude.ai'].includes(url.hostname)) {
                    continue;
                }
                const path = url.pathname.replace(/\/+$/, '');
                const isConversation = /^\/chat\/[A-Za-z0-9_-]+$/.test(path)
                    || /^\/project\/[A-Za-z0-9_-]+\/(?:chat|c)\/[A-Za-z0-9_-]+$/.test(path)
                    || (/^\/project\/[A-Za-z0-9_-]+$/.test(path)
                        && /^[A-Za-z0-9_-]+$/.test(url.searchParams.get('chat') || ''));
                if (!isConversation) continue;
                const row = element.closest('tr, li') || element.parentElement || element;
                const timestamp = row.querySelector('time[datetime]')?.getAttribute('datetime') || '';
                rows.push({href: url.href, title: textOf(element) || textOf(row), updatedAt: timestamp});
            }
            return rows;
        }"""
    )
    links: list[ClaudeConversationLink] = []
    by_url: dict[str, ClaudeConversationLink] = {}
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        url = normalize_claude_conversation_url(str(item.get("href") or ""))
        conversation_id = claude_conversation_id(url)
        if not url or not conversation_id:
            continue
        candidate = ClaudeConversationLink(
            conversation_id=conversation_id,
            url=url,
            title=str(item.get("title") or "").strip() or f"Claude session {conversation_id}",
            updated_at=str(item.get("updatedAt") or "").strip(),
        )
        previous = by_url.get(url)
        if previous is None or len(candidate.title) > len(previous.title):
            by_url[url] = candidate
    links.extend(by_url.values())
    return links


def _scroll_claude_navigation(page: Any) -> dict[str, Any]:
    """Scroll the rendered conversation list without touching the message page."""

    result = page.evaluate(
        r"""() => {
            const candidates = [...document.querySelectorAll('*')].filter((element) => {
                const style = getComputedStyle(element);
                return element.scrollHeight > element.clientHeight + 4
                    && (style.overflowY === 'auto' || style.overflowY === 'scroll');
            });
            const target = candidates.find((element) =>
                element.querySelector?.('a[href*="/chat/"], a[href*="/project/"]')
            ) || document.scrollingElement || document.documentElement;
            const before = target.scrollTop;
            target.scrollTop = Math.min(target.scrollHeight, before + Math.max(480, target.clientHeight * 0.85));
            target.dispatchEvent(new Event('scroll', {bubbles: true}));
            return {
                moved: target.scrollTop !== before,
                scrollTop: target.scrollTop,
                scrollHeight: target.scrollHeight,
            };
        }"""
    )
    return dict(result) if isinstance(result, dict) else {}


def discover_claude_conversations(page: Any) -> list[ClaudeConversationLink]:
    """Enumerate all conversations exposed by Claude's rendered chats page."""

    goto_with_retry(page, CLAUDE_CHATS_URL, attempts=2, timeout_ms=90_000)
    page.wait_for_timeout(500)
    collected: dict[str, ClaudeConversationLink] = {}
    previous_signature: tuple[int, int, int] | None = None
    for _round_index in range(CLAUDE_DISCOVERY_ROUND_LIMIT):
        for link in _discover_claude_links_from_dom(page):
            collected.setdefault(link.url, link)
        scroll_state = _scroll_claude_navigation(page)
        signature = (
            len(collected),
            int(scroll_state.get("scrollTop") or 0),
            int(scroll_state.get("scrollHeight") or 0),
        )
        if signature == previous_signature or not scroll_state.get("moved"):
            break
        previous_signature = signature
        page.wait_for_timeout(300)
    return list(collected.values())


def _wait_for_claude_ready(page, timeout_seconds: float = CLAUDE_READY_TIMEOUT_SECONDS) -> None:
    """Wait for a signed-in Claude page to expose its rendered composer."""

    checks = max(1, int(max(1.0, timeout_seconds) / 0.5))
    last_title = ""
    for _attempt in range(checks):
        body_text = str(
            page.evaluate("() => document.body ? (document.body.innerText || '') : ''") or ""
        ).lower()
        last_title = str(getattr(page, "title", lambda: "")() or "")
        if any(marker in body_text for marker in CLAUDE_RESTRICTED_MARKERS):
            raise RuntimeError("The selected Claude account is restricted or unavailable.")
        if re.search(r"\b(?:sign in|log in|sign up|create account)\b", body_text):
            raise RuntimeError("The selected browser is not signed in to Claude.")
        composer = page.locator(visible_claude_composer_selector())
        if composer.count() == 1 and composer.first.is_visible():
            return
        page.wait_for_timeout(500)
    raise RuntimeError(
        "Claude did not expose an authenticated message composer before the startup timeout. "
        f"Last page: {last_title or 'unknown'}."
    )


def _prepare_claude_conversation_for_rendering(page: Any) -> None:
    """Render older Claude messages by moving the conversation scroll owner to its top."""

    page.evaluate(
        r"""() => {
            const feed = document.querySelector('main [role="feed"]');
            let target = feed;
            for (let current = feed?.parentElement; current; current = current.parentElement) {
                const style = getComputedStyle(current);
                if (current.scrollHeight > current.clientHeight + 4
                    && (style.overflowY === 'auto' || style.overflowY === 'scroll')) {
                    target = current;
                    break;
                }
            }
            if (target) {
                target.scrollTop = 0;
                target.dispatchEvent(new Event('scroll', {bubbles: true}));
            }
        }"""
    )
    page.wait_for_timeout(CLAUDE_RENDER_SETTLE_MILLISECONDS)


def extract_claude_conversation_messages(
    page: Any,
    conversation: ClaudeConversationLink,
) -> list[dict[str, Any]]:
    """Extract ordered, rendered user and assistant messages from one Claude page."""

    payload = page.evaluate(
        r"""({fallbackTitle}) => {
            const normalizeText = (value) => String(value || '')
                .replace(/\u200b/g, '')
                .replace(/\r\n?/g, '\n')
                .replace(/[ \t]+\n/g, '\n')
                .replace(/\n{3,}/g, '\n\n')
                .trim();
            const modelButton = [...document.querySelectorAll('button[aria-label^="Model:"]')]
                .find((button) => button.getClientRects().length > 0);
            const renameButton = document.querySelector('main button[aria-label*="rename chat" i]');
            const title = normalizeText(renameButton?.innerText || renameButton?.textContent)
                || normalizeText(fallbackTitle);
            const modelLabel = normalizeText(
                modelButton?.getAttribute('aria-label') || modelButton?.innerText || ''
            ).replace(/^Model:\s*/i, '');
            const articles = [...document.querySelectorAll('main [role="article"]')];
            let turnIndex = 0;
            const messages = [];
            for (const article of articles) {
                const userRoot = article.querySelector('[data-testid="user-message"]');
                const assistantRoot = article.querySelector('[data-cds="Prose"], .prose');
                const role = userRoot ? 'user' : assistantRoot ? 'assistant' : '';
                const contentRoot = userRoot || assistantRoot;
                if (!role || !contentRoot) continue;
                const contentText = normalizeText(contentRoot.innerText || contentRoot.textContent);
                if (!contentText) continue;
                if (role === 'user') turnIndex += 1;
                const links = [...contentRoot.querySelectorAll('a[href]')].map((link) => {
                    try { return new URL(link.href, location.href).href; } catch (_) { return ''; }
                }).filter((link) => /^https?:/i.test(link));
                messages.push({
                    conversation_title: title,
                    turn_index: turnIndex,
                    message_index: messages.length,
                    role,
                    author_label: role === 'user' ? 'You' : 'Claude',
                    content_text: contentText,
                    content_html: String(contentRoot.innerHTML || '').trim(),
                    source_links: [...new Set(links)],
                    model_label: role === 'assistant' ? modelLabel : '',
                    message_timestamp: article.querySelector('time[datetime]')?.getAttribute('datetime') || '',
                });
            }
            return {title, modelLabel, messages};
        }""",
        {"fallbackTitle": conversation.title},
    )
    if not isinstance(payload, dict):
        return []
    return [dict(item) for item in payload.get("messages", []) if isinstance(item, dict)]


class ClaudeHistoryStore:
    """Merge complete Claude sessions into one atomically persisted Parquet file."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        rows = read_parquet_rows(self.path)
        if self.path.exists() and rows is None:
            raise RuntimeError(f"Claude history Parquet is unreadable: {self.path}")
        self._rows: dict[str, dict[str, Any]] = {
            str(row.get("message_key")): dict(row)
            for row in rows or []
            if row.get("message_key")
        }

    @property
    def rows(self) -> list[dict[str, Any]]:
        """Return deterministic rows ordered by session and message position."""

        return sort_history_rows(self._rows.values())

    @property
    def cached_conversations(self) -> int:
        """Return the number of unique cached sessions."""

        return len({str(row.get("conversation_id") or "") for row in self._rows.values()})

    @property
    def cached_messages(self) -> int:
        """Return the number of cached message rows."""

        return len(self._rows)

    def replace_conversation(
        self,
        conversation: ClaudeConversationLink,
        messages: list[dict[str, Any]],
        captured_at: str,
    ) -> ClaudeConversationSync:
        """Replace one session while preserving first-seen metadata for unchanged rows."""

        previous, next_rows = partition_conversation_rows(
            self._rows,
            conversation.conversation_id,
        )
        added_or_changed = 0
        unchanged_messages = 0
        for message in messages:
            role = str(message.get("role") or "").strip().lower()
            content_text = str(message.get("content_text") or "").replace("\x00", "").strip()
            if role not in {"user", "assistant"} or not content_text:
                continue
            message_index = int(message.get("message_index") or 0)
            message_key = f"{conversation.conversation_id}:{message_index}:{role}"
            content_sha256 = hashlib.sha256(content_text.encode("utf-8")).hexdigest()
            prior = previous.get(message_key)
            same_content = prior is not None and prior.get("content_sha256") == content_sha256
            source_timestamp = str(message.get("message_timestamp") or "").strip()
            row = {
                "schema_version": CLAUDE_HISTORY_SCHEMA_VERSION,
                "platform": "claude",
                "conversation_id": conversation.conversation_id,
                "conversation_url": conversation.url,
                "conversation_title": str(
                    message.get("conversation_title") or conversation.title
                ).strip(),
                "message_key": message_key,
                "turn_index": int(message.get("turn_index") or 0),
                "message_index": message_index,
                "role": role,
                "author_label": str(
                    message.get("author_label") or ("You" if role == "user" else "Claude")
                ).strip(),
                "content_text": content_text,
                "content_html": str(message.get("content_html") or "").replace("\x00", "").strip(),
                "content_sha256": content_sha256,
                "source_links": _safe_http_links(message.get("source_links")),
                "model_label": str(message.get("model_label") or "").strip(),
                "first_seen_at": (
                    str(prior.get("first_seen_at") or captured_at)
                    if same_content
                    else captured_at
                ),
                "last_seen_at": source_timestamp or (str(prior.get("last_seen_at") or "") if same_content else captured_at),
            }
            if history_rows_match(prior, row, CLAUDE_HISTORY_SCHEMA.names):
                unchanged_messages += 1
            else:
                added_or_changed += 1
            next_rows[message_key] = row
        if not next_rows or not any(
            str(row.get("conversation_id") or "") == conversation.conversation_id
            for row in next_rows.values()
        ):
            raise ClaudeNoCacheableMessagesError("Claude session exposed no cacheable text messages.")
        self._rows = next_rows
        message_count = sum(
            str(row.get("conversation_id") or "") == conversation.conversation_id
            for row in next_rows.values()
        )
        return ClaudeConversationSync(
            message_count=message_count,
            added_or_changed=added_or_changed,
            unchanged_messages=unchanged_messages,
            unchanged_sessions=int(
                message_count > 0 and unchanged_messages == message_count
            ),
        )

    def save(self) -> None:
        """Persist all cached messages with schema verification and atomic replacement."""

        write_parquet_rows_atomic(self.path, self.rows, CLAUDE_HISTORY_SCHEMA)


def build_claude_initial_snapshot(
    version: str,
    local_store_root: Path | str = LOCAL_STORE_ROOT,
) -> TaskSnapshot:
    """Hydrate an idle Claude snapshot from the local Parquet cache."""

    history_path = claude_history_path(local_store_root)
    store = ClaudeHistoryStore(history_path)
    snapshot = TaskSnapshot(
        version=version,
        account_name="Claude",
        output_dir=str(history_path.parent),
        progress_unit="sessions",
        discovered_tweets=store.cached_conversations,
        queued_tweets=store.cached_conversations,
        processed_tweets=store.cached_conversations,
        downloaded_posts=store.cached_conversations,
        downloaded_tweets=store.cached_messages,
        discovered_images=store.cached_messages,
        message=(
            f"Ready. Found existing Claude history: {store.cached_conversations:,} sessions, "
            f"{store.cached_messages:,} messages."
        ),
    )
    return snapshot


def sync_claude_history(
    state: TaskState,
    config: CrawlConfig,
    should_stop: Callable[[], bool],
    local_store_root: Path | str = LOCAL_STORE_ROOT,
) -> dict[str, Any]:
    """Cache all browser-rendered Claude conversations into local Parquet."""

    descriptor = browser_descriptors(config).get(config.claude_browser)
    if descriptor is None:
        raise RuntimeError(f"Unsupported Claude browser: {config.claude_browser}")
    if descriptor.engine != "chromium":
        raise RuntimeError("Claude text history currently requires a Chromium browser such as Edge")

    history_path = claude_history_path(local_store_root)
    store = ClaudeHistoryStore(history_path)
    state.update(
        phase="collecting",
        progress_unit="sessions",
        account_name="Claude",
        output_dir=str(history_path.parent),
        downloaded_posts=store.cached_conversations,
        downloaded_tweets=store.cached_messages,
        discovered_images=store.cached_messages,
        downloaded_images=0,
        message="Opening authenticated Claude history in the selected browser...",
    )
    state.append_event("Opening authenticated Claude history in the selected browser.")

    processed_sessions = 0
    discovered_messages = 0
    added_or_changed = 0
    unchanged_messages = 0
    unchanged_sessions = 0
    failed_sessions = 0
    stopped = False
    with sync_playwright_or_error() as playwright:
        with launch_chromium_context(
            playwright,
            descriptor,
            headless=False,
            clone_profile_first=True,
            background_window=True,
        ) as context:
            page = context.pages[0] if context.pages else context.new_page()
            goto_with_retry(page, CLAUDE_HOME_URL, attempts=3, timeout_ms=90_000)
            _wait_for_claude_ready(page)
            conversations = discover_claude_conversations(page)
            if not conversations:
                raise RuntimeError(
                    "Claude history discovery returned no rendered sessions. "
                    "The authenticated Chats page may not have loaded."
                )
            state.update(
                phase="downloading",
                discovered_tweets=len(conversations),
                queued_tweets=len(conversations),
                discovery_complete=True,
                message=f"Found {len(conversations):,} Claude sessions; loading rendered messages...",
            )
            state.append_event(f"Found {len(conversations):,} Claude sessions in {descriptor.label}.")

            for index, conversation in enumerate(conversations, start=1):
                if should_stop():
                    stopped = True
                    break
                scan_wait = config.cache_scan_wait("claude", "text")
                if index > 1 and scan_wait > 0:
                    if wait_for_cache_scan(scan_wait, should_stop):
                        stopped = True
                        break
                last_error: Exception | None = None
                for attempt_index in range(CLAUDE_CONVERSATION_RETRY_LIMIT):
                    try:
                        goto_with_retry(page, conversation.url, attempts=2, timeout_ms=90_000)
                        page.wait_for_timeout(CLAUDE_RENDER_SETTLE_MILLISECONDS)
                        _prepare_claude_conversation_for_rendering(page)
                        messages = extract_claude_conversation_messages(page, conversation)
                        captured_at = utc_now_iso()
                        result = store.replace_conversation(conversation, messages, captured_at)
                        store.save()
                        discovered_messages += result.message_count
                        added_or_changed += result.added_or_changed
                        unchanged_messages += result.unchanged_messages
                        unchanged_sessions += result.unchanged_sessions
                        state.append_event(
                            f"Cached Claude session {index:,}/{len(conversations):,}: "
                            f"{conversation.title} ({result.message_count:,} messages)."
                        )
                        last_error = None
                        break
                    except ClaudeNoCacheableMessagesError:
                        state.append_event(
                            f"Skipped Claude session {index:,}/{len(conversations):,}: "
                            "no cacheable text messages."
                        )
                        last_error = None
                        break
                    except Exception as exc:
                        last_error = exc
                        if attempt_index + 1 < CLAUDE_CONVERSATION_RETRY_LIMIT:
                            state.append_event(
                                f"Retrying Claude session {index:,}/{len(conversations):,} "
                                f"after attempt {attempt_index + 1:,}: {str(exc).splitlines()[0][:300]}"
                            )
                            page.wait_for_timeout(CLAUDE_RENDER_SETTLE_MILLISECONDS)
                if last_error is not None:
                    failed_sessions += 1
                    state.append_event(
                        f"Failed Claude session {index:,}/{len(conversations):,}: "
                        f"{str(last_error).splitlines()[0][:300]}"
                    )
                processed_sessions = index
                state.update(
                    processed_tweets=processed_sessions,
                    downloaded_posts=store.cached_conversations,
                    downloaded_tweets=store.cached_messages,
                    skipped_tweets=unchanged_sessions,
                    discovered_images=discovered_messages,
                    failed_tweets=failed_sessions,
                )

            stopped = stopped or should_stop()

    phase = "stopped" if stopped else "completed"
    message = (
        f"{'Stopped' if stopped else 'Finished'} Claude history sync after "
        f"{processed_sessions:,}/{len(conversations):,} sessions; found {discovered_messages:,} messages, "
        f"added or changed {added_or_changed:,} messages, unchanged {unchanged_sessions:,} sessions, "
        f"failed {failed_sessions:,}; {store.cached_conversations:,} sessions and "
        f"{store.cached_messages:,} messages cached."
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
        skipped_tweets=unchanged_sessions,
        failed_tweets=failed_sessions,
        message=message,
    )
    state.append_event(message)
    return {
        "sessions": processed_sessions,
        "messages": discovered_messages,
        "added_or_changed": added_or_changed,
        "unchanged": unchanged_sessions,
        "unchanged_messages": unchanged_messages,
        "failed": failed_sessions,
        "stopped": stopped,
    }
