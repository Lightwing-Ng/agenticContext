"""Lifecycle management for project-owned Chromium debug browsers.

On Windows a running Chrome or Edge keeps its sign-in cookies under an exclusive
OS lock, so the standard clone-then-launch path cannot reuse the user's live
login state. This module owns a separate Chromium instance launched with
``--remote-debugging-port`` against a dedicated user-data directory, so Playwright
can ``connect_over_cdp`` to it and read the authenticated session without ever
touching the locked profile files.

On macOS, Edge Jury uses the same verified-CDP primitives with a caller-supplied
project profile root. That path is deliberately separate from the daily Edge
profile and never copies browser or Microsoft account identity data. macOS Edge
Agent login and tasks reuse this same persistent debug browser over CDP, matching
Windows, so an already authorized window stays running even in Stage Manager.
macOS launches a new Edge instance against the project profile instead of
activating the daily browser, and leaves that window open for later reattach.

Code version: v1.24.6-codex.0
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import LOCAL_STORE_ROOT, is_macos_host, is_windows_host
from .platform_lock import lock_file, unlock_file


LOGGER = logging.getLogger(__name__)

DEBUG_BROWSER_ROOT = LOCAL_STORE_ROOT / "agent_browser_profile"
DEBUG_PORT_FILENAME = "debug_port"
DEVTOOLS_ACTIVE_PORT_FILENAME = "DevToolsActivePort"
CDP_READY_TIMEOUT_SECONDS = 20.0
CDP_PROBE_TIMEOUT_SECONDS = 3.0
CDP_REATTACH_TIMEOUT_SECONDS = 8.0
CDP_OCCUPIED_REATTACH_TIMEOUT_SECONDS = 20.0
CDP_CALLER_LOCK_TIMEOUT_SECONDS = 5.0

# Values reported by Chromium's ``/json/version`` ``Browser`` field. Microsoft
# Edge uses the product token ``Edg``; ``Edge`` is not a valid CDP product token.
_BRAND_PREFIXES: dict[str, str] = {"edge": "Edg/", "chrome": "Chrome/"}

# One reentrant lock per browser identity serializes the complete lifetime of a
# CDP caller. The Windows debug browser reuses one process and one rendered tab,
# while clone-profile launches remain isolated and do not use these locks.
_DEBUG_BROWSER_LOCKS: dict[str, threading.RLock] = {}
_DEBUG_BROWSER_LOCKS_GUARD = threading.Lock()
_DEBUG_BROWSER_START_LOCKS: dict[str, threading.RLock] = {}
_DEBUG_BROWSER_START_LOCKS_GUARD = threading.Lock()


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
    "--disable-session-crashed-bubble",
    "--disable-notifications",
    "--remote-allow-origins=*",
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


def _debug_profile_dir(
    browser_id: str,
    profile_root: Path | None = None,
) -> Path:
    """Return the persistent user-data directory for one debug browser."""
    return Path(profile_root or DEBUG_BROWSER_ROOT).expanduser() / browser_id


def _debug_port_path(
    browser_id: str,
    profile_root: Path | None = None,
) -> Path:
    """Return the file that records the last-used debug port for one browser."""
    return _debug_profile_dir(browser_id, profile_root) / DEBUG_PORT_FILENAME


def _devtools_active_port_path(
    browser_id: str,
    profile_root: Path | None = None,
) -> Path:
    """Return Chromium's launch-owned endpoint marker for one profile."""
    return _debug_profile_dir(browser_id, profile_root) / DEVTOOLS_ACTIVE_PORT_FILENAME


