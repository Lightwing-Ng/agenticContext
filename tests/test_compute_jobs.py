"""Durable compute-job lifecycle and safety contract tests.

Code version: v1.3.4-codex.1
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

import pytest

from app.core.agent import compute_jobs
from app.core.agent.compute_jobs import (
    ComputeJobError,
    ComputeJobManager,
    MAX_LOG_BYTES,
    validate_optimizer_checkpoint,
    write_optimizer_checkpoint_atomic,
)
from app.core.computer_use_agent import (
    ComputerUseSettings,
    WorkspaceController,
    _workspace_mutation_fingerprint,
)
from app.web.app import create_app


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _prepare_workspace(
    tmp_path: Path,
    *,
    script_body: str,
    max_runtime_seconds: int = 43_200,
) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    script = workspace / "optimizer.py"
    script.write_text(script_body, encoding="utf-8")
    config = workspace / "optimizer.json"
    config.write_text('{"generations":2}', encoding="utf-8")
    approval = {
        "schema_version": 1,
        "entrypoints": [
            {
                "id": "optimizer",
                "path": "optimizer.py",
                "sha256": _sha256(script),
                "max_runtime_seconds": max_runtime_seconds,
            }
        ],
    }
    (workspace / ".agenticContext-compute.json").write_text(
        json.dumps(approval),
        encoding="utf-8",
    )
    return workspace, config


def _wait_for_terminal(manager: ComputeJobManager, job_id: str, timeout: float = 8) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = manager.status(job_id)
        if status["state"] in compute_jobs.TERMINAL_STATES:
            return status
        time.sleep(0.05)
    raise AssertionError(f"compute job {job_id} did not reach a terminal state")


SUCCESS_SCRIPT = """
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--config", required=True)
parser.add_argument("--job-runtime", required=True)
parser.add_argument("--resume")
args = parser.parse_args()
runtime = Path(args.job_runtime)
progress = runtime / "progress.json"
temporary = runtime / ".progress.tmp"
temporary.write_text(json.dumps({"iteration": 2, "evaluations_completed": 8, "summary": "Generation 2 complete"}))
temporary.replace(progress)
checkpoint = {
    "schema_version": 1,
    "optimizer_version": "test-v1",
    "iteration": 2,
    "population": [[1, 2]],
    "rng_state": {"state": 42},
    "seed": 7,
    "best_objective": 0.25,
    "best_parameters": {"workers": 4},
    "evaluation_count": 8,
}
temporary = runtime / ".checkpoint.tmp"
temporary.write_text(json.dumps(checkpoint))
temporary.replace(runtime / "checkpoint.json")
(runtime / "result.json").write_text(json.dumps({"best_objective": 0.25}))
print("optimization complete")
"""


def test_job_start_is_durable_idempotent_and_outside_verification_timeout(tmp_path: Path) -> None:
    workspace, _config = _prepare_workspace(tmp_path, script_body=SUCCESS_SCRIPT)
    runtime = tmp_path / "runtime"
    manager = ComputeJobManager(workspace, runtime)
    before_fingerprint = _workspace_mutation_fingerprint(workspace)

    started = manager.start(
        entrypoint_id="optimizer",
        config_path="optimizer.json",
        idempotency_key="nightly-search-001",
    )
    duplicate = manager.start(
        entrypoint_id="optimizer",
        config_path="optimizer.json",
        idempotency_key="nightly-search-001",
    )

    assert started["job_id"] == duplicate["job_id"], {
        "started": started,
        "duplicate": duplicate,
    }
    assert started["max_runtime_seconds"] == 43_200, started
    finished = _wait_for_terminal(ComputeJobManager(workspace, runtime), str(started["job_id"]))
    assert finished["state"] == "succeeded", finished
    assert finished["progress"].get("evaluations_completed") == 8, finished
    assert "optimization complete" in finished["log_tail"], finished
    assert _workspace_mutation_fingerprint(workspace) == before_fingerprint, finished


def test_controller_job_actions_do_not_change_verification_generation(tmp_path: Path) -> None:
    workspace, _config = _prepare_workspace(tmp_path, script_body=SUCCESS_SCRIPT)
    controller = WorkspaceController(
        workspace,
        ComputerUseSettings(command_timeout_seconds=5),
        lambda: False,
        compute_job_runtime_root=tmp_path / "runtime",
    )
    result = controller.execute(
        {
            "action": "job_start",
            "entrypoint": "optimizer",
            "config_path": "optimizer.json",
            "idempotency_key": "controller-search-001",
        }
    )
    assert result["ok"]
    assert controller.state.edit_generation == 0
    assert controller.state.verification_generation == -1
    status = controller.execute({"action": "job_status", "job_id": result["job"]["job_id"]})
    assert status["ok"]
    _wait_for_terminal(controller._compute_job_manager(), result["job"]["job_id"])


def test_checkpoint_is_atomic_validated_and_explicitly_resumable(tmp_path: Path) -> None:
    workspace, _config = _prepare_workspace(tmp_path, script_body=SUCCESS_SCRIPT)
    runtime = tmp_path / "runtime"
    manager = ComputeJobManager(workspace, runtime)
    first = manager.start(
        entrypoint_id="optimizer",
        config_path="optimizer.json",
        idempotency_key="resume-source-001",
    )
    completed = _wait_for_terminal(manager, str(first["job_id"]))
    assert completed["state"] == "succeeded", completed
    assert completed["can_resume"]

    resumed = manager.start(
        entrypoint_id="optimizer",
        config_path="optimizer.json",
        idempotency_key="resume-target-001",
        resume_job_id=str(first["job_id"]),
    )
    assert resumed["resumed_from"] == first["job_id"]
    finished = _wait_for_terminal(manager, str(resumed["job_id"]))
    assert finished["state"] == "succeeded", finished

    checkpoint_dir = tmp_path / "checkpoint"
    checkpoint_dir.mkdir()
    checkpoint = {
        "schema_version": 1,
        "optimizer_version": "v1",
        "iteration": 4,
        "optimizer_state": {"temperature": 0.4},
        "rng_state": [1, 2, 3],
        "seed": 9,
        "best_objective": 1.5,
        "best_parameters": {"batch": 32},
        "evaluation_count": 100,
    }
    path = write_optimizer_checkpoint_atomic(checkpoint_dir, checkpoint)
    assert json.loads(path.read_text(encoding="utf-8")) == checkpoint
    assert not list(checkpoint_dir.glob("*.tmp"))


def test_checkpoint_rejects_incomplete_optimizer_state() -> None:
    with pytest.raises(ComputeJobError, match="missing"):
        validate_optimizer_checkpoint({"schema_version": 1})


def test_manifest_hash_paths_and_runtime_bounds_fail_closed(tmp_path: Path) -> None:
    workspace, config = _prepare_workspace(tmp_path, script_body=SUCCESS_SCRIPT)
    manager = ComputeJobManager(workspace, tmp_path / "runtime")
    (workspace / "optimizer.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    with pytest.raises(ComputeJobError, match="SHA-256"):
        manager.start(
            entrypoint_id="optimizer",
            config_path=config.name,
            idempotency_key="changed-script-001",
        )
    (workspace / "optimizer.py").write_text(SUCCESS_SCRIPT, encoding="utf-8")
    with pytest.raises(ComputeJobError, match="relative"):
        manager.start(
            entrypoint_id="optimizer",
            config_path="../optimizer.json",
            idempotency_key="traversal-config-001",
        )

    workspace_two, _ = _prepare_workspace(
        tmp_path / "second",
        script_body=SUCCESS_SCRIPT,
        max_runtime_seconds=1_800,
    )
    with pytest.raises(ComputeJobError, match="43,200"):
        ComputeJobManager(workspace_two, tmp_path / "runtime-two").start(
            entrypoint_id="optimizer",
            config_path="optimizer.json",
            idempotency_key="short-runtime-001",
        )


def test_symlink_and_shell_shaped_config_are_rejected(tmp_path: Path) -> None:
    workspace, _config = _prepare_workspace(tmp_path, script_body=SUCCESS_SCRIPT)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    link = workspace / "linked.json"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    manager = ComputeJobManager(workspace, tmp_path / "runtime")
    for candidate in ("linked.json", "optimizer.json;curl.example"):
        with pytest.raises(ComputeJobError):
            manager.start(
                entrypoint_id="optimizer",
                config_path=candidate,
                idempotency_key=f"reject-{candidate[:12]}-001".replace(";", "-"),
            )


def test_restart_reconciles_live_and_missing_worker_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, _config = _prepare_workspace(tmp_path, script_body=SUCCESS_SCRIPT)
    manager = ComputeJobManager(workspace, tmp_path / "runtime")
    job_root = manager.jobs_root / ("a" * 32)
    job_root.mkdir()
    metadata = {
        "schema_version": 1,
        "job_id": "a" * 32,
        "workspace": str(workspace),
        "state": "running",
        "pid": 123,
        "process_identity": "birth-a",
        "started_at": "2026-09-01T00:00:00+00:00",
        "updated_at": "2026-09-01T00:00:00+00:00",
    }
    compute_jobs._atomic_write_json(job_root / "metadata.json", metadata)
    monkeypatch.setattr(compute_jobs, "_process_identity", lambda pid: "birth-a" if pid == 123 else "")
    manager.reconcile()
    assert manager._load_metadata("a" * 32)["state"] == "running"
    monkeypatch.setattr(compute_jobs, "_process_identity", lambda _pid: "")
    manager.reconcile()
    assert manager._load_metadata("a" * 32)["state"] == "interrupted"


def test_pid_reuse_refuses_stop_without_signaling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace, _config = _prepare_workspace(tmp_path, script_body=SUCCESS_SCRIPT)
    manager = ComputeJobManager(workspace, tmp_path / "runtime")
    job_root = manager.jobs_root / ("b" * 32)
    job_root.mkdir()
    metadata = {
        "schema_version": 1,
        "job_id": "b" * 32,
        "workspace": str(workspace),
        "state": "running",
        "pid": 321,
        "process_identity": "original-birth",
        "started_at": "2026-09-01T00:00:00+00:00",
        "updated_at": "2026-09-01T00:00:00+00:00",
    }
    compute_jobs._atomic_write_json(job_root / "metadata.json", metadata)
    monkeypatch.setattr(compute_jobs, "_process_identity", lambda _pid: "reused-birth")
    signaled: list[int] = []
    monkeypatch.setattr(compute_jobs, "_terminate_process_group", lambda pid, timeout=5: signaled.append(pid))
    with pytest.raises(ComputeJobError, match="refusing"):
        manager.stop("b" * 32)
    assert signaled == []
    assert manager._load_metadata("b" * 32)["state"] == "interrupted"


def test_stop_terminates_owned_worker_tree(tmp_path: Path) -> None:
    script = """
