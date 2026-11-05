"""Durable compute-job lifecycle and safety contract tests.

Code version: v1.6.6-codex.0
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier, Event, Lock
import time
from types import SimpleNamespace
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
from app.core.computer_use_agent import ComputerUseSettings
from app.core.workspace.controller import WorkspaceController
from app.core.workspace.evidence import _workspace_mutation_fingerprint
from app.web.app import create_app


@pytest.fixture
def windows_worker_probe(monkeypatch):
    import ctypes

    def install(*, handle=42, error=0, exit_code=259, query_ok=True, query_error=False):
        closed = []

        class Call:
            def __init__(self, action):
                self.action = action

            def __call__(self, *args):
                return self.action(*args)

        def open_process(access, inherit, pid):
            assert (access, inherit, pid) == (0x1000, False, 321)
            return handle

        def query(process, output):
            assert process == handle
            if query_error:
                raise OSError("Synthetic native process query failure")
            output._obj.value = exit_code
            return query_ok

        kernel = SimpleNamespace(
            OpenProcess=Call(open_process),
            GetExitCodeProcess=Call(query),
            CloseHandle=Call(closed.append),
        )
        monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: kernel, raising=False)
        monkeypatch.setattr(ctypes, "get_last_error", lambda: error, raising=False)
        return closed

    return install


@pytest.mark.parametrize("error,expected", [(87, False), (5, True), (0, True), (123, True)])
def test_windows_worker_absence_requires_a_definitive_native_result(windows_worker_probe, error, expected):
    closed = windows_worker_probe(handle=0, error=error)

    assert compute_jobs._windows_worker_may_be_alive(321) is expected
    assert closed == []


@pytest.mark.parametrize("exit_code,query_ok,query_error,expected", [
    (259, True, False, True), (0, True, False, False),
    (259, False, False, True), (259, True, True, True),
])
def test_windows_worker_probe_closes_handles_and_preserves_unknown_liveness(
    windows_worker_probe, exit_code, query_ok, query_error, expected,
):
    closed = windows_worker_probe(exit_code=exit_code, query_ok=query_ok, query_error=query_error)

    assert compute_jobs._windows_worker_may_be_alive(321) is expected
    assert closed == [42]


def test_windows_worker_current_pid_never_needs_a_signal_or_native_query(monkeypatch):
    import ctypes

    def unexpected_probe(*_args, **_kwargs):
        pytest.fail("The current process is already known to be alive.")

    monkeypatch.setattr(ctypes, "WinDLL", unexpected_probe, raising=False)
    monkeypatch.setattr(compute_jobs.os, "kill", unexpected_probe)

    assert compute_jobs._windows_worker_may_be_alive(os.getpid()) is True


@pytest.mark.parametrize("observed,may_be_alive", [("", True), ("", False), ("different-birth", None)])
def test_windows_reconciliation_requires_absence_when_worker_identity_is_unknown(
    tmp_path, monkeypatch, observed, may_be_alive,
):
    metadata = {
        "state": "running", "pid": 321, "process_identity": "expected-birth",
        "containment_kind": "windows-job-object", "job_id": "1" * 32,
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    windows_os = SimpleNamespace(**{**vars(os), "name": "nt"})
    monkeypatch.setattr(compute_jobs, "os", windows_os)
    monkeypatch.setattr(compute_jobs, "_process_identity", lambda _pid: observed)
    monkeypatch.setattr(compute_jobs, "_update_metadata_file", lambda _path, update: update(metadata) or metadata)
    monkeypatch.setattr(compute_jobs, "_release_sleep_assertion", lambda _metadata: None)
    probes = []

    def probe(pid):
        assert pid == metadata["pid"] and may_be_alive is not None
        probes.append(pid)
        return may_be_alive

    monkeypatch.setattr(compute_jobs, "_windows_worker_may_be_alive", probe)
    if may_be_alive is True:
        with pytest.raises(ComputeJobError, match="liveness could not be verified safely"):
            compute_jobs._reconcile_scanned_compute_job_metadata(tmp_path / "metadata.json", metadata)
        assert metadata["state"] == "running"
    else:
        result = compute_jobs._reconcile_scanned_compute_job_metadata(tmp_path / "metadata.json", metadata)
        assert result["state"] == "interrupted"
    assert probes == ([] if may_be_alive is None else [321])


def test_native_worker_probe_observes_current_and_exited_owned_process(monkeypatch):
    if os.name == "nt":
        def unexpected_signal(*_args, **_kwargs):
            pytest.fail("Windows liveness checks must not emit console signals.")
        monkeypatch.setattr(compute_jobs.os, "kill", unexpected_signal)
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **options)
    try:
        assert compute_jobs._windows_worker_may_be_alive(os.getpid()) is True
        if os.name == "nt":
            assert compute_jobs._windows_worker_may_be_alive(child.pid) is True
        else:
            assert compute_jobs._process_identity(child.pid)
    finally:
        child.terminate()
        child.wait(timeout=5)
    if os.name == "nt":
        assert compute_jobs._windows_worker_may_be_alive(child.pid) is False
    else:
        assert compute_jobs._process_identity(child.pid) == ""


def test_ps_process_identity_forces_a_stable_locale_and_timezone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_environment: dict[str, str] = {}

    def run_probe(*_args, **kwargs):
        observed_environment.update(kwargs["env"])
        return subprocess.CompletedProcess(
            args=["ps"],
            returncode=0,
            stdout="Sat Sep 12 07:00:00 2026\n",
            stderr="",
        )

    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    monkeypatch.setattr(compute_jobs.subprocess, "run", run_probe)

    identity = compute_jobs._ps_process_identity(123)

    assert identity.startswith("ps:123:")
    assert observed_environment["LC_ALL"] == "C"
    assert observed_environment["TZ"] == "UTC"


def test_linux_marker_scan_skips_permission_protected_environments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cooperative marker scan must tolerate unrelated protected processes."""

    class ProcessEntry:
        def __init__(self, pid: int) -> None:
            self.name = str(pid)

        def stat(self, *, follow_symlinks: bool) -> SimpleNamespace:
            assert follow_symlinks is False
            return SimpleNamespace(st_uid=501)

    class ProcessEntries:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def __iter__(self):
            return iter(ProcessEntry(pid) for pid in (101, 102, 103))

    job_id = "1" * 32
    marker = f"AGENTIC_CONTEXT_COMPUTE_JOB={job_id}".encode("ascii")
    reads = {103: [marker + b"\0", b""]}
    closed: list[int] = []

    def open_environment(path: str, _flags: int) -> int:
        pid = int(Path(path).parent.name)
        if pid == 101:
            raise PermissionError(13, "Permission denied", path)
        return pid

    def read_environment(descriptor: int, _size: int) -> bytes:
        if descriptor == 102:
            raise PermissionError(13, "Permission denied")
        return reads[descriptor].pop(0)

    monkeypatch.setattr(compute_jobs.os, "scandir", lambda _path: ProcessEntries())
    monkeypatch.setattr(compute_jobs.os, "geteuid", lambda: 501, raising=False)
    monkeypatch.setattr(compute_jobs, "_process_identity", lambda pid: f"proc:{pid}")
    monkeypatch.setattr(compute_jobs.os, "open", open_environment)
    monkeypatch.setattr(compute_jobs.os, "read", read_environment)
    monkeypatch.setattr(compute_jobs.os, "close", closed.append)

    assert compute_jobs._scan_linux_job_marker_processes(
        job_id,
        exclude_pids=frozenset(),
        timeout=1.0,
    ) == {103: "proc:103"}
    assert closed == [102, 103]


