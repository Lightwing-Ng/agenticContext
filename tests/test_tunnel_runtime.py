"""tunnel-client supervision tests with a local fake client.

Code version: v1.4.2-codex.0
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

from app.core import tunnel_runtime
from app.core.tunnel_credentials import TunnelCredentials
from app.core.tunnel_runtime import (
    TunnelClientAsset,
    TunnelRuntime,
    TunnelRuntimeError,
    describe_tunnel_status,
    detect_outbound_proxy,
    host_tunnel_client_asset,
    install_tunnel_client,
    valid_tunnel_id,
)

VALID_TUNNEL_ID = "tunnel_" + "0123456789abcdef" * 2

FAKE_CLIENT = """#!{python}
import http.server, json, os, sys
args = sys.argv[1:]
def value(name):
    return args[args.index(name) + 1]
if args and args[0] == "doctor":
    header = value("--mcp.extra-headers")
    authorization_file = header.removeprefix("Authorization: file:")
    record = {{
        "args": args,
        "api_key": os.environ.get("CONTROL_PLANE_API_KEY"),
        "openai_api_key": os.environ.get("OPENAI_API_KEY"),
        "unrelated_secret": os.environ.get("UNRELATED_SECRET"),
    }}
    with open(os.path.join(os.path.dirname(authorization_file), "fake-doctor.json"), "w") as handle:
        json.dump(record, handle)
    print(json.dumps({{"ok": True}}))
    raise SystemExit(0)
url_file = value("--health.url-file")
record = {{
    "args": args,
    "api_key": os.environ.get("CONTROL_PLANE_API_KEY"),
    "tunnel_id": os.environ.get("CONTROL_PLANE_TUNNEL_ID"),
    "openai_api_key": os.environ.get("OPENAI_API_KEY"),
    "unrelated_secret": os.environ.get("UNRELATED_SECRET"),
    "no_proxy": os.environ.get("NO_PROXY"),
}}
with open(os.path.join(os.path.dirname(url_file), "fake-invocation.json"), "w") as handle:
    json.dump(record, handle)
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/readyz" else 404)
        self.end_headers()
    def log_message(self, *args):
        pass
server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
with open(url_file, "w") as handle:
    handle.write(f"http://127.0.0.1:{{server.server_port}}")
server.serve_forever()
"""


def wait_for(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


@pytest.fixture
def fake_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    if os.name == "nt":
        pytest.skip("The fake client relies on a POSIX shebang.")
    binary = tmp_path / "fake-tunnel-client"
    binary.write_text(FAKE_CLIENT.format(python=sys.executable))
    binary.chmod(0o755)
    monkeypatch.setenv(tunnel_runtime.TUNNEL_CLIENT_BIN_ENV, str(binary))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-leak-either")
    return binary


def test_valid_tunnel_ids_and_host_assets() -> None:
    assert valid_tunnel_id(VALID_TUNNEL_ID)
    assert not valid_tunnel_id("tunnel_abc")
    assert not valid_tunnel_id(VALID_TUNNEL_ID.upper())
    assert host_tunnel_client_asset("darwin", "arm64").target == "darwin-arm64"
    assert host_tunnel_client_asset("linux", "x86_64").target == "linux-amd64"
    assert host_tunnel_client_asset("win32", "AMD64").member_name == "tunnel-client.exe"
    with pytest.raises(TunnelRuntimeError):
        host_tunnel_client_asset("plan9", "mips")


def test_snapshot_identifies_one_service_runtime_instance(tmp_path: Path) -> None:
    first = TunnelRuntime(
        credentials_loader=lambda: TunnelCredentials(),
        state_root=tmp_path / "first",
    )
    second = TunnelRuntime(
        credentials_loader=lambda: TunnelCredentials(),
        state_root=tmp_path / "second",
    )

    first_id = first.snapshot()["runtime_instance_id"]

    assert len(first_id) == 32
    assert all(character in "0123456789abcdef" for character in first_id)
    assert first.snapshot()["runtime_instance_id"] == first_id
    assert second.snapshot()["runtime_instance_id"] != first_id


def test_failed_reconnect_start_revokes_old_generation_and_retains_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A synchronous startup failure cannot leave an old client marked ready."""
    runtime = TunnelRuntime(
        credentials_loader=lambda: TunnelCredentials(VALID_TUNNEL_ID, "sk-proj-test"),
        state_root=tmp_path / "state",
    )
    runtime._enabled = True
    runtime._generation = 3
    runtime._authorization = "Bearer old-test-token"
    runtime._set_state("ready", "Ready for a project tool call.")

    def fail_connect() -> None:
        raise RuntimeError("sk-private-error-must-not-leak")

    monkeypatch.setattr(runtime, "connect", fail_connect)
    with pytest.raises(RuntimeError):
        runtime.connect_or_fail_closed(
            expected_revision=1,
            current_revision=lambda: 1,
        )

    snapshot = runtime.snapshot()
    assert snapshot["generation"] == 4
    assert snapshot["enabled"] is False
    assert snapshot["ready"] is False
    assert snapshot["state"] == "error"
    assert snapshot["problem_code"] == "reconnect_start_failed"
    assert "selection was saved" in snapshot["message"]
    assert runtime.authorization_matches("Bearer old-test-token") is False
    assert runtime._set_state_for_generation(3, "ready", "Stale ready") is False
    assert runtime.snapshot()["state"] == "error"


