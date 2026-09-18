"""Supervise OpenAI's tunnel-client so ChatGPT can reach the local MCP endpoint.

Code version: v1.1.0-codex.0

The Tunnel connection has three parts:

1. ``/mcp`` on this app serves the project tools (see ``tunnel_mcp``).
2. OpenAI's official ``tunnel-client`` polls the OpenAI control plane with the
   saved Tunnel ID and API key, then forwards each ChatGPT request to ``/mcp``
   with a per-start bearer token that only this process knows.
3. The ChatGPT app configured with Connection = Tunnel sends tool calls into
   that Tunnel.

The client binary is pinned by version and SHA-256. It is installed from the
official GitHub release only when no verified copy exists.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import platform
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.tunnel_credentials import TunnelCredentials

LOGGER = logging.getLogger(__name__)

TUNNEL_CLIENT_VERSION = "0.0.14"
TUNNEL_CLIENT_RELEASE_BASE = (
    f"https://github.com/openai/tunnel-client/releases/download/v{TUNNEL_CLIENT_VERSION}"
)
TUNNEL_CLIENT_BIN_ENV = "AGENTIC_CONTEXT_TUNNEL_CLIENT_BIN"
TUNNEL_CLIENT_MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
TUNNEL_READY_TIMEOUT_SECONDS = 60.0
TUNNEL_HEALTH_PROBE_INTERVAL_SECONDS = 5.0
TUNNEL_RESTART_DEBOUNCE_SECONDS = 2.0
TUNNEL_MAX_BACKOFF_SECONDS = 60.0
TUNNEL_ID_PREFIX = "tunnel_"
LOOPBACK_NO_PROXY = "127.0.0.1,localhost,::1"


@dataclass(frozen=True, slots=True)
class TunnelClientAsset:
    """One pinned release asset for a host platform."""

    target: str
    archive_sha256: str
    binary_sha256: str
    member_name: str

    @property
    def file_name(self) -> str:
        return f"tunnel-client-v{TUNNEL_CLIENT_VERSION}-{self.target}.zip"


TUNNEL_CLIENT_ASSETS: dict[tuple[str, str], TunnelClientAsset] = {
    ("darwin", "arm64"): TunnelClientAsset(
        "darwin-arm64",
        "b540493c5bdbcdbb755700c8e2e16597e28b1569e425007e0f73111047bd6a64",
        "309fd85da5a8c2ca8dae920deea8ac10a4d7934ed18ac46e7df0c200139cc9c5",
        "tunnel-client",
    ),
    ("darwin", "amd64"): TunnelClientAsset(
        "darwin-amd64",
        "75e10be774184fb42189e347b16eb6bc9fb0780135d8af714d34e30ce068dc53",
        "89478d1d58350818275b852169745e1af0e18c02ff9b5b46d50df22018c95be9",
        "tunnel-client",
    ),
    ("linux", "amd64"): TunnelClientAsset(
        "linux-amd64",
        "15bd17e805cad39d412199115bb9e10a978dd35258a114cdf25dd2ae6681c7d3",
        "472eb9dd9dd625b4e6023c3b4a5736b3a2e5a1b6dbe9338e001887a64ec992a6",
        "tunnel-client",
    ),
    ("linux", "arm64"): TunnelClientAsset(
        "linux-arm64",
        "2de3fb879a18edb847e0313592c912f1983685488290a7fdba7ac403e6a4fb0a",
        "ab6c05258f15dc43a8e23f39460beb69892a8ced03e4c345a6f1aef0dd009b0f",
        "tunnel-client",
    ),
    ("windows", "amd64"): TunnelClientAsset(
        "windows-amd64",
        "784ab8da7b5a88f0109f1fd8aaf0a1c86067430b896dddf307ef7e3cc49fa1a5",
        "fcc85a69ec0ad82518e4f8964f60c45e31787957782a0fc9c1b0c44e82d61b9b",
        "tunnel-client.exe",
    ),
    ("windows", "arm64"): TunnelClientAsset(
        "windows-arm64",
        "fa775db8897df543dd4ba66404f69492a2acfbc6a291f10df27aced064a16568",
        "7260ec886a7efd34202c6506bd35b068e94723a5402ea6f76af5a3af3dbd0a0b",
        "tunnel-client.exe",
    ),
}


class TunnelRuntimeError(RuntimeError):
    """A user-facing Tunnel failure."""


def host_tunnel_client_asset(
    system: str | None = None,
    machine: str | None = None,
) -> TunnelClientAsset:
    """Return the pinned asset for this host."""
    system_name = (system or sys.platform).lower()
    if system_name.startswith("win"):
        system_name = "windows"
    elif system_name.startswith("linux"):
        system_name = "linux"
    machine_name = (machine or platform.machine()).lower()
    machine_name = {"x86_64": "amd64", "aarch64": "arm64"}.get(machine_name, machine_name)
    asset = TUNNEL_CLIENT_ASSETS.get((system_name, machine_name))
    if asset is None:
        raise TunnelRuntimeError(
            f"OpenAI tunnel-client is not available for {system_name}/{machine_name}."
        )
    return asset


def valid_tunnel_id(value: str) -> bool:
    """Return whether a Tunnel ID has the OpenAI ``tunnel_<32 hex>`` shape."""
    suffix = value.removeprefix(TUNNEL_ID_PREFIX)
    return (
        value.startswith(TUNNEL_ID_PREFIX)
        and len(suffix) == 32
        and all(character in "0123456789abcdef" for character in suffix)
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def detect_outbound_proxy() -> str:
    """Return the HTTPS proxy the control plane should use, or an empty string."""
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    if sys.platform != "darwin":
        return ""
    try:
        output = subprocess.run(
            ["scutil", "--proxy"], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition(" : ")
        if separator:
            values[key.strip()] = value.strip()
    for prefix in ("HTTPS", "HTTP"):
        if values.get(f"{prefix}Enable") == "1" and values.get(f"{prefix}Proxy"):
            port = values.get(f"{prefix}Port", "")
            host = values[f"{prefix}Proxy"]
            return f"http://{host}:{port}" if port else f"http://{host}"
    return ""


class TunnelRuntime:
    """Own one tunnel-client process for the saved Tunnel credentials."""

    def __init__(
        self,
        *,
        credentials_loader: Callable[[], TunnelCredentials],
        state_root: Path,
        activity_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._credentials_loader = credentials_loader
        self._state_root = state_root
        self._activity_provider = activity_provider or (lambda: {})
        self._lock = threading.RLock()
        self._mcp_url = ""
        self._enabled = False
        self._shutdown = False
        self._generation = 0
        self._process: subprocess.Popen[bytes] | None = None
        self._restart_timer: threading.Timer | None = None
        self._authorization = ""
        self._state = "disabled"
        self._message = "The Tunnel starts with the local service."
        self._ready_since: float | None = None
        self._started_at: float | None = None
        self._health_url = ""

    # Paths ----------------------------------------------------------------

    @property
    def tools_root(self) -> Path:
        return self._state_root / "tools" / "tunnel-client" / TUNNEL_CLIENT_VERSION

    @property
    def log_path(self) -> Path:
        return self._state_root / "tunnel-client.log"

    @property
    def _pid_path(self) -> Path:
        return self._state_root / "tunnel-client.pid"

    @property
    def _health_url_path(self) -> Path:
        return self._state_root / "tunnel-client-health.url"

    @property
    def _authorization_path(self) -> Path:
        return self._state_root / "mcp-authorization"

    # Public control ---------------------------------------------------------

    def enable(self, mcp_url: str) -> None:
        """Start supervising the Tunnel for this service's MCP URL."""
        with self._lock:
            self._mcp_url = mcp_url
            self._enabled = True
        self.restart()

    def restart(self) -> None:
        """Stop any client and start a new generation when credentials exist."""
        with self._lock:
            if not self._enabled or self._shutdown:
                return
            self._cancel_restart_timer()
            self._generation += 1
            generation = self._generation
            self._terminate_process()
            self._set_state("starting", "Connecting the Tunnel…")
        threading.Thread(
            target=self._supervise,
            args=(generation,),
            name="tunnel-client-supervisor",
            daemon=True,
        ).start()

    def request_restart(self, delay: float = TUNNEL_RESTART_DEBOUNCE_SECONDS) -> None:
        """Restart after edits settle, so typing a key does not thrash the client."""
        with self._lock:
            if not self._enabled or self._shutdown:
                return
            self._cancel_restart_timer()
            self._restart_timer = threading.Timer(delay, self.restart)
            self._restart_timer.daemon = True
            self._restart_timer.start()

    def stop(self) -> None:
        """Stop the client for good; used when the local service exits."""
        with self._lock:
            self._shutdown = True
            self._generation += 1
            self._cancel_restart_timer()
            self._terminate_process()
            self._set_state("stopped", "The Tunnel stopped with the local service.")

    def authorization_matches(self, header_value: str | None) -> bool:
        """Return whether a request carries this start's bearer token."""
        expected = self._authorization
        return bool(expected) and secrets.compare_digest(
            str(header_value or "").encode(), expected.encode()
        )

    def snapshot(self) -> dict[str, Any]:
        """Return a UI-safe status record; it never includes secrets."""
        credentials = self._credentials_loader()
        with self._lock:
            state = self._state
            message = self._message
            if not credentials.configured:
                state, message = "not_configured", (
                    "Save the Tunnel ID and API key in Settings to connect ChatGPT."
                )
            return {
                "state": state,
                "ready": state == "ready",
                "configured": credentials.configured,
                "message": message,
                "ready_since": self._ready_since,
                "client_version": TUNNEL_CLIENT_VERSION,
                **self._activity_provider(),
            }

    # Supervision ------------------------------------------------------------

    def _set_state(self, state: str, message: str) -> None:
        with self._lock:
            if state == "ready" and self._state != "ready":
                self._ready_since = time.time()
            elif state != "ready":
                self._ready_since = None
            self._state = state
            self._message = message

    def _current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation and not self._shutdown

    def _supervise(self, generation: int) -> None:
        backoff = 5.0
        while self._current(generation):
            credentials = self._credentials_loader()
            if not credentials.configured:
                self._set_state("not_configured", "Save the Tunnel ID and API key in Settings.")
                return
            if not valid_tunnel_id(credentials.tunnel_id):
                self._set_state(
                    "error",
                    "The Tunnel ID must look like tunnel_ followed by 32 lowercase hexadecimal characters.",
                )
                return
            try:
                binary = self._resolve_binary(generation)
                process = self._spawn(binary, credentials)
            except TunnelRuntimeError as exc:
                self._set_state("error", str(exc))
                return
            with self._lock:
                if not self._current(generation):
                    _terminate(process)
                    return
                self._process = process
                self._started_at = time.monotonic()
            self._monitor(generation, process)
            if not self._current(generation):
                return
            exit_code = process.poll()
            detail = self._last_log_problem() or f"exit code {exit_code}"
            self._set_state("error", f"The Tunnel stopped: {detail}. Retrying in {int(backoff)} s.")
            if not self._sleep(generation, backoff):
                return
            backoff = min(backoff * 2, TUNNEL_MAX_BACKOFF_SECONDS)
            self._set_state("starting", "Reconnecting the Tunnel…")

    def _monitor(self, generation: int, process: subprocess.Popen[bytes]) -> None:
        """Track readiness until the process exits or a new generation starts."""
        deadline = time.monotonic() + TUNNEL_READY_TIMEOUT_SECONDS
        while self._current(generation) and process.poll() is None:
            if self._probe_ready():
                self._set_state(
                    "ready",
                    "Connected. ChatGPT reaches this computer directly; no separate desktop app is needed.",
                )
            elif time.monotonic() > deadline:
                detail = self._last_log_problem() or "waiting for the OpenAI control plane"
                self._set_state("starting", f"Still connecting: {detail}.")
            self._sleep(generation, TUNNEL_HEALTH_PROBE_INTERVAL_SECONDS if self._state == "ready" else 1.0)

    def _sleep(self, generation: int, seconds: float) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if not self._current(generation):
                return False
            time.sleep(min(0.25, end - time.monotonic()))
        return self._current(generation)

    def _probe_ready(self) -> bool:
        if not self._health_url:
            try:
                value = self._health_url_path.read_text().strip()
            except OSError:
                return False
            if not value.startswith(("http://127.0.0.1:", "http://localhost:", "http://[::1]:")):
                return False
            self._health_url = value.rstrip("/")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(f"{self._health_url}/readyz", timeout=2) as response:
                return 200 <= response.status < 300
        except (OSError, urllib.error.URLError):
            return False

    def _spawn(self, binary: Path, credentials: TunnelCredentials) -> subprocess.Popen[bytes]:
        if not self._mcp_url:
            raise TunnelRuntimeError("The local MCP endpoint is not configured.")
        self._state_root.mkdir(parents=True, exist_ok=True)
        self._stop_orphan()
        self._authorization = f"Bearer {secrets.token_urlsafe(32)}"
        _write_owner_only(self._authorization_path, self._authorization)
        self._health_url = ""
        self._health_url_path.unlink(missing_ok=True)
        if self.log_path.exists():
            os.replace(self.log_path, self.log_path.with_suffix(".log.1"))
        env = {
            key: value
            for key, value in os.environ.items()
            if key.upper()
            not in {
                "OPENAI_API_KEY",
                "OPENAI_ADMIN_KEY",
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "NO_PROXY",
            }
        }
        env["CONTROL_PLANE_API_KEY"] = credentials.api_key
        env["CONTROL_PLANE_TUNNEL_ID"] = credentials.tunnel_id
        proxy = detect_outbound_proxy()
        if proxy:
            env.update({"HTTPS_PROXY": proxy, "HTTP_PROXY": proxy})
        env["NO_PROXY"] = LOOPBACK_NO_PROXY
        command = [
            str(binary),
            "run",
            "--control-plane.tunnel-id",
            credentials.tunnel_id,
            "--mcp.server-url",
            f"url={self._mcp_url},channel=main",
            "--mcp.extra-headers",
            f"Authorization: file:{self._authorization_path}",
            "--mcp.startup-wait-timeout",
            "30s",
            "--health.listen-addr",
            "127.0.0.1:0",
            "--health.url-file",
            str(self._health_url_path),
            "--pid.file",
            str(self._pid_path),
            "--log.file",
            str(self.log_path),
            "--log.format",
            "json",
            "--log.level",
            "info",
        ]
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            return subprocess.Popen(
                command,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creation_flags,
            )
        except OSError as exc:
            raise TunnelRuntimeError(f"OpenAI tunnel-client could not start: {exc}") from exc

    def _terminate_process(self) -> None:
        process, self._process = self._process, None
        self._authorization = ""
        if process is not None:
            _terminate(process)
            # Only a client this service owned clears the pid file; a stale file from a
            # killed service must survive until _stop_orphan() has read it.
            self._pid_path.unlink(missing_ok=True)

    def _cancel_restart_timer(self) -> None:
        if self._restart_timer is not None:
            self._restart_timer.cancel()
            self._restart_timer = None

    def _stop_orphan(self) -> None:
        """Stop clients left behind by a previous service that was killed.

        A client deletes its own pid file on a clean exit, so the process table is
        scanned too; only commands that reference this service's state folder match.
        """
        if os.name == "nt":
            return
        try:
            listing = subprocess.run(
                ["ps", "-ww", "-ax", "-o", "pid=,command="],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            return
        marker = str(self._state_root)
        orphans = []
        for line in listing.splitlines():
            pid_text, _, command = line.strip().partition(" ")
            if (
                pid_text.isdigit()
                and int(pid_text) != os.getpid()
                and "tunnel-client" in command
                and " run " in command
                and marker in command
            ):
                orphans.append(int(pid_text))
        for pid in orphans:
            LOGGER.info("Stopping orphaned tunnel-client %s", pid)
            try:
                os.kill(pid, signal.SIGTERM)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    os.kill(pid, 0)
                    time.sleep(0.1)
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        self._pid_path.unlink(missing_ok=True)

    def _last_log_problem(self) -> str:
        """Return the newest warning or error from the client's JSON log."""
        try:
            with self.log_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - 65_536))
                lines = handle.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return ""
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(record.get("level", "")).lower() not in {"error", "warn", "warning", "fatal"}:
                continue
            message = str(record.get("msg") or record.get("message") or "").strip()
            if message.startswith("OAuth discovery"):
                # Expected: the ChatGPT app uses "No auth", so this server publishes no
                # OAuth metadata and readiness does not depend on it.
                continue
            error = str(record.get("error") or record.get("err") or "").strip()
            text = f"{message}: {error}" if message and error else message or error
            return text[:300]
        return ""

    # Binary installation ---------------------------------------------------------

    def _resolve_binary(self, generation: int) -> Path:
        override = os.environ.get(TUNNEL_CLIENT_BIN_ENV, "").strip()
        if override:
            path = Path(override).expanduser()
            if not path.is_file():
                raise TunnelRuntimeError(f"{TUNNEL_CLIENT_BIN_ENV} does not point to a file.")
            return path
        asset = host_tunnel_client_asset()
        binary = self.tools_root / asset.target / asset.member_name
        if binary.is_file() and _sha256_file(binary) == asset.binary_sha256:
            return binary
        if not self._current(generation):
            raise TunnelRuntimeError("The Tunnel start was superseded.")
        self._set_state("installing", f"Installing OpenAI tunnel-client {TUNNEL_CLIENT_VERSION}…")
        install_tunnel_client(asset, binary)
        return binary


