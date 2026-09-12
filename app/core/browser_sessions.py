"""Browser session probing helpers for supported cache sources."""

# Code version: v1.23.0-codex.1

from __future__ import annotations

import contextlib
import json
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .browser.x_session import X_READY_SELECTORS, detect_account_handle
from .config import CrawlConfig, default_edge_user_data_dir, is_macos_host, is_windows_host
from .safari_automation import SafariContext


LOGGER = logging.getLogger(__name__)

try:  # pragma: no cover - depends on local runtime
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    PlaywrightError = RuntimeError
    sync_playwright = None


class _CdpAttachCleanupNoiseFilter(logging.Filter):
    """Silence known benign Playwright CDP-detach cleanup records."""

    _NOISE_FRAGMENTS = (
        "task was destroyed but it is pending",
        "future exception was never retrieved",
    )
    _PLAYWRIGHT_TRANSPORT_MARKERS = (
        "connection.run",
        "targetclosederror",
        "target page, context or browser has been closed",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage().casefold()
        except Exception:
            message = str(getattr(record, "msg", "") or "").casefold()
        if not any(fragment in message for fragment in self._NOISE_FRAGMENTS):
            return True
        combined = " ".join(
            str(item).casefold()
            for item in (
                message,
                getattr(record, "args", "") or "",
                str(getattr(record, "exc_info", None) or "") or "",
            )
            if item
        )
        return not any(marker in combined for marker in self._PLAYWRIGHT_TRANSPORT_MARKERS)


if is_windows_host():
    logging.getLogger("asyncio").addFilter(_CdpAttachCleanupNoiseFilter())


X_HOME_URL = "https://x.com/home"
GROK_FILES_URL = "https://grok.com/files"
CHATGPT_HOME_URL = "https://chatgpt.com/"
CHATGPT_AUTH_SESSION_URL = "https://chatgpt.com/api/auth/session"
GEMINI_HOME_URL = "https://gemini.google.com/app"
CLAUDE_HOME_URL = "https://claude.ai/new"
ZHIHU_HOME_URL = "https://www.zhihu.com/"
ZHIHU_AUTH_SESSION_URL = "https://www.zhihu.com/api/v4/me"
CLAUDE_COMPOSER_SELECTOR = (
    'div.ProseMirror[contenteditable="true"], '
    '[data-testid*="composer" i] [contenteditable="true"], '
    '[data-testid*="message-input" i] [contenteditable="true"], '
    '[contenteditable="true"][role="textbox"][aria-label*="message" i], '
    '[contenteditable="true"][role="textbox"][aria-label*="prompt" i], '
    '[contenteditable="true"][role="textbox"][aria-label*="ask" i], '
    '[contenteditable="true"][data-placeholder*="message" i], '
    'textarea[aria-label*="message" i], '
    'textarea[aria-label*="prompt" i], '
    'textarea[placeholder*="message" i]'
)


def visible_claude_composer_selector() -> str:
    """Return the shared Claude composer selector with visible-state constraints."""
    return ", ".join(
        f'{candidate.strip()}:visible:not([disabled]):not([aria-disabled="true"])'
        for candidate in CLAUDE_COMPOSER_SELECTOR.split(",")
        if candidate.strip()
    )
EDGE_USER_DATA_DIR = default_edge_user_data_dir()
EDGE_PROFILE_DIRECTORY = "Default"
SAFARI_APPLESCRIPT_SOURCE_LIMIT = 500_000
X_AUTH_MARKERS = ("Sign in", "Log in", "登录", "注册")
X_LOGGED_OUT_SOURCE_MARKERS = ("bundle.LoggedOutShell", "bundle.LoggedOutRoutes", "Sign in to X")
TRANSIENT_BROWSER_ERROR_MARKERS = (
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_NETWORK_CHANGED",
    "ERR_TIMED_OUT",
    "ERR_CONNECTION_TIMED_OUT",
    "ERR_CONNECTION_RESET",
)
IDEMPOTENT_CHROMIUM_CONTEXT_CLOSE_ERROR_MARKERS = (
    "browsercontext.close: connection closed",
    "browsercontext.close: target page, context or browser has been closed",
    "browsercontext.close: browser has been closed",
    "browsercontext.close: browser was closed",
    "browsercontext.close: driver disconnected",
    "browsercontext.close: driver was disconnected",
)
GROK_SECURITY_CHALLENGE_TITLE_MARKERS = ("just a moment", "attention required")
GROK_SECURITY_CHALLENGE_BODY_MARKERS = (
    "performing security verification",
    "security service to protect against malicious bots",
    "performance and security by cloudflare",
    "checking your browser before accessing",
)
CHROMIUM_WINDOW_MODE_OFFSCREEN = "offscreen"
CHROMIUM_WINDOW_MODE_TASK_STAGE = "task_stage"
CHROMIUM_RENDERING_BACKGROUND_ARGS = (
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-session-crashed-bubble",
    "--noerrdialogs",
    "--disable-notifications",
    "--disable-prompt-on-repost",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
)
BACKGROUND_CHROMIUM_WINDOW_ARGS = (
    "--window-position=-32000,-32000",
    "--window-size=1280,900",
    "--start-minimized",
    *CHROMIUM_RENDERING_BACKGROUND_ARGS,
)
TASK_STAGE_CHROMIUM_WINDOW_ARGS = (
    *CHROMIUM_RENDERING_BACKGROUND_ARGS,
)
CHROMIUM_TEMP_PROFILE_STALE_AFTER_SECONDS = 24 * 60 * 60
_ACTIVE_CHROMIUM_PROFILE_ROOTS: set[Path] = set()
_PLAYWRIGHT_LAUNCH_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class BrowserDescriptor:
    """Describe one browser option exposed in the UI."""

    browser_id: str
    label: str
    icon_filename: str
    engine: str
    user_data_dir: Path | None = None
    profile_directory: str = ""
    channel: str = ""


def build_browser_options(config: CrawlConfig) -> list[dict[str, str]]:
    """Return browser options for the sidebar selector."""
    return [
        {
            "id": descriptor.browser_id,
            "label": descriptor.label,
            "icon_filename": descriptor.icon_filename,
        }
        for descriptor in browser_descriptors(config).values()
    ]


def open_zhihu_browser_for_login(
    browser_name: str,
    config: CrawlConfig,
) -> dict[str, Any]:
    """Open Zhihu in the selected visible Chromium browser for manual sign-in."""

    selected = str(browser_name or "").strip().lower()
    descriptor = browser_descriptors(config).get(selected)
    if descriptor is None or descriptor.engine != "chromium":
        raise ValueError("Zhihu sign-in requires Edge or Chrome.")
    application = "Microsoft Edge" if selected == "edge" else "Google Chrome"
    process_options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if is_macos_host():
        command = ["/usr/bin/open", "-g", "-a", application, ZHIHU_HOME_URL]
        process_options["start_new_session"] = True
    elif is_windows_host():
        from .computer_use_agent import resolve_windows_browser_executable

        executable = resolve_windows_browser_executable(selected)
        if executable is None:
            raise RuntimeError(f"{application} could not be found on this host.")
        command = [
            executable,
            f"--user-data-dir={descriptor.user_data_dir}",
            f"--profile-directory={descriptor.profile_directory}",
            ZHIHU_HOME_URL,
        ]
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess,
            "DETACHED_PROCESS",
            0,
        )
        if creation_flags:
            process_options["creationflags"] = creation_flags
    else:
        executable = shutil.which("microsoft-edge" if selected == "edge" else "google-chrome")
        if not executable:
            raise RuntimeError(f"{application} could not be found on this host.")
        command = [executable, ZHIHU_HOME_URL]
        process_options["start_new_session"] = True
    try:
        subprocess.Popen(command, **process_options)
    except OSError as exc:
        raise RuntimeError(f"Could not open Zhihu in {application}: {exc}") from exc
    return {
        "opened": True,
        "platform": "zhihu",
        "browser": selected,
        "application": application,
        "url": ZHIHU_HOME_URL,
        "message": f"Opened Zhihu in {application}. Sign in, then choose Recheck.",
    }