def test_failed_reconnect_cannot_revoke_a_newer_successful_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure transition completes before a later connect can start."""
    runtime = TunnelRuntime(
        credentials_loader=lambda: TunnelCredentials(VALID_TUNNEL_ID, "sk-proj-test"),
        state_root=tmp_path / "state",
    )
    first_started = threading.Event()
    release_first = threading.Event()
    second_called = threading.Event()
    calls = []
    errors = []

    def controlled_connect() -> None:
        calls.append(True)
        if len(calls) == 1:
            first_started.set()
            assert release_first.wait(5)
            raise RuntimeError("first start failed")
        second_called.set()
        runtime._enabled = True
        runtime._generation += 1
        runtime._set_state("ready", "Newer client is ready.")

    def first_attempt() -> None:
        try:
            runtime.connect_or_fail_closed(
                expected_revision=1,
                current_revision=lambda: 1,
            )
        except RuntimeError as exc:
            errors.append(str(exc))

    monkeypatch.setattr(runtime, "connect", controlled_connect)
    first = threading.Thread(target=first_attempt)
    second = threading.Thread(
        target=runtime.connect_or_fail_closed,
        kwargs={"expected_revision": 2, "current_revision": lambda: 2},
    )
    first.start()
    assert first_started.wait(5)
    second.start()
    try:
        assert not second_called.wait(0.1)
    finally:
        release_first.set()
        first.join(timeout=5)
        second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == ["first start failed"]
    assert len(calls) == 2
    assert runtime.snapshot()["state"] == "ready"
    assert runtime.snapshot()["generation"] == 2


def test_superseded_project_revision_skips_reconnect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later saved selection prevents an older request from touching the client."""
    runtime = TunnelRuntime(
        credentials_loader=lambda: TunnelCredentials(VALID_TUNNEL_ID, "sk-proj-test"),
        state_root=tmp_path / "state",
    )

    def unexpected_connect() -> None:
        raise AssertionError("A superseded selection must not reconnect.")

    monkeypatch.setattr(runtime, "connect", unexpected_connect)
    assert runtime.connect_or_fail_closed(
        expected_revision=2,
        current_revision=lambda: 3,
    ) is False
    assert runtime.snapshot()["generation"] == 0
    assert runtime.snapshot()["state"] == "disabled"


def test_outbound_proxy_prefers_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1082")
    assert detect_outbound_proxy() == "http://127.0.0.1:1082"


def test_runtime_supervises_the_client_until_stopped(tmp_path: Path, fake_client: Path) -> None:
    state_root = tmp_path / "state"
    runtime = TunnelRuntime(
        credentials_loader=lambda: TunnelCredentials(VALID_TUNNEL_ID, "sk-proj-test"),
        state_root=state_root,
    )
    assert runtime.snapshot()["state"] == "disabled"
    runtime.enable("http://127.0.0.1:8666/mcp")
    try:
        assert wait_for(lambda: runtime.snapshot()["ready"]), runtime.snapshot()
        doctor = json.loads((state_root / "fake-doctor.json").read_text())
        assert doctor["api_key"] == "sk-proj-test"
        assert doctor["openai_api_key"] is None
        assert doctor["unrelated_secret"] is None
        assert doctor["args"][0] == "doctor"
        assert "--json" in doctor["args"]
        assert "sk-proj-test" not in " ".join(doctor["args"])

        invocation = json.loads((state_root / "fake-invocation.json").read_text())
        assert invocation["api_key"] == "sk-proj-test"
        assert invocation["tunnel_id"] == VALID_TUNNEL_ID
        assert invocation["openai_api_key"] is None
        assert invocation["unrelated_secret"] is None
        assert "127.0.0.1" in invocation["no_proxy"]
        assert "sk-proj-test" not in " ".join(invocation["args"])
        args = invocation["args"]
        assert args[0] == "run"
        assert args[args.index("--mcp.server-url") + 1] == "url=http://127.0.0.1:8666/mcp,channel=main"
        header = args[args.index("--mcp.extra-headers") + 1]
        authorization_file = Path(header.removeprefix("Authorization: file:"))
        assert runtime.authorization_matches(authorization_file.read_text())
        assert not runtime.authorization_matches("Bearer guess")
        if os.name == "posix":
            assert authorization_file.stat().st_mode & 0o777 == 0o600

        runtime.disconnect()
        assert runtime.snapshot()["state"] == "disconnected"
        assert runtime.snapshot()["enabled"] is False
        runtime.connect()
        assert wait_for(lambda: runtime.snapshot()["ready"]), runtime.snapshot()
        assert runtime.snapshot()["enabled"] is True
    finally:
        runtime.stop()
    assert runtime.snapshot()["state"] == "stopped"
    assert not runtime.authorization_matches(authorization_file.read_text())


