"""Provider-neutral Web Agent Project and session discovery.

Code version: v1.9.9-codex.1
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import re
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from .browser_sessions import (
    browser_descriptors,
    goto_with_retry,
    is_grok_security_verification_page,
    launch_chromium_context,
    select_provider_tab,
    sync_playwright_or_error,
    visible_claude_composer_selector,
)
from .chatgpt_agent_sources import (
    list_chatgpt_agent_sources,
    list_chatgpt_project_sessions,
    normalize_chatgpt_conversation_url,
    normalize_chatgpt_project_url,
)
from .chatgpt_downloader import _chatgpt_project_id as _extract_chatgpt_project_id
from .config import CrawlConfig
from .gemini_downloader import (
    GEMINI_HOME_URL,
    collect_gemini_conversation_links,
    normalize_gemini_conversation_url,
)
from .grok_history import (
    GROK_HOME_URL,
    GrokConversation,
    _grok_api_json,
    _load_responses,
    _normalized_messages,
    _response_nodes,
    list_grok_conversations,
)


AGENT_SOURCE_LIMIT = 20
AGENT_SESSION_HISTORY_LIMIT = 100
SUPPORTED_AGENT_SOURCE_PLATFORMS = frozenset({"chatgpt", "gemini", "grok", "claude"})
AGENT_SOURCE_PLATFORM_LABELS = {
    "chatgpt": "ChatGPT",
    "gemini": "Gemini",
    "grok": "Grok",
    "claude": "Claude",
}
CLAUDE_HOME_URL = "https://claude.ai/new"
CLAUDE_HOSTS = frozenset({"claude.ai", "www.claude.ai"})
GEMINI_HOSTS = frozenset({"gemini.google.com"})
GEMINI_PROJECT_PATH_PATTERN = re.compile(r"^/(?:app|notebook|notebooks)/[A-Za-z0-9_-]+/?$")
GEMINI_RESERVED_PROJECT_IDS = frozenset({"create", "new"})
GROK_HOSTS = frozenset({"grok.com", "www.grok.com"})
GROK_CONVERSATION_PATH_PATTERN = re.compile(r"^/c/[A-Za-z0-9_-]+/?$")
GROK_PROJECT_PATH_PATTERN = re.compile(r"^/project/[A-Za-z0-9_-]+/?$")
CLAUDE_CONVERSATION_PATH_PATTERN = re.compile(
    r"^/(?:chat/[A-Za-z0-9_-]+|project/[A-Za-z0-9_-]+/(?:chat|c)/[A-Za-z0-9_-]+)/?$"
)
CLAUDE_PROJECT_PATH_PATTERN = re.compile(r"^/project/[A-Za-z0-9_-]+/?$")


def normalize_agent_conversation_url(platform: str, value: str) -> str:
    """Return one canonical conversation URL for a supported provider."""
    platform_key = str(platform or "").strip().lower()
    if platform_key == "chatgpt":
        return normalize_chatgpt_conversation_url(value)
    if platform_key == "gemini":
        return normalize_gemini_conversation_url(value)
    if platform_key == "grok":
        return normalize_grok_conversation_url(value)
    if platform_key == "claude":
        return normalize_claude_conversation_url(value)
    return ""


def normalize_agent_project_url(platform: str, value: str) -> str:
    """Return one canonical provider Project URL without exposing native names."""
    platform_key = str(platform or "").strip().lower()
    if platform_key == "chatgpt":
        return normalize_chatgpt_project_url(value)
    if platform_key == "gemini":
        # Gemini Notebooks open inside the same /app/<id> surface as a chat.
        return normalize_gemini_project_url(value)
    if platform_key == "grok":
        return normalize_grok_project_url(value)
    if platform_key == "claude":
        return normalize_claude_project_url(value)
    return ""


def chatgpt_project_id(value: str) -> str:
    """Return the stable ChatGPT Project id from a validated Project or chat URL."""
    normalized = (
        normalize_chatgpt_project_url(value)
        or normalize_chatgpt_conversation_url(value)
    )
    if not normalized:
        return ""
    project_id = _extract_chatgpt_project_id(normalized).casefold()
    if not re.fullmatch(r"g-p-[0-9a-f]{32}", project_id):
        return ""
    return project_id


def normalize_gemini_project_url(value: str) -> str:
    """Return a canonical Gemini Notebook URL from the shared Project contract."""
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return ""
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() not in GEMINI_HOSTS
        or parsed.username
        or parsed.password
        or not GEMINI_PROJECT_PATH_PATTERN.fullmatch(parsed.path)
    ):
        return ""
    project_id = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if project_id.casefold() in GEMINI_RESERVED_PROJECT_IDS:
        return ""
    return f"https://gemini.google.com/app/{project_id}"


def normalize_agent_source_catalog_payload(
    platform: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Revalidate cached Project rows through the current provider URL contract."""
    platform_key = str(platform or "").strip().lower()
    if platform_key not in SUPPORTED_AGENT_SOURCE_PLATFORMS:
        raise ValueError("Choose ChatGPT, Gemini, Grok, or Claude for the Web Agent.")
    normalized = dict(payload) if isinstance(payload, dict) else {}
    normalized["platform"] = platform_key
    raw_sessions = normalized.get("recent_sessions", [])
    raw_session_rows = raw_sessions if isinstance(raw_sessions, list) else []
    normalized_sessions = _normalize_session_rows(platform_key, raw_session_rows)
    normalized["recent_sessions"] = normalized_sessions[:AGENT_SOURCE_LIMIT]
    normalized["projects"] = _normalize_project_rows(
        platform_key,
        normalized.get("projects", []),
    )[:AGENT_SOURCE_LIMIT]
    if "sessions" in normalized:
        raw_project_sessions = normalized.get("sessions", [])
        project_session_rows = (
            raw_project_sessions if isinstance(raw_project_sessions, list) else []
        )
        normalized_sessions = _normalize_session_rows(platform_key, project_session_rows)
        normalized["sessions"] = normalized_sessions[:AGENT_SOURCE_LIMIT]
    return normalized


