"""Focused tests for the durable, bounded Agent event chain.

Code version: v1.2.0-codex.1
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from app.core.agent import event_chain as event_chain_module
from app.core.owner_only_permissions import assert_owner_only_path

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
    assert_owner_only_path(chain.path, directory=False)
    assert_owner_only_path(chain.path.parent, directory=True)
    assert_owner_only_path(tmp_path / "runtime", directory=True)

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


def test_non_regular_event_file_is_rejected_without_reading_or_blocking(
    tmp_path, monkeypatch
) -> None:
    runtime_root = tmp_path / "runtime"
    event_directory = runtime_root / "events"
    event_directory.mkdir(parents=True)
    event_path = event_directory / f"{new_run_id()}.jsonl"
    if os.name == "nt":
        event_path.mkdir()
    else:
        os.mkfifo(event_path)
    original_read = Path.read_text
    reads = []

    def reject_event_read(path, *args, **kwargs):
        if path == event_path:
            reads.append(path)
            raise AssertionError("A non-regular event leaf must never be read.")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reject_event_read)

    chain = AgentEventChain(runtime_root, event_path.stem)

    assert chain.summary()["state"] == "invalid"
    assert chain.summary()["count"] == 0
    assert reads == []


def test_existing_event_chain_is_hardened_without_losing_earlier_records(tmp_path):
    runtime = tmp_path / "runtime"
    (runtime / "events").mkdir(parents=True)
    chain = AgentEventChain(runtime, new_run_id())
    first = chain.start()
    assert first is not None
    original = chain.path.read_bytes()
    # Recreate a legacy leaf with the parent's ordinary inherited permissions.
    chain.path.unlink()
    chain.path.write_bytes(original)
    reloaded = AgentEventChain(runtime, chain.run_id)
    second = reloaded.terminal("run.completed", status="finished", detail="Done.")
    assert second is not None
    assert reloaded.summary()["state"] == "ready"
    assert chain.path.read_bytes().startswith(original)
    records = [json.loads(line) for line in chain.path.read_text().splitlines()]
    assert [record["sequence"] for record in records] == [1, 2]
    assert records[1]["parent_event_id"] == records[0]["event_id"]
    assert_owner_only_path(chain.path, directory=False)
    assert_owner_only_path(chain.path.parent, directory=True)
    assert_owner_only_path(runtime, directory=True)


def test_private_append_failure_retains_in_memory_event_and_degraded_state(
    tmp_path, monkeypatch
):
    chain = AgentEventChain(tmp_path / "runtime", new_run_id())
    chain.start()
    original = chain.path.read_bytes()

    def failed_append(_path):
        failure = OSError("Synthetic private append permission failure.")
        failure.add_note("Synthetic stream cleanup failure.")
        failure.add_note("Synthetic parent cleanup failure.")
        raise failure

    monkeypatch.setattr(event_chain_module, "open_owner_only_append", failed_append)
    event = chain.terminal("run.failed", status="failed", detail="Stopped safely.")
    assert event is not None
    assert chain.summary()["count"] == 2
    assert chain.summary()["state"] == "degraded"
    assert "private append permission failure" in chain.summary()["error"]
    assert "stream cleanup failure" in chain.summary()["error"]
    assert "parent cleanup failure" in chain.summary()["error"]
    assert len(chain.summary()["error"]) <= 320
    assert chain.path.read_bytes() == original


def test_event_append_rejects_replaced_hardlink_without_touching_external_bytes(
    tmp_path,
):
    chain = AgentEventChain(tmp_path / "runtime", new_run_id())
    chain.start()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"Keep these unrelated bytes.")
    chain.path.unlink()
    os.link(outside, chain.path)
    event = chain.terminal("run.failed", status="failed", detail="Stopped safely.")
    assert event is not None
    assert chain.summary()["state"] == "degraded"
    assert outside.read_bytes() == b"Keep these unrelated bytes."


def test_event_fsync_failure_still_reports_degraded_persistence(tmp_path, monkeypatch):
    chain = AgentEventChain(tmp_path / "runtime", new_run_id())
    chain.start()

    def failed_fsync(_descriptor):
        raise OSError("Synthetic event fsync failure.")

    monkeypatch.setattr(os, "fsync", failed_fsync)
    event = chain.terminal("run.failed", status="failed", detail="Stopped safely.")
    assert event is not None
    assert chain.summary()["count"] == 2
    assert chain.summary()["state"] == "degraded"
    assert "event fsync failure" in chain.summary()["error"]
    assert len(chain.path.read_text().splitlines()) == 2
