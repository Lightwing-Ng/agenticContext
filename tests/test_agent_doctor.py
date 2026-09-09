"""Route and service tests for Agent doctor recovery UX.

Code version: v1.6.1-codex.1
"""

from __future__ import annotations

import json
from pathlib import Path
import time

import pytest

from app.core.agent.event_chain import AgentEventChain
from app.core.computer_use_agent import (
    CONTINUE_INTERRUPTED_AGENT_PROMPT,
    ComputerUseAgentService,
    ComputerUseSettingsStore,
    detect_host_operating_system,
)
from app.core.config import CrawlConfig


def _wait_for_completion(service: ComputerUseAgentService) -> dict[str, object]:
    deadline = time.monotonic() + 3
    while service.snapshot()["running"] and time.monotonic() < deadline:
        time.sleep(0.01)
    return service.snapshot()


def _write_valid_event_chain(
    runtime_root: Path,
    run_id: str,
    *,
    workspace: Path | None = None,
    delivery_phase: str = "",
    terminal_kind: str = "",
) -> AgentEventChain:
    chain = AgentEventChain(runtime_root, run_id)
    start_data: dict[str, object] = {"platform": "chatgpt", "browser": "edge"}
    if workspace is not None:
        workspace_stat = workspace.stat()
        start_data["workspace_identity"] = {
            "device": int(workspace_stat.st_dev),
            "inode": int(workspace_stat.st_ino),
        }
    assert chain.start(data=start_data) is not None
    if delivery_phase:
        assert chain.page_observation(
            "page.observe.agent_response",
            status=delivery_phase,
            detail="Durable provider delivery checkpoint recorded.",
            data={
                "delivery_checkpoint_version": "1.0.0",
                "delivery_phase": delivery_phase,
                "delivery_kind": "controller_observation",
                "exchange_id": "exchange-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "exchange_sequence": 1,
                "exchange_outbound_sha256": "b" * 64,
                "exchange_response_sha256": "c" * 64,
            },
        ) is not None
    if terminal_kind:
        assert chain.terminal(
            terminal_kind,
            status="finished" if terminal_kind == "run.completed" else "interrupted",
            detail="Persisted Agent run terminal state.",
        ) is not None
    assert chain.summary()["state"] == "ready"
    return chain


def _workspace_checkpoint(workspace: Path) -> dict[str, int]:
    workspace_stat = workspace.stat()
    return {
        "workspace_device": int(workspace_stat.st_dev),
        "workspace_inode": int(workspace_stat.st_ino),
    }


def test_idle_doctor_is_healthy_and_marks_run_only_checks_not_applicable(tmp_path) -> None:
    service = ComputerUseAgentService(
        ComputerUseSettingsStore(tmp_path / "settings.json"),
        runtime_root=tmp_path / "runtime",
    )

    doctor = service.doctor()
    checks = {check["id"]: check for check in doctor["checks"]}
    assert doctor["status"] == "healthy"
    assert checks["event_chain"]["status"] == "pass"
    assert checks["verification"]["status"] == "info"
    assert checks["bodycheck"]["status"] == "info"
    assert doctor["actions"][0]["enabled"] is False


def test_doctor_does_not_call_active_context_a_cleanup_failure(tmp_path) -> None:
    service = ComputerUseAgentService(
        ComputerUseSettingsStore(tmp_path / "settings.json"),
        runtime_root=tmp_path / "runtime",
    )
    service._snapshot.running = True
    service._snapshot.run_id = "run-0123456789abcdef"
    service._snapshot.context_file = str(tmp_path / "runtime" / "context.md")

    checks = {check["id"]: check for check in service.doctor()["checks"]}

    assert checks["context_cleanup"]["status"] == "pass"
    assert "active Agent run" in checks["context_cleanup"]["detail"]


