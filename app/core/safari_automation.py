"""Minimal Safari automation primitives backed by Apple Events."""

# Code version: v2.11.3-codex.0

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import re
import secrets
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import urlsplit

from .platform_lock import lock_file, unlock_file


SAFARI_DOWNLOAD_RANGE_BYTES = 512 * 1024
SAFARI_BASE64_SLICE_BYTES = 96 * 1024
SAFARI_RESPONSE_TEXT_SLICE_CHARS = 96 * 1024
SAFARI_POLL_INTERVAL_SECONDS = 0.2
SAFARI_APPLESCRIPT_RETRY_LIMIT = 2
SAFARI_APPLESCRIPT_RETRY_DELAY_SECONDS = 0.25
SAFARI_APPLESCRIPT_TIMEOUT_SECONDS = 20.0
SAFARI_NAVIGATION_RETRY_LIMIT = 3
SAFARI_NAVIGATION_RETRY_DELAY_SECONDS = 0.5
SAFARI_WRONG_PAGE_GRACE_SECONDS = 1.0
SAFARI_CLOSE_RETRY_LIMIT = 3
SAFARI_NATIVE_FOCUS_POLL_ATTEMPTS = 20
SAFARI_NATIVE_FOCUS_POLL_DELAY_SECONDS = 0.05
SAFARI_JAVASCRIPT_RETRY_LIMIT = 2
SAFARI_JAVASCRIPT_ARGUMENT_INLINE_LIMIT = 1_200
SAFARI_JAVASCRIPT_ARGUMENT_CHUNK_SIZE = 1_000
SAFARI_WINDOW_CREATION_LOCK = RLock()
SAFARI_WINDOW_CREATION_LOCK_PATH = Path(tempfile.gettempdir()) / "cachelikes-safari-window-creation.lock"
SAFARI_CONTEXT_LOCK_PATH = Path(tempfile.gettempdir()) / "cachelikes-safari-context.lock"
SAFARI_CONTEXT_LEASE_VERSION = 2
SAFARI_CONTEXT_LEASE_MAX_BYTES = 4_096
SAFARI_CONTEXT_CREATION_SETTLE_SECONDS = SAFARI_APPLESCRIPT_TIMEOUT_SECONDS * 2
SAFARI_CONTEXT_INVENTORY_STABILITY_SECONDS = 0.5
SAFARI_OWNED_WINDOW_REMAINS_MARKER = "SAFARI_OWNED_WINDOW_REMAINS:"
SAFARI_NATIVE_INPUT_LOCK = RLock()
SAFARI_NATIVE_INPUT_LOCK_PATH = Path(tempfile.gettempdir()) / "cachelikes-safari-native-input.lock"
SAFARI_PENDING_CONTEXT_LOCK = RLock()
SAFARI_PENDING_CONTEXTS: dict[int, "SafariContext"] = {}
SAFARI_NATIVE_INPUT_HOSTS = frozenset(
    {
        "chatgpt.com",
        "www.chatgpt.com",
        "grok.com",
        "www.grok.com",
        "gemini.google.com",
        "claude.ai",
        "www.claude.ai",
        "x.com",
        "www.x.com",
        "twitter.com",
        "www.twitter.com",
    }
)
SAFARI_CAPTURE_FRONT_WINDOW_APPLESCRIPT = """
set previousFrontmostProcessName to ""
tell application "System Events"
    try
        set previousFrontmostProcessName to name of first application process whose frontmost is true
    end try
end tell
set previousWindowId to 0
set previousWindowWasVisible to false
set previousWindowWasMiniaturized to false
if (count of windows) > 0 then
    set previousWindowId to id of front window
    try
        set previousWindowWasVisible to visible of front window
    end try
    try
        set previousWindowWasMiniaturized to miniaturized of front window
    end try
end if
""".strip()
SAFARI_KEEP_WINDOW_AVAILABLE_APPLESCRIPT = """
try
    set miniaturized of targetWindow to false
end try
try
    set visible of targetWindow to true
end try
""".strip()
SAFARI_RESTORE_FRONT_WINDOW_APPLESCRIPT = """
if previousWindowId is not 0 and previousWindowWasVisible and not previousWindowWasMiniaturized then
    try
        set index of (first window whose id is previousWindowId) to 1
    end try
end if
if previousFrontmostProcessName is not "" and previousFrontmostProcessName is not "Safari" then
    tell application "System Events"
        try
            set frontmost of process previousFrontmostProcessName to true
        end try
    end tell
end if
""".strip()
SAFARI_RESTORE_FRONT_WINDOW_IF_TARGET_STILL_FRONT_APPLESCRIPT = """
set currentFrontmostProcessName to ""
tell application "System Events"
    try
        set currentFrontmostProcessName to name of first application process whose frontmost is true
    end try
end tell
set targetWindowStillFront to false
if currentFrontmostProcessName is "Safari" then
    try
        set targetWindowStillFront to (id of front window) is (id of targetWindow)
    end try
end if
set canRestorePreviousSafariWindow to (currentFrontmostProcessName is not "Safari") or targetWindowStillFront
if canRestorePreviousSafariWindow and previousWindowId is not 0 and previousWindowWasVisible and not previousWindowWasMiniaturized then
    try
        set index of (first window whose id is previousWindowId) to 1
    end try
end if
if targetWindowStillFront and previousFrontmostProcessName is not "" and previousFrontmostProcessName is not "Safari" then
    tell application "System Events"
        try
            set frontmost of process previousFrontmostProcessName to true
        end try
    end tell
end if
""".strip()
SAFARI_RESTORE_FRONT_APP_IF_STILL_SAFARI_APPLESCRIPT = """
set shouldRestoreClosedWindowFocus to false
tell application "System Events"
    try
        set shouldRestoreClosedWindowFocus to (name of first application process whose frontmost is true) is "Safari"
    end try
end tell
if shouldRestoreClosedWindowFocus then
    if previousWindowId is not 0 and previousWindowWasVisible and not previousWindowWasMiniaturized then
        try
            set index of (first window whose id is previousWindowId) to 1
        end try
    end if
    if previousFrontmostProcessName is not "" and previousFrontmostProcessName is not "Safari" then
        tell application "System Events"
            try
                set frontmost of process previousFrontmostProcessName to true
            end try
        end tell
    end if
end if
""".strip()
SAFARI_BACKGROUND_WINDOW_APPLESCRIPT = (
    f"{SAFARI_KEEP_WINDOW_AVAILABLE_APPLESCRIPT}\n"
    f"{SAFARI_RESTORE_FRONT_WINDOW_IF_TARGET_STILL_FRONT_APPLESCRIPT}"
)
SAFARI_WAIT_FOR_NATIVE_FOCUS_APPLESCRIPT = f"""
set current tab of targetWindow to targetTab
{SAFARI_KEEP_WINDOW_AVAILABLE_APPLESCRIPT}
set index of targetWindow to 1
activate
set nativeFocusReady to false
repeat with focusPollIndex from 1 to {SAFARI_NATIVE_FOCUS_POLL_ATTEMPTS}
    set currentFrontmostProcessName to ""
    tell application "System Events"
        try
            set currentFrontmostProcessName to name of first application process whose frontmost is true
        end try
    end tell
    if currentFrontmostProcessName is "Safari" then
        if (id of front window) is (id of targetWindow) then
            if (current tab of targetWindow) is targetTab then
                set documentFocused to false
                try
                    set documentFocused to do JavaScript "document.hasFocus()" in targetTab
                end try
                if documentFocused then
                    set nativeFocusReady to true
                    exit repeat
                end if
            end if
        end if
    end if
    delay {SAFARI_NATIVE_FOCUS_POLL_DELAY_SECONDS:g}
end repeat
if not nativeFocusReady then error "Safari native activation refused: document-unfocused"
""".strip()


logger = logging.getLogger(__name__)


class SafariNativeActivationError(RuntimeError):
    """Describe whether a failed trusted activation may already have reached Safari."""

    def __init__(self, message: str, *, input_attempted: bool) -> None:
        super().__init__(message)
        self.input_attempted = bool(input_attempted)


def is_missing_safari_window_error(error: BaseException) -> bool:
    """Return whether Safari rejected an operation for a window that vanished."""
    message = str(error).lower()
    return (
        ("invalid index" in message and ("window" in message or "tab" in message))
        or "can't get current tab" in message
        or "can’t get current tab" in message
        or "can't get window" in message
        or "can’t get window" in message
    )


def safari_navigation_matches(target_url: str, current_url: str) -> bool:
    """Return whether Safari reached the requested page or its authentication flow."""
    normalized_target = str(target_url or "").strip()
    normalized_current = str(current_url or "").strip()
    if not normalized_target or not normalized_current:
        return False
    if normalized_target in {"about:blank", "favorites://"}:
        return normalized_current == normalized_target

    target = urlsplit(normalized_target)
    current = urlsplit(normalized_current)
    if target.scheme not in {"http", "https"}:
        return normalized_current == normalized_target
    if current.scheme not in {"http", "https"} or current.scheme != target.scheme:
        return False

    target_host = target.netloc.casefold()
    current_host = current.netloc.casefold()
    target_path = target.path.rstrip("/") or "/"
    current_path = current.path.rstrip("/") or "/"
    if current_host == target_host and current_path == target_path:
        return True

    authentication_hosts = {"auth.openai.com", "auth0.openai.com"}
    chatgpt_hosts = {"chatgpt.com", "www.chatgpt.com"}
    authentication_paths = ("/auth", "/login", "/i/flow/login", "/account/login")
    return (
        (target.hostname or "").casefold() in chatgpt_hosts
        and (current.hostname or "").casefold() in authentication_hosts
        and current.port in {None, 443}
        and not current.username
        and not current.password
    ) or (
        current_host == target_host and current_path.startswith(authentication_paths)
    )


def escape_applescript_text(value: str) -> str:
    """Escape text embedded in an AppleScript string literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def run_applescript(source: str, *, retry_transient: bool = True) -> str:
    """Run one AppleScript program and return its standard output."""
    last_error = ""
    retry_limit = SAFARI_APPLESCRIPT_RETRY_LIMIT if retry_transient else 0
    for attempt_index in range(retry_limit + 1):
        try:
            process = subprocess.run(
                ["osascript"],
                input=source,
                text=True,
                capture_output=True,
                check=False,
                timeout=SAFARI_APPLESCRIPT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            last_error = (
                f"Safari automation timed out after "
                f"{SAFARI_APPLESCRIPT_TIMEOUT_SECONDS:g} seconds."
            )
            if attempt_index >= retry_limit:
                break
            time.sleep(SAFARI_APPLESCRIPT_RETRY_DELAY_SECONDS * (attempt_index + 1))
            continue
        if process.returncode == 0:
            return (process.stdout or "").rstrip("\n")
        last_error = (process.stderr or process.stdout or "").strip()
        if attempt_index >= retry_limit:
            break
        lowered = last_error.lower()
        if SAFARI_OWNED_WINDOW_REMAINS_MARKER.lower() in lowered:
            break
        if not any(
            marker in lowered
            for marker in ("-1712", "-1719", "-609", "-600", "timed out", "connection is invalid")
        ):
            break
        time.sleep(SAFARI_APPLESCRIPT_RETRY_DELAY_SECONDS * (attempt_index + 1))
    raise RuntimeError(last_error or "Safari automation failed.")


def _safari_window_inventory() -> dict[int, int]:
    """Return Safari window IDs and tab counts without launching or activating it."""
    raw = run_applescript(
        """
tell application "System Events"
    set safariIsRunning to exists application process "Safari"
end tell
if not safariIsRunning then return "not-running"
tell application "Safari"
    set safariWindowRows to {}
    repeat with candidateWindow in every window
        set end of safariWindowRows to ((id of candidateWindow) as text) & ":" & ((count of tabs of candidateWindow) as text)
    end repeat
