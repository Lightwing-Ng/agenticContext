"""Supervise OpenAI's tunnel-client so ChatGPT can reach the local MCP endpoint.

Code version: v1.6.0-codex.0

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

import errno
import hashlib
import io
import json
import logging
import os
import platform
import re
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
TUNNEL_DOCTOR_TIMEOUT_SECONDS = 45.0
TUNNEL_HEALTH_PROBE_INTERVAL_SECONDS = 5.0
TUNNEL_CONNECTING_PROBE_INTERVAL_SECONDS = 1.0
TUNNEL_INITIAL_DEGRADED_SECONDS = 5.0
TUNNEL_UNHEALTHY_RESTART_SECONDS = 20.0
TUNNEL_RESTART_DEBOUNCE_SECONDS = 2.0
TUNNEL_INITIAL_BACKOFF_SECONDS = 5.0
TUNNEL_MAX_BACKOFF_SECONDS = 60.0
TUNNEL_BACKOFF_RESET_SECONDS = 30.0
TUNNEL_MAX_UNCERTAIN_CALLS = 20
TUNNEL_ID_PREFIX = "tunnel_"
LOOPBACK_NO_PROXY = "127.0.0.1,localhost,::1"
TUNNEL_ENVIRONMENT_PASSTHROUGH = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TZ",
        "USERPROFILE",
        "WINDIR",
    }
)


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


_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;\"']+"),
        r"\1[REDACTED]",
    ),
    (re.compile(r"(?i)\bbearer\s+[^\s,;\"']+"), "Bearer [REDACTED]"),
    (
        re.compile(
            r"(?i)((?:control_plane_api_key|api[_-]?key|access[_-]?token)\s*[:=]\s*)"
            r"[^\s,;\"']+"
        ),
        r"\1[REDACTED]",
    ),
    (re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_-]{3,}\b"), "sk-[REDACTED]"),
)


def _redact_tunnel_message(value: object) -> str:
    """Return a compact, user-visible diagnostic with credential material removed."""
    text = " ".join(str(value or "").split())
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _doctor_failure_detail(result: subprocess.CompletedProcess[Any]) -> str:
    """Extract a bounded actionable failure from doctor output without dumping config."""
    details: list[str] = []

    def visit(value: Any, *, key: str = "") -> None:
        normalized_key = key.lower().replace("-", "_")
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                visit(child_value, key=str(child_key))
            return
        if isinstance(value, list):
            for child in value:
                visit(child, key=key)
            return
        if normalized_key in {
            "detail",
            "error",
            "errors",
            "failure",
            "message",
            "msg",
            "reason",
        } and isinstance(value, (str, int, float)):
            text = _redact_tunnel_message(value)
            if text and text.lower() not in {"false", "failed", "error"}:
                details.append(text)

    for output in (getattr(result, "stderr", None), getattr(result, "stdout", None)):
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        if not isinstance(output, str) or not output.strip():
            continue
        try:
            visit(json.loads(output))
        except (TypeError, ValueError):
            lines = [_redact_tunnel_message(line) for line in output.splitlines() if line.strip()]
            details.extend(lines[-2:])
    unique: list[str] = []
    for detail in details:
        if detail not in unique:
            unique.append(detail)
    return "; ".join(unique)[:500]


def _doctor_failure_retryable(detail: str) -> bool:
    """Classify known control-plane/network failures without retrying bad credentials."""
    normalized = detail.lower()
    configuration_markers = (
        "http 401",
        "http 403",
        "http 404",
        "status 401",
        "status 403",
        "status 404",
        "unauthorized",
        "authentication failed",
        "forbidden",
        "invalid api key",
        "invalid credentials",
        "invalid tunnel",
        "insufficient permission",
        "insufficient_scope",
        "permission denied",
        "does not exist",
        "tunnel not found",
        "malformed",
        "missing required",
    )
    if any(marker in normalized for marker in configuration_markers):
        return False
    transient_markers = (
        "http 408",
        "http 429",
        "status 408",
        "status 429",
        "connection refused",
        "connection reset",
        "connection aborted",
        "context deadline",
        "deadline exceeded",
        "dial tcp",
        "dns",
        "eof",
        "network is unreachable",
        "no such host",
        "proxyconnect",
        "rate limit",
        "service unavailable",
        "bad gateway",
        "gateway timeout",
        "temporarily unavailable",
        "temporary failure",
        "too many requests",
        "timed out",
        "timeout",
        "tls handshake",
    )
    return bool(
        any(marker in normalized for marker in transient_markers)
        or re.search(r"(?:http|status)(?: code)?\s*[:=]?\s*5\d\d\b", normalized)
    )


def _process_start_error_retryable(error: OSError) -> bool:
    """Return whether a local process launch failure can plausibly clear on retry."""
    transient_names = ("EAGAIN", "EINTR", "EMFILE", "ENFILE", "ENOMEM", "ETXTBSY")
    transient_values = {
        value
        for name in transient_names
        if isinstance(value := getattr(errno, name, None), int)
    }
    return error.errno in transient_values


class TunnelRuntimeError(RuntimeError):
    """A classified, user-facing Tunnel failure."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        code: str = "tunnel_runtime_error",
    ) -> None:
        safe_message = _redact_tunnel_message(message)
        super().__init__(safe_message)
        self.retryable = retryable
        self.code = code