def _probe_linux_cgroup_execution() -> bool:
    """Use the production cgroup constructor to decide whether Linux can execute jobs."""
    if not sys.platform.startswith("linux"):
        return False
    cgroup = None
    try:
        cgroup = compute_jobs._LinuxCgroup.create("0" * 32)
        cleared, _detected = cgroup.terminate_and_remove(timeout=0.2)
        return cleared
    except ComputeJobError:
        return False
    finally:
        if cgroup is not None and cgroup.path.exists():
            try:
                cgroup.path.rmdir()
            except OSError:
                pass


LINUX_CGROUP_EXECUTION_AVAILABLE = _probe_linux_cgroup_execution()
REQUIRES_HOST_COMPUTE_CONTAINMENT = pytest.mark.skipif(
    sys.platform.startswith("linux") and not LINUX_CGROUP_EXECUTION_AVAILABLE,
    reason="host lacks writable, verifiable Linux cgroup v2 process containment",
)


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


def _print_synthetic_compute_failure(
    manager: ComputeJobManager,
    status: dict[str, object],
) -> None:
    """Expose bounded synthetic lifecycle evidence when a native assertion fails."""
    job_id = str(status["job_id"])
    job_root = manager.jobs_root / job_id
    diagnostics = {
        key: status.get(key)
        for key in (
            "state", "message", "exit_status", "can_resume", "active",
            "started_at", "updated_at", "ended_at", "checkpoint_path",
            "resumed_from", "max_runtime_seconds", "progress",
        )
    }
    diagnostics["worker_log_exists"] = (job_root / "worker.log").is_file()
    diagnostics["log_tail_chars"] = len(str(status.get("log_tail") or ""))
    diagnostics["log_tail_end"] = str(status.get("log_tail") or "")[-512:]
    try:
        metadata = manager._load_metadata(job_id)
        diagnostics["metadata"] = {
            key: metadata.get(key)
            for key in (
                "state", "message", "error_type", "exit_status", "revision",
                "pid", "process_identity", "child_pid", "child_process_identity",
                "containment_kind", "containment_cleared_at", "updated_at", "ended_at",
            )
        }
    except (ComputeJobError, OSError) as exc:
        diagnostics["metadata_error_type"] = type(exc).__name__
        diagnostics["metadata_error"] = str(exc)
    checkpoint = job_root / "checkpoint.json"
    diagnostics["checkpoint_exists"] = checkpoint.is_file()
    if checkpoint.is_file():
        try:
            validate_optimizer_checkpoint(compute_jobs._read_json_object(
                checkpoint, maximum_bytes=compute_jobs.MAX_CONFIG_BYTES,
            ))
            diagnostics["checkpoint_valid"] = True
        except (ComputeJobError, OSError) as exc:
            diagnostics["checkpoint_valid"] = False
            diagnostics["checkpoint_error_type"] = type(exc).__name__
            diagnostics["checkpoint_error"] = str(exc)
    print("Synthetic compute failure: " + json.dumps(diagnostics, separators=(",", ":")))


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