end tell
set previousDelimiters to AppleScript's text item delimiters
set AppleScript's text item delimiters to linefeed
set serializedWindowRows to safariWindowRows as text
set AppleScript's text item delimiters to previousDelimiters
return "windows:" & serializedWindowRows
""".strip(),
        retry_transient=False,
    ).strip()
    if raw == "not-running":
        return {}
    if not raw.startswith("windows:"):
        raise RuntimeError("Safari returned an invalid window inventory.")
    payload = raw.removeprefix("windows:").strip()
    if not payload:
        return {}
    inventory: dict[int, int] = {}
    for row in payload.splitlines():
        window_id, separator, tab_count = row.strip().partition(":")
        if not separator or not window_id.isdigit() or not tab_count.isdigit():
            raise RuntimeError("Safari returned an invalid window inventory row.")
        inventory[int(window_id)] = int(tab_count)
    return inventory


def _safari_pid_is_alive(pid: int) -> bool:
    """Return whether a recorded Safari-task owner process still exists."""
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _close_safari_window_id(window_id: int) -> bool:
    """Close one leftover task window without activating Safari.

    Returns True when the window is absent afterward.
    """
    if type(window_id) is not int or window_id <= 0:
        return False
    source = f"""
tell application "System Events"
    set safariIsRunning to exists application process "Safari"
end tell
if not safariIsRunning then return "absent"
tell application "Safari"
    {SAFARI_CAPTURE_FRONT_WINDOW_APPLESCRIPT}
    if not (exists (first window whose id is {window_id})) then
        return "absent"
    end if
    try
        close (first window whose id is {window_id})
    end try
    repeat with closePollIndex from 1 to 20
        if not (exists (first window whose id is {window_id})) then exit repeat
        delay 0.1
    end repeat
    {SAFARI_RESTORE_FRONT_WINDOW_APPLESCRIPT}
    if exists (first window whose id is {window_id}) then
        return "still-open"
    end if