def _resolve_browser_executable(browser_id: str) -> str | None:
    """Resolve an installed Chromium executable without a circular import."""
    if is_macos_host():
        if browser_id != "edge":
            return None
        candidates = (
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            Path.home()
            / "Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        )
        return next(
            (
                str(candidate)
                for candidate in candidates
                if candidate.is_file() and os.access(candidate, os.X_OK)
            ),
            None,
        )
    # Lazy import: computer_use_agent imports browser_sessions, which would form a
    # cycle if this module imported it at load time. The call below only runs after
    # the application has finished importing, so the deferred import is safe.
    from .computer_use_agent import resolve_windows_browser_executable

    return resolve_windows_browser_executable(browser_id)


def _read_recorded_port(
    browser_id: str,
    profile_root: Path | None = None,
) -> int | None:
    """Return a previously recorded debug port, or None when it is absent."""
    target = _read_recorded_target(browser_id, profile_root)
    return target.port if target is not None else None


def _valid_port(value: object) -> int | None:
    """Return one valid TCP port while rejecting booleans and malformed values."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 < value <= 65_535 else None


def _read_small_text(path: Path) -> str | None:
    """Read one bounded runtime marker without accepting oversized content."""
    try:
        if path.is_symlink():
            return None
        if path.stat().st_size > 4_096:
            return None
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _read_recorded_target(
    browser_id: str,
    profile_root: Path | None = None,
) -> RecordedTarget | None:
    """Read the persisted CDP target, including legacy bare-port records."""
    port_file = _debug_port_path(browser_id, profile_root)
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


def _record_debug_target(
    browser_id: str,
    identity: CdpIdentity,
    profile_root: Path | None = None,
) -> None:
    """Atomically persist a reachable endpoint and its browser instance identity."""
    port_file = _debug_port_path(browser_id, profile_root)
    strict_project_marker = profile_root is not None and is_macos_host()
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
        if os.name != "nt":
            port_file.chmod(0o600)
    except OSError as exc:
        if strict_project_marker:
            raise RuntimeError(
                "The project Edge debug endpoint could not be recorded safely."
            ) from exc
        LOGGER.warning("Could not record the debug browser target at %s: %s", port_file, exc)
    finally:
        if temp_path is not None and temp_path.exists():
            with contextlib.suppress(OSError):
                temp_path.unlink()
    if strict_project_marker:
        recorded = _read_recorded_target(browser_id, profile_root)
        if recorded != RecordedTarget(
            port=identity.port,
            instance=identity.instance_guid,
            browser=identity.browser_brand,
        ):
            raise RuntimeError(
                "The project Edge debug endpoint could not be verified after it was recorded."
            )


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


def _read_devtools_active_target(
    browser_id: str,
    profile_root: Path | None = None,
) -> RecordedTarget | None:
    """Read the endpoint selected by the browser launched with port zero."""
    raw = _read_small_text(_devtools_active_port_path(browser_id, profile_root))
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


def _fetch_cdp_json(port: int, path: str) -> Any:
    """Return one JSON payload from the browser-level CDP HTTP endpoint."""
    endpoint = f"http://127.0.0.1:{int(port)}{path}"
    with urllib.request.urlopen(endpoint, timeout=CDP_PROBE_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    return payload


def _live_debug_port(
    browser_id: str,
    profile_root: Path | None = None,
) -> int | None:
    """Return a reachable project debug port without attaching Playwright."""
    recorded = (
        _read_recorded_target(browser_id)
        if profile_root is None
        else _read_recorded_target(browser_id, profile_root)
    )
    if recorded is not None and _cdp_endpoint_alive(recorded.port):
        return recorded.port
    live = (
        _read_devtools_active_target(browser_id)
        if profile_root is None
        else _read_devtools_active_target(browser_id, profile_root)
    )
    if live is not None and _cdp_endpoint_alive(live.port):
        return live.port
    return None


def list_debug_browser_page_targets(
    browser_id: str,
    profile_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Return open page targets via HTTP ``/json/list`` without enabling Runtime."""
    port = _live_debug_port(browser_id, profile_root)
    if port is None:
        return []
    try:
        payload = _fetch_cdp_json(port, "/json/list")
    except (urllib.error.URLError, OSError, ValueError):
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def open_debug_browser_url(
    browser_id: str,
    url: str,
    profile_root: Path | None = None,
) -> bool:
    """Open one URL through Chromium's HTTP ``/json/new`` endpoint.

    This avoids Playwright ``connect_over_cdp``, which enables Runtime on every
    page and restarts Cloudflare Turnstile.
    """
    port = _live_debug_port(browser_id, profile_root)
    destination = str(url or "").strip()
    if port is None or not destination:
        return False
    encoded = urllib.parse.quote(destination, safe=":/?&=%#")
    try:
        _fetch_cdp_json(port, f"/json/new?{encoded}")
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return True


def activate_debug_browser_target(
    browser_id: str,
    target_id: str,
    profile_root: Path | None = None,
) -> bool:
    """Focus one existing tab through Chromium's HTTP ``/json/activate`` endpoint."""
    port = _live_debug_port(browser_id, profile_root)
    token = str(target_id or "").strip()
    if port is None or not token or "/" in token or "\\" in token:
        return False
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{int(port)}/json/activate/{token}",
            timeout=CDP_PROBE_TIMEOUT_SECONDS,
        ) as response:
            response.read()
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return True


