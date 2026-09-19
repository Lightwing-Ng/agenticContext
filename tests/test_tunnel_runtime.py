"""tunnel-client supervision tests with a local fake client.

Code version: v1.2.0-codex.0
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