@pytest.fixture
def captured_compute_worker_environment(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    temporary = tmp_path / "temporary"
    profile.mkdir()
    temporary.mkdir()
    platform_values = {
        "USERPROFILE": str(profile), "HOMEDRIVE": profile.drive or "C:",
        "HOMEPATH": "\\synthetic-profile", "APPDATA": str(profile / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(profile / "AppData" / "Local"),
        "TEMP": str(temporary), "TMP": str(temporary),
    }
    for name, value in platform_values.items():
        monkeypatch.setenv(name, value)
    for name in ("ARBITRARY_SECRET", "CREDENTIAL_TEST_SECRET", "TOKEN_TEST_SECRET"):
        monkeypatch.setenv(name, "synthetic-do-not-forward")
    workspace, _config = _prepare_workspace(tmp_path, script_body=SUCCESS_SCRIPT)
    manager = ComputeJobManager(workspace, tmp_path / "runtime")
    captured = {}

    def capture_launch(command, **options):
        assert command[:3] == [sys.executable, "-m", "app.core.agent.compute_jobs"]
        captured.update(options["env"])
        raise OSError("Synthetic launch boundary after environment capture")

    with monkeypatch.context() as patch:
        patch.setattr(compute_jobs.subprocess, "Popen", capture_launch)
        with pytest.raises(ComputeJobError, match="worker could not start"):
            manager.start(entrypoint_id="optimizer", config_path="optimizer.json", idempotency_key="environment-bootstrap-001")
    return captured, platform_values


def test_compute_worker_environment_keeps_only_explicit_platform_and_runtime_keys(captured_compute_worker_environment):
    environment, platform_values = captured_compute_worker_environment

    assert all(environment[name] == value for name, value in platform_values.items())
    assert not {"ARBITRARY_SECRET", "CREDENTIAL_TEST_SECRET", "TOKEN_TEST_SECRET"} & environment.keys()
    assert environment["AGENTIC_CONTEXT_COMPUTE_JOB"]
    assert environment["PYTHONUNBUFFERED"] == "1"
    assert environment["PYTHONPATH"] == str(Path(compute_jobs.__file__).resolve().parents[3])


def test_sanitized_worker_bootstrap_preserves_windows_home_resolution(captured_compute_worker_environment):
    environment, platform_values = captured_compute_worker_environment
    script = """import ntpath
from pathlib import Path
import runpy
def windows_home(cls):
    value = ntpath.expanduser('~')
    if value == '~':
        raise RuntimeError('Could not determine home directory.')
    return Path(value)
Path.home = classmethod(windows_home)
runpy.run_module('app.core.agent.compute_jobs', run_name='__main__')
"""
    stripped = {name: value for name, value in environment.items() if name not in platform_values}
    before = subprocess.run([sys.executable, "-c", script], env=stripped, capture_output=True, text=True, timeout=15)
    assert before.returncode == 1
    assert "Could not determine home directory." in before.stderr
    after = subprocess.run([sys.executable, "-c", script], env=environment, capture_output=True, text=True, timeout=15)
    assert after.returncode == 64
    assert "Could not determine home directory." not in after.stderr
    native = subprocess.run([sys.executable, "-m", "app.core.agent.compute_jobs"], env=environment, capture_output=True, text=True, timeout=15)
    assert native.returncode == 64


def _install_concurrent_start_harness(
    monkeypatch: pytest.MonkeyPatch,
    manager: ComputeJobManager,
) -> tuple[list[list[str]], list[Path]]:
    """Force two callers past input validation before the first staging publish."""
    approval_barrier = Barrier(2)
    original_approved_entrypoint = ComputeJobManager._approved_entrypoint

    def synchronized_approved_entrypoint(
        current: ComputeJobManager,
        entrypoint_id: str,
    ) -> tuple[Path, dict[str, object], bytes, str]:
        approved = original_approved_entrypoint(current, entrypoint_id)
        approval_barrier.wait(timeout=5.0)
        return approved

    monkeypatch.setattr(
        ComputeJobManager,
        "_approved_entrypoint",
        synchronized_approved_entrypoint,
    )

    original_atomic_write = compute_jobs._atomic_write_json
    staged_metadata_paths: list[Path] = []
    staging_guard = Lock()
    competing_stage = Event()

    def coordinated_atomic_write(path: Path, payload: dict[str, object]) -> None:
        stage_number = 0
        if path.name == "metadata.json" and path.parent.parent == manager.staging_root:
            with staging_guard:
                staged_metadata_paths.append(path)
                stage_number = len(staged_metadata_paths)
            if stage_number == 1:
                competing_stage.wait(timeout=0.5)
            elif stage_number == 2:
                competing_stage.set()
        original_atomic_write(path, payload)

    monkeypatch.setattr(compute_jobs, "_atomic_write_json", coordinated_atomic_write)

    popen_calls: list[list[str]] = []
    process_guard = Lock()

    class FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def poll(self) -> None:
            return None

    def fake_popen(command: list[str], **_kwargs: object) -> FakeProcess:
        with process_guard:
            popen_calls.append(command)
            pid = 51_000 + len(popen_calls)
        return FakeProcess(pid)

    monkeypatch.setattr(compute_jobs.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        compute_jobs,
        "_process_identity",
        lambda pid: f"test-process:{pid}",
    )
    return popen_calls, staged_metadata_paths


@REQUIRES_HOST_COMPUTE_CONTAINMENT
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


@REQUIRES_HOST_COMPUTE_CONTAINMENT
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


@REQUIRES_HOST_COMPUTE_CONTAINMENT
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


@REQUIRES_HOST_COMPUTE_CONTAINMENT
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
    if not completed["can_resume"]:
        _print_synthetic_compute_failure(manager, completed)
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


@pytest.mark.skipif(os.name != "posix", reason="Linux worker containment uses POSIX signals")
def test_linux_worker_refuses_to_run_without_verified_cgroup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, config_source = _prepare_workspace(
        tmp_path,
        script_body="from pathlib import Path\nPath('unexpected-run').write_text('ran')\n",
    )
    manager = ComputeJobManager(workspace, tmp_path / "runtime")
    job_id = "7" * 32
    job_root = manager.jobs_root / job_id
    job_root.mkdir()
    entrypoint = job_root / "entrypoint.py"
    config = job_root / "config.json"
    compute_jobs._write_runtime_snapshot(
        entrypoint,
        (workspace / "optimizer.py").read_bytes(),
    )
    compute_jobs._write_runtime_snapshot(config, config_source.read_bytes())
    workspace_metadata = workspace.stat()
    process_identity = compute_jobs._process_identity(os.getpid())
    assert process_identity
    compute_jobs._atomic_write_json(
        job_root / "metadata.json",
        {
            "schema_version": 1,
            "revision": 1,
            "job_id": job_id,
            "workspace": str(workspace),
            "workspace_device": int(workspace_metadata.st_dev),
            "workspace_inode": int(workspace_metadata.st_ino),
            "state": "running",
            "pid": os.getpid(),
            "process_identity": process_identity,
            "child_pid": 0,
            "child_process_identity": "",
            "child_process_group": 0,
            "child_cgroup": "",
            "containment_kind": "linux-cgroup-v2",
            "containment_cleared_at": "",
            "started_at": "2026-09-01T00:00:00+00:00",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "ended_at": "",
            "entrypoint_path": "optimizer.py",
            "entrypoint_snapshot_path": "entrypoint.py",
            "entrypoint_sha256": hashlib.sha256(entrypoint.read_bytes()).hexdigest(),
            "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
            "resume_checkpoint_path": "",
            "resume_checkpoint_sha256": "",
        },
    )
    monkeypatch.setattr(compute_jobs.sys, "platform", "linux")
    monkeypatch.setattr(compute_jobs.signal, "signal", lambda *_args: None)

    def refuse_cgroup(_job_id: str) -> None:
        raise ComputeJobError("cgroup unavailable")

    monkeypatch.setattr(compute_jobs._LinuxCgroup, "create", refuse_cgroup)

    exit_status = compute_jobs._worker_main(
        [str(job_root / "metadata.json"), str(entrypoint), str(config), "43_200"]
    )

    assert exit_status == 70
    metadata = manager._load_metadata(job_id)
    assert metadata["state"] == "failed"
    assert metadata["containment_cleared_at"]
    assert "approved code was not run" in str(metadata["message"])
    assert not (workspace / "unexpected-run").exists()


@REQUIRES_HOST_COMPUTE_CONTAINMENT
def test_job_executes_approved_snapshot_after_live_entrypoint_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approved_script = """
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--config", required=True)
parser.add_argument("--job-runtime", required=True)
parser.add_argument("--resume")
args = parser.parse_args()
(Path(args.job_runtime) / "result.json").write_text(
    json.dumps({"source": "approved-snapshot"}),
    encoding="utf-8",
)
"""
    swapped_script = approved_script.replace("approved-snapshot", "swapped-live-source")
    workspace, _config = _prepare_workspace(tmp_path, script_body=approved_script)
    manager = ComputeJobManager(workspace, tmp_path / "runtime")
    approved_entrypoint = manager._approved_entrypoint

    def approve_then_swap(entrypoint_id: str):
        approved = approved_entrypoint(entrypoint_id)
        (workspace / "optimizer.py").write_text(swapped_script, encoding="utf-8")
        return approved

    monkeypatch.setattr(manager, "_approved_entrypoint", approve_then_swap)

    started = manager.start(
        entrypoint_id="optimizer",
        config_path="optimizer.json",
        idempotency_key="approved-snapshot-001",
    )
    finished = _wait_for_terminal(manager, str(started["job_id"]))
    job_root = manager.jobs_root / str(started["job_id"])

    assert finished["state"] == "succeeded"
    assert json.loads((job_root / "result.json").read_text(encoding="utf-8")) == {
        "source": "approved-snapshot"
    }
    assert (job_root / "entrypoint.py").read_text(encoding="utf-8") == approved_script


@REQUIRES_HOST_COMPUTE_CONTAINMENT
def test_parent_worker_handoff_cannot_overwrite_fast_terminal_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _config = _prepare_workspace(tmp_path, script_body=SUCCESS_SCRIPT)
    manager = ComputeJobManager(workspace, tmp_path / "runtime")
    atomic_write = compute_jobs._atomic_write_json
    delayed_running_write = False

    def delay_parent_running_commit(path: Path, payload: dict[str, object]) -> None:
        nonlocal delayed_running_write
        if (
            path.name == "metadata.json"
            and payload.get("state") == "running"
            and int(payload.get("pid") or 0) > 0
            and not delayed_running_write
        ):
            delayed_running_write = True
            time.sleep(0.35)
        atomic_write(path, payload)

    monkeypatch.setattr(compute_jobs, "_atomic_write_json", delay_parent_running_commit)

    started = manager.start(
        entrypoint_id="optimizer",
        config_path="optimizer.json",
        idempotency_key="handoff-fast-worker-001",
    )
    finished = _wait_for_terminal(manager, str(started["job_id"]))
    metadata = manager._load_metadata(str(started["job_id"]))

    if finished["state"] != "succeeded":
        _print_synthetic_compute_failure(manager, finished)
    assert delayed_running_write
    assert finished["state"] == "succeeded"
    assert metadata["state"] == "succeeded"
    assert int(metadata["revision"]) >= 4
    assert metadata["ended_at"]


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
        "containment_kind": compute_jobs._platform_compute_containment_kind(),
        "started_at": "2026-09-01T00:00:00+00:00",
        "updated_at": "2026-09-01T00:00:00+00:00",
    }
    compute_jobs._atomic_write_json(job_root / "metadata.json", metadata)
    monkeypatch.setattr(compute_jobs, "_process_identity", lambda pid: "birth-a" if pid == 123 else "")
    manager.reconcile()
    assert manager._load_metadata("a" * 32)["state"] == "running"
    monkeypatch.setattr(compute_jobs, "_process_identity", lambda _pid: "")
    absence_probes = []
    marker_scans = []

    def absent_worker(pid, signal):
        assert pid == metadata["pid"] and signal == 0
        absence_probes.append(pid)
        raise ProcessLookupError("Synthetic worker has exited")

    def absent_windows_worker(pid):
        assert pid == metadata["pid"]
        absence_probes.append(pid)
        return False

    def no_job_descendants(job_id, **_options):
        assert job_id == metadata["job_id"]
        marker_scans.append(job_id)
        return {}

    monkeypatch.setattr(compute_jobs.os, "kill", absent_worker)
    monkeypatch.setattr(compute_jobs, "_windows_worker_may_be_alive", absent_windows_worker)
    monkeypatch.setattr(compute_jobs, "_scan_posix_job_marker_processes", no_job_descendants)
    manager.reconcile()
    assert manager._load_metadata("a" * 32)["state"] == "interrupted"
    assert absence_probes and set(absence_probes) == {metadata["pid"]}
    if os.name == "posix":
        assert marker_scans and set(marker_scans) == {metadata["job_id"]}


def test_stale_starting_job_without_worker_identity_remains_fail_closed(
    tmp_path: Path,
) -> None:
    workspace, _config = _prepare_workspace(tmp_path, script_body=SUCCESS_SCRIPT)
    manager = ComputeJobManager(workspace, tmp_path / "runtime")
    job_id = "9" * 32
    job_root = manager.jobs_root / job_id
    job_root.mkdir()
    compute_jobs._atomic_write_json(
        job_root / "metadata.json",
        {
            "schema_version": 1,
            "revision": 1,
            "job_id": job_id,
            "workspace": str(workspace),
            "state": "starting",
            "pid": 0,
            "process_identity": "",
            "updated_at": "2026-01-01T00:00:00+00:00",
        },
    )

    with pytest.raises(ComputeJobError, match="launch ownership is unresolved"):
        manager.reconcile()

    assert manager._load_metadata(job_id)["state"] == "starting"


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
            "containment_kind": compute_jobs._platform_compute_containment_kind(),
            "updated_at": "2026-01-01T00:00:00+00:00",
            "ended_at": "",
        },
    )
    monkeypatch.setattr(compute_jobs, "_process_identity", lambda _pid: "")

    records = scan_compute_job_metadata(runtime, reconcile_liveness=True)

    assert records[0]["state"] == "interrupted"
    assert manager._load_metadata(job_id)["state"] == "interrupted"


