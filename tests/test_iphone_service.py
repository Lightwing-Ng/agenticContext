"""IPhoneService contracts: lazy runtime creation, whitelists, sanitization, and errors.

Every test injects a fake runtime and a fake WebDriverAgent client, so nothing here
imports the vendored macOS modules, starts a process, or contacts a device.

Code version: v1.0.2-codex.0
"""

from __future__ import annotations

import base64
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time
from typing import Any

import pytest

from app.core.iphone.service import (
    ACTION_OPS,
    SETUP_ACTIONS,
    IPhoneOperationError,
    IPhoneService,
    IPhoneUnavailableError,
    mask_team_id,
    png_dimensions,
    sanitize_result,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    + struct.pack(">I", 13)
    + b"IHDR"
    + struct.pack(">II", 1179, 2556)
    + b"\x08\x02\x00\x00\x00"
    + b"\x00" * 16
)


class WDAError(Exception):
    """Mirror of the vendored error; the service recognizes it by class name."""

    def __init__(self, code, message, uncertain=False, details=None):
        super().__init__(message)
        self.code, self.uncertain, self.details = code, uncertain, details or {}


class FakeScreen:
    def __init__(self) -> None:
        self.is_paused = False
        self.busy = False
        self.closed = 0

    def paused(self) -> bool:
        return self.is_paused

    def pause_status(self) -> dict[str, Any]:
        return {"paused": self.is_paused, "pause_reason": "authentication" if self.is_paused else None}

    def start(self) -> dict[str, Any]:
        return {"busy": self.busy, "frame": None}

    def close(self) -> None:
        self.closed += 1


class FakeSetupManager:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def setup(self, action: str, **arguments: Any) -> dict[str, Any]:
        self.calls.append((action, arguments))
        return {
            "ok": True,
            "configured": True,
            "config": {
                "udid": "00008110-000A1B2C3D4E5F60",
                "team_id": "ABCDE12345",
                "bundle_id": "com.example.wda",
                "source_dir": str(self.state_dir / "runtime" / "WebDriverAgent"),
                "local_port": 18100,
            },
            "jobs": [{
                "id": "a" * 32,
                "action": "build",
                "state": "running",
                "log_path": str(self.state_dir / "logs" / ("a" * 32 + ".log")),
                "worker_entrypoint": "/repo/app/core/iphone/wda_setup.py",
                "log_tail": f"Compiling {Path.home()}/Projects/WebDriverAgent",
            }],
            "service": {"reachable": False, "ready": False},
            "runtime": str(self.state_dir),
        }

    def doctor(self) -> dict[str, Any]:
        self.calls.append(("doctor", {}))
        return {
            "ok": False,
            "checks": {"macos": True, "full_xcode": False},
            "binaries": {"xcodebuild": "/usr/bin/xcodebuild", "node": None},
            "developer_dir": "/Library/Developer/CommandLineTools",
            "source_dir": str(self.state_dir / "runtime" / "WebDriverAgent"),
            "runtime": str(self.state_dir),
            "next_steps": ["Install full Xcode."],
        }


class FakeRuntime:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.base_url = "http://127.0.0.1:18100"
        self.screen = FakeScreen()
        self.setup_manager = FakeSetupManager(state_dir)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.results: dict[str, Any] = {}
        self.closed = 0

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, args))
        result = self.results.get(name, {"ok": True, "tool": name})
        if isinstance(result, BaseException):
            raise result
        if callable(result):
            return result(args)
        return result

    def close(self) -> None:
        self.closed += 1


class FakeWDAClient:
    def __init__(self, response: Any = None, error: BaseException | None = None) -> None:
        self.response = response
        self.error = error
        self.requests: list[tuple[str, str]] = []
        self.closed = 0

    def request(self, method: str, path: str, payload: Any = None) -> Any:
        self.requests.append((method, path))
        if self.error is not None:
            raise self.error
        return self.response

    def close(self) -> None:
        self.closed += 1