CHATGPT_HOME_URL = "https://chatgpt.com/"
TUNNEL_CHATGPT_HINT = (
    "In ChatGPT, turn on your Tunnel app from the composer's + menu. If its tools look "
    "outdated, refresh that app under Settings → Apps."
)


def describe_tunnel_status(
    snapshot: dict[str, Any],
    *,
    project_name: str,
    settings_url: str,
) -> dict[str, Any]:
    """Return the Agent card presentation for one status snapshot."""
    state = str(snapshot.get("state") or "")
    message = str(snapshot.get("message") or "")
    if state == "ready":
        return {
            "tone": "ready",
            "label": "Connected",
            "message": (
                f"ChatGPT reaches {project_name} on this computer through the OpenAI Tunnel. "
                "No separate desktop app is needed."
            ),
            "hint": TUNNEL_CHATGPT_HINT,
            "action": {"kind": "link", "label": "Open ChatGPT", "href": CHATGPT_HOME_URL},
        }
    if state in {"starting", "installing"}:
        return {"tone": "loading", "label": "Connecting", "message": message, "hint": "", "action": None}
    if state == "not_configured":
        return {
            "tone": "error",
            "label": "Not configured",
            "message": "Save the Tunnel ID and API key in Settings to connect ChatGPT.",
            "hint": "",
            "action": {"kind": "link", "label": "Open Settings", "href": settings_url},
        }
    if state == "error":
        return {
            "tone": "error",
            "label": "Unavailable",
            "message": message,
            "hint": "",
            "action": {"kind": "restart", "label": "Reconnect"},
        }
    return {
        "tone": "error",
        "label": "Not running",
        "message": "Restart the local service to start the Tunnel.",
        "hint": "",
        "action": None,
    }