def browser_descriptors(config: CrawlConfig) -> dict[str, BrowserDescriptor]:
    """Return runtime-aware browser descriptors."""
    descriptors = {
        "edge": BrowserDescriptor(
            browser_id="edge",
            label="Edge",
            icon_filename="images/browser.edge.png",
            engine="chromium",
            user_data_dir=default_edge_user_data_dir(),
            profile_directory=EDGE_PROFILE_DIRECTORY,
            channel="msedge",
        ),
        "chrome": BrowserDescriptor(
            browser_id="chrome",
            label="Chrome",
            icon_filename="images/browser.chrome.png",
            engine="chromium",
            user_data_dir=Path(config.chrome_user_data_dir).expanduser(),
            profile_directory=config.chrome_profile_directory,
            channel="chrome",
        ),
    }
    if is_macos_host():
        descriptors["safari"] = BrowserDescriptor(
            browser_id="safari",
            label="Safari",
            icon_filename="images/browser.safari.png",
            engine="safari",
        )
    return descriptors


def probe_browser_session(
    platform_name: str,
    browser_name: str,
    config: CrawlConfig,
    *,
    silent: bool = False,
    prefer_initialized_debug_profile: bool = False,
) -> dict[str, Any]:
    """Probe whether one browser is signed in for the requested platform."""
    descriptors = browser_descriptors(config)
    descriptor = descriptors.get(browser_name)
    if descriptor is None:
        raise ValueError(f"Unsupported browser: {browser_name}")

    platform_key = (platform_name or "").strip().lower()
    if platform_key not in {"x", "grok", "chatgpt", "gemini", "claude", "zhihu"}:
        raise ValueError(f"Unsupported platform: {platform_name}")

    result = {
        "platform": platform_key,
        "browser": descriptor.browser_id,
        "browser_label": descriptor.label,
        "icon_filename": descriptor.icon_filename,
        "logged_in": False,
        "can_download": False,
        "account_name": "",
        "message": "",
    }

    try:
        if platform_key == "chatgpt":
            result.update(
                _probe_chatgpt_session(
                    descriptor,
                    config,
                    silent=silent,
                    prefer_initialized_debug_profile=prefer_initialized_debug_profile,
                )
            )
        elif platform_key == "gemini":
            result.update(
                _probe_gemini_session(
                    descriptor,
                    config,
                    silent=silent,
                    prefer_initialized_debug_profile=prefer_initialized_debug_profile,
                )
            )
        elif platform_key == "claude":
            result.update(
                _probe_claude_session(
                    descriptor,
                    silent=silent,
                    prefer_initialized_debug_profile=prefer_initialized_debug_profile,
                )
            )
        elif platform_key == "zhihu":
            result.update(
                _probe_zhihu_session(
                    descriptor,
                    silent=silent,
                    prefer_initialized_debug_profile=prefer_initialized_debug_profile,
                )
            )
        elif descriptor.engine == "safari":
            if platform_key == "x":
                result.update(_probe_safari_x_session(descriptor))
            else:
                result.update(_probe_safari_grok_session(descriptor))
        elif platform_key == "x":
            result.update(
                _probe_chromium_x_session(
                    descriptor,
                    prefer_initialized_debug_profile=prefer_initialized_debug_profile,
                )
            )
        else:
            result.update(
                _probe_chromium_grok_session(
                    descriptor,
                    silent=silent,
                    prefer_initialized_debug_profile=prefer_initialized_debug_profile,
                )
            )
    except Exception as exc:  # pragma: no cover - depends on local browser state
        result["message"] = f"{type(exc).__name__}: {exc}"
        return result

    if not result["message"]:
        if result["can_download"]:
            result["message"] = f"{descriptor.label} is ready to download from {platform_key.upper()}."
        else:
            result["message"] = f"{descriptor.label} is not ready for {platform_key.upper()} yet."
    return result