def normalize_claude_conversation_url(value: str) -> str:
    """Return a canonical Claude conversation URL, including Project chats."""
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return ""
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() not in CLAUDE_HOSTS
        or parsed.username
        or parsed.password
    ):
        return ""
    if CLAUDE_PROJECT_PATH_PATTERN.fullmatch(parsed.path):
        chat_id = str(parse_qs(parsed.query).get("chat", [""])[0] or "").strip()
        if re.fullmatch(r"[A-Za-z0-9_-]+", chat_id):
            return f"https://claude.ai{parsed.path.rstrip('/')}/chat/{chat_id}"
        return ""
    if not CLAUDE_CONVERSATION_PATH_PATTERN.fullmatch(parsed.path):
        return ""
    return f"https://claude.ai{parsed.path.rstrip('/')}"


def normalize_claude_project_url(value: str) -> str:
    """Return a canonical Claude Project URL from the shared Project contract."""
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return ""
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() not in CLAUDE_HOSTS
        or parsed.username
        or parsed.password
        or not CLAUDE_PROJECT_PATH_PATTERN.fullmatch(parsed.path)
    ):
        return ""
    return f"https://claude.ai{parsed.path.rstrip('/')}"


def claude_project_session_id(conversation_url: str, project_url: str) -> str:
    """Return a Claude Project session id only when both URLs share that Project path."""
    normalized_project = normalize_claude_project_url(project_url)
    normalized_conversation = normalize_claude_conversation_url(conversation_url)
    if not normalized_project or not normalized_conversation:
        return ""
    project_path = urlsplit(normalized_project).path.rstrip("/")
    conversation_path = urlsplit(normalized_conversation).path.rstrip("/")
    session_suffix = conversation_path.removeprefix(f"{project_path}/")
    match = re.fullmatch(r"(?:chat|c)/([A-Za-z0-9_-]+)", session_suffix)
    return match.group(1) if match else ""


def normalize_grok_conversation_url(value: str) -> str:
    """Return one canonical Grok conversation URL, including Project sessions."""
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return ""
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() not in GROK_HOSTS
        or parsed.username
        or parsed.password
    ):
        return ""
    if GROK_CONVERSATION_PATH_PATTERN.fullmatch(parsed.path):
        return f"https://grok.com{parsed.path.rstrip('/')}"
    if GROK_PROJECT_PATH_PATTERN.fullmatch(parsed.path):
        chat_id = str(parse_qs(parsed.query).get("chat", [""])[0] or "").strip()
        if re.fullmatch(r"[A-Za-z0-9_-]+", chat_id):
            return f"https://grok.com{parsed.path.rstrip('/')}?chat={chat_id}"
    return ""


def normalize_grok_project_url(value: str) -> str:
    """Return a canonical Grok Project URL from the shared Project contract."""
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return ""
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() not in GROK_HOSTS
        or parsed.username
        or parsed.password
        or not GROK_PROJECT_PATH_PATTERN.fullmatch(parsed.path)
    ):
        return ""
    return f"https://grok.com{parsed.path.rstrip('/')}?tab=conversations"


def list_agent_sources(
    platform: str,
    browser_name: str,
    config: CrawlConfig,
    *,
    silent: bool = False,
) -> dict[str, Any]:
    """Return a shared recent-session payload for one provider and browser."""
    platform_key = str(platform or "").strip().lower()
    if platform_key not in SUPPORTED_AGENT_SOURCE_PLATFORMS:
        raise ValueError("Choose ChatGPT, Gemini, Grok, or Claude for the Web Agent.")

    if platform_key == "chatgpt":
        payload = dict(list_chatgpt_agent_sources(browser_name, config, silent=silent))
        payload["platform"] = platform_key
        return payload
    if platform_key == "gemini":
        return _list_gemini_agent_sources(browser_name, config, silent=silent)
    if platform_key == "claude":
        return _list_claude_agent_sources(browser_name, config, silent=silent)
    return _list_grok_agent_sources(browser_name, config, silent=silent)


def list_agent_project_sessions(
    platform: str,
    browser_name: str,
    project_url: str,
    config: CrawlConfig,
    *,
    silent: bool = False,
) -> dict[str, Any]:
    """Return sessions inside one provider Project through one shared contract."""
    platform_key = str(platform or "").strip().lower()
    if platform_key not in SUPPORTED_AGENT_SOURCE_PLATFORMS:
        raise ValueError("Choose ChatGPT, Gemini, Grok, or Claude for the Web Agent.")
    normalized_project_url = normalize_agent_project_url(platform_key, project_url)
    if not normalized_project_url:
        platform_label = AGENT_SOURCE_PLATFORM_LABELS[platform_key]
        raise ValueError(f"Choose a valid {platform_label} Project before loading its sessions.")
    if platform_key == "chatgpt":
        payload = dict(
            list_chatgpt_project_sessions(
                browser_name,
                normalized_project_url,
                config,
                silent=silent,
            )
        )
        payload["platform"] = platform_key
        return payload
    if platform_key == "gemini":
        return {
            "platform": "gemini",
            "project_url": normalized_project_url,
            "sessions": [],
            "limit": AGENT_SOURCE_LIMIT,
            "message": (
                "Gemini Notebook session ownership cannot be verified; "
                "use New session in project."
            ),
        }
    if platform_key == "claude":
        return _list_claude_project_sessions(
            browser_name,
            normalized_project_url,
            config,
            silent=silent,
        )
    return _list_grok_project_sessions(
        browser_name,
        normalized_project_url,
        config,
        silent=silent,
    )