@dataclass(frozen=True, slots=True)
class _HealthProbe:
    """One local tunnel-client readiness observation."""

    ready: bool
    detail: str = ""
    code: str = ""


@dataclass(frozen=True, slots=True)
class _MonitorResult:
    """Why one tunnel-client process stopped being supervised."""

    detail: str
    code: str
    max_ready_seconds: float = 0.0
    uncertain_count: int = 0


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
            f"OpenAI tunnel-client is not available for {system_name}/{machine_name}.",
            code="unsupported_host",
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


def tunnel_client_environment(credentials: TunnelCredentials) -> dict[str, str]:
    """Build the Tunnel child's minimum host environment plus explicit credentials."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in TUNNEL_ENVIRONMENT_PASSTHROUGH
    }
    environment["CONTROL_PLANE_API_KEY"] = credentials.api_key
    environment["CONTROL_PLANE_TUNNEL_ID"] = credentials.tunnel_id
    proxy = detect_outbound_proxy()
    if proxy:
        environment.update({"HTTPS_PROXY": proxy, "HTTP_PROXY": proxy})
    environment["NO_PROXY"] = LOOPBACK_NO_PROXY
    return environment


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
        self._runtime_instance_id = secrets.token_hex(16)
        self._lock = threading.RLock()
        self._launch_lock = threading.Lock()
        self._mcp_url = ""
        self._enabled = False
        self._shutdown = False
        self._generation = 0
        self._process: subprocess.Popen[bytes] | None = None
        self._restart_timer: threading.Timer | None = None
        self._authorization = ""
        self._state = "disabled"
        self._message = "The Tunnel starts with the local service."
        self._state_revision = 0
        self._state_changed_at = time.time()
        self._ready_since: float | None = None
        self._last_ready_at: float | None = None
        self._last_probe_at: float | None = None
        self._started_at: float | None = None
        self._health_url = ""
        self._retry_attempt = 0
        self._next_retry_at: float | None = None
        self._retryable = False
        self._problem_code = ""
        self._uncertain_calls: list[dict[str, Any]] = []
        self._outcome_uncertain_since: float | None = None

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
        self.connect()

    def connect(self) -> None:
        """Enable the Tunnel and start a fresh client generation."""
        with self._lock:
            if self._shutdown:
                return
            self._enabled = True
        self.restart()

    def disconnect(self) -> None:
        """Stop forwarding while preserving the saved credentials."""
        with self._lock:
            generation = self._generation
            has_process = self._process is not None
        uncertain = (
            self._record_uncertain_calls_for_generation(
                generation,
                reason_code="manual_disconnect",
            )
            if has_process
            else 0
        )
        with self._lock:
            if self._shutdown:
                return
            self._enabled = False
            self._generation += 1
            self._cancel_restart_timer()
            self._terminate_process()
            message = "OpenAI Secure Tunnel is disconnected."
            if uncertain:
                message += self._uncertain_message(uncertain)
            self._set_state("disconnected", message, problem_code="manual_disconnect")

    def restart(self) -> None:
        """Stop any client and start a new generation when credentials exist."""
        with self._lock:
            previous_generation = self._generation
            has_process = self._process is not None
        uncertain = (
            self._record_uncertain_calls_for_generation(
                previous_generation,
                reason_code="manual_reconnect",
            )
            if has_process
            else 0
        )
        with self._lock:
            if not self._enabled or self._shutdown:
                return
            self._cancel_restart_timer()
            self._generation += 1
            generation = self._generation
            self._terminate_process()
            message = "Connecting the Tunnel…"
            if uncertain:
                message += self._uncertain_message(uncertain)
            self._set_state("starting", message)
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
            generation = self._generation
            has_process = self._process is not None
        uncertain = (
            self._record_uncertain_calls_for_generation(
                generation,
                reason_code="service_stop",
            )
            if has_process
            else 0
        )
        with self._lock:
            self._shutdown = True
            self._generation += 1
            self._cancel_restart_timer()
            self._terminate_process()
            message = "The Tunnel stopped with the local service."
            if uncertain:
                message += self._uncertain_message(uncertain)
            self._set_state("stopped", message, problem_code="service_stop")

    def authorization_matches(self, header_value: str | None) -> bool:
        """Return whether a request carries this start's bearer token."""
        expected = self._authorization
        return bool(expected) and secrets.compare_digest(
            str(header_value or "").encode(), expected.encode()
        )

    def snapshot(self) -> dict[str, Any]:
        """Return a UI-safe status record; it never includes secrets."""
        credentials = self._credentials_loader()
        try:
            activity = self._activity_provider()
        except Exception:
            LOGGER.exception("Tunnel activity snapshot failed")
            activity = {}
        with self._lock:
            state = self._state
            message = self._message
            if not credentials.configured:
                state, message = "not_configured", (
                    "Enter the Tunnel ID and API key on the Agent Tunnel page."
                )
            return {
                **activity,
                "state": state,
                "ready": state == "ready",
                "enabled": self._enabled,
                "configured": credentials.configured,
                "message": message,
                "ready_since": self._ready_since,
                "last_ready_at": self._last_ready_at,
                "last_probe_at": self._last_probe_at,
                "state_changed_at": self._state_changed_at,
                "generation": self._generation,
                "runtime_instance_id": self._runtime_instance_id,
                "state_revision": self._state_revision,
                "retryable": self._retryable,
                "retry_attempt": self._retry_attempt,
                "next_retry_at": self._next_retry_at,
                "problem_code": self._problem_code,
                "outcome_uncertain": bool(self._uncertain_calls),
                "outcome_uncertain_since": self._outcome_uncertain_since,
                "uncertain_calls": [dict(record) for record in self._uncertain_calls],
                "client_version": TUNNEL_CLIENT_VERSION,
            }

    # Supervision ------------------------------------------------------------

    def _set_state(
        self,
        state: str,
        message: str,
        *,
        retryable: bool = False,
        retry_attempt: int = 0,
        next_retry_at: float | None = None,
        problem_code: str = "",
    ) -> None:
        with self._lock:
            safe_message = _redact_tunnel_message(message)
            changed = (
                state != self._state
                or safe_message != self._message
                or retryable != self._retryable
                or retry_attempt != self._retry_attempt
                or next_retry_at != self._next_retry_at
                or problem_code != self._problem_code
            )
            if state == "ready" and self._state != "ready":
                self._ready_since = time.time()
            elif state != "ready":
                self._ready_since = None
            self._state = state
            self._message = safe_message
            self._retryable = retryable
            self._retry_attempt = retry_attempt
            self._next_retry_at = next_retry_at
            self._problem_code = problem_code
            if changed:
                self._state_revision += 1
                self._state_changed_at = time.time()

    def _set_state_for_generation(
        self,
        generation: int,
        state: str,
        message: str,
        **metadata: Any,
    ) -> bool:
        """Set state only while ``generation`` still owns supervision."""
        with self._lock:
            if (
                generation != self._generation
                or not self._enabled
                or self._shutdown
            ):
                return False
            self._set_state(state, message, **metadata)
            return True

    def _record_probe_for_generation(
        self,
        generation: int,
        *,
        ready: bool,
    ) -> bool:
        """Record probe timestamps without allowing an old generation to publish."""
        with self._lock:
            if not self._current(generation):
                return False
            now = time.time()
            self._last_probe_at = now
            if ready:
                self._last_ready_at = now
            return True

    def _current(self, generation: int) -> bool:
        with self._lock:
            return (
                generation == self._generation
                and self._enabled
                and not self._shutdown
            )

    def _supervise(self, generation: int) -> None:
        backoff = TUNNEL_INITIAL_BACKOFF_SECONDS
        retry_attempt = 0
        while self._current(generation):
            credentials = self._credentials_loader()
            if not credentials.configured:
                self._set_state_for_generation(
                    generation,
                    "not_configured",
                    "Enter the Tunnel ID and API key on the Agent Tunnel page.",
                    problem_code="credentials_missing",
                )
                return
            if not valid_tunnel_id(credentials.tunnel_id):
                self._set_state_for_generation(
                    generation,
                    "error",
                    "The Tunnel ID must look like tunnel_ followed by 32 lowercase hexadecimal characters.",
                    problem_code="tunnel_id_invalid",
                )
                return
            try:
                # A superseded doctor process cannot be cancelled portably. Serialize
                # preflight and launch so the next generation cannot share its auth,
                # health, pid, or log files with an older generation still returning.
                with self._launch_lock:
                    if not self._current(generation):
                        return
                    binary = self._resolve_binary(generation)
                    if not self._current(generation):
                        return
                    process = self._spawn(binary, credentials, generation)
                    if process is None:
                        return
                    with self._lock:
                        if not self._current(generation):
                            _terminate(process)
                            return
                        self._process = process
                        self._started_at = time.monotonic()
            except TunnelRuntimeError as exc:
                if not exc.retryable:
                    self._set_state_for_generation(
                        generation,
                        "error",
                        str(exc),
                        problem_code=exc.code,
                    )
                    return
                retry_attempt += 1
                if not self._wait_to_retry(
                    generation,
                    detail=str(exc),
                    code=exc.code,
                    delay=backoff,
                    retry_attempt=retry_attempt,
                    uncertain_count=0,
                ):
                    return
                backoff = min(backoff * 2, TUNNEL_MAX_BACKOFF_SECONDS)
                continue
            result = self._monitor(generation, process)
            if not self._current(generation):
                return
            if result.max_ready_seconds >= TUNNEL_BACKOFF_RESET_SECONDS:
                backoff = TUNNEL_INITIAL_BACKOFF_SECONDS
                retry_attempt = 0
            self._release_process_for_generation(generation, process)
            retry_attempt += 1
            if not self._wait_to_retry(
                generation,
                detail=result.detail,
                code=result.code,
                delay=backoff,
                retry_attempt=retry_attempt,
                uncertain_count=result.uncertain_count,
            ):
                return
            backoff = min(backoff * 2, TUNNEL_MAX_BACKOFF_SECONDS)

    def _monitor(
        self,
        generation: int,
        process: subprocess.Popen[bytes],
    ) -> _MonitorResult:
        """Track readiness and recycle a living client that stays unhealthy."""
        deadline = time.monotonic() + TUNNEL_READY_TIMEOUT_SECONDS
        initial_unhealthy_since = time.monotonic()
        ready_started_at: float | None = None
        unhealthy_since: float | None = None
        max_ready_seconds = 0.0
        while self._current(generation) and process.poll() is None:
            probe = self._probe_ready(generation)
            if not self._record_probe_for_generation(generation, ready=probe.ready):
                return _MonitorResult("supervision was superseded", "superseded")
            now = time.monotonic()
            if probe.ready:
                if ready_started_at is None:
                    ready_started_at = now
                unhealthy_since = None
                if not self._set_state_for_generation(
                    generation,
                    "ready",
                    "OpenAI Secure Tunnel ready; waiting for ChatGPT.",
                ):
                    return _MonitorResult("supervision was superseded", "superseded")
            else:
                if ready_started_at is not None:
                    max_ready_seconds = max(max_ready_seconds, now - ready_started_at)
                    ready_started_at = None
                    unhealthy_since = now
                if unhealthy_since is not None:
                    detail = self._last_log_problem() or probe.detail
                    if not self._set_state_for_generation(
                        generation,
                        "degraded",
                        f"Tunnel health check failed: {detail}. Reconnecting if it does not recover.",
                        retryable=True,
                        problem_code=probe.code or "health_unavailable",
                    ):
                        return _MonitorResult("supervision was superseded", "superseded")
                    if now - unhealthy_since >= TUNNEL_UNHEALTHY_RESTART_SECONDS:
                        uncertain_count = self._record_uncertain_calls_for_generation(
                            generation,
                            reason_code=probe.code or "health_unavailable",
                        )
                        self._terminate_process_for_generation(generation, process)
                        return _MonitorResult(
                            f"Tunnel remained unhealthy: {detail}",
                            probe.code or "health_unavailable",
                            max_ready_seconds,
                            uncertain_count,
                        )
                elif now >= deadline:
                    detail = self._last_log_problem() or probe.detail
                    uncertain_count = self._record_uncertain_calls_for_generation(
                        generation,
                        reason_code=probe.code or "ready_timeout",
                    )
                    self._terminate_process_for_generation(generation, process)
                    return _MonitorResult(
                        f"Tunnel did not become ready: {detail}",
                        probe.code or "ready_timeout",
                        max_ready_seconds,
                        uncertain_count,
                    )
                elif now - initial_unhealthy_since >= TUNNEL_INITIAL_DEGRADED_SECONDS:
                    detail = self._last_log_problem() or probe.detail
                    if not self._set_state_for_generation(
                        generation,
                        "degraded",
                        f"Tunnel has not become ready: {detail}. It is still connecting.",
                        retryable=True,
                        problem_code=probe.code or "health_unavailable",
                    ):
                        return _MonitorResult("supervision was superseded", "superseded")
            with self._lock:
                if not self._current(generation):
                    return _MonitorResult("supervision was superseded", "superseded")
                interval = (
                    TUNNEL_HEALTH_PROBE_INTERVAL_SECONDS
                    if self._state == "ready"
                    else TUNNEL_CONNECTING_PROBE_INTERVAL_SECONDS
                )
            if not self._sleep(generation, interval):
                return _MonitorResult("supervision was superseded", "superseded")
        if ready_started_at is not None:
            max_ready_seconds = max(
                max_ready_seconds,
                time.monotonic() - ready_started_at,
            )
        if not self._current(generation):
            return _MonitorResult("supervision was superseded", "superseded", max_ready_seconds)
        exit_code = process.poll()
        detail = self._last_log_problem() or f"tunnel-client exited with code {exit_code}"
        uncertain_count = self._record_uncertain_calls_for_generation(
            generation,
            reason_code="client_exited",
        )
        return _MonitorResult(
            detail,
            "client_exited",
            max_ready_seconds,
            uncertain_count,
        )

    def _wait_to_retry(
        self,
        generation: int,
        *,
        detail: str,
        code: str,
        delay: float,
        retry_attempt: int,
        uncertain_count: int,
    ) -> bool:
        """Publish a bounded retry and wait without blocking a replacement generation."""
        safe_detail = _redact_tunnel_message(detail).rstrip(". ")
        delay_text = f"{delay:.1f}".rstrip("0").rstrip(".")
        message = f"{safe_detail}. Retrying in {delay_text} s."
        if uncertain_count:
            message += self._uncertain_message(uncertain_count)
        if not self._set_state_for_generation(
            generation,
            "retrying",
            message,
            retryable=True,
            retry_attempt=retry_attempt,
            next_retry_at=time.time() + delay,
            problem_code=code,
        ):
            return False
        if not self._sleep(generation, delay):
            return False
        return self._set_state_for_generation(
            generation,
            "starting",
            "Reconnecting the Tunnel…",
            retry_attempt=retry_attempt,
        )

    def _record_uncertain_calls_for_generation(
        self,
        generation: int,
        *,
        reason_code: str,
    ) -> int:
        """Remember calls whose response delivery may have been cut by a restart."""
        try:
            activity = self._activity_provider()
        except Exception:
            LOGGER.exception("Tunnel activity snapshot failed during reconnect")
            return 0
        active_calls = activity.get("active_calls") if isinstance(activity, dict) else None
        if not isinstance(active_calls, list):
            return 0
        detected_at = time.time()
        candidates: list[dict[str, Any]] = []
        for record in active_calls:
            if not isinstance(record, dict):
                continue
            call_id = record.get("call_id")
            if not isinstance(call_id, (int, str)):
                continue
            candidates.append(
                {
                    "call_id": call_id,
                    "provider": str(record.get("provider") or "chatgpt")[:32],
                    "tool": str(record.get("tool") or "unknown")[:64],
                    "project": str(record.get("project") or "")[:64],
                    "target": _redact_tunnel_message(record.get("target"))[:160],
                    "started_at": record.get("started_at"),
                    "detected_at": detected_at,
                    "generation": generation,
                    "reason_code": reason_code,
                    "guidance": (
                        "Read the affected state before retrying; the local operation may "
                        "have completed even though response delivery was interrupted."
                    ),
                }
            )
        if not candidates:
            return 0
        with self._lock:
            if not self._current(generation):
                return 0
            known = {
                (record.get("call_id"), record.get("started_at"))
                for record in self._uncertain_calls
            }
            added = 0
            for record in candidates:
                identity = (record.get("call_id"), record.get("started_at"))
                if identity not in known:
                    self._uncertain_calls.append(record)
                    known.add(identity)
                    added += 1
            self._uncertain_calls = self._uncertain_calls[-TUNNEL_MAX_UNCERTAIN_CALLS:]
            if self._outcome_uncertain_since is None:
                self._outcome_uncertain_since = detected_at
        return added

    @staticmethod
    def _uncertain_message(count: int) -> str:
        noun = "call" if count == 1 else "calls"
        return (
            f" {count} in-flight tool {noun} may have completed locally, but response "
            "delivery cannot be confirmed; read the affected state before retrying."
        )

    def _terminate_process_for_generation(
        self,
        generation: int,
        process: subprocess.Popen[bytes],
    ) -> bool:
        """Terminate only the process still owned by the current generation."""
        with self._lock:
            if not self._current(generation) or self._process is not process:
                return False
            self._process = None
            self._authorization = ""
        _terminate(process)
        self._pid_path.unlink(missing_ok=True)
        return True

    def _release_process_for_generation(
        self,
        generation: int,
        process: subprocess.Popen[bytes],
    ) -> None:
        """Clear a stopped process without touching a replacement generation."""
        with self._lock:
            if not self._current(generation) or self._process is not process:
                return
            self._process = None
            self._authorization = ""
        self._pid_path.unlink(missing_ok=True)

    def _sleep(self, generation: int, seconds: float) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if not self._current(generation):
                return False
            time.sleep(min(0.25, end - time.monotonic()))
        return self._current(generation)

    def _probe_ready(self, generation: int) -> _HealthProbe:
        with self._lock:
            if not self._current(generation):
                return _HealthProbe(False, "supervision was superseded", "superseded")
            health_url = self._health_url
        if not health_url:
            try:
                value = self._health_url_path.read_text().strip()
            except FileNotFoundError:
                return _HealthProbe(
                    False,
                    "tunnel-client has not published its health endpoint",
                    "health_url_pending",
                )
            except OSError as exc:
                return _HealthProbe(
                    False,
                    f"health endpoint file could not be read: {_redact_tunnel_message(exc)}",
                    "health_url_unreadable",
                )
            if not value.startswith(("http://127.0.0.1:", "http://localhost:", "http://[::1]:")):
                return _HealthProbe(
                    False,
                    "tunnel-client published a non-loopback health endpoint",
                    "health_url_invalid",
                )
            with self._lock:
                if not self._current(generation):
                    return _HealthProbe(False, "supervision was superseded", "superseded")
                if not self._health_url:
                    self._health_url = value.rstrip("/")
                health_url = self._health_url
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(f"{health_url}/readyz", timeout=2) as response:
                status = int(getattr(response, "status", 0))
                if 200 <= status < 300:
                    return _HealthProbe(True)
                return _HealthProbe(
                    False,
                    f"health endpoint returned HTTP {status}",
                    "health_http_status",
                )
        except urllib.error.HTTPError as exc:
            return _HealthProbe(
                False,
                f"health endpoint returned HTTP {exc.code}",
                "health_http_status",
            )
        except urllib.error.URLError as exc:
            reason = _redact_tunnel_message(exc.reason or exc)
            return _HealthProbe(
                False,
                f"health endpoint is unreachable: {reason}",
                "health_unreachable",
            )
        except OSError as exc:
            return _HealthProbe(
                False,
                f"health endpoint is unreachable: {_redact_tunnel_message(exc)}",
                "health_unreachable",
            )

    def _spawn(
        self,
        binary: Path,
        credentials: TunnelCredentials,
        generation: int,
    ) -> subprocess.Popen[bytes] | None:
        with self._lock:
            if not self._current(generation):
                return None
            mcp_url = self._mcp_url
        if not mcp_url:
            raise TunnelRuntimeError(
                "The local MCP endpoint is not configured.",
                code="mcp_endpoint_missing",
            )
        try:
            self._state_root.mkdir(parents=True, exist_ok=True)
            self._stop_orphan()
            with self._lock:
                if not self._current(generation):
                    return None
                self._authorization = f"Bearer {secrets.token_urlsafe(32)}"
                _write_owner_only(self._authorization_path, self._authorization)
                self._health_url = ""
                self._health_url_path.unlink(missing_ok=True)
                if self.log_path.exists():
                    os.replace(self.log_path, self.log_path.with_suffix(".log.1"))
        except OSError as exc:
            self._discard_authorization()
            raise TunnelRuntimeError(
                f"Tunnel runtime files could not be prepared: {exc}",
                code="runtime_state_unwritable",
            ) from exc
        env = tunnel_client_environment(credentials)
        proxy = env.get("HTTPS_PROXY", "")
        doctor_command = [
            str(binary),
            "doctor",
            "--control-plane.tunnel-id",
            credentials.tunnel_id,
            "--mcp.server-url",
            f"url={mcp_url},channel=main",
            "--mcp.extra-headers",
            f"Authorization: file:{self._authorization_path}",
            "--mcp.startup-wait-timeout",
            "30s",
            "--json",
        ]
        if proxy:
            doctor_command.extend(["--control-plane.http-proxy", proxy])
        try:
            doctor = subprocess.run(
                doctor_command,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=TUNNEL_DOCTOR_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            self._discard_authorization()
            raise TunnelRuntimeError(
                f"Tunnel preflight timed out after {TUNNEL_DOCTOR_TIMEOUT_SECONDS:g} s. "
                "Check the network route and local MCP endpoint.",
                retryable=True,
                code="preflight_timeout",
            ) from exc
        except OSError as exc:
            self._discard_authorization()
            raise TunnelRuntimeError(
                f"Tunnel preflight could not start: {exc}",
                retryable=_process_start_error_retryable(exc),
                code="preflight_start_failed",
            ) from exc
        if not self._current(generation):
            return None
        if doctor.returncode != 0:
            detail = _doctor_failure_detail(doctor)
            retryable = _doctor_failure_retryable(detail)
            explanation = (
                f"Tunnel preflight failed: {detail}."
                if detail
                else "Tunnel preflight failed without a diagnostic."
            )
            self._discard_authorization()
            raise TunnelRuntimeError(
                f"{explanation} Check the Tunnel ID, API key with Permissions set to "
                "All, network route, and local MCP endpoint.",
                retryable=retryable,
                code=(
                    "preflight_transient"
                    if retryable
                    else "preflight_configuration_error"
                ),
            )
        command = [
            str(binary),
            "run",
            "--control-plane.tunnel-id",
            credentials.tunnel_id,
            "--mcp.server-url",
            f"url={mcp_url},channel=main",
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
        with self._lock:
            if not self._current(generation):
                return None
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
                self._discard_authorization()
                raise TunnelRuntimeError(
                    f"OpenAI tunnel-client could not start: {exc}",
                    retryable=_process_start_error_retryable(exc),
                    code="client_start_failed",
                ) from exc

    def _discard_authorization(self) -> None:
        """Invalidate a pending bearer token when no client can use it."""
        with self._lock:
            self._authorization = ""
        try:
            self._authorization_path.unlink(missing_ok=True)
        except OSError:
            pass

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
            return _redact_tunnel_message(text)[:300]
        return ""

    # Binary installation ---------------------------------------------------------

    def _resolve_binary(self, generation: int) -> Path:
        override = os.environ.get(TUNNEL_CLIENT_BIN_ENV, "").strip()
        if override:
            path = Path(override).expanduser()
            if not path.is_file():
                raise TunnelRuntimeError(
                    f"{TUNNEL_CLIENT_BIN_ENV} does not point to a file.",
                    code="binary_override_invalid",
                )
            return path
        asset = host_tunnel_client_asset()
        binary = self.tools_root / asset.target / asset.member_name
        if binary.is_file() and _sha256_file(binary) == asset.binary_sha256:
            return binary
        if not self._current(generation):
            raise TunnelRuntimeError(
                "The Tunnel start was superseded.",
                code="superseded",
            )
        if not self._set_state_for_generation(
            generation,
            "installing",
            f"Installing OpenAI tunnel-client {TUNNEL_CLIENT_VERSION}…",
        ):
            raise TunnelRuntimeError(
                "The Tunnel start was superseded.",
                code="superseded",
            )
        install_tunnel_client(asset, binary)
        return binary


CHATGPT_HOME_URL = "https://chatgpt.com/"
TUNNEL_CHATGPT_HINT = "Not connected until ChatGPT makes a tool call."


def describe_tunnel_status(
    snapshot: dict[str, Any],
    *,
    project_name: str,
    settings_url: str,
) -> dict[str, Any]:
    """Return the Agent card presentation for one status snapshot."""
    state = str(snapshot.get("state") or "")
    message = _redact_tunnel_message(snapshot.get("message"))
    if state == "ready":
        activity_observed = bool(snapshot.get("activity_observed"))
        uncertain_count = len(snapshot.get("uncertain_calls") or [])
        return {
            "tone": "ready",
            "label": "Active" if activity_observed else "Ready",
            "message": (
                f"Tool call received for {project_name}."
                if activity_observed
                else f"Ready for {project_name}."
            ),
            "hint": (
                TunnelRuntime._uncertain_message(uncertain_count).strip()
                if uncertain_count
                else ""
            ),
            "action": None,
        }
    if state in {"starting", "installing"}:
        detail = message or "Connecting..."
        return {
            "tone": "loading",
            "label": "Connecting",
            "message": detail,
            "hint": "",
            "action": None,
        }
    if state == "retrying":
        detail = message or "The Tunnel is reconnecting after a temporary failure."
        return {
            "tone": "loading",
            "label": "Reconnecting",
            "message": detail[:240],
            "hint": detail[:240],
            "action": None,
        }
    if state == "degraded":
        detail = message or "The Tunnel lost readiness and is trying to recover."
        return {
            "tone": "error",
            "label": "Connection lost",
            "message": detail[:240],
            "hint": detail[:240],
            "action": None,
        }
    if state == "not_configured":
        return {
            "tone": "error",
            "label": "Not configured",
            "message": "Enter credentials in step ➋.",
            "hint": "",
            "action": None,
        }
    if state == "disconnected":
        return {
            "tone": "error",
            "label": "Disconnected",
            "message": "Tunnel disconnected.",
            "hint": "",
            "action": None,
        }
    if state == "error":
        detail = message or "Tunnel unavailable."
        return {
            "tone": "error",
            "label": "Unavailable",
            "message": detail[:240],
            "hint": detail[:240],
            "action": None,
        }
    return {
        "tone": "error",
        "label": "Not running",
        "message": "Reconnect in step ➋.",
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
    except urllib.error.HTTPError as exc:
        retryable = exc.code in {408, 425, 429} or 500 <= exc.code < 600
        raise TunnelRuntimeError(
            f"Could not download OpenAI tunnel-client {TUNNEL_CLIENT_VERSION}: "
            f"release server returned HTTP {exc.code}.",
            retryable=retryable,
            code="client_download_http_error",
        ) from exc
    except (OSError, urllib.error.URLError) as exc:
        raise TunnelRuntimeError(
            f"Could not download OpenAI tunnel-client {TUNNEL_CLIENT_VERSION}: {exc}",
            retryable=True,
            code="client_download_unreachable",
        ) from exc
    if len(archive) > TUNNEL_CLIENT_MAX_DOWNLOAD_BYTES:
        raise TunnelRuntimeError(
            "The tunnel-client download is larger than expected.",
            code="client_download_oversized",
        )
    if hashlib.sha256(archive).hexdigest() != asset.archive_sha256:
        raise TunnelRuntimeError(
            "The tunnel-client download failed SHA-256 verification.",
            code="client_archive_hash_mismatch",
        )
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            payload = bundle.read(asset.member_name)
    except (KeyError, zipfile.BadZipFile) as exc:
        raise TunnelRuntimeError(
            "The tunnel-client archive is malformed.",
            code="client_archive_malformed",
        ) from exc
    if hashlib.sha256(payload).hexdigest() != asset.binary_sha256:
        raise TunnelRuntimeError(
            "The tunnel-client binary failed SHA-256 verification.",
            code="client_binary_hash_mismatch",
        )
    temporary = destination.with_name(f".{destination.name}.partial")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(payload)
        temporary.chmod(0o755)
        os.replace(temporary, destination)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise TunnelRuntimeError(
            f"OpenAI tunnel-client could not be installed: {exc}",
            code="client_install_failed",
        ) from exc


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
