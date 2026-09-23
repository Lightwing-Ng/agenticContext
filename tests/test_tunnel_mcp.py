"""Tunnel MCP adapter and /mcp route tests.

Code version: v2.11.0-codex.0
"""

from __future__ import annotations

import errno
import hashlib
from itertools import count
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core import tunnel_mcp as tunnel_mcp_module
from app.core.agent.capability_registry import capability_for_action
from app.core.computer_use_agent import ComputerUseSettings
from app.core.tunnel_mcp import (
    MAX_MCP_BATCH_ITEMS,
    MCP_STATELESS_PROTOCOL_VERSION,
    SERVER_INSTRUCTIONS,
    TUNNEL_TOOLS,
    TUNNEL_TOOLS_BY_NAME,
    TunnelMcpService,
    tunnel_tool_definitions,
)
from app.core.tunnel_projects import (
    ProjectRegistry,
    ProjectSelectionConflict,
    ProjectSelectionStore,
    TunnelProject,
    default_tunnel_browse_root,
    parse_project_registry,
)
from app.web.tunnel_routes import MAX_MCP_BODY_BYTES
from app.web.app import create_app

EXPECTED_TOOLS = [
    "current_project",
    "project_overview",
    "list_files",
    "search_files",
    "read_files",
    "apply_edits",
    "write_file",
    "delete_file",
    "run_check",
    "start_check",
    "observe_check",
    "stop_check",
    "show_changes",
    "review_changes",
]
_REQUEST_SEQUENCE = count(1)


def write_registry(path: Path, projects: list[dict]) -> ProjectRegistry:
    path.write_text(json.dumps({"schema_version": 1, "projects": projects}))
    return ProjectRegistry(path)


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def commit_all(root: Path, message: str = "init") -> None:
    git(root, "add", "-A")
    git(root, "commit", "-qm", message)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("# Rules\n")
    (project / "app.py").write_text('print("hi")\n')
    return project


@pytest.fixture
def reference(tmp_path: Path) -> Path:
    project = tmp_path / "reference"
    project.mkdir()
    (project / "AGENTS.md").write_text("# Reference rules\n")
    (project / "app.py").write_text("reference = True\n")
    return project


@pytest.fixture
def registry(tmp_path: Path, workspace: Path, reference: Path) -> ProjectRegistry:
    return write_registry(
        tmp_path / "tunnel-projects.json",
        [
            {"id": "main", "root": str(workspace), "writable": True},
            {
                "id": "ref",
                "root": str(reference),
                "writable": False,
                "description": "Reference.",
            },
        ],
    )


@pytest.fixture
def service(registry: ProjectRegistry, tmp_path: Path) -> TunnelMcpService:
    settings = ComputerUseSettings(workspace_path=str(tmp_path))
    return TunnelMcpService(
        lambda: settings, registry=registry, runtime_root=tmp_path / "runtime"
    )


def rpc(service: TunnelMcpService, method: str, params: dict | None = None, **headers):
    status, body = service.handle(
        {"jsonrpc": "2.0", "id": 7, "method": method, "params": params or {}},
        headers,
    )
    assert status == 200
    return body


def call(
    service: TunnelMcpService,
    name: str,
    arguments: dict | None = None,
    project: str | None = "main",
) -> dict:
    arguments = dict(arguments or {})
    if project is not None:
        arguments.setdefault("project", project)
        if name != "project_overview" and "project_identity" not in arguments:
            try:
                arguments["project_identity"] = service.registry.resolve(
                    project, service._fallback_workspace()
                ).identity
            except ValueError:
                # Invalid-project tests must still exercise the public validator.
                pass
    if TUNNEL_TOOLS_BY_NAME.get(name) and TUNNEL_TOOLS_BY_NAME[name].journaled:
        arguments.setdefault("request_id", f"test-auto-{next(_REQUEST_SEQUENCE):08d}")
    return rpc(service, "tools/call", {"name": name, "arguments": arguments})["result"]


def call_without_test_defaults(
    service: TunnelMcpService,
    name: str,
    arguments: dict,
) -> dict:
    """Exercise the exact public schema without helper-injected safety fields."""
    return rpc(service, "tools/call", {"name": name, "arguments": arguments})["result"]


def content(result: dict) -> dict:
    return result["structuredContent"]


def wait_for_check(
    service: TunnelMcpService,
    job_id: str,
    *,
    project: str = "main",
    timeout_seconds: float = 15.0,
) -> dict:
    """Poll one disposable check without hiding its terminal MCP result."""
    deadline = time.monotonic() + timeout_seconds
    latest: dict = {}
    while time.monotonic() < deadline:
        latest = call(service, "observe_check", {"job_id": job_id}, project=project)
        if content(latest)["job"]["state"] not in {"starting", "running"}:
            return latest
        time.sleep(0.05)
    pytest.fail(f"Check {job_id} did not finish: {latest}")


def provider_rpc(
    service: TunnelMcpService,
    provider: str,
    method: str,
    params: dict | None = None,
) -> dict:
    """Call the shared MCP service through one authenticated ingress identity."""
    status, body = service.handle(
        {"jsonrpc": "2.0", "id": 9, "method": method, "params": params or {}},
        {},
        provider=provider,
    )
    assert status == 200
    return body


# Protocol ---------------------------------------------------------------------


def test_provider_ingresses_share_tools_but_keep_activity_attribution(
    service: TunnelMcpService,
) -> None:
    chatgpt_tools = provider_rpc(service, "chatgpt", "tools/list")["result"]["tools"]
    gemini_tools = provider_rpc(service, "gemini", "tools/list")["result"]["tools"]
    assert gemini_tools == chatgpt_tools
    assert [tool["name"] for tool in gemini_tools] == EXPECTED_TOOLS
    assert all("provider" not in tool["inputSchema"]["properties"] for tool in gemini_tools)

    provider_rpc(
        service,
        "chatgpt",
        "tools/call",
        {"name": "list_files", "arguments": {"project": "main"}},
    )
    provider_rpc(
        service,
        "gemini",
        "tools/call",
        {"name": "list_files", "arguments": {"project": "main"}},
    )

    chatgpt_activity = service.activity_snapshot("chatgpt")
    gemini_activity = service.activity_snapshot("gemini")
    assert chatgpt_activity["call_count"] == 1
    assert gemini_activity["call_count"] == 1
    assert {record["provider"] for record in chatgpt_activity["recent_calls"]} == {
        "chatgpt"
    }
    assert {record["provider"] for record in gemini_activity["recent_calls"]} == {
        "gemini"
    }
    assert service.activity_snapshot()["call_count"] == 2


def test_unknown_provider_identity_is_rejected(service: TunnelMcpService) -> None:
    with pytest.raises(ValueError, match="Unknown authenticated MCP provider"):
        service.handle(
            {"jsonrpc": "2.0", "id": 10, "method": "tools/list", "params": {}},
            {},
            provider="untrusted-header",
        )


def test_tunnel_mcp_imports_in_a_fresh_interpreter() -> None:
    repository = Path(__file__).resolve().parents[1]
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from app.core.tunnel_mcp import TunnelMcpService; "
                "assert TunnelMcpService.__name__ == 'TunnelMcpService'"
            ),
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 0, process.stderr


def test_initialize_negotiates_supported_versions(service: TunnelMcpService) -> None:
    chatgpt = rpc(service, "initialize", {"protocolVersion": "2025-11-25"})["result"]
    assert chatgpt["protocolVersion"] == "2025-11-25"
    assert chatgpt["capabilities"] == {"tools": {"listChanged": False}}
    assert chatgpt["serverInfo"]["name"] == "AgenticContext"
    unknown = rpc(service, "initialize", {"protocolVersion": "1999-01-01"})["result"]
    assert unknown["protocolVersion"] == "2025-06-18"


def test_notifications_and_batches_follow_json_rpc(service: TunnelMcpService) -> None:
    assert service.handle(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}, {}
    ) == (202, None)
    status, body = service.handle(
        [
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        ],
        {},
    )
    assert status == 200
    assert body == [{"jsonrpc": "2.0", "id": 1, "result": {}}]
    missing = rpc(service, "does/not/exist")
    assert missing["error"]["code"] == -32601


def test_oversized_json_rpc_batch_is_rejected_before_any_item_runs(
    service: TunnelMcpService,
    workspace: Path,
) -> None:
    identity = service.registry.resolve("main", service._fallback_workspace()).identity
    batch = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "apply_edits",
                "arguments": {
                    "project": "main",
                    "project_identity": identity,
                    "request_id": "batch-limit-0001",
                    "edits": [
                        {
                            "path": "app.py",
                            "old_text": "hi",
                            "new_text": "changed",
                            "replace_all": True,
                        }
                    ],
                },
            },
        },
        *[
            {"jsonrpc": "2.0", "id": index, "method": "ping"}
            for index in range(2, MAX_MCP_BATCH_ITEMS + 2)
        ],
    ]

    status, result = service.handle(batch, {})

    assert status == 400
    assert result["error"]["code"] == -32600
    assert f"{MAX_MCP_BATCH_ITEMS} items" in result["error"]["message"]
    assert (workspace / "app.py").read_text(encoding="utf-8") == 'print("hi")\n'
    assert service.activity_snapshot()["call_count"] == 0


