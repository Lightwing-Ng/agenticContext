"""Lifecycle management for the project-owned Chromium debug browser.

On Windows a running Chrome or Edge keeps its sign-in cookies under an exclusive
OS lock, so the standard clone-then-launch path cannot reuse the user's live
login state. This module owns a separate Chromium instance launched with
``--remote-debugging-port`` against a dedicated user-data directory, so Playwright
can ``connect_over_cdp`` to it and read the authenticated session without ever
touching the locked profile files.

Code version: v1.23.0-codex.1
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .config import LOCAL_STORE_ROOT, is_windows_host


LOGGER = logging.getLogger(__name__)

DEBUG_BROWSER_ROOT = LOCAL_STORE_ROOT / "agent_browser_profile"
DEBUG_PORT_FILENAME = "debug_port"
DEVTOOLS_ACTIVE_PORT_FILENAME = "DevToolsActivePort"
CDP_READY_TIMEOUT_SECONDS = 20.0
CDP_PROBE_TIMEOUT_SECONDS = 3.0
CDP_CALLER_LOCK_TIMEOUT_SECONDS = 5.0

# Values reported by Chromium's ``/json/version`` ``Browser`` field. Microsoft
# Edge uses the product token ``Edg``; ``Edge`` is not a valid CDP product token.
_BRAND_PREFIXES: dict[str, str] = {"edge": "Edg/", "chrome": "Chrome/"}

# One reentrant lock per browser identity serializes the complete lifetime of a
# CDP caller. The Windows debug browser reuses one process and one rendered tab,
# while clone-profile launches remain isolated and do not use these locks.
_DEBUG_BROWSER_LOCKS: dict[str, threading.RLock] = {}
_DEBUG_BROWSER_LOCKS_GUARD = threading.Lock()


def _debug_browser_lock(browser_id: str) -> threading.RLock:
    """Return the process-wide reentrant lock for one debug browser identity."""
    with _DEBUG_BROWSER_LOCKS_GUARD:
        lock = _DEBUG_BROWSER_LOCKS.get(browser_id)
        if lock is None:
            lock = threading.RLock()
            _DEBUG_BROWSER_LOCKS[browser_id] = lock
        return lock


def _acquire_debug_browser_lock(browser_id: str) -> threading.RLock:
    """Acquire one debug-browser lock without allowing an indefinite wait."""
    lock = _debug_browser_lock(browser_id)
    if not lock.acquire(timeout=CDP_CALLER_LOCK_TIMEOUT_SECONDS):
        raise RuntimeError(
            f"The project debug {browser_id} is busy with another operation. "
            "Retry after it finishes."
        )
    return lock


@contextlib.contextmanager
def debug_browser_lock(browser_id: str):
    """Hold one browser's CDP lock for the complete caller block."""
    lock = _acquire_debug_browser_lock(browser_id)
    try:
        yield
    finally:
        lock.release()


_DEFAULT_VIEWPORT_ARGS = (
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-session-crashed-bubble",
    "--disable-notifications",
    "--window-size=1280,900",
)


@dataclass(frozen=True, slots=True)
class DebugBrowserHandle:
    """Describe one reachable project-owned debug browser."""

    browser_id: str
    cdp_endpoint: str
    user_data_dir: Path


@dataclass(frozen=True, slots=True)
class CdpIdentity:
    """Identify one responsive browser-level CDP endpoint."""

    port: int
    browser_brand: str
    instance_guid: str


@dataclass(frozen=True, slots=True)
class RecordedTarget:
    """Describe one persisted debug target.

    ``instance`` and ``browser`` are absent in the legacy bare-port format.
    """

    port: int
    instance: str | None = None
    browser: str | None = None


def _debug_profile_dir(browser_id: str) -> Path:
    """Return the persistent user-data directory for one debug browser."""
    return DEBUG_BROWSER_ROOT / browser_id


def _debug_port_path(browser_id: str) -> Path:
    """Return the file that records the last-used debug port for one browser."""
    return _debug_profile_dir(browser_id) / DEBUG_PORT_FILENAME


def _devtools_active_port_path(browser_id: str) -> Path:
    """Return Chromium's launch-owned endpoint marker for one profile."""
    return _debug_profile_dir(browser_id) / DEVTOOLS_ACTIVE_PORT_FILENAME


def _resolve_browser_executable(browser_id: str) -> str | None:
    """Resolve an installed Chromium executable without a circular import."""
    # Lazy import: computer_use_agent imports browser_sessions, which would form a
    # cycle if this module imported it at load time. The call below only runs after
    # the application has finished importing, so the deferred import is safe.
    from .computer_use_agent import resolve_windows_browser_executable

    return resolve_windows_browser_executable(browser_id)


def _read_recorded_port(browser_id: str) -> int | None:
    """Return a previously recorded debug port, or None when it is absent."""
    target = _read_recorded_target(browser_id)
    return target.port if target is not None else None