def test_runtime_rejects_malformed_tunnel_ids(tmp_path: Path, fake_client: Path) -> None:
    runtime = TunnelRuntime(
        credentials_loader=lambda: TunnelCredentials("tunnel_bad", "sk-proj-test"),
        state_root=tmp_path / "state",
    )
    runtime.enable("http://127.0.0.1:8666/mcp")
    try:
        assert wait_for(lambda: runtime.snapshot()["state"] == "error")
        assert "32 lowercase hexadecimal" in runtime.snapshot()["message"]
    finally:
        runtime.stop()


class _FakeRunningProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self) -> None:
        self.terminated = True
        self.returncode = -9


def _fast_supervisor(monkeypatch: pytest.MonkeyPatch, *, backoff: float = 0.2) -> None:
    monkeypatch.setattr(tunnel_runtime, "TUNNEL_INITIAL_BACKOFF_SECONDS", backoff)
    monkeypatch.setattr(tunnel_runtime, "TUNNEL_MAX_BACKOFF_SECONDS", backoff)
    monkeypatch.setattr(tunnel_runtime, "TUNNEL_CONNECTING_PROBE_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(tunnel_runtime, "TUNNEL_HEALTH_PROBE_INTERVAL_SECONDS", 0.01)


def test_transient_preflight_failure_retries_with_redacted_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fast_supervisor(monkeypatch, backoff=0.3)
    binary = tmp_path / "fake-tunnel-client"
    binary.write_text("fake")
    binary.chmod(0o755)
    monkeypatch.setenv(tunnel_runtime.TUNNEL_CLIENT_BIN_ENV, str(binary))
    monkeypatch.setattr(tunnel_runtime, "detect_outbound_proxy", lambda: "")
    doctor_calls = 0
    process = _FakeRunningProcess()

    def fake_run(command, **_kwargs):
        nonlocal doctor_calls
        doctor_calls += 1
        if doctor_calls == 1:
            return tunnel_runtime.subprocess.CompletedProcess(
                command,
                1,
                stdout=json.dumps(
                    {
                        "error": (
                            "HTTP 503 temporarily unavailable; "
                            "api_key=sk-proj-super-secret; "
                            "Authorization: Bearer delivery-secret"
                        )
                    }
                ),
                stderr="",
            )
        return tunnel_runtime.subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"ok": True}),
            stderr="",
        )

    monkeypatch.setattr(tunnel_runtime.subprocess, "run", fake_run)
    monkeypatch.setattr(tunnel_runtime.subprocess, "Popen", lambda *_args, **_kwargs: process)
    runtime = TunnelRuntime(
        credentials_loader=lambda: TunnelCredentials(VALID_TUNNEL_ID, "sk-proj-test"),
        state_root=tmp_path / "state",
    )
    monkeypatch.setattr(runtime, "_stop_orphan", lambda: None)
    monkeypatch.setattr(
        runtime,
        "_probe_ready",
        lambda _generation: tunnel_runtime._HealthProbe(True),
    )

    runtime.enable("http://127.0.0.1:8666/mcp")
    try:
        assert wait_for(lambda: runtime.snapshot()["state"] == "retrying")
        retrying = runtime.snapshot()
        assert retrying["problem_code"] == "preflight_transient"
        assert retrying["retryable"] is True
        assert retrying["retry_attempt"] == 1
        assert retrying["next_retry_at"] > time.time()
        assert "HTTP 503 temporarily unavailable" in retrying["message"]
        assert "super-secret" not in retrying["message"]
        assert "delivery-secret" not in retrying["message"]
        assert "[REDACTED]" in retrying["message"]
        assert wait_for(lambda: runtime.snapshot()["ready"])
        assert doctor_calls == 2
    finally:
        runtime.stop()


