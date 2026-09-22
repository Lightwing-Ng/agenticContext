"""Durable, identity-checked verification jobs for the Secure MCP Tunnel.

Code version: v1.3.0-codex.0

A check job runs one command that the controller's approved verification policy
already accepted (the same policy ``run_check`` uses) through the stdlib-only
``tunnel_check_runner``. Jobs live under the Agent runtime root, keyed by the exact
project identity and root, so they survive a slow MCP request and a service
restart. Starting is deduplicated by an idempotency key, an identical running
check is shared instead of started twice, and one check runs per project at a
time; stopping signals only the runner whose recorded birth identity still
matches.

This deliberately does not reuse ``ComputeJobManager`` for execution: that manager
admits only SHA-pinned Python optimizer entrypoints from a workspace manifest,
requires a 12-hour minimum runtime, and on macOS confines the child with a
no-fork sandbox that test runners cannot work under. Its durable-file, lock, and
process-identity primitives are reused here instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any


def _jobs() -> Any:
    """Return the compute-job primitives module.

    It is imported on first use because ``app.core.agent`` imports the Tunnel
    service, which imports this module.
    """
    from app.core.agent import compute_jobs

    return compute_jobs


CHECKS_DIRNAME = "tunnel-checks"
ACTIVE_STATES = frozenset({"starting", "running"})
TERMINAL_STATES = frozenset({"succeeded", "failed", "stopped", "timeout", "unknown"})
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
OUTPUT_TAIL_CHARACTERS = 6_000
RETAINED_JOBS_PER_PROJECT = 20
STARTING_GRACE_SECONDS = 60.0
STOP_WAIT_SECONDS = 15.0
MIN_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 2 * 60 * 60
DEFAULT_TIMEOUT_SECONDS = 30 * 60
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"
_IDEMPOTENCY_KEY_RE = re.compile(IDEMPOTENCY_KEY_PATTERN)
_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
# Credentials the service may hold that a verification command never needs.
_WITHHELD_ENVIRONMENT = frozenset(
    {"OPENAI_API_KEY", "OPENAI_ADMIN_KEY", "CONTROL_PLANE_API_KEY", "ANTHROPIC_API_KEY"}
)
RUNNER_PATH = Path(__file__).with_name("tunnel_check_runner.py")


class CheckJobError(ValueError):
    """Raised with an actionable explanation for the model."""


def _iso(timestamp: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


class TunnelCheckStore:
    """Start, observe, and stop durable checks for registered projects."""

    def __init__(self, runtime_root: Path) -> None:
        self.root = Path(runtime_root) / CHECKS_DIRNAME

    # Layout -----------------------------------------------------------------

    @staticmethod
    def project_key(project_id: str, project_root: Path) -> str:
        return hashlib.sha256(f"{project_id}\0{project_root}".encode("utf-8")).hexdigest()[:24]

    def _project_dir(self, project_id: str, project_root: Path) -> Path:
        return self.root / self.project_key(project_id, project_root)

    def _job_dir(self, project_id: str, project_root: Path, job_id: str) -> Path:
        if not isinstance(job_id, str) or not _JOB_ID_RE.fullmatch(job_id):
            raise CheckJobError("job_id must be the 32-character id returned by start_check.")
        job_dir = self._project_dir(project_id, project_root) / job_id
        if not (job_dir / "metadata.json").is_file():
            # Jobs are looked up only inside the named project's bucket, so an id from
            # another project is simply unknown here.
            raise CheckJobError(f"Unknown check job {job_id} for project {project_id}.")
        return job_dir

    def _load(self, job_dir: Path) -> dict[str, Any]:
        try:
            return _jobs()._read_json_object(job_dir / "metadata.json", maximum_bytes=64 * 1024)
        except _jobs().ComputeJobError as exc:
            raise CheckJobError("The check job record is unreadable.") from exc

    def _records(self, project_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
        records = []
        if not project_dir.is_dir():
            return records
        for job_dir in sorted(project_dir.iterdir()):
            if not _JOB_ID_RE.fullmatch(job_dir.name) or not job_dir.is_dir():
                continue
            try:
                records.append((job_dir, self._load(job_dir)))
            except CheckJobError:
                continue
        records.sort(key=lambda item: float(item[1].get("created_at") or 0))
        return records

    # State ------------------------------------------------------------------

    @staticmethod
    def _result(job_dir: Path) -> dict[str, Any] | None:
        try:
            result = _jobs()._read_json_object(job_dir / "result.json", maximum_bytes=16 * 1024)
        except _jobs().ComputeJobError:
            return None
        return result if result.get("outcome") in TERMINAL_STATES else None

    def _state(self, job_dir: Path, metadata: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        result = self._result(job_dir)
        if result is not None:
            return str(result["outcome"]), result
        pid = int(metadata.get("pid") or 0)
        if pid <= 0:
            age = time.time() - float(metadata.get("created_at") or 0)
            return ("starting" if age < STARTING_GRACE_SECONDS else "unknown"), None
        if _jobs()._identity_matches(pid, metadata.get("process_identity")):
            return "running", None
        # The runner may have finished between the two reads.
        result = self._result(job_dir)
        if result is not None:
            return str(result["outcome"]), result
        return "unknown", None

    @staticmethod
    def _orphaned_command_active(job_dir: Path) -> bool:
        """Return whether a runner-less POSIX command still has its birth identity."""
        if os.name != "posix":
            return False
        try:
            child = _jobs()._read_json_object(
                job_dir / "child.json",
                maximum_bytes=4 * 1024,
            )
        except _jobs().ComputeJobError:
            return False
        return _jobs()._identity_matches(child.get("pid"), child.get("identity"))

    def status(self, job_dir: Path, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return a bounded model-facing status with an output tail."""
        metadata = metadata if metadata is not None else self._load(job_dir)
        state, result = self._state(job_dir, metadata)
        created = float(metadata.get("created_at") or 0)
        status: dict[str, Any] = {
            "job_id": metadata.get("job_id"),
            "state": state,
            "command": metadata.get("command"),
            "started_at": _iso(created),
        }
        if result is not None:
            status["exit_code"] = result.get("exit_code")
            status["duration_seconds"] = result.get("duration_seconds")
            if result.get("output_truncated"):
                status["output_truncated"] = True
        else:
            status["elapsed_seconds"] = round(max(0.0, time.time() - created), 1)
        if state == "unknown":
            orphaned = self._orphaned_command_active(job_dir)
            status["command_process_active"] = orphaned
            status["message"] = (
                "The check runner is gone and recorded no result, so its outcome cannot be "
                "proven. Treat it as unverified; "
                + (
                    "stop_check must end the still-running command before another check starts."
                    if orphaned
                    else "no identity-matching command is still running."
                )
            )
        elif state == "timeout":
            status["message"] = f"The check exceeded its {metadata.get('timeout_seconds')}-second limit."
        status["output_tail"] = self._tail(job_dir / "output.log")
        return status

    @staticmethod
    def _tail(path: Path) -> str:
        try:
            with path.open("rb") as handle:
                size = path.stat().st_size
                handle.seek(max(0, size - OUTPUT_TAIL_CHARACTERS * 4))
                return handle.read().decode("utf-8", errors="replace")[-OUTPUT_TAIL_CHARACTERS:]
        except OSError:
            return ""

    # Lifecycle --------------------------------------------------------------

    def start(
        self,
        *,
        project_id: str,
        project_root: Path,
        argv: list[str],
        command: str,
        idempotency_key: str,
        timeout_seconds: int,
        evidence: dict[str, Any],
    ) -> tuple[Path, dict[str, Any], bool]:
        """Start one approved check; return (job_dir, metadata, deduplicated)."""
        if not _IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key or ""):
            raise CheckJobError("idempotency_key must contain 8 to 128 safe characters.")
        if not MIN_TIMEOUT_SECONDS <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise CheckJobError("timeout_seconds is outside the allowed range.")
        project_dir = self._project_dir(project_id, project_root)
        project_dir.mkdir(parents=True, exist_ok=True)
        fingerprint = hashlib.sha256(
            json.dumps(
                {"project": project_id, "root": str(project_root), "argv": argv, "timeout": timeout_seconds},
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        lock_path = self.root / "locks" / f"{project_dir.name}.lock"
        try:
            with _jobs()._persistent_workspace_lock(lock_path):
                records = self._records(project_dir)
                for job_dir, metadata in records:
                    if metadata.get("idempotency_key") != idempotency_key:
                        continue
                    if metadata.get("request_fingerprint") != fingerprint:
                        raise CheckJobError(
                            "This idempotency_key was already used for a different check; "
                            "use a new key for a new check."
                        )
                    return job_dir, metadata, True
                for job_dir, metadata in records:
                    state = self._state(job_dir, metadata)[0]
                    if state in ACTIVE_STATES:
                        if metadata.get("request_fingerprint") == fingerprint:
                            # The same check is already running; share it.
                            return job_dir, metadata, True
                        raise CheckJobError(
                            f"Check {metadata.get('job_id')} is still running in this project. "
                            "Observe or stop it before starting another."
                        )
                    if state == "unknown" and self._orphaned_command_active(job_dir):
                        raise CheckJobError(
                            f"Check {metadata.get('job_id')} lost its runner while its command "
                            "is still active. Stop that job before starting another."
                        )
                self._prune(records)
                return self._launch(
                    project_dir=project_dir,
                    project_id=project_id,
                    project_root=project_root,
                    argv=argv,
                    command=command,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                    timeout_seconds=timeout_seconds,
                    evidence=evidence,
                )
        except _jobs().ComputeJobError as exc:
            raise CheckJobError(str(exc)) from exc

    def _launch(
        self,
        *,
        project_dir: Path,
        project_id: str,
        project_root: Path,
        argv: list[str],
        command: str,
        idempotency_key: str,
        fingerprint: str,
        timeout_seconds: int,
        evidence: dict[str, Any],
    ) -> tuple[Path, dict[str, Any], bool]:
        job_id = secrets.token_hex(16)
        job_dir = project_dir / job_id
        job_dir.mkdir(mode=0o700)
        _jobs()._atomic_write_json(
            job_dir / "command.json",
            {
                "argv": argv,
                "cwd": str(project_root),
                "timeout_seconds": timeout_seconds,
                "max_output_bytes": MAX_OUTPUT_BYTES,
            },
        )
        metadata: dict[str, Any] = {
            "job_id": job_id,
            "project_id": project_id,
            "command": command[:4_000],
            "idempotency_key": idempotency_key,
            "request_fingerprint": fingerprint,
            "timeout_seconds": timeout_seconds,
            "created_at": time.time(),
            "pid": 0,
            "process_identity": "",
            "evidence": evidence,
        }
        _jobs()._atomic_write_json(job_dir / "metadata.json", metadata)
        environment = {
            key: value for key, value in os.environ.items() if key not in _WITHHELD_ENVIRONMENT
        }
        group_options: dict[str, Any] = (
            {"start_new_session": True}
            if os.name == "posix"
            else {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
        )
        try:
            process = subprocess.Popen(
                [sys.executable, "-I", str(RUNNER_PATH), str(job_dir)],
                cwd=project_root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                **group_options,
            )
        except OSError as exc:
            _jobs()._atomic_write_json(
                job_dir / "result.json",
                {"outcome": "failed", "exit_code": None, "duration_seconds": 0, "finished_at": time.time()},
            )
            raise CheckJobError("The check runner could not start.") from exc
        # Reap the runner when it exits so a zombie never looks alive.
        threading.Thread(target=process.wait, name="tunnel-check-reaper", daemon=True).start()
        identity = ""
        for _ in range(100):
            identity = _jobs()._process_identity(process.pid)
            if identity or process.poll() is not None:
                break
            time.sleep(0.02)
        if not identity and self._result(job_dir) is None:
            if process.poll() is None:
                process.kill()
            _jobs()._atomic_write_json(
                job_dir / "result.json",
                {"outcome": "unknown", "exit_code": None, "duration_seconds": 0, "finished_at": time.time()},
            )
            raise CheckJobError("The check runner identity could not be verified; it was stopped.")
        metadata.update({"pid": process.pid, "process_identity": identity})
        _jobs()._atomic_write_json(job_dir / "metadata.json", metadata)
        return job_dir, metadata, False

    def _prune(self, records: list[tuple[Path, dict[str, Any]]]) -> None:
        """Keep the newest records; remove only terminal jobs this store created."""
        excess = len(records) - (RETAINED_JOBS_PER_PROJECT - 1)
        for job_dir, metadata in records:
            if excess <= 0:
                break
            state = self._state(job_dir, metadata)[0]
            if state in TERMINAL_STATES and not (
                state == "unknown" and self._orphaned_command_active(job_dir)
            ):
                shutil.rmtree(job_dir, ignore_errors=True)
                excess -= 1

    def find(self, project_id: str, project_root: Path, job_id: str) -> tuple[Path, dict[str, Any]]:
        job_dir = self._job_dir(project_id, project_root, job_id)
        metadata = self._load(job_dir)
        if metadata.get("project_id") != project_id or metadata.get("job_id") != job_id:
            raise CheckJobError(f"Unknown check job {job_id} for project {project_id}.")
        return job_dir, metadata

    def active_jobs(self, project_id: str, project_root: Path) -> list[str]:
        project_dir = self._project_dir(project_id, project_root)
        return [
            str(metadata.get("job_id"))
            for job_dir, metadata in self._records(project_dir)
            if (
                self._state(job_dir, metadata)[0] in ACTIVE_STATES
                or self._orphaned_command_active(job_dir)
            )
        ]

    def latest_evaluated_job(self, project_id: str, project_root: Path) -> str | None:
        """Return the newest job whose terminal result was reconciled."""
        project_dir = self._project_dir(project_id, project_root)
        for job_dir, metadata in reversed(self._records(project_dir)):
            if self.evaluation(job_dir) is not None:
                return str(metadata.get("job_id") or "") or None
        return None

    def stop(self, job_dir: Path, metadata: dict[str, Any]) -> dict[str, Any]:
        """Ask only the identity-verified runner to stop its own process group."""
        state, _ = self._state(job_dir, metadata)
        if state == "unknown":
            self._stop_orphaned_command(job_dir)
            return self.status(job_dir, metadata)
        if state not in ACTIVE_STATES:
            return self.status(job_dir, metadata)
        pid = int(metadata.get("pid") or 0)
        if pid <= 0 or not _jobs()._identity_matches(pid, metadata.get("process_identity")):
            return self.status(job_dir, metadata)
        # The launcher can return after recording the runner identity but just before
        # the runner installs its signal handler. Its output file is created only
        # after that handler is ready, so briefly wait for that marker before asking
        # it to stop.
        ready_deadline = time.monotonic() + 2.0
        while (
            time.monotonic() < ready_deadline
            and self._result(job_dir) is None
            and not (job_dir / "output.log").exists()
            and _jobs()._identity_matches(pid, metadata.get("process_identity"))
        ):
            time.sleep(0.02)
        try:
            if os.name == "nt":
                os.kill(pid, getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
            else:
                os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + STOP_WAIT_SECONDS
        runner_gone_at: float | None = None
        while time.monotonic() < deadline and self._result(job_dir) is None:
            if not _jobs()._identity_matches(pid, metadata.get("process_identity")):
                runner_gone_at = runner_gone_at or time.monotonic()
                if time.monotonic() - runner_gone_at >= 1.0:
                    break
            time.sleep(0.1)
        return self.status(job_dir, metadata)

    def _stop_orphaned_command(self, job_dir: Path) -> None:
        """End a command whose runner is gone, only while its birth identity matches."""
        if os.name != "posix" or self._result(job_dir) is not None:
            return
        try:
            child = _jobs()._read_json_object(job_dir / "child.json", maximum_bytes=4 * 1024)
        except _jobs().ComputeJobError:
            return
        pid = child.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return
        if not _jobs()._identity_matches(pid, child.get("identity")):
            return
        # The command leads its own session, so its group holds exactly its tree.
        if _jobs()._terminate_posix_process_group(pid, timeout=STOP_WAIT_SECONDS / 3):
            _jobs()._atomic_write_json(
                job_dir / "result.json",
                {"outcome": "stopped", "exit_code": None, "duration_seconds": None, "finished_at": time.time()},
            )

    # Evidence ---------------------------------------------------------------

    @staticmethod
    def evaluation(job_dir: Path) -> dict[str, Any] | None:
        try:
            return _jobs()._read_json_object(job_dir / "evidence.json", maximum_bytes=16 * 1024)
        except _jobs().ComputeJobError:
            return None

    @staticmethod
    def record_evaluation(job_dir: Path, evaluation: dict[str, Any]) -> None:
        _jobs()._atomic_write_json(job_dir / "evidence.json", evaluation)
