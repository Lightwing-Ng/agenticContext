"""Browser-backed Gemini session history caching."""

# Code version: v1.10.5-codex.1

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .browser_sessions import (
    TRANSIENT_BROWSER_ERROR_MARKERS,
    browser_descriptors,
    goto_with_retry,
    launch_chromium_context,
    sync_playwright_or_error,
)
from .config import LOCAL_STORE_ROOT, CrawlConfig
from .resource_persistence import (
    GEMINI_HISTORY_FILENAME,
    GEMINI_HISTORY_SCHEMA,
    GEMINI_HISTORY_SCHEMA_VERSION,
    read_parquet_rows,
    write_parquet_rows_atomic,
)
from .safari_automation import SafariContext, SafariPage
from .state import TaskSnapshot, TaskState, utc_now


GEMINI_HOME_URL = "https://gemini.google.com/app"
GEMINI_HISTORY_RELATIVE_DIR = Path("llm") / "gemini"
GEMINI_DISCOVERY_CHECKPOINT_FILENAME = "discovery_checkpoint.json"
GEMINI_CONVERSATION_PATH_PATTERN = re.compile(r"^/app/([A-Za-z0-9_-]+)$")
GEMINI_READY_TIMEOUT_SECONDS = 45.0
GEMINI_RENDER_SETTLE_MILLISECONDS = 1_000
GEMINI_BOT_CHECK_POLL_MILLISECONDS = 1_000
GEMINI_CONVERSATION_RETRY_LIMIT = 3
GEMINI_HISTORY_RPC_ID = "MaZiqc"
GEMINI_CONVERSATION_RPC_ID = "hNvQHb"
GEMINI_HISTORY_RPC_PAGE_SIZE = 1_000
GEMINI_HISTORY_RPC_PAUSE_MILLISECONDS = 100
GEMINI_HISTORY_RPC_RETRY_LIMIT = 3
GEMINI_HISTORY_RPC_ACCESSIBLE_LIMIT = 500
GEMINI_HISTORY_RPC_READY_WAIT_MILLISECONDS = 60_000
GEMINI_DISCOVERY_CHECKPOINT_MAX_AGE_SECONDS = 24 * 60 * 60
GEMINI_TRANSIENT_NAVIGATION_RETRY_BASE_MILLISECONDS = 5_000
GEMINI_TRANSIENT_NAVIGATION_COOLDOWN_MILLISECONDS = 30_000
GEMINI_TRANSIENT_NAVIGATION_MARKERS = TRANSIENT_BROWSER_ERROR_MARKERS
GEMINI_BOT_CHECK_MARKERS = (
    "unusual traffic",
    "verify you are human",
    "verify you're human",
    "verify that you're human",
    "are you a robot",
    "suspicious activity",
    "security check",
    "verify it's you",
    "verify it’s you",
    "captcha",
    "recaptcha",
)
GEMINI_RPC_RESPONSE_SLICE_CHARS = 96 * 1024


@dataclass(frozen=True, slots=True)
class GeminiConversationLink:
    """Identify one session discovered from the Gemini navigation."""

    conversation_id: str
    url: str
    title: str


@dataclass(frozen=True, slots=True)
class GeminiSyncResult:
    """Summarize one browser-backed Gemini history sync."""

    discovered_conversations: int
    processed_conversations: int
    discovered_messages: int
    new_messages: int
    unchanged_conversations: int
    failed_conversations: int
    cached_conversations: int
    cached_messages: int
    stopped: bool = False


class GeminiNoCacheableMessagesError(RuntimeError):
    """Indicate that a rendered session contains no text rows for the text cache."""


def gemini_history_dir(local_store_root: Path | str = LOCAL_STORE_ROOT) -> Path:
    """Return the local directory dedicated to Gemini chat history."""
    return Path(local_store_root) / GEMINI_HISTORY_RELATIVE_DIR


def gemini_history_path(local_store_root: Path | str = LOCAL_STORE_ROOT) -> Path:
    """Return the typed Parquet file used for Gemini chat history."""
    return gemini_history_dir(local_store_root) / GEMINI_HISTORY_FILENAME


def gemini_discovery_checkpoint_path(
    local_store_root: Path | str = LOCAL_STORE_ROOT,
) -> Path:
    """Return the local resume checkpoint for a completed history discovery."""
    return gemini_history_dir(local_store_root) / GEMINI_DISCOVERY_CHECKPOINT_FILENAME


def save_gemini_discovery_checkpoint(
    links: list[GeminiConversationLink],
    local_store_root: Path | str = LOCAL_STORE_ROOT,
) -> Path:
    """Atomically persist one complete discovery result for interruption-safe resume."""
    checkpoint_path = gemini_discovery_checkpoint_path(local_store_root)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": "v1.0.0",
        "captured_at": utc_now(),
        "sessions": [
            {
                "conversation_id": link.conversation_id,
                "url": link.url,
                "title": link.title,
            }
            for link in links
        ],
    }
    temporary_path = checkpoint_path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary_path.replace(checkpoint_path)
    return checkpoint_path