def test_completed_run_persists_event_chain_and_doctor_can_report_healthy(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setattr(
        "app.core.computer_use_agent._start_macos_idle_sleep_assertion",
        lambda: None,
    )
    monkeypatch.setattr(
        "app.core.computer_use_agent._stop_macos_idle_sleep_assertion",
        lambda _process: None,
    )

    def runner(**kwargs):
        update = kwargs["update"]
        update(
            phase="running",
            message="Controller active.",
            verification_passed=True,
            bodycheck_passed=True,
        )
        return "Verified result", "https://chatgpt.com/c/example", 1, True

    runtime_root = tmp_path / "runtime"
    service = ComputerUseAgentService(
        ComputerUseSettingsStore(tmp_path / "settings.json"),
        runner=runner,
        runtime_root=runtime_root,
    )
    service.start("Inspect the project", str(workspace), CrawlConfig())
    snapshot = _wait_for_completion(service)

    assert snapshot["running"] is False
    assert snapshot["run_id"].startswith("run-")
    assert snapshot["event_chain_state"] == "ready"
    assert snapshot["event_count"] == 6
    assert snapshot["last_event_kind"] == "run.completed"
    event_path = runtime_root / "events" / f"{snapshot['run_id']}.jsonl"
    records = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
    assert [record["kind"] for record in records] == [
        "run.started",
        "page.observation",
        "page.observation",
        "page.observation",
        "page.observation",
        "run.completed",
    ]
    assert all(
        record["capability"] == "page.observe.agent_status"
        for record in records[1:5]
    )
    assert records[0]["data"]["workspace_identity"] == {
        "device": workspace.stat().st_dev,
        "inode": workspace.stat().st_ino,
    }
    assert "workspace_path" not in records[0]["data"]
    assert str(workspace) not in json.dumps(records)

    doctor = service.doctor()
    assert doctor["status"] == "healthy"
    assert doctor["run_id"] == snapshot["run_id"]
    assert doctor["events"][-1]["kind"] == "run.completed"


def test_run_revision_is_monotonic_across_a_persisted_service_restart(
    tmp_path,
    monkeypatch,
) -> None:
    """Persist an opaque run order even when wall-clock starts share one second."""
    workspace = tmp_path / "project"
    workspace.mkdir()
    settings_path = tmp_path / "settings.json"
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr(
        "app.core.computer_use_agent._start_macos_idle_sleep_assertion",
        lambda: None,
    )
    monkeypatch.setattr(
        "app.core.computer_use_agent._stop_macos_idle_sleep_assertion",
        lambda _process: None,
    )

    def runner(**_kwargs):
        return "Verified result", "https://chatgpt.com/c/example", 1, True

    service = ComputerUseAgentService(
        ComputerUseSettingsStore(settings_path),
        runner=runner,
        runtime_root=runtime_root,
    )
    service.start("Inspect the project", str(workspace), CrawlConfig())
    first_snapshot = _wait_for_completion(service)
    assert first_snapshot["run_revision"] == 1

    persisted_path = runtime_root / "last-run.json"
    assert json.loads(persisted_path.read_text(encoding="utf-8"))["run_revision"] == 1

    restarted = ComputerUseAgentService(
        ComputerUseSettingsStore(settings_path),
        runner=runner,
        runtime_root=runtime_root,
    )
    assert restarted.snapshot()["run_revision"] == 1
    restarted.start("Inspect the project again", str(workspace), CrawlConfig())
    second_snapshot = _wait_for_completion(restarted)
    assert second_snapshot["run_revision"] == 2
    assert json.loads(persisted_path.read_text(encoding="utf-8"))["run_revision"] == 2


def test_capability_and_doctor_routes_are_local_and_bounded(client) -> None:
    capabilities_response = client.get("/api/agent/capabilities")
    doctor_response = client.get("/api/agent/doctor")
    recovery_response = client.post(
        "/api/agent/doctor/recover",
        json={"action": "cleanup_context"},
    )
    invalid_recovery_response = client.post(
        "/api/agent/doctor/recover",
        json={"action": "unsupported"},
    )
    remote_response = client.get(
        "/api/agent/doctor",
        environ_base={"REMOTE_ADDR": "192.0.2.10"},
    )

    assert capabilities_response.status_code == 200
    assert doctor_response.status_code == 200
    assert recovery_response.status_code == 200
    assert recovery_response.get_json()["recovery"]["ok"] is True
    assert invalid_recovery_response.status_code == 409
    assert remote_response.status_code == 403
    capabilities = capabilities_response.get_json()
    doctor = doctor_response.get_json()
    assert capabilities["version"] == "1.4.0"
    assert len(capabilities["capabilities"]) == 28
    assert doctor["capability_registry_version"] == "1.4.0"
    assert "prompt" not in doctor
    assert "response" not in doctor


def test_agent_page_exposes_doctor_panel_and_recovery_script(client) -> None:
    body = client.get("/agent", follow_redirects=True).get_data(as_text=True)
    script = client.get("/static/computer-use-agent.js").get_data(as_text=True)

    assert 'id="agent_doctor_panel"' in body
    assert 'id="agent_doctor_checks"' in body
    assert 'id="agent_doctor_actions"' in body
    assert 'id="agent_doctor_events"' in body
    assert 'requestJson("/api/agent/doctor")' in script
    assert 'mutate("/api/agent/doctor/recover", {action})' in script
    assert "agentNeedsDoctor" in script


def test_doctor_continues_an_interrupted_edge_chatgpt_task_without_context_upload(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    run_id = "run-0123456789abcdef"
    _write_valid_event_chain(runtime_root, run_id, workspace=workspace)
    snapshot_path = runtime_root / "last-run.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "running": True,
                "phase": "running",
                "message": "Agent was running.",
                "workspace_path": str(workspace),
                **_workspace_checkpoint(workspace),
                "conversation_url": "https://chatgpt.com/c/interrupted-flight",
                "session_title": "demo_flight task",
                "session_mode": "recent",
                "operating_system": detect_host_operating_system(),
                "platform": "chatgpt",
                "browser": "edge",
                "model": "gpt-5.6-sol",
                "chatgpt_effort": "Cruise review",
                "read_only": True,
                "conversation_bound": True,
                "run_id": run_id,
            }
        ),
        encoding="utf-8",
    )
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        "app.core.computer_use_agent._start_macos_idle_sleep_assertion",
        lambda: None,
    )
    monkeypatch.setattr(
        "app.core.computer_use_agent._stop_macos_idle_sleep_assertion",
        lambda _process: None,
    )

    def runner(**kwargs):
        observed.update(kwargs)
        kwargs["update"](
            phase="running",
            message="Resuming controller actions.",
            conversation_bound=True,
        )
        return "Verified continuation", "https://chatgpt.com/c/interrupted-flight", 1, True

    service = ComputerUseAgentService(
        ComputerUseSettingsStore(tmp_path / "settings.json"),
        runner=runner,
        runtime_root=runtime_root,
        config_provider=CrawlConfig,
    )
    doctor = service.doctor()
    actions = {action["id"]: action for action in doctor["actions"]}
    assert service.snapshot()["phase"] == "interrupted"
    assert actions["continue"]["enabled"] is True

    recovery = service.recover("continue")
    snapshot = _wait_for_completion(service)

    assert recovery["ok"] is True
    assert observed["prompt"] == CONTINUE_INTERRUPTED_AGENT_PROMPT
    assert observed["context_path"] is None
    assert observed["target_url"] == "https://chatgpt.com/c/interrupted-flight"
    assert observed["session_mode"] == "recent"
    assert observed["read_only"] is True
    assert observed["settings"].chatgpt_effort == "Cruise review"
    assert snapshot["phase"] == "finished"
    assert snapshot["conversation_url"] == "https://chatgpt.com/c/interrupted-flight"
    assert not (runtime_root / "last-run.json").read_text(encoding="utf-8").find(
        "Agent was running."
    ) >= 0

    invalid_runtime_root = tmp_path / "invalid-runtime"
    invalid_runtime_root.mkdir()
    invalid_snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    invalid_snapshot.update(
        {
            "running": True,
            "phase": "running",
            "browser": "chrome",
            "workspace_path": str(Path(workspace)),
        }
    )
    (invalid_runtime_root / "last-run.json").write_text(
        json.dumps(invalid_snapshot),
        encoding="utf-8",
    )
    invalid_service = ComputerUseAgentService(
        ComputerUseSettingsStore(tmp_path / "invalid-settings.json"),
        runtime_root=invalid_runtime_root,
    )
    invalid_actions = {
        action["id"]: action for action in invalid_service.doctor()["actions"]
    }
    assert invalid_actions["continue"]["enabled"] is False