def fetch_grok_conversation_history(
    browser_name: str,
    conversation_url: str,
    config: CrawlConfig,
    *,
    silent: bool = False,
) -> dict[str, Any]:
    """Fetch one authenticated Grok conversation as read-only Agent history."""
    normalized_url = normalize_agent_conversation_url("grok", conversation_url)
    if not normalized_url:
        raise ValueError("Choose a valid Grok conversation before loading its history.")

    parsed_url = urlsplit(normalized_url)
    project_id = ""
    if GROK_PROJECT_PATH_PATTERN.fullmatch(parsed_url.path):
        project_id = parsed_url.path.rstrip("/").rsplit("/", 1)[-1]
        conversation_id = str(parse_qs(parsed_url.query).get("chat", [""])[0] or "").strip()
    else:
        conversation_id = parsed_url.path.rstrip("/").rsplit("/", 1)[-1]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", conversation_id):
        raise ValueError("Choose a valid Grok conversation before loading its history.")

    def collect(page: Any) -> dict[str, Any]:
        title = _grok_conversation_title(page, project_id, conversation_id)
        conversation = GrokConversation(
            conversation_id=conversation_id,
            title=title or f"Grok session {conversation_id}",
            created_at="",
            updated_at="",
            url=normalized_url,
        )
        nodes = _response_nodes(page, conversation_id)
        response_ids = [
            str(node.get("responseId") or node.get("id") or "").strip()
            for node in nodes
            if isinstance(node, dict)
        ]
        responses = _load_responses(page, conversation_id, response_ids)
        history = _pair_grok_history(
            _normalized_messages(conversation, nodes, responses, "")
        )
        return {
            "conversation_url": normalized_url,
            "title": title,
            "history": history[-AGENT_SESSION_HISTORY_LIMIT:],
            "limit": AGENT_SESSION_HISTORY_LIMIT,
        }

    return _run_chromium_source_collection(
        browser_name,
        config,
        normalized_url,
        collect,
        silent=silent,
    )


def _grok_conversation_title(page: Any, project_id: str, conversation_id: str) -> str:
    """Read a selected Grok title without making title lookup required for history."""
    query = {"pageSize": "100"}
    if project_id:
        query["workspaceId"] = project_id
    else:
        query["excludeProjects"] = "true"
    try:
        payload = _grok_api_json(
            page,
            "/rest/app-chat/conversations?" + urlencode(query),
        )
    except Exception:
        return ""
    for item in payload.get("conversations", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("conversationId") or item.get("id") or "").strip()
        if item_id != conversation_id:
            continue
        return str(item.get("title") or "").strip()
    return ""


def _pair_grok_history(messages: list[Any]) -> list[dict[str, str]]:
    """Pair ordered Grok user and assistant messages into Agent response pages."""
    history: list[dict[str, str]] = []
    pending_prompt: Any | None = None
    for message in sorted(
        (item for item in messages if item is not None),
        key=lambda item: int(getattr(item, "message_index", 0) or 0),
    ):
        role = str(getattr(message, "role", "") or "").strip().lower()
        content = str(getattr(message, "content_text", "") or "").strip()
        if not content:
            continue
        if role == "user":
            pending_prompt = message
            continue
        if role != "assistant" or pending_prompt is None:
            continue
        history.append(
            {
                "prompt": str(getattr(pending_prompt, "content_text", "") or "").strip(),
                "response": content,
                "started_at": str(getattr(pending_prompt, "timestamp", "") or ""),
                "finished_at": str(getattr(message, "timestamp", "") or ""),
            }
        )
        pending_prompt = None
    return history


def _list_gemini_agent_sources(
    browser_name: str,
    config: CrawlConfig,
    *,
    silent: bool = False,
) -> dict[str, Any]:
    """Collect Gemini's rendered recent-session links through the shared crawler."""
    capped_config = replace(
        config,
        gemini_max_conversations=min(
            AGENT_SOURCE_LIMIT,
            max(1, int(config.gemini_max_conversations)),
        ),
    )
    snapshot = _run_chromium_source_collection(
        browser_name,
        capped_config,
        GEMINI_HOME_URL,
        lambda page: _collect_gemini_sources(page, capped_config),
        silent=silent,
    )
    sessions = _normalize_session_rows("gemini", _snapshot_rows(snapshot, "recent_sessions"))
    projects = _normalize_project_rows(
        "gemini",
        _snapshot_rows(snapshot, "projects"),
    )
    return {
        "platform": "gemini",
        "browser_label": _browser_label(browser_name, config),
        "recent_sessions": sessions,
        "projects": projects[:AGENT_SOURCE_LIMIT],
        "limit": AGENT_SOURCE_LIMIT,
    }