def _identity_matches_browser(identity: CdpIdentity, browser_id: str) -> bool:
    """Return whether a CDP product token matches the requested browser."""
    prefix = _BRAND_PREFIXES.get(browser_id)
    return prefix is not None and identity.browser_brand.startswith(prefix)


def _debug_profile_is_occupied(
    browser_id: str,
    profile_root: Path | None = None,
) -> bool:
    """Return whether Chromium still holds the project profile singleton."""
    profile_dir = _debug_profile_dir(browser_id, profile_root)
    for name in ("SingletonLock", "SingletonSocket"):
        marker = profile_dir / name
        try:
            if marker.is_symlink() or marker.exists():
                return True
        except OSError:
            continue
    return False


def _chromium_user_data_pid(user_data_dir: Path) -> int | None:
    """Return the Chromium PID encoded in a profile SingletonLock."""
    lock = Path(user_data_dir) / "SingletonLock"
    try:
        if not lock.is_symlink():
            return None
        target = os.readlink(lock)
    except OSError:
        return None
    suffix = str(target).rsplit("-", 1)[-1]
    if not suffix.isdigit():
        return None
    pid = int(suffix)
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def _debug_profile_browser_pid(
    browser_id: str,
    profile_root: Path | None = None,
) -> int | None:
    """Return the project browser PID encoded in Chromium's SingletonLock."""
    return _chromium_user_data_pid(_debug_profile_dir(browser_id, profile_root))


def debug_browser_command_line(
    browser_id: str,
    profile_root: Path | None = None,
) -> str:
    """Return the project browser command line, or an empty string."""
    pid = _debug_profile_browser_pid(browser_id, profile_root)
    if pid is None:
        return ""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return str(result.stdout or "").strip()


def bring_debug_browser_to_front(
    browser_id: str,
    profile_root: Path | None = None,
) -> bool:
    """Raise the project debug process by PID without activating daily Edge."""
    pid = _debug_profile_browser_pid(browser_id, profile_root)
    if pid is None or not is_macos_host():
        return False
    try:
        result = subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                (
                    'tell application "System Events" to set frontmost of '
                    f"(first process whose unix id is {pid}) to true"
                ),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def restart_debug_browser(
    browser_id: str,
    *,
    start_url: str = "",
    profile_root: Path | None = None,
) -> DebugBrowserHandle:
    """Quit the occupied project browser and start a replacement in-place.

    Used only for a human login window that still carries ``--disable-extensions``,
    which Cloudflare treats as automation. The same profile is reused.
    """
    if not debug_browser_supported(browser_id):
        raise RuntimeError(
            "The project-owned debug browser supports Edge on macOS and "
            "Edge or Chrome on Windows."
        )
    with _debug_browser_startup_lock(browser_id, profile_root):
        pid = _debug_profile_browser_pid(browser_id, profile_root)
        if pid is not None:
            LOGGER.info(
                "Stopping debug %s pid %s so login can continue without --disable-extensions.",
                browser_id,
                pid,
            )
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)
            deadline = time.time() + 12.0
            while time.time() < deadline and _debug_profile_is_occupied(
                browser_id, profile_root
            ):
                time.sleep(0.4)
            if _debug_profile_is_occupied(browser_id, profile_root):
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)
                deadline = time.time() + 5.0
                while time.time() < deadline and _debug_profile_is_occupied(
                    browser_id, profile_root
                ):
                    time.sleep(0.4)
        if profile_root is None:
            _clear_devtools_active_port(browser_id)
        else:
            _clear_devtools_active_port(browser_id, profile_root)
        port_file = _debug_port_path(browser_id, profile_root)
        with contextlib.suppress(FileNotFoundError, OSError):
            port_file.unlink()
    handle = ensure_debug_browser(browser_id, profile_root=profile_root)
    destination = str(start_url or "").strip()
    if destination:
        open_debug_browser_url(browser_id, destination, profile_root)
    return handle


def _debug_browser_handle(
    browser_id: str,
    identity: CdpIdentity,
    profile_root: Path | None = None,
) -> DebugBrowserHandle:
    """Return a handle for one verified debug-browser identity."""
    return DebugBrowserHandle(
        browser_id=browser_id,
        cdp_endpoint=f"http://127.0.0.1:{identity.port}",
        user_data_dir=_debug_profile_dir(browser_id, profile_root),
    )