@pytest.mark.skipif(os.name != "posix", reason="POSIX job-marker scans are required")
def test_stale_reconciliation_blocks_while_job_marker_descendant_survives(
    tmp_path: Path,
) -> None:
    workspace, _config = _prepare_workspace(tmp_path, script_body=SUCCESS_SCRIPT)
    runtime = tmp_path / "runtime"
    manager = ComputeJobManager(workspace, runtime)
    workspace_metadata = workspace.stat()
    job_id = "8" * 32
    job_root = manager.jobs_root / job_id
    job_root.mkdir()
    compute_jobs._atomic_write_json(
        job_root / "metadata.json",
        {
            "schema_version": 1,
            "revision": 1,
            "job_id": job_id,
            "workspace": str(workspace),
            "workspace_device": int(workspace_metadata.st_dev),
            "workspace_inode": int(workspace_metadata.st_ino),
            "state": "running",
            "pid": 987_654,
            "process_identity": "missing-worker-birth",
            "containment_kind": compute_jobs._platform_compute_containment_kind(),
            "updated_at": "2026-01-01T00:00:00+00:00",
            "ended_at": "",
        },
    )
    environment = dict(os.environ)
    environment["AGENTIC_CONTEXT_COMPUTE_JOB"] = job_id
    descendant = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
    descendant_identity = compute_jobs._wait_for_process_identity(descendant.pid)
    assert descendant_identity
    try:
        with pytest.raises(ComputeJobError, match="job-marker descendants"):
            scan_compute_job_metadata(runtime, reconcile_liveness=True)
        assert manager._load_metadata(job_id)["state"] == "running"
    finally:
        if compute_jobs._process_identity(descendant.pid) == descendant_identity:
            os.kill(descendant.pid, compute_jobs.signal.SIGKILL)
        descendant.wait(timeout=2.0)

    records = scan_compute_job_metadata(runtime, reconcile_liveness=True)
    assert records[0]["state"] == "interrupted"


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
        "containment_kind": compute_jobs._platform_compute_containment_kind(),
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


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS compute workers cannot fork")
@REQUIRES_HOST_COMPUTE_CONTAINMENT
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


