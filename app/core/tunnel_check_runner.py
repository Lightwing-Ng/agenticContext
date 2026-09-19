"""Detached runner for one durable Secure MCP Tunnel verification job.

Code version: v1.0.0-claude.0

``TunnelCheckStore`` launches this file directly with ``python -I`` so the runner
imports nothing from the application and cannot change the checked project's
import path. The runner owns exactly one approved argv that the store already
validated: it starts that argv in a new process group, streams its output into a
size-capped log, enforces the timeout, stops the whole group when asked (SIGTERM,
or CTRL_BREAK on Windows), and records one terminal ``result.json``. It also records
the command's pid and birth identity in ``child.json`` so the store can still stop
exactly that process group if the runner itself is killed. No shell is involved at
any point.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

POLL_SECONDS = 0.1
TERMINATE_GRACE_SECONDS = 3.0
READ_CHUNK_BYTES = 64 * 1024


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class _BoundedLog:
    """Append output while keeping only the newest ``limit`` bytes on disk."""

    def __init__(self, path: Path, limit: int) -> None:
        self.path = path
        self.limit = limit
        self.truncated = False
        path.write_bytes(b"")

    def append(self, data: bytes) -> None:
        with self.path.open("ab") as handle:
            handle.write(data)
        size = self.path.stat().st_size
        if size <= self.limit:
            return
        self.truncated = True
        with self.path.open("rb") as handle:
            handle.seek(size - self.limit // 2)
            retained = handle.read()
        with self.path.open("wb") as handle:
            handle.write(retained)


def _birth_identity(pid: int) -> str:
    """Mirror ``compute_jobs._process_identity`` on POSIX without importing the app."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        if len(fields) > 21:
            return f"proc:{fields[21]}"
    except (OSError, UnicodeError):
        pass
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
            check=False,
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        return ""
    return f"ps:{pid}:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stop_tree(child: subprocess.Popen[bytes]) -> None:
    """Stop the child and every process in its group."""
    if child.poll() is not None and os.name != "posix":
        return
    if os.name == "posix":
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(child.pid, sig)
            except (ProcessLookupError, PermissionError):
                # macOS reports EPERM for a group whose remaining members are zombies.
                return
            deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
            while time.monotonic() < deadline:
                child.poll()
                try:
                    os.killpg(child.pid, 0)
                except (ProcessLookupError, PermissionError):
                    return
                time.sleep(POLL_SECONDS)
        return
    system_root = os.environ.get("SystemRoot", "")
    taskkill = Path(system_root) / "System32" / "taskkill.exe" if system_root else None
    if taskkill is not None and taskkill.is_file():
        subprocess.run(
            [str(taskkill), "/PID", str(child.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    if child.poll() is None:
        child.kill()


def _supervise(
    argv: list[str],
    spec: dict[str, Any],
    log: _BoundedLog,
    stop_requested: threading.Event,
    group_options: dict[str, Any],
    timeout_seconds: float,
) -> tuple[str, int | None]:
    """Run the approved argv to completion, stop, or timeout; return (outcome, exit code)."""
    try:
        child = subprocess.Popen(
            argv,
            cwd=spec["cwd"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            **group_options,
        )
    except OSError as exc:
        log.append(f"The approved command could not start: {exc.strerror or exc}\n".encode())
        return "failed", None
    if os.name == "posix":
        _atomic_json(
            Path(spec["job_dir"]) / "child.json",
            {"pid": child.pid, "identity": _birth_identity(child.pid)},
        )
    assert child.stdout is not None
    stream = child.stdout

    def pump() -> None:
        while chunk := stream.read1(READ_CHUNK_BYTES):
            log.append(chunk)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout_seconds
    stopped_as = ""
    while child.poll() is None:
        if stop_requested.is_set():
            stopped_as = "stopped"
        elif time.monotonic() > deadline:
            stopped_as = "timeout"
        if stopped_as:
            _stop_tree(child)
            break
        time.sleep(POLL_SECONDS)
    exit_code = child.wait()
    if os.name == "posix":
        # Descendants that outlived the command still belong to its group.
        _stop_tree(child)
    reader.join(timeout=5)
    return stopped_as or ("succeeded" if exit_code == 0 else "failed"), exit_code


def run(job_dir: Path) -> int:
    spec = json.loads((job_dir / "command.json").read_text(encoding="utf-8"))
    spec["job_dir"] = str(job_dir)
    argv = [str(part) for part in spec["argv"]]
    timeout_seconds = float(spec["timeout_seconds"])
    log = _BoundedLog(job_dir / "output.log", int(spec["max_output_bytes"]))
    stop_requested = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_requested.set()

    signal.signal(signal.SIGTERM, request_stop)
    if os.name == "nt" and hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, request_stop)
    group_options: dict[str, Any] = (
        {"start_new_session": True}
        if os.name == "posix"
        else {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    )
    started = time.monotonic()
    outcome = "failed"
    exit_code: int | None = None
    try:
        outcome, exit_code = _supervise(argv, spec, log, stop_requested, group_options, timeout_seconds)
    except BaseException as exc:  # Always record a truthful terminal result below.
        outcome = "unknown"
        try:
            log.append(f"The check runner failed: {type(exc).__name__}\n".encode())
        except OSError:
            pass
    _atomic_json(
        job_dir / "result.json",
        {
            "outcome": outcome,
            "exit_code": exit_code,
            "duration_seconds": round(time.monotonic() - started, 2),
            "finished_at": time.time(),
            "output_truncated": log.truncated,
        },
    )
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: tunnel_check_runner.py <job-directory>")
    raise SystemExit(run(Path(sys.argv[1])))