def _record_debug_browser_handle(
    browser_id: str,
    identity: CdpIdentity,
    profile_root: Path | None = None,
) -> DebugBrowserHandle:
    """Persist one live identity and return its handle."""
    if profile_root is None:
        _record_debug_target(browser_id, identity)
    else:
        _record_debug_target(browser_id, identity, profile_root)
    return _debug_browser_handle(browser_id, identity, profile_root)


def _adopt_running_debug_browser(
    browser_id: str,
    profile_root: Path | None = None,
) -> DebugBrowserHandle | None:
    """Reuse a still-running project browser instead of launching a replacement.

    ``open -n`` against an occupied profile starts a second Edge, fights the
    singleton lock, and restarts ChatGPT's Cloudflare loop.
    """
    live = (
        _read_devtools_active_target(browser_id)
        if profile_root is None
        else _read_devtools_active_target(browser_id, profile_root)
    )
    recorded = (
        _read_recorded_target(browser_id)
        if profile_root is None
        else _read_recorded_target(browser_id, profile_root)
    )
    if (
        recorded is not None
        and profile_root is not None
        and is_macos_host()
        and recorded.instance is None
    ):
        recorded = None
    candidates: list[tuple[int, str | None]] = []
    seen_ports: set[int] = set()
    if live is not None:
        candidates.append((live.port, live.instance))
        seen_ports.add(live.port)
    if recorded is not None and recorded.port not in seen_ports:
        candidates.append((recorded.port, None))

    occupied = _debug_profile_is_occupied(browser_id, profile_root)
    for port, guid in candidates:
        identity = _wait_for_cdp_ready(
            port,
            CDP_REATTACH_TIMEOUT_SECONDS if occupied else CDP_PROBE_TIMEOUT_SECONDS,
            expected_guid=guid,
        )
        if identity is None and guid is not None:
            identity = _wait_for_cdp_ready(
                port,
                CDP_PROBE_TIMEOUT_SECONDS,
                expected_guid=None,
            )
        if identity is not None and _identity_matches_browser(identity, browser_id):
            LOGGER.info(
                "Reusing the running debug %s on CDP port %s without launching a replacement.",
                browser_id,
                identity.port,
            )
            return _record_debug_browser_handle(browser_id, identity, profile_root)

    if not occupied:
        return None
    wait_port = live.port if live is not None else (
        recorded.port if recorded is not None else None
    )
    if wait_port is None:
        return None
    identity = _wait_for_cdp_ready(
        wait_port,
        CDP_OCCUPIED_REATTACH_TIMEOUT_SECONDS,
        expected_guid=None,
    )
    if identity is None or not _identity_matches_browser(identity, browser_id):
        return None
    LOGGER.info(
        "Reusing the occupied debug %s on CDP port %s after a CDP gap.",
        browser_id,
        identity.port,
    )
    return _record_debug_browser_handle(browser_id, identity, profile_root)


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


def _clear_devtools_active_port(
    browser_id: str,
    profile_root: Path | None = None,
) -> None:
    """Remove the old launch marker before asking Chromium to choose a new port."""
    marker = _devtools_active_port_path(browser_id, profile_root)
    try:
        marker.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError(
            f"Could not clear the stale debug {browser_id} endpoint marker."
        ) from exc


def _wait_for_launched_cdp(
    browser_id: str,
    timeout_seconds: float,
    profile_root: Path | None = None,
) -> CdpIdentity | None:
    """Wait for the profile marker and live endpoint from this launch to agree."""
    deadline = time.time() + max(0.0, timeout_seconds)
    while time.time() < deadline:
        target = (
            _read_devtools_active_target(browser_id)
            if profile_root is None
            else _read_devtools_active_target(browser_id, profile_root)
        )
        if target is not None and target.instance is not None:
            identity = _probe_cdp_identity(target.port)
            if identity is not None and identity.instance_guid == target.instance:
                if os.name != "nt":
                    with contextlib.suppress(OSError):
                        _devtools_active_port_path(
                            browser_id,
                            profile_root,
                        ).chmod(0o600)
                return identity
        time.sleep(0.4)
    return None