def test_doctor_rejects_continuation_without_confirmed_conversation_binding(
    tmp_path,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    run_id = "run-fedcba9876543210"
    _write_valid_event_chain(runtime_root, run_id, workspace=workspace)
    (runtime_root / "last-run.json").write_text(
        json.dumps(
            {
                "running": True,
                "phase": "running",
                "workspace_path": str(workspace),
                **_workspace_checkpoint(workspace),
                "conversation_url": "https://chatgpt.com/c/unproved-target",
                "session_mode": "recent",
                "operating_system": detect_host_operating_system(),
                "platform": "chatgpt",
                "browser": "edge",
                "model": "gpt-5.6-sol",
                "chatgpt_effort": "highest_available",
                "read_only": False,
                "conversation_bound": False,
                "run_id": run_id,
            }
        ),
        encoding="utf-8",
    )

    service = ComputerUseAgentService(
        ComputerUseSettingsStore(tmp_path / "settings.json"),
        runtime_root=runtime_root,
    )
    doctor = service.doctor()
    checks = {check["id"]: check for check in doctor["checks"]}
    actions = {action["id"]: action for action in doctor["actions"]}

    assert checks["interrupted_continuation"]["status"] == "warn"
    assert "before its ChatGPT conversation binding" in checks[
        "interrupted_continuation"
    ]["detail"]
    assert actions["continue"]["enabled"] is False
    with pytest.raises(RuntimeError, match="binding was confirmed"):
        service.recover("continue")


def test_finished_completed_delivery_checkpoint_is_healthy(tmp_path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    run_id = "run-0011223344556677"
    _write_valid_event_chain(
        runtime_root,
        run_id,
        workspace=workspace,
        delivery_phase="completed",
        terminal_kind="run.completed",
    )
    (runtime_root / "last-run.json").write_text(
        json.dumps(
            {
                "running": False,
                "phase": "finished",
                "message": "Agent completed the requested task.",
                "workspace_path": str(workspace),
                **_workspace_checkpoint(workspace),
                "conversation_url": "https://chatgpt.com/c/completed-delivery",
                "session_mode": "recent",
                "operating_system": detect_host_operating_system(),
                "platform": "chatgpt",
                "browser": "edge",
                "model": "gpt-5.6-sol",
                "chatgpt_effort": "highest_available",
                "read_only": False,
                "conversation_bound": True,
                "run_id": run_id,
                "verification_passed": True,
                "bodycheck_passed": True,
                "delivery_checkpoint_version": "1.0.0",
                "delivery_phase": "completed",
                "delivery_kind": "controller_observation",
                "exchange_id": "exchange-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "exchange_sequence": 1,
                "exchange_outbound_sha256": "b" * 64,
                "exchange_response_sha256": "c" * 64,
            }
        ),
        encoding="utf-8",
    )

    service = ComputerUseAgentService(
        ComputerUseSettingsStore(tmp_path / "settings.json"),
        runtime_root=runtime_root,
    )
    doctor = service.doctor()
    checks = {check["id"]: check for check in doctor["checks"]}

    assert doctor["status"] == "healthy"
    assert checks["event_chain"]["status"] == "pass"
    assert checks["delivery_checkpoint"]["status"] == "pass"
    assert checks["delivery_checkpoint"]["phase"] == "completed"


@pytest.mark.parametrize(
    "delivery_phase",
    [
        "prepared",
        "commit_attempted",
        "delivered",
        "response_received",
        "response_consumed",
        "completed",
        "unknown",
    ],
)
def test_doctor_blocks_continuation_across_non_idle_delivery_checkpoints(
    tmp_path,
    delivery_phase,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    run_id = "run-aabbccddeeff0011"
    _write_valid_event_chain(
        runtime_root,
        run_id,
        workspace=workspace,
        delivery_phase=delivery_phase,
    )
    (runtime_root / "last-run.json").write_text(
        json.dumps(
            {
                "running": True,
                "phase": "running",
                "workspace_path": str(workspace),
                **_workspace_checkpoint(workspace),
                "conversation_url": "https://chatgpt.com/c/delivery-boundary",
                "session_mode": "recent",
                "operating_system": detect_host_operating_system(),
                "platform": "chatgpt",
                "browser": "edge",
                "model": "gpt-5.6-sol",
                "chatgpt_effort": "highest_available",
                "read_only": False,
                "conversation_bound": True,
                "run_id": run_id,
                "delivery_checkpoint_version": "1.0.0",
                "delivery_phase": delivery_phase,
                "delivery_kind": "controller_observation",
                "exchange_id": "exchange-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "exchange_sequence": 1,
                "exchange_outbound_sha256": "b" * 64,
                "exchange_response_sha256": "c" * 64,
            }
        ),
        encoding="utf-8",
    )

    service = ComputerUseAgentService(
        ComputerUseSettingsStore(tmp_path / "settings.json"),
        runtime_root=runtime_root,
    )
    doctor = service.doctor()
    checks = {check["id"]: check for check in doctor["checks"]}
    actions = {action["id"]: action for action in doctor["actions"]}

    assert checks["interrupted_continuation"]["status"] == "warn"
    assert checks["event_chain"]["status"] == "pass"
    assert checks["delivery_checkpoint"]["status"] == "warn"
    assert delivery_phase.replace("_", " ") in checks[
        "interrupted_continuation"
    ]["detail"]
    assert actions["continue"]["enabled"] is False
    with pytest.raises(RuntimeError, match="delivery checkpoint"):
        service.recover("continue")


def test_doctor_blocks_continuation_when_event_chain_is_corrupted(tmp_path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    run_id = "run-8899aabbccddeeff"
    chain = _write_valid_event_chain(runtime_root, run_id, workspace=workspace)
    with chain.path.open("a", encoding="utf-8") as handle:
        handle.write("{not-valid-json}\n")
    (runtime_root / "last-run.json").write_text(
        json.dumps(
            {
                "running": True,
                "phase": "running",
                "workspace_path": str(workspace),
                **_workspace_checkpoint(workspace),
                "conversation_url": "https://chatgpt.com/c/corrupt-chain",
                "session_mode": "recent",
                "operating_system": detect_host_operating_system(),
                "platform": "chatgpt",
                "browser": "edge",
                "model": "gpt-5.6-sol",
                "chatgpt_effort": "highest_available",
                "read_only": False,
                "conversation_bound": True,
                "run_id": run_id,
            }
        ),
        encoding="utf-8",
    )

    service = ComputerUseAgentService(
        ComputerUseSettingsStore(tmp_path / "settings.json"),
        runtime_root=runtime_root,
    )
    doctor = service.doctor()
    checks = {check["id"]: check for check in doctor["checks"]}
    actions = {action["id"]: action for action in doctor["actions"]}

    assert doctor["status"] == "blocked"
    assert checks["event_chain"]["status"] == "fail"
    assert checks["interrupted_continuation"]["status"] == "warn"
    assert "not trustworthy" in checks["interrupted_continuation"]["detail"]
    assert actions["continue"]["enabled"] is False
    with pytest.raises(RuntimeError, match="not trustworthy"):
        service.recover("continue")


@pytest.mark.parametrize(
    ("checkpoint_version", "delivery_phase"),
    [("2.0.0", "prepared"), ("1.0.0", "future_phase")],
)
def test_doctor_blocks_continuation_for_future_or_unknown_delivery_checkpoint(
    tmp_path,
    checkpoint_version,
    delivery_phase,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    run_id = "run-7766554433221100"
    _write_valid_event_chain(
        runtime_root,
        run_id,
        workspace=workspace,
        delivery_phase="idle",
    )
    (runtime_root / "last-run.json").write_text(
        json.dumps(
            {
                "running": True,
                "phase": "running",
                "workspace_path": str(workspace),
                **_workspace_checkpoint(workspace),
                "conversation_url": "https://chatgpt.com/c/future-checkpoint",
                "session_mode": "recent",
                "operating_system": detect_host_operating_system(),
                "platform": "chatgpt",
                "browser": "edge",
                "model": "gpt-5.6-sol",
                "chatgpt_effort": "highest_available",
                "read_only": False,
                "conversation_bound": True,
                "run_id": run_id,
                "delivery_checkpoint_version": checkpoint_version,
                "delivery_phase": delivery_phase,
                "exchange_sequence": 1,
            }
        ),
        encoding="utf-8",
    )

    service = ComputerUseAgentService(
        ComputerUseSettingsStore(tmp_path / "settings.json"),
        runtime_root=runtime_root,
    )
    doctor = service.doctor()
    checks = {check["id"]: check for check in doctor["checks"]}
    actions = {action["id"]: action for action in doctor["actions"]}

    assert service.snapshot()["delivery_checkpoint_version"] == (
        "1.0.0" if checkpoint_version == "1.0.0" else "unknown"
    )
    assert checks["interrupted_continuation"]["status"] == "warn"
    assert "checkpoint" in checks["interrupted_continuation"]["detail"]
    assert actions["continue"]["enabled"] is False
    with pytest.raises(RuntimeError, match="checkpoint"):
        service.recover("continue")


@pytest.mark.parametrize(
    "malformed_phase",
    [[], {}],
    ids=["list", "object"],
)
def test_doctor_degrades_unhashable_delivery_phase_types_without_crashing(
    tmp_path,
    malformed_phase,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    run_id = "run-1029384756abcdef"
    _write_valid_event_chain(
        runtime_root,
        run_id,
        workspace=workspace,
        delivery_phase="idle",
    )
    (runtime_root / "last-run.json").write_text(
        json.dumps(
            {
                "running": True,
                "phase": "running",
                "workspace_path": str(workspace),
                **_workspace_checkpoint(workspace),
                "conversation_url": "https://chatgpt.com/c/malformed-checkpoint",
                "session_mode": "recent",
                "operating_system": detect_host_operating_system(),
                "platform": "chatgpt",
                "browser": "edge",
                "model": "gpt-5.6-sol",
                "chatgpt_effort": "highest_available",
                "read_only": False,
                "conversation_bound": True,
                "run_id": run_id,
                "delivery_checkpoint_version": "1.0.0",
                "delivery_phase": malformed_phase,
                "exchange_sequence": 1,
            }
        ),
        encoding="utf-8",
    )

    service = ComputerUseAgentService(
        ComputerUseSettingsStore(tmp_path / "settings.json"),
        runtime_root=runtime_root,
    )
    snapshot = service.snapshot()
    doctor = service.doctor()
    checks = {check["id"]: check for check in doctor["checks"]}
    actions = {action["id"]: action for action in doctor["actions"]}

    assert snapshot["delivery_checkpoint_version"] == "1.0.0"
    assert snapshot["delivery_phase"] == "unknown"
    assert checks["delivery_checkpoint"]["status"] == "warn"
    assert checks["delivery_checkpoint"]["phase"] == "unknown"
    assert actions["continue"]["enabled"] is False
    with pytest.raises(RuntimeError, match="delivery checkpoint"):
        service.recover("continue")