def _list_grok_agent_sources(
    browser_name: str,
    config: CrawlConfig,
    *,
    silent: bool = False,
) -> dict[str, Any]:
    """Collect Grok's recent root-level conversations from its authenticated API."""
    snapshot = _run_chromium_source_collection(
        browser_name,
        config,
        GROK_HOME_URL,
        _collect_grok_sources,
        silent=silent,
    )
    sessions = _normalize_session_rows("grok", _snapshot_rows(snapshot, "recent_sessions"))
    projects = _snapshot_rows(snapshot, "projects")
    return {
        "platform": "grok",
        "browser_label": _browser_label(browser_name, config),
        "recent_sessions": sessions,
        "projects": projects[:AGENT_SOURCE_LIMIT],
        "limit": AGENT_SOURCE_LIMIT,
    }


def _list_claude_agent_sources(
    browser_name: str,
    config: CrawlConfig,
    *,
    silent: bool = False,
) -> dict[str, Any]:
    """Collect Claude's rendered recent chats and Projects from the signed-in page."""
    snapshot = _run_chromium_source_collection(
        browser_name,
        config,
        CLAUDE_HOME_URL,
        _collect_claude_sources,
        silent=silent,
    )
    return {
        "platform": "claude",
        "browser_label": _browser_label(browser_name, config),
        "recent_sessions": _normalize_session_rows(
            "claude",
            _snapshot_rows(snapshot, "recent_sessions"),
        ),
        "projects": _normalize_project_rows(
            "claude",
            _snapshot_rows(snapshot, "projects"),
        )[:AGENT_SOURCE_LIMIT],
        "limit": AGENT_SOURCE_LIMIT,
    }