@pytest.fixture
def runtime(tmp_path: Path) -> FakeRuntime:
    return FakeRuntime(tmp_path / "iphone-state")


@pytest.fixture
def factory_calls() -> list[Path | None]:
    return []


@pytest.fixture
def service(tmp_path: Path, runtime: FakeRuntime, factory_calls: list[Path | None]) -> IPhoneService:
    def create(state_dir: Path | None) -> FakeRuntime:
        factory_calls.append(state_dir)
        return runtime

    return IPhoneService(
        tmp_path / "iphone-state",
        platform="darwin",
        runtime_factory=create,
    )


# Construction, platform, and lifecycle
def test_construction_touches_nothing_and_shutdown_is_a_no_op(tmp_path, factory_calls) -> None:
    state_dir = tmp_path / "never-created"
    service = IPhoneService(
        state_dir,
        platform="darwin",
        runtime_factory=lambda path: factory_calls.append(path),
    )

    assert service.supported is True
    assert service.started() is False
    service.stop_at_exit()
    service.stop_at_exit()
    assert factory_calls == []
    assert not state_dir.exists()


def test_default_platform_follows_the_host() -> None:
    service = IPhoneService()
    assert service.platform == sys.platform
    assert service.supported is (sys.platform == "darwin")


def test_unsupported_platform_reports_status_and_refuses_everything_else(factory_calls) -> None:
    service = IPhoneService(platform="win32", runtime_factory=factory_calls.append)

    status = service.status()
    assert status["supported"] is False
    assert status["started"] is False
    assert "macOS" in status["reason"]
    for call in (
        service.doctor,
        service.ready,
        service.screenshot_png,
        lambda: service.setup("discover", {}),
        lambda: service.frame(0, 0),
        lambda: service.screen("pause"),
        lambda: service.screen_action("refresh"),
        lambda: service.act("observe", {}),
        lambda: service.apps("Maps"),
    ):
        with pytest.raises(IPhoneUnavailableError):
            call()
    assert factory_calls == []