def test_configuration_preflight_failure_does_not_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fast_supervisor(monkeypatch)
    binary = tmp_path / "fake-tunnel-client"
    binary.write_text("fake")
    binary.chmod(0o755)
    monkeypatch.setenv(tunnel_runtime.TUNNEL_CLIENT_BIN_ENV, str(binary))
    monkeypatch.setattr(tunnel_runtime, "detect_outbound_proxy", lambda: "")
    doctor_calls = 0

    def fake_run(command, **_kwargs):
        nonlocal doctor_calls
        doctor_calls += 1
        return tunnel_runtime.subprocess.CompletedProcess(
            command,
            1,
            stdout=json.dumps({"message": "HTTP 401 unauthorized: sk-proj-do-not-show"}),
            stderr="",
        )

    monkeypatch.setattr(tunnel_runtime.subprocess, "run", fake_run)
    runtime = TunnelRuntime(
        credentials_loader=lambda: TunnelCredentials(VALID_TUNNEL_ID, "sk-proj-test"),
        state_root=tmp_path / "state",
    )
    monkeypatch.setattr(runtime, "_stop_orphan", lambda: None)

    runtime.enable("http://127.0.0.1:8666/mcp")
    try:
        assert wait_for(lambda: runtime.snapshot()["state"] == "error")
        failed = runtime.snapshot()
        assert failed["problem_code"] == "preflight_configuration_error"
        assert failed["retryable"] is False
        assert "HTTP 401 unauthorized" in failed["message"]
        assert "do-not-show" not in failed["message"]
        assert doctor_calls == 1
    finally:
        runtime.stop()


def test_transient_binary_download_recovers_without_manual_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fast_supervisor(monkeypatch, backoff=0.3)
    monkeypatch.delenv(tunnel_runtime.TUNNEL_CLIENT_BIN_ENV, raising=False)
    payload = b"#!/bin/sh\nexit 0\n"
    archive = _zip_with("tunnel-client", payload)
    asset = TunnelClientAsset(
        "test-host",
        hashlib.sha256(archive).hexdigest(),
        hashlib.sha256(payload).hexdigest(),
        "tunnel-client",
    )

    class FlakyOpener:
        def __init__(self) -> None:
            self.calls = 0

        def open(self, _request, timeout=None):
            del timeout
            self.calls += 1
            if self.calls == 1:
                raise tunnel_runtime.urllib.error.URLError("temporary DNS failure")
            return io.BytesIO(archive)

    opener = FlakyOpener()
    monkeypatch.setattr(tunnel_runtime, "host_tunnel_client_asset", lambda: asset)
    monkeypatch.setattr(tunnel_runtime, "detect_outbound_proxy", lambda: "")
    monkeypatch.setattr(
        tunnel_runtime.urllib.request,
        "build_opener",
        lambda *_handlers: opener,
    )
    process = _FakeRunningProcess()
    runtime = TunnelRuntime(
        credentials_loader=lambda: TunnelCredentials(VALID_TUNNEL_ID, "sk-proj-test"),
        state_root=tmp_path / "state",
    )
    monkeypatch.setattr(runtime, "_spawn", lambda *_args: process)
    monkeypatch.setattr(
        runtime,
        "_probe_ready",
        lambda _generation: tunnel_runtime._HealthProbe(True),
    )

    runtime.enable("http://127.0.0.1:8666/mcp")
    try:
        assert wait_for(lambda: runtime.snapshot()["state"] == "retrying")
        retrying = runtime.snapshot()
        assert retrying["problem_code"] == "client_download_unreachable"
        assert "temporary DNS failure" in retrying["message"]
        assert wait_for(lambda: runtime.snapshot()["ready"])
        assert opener.calls == 2
        installed = runtime.tools_root / asset.target / asset.member_name
        assert installed.read_bytes() == payload
    finally:
        runtime.stop()