@pytest.mark.skipif(
    not LINUX_CGROUP_EXECUTION_AVAILABLE,
    reason="requires a Linux host with writable, verifiable cgroup v2 containment",
)
def test_worker_clears_inherited_stdout_descendants_before_terminal(
    tmp_path: Path,
) -> None:
    script = """
import argparse
from pathlib import Path
import subprocess
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--config", required=True)
parser.add_argument("--job-runtime", required=True)
parser.add_argument("--resume")
args = parser.parse_args()
descendant = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
)
(Path(args.job_runtime) / "descendant.pid").write_text(
    str(descendant.pid),
    encoding="utf-8",
)
"""
    workspace, _config = _prepare_workspace(tmp_path, script_body=script)
    manager = ComputeJobManager(workspace, tmp_path / "runtime")
    started = manager.start(
        entrypoint_id="optimizer",
        config_path="optimizer.json",
        idempotency_key="descendant-containment-001",
    )
    job_root = manager.jobs_root / str(started["job_id"])
    descendant_path = job_root / "descendant.pid"
    deadline = time.monotonic() + 5.0
    while not descendant_path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert descendant_path.exists()
    descendant_pid = int(descendant_path.read_text(encoding="utf-8"))
    descendant_identity = compute_jobs._process_identity(descendant_pid)
    try:
        finished = _wait_for_terminal(manager, str(started["job_id"]))

        assert finished["state"] == "failed"
        assert "descendant processes" in str(finished["message"])
        assert not compute_jobs._process_identity(descendant_pid)
        metadata = manager._load_metadata(str(started["job_id"]))
        assert metadata["containment_cleared_at"]
    finally:
        if (
            descendant_identity
            and compute_jobs._process_identity(descendant_pid) == descendant_identity
        ):
            os.kill(descendant_pid, compute_jobs.signal.SIGKILL)


