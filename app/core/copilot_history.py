"""Read Microsoft Copilot's authenticated, rendered conversation history.

Code version: v1.3.0-codex.0
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from time import monotonic
from typing import Any, Callable
from urllib.parse import urlsplit

from .browser_sessions import (
    browser_descriptors,
    goto_with_retry,
    launch_chromium_context,
    sync_playwright_or_error,
)
from .cache_timing import wait_for_cache_scan
from .config import LOCAL_STORE_ROOT, CrawlConfig, normalize_copilot_entry_url
from .copilot_history_store import CopilotHistoryStore
from .copilot_legacy_catalog import read_copilot_legacy_catalog
from .copilot_sync_state import read_copilot_sync_state, write_copilot_sync_state
from .safari_automation import SafariContext
from .state import TaskState, utc_now


COPILOT_HOME_URL = "https://copilot.com/chat"
COPILOT_HISTORY_URL = "https://copilot.com/chat/all"
COPILOT_HOSTS = frozenset({"copilot.com", "www.copilot.com"})
COPILOT_READY_TIMEOUT_SECONDS = 45.0
COPILOT_SETTLE_MS = 750
COPILOT_SCAN_LIMIT = 1_000
COPILOT_STABLE_ROUNDS = 3


class CopilotAccessError(RuntimeError):
    """Stop collection when the authenticated page is unavailable."""


class CopilotDiscoveryIncompleteError(RuntimeError):
    """Retain a partial catalog without claiming it represents the entire account."""

    def __init__(self, message: str, conversations: list[dict[str, str]]) -> None:
        super().__init__(message)
        self.conversations = conversations


@dataclass(frozen=True, slots=True)
class CopilotDiscovery:
    """Carry catalog coverage evidence separately from its discovered sessions."""

    conversations: list[dict[str, str]]
    discovery_complete: bool
    expected_sessions: int | None = None
    completion_basis: str = ""


def traverse_copilot_catalog(
    initial_page: dict[str, Any],
    fetch_next_page: Callable[[str], dict[str, Any]],
    should_stop: Callable[[], bool],
    on_progress: Callable[[int], None] | None = None,
) -> CopilotDiscovery:
    """Follow every provider cursor; only an explicit empty cursor proves exhaustion."""

    found: dict[str, dict[str, str]] = {}
    cursors: set[str] = set()
    payload = initial_page
    no_growth = 0

    def incomplete(reason: str) -> CopilotDiscoveryIncompleteError:
        return CopilotDiscoveryIncompleteError(
            f"Microsoft Copilot history discovery is incomplete. {reason} "
            "Existing cached history was preserved.", list(found.values()),
        )

    for page_index in range(COPILOT_SCAN_LIMIT):
        if should_stop():
            return CopilotDiscovery(list(found.values()), False)
        if not isinstance(payload, dict) or not isinstance(payload.get("chats"), list):
            raise incomplete("The provider returned an unsupported catalog response.")
        if not isinstance(payload.get("syncState"), str):
            raise incomplete("The provider did not expose a valid pagination cursor.")
        previous_count = len(found)
        for chat in payload["chats"]:
            if not isinstance(chat, dict):
                raise incomplete("The provider returned a malformed catalog entry.")
            identity = chat.get("conversationId")
            if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity):
                raise incomplete("A catalog entry has no stable session identity.")
            url = normalize_copilot_conversation_url(
                f"https://copilot.com/chat/conversation/{identity}"
            )
            if not url:
                raise incomplete("A catalog entry has an invalid session identity.")
            found[identity] = {
                "conversation_id": identity, "conversation_url": url,
                "conversation_title": str(chat.get("chatName") or "").strip()
                or f"Copilot session {identity}",
            }
        if on_progress is not None:
            on_progress(len(found))
        cursor = payload["syncState"]
        if should_stop():
            return CopilotDiscovery(list(found.values()), False)
        if not cursor:
            return CopilotDiscovery(
                list(found.values()), True,
                completion_basis=(
                    "Microsoft Copilot GetConversationPageHistoryList pagination returned "
                    f"an empty syncState after {page_index + 1:,} pages."
                ),
            )
        if cursor in cursors:
            raise incomplete("The provider repeated a pagination cursor.")
        cursors.add(cursor)
        no_growth = no_growth + 1 if len(found) == previous_count else 0
        if no_growth >= 2:
            raise incomplete("Two continuation pages added no sessions; pagination stalled.")
        if page_index + 1 >= COPILOT_SCAN_LIMIT:
            break
        try:
            payload = fetch_next_page(cursor)
        except Exception as exc:
            raise incomplete("A continuation page could not be read.") from exc
    raise incomplete("The bounded pagination limit was reached before exhaustion.")


def normalize_copilot_conversation_url(value: str) -> str:
    """Keep only observed first-party conversation paths, without tracking queries."""
    try:
        parsed = urlsplit(str(value or "").strip())
        if (
            parsed.scheme != "https"
            or parsed.hostname not in COPILOT_HOSTS
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
        ):
            return ""
    except ValueError:
        return ""
    path = parsed.path.rstrip("/")
    if not re.fullmatch(r"/chat/conversation/[A-Za-z0-9_-]{1,128}", path):
        return ""
    return f"https://copilot.com{path}"


COPILOT_STATE_SCRIPT = r"""() => {
    /* Read account state outside the conversation's untrusted text. */
    const links = [...document.querySelectorAll('a[href*="/chat/conversation/"]')]
        .filter(e => !e.closest('[data-testid="MessageListContainer"]'));
    const controls = [...document.querySelectorAll('button, a')].filter(e =>
        !e.closest('[data-testid="MessageListContainer"], a[href*="/chat/conversation/"]'));
    const signIn = controls.some(e => /^(sign in|log in)$/i.test(
        (e.getAttribute('aria-label') || e.innerText || '').trim()));
    const account = document.querySelector('[data-testid="nav-footer"] img[alt]');
    const heading = [...document.querySelectorAll('h1, h2, [role="alert"]')]
        .filter(e => !e.closest('[data-testid="MessageListContainer"]'))
        .map(e => e.innerText || '').join(' ');
    const challenge = !account && links.length === 0
        && /verify (you are|you're) human|unusual activity|access denied|captcha/i
            .test(document.title + ' ' + heading);
    return {
        url: location.href,
        signed_in: !signIn && (Boolean(account) || links.length > 0),
        signed_out: signIn,
        challenge,
        loading: Boolean(document.querySelector('main [role="progressbar"], main [aria-busy="true"]')),
        conversation_count: links.length,
    };
}"""


def inspect_copilot_page(page: Any) -> dict[str, Any]:
    """Read a small semantic readiness snapshot without credentials or account names."""
    current_url = str(page.url or "")
    try:
        parsed = urlsplit(current_url)
    except ValueError as exc:
        raise CopilotAccessError("Copilot returned an invalid page URL.") from exc
    if parsed.hostname not in COPILOT_HOSTS:
        raise CopilotAccessError("The selected browser needs Microsoft Copilot sign-in.")
    state = page.evaluate(COPILOT_STATE_SCRIPT)
    if not isinstance(state, dict):
        raise CopilotAccessError("Microsoft Copilot account state could not be read.")
    if state.get("challenge"):
        raise CopilotAccessError("Microsoft Copilot requires a human verification check in the browser.")
    if state.get("signed_out"):
        raise CopilotAccessError("The selected browser is not signed in to Microsoft Copilot.")
    return state


def wait_for_copilot_ready(
    page: Any,
    should_stop: Callable[[], bool] = lambda: False,
    *,
    timeout_seconds: float = COPILOT_READY_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Wait for authenticated navigation while honoring cooperative cancellation."""
    deadline = monotonic() + timeout_seconds
    while not should_stop():
        if str(page.url or "") in {"", "about:blank", "favorites://"}:
            if monotonic() >= deadline:
                raise CopilotAccessError("Microsoft Copilot did not finish opening its chat page.")
            page.wait_for_timeout(COPILOT_SETTLE_MS)
            continue
        state = inspect_copilot_page(page)
        if state.get("signed_in") and not state.get("loading"):
            return state
        if monotonic() >= deadline:
            raise CopilotAccessError("Microsoft Copilot did not expose authenticated chat history.")
        page.wait_for_timeout(COPILOT_SETTLE_MS)
    return {}


@contextmanager
def open_copilot_history_page(
    descriptor: Any, *, silent: bool = False, entry_url: str = COPILOT_HOME_URL,
):
    """Use the existing browser transport and close only the owned task context."""
    entry_url = normalize_copilot_entry_url(entry_url)
    if descriptor.engine == "safari":
        with SafariContext(entry_url, lock_blocking=False) as context:
            yield context.primary_page
        return
    if descriptor.engine != "chromium":
        raise RuntimeError(f"Microsoft Copilot does not support {descriptor.label}.")
    with sync_playwright_or_error() as playwright:
        with launch_chromium_context(
            playwright,
            descriptor,
            headless=False,
            clone_profile_first=True,
            background_window=True,
        ) as context:
            page = context.pages[0] if context.pages else context.new_page()
            goto_with_retry(page, entry_url, attempts=1, timeout_ms=90_000)
            yield page


def probe_copilot_session(
    descriptor: Any,
    config: CrawlConfig,
    *,
    silent: bool = False,
    prefer_initialized_debug_profile: bool = False,
) -> dict[str, Any]:
    """Check the signed-in history surface without sending a prompt."""
    with open_copilot_history_page(descriptor, silent=silent, entry_url=config.copilot_entry_url) as page:
        wait_for_copilot_ready(page)
    return {
        "logged_in": True,
        "can_download": True,
        "account_name": "Microsoft Copilot",
        "message": f"{descriptor.label} is ready to cache Microsoft Copilot history.",
    }


COPILOT_LINKS_SCRIPT = r"""() => {
    /* Read the actual all-chats loader; sidebar anchors are not a catalog. */
    if (location.pathname.replace(/\/+$/, '') !== '/chat/all') return [];
    const root = document.querySelector('#llm-web-ui-messageList-scrollable-container');
    if (!root?.querySelector('[data-testid="chat-history-search-input"]')
        || !root.querySelector('[role="grid"]')) return [];
    const catalog = window.__staticRouterHydrationData?.loaderData
        ?.['all-chat-history']?.store?.chatLandingPageHistoryList;
    return Array.isArray(catalog?.chats) ? catalog.chats.map(chat => ({
        url: typeof chat.conversationId === 'string'
            ? 'https://copilot.com/chat/conversation/' + chat.conversationId : '',
        title: typeof chat.chatName === 'string' ? chat.chatName : '',
    })) : [];
}"""

COPILOT_CATALOG_SCRIPT = r"""() => {
    /* The complete history loader is distinct from the recent-chat sidebar. */
    if (location.pathname.replace(/\/+$/, '') !== '/chat/all') return {ready: false};
    const root = document.querySelector('#llm-web-ui-messageList-scrollable-container');
    const loader = window.__staticRouterHydrationData?.loaderData;
    const catalog = loader?.['all-chat-history']?.store?.chatLandingPageHistoryList;
    const fields = ['eligibility', 'hostContext', 'sessionID'];
    const requestContext = {};
    const isObject = value => Boolean(value) && typeof value === 'object' && !Array.isArray(value);
    const copySelected = patch => {
        if (!isObject(patch)) return;
        for (const field of fields) {
            if (Object.prototype.hasOwnProperty.call(patch, field) && patch[field] !== undefined) {
                requestContext[field] = patch[field];
            }
        }
    };
    /* Match route-store hydration, then the latest successful mirrored query per field. */
    for (const route of Object.values(loader || {})) copySelected(route?.store);
    const queryState = (loader?.root ?? loader?.hwav2)?.__queryState;
    const latest = new Map();
    for (const query of Array.isArray(queryState?.queries) ? queryState.queries : []) {
        if (query?.state?.status !== 'success'
            || query?.meta?.storeMirror?.type !== 'storePatch'
            || !isObject(query.state.data) || !Number.isFinite(query.state.dataUpdatedAt)) continue;
        for (const field of fields) {
            if (!Object.prototype.hasOwnProperty.call(query.state.data, field)
                || query.state.data[field] === undefined) continue;
            const previous = latest.get(field);
            if (!previous || query.state.dataUpdatedAt >= previous.updatedAt) {
                latest.set(field, {value: query.state.data[field], updatedAt: query.state.dataUpdatedAt});
            }
        }
    }
    for (const [field, update] of latest) requestContext[field] = update.value;
    const credentialKeys = new Set(['authorization', 'cookie', 'access_token', 'accesstoken',
        'refresh_token', 'refreshtoken', 'id_token', 'idtoken']);
    const containsCredentials = value => value && typeof value === 'object'
        && Object.entries(value).some(([key, item]) => credentialKeys.has(key.toLowerCase())
            || containsCredentials(item));
    if (containsCredentials(requestContext)) {
        throw new Error('Microsoft Copilot returned credentials in its history request context.');
    }
    return {
        ready: Boolean(root?.querySelector('[data-testid="chat-history-search-input"]')
            && root.querySelector('[role="grid"]') && Array.isArray(catalog?.chats)),
        catalog: catalog || null,
        session_id: typeof requestContext.sessionID === 'string' ? requestContext.sessionID : '',
        request_context: requestContext,
    };
}"""


def _copilot_context_header(value: dict[str, Any], *, ascii_only: bool) -> str:
    """Serialize selected provider metadata without forwarding credential fields."""

    credential_keys = {
        "authorization", "cookie", "access_token", "accesstoken",
        "refresh_token", "refreshtoken", "id_token", "idtoken",
    }

    def contains_credentials(item: Any) -> bool:
        if isinstance(item, dict):
            return any(
                str(key).lower() in credential_keys or contains_credentials(nested)
                for key, nested in item.items()
            )
        return isinstance(item, list) and any(contains_credentials(nested) for nested in item)

    if not isinstance(value, dict) or contains_credentials(value):
        raise RuntimeError("Microsoft Copilot returned an unsupported history request context.")
    try:
        return json.dumps(value, ensure_ascii=ascii_only, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Microsoft Copilot returned an unsupported history request context.") from exc


def fetch_copilot_catalog_page(
    page: Any,
    previous: dict[str, Any],
    cursor: str,
    session_id: str,
    request_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Use the first-party history read action with its existing browser cookies.

    Microsoft's all-chats frontend submits this action to /chat and consumes the
    resulting store. It is a history read, never a prompt, deletion, or rename.
    """

    parsed = urlsplit(str(page.url or ""))
    if parsed.scheme != "https" or parsed.hostname not in COPILOT_HOSTS or parsed.path != "/chat/all":
        raise CopilotAccessError("Copilot catalog requests require the authenticated all-chats page.")
    url = f"https://{parsed.netloc}/chat" + (f"?{parsed.query}" if parsed.query else "")
    headers = {"Accept": "application/json", "Content-Type": "application/json", "X-Route-Id": "chat"}
    if request_context is not None:
        if not isinstance(request_context, dict):
            raise RuntimeError("Microsoft Copilot returned an unsupported history request context.")
        if "sessionID" in request_context:
            session_id = request_context["sessionID"]
            if session_id is not None and not isinstance(session_id, str):
                raise RuntimeError("Microsoft Copilot returned an unsupported history request context.")
        if request_context.get("eligibility") is not None:
            headers["X-Client-Eligibility"] = _copilot_context_header(
                request_context["eligibility"], ascii_only=False,
            )
        if request_context.get("hostContext") is not None:
            headers["x-host-context"] = _copilot_context_header(
                request_context["hostContext"], ascii_only=True,
            )
    if session_id:
        headers["X-Session-Id"] = session_id
    body = json.dumps({
        "action": "GetConversationPageHistoryList", "syncState": cursor,
        "enableLastMessage": True, "state": {"chatLandingPageHistoryList": previous},
        "saveToStore": "chatLandingPageHistoryList",
    }, ensure_ascii=False)
    request = page.context.request
    if callable(getattr(request, "request_from_page", None)):
        response = request.request_from_page(
            page, url, 45_000, headers, method="POST", body=body,
        )
    else:
        response = request.post(
            url, headers={**headers, "Origin": f"https://{parsed.netloc}"},
            data=body, timeout=45_000, max_redirects=0,
        )
    if response.status != 200:
        raise RuntimeError(f"Microsoft Copilot history read returned HTTP {response.status}.")
    try:
        payload = json.loads(response.text())
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Microsoft Copilot returned an invalid history-action response.") from exc
    if isinstance(payload, dict) and payload.get("redirectTo"):
        raise RuntimeError("Microsoft Copilot redirected its history action; cached history was preserved.")
    store = payload.get("store") if isinstance(payload, dict) else None
    catalog = store.get("chatLandingPageHistoryList") if isinstance(store, dict) else None
    if not isinstance(catalog, dict) or payload.get("error"):
        raise RuntimeError("Microsoft Copilot returned an unsupported history-action response.")
    return catalog

COPILOT_SCROLL_SCRIPT = r"""({kind, top}) => {
    /* Find the rendered scroll owner from its content. */
    const main = document.querySelector('main') || document.body;
    const anchor = kind === 'history'
        ? main.querySelector('#llm-web-ui-messageList-scrollable-container')
        : main.querySelector('[data-testid="MessageListContainer"]');
    let target = anchor;
    while (target && target !== document.body) {
        const style = getComputedStyle(target);
        if (target.scrollHeight > target.clientHeight + 2
            && /auto|scroll/.test(style.overflowY)) break;
        target = target.parentElement;
    }
    target = target && target !== document.body ? target : document.scrollingElement;
    const before = target.scrollTop;
    target.scrollTop = top ? 0 : Math.min(target.scrollHeight,
        before + Math.max(400, target.clientHeight * 0.8));
    target.dispatchEvent(new Event('scroll', {bubbles: true}));
    const atEnd = target.scrollTop + target.clientHeight >= target.scrollHeight - 3;
    return {moved: Math.abs(target.scrollTop - before) > 1,
        at_end: atEnd, height: target.scrollHeight, position: target.scrollTop};
}"""


def discover_copilot_conversations(
    page: Any,
    should_stop: Callable[[], bool],
    on_progress: Callable[[int], None] | None = None,
) -> CopilotDiscovery:
    """Traverse every first-party history continuation before claiming completeness."""
    entry = urlsplit(str(page.url or ""))
    history_url = COPILOT_HISTORY_URL + (f"?{entry.query}" if entry.query else "")
    goto_with_retry(page, history_url, attempts=1, timeout_ms=90_000)
    wait_for_copilot_ready(page, should_stop)
    deadline = monotonic() + COPILOT_READY_TIMEOUT_SECONDS
    while True:
        if should_stop():
            return CopilotDiscovery([], False)
        inspect_copilot_page(page)
        snapshot = page.evaluate(COPILOT_CATALOG_SCRIPT)
        if isinstance(snapshot, dict) and snapshot.get("ready"):
            break
        if monotonic() >= deadline:
            raise CopilotDiscoveryIncompleteError(
                "Microsoft Copilot history discovery is incomplete. The all-chats catalog "
                "did not load; sidebar sessions cannot establish full history coverage.", [],
            )
        page.wait_for_timeout(COPILOT_SETTLE_MS)
    current = snapshot["catalog"]

    def fetch_next(cursor: str) -> dict[str, Any]:
        nonlocal current
        inspect_copilot_page(page)
        current = fetch_copilot_catalog_page(
            page, current, cursor, str(snapshot.get("session_id") or ""),
            request_context=snapshot.get("request_context"),
        )
        return current

    return traverse_copilot_catalog(current, fetch_next, should_stop, on_progress)


COPILOT_MESSAGES_SCRIPT = r"""() => {
    /* These selectors were observed on the signed-in copilot.com chat UI. */
    const root = document.querySelector('[data-testid="MessageListContainer"]');
    if (!root) return {messages: [], loading: true};
    const clean = e => {
        const clone = e.cloneNode(true);
        clone.querySelectorAll('button, script, style, svg, [aria-hidden="true"]')
            .forEach(x => x.remove());
        return clone;
    };
    const normalize = text => String(text || '').replace(/\r\n?/g, '\n')
        .replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
    const textOf = node => {
        if (node.nodeType === Node.TEXT_NODE) return node.textContent || '';
        if (node.nodeType !== Node.ELEMENT_NODE) return '';
        if (node.tagName === 'BR') return '\n';
        const text = [...node.childNodes].map(textOf).join('');
        if (/^(P|DIV|LI|PRE|TR|H[1-6]|BLOCKQUOTE)$/.test(node.tagName)) return text + '\n';
        if (/^(TD|TH)$/.test(node.tagName)) return text + '\t';
        return text;
    };
    const messages = [];
    const blocks = root.querySelectorAll('[data-testid="m365-chat-llm-web-ui-chat-message"]');
    for (const block of blocks) {
        const response = block.querySelector('[data-testid="copilot-message-div"]');
        const providerId = (response?.id || '').replace(/^chatMessageResponse-/, '');
        const question = block.querySelector('[data-testid="chatQuestion"] [data-testid="chatOutput"]');
        const answer = block.querySelector('[data-testid="markdown-reply"]');
        for (const [role, element] of [['user', question], ['assistant', answer]]) {
            if (!element) continue;
            const clone = clean(element);
            const text = normalize(textOf(clone));
            if (!text) continue;
            messages.push({role, message_id: providerId ? providerId + ':' + role : '',
                content_text: text, content_html: clone.innerHTML,
                source_links: [...new Set([...clone.querySelectorAll('a[href]')]
                    .map(a => a.href).filter(href => /^https?:\/\//i.test(href)))],
                message_timestamp: block.querySelector('time[datetime]')?.getAttribute('datetime') || '',
                model_label: '',
            });
        }
    }
    return {messages, loading: Boolean(root.querySelector('[role="progressbar"], [aria-busy="true"]')),
        generating: Boolean(document.querySelector('button[aria-label="Stop generating"]'))};
}"""


def collect_copilot_messages(
    page: Any,
    conversation: dict[str, str],
    should_stop: Callable[[], bool],
) -> list[dict[str, Any]]:
    """Collect an ordered transcript only after the matching session settles."""
    expected_url = conversation["conversation_url"]
    entry_query = urlsplit(str(page.url or "")).query
    navigation_url = expected_url + (f"?{entry_query}" if entry_query else "")
    goto_with_retry(page, navigation_url, attempts=1, timeout_ms=90_000)
    wait_for_copilot_ready(page, should_stop)
    if should_stop():
        return []
    if normalize_copilot_conversation_url(page.url) != expected_url:
        raise RuntimeError("Microsoft Copilot opened a different session; cached history was preserved.")
    page.evaluate(COPILOT_SCROLL_SCRIPT, {"kind": "transcript", "top": True})
    page.wait_for_timeout(COPILOT_SETTLE_MS * COPILOT_STABLE_ROUNDS)
    collected: dict[str, dict[str, Any]] = {}
    following: dict[str, set[str]] = {}
    stable = 0
    previous_signature = None
    for _ in range(COPILOT_SCAN_LIMIT):
        if should_stop():
            return []
        inspect_copilot_page(page)
        if normalize_copilot_conversation_url(page.url) != expected_url:
            raise RuntimeError("Microsoft Copilot changed sessions during collection.")
        payload = page.evaluate(COPILOT_MESSAGES_SCRIPT)
        if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
            raise RuntimeError("Microsoft Copilot transcript could not be read.")
        if payload.get("generating"):
            raise RuntimeError("Microsoft Copilot is still generating; the existing cached session was preserved.")
        visible_keys = []
        missing_identity = False
        for position, message in enumerate(payload["messages"]):
            if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
                raise RuntimeError("Microsoft Copilot returned an unsupported message structure.")
            identity = str(message.get("message_id") or f"position:{position}:{message['role']}")
            missing_identity = missing_identity or not message.get("message_id")
            if not str(message.get("content_text") or "").strip():
                raise RuntimeError("Microsoft Copilot returned an empty transcript message.")
            prior = collected.get(identity)
            if not message.get("message_id") and prior is not None and (
                prior["content_text"] != message["content_text"]
                or prior["role"] != message["role"]
            ):
                raise RuntimeError("Microsoft Copilot changed a transcript without stable message identities.")
            collected[identity] = dict(message)
            following.setdefault(identity, set())
            visible_keys.append(identity)
        if len(set(visible_keys)) != len(visible_keys):
            raise RuntimeError("Microsoft Copilot returned duplicate message identities.")
        for before, after in zip(visible_keys, visible_keys[1:]):
            following[before].add(after)
        scroll = page.evaluate(COPILOT_SCROLL_SCRIPT, {"kind": "transcript", "top": False})
        if not isinstance(scroll, dict):
            raise RuntimeError("Microsoft Copilot transcript scroll state could not be read.")
        if missing_identity and (scroll.get("moved") or not scroll.get("at_end")):
            raise RuntimeError("Microsoft Copilot did not expose stable identities for a scrolling transcript.")
        signature = (tuple((key, row["content_text"]) for key, row in collected.items()), scroll.get("height"))
        stable = stable + 1 if (
            signature == previous_signature and scroll.get("at_end") and not payload.get("loading")
        ) else 0
        if stable >= COPILOT_STABLE_ROUNDS:
            if not collected:
                raise RuntimeError("Microsoft Copilot exposed no cacheable text in this session.")
            turn = 0
            messages = []
            preceding = {key: set() for key in collected}
            for before, after_keys in following.items():
                for after in after_keys:
                    preceding[after].add(before)
            try:
                ordered_keys = list(TopologicalSorter(preceding).static_order())
            except CycleError as exc:
                raise RuntimeError("Microsoft Copilot transcript order changed during collection.") from exc
            for position, key in enumerate(ordered_keys):
                message = collected[key]
                turn += int(message["role"] == "user")
                messages.append({**message, "message_index": position, "turn_index": turn})
            return messages
        previous_signature = signature
        page.wait_for_timeout(COPILOT_SETTLE_MS)
    raise RuntimeError("Microsoft Copilot transcript is incomplete; the scan limit was reached.")


def sync_copilot_history(
    state: TaskState,
    config: CrawlConfig,
    should_stop: Callable[[], bool],
    local_store_root=LOCAL_STORE_ROOT,
) -> dict[str, Any]:
    """Cache complete rendered sessions with the shared browser lease and task state."""
    descriptor = browser_descriptors(config).get(config.copilot_browser)
    if descriptor is None:
        raise RuntimeError(f"Unsupported Microsoft Copilot browser: {config.copilot_browser}")
    store = CopilotHistoryStore(local_store_root)
    legacy_catalog = read_copilot_legacy_catalog(local_store_root)
    legacy_present = legacy_catalog["status"] != "missing"
    legacy_count = legacy_catalog["sessions"] or 0
    legacy_ids = set(legacy_catalog["conversation_ids"])
    legacy_reason = (
        f"The preserved Edge legacy directory contains {legacy_count:,} sessions. "
        "Full legacy transcript coverage remains unverified."
        if legacy_catalog["status"] == "valid"
        else "The preserved Edge legacy directory cannot be verified. Full history coverage remains unverified."
    ) if legacy_present else ""
    result = dict(
        sessions=0, messages=0, added_or_changed=0, unchanged=0, failed=0, stopped=False,
        discovery_complete=False, sync_complete=False, expected_sessions=None,
        discovered_sessions=legacy_count, completion_basis="", scope_reason=legacy_reason,
    )
    write_copilot_sync_state(local_store_root, {
        "status": "running", "started_at": utc_now(),
        "discovered_sessions": legacy_count,
        "reason": legacy_reason or "Full history completeness has not been verified for this sync.",
    })
    state.update(
        phase="collecting", progress_unit="sessions", account_name="Microsoft Copilot",
        output_dir=str(store.path.parent), downloaded_posts=store.cached_conversations,
        downloaded_tweets=store.cached_messages,
        discovered_images=0, downloaded_images=0,
        discovered_tweets=legacy_count, queued_tweets=legacy_count,
        message="Reading Microsoft Copilot's authenticated all-chats page...",
    )
    if should_stop():
        return {**result, "stopped": True}
    with open_copilot_history_page(descriptor, entry_url=config.copilot_entry_url) as page:
        wait_for_copilot_ready(page, should_stop)

        def catalog_progress(count: int) -> None:
            coverage = read_copilot_sync_state(local_store_root)
            coverage.update(discovered_sessions=max(count, legacy_count))
            write_copilot_sync_state(local_store_root, coverage)
            state.update(
                discovered_tweets=max(count, legacy_count), discovery_complete=False,
                message=f"Discovered {count:,} Microsoft Copilot sessions; following history pagination...",
            )

        try:
            discovery = discover_copilot_conversations(page, should_stop, catalog_progress)
        except CopilotDiscoveryIncompleteError as exc:
            coverage = read_copilot_sync_state(local_store_root)
            coverage.update(
                status="incomplete", discovered_sessions=max(len(exc.conversations), legacy_count),
                discovery_complete=False, sync_complete=False, finished_at=utc_now(),
                reason=str(exc),
            )
            write_copilot_sync_state(local_store_root, coverage)
            state.update(discovered_tweets=max(len(exc.conversations), legacy_count), discovery_complete=False)
            raise
        if should_stop():
            return {**result, "stopped": True}
        if not isinstance(discovery, CopilotDiscovery) or not discovery.discovery_complete:
            raise CopilotDiscoveryIncompleteError(
                "Microsoft Copilot history discovery did not provide verified exhaustion evidence.",
                discovery.conversations if isinstance(discovery, CopilotDiscovery) else [],
            )
        conversations = discovery.conversations
        known_ids = {row["conversation_id"] for row in store.rows} - legacy_ids
        discovered_ids = {row["conversation_id"] for row in conversations}
        if known_ids - discovered_ids:
            raise CopilotDiscoveryIncompleteError(
                "Microsoft Copilot's catalog omitted previously cached sessions; "
                "the existing history was preserved and completeness remains unverified.", conversations,
            )
        result.update(
            discovery_complete=not legacy_present,
            discovered_sessions=max(len(conversations), legacy_count),
            expected_sessions=None if legacy_present else discovery.expected_sessions,
            completion_basis=legacy_reason or discovery.completion_basis,
        )
        coverage = read_copilot_sync_state(local_store_root)
        coverage.update(
            discovery_complete=result["discovery_complete"], discovered_sessions=result["discovered_sessions"],
            expected_sessions=result["expected_sessions"], completion_basis=result["completion_basis"],
        )
        write_copilot_sync_state(local_store_root, coverage)
        state.update(
            phase="downloading", discovered_tweets=result["discovered_sessions"],
            queued_tweets=result["discovered_sessions"], discovery_complete=result["discovery_complete"],
            message=f"Reading {len(conversations):,} Microsoft Copilot sessions...",
        )
        for position, conversation in enumerate(conversations):
            if should_stop() or (position and wait_for_cache_scan(
                config.cache_scan_wait("copilot", "text"), should_stop,
            )):
                result["stopped"] = True
                break
            try:
                messages = collect_copilot_messages(page, conversation, should_stop)
                if should_stop():
                    result["stopped"] = True
                    break
                previous_count = sum(
                    row["conversation_id"] == conversation["conversation_id"]
                    for row in store.rows
                )
                if len(messages) < previous_count:
                    raise RuntimeError(
                        "Microsoft Copilot exposed fewer messages than the cached session; "
                        "the existing history was preserved."
                    )
                merged = store.replace_conversation(conversation, messages)
                result["messages"] += merged["message_count"]
                result["added_or_changed"] += merged["added_or_changed"]
                result["unchanged"] += merged["unchanged_sessions"]
            except CopilotAccessError:
                raise
            except Exception as exc:
                result["failed"] += 1
                state.append_event(
                    f"Microsoft Copilot session {position + 1:,}/{len(conversations):,} failed: "
                    f"{str(exc).splitlines()[0][:250]}"
                )
            result["sessions"] += 1
            coverage = read_copilot_sync_state(local_store_root)
            coverage.update(processed_sessions=result["sessions"], failed_sessions=result["failed"])
            write_copilot_sync_state(local_store_root, coverage)
            state.update(
                processed_tweets=result["sessions"], downloaded_posts=store.cached_conversations,
                downloaded_tweets=store.cached_messages, skipped_tweets=result["unchanged"],
                discovered_images=result["messages"],
                failed_tweets=result["failed"],
                message=f"Read {result['sessions']:,}/{len(conversations):,} Microsoft Copilot sessions.",
            )
    result["stopped"] = result["stopped"] or should_stop()
    result["sync_complete"] = (
        result["discovery_complete"] and not result["stopped"] and not result["failed"]
        and result["sessions"] == result["discovered_sessions"]
    )
    return result