def test_living_client_that_never_becomes_ready_is_recycled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fast_supervisor(monkeypatch, backoff=0.2)
    monkeypatch.setattr(tunnel_runtime, "TUNNEL_READY_TIMEOUT_SECONDS", 0.08)
    processes: list[_FakeRunningProcess] = []
    runtime = TunnelRuntime(
        credentials_loader=lambda: TunnelCredentials(VALID_TUNNEL_ID, "sk-proj-test"),
        state_root=tmp_path / "state",
    )
    monkeypatch.setattr(runtime, "_resolve_binary", lambda _generation: tmp_path / "client")

    def fake_spawn(*_args):
        process = _FakeRunningProcess()
        processes.append(process)
        return process

    def fake_probe(_generation):
        return tunnel_runtime._HealthProbe(
            len(processes) > 1,
            "simulated health endpoint timeout",
            "health_unreachable",
        )

    monkeypatch.setattr(runtime, "_spawn", fake_spawn)
    monkeypatch.setattr(runtime, "_probe_ready", fake_probe)
    runtime.enable("http://127.0.0.1:8666/mcp")
    try:
        assert wait_for(lambda: runtime.snapshot()["state"] == "retrying")
        retrying = runtime.snapshot()
        assert processes[0].terminated is True
        assert "did not become ready" in retrying["message"]
        assert retrying["problem_code"] == "health_unreachable"
        assert wait_for(lambda: len(processes) >= 2 and runtime.snapshot()["ready"])
    finally:
        runtime.stop()


def test_never_ready_client_degrades_early_but_recycles_at_ready_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fast_supervisor(monkeypatch, backoff=0.5)
    monkeypatch.setattr(tunnel_runtime, "TUNNEL_INITIAL_DEGRADED_SECONDS", 0.04)
    monkeypatch.setattr(tunnel_runtime, "TUNNEL_READY_TIMEOUT_SECONDS", 0.35)
    process = _FakeRunningProcess()
    runtime = TunnelRuntime(
        credentials_loader=lambda: TunnelCredentials(VALID_TUNNEL_ID, "sk-proj-test"),
        state_root=tmp_path / "state",
    )
    monkeypatch.setattr(runtime, "_resolve_binary", lambda _generation: tmp_path / "client")
    monkeypatch.setattr(runtime, "_spawn", lambda *_args: process)
    monkeypatch.setattr(
        runtime,
        "_probe_ready",
        lambda _generation: tunnel_runtime._HealthProbe(
            False,
            "simulated HTTP 503; api_key=sk-proj-never-show",
            "health_http_status",
        ),
    )

    runtime.enable("http://127.0.0.1:8666/mcp")
    try:
        assert wait_for(lambda: runtime.snapshot()["state"] == "degraded")
        degraded = runtime.snapshot()
        assert process.terminated is False
        assert degraded["ready"] is False
        assert degraded["retryable"] is True
        assert degraded["problem_code"] == "health_http_status"
        assert "simulated HTTP 503" in degraded["message"]
        assert "never-show" not in degraded["message"]
        assert "[REDACTED]" in degraded["message"]

        assert wait_for(lambda: runtime.snapshot()["state"] == "retrying")
        retrying = runtime.snapshot()
        assert process.terminated is True
        assert "did not become ready" in retrying["message"]
    finally:
        runtime.stop()


def test_ready_loss_revokes_ready_then_recovers_and_marks_inflight_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fast_supervisor(monkeypatch, backoff=0.1)
    monkeypatch.setattr(tunnel_runtime, "TUNNEL_UNHEALTHY_RESTART_SECONDS", 0.25)
    active_call = {
        "call_id": 17,
        "provider": "chatgpt",
        "tool": "apply_edits",
        "project": "demo",
        "target": "demo: app.py",
        "started_at": time.time(),
    }
    processes: list[_FakeRunningProcess] = []
    first_probe_count = 0
    runtime = TunnelRuntime(
        credentials_loader=lambda: TunnelCredentials(VALID_TUNNEL_ID, "sk-proj-test"),
        state_root=tmp_path / "state",
        activity_provider=lambda: {"active_calls": [active_call]},
    )
    monkeypatch.setattr(runtime, "_resolve_binary", lambda _generation: tmp_path / "client")

    def fake_spawn(*_args):
        process = _FakeRunningProcess()
        processes.append(process)
        return process

    def fake_probe(_generation):
        nonlocal first_probe_count
        if len(processes) == 1:
            first_probe_count += 1
            if first_probe_count == 1:
                return tunnel_runtime._HealthProbe(True)
            return tunnel_runtime._HealthProbe(
                False,
                "simulated HTTP 503",
                "health_http_status",
            )
        return tunnel_runtime._HealthProbe(True)

    monkeypatch.setattr(runtime, "_spawn", fake_spawn)
    monkeypatch.setattr(runtime, "_probe_ready", fake_probe)
    runtime.enable("http://127.0.0.1:8666/mcp")
    try:
        assert wait_for(lambda: runtime.snapshot()["state"] == "degraded")
        degraded = runtime.snapshot()
        generation = degraded["generation"]
        assert degraded["ready"] is False
        assert degraded["retryable"] is True
        assert "simulated HTTP 503" in degraded["message"]
        assert wait_for(lambda: len(processes) >= 2 and runtime.snapshot()["ready"])
        recovered = runtime.snapshot()
        assert recovered["generation"] == generation
        assert recovered["last_ready_at"] is not None
        assert recovered["last_probe_at"] is not None
        assert processes[0].terminated is True
        assert recovered["outcome_uncertain"] is True
        assert recovered["uncertain_calls"][0]["call_id"] == 17
        assert "Read the affected state" in recovered["uncertain_calls"][0]["guidance"]
        presentation = describe_tunnel_status(
            recovered,
            project_name="demo",
            settings_url="/settings",
        )
        assert "response delivery cannot be confirmed" in presentation["hint"]
    finally:
        runtime.stop()