def test_runtime_is_created_once_under_concurrent_first_calls(tmp_path, runtime) -> None:
    created: list[Path | None] = []
    barrier = threading.Barrier(8)

    def slow_factory(state_dir: Path | None) -> FakeRuntime:
        created.append(state_dir)
        time.sleep(0.05)
        return runtime

    service = IPhoneService(tmp_path / "state", platform="darwin", runtime_factory=slow_factory)
    errors: list[BaseException] = []

    def first_call() -> None:
        barrier.wait()
        try:
            service.ready()
        except BaseException as exc:  # pragma: no cover - surfaced by the assertion
            errors.append(exc)

    threads = [threading.Thread(target=first_call) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert errors == []
    assert created == [tmp_path / "state"]
    assert service.started() is True
    assert len(runtime.calls) == 8


def test_production_state_dir_defers_to_the_runtime_resolution(runtime) -> None:
    received: list[Path | None] = []
    service = IPhoneService(
        platform="darwin",
        runtime_factory=lambda state_dir: received.append(state_dir) or runtime,
    )
    service.ready()
    assert received == [None]


@pytest.mark.parametrize("failure", [ValueError("Runtime state must be outside all Git checkouts."), OSError("denied")])
def test_runtime_construction_failure_is_unavailable_and_retried(tmp_path, runtime, failure) -> None:
    attempts: list[int] = []

    def factory(_state_dir: Path | None) -> FakeRuntime:
        attempts.append(1)
        if len(attempts) == 1:
            raise failure
        return runtime

    service = IPhoneService(tmp_path / "state", platform="darwin", runtime_factory=factory)
    with pytest.raises(IPhoneUnavailableError, match=str(failure)):
        service.status()
    assert service.started() is False
    assert service.status()["started"] is True
    assert len(attempts) == 2


def test_stop_at_exit_closes_a_started_runtime_and_never_raises(service, runtime) -> None:
    service.ready()
    service.stop_at_exit()
    assert runtime.closed == 1

    def broken_close() -> None:
        raise RuntimeError("node child already gone")

    runtime.close = broken_close
    service.stop_at_exit()


def test_service_module_and_facade_do_not_load_the_vendored_runtime() -> None:
    script = (
        "import sys\n"
        "import app.core.iphone.service\n"
        "from app.core.agent import IPhoneService, IPhoneOperationError, IPhoneUnavailableError\n"
        "assert IPhoneService is app.core.iphone.service.IPhoneService\n"
        "loaded = sorted(name for name in sys.modules if name.startswith('app.core.iphone.'))\n"
        "print(','.join(loaded))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    # The fcntl-based vendored modules load only when a runtime is first created.
    assert completed.stdout.strip() == "app.core.iphone.service"


# Status and doctor
def test_status_masks_signing_and_strips_host_paths_without_the_operation_lock(service, runtime) -> None:
    runtime.screen.busy = True

    status = service.status()

    assert status["supported"] is True
    assert status["started"] is True
    assert status["state_dir"] == str(runtime.state_dir)
    assert status["preview"] == {"paused": False, "pause_reason": None}
    assert status["busy"] is True
    setup = status["setup"]
    assert setup["config"]["team_id"] == "******2345"
    assert setup["config"]["udid"] == "00008110-000A1B2C3D4E5F60"
    assert "source_dir" not in setup["config"]
    assert "runtime" not in setup
    job = setup["jobs"][0]
    assert "log_path" not in job
    assert "worker_entrypoint" not in job
    assert job["log_tail"] == "Compiling ~/Projects/WebDriverAgent"
    assert runtime.setup_manager.calls == [("status", {})]
    # Status reads setup state directly; it never enters runtime.call or operation.lock.
    assert runtime.calls == []


def test_status_reports_busy_while_a_page_action_is_running(service, runtime) -> None:
    observed: list[bool] = []

    def tap(_args: dict[str, Any]) -> dict[str, Any]:
        observed.append(service.status()["busy"])
        return {"ok": True}

    runtime.results["pua_tap"] = tap
    service.act("tap", {"x": 10, "y": 20})

    assert observed == [True]
    assert service.status()["busy"] is False


def test_status_masks_signing_identifiers_in_build_logs(service, runtime, monkeypatch) -> None:
    setup = runtime.setup_manager.setup("status")
    log = (
        f"Compiling {Path.home()}/Projects/WebDriverAgent\n"
        "DEVELOPMENT_TEAM = ABCDE12345\n"
        'Signing failed for Team "VWXYZ67890"'
    )
    setup["jobs"][0]["log_tail"] = log
    monkeypatch.setattr(runtime.setup_manager, "setup", lambda **_kwargs: setup)

    result = service.status()["setup"]

    assert result["config"]["team_id"] == "******2345"
    assert result["jobs"][0]["log_tail"] == (
        "Compiling ~/Projects/WebDriverAgent\n"
        "DEVELOPMENT_TEAM = ******2345\n"
        'Signing failed for Team "******7890"'
    )
    assert setup["jobs"][0]["log_tail"] == log
    assert setup["config"]["team_id"] == "ABCDE12345"


def test_doctor_reads_setup_directly_and_reduces_binaries_to_presence(service, runtime) -> None:
    result = service.doctor()

    assert runtime.setup_manager.calls == [("doctor", {})]
    assert runtime.calls == []
    assert result["binaries"] == {"xcodebuild": True, "node": False}
    for key in ("developer_dir", "source_dir", "runtime"):
        assert key not in result
    assert result["checks"]["full_xcode"] is False


# Setup and connection
@pytest.mark.parametrize("action", ["status", "recover", "", "configure "])
def test_setup_rejects_actions_outside_the_whitelist_before_creating_the_runtime(
    service, factory_calls, action,
) -> None:
    with pytest.raises(IPhoneOperationError) as caught:
        service.setup(action, {})
    assert caught.value.code == "invalid_argument"
    assert caught.value.details["allowed_actions"] == list(SETUP_ACTIONS)
    assert factory_calls == []


@pytest.mark.parametrize("key", ["source_dir", "local_port", "device_port", "wait_seconds", "action"])
def test_setup_rejects_arguments_outside_the_whitelist(service, factory_calls, key) -> None:
    with pytest.raises(IPhoneOperationError) as caught:
        service.setup("configure", {"udid": "00008110-000A1B2C3D4E5F60", key: "x"})
    assert caught.value.code == "invalid_argument"
    assert caught.value.details["unknown_fields"] == [key]
    assert factory_calls == []


def test_setup_start_never_waits_and_configure_passes_whitelisted_fields(service, runtime) -> None:
    runtime.results["pua_setup"] = lambda args: {
        "ok": True,
        "config": {"team_id": args.get("team_id", ""), "source_dir": "/Users/someone/wda"},
    }

    service.setup("start", {})
    configured = service.setup(
        "configure",
        {"udid": "00008110-000A1B2C3D4E5F60", "team_id": "ABCDE12345", "bundle_id": "com.example.wda"},
    )
    service.setup("stop", {"job_id": "b" * 32})

    assert runtime.calls == [
        ("pua_setup", {"action": "start", "wait_seconds": 0}),
        ("pua_setup", {
            "action": "configure",
            "udid": "00008110-000A1B2C3D4E5F60",
            "team_id": "ABCDE12345",
            "bundle_id": "com.example.wda",
        }),
        ("pua_setup", {"action": "stop", "job_id": "b" * 32}),
    ]
    assert configured == {"ok": True, "config": {"team_id": "******2345"}}


def test_ready_verifies_without_a_screenshot_and_with_recovery(service, runtime) -> None:
    runtime.results["pua_ready"] = {
        "ready": True,
        "observation": {"image": {"path": "/private/tmp/shot.jpg", "mimeType": "image/jpeg", "width": 390}},
    }

    result = service.ready()

    assert runtime.calls == [("pua_ready", {"screenshot": False, "recover": True})]
    assert result["observation"]["image"] == {"mimeType": "image/jpeg", "width": 390}


# Live preview
def test_frame_passes_cursors_and_keeps_encoded_data(service, runtime) -> None:
    home = str(Path.home())
    encoded = f"{home}/AAAA"  # Base64 text must never be rewritten by home redaction.
    runtime.results["pua_screen_frame"] = {
        "frame": {"seq": 4, "mimeType": "image/jpeg", "width": 390, "height": 844, "data": encoded},
        "paused": False,
    }

    wire = service.frame(3, 9)

    assert runtime.calls == [("pua_screen_frame", {"after_seq": 3, "last_event_id": 9})]
    assert wire["frame"]["data"] == encoded
    assert service.status()["busy"] is False


@pytest.mark.parametrize(
    ("after_seq", "last_event_id"),
    [(-1, 0), (0, -1), (2**53, 0), (True, 0), (0, 1.5), ("1", 0)],
)
def test_frame_rejects_invalid_cursors_before_creating_the_runtime(
    service, factory_calls, after_seq, last_event_id,
) -> None:
    with pytest.raises(IPhoneOperationError) as caught:
        service.frame(after_seq, last_event_id)
    assert caught.value.code == "invalid_argument"
    assert factory_calls == []


def test_screen_and_toolbar_actions_are_whitelisted(service, runtime, factory_calls) -> None:
    for call in (
        lambda: service.screen("open"),
        lambda: service.screen_action("screenshot"),
        lambda: service.screen_action("HOME"),
    ):
        with pytest.raises(IPhoneOperationError) as caught:
            call()
        assert caught.value.code == "invalid_argument"
    assert factory_calls == []

    service.screen("pause")
    service.screen("resume")
    service.screen_action("refresh")
    service.screen_action("home")
    assert runtime.calls == [
        ("pua_screen", {"action": "pause"}),
        ("pua_screen", {"action": "resume"}),
        ("pua_screen_action", {"action": "refresh"}),
        ("pua_screen_action", {"action": "home"}),
    ]


def test_screenshot_returns_a_validated_native_png(tmp_path, runtime) -> None:
    clients: list[FakeWDAClient] = []

    def client_factory(base_url: str, timeout: float) -> FakeWDAClient:
        assert base_url == runtime.base_url
        assert timeout <= 10
        client = FakeWDAClient({"value": base64.b64encode(PNG_BYTES).decode()})
        clients.append(client)
        return client

    service = IPhoneService(
        tmp_path, platform="darwin",
        runtime_factory=lambda _path: runtime,
        wda_client_factory=client_factory,
    )

    assert service.screenshot_png() == PNG_BYTES
    assert clients[0].requests == [("GET", "/screenshot")]
    assert clients[0].closed == 1
    # A capture is not a runtime tool call and never enters the operation lock.
    assert runtime.calls == []


def test_screenshot_is_refused_while_the_preview_is_paused(tmp_path, runtime) -> None:
    created: list[Any] = []
    runtime.screen.is_paused = True
    service = IPhoneService(
        tmp_path, platform="darwin",
        runtime_factory=lambda _path: runtime,
        wda_client_factory=lambda *args: created.append(args),
    )

    with pytest.raises(IPhoneOperationError) as caught:
        service.screenshot_png()

    assert caught.value.code == "preview_paused"
    assert caught.value.details["action_executed"] is False
    assert created == []


@pytest.mark.parametrize(
    ("response", "error", "code"),
    [
        ({"value": "not base64!"}, None, "invalid_response"),
        ({"value": None}, None, "invalid_response"),
        ({"value": base64.b64encode(b"GIF89a" + b"\x00" * 40).decode()}, None, "invalid_response"),
        (None, WDAError("pua_unreachable", "PUA is not reachable."), "pua_unreachable"),
    ],
)
def test_screenshot_failures_are_operation_errors(tmp_path, runtime, response, error, code) -> None:
    client = FakeWDAClient(response, error)
    service = IPhoneService(
        tmp_path, platform="darwin",
        runtime_factory=lambda _path: runtime,
        wda_client_factory=lambda *_args: client,
    )

    with pytest.raises(IPhoneOperationError) as caught:
        service.screenshot_png()

    assert caught.value.code == code
    assert client.closed == 1


# Phone actions
@pytest.mark.parametrize("op", ["batch", "setup", "doctor", "screen_frame", "Tap", ""])
def test_act_rejects_operations_outside_the_whitelist(service, factory_calls, op) -> None:
    with pytest.raises(IPhoneOperationError) as caught:
        service.act(op, {})
    assert caught.value.code == "invalid_argument"
    assert caught.value.details["allowed_ops"] == list(ACTION_OPS)
    assert factory_calls == []


def test_act_requires_object_arguments(service, factory_calls) -> None:
    with pytest.raises(IPhoneOperationError):
        service.act("tap", ["x", 1])
    assert factory_calls == []


@pytest.mark.parametrize("op", ACTION_OPS)
def test_act_dispatches_each_whitelisted_operation(service, runtime, op) -> None:
    service.act(op, {"example": op})
    assert runtime.calls == [(f"pua_{op}", {"example": op})]


def test_act_results_lose_image_paths(service, runtime) -> None:
    runtime.results["pua_observe"] = {
        "image": {"path": "/private/var/folders/x/shot.jpg", "mimeType": "image/jpeg", "pixel_to_point": [0.5, 0.5]},
        "nodes": [{"type": "Button", "label": "path", "path": "/session/1/element/2"}],
    }

    result = service.act("observe", {"mode": "both"})

    assert result["image"] == {"mimeType": "image/jpeg", "pixel_to_point": [0.5, 0.5]}
    # Only image descriptors lose ``path``; ordinary node fields are preserved.
    assert result["nodes"][0]["path"] == "/session/1/element/2"


@pytest.mark.parametrize("query", ["", "   ", "x" * 101, None])
def test_apps_validates_the_query_before_creating_the_runtime(service, factory_calls, query) -> None:
    with pytest.raises(IPhoneOperationError) as caught:
        service.apps(query)
    assert caught.value.code == "invalid_argument"
    assert factory_calls == []


def test_apps_searches_every_source_with_a_bounded_limit(service, runtime) -> None:
    service.apps("  Maps ")
    assert runtime.calls == [("pua_apps", {"query": "Maps", "source": "auto", "limit": 10})]


# Error translation
def test_wda_errors_become_sanitized_operation_errors(service, runtime) -> None:
    runtime.results["pua_tap"] = WDAError(
        "ambiguous_target",
        "Several matches.",
        uncertain=False,
        details={
            "code": "spoofed",
            "message": "spoofed",
            "observation": {"image": {"path": "/private/tmp/err.jpg", "mimeType": "image/jpeg"}},
            "log_path": "/private/tmp/log",
        },
    )

    with pytest.raises(IPhoneOperationError) as caught:
        service.act("tap", {"selector": {"label": "OK"}})

    error = caught.value
    assert error.code == "ambiguous_target"
    assert error.message == "Several matches."
    body = error.as_dict()
    assert body["code"] == "ambiguous_target"
    assert body["message"] == "Several matches."
    assert body["uncertain"] is False
    assert body["observation"] == {"image": {"mimeType": "image/jpeg"}}
    assert "log_path" not in body


def test_unexpected_runtime_failures_are_internal_errors(service, runtime) -> None:
    runtime.results["pua_launch_app"] = OSError("/Users/someone/state/operation.lock: denied")

    with pytest.raises(IPhoneOperationError) as caught:
        service.act("launch_app", {"bundle_id": "com.apple.Maps"})

    assert caught.value.code == "internal_error"
    assert "/Users" not in caught.value.message
    assert caught.value.details["uncertain"] is True
    assert service.status()["busy"] is False


# Helpers
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("DEVELOPMENT_TEAM=ABCDE12345", "DEVELOPMENT_TEAM=******2345"),
        ('DEVELOPMENT_TEAM = "ABCDE12345"', 'DEVELOPMENT_TEAM = "******2345"'),
        ("DEVELOPMENT_TEAM='ABCDE12345'", "DEVELOPMENT_TEAM='******2345'"),
        ('Team "ABCDE12345"', 'Team "******2345"'),
        ("Team 'ABCDE12345'", "Team '******2345'"),
        ("DEVELOPMENT_TEAM=ABCDE123456", "DEVELOPMENT_TEAM=ABCDE123456"),
        ("Reference ABCDE12345", "Reference ABCDE12345"),
    ],
)
def test_sanitize_result_masks_only_recognized_signing_log_formats(text, expected) -> None:
    assert sanitize_result({"log_tail": text}, home="/home/example") == {"log_tail": expected}
    assert sanitize_result({"data": text}, home="/home/example") == {"data": text}


def test_helpers_mask_validate_and_redact() -> None:
    assert mask_team_id("ABCDE12345") == "******2345"
    assert mask_team_id("ABC") == "***"
    assert mask_team_id("") == ""
    assert mask_team_id(None) is None
    assert png_dimensions(PNG_BYTES) == (1179, 2556)
    assert png_dimensions(b"\x89PNG\r\n\x1a\n") is None
    assert sanitize_result({"a": "/home/me/x", "b": "/home/me", "c": "/home/meadow"}, home="/home/me") == {
        "a": "~/x", "b": "~", "c": "/home/meadow",
    }
    assert sanitize_result(["/x/y"], home="/") == ["/x/y"]