def _prepare_private_profile_directory(
    browser_id: str,
    profile_root: Path | None = None,
) -> Path:
    """Create an owner-only project profile without accepting link replacement."""
    root = Path(profile_root or DEBUG_BROWSER_ROOT).expanduser()
    profile_dir = _debug_profile_dir(browser_id, root)
    for directory in (root, profile_dir):
        if directory.is_symlink():
            raise RuntimeError(
                f"The project debug {browser_id} profile path must not be a symbolic link."
            )
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            metadata = directory.stat()
        except OSError as exc:
            raise RuntimeError(
                f"The project debug {browser_id} profile path is unavailable."
            ) from exc
        if not directory.is_dir():
            raise RuntimeError(
                f"The project debug {browser_id} profile path is not a directory."
            )
        getuid = getattr(os, "getuid", None)
        if callable(getuid) and metadata.st_uid != getuid():
            raise RuntimeError(
                f"The project debug {browser_id} profile is owned by another user."
            )
        if os.name != "nt":
            directory.chmod(0o700)
    return profile_dir


@contextlib.contextmanager
def _debug_browser_startup_lock(
    browser_id: str,
    profile_root: Path | None = None,
):
    """Serialize one profile launch across threads and macOS service processes."""
    profile_dir = _prepare_private_profile_directory(browser_id, profile_root)
    key = str(profile_dir.absolute())
    with _DEBUG_BROWSER_START_LOCKS_GUARD:
        thread_lock = _DEBUG_BROWSER_START_LOCKS.setdefault(key, threading.RLock())
    with thread_lock:
        if not is_macos_host():
            yield
            return
        lock_path = profile_dir / ".launch.lock"
        if lock_path.is_symlink():
            raise RuntimeError(
                f"The project debug {browser_id} launch lock must not be a symbolic link."
            )
        handle = lock_path.open("a+", encoding="utf-8")
        try:
            lock_path.chmod(0o600)
            lock_file(handle)
            try:
                yield
            finally:
                unlock_file(handle)
        finally:
            handle.close()


def _macos_application_bundle(executable: str) -> Path | None:
    """Return the enclosing .app bundle for one macOS browser executable."""
    path = Path(executable)
    for candidate in (path, *path.parents):
        if candidate.suffix == ".app":
            return candidate
    return None


def _launch_debug_browser(
    browser_id: str,
    executable: str,
    port: int,
    profile_root: Path | None = None,
) -> subprocess.Popen[bytes]:
    """Start one detached Chromium instance exposing a CDP debug endpoint."""
    user_data_dir = _prepare_private_profile_directory(browser_id, profile_root)
    launch_args = [
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        *_DEFAULT_VIEWPORT_ARGS,
    ]
    macos_bundle = _macos_application_bundle(executable) if is_macos_host() else None
    if macos_bundle is not None:
        # ``open -n`` starts a second Edge against the project profile instead of
        # activating the already-running daily browser, which has no CDP port.
        command = [
            "/usr/bin/open",
            "-n",
            "-a",
            str(macos_bundle),
            "--args",
            *launch_args,
        ]
    else:
        command = [executable, *launch_args]
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
    elif is_macos_host():
        popen_kwargs["start_new_session"] = True
    LOGGER.info(
        "Starting debug %s with an OS-selected CDP port (profile: %s).",
        browser_id,
        user_data_dir,
    )
    return subprocess.Popen(command, **popen_kwargs)  # type: ignore[arg-type]


def _wait_for_user_data_cdp(
    user_data_dir: Path,
    browser_id: str,
    timeout_seconds: float,
) -> CdpIdentity:
    """Wait until a launched user-data directory publishes a matching CDP endpoint."""
    marker = Path(user_data_dir) / DEVTOOLS_ACTIVE_PORT_FILENAME
    deadline = time.time() + max(0.0, timeout_seconds)
    while time.time() < deadline:
        raw = _read_small_text(marker)
        if raw:
            lines = raw.splitlines()
            if len(lines) >= 2 and lines[0].strip().isdigit():
                port = _valid_port(int(lines[0].strip()))
                instance = _instance_guid_from_websocket(lines[1].strip())
                if port is not None and instance:
                    identity = _probe_cdp_identity(port)
                    if (
                        identity is not None
                        and identity.instance_guid == instance
                        and _identity_matches_browser(identity, browser_id)
                    ):
                        return identity
        time.sleep(0.4)
    raise RuntimeError(
        f"The cloned {browser_id} did not publish a verified CDP endpoint "
        f"within {int(timeout_seconds)} seconds."
    )


