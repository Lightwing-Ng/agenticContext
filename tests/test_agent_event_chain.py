"""Focused tests for the durable, bounded Agent event chain.

Code version: v1.3.1-codex.1
"""

from __future__ import annotations

import json
import os

import pytest

from app.core.agent import event_chain as event_chain_module
from app.core.agent.event_chain import (
    AgentEventChain,
    event_chain_for_snapshot,
    new_run_id,
    summarize_observation,
)


def test_event_chain_links_action_observation_verification_and_bodycheck(tmp_path) -> None:
    run_id = new_run_id()
    chain = AgentEventChain(tmp_path / "runtime", run_id)
    chain.start(data={"platform": "chatgpt", "prompt": "must not persist"})

    run_action_id, _ = chain.begin_action(
        "agent.action.run",
        turn=1,
        action_name="run",
    )
    run_observation = {
        "ok": True,
        "action": "run",
        "output": "PRIVATE_COMMAND_OUTPUT",
        "matches": ["PRIVATE_SOURCE_CONTENT"],
        "exit_code": 0,
    }
    chain.observation(run_action_id, "agent.action.run", run_observation)
    chain.verification(run_action_id, "agent.action.run", run_observation)

    bodycheck_action_id, _ = chain.begin_action(
        "agent.action.bodycheck",
        turn=2,
        action_name="bodycheck",
    )
    chain.observation(
        bodycheck_action_id,
        "agent.action.bodycheck",
        {"ok": True, "action": "bodycheck", "checks": [{"name": "git", "ok": True}]},
    )
    chain.bodycheck(
        bodycheck_action_id,
        "agent.action.bodycheck",
        {"ok": True, "action": "bodycheck", "checks": [{"name": "git", "ok": True}]},
    )
    chain.terminal(
        "run.completed",
        status="finished",
        detail="Agent run completed.",
        action_id=bodycheck_action_id,
    )

    summary = chain.summary()
    assert summary["state"] == "ready"
    assert summary["count"] == 8
    assert summary["last_event"]["kind"] == "run.completed"

    records = [
        json.loads(line)
        for line in chain.path.read_text(encoding="utf-8").splitlines()
    ]
    assert [record["sequence"] for record in records] == list(range(1, 9))
    assert [record["kind"] for record in records] == [
        "run.started",
        "action.requested",
        "observation",
        "verification",
        "action.requested",
        "observation",
        "bodycheck",
        "run.completed",
    ]
    assert records[2]["action_id"] == run_action_id
    assert records[3]["action_id"] == run_action_id
    assert records[5]["action_id"] == bodycheck_action_id
    assert records[6]["action_id"] == bodycheck_action_id
    assert all(
        record["parent_event_id"] == "" if index == 0 else record["parent_event_id"] == records[index - 1]["event_id"]
        for index, record in enumerate(records)
    )
    serialized = chain.path.read_text(encoding="utf-8")
    assert "must not persist" not in serialized
    assert "PRIVATE_COMMAND_OUTPUT" not in serialized
    assert "PRIVATE_SOURCE_CONTENT" not in serialized
    assert chain.path.stat().st_mode & 0o777 == 0o600
    assert chain.path.parent.stat().st_mode & 0o777 == 0o700

    reloaded = event_chain_for_snapshot(tmp_path / "runtime", run_id)
    assert reloaded is not None
    assert reloaded.summary()["count"] == 8
    assert reloaded.summary()["state"] == "ready"


def test_event_chain_rejects_verification_before_action_observation(tmp_path) -> None:
    chain = AgentEventChain(tmp_path / "runtime", new_run_id())
    chain.start()
    action_id, _ = chain.begin_action(
        "agent.action.run",
        turn=1,
        action_name="run",
    )

    assert chain.verification(action_id, "agent.action.run", {"ok": True}) is None
    assert chain.summary()["state"] == "invalid"
    assert chain.summary()["count"] == 2


def test_observation_summary_keeps_evidence_metadata_without_raw_content() -> None:
    digest = "a" * 64
    deleted_digest = "b" * 64
    summary = summarize_observation(
        {
            "ok": True,
            "error": "PRIVATE_COMMAND_OUTPUT",
            "output": "PRIVATE_COMMAND_OUTPUT",
            "matches": ["PRIVATE_SOURCE_CONTENT"],
            "checks": [{"name": "bodycheck", "ok": True}],
            "path": "README.md",
            "recovery_path": ".README.md.agent-backup-a1b2c3d4e5f60718.tmp",
            "workspace_identity": {"device": 123, "inode": 456},
            "read_receipt": {
                "sha256": digest,
                "generation": 7,
                "file_identity": {
                    "device": 123,
                    "inode": 789,
                    "size": 42,
                    "mtime_ns": 999,
                    "mode": 0o100644,
                },
                "content": "PRIVATE_READ_RECEIPT_CONTENT",
            },
            "delete_digest": deleted_digest,
        }
    )

    assert summary == {
        "ok": True,
        "error_present": True,
        "match_count": 1,
        "checks": [{"name": "bodycheck", "ok": True}],
        "output_chars": len("PRIVATE_COMMAND_OUTPUT"),
        "path": "README.md",
        "recovery_path": ".README.md.agent-backup-a1b2c3d4e5f60718.tmp",
        "workspace_identity": {"device": 123, "inode": 456},
        "read_receipt": {
            "sha256": digest,
            "generation": 7,
            "file_identity": {
                "device": 123,
                "inode": 789,
                "size": 42,
                "mtime_ns": 999,
                "mode": 0o100644,
            },
        },
        "delete_digest": deleted_digest,
    }
    assert "PRIVATE_COMMAND_OUTPUT" not in str(summary)
    assert "PRIVATE_SOURCE_CONTENT" not in str(summary)
    assert "PRIVATE_READ_RECEIPT_CONTENT" not in str(summary)