def test_late_probe_from_old_generation_cannot_overwrite_ready_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fast_supervisor(monkeypatch)
    first_probe_started = threading.Event()
    release_first_probe = threading.Event()
    first_probe_returned = threading.Event()
    probe_calls = 0
    processes: list[_FakeRunningProcess] = []
    runtime = TunnelRuntime(
        credentials_loader=lambda: TunnelCredentials(VALID_TUNNEL_ID, "sk-proj-test"),
        state_root=tmp_path / "state",
    )
    monkeypatch.setattr(runtime, "_resolve_binary", lambda _generation: tmp_path / "client")

    def fake_spawn(*_args):
        process = _FakeRunningProcess()
        processes.append(process)
        return process

    def fake_probe(_generation):
        nonlocal probe_calls
        probe_calls += 1
        if probe_calls == 1:
            first_probe_started.set()
            assert release_first_probe.wait(5)
            first_probe_returned.set()
            return tunnel_runtime._HealthProbe(
                False,
                "stale HTTP 503",
                "health_http_status",
            )
        return tunnel_runtime._HealthProbe(True)

    monkeypatch.setattr(runtime, "_spawn", fake_spawn)
    monkeypatch.setattr(runtime, "_probe_ready", fake_probe)
    runtime.enable("http://127.0.0.1:8666/mcp")
    try:
        assert first_probe_started.wait(3)
        first_generation = runtime.snapshot()["generation"]
        runtime.restart()
        assert wait_for(
            lambda: runtime.snapshot()["generation"] > first_generation
            and runtime.snapshot()["ready"]
        )
        current_generation = runtime.snapshot()["generation"]
        release_first_probe.set()
        assert first_probe_returned.wait(3)
        assert wait_for(lambda: runtime.snapshot()["state"] == "ready")
        current = runtime.snapshot()
        assert current["generation"] == current_generation
        assert current["problem_code"] == ""
        assert "stale HTTP 503" not in current["message"]
        assert len(processes) == 2
    finally:
        release_first_probe.set()
        runtime.stop()


def test_stable_ready_period_resets_retry_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = TunnelRuntime(
        credentials_loader=lambda: TunnelCredentials(VALID_TUNNEL_ID, "sk-proj-test"),
        state_root=tmp_path / "state",
    )
    processes: list[_FakeRunningProcess] = []
    monitor_results = iter(
        [
            tunnel_runtime._MonitorResult("first failure", "client_exited", 0.0),
            tunnel_runtime._MonitorResult(
                "failure after stable recovery",
                "client_exited",
                tunnel_runtime.TUNNEL_BACKOFF_RESET_SECONDS + 1,
            ),
        ]
    )
    retry_delays: list[float] = []
    monkeypatch.setattr(runtime, "_resolve_binary", lambda _generation: tmp_path / "client")

    def fake_spawn(*_args):
        process = _FakeRunningProcess()
        processes.append(process)
        return process

    def fake_monitor(_generation, process):
        process.returncode = 1
        return next(monitor_results)

    def fake_wait_to_retry(_generation, **kwargs):
        retry_delays.append(kwargs["delay"])
        if len(retry_delays) == 2:
            runtime.disconnect()
            return False
        return True

    monkeypatch.setattr(runtime, "_spawn", fake_spawn)
    monkeypatch.setattr(runtime, "_monitor", fake_monitor)
    monkeypatch.setattr(runtime, "_wait_to_retry", fake_wait_to_retry)
    with runtime._lock:
        runtime._enabled = True
        runtime._mcp_url = "http://127.0.0.1:8666/mcp"
        runtime._generation = 1
    runtime._supervise(1)
    assert retry_delays == [
        tunnel_runtime.TUNNEL_INITIAL_BACKOFF_SECONDS,
        tunnel_runtime.TUNNEL_INITIAL_BACKOFF_SECONDS,
    ]