def launch_owned_user_data_over_cdp(
    browser_id: str,
    user_data_dir: Path,
    *,
    extra_args: tuple[str, ...] = (),
) -> tuple[subprocess.Popen[bytes], CdpIdentity]:
    """Launch one native Chromium against a clone directory and expose CDP.

    Playwright ``launch_persistent_context`` injects automation that ChatGPT can
    reject on Send. Windows Agent tasks succeed by connecting to a native Edge
    over CDP; macOS Agent tasks reuse that shape on a daily-profile clone.
    """
    executable = _resolve_browser_executable(browser_id)
    if not executable:
        raise RuntimeError(f"Could not find an installed {browser_id} executable.")
    marker = Path(user_data_dir) / DEVTOOLS_ACTIVE_PORT_FILENAME
    with contextlib.suppress(FileNotFoundError, OSError):
        marker.unlink()
    launch_args = [
        "--remote-debugging-port=0",
        f"--user-data-dir={user_data_dir}",
        *_DEFAULT_VIEWPORT_ARGS,
        *extra_args,
    ]
    macos_bundle = _macos_application_bundle(executable) if is_macos_host() else None
    if macos_bundle is not None:
        command = [
            "/usr/bin/open",
            "-n",
            "-a",
            str(macos_bundle),
            "--args",
            *launch_args,
        ]
    else:
        command = [executable, *launch_args]
    popen_kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if is_macos_host():
        popen_kwargs["start_new_session"] = True
    LOGGER.info(
        "Starting a native %s clone over CDP (profile: %s).",
        browser_id,
        user_data_dir,
    )
    process = subprocess.Popen(command, **popen_kwargs)  # type: ignore[arg-type]
    try:
        identity = _wait_for_user_data_cdp(
            user_data_dir,
            browser_id,
            CDP_READY_TIMEOUT_SECONDS,
        )
    except Exception:
        terminate_owned_chromium(user_data_dir, process)
        raise
    return process, identity


def terminate_owned_chromium(
    user_data_dir: Path,
    process: subprocess.Popen[bytes] | None = None,
) -> None:
    """Stop one owned Chromium clone without touching the daily browser."""
    pid = _chromium_user_data_pid(user_data_dir)
    if pid is not None:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
        deadline = time.time() + 8.0
        while time.time() < deadline and _chromium_user_data_pid(user_data_dir) == pid:
            time.sleep(0.2)
        if _chromium_user_data_pid(user_data_dir) == pid:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
    if process is not None and process.poll() is None:
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            process.terminate()
            process.wait(timeout=3)


def debug_browser_supported(browser_id: str) -> bool:
    """Return whether this host owns a persistent debug browser for the id."""
    if browser_id not in _BRAND_PREFIXES:
        return False
    if is_windows_host():
        return True
    return is_macos_host() and browser_id == "edge"


def ensure_debug_browser(
    browser_id: str,
    *,
    profile_root: Path | None = None,
) -> DebugBrowserHandle:
    """Return a handle to a reachable debug browser, starting one if needed.

    A recorded endpoint is reused only when its live product and instance identity
    still match. Fresh launches ask Chromium to bind an OS-selected port and trust
    it only after the profile-owned ``DevToolsActivePort`` marker agrees with the
    live endpoint, avoiding a free-port selection race. The browser intentionally
    remains running so subsequent requests can reattach.
    """
    if not debug_browser_supported(browser_id):
        raise RuntimeError(
            "The project-owned debug browser supports Edge on macOS and "
            "Edge or Chrome on Windows."
        )
    with _debug_browser_startup_lock(browser_id, profile_root):
        return _ensure_debug_browser_locked(browser_id, profile_root=profile_root)


