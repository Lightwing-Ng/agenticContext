"""Durable Secure MCP Tunnel background-check store tests.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import pytest

from app.core import tunnel_checks
from app.core.tunnel_checks import CheckJobError, TunnelCheckStore


def wait_for_terminal(
    store: TunnelCheckStore,
    job_dir: Path,
    metadata: dict,
    *,
    timeout_seconds: float = 15.0,
) -> dict:
    """Wait for one disposable runner without hiding its persisted state."""
    deadline = time.monotonic() + timeout_seconds
    latest: dict = {}
    while time.monotonic() < deadline:
        latest = store.status(job_dir, metadata)
        if latest["state"] not in {"starting", "running"}:
            return latest
        time.sleep(0.05)
    pytest.fail(f"Check did not finish: {latest}")


def start_python_check(
    store: TunnelCheckStore,
    project: Path,
    *,
    source: str,
    key: str,
) -> tuple[Path, dict]:
    """Launch a direct store-level check with deterministic evidence."""
    job_dir, metadata, deduplicated = store.start(
        project_id="main",
        project_root=project,
        argv=[sys.executable, "-c", source],
        command="python -c <test>",
        idempotency_key=key,
        timeout_seconds=60,
        evidence={"snapshot_id": key, "binding_epoch": 1},
    )
    assert deduplicated is False
    return job_dir, metadata


def test_completed_check_survives_store_restart_with_bounded_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(tunnel_checks, "MAX_OUTPUT_BYTES", 4_096)
    first = TunnelCheckStore(runtime)
    job_dir, metadata = start_python_check(
        first,
        project,
        source="print('x' * 20000)",
        key="bounded-output-0001",
    )

    completed = wait_for_terminal(first, job_dir, metadata)
    restarted = TunnelCheckStore(runtime)
    found_dir, found_metadata = restarted.find("main", project, metadata["job_id"])
    recovered = restarted.status(found_dir, found_metadata)

    assert completed["state"] == "succeeded"
    assert completed["output_truncated"] is True
    assert recovered["state"] == "succeeded"
    assert recovered["output_truncated"] is True
    assert len(recovered["output_tail"]) <= tunnel_checks.OUTPUT_TAIL_CHARACTERS
    assert (job_dir / "output.log").stat().st_size <= 4_096


@pytest.mark.skipif(os.name != "posix", reason="POSIX runner-loss recovery contract")
def test_runner_loss_is_unknown_after_restart_and_orphan_can_be_stopped(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    runtime = tmp_path / "runtime"
    first = TunnelCheckStore(runtime)
    job_dir, metadata = start_python_check(
        first,
        project,
        source="import time; time.sleep(30)",
        key="runner-loss-0001",
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not (job_dir / "child.json").is_file():
        time.sleep(0.05)
    assert (job_dir / "child.json").is_file()

    os.kill(int(metadata["pid"]), signal.SIGKILL)
    restarted = TunnelCheckStore(runtime)
    try:
        deadline = time.monotonic() + 10
        observed: dict = {}
        while time.monotonic() < deadline:
            found_dir, found_metadata = restarted.find(
                "main", project, metadata["job_id"]
            )
            observed = restarted.status(found_dir, found_metadata)
            if observed["state"] == "unknown":
                break
            time.sleep(0.05)

        assert observed["state"] == "unknown"
        assert observed["command_process_active"] is True
        assert "outcome cannot be proven" in observed["message"]
        stopped = restarted.stop(found_dir, found_metadata)
        assert stopped["state"] == "stopped"
    finally:
        if "found_dir" in locals():
            restarted.stop(found_dir, found_metadata)


def test_terminal_check_retention_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = TunnelCheckStore(tmp_path / "runtime")
    monkeypatch.setattr(tunnel_checks, "RETAINED_JOBS_PER_PROJECT", 2)
    job_ids: list[str] = []

    for index in range(3):
        job_dir, metadata = start_python_check(
            store,
            project,
            source="print('ok')",
            key=f"retained-check-{index:04d}",
        )
        assert wait_for_terminal(store, job_dir, metadata)["state"] == "succeeded"
        job_ids.append(metadata["job_id"])

    project_dir = store._project_dir("main", project)
    retained = sorted(path.name for path in project_dir.iterdir() if path.is_dir())
    assert len(retained) == 2
    assert job_ids[0] not in retained
    with pytest.raises(CheckJobError, match="Unknown check job"):
        store.find("main", project, job_ids[0])

