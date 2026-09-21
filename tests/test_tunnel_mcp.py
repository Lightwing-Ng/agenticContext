"""Tunnel MCP adapter and /mcp route tests.

Code version: v2.3.2-codex.0
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.agent.capability_registry import capability_for_action
from app.core.computer_use_agent import ComputerUseSettings
from app.core.tunnel_mcp import (
    MCP_STATELESS_PROTOCOL_VERSION,
    SERVER_INSTRUCTIONS,
    TUNNEL_TOOLS,
    TUNNEL_TOOLS_BY_NAME,
    TunnelMcpService,
    tunnel_tool_definitions,
)
from app.core.tunnel_projects import (
    ProjectRegistry,
    TunnelProject,
    parse_project_registry,
)
from app.web.app import create_app

EXPECTED_TOOLS = [
    "project_overview",
    "list_files",
    "search_files",
    "read_files",
    "apply_edits",
    "write_file",
    "delete_file",
    "run_check",
    "show_changes",
    "review_changes",
]


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
    return rpc(service, "tools/call", {"name": name, "arguments": arguments})["result"]


def content(result: dict) -> dict:
    return result["structuredContent"]


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
        assert schema["required"][0] == "project", name
    assert tools["delete_file"]["inputSchema"]["required"] == [
        "project",
        "path",
        "expected_sha256",
    ]
    assert tools["read_files"]["inputSchema"]["required"] == ["project", "files"]
    assert set(tools["show_changes"]["inputSchema"]["properties"]) == {
        "project",
        "path",
        "include_patch",
        "staged",
    }
    assert tools["read_files"]["annotations"]["readOnlyHint"] is True
    assert tools["show_changes"]["annotations"]["readOnlyHint"] is True
    assert tools["write_file"]["annotations"]["destructiveHint"] is True
    assert tools["delete_file"]["annotations"]["destructiveHint"] is True
    for mutating in ("apply_edits", "write_file", "delete_file", "run_check"):
        assert tools[mutating]["annotations"]["readOnlyHint"] is False
    public_text = f"{SERVER_INSTRUCTIONS}\n{json.dumps(list(tools.values()))}"
    for removed in (
        "read_file",
        "replace_in_file",
        "create_file",
        "call_runtime_tool",
        "list_projects",
        "start_check",
        "observe_check",
        "stop_check",
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
        "start_check",
        "observe_check",
        "stop_check",
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
    assert "no longer matches" in content(result)["error"]
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
    assert content(ordered)["files"] == [
        {
            "path": "seq.txt",
            "replacements": 2,
            "sha256": content(ordered)["files"][0]["sha256"],
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


# Explicit project registry -----------------------------------------------------


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


def test_projects_do_not_block_each_other_and_git_observation_is_lock_free(
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
        blocked = threading.Thread(
            target=run,
            args=("main", "read_files", {"files": [{"path": "app.py"}]}, "main"),
        )
        for thread in (other, observation, blocked):
            thread.start()
        other.join(timeout=10)
        observation.join(timeout=10)
        blocked.join(timeout=0.3)
        assert results["ref"]["isError"] is False
        assert results["changes"]["isError"] is False
        assert "main" not in results
    blocked.join(timeout=10)
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