def install_tunnel_client(asset: TunnelClientAsset, destination: Path) -> None:
    """Download, verify, and atomically place one pinned tunnel-client binary."""
    url = f"{TUNNEL_CLIENT_RELEASE_BASE}/{asset.file_name}"
    proxy = detect_outbound_proxy()
    handlers = [urllib.request.ProxyHandler({"https": proxy, "http": proxy})] if proxy else []
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, headers={"User-Agent": "agenticContext-tunnel"})
    try:
        with opener.open(request, timeout=120) as response:
            archive = response.read(TUNNEL_CLIENT_MAX_DOWNLOAD_BYTES + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise TunnelRuntimeError(
            f"Could not download OpenAI tunnel-client {TUNNEL_CLIENT_VERSION}: {exc}"
        ) from exc
    if len(archive) > TUNNEL_CLIENT_MAX_DOWNLOAD_BYTES:
        raise TunnelRuntimeError("The tunnel-client download is larger than expected.")
    if hashlib.sha256(archive).hexdigest() != asset.archive_sha256:
        raise TunnelRuntimeError("The tunnel-client download failed SHA-256 verification.")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            payload = bundle.read(asset.member_name)
    except (KeyError, zipfile.BadZipFile) as exc:
        raise TunnelRuntimeError("The tunnel-client archive is malformed.") from exc
    if hashlib.sha256(payload).hexdigest() != asset.binary_sha256:
        raise TunnelRuntimeError("The tunnel-client binary failed SHA-256 verification.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.partial")
    temporary.write_bytes(payload)
    temporary.chmod(0o755)
    os.replace(temporary, destination)


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _write_owner_only(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(content)
    os.replace(temporary, path)


__all__ = [
    "CHATGPT_HOME_URL",
    "TUNNEL_CLIENT_ASSETS",
    "TUNNEL_CLIENT_VERSION",
    "TunnelRuntime",
    "TunnelRuntimeError",
    "describe_tunnel_status",
    "detect_outbound_proxy",
    "host_tunnel_client_asset",
    "install_tunnel_client",
    "valid_tunnel_id",
]