def test_log_problem_and_state_messages_redact_credentials(tmp_path: Path) -> None:
    runtime = TunnelRuntime(
        credentials_loader=lambda: TunnelCredentials(VALID_TUNNEL_ID, "sk-proj-test"),
        state_root=tmp_path / "state",
    )
    runtime.log_path.parent.mkdir(parents=True)
    runtime.log_path.write_text(
        json.dumps(
            {
                "level": "error",
                "message": "upstream refused sk-proj-log-secret",
                "error": "Authorization: Bearer bearer-log-secret",
            }
        )
        + "\n"
    )
    detail = runtime._last_log_problem()
    assert "log-secret" not in detail
    assert "bearer-log-secret" not in detail
    assert detail.count("[REDACTED]") == 2
    runtime._set_state("error", f"failed: {detail}", problem_code="test")
    snapshot = runtime.snapshot()
    assert "secret" not in snapshot["message"]


def test_disconnect_during_blocked_doctor_does_not_launch_stale_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "fake-tunnel-client"
    binary.write_text("fake")
    binary.chmod(0o755)
    monkeypatch.setenv(tunnel_runtime.TUNNEL_CLIENT_BIN_ENV, str(binary))
    doctor_started = threading.Event()
    release_doctor = threading.Event()
    popen_calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        if len(command) > 1 and command[1] == "doctor":
            doctor_started.set()
            assert release_doctor.wait(5)
        return tunnel_runtime.subprocess.CompletedProcess(command, 0, stdout="")

    def fake_popen(command, **_kwargs):
        popen_calls.append(command)
        return _FakeRunningProcess()

    monkeypatch.setattr(tunnel_runtime.subprocess, "run", fake_run)
    monkeypatch.setattr(tunnel_runtime.subprocess, "Popen", fake_popen)
    runtime = TunnelRuntime(
        credentials_loader=lambda: TunnelCredentials(VALID_TUNNEL_ID, "sk-proj-test"),
        state_root=tmp_path / "state",
    )

    runtime.enable("http://127.0.0.1:8666/mcp")
    try:
        assert doctor_started.wait(3)
        runtime.disconnect()
        release_doctor.set()
        assert runtime._launch_lock.acquire(timeout=3)
        runtime._launch_lock.release()

        assert popen_calls == []
        assert runtime.snapshot()["state"] == "disconnected"
        assert runtime.snapshot()["enabled"] is False
    finally:
        release_doctor.set()
        runtime.stop()


@pytest.mark.parametrize("stale_doctor_returncode", [0, 1])
def test_restart_during_blocked_doctor_launches_only_current_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stale_doctor_returncode: int,
) -> None:
    binary = tmp_path / "fake-tunnel-client"
    binary.write_text("fake")
    binary.chmod(0o755)
    monkeypatch.setenv(tunnel_runtime.TUNNEL_CLIENT_BIN_ENV, str(binary))
    first_doctor_started = threading.Event()
    release_first_doctor = threading.Event()
    calls_lock = threading.Lock()
    doctor_calls = 0
    popen_calls: list[list[str]] = []
    processes: list[_FakeRunningProcess] = []
    current_health_url = "http://127.0.0.1:9"
    stale_health_url = "http://127.0.0.1:10"

    def fake_run(command, **_kwargs):
        nonlocal doctor_calls
        if len(command) > 1 and command[1] == "doctor":
            with calls_lock:
                doctor_calls += 1
                call_number = doctor_calls
            if call_number == 1:
                first_doctor_started.set()
                assert release_first_doctor.wait(5)
                return tunnel_runtime.subprocess.CompletedProcess(
                    command,
                    stale_doctor_returncode,
                    stdout="",
                )
        return tunnel_runtime.subprocess.CompletedProcess(command, 0, stdout="")

    def fake_popen(command, **_kwargs):
        popen_calls.append(command)
        health_path = Path(command[command.index("--health.url-file") + 1])
        health_path.write_text(
            current_health_url if len(popen_calls) == 1 else stale_health_url
        )
        process = _FakeRunningProcess()
        processes.append(process)
        return process

    monkeypatch.setattr(tunnel_runtime.subprocess, "run", fake_run)
    monkeypatch.setattr(tunnel_runtime.subprocess, "Popen", fake_popen)
    runtime = TunnelRuntime(
        credentials_loader=lambda: TunnelCredentials(VALID_TUNNEL_ID, "sk-proj-test"),
        state_root=tmp_path / "state",
    )

    runtime.enable("http://127.0.0.1:8666/mcp")
    try:
        assert first_doctor_started.wait(3)
        runtime.restart()
        release_first_doctor.set()
        assert wait_for(lambda: len(popen_calls) == 1 and doctor_calls == 2)
        assert runtime._launch_lock.acquire(timeout=3)
        runtime._launch_lock.release()

        assert len(popen_calls) == 1
        assert len(processes) == 1
        assert runtime._process is processes[0]
        assert runtime._health_url_path.read_text() == current_health_url
        assert runtime._health_url in {"", current_health_url}
        assert runtime.snapshot()["state"] != "error"
    finally:
        release_first_doctor.set()
        runtime.stop()