@pytest.mark.skipif(
    not LINUX_CGROUP_EXECUTION_AVAILABLE,
    reason="requires a Linux host with writable, verifiable cgroup v2 containment",
)
def test_worker_clears_detached_scrubbed_environment_descendant_before_terminal(
    tmp_path: Path,
) -> None:
    script = """
import argparse
from pathlib import Path
import subprocess
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--config", required=True)
parser.add_argument("--job-runtime", required=True)
parser.add_argument("--resume")
args = parser.parse_args()
descendant = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    env={"PATH": "/usr/bin:/bin"},
    start_new_session=True,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    close_fds=True,
)
(Path(args.job_runtime) / "detached-descendant.pid").write_text(
    str(descendant.pid),
    encoding="utf-8",
)
"""
    workspace, _config = _prepare_workspace(tmp_path, script_body=script)
    manager = ComputeJobManager(workspace, tmp_path / "runtime")
    started = manager.start(
        entrypoint_id="optimizer",
        config_path="optimizer.json",
        idempotency_key="detached-descendant-containment-001",
    )
    job_id = str(started["job_id"])
    job_root = manager.jobs_root / job_id
    descendant_path = job_root / "detached-descendant.pid"
    deadline = time.monotonic() + 5.0
    while not descendant_path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert descendant_path.exists()
    descendant_pid = int(descendant_path.read_text(encoding="utf-8"))
    descendant_identity = compute_jobs._process_identity(descendant_pid)
    assert descendant_identity
    try:
        finished = _wait_for_terminal(manager, job_id)

        assert finished["state"] == "failed"
        assert "descendant processes" in str(finished["message"])
        assert not compute_jobs._process_identity(descendant_pid)
        metadata = manager._load_metadata(job_id)
        assert metadata["containment_cleared_at"]
        assert compute_jobs._scan_posix_job_marker_processes(job_id) == {}
    finally:
        if compute_jobs._process_identity(descendant_pid) == descendant_identity:
            os.kill(descendant_pid, compute_jobs.signal.SIGKILL)


