"""Lifecycle management for the project-owned Chromium debug browser.

On Windows a running Chrome or Edge keeps its sign-in cookies under an exclusive
OS lock, so the standard clone-then-launch path cannot reuse the user's live
login state. This module owns a separate Chromium instance launched with
``--remote-debugging-port`` against a dedicated user-data directory, so Playwright
can ``connect_over_cdp`` to it and read the authenticated session without ever
touching the locked profile files.

Code version: v1.21.0-codex.1
"""

from __future__ import annotations

import contextlib
import json
import logging
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .config import LOCAL_STORE_ROOT, is_windows_host


LOGGER = logging.getLogger(__name__)

DEBUG_BROWSER_ROOT = LOCAL_STORE_ROOT / "agent_browser_profile"
DEBUG_PORT_FILENAME = "debug_port"
CDP_READY_TIMEOUT_SECONDS = 20.0
CDP_PROBE_TIMEOUT_SECONDS = 3.0
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


def _debug_profile_dir(browser_id: str) -> Path:
    """Return the persistent user-data directory for one debug browser."""
    return DEBUG_BROWSER_ROOT / browser_id


def _debug_port_path(browser_id: str) -> Path:
    """Return the file that records the last-used debug port for one browser."""
    return _debug_profile_dir(browser_id) / DEBUG_PORT_FILENAME


def _resolve_browser_executable(browser_id: str) -> str | None:
    """Resolve an installed Chromium executable without a circular import."""
    # Lazy import: computer_use_agent imports browser_sessions, which would form a
    # cycle if this module imported it at load time. The call below only runs after
    # the application has finished importing, so the deferred import is safe.
    from .computer_use_agent import resolve_windows_browser_executable

    return resolve_windows_browser_executable(browser_id)


def _pick_free_port() -> int:
    """Bind a transient socket to let the OS pick a free loopback port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _read_recorded_port(browser_id: str) -> int | None:
    """Return a previously recorded debug port, or None when it is absent."""
    port_file = _debug_port_path(browser_id)
    try:
        raw = port_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if raw.isdigit():
        return int(raw)
    return None


def _record_port(browser_id: str, port: int) -> None:
    """Persist the active debug port so later processes can reuse the browser."""
    port_file = _debug_port_path(browser_id)
    try:
        port_file.parent.mkdir(parents=True, exist_ok=True)
        port_file.write_text(str(port), encoding="utf-8")
    except OSError as exc:
        LOGGER.warning("Could not record the debug browser port at %s: %s", port_file, exc)


def _cdp_endpoint_alive(port: int) -> bool:
    """Return whether a CDP debug endpoint answers on the given loopback port."""
    endpoint = f"http://127.0.0.1:{port}/json/version"
    try:
        with urllib.request.urlopen(endpoint, timeout=CDP_PROBE_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return isinstance(payload, dict) and bool(payload.get("Browser"))


def _wait_for_cdp_ready(port: int, timeout_seconds: float) -> bool:
    """Poll the CDP endpoint until it answers or the deadline elapses."""
    deadline = time.time() + max(0.0, timeout_seconds)
    while time.time() < deadline:
        if _cdp_endpoint_alive(port):
            return True
        time.sleep(0.4)
    return False


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
    LOGGER.info("Starting debug %s on CDP port %s (profile: %s).", browser_id, port, user_data_dir)
    return subprocess.Popen(command, **popen_kwargs)  # type: ignore[arg-type]


def ensure_debug_browser(browser_id: str) -> DebugBrowserHandle:
    """Return a handle to a reachable debug browser, starting one if needed.

    First probes the last recorded port; if that endpoint is alive the existing
    browser is reused. Otherwise a fresh detached Chromium is launched against a
    dedicated user-data directory and the new port is recorded for later reuse.
    The browser process is intentionally left running after this call so that
    subsequent requests can reattach without paying another startup cost.
    """
    if not is_windows_host():
        raise RuntimeError("The project-owned debug browser is only supported on Windows.")
    if browser_id not in {"edge", "chrome"}:
        raise RuntimeError(f"The debug browser does not support {browser_id!r}.")

    recorded_port = _read_recorded_port(browser_id)
    if recorded_port is not None and _cdp_endpoint_alive(recorded_port):
        LOGGER.info("Reusing the running debug %s on CDP port %s.", browser_id, recorded_port)
        return DebugBrowserHandle(
            browser_id=browser_id,
            cdp_endpoint=f"http://127.0.0.1:{recorded_port}",
            user_data_dir=_debug_profile_dir(browser_id),
        )

    executable = _resolve_browser_executable(browser_id)
    if executable is None:
        raise RuntimeError(
            f"Could not find an installed {browser_id} executable to launch the debug browser."
        )

    port = _pick_free_port()
    process = _launch_debug_browser(browser_id, executable, port)
    if not _wait_for_cdp_ready(port, CDP_READY_TIMEOUT_SECONDS):
        with contextlib.suppress(Exception):
            process.terminate()
        raise RuntimeError(
            f"The debug {browser_id} did not expose its CDP endpoint on port {port} "
            f"within {int(CDP_READY_TIMEOUT_SECONDS)} seconds."
        )
    _record_port(browser_id, port)
    return DebugBrowserHandle(
        browser_id=browser_id,
        cdp_endpoint=f"http://127.0.0.1:{port}",
        user_data_dir=_debug_profile_dir(browser_id),
    )


def debug_browser_login_url(browser_id: str) -> str | None:
    """Return the visible debug browser's CDP endpoint if one is already running."""
    if not is_windows_host():
        return None
    recorded_port = _read_recorded_port(browser_id)
    if recorded_port is None or not _cdp_endpoint_alive(recorded_port):
        return None
    return f"http://127.0.0.1:{recorded_port}"