def _probe_claude_session(
    descriptor: BrowserDescriptor,
    *,
    silent: bool = False,
    prefer_initialized_debug_profile: bool = False,
) -> dict[str, Any]:
    """Verify a Claude Web composer without reading account or credential data."""
    if descriptor.engine != "chromium":
        return {
            "logged_in": False,
            "can_download": False,
            "account_name": "",
            "message": f"Claude Agent sessions require Edge or Chrome, not {descriptor.label}.",
        }
    with _serialized_sync_playwright() as playwright:
        with launch_chromium_context(
            playwright,
            descriptor,
            # Match the headed background context used by Claude history and Agent sources.
            headless=False,
            clone_profile_first=True,
            background_window=True,
            silent=silent,
            prefer_initialized_debug_profile=prefer_initialized_debug_profile,
        ) as context:
            page = context.pages[0] if context.pages else context.new_page()
            goto_with_retry(page, CLAUDE_HOME_URL, attempts=2, timeout_ms=60_000)
            page.wait_for_timeout(2_000)
            try:
                body_text = page.locator("body").inner_text(timeout=5_000)
            except Exception:
                body_text = ""
            normalized_body = str(body_text or "").casefold()
            if any(
                marker in normalized_body
                for marker in (
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
            ):
                return {
                    "logged_in": False,
                    "can_download": False,
                    "account_name": "Claude account restricted",
                    "message": (
                        f"{descriptor.label} reported that the Claude account is restricted or unavailable."
                    ),
                }
            try:
                composer = page.locator(visible_claude_composer_selector())
                composer.first.wait_for(
                    state="visible",
                    timeout=20_000,
                )
                if composer.count() != 1:
                    raise RuntimeError("Claude composer count was not unique.")
            except Exception:
                # Hydration can reveal sign-in UI after the initial body snapshot.
                try:
                    normalized_body = page.locator("body").inner_text(timeout=5_000).casefold()
                except Exception:
                    pass
                message = (
                    f"{descriptor.label} is not signed in to Claude."
                    if re.search(r"\b(?:sign in|log in|sign up|create account)\b", normalized_body)
                    else f"{descriptor.label} could not verify an available Claude message composer."
                )
                return {
                    "logged_in": False,
                    "can_download": False,
                    "account_name": "",
                    "message": message,
                }
            return {
                "logged_in": True,
                "can_download": True,
                "account_name": "Claude account",
                "message": f"{descriptor.label} is ready to use Claude Web.",
            }


def _probe_gemini_session(
    descriptor: BrowserDescriptor,
    config: CrawlConfig,
    *,
    silent: bool = False,
    prefer_initialized_debug_profile: bool = False,
) -> dict[str, Any]:
    """Verify that the selected browser exposes an authenticated Gemini page."""
    from .gemini_downloader import _wait_for_gemini_ready

    if descriptor.engine == "safari":
        with SafariContext(GEMINI_HOME_URL) as context:
            page = context.primary_page
            goto_with_retry(page, GEMINI_HOME_URL, attempts=2, timeout_ms=60_000)
            snapshot = _wait_for_gemini_ready(page)
    elif descriptor.engine == "chromium":
        with _serialized_sync_playwright() as playwright:
            with launch_chromium_context(
                playwright,
                descriptor,
                headless=False,
                clone_profile_first=True,
                background_window=True,
                silent=silent,
                prefer_initialized_debug_profile=prefer_initialized_debug_profile,
            ) as context:
                page = context.pages[0] if context.pages else context.new_page()
                goto_with_retry(page, GEMINI_HOME_URL, attempts=2, timeout_ms=60_000)
                snapshot = _wait_for_gemini_ready(page)
    else:
        return {
            "logged_in": False,
            "can_download": False,
            "account_name": "",
            "message": f"Gemini history sync does not support {descriptor.label}.",
        }

    if snapshot.get("signedOut"):
        return {
            "logged_in": False,
            "can_download": False,
            "account_name": "",
            "message": f"{descriptor.label} is not signed in to Gemini.",
        }
    return {
        "logged_in": True,
        "can_download": True,
        "account_name": "Google account",
        "message": (
            f"{descriptor.label} verified an authenticated Gemini session. "
            "A background browser window will cache rendered sessions to Parquet."
        ),
    }


def _probe_chromium_x_session(
    descriptor: BrowserDescriptor,
    *,
    prefer_initialized_debug_profile: bool = False,
) -> dict[str, Any]:
    """Probe an X session from a Chromium-family browser profile."""
    with _serialized_sync_playwright() as playwright:
        with launch_chromium_context(
            playwright,
            descriptor,
            headless=True,
            clone_profile_first=True,
            background_window=True,
            prefer_initialized_debug_profile=prefer_initialized_debug_profile,
        ) as context:
            page = context.pages[0] if context.pages else context.new_page()
            goto_with_retry(page, X_HOME_URL)
            wait_for_x_page_ready(page, descriptor.label)
            account_handle = detect_account_handle(page)
            return {
                "logged_in": True,
                "can_download": True,
                "account_name": f"@{account_handle}",
                "message": f"{descriptor.label} is signed in to X as @{account_handle}.",
            }


def _probe_chromium_grok_session(
    descriptor: BrowserDescriptor,
    *,
    silent: bool = False,
    prefer_initialized_debug_profile: bool = False,
) -> dict[str, Any]:
    """Probe a Grok session from a Chromium-family browser profile."""
    with _serialized_sync_playwright() as playwright:
        with launch_chromium_context(
            playwright,
            descriptor,
            headless=True,
            clone_profile_first=True,
            background_window=True,
            silent=silent,
            prefer_initialized_debug_profile=prefer_initialized_debug_profile,
        ) as context:
            page = context.pages[0] if context.pages else context.new_page()
            goto_with_retry(page, GROK_FILES_URL)
            page.wait_for_timeout(8_000)
            title = page.title()
            body_text = page.locator("body").inner_text(timeout=10_000)
            html = page.content()
            account_name = parse_grok_account_label(html)
            if account_name:
                return {
                    "logged_in": True,
                    "can_download": True,
                    "account_name": account_name,
                    "message": f"{descriptor.label} is ready to sync Grok.",
                }
            if is_grok_security_verification_page(title, body_text, html):
                return {
                    "logged_in": False,
                    "can_download": False,
                    "account_name": "Security verification required",
                    "message": (
                        f"Grok showed a Cloudflare security verification page in {descriptor.label}, "
                        "so the signed-in account could not be verified."
                    ),
                }
            if any(marker in body_text for marker in ("Sign in", "Log in")):
                return {
                    "logged_in": False,
                    "can_download": False,
                    "account_name": "",
                    "message": f"{descriptor.label} is not signed in to Grok.",
                }
            raise RuntimeError(f"Could not detect the signed-in Grok account from {descriptor.label}.")


def _read_chatgpt_auth_payload(page: Any, browser_label: str) -> dict[str, Any]:
    """Read the authenticated ChatGPT session from the active page context."""
    auth_result = page.evaluate(
        """async () => {
            try {
                const response = await fetch('/api/auth/session', {
                    credentials: 'include',
                    cache: 'no-store',
                    headers: { Accept: 'application/json' },
                });
                return {
                    ok: response.ok,
                    status: response.status,
                    bodyText: await response.text(),
                    error: '',
                };
            } catch (error) {
                return {
                    ok: false,
                    status: 0,
                    bodyText: '',
                    error: String(error && error.message ? error.message : error),
                };
            }
        }"""
    )
    if not isinstance(auth_result, dict) or not auth_result.get("ok"):
        error_text = str(auth_result.get("error") or "") if isinstance(auth_result, dict) else ""
        status = int(auth_result.get("status") or 0) if isinstance(auth_result, dict) else 0
        raise RuntimeError(
            f"{browser_label} could not verify the ChatGPT session in-page "
            f"(HTTP {status or 'unavailable'}{f': {error_text}' if error_text else ''})."
        )
    return _parse_chatgpt_auth_response(True, str(auth_result.get("bodyText") or ""))


def _parse_chatgpt_auth_response(response_ok: bool, body_text: str) -> dict[str, Any]:
    """Decode the small ChatGPT auth response used by the status probe."""
    try:
        payload = json.loads(body_text) if response_ok else {}
    except (json.JSONDecodeError, TypeError, ValueError):
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _chatgpt_status_payload(browser_label: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Convert ChatGPT auth JSON into the shared browser-readiness contract."""
    if not str(payload.get("accessToken") or "").strip():
        return {
            "logged_in": False,
            "can_download": False,
            "account_name": "ChatGPT account",
            "message": (
                f"{browser_label} opened ChatGPT but did not expose an authorized account."
            ),
        }
    return {
        "logged_in": True,
        "can_download": True,
        "account_name": "ChatGPT account",
        "message": f"The ChatGPT account is ready in the selected {browser_label} browser.",
    }


def _probe_chatgpt_session(
    descriptor: BrowserDescriptor,
    config: CrawlConfig,
    *,
    silent: bool = False,
    prefer_initialized_debug_profile: bool = False,
) -> dict[str, Any]:
    """Validate ChatGPT authorization in the selected browser."""
    del config
    project_url = CHATGPT_HOME_URL

    if descriptor.engine == "safari":
        with SafariContext(project_url) as context:
            page = context.primary_page
            page.goto(project_url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_load_state("domcontentloaded", 60_000)
            response = context.request.get(
                CHATGPT_AUTH_SESSION_URL,
                timeout=60_000,
                headers={"Accept": "application/json", "Referer": project_url},
            )
            payload = _parse_chatgpt_auth_response(response.ok, response.text())
    elif descriptor.engine == "chromium":
        with _serialized_sync_playwright() as playwright:
            with launch_chromium_context(
                playwright,
                descriptor,
                headless=False,
                clone_profile_first=True,
                background_window=True,
                silent=silent,
                prefer_initialized_debug_profile=prefer_initialized_debug_profile,
            ) as context:
                page = context.pages[0] if context.pages else context.new_page()
                goto_with_retry(page, project_url, attempts=2, timeout_ms=30_000)
                payload = _read_chatgpt_auth_payload(page, descriptor.label)
    else:
        return {
            "logged_in": False,
            "can_download": False,
            "account_name": "ChatGPT account",
            "message": f"ChatGPT sync does not support {descriptor.label}.",
        }

    return _chatgpt_status_payload(descriptor.label, payload)


def _zhihu_status_payload(browser_label: str, payload: object) -> dict[str, Any]:
    """Convert Zhihu's current-account JSON into browser readiness."""

    account = payload if isinstance(payload, dict) else {}
    author_token = str(account.get("url_token") or "").strip()
    account_id = str(account.get("id") or "").strip()
    if not account_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", author_token):
        return {
            "logged_in": False,
            "can_download": False,
            "account_name": "",
            "message": f"{browser_label} is not signed in to Zhihu.",
        }
    display_name = str(account.get("name") or author_token).replace("\x00", "").strip()
    return {
        "logged_in": True,
        "can_download": True,
        "account_name": display_name or author_token,
        "account_handle": author_token,
        "message": f"The Zhihu account @{author_token} is ready in {browser_label}.",
    }


def _probe_zhihu_session(
    descriptor: BrowserDescriptor,
    *,
    silent: bool = False,
    prefer_initialized_debug_profile: bool = False,
) -> dict[str, Any]:
    """Verify Zhihu sign-in without reading credentials or changing account state."""

    if descriptor.engine != "chromium":
        return {
            "logged_in": False,
            "can_download": False,
            "account_name": "",
            "message": f"Zhihu answer caching requires Edge or Chrome, not {descriptor.label}.",
        }
    with _serialized_sync_playwright() as playwright:
        with launch_chromium_context(
            playwright,
            descriptor,
            headless=False,
            clone_profile_first=True,
            background_window=True,
            silent=silent,
            prefer_initialized_debug_profile=prefer_initialized_debug_profile,
        ) as context:
            page = context.pages[0] if context.pages else context.new_page()
            goto_with_retry(page, ZHIHU_HOME_URL, attempts=2, timeout_ms=60_000)
            result = page.evaluate(
                """
                async ({url}) => {
                  try {
                    const response = await fetch(url, {
                      method: "GET",
                      credentials: "include",
                      headers: {Accept: "application/json"},
                    });
                    const text = await response.text();
                    return {status: response.status, text: text.slice(0, 1_000_001)};
                  } catch (error) {
                    return {status: 0, text: "", error: String(error)};
                  }
                }
                """,
                {"url": ZHIHU_AUTH_SESSION_URL},
            )
    if not isinstance(result, dict):
        return _zhihu_status_payload(descriptor.label, {})
    try:
        status = int(result.get("status") or 0)
    except (TypeError, ValueError):
        status = 0
    try:
        payload = json.loads(str(result.get("text") or "{}"))
    except json.JSONDecodeError:
        payload = {}
    if status < 200 or status >= 300:
        payload = {}
    return _zhihu_status_payload(descriptor.label, payload)


def _probe_safari_x_session(descriptor: BrowserDescriptor) -> dict[str, Any]:
    """Probe an X session from Safari by reading the rendered page source."""
    account_handle = detect_safari_x_account_handle()
    home_source = ""
    if not account_handle:
        home_snapshot = fetch_safari_page_snapshot(X_HOME_URL)
        home_source = home_snapshot["source"]
        account_handle = extract_x_account_from_source(home_source)
    if account_handle:
        return {
            "logged_in": True,
            "can_download": True,
            "account_name": f"@{account_handle}",
            "message": f"Safari is signed in to X as @{account_handle}.",
        }

    lowered_home_source = home_source.lower()
    if any(marker.lower() in lowered_home_source for marker in X_LOGGED_OUT_SOURCE_MARKERS):
        return {
            "logged_in": False,
            "can_download": False,
            "account_name": "",
            "message": "Safari is not signed in to X.",
        }

    inferred_handle = extract_json_string_field(fetch_safari_page_snapshot(GROK_FILES_URL)["source"], "xUsername")
    if inferred_handle:
        return {
            "logged_in": True,
            "can_download": True,
            "account_name": f"@{inferred_handle}",
            "message": f"Safari X account inferred from the linked Grok session as @{inferred_handle}.",
        }

    return {
        "logged_in": False,
        "can_download": False,
        "account_name": "",
        "message": "Safari did not expose a verifiable X account handle from page source.",
    }


def _probe_safari_grok_session(descriptor: BrowserDescriptor) -> dict[str, Any]:
    """Probe a Grok session from Safari by reading the rendered page source."""
    for _attempt in range(2):
        safari_snapshot = fetch_safari_page_snapshot(GROK_FILES_URL, wait_seconds=10)
        account_name = parse_grok_account_label(safari_snapshot["source"])
        if account_name:
            return {
                "logged_in": True,
                "can_download": True,
                "account_name": account_name,
                "message": "Safari is ready to sync Grok.",
            }

    inferred_handle = extract_x_account_from_source(fetch_safari_page_snapshot(X_HOME_URL, wait_seconds=10)["source"])
    if inferred_handle:
        return {
            "logged_in": True,
            "can_download": True,
            "account_name": f"@{inferred_handle}",
            "message": "Safari is ready to sync Grok using the linked X session.",
        }

    return {
        "logged_in": False,
        "can_download": False,
        "account_name": "",
        "message": "Safari is not signed in to Grok, or Grok did not expose the current account in page source.",
    }


def sync_playwright_or_error():
    """Return sync_playwright when the dependency is available."""
    if sync_playwright is None:
        setup_command = ".\\scripts\\setup_python.ps1" if is_windows_host() else "./scripts/setup_python.sh"
        raise RuntimeError(
            "Playwright is not installed for the current interpreter. "
            f"Run `{setup_command}` with a supported Python 3.13 or newer interpreter."
        )
    return sync_playwright()


@contextlib.contextmanager
def _serialized_sync_playwright():
    """Run one complete synchronous Playwright lifecycle at a time."""
    with _PLAYWRIGHT_LAUNCH_LOCK:
        with sync_playwright_or_error() as playwright:
            yield playwright


def wait_for_x_page_ready(page, browser_label: str) -> None:
    """Wait until the X page is usable or fail with a clear auth message."""
    deadline = time.time() + 30
    while time.time() < deadline:
        if any(page.locator(selector).count() for selector in X_READY_SELECTORS):
            page.wait_for_timeout(1_500)
            return

        body_text = page.locator("body").inner_text(timeout=5_000)
        if any(marker in body_text for marker in X_AUTH_MARKERS):
            raise RuntimeError(f"{browser_label} is not signed in to X.")

        page.wait_for_timeout(1_000)

    raise RuntimeError(f"X page did not finish loading in {browser_label}.")


def goto_with_retry(
    page,
    url: str,
    attempts: int = 3,
    timeout_ms: int = 120_000,
    *,
    should_stop: Callable[[], bool] | None = None,
) -> None:
    """Navigate with bounded transient retries that honor an optional Stop request."""
    stop_requested = should_stop or (lambda: False)
    last_error: Exception | None = None
    for attempt_index in range(1, attempts + 1):
        if stop_requested():
            return
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=max(1_000, int(timeout_ms)))
            return
        except Exception as exc:  # pragma: no cover - depends on local browser/network state
            last_error = exc
            if stop_requested():
                return
            error_text = str(exc)
            if attempt_index >= attempts or not any(marker in error_text for marker in TRANSIENT_BROWSER_ERROR_MARKERS):
                raise
            if stop_requested():
                return
            page.wait_for_timeout(1_500)
            if stop_requested():
                return
    if last_error is not None:
        raise last_error


def _housekeep_stale_chromium_profiles(descriptor: BrowserDescriptor) -> int:
    """Remove only abandoned temporary profiles owned by this application."""
    temp_root = Path(tempfile.gettempdir())
    profile_prefix = f"cachelikes-{descriptor.browser_id}-"
    try:
        candidates = tuple(temp_root.iterdir())
    except OSError as exc:
        LOGGER.warning("Could not inspect Chromium temporary profiles in %s: %s", temp_root, exc)
        return 0

    now = time.time()
    removed = 0
    for candidate in candidates:
        if (
            not candidate.name.startswith(profile_prefix)
            or candidate.is_symlink()
            or not candidate.is_dir()
            or candidate in _ACTIVE_CHROMIUM_PROFILE_ROOTS
        ):
            continue
        try:
            age = now - candidate.stat().st_mtime
        except OSError:
            continue
        if age < CHROMIUM_TEMP_PROFILE_STALE_AFTER_SECONDS:
            continue
        try:
            shutil.rmtree(candidate)
        except OSError as exc:
            LOGGER.warning("Could not remove stale Chromium temporary profile %s: %s", candidate, exc)
        else:
            removed += 1

    if removed:
        LOGGER.info(
            "Removed %d stale %s Chromium temporary profile(s).",
            removed,
            descriptor.label,
        )
    return removed


def _cleanup_cloned_browser_profile(
    temp_profile_dir: tempfile.TemporaryDirectory[str],
    *,
    original_error: BaseException | None = None,
) -> None:
    """Report retained profiles without replacing an existing task or launch error."""
    profile_root = Path(temp_profile_dir.name)
    try:
        temp_profile_dir.cleanup()
    except OSError as exc:
        message = (
            f"Could not remove the task's temporary browser profile at {profile_root}. "
            "It remains protected from this process's stale-profile cleanup; "
            "check for remaining task-owned browser processes before removing it."
        )
        LOGGER.warning("%s %s", message, exc)
        if original_error is not None:
            original_error.add_note(message)
            return
        raise RuntimeError(message) from exc
    _ACTIVE_CHROMIUM_PROFILE_ROOTS.discard(profile_root)


def _is_idempotent_chromium_context_close_error(error: Exception) -> bool:
    """Return whether Playwright reports that the managed context is already closed."""
    normalized_error = " ".join(str(error or "").casefold().split())
    return any(
        marker in normalized_error
        for marker in IDEMPOTENT_CHROMIUM_CONTEXT_CLOSE_ERROR_MARKERS
    )


def _is_browser_running_copy_error(error: BaseException) -> bool:
    """Return whether a profile copy failed because the host browser holds its login cookies.

    On Windows a running Chrome or Edge keeps ``Network/Cookies`` under an exclusive
    SQLite lock, so ``shutil.copytree`` aggregates a ``WinError 32`` sharing
    violation into a ``shutil.Error``. That error is an ``OSError`` subclass but not
    a ``PermissionError``, so it bypasses the friendlier message below unless we
    recognize it explicitly. We match on the locked file ending in ``Cookies`` to
    avoid mistaking unrelated transient copy errors for a running-browser lock.
    """
    lock_markers = ("winerror 32", "being used by another process")

    def message_indicates_lock(message: object) -> bool:
        normalized = str(message or "").casefold()
        return any(marker in normalized for marker in lock_markers)

    def source_is_cookie_file(source: object) -> bool:
        name = Path(str(source or "")).name.lower()
        return name == "cookies"

    if isinstance(error, shutil.Error):
        failures = error.args[0] if error.args else []
        for entry in failures:
            try:
                source, _destination, message = entry
            except (TypeError, ValueError):
                continue
            if message_indicates_lock(message) and source_is_cookie_file(source):
                return True
        return False

    if isinstance(error, PermissionError) and getattr(error, "winerror", None) == 32:
        denied_path = getattr(error, "filename", None) or ""
        return source_is_cookie_file(denied_path)

    return False


def select_provider_tab(
    context: Any,
    *,
    home_url: str,
    hosts: set[str] | frozenset[str],
    title: str = "",
) -> Any:
    """Reuse an existing provider tab by id, exact URL, and title.

    Catalog discovery must never call bring_to_front. Matching prefers an exact
    URL, then an exact title on the provider host, then any provider-host tab.
    """
    pages = [page for page in list(getattr(context, "pages", None) or [])]
    exact_url: list[Any] = []
    title_matches: list[Any] = []
    host_matches: list[Any] = []
    wanted_url = str(home_url or "").strip().rstrip("/")
    wanted_hosts = {str(host or "").strip().lower() for host in hosts if str(host or "").strip()}
    wanted_title = str(title or "").strip()

    for page in pages:
        is_closed = getattr(page, "is_closed", None)
        if callable(is_closed):
            try:
                if is_closed():
                    continue
            except Exception:
                continue
        url = str(getattr(page, "url", "") or "").strip()
        host = (urlsplit(url).hostname or "").lower()
        if host not in wanted_hosts:
            continue
        page_title = ""
        title_fn = getattr(page, "title", None)
        if callable(title_fn):
            try:
                page_title = str(title_fn() or "").strip()
            except Exception:
                page_title = ""
        if wanted_url and url.rstrip("/") == wanted_url:
            exact_url.append(page)
        elif wanted_title and page_title == wanted_title:
            title_matches.append(page)
        else:
            host_matches.append(page)

    chosen = (exact_url or title_matches or host_matches or [None])[0]
    if chosen is None:
        new_page = getattr(context, "new_page", None)
        if callable(new_page):
            return new_page()
        if pages:
            return pages[0]
        raise RuntimeError("The browser context has no pages for source discovery.")
    return chosen


class _CdpAttachContext:
    """Wrap a CDP-attached browser/context so callers can use it like a launch.

    The underlying debug browser process is intentionally left running after the
    context manager exits; closing only drops the Playwright CDP connection so
    the next request can reattach to the same authenticated session.
    """

    def __init__(self, browser: Any, context: Any, lock: Any | None = None) -> None:
        self._browser = browser
        self._context = context
        self._lock = lock

    @property
    def pages(self) -> Any:
        return self._context.pages

    def new_page(self, *args: Any, **kwargs: Any) -> Any:
        return self._context.new_page(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._context, name)

    def __enter__(self) -> "_CdpAttachContext":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        try:
            self._browser.close()
        except Exception as close_error:
            if not _is_idempotent_chromium_context_close_error(close_error):
                LOGGER.warning("Closing the CDP attach connection failed: %s", close_error)
        finally:
            lock = self._lock
            self._lock = None
            if lock is not None:
                lock.release()
        return False


def launch_chromium_context(
    playwright,
    descriptor: BrowserDescriptor,
    headless: bool,
    clone_profile_first: bool = True,
    background_window: bool = True,
    silent: bool = False,
    window_mode: str | None = None,
    *,
    allow_cdp_attach: bool = True,
    prefer_initialized_debug_profile: bool = False,
):
    """Launch an isolated Chromium-family browser with an explicit window mode.

    On Windows, Agent callers can explicitly prefer an initialized project debug
    profile and restart or reuse it over CDP. Other callers retain the existing
    clone-first behavior. A locked daily profile can still trigger the existing
    debug-browser fallback. Neither CDP path reads locked cookie files or opens
    the daily profile for writing.
    """
    user_data_dir = descriptor.user_data_dir
    if user_data_dir is None:
        raise RuntimeError(f"{descriptor.label} does not expose a Chromium profile directory.")

    temp_profile_dir: tempfile.TemporaryDirectory[str] | None = None
    if window_mode is None:
        window_mode = (
            CHROMIUM_WINDOW_MODE_TASK_STAGE
            if silent and not headless and is_macos_host()
            else CHROMIUM_WINDOW_MODE_OFFSCREEN
        )

    def do_launch(target_user_data_dir: Path):
        effective_headless = headless
        effective_background_window = background_window or (
            silent
            and descriptor.browser_id in {"edge", "chrome"}
            and window_mode == CHROMIUM_WINDOW_MODE_OFFSCREEN
        )
        from .computer_use_agent import (
            _capture_macos_frontmost_application,
            _restore_macos_frontmost_application_after_task_stage,
        )

        task_stage = is_macos_host() and window_mode == CHROMIUM_WINDOW_MODE_TASK_STAGE
        previous = _capture_macos_frontmost_application() if task_stage else ""
        try:
            return playwright.chromium.launch_persistent_context(
                user_data_dir=str(target_user_data_dir),
                channel=descriptor.channel,
                headless=effective_headless,
                args=build_chromium_launch_args(
                    descriptor,
                    background_window=effective_background_window,
                    window_mode=window_mode,
                ),
                ignore_default_args=["--use-mock-keychain", "--password-store=basic"],
                viewport={"width": 1440, "height": 1200},
            )
        finally:
            if task_stage:
                _restore_macos_frontmost_application_after_task_stage(
                    previous,
                    "Google Chrome" if descriptor.browser_id == "chrome" else "Microsoft Edge",
                )

    def should_retry_with_cloned_profile(error_text: str) -> bool:
        normalized_error = str(error_text or "")
        retry_markers = (
            "ProcessSingleton",
            "SingletonLock",
            "SingletonSocket",
            "non-default data directory",
            "DevTools remote debugging requires a non-default data directory",
        )
        return any(marker in normalized_error for marker in retry_markers)

    def attach_debug_browser() -> Any:
        """Attach while serializing the complete shared debug-browser use."""
        from .agent_debug_browser import _acquire_debug_browser_lock, ensure_debug_browser

        browser_id = descriptor.browser_id
        lock = _acquire_debug_browser_lock(browser_id)
        try:
            endpoint = ensure_debug_browser(browser_id).cdp_endpoint
            browser = playwright.chromium.connect_over_cdp(endpoint)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
        except BaseException:
            lock.release()
            raise
        return _CdpAttachContext(browser, context, lock=lock)

    if (
        allow_cdp_attach
        and prefer_initialized_debug_profile
        and is_windows_host()
    ):
        from .agent_debug_browser import debug_browser_profile_initialized

        if debug_browser_profile_initialized(descriptor.browser_id):
            LOGGER.info(
                "Using the initialized project debug %s over CDP.",
                descriptor.label,
            )
            return attach_debug_browser()

    if not user_data_dir.exists():
        raise RuntimeError(f"{descriptor.label} user data directory was not found: {user_data_dir}")

    if clone_profile_first:
        try:
            temp_user_data_dir, temp_profile_dir = clone_browser_profile(descriptor)
        except RuntimeError as exc:
            if allow_cdp_attach and is_windows_host() and _is_browser_running_copy_error(exc.__cause__ or exc):
                LOGGER.info(
                    "%s keeps its sign-in cookies locked; attaching the project debug browser over CDP.",
                    descriptor.label,
                )
                return attach_debug_browser()
            raise
        try:
            context = do_launch(temp_user_data_dir)
        except Exception as exc:
            _cleanup_cloned_browser_profile(temp_profile_dir, original_error=exc)
            raise
    else:
        try:
            context = do_launch(user_data_dir)
        except PlaywrightError as exc:
            error_text = str(exc)
            if not should_retry_with_cloned_profile(error_text):
                raise
            try:
                temp_user_data_dir, temp_profile_dir = clone_browser_profile(descriptor)
            except RuntimeError as clone_exc:
                if allow_cdp_attach and is_windows_host() and _is_browser_running_copy_error(clone_exc.__cause__ or clone_exc):
                    LOGGER.info(
                        "%s keeps its sign-in cookies locked; attaching the project debug browser over CDP.",
                        descriptor.label,
                    )
                    return attach_debug_browser()
                raise
            try:
                context = do_launch(temp_user_data_dir)
            except Exception as exc:
                _cleanup_cloned_browser_profile(temp_profile_dir, original_error=exc)
                raise

    if temp_profile_dir is None:
        return contextlib.closing(context)

    class ManagedContext:
        def __enter__(self_nonlocal):
            return context

        def __exit__(self_nonlocal, exc_type, exc, tb):
            primary_error = exc
            try:
                try:
                    context.close()
                except Exception as close_error:
                    if not _is_idempotent_chromium_context_close_error(close_error):
                        if primary_error is None:
                            primary_error = close_error
                            raise
                        primary_error.add_note(f"Browser context cleanup also failed: {close_error}")
                        LOGGER.warning("Browser context cleanup also failed: %s", close_error)
                    else:
                        LOGGER.info("Chromium context was already closed during cleanup.")
            finally:
                _cleanup_cloned_browser_profile(temp_profile_dir, original_error=primary_error)
            return False

    return ManagedContext()


def build_chromium_launch_args(
    descriptor: BrowserDescriptor,
    background_window: bool = True,
    window_mode: str = CHROMIUM_WINDOW_MODE_OFFSCREEN,
) -> list[str]:
    """Build Chromium launch arguments for an isolated background or task-stage window."""
    args = [f"--profile-directory={descriptor.profile_directory}"]
    window_args_by_mode = {
        CHROMIUM_WINDOW_MODE_OFFSCREEN: BACKGROUND_CHROMIUM_WINDOW_ARGS,
        CHROMIUM_WINDOW_MODE_TASK_STAGE: TASK_STAGE_CHROMIUM_WINDOW_ARGS,
    }
    try:
        window_args = window_args_by_mode[window_mode]
    except KeyError as exc:
        raise ValueError(f"Unsupported Chromium window mode: {window_mode}") from exc
    if background_window:
        args.extend(window_args)
    return args


def clone_browser_profile(descriptor: BrowserDescriptor) -> tuple[Path, tempfile.TemporaryDirectory[str]]:
    """Clone one Chromium browser profile to avoid singleton locks."""
    source_user_data_dir = descriptor.user_data_dir
    if source_user_data_dir is None:
        raise RuntimeError(f"{descriptor.label} does not expose a clonable profile.")

    source_profile_dir = source_user_data_dir / descriptor.profile_directory
    if not source_profile_dir.exists():
        raise RuntimeError(f"{descriptor.label} profile directory was not found: {source_profile_dir}")

    _housekeep_stale_chromium_profiles(descriptor)
    temp_dir = tempfile.TemporaryDirectory(prefix=f"cachelikes-{descriptor.browser_id}-")
    temp_root = Path(temp_dir.name)
    _ACTIVE_CHROMIUM_PROFILE_ROOTS.add(temp_root)
    target_user_data_dir = temp_root / f"{descriptor.label.replace(' ', '')}UserData"
    target_profile_dir = target_user_data_dir / descriptor.profile_directory

    def ignore_transient_files(_directory: str, names: list[str]) -> set[str]:
        ignored = {
            "SingletonCookie",
            "SingletonLock",
            "SingletonSocket",
            "lockfile",
        }
        ignored.update(name for name in names if name.endswith(".lock"))
        return ignored

    try:
        target_user_data_dir.mkdir(parents=True, exist_ok=True)
        local_state = source_user_data_dir / "Local State"
        if local_state.exists():
            local_state_target = target_user_data_dir / "Local State"
            try:
                local_state_target.write_bytes(local_state.read_bytes())
            except PermissionError:
                if is_windows_host():
                    raise
                LOGGER.warning(
                    "%s denied access to %s; continuing with the readable %s profile directory.",
                    "macOS" if is_macos_host() else "The host",
                    local_state,
                    source_profile_dir,
                )
        shutil.copytree(source_profile_dir, target_profile_dir, dirs_exist_ok=True, ignore=ignore_transient_files)
    except PermissionError as exc:
        denied_path = getattr(exc, "filename", None) or source_profile_dir
        _cleanup_cloned_browser_profile(temp_dir, original_error=exc)
        if is_macos_host():
            raise RuntimeError(
                f"macOS denied access to the {descriptor.label} profile at {denied_path}. "
                "Open System Settings > Privacy & Security > Full Disk Access and enable "
                "the Python 3.13 or 3.14 runtime used by agenticContext, then restart the cache service."
            ) from exc
        if is_windows_host():
            raise RuntimeError(
                f"Windows denied access to the {descriptor.label} profile at {denied_path}. "
                "Close any running Edge or Chrome windows, then retry the browser session check."
            ) from exc
        raise
    except shutil.Error as exc:
        _cleanup_cloned_browser_profile(temp_dir, original_error=exc)
        if is_windows_host() and _is_browser_running_copy_error(exc):
            raise RuntimeError(
                f"{descriptor.label} keeps its sign-in cookies locked while it is running. "
                "Close all Edge or Chrome windows, then retry the browser session check."
            ) from exc
        raise
    except OSError as exc:
        _cleanup_cloned_browser_profile(temp_dir, original_error=exc)
        raise
    return target_user_data_dir, temp_dir


def parse_grok_account_label(html: str) -> str:
    """Extract a user-facing Grok account label from injected page data."""
    if not html:
        return ""

    given_name = extract_json_string_field(html, "givenName")
    x_username = extract_json_string_field(html, "xUsername")
    email = extract_json_string_field(html, "email")
    user_id = extract_json_string_field(html, "userId")

    if given_name and x_username:
        return f"{given_name} (@{x_username})"
    if given_name:
        return given_name
    if x_username:
        return f"@{x_username}"
    if email:
        return email
    if user_id:
        return f"User {user_id[:8]}"
    return ""


def is_grok_security_verification_page(title: str, body_text: str, html: str = "") -> bool:
    """Return whether the loaded Grok page is a security verification interstitial."""
    normalized_title = (title or "").strip().lower()
    normalized_body = (body_text or "").strip().lower()
    normalized_html = (html or "").strip().lower()

    if any(marker in normalized_title for marker in GROK_SECURITY_CHALLENGE_TITLE_MARKERS):
        return True

    marker_hits = 0
    for marker in GROK_SECURITY_CHALLENGE_BODY_MARKERS:
        if marker in normalized_body or marker in normalized_html:
            marker_hits += 1
    return marker_hits >= 2


def extract_json_string_field(text: str, field_name: str) -> str:
    """Extract one JSON string field from page source and decode escapes."""
    patterns = (
        rf'"{re.escape(field_name)}":"((?:[^"\\\\]|\\\\.)*)"',
        rf'\\"{re.escape(field_name)}\\":\\"((?:[^"\\\\]|\\\\.)*)\\"',
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return decode_js_string(match.group(1))
    return ""


def decode_js_string(value: str) -> str:
    """Decode one JavaScript JSON string literal payload."""
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value


def extract_x_account_from_source(source: str) -> str:
    """Try to infer the signed-in X account handle from page source."""
    patterns = (
        r'"screen_name":"([A-Za-z0-9_]{1,30})"',
        r'"screenName":"([A-Za-z0-9_]{1,30})"',
        r'"userName":"([A-Za-z0-9_]{1,30})"',
        r'"handle":"([A-Za-z0-9_]{1,30})"',
    )
    for pattern in patterns:
        match = re.search(pattern, source)
        if match:
            handle = match.group(1).strip()
            if handle and handle.lower() not in {"twitter", "x"}:
                return handle
    return ""


def detect_safari_x_account_handle(wait_seconds: int = 10) -> str:
    """Read Safari's signed-in X profile link before falling back to page source."""
    extract_handle_js = """
() => {
    const profileLinks = [
        document.querySelector('a[data-testid="AppTabBar_Profile_Link"]'),
        ...document.querySelectorAll('a[href$="/likes"], a[href*="/likes?"]'),
    ].filter(Boolean);
    for (const link of profileLinks) {
        const match = String(link.href || '').match(
            /^https?:\\/\\/(?:www\\.)?(?:x|twitter)\\.com\\/([A-Za-z0-9_]{1,15})(?:[\\/?#]|$)/i,
        );
        if (match && !['home', 'i', 'settings'].includes(match[1].toLowerCase())) {
            return match[1];
        }
    }
    return '';
}
""".strip()
    with SafariContext(X_HOME_URL) as context:
        page = context.primary_page
        page.wait_for_timeout(max(0, int(wait_seconds)) * 1_000)
        handle = page.evaluate(extract_handle_js)

    handle = str(handle or "").strip().lstrip("@")
    if re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle) and handle.lower() not in {"home", "i", "settings"}:
        return handle
    return ""


def fetch_safari_page_snapshot(url: str, wait_seconds: int = 8) -> dict[str, str]:
    """Open one URL in Safari, capture the page source, and close the temporary tab."""
    with SafariContext(url) as context:
        page = context.primary_page
        page.wait_for_timeout(max(0, int(wait_seconds)) * 1_000)
        return {
            "url": page.url,
            "source": page.content(limit=SAFARI_APPLESCRIPT_SOURCE_LIMIT),
        }
