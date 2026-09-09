"""Durable compute-job lifecycle and safety contract tests.

Code version: v1.3.0-codex.1
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
    scan_compute_job_metadata,
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

    assert started["job_id"] == duplicate["job_id"]
    assert started["max_runtime_seconds"] == 43_200
    metadata = manager._load_metadata(str(started["job_id"]))
    workspace_metadata = workspace.stat()
    assert metadata["workspace_device"] == int(workspace_metadata.st_dev)
    assert metadata["workspace_inode"] == int(workspace_metadata.st_ino)
    finished = _wait_for_terminal(ComputeJobManager(workspace, runtime), str(started["job_id"]))
    assert finished["state"] == "succeeded"
    assert finished["progress"]["evaluations_completed"] == 8
    assert "optimization complete" in finished["log_tail"]
    assert _workspace_mutation_fingerprint(workspace) == before_fingerprint


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
    assert controller.state.edit_generation == 0
    _wait_for_terminal(controller._compute_job_manager(), result["job"]["job_id"])


def test_active_job_blocks_controller_workspace_mutations_and_run(tmp_path: Path) -> None:
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
    (workspace / "existing.txt").write_text("original\n", encoding="utf-8")
    controller = WorkspaceController(
        workspace,
        ComputerUseSettings(command_timeout_seconds=5),
        lambda: False,
        compute_job_runtime_root=tmp_path / "runtime",
    )
    started = controller.execute(
        {
            "action": "job_start",
            "entrypoint": "optimizer",
            "config_path": "optimizer.json",
            "idempotency_key": "controller-blocking-001",
        }
    )
    assert started["ok"]
    job_id = str(started["job"]["job_id"])
    blocked_actions = (
        {
            "action": "replace",
            "path": "existing.txt",
            "old": "original\n",
            "new": "changed\n",
        },
        {
            "action": "replace_base64",
            "path": "existing.txt",
            "old_base64": "b3JpZ2luYWwK",
            "new_base64": "Y2hhbmdlZAo=",
        },
        {"action": "write", "path": "new.txt", "content": "new\n"},
        {
            "action": "write_base64",
            "path": "new-base64.txt",
            "content_base64": "bmV3Cg==",
        },
        {
            "action": "delete",
            "path": "existing.txt",
            "expected_sha256": _sha256(workspace / "existing.txt"),
        },
        {"action": "run", "command": "python3 -m py_compile optimizer.py"},
    )
    try:
        for action in blocked_actions:
            result = controller.execute(action)
            assert result["ok"] is False
            assert "durable compute job is active" in result["error"]
        assert (workspace / "existing.txt").read_text(encoding="utf-8") == "original\n"
        assert not (workspace / "new.txt").exists()
        assert not (workspace / "new-base64.txt").exists()
        assert controller.execute({"action": "bodycheck"})["ok"] is True
    finally:
        stopped = controller.execute({"action": "job_stop", "job_id": job_id})
        assert stopped["ok"] is True


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
    assert completed["can_resume"]

    resumed = manager.start(
        entrypoint_id="optimizer",
        config_path="optimizer.json",
        idempotency_key="resume-target-001",
        resume_job_id=str(first["job_id"]),
    )
    assert resumed["resumed_from"] == first["job_id"]
    assert _wait_for_terminal(manager, str(resumed["job_id"]))["state"] == "succeeded"

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


def test_corrupt_job_metadata_is_never_silently_skipped(tmp_path: Path) -> None:
    workspace, _config = _prepare_workspace(tmp_path, script_body=SUCCESS_SCRIPT)
    manager = ComputeJobManager(workspace, tmp_path / "runtime")
    job_root = manager.jobs_root / ("c" * 32)
    job_root.mkdir()
    (job_root / "metadata.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(ComputeJobError, match="Invalid JSON"):
        manager.status()


def test_global_metadata_scan_is_bounded_and_validates_workspace_identity(
    tmp_path: Path,
) -> None:
    workspace, _config = _prepare_workspace(tmp_path, script_body=SUCCESS_SCRIPT)
    runtime = tmp_path / "runtime"
    manager = ComputeJobManager(workspace, runtime)
    workspace_metadata = workspace.stat()
    for job_id in ("d" * 32, "e" * 32):
        job_root = manager.jobs_root / job_id
        job_root.mkdir()
        compute_jobs._atomic_write_json(
            job_root / "metadata.json",
            {
                "schema_version": 1,
                "job_id": job_id,
                "workspace": str(workspace),
                "workspace_device": int(workspace_metadata.st_dev),
                "workspace_inode": int(workspace_metadata.st_ino),
                "state": "succeeded",
            },
        )

    with pytest.raises(ComputeJobError, match="bounded scan limit"):
        scan_compute_job_metadata(runtime, maximum_records=1)

    metadata_path = manager.jobs_root / ("d" * 32) / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.pop("workspace_inode")
    compute_jobs._atomic_write_json(metadata_path, metadata)
    with pytest.raises(ComputeJobError, match="workspace identity"):
        scan_compute_job_metadata(runtime)


def test_global_metadata_scan_reconciles_a_stale_active_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _config = _prepare_workspace(tmp_path, script_body=SUCCESS_SCRIPT)
    runtime = tmp_path / "runtime"
    manager = ComputeJobManager(workspace, runtime)
    workspace_metadata = workspace.stat()
    job_id = "f" * 32
    job_root = manager.jobs_root / job_id
    job_root.mkdir()
    compute_jobs._atomic_write_json(
        job_root / "metadata.json",
        {
            "schema_version": 1,
            "job_id": job_id,
            "workspace": str(workspace),
            "workspace_device": int(workspace_metadata.st_dev),
            "workspace_inode": int(workspace_metadata.st_ino),
            "state": "running",
            "pid": 987_654,
            "process_identity": "missing-worker-birth",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "ended_at": "",
        },
    )
    monkeypatch.setattr(compute_jobs, "_process_identity", lambda _pid: "")

    records = scan_compute_job_metadata(runtime, reconcile_liveness=True)

    assert records[0]["state"] == "interrupted"
    assert manager._load_metadata(job_id)["state"] == "interrupted"


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