def test_runtime_without_credentials_reports_not_configured(tmp_path: Path) -> None:
    runtime = TunnelRuntime(credentials_loader=TunnelCredentials, state_root=tmp_path)
    snapshot = runtime.snapshot()
    assert snapshot["state"] == "not_configured"
    view = describe_tunnel_status(snapshot, project_name="demo", settings_url="/settings#settings-llm")
    assert view["label"] == "Not configured"
    assert "step ➋" in view["message"]
    assert view["action"] is None


def test_ready_presentation_distinguishes_local_readiness_from_tool_activity() -> None:
    view = describe_tunnel_status({"state": "ready"}, project_name="demo", settings_url="/s")
    assert view["tone"] == "ready"
    assert view["label"] == "Ready"
    assert view["message"] == "Ready for demo."
    assert view["hint"] == ""
    assert view["action"] is None

    active = describe_tunnel_status(
        {"state": "ready", "activity_observed": True},
        project_name="demo",
        settings_url="/s",
    )
    assert active["label"] == "Active"
    assert "Tool call received for demo" in active["message"]

    disconnected = describe_tunnel_status(
        {"state": "disconnected"},
        project_name="demo",
        settings_url="/s",
    )
    assert disconnected["label"] == "Disconnected"
    error = describe_tunnel_status(
        {"state": "error", "message": "boom"},
        project_name="d",
        settings_url="/s",
    )
    assert error["action"] is None
    assert error["message"] == "boom"
    assert error["hint"] == "boom"

    retrying = describe_tunnel_status(
        {"state": "retrying", "message": "HTTP 503; retrying."},
        project_name="d",
        settings_url="/s",
    )
    assert retrying["label"] == "Reconnecting"
    assert retrying["tone"] == "loading"
    assert retrying["hint"] == "HTTP 503; retrying."

    degraded = describe_tunnel_status(
        {"state": "degraded", "message": "Health probe timed out."},
        project_name="d",
        settings_url="/s",
    )
    assert degraded["label"] == "Connection lost"
    assert degraded["tone"] == "error"
    assert degraded["hint"] == "Health probe timed out."


def _zip_with(member: str, payload: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr(member, payload)
    return buffer.getvalue()


class _FakeOpener:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.urls: list[str] = []

    def open(self, request, timeout=None):
        self.urls.append(request.full_url)
        return io.BytesIO(self.payload)


def test_install_verifies_archive_and_binary_hashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"#!/bin/sh\necho tunnel-client\n"
    archive = _zip_with("tunnel-client", payload)
    asset = TunnelClientAsset(
        "darwin-arm64",
        hashlib.sha256(archive).hexdigest(),
        hashlib.sha256(payload).hexdigest(),
        "tunnel-client",
    )
    opener = _FakeOpener(archive)
    monkeypatch.setattr(tunnel_runtime.urllib.request, "build_opener", lambda *handlers: opener)
    monkeypatch.setattr(tunnel_runtime, "detect_outbound_proxy", lambda: "")
    destination = tmp_path / "bin" / "tunnel-client"
    install_tunnel_client(asset, destination)
    assert destination.read_bytes() == payload
    assert opener.urls == [
        "https://github.com/openai/tunnel-client/releases/download/"
        f"v{tunnel_runtime.TUNNEL_CLIENT_VERSION}/{asset.file_name}"
    ]

    tampered = TunnelClientAsset("darwin-arm64", asset.archive_sha256, "0" * 64, "tunnel-client")
    with pytest.raises(TunnelRuntimeError, match="binary failed SHA-256"):
        install_tunnel_client(tampered, tmp_path / "other" / "tunnel-client")
    wrong_archive = TunnelClientAsset("darwin-arm64", "0" * 64, asset.binary_sha256, "tunnel-client")
    with pytest.raises(TunnelRuntimeError, match="download failed SHA-256"):
        install_tunnel_client(wrong_archive, tmp_path / "third" / "tunnel-client")