end tell
return "closed"
""".strip()
    with safari_window_creation_guard():
        try:
            result = run_applescript(source, retry_transient=False).strip()
        except RuntimeError as exc:
            if is_missing_safari_window_error(exc):
                return True
            logger.warning(
                "Safari could not close leftover task window %s: %s",
                window_id,
                exc,
            )
            return False
    return result in {"absent", "closed"}


def _safari_context_lease_path() -> Path:
    """Derive durable state beside the separately locked coordination file."""
    return SAFARI_CONTEXT_LOCK_PATH.with_name(
        f"{SAFARI_CONTEXT_LOCK_PATH.name}.state"
    )


@contextlib.contextmanager
def safari_window_creation_guard():
    """Serialize front-window capture across threads and local app processes."""
    with SAFARI_WINDOW_CREATION_LOCK:
        with SAFARI_WINDOW_CREATION_LOCK_PATH.open("a+") as lock_handle:
            lock_file(lock_handle)
            try:
                yield
            finally:
                unlock_file(lock_handle)


@contextlib.contextmanager
def safari_native_input_guard():
    """Serialize native Safari input across threads and local app processes."""
    with SAFARI_NATIVE_INPUT_LOCK:
        with SAFARI_NATIVE_INPUT_LOCK_PATH.open("a+") as lock_handle:
            lock_file(lock_handle)
            try:
                yield
            finally:
                unlock_file(lock_handle)


@dataclass(slots=True)
class SafariRequest:
    """Expose request headers without exporting Safari cookies."""

    headers: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class SafariResponse:
    """Expose the browser-response surface used by authenticated resolvers."""

    status: int
    body_text: str
    headers: dict[str, str] = field(default_factory=dict)
    request: SafariRequest = field(default_factory=SafariRequest)

    @property
    def ok(self) -> bool:
        """Return whether the request completed successfully."""
        return 200 <= self.status < 300

    def text(self) -> str:
        """Return the response body."""
        return self.body_text


class SafariRequestClient:
    """Issue same-origin requests inside an authenticated Safari page."""

    def __init__(self, context: SafariContext) -> None:
        self._context = context

    def get(
        self,
        url: str,
        timeout: int,
        headers: dict[str, str] | None = None,
    ) -> SafariResponse:
        """Fetch one text response inside the authenticated Safari page."""
        return self.get_from_page(
            self._context.primary_page,
            url,
            timeout,
            headers,
            serialize=True,
        )

    def get_from_page(
        self,
        page: SafariPage,
        url: str,
        timeout: int,
        headers: dict[str, str] | None = None,
        *,
        serialize: bool = False,
    ) -> SafariResponse:
        """Fetch through one owned page so separate Safari tabs can run concurrently."""
        if page.context is not self._context:
            raise RuntimeError("Safari request page belongs to a different browser context.")
        last_error: RuntimeError | None = None
        for attempt_index in range(3):
            try:
                return self._get_once(page, url, timeout, headers or {}, serialize=serialize)
            except RuntimeError as exc:
                last_error = exc
                error_text = str(exc).lower()
                retryable = any(
                    marker in error_text
                    for marker in (
                        "request state disappeared",
                        "load failed",
                        "failed to fetch",
                        "fetch is aborted",
                    )
                )
                if not retryable or attempt_index >= 2:
                    raise
                if "request state disappeared" in error_text:
                    page.wait_for_load_state(
                        "domcontentloaded",
                        min(max(1, int(timeout)), 60_000),
                    )
                if any(
                    marker in error_text
                    for marker in ("load failed", "failed to fetch", "fetch is aborted")
                ):
                    # Safari can reject fetch from a suspended document. Keep the owned
                    # window render-active in the background, then retry without stealing focus.
                    with contextlib.suppress(RuntimeError):
                        page.keep_rendering_in_background()
                time.sleep(SAFARI_APPLESCRIPT_RETRY_DELAY_SECONDS)
        raise last_error or RuntimeError("Safari request failed.")

    def request_from_page(
        self,
        page: SafariPage,
        url: str,
        timeout: int,
        headers: dict[str, str] | None = None,
        *,
        method: str = "GET",
        body: str | None = None,
        serialize: bool = False,
    ) -> SafariResponse:
        """Issue one authenticated same-origin request from an owned Safari page."""
        normalized_method = str(method or "GET").strip().upper()
        if normalized_method == "GET" and body is None:
            return self.get_from_page(
                page,
                url,
                timeout,
                headers,
                serialize=serialize,
            )
        if page.context is not self._context:
            raise RuntimeError("Safari request page belongs to a different browser context.")
        if normalized_method not in {"GET", "POST"}:
            raise ValueError("Safari browser requests support only GET or POST.")
        last_error: RuntimeError | None = None
        for attempt_index in range(3):
            try:
                return self._request_once(
                    page,
                    url,
                    timeout,
                    headers or {},
                    method=normalized_method,
                    body=body,
                    serialize=serialize,
                )
            except RuntimeError as exc:
                last_error = exc
                error_text = str(exc).lower()
                retryable = any(
                    marker in error_text
                    for marker in (
                        "request state disappeared",
                        "load failed",
                        "failed to fetch",
                        "fetch is aborted",
                    )
                )
                if not retryable or attempt_index >= 2:
                    raise
                if "request state disappeared" in error_text:
                    page.wait_for_load_state(
                        "domcontentloaded",
                        min(max(1, int(timeout)), 60_000),
                    )
                if any(
                    marker in error_text
                    for marker in ("load failed", "failed to fetch", "fetch is aborted")
                ):
                    with contextlib.suppress(RuntimeError):
                        page.keep_rendering_in_background()
                time.sleep(SAFARI_APPLESCRIPT_RETRY_DELAY_SECONDS)
        raise last_error or RuntimeError("Safari request failed.")

    def _get_once(
        self,
        page: SafariPage,
        url: str,
        timeout: int,
        headers: dict[str, str],
        *,
        serialize: bool,
    ) -> SafariResponse:
        return self._request_once(
            page,
            url,
            timeout,
            headers,
            method="GET",
            body=None,
            serialize=serialize,
        )

    def _request_once(
        self,
        page: SafariPage,
        url: str,
        timeout: int,
        headers: dict[str, str],
        *,
        method: str,
        body: str | None,
        serialize: bool,
    ) -> SafariResponse:
        request_headers, referrer = _split_fetch_headers(headers)
        timeout_ms = max(1, int(timeout))
        request_guard = self._context.request_lock if serialize else contextlib.nullcontext()
        with request_guard:
            page.evaluate(
                """(request) => {
                    let targetUrl;
                    try {
                        targetUrl = new URL(request.url, location.href);
                    } catch (_) {
                        window.__cachelikesSafariRequest = {
                            state: "failed",
                            error: "Safari request URL is invalid.",
                        };
                        return false;
                    }
                    if (targetUrl.origin !== location.origin) {
                        window.__cachelikesSafariRequest = {
                            state: "failed",
                            error: "Safari request must remain same-origin.",
                        };
                        return false;
                    }
                    const controller = new AbortController();
                    window.__cachelikesSafariRequest = {
                        state: "pending",
                        controller,
                    };
                    const timeoutId = setTimeout(() => controller.abort(), request.timeoutMs);
                    const options = {
                        method: request.method,
                        credentials: "include",
                        cache: "no-store",
                        headers: request.headers,
                        redirect: "error",
                        signal: controller.signal,
                    };
                    if (request.referrer) options.referrer = request.referrer;
                    if (request.body !== null) options.body = request.body;
                    fetch(targetUrl.href, options).then(async (response) => {
                        if (response.redirected) {
                            throw new Error("Safari response left the current origin.");
                        }
                        if (response.url) {
                            let responseUrl;
                            try {
                                responseUrl = new URL(response.url, targetUrl.href);
                            } catch (_) {
                                throw new Error("Safari returned an invalid response URL.");
                            }
                            if (responseUrl.origin !== location.origin) {
                                throw new Error("Safari response left the current origin.");
                            }
                        }
                        const responseHeaders = {};
                        response.headers.forEach((value, key) => {
                            responseHeaders[key] = value;
                        });
                        const bodyText = await response.text();
                        clearTimeout(timeoutId);
                        window.__cachelikesSafariRequest = {
                            state: "ready",
                            status: response.status,
                            headers: responseHeaders,
                            bodyText,
                        };
                    }).catch((error) => {
                        clearTimeout(timeoutId);
                        window.__cachelikesSafariRequest = {
                            state: "failed",
                            error: String(error && error.message ? error.message : error),
                        };
                    });
                    return true;
                }""",
                {
                    "url": url,
                    "method": method,
                    "body": body,
                    "timeoutMs": timeout_ms,
                    "headers": request_headers,
                    "referrer": referrer,
                },
            )

            deadline = time.monotonic() + timeout_ms / 1_000 + 5
            metadata: dict[str, Any] = {}
            cleaned_inline = False
            try:
                while time.monotonic() < deadline:
                    raw_metadata = page.evaluate(
                        """(inlineLimit) => {
                            const current = window.__cachelikesSafariRequest;
                            if (!current) return { state: "missing" };
                            const bodyLength = current.bodyText ? current.bodyText.length : 0;
                            const result = {
                                state: current.state || "missing",
                                status: current.status || 0,
                                headers: current.headers || {},
                                bodyLength,
                                error: current.error || "",
                            };
                            if (current.state === "ready" && bodyLength <= inlineLimit) {
                                result.bodyText = current.bodyText || "";
                                result.cleanedInline = true;
                                delete window.__cachelikesSafariRequest;
                            }
                            return result;
                        }""",
                        SAFARI_RESPONSE_TEXT_SLICE_CHARS,
                    )
                    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
                    state = str(metadata.get("state") or "missing")
                    if state == "ready":
                        break
                    if state == "failed":
                        raise RuntimeError(
                            f"Safari request failed: {metadata.get('error') or 'unknown error'}"
                        )
                    if state == "missing":
                        raise RuntimeError("Safari request state disappeared while the page was loading.")
                    time.sleep(SAFARI_POLL_INTERVAL_SECONDS)
                else:
                    raise RuntimeError(f"Safari request timed out after {timeout_ms:,} ms.")

                body_length = max(0, int(metadata.get("bodyLength") or 0))
                cleaned_inline = bool(metadata.get("cleanedInline"))
                body_parts: list[str] = [str(metadata.get("bodyText") or "")] if cleaned_inline else []
                slice_start = 0
                while not cleaned_inline and slice_start < body_length:
                    raw_slice = page.evaluate(
                        """(bounds) => {
                            const text = window.__cachelikesSafariRequest.bodyText || "";
                            let end = Math.min(text.length, bounds.end);
                            if (
                                end < text.length &&
                                end > bounds.start &&
                                /[\\uD800-\\uDBFF]/.test(text.charAt(end - 1))
                            ) {
                                end -= 1;
                            }
                            return { text: text.slice(bounds.start, end), end };
                        }""",
                        {
                            "start": slice_start,
                            "end": min(
                                body_length,
                                slice_start + SAFARI_RESPONSE_TEXT_SLICE_CHARS,
                            ),
                        },
                    )
                    if not isinstance(raw_slice, dict):
                        raise RuntimeError("Safari returned an invalid response text slice.")
                    slice_end = int(raw_slice.get("end") or 0)
                    if slice_end <= slice_start:
                        raise RuntimeError("Safari response text slicing stopped making progress.")
                    body_parts.append(str(raw_slice.get("text") or ""))
                    slice_start = slice_end
                response_headers = metadata.get("headers")
                return SafariResponse(
                    status=int(metadata.get("status") or 0),
                    body_text="".join(body_parts),
                    headers={
                        str(key).lower(): str(value)
                        for key, value in (
                            response_headers.items()
                            if isinstance(response_headers, dict)
                            else ()
                        )
                    },
                    request=SafariRequest(headers=dict(headers)),
                )
            finally:
                if not cleaned_inline:
                    with contextlib.suppress(RuntimeError):
                        page.evaluate(
                            """() => {
                                const current = window.__cachelikesSafariRequest;
                                if (current && current.controller) current.controller.abort();
                                delete window.__cachelikesSafariRequest;
                                return true;
                            }"""
                        )


class SafariLocator:
    """Expose the bounded locator operations used by Safari Web Agent flows."""

    def __init__(self, page: SafariPage, selector: str, index: int | None = None) -> None:
        self._page = page
        self._selector = selector
        self._index = index

    @property
    def first(self) -> SafariLocator:
        """Return a locator bound to the first matching element."""
        return SafariLocator(self._page, self._selector, 0)

    @property
    def last(self) -> SafariLocator:
        """Return a locator bound to the last matching element."""
        return SafariLocator(self._page, self._selector, -1)

    def nth(self, index: int) -> SafariLocator:
        """Return a locator bound to one zero-based matching element."""
        return SafariLocator(self._page, self._selector, int(index))

    def count(self) -> int:
        """Return the current number of matching elements."""
        payload = self._page.evaluate(
            """({selector, index}) => {
                const count = document.querySelectorAll(selector).length;
                if (index === null) return count;
                const resolved = index < 0 ? count + index : index;
                return resolved >= 0 && resolved < count ? 1 : 0;
            }""",
            {"selector": self._selector, "index": self._index},
        )
        return int(payload or 0)

    def is_visible(self) -> bool:
        """Return whether the selected element is currently rendered and visible."""
        payload = self._page.evaluate(
            """({selector, index}) => {
                const elements = [...document.querySelectorAll(selector)];
                const resolved = index === null ? 0 : (index < 0 ? elements.length + index : index);
                const element = elements[resolved];
                if (!element || element.getClientRects().length === 0) return false;
                for (let current = element; current; current = current.parentElement) {
                    const style = getComputedStyle(current);
                    const opacity = Number.parseFloat(style.opacity || '1');
                    if (style.display === 'none'
                        || style.visibility === 'hidden'
                        || style.visibility === 'collapse'
                        || (Number.isFinite(opacity) && opacity <= 0)) return false;
                }
                return true;
            }""",
            {"selector": self._selector, "index": self._index},
        )
        return bool(payload)

    def wait_for(self, state: str = "visible", timeout: int = 30_000) -> None:
        """Wait for one supported locator state within a bounded interval."""
        normalized_state = str(state or "visible").strip().lower()
        if normalized_state not in {"visible", "hidden", "attached", "detached"}:
            raise ValueError(f"Unsupported Safari locator state: {state}")
        deadline = time.monotonic() + max(0.001, int(timeout) / 1_000)
        while time.monotonic() < deadline:
            payload = self._page.evaluate(
                """({selector, index}) => {
                    const elements = [...document.querySelectorAll(selector)];
                    const resolved = index === null ? 0 : (index < 0 ? elements.length + index : index);
                    const element = elements[resolved];
                    let visible = false;
                    if (element && element.getClientRects().length > 0) {
                        visible = true;
                        for (let current = element; current; current = current.parentElement) {
                            const style = getComputedStyle(current);
                            const opacity = Number.parseFloat(style.opacity || '1');
                            if (style.display === 'none'
                                || style.visibility === 'hidden'
                                || style.visibility === 'collapse'
                                || (Number.isFinite(opacity) && opacity <= 0)) {
                                visible = false;
                                break;
                            }
                        }
                    }
                    return {found: Boolean(element), visible};
                }""",
                {"selector": self._selector, "index": self._index},
            )
            found = bool(isinstance(payload, dict) and payload.get("found"))
            visible = bool(isinstance(payload, dict) and payload.get("visible"))
            if (
                (normalized_state == "visible" and visible)
                or (normalized_state == "hidden" and (not found or not visible))
                or (normalized_state == "attached" and found)
                or (normalized_state == "detached" and not found)
            ):
                return
            time.sleep(SAFARI_POLL_INTERVAL_SECONDS)
        raise TimeoutError(
            f"Safari locator {self._selector!r} did not reach {normalized_state!r} "
            f"within {timeout:,} ms."
        )

    def _mark_for_native_input(
        self,
        timeout: int,
        *,
        expected_url: str = "",
    ) -> tuple[str, str]:
        """Focus and mark one exact locator before a trusted native key event."""
        deadline = time.monotonic() + max(0.001, int(timeout) / 1_000)
        activation_marker = f"safari-native-{secrets.token_hex(16)}"
        bound_url = str(expected_url or "").strip()
        while time.monotonic() < deadline:
            payload = self._page.evaluate(
                """({selector, index, activationMarker, expectedCurrentUrl}) => {
                    if (expectedCurrentUrl && location.href !== expectedCurrentUrl) {
                        return {
                            actionable: false,
                            targetMismatch: true,
                            currentUrl: location.href,
                        };
                    }
                    const elements = [...document.querySelectorAll(selector)];
                    const resolved = index === null ? 0 : (index < 0 ? elements.length + index : index);
                    const element = elements[resolved];
                    document.querySelectorAll('[data-cachelikes-safari-native-activation]')
                        .forEach((candidate) => candidate.removeAttribute(
                            'data-cachelikes-safari-native-activation'
                        ));
                    if (!element || element.getClientRects().length === 0) return {actionable: false};
                    for (let current = element; current; current = current.parentElement) {
                        const style = getComputedStyle(current);
                        const opacity = Number.parseFloat(style.opacity || '1');
                        if (style.display === 'none'
                            || style.visibility === 'hidden'
                            || style.visibility === 'collapse'
                            || (Number.isFinite(opacity) && opacity <= 0)) return {actionable: false};
                    }
                    if (element.disabled || element.getAttribute('aria-disabled') === 'true') {
                        return {actionable: false};
                    }
                    element.scrollIntoView({block: 'center', inline: 'center'});
                    const rect = element.getBoundingClientRect();
                    const left = Math.max(0, rect.left);
                    const right = Math.min(window.innerWidth, rect.right);
                    const top = Math.max(0, rect.top);
                    const bottom = Math.min(window.innerHeight, rect.bottom);
                    if (right <= left || bottom <= top) return {actionable: false};
                    const hit = document.elementFromPoint(
                        left + ((right - left) / 2),
                        top + ((bottom - top) / 2),
                    );
                    if (!hit || (hit !== element && !element.contains(hit))) {
                        return {actionable: false};
                    }
                    element.setAttribute(
                        'data-cachelikes-safari-native-activation',
                        activationMarker,
                    );
                    element.focus({preventScroll: true});
                    const focused = document.activeElement === element
                        || element.contains(document.activeElement);
                    if (!focused) {
                        element.removeAttribute('data-cachelikes-safari-native-activation');
                    }
                    return {
                        actionable: focused,
                        currentUrl: location.href,
                    };
                }""",
                {
                    "selector": self._selector,
                    "index": self._index,
                    "activationMarker": activation_marker,
                    "expectedCurrentUrl": bound_url,
                },
            )
            if isinstance(payload, dict) and payload.get("targetMismatch"):
                raise RuntimeError("Safari target changed before native activation.")
            if isinstance(payload, dict) and payload.get("actionable"):
                return (
                    activation_marker,
                    bound_url or str(payload.get("currentUrl") or ""),
                )
            time.sleep(SAFARI_POLL_INTERVAL_SECONDS)
        raise TimeoutError(
            f"Safari could not focus locator {self._selector!r} within {timeout:,} ms."
        )

    def click(self, timeout: int = 30_000, *, expected_url: str = "") -> None:
        """Activate the selected unique element with one trusted native Return key."""
        activation_marker, bound_url = self._mark_for_native_input(
            timeout,
            expected_url=expected_url,
        )
        self._page._activate_marked_element(activation_marker, bound_url)

    def press(
        self,
        key: str,
        timeout: int = 30_000,
        *,
        expected_url: str = "",
    ) -> None:
        """Send one supported trusted navigation key to the selected unique element."""
        normalized_key = str(key or "").strip()
        if normalized_key not in {"Home", "ArrowRight", "Escape"}:
            raise ValueError(f"Unsupported Safari locator key: {key}")
        activation_marker, bound_url = self._mark_for_native_input(
            timeout,
            expected_url=expected_url,
        )
        self._page._activate_marked_element(
            activation_marker,
            bound_url,
            key=normalized_key,
        )

    def get_attribute(self, name: str) -> str | None:
        """Return one attribute from the selected element without exposing page state."""
        payload = self._page.evaluate(
            """({selector, index, name}) => {
                const elements = [...document.querySelectorAll(selector)];
                const resolved = index === null ? 0 : (index < 0 ? elements.length + index : index);
                const element = elements[resolved];
                return element ? element.getAttribute(name) : null;
            }""",
            {"selector": self._selector, "index": self._index, "name": str(name)},
        )
        return None if payload is None else str(payload)

    def evaluate(self, expression: str, argument: Any = None) -> Any:
        """Evaluate one trusted locator callback against the selected element."""
        callback_source = str(expression or "").strip()
        locator_expression = f"""({{selector, index, argument}}) => {{
                const elements = Array.from(document.querySelectorAll(selector));
                const resolved = index === null
                    ? 0
                    : (index < 0 ? elements.length + index : index);
                const element = elements[resolved];
                if (!element) throw new Error('Safari locator element is unavailable.');
                const callback = ({callback_source});
                return callback(element, argument);
            }}"""
        return self._page.evaluate(
            locator_expression,
            {
                "selector": self._selector,
                "index": self._index,
                "argument": argument,
            },
        )

    def inner_text(self, timeout: int = 30_000) -> str:
        """Return text from the selected matching element within a bounded wait."""
        deadline = time.monotonic() + max(0.001, int(timeout) / 1_000)
        while time.monotonic() < deadline:
            payload = self._page.evaluate(
                """({selector, index}) => {
                    const elements = [...document.querySelectorAll(selector)];
                    const resolved = index === null ? 0 : (index < 0 ? elements.length + index : index);
                    const element = elements[resolved];
                    return {
                        found: Boolean(element),
                        text: element ? (element.innerText || element.textContent || "") : "",
                    };
                }""",
                {"selector": self._selector, "index": self._index},
            )
            if isinstance(payload, dict) and payload.get("found"):
                return str(payload.get("text") or "")
            time.sleep(SAFARI_POLL_INTERVAL_SECONDS)
        raise RuntimeError(f"Safari did not find selector {self._selector!r} within {timeout:,} ms.")


class SafariPage:
    """Represent one Safari tab in the single window owned by the current sync."""

    def __init__(
        self,
        context: SafariContext,
        window_id: int,
        tab_index: int = 1,
    ) -> None:
        self._context = context
        self.window_id = int(window_id)
        self.tab_index = max(1, int(tab_index))
        self._closed = False
        self._rendering_active = False
        self._native_input_transaction_depth = 0
        self._background_only_depth = 0
        self._recovery_url = context.initial_url if not context.pages else "about:blank"

    @property
    def context(self) -> SafariContext:
        """Return the owning Safari context."""
        return self._context

    @property
    def url(self) -> str:
        """Return the current tab URL."""
        return self._run_in_window("return URL of targetTab").strip()

    def goto(self, url: str, wait_until: str = "domcontentloaded", timeout: int = 60_000) -> None:
        """Navigate the current tab with retries and verify the destination URL."""
        target_url = str(url or "").strip()
        if not target_url:
            raise RuntimeError("Safari cannot navigate to an empty URL.")
        self._recovery_url = target_url
        accepted_states = {"complete"}
        if wait_until in {"commit", "domcontentloaded"}:
            accepted_states.add("interactive")

        timeout_seconds = max(1.0, int(timeout) / 1_000)
        deadline = time.monotonic() + timeout_seconds
        last_error: RuntimeError | None = None
        last_url = ""

        try:
            initial_state = self._read_navigation_state()
        except RuntimeError as exc:
            last_error = exc
            initial_state = None
        if initial_state is not None:
            last_url = str(initial_state.get("href") or "")
            if (
                str(initial_state.get("readyState") or "") in accepted_states
                and safari_navigation_matches(target_url, last_url)
            ):
                self._keep_in_background()
                return

        for attempt_index in range(SAFARI_NAVIGATION_RETRY_LIMIT):
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                break
            attempts_remaining = SAFARI_NAVIGATION_RETRY_LIMIT - attempt_index
            attempt_deadline = min(
                deadline,
                time.monotonic() + max(2.0, remaining_seconds / attempts_remaining),
            )
            try:
                self._run_in_window(
                    f'set URL of targetTab to "{escape_applescript_text(target_url)}"',
                    reveal_tab=True,
                )
            except RuntimeError as exc:
                last_error = exc
                if attempt_index + 1 < SAFARI_NAVIGATION_RETRY_LIMIT:
                    time.sleep(
                        min(
                            SAFARI_NAVIGATION_RETRY_DELAY_SECONDS * (attempt_index + 1),
                            max(0.0, deadline - time.monotonic()),
                        )
                    )
                continue

            wrong_page_since = 0.0
            while time.monotonic() < attempt_deadline:
                try:
                    navigation_state = self._read_navigation_state()
                except RuntimeError as exc:
                    last_error = exc
                    navigation_state = None
                if navigation_state is not None:
                    ready_state = str(navigation_state.get("readyState") or "")
                    last_url = str(navigation_state.get("href") or "")
                    if ready_state in accepted_states and safari_navigation_matches(target_url, last_url):
                        self._keep_in_background()
                        return
                    if ready_state in accepted_states and last_url in {"about:blank", "favorites://"}:
                        if not wrong_page_since:
                            wrong_page_since = time.monotonic()
                        elif time.monotonic() - wrong_page_since >= SAFARI_WRONG_PAGE_GRACE_SECONDS:
                            break
                    else:
                        wrong_page_since = 0.0
                time.sleep(SAFARI_POLL_INTERVAL_SECONDS)

            if attempt_index + 1 < SAFARI_NAVIGATION_RETRY_LIMIT:
                time.sleep(
                    min(
                        SAFARI_NAVIGATION_RETRY_DELAY_SECONDS * (attempt_index + 1),
                        max(0.0, deadline - time.monotonic()),
                    )
                )

        details = []
        if last_url:
            details.append(f"last page was {last_url}")
        if last_error is not None:
            details.append(f"last Safari error was {last_error}")
        detail_text = f" ({'; '.join(details)})" if details else ""
        raise RuntimeError(
            f"Safari did not finish loading {target_url} within {int(timeout):,} ms{detail_text}."
        )

    def _keep_in_background(self) -> None:
        """Keep the owned window visible and rendered without taking focus."""
        self.keep_rendering_in_background()

    def _read_navigation_state(self) -> dict[str, str] | None:
        """Return Safari's native URL and DOM readiness during navigation."""
        raw_state = self._run_in_window(
            """
set pageUrlValue to ""
set pageStateValue to ""
try
    set candidateUrl to URL of targetTab
    if candidateUrl is not missing value then set pageUrlValue to candidateUrl as text
end try
try
    set candidateState to do JavaScript "document.readyState || ''" in current tab of targetWindow
    if candidateState is not missing value then set pageStateValue to candidateState as text
end try
return pageUrlValue & linefeed & pageStateValue
""".strip(),
            reveal_tab=True,
        )
        state_lines = raw_state.split("\n", maxsplit=1)
        return {
            "href": state_lines[0].strip() if state_lines else "",
            "readyState": state_lines[1].strip() if len(state_lines) > 1 else "",
        }

    def wait_for_load_state(self, state: str, timeout: int) -> None:
        """Wait for DOM readiness; Safari does not expose network-idle state."""
        accepted_states = {"complete"}
        if state in {"commit", "domcontentloaded"}:
            accepted_states.add("interactive")
        deadline = time.monotonic() + max(1.0, timeout / 1_000)
        last_error: RuntimeError | None = None
        while time.monotonic() < deadline:
            try:
                if self.evaluate("() => document.readyState") in accepted_states:
                    return
            except RuntimeError as exc:
                last_error = exc
            time.sleep(SAFARI_POLL_INTERVAL_SECONDS)
        detail = f" Last Safari error: {last_error}" if last_error is not None else ""
        raise RuntimeError(f"Safari did not reach load state {state!r} within {timeout:,} ms.{detail}")

    def wait_for_timeout(self, milliseconds: int) -> None:
        """Pause for a Playwright-compatible millisecond interval."""
        time.sleep(max(0, int(milliseconds)) / 1_000)

    def title(self) -> str:
        """Return the current document title."""
        return str(self.evaluate("() => document.title"))

    def content(self, limit: int | None = None) -> str:
        """Return the current tab's rendered source, optionally clipped in Python."""
        source = self._run_in_window("return source of targetTab")
        if limit is None:
            return source
        return source[: max(0, int(limit))]

    def locator(self, selector: str) -> SafariLocator:
        """Return a minimal locator bound to this page."""
        return SafariLocator(self, selector)

    def evaluate(self, expression: str, argument: Any = None) -> Any:
        """Evaluate a Playwright-style page function and decode its result."""
        function_source = re.sub(r"\s+", " ", str(expression or "").strip())
        argument_json = json.dumps(argument, separators=(",", ":"))
        if len(argument_json) > SAFARI_JAVASCRIPT_ARGUMENT_INLINE_LIMIT:
            token = self._store_javascript_argument(argument_json)
            argument_json = (
                "JSON.parse(window.__cachelikesSafariEvalArg["
                f"{json.dumps(token)}])"
            )
        return self._evaluate_compact(function_source, argument_json)

    def _store_javascript_argument(self, payload: str) -> str:
        """Move a large JSON argument into the page in bounded Safari scripts."""
        token = secrets.token_hex(8)
        self._evaluate_compact(
            "({token}) => { window.__cachelikesSafariEvalArg = window.__cachelikesSafariEvalArg || {}; window.__cachelikesSafariEvalArg[token] = ''; }",
            json.dumps({"token": token}, separators=(",", ":")),
        )
        for offset in range(0, len(payload), SAFARI_JAVASCRIPT_ARGUMENT_CHUNK_SIZE):
            chunk = payload[offset:offset + SAFARI_JAVASCRIPT_ARGUMENT_CHUNK_SIZE]
            self._evaluate_compact(
                "({token, chunk}) => { window.__cachelikesSafariEvalArg[token] += chunk; }",
                json.dumps({"token": token, "chunk": chunk}, separators=(",", ":")),
            )
        return token

    def _evaluate_compact(self, function_source: str, argument_json: str) -> Any:
        """Run one compact, single-line Safari JavaScript function."""
        wrapper = (
            "(function(){try{const value=("
            f"{function_source})({argument_json});"
            "return JSON.stringify({ok:true,value});"
            "}catch(error){return JSON.stringify({ok:false,error:String("
            "error&&error.message?error.message:error)});}})()"
        )
        statement = (
            f'return do JavaScript "{escape_applescript_text(wrapper)}" '
            "in current tab of targetWindow"
        )
        last_raw = ""
        last_error: Exception | None = None
        for attempt_index in range(SAFARI_JAVASCRIPT_RETRY_LIMIT + 1):
            if attempt_index:
                if not self._native_input_transaction_depth:
                    with contextlib.suppress(RuntimeError):
                        if attempt_index == 1 or self._background_only_depth:
                            # Background transfers must never activate Safari:
                            # restoring the previous app afterward flashes
                            # other windows (such as Terminal) to the front.
                            self.keep_rendering_in_background()
                        else:
                            self.wake_for_javascript()
                time.sleep(SAFARI_POLL_INTERVAL_SECONDS * attempt_index)
            try:
                raw_result = self._run_in_window(statement, reveal_tab=True)
            except RuntimeError as exc:
                last_error = exc
                if attempt_index >= SAFARI_JAVASCRIPT_RETRY_LIMIT:
                    break
                continue
            last_raw = str(raw_result or "")
            normalized = last_raw.strip()
            if not normalized or normalized.casefold() in {"missing value", "null"}:
                last_error = RuntimeError("Safari returned an unreadable JavaScript result.")
                if attempt_index >= SAFARI_JAVASCRIPT_RETRY_LIMIT:
                    break
                continue
            try:
                payload = json.loads(normalized)
            except json.JSONDecodeError as exc:
                last_error = RuntimeError(
                    "Safari returned an unreadable JavaScript result."
                )
                last_error.__cause__ = exc
                if attempt_index >= SAFARI_JAVASCRIPT_RETRY_LIMIT:
                    break
                continue
            if not payload.get("ok"):
                raise RuntimeError(
                    f"Safari JavaScript failed: {payload.get('error') or 'unknown error'}"
                )
            return payload.get("value")
        preview = re.sub(r"\s+", " ", last_raw).strip()[:120]
        detail = f" ({preview!r})" if preview else ""
        raise RuntimeError(
            f"Safari returned an unreadable JavaScript result{detail}."
        ) from last_error

    def _activate_marked_element(
        self,
        marker: str,
        expected_url: str,
        *,
        key: str = "Enter",
    ) -> None:
        """Dispatch one non-retried trusted key event to an exact marked web control."""
        native_key_codes = {
            "Enter": 36,
            "Home": 115,
            "ArrowRight": 124,
            "Escape": 53,
        }
        normalized_key = str(key or "").strip()
        key_code = native_key_codes.get(normalized_key)
        if key_code is None:
            raise ValueError(f"Unsupported Safari native input key: {key}")
        try:
            parsed = urlsplit(str(expected_url or "").strip())
            port = parsed.port
        except ValueError as exc:
            raise RuntimeError("Safari refused native input on an invalid page URL.") from exc
        if (
            parsed.scheme.lower() != "https"
            or (parsed.hostname or "").lower() not in SAFARI_NATIVE_INPUT_HOSTS
            or port not in {None, 443}
            or parsed.username
            or parsed.password
        ):
            raise RuntimeError("Safari refused native input outside an official HTTPS provider origin.")

        request_json = json.dumps(
            {
                "activationMarker": str(marker),
                "expectedUrl": str(expected_url),
                "expectedKey": normalized_key,
                "allowedHosts": sorted(SAFARI_NATIVE_INPUT_HOSTS),
            },
            separators=(",", ":"),
        )
        prepare_script = f"""
(() => {{
    const request = {request_json};
    let expected;
    let current;
    try {{
        expected = new URL(request.expectedUrl);
        current = new URL(location.href);
    }} catch (_) {{
        return 'invalid-url';
    }}
    const allowedHosts = new Set(request.allowedHosts);
    if (expected.protocol !== 'https:'
        || current.protocol !== 'https:'
        || !allowedHosts.has(expected.hostname.toLowerCase())
        || !allowedHosts.has(current.hostname.toLowerCase())
        || expected.port
        || current.port
        || expected.username
        || expected.password
        || current.username
        || current.password
        || expected.href !== current.href) return 'target-mismatch';
    const candidates = [...document.querySelectorAll(
        '[data-cachelikes-safari-native-activation]'
    )].filter((element) => (
        element.getAttribute('data-cachelikes-safari-native-activation')
            === request.activationMarker
    ));
    if (candidates.length !== 1) return 'target-ambiguous';
    const element = candidates[0];
    if (!element.getClientRects().length
        || element.disabled
        || element.getAttribute('aria-disabled') === 'true') return 'target-disabled';
    for (let currentElement = element;
        currentElement;
        currentElement = currentElement.parentElement) {{
        const style = getComputedStyle(currentElement);
        const opacity = Number.parseFloat(style.opacity || '1');
        if (style.display === 'none'
            || style.visibility === 'hidden'
            || style.visibility === 'collapse'
            || (Number.isFinite(opacity) && opacity <= 0)) return 'target-hidden';
    }}
    const rect = element.getBoundingClientRect();
    const left = Math.max(0, rect.left);
    const right = Math.min(window.innerWidth, rect.right);
    const top = Math.max(0, rect.top);
    const bottom = Math.min(window.innerHeight, rect.bottom);
    if (right <= left || bottom <= top) return 'target-offscreen';
    const hit = document.elementFromPoint(
        left + ((right - left) / 2),
        top + ((bottom - top) / 2),
    );
    if (!hit || (hit !== element && !element.contains(hit))) return 'target-obscured';
    element.focus({{preventScroll: true}});
    if (!document.hasFocus()) return 'document-unfocused';
    if (document.activeElement !== element
        && !element.contains(document.activeElement)) return 'target-unfocused';
    const previous = window.__cachelikesSafariNativeActivation;
    if (previous && typeof previous.cleanup === 'function') previous.cleanup();
    const state = {{
        token: request.activationMarker,
        keydownTrusted: false,
        clickTrusted: false,
    }};
    const belongsToTarget = (event) => (
        event.target === element || element.contains(event.target)
    );
    const recordKeydown = (event) => {{
        if (belongsToTarget(event) && event.isTrusted && event.key === request.expectedKey) {{
            state.keydownTrusted = true;
        }}
    }};
    const recordClick = (event) => {{
        if (belongsToTarget(event) && event.isTrusted) state.clickTrusted = true;
    }};
    document.addEventListener('keydown', recordKeydown, true);
    document.addEventListener('click', recordClick, true);
    state.cleanup = () => {{
        document.removeEventListener('keydown', recordKeydown, true);
        document.removeEventListener('click', recordClick, true);
    }};
    window.__cachelikesSafariNativeActivation = state;
    return 'ready';
}})()
""".strip()
        receipt_script = f"""
(() => {{
    const state = window.__cachelikesSafariNativeActivation;
    if (!state || state.token !== {json.dumps(str(marker))}) return 'untrusted';
    const trusted = Boolean(state.keydownTrusted || state.clickTrusted);
    if (typeof state.cleanup === 'function') state.cleanup();
    delete window.__cachelikesSafariNativeActivation;
    return trusted ? 'trusted' : 'untrusted';
}})()
""".strip()
        expected_url_literal = escape_applescript_text(str(expected_url))
        verify_script = f"""
(() => {{
    if (location.href !== {json.dumps(str(expected_url))}) return 'target-mismatch';
    if (!document.hasFocus()) return 'document-unfocused';
    const candidates = [...document.querySelectorAll(
        '[data-cachelikes-safari-native-activation]'
    )].filter((element) => (
        element.getAttribute('data-cachelikes-safari-native-activation')
            === {json.dumps(str(marker))}
    ));
    if (candidates.length !== 1) return 'target-ambiguous';
    const element = candidates[0];
    if (!element.getClientRects().length
        || element.disabled
        || element.getAttribute('aria-disabled') === 'true') return 'target-disabled';
    for (let currentElement = element;
        currentElement;
        currentElement = currentElement.parentElement) {{
        const style = getComputedStyle(currentElement);
        const opacity = Number.parseFloat(style.opacity || '1');
        if (style.display === 'none'
            || style.visibility === 'hidden'
            || style.visibility === 'collapse'
            || (Number.isFinite(opacity) && opacity <= 0)) return 'target-hidden';
    }}
    const rect = element.getBoundingClientRect();
    const left = Math.max(0, rect.left);
    const right = Math.min(window.innerWidth, rect.right);
    const top = Math.max(0, rect.top);
    const bottom = Math.min(window.innerHeight, rect.bottom);
    if (right <= left || bottom <= top) return 'target-offscreen';
    const hit = document.elementFromPoint(
        left + ((right - left) / 2),
        top + ((bottom - top) / 2),
    );
    if (!hit || (hit !== element && !element.contains(hit))) return 'target-obscured';
    if (document.activeElement !== element
        && !element.contains(document.activeElement)) return 'target-unfocused';
    return 'ready';
}})()
""".strip()
        statement = f"""
{SAFARI_WAIT_FOR_NATIVE_FOCUS_APPLESCRIPT}
set nativeInputAttempted to false
try
    set activationState to do JavaScript "{escape_applescript_text(prepare_script)}" in targetTab
    if activationState is not "ready" then error "Safari native activation refused: " & activationState
    if (id of front window) is not (id of targetWindow) then error "Safari target window changed before native activation."
    if (current tab of targetWindow) is not targetTab then error "Safari target tab changed before native activation."
    set currentUrlBeforeInput to URL of targetTab as text
    if currentUrlBeforeInput is not "{expected_url_literal}" then error "Safari target URL changed before native activation."
    set finalActivationState to do JavaScript "{escape_applescript_text(verify_script)}" in targetTab
    if finalActivationState is not "ready" then error "Safari native activation refused: " & finalActivationState
    if (id of front window) is not (id of targetWindow) then error "Safari target window changed before native activation."
    if (current tab of targetWindow) is not targetTab then error "Safari target tab changed before native activation."
    set finalUrlBeforeInput to URL of targetTab as text
    if finalUrlBeforeInput is not "{expected_url_literal}" then error "Safari target URL changed before native activation."
    tell application "System Events"
        set currentFrontmostProcessName to name of first application process whose frontmost is true
        if currentFrontmostProcessName is not "Safari" then error "Safari lost focus before native activation."
        tell process "Safari"
            if (count of sheets of front window) is not 0 then error "Safari displayed a native sheet before activation."
            set safariHasNativeDialog to false
            repeat with safariWindow in windows
                try
                    if (subrole of safariWindow as text) is "AXDialog" then
                        set safariHasNativeDialog to true
                        exit repeat
                    end if
                end try
            end repeat
            if safariHasNativeDialog then error "Safari displayed a native dialog before activation."
            set nativeInputAttempted to true
            key code {key_code}
        end tell
    end tell
    delay 0.1
    set currentUrlAfterInput to ""
    try
        set currentUrlAfterInput to URL of targetTab as text
    end try
    set receiptState to "unavailable"
    try
        set receiptState to do JavaScript "{escape_applescript_text(receipt_script)}" in targetTab
    end try
    if receiptState is not "trusted" and currentUrlAfterInput is "{expected_url_literal}" then
        error "Safari did not deliver a trusted activation event to the marked control."
    end if
on error errorMessage number errorNumber
    if nativeInputAttempted then
        error "SAFARI_NATIVE_INPUT_UNCERTAIN: " & errorMessage number errorNumber
    end if
    error "SAFARI_NATIVE_INPUT_NOT_ATTEMPTED: " & errorMessage number errorNumber
end try
return receiptState
""".strip()
        with self.native_input_transaction():
            try:
                self._run_in_window(statement, retry_transient=False, reveal_tab=True)
            except RuntimeError as exc:
                message = str(exc)
                uncertain_token = "SAFARI_NATIVE_INPUT_UNCERTAIN:"
                not_attempted_token = "SAFARI_NATIVE_INPUT_NOT_ATTEMPTED:"
                if uncertain_token in message:
                    raise SafariNativeActivationError(
                        message.split(uncertain_token, 1)[1].strip(),
                        input_attempted=True,
                    ) from exc
                if not_attempted_token in message:
                    raise SafariNativeActivationError(
                        message.split(not_attempted_token, 1)[1].strip(),
                        input_attempted=False,
                    ) from exc
                raise SafariNativeActivationError(
                    message,
                    input_attempted=True,
                ) from exc

    def wake_for_javascript(self) -> None:
        """Briefly activate the owned tab so page JavaScript can run, then restore focus."""
        with self.native_input_transaction():
            self._run_in_window(
                SAFARI_WAIT_FOR_NATIVE_FOCUS_APPLESCRIPT,
                reveal_tab=True,
            )

    @contextlib.contextmanager
    def native_input_transaction(self):
        """Hold native Safari focus for one short trusted-input sequence.

        Restoration is skipped when the user has already switched away from the
        owned Safari window, so cleanup cannot steal a different foreground app.
        """
        if self._native_input_transaction_depth:
            self._native_input_transaction_depth += 1
            try:
                yield
            finally:
                self._native_input_transaction_depth -= 1
            return
        with safari_native_input_guard():
            focus_state = self._run_in_window(
                f"""
{SAFARI_CAPTURE_FRONT_WINDOW_APPLESCRIPT}
return previousFrontmostProcessName & linefeed & (previousWindowId as text) & linefeed & (previousWindowWasVisible as text) & linefeed & (previousWindowWasMiniaturized as text)
""".strip()
            ).splitlines()
            if len(focus_state) != 4 or not focus_state[1].isdigit():
                raise RuntimeError("Safari could not capture the native focus owner.")
            previous_process = focus_state[0]
            previous_window_id = int(focus_state[1])
            previous_window_visible = focus_state[2].strip().lower() == "true"
            previous_window_miniaturized = focus_state[3].strip().lower() == "true"

            def restore_native_focus() -> None:
                self._run_in_window(
                    f"""
set previousFrontmostProcessName to "{escape_applescript_text(previous_process)}"
set previousWindowId to {previous_window_id}
set previousWindowWasVisible to {str(previous_window_visible).lower()}
set previousWindowWasMiniaturized to {str(previous_window_miniaturized).lower()}
{SAFARI_RESTORE_FRONT_WINDOW_IF_TARGET_STILL_FRONT_APPLESCRIPT}
""".strip()
                )

            self._native_input_transaction_depth = 1
            try:
                yield
            except BaseException:
                self._native_input_transaction_depth = 0
                try:
                    restore_native_focus()
                except RuntimeError as restore_error:
                    logger.error(
                        "Safari could not restore native focus after input failed: %s",
                        restore_error,
                    )
                raise
            finally:
                if self._native_input_transaction_depth:
                    self._native_input_transaction_depth = 0
                    try:
                        restore_native_focus()
                    except RuntimeError as restore_error:
                        logger.error(
                            "Safari could not restore native focus after input completed: %s",
                            restore_error,
                        )

    def bring_to_front(self) -> None:
        """Bring the owned Safari window forward."""
        self._run_in_window("set index of targetWindow to 1")

    def keep_rendering_in_background(self) -> None:
        """Keep a standard Safari window rendered without replacing the front window."""
        self._run_in_window(
            f"""
{SAFARI_CAPTURE_FRONT_WINDOW_APPLESCRIPT}
{SAFARI_BACKGROUND_WINDOW_APPLESCRIPT}
""".strip()
        )
        self._rendering_active = True

    def keep_rendering_offscreen(self) -> None:
        """Keep the page rendered in a normal background window for compatibility."""
        self.keep_rendering_in_background()

    def keep_background(self) -> None:
        """Keep the page rendered in a normal background window for compatibility."""
        self.keep_rendering_in_background()

    def close(self) -> None:
        """Close the owned tab, or the shared window when this is the last tab."""
        if self._closed:
            self._context._forget_page(self)
            return
        with safari_window_creation_guard():
            siblings = [
                page
                for page in self._context.pages
                if page is not self
                and page.window_id == self.window_id
                and not page._closed
            ]
            if siblings:
                last_error = self._close_owned_tab()
                if last_error is None:
                    self._context._reindex_tabs_after_close(self)
            else:
                last_error = self._close_owned_window()
        if last_error is not None:
            logger.warning(
                "Safari could not fully close owned tab %s in window %s: %s",
                self.tab_index,
                self.window_id,
                last_error,
            )
            raise last_error
        self._closed = True
        self._context._forget_page(self)

    def _close_owned_tab(self) -> RuntimeError | None:
        """Close this tab without activating Safari or closing sibling tabs."""
        last_error: RuntimeError | None = None
        for attempt_index in range(SAFARI_CLOSE_RETRY_LIMIT):
            try:
                close_state = self._run_in_window(
                    f"""
{SAFARI_CAPTURE_FRONT_WINDOW_APPLESCRIPT}
try
    close targetTab
on error errorMessage number errorNumber
    {SAFARI_RESTORE_FRONT_WINDOW_IF_TARGET_STILL_FRONT_APPLESCRIPT}
    error errorMessage number errorNumber
end try
{SAFARI_RESTORE_FRONT_WINDOW_IF_TARGET_STILL_FRONT_APPLESCRIPT}
return "closed"
""".strip(),
                    recover_missing=False,
                ).strip()
                if close_state == "closed":
                    last_error = None
                    break
                last_error = RuntimeError(
                    f"Safari tab {self.tab_index} of window {self.window_id} "
                    f"returned unexpected close state {close_state!r}."
                )
            except RuntimeError as exc:
                if is_missing_safari_window_error(exc):
                    last_error = None
                    break
                last_error = exc
            if attempt_index + 1 < SAFARI_CLOSE_RETRY_LIMIT:
                time.sleep(SAFARI_APPLESCRIPT_RETRY_DELAY_SECONDS * (attempt_index + 1))
        return last_error

    def _close_owned_window(self) -> RuntimeError | None:
        """Close the owned window without touching the user's front window."""
        last_error: RuntimeError | None = None
        for attempt_index in range(SAFARI_CLOSE_RETRY_LIMIT):
            try:
                close_state = self._run_in_window(
                    f"""
try
    close targetWindow
on error errorMessage number errorNumber
    error errorMessage number errorNumber
end try
repeat with closePollIndex from 1 to 20
    if not (exists (first window whose id is {self.window_id})) then exit repeat
    delay 0.1
end repeat
if exists (first window whose id is {self.window_id}) then
    return "still-open"
end if
return "closed"
""".strip(),
                    recover_missing=False,
                    retry_transient=False,
                    bind_tab=False,
                ).strip()
                if close_state == "closed":
                    last_error = None
                    break
                last_error = RuntimeError(
                    f"Safari window {self.window_id} returned unexpected close state {close_state!r}."
                )
            except RuntimeError as exc:
                if is_missing_safari_window_error(exc):
                    last_error = None
                    break
                last_error = exc
            if attempt_index + 1 < SAFARI_CLOSE_RETRY_LIMIT:
                time.sleep(SAFARI_APPLESCRIPT_RETRY_DELAY_SECONDS * (attempt_index + 1))
        return last_error

    @contextlib.contextmanager
    def _background_only_transfer(self):
        """Keep JavaScript retries from activating Safari during a transfer."""
        self._background_only_depth += 1
        try:
            yield
        finally:
            self._background_only_depth -= 1

    def download_to_path(
        self,
        source_url: str,
        destination_path: Path,
        should_stop,
        headers: dict[str, str] | None = None,
        expected_bytes: int = 0,
    ) -> tuple[str, bool]:
        """Stream an authenticated media URL from Safari into a local file."""
        with self._context.download_lock, self._background_only_transfer():
            if not self._rendering_active:
                with contextlib.suppress(RuntimeError):
                    self.keep_rendering_in_background()
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            initial_bytes = destination_path.stat().st_size if destination_path.exists() else 0
            range_start = initial_bytes
            content_type = ""
            expected_total = 0

            restarted_after_range_error = False
            # Bytes written by this call. Cross-origin media hosts often hide
            # Content-Range from fetch(), so the total size can stay unknown.
            received_this_call = 0
            while expected_total == 0 or range_start < expected_total:
                if should_stop():
                    raise RuntimeError("Stop requested while downloading browser media.")

                range_end = range_start + SAFARI_DOWNLOAD_RANGE_BYTES - 1
                request_headers, referrer = _split_fetch_headers(headers or {})
                request_payload = {
                    "sourceUrl": source_url,
                    "rangeHeader": f"bytes={range_start}-{range_end}",
                    "headers": request_headers,
                    "referrer": referrer,
                }
                self.evaluate(
                    """(request) => {
                        window.__cachelikesSafariDownload = { state: "pending" };
                        const options = {
                            credentials: "include",
                            cache: "no-store",
                            headers: { ...request.headers, Range: request.rangeHeader },
                        };
                        if (request.referrer) options.referrer = request.referrer;
                        fetch(request.sourceUrl, options).then(async (response) => {
                            const bytes = new Uint8Array(await response.arrayBuffer());
                            window.__cachelikesSafariDownload = {
                                state: "ready",
                                status: response.status,
                                contentType: response.headers.get("content-type") || "",
                                contentRange: response.headers.get("content-range") || "",
                                contentLength: response.headers.get("content-length") || "",
                                bytes,
                            };
                        }).catch((error) => {
                            window.__cachelikesSafariDownload = {
                                state: "failed",
                                error: String(error && error.message ? error.message : error),
                            };
                        });
                        return true;
                    }""",
                    request_payload,
                )

                metadata = self._wait_for_download_chunk(should_stop)
                status = int(metadata.get("status") or 0)
                chunk_bytes = int(metadata.get("bytes") or 0)
                if status == 416 and received_this_call > 0 and expected_total == 0:
                    # The previous chunk ended exactly at EOF but the size was
                    # not readable, so the follow-up range is unsatisfiable.
                    self.evaluate(
                        """() => {
                            delete window.__cachelikesSafariDownload;
                            return true;
                        }"""
                    )
                    break
                if status == 416 and range_start > 0 and not restarted_after_range_error:
                    # A stale partial file can be exactly at the remote EOF, or the
                    # asset may have changed since the partial was written. Safari
                    # reports that as 416 instead of returning an empty range.
                    destination_path.unlink(missing_ok=True)
                    range_start = 0
                    expected_total = 0
                    content_type = ""
                    restarted_after_range_error = True
                    received_this_call = 0
                    self.evaluate(
                        """() => {
                            delete window.__cachelikesSafariDownload;
                            return true;
                        }"""
                    )
                    continue
                if status not in {200, 206} or chunk_bytes <= 0:
                    raise RuntimeError(
                        f"Safari media request returned HTTP {status} with {chunk_bytes:,} bytes."
                    )
                if range_start > 0 and status != 206:
                    destination_path.unlink(missing_ok=True)
                    if not restarted_after_range_error:
                        range_start = 0
                        expected_total = 0
                        content_type = ""
                        restarted_after_range_error = True
                        received_this_call = 0
                        continue
                    raise RuntimeError("Safari media server did not honor the resume range.")

                content_range = str(metadata.get("contentRange") or "")
                range_match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+)", content_range)
                if range_match:
                    response_start = int(range_match.group(1))
                    if response_start != range_start:
                        if not restarted_after_range_error:
                            destination_path.unlink(missing_ok=True)
                            range_start = 0
                            expected_total = 0
                            content_type = ""
                            restarted_after_range_error = True
                            received_this_call = 0
                            continue
                        raise RuntimeError(
                            f"Safari media server resumed at byte {response_start:,}, expected {range_start:,}."
                        )
                    expected_total = int(range_match.group(3))
                elif status == 200:
                    expected_total = chunk_bytes
                elif expected_bytes > 0:
                    expected_total = expected_bytes
                elif chunk_bytes < SAFARI_DOWNLOAD_RANGE_BYTES:
                    # A short 206 without a readable Content-Range is the tail.
                    expected_total = range_start + chunk_bytes

                content_type = str(metadata.get("contentType") or content_type)
                mode = "ab" if range_start > 0 else "wb"
                with destination_path.open(mode) as handle:
                    slice_start = 0
                    while slice_start < chunk_bytes:
                        if should_stop():
                            raise RuntimeError("Stop requested while downloading browser media.")
                        slice_end = min(chunk_bytes, slice_start + SAFARI_BASE64_SLICE_BYTES)
                        encoded = self.evaluate(
                            """(bounds) => {
                                const bytes = window.__cachelikesSafariDownload.bytes;
                                let binary = "";
                                for (let index = bounds.start; index < bounds.end; index += 1) {
                                    binary += String.fromCharCode(bytes[index]);
                                }
                                return btoa(binary);
                            }""",
                            {"start": slice_start, "end": slice_end},
                        )
                        handle.write(base64.b64decode(str(encoded)))
                        slice_start = slice_end

                range_start += chunk_bytes
                received_this_call += chunk_bytes
                self.evaluate(
                    """() => {
                        delete window.__cachelikesSafariDownload;
                        return true;
                    }"""
                )
                if status == 200:
                    break

            return content_type, initial_bytes > 0

    def _wait_for_download_chunk(self, should_stop) -> dict[str, Any]:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if should_stop():
                raise RuntimeError("Stop requested while downloading Grok media.")
            metadata = self.evaluate(
                """() => {
                    const current = window.__cachelikesSafariDownload || { state: "missing" };
                    return {
                        state: current.state || "missing",
                        status: current.status || 0,
                        contentType: current.contentType || "",
                        contentRange: current.contentRange || "",
                        contentLength: current.contentLength || "",
                        bytes: current.bytes ? current.bytes.byteLength : 0,
                        error: current.error || "",
                    };
                }"""
            )
            if isinstance(metadata, dict) and metadata.get("state") == "ready":
                return metadata
            if isinstance(metadata, dict) and metadata.get("state") == "failed":
                raise RuntimeError(f"Safari media request failed: {metadata.get('error') or 'unknown error'}")
            time.sleep(SAFARI_POLL_INTERVAL_SECONDS)
        raise RuntimeError("Safari media request timed out.")

    def _owned_tab_script(self, *, reveal: bool = False) -> str:
        """Bind the owned tab, optionally making it current without activating Safari."""
        reveal_script = (
            "if (current tab of targetWindow) is not targetTab then\n"
            "    set current tab of targetWindow to targetTab\n"
            "    delay 0.05\n"
            "end if"
            if reveal
            else ""
        )
        return "\n".join(
            line
            for line in (
                (
                    f"if (count of tabs of targetWindow) < {int(self.tab_index)} then "
                    'error "Safari target tab is missing."'
                ),
                f"set targetTab to tab {int(self.tab_index)} of targetWindow",
                reveal_script,
            )
            if line
        )

    def _run_in_window(
        self,
        statement: str,
        *,
        recover_missing: bool = True,
        retry_transient: bool = True,
        reveal_tab: bool = False,
        bind_tab: bool = True,
    ) -> str:
        if self._closed:
            raise RuntimeError("Safari window is already closed.")
        tab_binding = self._owned_tab_script(reveal=reveal_tab) if bind_tab else ""
        source = f"""
tell application "Safari"
    set targetWindow to first window whose id is {self.window_id}
    {tab_binding}
    {statement}
end tell
        """
        try:
            if retry_transient:
                return run_applescript(source)
            return run_applescript(source, retry_transient=False)
        except RuntimeError as exc:
            if not recover_missing or not is_missing_safari_window_error(exc):
                raise
            raise RuntimeError(
                "The Safari cache window was closed. Restart the cache task when ready."
            ) from exc