def test_nested_event_data_does_not_reintroduce_sensitive_values(tmp_path) -> None:
    chain = AgentEventChain(tmp_path / "runtime", new_run_id())
    safe = chain.start(
        data={
            "workspace_path": "/private/workspace",
            "conversation_url": "https://chatgpt.com/c/private-conversation",
            "url": "https://example.invalid/private",
            "token": "PRIVATE_TOKEN",
            "outer": {
                "middle": {
                    "inner": {
                        "output": "PRIVATE_NESTED_OUTPUT",
                        "workspace_path": "/private/nested-workspace",
                        "conversation_url": "https://chatgpt.com/c/private-nested",
                        "url": "https://example.invalid/private-nested",
                        "token": "PRIVATE_NESTED_TOKEN",
                    },
                },
            },
        }
    )

    assert safe is not None
    serialized = str(safe.as_dict())
    for private_value in (
        "PRIVATE_NESTED_OUTPUT",
        "/private/workspace",
        "https://chatgpt.com/c/private-conversation",
        "https://example.invalid/private",
        "PRIVATE_TOKEN",
        "/private/nested-workspace",
        "https://chatgpt.com/c/private-nested",
        "https://example.invalid/private-nested",
        "PRIVATE_NESTED_TOKEN",
    ):
        assert private_value not in serialized


def test_recovery_metadata_may_follow_a_terminal_event_but_not_an_action(tmp_path) -> None:
    chain = AgentEventChain(tmp_path / "runtime", new_run_id())
    chain.start()
    chain.terminal("run.completed", status="finished", detail="Completed.")

    recovery = chain.recovery(
        "cleanup_context",
        status="completed",
        detail="Cleanup completed.",
    )
    assert recovery is not None
    action_id, action = chain.begin_action(
        "agent.action.read",
        turn=1,
        action_name="read",
    )
    assert action_id == "action-0001"
    assert action is None
    assert chain.summary()["state"] == "invalid"


def test_non_regular_event_file_is_rejected_without_reading_or_blocking(tmp_path) -> None:
    runtime_root = tmp_path / "runtime"
    event_directory = runtime_root / "events"
    event_directory.mkdir(parents=True)
    event_path = event_directory / f"{new_run_id()}.jsonl"
    os.mkfifo(event_path)

    chain = AgentEventChain(runtime_root, event_path.stem)

    assert chain.summary()["state"] == "invalid"
    assert chain.summary()["count"] == 0


def test_event_append_does_not_enter_memory_when_persistence_fails(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chain = AgentEventChain(tmp_path / "runtime", new_run_id())
    assert chain.start() is not None

    def reject_fsync(_descriptor: int) -> None:
        raise OSError("simulated durable-write failure")

    monkeypatch.setattr("app.core.agent.event_chain.os.fsync", reject_fsync)
    action_id, event = chain.begin_action(
        "agent.action.write",
        turn=1,
        action_name="write",
    )

    assert action_id == "action-0001"
    assert event is None
    assert chain.summary()["state"] == "degraded"
    assert chain.summary()["count"] == 1
    assert chain.append("page.observation") is None


def test_event_append_refuses_before_exceeding_the_reloadable_line_cap(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(event_chain_module, "MAX_EVENT_FILE_LINES", 2)
    runtime_root = tmp_path / "runtime"
    run_id = new_run_id()
    chain = AgentEventChain(runtime_root, run_id)

    assert chain.start() is not None
    assert chain.page_observation("page.observe.agent_status") is not None
    persisted_at_cap = chain.path.read_bytes()

    assert chain.page_observation("page.observe.agent_status") is None
    assert chain.summary()["state"] == "degraded"
    assert chain.summary()["count"] == 2
    assert chain.path.read_bytes() == persisted_at_cap
    assert len(chain.path.read_text(encoding="utf-8").splitlines()) == 2

    reloaded = AgentEventChain(runtime_root, run_id)
    assert reloaded.summary()["state"] == "ready"
    assert reloaded.summary()["count"] == 2


def test_event_reload_rejects_records_copied_from_another_run(tmp_path) -> None:
    runtime_root = tmp_path / "runtime"
    first_run_id = new_run_id()
    second_run_id = new_run_id()
    first = AgentEventChain(runtime_root, first_run_id)
    second = AgentEventChain(runtime_root, second_run_id)
    assert first.start() is not None
    assert second.start() is not None
    assert second.page_observation(
        "page.observe.agent_status",
        data={
            "delivery_checkpoint_version": "1.0.0",
            "delivery_phase": "idle",
            "exchange_sequence": 0,
        },
    ) is not None

    first.path.write_bytes(second.path.read_bytes())
    reloaded = AgentEventChain(runtime_root, first_run_id)

    assert reloaded.summary()["state"] == "invalid"
    assert reloaded.summary()["count"] == 2