@pytest.mark.skipif(
    sys.platform != "darwin" or not compute_jobs.MACOS_SANDBOX_EXECUTABLE.is_file(),
    reason="macOS sandbox-exec is required for the real fork-denial probe",
)
def test_macos_worker_denies_scrubbed_environment_process_escape(tmp_path: Path) -> None:
    script = """
import argparse
import json
from pathlib import Path
import subprocess
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--config", required=True)
parser.add_argument("--job-runtime", required=True)
parser.add_argument("--resume")
args = parser.parse_args()
runtime = Path(args.job_runtime)
try:
    descendant = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env={"PATH": "/usr/bin:/bin"},
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
except OSError:
    (runtime / "result.json").write_text(
        json.dumps({"process_fork": "denied"}),
        encoding="utf-8",
    )
else:
    (runtime / "escaped.pid").write_text(str(descendant.pid), encoding="utf-8")
    (runtime / "result.json").write_text(
        json.dumps({"process_fork": "allowed"}),
        encoding="utf-8",
    )
"""
    workspace, _config = _prepare_workspace(tmp_path, script_body=script)
    manager = ComputeJobManager(workspace, tmp_path / "runtime")
    started = manager.start(
        entrypoint_id="optimizer",
        config_path="optimizer.json",
        idempotency_key="macos-scrubbed-environment-escape-001",
    )
    job_id = str(started["job_id"])
    job_root = manager.jobs_root / job_id
    escaped_pid = 0
    escaped_identity = ""
    try:
        finished = _wait_for_terminal(manager, job_id)
        escaped_path = job_root / "escaped.pid"
        if escaped_path.exists():
            escaped_pid = int(escaped_path.read_text(encoding="utf-8"))
            escaped_identity = compute_jobs._process_identity(escaped_pid)

        assert finished["state"] == "succeeded"
        result = json.loads((job_root / "result.json").read_text(encoding="utf-8"))
        assert result == {"process_fork": "denied"}
        assert not escaped_path.exists()
        assert manager._load_metadata(job_id)["containment_cleared_at"]
    finally:
        if escaped_identity and compute_jobs._process_identity(escaped_pid) == escaped_identity:
            os.kill(escaped_pid, compute_jobs.signal.SIGKILL)


@REQUIRES_HOST_COMPUTE_CONTAINMENT
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


def test_concurrent_same_idempotency_key_publishes_one_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _config = _prepare_workspace(tmp_path, script_body=SUCCESS_SCRIPT)
    runtime = tmp_path / "runtime"
    missing_caffeinate = tmp_path / "missing-caffeinate"
    managers = (
        ComputeJobManager(
            workspace,
            runtime,
            caffeinate_executable=missing_caffeinate,
        ),
        ComputeJobManager(
            workspace,
            runtime,
            caffeinate_executable=missing_caffeinate,
        ),
    )
    popen_calls, staged_metadata_paths = _install_concurrent_start_harness(
        monkeypatch,
        managers[0],
    )

    def start(current: ComputeJobManager) -> dict[str, object]:
        return current.start(
            entrypoint_id="optimizer",
            config_path="optimizer.json",
            idempotency_key="parallel-same-key-001",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(start, managers))

    assert {str(result["job_id"]) for result in results} == {
        str(results[0]["job_id"])
    }
    assert len(popen_calls) == 1
    assert len(staged_metadata_paths) == 1
    assert managers[0].workspace_lock_path.is_file()
    assert len([path for path in managers[0].jobs_root.iterdir() if path.is_dir()]) == 1


def test_concurrent_different_idempotency_keys_publish_only_one_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _config = _prepare_workspace(tmp_path, script_body=SUCCESS_SCRIPT)
    runtime = tmp_path / "runtime"
    missing_caffeinate = tmp_path / "missing-caffeinate"
    managers = (
        ComputeJobManager(
            workspace,
            runtime,
            caffeinate_executable=missing_caffeinate,
        ),
        ComputeJobManager(
            workspace,
            runtime,
            caffeinate_executable=missing_caffeinate,
        ),
    )
    popen_calls, staged_metadata_paths = _install_concurrent_start_harness(
        monkeypatch,
        managers[0],
    )

    def start(request: tuple[ComputeJobManager, str]) -> tuple[str, object]:
        current, idempotency_key = request
        try:
            result = current.start(
                entrypoint_id="optimizer",
                config_path="optimizer.json",
                idempotency_key=idempotency_key,
            )
        except ComputeJobError as exc:
            return "error", str(exc)
        return "started", result

    requests = (
        (managers[0], "parallel-distinct-key-001"),
        (managers[1], "parallel-distinct-key-002"),
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(start, requests))

    assert [kind for kind, _value in outcomes].count("started") == 1
    errors = [str(value) for kind, value in outcomes if kind == "error"]
    assert len(errors) == 1
    assert "concurrency is limited to 1" in errors[0]
    assert len(popen_calls) == 1
    assert len(staged_metadata_paths) == 1
    assert managers[0].workspace_lock_path.is_file()
    assert len([path for path in managers[0].jobs_root.iterdir() if path.is_dir()]) == 1


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