import argparse
from pathlib import Path
import subprocess
import sys
import time
parser = argparse.ArgumentParser()
parser.add_argument("--config", required=True)
parser.add_argument("--job-runtime", required=True)
parser.add_argument("--resume")
args = parser.parse_args()
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
(Path(args.job_runtime) / "child.pid").write_text(str(child.pid))
time.sleep(60)
"""
    workspace, _config = _prepare_workspace(tmp_path, script_body=script)
    manager = ComputeJobManager(workspace, tmp_path / "runtime")
    started = manager.start(
        entrypoint_id="optimizer",
        config_path="optimizer.json",
        idempotency_key="stop-tree-001",
    )
    child_path = manager.jobs_root / str(started["job_id"]) / "child.pid"
    deadline = time.monotonic() + 5
    while not child_path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert child_path.exists()
    child_pid = int(child_path.read_text(encoding="utf-8"))
    stopped = manager.stop(str(started["job_id"]))
    assert stopped["state"] == "stopped"
    deadline = time.monotonic() + 5
    while compute_jobs._process_identity(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not compute_jobs._process_identity(child_pid)


def test_active_job_limit_blocks_a_second_request(tmp_path: Path) -> None:
    script = """
import argparse
import time
parser = argparse.ArgumentParser()
parser.add_argument("--config", required=True)
parser.add_argument("--job-runtime", required=True)
parser.add_argument("--resume")
parser.parse_args()
time.sleep(60)
"""
    workspace, _config = _prepare_workspace(tmp_path, script_body=script)
    manager = ComputeJobManager(workspace, tmp_path / "runtime")
    first = manager.start(
        entrypoint_id="optimizer",
        config_path="optimizer.json",
        idempotency_key="concurrency-first-001",
    )
    with pytest.raises(ComputeJobError, match="concurrency is limited to 1"):
        manager.start(
            entrypoint_id="optimizer",
            config_path="optimizer.json",
            idempotency_key="concurrency-second-001",
        )
    assert manager.stop(str(first["job_id"]))["state"] == "stopped"


def test_macos_job_sleep_assertion_is_identity_checked_and_released(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _config = _prepare_workspace(tmp_path, script_body=SUCCESS_SCRIPT)
    executable = tmp_path / "caffeinate"
    executable.write_text("fixture", encoding="utf-8")
    manager = ComputeJobManager(
        workspace,
        tmp_path / "runtime",
        caffeinate_executable=executable,
    )

    class FakeProcess:
        pid = 8123

    commands: list[list[str]] = []
    monkeypatch.setattr(compute_jobs.sys, "platform", "darwin")
    monkeypatch.setattr(
        compute_jobs.subprocess,
        "Popen",
        lambda command, **_kwargs: commands.append(command) or FakeProcess(),
    )
    monkeypatch.setattr(
        compute_jobs,
        "_process_identity",
        lambda pid: "assertion-birth" if pid == 8123 else "",
    )
    metadata = {"pid": 7001}
    manager._start_sleep_assertion(metadata)
    assert commands == [[str(executable), "-i", "-w", "7001"]]
    assert metadata["sleep_assertion_identity"] == "assertion-birth"

    signaled: list[tuple[int, int]] = []
    monkeypatch.setattr(compute_jobs.os, "kill", lambda pid, sig: signaled.append((pid, sig)))
    manager._release_assertion(metadata)
    assert signaled == [(8123, compute_jobs.signal.SIGTERM)]


def test_log_tail_and_file_are_bounded(tmp_path: Path) -> None:
    script = f"""
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--config", required=True)
parser.add_argument("--job-runtime", required=True)
parser.add_argument("--resume")
parser.parse_args()
print("x" * {MAX_LOG_BYTES + 256_000})
"""
    workspace, _config = _prepare_workspace(tmp_path, script_body=script)
    manager = ComputeJobManager(workspace, tmp_path / "runtime")
    started = manager.start(
        entrypoint_id="optimizer",
        config_path="optimizer.json",
        idempotency_key="bounded-log-001",
    )
    status = _wait_for_terminal(manager, str(started["job_id"]))
    log_path = manager.jobs_root / str(started["job_id"]) / "worker.log"
    assert log_path.stat().st_size <= MAX_LOG_BYTES
    assert len(status["log_tail"]) <= compute_jobs.MAX_LOG_TAIL_CHARS


def test_status_filters_unpublished_progress_fields(tmp_path: Path) -> None:
    workspace, _config = _prepare_workspace(tmp_path, script_body=SUCCESS_SCRIPT)
    manager = ComputeJobManager(workspace, tmp_path / "runtime")
    job_id = "d" * 32
    job_root = manager.jobs_root / job_id
    job_root.mkdir()
    compute_jobs._atomic_write_json(
        job_root / "metadata.json",
        {
            "schema_version": 1,
            "job_id": job_id,
            "workspace": str(workspace),
            "state": "succeeded",
            "started_at": "2026-09-01T00:00:00+00:00",
            "updated_at": "2026-09-01T00:01:00+00:00",
            "ended_at": "2026-09-01T00:01:00+00:00",
        },
    )
    compute_jobs._atomic_write_json(
        job_root / "progress.json",
        {
            "iteration": 7,
            "summary": "x" * (compute_jobs.MAX_STATUS_TEXT_CHARS + 10),
            "private_debug_state": "must not cross the status boundary",
        },
    )

    progress = manager.status(job_id)["progress"]

    assert progress["iteration"] == 7
    assert len(progress["summary"]) == compute_jobs.MAX_STATUS_TEXT_CHARS
    assert "private_debug_state" not in progress


@pytest.mark.skipif(
    sys.platform != "darwin" or not compute_jobs.MACOS_SANDBOX_EXECUTABLE.is_file(),
    reason="macOS sandbox-exec is required for the real network-denial probe",
)
def test_macos_worker_cannot_open_a_network_socket(tmp_path: Path) -> None:
    script = """