def test_stateless_discovery_marks_complete_results(service: TunnelMcpService) -> None:
    body = rpc(
        service,
        "server/discover",
        **{"MCP-Protocol-Version": MCP_STATELESS_PROTOCOL_VERSION},
    )["result"]
    assert body["resultType"] == "complete"
    assert MCP_STATELESS_PROTOCOL_VERSION in body["supportedVersions"]
    assert (
        body["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "AgenticContext"
    )


def test_tool_list_is_closed_and_project_scoped() -> None:
    tools = {tool["name"]: tool for tool in tunnel_tool_definitions()}
    assert list(tools) == EXPECTED_TOOLS
    assert [tool.name for tool in TUNNEL_TOOLS] == EXPECTED_TOOLS
    assert list(TUNNEL_TOOLS_BY_NAME) == EXPECTED_TOOLS
    for name, tool in tools.items():
        schema = tool["inputSchema"]
        assert "action" not in schema.get("properties", {})
        assert schema["additionalProperties"] is False
        if name == "current_project":
            assert "project" not in schema.get("properties", {})
        else:
            assert schema["required"][0] == "project", name
    assert tools["delete_file"]["inputSchema"]["required"] == [
        "project",
        "project_identity",
        "path",
        "expected_sha256",
        "request_id",
    ]
    assert tools["read_files"]["inputSchema"]["required"] == [
        "project",
        "project_identity",
        "files",
    ]
    assert set(tools["show_changes"]["inputSchema"]["properties"]) == {
        "project",
        "project_identity",
        "path",
        "include_patch",
        "staged",
    }
    assert tools["read_files"]["annotations"]["readOnlyHint"] is True
    assert tools["show_changes"]["annotations"]["readOnlyHint"] is True
    assert tools["write_file"]["annotations"]["destructiveHint"] is True
    assert tools["delete_file"]["annotations"]["destructiveHint"] is True
    for mutating in (
        "apply_edits",
        "write_file",
        "delete_file",
        "run_check",
        "start_check",
        "stop_check",
    ):
        assert tools[mutating]["annotations"]["readOnlyHint"] is False
    assert tools["observe_check"]["annotations"]["readOnlyHint"] is True
    public_text = f"{SERVER_INSTRUCTIONS}\n{json.dumps(list(tools.values()))}"
    for removed in (
        "read_file",
        "replace_in_file",
        "create_file",
        "call_runtime_tool",
        "list_projects",
        "git_log",
        "git_diff_hunks",
    ):
        assert re.search(rf"\b{re.escape(removed)}\b", public_text) is None


def test_every_listed_tool_is_dispatchable_by_its_published_schema(
    service: TunnelMcpService,
) -> None:
    listed = [tool["name"] for tool in rpc(service, "tools/list")["result"]["tools"]]
    assert listed == EXPECTED_TOOLS == [tool.name for tool in TUNNEL_TOOLS]
    project = service._registry.resolve("main", service._fallback_workspace())
    controller = service._workspace_for(project)
    for tool in TUNNEL_TOOLS:
        if tool.action:
            capability = capability_for_action(tool.action)
            assert capability is not None, tool.name
            assert callable(getattr(controller, capability.handler_name, None)), (
                tool.name
            )
        else:
            assert callable(getattr(service, f"_tool_{tool.name}", None)), tool.name


def test_every_listed_tool_accepts_a_behavior_valid_call(
    service: TunnelMcpService,
    repo: Path,
) -> None:
    (repo / "temporary.txt").write_text("temporary\n")
    results: dict[str, dict] = {
        "current_project": call(service, "current_project", project=None),
        "project_overview": call(service, "project_overview"),
        "list_files": call(service, "list_files", {"path": "."}),
        "search_files": call(service, "search_files", {"query": "print"}),
        "read_files": call(service, "read_files", {"files": [{"path": "app.py"}]}),
    }
    results["apply_edits"] = call(
        service,
        "apply_edits",
        {"edits": [{"path": "app.py", "old_text": "hi", "new_text": "hello"}]},
    )
    results["write_file"] = call(
        service,
        "write_file",
        {"path": "check.js", "content": "const value = 1;\n"},
    )
    temporary = call(service, "read_files", {"files": [{"path": "temporary.txt"}]})
    results["delete_file"] = call(
        service,
        "delete_file",
        {
            "path": "temporary.txt",
            "expected_sha256": content(temporary)["files"][0]["sha256"],
        },
    )
    results["run_check"] = call(
        service, "run_check", {"command": "node --check check.js"}
    )
    started = call(
        service,
        "start_check",
        {
            "command": "node --check check.js",
            "idempotency_key": "catalog-check-0001",
            "timeout_seconds": 60,
        },
    )
    results["start_check"] = started
    job_id = content(started)["job"]["job_id"]
    for _ in range(100):
        observed = call(service, "observe_check", {"job_id": job_id})
        if content(observed)["job"]["state"] not in {"starting", "running"}:
            break
        time.sleep(0.02)
    results["observe_check"] = observed
    results["stop_check"] = call(service, "stop_check", {"job_id": job_id})
    results["show_changes"] = call(service, "show_changes", {"include_patch": True})
    results["review_changes"] = call(service, "review_changes")

    assert set(results) == set(EXPECTED_TOOLS)
    assert all(not result["isError"] for result in results.values()), results


def test_unknown_semantic_fields_fail_instead_of_being_ignored(
    service: TunnelMcpService,
) -> None:
    extra = call(service, "read_files", {"files": [{"path": "app.py", "mode": "raw"}]})
    assert extra["isError"] is True
    assert "unsupported field(s): mode" in content(extra)["error"]
    legacy = call(service, "show_changes", {"base_commit": "HEAD"})
    assert legacy["isError"] is True
    assert "base_commit" in content(legacy)["error"]
    wrong_type = call(
        service,
        "apply_edits",
        {
            "edits": [
                {
                    "path": "app.py",
                    "old_text": "hi",
                    "new_text": "x",
                    "replace_all": "yes",
                }
            ]
        },
    )
    assert wrong_type["isError"] is True
    assert "boolean" in content(wrong_type)["error"]


@pytest.mark.parametrize(
    "selector",
    [
        {"runtime": "adaptive"},
        {"runtime_name": "full_operator_runtime"},
        {"execution_mode": "adaptive_runtime"},
        {"runtime_gateway": "direct"},
        {"adaptive_runtime": True},
        {"full_operator_runtime": True},
    ],
)
def test_runtime_selectors_are_neither_published_nor_required(
    service: TunnelMcpService,
    selector: dict,
) -> None:
    for tool in tunnel_tool_definitions():
        properties = set(tool["inputSchema"].get("properties", {}))
        assert properties.isdisjoint(
            {
                "runtime",
                "runtime_name",
                "execution_mode",
                "runtime_gateway",
                "adaptive_runtime",
                "full_operator_runtime",
            }
        ), tool["name"]

    plain = call(service, "read_files", {"files": [{"path": "app.py"}]})
    wrapped = call(service, "read_files", {"files": [{"path": "app.py"}], **selector})
    assert plain["isError"] is False
    assert wrapped["structuredContent"] == plain["structuredContent"]
    assert "adaptive_runtime" not in wrapped["content"][0]["text"]


@pytest.mark.parametrize(
    "name",
    [
        "read_file",
        "replace_in_file",
        "create_file",
        "call_runtime_tool",
        "list_projects",
        "git_log",
        "git_diff_hunks",
        "select_project",
        "run_process",
    ],
)
def test_removed_tool_names_are_protocol_errors(
    service: TunnelMcpService,
    name: str,
) -> None:
    body = rpc(service, "tools/call", {"name": name, "arguments": {}})
    assert body["error"]["code"] == -32602
    assert "Unknown tool" in body["error"]["message"]


def test_unknown_tool_is_a_protocol_error(service: TunnelMcpService) -> None:
    body = rpc(service, "tools/call", {"name": "shell", "arguments": {}})
    assert body["error"]["code"] == -32602


# Existing controller guarantees ----------------------------------------------


def test_tools_run_through_the_workspace_controller(
    service: TunnelMcpService,
    workspace: Path,
) -> None:
    overview = call(service, "project_overview")
    assert overview["isError"] is False
    assert content(overview)["instruction_files"] == ["AGENTS.md"]
    assert content(overview)["project"] == "main"
    assert content(overview)["writable"] is True
    assert str(workspace) not in overview["content"][0]["text"]

    read = call(
        service, "read_files", {"files": [{"path": "app.py"}, {"path": "AGENTS.md"}]}
    )
    assert [item["content"] for item in content(read)["files"]] == [
        '1: print("hi")',
        "1: # Rules",
    ]

    edited = call(
        service,
        "apply_edits",
        {"edits": [{"path": "app.py", "old_text": "hi", "new_text": "hello"}]},
    )
    assert edited["isError"] is False
    assert (workspace / "app.py").read_text() == 'print("hello")\n'

    escaped = call(service, "read_files", {"files": [{"path": "../outside.txt"}]})
    assert escaped["isError"] is True
    assert "inside the selected project" in content(escaped)["files"][0]["error"]

    refused = call(service, "run_check", {"command": "rm -rf ."})
    assert refused["isError"] is True

    smuggled = rpc(
        service,
        "tools/call",
        {
            "name": "delete_file",
            "arguments": {"project": "main", "path": "x", "action": "write"},
        },
    )
    assert smuggled["error"]["code"] == -32602

    activity = service.activity_snapshot()
    assert activity["call_count"] == 5
    assert activity["recent_calls"][0]["tool"] == "run_check"
    assert activity["recent_calls"][0]["target"].startswith("main: ")


def test_read_files_reports_all_three_batch_outcomes_and_utf8_failures(
    service: TunnelMcpService,
    workspace: Path,
) -> None:
    (workspace / "binary.dat").write_bytes(b"\xff\xfe\x00")

    succeeded = call(
        service,
        "read_files",
        {"files": [{"path": "app.py"}, {"path": "AGENTS.md"}]},
    )
    partial = call(
        service,
        "read_files",
        {"files": [{"path": "app.py"}, {"path": "missing.txt"}]},
    )
    failed = call(
        service,
        "read_files",
        {"files": [{"path": "missing.txt"}, {"path": "binary.dat"}]},
    )

    assert succeeded["isError"] is False
    assert content(succeeded)["outcome"] == "all_succeeded"
    assert content(succeeded)["succeeded"] == 2
    assert partial["isError"] is True
    assert content(partial)["outcome"] == "partial"
    assert (content(partial)["succeeded"], content(partial)["failed"]) == (1, 1)
    assert failed["isError"] is True
    assert content(failed)["outcome"] == "all_failed"
    assert content(failed)["succeeded"] == 0
    assert content(failed)["files"][1]["code"] == "not_text"


def test_read_only_observation_does_not_wait_for_a_long_synchronous_check(
    service: TunnelMcpService,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    original_execute = service._execute_action
    check_result: list[dict] = []

    def execute(project, action, arguments):
        if action == "run":
            started.set()
            assert release.wait(timeout=5)
            return {"ok": True, "action": "run", "command": arguments["command"]}
        return original_execute(project, action, arguments)

    monkeypatch.setattr(service, "_execute_action", execute)
    worker = threading.Thread(
        target=lambda: check_result.append(
            call(service, "run_check", {"command": "git status --short"})
        ),
        daemon=True,
    )
    worker.start()
    assert started.wait(timeout=2)
    try:
        observations = [
            call(service, "project_overview"),
            call(service, "list_files", {"path": ".", "depth": 1}),
            call(service, "search_files", {"query": "print", "path": "."}),
            call(service, "read_files", {"files": [{"path": "app.py"}]}),
        ]
        assert all(result["isError"] is False for result in observations)
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert check_result and check_result[0]["isError"] is False


def test_read_files_exposes_a_lossless_line_range_continuation(
    service: TunnelMcpService,
    workspace: Path,
) -> None:
    target = workspace / "large.txt"
    target.write_text(
        "".join(f"line-{index:04d} " + ("x" * 1_000) + "\n" for index in range(300)),
        encoding="utf-8",
    )

    first = content(call(service, "read_files", {"files": [{"path": "large.txt"}]}))[
        "files"
    ][0]
    assert first["content_truncated"] is True
    assert first["has_more"] is True
    assert first["next_start_line"] == first["end_line"] + 1

    continued = content(
        call(
            service,
            "read_files",
            {"files": [{"path": "large.txt", "start_line": first["next_start_line"]}]},
        )
    )["files"][0]
    assert continued["start_line"] == first["next_start_line"]
    assert continued["content"].startswith(f"{continued['start_line']}: line-")


def test_read_files_losslessly_continues_one_oversized_utf8_line(
    service: TunnelMcpService,
    workspace: Path,
) -> None:
    expected = ("αβγ" * 90_000) + "tail"
    (workspace / "single-line.txt").write_text(expected, encoding="utf-8")
    request = {"path": "single-line.txt", "start_line": 1}
    fragments: list[str] = []

    while True:
        result = content(call(service, "read_files", {"files": [request]}))["files"][0]
        assert result["sha256"] == hashlib.sha256(expected.encode("utf-8")).hexdigest()
        prefix = f"{result['start_line']}: "
        assert result["content"].startswith(prefix)
        fragments.append(result["content"][len(prefix):])
        if not result["has_more"]:
            break
        assert result["next_start_line"] == 1
        assert isinstance(result["next_start_character"], int)
        request = {
            "path": "single-line.txt",
            "start_line": result["next_start_line"],
            "start_character": result["next_start_character"],
        }

    assert "".join(fragments) == expected


def test_truncated_search_explains_how_to_continue(
    service: TunnelMcpService,
    workspace: Path,
) -> None:
    (workspace / "one.txt").write_text("needle\n", encoding="utf-8")
    (workspace / "two.txt").write_text("needle\n", encoding="utf-8")

    result = content(
        call(service, "search_files", {"query": "needle", "max_results": 1})
    )

    assert result["truncated"] is True
    assert "narrower project-relative path" in result["next_step"]


def test_multi_file_read_keeps_the_text_projection_valid_and_bounded(
    service: TunnelMcpService,
    workspace: Path,
) -> None:
    paths = []
    for index in range(8):
        path = f"large-{index}.txt"
        (workspace / path).write_text("λ" * 40_000, encoding="utf-8")
        paths.append({"path": path})

    result = call(service, "read_files", {"files": paths})
    projected = result["content"][0]["text"]
    parsed_projection = json.loads(projected)
    observation = content(result)

    assert parsed_projection["outcome"] == "all_succeeded"
    assert len(projected) <= 200_000
    assert sum(len(item["content"]) for item in observation["files"]) <= 150_000
    assert all(item["has_more"] for item in observation["files"])
    assert all(item["next_start_character"] for item in observation["files"])


def test_apply_edits_is_all_or_nothing(
    service: TunnelMcpService, workspace: Path
) -> None:
    (workspace / "lib.py").write_text("value = 1\nvalue = 1\n")
    result = call(
        service,
        "apply_edits",
        {
            "edits": [
                {"path": "app.py", "old_text": "hi", "new_text": "hello"},
                {"path": "lib.py", "old_text": "value = 1", "new_text": "value = 2"},
            ]
        },
    )
    assert result["isError"] is True
    assert "appears 2 times in lib.py" in content(result)["error"]
    assert (workspace / "app.py").read_text() == 'print("hi")\n'

    batch = call(
        service,
        "apply_edits",
        {
            "edits": [
                {"path": "app.py", "old_text": "hi", "new_text": "hello"},
                {
                    "path": "lib.py",
                    "old_text": "value = 1",
                    "new_text": "value = 2",
                    "replace_all": True,
                },
            ]
        },
    )
    assert batch["isError"] is False
    assert [item["replacements"] for item in content(batch)["files"]] == [1, 2]
    assert (workspace / "lib.py").read_text() == "value = 2\nvalue = 2\n"


def test_apply_edits_rolls_back_a_second_file_that_failed_after_publication(
    service: TunnelMcpService,
    registry: ProjectRegistry,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = workspace / "first.txt"
    second = workspace / "second.txt"
    first.write_text("first-before\n", encoding="utf-8")
    second.write_text("second-before\n", encoding="utf-8")
    access = service._workspace_for(registry.resolve("main"))
    original_snapshot = access._current_file_sha256
    replacement_sha = hashlib.sha256(b"second-after\n").hexdigest()
    failed_after_publish = False

    def fail_post_publish(path: Path):
        nonlocal failed_after_publish
        snapshot = original_snapshot(path)
        if (
            not failed_after_publish
            and path.name == "second.txt"
            and snapshot[0] == replacement_sha
        ):
            failed_after_publish = True
            raise OSError(5, "simulated post-publication verification failure")
        return snapshot

    monkeypatch.setattr(access, "_current_file_sha256", fail_post_publish)

    result = call(
        service,
        "apply_edits",
        {
            "edits": [
                {"path": "first.txt", "old_text": "before", "new_text": "after"},
                {"path": "second.txt", "old_text": "before", "new_text": "after"},
            ]
        },
    )

    assert failed_after_publish is True
    assert result["isError"] is True
    assert content(result)["outcome"] == "rolled_back"
    assert [record["status"] for record in content(result)["files"]] == [
        "rolled_back",
        "rolled_back",
    ]
    assert first.read_text(encoding="utf-8") == "first-before\n"
    assert second.read_text(encoding="utf-8") == "second-before\n"


def test_apply_edits_preserves_a_concurrent_edit_when_rollback_cannot_compare_swap(
    service: TunnelMcpService,
    registry: ProjectRegistry,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = workspace / "first.txt"
    second = workspace / "second.txt"
    first.write_text("first-before\n", encoding="utf-8")
    second.write_text("second-before\n", encoding="utf-8")
    access = service._workspace_for(registry.resolve("main"))
    original_replace = access._replace_text_file

    def fail_second(relative: Path, old: str, new: str, **kwargs):
        if relative.as_posix() == "second.txt":
            first.write_text("concurrent-user-edit\n", encoding="utf-8")
            raise OSError(28, "simulated disk full")
        return original_replace(relative, old, new, **kwargs)

    monkeypatch.setattr(access, "_replace_text_file", fail_second)

    result = call(
        service,
        "apply_edits",
        {
            "edits": [
                {"path": "first.txt", "old_text": "before", "new_text": "after"},
                {"path": "second.txt", "old_text": "before", "new_text": "after"},
            ]
        },
    )

    assert result["isError"] is True
    observation = content(result)
    assert observation["outcome"] == "partial"
    assert observation["files"][0]["status"] == "conflict"
    recovery = workspace / observation["files"][0]["recovery_path"]
    assert first.read_text(encoding="utf-8") == "concurrent-user-edit\n"
    assert recovery.read_text(encoding="utf-8") == "first-before\n"
    assert second.read_text(encoding="utf-8") == "second-before\n"


def test_apply_edits_reports_a_recovery_write_failure_as_not_concurrent(
    service: TunnelMcpService,
    registry: ProjectRegistry,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = workspace / "first.txt"
    second = workspace / "second.txt"
    first.write_text("first-before\n", encoding="utf-8")
    second.write_text("second-before\n", encoding="utf-8")
    access = service._workspace_for(registry.resolve("main"))
    original_replace = access._replace_text_file

    def fail_write_or_recovery(relative: Path, old: str, new: str, **kwargs):
        if relative.as_posix() == "second.txt":
            raise OSError(28, "simulated disk full during second write")
        if relative.as_posix() == "first.txt" and old == "first-after\n":
            raise OSError(28, "simulated disk full during recovery")
        return original_replace(relative, old, new, **kwargs)

    monkeypatch.setattr(access, "_replace_text_file", fail_write_or_recovery)

    result = call(
        service,
        "apply_edits",
        {
            "edits": [
                {"path": "first.txt", "old_text": "before", "new_text": "after"},
                {"path": "second.txt", "old_text": "before", "new_text": "after"},
            ]
        },
    )

    assert result["isError"] is True
    observation = content(result)
    assert observation["outcome"] == "partial"
    assert observation["files"][0]["status"] == "recovery_failed"
    assert "disk" in observation["files"][0]["detail"].casefold()
    assert "concurrent" not in observation["error"].casefold()
    assert first.read_text(encoding="utf-8") == "first-after\n"
    assert (workspace / observation["files"][0]["recovery_path"]).read_text(
        encoding="utf-8"
    ) == "first-before\n"


def test_path_guarded_recovery_cleanup_removes_the_verified_copy_once(
    service: TunnelMcpService,
    registry: ProjectRegistry,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    access = service._workspace_for(registry.resolve("main"))
    recovery = ".app.py.agent-backup-test.tmp"
    recovery_path = workspace / recovery
    recovery_path.write_text('print("hi")\n', encoding="utf-8")
    monkeypatch.setattr("app.core.workspace.controller._ANCHORED_MUTATION_SUPPORTED", False)

    removed = access._discard_recovery_file(
        Path("app.py"), recovery, 'print("hi")\n'
    )

    assert removed is True
    assert not recovery_path.exists()


def test_request_id_replays_the_recorded_result_in_process_and_after_restart(
    service: TunnelMcpService,
    registry: ProjectRegistry,
    workspace: Path,
    tmp_path: Path,
) -> None:
    target = workspace / "retry.txt"
    target.write_text("a\n", encoding="utf-8")
    arguments = {
        "request_id": "durable-retry-0001",
        "edits": [
            {
                "path": "retry.txt",
                "old_text": "a",
                "new_text": "aa",
                "replace_all": True,
            }
        ],
    }

    first = call(service, "apply_edits", arguments)
    replayed = call(service, "apply_edits", arguments)
    restarted = TunnelMcpService(
        lambda: ComputerUseSettings(workspace_path=str(workspace)),
        registry=ProjectRegistry(registry.path),
        runtime_root=tmp_path / "runtime",
    )
    replayed_after_restart = call(restarted, "apply_edits", arguments)

    assert first["isError"] is False
    assert content(replayed)["replayed"] is True
    assert content(replayed_after_restart)["replayed"] is True
    assert target.read_text(encoding="utf-8") == "aa\n"


def test_pruned_request_ids_remain_non_replayable_after_restart(
    registry: ProjectRegistry,
    workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tunnel_mcp_module, "REQUEST_JOURNAL_LIMIT", 2)
    monkeypatch.setattr(tunnel_mcp_module, "REQUEST_TOMBSTONE_LIMIT", 10)
    targets = [workspace / f"journal-{index}.txt" for index in range(1, 4)]
    for target in targets:
        target.write_text("a\n", encoding="utf-8")
    runtime_root = tmp_path / "journal-retention-runtime"
    service = TunnelMcpService(
        lambda: ComputerUseSettings(workspace_path=str(workspace)),
        registry=ProjectRegistry(registry.path),
        runtime_root=runtime_root,
    )

    requests = [
        {
            "request_id": f"pruned-id-000{index}",
            "edits": [
                {
                    "path": target.name,
                    "old_text": "a",
                    "new_text": "aa",
                    "replace_all": True,
                }
            ],
        }
        for index, target in enumerate(targets, start=1)
    ]
    for arguments in requests:
        assert call(service, "apply_edits", arguments)["isError"] is False

    assert (runtime_root / "tunnel-requests" / "expired-ids.log").is_file()
    restarted = TunnelMcpService(
        lambda: ComputerUseSettings(workspace_path=str(workspace)),
        registry=ProjectRegistry(registry.path),
        runtime_root=runtime_root,
    )
    duplicate = call(restarted, "apply_edits", requests[0])

    assert duplicate["isError"] is True
    assert content(duplicate)["code"] == "outcome_unknown"
    assert "Nothing was retried" in content(duplicate)["error"]
    assert targets[0].read_text(encoding="utf-8") == "aa\n"


def test_request_journal_fails_closed_when_tombstone_capacity_is_exhausted(
    registry: ProjectRegistry,
    workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tunnel_mcp_module, "REQUEST_JOURNAL_LIMIT", 1)
    monkeypatch.setattr(tunnel_mcp_module, "REQUEST_TOMBSTONE_LIMIT", 0)
    first = workspace / "first.txt"
    second = workspace / "second.txt"
    first.write_text("a\n", encoding="utf-8")
    second.write_text("a\n", encoding="utf-8")
    service = TunnelMcpService(
        lambda: ComputerUseSettings(workspace_path=str(workspace)),
        registry=ProjectRegistry(registry.path),
        runtime_root=tmp_path / "journal-full-runtime",
    )

    initial = call(
        service,
        "apply_edits",
        {
            "request_id": "retained-id-0001",
            "edits": [
                {"path": "first.txt", "old_text": "a", "new_text": "aa", "replace_all": True}
            ],
        },
    )
    blocked = call(
        service,
        "apply_edits",
        {
            "request_id": "retained-id-0002",
            "edits": [
                {"path": "second.txt", "old_text": "a", "new_text": "aa", "replace_all": True}
            ],
        },
    )

    assert initial["isError"] is False
    assert blocked["isError"] is True
    assert content(blocked)["code"] == "journal_unavailable"
    assert second.read_text(encoding="utf-8") == "a\n"


def test_mutation_without_request_id_is_refused_before_touching_disk(
    service: TunnelMcpService,
    workspace: Path,
) -> None:
    identity = service.registry.resolve("main", service._fallback_workspace()).identity

    result = call_without_test_defaults(
        service,
        "apply_edits",
        {
            "project": "main",
            "project_identity": identity,
            "edits": [{"path": "app.py", "old_text": "hi", "new_text": "changed"}],
        },
    )

    assert result["isError"] is True
    assert content(result)["code"] == "invalid_arguments"
    assert "request_id is required" in content(result)["error"]
    assert (workspace / "app.py").read_text(encoding="utf-8") == 'print("hi")\n'


@pytest.mark.parametrize(
    ("failure", "code", "guidance"),
    [
        (
            PermissionError(errno.EACCES, "Permission denied", "/private/hidden.txt"),
            "permission_denied",
            "Check the file and folder permissions",
        ),
        (
            OSError(errno.ENOSPC, "No space left on device", "/private/hidden.txt"),
            "disk_full",
            "Free space",
        ),
    ],
)
def test_mutation_os_failures_return_path_free_actionable_diagnostics(
    service: TunnelMcpService,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: OSError,
    code: str,
    guidance: str,
) -> None:
    def fail_write(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(service, "_dispatch_tool", fail_write)
    result = call(
        service,
        "write_file",
        {"path": "diagnostic.txt", "content": "not written\n"},
    )

    assert result["isError"] is True
    assert content(result)["code"] == code
    assert guidance in content(result)["error"]
    assert "/private/hidden.txt" not in json.dumps(result)
    assert not (workspace / "diagnostic.txt").exists()


def test_request_id_refuses_to_mutate_without_a_durable_started_record(
    registry: ProjectRegistry,
    workspace: Path,
    tmp_path: Path,
) -> None:
    target = workspace / "retry.txt"
    target.write_text("a\n", encoding="utf-8")
    runtime = tmp_path / "blocked-runtime"
    runtime.mkdir()
    (runtime / "tunnel-requests").write_text("not a directory", encoding="utf-8")
    unavailable = TunnelMcpService(
        lambda: ComputerUseSettings(workspace_path=str(workspace)),
        registry=ProjectRegistry(registry.path),
        runtime_root=runtime,
    )

    result = call(
        unavailable,
        "apply_edits",
        {
            "request_id": "durable-retry-0002",
            "edits": [
                {
                    "path": "retry.txt",
                    "old_text": "a",
                    "new_text": "aa",
                    "replace_all": True,
                }
            ],
        },
    )

    assert result["isError"] is True
    assert content(result)["code"] == "journal_unavailable"
    assert target.read_text(encoding="utf-8") == "a\n"


def test_request_id_with_an_unrecorded_outcome_is_never_replayed_blindly(
    service: TunnelMcpService,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = workspace / "retry.txt"
    target.write_text("a\n", encoding="utf-8")
    arguments = {
        "request_id": "lost-response-0001",
        "edits": [
            {
                "path": "retry.txt",
                "old_text": "a",
                "new_text": "aa",
                "replace_all": True,
            }
        ],
    }
    original_dispatch = service._dispatch_tool

    class SimulatedLostResponse(BaseException):
        pass

    def apply_then_lose_response(*args, **kwargs):
        original_dispatch(*args, **kwargs)
        raise SimulatedLostResponse

    monkeypatch.setattr(service, "_dispatch_tool", apply_then_lose_response)
    with pytest.raises(SimulatedLostResponse):
        call(service, "apply_edits", arguments)
    monkeypatch.setattr(service, "_dispatch_tool", original_dispatch)

    refused = call(service, "apply_edits", arguments)
    assert refused["isError"] is True
    assert content(refused)["code"] == "outcome_unknown"
    assert target.read_text(encoding="utf-8") == "aa\n"


def test_request_id_with_a_corrupt_existing_record_is_never_replayed(
    registry: ProjectRegistry,
    workspace: Path,
    tmp_path: Path,
) -> None:
    target = workspace / "retry.txt"
    target.write_text("a\n", encoding="utf-8")
    runtime = tmp_path / "corrupt-runtime"
    request_id = "corrupt-record-0001"
    project = registry.resolve("main")
    key = hashlib.sha256(
        f"{project.id}\0{project.identity}\0{request_id}".encode("utf-8")
    ).hexdigest()[:40]
    journal = runtime / "tunnel-requests"
    journal.mkdir(parents=True)
    (journal / f"{key}.json").write_text("{broken", encoding="utf-8")
    restarted = TunnelMcpService(
        lambda: ComputerUseSettings(workspace_path=str(workspace)),
        registry=ProjectRegistry(registry.path),
        runtime_root=runtime,
    )

    result = call(
        restarted,
        "apply_edits",
        {
            "request_id": request_id,
            "edits": [
                {
                    "path": "retry.txt",
                    "old_text": "a",
                    "new_text": "aa",
                    "replace_all": True,
                }
            ],
        },
    )

    assert result["isError"] is True
    assert content(result)["code"] == "outcome_unknown"
    assert target.read_text(encoding="utf-8") == "a\n"


def test_request_journal_refuses_growth_when_all_records_are_uncertain(
    registry: ProjectRegistry,
    workspace: Path,
    tmp_path: Path,
) -> None:
    target = workspace / "retry.txt"
    target.write_text("a\n", encoding="utf-8")
    runtime = tmp_path / "full-runtime"
    journal = runtime / "tunnel-requests"
    journal.mkdir(parents=True)
    for index in range(256):
        (journal / f"uncertain-{index:03d}.json").write_text("{broken", encoding="utf-8")
    bounded = TunnelMcpService(
        lambda: ComputerUseSettings(workspace_path=str(workspace)),
        registry=ProjectRegistry(registry.path),
        runtime_root=runtime,
    )

    result = call(
        bounded,
        "apply_edits",
        {
            "request_id": "bounded-journal-0001",
            "edits": [{"path": "retry.txt", "old_text": "a", "new_text": "aa"}],
        },
    )

    assert result["isError"] is True
    assert content(result)["code"] == "journal_unavailable"
    assert "256 unresolved" in content(result)["error"]
    assert target.read_text(encoding="utf-8") == "a\n"
    assert len(list(journal.glob("*.json"))) == 256


def test_write_file_requires_the_current_sha_to_replace(
    service: TunnelMcpService,
    workspace: Path,
) -> None:
    created = call(service, "write_file", {"path": "pkg/new.py", "content": "x = 1\n"})
    assert content(created)["created"] is True
    assert (workspace / "pkg" / "new.py").read_text() == "x = 1\n"
    assert not (workspace / "pkg" / "new.py").stat().st_mode & 0o111

    blind = call(service, "write_file", {"path": "app.py", "content": "replaced\n"})
    assert blind["isError"] is True
    assert "expected_sha256" in content(blind)["error"]

    stale = call(
        service,
        "write_file",
        {"path": "app.py", "content": "replaced\n", "expected_sha256": "0" * 64},
    )
    assert stale["isError"] is True

    calculated_without_read = hashlib.sha256(
        (workspace / "app.py").read_bytes()
    ).hexdigest()
    uncredentialed = call(
        service,
        "write_file",
        {
            "path": "app.py",
            "content": "caller-computed\n",
            "expected_sha256": calculated_without_read,
        },
    )
    assert uncredentialed["isError"] is True
    assert "read the current file first" in content(uncredentialed)["error"]

    current = call(service, "read_files", {"files": [{"path": "app.py"}]})
    sha = content(current)["files"][0]["sha256"]
    replaced = call(
        service,
        "write_file",
        {"path": "app.py", "content": "replaced\n", "expected_sha256": sha},
    )
    assert content(replaced)["created"] is False
    assert (workspace / "app.py").read_text() == "replaced\n"

    outside = call(service, "write_file", {"path": "../escape.py", "content": "x"})
    assert outside["isError"] is True
    absolute = call(
        service,
        "write_file",
        {"path": str(workspace.parent / "absolute-escape.py"), "content": "x"},
    )
    assert absolute["isError"] is True
    assert not (workspace.parent / "absolute-escape.py").exists()
    secret = call(service, "write_file", {"path": ".git/config", "content": "x"})
    assert secret["isError"] is True
    credential = call(service, "write_file", {"path": ".env", "content": "TOKEN=x"})
    assert credential["isError"] is True
    assert not (workspace / ".env").exists()


def test_write_file_rejects_a_receipt_after_an_external_change(
    service: TunnelMcpService,
    workspace: Path,
) -> None:
    current = call(service, "read_files", {"files": [{"path": "app.py"}]})
    old_sha = content(current)["files"][0]["sha256"]
    (workspace / "app.py").write_text("changed elsewhere\n", encoding="utf-8")

    result = call(
        service,
        "write_file",
        {
            "path": "app.py",
            "content": "replacement\n",
            "expected_sha256": old_sha,
        },
    )

    assert result["isError"] is True
    assert content(result)["code"] == "stale_file"
    assert "changed since you read it" in content(result)["error"]
    assert (workspace / "app.py").read_text(encoding="utf-8") == "changed elsewhere\n"


def test_write_file_refreshes_a_held_receipt_after_an_unrelated_edit(
    service: TunnelMcpService,
    workspace: Path,
) -> None:
    current = call(service, "read_files", {"files": [{"path": "app.py"}]})
    sha = content(current)["files"][0]["sha256"]
    created = call(
        service,
        "write_file",
        {"path": "unrelated.txt", "content": "new\n"},
    )
    assert created["isError"] is False

    replaced = call(
        service,
        "write_file",
        {
            "path": "app.py",
            "content": "replacement\n",
            "expected_sha256": sha,
        },
    )

    assert replaced["isError"] is False
    assert (workspace / "app.py").read_text(encoding="utf-8") == "replacement\n"


def test_delete_file_rejects_stale_sha_and_accepts_current_sha(
    service: TunnelMcpService,
    workspace: Path,
) -> None:
    target = workspace / "delete-me.txt"
    target.write_text("delete me\n")

    without_read = call(
        service,
        "delete_file",
        {
            "path": "delete-me.txt",
            "expected_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        },
    )
    assert without_read["isError"] is True
    assert "read" in content(without_read)["error"].casefold()
    assert target.read_text() == "delete me\n"

    current = call(service, "read_files", {"files": [{"path": "delete-me.txt"}]})
    sha = content(current)["files"][0]["sha256"]
    stale = call(
        service,
        "delete_file",
        {"path": "delete-me.txt", "expected_sha256": "0" * 64},
    )
    assert stale["isError"] is True
    assert "does not match" in content(stale)["error"]
    assert target.read_text() == "delete me\n"

    deleted = call(
        service,
        "delete_file",
        {"path": "delete-me.txt", "expected_sha256": sha},
    )
    assert deleted["isError"] is False
    assert not target.exists()


def test_delete_file_accepts_the_read_sha_after_an_unrelated_edit(
    service: TunnelMcpService,
    workspace: Path,
) -> None:
    target = workspace / "obsolete.txt"
    target.write_text("obsolete\n")
    sha = content(call(service, "read_files", {"files": [{"path": "obsolete.txt"}]}))[
        "files"
    ][0]["sha256"]
    edited = call(
        service,
        "apply_edits",
        {"edits": [{"path": "app.py", "old_text": "hi", "new_text": "hello"}]},
    )
    assert edited["isError"] is False

    deleted = call(
        service, "delete_file", {"path": "obsolete.txt", "expected_sha256": sha}
    )

    assert deleted["isError"] is False, content(deleted)
    assert not target.exists()


def test_delete_file_rejects_a_file_changed_after_it_was_read(
    service: TunnelMcpService,
    workspace: Path,
) -> None:
    target = workspace / "shared.txt"
    target.write_text("original\n")
    stale_sha = content(
        call(service, "read_files", {"files": [{"path": "shared.txt"}]})
    )["files"][0]["sha256"]
    target.write_text("changed by the user\n")
    call(service, "write_file", {"path": "unrelated.txt", "content": "x\n"})

    refused = call(
        service, "delete_file", {"path": "shared.txt", "expected_sha256": stale_sha}
    )

    assert refused["isError"] is True
    assert target.read_text() == "changed by the user\n"


@pytest.mark.parametrize(
    "path", ["../outside.txt", "/etc/hosts", ".git/config", "~/.ssh/id_rsa"]
)
def test_delete_file_stays_inside_the_project(
    service: TunnelMcpService,
    workspace: Path,
    path: str,
) -> None:
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("[core]\n")

    refused = call(service, "delete_file", {"path": path, "expected_sha256": "0" * 64})

    assert refused["isError"] is True
    assert (workspace / ".git" / "config").exists()


def test_apply_edits_rejects_missing_text_and_applies_same_file_edits_in_order(
    service: TunnelMcpService,
    workspace: Path,
) -> None:
    (workspace / "seq.txt").write_text("one two\n")
    missing = call(
        service,
        "apply_edits",
        {"edits": [{"path": "seq.txt", "old_text": "absent", "new_text": "x"}]},
    )
    assert missing["isError"] is True
    ordered = call(
        service,
        "apply_edits",
        {
            "edits": [
                {"path": "seq.txt", "old_text": "one", "new_text": "two"},
                {"path": "seq.txt", "old_text": "two two", "new_text": "three"},
            ]
        },
    )
    assert ordered["isError"] is False
    assert content(ordered)["outcome"] == "committed"
    assert content(ordered)["files"] == [
        {
            "path": "seq.txt",
            "status": "written",
            "replacements": 2,
            "sha256": content(ordered)["files"][0]["sha256"],
            "verified": True,
        }
    ]
    assert (workspace / "seq.txt").read_text() == "three\n"


def test_review_changes_requires_current_verification_and_leaves_the_tree_intact(
    service: TunnelMcpService,
    workspace: Path,
) -> None:
    (workspace / "check.js").write_text("const value = 1;\n")
    git(workspace, "init", "-q")
    commit_all(workspace)
    (workspace / "user_notes.txt").write_text("pre-existing user change\n")
    call(
        service,
        "apply_edits",
        {"edits": [{"path": "app.py", "old_text": "hi", "new_text": "hey"}]},
    )
    before = git(workspace, "status", "--porcelain")

    unverified = call(service, "review_changes")

    assert unverified["isError"] is True
    assert "verification" in content(unverified)["error"]
    checked = call(service, "run_check", {"command": "node --check check.js"})
    assert checked["isError"] is False, content(checked)
    assert content(call(service, "project_overview"))["verification_current"] is True
    before_review = git(workspace, "status", "--porcelain")
    reviewed = call(service, "review_changes")
    assert reviewed["isError"] is False, content(reviewed)
    assert before_review == before == git(workspace, "status", "--porcelain")
    assert (workspace / "app.py").read_text() == 'print("hey")\n'
    assert (workspace / "user_notes.txt").read_text() == "pre-existing user change\n"


def test_run_check_supports_required_commands_and_refuses_destruction(
    service: TunnelMcpService,
    workspace: Path,
) -> None:
    (workspace / "syntax.js").write_text("const answer = 42;\n")
    git(workspace, "init", "-q")
    commit_all(workspace)

    status = call(service, "run_check", {"command": "git status"})
    syntax = call(service, "run_check", {"command": "node --check syntax.js"})
    refused = call(service, "run_check", {"command": "rm -rf ."})

    assert status["isError"] is False, content(status)
    assert content(status)["exit_code"] == 0
    assert syntax["isError"] is False, content(syntax)
    assert refused["isError"] is True
    assert (workspace / "app.py").exists()


def test_background_check_deduplicates_and_survives_service_restart(
    service: TunnelMcpService,
    registry: ProjectRegistry,
    workspace: Path,
    tmp_path: Path,
) -> None:
    (workspace / "check_long.py").write_text(
        "import time\ntime.sleep(0.4)\nprint('checked')\n",
        encoding="utf-8",
    )
    arguments = {
        "command": "python check_long.py",
        "idempotency_key": "long-check-0001",
        "timeout_seconds": 60,
    }

    started = call(service, "start_check", arguments)
    duplicate = call(service, "start_check", arguments)
    job_id = content(started)["job"]["job_id"]
    assert content(duplicate)["job"]["job_id"] == job_id
    assert content(duplicate)["deduplicated"] is True

    restarted = TunnelMcpService(
        lambda: ComputerUseSettings(workspace_path=str(workspace)),
        registry=ProjectRegistry(registry.path),
        runtime_root=tmp_path / "runtime",
    )
    finished = wait_for_check(restarted, job_id)

    assert finished["isError"] is False, content(finished)
    assert content(finished)["job"]["state"] == "succeeded"
    assert content(finished)["job"]["verification"] == "recorded"
    reviewed = call(restarted, "review_changes")
    assert reviewed["isError"] is False, content(reviewed)


def test_completed_background_check_rebinds_verification_after_restart(
    service: TunnelMcpService,
    registry: ProjectRegistry,
    workspace: Path,
    tmp_path: Path,
) -> None:
    (workspace / "check_completed.py").write_text(
        "print('checked')\n",
        encoding="utf-8",
    )
    started = call(
        service,
        "start_check",
        {
            "command": "python check_completed.py",
            "idempotency_key": "completed-check-0001",
            "timeout_seconds": 60,
        },
    )
    job_id = content(started)["job"]["job_id"]
    completed = wait_for_check(service, job_id)
    assert completed["isError"] is False, content(completed)

    restarted = TunnelMcpService(
        lambda: ComputerUseSettings(workspace_path=str(workspace)),
        registry=ProjectRegistry(registry.path),
        runtime_root=tmp_path / "runtime",
    )
    recovered = call(restarted, "observe_check", {"job_id": job_id})

    assert recovered["isError"] is False, content(recovered)
    assert content(recovered)["job"]["verification"] == "recorded"
    assert call(restarted, "review_changes")["isError"] is False


def test_background_check_can_be_cancelled(
    service: TunnelMcpService,
    workspace: Path,
) -> None:
    (workspace / "check_cancel.py").write_text(
        "import time\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    started = call(
        service,
        "start_check",
        {
            "command": "python check_cancel.py",
            "idempotency_key": "cancel-check-0001",
            "timeout_seconds": 60,
        },
    )
    job_id = content(started)["job"]["job_id"]

    stopped = call(service, "stop_check", {"job_id": job_id})

    assert stopped["isError"] is False, content(stopped)
    assert content(stopped)["job"]["state"] == "stopped"
    observed = call(service, "observe_check", {"job_id": job_id})
    assert observed["isError"] is True
    assert content(observed)["job"]["state"] == "stopped"


def test_background_check_evidence_expires_after_an_external_edit(
    service: TunnelMcpService,
    workspace: Path,
) -> None:
    (workspace / "check_quick.py").write_text("print('checked')\n", encoding="utf-8")
    started = call(
        service,
        "start_check",
        {
            "command": "python check_quick.py",
            "idempotency_key": "fresh-check-0001",
            "timeout_seconds": 60,
        },
    )
    job_id = content(started)["job"]["job_id"]
    finished = wait_for_check(service, job_id)
    assert finished["isError"] is False, content(finished)

    (workspace / "app.py").write_text('print("changed later")\n', encoding="utf-8")
    stale = call(service, "observe_check", {"job_id": job_id})

    assert stale["isError"] is True
    assert content(stale)["code"] == "stale_verification"
    assert content(stale)["job"]["workspace_changed"] is True
    assert call(service, "review_changes")["isError"] is True


def test_reobserving_old_failed_check_does_not_revoke_a_newer_pass(
    service: TunnelMcpService,
    workspace: Path,
) -> None:
    (workspace / "check_fail.py").write_text(
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    (workspace / "check_pass.py").write_text("print('passed')\n", encoding="utf-8")
    failed_start = call(
        service,
        "start_check",
        {
            "command": "python check_fail.py",
            "idempotency_key": "ordered-check-fail-0001",
            "timeout_seconds": 60,
        },
    )
    failed_job = content(failed_start)["job"]["job_id"]
    failed = wait_for_check(service, failed_job)
    assert failed["isError"] is True
    assert content(failed)["job"]["state"] == "failed"

    passed_start = call(
        service,
        "start_check",
        {
            "command": "python check_pass.py",
            "idempotency_key": "ordered-check-pass-0001",
            "timeout_seconds": 60,
        },
    )
    passed_job = content(passed_start)["job"]["job_id"]
    passed = wait_for_check(service, passed_job)
    assert passed["isError"] is False, content(passed)
    assert content(call(service, "project_overview"))["verification_current"] is True
    assert call(service, "review_changes")["isError"] is False

    observed_old = call(service, "observe_check", {"job_id": failed_job})

    assert observed_old["isError"] is True
    assert content(observed_old)["job"]["state"] == "failed"
    assert content(call(service, "project_overview"))["verification_current"] is True
    assert call(service, "review_changes")["isError"] is False


def test_reobserving_old_success_does_not_override_a_newer_failure(
    service: TunnelMcpService,
    workspace: Path,
) -> None:
    (workspace / "check_pass_first.py").write_text(
        "print('passed')\n",
        encoding="utf-8",
    )
    (workspace / "check_fail_later.py").write_text(
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    passed_start = call(
        service,
        "start_check",
        {
            "command": "python check_pass_first.py",
            "idempotency_key": "ordered-pass-first-0001",
            "timeout_seconds": 60,
        },
    )
    passed_job = content(passed_start)["job"]["job_id"]
    assert wait_for_check(service, passed_job)["isError"] is False

    failed_start = call(
        service,
        "start_check",
        {
            "command": "python check_fail_later.py",
            "idempotency_key": "ordered-fail-later-0001",
            "timeout_seconds": 60,
        },
    )
    failed_job = content(failed_start)["job"]["job_id"]
    assert wait_for_check(service, failed_job)["isError"] is True
    assert call(service, "review_changes")["isError"] is True

    observed_old = call(service, "observe_check", {"job_id": passed_job})

    assert observed_old["isError"] is True
    assert content(observed_old)["code"] == "stale_verification"
    assert content(observed_old)["job"]["verification"] == "not_recorded"
    assert call(service, "review_changes")["isError"] is True


# Explicit project registry -----------------------------------------------------


def test_current_project_reports_the_exact_saved_authority_without_host_paths(
    service: TunnelMcpService,
    registry: ProjectRegistry,
    workspace: Path,
    reference: Path,
) -> None:
    saved = service.selection_store.save("main")

    discovered = call(service, "current_project", project=None)

    assert discovered["isError"] is False
    observation = content(discovered)
    selected = observation["current_project"]
    expected = registry.resolve("main")
    assert selected == {
        "id": "main",
        "writable": True,
        "identity": expected.identity,
        "registered": True,
        "available": True,
        "selected": True,
    }
    assert observation["selection"]["revision"] == saved.revision
    assert observation["selection"]["source"] == "selection"
    assert observation["selection"]["selected_project_ids"] == ["main", "ref"]
    rendered = json.dumps(observation)
    assert str(workspace) not in rendered
    assert str(reference) not in rendered


def test_switching_the_saved_project_does_not_redirect_a_pinned_task(
    service: TunnelMcpService,
    registry: ProjectRegistry,
) -> None:
    first_selection = service.selection_store.save("main")
    resolved = content(call(service, "current_project", project=None))["current_project"]

    service.selection_store.save("ref", expected_revision=first_selection.revision)
    switched = content(call(service, "current_project", project=None))
    pinned = call(
        service,
        "read_files",
        {
            "project_identity": resolved["identity"],
            "files": [{"path": "app.py"}],
        },
        project="main",
    )

    assert switched["current_project"]["id"] == "ref"
    assert switched["current_project"]["writable"] is False
    assert content(pinned)["files"][0]["content"] == '1: print("hi")'
    assert content(pinned)["files"][0]["sha256"] == hashlib.sha256(
        b'print("hi")\n'
    ).hexdigest()
    assert registry.resolve("main").identity == resolved["identity"]


def test_multi_project_preference_is_atomic_without_revoking_registry_access(
    service: TunnelMcpService,
    tmp_path: Path,
) -> None:
    store = ProjectSelectionStore(tmp_path / "selection.json")
    legacy_payload = {
        "schema_version": 1,
        "project_id": "main",
        "revision": 4,
        "selected_at": 1.0,
    }
    store.path.write_text(json.dumps(legacy_payload), encoding="utf-8")
    assert store.load().selected_project_ids is None

    saved = store.save(
        "main",
        selected_project_ids=["main", "ref"],
        expected_revision=4,
    )
    assert saved.selected_project_ids == ("main", "ref")
    persisted = json.loads(store.path.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == 2
    assert persisted["selected_project_ids"] == ["main", "ref"]

    # A legacy single-project switch remains usable after the preferred list
    # exists; it adds the requested project instead of failing mysteriously.
    narrowed = store.save(
        "main",
        selected_project_ids=["main"],
        expected_revision=saved.revision,
    )
    switched = store.save("ref", expected_revision=narrowed.revision)
    assert switched.project_id == "ref"
    assert switched.selected_project_ids == ("main", "ref")

    before_conflict = store.path.read_bytes()
    with pytest.raises(ProjectSelectionConflict):
        store.save(
            "ref",
            selected_project_ids=["ref"],
            expected_revision=narrowed.revision,
        )
    assert store.path.read_bytes() == before_conflict

    service.selection_store.save(
        "main",
        selected_project_ids=["ref", "main"],
    )
    discovered = content(call(service, "current_project", project=None))
    records = {project["id"]: project for project in discovered["projects"]}
    assert records["main"]["selected"] is True
    assert records["ref"]["selected"] is True
    assert discovered["selection"]["selected_project_ids"] == ["ref", "main"]

    service.selection_store.save(
        "main",
        selected_project_ids=["main"],
        expected_revision=discovered["selection"]["revision"],
    )
    narrowed_discovery = content(call(service, "current_project", project=None))
    narrowed_records = {
        project["id"]: project for project in narrowed_discovery["projects"]
    }
    assert narrowed_records["ref"]["selected"] is False
    # The preference list is not an authority boundary. Explicit access to a
    # registered read-only reference remains available.
    assert call(service, "project_overview", project="ref")["isError"] is False


def test_selected_fallback_recovers_when_the_previous_current_project_is_removed(
    service: TunnelMcpService,
    registry: ProjectRegistry,
    reference: Path,
) -> None:
    service.selection_store.save(
        "main",
        selected_project_ids=["main", "ref"],
    )
    write_registry(
        registry.path,
        [{"id": "ref", "root": str(reference), "writable": False}],
    )

    discovered = content(call(service, "current_project", project=None))

    assert discovered["current_project"]["id"] == "ref"
    assert discovered["selection"]["source"] == "only_selected_project"
    assert discovered["selection"]["selected_project_ids"] == ["ref"]


def test_tunnel_browse_root_uses_the_current_home_without_a_username_literal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "different-user"
    desktop = home / "Desktop"
    desktop.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)

    assert default_tunnel_browse_root() == desktop
    source = Path("app/core/tunnel_projects.py").read_text(encoding="utf-8")
    assert "/Users/lightwing" not in source


def test_current_project_reports_unselected_stale_and_invalid_registry_states(
    service: TunnelMcpService,
    registry: ProjectRegistry,
    reference: Path,
    workspace: Path,
    tmp_path: Path,
) -> None:
    unselected = call(service, "current_project", project=None)
    assert unselected["isError"] is False
    assert content(unselected)["current_project"] is None
    assert content(unselected)["selection"]["source"] == "none"
    assert "select" in content(unselected)["next_step"].casefold()

    service.selection_store.save("main")
    write_registry(
        registry.path,
        [{"id": "ref", "root": str(reference), "writable": False}],
    )
    stale = call(service, "current_project", project=None)
    assert stale["isError"] is False
    assert content(stale)["current_project"] is None
    assert "no longer registered" in content(stale)["problem"]

    invalid_path = tmp_path / "invalid-current-project.json"
    invalid_path.write_text("{not-json", encoding="utf-8")
    invalid = TunnelMcpService(
        lambda: ComputerUseSettings(workspace_path=str(workspace)),
        registry=ProjectRegistry(invalid_path),
        runtime_root=tmp_path / "invalid-runtime",
    )
    failed = call(invalid, "current_project", project=None)
    assert failed["isError"] is True
    assert content(failed)["code"] == "project_unavailable"
    for result in (unselected, stale, failed):
        rendered = json.dumps(result)
        assert str(workspace) not in rendered
        assert str(reference) not in rendered


def test_missing_registered_root_stays_visible_without_hiding_usable_projects(
    tmp_path: Path,
) -> None:
    """One unavailable registration must not invalidate every other authority."""
    available = tmp_path / "available"
    available.mkdir()
    missing = tmp_path / "missing-reference"
    registry = write_registry(
        tmp_path / "mixed-registry.json",
        [
            {"id": "available", "root": str(available), "writable": True},
            {"id": "missing", "root": str(missing), "writable": False},
        ],
    )
    service = TunnelMcpService(
        lambda: ComputerUseSettings(),
        registry=registry,
        runtime_root=tmp_path / "runtime",
    )

    discovered = content(call(service, "current_project", project=None))
    projects = {item["id"]: item for item in discovered["projects"]}

    assert discovered["ok"] is True
    assert projects["available"]["available"] is True
    assert projects["missing"]["available"] is False
    assert "missing" in projects["missing"]["problem"].casefold()
    unavailable_identity = projects["missing"]["identity"]
    refused = call(service, "project_overview", project="missing")
    assert refused["isError"] is True
    assert content(refused)["code"] == "project_unavailable"
    assert call(service, "project_overview", project="available")["isError"] is False

    missing.mkdir()
    rebound = {
        item["id"]: item
        for item in content(call(service, "current_project", project=None))["projects"]
    }
    assert rebound["missing"]["available"] is True
    assert rebound["missing"]["identity"] != unavailable_identity


def test_discovery_reports_when_background_checks_are_not_configured(
    registry: ProjectRegistry,
    workspace: Path,
) -> None:
    without_runtime = TunnelMcpService(
        lambda: ComputerUseSettings(workspace_path=str(workspace)),
        registry=ProjectRegistry(registry.path),
        runtime_root=None,
    )

    discovered = content(call(without_runtime, "current_project", project=None))
    overview = content(call(without_runtime, "project_overview"))

    assert discovered["limits"]["background_checks"] is False
    assert overview["limits"]["background_checks"] is False


def test_project_overview_fails_closed_when_the_registered_root_is_unavailable(
    service: TunnelMcpService,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.core.tunnel_mcp.project_availability",
        lambda _project: "The project folder is not readable by this service.",
    )

    result = call(service, "project_overview")

    assert result["isError"] is True
    assert content(result)["code"] == "project_unavailable"
    assert "not readable" in content(result)["error"]
    assert str(workspace) not in json.dumps(result)


def test_pinned_project_identity_refuses_a_later_registration_remap(
    tmp_path: Path,
) -> None:
    original = tmp_path / "original"
    replacement = tmp_path / "replacement-project"
    for root, value in ((original, "original\n"), (replacement, "replacement\n")):
        root.mkdir()
        (root / "note.txt").write_text(value, encoding="utf-8")
    registry_path = tmp_path / "identity-registry.json"
    registry = write_registry(
        registry_path,
        [{"id": "work", "root": str(original), "writable": True}],
    )
    service = TunnelMcpService(
        lambda: ComputerUseSettings(),
        registry=registry,
        runtime_root=tmp_path / "identity-runtime",
    )
    service.selection_store.save("work")
    identity = content(call(service, "current_project", project=None))["current_project"][
        "identity"
    ]

    write_registry(
        registry_path,
        [{"id": "work", "root": str(replacement), "writable": True}],
    )
    refused = call(
        service,
        "read_files",
        {"project_identity": identity, "files": [{"path": "note.txt"}]},
        project="work",
    )

    assert refused["isError"] is True
    assert content(refused)["code"] == "project_changed"
    assert "re-registered" in content(refused)["error"]
    assert str(original) not in json.dumps(refused)
    assert str(replacement) not in json.dumps(refused)


def test_pinned_project_identity_refuses_same_path_root_replacement(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "note.txt").write_text("original\n", encoding="utf-8")
    registry = write_registry(
        tmp_path / "identity-registry.json",
        [{"id": "work", "root": str(project_root), "writable": True}],
    )
    service = TunnelMcpService(
        lambda: ComputerUseSettings(),
        registry=registry,
        runtime_root=tmp_path / "identity-runtime",
    )
    service.selection_store.save("work")
    identity = content(call(service, "current_project", project=None))["current_project"][
        "identity"
    ]

    admitted_root = tmp_path / "admitted-project"
    project_root.rename(admitted_root)
    project_root.mkdir()
    replacement_note = project_root / "note.txt"
    replacement_note.write_text("replacement\n", encoding="utf-8")

    refused_read = call(
        service,
        "read_files",
        {"project_identity": identity, "files": [{"path": "note.txt"}]},
        project="work",
    )
    refused_write = call(
        service,
        "write_file",
        {
            "project_identity": identity,
            "path": "created.txt",
            "content": "must not be written\n",
        },
        project="work",
    )

    for refused in (refused_read, refused_write):
        assert refused["isError"] is True
        assert content(refused)["code"] == "project_changed"
        assert "current_project or project_overview" in content(refused)["error"]
    assert replacement_note.read_text(encoding="utf-8") == "replacement\n"
    assert not (project_root / "created.txt").exists()
    assert (admitted_root / "note.txt").read_text(encoding="utf-8") == "original\n"

    rediscovered = content(call(service, "current_project", project=None))["current_project"]
    assert rediscovered["identity"] != identity
    reread = call(
        service,
        "read_files",
        {
            "project_identity": rediscovered["identity"],
            "files": [{"path": "note.txt"}],
        },
        project="work",
    )
    assert content(reread)["files"][0]["content"] == "1: replacement"


def test_controller_identity_failure_is_not_replayed_against_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "note.txt").write_text("admitted\n", encoding="utf-8")
    registry = write_registry(
        tmp_path / "identity-registry.json",
        [{"id": "work", "root": str(project_root), "writable": True}],
    )
    service = TunnelMcpService(
        lambda: ComputerUseSettings(),
        registry=registry,
        runtime_root=tmp_path / "identity-runtime",
    )
    service.selection_store.save("work")
    identity = content(call(service, "current_project", project=None))["current_project"][
        "identity"
    ]
    assert call(
        service,
        "read_files",
        {"project_identity": identity, "files": [{"path": "note.txt"}]},
        project="work",
    )["isError"] is False

    from app.core.tunnel_projects import project_availability as check_availability

    admitted_root = tmp_path / "admitted-project"
    swapped = False

    def swap_after_admission(project: TunnelProject) -> str:
        nonlocal swapped
        problem = check_availability(project)
        if not swapped:
            swapped = True
            project_root.rename(admitted_root)
            project_root.mkdir()
            (project_root / "note.txt").write_text("replacement\n", encoding="utf-8")
        return problem

    monkeypatch.setattr("app.core.tunnel_mcp.project_availability", swap_after_admission)
    refused = call(
        service,
        "read_files",
        {"project_identity": identity, "files": [{"path": "note.txt"}]},
        project="work",
    )

    assert refused["isError"] is True
    assert content(refused)["code"] == "project_changed"
    assert "nothing was replayed" in content(refused)["error"]
    assert "files" not in content(refused)
    assert (project_root / "note.txt").read_text(encoding="utf-8") == "replacement\n"
    assert (admitted_root / "note.txt").read_text(encoding="utf-8") == "admitted\n"


@pytest.mark.parametrize(
    "project", ["MAIN", "main ", "../project", "project", "/tmp", "", "unknown"]
)
def test_project_ids_resolve_exactly_and_are_never_paths(
    service: TunnelMcpService,
    project: str,
) -> None:
    result = call(
        service, "read_files", {"files": [{"path": "app.py"}]}, project=project
    )
    assert result["isError"] is True
    error = content(result)["error"]
    assert "project" in error.casefold()


def test_missing_project_argument_is_refused(service: TunnelMcpService) -> None:
    result = call(service, "read_files", {"files": [{"path": "app.py"}]}, project=None)
    assert result["isError"] is True
    assert "project is required" in content(result)["error"]


def test_project_identity_is_required_after_project_discovery(
    service: TunnelMcpService,
) -> None:
    result = rpc(
        service,
        "tools/call",
        {
            "name": "read_files",
            "arguments": {"project": "main", "files": [{"path": "app.py"}]},
        },
    )["result"]

    assert result["isError"] is True
    assert "project_identity is required" in content(result)["error"]


def test_read_only_projects_reject_every_mutation_before_touching_files(
    service: TunnelMcpService,
    reference: Path,
) -> None:
    before = {path.name: path.read_bytes() for path in reference.iterdir()}
    sha = content(
        call(service, "read_files", {"files": [{"path": "app.py"}]}, project="ref")
    )["files"][0]["sha256"]
    attempts = [
        (
            "apply_edits",
            {"edits": [{"path": "app.py", "old_text": "True", "new_text": "False"}]},
        ),
        ("write_file", {"path": "new.py", "content": "x\n"}),
        ("write_file", {"path": "app.py", "content": "x\n", "expected_sha256": sha}),
        ("delete_file", {"path": "app.py", "expected_sha256": sha}),
        ("run_check", {"command": "git status"}),
    ]
    for name, arguments in attempts:
        result = call(service, name, arguments, project="ref")
        assert result["isError"] is True, name
        assert "read-only" in content(result)["error"], name
    assert {path.name: path.read_bytes() for path in reference.iterdir()} == before

    overview = content(call(service, "project_overview", project="ref"))
    assert overview["writable"] is False
    assert overview["instruction_files"] == ["AGENTS.md"]
    assert "verification_current" not in overview
    assert (
        call(service, "search_files", {"query": "reference"}, project="ref")["isError"]
        is False
    )
    assert call(service, "list_files", {"path": "."}, project="ref")["isError"] is False


def test_paths_cannot_cross_project_boundaries(
    service: TunnelMcpService,
    workspace: Path,
    reference: Path,
) -> None:
    relative_escape = call(
        service, "read_files", {"files": [{"path": "../reference/app.py"}]}
    )
    assert content(relative_escape)["files"][0]["ok"] is False
    for absolute in (str(reference / "app.py"), str(workspace / "app.py")):
        result = call(service, "read_files", {"files": [{"path": absolute}]})
        assert result["isError"] is True
        assert "relative to the project root" in content(result)["error"]
    edit = call(
        service,
        "apply_edits",
        {
            "edits": [
                {"path": "../reference/app.py", "old_text": "True", "new_text": "False"}
            ]
        },
    )
    assert edit["isError"] is True
    write = call(
        service, "write_file", {"path": str(reference / "planted.py"), "content": "x"}
    )
    assert write["isError"] is True
    assert (reference / "app.py").read_text() == "reference = True\n"
    assert not (reference / "planted.py").exists()


def test_symlinked_escape_stays_refused(
    service: TunnelMcpService, workspace: Path, reference: Path
) -> None:
    (workspace / "linked").symlink_to(reference, target_is_directory=True)
    result = call(service, "read_files", {"files": [{"path": "linked/app.py"}]})
    assert content(result)["files"][0]["ok"] is False
    write = call(service, "write_file", {"path": "linked/new.py", "content": "x"})
    assert write["isError"] is True
    assert not (reference / "new.py").exists()


def test_instruction_discovery_is_scoped_to_the_selected_project(
    tmp_path: Path,
) -> None:
    desktop = tmp_path / "desktop"
    first = desktop / "first"
    second = desktop / "second"
    for project in (first, second):
        (project / "docs").mkdir(parents=True)
        (project / "AGENTS.md").write_text("# Rules\n")
        (project / "docs" / "AGENTS.md").write_text("# Docs rules\n")
    (desktop / "AGENTS.md").write_text("# Desktop\n")
    vendored = first / "vendor" / "other"
    vendored.mkdir(parents=True)
    (vendored / ".git").mkdir()
    (vendored / "AGENTS.md").write_text("# Another repository\n")
    registry = write_registry(
        tmp_path / "registry.json",
        [
            {"id": "first", "root": str(first), "writable": True},
            {"id": "second", "root": str(second), "writable": False},
        ],
    )
    service = TunnelMcpService(
        lambda: ComputerUseSettings(workspace_path=str(desktop)),
        registry=registry,
        runtime_root=tmp_path / "rt",
    )

    overview = content(call(service, "project_overview", project="first"))

    assert overview["instruction_files"] == ["AGENTS.md"]
    assert overview["nested_instruction_files"] == ["docs/AGENTS.md"]
    rendered = json.dumps(overview)
    assert "vendor/other" not in rendered
    assert "second" not in rendered
    assert "Desktop" not in rendered


def test_switching_projects_keeps_each_projects_sha_guards(
    service: TunnelMcpService,
    workspace: Path,
    tmp_path: Path,
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (other / "app.py").write_text("other = 1\n")
    registry = write_registry(
        tmp_path / "two-writable.json",
        [
            {"id": "main", "root": str(workspace), "writable": True},
            {"id": "other", "root": str(other), "writable": True},
        ],
    )
    service = TunnelMcpService(
        lambda: ComputerUseSettings(), registry=registry, runtime_root=tmp_path / "rt2"
    )
    main_sha = content(call(service, "read_files", {"files": [{"path": "app.py"}]}))[
        "files"
    ][0]["sha256"]
    other_sha = content(
        call(service, "read_files", {"files": [{"path": "app.py"}]}, project="other")
    )["files"][0]["sha256"]
    call(
        service,
        "apply_edits",
        {"edits": [{"path": "app.py", "old_text": "1", "new_text": "2"}]},
        project="other",
    )

    wrong_project_sha = call(
        service,
        "write_file",
        {"path": "app.py", "content": "x\n", "expected_sha256": other_sha},
    )
    assert wrong_project_sha["isError"] is True
    stale_other = call(
        service,
        "delete_file",
        {"path": "app.py", "expected_sha256": other_sha},
        project="other",
    )
    assert stale_other["isError"] is True
    assert (other / "app.py").read_text() == "other = 2\n"

    deleted = call(
        service, "delete_file", {"path": "app.py", "expected_sha256": main_sha}
    )
    assert deleted["isError"] is False
    assert not (workspace / "app.py").exists()
    assert (other / "app.py").exists()


def test_reregistering_a_project_rebinds_its_controller(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for project, text in ((first, "first\n"), (second, "second\n")):
        project.mkdir()
        (project / "note.txt").write_text(text)
    path = tmp_path / "registry.json"
    registry = write_registry(
        path, [{"id": "work", "root": str(first), "writable": True}]
    )
    service = TunnelMcpService(
        lambda: ComputerUseSettings(), registry=registry, runtime_root=tmp_path / "rt"
    )
    first_sha = content(
        call(service, "read_files", {"files": [{"path": "note.txt"}]}, project="work")
    )["files"][0]["sha256"]

    time.sleep(0.01)
    write_registry(path, [{"id": "work", "root": str(second), "writable": True}])
    reread = content(
        call(service, "read_files", {"files": [{"path": "note.txt"}]}, project="work")
    )
    assert reread["files"][0]["content"] == "1: second"
    stale = call(
        service,
        "delete_file",
        {"path": "note.txt", "expected_sha256": first_sha},
        project="work",
    )
    assert stale["isError"] is True
    assert (second / "note.txt").exists() and (first / "note.txt").exists()


@pytest.mark.parametrize("in_place", [False, True], ids=["atomic-replace", "same-inode"])
def test_registry_cache_invalidates_equal_size_replacement_with_preserved_mtime(
    tmp_path: Path,
    in_place: bool,
) -> None:
    first = tmp_path / "first-root"
    second = tmp_path / "other-root"
    first.mkdir()
    second.mkdir()
    registry_path = tmp_path / "projects.json"
    write_registry(
        registry_path,
        [{"id": "work", "root": str(first), "writable": True}],
    )
    registry = ProjectRegistry(registry_path)
    previous = registry.resolve("work")
    previous_stat = registry_path.stat()

    replacement = tmp_path / "replacement.json"
    replacement.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "projects": [{"id": "work", "root": str(second), "writable": True}],
            }
        ),
        encoding="utf-8",
    )
    os.utime(
        replacement,
        ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns),
    )
    assert replacement.stat().st_size == previous_stat.st_size
    if in_place:
        registry_path.write_bytes(replacement.read_bytes())
        os.utime(
            registry_path,
            ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns),
        )
    else:
        os.replace(replacement, registry_path)
    current_stat = registry_path.stat()
    assert current_stat.st_size == previous_stat.st_size
    assert current_stat.st_mtime_ns == previous_stat.st_mtime_ns
    if in_place:
        assert current_stat.st_ino == previous_stat.st_ino

    current = registry.resolve("work")

    assert current.root == second.resolve()
    assert current.identity != previous.identity


def test_queued_mutation_rechecks_registry_authority_after_project_lock(
    service: TunnelMcpService,
    registry: ProjectRegistry,
    workspace: Path,
    reference: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = registry.resolve("main", service._fallback_workspace())
    lock = service._project_lock(project)
    reached_journal = threading.Event()
    original_run_journaled = service._run_journaled

    def mark_waiting_for_project_lock(*args, **kwargs):
        reached_journal.set()
        return original_run_journaled(*args, **kwargs)

    monkeypatch.setattr(service, "_run_journaled", mark_waiting_for_project_lock)
    results: list[dict] = []
    lock.acquire()
    worker = threading.Thread(
        target=lambda: results.append(
            call(
                service,
                "apply_edits",
                {
                    "request_id": "queued-permission-0001",
                    "edits": [
                        {
                            "path": "app.py",
                            "old_text": "hi",
                            "new_text": "changed",
                            "replace_all": True,
                        }
                    ],
                },
            )
        )
    )
    worker.start()
    try:
        assert reached_journal.wait(timeout=2)
        write_registry(
            registry.path,
            [
                {"id": "main", "root": str(workspace), "writable": False},
                {"id": "ref", "root": str(reference), "writable": False},
            ],
        )
    finally:
        lock.release()
        worker.join(timeout=3)

    assert not worker.is_alive()
    assert len(results) == 1
    assert results[0]["isError"] is True
    assert content(results[0])["code"] == "project_changed"
    assert (workspace / "app.py").read_text(encoding="utf-8") == 'print("hi")\n'


def test_parent_folder_is_never_an_implicit_project(tmp_path: Path) -> None:
    desktop = tmp_path / "desktop"
    repository = desktop / "repo"
    repository.mkdir(parents=True)
    git(repository, "init", "-q")
    service = TunnelMcpService(
        lambda: ComputerUseSettings(workspace_path=str(desktop)),
        registry=ProjectRegistry(tmp_path / "missing.json"),
        runtime_root=tmp_path / "rt",
    )
    for project in ("desktop", "repo", "project"):
        assert call(service, "project_overview", project=project)["isError"] is True


def test_git_root_workspace_is_the_only_fallback_project(tmp_path: Path) -> None:
    repository = tmp_path / "solo"
    repository.mkdir()
    (repository / "AGENTS.md").write_text("# Rules\n")
    git(repository, "init", "-q")
    service = TunnelMcpService(
        lambda: ComputerUseSettings(workspace_path=str(repository)),
        registry=ProjectRegistry(tmp_path / "missing.json"),
        runtime_root=tmp_path / "rt",
    )
    assert call(service, "project_overview", project="solo")["isError"] is False
    discovered = content(call(service, "current_project", project=None))
    assert discovered["current_project"]["id"] == "solo"
    assert discovered["current_project"]["registered"] is False
    assert discovered["current_project"]["writable"] is True


def test_invalid_git_placeholder_cannot_enter_read_only_fallback(tmp_path: Path) -> None:
    workspace = tmp_path / "not-a-repository"
    workspace.mkdir()
    (workspace / ".git").write_text("placeholder", encoding="utf-8")
    service = TunnelMcpService(
        lambda: ComputerUseSettings(workspace_path=str(workspace)),
        registry=ProjectRegistry(tmp_path / "missing-registry.json"),
        runtime_root=tmp_path / "runtime",
    )

    result = call(service, "project_overview", project=workspace.name)

    assert result["isError"] is True
    assert content(result)["ok"] is False


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload, tmp: payload["projects"].append(
                {
                    "id": "nested",
                    "root": str(tmp / "project" / "sub"),
                    "writable": False,
                }
            ),
            "overlap",
        ),
        (
            lambda payload, tmp: payload["projects"].append(
                {"id": "Main", "root": str(tmp / "reference"), "writable": False}
            ),
            "twice",
        ),
        (
            lambda payload, tmp: payload["projects"][0].update({"shell": True}),
            "unknown fields",
        ),
        (
            lambda payload, tmp: payload["projects"][0].update(
                {"root": "relative/path"}
            ),
            "absolute",
        ),
        (
            lambda payload, tmp: payload["projects"][0].update(
                {"root": str(Path.home())}
            ),
            "too broad",
        ),
        (
            lambda payload, tmp: payload["projects"][0].update({"writable": "yes"}),
            "true or false",
        ),
        (
            lambda payload, tmp: payload["projects"][0].update({"id": "../main"}),
            "must start with a letter",
        ),
        (lambda payload, tmp: payload.update({"schema_version": 2}), "schema_version"),
    ],
)
def test_invalid_registries_fail_closed(
    tmp_path: Path, workspace: Path, reference: Path, mutate, message: str
) -> None:
    (workspace / "sub").mkdir()
    payload = {
        "schema_version": 1,
        "projects": [
            {"id": "main", "root": str(workspace), "writable": True},
            {"id": "ref", "root": str(reference), "writable": False},
        ],
    }
    mutate(payload, tmp_path)
    with pytest.raises(ValueError, match=message):
        parse_project_registry(payload)
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload))
    service = TunnelMcpService(
        lambda: ComputerUseSettings(workspace_path=str(workspace)),
        registry=ProjectRegistry(path),
        runtime_root=tmp_path / "rt",
    )
    assert (
        call(service, "read_files", {"files": [{"path": "app.py"}]})["isError"] is True
    )


# Git change inspection ---------------------------------------------------------


@pytest.fixture
def repo(workspace: Path) -> Path:
    git(workspace, "init", "-q")
    commit_all(workspace)
    return workspace


def test_show_changes_reports_status_bounded_patch_and_scoping(
    service: TunnelMcpService,
    repo: Path,
) -> None:
    edited = call(
        service,
        "apply_edits",
        {"edits": [{"path": "app.py", "old_text": "hi", "new_text": "changed"}]},
    )
    assert edited["isError"] is False, content(edited)
    (repo / "AGENTS.md").write_text("# Rules\n\nStaged.\n")
    git(repo, "add", "AGENTS.md")
    (repo / "draft.py").write_text("x = 1\n")

    changes = content(call(service, "show_changes", {"include_patch": True}))

    assert " M app.py" in changes["status"]
    assert "M  AGENTS.md" in changes["status"]
    assert "?? draft.py" in changes["status"]
    assert ".agent-" not in changes["status"]
    assert "app.py" in changes["unstaged_stat"]
    assert "AGENTS.md" in changes["staged_stat"]
    assert '+print("changed")' in changes["patch"]
    assert "Staged." not in changes["patch"]
    assert "draft.py" not in changes["patch"]
    assert changes["patch_truncated"] is False

    staged = content(
        call(service, "show_changes", {"include_patch": True, "staged": True})
    )
    assert "+Staged." in staged["patch"]
    assert "app.py" not in staged["patch"]

    scoped = content(
        call(service, "show_changes", {"include_patch": True, "path": "AGENTS.md"})
    )
    assert scoped["patch"] == ""
    assert "app.py" not in scoped["unstaged_stat"]
    assert "app.py" not in scoped["status"]
    assert "draft.py" not in scoped["status"]
    scoped_staged = content(
        call(
            service,
            "show_changes",
            {"include_patch": True, "staged": True, "path": "AGENTS.md"},
        )
    )
    assert "+Staged." in scoped_staged["patch"]
    assert "app.py" not in scoped_staged["patch"]
    assert call(service, "show_changes", {"path": "../outside"})["isError"] is True


def test_show_changes_truncates_an_oversized_patch(
    service: TunnelMcpService,
    repo: Path,
) -> None:
    target = repo / "large.txt"
    target.write_text(
        "".join(f"before {index:05d} " + ("a" * 80) + "\n" for index in range(4_000))
    )
    commit_all(repo, "large fixture")
    target.write_text(
        "".join(f"after {index:05d} " + ("b" * 80) + "\n" for index in range(4_000))
    )
    full_patch = git(repo, "diff", "--", "large.txt")

    changes = content(
        call(service, "show_changes", {"include_patch": True, "path": "large.txt"})
    )

    assert changes["patch_truncated"] is True
    assert len(changes["patch"]) <= 60_000
    assert len(changes["patch"]) < len(full_patch)
    assert "large.txt" in changes["patch"]


def test_git_summaries_withhold_sensitive_paths(
    service: TunnelMcpService,
    repo: Path,
) -> None:
    for name in (".env", ".env.local", "visible.txt", "staged.txt"):
        (repo / name).write_text("before\n")
    commit_all(repo, "Git summary fixture")
    (repo / ".env").write_text("secret after\n")
    (repo / ".env.local").write_text("secret staged\n")
    (repo / "visible.txt").write_text("visible after\n")
    (repo / "staged.txt").write_text("visible staged\n")
    (repo / ".env.untracked").write_text("secret untracked\n")
    git(repo, "add", ".env.local", "staged.txt")

    overview = content(call(service, "project_overview"))
    changes = content(call(service, "show_changes", {"include_patch": True}))
    staged = content(
        call(service, "show_changes", {"include_patch": True, "staged": True})
    )

    for model_visible in (
        overview["git_status"],
        changes["status"],
        changes["unstaged_stat"],
        changes["staged_stat"],
        changes["patch"],
        staged["patch"],
    ):
        assert ".env" not in model_visible
    assert "visible.txt" in changes["status"]
    assert "visible.txt" in changes["unstaged_stat"]
    assert "visible.txt" in changes["patch"]
    assert "staged.txt" in changes["staged_stat"]
    assert "staged.txt" in staged["patch"]


def test_git_summaries_withhold_sensitive_paths_with_adversarial_names(
    service: TunnelMcpService,
    repo: Path,
) -> None:
    status_parent = repo / "arrow -> parent\n"
    patch_parent = repo / 'quote" parent'
    status_parent.mkdir()
    patch_parent.mkdir()
    status_secret = status_parent / ".env"
    patch_secret = patch_parent / ".env"
    status_secret.write_text("status before\n")
    patch_secret.write_text("patch before\n")
    (repo / "rename source.txt").write_text("rename before\n")
    (repo / "visible old.txt").write_text("visible rename\n")
    (repo / " visible edge.txt").write_text("edge before\n")
    commit_all(repo, "Adversarial Git path fixture")

    status_secret.write_text("status secret after\n")
    (repo / " visible edge.txt").write_text("edge after\n")
    git(repo, "update-index", "--chmod=+x", "--", 'quote" parent/.env')
    renamed_secret = repo / "rename -> parent" / ".env"
    renamed_secret.parent.mkdir()
    git(repo, "mv", "--", "rename source.txt", "rename -> parent/.env")
    git(repo, "mv", "--", "visible old.txt", "visible -> new.txt")

    overview = content(call(service, "project_overview"))
    unstaged = content(call(service, "show_changes", {"include_patch": True}))
    staged = content(
        call(service, "show_changes", {"include_patch": True, "staged": True})
    )

    for model_visible in (
        overview["git_status"],
        unstaged["status"],
        unstaged["unstaged_stat"],
        unstaged["staged_stat"],
        unstaged["patch"],
        staged["patch"],
    ):
        assert ".env" not in model_visible
        assert "status secret after" not in model_visible
        assert "rename before" not in model_visible
        assert "arrow -> parent" not in model_visible
        assert 'quote" parent' not in model_visible
        assert "rename -> parent" not in model_visible
    assert 'visible old.txt -> "visible -> new.txt"' in staged["status"]
    assert ' M " visible edge.txt"' in unstaged["status"]
    assert "+edge after" in unstaged["patch"]
    assert "rename from visible old.txt" in staged["patch"]
    assert "rename to visible -> new.txt" in staged["patch"]


def test_non_git_projects_report_truthfully(service: TunnelMcpService) -> None:
    result = call(service, "show_changes")
    assert result["isError"] is True
    assert "not a Git repository" in content(result)["error"]
    assert (
        "not a Git repository"
        in content(call(service, "project_overview"))["git_status"]
    )


def test_git_discovery_never_climbs_above_the_project_root(tmp_path: Path) -> None:
    parent = tmp_path / "desktop"
    child = parent / "notes"
    child.mkdir(parents=True)
    (child / "doc.md").write_text("notes\n")
    git(parent, "init", "-q")
    commit_all(parent)
    (child / "doc.md").write_text("changed\n")
    registry = write_registry(
        tmp_path / "r.json",
        [{"id": "notes", "root": str(child), "writable": False}],
    )
    service = TunnelMcpService(
        lambda: ComputerUseSettings(),
        registry=registry,
        runtime_root=tmp_path / "rt",
    )

    result = call(service, "show_changes", project="notes")

    assert result["isError"] is True
    assert "not a Git repository" in content(result)["error"]


# Lock scope -------------------------------------------------------------------


def test_project_reads_and_git_observation_are_lock_free(
    service: TunnelMcpService,
    registry: ProjectRegistry,
    workspace: Path,
) -> None:
    git(workspace, "init", "-q")
    commit_all(workspace)
    main = registry.resolve("main")
    lock = service._project_lock(main)
    results: dict[str, dict] = {}

    def run(name: str, tool: str, arguments: dict, project: str) -> None:
        results[name] = call(service, tool, arguments, project=project)

    with lock:
        other = threading.Thread(
            target=run,
            args=("ref", "read_files", {"files": [{"path": "app.py"}]}, "ref"),
        )
        observation = threading.Thread(
            target=run, args=("changes", "show_changes", {}, "main")
        )
        reader = threading.Thread(
            target=run,
            args=("main", "read_files", {"files": [{"path": "app.py"}]}, "main"),
        )
        for thread in (other, observation, reader):
            thread.start()
        other.join(timeout=10)
        observation.join(timeout=10)
        reader.join(timeout=10)
        assert results["ref"]["isError"] is False
        assert results["changes"]["isError"] is False
        assert results["main"]["isError"] is False
    assert results["main"]["isError"] is False


# Route -------------------------------------------------------------------------


@pytest.fixture
def mcp_client(workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        "AGENTIC_CONTEXT_SETTINGS_PATH", str(tmp_path / "settings" / "settings.json")
    )
    with patch(
        "app.core.computer_use_agent.load_computer_use_settings",
        return_value=ComputerUseSettings(workspace_path=str(workspace)),
    ):
        application = create_app()
    application.config.update(TESTING=True)
    application.extensions["tunnel_runtime"]._authorization = "Bearer test-token"
    with application.test_client() as client:
        yield client


def test_mcp_route_requires_the_tunnel_bearer_token(mcp_client) -> None:
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert mcp_client.post("/mcp", json=request).status_code == 401
    assert (
        mcp_client.post(
            "/mcp", json=request, headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )
    response = mcp_client.post(
        "/mcp", json=request, headers={"Authorization": "Bearer test-token"}
    )
    assert response.status_code == 200
    assert len(response.get_json()["result"]["tools"]) == len(TUNNEL_TOOLS)
    notification = mcp_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert notification.status_code == 202
    assert mcp_client.get("/mcp").status_code == 405


def test_chatgpt_mcp_route_rejects_oversized_body_before_json_parsing(mcp_client) -> None:
    response = mcp_client.post(
        "/mcp",
        data=b" " * (MAX_MCP_BODY_BYTES + 1),
        content_type="application/json",
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 413
    assert response.get_json()["error"]["message"] == "Request is too large."
    assert mcp_client.application.extensions["tunnel_mcp_service"].activity_snapshot()[
        "call_count"
    ] == 0


def test_mcp_route_rejects_non_loopback_callers(mcp_client) -> None:
    response = mcp_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": "Bearer test-token"},
        environ_base={"REMOTE_ADDR": "192.168.1.20"},
    )
    assert response.status_code in {401, 403}


def test_tunnel_project_registration_type_is_immutable() -> None:
    project = TunnelProject("main", Path(sys.prefix), True)
    with pytest.raises(AttributeError):
        project.writable = False  # type: ignore[misc]