def _ensure_debug_browser_locked(
    browser_id: str,
    *,
    profile_root: Path | None = None,
) -> DebugBrowserHandle:
    """Return one verified endpoint while its launch lock is held."""
    if browser_id not in {"edge", "chrome"}:
        raise RuntimeError(f"The debug browser does not support {browser_id!r}.")

    recorded = (
        _read_recorded_target(browser_id)
        if profile_root is None
        else _read_recorded_target(browser_id, profile_root)
    )
    if (
        recorded is not None
        and profile_root is not None
        and is_macos_host()
        and recorded.instance is None
    ):
        LOGGER.warning(
            "Ignoring a legacy debug Edge marker without a browser instance identity."
        )
        recorded = None
    if recorded is not None:
        identity = _wait_for_cdp_ready(
            recorded.port,
            CDP_PROBE_TIMEOUT_SECONDS,
            expected_guid=recorded.instance,
        )
        if identity is not None and _identity_matches_browser(identity, browser_id):
            if recorded.instance is None:
                if profile_root is None:
                    _record_debug_target(browser_id, identity)
                else:
                    _record_debug_target(browser_id, identity, profile_root)
            LOGGER.info(
                "Reusing the running debug %s on CDP port %s.",
                browser_id,
                recorded.port,
            )
            return DebugBrowserHandle(
                browser_id=browser_id,
                cdp_endpoint=f"http://127.0.0.1:{recorded.port}",
                user_data_dir=_debug_profile_dir(browser_id, profile_root),
            )
        LOGGER.info(
            "Retrying CDP attach to the recorded debug %s before launching a replacement.",
            browser_id,
        )
        identity = _wait_for_cdp_ready(
            recorded.port,
            CDP_REATTACH_TIMEOUT_SECONDS,
            expected_guid=recorded.instance,
        )
        if identity is not None and _identity_matches_browser(identity, browser_id):
            LOGGER.info(
                "Reusing the running debug %s on CDP port %s after a brief CDP gap.",
                browser_id,
                recorded.port,
            )
            return DebugBrowserHandle(
                browser_id=browser_id,
                cdp_endpoint=f"http://127.0.0.1:{recorded.port}",
                user_data_dir=_debug_profile_dir(browser_id, profile_root),
            )
        LOGGER.info(
            "The recorded debug %s target is not reachable; looking for the existing project process.",
            browser_id,
        )

    adopted = _adopt_running_debug_browser(browser_id, profile_root)
    if adopted is not None:
        return adopted
    if _debug_profile_is_occupied(browser_id, profile_root):
        raise RuntimeError(
            f"The project debug {browser_id} is still running. "
            "Complete any human verification in that open window, then Recheck. "
            "A second instance will not be launched against the same profile."
        )
    if recorded is not None:
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

    _prepare_private_profile_directory(browser_id, profile_root)
    if profile_root is None:
        _clear_devtools_active_port(browser_id)
        process = _launch_debug_browser(browser_id, executable, 0)
        identity = _wait_for_launched_cdp(
            browser_id,
            CDP_READY_TIMEOUT_SECONDS,
        )
    else:
        _clear_devtools_active_port(browser_id, profile_root)
        process = _launch_debug_browser(browser_id, executable, 0, profile_root)
        identity = _wait_for_launched_cdp(
            browser_id,
            CDP_READY_TIMEOUT_SECONDS,
            profile_root,
        )
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
    try:
        if profile_root is None:
            _record_debug_target(browser_id, identity)
        else:
            _record_debug_target(browser_id, identity, profile_root)
    except Exception:
        with contextlib.suppress(Exception):
            process.terminate()
        raise
    return DebugBrowserHandle(
        browser_id=browser_id,
        cdp_endpoint=f"http://127.0.0.1:{identity.port}",
        user_data_dir=_debug_profile_dir(browser_id, profile_root),
    )


def debug_browser_login_url(
    browser_id: str,
    *,
    profile_root: Path | None = None,
) -> str | None:
    """Return the recorded endpoint only while its complete identity still matches."""
    if not debug_browser_supported(browser_id):
        return None
    recorded = _read_recorded_target(browser_id, profile_root)
    if recorded is None:
        return None
    if is_macos_host() and browser_id == "edge" and recorded.instance is None:
        return None
    identity = _wait_for_cdp_ready(
        recorded.port,
        CDP_PROBE_TIMEOUT_SECONDS,
        expected_guid=recorded.instance,
    )
    if identity is None or not _identity_matches_browser(identity, browser_id):
        return None
    return f"http://127.0.0.1:{recorded.port}"


def debug_browser_profile_initialized(
    browser_id: str,
    *,
    profile_root: Path | None = None,
) -> bool:
    """Return whether one debug profile completed a prior successful launch."""
    if not debug_browser_supported(browser_id):
        return False
    recorded = _read_recorded_target(browser_id, profile_root)
    if recorded is None:
        return False
    if is_macos_host() and browser_id == "edge" and recorded.instance is None:
        return False
    return True