def load_gemini_discovery_checkpoint(
    local_store_root: Path | str = LOCAL_STORE_ROOT,
) -> list[GeminiConversationLink]:
    """Load a recent, valid discovery checkpoint or return an empty list."""
    checkpoint_path = gemini_discovery_checkpoint_path(local_store_root)
    try:
        if time.time() - checkpoint_path.stat().st_mtime > GEMINI_DISCOVERY_CHECKPOINT_MAX_AGE_SECONDS:
            return []
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    links: list[GeminiConversationLink] = []
    seen: set[str] = set()
    for item in payload.get("sessions", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        url = normalize_gemini_conversation_url(str(item.get("url") or ""))
        conversation_id = gemini_conversation_id(url)
        if not conversation_id or conversation_id in seen:
            continue
        seen.add(conversation_id)
        links.append(
            GeminiConversationLink(
                conversation_id=conversation_id,
                url=url,
                title=str(item.get("title") or "").strip()
                or f"Gemini session {conversation_id}",
            )
        )
    return links


def normalize_gemini_conversation_url(value: str) -> str:
    """Return one canonical Gemini session URL or an empty string."""
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return ""
    if parsed.scheme != "https" or parsed.netloc.lower() != "gemini.google.com":
        return ""
    match = GEMINI_CONVERSATION_PATH_PATTERN.fullmatch(parsed.path.rstrip("/"))
    if match is None:
        return ""
    return urlunsplit(("https", "gemini.google.com", f"/app/{match.group(1)}", "", ""))


def is_gemini_conversation_url(value: str) -> bool:
    """Return whether a URL points to one Gemini session."""
    return bool(normalize_gemini_conversation_url(value))


def gemini_conversation_id(value: str) -> str:
    """Extract a stable Gemini session identifier."""
    normalized = normalize_gemini_conversation_url(value)
    if not normalized:
        return ""
    return urlsplit(normalized).path.rsplit("/", maxsplit=1)[-1]


def build_gemini_initial_snapshot(
    version: str,
    local_store_root: Path | str = LOCAL_STORE_ROOT,
) -> TaskSnapshot:
    """Hydrate an idle Gemini snapshot from the local Parquet cache."""
    history_path = gemini_history_path(local_store_root)
    rows = read_parquet_rows(history_path)
    snapshot = TaskSnapshot(
        version=version,
        account_name="Gemini",
        output_dir=str(history_path.parent),
        progress_unit="sessions",
    )
    if not rows:
        return snapshot

    conversation_count = len(
        {
            str(row.get("conversation_id") or "").strip()
            for row in rows
            if str(row.get("conversation_id") or "").strip()
        }
    )
    message_count = len(rows)
    snapshot.discovered_tweets = conversation_count
    snapshot.downloaded_posts = conversation_count
    snapshot.downloaded_tweets = message_count
    snapshot.discovered_images = message_count
    conversation_label = "session" if conversation_count == 1 else "sessions"
    message_label = "message" if message_count == 1 else "messages"
    snapshot.message = (
        f"Ready. Found existing Gemini history: {conversation_count:,} {conversation_label}, "
        f"{message_count:,} {message_label}."
    )
    return snapshot


class GeminiHistoryStore:
    """Merge complete Gemini sessions into one atomic Parquet file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        rows = read_parquet_rows(path)
        if path.exists() and rows is None:
            raise RuntimeError(f"Gemini history Parquet is unreadable: {path}")
        self._rows_by_key: dict[str, dict[str, Any]] = {}
        for row in rows or []:
            message_key = str(row.get("message_key") or "").strip()
            if message_key:
                self._rows_by_key[message_key] = dict(row)

    @property
    def rows(self) -> list[dict[str, Any]]:
        """Return deterministic rows ordered by session and message position."""
        return sorted(
            self._rows_by_key.values(),
            key=lambda row: (
                str(row.get("conversation_id") or ""),
                int(row.get("message_index") or 0),
            ),
        )

    @property
    def cached_conversations(self) -> int:
        """Return the number of unique cached sessions."""
        return len(
            {
                str(row.get("conversation_id") or "").strip()
                for row in self._rows_by_key.values()
                if str(row.get("conversation_id") or "").strip()
            }
        )

    @property
    def cached_messages(self) -> int:
        """Return the number of cached message rows."""
        return len(self._rows_by_key)

    @property
    def cached_conversation_ids(self) -> set[str]:
        """Return the stable identifiers already represented in the text cache."""
        return {
            str(row.get("conversation_id") or "").strip()
            for row in self._rows_by_key.values()
            if str(row.get("conversation_id") or "").strip()
        }

    def replace_conversation(
        self,
        conversation: GeminiConversationLink,
        messages: list[dict[str, Any]],
        captured_at: str,
    ) -> tuple[int, bool]:
        """Replace one session atomically in memory and report new content."""
        previous_rows = {
            key: row
            for key, row in self._rows_by_key.items()
            if str(row.get("conversation_id") or "") == conversation.conversation_id
        }
        next_rows: dict[str, dict[str, Any]] = {}
        new_message_count = 0
        for message in messages:
            message_index = int(message.get("message_index") or 0)
            role = str(message.get("role") or "").strip().lower()
            content_text = str(message.get("content_text") or "").replace("\x00", "").strip()
            content_html = str(message.get("content_html") or "").replace("\x00", "").strip()
            if role not in {"user", "assistant"} or not content_text:
                continue
            message_key = f"{conversation.conversation_id}:{message_index}:{role}"
            content_sha256 = hashlib.sha256(content_text.encode("utf-8")).hexdigest()
            previous = previous_rows.get(message_key)
            first_seen_at = captured_at
            if previous and str(previous.get("content_sha256") or "") == content_sha256:
                first_seen_at = str(previous.get("first_seen_at") or captured_at)
            else:
                new_message_count += 1
            source_timestamp = str(message.get("message_timestamp") or "").strip()
            previous_source_timestamp = ""
            if previous and str(previous.get("content_sha256") or "") == content_sha256:
                previous_source_timestamp = str(previous.get("last_seen_at") or "").strip()
            last_seen_at = source_timestamp or previous_source_timestamp or captured_at
            source_links = [
                str(link).strip()
                for link in message.get("source_links") or []
                if str(link).strip()
            ]
            next_rows[message_key] = {
                "schema_version": GEMINI_HISTORY_SCHEMA_VERSION,
                "platform": "gemini",
                "conversation_id": conversation.conversation_id,
                "conversation_url": conversation.url,
                "conversation_title": str(message.get("conversation_title") or conversation.title).strip(),
                "message_key": message_key,
                "turn_index": int(message.get("turn_index") or 0),
                "message_index": message_index,
                "role": role,
                "author_label": str(message.get("author_label") or role.title()).strip(),
                "content_text": content_text,
                "content_html": content_html,
                "content_sha256": content_sha256,
                "source_links": list(dict.fromkeys(source_links)),
                "model_label": str(message.get("model_label") or "").strip(),
                "first_seen_at": first_seen_at,
                "last_seen_at": last_seen_at,
            }

        if not next_rows:
            raise GeminiNoCacheableMessagesError("Gemini session exposed no cacheable text messages.")

        unchanged = (
            set(previous_rows) == set(next_rows)
            and all(
                str(previous_rows[key].get("content_sha256") or "")
                == str(next_rows[key].get("content_sha256") or "")
                for key in next_rows
            )
        )
        for key in previous_rows:
            self._rows_by_key.pop(key, None)
        self._rows_by_key.update(next_rows)
        return new_message_count, unchanged

    def save(self) -> None:
        """Persist all cached messages with schema verification and atomic replacement."""
        write_parquet_rows_atomic(self.path, self.rows, GEMINI_HISTORY_SCHEMA)


def inspect_gemini_session(page) -> dict[str, Any]:
    """Return a browser-independent snapshot of Gemini account readiness."""
    payload = page.evaluate(
        r"""() => {
            const bodyText = document.body ? (document.body.innerText || "") : "";
            const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
            const visible = (element) => {
                if (!element || element.getClientRects().length === 0) return false;
                for (let current = element; current; current = current.parentElement) {
                    const style = getComputedStyle(current);
                    const opacity = Number.parseFloat(style.opacity || '1');
                    if (style.visibility === 'hidden'
                        || style.visibility === 'collapse'
                        || style.display === 'none'
                        || (Number.isFinite(opacity) && opacity <= 0)) return false;
                }
                return true;
            };
            const account = [...document.querySelectorAll(
                '[aria-label^="Google Account"], [aria-label*="Google Account:"]'
            )].find(visible) || null;
            const conversationLinks = [...document.querySelectorAll('a[href]')].filter((link) => {
                try {
                    return /^\/app\/[A-Za-z0-9_-]+\/?$/.test(new URL(link.href, location.href).pathname);
                } catch (_error) {
                    return false;
                }
            }).length;
            const hasComposer = Boolean(document.querySelector('textarea, [contenteditable="true"]'));
            const authAction = [...document.querySelectorAll('a, button, [role="button"]')]
                .filter(visible)
                .filter((element) => !element.closest(
                    'model-response, user-query, [data-test-id="model-response"], '
                    + '[data-message-author-role], textarea, [contenteditable="true"]'
                ))
                .find((element) => /^(?:sign in|log in)$/i.test(normalize(
                    element.getAttribute('aria-label')
                    || element.innerText
                    || element.textContent
                )));
            const signedOut = Boolean(authAction && !account);
            const unsupportedCopy = (
                /gemini (?:isn['’]t|is not) (?:currently )?(?:supported|available) in your country/i
                    .test(bodyText)
                || /gemini\s*(?:目前)?(?:不支持|不支援)(?:你|您)?所在的(?:国家(?:或|\/)?|國家(?:或|\/)?)?(?:地区|地區)/i
                    .test(bodyText)
                || /gemini\s*(?:目前)?(?:无法|無法)在(?:你|您)?所在的(?:国家(?:或|\/)?|國家(?:或|\/)?)?(?:地区|地區)(?:使用|提供服务|提供服務)/i
                    .test(bodyText)
            );
            const unsupportedRegion = unsupportedCopy && !hasComposer && conversationLinks === 0;
            return {
                href: location.href,
                title: document.title || "",
                accountLabel: account ? (account.getAttribute("aria-label") || account.textContent || "") : "",
                conversationLinks,
                hasComposer,
                hasAuthAction: Boolean(authAction),
                signedOut,
                unsupportedRegion,
            };
        }"""
    )
    return dict(payload) if isinstance(payload, dict) else {}


def inspect_gemini_bot_check(page) -> dict[str, Any]:
    """Return visible signals that Google requires a human verification step."""
    payload = page.evaluate(
        r"""(markers) => {
            const title = document.title || "";
            const bodyText = document.body ? (document.body.innerText || "") : "";
            const haystack = `${title}\n${bodyText}`.toLowerCase();
            const marker = markers.find((candidate) => haystack.includes(candidate)) || "";
            const challengeElement = document.querySelector(
                'iframe[src*="captcha"], iframe[src*="recaptcha"], [data-sitekey], [id*="captcha"], '
                + '[class*="captcha"], form[action*="challenge"]'
            );
            return {
                detected: Boolean(marker || challengeElement),
                reason: marker || (challengeElement ? "challenge element" : ""),
                href: location.href,
                title,
            };
        }""",
        list(GEMINI_BOT_CHECK_MARKERS),
    )
    return dict(payload) if isinstance(payload, dict) else {}


def wait_for_gemini_bot_check_clear(
    page,
    state: TaskState | None,
    should_stop,
    resume_phase: str,
) -> bool:
    """Pause in place for a user to complete Google's verification challenge."""
    notified = False
    while True:
        check = inspect_gemini_bot_check(page)
        if not check.get("detected"):
            if notified and state is not None:
                state.update(phase=resume_phase)
                state.append_event("Google verification cleared. Resuming Gemini history sync.")
            return True
        if should_stop():
            return False
        if not notified:
            reason = str(check.get("reason") or "Google verification").strip()
            message = (
                f"Google requested a human verification ({reason}). "
                "Gemini sync is paused; complete it in Safari, then leave the page open."
            )
            if state is not None:
                state.update(phase="paused")
                state.append_event(message)
            bring_to_front = getattr(page, "bring_to_front", None)
            if callable(bring_to_front):
                with contextlib.suppress(Exception):
                    bring_to_front()
            notified = True
        page.wait_for_timeout(GEMINI_BOT_CHECK_POLL_MILLISECONDS)


def _wait_for_gemini_ready(page, timeout_seconds: float = GEMINI_READY_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Wait until Gemini exposes authenticated navigation or a chat composer."""
    remaining_checks = max(1, int(max(1.0, timeout_seconds) / 0.5))
    last_snapshot: dict[str, Any] = {}
    consecutive_signed_out_checks = 0
    for _attempt in range(remaining_checks):
        last_snapshot = inspect_gemini_session(page)
        if last_snapshot.get("unsupportedRegion"):
            raise RuntimeError(
                "Gemini Web is not available in the selected browser's current region."
            )
        if last_snapshot.get("signedOut"):
            consecutive_signed_out_checks += 1
            if consecutive_signed_out_checks >= 2:
                raise RuntimeError("The selected browser is not signed in to Gemini.")
            page.wait_for_timeout(500)
            continue
        consecutive_signed_out_checks = 0
        if (
            last_snapshot.get("accountLabel")
            or int(last_snapshot.get("conversationLinks") or 0) > 0
            or last_snapshot.get("hasComposer")
            or is_gemini_conversation_url(str(last_snapshot.get("href") or ""))
        ):
            return last_snapshot
        page.wait_for_timeout(500)
    raise RuntimeError(
        "Gemini did not expose an authenticated chat page before the startup timeout. "
        f"Last page: {last_snapshot.get('title') or last_snapshot.get('href') or 'unknown'}."
    )


def _open_gemini_sidebar(page) -> None:
    """Open the main navigation and its independently collapsible Recents list."""
    opened_sidebar = page.evaluate(
        r"""() => {
            const hasConversationLinks = [...document.querySelectorAll('a[href]')].some((link) => {
                try {
                    return /^\/app\/[A-Za-z0-9_-]+\/?$/.test(new URL(link.href, location.href).pathname);
                } catch (_error) {
                    return false;
                }
            });
            const button = [...document.querySelectorAll('button')].find((candidate) => {
                const label = `${candidate.getAttribute('aria-label') || ''} ${candidate.textContent || ''}`;
                return /(?:main menu|open sidebar)/i.test(label);
            });
            if (button && !hasConversationLinks) button.click();
            return Boolean(button && !hasConversationLinks);
        }"""
    )
    if opened_sidebar:
        page.wait_for_timeout(GEMINI_RENDER_SETTLE_MILLISECONDS)
    for _attempt_index in range(20):
        recents_state = page.evaluate(
            r"""() => {
            const conversationLinks = [...document.querySelectorAll('a[href]')].filter((link) => {
                try {
                    return /^\/app\/[A-Za-z0-9_-]+\/?$/.test(new URL(link.href, location.href).pathname);
                } catch (_error) {
                    return false;
                }
            });
            const recentsSection = document.querySelector('[data-test-id="chats-expandable-section"]');
            const recentsButton = recentsSection?.querySelector(
                'button[data-test-id="expandable-section-toggle"]'
            ) || [...document.querySelectorAll('button')].find((candidate) => {
                const label = `${candidate.getAttribute('aria-label') || ''} ${candidate.textContent || ''}`;
                return /(?:toggle\s+)?recents/i.test(label);
            }) || null;
            const expanded = recentsButton?.getAttribute('aria-expanded') === 'true';
            if (!conversationLinks.length && recentsButton && !expanded) recentsButton.click();
            return {
                buttonFound: Boolean(recentsButton),
                expanded,
                links: conversationLinks.length,
            };
        }"""
        )
        if isinstance(recents_state, dict):
            if int(recents_state.get("links") or 0) > 0 or recents_state.get("expanded"):
                return
        page.wait_for_timeout(500)


def _prepare_gemini_page_for_rendering(page) -> None:
    """Keep the owned browser page usable without routine Safari focus changes."""
    keep_rendering_in_background = getattr(page, "keep_rendering_in_background", None)
    if callable(keep_rendering_in_background):
        keep_rendering_in_background()
        page.wait_for_timeout(GEMINI_RENDER_SETTLE_MILLISECONDS)


def _read_gemini_conversation_links(page) -> list[GeminiConversationLink]:
    """Read the currently rendered session links in DOM order."""
    payload = page.evaluate(
        r"""() => [...document.querySelectorAll('a[href]')].map((link) => {
            let url;
            try {
                url = new URL(link.href, location.href);
            } catch (_error) {
                return null;
            }
            const match = url.pathname.replace(/\/$/, '').match(/^\/app\/([A-Za-z0-9_-]+)$/);
            if (!match) return null;
            return {
                conversationId: match[1],
                url: `${url.origin}/app/${match[1]}`,
                title: (link.innerText || link.getAttribute('aria-label') || '').trim(),
            };
        }).filter(Boolean)"""
    )
    links: list[GeminiConversationLink] = []
    seen: set[str] = set()
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        url = normalize_gemini_conversation_url(str(item.get("url") or ""))
        conversation_id = gemini_conversation_id(url)
        if not conversation_id or conversation_id in seen:
            continue
        seen.add(conversation_id)
        links.append(
            GeminiConversationLink(
                conversation_id=conversation_id,
                url=url,
                title=str(item.get("title") or "").strip() or f"Gemini session {conversation_id}",
            )
        )
    return links


def _is_gemini_history_rpc_url(value: str) -> bool:
    """Return whether a URL targets Gemini's conversation-list RPC."""
    parsed = urlsplit(str(value or ""))
    if parsed.netloc.lower() != "gemini.google.com" or not parsed.path.endswith("/batchexecute"):
        return False
    return any(
        key == "rpcids" and rpc_id == GEMINI_HISTORY_RPC_ID
        for key, rpc_id in parse_qsl(parsed.query, keep_blank_values=True)
    )


def _attach_gemini_history_rpc_capture(page) -> list[Any] | None:
    """Capture Chromium history RPC responses without affecting Safari automation."""
    responses: list[Any] = []
    on_event = getattr(page, "on", None)
    if not callable(on_event):
        return None

    def capture_response(response) -> None:
        if _is_gemini_history_rpc_url(str(getattr(response, "url", ""))):
            responses.append(response)

    on_event("response", capture_response)
    return responses


def _is_gemini_conversation_rpc_url(value: str) -> bool:
    """Return whether a URL targets one rendered Gemini session."""
    parsed = urlsplit(str(value or ""))
    if parsed.netloc.lower() != "gemini.google.com" or not parsed.path.endswith("/batchexecute"):
        return False
    return any(
        key == "rpcids" and rpc_id == GEMINI_CONVERSATION_RPC_ID
        for key, rpc_id in parse_qsl(parsed.query, keep_blank_values=True)
    )


def _attach_gemini_conversation_rpc_capture(page) -> list[Any] | None:
    """Capture rendered-session RPC responses on Chromium pages."""
    responses: list[Any] = []
    on_event = getattr(page, "on", None)
    if not callable(on_event):
        return None

    def capture_response(response) -> None:
        if _is_gemini_conversation_rpc_url(str(getattr(response, "url", ""))):
            responses.append(response)

    on_event("response", capture_response)
    return responses


def _decode_gemini_history_rpc_payloads(body_text: str) -> list[list[Any]]:
    """Decode nested payloads from one Google batchexecute response."""
    payloads: list[list[Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, list):
            if (
                len(value) >= 3
                and value[1] == GEMINI_HISTORY_RPC_ID
                and isinstance(value[2], str)
            ):
                try:
                    payload = json.loads(value[2])
                except json.JSONDecodeError:
                    payload = None
                if isinstance(payload, list):
                    payloads.append(payload)
            for child in value:
                visit(child)
        elif isinstance(value, dict):
            for child in value.values():
                visit(child)

    for line in str(body_text or "").splitlines():
        if not line.startswith("["):
            continue
        try:
            visit(json.loads(line))
        except json.JSONDecodeError:
            continue
    return payloads


def _normalize_gemini_rpc_timestamp(value: Any) -> str:
    """Normalize Gemini's ``[seconds, nanoseconds]`` timestamp to UTC ISO text."""
    seconds: float | None = None
    nanos = 0.0
    if isinstance(value, (list, tuple)) and value:
        try:
            seconds = float(value[0])
            if len(value) > 1 and value[1] is not None:
                nanos = float(value[1])
        except (TypeError, ValueError):
            return ""
    elif isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            seconds = float(value.strip())
        except ValueError:
            return ""
    if seconds is None:
        return ""
    if abs(seconds) >= 1_000_000_000_000:
        seconds /= 1_000
    try:
        timestamp = datetime.fromtimestamp(seconds + nanos / 1_000_000_000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return ""
    return timestamp.replace(microsecond=timestamp.microsecond).isoformat().replace("+00:00", "Z")


def _contains_gemini_conversation_id(value: Any, conversation_id: str) -> bool:
    """Return whether one decoded RPC fragment references the requested session."""
    expected = f"c_{conversation_id}"
    if isinstance(value, str):
        return value == expected
    if isinstance(value, list):
        return any(_contains_gemini_conversation_id(item, conversation_id) for item in value)
    if isinstance(value, dict):
        return any(_contains_gemini_conversation_id(item, conversation_id) for item in value.values())
    return False


def _gemini_message_timestamps_from_rpc_payloads(
    payloads: list[list[Any]],
    conversation_id: str,
) -> dict[int, str]:
    """Extract one source timestamp per Gemini turn from the conversation RPC."""
    timestamps: dict[int, str] = {}
    for payload in payloads:
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], list):
            continue
        turn_index = 0
        for entry in payload[0]:
            if not isinstance(entry, list) or len(entry) < 5:
                continue
            if not _contains_gemini_conversation_id(entry[:2], conversation_id):
                continue
            timestamp = _normalize_gemini_rpc_timestamp(entry[4])
            if timestamp:
                timestamps.setdefault(turn_index, timestamp)
            turn_index += 1
    return timestamps


def _gemini_message_timestamps_from_captured_responses(
    responses: list[Any] | None,
    conversation_id: str,
) -> dict[int, str]:
    """Read the newest matching conversation RPC response from Chromium."""
    if not responses:
        return {}
    for response in reversed(responses):
        try:
            if not _is_gemini_conversation_rpc_url(str(getattr(response, "url", ""))):
                continue
            payloads = _decode_gemini_history_rpc_payloads(response.text())
        except Exception:
            continue
        timestamps = _gemini_message_timestamps_from_rpc_payloads(payloads, conversation_id)
        if timestamps:
            return timestamps
    return {}


def _fetch_gemini_conversation_rpc_payloads(
    page,
    conversation: GeminiConversationLink,
    timeout_ms: int = 15_000,
) -> list[list[Any]]:
    """Fetch the conversation RPC inside Safari without exporting browser credentials."""
    if not isinstance(page, SafariPage):
        return []
    state_key = "__cachelikesGeminiConversationRpc"
    started = page.evaluate(
        r"""({ conversationId, stateKey }) => {
            const tokenMatch = (document.documentElement?.innerHTML || '')
                .match(/(ADR[A-Za-z0-9:_-]{10,})/);
            const at = String(window.WIZ_global_data?.SNlM0e || (tokenMatch ? tokenMatch[1] : ''));
            const state = { state: 'pending', ok: false, status: 0, text: '', error: '' };
            window[stateKey] = state;
            if (!at || typeof fetch !== 'function') {
                state.state = 'failed';
                state.error = !at ? 'Gemini request token was not found.' : 'fetch is unavailable.';
                return { started: false, error: state.error };
            }
            const batch = [[[
                'hNvQHb',
                JSON.stringify(['c_' + conversationId, 10, null, 1, [0], [4], null, 1]),
                null,
                'generic',
            ]]];
            const body = new URLSearchParams({ 'f.req': JSON.stringify(batch), at }).toString();
            const endpoint = new URL('/_/BardChatUi/data/batchexecute', location.origin);
            endpoint.searchParams.set('rpcids', 'hNvQHb');
            endpoint.searchParams.set('source-path', '/app/' + conversationId);
            endpoint.searchParams.set('hl', document.documentElement?.lang || 'en');
            endpoint.searchParams.set('rt', 'c');
            fetch(endpoint.toString(), {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
                    'X-Same-Domain': '1',
                },
                body,
            }).then(async (response) => {
                state.ok = response.ok;
                state.status = response.status;
                state.text = await response.text();
                state.state = 'done';
            }).catch((error) => {
                state.state = 'failed';
                state.error = String(error && error.message ? error.message : error);
            });
            return { started: true };
        }""",
        {"conversationId": conversation.conversation_id, "stateKey": state_key},
    )
    if not isinstance(started, dict) or not started.get("started"):
        return []
    deadline = time.monotonic() + max(1, int(timeout_ms)) / 1_000
    state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        state = page.evaluate(
            """(stateKey) => {
                const state = window[stateKey];
                return state ? {
                    state: state.state || '',
                    ok: Boolean(state.ok),
                    status: Number(state.status || 0),
                    length: String(state.text || '').length,
                    error: String(state.error || ''),
                } : { state: 'missing' };
            }""",
            state_key,
        )
        if isinstance(state, dict) and state.get("state") in {"done", "failed"}:
            break
        page.wait_for_timeout(200)
    if not isinstance(state, dict) or state.get("state") != "done" or not state.get("ok"):
        return []
    response_length = max(0, int(state.get("length") or 0))
    chunks: list[str] = []
    for start in range(0, response_length, GEMINI_RPC_RESPONSE_SLICE_CHARS):
        chunk = page.evaluate(
            """({ stateKey, start, length }) => {
                const text = String(window[stateKey]?.text || '');
                return text.slice(start, start + length);
            }""",
            {
                "stateKey": state_key,
                "start": start,
                "length": GEMINI_RPC_RESPONSE_SLICE_CHARS,
            },
        )
        chunks.append(str(chunk or ""))
    return _decode_gemini_history_rpc_payloads("".join(chunks))


def _gemini_links_and_cursor_from_rpc_payload(
    payload: list[Any],
) -> tuple[list[GeminiConversationLink], str]:
    """Extract stable session links and the next-page cursor from one RPC payload."""
    cursor = payload[1] if len(payload) > 1 and isinstance(payload[1], str) else ""
    entries = payload[2] if len(payload) > 2 and isinstance(payload[2], list) else []
    links: list[GeminiConversationLink] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, list) or not entry or not isinstance(entry[0], str):
            continue
        raw_id = entry[0]
        if not raw_id.startswith("c_"):
            continue
        conversation_id = raw_id[2:]
        url = normalize_gemini_conversation_url(f"{GEMINI_HOME_URL}/{conversation_id}")
        if not url or conversation_id in seen:
            continue
        seen.add(conversation_id)
        title = str(entry[1] if len(entry) > 1 else "").strip()
        links.append(
            GeminiConversationLink(
                conversation_id=conversation_id,
                url=url,
                title=title or f"Gemini session {conversation_id}",
            )
        )
    return links, cursor


def _build_gemini_history_rpc_page_request(
    request,
    cursor: str,
    request_index: int = 1,
) -> tuple[str, str]:
    """Build the next authenticated history-page request from Gemini's own request."""
    pairs = parse_qsl(str(getattr(request, "post_data", "") or ""), keep_blank_values=True)
    request_fields = dict(pairs)
    raw_batch = request_fields.get("f.req", "")
    if not raw_batch or "at" not in request_fields:
        raise RuntimeError("Gemini history RPC did not expose reusable request fields.")
    try:
        batch = json.loads(raw_batch)
        batch[0][0][1] = json.dumps(
            [GEMINI_HISTORY_RPC_PAGE_SIZE, cursor, [0, None, 1]],
            separators=(",", ":"),
        )
    except (IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Gemini history RPC request shape changed.") from exc
    updated_batch = json.dumps(batch, separators=(",", ":"))
    updated_pairs = [(key, updated_batch if key == "f.req" else value) for key, value in pairs]
    del request_index
    return str(getattr(request, "url", "")), urlencode(updated_pairs)


def _fetch_gemini_history_rpc_page(
    page,
    request,
    cursor: str,
    request_index: int,
) -> list[Any]:
    """Fetch and decode one cursor page in the authenticated Chromium page."""
    request_url, request_body = _build_gemini_history_rpc_page_request(
        request,
        cursor,
        request_index,
    )
    response = page.evaluate(
        """async ({ url, body }) => {
            const result = await fetch(url, {
                method: 'POST',
                credentials: 'include',
                headers: { 'content-type': 'application/x-www-form-urlencoded;charset=UTF-8' },
                body,
            });
            return { ok: result.ok, status: result.status, text: await result.text() };
        }""",
        {"url": request_url, "body": request_body},
    )
    if not isinstance(response, dict) or not response.get("ok"):
        status = int(response.get("status") or 0) if isinstance(response, dict) else 0
        raise RuntimeError(f"Gemini history RPC returned HTTP {status}.")
    payloads = _decode_gemini_history_rpc_payloads(str(response.get("text") or ""))
    if not payloads:
        raise RuntimeError(
            f"Gemini history RPC returned an unreadable {len(str(response.get('text') or '')):,}-character page."
        )
    return payloads


def _collect_gemini_rpc_conversation_links(
    page,
    captured_responses: list[Any] | None,
    config: CrawlConfig,
    should_stop,
    state: TaskState | None = None,
) -> list[GeminiConversationLink]:
    """Collect all Chromium sessions through Gemini's cursor-based history RPC."""
    if captured_responses is None:
        return []
    max_conversations = max(1, int(config.gemini_max_conversations))
    collected: dict[str, GeminiConversationLink] = {}
    page_request = None
    cursor = ""
    wait_rounds = max(1, GEMINI_HISTORY_RPC_READY_WAIT_MILLISECONDS // 500)
    for wait_index in range(wait_rounds + 1):
        for response in list(captured_responses):
            try:
                payloads = _decode_gemini_history_rpc_payloads(response.text())
            except Exception:
                continue
            for payload in payloads:
                links, payload_cursor = _gemini_links_and_cursor_from_rpc_payload(payload)
                for link in links:
                    collected.setdefault(link.conversation_id, link)
                if len(links) > 1 and payload_cursor:
                    page_request = getattr(response, "request", None)
                    cursor = payload_cursor
        if page_request is not None:
            break
        if wait_index < wait_rounds:
            page.wait_for_timeout(500)
    if page_request is None:
        raise RuntimeError(
            "Edge did not expose the ordinary Gemini history RPC before the 60-second timeout."
        )

    pause_ms = GEMINI_HISTORY_RPC_PAUSE_MILLISECONDS
    previous_cursors: set[str] = set()
    request_index = 0
    while (
        page_request is not None
        and cursor
        and cursor not in previous_cursors
        and len(collected) < max_conversations
        and not should_stop()
    ):
        page.wait_for_timeout(pause_ms)
        payloads: list[Any] | None = None
        last_error: Exception | None = None
        for attempt_index in range(GEMINI_HISTORY_RPC_RETRY_LIMIT):
            request_index += 1
            try:
                payloads = _fetch_gemini_history_rpc_page(
                    page,
                    page_request,
                    cursor,
                    request_index,
                )
                break
            except Exception as exc:
                last_error = exc
                if attempt_index + 1 < GEMINI_HISTORY_RPC_RETRY_LIMIT:
                    retry_delay_ms = pause_ms * (2**attempt_index)
                    if state is not None:
                        state.append_event(
                            f"Gemini history pagination retry {attempt_index + 1:,}/"
                            f"{GEMINI_HISTORY_RPC_RETRY_LIMIT - 1:,} after "
                            f"{retry_delay_ms / 1_000:g} seconds."
                        )
                    page.wait_for_timeout(retry_delay_ms)
        if payloads is None:
            if len(collected) >= GEMINI_HISTORY_RPC_ACCESSIBLE_LIMIT:
                if state is not None:
                    state.append_event(
                        f"Google ended the Chromium history cursor after "
                        f"{len(collected):,} accessible Gemini sessions."
                    )
                break
            raise RuntimeError(
                f"Gemini history pagination failed after discovering {len(collected):,} sessions: "
                f"{last_error or 'unknown RPC error'}"
            )
        previous_cursors.add(cursor)
        next_cursor = ""
        page_links: list[GeminiConversationLink] = []
        for payload in payloads:
            links, payload_cursor = _gemini_links_and_cursor_from_rpc_payload(payload)
            page_links.extend(links)
            if payload_cursor:
                next_cursor = payload_cursor
        for link in page_links:
            collected.setdefault(link.conversation_id, link)
            if len(collected) >= max_conversations:
                break
        if not page_links:
            break
        cursor = next_cursor
        if state is not None:
            state.append_event(
                f"Discovered {len(collected):,} Gemini sessions through Chromium history pagination."
            )
    return list(collected.values())[:max_conversations]


def _scroll_gemini_conversation_navigation(page) -> dict[str, Any]:
    """Advance the scrollable sidebar region that owns session links."""
    payload = page.evaluate(
        r"""() => {
            const links = [...document.querySelectorAll('a[href]')].filter((link) => {
                try {
                    return /^\/app\/[A-Za-z0-9_-]+\/?$/.test(new URL(link.href, location.href).pathname);
                } catch (_error) {
                    return false;
                }
            });
            const conversationList = document.querySelector('conversations-list');
            let target = conversationList
                ? conversationList.closest('infinite-scroller')
                : (links.length ? links[links.length - 1].closest('infinite-scroller') : null);
            if (!target && links.length) target = links[links.length - 1].parentElement;
            while (target && target !== document.body) {
                const style = getComputedStyle(target);
                if (target.scrollHeight > target.clientHeight + 4 && /(auto|scroll)/.test(style.overflowY)) break;
                target = target.parentElement;
            }
            if (!target || target === document.body) {
                const candidates = [...document.querySelectorAll('*')].filter((element) => {
                    if (element.scrollHeight <= element.clientHeight + 4) return false;
                    const style = getComputedStyle(element);
                    return /(auto|scroll)/.test(style.overflowY)
                        && Boolean(element.querySelector('a[href*="/app/"]'));
                });
                target = candidates.sort((left, right) => left.clientHeight - right.clientHeight)[0] || null;
            }
            if (!target) {
                target = document.querySelector('conversations-list')?.closest('infinite-scroller') || null;
            }
            if (!target) return { moved: false, eventDispatched: false, top: 0, height: 0, viewport: 0 };
            const before = target.scrollTop;
            target.scrollTop = target.scrollHeight;
            target.dispatchEvent(new Event('scroll', { bubbles: true }));
            return {
                moved: target.scrollTop > before,
                eventDispatched: true,
                top: target.scrollTop,
                height: target.scrollHeight,
                viewport: target.clientHeight,
            };
        }"""
    )
    return dict(payload) if isinstance(payload, dict) else {}


def collect_gemini_conversation_links(
    page,
    config: CrawlConfig,
    should_stop,
    state: TaskState | None = None,
) -> list[GeminiConversationLink]:
    """Collect recent and lazy-loaded Gemini history links from the sidebar."""
    rpc_responses = _attach_gemini_history_rpc_capture(page)
    goto_with_retry(page, GEMINI_HOME_URL, attempts=2, timeout_ms=60_000)
    _prepare_gemini_page_for_rendering(page)
    if not wait_for_gemini_bot_check_clear(page, state, should_stop, "collecting"):
        return []
    _wait_for_gemini_ready(page)
    _open_gemini_sidebar(page)
    page.wait_for_timeout(GEMINI_RENDER_SETTLE_MILLISECONDS)

    rpc_links = _collect_gemini_rpc_conversation_links(
        page,
        rpc_responses,
        config,
        should_stop,
        state,
    )
    if rpc_links:
        return rpc_links

    max_conversations = max(1, int(config.gemini_max_conversations))
    max_rounds = max(1, int(config.max_scroll_rounds))
    # A virtualized list can dispatch its scroll handler before the next batch
    # is painted. Keep a small asynchronous grace window even when the setting
    # is configured aggressively low.
    stale_limit = max(3, int(config.gemini_stale_round_limit))
    pause_ms = max(100, int(float(config.gemini_scroll_pause_seconds) * 1_000))
    collected: dict[str, GeminiConversationLink] = {}
    stale_rounds = 0
    for _round_index in range(max_rounds):
        if should_stop():
            break
        if not wait_for_gemini_bot_check_clear(page, state, should_stop, "collecting"):
            break
        visible_links = _read_gemini_conversation_links(page)
        previous_count = len(collected)
        for link in visible_links:
            collected.setdefault(link.conversation_id, link)
            if len(collected) >= max_conversations:
                break
        if len(collected) >= max_conversations:
            break
        stale_rounds = stale_rounds + 1 if len(collected) == previous_count else 0
        scroll_state = _scroll_gemini_conversation_navigation(page)
        if (
            stale_rounds >= stale_limit
            or (
                not scroll_state.get("moved")
                and not scroll_state.get("eventDispatched")
            )
        ):
            break
        page.wait_for_timeout(pause_ms)
        if not wait_for_gemini_bot_check_clear(page, state, should_stop, "collecting"):
            break
    return list(collected.values())[:max_conversations]


def extract_gemini_conversation_messages(
    page,
    conversation: GeminiConversationLink,
    turn_timestamps: dict[int, str] | None = None,
) -> list[dict[str, Any]]:
    """Extract ordered user and assistant messages from one rendered session."""
    payload = page.evaluate(
        r"""({ fallbackTitle, turnTimestamps }) => {
            const normalizeText = (value) => String(value || '')
                .replace(/\u200b/g, '')
                .replace(/\r\n?/g, '\n')
                .replace(/[ \t]+\n/g, '\n')
                .replace(/\n{3,}/g, '\n\n')
                .trim();
            let containers = [...document.querySelectorAll('user-query, model-response')];
            if (!containers.length) {
                containers = [...document.querySelectorAll('h1, h2, h3')]
                    .filter((heading) => /^(You said|Gemini said)$/i.test(normalizeText(heading.textContent)))
                    .map((heading) => heading.closest('article, section, [role="article"]') || heading.parentElement)
                    .filter(Boolean);
            }
            containers = containers.filter((container, index) => containers.indexOf(container) === index);
            const documentTitle = normalizeText(document.title).replace(/\s*-\s*Google Gemini\s*$/i, '');
            const conversationTitle = documentTitle && !/^Google Gemini$/i.test(documentTitle)
                ? documentTitle
                : fallbackTitle;
            const modeButton = [...document.querySelectorAll('button')].find((button) => {
                const label = button.getAttribute('aria-label') || '';
                return /mode picker/i.test(label);
            });
            const modelLabel = normalizeText(
                modeButton ? (modeButton.getAttribute('aria-label') || modeButton.textContent) : ''
            ).replace(/^Open mode picker,?\s*(?:currently\s*)?/i, '');
            let turnIndex = -1;
            const messages = [];
            containers.forEach((container) => {
                const tagName = container.tagName.toLowerCase();
                const fullText = normalizeText(container.innerText || container.textContent);
                const role = tagName === 'user-query' || /^You said\b/i.test(fullText) ? 'user' : 'assistant';
                if (role === 'user') turnIndex += 1;
                if (turnIndex < 0) turnIndex = 0;
                const selectors = role === 'user'
                    ? ['.query-text', '[data-test-id="user-query-content"]', '.user-query-bubble-with-background']
                    : ['message-content', '.model-response-text', '[data-test-id="model-response"]', '.response-content'];
                const contentRoot = selectors.map((selector) => container.querySelector(selector)).find(Boolean)
                    || container;
                let contentText = normalizeText(contentRoot.innerText || contentRoot.textContent);
                contentText = contentText.replace(role === 'user' ? /^You said\s*/i : /^Gemini said\s*/i, '');
                if (role === 'user') {
                    contentText = contentText.replace(/\n(?:Expand\n)?Copy prompt\s*$/i, '').trim();
                } else {
                    contentText = contentText.split(/\n(?:Good response|Bad response)(?:\n|$)/i, 1)[0].trim();
                    contentText = contentText.replace(/\nGemini is AI and can make mistakes\.?\s*$/i, '').trim();
                }
                if (!contentText) return;
                const sourceLinks = [...contentRoot.querySelectorAll('a[href], img[src]')].map((element) => {
                    const rawValue = element.href || element.currentSrc || element.src || '';
                    try {
                        return new URL(rawValue, location.href).href;
                    } catch (_error) {
                        return '';
                    }
                }).filter(Boolean);
                messages.push({
                    conversation_title: conversationTitle,
                    turn_index: turnIndex,
                    message_index: messages.length,
                    role,
                    author_label: role === 'user' ? 'You' : 'Gemini',
                    content_text: contentText,
                    content_html: String(contentRoot.innerHTML || '').trim(),
                    source_links: [...new Set(sourceLinks)],
                    model_label: role === 'assistant' ? modelLabel : '',
                    message_timestamp: (turnTimestamps && turnTimestamps[turnIndex]) || '',
                });
            });
            return messages;
        }""",
        {"fallbackTitle": conversation.title, "turnTimestamps": turn_timestamps or {}},
    )
    return [dict(item) for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


@contextlib.contextmanager
def launch_gemini_browser_context(config: CrawlConfig):
    """Open an isolated authenticated Gemini context for Safari, Edge, or Chrome."""
    descriptor = browser_descriptors(config).get(config.gemini_browser)
    if descriptor is None:
        raise RuntimeError(f"Unsupported Gemini browser: {config.gemini_browser}")
    if descriptor.engine == "safari":
        with SafariContext(GEMINI_HOME_URL) as context:
            yield context, descriptor
        return
    with sync_playwright_or_error() as playwright:
        headless = descriptor.browser_id == "edge"
        with launch_chromium_context(
            playwright,
            descriptor,
            headless=headless,
            clone_profile_first=True,
            background_window=not headless,
        ) as context:
            yield context, descriptor


def _gemini_context_page(context):
    """Return the primary browser page for either context implementation."""
    if isinstance(context, SafariContext):
        return context.primary_page
    return context.pages[0] if context.pages else context.new_page()


def sync_gemini_history(
    state: TaskState,
    config: CrawlConfig,
    should_stop,
    local_store_root: Path | str = LOCAL_STORE_ROOT,
    *,
    skip_cached_conversations: bool = False,
) -> GeminiSyncResult:
    """Cache rendered Gemini sessions into a local Parquet file."""
    history_path = gemini_history_path(local_store_root)
    store = GeminiHistoryStore(history_path)
    state.update(
        phase="collecting",
        progress_unit="sessions",
        output_dir=str(history_path.parent),
        account_name="Gemini",
        downloaded_posts=store.cached_conversations,
        downloaded_tweets=store.cached_messages,
    )
    state.append_event("Opening the authenticated Gemini history in the selected browser.")

    discovered_messages = 0
    new_messages = 0
    unchanged_conversations = 0
    failed_conversations = 0
    processed_conversations = 0
    conversation_links = (
        load_gemini_discovery_checkpoint(local_store_root)
        if skip_cached_conversations
        else []
    )
    with launch_gemini_browser_context(config) as (context, descriptor):
        page = _gemini_context_page(context)
        conversation_rpc_responses = _attach_gemini_conversation_rpc_capture(page)
        if conversation_links:
            state.append_event(
                f"Resume mode loaded {len(conversation_links):,} Gemini sessions from the "
                "recent discovery checkpoint."
            )
        else:
            conversation_links = collect_gemini_conversation_links(
                page,
                config,
                should_stop,
                state,
            )
            if not conversation_links:
                raise RuntimeError(
                    "Gemini history discovery returned no sessions; the previous checkpoint was preserved."
                )
            save_gemini_discovery_checkpoint(conversation_links, local_store_root)
        state.update(
            phase="downloading",
            discovered_tweets=len(conversation_links),
            queued_tweets=len(conversation_links),
            discovery_complete=True,
        )
        state.append_event(
            f"Discovered {len(conversation_links):,} Gemini sessions in {descriptor.label}."
        )
        processing_links = conversation_links
        if skip_cached_conversations:
            cached_ids = store.cached_conversation_ids
            processing_links = [
                conversation
                for conversation in conversation_links
                if conversation.conversation_id not in cached_ids
            ]
            state.append_event(
                f"Resume mode skipped {len(conversation_links) - len(processing_links):,} "
                f"already cached Gemini sessions; {len(processing_links):,} remain."
            )
            state.update(queued_tweets=len(processing_links))
        for index, conversation in enumerate(processing_links, start=1):
            if should_stop():
                break
            last_error: Exception | None = None
            for attempt_index in range(GEMINI_CONVERSATION_RETRY_LIMIT):
                try:
                    goto_with_retry(page, conversation.url, attempts=2, timeout_ms=60_000)
                    _prepare_gemini_page_for_rendering(page)
                    if not wait_for_gemini_bot_check_clear(page, state, should_stop, "downloading"):
                        break
                    _wait_for_gemini_ready(page)
                    page.wait_for_timeout(max(250, int(config.gemini_scroll_pause_seconds * 1_000)))
                    turn_timestamps = _gemini_message_timestamps_from_captured_responses(
                        conversation_rpc_responses,
                        conversation.conversation_id,
                    )
                    if not turn_timestamps:
                        safari_payloads = _fetch_gemini_conversation_rpc_payloads(page, conversation)
                        turn_timestamps = _gemini_message_timestamps_from_rpc_payloads(
                            safari_payloads,
                            conversation.conversation_id,
                        )
                    messages = extract_gemini_conversation_messages(
                        page,
                        conversation,
                        turn_timestamps,
                    )
                    captured_at = utc_now()
                    added_count, unchanged = store.replace_conversation(
                        conversation,
                        messages,
                        captured_at,
                    )
                    store.save()
                    discovered_messages += len(messages)
                    new_messages += added_count
                    unchanged_conversations += int(unchanged)
                    state.append_event(
                        f"Cached Gemini session {index:,}/{len(processing_links):,}: "
                        f"{conversation.title} ({len(messages):,} messages)."
                    )
                    last_error = None
                    break
                except GeminiNoCacheableMessagesError:
                    state.append_event(
                        f"Skipped Gemini session {index:,}/{len(processing_links):,}: "
                        "no cacheable text messages."
                    )
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt_index + 1 < GEMINI_CONVERSATION_RETRY_LIMIT:
                        error_text = str(exc)
                        transient_navigation = any(
                            marker in error_text for marker in GEMINI_TRANSIENT_NAVIGATION_MARKERS
                        )
                        retry_delay_ms = (
                            GEMINI_TRANSIENT_NAVIGATION_RETRY_BASE_MILLISECONDS
                            * (attempt_index + 1)
                            if transient_navigation
                            else GEMINI_RENDER_SETTLE_MILLISECONDS
                        )
                        state.append_event(
                            f"Retrying Gemini session {index:,}/{len(processing_links):,} "
                            f"after attempt {attempt_index + 1:,}: "
                            f"{str(exc).splitlines()[0][:300]}"
                        )
                        page.wait_for_timeout(retry_delay_ms)
            if last_error is not None:
                failed_conversations += 1
                state.append_event(
                    f"Failed Gemini session {index:,}/{len(processing_links):,}: "
                    f"{str(last_error).splitlines()[0][:300]}"
                )
                if any(
                    marker in str(last_error) for marker in GEMINI_TRANSIENT_NAVIGATION_MARKERS
                ):
                    page.wait_for_timeout(GEMINI_TRANSIENT_NAVIGATION_COOLDOWN_MILLISECONDS)
            processed_conversations = index
            state.update(
                processed_tweets=processed_conversations,
                discovered_images=discovered_messages,
                downloaded_posts=store.cached_conversations,
                downloaded_tweets=store.cached_messages,
                skipped_tweets=unchanged_conversations,
                failed_tweets=failed_conversations,
            )

    stopped = should_stop()
    return GeminiSyncResult(
        discovered_conversations=len(conversation_links),
        processed_conversations=processed_conversations,
        discovered_messages=discovered_messages,
        new_messages=new_messages,
        unchanged_conversations=unchanged_conversations,
        failed_conversations=failed_conversations,
        cached_conversations=store.cached_conversations,
        cached_messages=store.cached_messages,
        stopped=stopped,
    )