def probe_and_collect_claude_sources(
    browser_name: str,
    config: CrawlConfig,
    *,
    silent: bool = False,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Verify Claude and collect its source catalog in one Chromium context."""
    descriptor = browser_descriptors(config).get(str(browser_name or "").strip().lower())
    if descriptor is None:
        raise ValueError(f"Unsupported browser: {browser_name}")
    if descriptor.engine != "chromium":
        raise ValueError(f"Claude Agent sessions require Edge or Chrome, not {descriptor.label}.")

    def collect(page: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
        status = _claude_page_status(page, descriptor.label)
        if not status["can_download"]:
            return status, None
        snapshot = _collect_claude_sources(page)
        return status, {
            "platform": "claude",
            "browser_label": descriptor.label,
            "recent_sessions": _normalize_session_rows(
                "claude",
                _snapshot_rows(snapshot, "recent_sessions"),
            ),
            "projects": _normalize_project_rows(
                "claude",
                _snapshot_rows(snapshot, "projects"),
            )[:AGENT_SOURCE_LIMIT],
            "limit": AGENT_SOURCE_LIMIT,
        }

    return _run_chromium_source_collection(
        browser_name,
        config,
        CLAUDE_HOME_URL,
        collect,
        silent=silent,
    )


def probe_and_collect_grok_sources(
    browser_name: str,
    config: CrawlConfig,
    *,
    silent: bool = False,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Verify Grok Web and collect Agent sources in one Chromium context."""
    descriptor = browser_descriptors(config).get(str(browser_name or "").strip().lower())
    if descriptor is None:
        raise ValueError(f"Unsupported browser: {browser_name}")
    if descriptor.engine != "chromium":
        raise ValueError(f"Grok Agent sessions require Edge or Chrome, not {descriptor.label}.")

    def collect(page: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
        status = _grok_page_status(page, descriptor.label)
        if not status["can_download"]:
            return status, None
        try:
            snapshot = _collect_grok_sources(page)
        except Exception:
            return status, None
        return status, {
            "platform": "grok",
            "browser_label": descriptor.label,
            "recent_sessions": _normalize_session_rows(
                "grok",
                _snapshot_rows(snapshot, "recent_sessions"),
            ),
            "projects": _normalize_project_rows(
                "grok",
                _snapshot_rows(snapshot, "projects"),
            )[:AGENT_SOURCE_LIMIT],
            "limit": AGENT_SOURCE_LIMIT,
        }

    try:
        return _run_chromium_source_collection(
            browser_name,
            config,
            GROK_HOME_URL,
            collect,
            silent=silent,
        )
    except Exception as exc:  # pragma: no cover - depends on local browser state
        return {
            "platform": "grok",
            "browser_label": descriptor.label,
            "logged_in": False,
            "can_download": False,
            "account_name": "",
            "message": str(exc),
        }, None


def _grok_page_status(page: Any, browser_label: str) -> dict[str, Any]:
    """Verify Grok Agent readiness from its actual message composer."""
    page.wait_for_timeout(2_000)
    title = ""
    body_text = ""
    html = ""
    try:
        title = str(page.title() or "")
    except Exception:
        pass
    try:
        body_text = str(page.locator("body").inner_text(timeout=5_000) or "")
    except Exception:
        pass
    try:
        html = str(page.content() or "")
    except Exception:
        pass
    if is_grok_security_verification_page(title, body_text, html):
        return {
            "platform": "grok",
            "browser_label": browser_label,
            "logged_in": False,
            "can_download": False,
            "account_name": "Security verification required",
            "message": (
                f"Grok showed a security verification page in {browser_label}, "
                "so the signed-in account could not be verified."
            ),
        }

    normalized_body = body_text.casefold()
    restricted_markers = (
        "account suspended",
        "account disabled",
        "account deactivated",
        "access restricted",
        "account is unavailable",
    )
    if any(marker in normalized_body for marker in restricted_markers):
        return {
            "platform": "grok",
            "browser_label": browser_label,
            "logged_in": False,
            "can_download": False,
            "account_name": "Grok account restricted",
            "message": f"{browser_label} reported that the Grok account is restricted or unavailable.",
        }

    signed_out = False
    try:
        signed_out = bool(
            page.evaluate(
                r"""() => {
                    const visible = (element) => element && element.getClientRects().length > 0
                        && getComputedStyle(element).visibility !== 'hidden'
                        && getComputedStyle(element).display !== 'none';
                    const authAction = [...document.querySelectorAll('a,button')].some((element) =>
                        visible(element) && [
                            element.getAttribute('aria-label') || '',
                            element.innerText || element.textContent || '',
                        ].some((value) => /^(?:sign in|log in|sign up|create account)(?:\s+to\s+grok)?$/i.test(
                            value.replace(/\s+/g, ' ').trim()
                        ))
                    );
                    return Boolean(authAction);
                }"""
            )
        )
    except Exception:
        pass
    if signed_out or re.search(
        r"(?:^|\n)\s*(?:sign in|log in|sign up|create account)(?:\s+to\s+grok)?\s*(?:$|\n)",
        normalized_body,
    ):
        return {
            "platform": "grok",
            "browser_label": browser_label,
            "logged_in": False,
            "can_download": False,
            "account_name": "",
            "message": f"{browser_label} is not signed in to Grok.",
        }

    composer = page.locator("textarea").first
    try:
        composer.wait_for(state="visible", timeout=20_000)
    except Exception:
        if re.search(r"\b(?:sign in|log in|sign up|create account)\b", normalized_body):
            message = f"{browser_label} is not signed in to Grok."
        else:
            message = f"{browser_label} could not verify an available Grok message composer."
        return {
            "platform": "grok",
            "browser_label": browser_label,
            "logged_in": False,
            "can_download": False,
            "account_name": "",
            "message": message,
        }
    try:
        authenticated_payload = _grok_api_json(
            page,
            "/rest/app-chat/conversations?"
            + urlencode({"pageSize": "1", "excludeProjects": "true"}),
        )
        if not isinstance(authenticated_payload.get("conversations"), list):
            raise RuntimeError("Grok authentication probe returned an invalid payload.")
    except Exception:
        return {
            "platform": "grok",
            "browser_label": browser_label,
            "logged_in": False,
            "can_download": False,
            "account_name": "",
            "message": (
                f"{browser_label} could not verify an authenticated Grok account."
            ),
        }
    return {
        "platform": "grok",
        "browser_label": browser_label,
        "logged_in": True,
        "can_download": True,
        "account_name": "Grok account",
        "message": f"{browser_label} verified an authenticated Grok Web session.",
    }


def _claude_page_status(page: Any, browser_label: str) -> dict[str, Any]:
    """Return a bounded readiness result without reading account or credential data."""
    page.wait_for_timeout(2_000)
    body_text = ""
    try:
        body_text = page.locator("body").inner_text(timeout=5_000)
    except Exception:
        pass
    normalized_body = str(body_text or "").casefold()
    restricted_markers = (
        "account suspended",
        "account has been suspended",
        "account disabled",
        "account has been disabled",
        "banned",
        "deactivated",
        "access restricted",
        "account is unavailable",
        "usage policy",
        "terms of service",
    )
    if any(marker in normalized_body for marker in restricted_markers):
        return {
            "platform": "claude",
            "browser_label": browser_label,
            "logged_in": False,
            "can_download": False,
            "account_name": "Claude account restricted",
            "message": f"{browser_label} reported that the Claude account is restricted or unavailable.",
        }
    try:
        composer = page.locator(visible_claude_composer_selector())
        count = getattr(composer, "count", None)
        if callable(count) and count() != 1:
            raise RuntimeError("Claude composer count was not unique.")
        composer.wait_for(state="visible", timeout=20_000)
    except Exception:
        if re.search(r"\b(?:sign in|log in|sign up|create account)\b", normalized_body):
            message = f"{browser_label} is not signed in to Claude."
        else:
            message = f"{browser_label} could not verify an available Claude message composer."
        return {
            "platform": "claude",
            "browser_label": browser_label,
            "logged_in": False,
            "can_download": False,
            "account_name": "",
            "message": message,
        }
    return {
        "platform": "claude",
        "browser_label": browser_label,
        "logged_in": True,
        "can_download": True,
        "account_name": "Claude account",
        "message": f"{browser_label} verified an authenticated Claude Web session.",
    }


def _list_claude_project_sessions(
    browser_name: str,
    project_url: str,
    config: CrawlConfig,
    *,
    silent: bool = False,
) -> dict[str, Any]:
    """Collect Claude chats whose rendered links belong to one Project."""
    sessions = _run_chromium_source_collection(
        browser_name,
        config,
        project_url,
        lambda page: _read_project_session_links(page, "claude", project_url),
        silent=silent,
    )
    return {
        "platform": "claude",
        "project_url": project_url,
        "sessions": sessions[:AGENT_SOURCE_LIMIT],
        "limit": AGENT_SOURCE_LIMIT,
    }


def _list_grok_project_sessions(
    browser_name: str,
    project_url: str,
    config: CrawlConfig,
    *,
    silent: bool = False,
) -> dict[str, Any]:
    sessions = _run_chromium_source_collection(
        browser_name,
        config,
        project_url,
        lambda page: _read_grok_project_session_links(page, project_url),
        silent=silent,
    )
    return {
        "platform": "grok",
        "project_url": project_url,
        "sessions": sessions[:AGENT_SOURCE_LIMIT],
        "limit": AGENT_SOURCE_LIMIT,
    }


def _collect_gemini_sources(page: Any, config: CrawlConfig) -> dict[str, Any]:
    """Reuse Gemini's history collector, then read Notebook links from its sidebar."""
    links = collect_gemini_conversation_links(page, config, lambda: False)
    return {
        "recent_sessions": links,
        "projects": _read_gemini_project_links(page),
    }


def _collect_grok_sources(page: Any) -> dict[str, Any]:
    """Reuse Grok's authenticated conversation API and read Project links from the page."""
    return {
        "recent_sessions": list_grok_conversations(page),
        "projects": _read_grok_project_links(page),
    }


def _collect_claude_sources(page: Any) -> dict[str, Any]:
    """Read Claude's sidebar links without depending on private API endpoints."""
    try:
        rows = page.evaluate(
            r"""() => {
                const textOf = (element) => [
                    element?.innerText,
                    element?.textContent,
                    element?.getAttribute?.('aria-label'),
                    element?.getAttribute?.('title'),
                ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
                const recentSessions = [];
                const projects = [];
                const seen = new Set();
                for (const element of document.querySelectorAll('a[href], [role="link"]')) {
                    const rawHref = element.href || element.getAttribute('href') || '';
                    let url;
                    try { url = new URL(rawHref, location.href); } catch (_) { continue; }
                    if (url.protocol !== 'https:' || !['claude.ai', 'www.claude.ai'].includes(url.hostname)) continue;
                    const path = url.pathname.replace(/\/+$/, '');
                    const row = element.closest('li') || element.parentElement || element;
                    const title = textOf(row) || textOf(element) || 'Untitled';
                    const item = {href: url.href, title};
                    const key = `${path}|${url.search}`;
                    if (seen.has(key)) continue;
                    seen.add(key);
                    const projectChat = /^\/project\/[A-Za-z0-9_-]+$/.test(path)
                        && /^[A-Za-z0-9_-]+$/.test(url.searchParams.get('chat') || '');
                    if (/^\/project\/[A-Za-z0-9_-]+$/.test(path) && !projectChat) {
                        projects.push(item);
                    } else if (projectChat
                        || /^\/chat\/[A-Za-z0-9_-]+$/.test(path)
                        || /^\/project\/[A-Za-z0-9_-]+\/(?:chat|c)\/[A-Za-z0-9_-]+$/.test(path)) {
                        recentSessions.push(item);
                    }
                }
                return {recent_sessions: recentSessions, projects};
            }"""
        )
    except Exception:
        return {"recent_sessions": [], "projects": []}
    return rows if isinstance(rows, dict) else {"recent_sessions": [], "projects": []}


def _read_gemini_project_links(page: Any) -> list[dict[str, str]]:
    """Read only explicit Gemini Notebook routes as shared Projects."""
    try:
        rows = page.locator(
            'a[href*="/notebook/"], a[href*="/notebooks/"], '
            '[role="link"][href*="/notebook/"], [role="link"][href*="/notebooks/"]'
        ).evaluate_all(
            r"""(elements) => {
                const textOf = (element) => [
                    element?.innerText,
                    element?.textContent,
                    element?.getAttribute?.('aria-label'),
                    element?.getAttribute?.('title'),
                ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
                const rows = [];
                for (const element of elements) {
                    const rawHref = element.href || element.getAttribute('href') || '';
                    let url;
                    try { url = new URL(rawHref, location.href); } catch (_) { continue; }
                    if (url.protocol !== 'https:' || url.hostname !== 'gemini.google.com') continue;
                    const path = url.pathname.replace(/\/+$/, '');
                    if (!/^\/(?:notebook|notebooks)\/[A-Za-z0-9_-]+$/.test(path)) continue;
                    const projectId = path.split('/').at(-1).toLowerCase();
                    if (['create', 'new'].includes(projectId)) continue;
                    rows.push({href: url.href, title: textOf(element)});
                }
                return rows;
            }"""
        )
    except Exception:
        return []
    return _normalize_project_rows("gemini", rows)


def _read_grok_project_links(page: Any) -> list[dict[str, str]]:
    """Read Grok Project rows, including button-only sidebar implementations."""
    try:
        projects = _read_grok_project_api_rows(page)
    except Exception:
        projects = []
    if projects:
        return _normalize_project_rows("grok", projects)

    try:
        projects_toggle = page.get_by_role("button", name="Projects", exact=True)
        if projects_toggle.count() and projects_toggle.is_visible():
            if projects_toggle.get_attribute("aria-expanded") != "true":
                projects_toggle.click(timeout=10_000)
                page.wait_for_timeout(250)

        project_buttons = page.locator('button, [role="button"]').evaluate_all(
            r"""(buttons) => buttons.map((button, index) => ({
                index,
                label: (button.innerText || button.textContent || button.getAttribute('aria-label') || '')
                    .replace(/\s+/g, ' ').trim(),
                hasNestedButton: Boolean(button.querySelector('button')),
                visible: button.getClientRects().length > 0,
            })).filter((item) => item.visible && item.hasNestedButton && item.label)"""
        )
        for item in project_buttons if isinstance(project_buttons, list) else []:
            label = str(item.get("label") or "") if isinstance(item, dict) else ""
            if not label or re.search(
                r"^(?:add project|history|search|see all|new chat|options|pfp|toggle sidebar)",
                label,
                flags=re.IGNORECASE,
            ):
                continue
            try:
                page.locator('button, [role="button"]').nth(int(item["index"])).click(timeout=5_000)
                page.wait_for_timeout(150)
            except Exception:
                continue

        rows = page.locator('a[href*="/project/"]').evaluate_all(
            r"""(links) => links.map((link) => {
                const text = (element) => (element?.innerText || element?.textContent || '')
                    .replace(/\s+/g, ' ').trim();
                const row = link.closest('li') || link.parentElement;
                return {
                    href: link.href || link.getAttribute('href') || '',
                    title: text(row) || text(link),
                };
            })"""
        )
    except Exception:
        return []
    return _normalize_project_rows("grok", rows)


def _read_grok_project_api_rows(page: Any) -> list[dict[str, str]]:
    """Read Grok Projects from the authenticated workspace repository API."""
    rows: list[dict[str, str]] = []
    page_token = ""
    seen_tokens: set[str] = set()
    for _ in range(20):
        query = {
            "pageSize": "50",
            "orderBy": "ORDER_BY_LAST_USE_TIME",
            "kind": "WORKSPACE_KIND_ALL",
        }
        if page_token:
            query["pageToken"] = page_token
        payload = _grok_api_json(page, "/rest/workspaces?" + urlencode(query))
        workspaces = payload.get("workspaces")
        if not isinstance(workspaces, list):
            break
        for item in workspaces:
            if not isinstance(item, dict):
                continue
            workspace_id = str(item.get("workspaceId") or "").strip()
            if not workspace_id or str(item.get("kind") or "").strip() == "WORKSPACE_KIND_IMAGINE":
                continue
            rows.append(
                {
                    "href": f"https://grok.com/project/{workspace_id}",
                    "title": str(item.get("name") or "").strip() or "Untitled project",
                    "updated_at": str(item.get("lastUseTime") or "").strip(),
                }
            )
        next_token = str(payload.get("nextPageToken") or "").strip()
        if not next_token or next_token in seen_tokens or not workspaces:
            break
        seen_tokens.add(next_token)
        page_token = next_token
    return rows


def _read_grok_project_session_links(page: Any, project_url: str) -> list[dict[str, str]]:
    try:
        project_id = urlsplit(project_url).path.rstrip("/").rsplit("/", 1)[-1]
        payload = _grok_api_json(
            page,
            "/rest/app-chat/conversations?"
            + urlencode({"workspaceId": project_id, "pageSize": "100"}),
        )
        rows = []
        for item in payload.get("conversations", []) if isinstance(payload, dict) else []:
            if not isinstance(item, dict):
                continue
            conversation_id = str(item.get("conversationId") or "").strip()
            if not conversation_id:
                continue
            rows.append(
                {
                    "id": conversation_id,
                    "url": (
                        f"https://grok.com/project/{quote(project_id, safe='')}"
                        f"?chat={quote(conversation_id, safe='')}"
                    ),
                    "title": str(item.get("title") or "").strip(),
                    "updated_at": str(item.get("modifyTime") or "").strip(),
                }
            )
        return _normalize_session_rows("grok", rows)
    except Exception:
        return _read_project_session_links(page, "grok", project_url)


def _read_project_session_links(page: Any, platform: str, project_url: str) -> list[dict[str, str]]:
    if platform == "gemini":
        return []
    try:
        rows = page.evaluate(
            r"""() => [...document.querySelectorAll('a[href], [role="link"]')].map((element) => ({
                href: element.href || element.getAttribute('href') || '',
                title: (element.innerText || element.textContent || element.getAttribute('aria-label') || '').trim(),
            }))"""
        )
    except Exception:
        return []
    normalized_project = normalize_agent_project_url(platform, project_url) if project_url else ""
    sessions: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        raw_url = str(row.get("href") or "")
        normalized_url = normalize_agent_conversation_url(platform, raw_url)
        if not normalized_url or normalized_url == normalized_project or normalized_url in seen_urls:
            continue
        if platform == "grok":
            normalized_session_project = normalize_agent_project_url(
                "grok",
                normalized_url,
            )
            chat_id = str(
                parse_qs(urlsplit(normalized_url).query).get("chat", [""])[0]
                or ""
            ).strip()
            if (
                not normalized_project
                or normalized_session_project != normalized_project
                or not re.fullmatch(r"[A-Za-z0-9_-]+", chat_id)
            ):
                continue
            session_id = chat_id
        elif platform == "claude":
            session_id = claude_project_session_id(
                normalized_url,
                normalized_project,
            )
            if not session_id:
                continue
        else:
            session_id = normalized_url.rsplit("/", 1)[-1]
        seen_urls.add(normalized_url)
        sessions.append(
            {
                "id": session_id,
                "title": str(row.get("title") or "").strip() or "Untitled session",
                "url": normalized_url,
                "updated_at": "",
            }
        )
    return sessions


def _normalize_project_rows(platform: str, rows: Any) -> list[dict[str, str]]:
    projects: list[dict[str, str]] = []
    deduplicated: dict[str, dict[str, str]] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        normalized_url = normalize_agent_project_url(
            platform,
            str(row.get("href") or row.get("url") or ""),
        )
        if not normalized_url:
            continue
        stable_chatgpt_id = (
            chatgpt_project_id(normalized_url)
            if platform == "chatgpt"
            else ""
        )
        project_path_segments = [
            segment
            for segment in urlsplit(normalized_url).path.rstrip("/").split("/")
            if segment
        ]
        url_project_id = (
            project_path_segments[-2]
            if (
                platform == "chatgpt"
                and len(project_path_segments) >= 2
                and project_path_segments[-1].casefold() == "project"
            )
            else project_path_segments[-1]
        )
        candidate = {
            "id": stable_chatgpt_id or url_project_id,
            "title": str(row.get("title") or "").strip() or "Untitled project",
            "url": normalized_url,
            "updated_at": str(row.get("updated_at") or "").strip(),
        }
        icon = _item_value(row, "icon") or _item_value(row, "emoji")
        icon_color = _item_value(row, "icon_color") or _item_value(row, "theme")
        if icon:
            candidate["icon"] = icon
        if icon_color:
            candidate["icon_color"] = icon_color
        identity = stable_chatgpt_id or normalized_url
        existing = deduplicated.get(identity)
        deduplicated[identity] = _prefer_newer_source_row(existing, candidate)
    projects.extend(deduplicated.values())
    projects.sort(key=_source_updated_at_key, reverse=True)
    return projects


def _snapshot_rows(snapshot: Any, key: str) -> list[Any]:
    """Read one adapter snapshot field while keeping old list-only collectors compatible."""
    if isinstance(snapshot, list):
        return snapshot if key == "recent_sessions" else []
    if not isinstance(snapshot, dict):
        return []
    rows = snapshot.get(key, [])
    return rows if isinstance(rows, list) else []


def _normalize_session_rows(platform: str, rows: list[Any]) -> list[dict[str, str]]:
    deduplicated: dict[str, dict[str, str]] = {}
    for row in rows:
        normalized_url = normalize_agent_conversation_url(platform, _item_value(row, "url"))
        if not normalized_url:
            continue
        candidate = {
            "id": _item_value(row, "conversation_id")
            or _item_value(row, "id")
            or normalized_url.rsplit("/", 1)[-1],
            "title": _item_value(row, "title") or "Untitled session",
            "url": normalized_url,
            "updated_at": _item_value(row, "updated_at"),
        }
        existing = deduplicated.get(normalized_url)
        deduplicated[normalized_url] = _prefer_newer_source_row(existing, candidate)
    sessions = list(deduplicated.values())
    sessions.sort(key=_source_updated_at_key, reverse=True)
    return sessions[:AGENT_SOURCE_LIMIT]


def _source_updated_at_key(item: dict[str, str]) -> tuple[int, float]:
    """Sort source rows by update time while keeping undated provider rows stable."""
    raw_value = str(item.get("updated_at") or "").strip()
    if not raw_value:
        return (0, 0.0)
    try:
        numeric_value = float(raw_value)
    except ValueError:
        numeric_value = None
    if numeric_value is not None:
        if numeric_value > 10_000_000_000:
            numeric_value /= 1_000
        return (1, numeric_value)
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        return (0, 0.0)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (1, parsed.astimezone(timezone.utc).timestamp())


def _prefer_newer_source_row(
    existing: dict[str, str] | None,
    candidate: dict[str, str],
) -> dict[str, str]:
    """Merge duplicate source rows without losing a newer title or timestamp."""
    if existing is None:
        return candidate
    preferred = candidate if _source_updated_at_key(candidate) > _source_updated_at_key(existing) else existing
    other = existing if preferred is candidate else candidate
    if not preferred.get("title") and other.get("title"):
        preferred = {**preferred, "title": other["title"]}
    for key in ("icon", "icon_color"):
        if not preferred.get(key) and other.get(key):
            preferred = {**preferred, key: other[key]}
    return preferred


def _item_value(item: Any, key: str) -> str:
    """Read dataclass and dictionary source rows through one small contract."""
    value = item.get(key) if isinstance(item, dict) else getattr(item, key, "")
    return str(value or "").strip()


def _browser_label(browser_name: str, config: CrawlConfig) -> str:
    """Resolve the selected browser label using the single browser registry."""
    descriptor = browser_descriptors(config).get(str(browser_name or "").strip().lower())
    if descriptor is None:
        raise ValueError(f"Unsupported browser: {browser_name}")
    return descriptor.label


def _run_chromium_source_collection(
    browser_name: str,
    config: CrawlConfig,
    home_url: str,
    collector: Callable[[Any], Any],
    *,
    silent: bool = False,
) -> Any:
    """Run one authenticated source collector in the selected Chromium profile."""
    descriptor = browser_descriptors(config).get(str(browser_name or "").strip().lower())
    if descriptor is None:
        raise ValueError(f"Unsupported browser: {browser_name}")
    if descriptor.engine != "chromium":
        raise ValueError(f"Gemini, Grok, and Claude Agent sources require Edge or Chrome, not {descriptor.label}.")

    with sync_playwright_or_error() as playwright:
        with launch_chromium_context(
            playwright,
            descriptor,
            headless=False,
            clone_profile_first=True,
            background_window=True,
            silent=silent,
            prefer_initialized_debug_profile=True,
        ) as context:
            home_host = (urlsplit(home_url).hostname or "").lower()
            hosts = {home_host, f"www.{home_host}"} if home_host else set()
            if home_host in GEMINI_HOSTS:
                hosts = set(GEMINI_HOSTS)
            elif home_host in GROK_HOSTS:
                hosts = set(GROK_HOSTS)
            elif home_host in CLAUDE_HOSTS:
                hosts = set(CLAUDE_HOSTS)
            page = select_provider_tab(context, home_url=home_url, hosts=hosts)
            current_url = str(getattr(page, "url", "") or "").strip().rstrip("/")
            if current_url != str(home_url).strip().rstrip("/"):
                goto_with_retry(page, home_url, attempts=2, timeout_ms=90_000)
            page.wait_for_timeout(500)
            return collector(page)