class SafariContext:
    """Own one Safari window and its tabs for one authenticated browser sync."""

    def __init__(self, initial_url: str, *, lock_blocking: bool = True) -> None:
        self.initial_url = initial_url
        self.lock_blocking = bool(lock_blocking)
        self.pages: list[SafariPage] = []
        self.request = SafariRequestClient(self)
        self.request_lock = RLock()
        self.download_lock = RLock()
        self._close_lock = RLock()
        self._context_lock_handle: Any | None = None
        self._ownership_token = secrets.token_hex(16)
        self._creation_baseline_inventory: dict[int, int] = {}
        self._durable_lease_started = False
        self._adopted_window_id: int | None = None

    def _read_context_lease_state(self) -> dict[str, Any] | None:
        """Read the atomically replaced ownership record while its lock is held."""
        if self._context_lock_handle is None:
            return {}
        lease_path = _safari_context_lease_path()
        try:
            with lease_path.open(encoding="utf-8") as lease_handle:
                raw = lease_handle.read(SAFARI_CONTEXT_LEASE_MAX_BYTES + 1)
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError) as exc:
            raise RuntimeError("Safari context ownership state could not be read.") from exc
        if len(raw.encode("utf-8")) > SAFARI_CONTEXT_LEASE_MAX_BYTES:
            raise RuntimeError("Safari context ownership state is oversized.")
        if not raw.strip():
            raise RuntimeError("Safari context ownership state is invalid.")
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Safari context ownership state is invalid.") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Safari context ownership state is invalid.")
        return payload

    def _write_context_lease_state(self, payload: dict[str, Any]) -> None:
        """Atomically persist one ownership transition before changing Safari."""
        if self._context_lock_handle is None:
            return
        serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if len(serialized.encode("utf-8")) > SAFARI_CONTEXT_LEASE_MAX_BYTES:
            raise RuntimeError("Safari context ownership state is oversized.")
        lease_path = _safari_context_lease_path()
        temporary_path = lease_path.with_name(
            f".{lease_path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        )
        file_descriptor: int | None = None
        try:
            file_descriptor = os.open(
                temporary_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary:
                file_descriptor = None
                temporary.write(serialized)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, lease_path)
            if os.name == "posix":
                directory_descriptor = os.open(lease_path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
        except OSError as exc:
            raise RuntimeError("Safari context ownership state could not be saved.") from exc
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            with contextlib.suppress(FileNotFoundError):
                temporary_path.unlink()

    def _clear_context_lease_state(self) -> None:
        """Clear durable ownership only after the owned window is proven absent."""
        if self._context_lock_handle is None:
            return
        self._write_context_lease_state(
            {
                "version": SAFARI_CONTEXT_LEASE_VERSION,
                "state": "clear",
            }
        )
        self._durable_lease_started = False
        self._creation_baseline_inventory = {}

    @staticmethod
    def _validated_baseline_inventory(payload: object) -> dict[int, int]:
        """Validate and decode a persisted pre-creation Safari inventory."""
        if not isinstance(payload, list):
            raise RuntimeError("Safari context ownership baseline is invalid.")
        inventory: dict[int, int] = {}
        for item in payload:
            if not isinstance(item, dict):
                raise RuntimeError("Safari context ownership baseline is invalid.")
            window_id = item.get("window_id")
            tab_count = item.get("tab_count")
            if (
                type(window_id) is not int
                or window_id <= 0
                or type(tab_count) is not int
                or tab_count < 0
                or window_id in inventory
            ):
                raise RuntimeError("Safari context ownership baseline is invalid.")
            inventory[window_id] = tab_count
        return inventory

    @staticmethod
    def _serialized_window_inventory(
        inventory: dict[int, int],
    ) -> list[dict[str, int]]:
        """Serialize a deterministic pre-creation window inventory."""
        return [
            {"window_id": window_id, "tab_count": inventory[window_id]}
            for window_id in sorted(inventory)
        ]

    def _reconcile_context_lease_state(self) -> None:
        """Clear a stale lease only when its Safari window is proven absent."""
        if self._context_lock_handle is None:
            return
        try:
            payload = self._read_context_lease_state()
        except RuntimeError as exc:
            try:
                current_inventory = _safari_window_inventory()
            except RuntimeError:
                raise RuntimeError(
                    "Safari has an unreadable prior ownership record and its windows "
                    "could not be verified. No new task window was opened."
                ) from exc
            if current_inventory:
                raise RuntimeError(
                    "Safari has an unreadable prior ownership record. Close any leftover "
                    "task window before starting another Safari task."
                ) from exc
            self._clear_context_lease_state()
            return
        if payload is None:
            return
        if payload == {"version": 1, "state": "clear"}:
            self._clear_context_lease_state()
            return
        if (
            payload.get("version") == SAFARI_CONTEXT_LEASE_VERSION
            and payload.get("state") == "clear"
        ):
            self._durable_lease_started = False
            self._creation_baseline_inventory = {}
            return
        legacy_owned_state = (
            payload.get("version") == 1 and payload.get("state") == "owned"
        )
        if (
            (
                payload.get("version") != SAFARI_CONTEXT_LEASE_VERSION
                and not legacy_owned_state
            )
            or payload.get("state") not in {"creating", "owned"}
            or not isinstance(payload.get("ownership_token"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", payload["ownership_token"])
            or type(payload.get("owner_pid")) is not int
            or payload["owner_pid"] <= 0
        ):
            if not _safari_window_inventory():
                self._clear_context_lease_state()
                return
            raise RuntimeError(
                "Safari has an invalid prior ownership record. No new task window was opened."
            )
        try:
            baseline = self._validated_baseline_inventory(
                payload.get("baseline_windows")
            )
        except RuntimeError:
            if not _safari_window_inventory():
                self._clear_context_lease_state()
                return
            raise
        if payload["state"] == "creating":
            creation_started_at_ns = payload.get("creation_started_at_ns")
            now_ns = time.time_ns()
            if (
                type(creation_started_at_ns) is not int
                or creation_started_at_ns <= 0
                or creation_started_at_ns > now_ns
            ):
                raise RuntimeError(
                    "Safari context creation timing state is invalid. "
                    "No new task window was opened."
                )
            settle_ns = int(SAFARI_CONTEXT_CREATION_SETTLE_SECONDS * 1_000_000_000)
            if now_ns - creation_started_at_ns < settle_ns:
                raise RuntimeError(
                    "Safari is still verifying an uncertain prior window creation. "
                    "No new task window was opened."
                )
        current_inventory = _safari_window_inventory()
        if payload["state"] == "owned":
            window_id = payload.get("window_id")
            if type(window_id) is not int or window_id <= 0:
                if not current_inventory:
                    self._clear_context_lease_state()
                    return
                raise RuntimeError("Safari context owned-window state is invalid.")
            if window_id in current_inventory:
                if window_id in baseline:
                    logger.info(
                        "Ignoring stale Safari ownership of pre-existing window %s.",
                        window_id,
                    )
                    self._clear_context_lease_state()
                    return
                foreign_owner = (
                    _safari_pid_is_alive(payload["owner_pid"])
                    and payload["owner_pid"] != os.getpid()
                )
                if foreign_owner:
                    raise RuntimeError(
                        "Safari still has a task-owned window from a previous process. "
                        "Close that window before starting another Safari task."
                    )
                logger.info(
                    "Closing leftover Safari task window %s after owner pid %s exited.",
                    window_id,
                    payload["owner_pid"],
                )
                _close_safari_window_id(window_id)
                current_inventory = _safari_window_inventory()
                if window_id not in current_inventory:
                    self._clear_context_lease_state()
                    return
                logger.info(
                    "Adopting leftover Safari task window %s because it could not be closed.",
                    window_id,
                )
                self._adopted_window_id = window_id
                return
        else:
            if self._uncertain_creation_candidates(current_inventory, baseline):
                raise RuntimeError(
                    "Safari may still have a task window whose creation was interrupted. "
                    "Close that window before starting another Safari task."
                )
            time.sleep(SAFARI_CONTEXT_INVENTORY_STABILITY_SECONDS)
            stable_inventory = _safari_window_inventory()
            if self._uncertain_creation_candidates(stable_inventory, baseline):
                raise RuntimeError(
                    "Safari may still have a task window whose creation was interrupted. "
                    "Close that window before starting another Safari task."
                )
        self._clear_context_lease_state()

    @staticmethod
    def _uncertain_creation_candidates(
        inventory: dict[int, int],
        baseline: dict[int, int],
    ) -> set[int]:
        """Return windows that could be an interrupted task-window creation."""
        return {
            window_id
            for window_id, tab_count in inventory.items()
            if window_id not in baseline
            or (baseline[window_id] == 0 and tab_count > 0)
        }

    def _begin_context_window_creation(self) -> None:
        """Persist Safari's window baseline before asking it to create a window."""
        if self._context_lock_handle is None:
            return
        inventory = _safari_window_inventory()
        self._creation_baseline_inventory = inventory
        self._write_context_lease_state(
            {
                "version": SAFARI_CONTEXT_LEASE_VERSION,
                "state": "creating",
                "ownership_token": self._ownership_token,
                "owner_pid": os.getpid(),
                "baseline_windows": self._serialized_window_inventory(inventory),
                "creation_started_at_ns": time.time_ns(),
            }
        )
        self._durable_lease_started = True

    def _mark_context_window_owned(self, window_id: int) -> None:
        """Bind the durable ownership record to the newly created Safari window."""
        if self._context_lock_handle is None:
            return
        self._write_context_lease_state(
            {
                "version": SAFARI_CONTEXT_LEASE_VERSION,
                "state": "owned",
                "ownership_token": self._ownership_token,
                "owner_pid": os.getpid(),
                "baseline_windows": self._serialized_window_inventory(
                    self._creation_baseline_inventory
                ),
                "window_id": int(window_id),
            }
        )
        self._durable_lease_started = True

    @property
    def primary_page(self) -> SafariPage:
        """Return the first live page owned by this context."""
        if not self.pages:
            raise RuntimeError("Safari context has no open page.")
        return self.pages[0]

    def __enter__(self) -> SafariContext:
        self._acquire_context_lock()
        try:
            self._create_page(self.initial_url)
        except Exception as creation_error:
            with SAFARI_PENDING_CONTEXT_LOCK:
                cleanup_pending = id(self) in SAFARI_PENDING_CONTEXTS
            cleanup_error: BaseException | None = None
            if cleanup_pending:
                try:
                    self.close()
                    cleanup_pending = False
                except Exception as exc:
                    cleanup_error = exc
            if not cleanup_pending and self._durable_lease_started:
                try:
                    self._reconcile_context_lease_state()
                except Exception as exc:
                    cleanup_error = exc
                    cleanup_pending = True
                    with SAFARI_PENDING_CONTEXT_LOCK:
                        SAFARI_PENDING_CONTEXTS[id(self)] = self
            if not cleanup_pending:
                self._release_context_lock()
            if cleanup_error is not None:
                raise RuntimeError(
                    "Safari could not verify cleanup after its task window failed "
                    "to initialize. No second Safari task will be opened."
                ) from creation_error
            raise
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            self.close()
        except Exception as cleanup_error:
            if exc_type is None:
                raise
            logger.error(
                "Safari housekeeping failed while another error was already unwinding: %s",
                cleanup_error,
            )
        return False

    def new_page(self) -> SafariPage:
        """Create an additional owned tab in the shared Safari window."""
        return self._create_page("about:blank")

    def cookies(self, urls: list[str]) -> list[dict[str, str]]:
        """Avoid exporting Safari cookies; authenticated requests stay in-page."""
        del urls
        return []

    def close(self) -> None:
        """Close every owned page, retaining the lease until cleanup succeeds."""
        with self._close_lock:
            had_tracked_pages = bool(self.pages)
            try:
                self.housekeep()
                if had_tracked_pages:
                    self._clear_context_lease_state()
                elif self._durable_lease_started:
                    self._reconcile_context_lease_state()
            except Exception:
                with SAFARI_PENDING_CONTEXT_LOCK:
                    SAFARI_PENDING_CONTEXTS[id(self)] = self
                raise
            self._release_context_lock()
            with SAFARI_PENDING_CONTEXT_LOCK:
                SAFARI_PENDING_CONTEXTS.pop(id(self), None)

    def _acquire_context_lock(self) -> None:
        """Serialize Safari contexts so no task can repurpose another task's window."""
        if self._context_lock_handle is not None:
            return
        _attempted_cleanup, pending_cleanup = retry_pending_safari_context_cleanup()
        if pending_cleanup:
            raise RuntimeError(
                "Safari is waiting for a previous task-owned window to close. "
                "Review that window before starting another browser task."
            )
        handle = SAFARI_CONTEXT_LOCK_PATH.open("a+")
        try:
            lock_file(handle, blocking=self.lock_blocking)
        except BlockingIOError as exc:
            handle.close()
            raise RuntimeError(
                "Safari is busy with another active browser task. Try again after it finishes."
            ) from exc
        self._context_lock_handle = handle
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), 0o600)
            self._reconcile_context_lease_state()
        except Exception:
            self._context_lock_handle = None
            try:
                unlock_file(handle)
            finally:
                handle.close()
            raise

    def _release_context_lock(self) -> None:
        """Release the cross-process Safari context lease."""
        handle = self._context_lock_handle
        if handle is None:
            return
        self._context_lock_handle = None
        try:
            unlock_file(handle)
        finally:
            handle.close()

    def housekeep(self) -> int:
        """Close every tracked Safari tab or window and return the number released."""
        closed_count = 0
        cleanup_errors: list[BaseException] = []
        for page in list(reversed(self.pages)):
            was_tracked = page in self.pages
            try:
                page.close()
            except Exception as exc:
                cleanup_errors.append(exc)
            if was_tracked and page not in self.pages:
                closed_count += 1
        if cleanup_errors:
            raise RuntimeError(
                f"Safari housekeeping failed for {len(cleanup_errors)} window(s)."
            ) from cleanup_errors[0]
        return closed_count

    def _create_page(self, url: str) -> SafariPage:
        if self.pages:
            window_id = self.pages[0].window_id
            tab_index = self._create_tab(window_id, url)
            page = SafariPage(self, window_id, tab_index=tab_index)
        else:
            adopted_window_id = self._adopted_window_id
            self._adopted_window_id = None
            if adopted_window_id is not None:
                try:
                    current_inventory = _safari_window_inventory()
                except RuntimeError:
                    current_inventory = {}
                if adopted_window_id not in current_inventory:
                    adopted_window_id = None
            if adopted_window_id is not None:
                page = SafariPage(self, adopted_window_id, tab_index=1)
            else:
                with safari_window_creation_guard():
                    self._begin_context_window_creation()
                    raw_window_id = self._create_window(url)
                if not raw_window_id.isdigit():
                    raise RuntimeError("Safari did not return a usable window identifier.")
                page = SafariPage(self, int(raw_window_id), tab_index=1)
        self.pages.append(page)
        try:
            if len(self.pages) == 1:
                self._mark_context_window_owned(page.window_id)
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except Exception:
            try:
                page.close()
            except Exception:
                with SAFARI_PENDING_CONTEXT_LOCK:
                    SAFARI_PENDING_CONTEXTS[id(self)] = self
            raise
        return page

    def _reindex_tabs_after_close(self, closed_page: SafariPage) -> None:
        """Keep remaining tab indexes aligned after Safari closes one owned tab."""
        for page in self.pages:
            if (
                page is not closed_page
                and page.window_id == closed_page.window_id
                and page.tab_index > closed_page.tab_index
            ):
                page.tab_index -= 1

    def _create_window(self, url: str) -> str:
        source = f"""
tell application "Safari"
    set targetWindow to missing value
    set targetDocument to missing value
    set previousFrontmostProcessName to ""
    set previousWindowId to 0
    set previousWindowWasVisible to false
    set previousWindowWasMiniaturized to false
    {SAFARI_CAPTURE_FRONT_WINDOW_APPLESCRIPT}
    try
        launch
        set targetDocument to make new document
        repeat with candidateWindow in every window
            try
                if (document of candidateWindow) is targetDocument then
                    set targetWindow to candidateWindow
                    exit repeat
                end if
            end try
        end repeat
        if targetWindow is missing value then error "Safari did not create an owned window." number -1719
        set URL of current tab of targetWindow to "{escape_applescript_text(url)}"
        {SAFARI_BACKGROUND_WINDOW_APPLESCRIPT}
        return id of targetWindow
    on error errorMessage number errorNumber
        set failedWindowId to 0
        set failedWindowStillOpen to false
        if targetWindow is missing value and targetDocument is not missing value then
            repeat with candidateWindow in every window
                try
                    if (document of candidateWindow) is targetDocument then
                        set targetWindow to candidateWindow
                        exit repeat
                    end if
                end try
            end repeat
        end if
        if targetWindow is not missing value then
            try
                set failedWindowId to id of targetWindow
            on error
                set failedWindowStillOpen to true
            end try
            {SAFARI_RESTORE_FRONT_WINDOW_IF_TARGET_STILL_FRONT_APPLESCRIPT}
            try
                close targetWindow
                delay 0.1
            on error
                set failedWindowStillOpen to true
            end try
            if failedWindowId is not 0 then
                try
                    set failedWindowStillOpen to exists (first window whose id is failedWindowId)
                on error
                    set failedWindowStillOpen to true
                end try
            end if
        end if
        if failedWindowStillOpen and failedWindowId is not 0 then
            error "{SAFARI_OWNED_WINDOW_REMAINS_MARKER}" & (failedWindowId as text) & ":" & errorMessage number errorNumber
        end if
        error errorMessage number errorNumber
    end try
end tell
"""
        try:
            return run_applescript(source, retry_transient=False).strip()
        except RuntimeError as exc:
            marker = re.search(
                rf"{re.escape(SAFARI_OWNED_WINDOW_REMAINS_MARKER)}(\d+):",
                str(exc),
            )
            if marker:
                page = SafariPage(self, int(marker.group(1)), tab_index=1)
                self.pages.append(page)
                try:
                    self._mark_context_window_owned(page.window_id)
                except Exception as lease_error:
                    logger.error(
                        "Safari could not persist its failed window ownership: %s",
                        lease_error,
                    )
                with SAFARI_PENDING_CONTEXT_LOCK:
                    SAFARI_PENDING_CONTEXTS[id(self)] = self
            raise

    def _create_tab(self, window_id: int, url: str) -> int:
        """Add one tab to the already owned Safari window without activating it."""
        source = f"""
tell application "Safari"
    set targetWindow to first window whose id is {int(window_id)}
    set newTab to missing value
    set previousFrontmostProcessName to ""
    set previousWindowId to 0
    set previousWindowWasVisible to false
    set previousWindowWasMiniaturized to false
    {SAFARI_CAPTURE_FRONT_WINDOW_APPLESCRIPT}
    try
        set newTab to make new tab at end of tabs of targetWindow
        set URL of newTab to "{escape_applescript_text(url)}"
        set current tab of targetWindow to newTab
        {SAFARI_BACKGROUND_WINDOW_APPLESCRIPT}
        return index of newTab
    on error errorMessage number errorNumber
        if newTab is not missing value then
            try
                close newTab
            end try
        end if
        {SAFARI_RESTORE_FRONT_WINDOW_IF_TARGET_STILL_FRONT_APPLESCRIPT}
        error errorMessage number errorNumber
    end try
end tell
"""
        with safari_window_creation_guard():
            raw_tab_index = run_applescript(
                source,
                retry_transient=False,
            ).strip()
        if not raw_tab_index.isdigit():
            raise RuntimeError("Safari did not return a usable tab identifier.")
        return int(raw_tab_index)

    def _forget_page(self, page: SafariPage) -> None:
        with contextlib.suppress(ValueError):
            self.pages.remove(page)


def retry_pending_safari_context_cleanup() -> tuple[int, int]:
    """Retry tracked task-window cleanup and return attempted and pending counts."""
    with SAFARI_PENDING_CONTEXT_LOCK:
        pending_contexts = tuple(SAFARI_PENDING_CONTEXTS.values())
    for context in pending_contexts:
        try:
            context.close()
        except Exception as exc:
            logger.warning(
                "Safari task-window cleanup remains pending: %s",
                exc,
            )
    with SAFARI_PENDING_CONTEXT_LOCK:
        pending_count = len(SAFARI_PENDING_CONTEXTS)
    return len(pending_contexts), pending_count


def verify_safari_context_cleanup_ready() -> None:
    """Prove no active or stale Safari ownership remains without opening a window."""
    _attempted, pending = retry_pending_safari_context_cleanup()
    if pending:
        raise RuntimeError(
            "Safari is waiting for a previous task-owned window to close. "
            "No new task window was opened."
        )
    probe = SafariContext("about:blank", lock_blocking=False)
    try:
        probe._acquire_context_lock()
    finally:
        probe._release_context_lock()


def _split_fetch_headers(headers: dict[str, str]) -> tuple[dict[str, str], str]:
    """Move the forbidden Referer header into Fetch's referrer option."""
    request_headers: dict[str, str] = {}
    referrer = ""
    for key, value in headers.items():
        normalized_key = str(key).strip()
        normalized_value = str(value).strip()
        if not normalized_key or not normalized_value:
            continue
        if normalized_key.lower() == "referer":
            referrer = normalized_value
        else:
            request_headers[normalized_key] = normalized_value
    return request_headers, referrer
