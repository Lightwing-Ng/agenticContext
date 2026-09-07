"""Startup publication races using synthetic processes and real isolated metadata.

Code version: v1.0.0-codex.1
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.agent import compute_jobs as jobs


@pytest.fixture
def startup(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    entrypoint = workspace / "optimizer.py"
    entrypoint.write_text("# Synthetic process tests never execute this file.\n", encoding="utf-8")
    (workspace / "optimizer.json").write_text("{}", encoding="utf-8")
    (workspace / jobs.APPROVAL_FILENAME).write_text(json.dumps({
        "schema_version": 1,
        "entrypoints": [{
            "id": "optimizer", "path": "optimizer.py",
            "sha256": hashlib.sha256(entrypoint.read_bytes()).hexdigest(),
            "max_runtime_seconds": jobs.DEFAULT_MAX_RUNTIME_SECONDS,
        }],
    }), encoding="utf-8")
    manager = jobs.ComputeJobManager(workspace, tmp_path / "runtime")
    state = SimpleNamespace(
        path=None, on_poll=None, on_cleanup=None, returncode=None,
        polls=0, cleanups=0, waits=0, sleeps=[], spawned=0,
    )

    def publish(terminal, exit_status):
        metadata = jobs._read_json_object(state.path, maximum_bytes=128 * 1_024)
        metadata.update({
            "state": terminal, "exit_status": exit_status,
            "ended_at": "2026-09-08T00:00:00+00:00",
            "message": f"Synthetic worker published {terminal}.",
        })
        jobs._atomic_write_json(state.path, metadata)

    def cleanup(*_args, **_kwargs):
        state.cleanups += 1
        if state.on_cleanup is not None:
            state.on_cleanup()
        state.returncode = 0

    class Process:
        pid = 424242

        def poll(self):
            state.polls += 1
            if state.on_poll is not None:
                state.on_poll()
            return state.returncode

        def terminate(self):
            cleanup()

        def wait(self, *, timeout):
            assert timeout == 5
            state.waits += 1
            return state.returncode

    def spawn(command, **_kwargs):
        state.spawned += 1
        assert command[3] == "--worker"
        state.path = Path(command[4])
        return Process()

    monkeypatch.setattr(jobs.subprocess, "Popen", spawn)
    monkeypatch.setattr(jobs, "_terminate_process_group", cleanup)
    monkeypatch.setattr(jobs, "_process_identity", lambda _pid: "synthetic-worker-identity")
    monkeypatch.setattr(jobs.time, "sleep", state.sleeps.append)
    return manager, state, publish


@pytest.mark.parametrize(
    "terminal, exit_status", (("succeeded", 0), ("failed", 19), ("stopped", 1), ("interrupted", 70))
)
def test_fast_supervisor_terminal_publication_is_not_overwritten_after_poll(startup, terminal, exit_status):
    manager, state, publish = startup

    def finish_between_metadata_read_and_poll():
        if state.polls == 1:
            publish(terminal, exit_status)
            state.returncode = exit_status

    state.on_poll = finish_between_metadata_read_and_poll
    started = manager.start(
        entrypoint_id="optimizer", config_path="optimizer.json", idempotency_key="fast-terminal-001",
    )
    assert started["state"] == terminal
    assert started["exit_status"] == exit_status
    assert started["message"] == f"Synthetic worker published {terminal}."
    assert started["active"] is False
    assert state.spawned == 1 and state.cleanups == 0 and state.sleeps == []
    recorded = jobs._read_json_object(state.path, maximum_bytes=128 * 1_024)
    assert recorded["state"] == terminal
    assert recorded["exit_status"] == exit_status


@pytest.mark.parametrize("terminal", ("succeeded", "failed", "stopped"))
def test_terminal_publication_during_confirmed_startup_cleanup_is_preserved(startup, terminal):
    manager, state, publish = startup
    state.on_cleanup = lambda: publish(terminal, 0 if terminal == "succeeded" else 1)
    started = manager.start(
        entrypoint_id="optimizer", config_path="optimizer.json", idempotency_key="cleanup-terminal-001",
    )
    assert started["state"] == terminal
    assert started["active"] is False
    assert started["message"] == f"Synthetic worker published {terminal}."
    assert state.cleanups == 1
    assert state.sleeps == [0.02] * 100


def test_terminal_publication_does_not_hide_unconfirmed_startup_cleanup(startup):
    manager, state, publish = startup
    primary = PermissionError("Synthetic startup cleanup is unconfirmed.")

    def fail_cleanup():
        publish("succeeded", 0)
        raise primary

    state.on_cleanup = fail_cleanup
    with pytest.raises(jobs.ComputeJobError, match="Startup cleanup is not confirmed") as caught:
        manager.start(
            entrypoint_id="optimizer", config_path="optimizer.json", idempotency_key="uncertain-cleanup-001",
        )
    assert caught.value.__cause__ is primary
    recorded = jobs._read_json_object(state.path, maximum_bytes=128 * 1_024)
    assert recorded["state"] == "stopping" and recorded["ended_at"] == ""
    assert str(primary) in recorded["message"]
    assert state.cleanups == 1 and state.sleeps == [0.02] * 100


@pytest.mark.parametrize("exit_status", (0, 19))
def test_supervisor_exit_without_terminal_metadata_never_implies_success(startup, exit_status):
    manager, state, _publish = startup
    state.returncode = exit_status
    with pytest.raises(jobs.ComputeJobError, match="readiness and identity"):
        manager.start(
            entrypoint_id="optimizer", config_path="optimizer.json", idempotency_key="missing-terminal-001",
        )
    recorded = jobs._read_json_object(state.path, maximum_bytes=128 * 1_024)
    assert recorded["state"] == "failed"
    assert state.cleanups == 0