@REQUIRES_HOST_COMPUTE_CONTAINMENT
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
    if not log_path.is_file():
        _print_synthetic_compute_failure(manager, status)
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


def test_approved_file_read_preserves_crlf_bytes_in_windows_binary_mode(tmp_path, monkeypatch):
    payload = b"print('approved')\r\n# Exact signed bytes\r\n"
    path = tmp_path / "approved.py"
    path.write_bytes(payload)
    native_binary = getattr(os, "O_BINARY", 0)
    binary = native_binary or 1 << 28
    opened = {}
    native_open, native_read = os.open, os.read

    def windows_open(candidate, flags, *args, **kwargs):
        native_flags = flags if native_binary else flags & ~binary
        descriptor = native_open(candidate, native_flags, *args, **kwargs)
        opened[descriptor] = bool(flags & binary)
        return descriptor

    def windows_read(descriptor, size):
        content = native_read(descriptor, size)
        return content if opened.get(descriptor) else content.replace(b"\r\n", b"\n")

    with monkeypatch.context() as patch:
        patch.setattr(os, "O_BINARY", binary, raising=False)
        patch.setattr(os, "open", windows_open)
        patch.setattr(os, "read", windows_read)
        result, digest = compute_jobs._stable_regular_file_bytes(
            path, maximum_bytes=4096, label="Approved entrypoint",
        )

    assert result == payload
    assert digest == hashlib.sha256(payload).hexdigest()
    assert path.read_bytes() == payload
    assert opened and all(opened.values())


@pytest.fixture
def windows_approved_file(tmp_path, monkeypatch):
    path = tmp_path / "approved.py"
    payload = b"print('approved')\r\n# Preserve exact signed bytes\r\n"
    path.write_bytes(payload)
    lstat = Path.lstat
    fstat = os.fstat
    path_reads = []
    handle_reads = []
    native_reads = []

    def install(*, tokens=(77, 77, 77, 77), changed_field="", changed_api=""):
        def metadata(original, *, api, index):
            fields = {field: getattr(original, field) for field in (
                "st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns",
            )}
            # Windows exposes different ctime meanings for path and descriptor stats.
            fields["st_ctime_ns"] = 900 if api == "path" else 300
            fields["st_birthtime_ns"] = 300
            if changed_api == api and index == 1:
                fields[changed_field] += 1
            return SimpleNamespace(**fields)

        def path_metadata(candidate):
            original = lstat(candidate)
            if candidate != path:
                return original
            index = len(path_reads)
            path_reads.append(candidate)
            return metadata(original, api="path", index=index)

        def handle_metadata(descriptor):
            index = len(handle_reads)
            handle_reads.append(descriptor)
            return metadata(fstat(descriptor), api="handle", index=index)

        def native_change_time(candidate, *, descriptor=None):
            assert candidate == path
            index = len(native_reads)
            native_reads.append(descriptor)
            return tokens[index]

        windows_os = SimpleNamespace(**{**vars(os), "name": "nt", "fstat": handle_metadata})
        monkeypatch.setattr(compute_jobs, "os", windows_os)
        monkeypatch.setattr(Path, "lstat", path_metadata)
        monkeypatch.setattr(compute_jobs, "file_change_time", native_change_time, raising=False)
        return path, payload, native_reads

    return install


def test_windows_approved_read_compares_one_native_change_time_across_stat_apis(windows_approved_file):
    path, payload, native_reads = windows_approved_file()

    result, digest = compute_jobs._stable_regular_file_bytes(path, maximum_bytes=4096, label="Approved entrypoint")

    assert result == payload
    assert digest == hashlib.sha256(payload).hexdigest()
    assert path.read_bytes() == payload
    assert len(native_reads) == 4 and native_reads[0] is None and native_reads[-1] is None
    assert native_reads[1] == native_reads[2] and isinstance(native_reads[1], int)


@pytest.mark.parametrize("tokens", [(77, 77, 78, 77), (77, 77, 77, 78)])
def test_windows_approved_read_refuses_native_handle_or_path_change(windows_approved_file, tokens):
    path, payload, _native_reads = windows_approved_file(tokens=tokens)

    with pytest.raises(ComputeJobError, match="changed while it was being read"):
        compute_jobs._stable_regular_file_bytes(path, maximum_bytes=4096, label="Approved entrypoint")

    assert path.read_bytes() == payload


@pytest.mark.parametrize("changed_api", ["path", "handle"])
@pytest.mark.parametrize("changed_field", ["st_ctime_ns", "st_birthtime_ns"])
def test_windows_approved_read_retains_each_apis_change_and_birth_guard(
    windows_approved_file, changed_api, changed_field,
):
    path, payload, _native_reads = windows_approved_file(changed_api=changed_api, changed_field=changed_field)

    with pytest.raises(ComputeJobError, match="changed while it was being read"):
        compute_jobs._stable_regular_file_bytes(path, maximum_bytes=4096, label="Approved entrypoint")

    assert path.read_bytes() == payload