def _valid_port(value: object) -> int | None:
    """Return one valid TCP port while rejecting booleans and malformed values."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 < value <= 65_535 else None


def _read_small_text(path: Path) -> str | None:
    """Read one bounded runtime marker without accepting oversized content."""
    try:
        if path.stat().st_size > 4_096:
            return None
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _read_recorded_target(browser_id: str) -> RecordedTarget | None:
    """Read the persisted CDP target, including legacy bare-port records."""
    port_file = _debug_port_path(browser_id)
    raw = _read_small_text(port_file)
    if not raw:
        return None
    if raw.isdigit():
        port = _valid_port(int(raw))
        return RecordedTarget(port=port) if port is not None else None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    port = _valid_port(payload.get("port"))
    if port is None:
        return None
    instance = payload.get("instance")
    browser = payload.get("browser")
    return RecordedTarget(
        port=port,
        instance=instance if isinstance(instance, str) and instance else None,
        browser=browser if isinstance(browser, str) and browser else None,
    )


def _record_debug_target(browser_id: str, identity: CdpIdentity) -> None:
    """Atomically persist a reachable endpoint and its browser instance identity."""
    port_file = _debug_port_path(browser_id)
    temp_path: Path | None = None
    try:
        port_file.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "port": identity.port,
                "instance": identity.instance_guid,
                "browser": identity.browser_brand,
            },
            separators=(",", ":"),
        )
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=port_file.parent,
            prefix=f".{port_file.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(payload)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        temp_path.replace(port_file)
    except OSError as exc:
        LOGGER.warning("Could not record the debug browser target at %s: %s", port_file, exc)
    finally:
        if temp_path is not None and temp_path.exists():
            with contextlib.suppress(OSError):
                temp_path.unlink()


def _instance_guid_from_websocket(websocket_url: object) -> str:
    """Extract the browser instance identifier from a CDP WebSocket path."""
    if not isinstance(websocket_url, str) or not websocket_url:
        return ""
    try:
        path = urllib.parse.urlsplit(websocket_url).path
    except ValueError:
        return ""
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) < 3 or segments[-3:-1] != ["devtools", "browser"]:
        return ""
    return segments[-1]


def _read_devtools_active_target(browser_id: str) -> RecordedTarget | None:
    """Read the endpoint selected by the browser launched with port zero."""
    raw = _read_small_text(_devtools_active_port_path(browser_id))
    if not raw:
        return None
    lines = raw.splitlines()
    if len(lines) < 2 or not lines[0].strip().isdigit():
        return None
    port = _valid_port(int(lines[0].strip()))
    instance = _instance_guid_from_websocket(lines[1].strip())
    if port is None or not instance:
        return None
    return RecordedTarget(port=port, instance=instance)


def _probe_cdp_identity(port: int) -> CdpIdentity | None:
    """Return the product and process identity exposed by one local CDP port."""
    endpoint = f"http://127.0.0.1:{port}/json/version"
    try:
        with urllib.request.urlopen(endpoint, timeout=CDP_PROBE_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    browser_brand = payload.get("Browser")
    instance_guid = _instance_guid_from_websocket(payload.get("webSocketDebuggerUrl"))
    if not isinstance(browser_brand, str) or not browser_brand or not instance_guid:
        return None
    return CdpIdentity(
        port=port,
        browser_brand=browser_brand,
        instance_guid=instance_guid,
    )


def _cdp_endpoint_alive(port: int) -> bool:
    """Return whether a CDP endpoint exposes a complete browser identity."""
    return _probe_cdp_identity(port) is not None


def _identity_matches_browser(identity: CdpIdentity, browser_id: str) -> bool:
    """Return whether a CDP product token matches the requested browser."""
    prefix = _BRAND_PREFIXES.get(browser_id)
    return prefix is not None and identity.browser_brand.startswith(prefix)


def _wait_for_cdp_ready(
    port: int,
    timeout_seconds: float,
    *,
    expected_guid: str | None = None,
) -> CdpIdentity | None:
    """Poll until one endpoint answers with the optional expected instance."""
    deadline = time.time() + max(0.0, timeout_seconds)
    while time.time() < deadline:
        identity = _probe_cdp_identity(port)
        if identity is not None and (
            expected_guid is None or identity.instance_guid == expected_guid
        ):
            return identity
        time.sleep(0.4)
    return None


def _clear_devtools_active_port(browser_id: str) -> None:
    """Remove the old launch marker before asking Chromium to choose a new port."""
    marker = _devtools_active_port_path(browser_id)
    try:
        marker.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError(
            f"Could not clear the stale debug {browser_id} endpoint marker."
        ) from exc


def _wait_for_launched_cdp(browser_id: str, timeout_seconds: float) -> CdpIdentity | None:
    """Wait for the profile marker and live endpoint from this launch to agree."""
    deadline = time.time() + max(0.0, timeout_seconds)
    while time.time() < deadline:
        target = _read_devtools_active_target(browser_id)
        if target is not None and target.instance is not None:
            identity = _probe_cdp_identity(target.port)
            if identity is not None and identity.instance_guid == target.instance:
                return identity
        time.sleep(0.4)
    return None


def _launch_debug_browser(browser_id: str, executable: str, port: int) -> subprocess.Popen[bytes]:
    """Start one detached Chromium instance exposing a CDP debug endpoint."""
    user_data_dir = _debug_profile_dir(browser_id)
    user_data_dir.mkdir(parents=True, exist_ok=True)
    command = [
        executable,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        *_DEFAULT_VIEWPORT_ARGS,
    ]
    creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
        subprocess,
        "DETACHED_PROCESS",
        0,
    )
    popen_kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if creation_flags:
        popen_kwargs["creationflags"] = creation_flags
    LOGGER.info(
        "Starting debug %s with an OS-selected CDP port (profile: %s).",
        browser_id,
        user_data_dir,
    )
    return subprocess.Popen(command, **popen_kwargs)  # type: ignore[arg-type]


def ensure_debug_browser(browser_id: str) -> DebugBrowserHandle:
    """Return a handle to a reachable debug browser, starting one if needed.

    A recorded endpoint is reused only when its live product and instance identity
    still match. Fresh launches ask Chromium to bind an OS-selected port and trust
    it only after the profile-owned ``DevToolsActivePort`` marker agrees with the
    live endpoint, avoiding a free-port selection race. The browser intentionally
    remains running so subsequent requests can reattach.
    """
    if not is_windows_host():
        raise RuntimeError("The project-owned debug browser is only supported on Windows.")
    if browser_id not in {"edge", "chrome"}:
        raise RuntimeError(f"The debug browser does not support {browser_id!r}.")

    recorded = _read_recorded_target(browser_id)
    if recorded is not None:
        identity = _wait_for_cdp_ready(
            recorded.port,
            CDP_PROBE_TIMEOUT_SECONDS,
            expected_guid=recorded.instance,
        )
        if identity is not None and _identity_matches_browser(identity, browser_id):
            if recorded.instance is None:
                _record_debug_target(browser_id, identity)
            LOGGER.info(
                "Reusing the running debug %s on CDP port %s.",
                browser_id,
                recorded.port,
            )
            return DebugBrowserHandle(
                browser_id=browser_id,
                cdp_endpoint=f"http://127.0.0.1:{recorded.port}",
                user_data_dir=_debug_profile_dir(browser_id),
            )
        LOGGER.warning(
            "The recorded debug %s target no longer reports its expected identity; "
            "launching a fresh project browser.",
            browser_id,
        )

    executable = _resolve_browser_executable(browser_id)
    if executable is None:
        raise RuntimeError(
            f"Could not find an installed {browser_id} executable to launch the debug browser."
        )

    _debug_profile_dir(browser_id).mkdir(parents=True, exist_ok=True)
    _clear_devtools_active_port(browser_id)
    process = _launch_debug_browser(browser_id, executable, 0)
    identity = _wait_for_launched_cdp(browser_id, CDP_READY_TIMEOUT_SECONDS)
    if identity is None:
        with contextlib.suppress(Exception):
            process.terminate()
        raise RuntimeError(
            f"The debug {browser_id} did not publish a verified CDP endpoint "
            f"within {int(CDP_READY_TIMEOUT_SECONDS)} seconds."
        )
    if not _identity_matches_browser(identity, browser_id):
        with contextlib.suppress(Exception):
            process.terminate()
        raise RuntimeError(
            f"The debug {browser_id} endpoint reported browser "
            f"{identity.browser_brand!r}, which does not match the requested "
            f"{browser_id!r} identity."
        )
    _record_debug_target(browser_id, identity)
    return DebugBrowserHandle(
        browser_id=browser_id,
        cdp_endpoint=f"http://127.0.0.1:{identity.port}",
        user_data_dir=_debug_profile_dir(browser_id),
    )


def debug_browser_login_url(browser_id: str) -> str | None:
    """Return the recorded endpoint only while its complete identity still matches."""
    if not is_windows_host() or browser_id not in _BRAND_PREFIXES:
        return None
    recorded = _read_recorded_target(browser_id)
    if recorded is None:
        return None
    identity = _wait_for_cdp_ready(
        recorded.port,
        CDP_PROBE_TIMEOUT_SECONDS,
        expected_guid=recorded.instance,
    )
    if identity is None or not _identity_matches_browser(identity, browser_id):
        return None
    return f"http://127.0.0.1:{recorded.port}"


def debug_browser_profile_initialized(browser_id: str) -> bool:
    """Return whether one debug profile completed a prior successful launch."""
    if not is_windows_host() or browser_id not in {"edge", "chrome"}:
        return False
    return _read_recorded_port(browser_id) is not None