import argparse
from pathlib import Path
import socket

parser = argparse.ArgumentParser()
parser.add_argument("--config", required=True)
parser.add_argument("--job-runtime", required=True)
parser.add_argument("--resume")
args = parser.parse_args()
try:
    network_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    network_socket.bind(("127.0.0.1", 0))
except OSError:
    (Path(args.job_runtime) / "result.json").write_text('{"network":"denied"}')
else:
    network_socket.close()
    raise SystemExit("network socket unexpectedly allowed")
"""
    workspace, _config = _prepare_workspace(tmp_path, script_body=script)
    manager = ComputeJobManager(workspace, tmp_path / "runtime")
    started = manager.start(
        entrypoint_id="optimizer",
        config_path="optimizer.json",
        idempotency_key="network-denial-001",
    )

    status = _wait_for_terminal(manager, str(started["job_id"]))

    assert status["state"] == "succeeded"
    result_path = manager.jobs_root / str(started["job_id"]) / "result.json"
    assert json.loads(result_path.read_text(encoding="utf-8")) == {"network": "denied"}


def test_compute_job_stop_route_uses_the_dedicated_job_boundary(tmp_path: Path) -> None:
    workspace, _config = _prepare_workspace(tmp_path, script_body=SUCCESS_SCRIPT)
    app = create_app(
        tmp_path / "local-store",
        computer_use_settings_path=tmp_path / "settings.json",
        computer_use_runtime_root=tmp_path / "agent-runtime",
        agent_external_operations_enabled=True,
    )
    service = app.extensions["computer_use_agent_service"]
    expected = {"job_id": "c" * 32, "state": "stopped", "active": False}
    with patch.object(service, "stop_compute_job", return_value=expected) as stop:
        with app.test_client() as client:
            response = client.post(
                "/api/agent/compute-job/stop",
                json={"workspace_path": str(workspace), "job_id": "c" * 32},
            )
    assert response.status_code == 200
    assert response.get_json() == {"compute_job": expected}
    stop.assert_called_once_with(str(workspace), "c" * 32)


def test_native_process_identity_is_live_stable_and_not_reused() -> None:
    import subprocess

    process = subprocess.Popen([sys.executable, "-c", (
        "import sys, time; sys.stdout.buffer.write(b'ready\\n'); "
        "sys.stdout.buffer.flush(); time.sleep(30)"
    )],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
    try:
        assert process.stdout.readline() == b"ready\n"
        first = compute_jobs._process_identity(process.pid)
        assert first
        assert compute_jobs._process_identity(process.pid) == first
        if sys.platform == "win32":
            assert first.startswith("windows:")
    finally:
        process.terminate()
        process.wait(timeout=5)
        process.stdout.close()
    assert compute_jobs._process_identity(process.pid) == ""


def test_windows_birth_identity_uses_native_handle_and_closes_it(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace
    from app.core.agent import _windows_processes

    closed: list[int] = []
    handle = 0x1234_5678_9ABC

    def process_times(actual_handle, created, *_other):
        assert actual_handle == handle
        created._obj.high = 0x12345678
        created._obj.low = 0x9ABCDEF0
        return 1

    kernel = SimpleNamespace(
        OpenProcess=lambda rights, inherit, pid: handle if pid == 4321 and not inherit else None,
        WaitForSingleObject=lambda actual_handle, timeout: 258,
        GetProcessTimes=process_times,
        CloseHandle=lambda actual_handle: closed.append(actual_handle),
    )
    monkeypatch.setattr(_windows_processes, "_kernel", lambda: kernel)
    assert _windows_processes.process_identity(4321) == "windows:123456789abcdef0"
    assert closed == [handle]
    kernel.WaitForSingleObject = lambda *_arguments: 0
    assert _windows_processes.process_identity(4321) == ""
    assert closed == [handle, handle]


def _running_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ComputeJobManager, str]:
    workspace, _config = _prepare_workspace(tmp_path, script_body=SUCCESS_SCRIPT)
    manager = ComputeJobManager(workspace, tmp_path / "runtime")
    job_id = "e" * 32
    manager._save_metadata({
        "schema_version": 1, "job_id": job_id, "workspace": str(workspace),
        "state": "running", "pid": 424242, "process_identity": "test-birth",
        "started_at": compute_jobs._utc_now(), "updated_at": compute_jobs._utc_now(),
        "ended_at": "",
    })
    monkeypatch.setattr(compute_jobs, "_process_identity", lambda pid: "test-birth" if pid == 424242 else "")
    return manager, job_id


@pytest.mark.parametrize("failure", [PermissionError("access denied"), ComputeJobError("taskkill failed")])
def test_stop_failure_remains_active_and_diagnostic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    manager, job_id = _running_record(tmp_path, monkeypatch)

    def fail_termination(*_arguments, **_options):
        raise failure

    monkeypatch.setattr(compute_jobs, "_terminate_process_group", fail_termination)
    with pytest.raises(ComputeJobError, match="not confirmed"):
        manager.stop(job_id)
    status = manager.status(job_id)
    assert status["state"] == "stopping"
    assert status["active"] is True
    assert status["ended_at"] == ""
    assert str(failure) in status["message"]


def test_successful_termination_command_cannot_hide_a_live_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, job_id = _running_record(tmp_path, monkeypatch)
    monkeypatch.setattr(compute_jobs, "_terminate_process_group", lambda *_args, **_kwargs: None)
    with pytest.raises(ComputeJobError, match="still running"):
        manager.stop(job_id)
    assert manager.status(job_id)["active"] is True


def test_windows_taskkill_failure_is_checked_and_uses_system_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import os
    import subprocess
    from types import SimpleNamespace

    windows = SimpleNamespace(**{**vars(os), "name": "nt"})
    monkeypatch.setattr(compute_jobs, "os", windows)
    monkeypatch.setenv("SYSTEMROOT", str(tmp_path / "Windows root"))
    commands: list[list[str]] = []
    monkeypatch.setattr(compute_jobs.subprocess, "run", lambda command, **_kwargs: commands.append(command) or subprocess.CompletedProcess(command, 1))
    with pytest.raises(ComputeJobError, match="status 1"):
        compute_jobs._terminate_process_group(424242)
    assert commands == [[str(tmp_path / "Windows root" / "System32" / "taskkill.exe"), "/PID", "424242", "/T", "/F"]]


def _worker_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], Path]:
    metadata_path = tmp_path / "metadata.json"
    compute_jobs._atomic_write_json(metadata_path, {"state": "starting", "job_id": "f" * 32})
    entrypoint = tmp_path / "optimizer.py"
    entrypoint.write_text("# Not executed by fake-process tests.\n", encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(compute_jobs, "_process_identity", lambda _pid: "test-worker-birth")
    monkeypatch.setattr(compute_jobs, "_start_sleep_assertion", lambda *_arguments: None)
    return [str(metadata_path), str(entrypoint), str(config), "43200"], metadata_path


def test_worker_pipe_failure_cleans_child_and_publishes_original_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    arguments, metadata_path = _worker_fixture(tmp_path, monkeypatch)
    output = SimpleNamespace(read1=Mock(side_effect=OSError("pipe read failed")), close=Mock())
    child = SimpleNamespace(stdout=output, poll=Mock(return_value=None), terminate=Mock(), kill=Mock(), wait=Mock(return_value=-1))
    monkeypatch.setattr(compute_jobs.subprocess, "Popen", lambda *_args, **_kwargs: child)
    assert compute_jobs._worker_main(arguments) == 70
    child.terminate.assert_called_once()
    child.wait.assert_called_once_with(timeout=5)
    output.close.assert_called_once()
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["state"] == "failed"
    assert "pipe read failed" in metadata["message"]
    assert metadata["ended_at"]


def test_worker_cleanup_failure_never_publishes_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    from types import SimpleNamespace
    from unittest.mock import Mock

    arguments, metadata_path = _worker_fixture(tmp_path, monkeypatch)
    child = SimpleNamespace(stdout=io.BytesIO(b"completed\n"), poll=Mock(return_value=0), wait=Mock(return_value=0))
    job = SimpleNamespace(terminate_descendants=Mock(), close_after_cleanup=Mock(side_effect=OSError("job cleanup denied")))
    monkeypatch.setattr(compute_jobs.subprocess, "Popen", lambda *_args, **_kwargs: child)
    assert compute_jobs._worker_main(arguments, windows_job=job) == 70
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["state"] == "failed"
    assert metadata["exit_status"] == 0
    assert "job cleanup denied" in metadata["message"]


def test_worker_spawn_failure_has_terminal_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import Mock

    arguments, metadata_path = _worker_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(compute_jobs.subprocess, "Popen", Mock(side_effect=OSError("spawn denied")))
    assert compute_jobs._worker_main(arguments) == 70
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["state"] == "failed"
    assert "spawn denied" in metadata["message"]


def test_worker_preserves_primary_and_cleanup_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    arguments, metadata_path = _worker_fixture(tmp_path, monkeypatch)
    output = SimpleNamespace(read1=Mock(side_effect=OSError("primary read failure")), close=Mock())
    child = SimpleNamespace(stdout=output, poll=Mock(return_value=None), terminate=Mock(side_effect=PermissionError("cleanup denied")))
    monkeypatch.setattr(compute_jobs.subprocess, "Popen", lambda *_args, **_kwargs: child)
    assert compute_jobs._worker_main(arguments) == 70
    message = json.loads(metadata_path.read_text(encoding="utf-8"))["message"]
    assert "primary read failure" in message
    assert "cleanup denied" in message


def test_windows_job_setup_failure_prevents_optimizer_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    from types import SimpleNamespace
    from unittest.mock import Mock

    arguments, metadata_path = _worker_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(compute_jobs, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    monkeypatch.setattr(compute_jobs._windows_processes, "WorkerJob", Mock(side_effect=PermissionError("job assignment denied")))
    run_worker = Mock()
    monkeypatch.setattr(compute_jobs, "_worker_main", run_worker)
    assert compute_jobs._main(["--worker", *arguments]) == 70
    run_worker.assert_not_called()
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["state"] == "failed"
    assert "job assignment denied" in metadata["message"]


@pytest.fixture
def windows_stop_kernel(monkeypatch: pytest.MonkeyPatch):
    from types import SimpleNamespace
    from app.core.agent import _windows_processes

    state = {"counts": [0], "member": 1, "wait_result": 0, "terminated": False}
    calls: list[tuple] = []
    closed: list[int] = []

    def process_times(handle, created, *_other):
        assert handle == 777
        created._obj.low = 42
        return 1

    def membership(process, job, member):
        assert (process, job) == (777, 999)
        member._obj.value = state["member"]
        return 1

    def terminate(handle, status):
        assert (handle, status) == (999, 1)
        state["terminated"] = True
        return 1

    def wait(handle, milliseconds):
        assert handle == 777
        if milliseconds == 0:
            return 258
        assert state["terminated"]
        calls.append(("wait", milliseconds))
        return state["wait_result"]

    def query_job(handle, information_class, accounting, _size, _returned):
        assert handle == 999
        assert information_class == 1
        active = state["counts"].pop(0)
        calls.append(("active", active))
        accounting._obj.active_processes = active
        return 1

    kernel = SimpleNamespace(
        OpenJobObjectW=lambda rights, inherit, name: 999,
        OpenProcess=lambda rights, inherit, pid: 777 if pid == 424242 and not inherit else None,
        GetProcessTimes=process_times,
        IsProcessInJob=membership,
        WaitForSingleObject=wait,
        TerminateJobObject=terminate,
        QueryInformationJobObject=query_job,
        CloseHandle=lambda handle: closed.append(handle),
    )
    monkeypatch.setattr(_windows_processes, "_kernel", lambda: kernel)
    return state, calls, closed


@pytest.mark.parametrize("counts", [[0], [3, 1, 0]])
def test_windows_stop_waits_for_worker_signal_and_every_job_member(windows_stop_kernel, counts) -> None:
    from app.core.agent import _windows_processes

    state, calls, closed = windows_stop_kernel
    state["counts"] = counts.copy()
    _windows_processes.terminate_job("Local\\test-job", pid=424242, expected_identity="windows:000000000000002a")
    assert state["terminated"] is True
    assert calls[0][0] == "wait"
    assert 0 < calls[0][1] <= 5000
    assert calls[1:] == [("active", count) for count in counts]
    assert closed == [777, 999]


@pytest.mark.parametrize("changed", ["identity", "membership"])
def test_windows_stop_refuses_reused_pid_or_foreign_job(windows_stop_kernel, changed) -> None:
    from app.core.agent import _windows_processes

    state, calls, closed = windows_stop_kernel
    if changed == "membership":
        state["member"] = 0
    expected = "old-birth" if changed == "identity" else "windows:000000000000002a"
    with pytest.raises(OSError, match="identity changed|no longer belongs"):
        _windows_processes.terminate_job("Local\\test-job", pid=424242, expected_identity=expected)
    assert state["terminated"] is False
    assert calls == []
    assert closed == [777, 999]


@pytest.mark.parametrize("wait_result", [258, 0xFFFFFFFF])
def test_windows_stop_unconfirmed_signal_remains_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows_stop_kernel, wait_result: int,
) -> None:
    import os
    from types import SimpleNamespace
    from app.core.agent import _windows_processes

    manager, job_id = _running_record(tmp_path, monkeypatch)
    birth = "windows:000000000000002a"
    metadata = manager._load_metadata(job_id)
    metadata.update(process_identity=birth, windows_job_name="Local\\test-job")
    manager._save_metadata(metadata)
    monkeypatch.setattr(compute_jobs, "_process_identity", lambda _pid: birth)
    monkeypatch.setattr(compute_jobs, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    monkeypatch.setattr(_windows_processes.ctypes, "get_last_error", lambda: 5, raising=False)
    monkeypatch.setattr(_windows_processes.ctypes, "WinError", lambda code: OSError(code, "wait denied"), raising=False)
    state, calls, closed = windows_stop_kernel
    state["wait_result"] = wait_result
    with pytest.raises(ComputeJobError, match="not confirmed"):
        manager.stop(job_id)
    status = manager.status(job_id)
    assert state["terminated"] is True
    assert calls[0][0] == "wait"
    assert len(calls) == 1
    assert closed == [777, 999]
    assert status["state"] == "stopping"
    assert status["active"] is True
    assert status["ended_at"] == ""
    assert ("termination could not be confirmed" if wait_result == 258 else "wait denied") in status["message"]


def test_windows_job_descendant_pid_reuse_does_not_kill_foreign_process(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock
    from app.core.agent import _windows_processes

    job = object.__new__(_windows_processes.WorkerJob)
    job.handle = 999
    inventories = iter([[12345], []])
    job._process_ids = lambda: next(inventories)
    terminate = Mock()

    def membership(_process, _job, result):
        result._obj.value = 0
        return 1

    job.kernel = SimpleNamespace(
        OpenProcess=lambda *_arguments: 777,
        IsProcessInJob=membership,
        TerminateProcess=terminate,
        CloseHandle=lambda _handle: None,
    )
    job.terminate_descendants()
    terminate.assert_not_called()


def test_output_thread_start_failure_cleans_started_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    from types import SimpleNamespace
    from unittest.mock import Mock

    arguments, metadata_path = _worker_fixture(tmp_path, monkeypatch)
    child = SimpleNamespace(stdout=io.BytesIO(), poll=Mock(return_value=None), terminate=Mock(), wait=Mock(return_value=-1))
    monkeypatch.setattr(compute_jobs.subprocess, "Popen", lambda *_args, **_kwargs: child)
    monkeypatch.setattr(compute_jobs.threading.Thread, "start", Mock(side_effect=RuntimeError("reader start denied")))
    assert compute_jobs._worker_main(arguments) == 70
    child.terminate.assert_called_once()
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["state"] == "failed"
    assert "reader start denied" in metadata["message"]


def test_startup_cleanup_failure_stays_active_with_diagnostic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    workspace, _config = _prepare_workspace(tmp_path, script_body=SUCCESS_SCRIPT)
    manager = ComputeJobManager(workspace, tmp_path / "runtime")
    child = SimpleNamespace(pid=424242, poll=Mock(return_value=None), terminate=Mock(side_effect=PermissionError("startup cleanup denied")))
    monkeypatch.setattr(compute_jobs.subprocess, "Popen", lambda *_args, **_kwargs: child)
    monkeypatch.setattr(compute_jobs, "_terminate_process_group", Mock(side_effect=PermissionError("startup cleanup denied")))
    monkeypatch.setattr(compute_jobs, "_process_identity", lambda pid: "test-birth" if pid == 424242 else "")
    monkeypatch.setattr(compute_jobs.time, "sleep", lambda _seconds: None)
    with pytest.raises(ComputeJobError, match="Startup cleanup is not confirmed"):
        manager.start(entrypoint_id="optimizer", config_path="optimizer.json", idempotency_key="startup-cleanup-001")
    status = manager.status()
    assert status["state"] == "stopping"
    assert status["active"] is True
    assert status["ended_at"] == ""
    assert "startup cleanup denied" in status["message"]


def test_worker_environment_preserves_path_authority_without_credentials(tmp_path: Path) -> None:
    script = '''
import argparse
import json
import os
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument("--config", required=True)
parser.add_argument("--job-runtime", required=True)
args = parser.parse_args()
(Path(args.job_runtime) / "result.json").write_text(json.dumps(dict(os.environ)))
'''
    workspace, _config = _prepare_workspace(tmp_path, script_body=script)
    manager = ComputeJobManager(workspace, tmp_path / "runtime")
    import os

    location_keys = {
        "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "HOMEDRIVE", "HOMEPATH",
        "AGENTIC_CONTEXT_RUNTIME_ROOT", "AGENTIC_CONTEXT_SETTINGS_PATH", "TMPDIR", "TEMP", "TMP",
    }
    expected = {key: os.environ[key] for key in location_keys if key in os.environ}
    with patch.dict(os.environ, {"AGENTIC_TEST_PRIVATE_TOKEN": "synthetic-secret"}):
        started = manager.start(entrypoint_id="optimizer", config_path="optimizer.json", idempotency_key="isolated-environment-001")
        finished = _wait_for_terminal(manager, str(started["job_id"]))
    assert finished["state"] == "succeeded"
    environment = json.loads((manager.jobs_root / str(started["job_id"]) / "result.json").read_text(encoding="utf-8"))
    assert {key: environment[key] for key in expected} == expected
    assert "AGENTIC_TEST_PRIVATE_TOKEN" not in environment
