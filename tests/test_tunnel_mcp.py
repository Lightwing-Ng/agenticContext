"""Tunnel MCP adapter and /mcp route tests.

Code version: v2.0.0-claude.0
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core import tunnel_checks
from app.core.computer_use_agent import ComputerUseSettings
from app.core.tunnel_checks import TunnelCheckStore
from app.core.tunnel_mcp import (
    MCP_STATELESS_PROTOCOL_VERSION,
    MAX_TOOL_TEXT_CHARACTERS,
    TUNNEL_TOOLS,
    TunnelMcpService,
    tunnel_tool_definitions,
)
from app.core.tunnel_projects import ProjectRegistry, TunnelProject, parse_project_registry
from app.web.app import create_app

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX process groups and shell scripts")

EXPECTED_TOOLS = [
    "list_projects",
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
    "git_log",
    "git_diff_hunks",
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
            {"id": "ref", "root": str(reference), "writable": False, "description": "Reference."},
        ],
    )


@pytest.fixture
def service(registry: ProjectRegistry, tmp_path: Path) -> TunnelMcpService:
    settings = ComputerUseSettings(workspace_path=str(tmp_path))
    return TunnelMcpService(lambda: settings, registry=registry, runtime_root=tmp_path / "runtime")


def rpc(service: TunnelMcpService, method: str, params: dict | None = None, **headers):
    status, body = service.handle(
        {"jsonrpc": "2.0", "id": 7, "method": method, "params": params or {}},
        headers,
    )
    assert status == 200
    return body


def call(service: TunnelMcpService, name: str, arguments: dict | None = None, project: str | None = "main") -> dict:
    arguments = dict(arguments or {})
    if project is not None and name != "list_projects":
        arguments.setdefault("project", project)
    return rpc(service, "tools/call", {"name": name, "arguments": arguments})["result"]


def content(result: dict) -> dict:
    return result["structuredContent"]


# Protocol ---------------------------------------------------------------------


def test_initialize_negotiates_supported_versions(service: TunnelMcpService) -> None:
    chatgpt = rpc(service, "initialize", {"protocolVersion": "2025-11-25"})["result"]
    assert chatgpt["protocolVersion"] == "2025-11-25"
    assert chatgpt["capabilities"] == {"tools": {"listChanged": False}}
    assert chatgpt["serverInfo"]["name"] == "AgenticContext"
    unknown = rpc(service, "initialize", {"protocolVersion": "1999-01-01"})["result"]
    assert unknown["protocolVersion"] == "2025-06-18"


def test_notifications_and_batches_follow_json_rpc(service: TunnelMcpService) -> None:
    assert service.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, {}) == (202, None)
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
    assert body["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "AgenticContext"


def test_tool_list_is_closed_and_project_scoped() -> None:
    tools = {tool["name"]: tool for tool in tunnel_tool_definitions()}
    assert list(tools) == EXPECTED_TOOLS
    assert [tool.name for tool in TUNNEL_TOOLS] == EXPECTED_TOOLS
    for name, tool in tools.items():
        schema = tool["inputSchema"]
        assert "action" not in schema.get("properties", {})
        assert schema["additionalProperties"] is False
        if name == "list_projects":
            assert "project" not in schema["properties"]
        else:
            assert schema["required"][0] == "project", name
    assert tools["delete_file"]["inputSchema"]["required"] == ["project", "path", "expected_sha256"]
    assert tools["read_files"]["inputSchema"]["required"] == ["project", "files"]
    assert tools["start_check"]["inputSchema"]["required"] == ["project", "command", "idempotency_key"]
    assert "include_patch" not in tools["show_changes"]["inputSchema"]["properties"]
    assert tools["read_files"]["annotations"]["readOnlyHint"] is True
    assert tools["git_diff_hunks"]["annotations"]["readOnlyHint"] is True
    assert tools["write_file"]["annotations"]["destructiveHint"] is True
    assert tools["delete_file"]["annotations"]["destructiveHint"] is True
    for mutating in ("apply_edits", "write_file", "delete_file", "run_check", "start_check", "stop_check"):
        assert tools[mutating]["annotations"]["readOnlyHint"] is False


def test_every_listed_tool_is_dispatchable_by_its_published_schema(
    service: TunnelMcpService,
) -> None:
    listed = [tool["name"] for tool in rpc(service, "tools/list")["result"]["tools"]]
    assert listed == [tool.name for tool in TUNNEL_TOOLS]
    for name in listed:
        body = rpc(service, "tools/call", {"name": name, "arguments": {}})
        # A tool may refuse empty arguments, but only as a tool result, never as an
        # unknown-tool protocol error.
        assert "result" in body, (name, body)


def test_unknown_semantic_fields_fail_instead_of_being_ignored(service: TunnelMcpService) -> None:
    extra = call(service, "read_files", {"files": [{"path": "app.py", "mode": "raw"}]})
    assert extra["isError"] is True
    assert "unsupported field(s): mode" in content(extra)["error"]
    legacy = call(service, "show_changes", {"include_patch": True})
    assert legacy["isError"] is True
    assert "include_patch" in content(legacy)["error"]
    wrong_type = call(service, "apply_edits", {"edits": [{"path": "app.py", "old_text": "hi", "new_text": "x", "replace_all": "yes"}]})
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
            {"runtime", "runtime_name", "execution_mode", "runtime_gateway",
             "adaptive_runtime", "full_operator_runtime"}
        ), tool["name"]

    plain = call(service, "read_files", {"files": [{"path": "app.py"}]})
    wrapped = call(service, "read_files", {"files": [{"path": "app.py"}], **selector})
    assert plain["isError"] is False
    assert wrapped["structuredContent"] == plain["structuredContent"]
    assert "adaptive_runtime" not in wrapped["content"][0]["text"]


@pytest.mark.parametrize(
    "name",
    ["read_file", "replace_in_file", "create_file", "call_runtime_tool", "select_project", "run_process"],
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

    read = call(service, "read_files", {"files": [{"path": "app.py"}, {"path": "AGENTS.md"}]})
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

    smuggled = rpc(service, "tools/call", {"name": "delete_file", "arguments": {"project": "main", "path": "x", "action": "write"}})
    assert smuggled["error"]["code"] == -32602

    activity = service.activity_snapshot()
    assert activity["call_count"] == 5
    assert activity["recent_calls"][0]["tool"] == "run_check"
    assert activity["recent_calls"][0]["target"].startswith("main: ")


def test_apply_edits_is_all_or_nothing(service: TunnelMcpService, workspace: Path) -> None:
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
                {"path": "lib.py", "old_text": "value = 1", "new_text": "value = 2", "replace_all": True},
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
    secret = call(service, "write_file", {"path": ".git/config", "content": "x"})
    assert secret["isError"] is True
    credential = call(service, "write_file", {"path": ".env", "content": "TOKEN=x"})
    assert credential["isError"] is True
    assert not (workspace / ".env").exists()


def test_delete_file_rejects_stale_sha_and_accepts_current_sha(
    service: TunnelMcpService,
    workspace: Path,
) -> None:
    target = workspace / "delete-me.txt"
    target.write_text("delete me\n")

    stale = call(
        service,
        "delete_file",
        {"path": "delete-me.txt", "expected_sha256": "0" * 64},
    )
    assert stale["isError"] is True
    assert "does not match" in content(stale)["error"]
    assert target.read_text() == "delete me\n"

    current = call(service, "read_files", {"files": [{"path": "delete-me.txt"}]})
    sha = content(current)["files"][0]["sha256"]
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
    sha = content(call(service, "read_files", {"files": [{"path": "obsolete.txt"}]}))["files"][0]["sha256"]
    edited = call(
        service,
        "apply_edits",
        {"edits": [{"path": "app.py", "old_text": "hi", "new_text": "hello"}]},
    )
    assert edited["isError"] is False

    deleted = call(service, "delete_file", {"path": "obsolete.txt", "expected_sha256": sha})

    assert deleted["isError"] is False, content(deleted)
    assert not target.exists()


def test_delete_file_rejects_a_file_changed_after_it_was_read(
    service: TunnelMcpService,
    workspace: Path,
) -> None:
    target = workspace / "shared.txt"
    target.write_text("original\n")
    stale_sha = content(call(service, "read_files", {"files": [{"path": "shared.txt"}]}))["files"][0]["sha256"]
    target.write_text("changed by the user\n")
    call(service, "write_file", {"path": "unrelated.txt", "content": "x\n"})

    refused = call(service, "delete_file", {"path": "shared.txt", "expected_sha256": stale_sha})

    assert refused["isError"] is True
    assert target.read_text() == "changed by the user\n"


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/hosts", ".git/config", "~/.ssh/id_rsa"])
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
    git(workspace, "init", "-q")
    commit_all(workspace)
    (workspace / "user_notes.txt").write_text("pre-existing user change\n")
    call(service, "apply_edits", {"edits": [{"path": "app.py", "old_text": "hi", "new_text": "hey"}]})
    before = git(workspace, "status", "--porcelain")

    unverified = call(service, "review_changes")

    assert unverified["isError"] is True
    assert "verification" in content(unverified)["error"]
    status = call(service, "run_check", {"command": "git status"})
    assert status["isError"] is False
    assert "user_notes.txt" in content(status)["output"]
    assert git(workspace, "status", "--porcelain") == before
    assert (workspace / "user_notes.txt").read_text() == "pre-existing user change\n"


# Phase 1: explicit project registry --------------------------------------------


def test_list_projects_returns_identities_without_host_paths(
    service: TunnelMcpService,
    workspace: Path,
) -> None:
    listed = call(service, "list_projects")
    assert listed["isError"] is False
    assert content(listed)["projects"] == [
        {"id": "main", "writable": True, "git": False},
        {"id": "ref", "writable": False, "description": "Reference.", "git": False},
    ]
    assert str(workspace.parent) not in listed["content"][0]["text"]


@pytest.mark.parametrize("project", ["MAIN", "main ", "../project", "project", "/tmp", "", "unknown"])
def test_project_ids_resolve_exactly_and_are_never_paths(
    service: TunnelMcpService,
    project: str,
) -> None:
    result = call(service, "read_files", {"files": [{"path": "app.py"}]}, project=project)
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
    sha = content(call(service, "read_files", {"files": [{"path": "app.py"}]}, project="ref"))["files"][0]["sha256"]
    attempts = [
        ("apply_edits", {"edits": [{"path": "app.py", "old_text": "True", "new_text": "False"}]}),
        ("write_file", {"path": "new.py", "content": "x\n"}),
        ("write_file", {"path": "app.py", "content": "x\n", "expected_sha256": sha}),
        ("delete_file", {"path": "app.py", "expected_sha256": sha}),
        ("run_check", {"command": "git status"}),
        ("start_check", {"command": "scripts/test.sh", "idempotency_key": "read-only-01"}),
        ("stop_check", {"job_id": "0" * 32}),
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
    assert call(service, "search_files", {"query": "reference"}, project="ref")["isError"] is False
    assert call(service, "list_files", {"path": "."}, project="ref")["isError"] is False


def test_paths_cannot_cross_project_boundaries(
    service: TunnelMcpService,
    workspace: Path,
    reference: Path,
) -> None:
    relative_escape = call(service, "read_files", {"files": [{"path": "../reference/app.py"}]})
    assert content(relative_escape)["files"][0]["ok"] is False
    for absolute in (str(reference / "app.py"), str(workspace / "app.py")):
        result = call(service, "read_files", {"files": [{"path": absolute}]})
        assert result["isError"] is True
        assert "relative to the project root" in content(result)["error"]
    edit = call(
        service,
        "apply_edits",
        {"edits": [{"path": "../reference/app.py", "old_text": "True", "new_text": "False"}]},
    )
    assert edit["isError"] is True
    write = call(service, "write_file", {"path": str(reference / "planted.py"), "content": "x"})
    assert write["isError"] is True
    assert (reference / "app.py").read_text() == "reference = True\n"
    assert not (reference / "planted.py").exists()


def test_symlinked_escape_stays_refused(service: TunnelMcpService, workspace: Path, reference: Path) -> None:
    (workspace / "linked").symlink_to(reference, target_is_directory=True)
    result = call(service, "read_files", {"files": [{"path": "linked/app.py"}]})
    assert content(result)["files"][0]["ok"] is False
    write = call(service, "write_file", {"path": "linked/new.py", "content": "x"})
    assert write["isError"] is True
    assert not (reference / "new.py").exists()


def test_instruction_discovery_is_scoped_to_the_selected_project(tmp_path: Path) -> None:
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
    service = TunnelMcpService(lambda: ComputerUseSettings(workspace_path=str(desktop)), registry=registry, runtime_root=tmp_path / "rt")

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
    service = TunnelMcpService(lambda: ComputerUseSettings(), registry=registry, runtime_root=tmp_path / "rt2")
    main_sha = content(call(service, "read_files", {"files": [{"path": "app.py"}]}))["files"][0]["sha256"]
    other_sha = content(call(service, "read_files", {"files": [{"path": "app.py"}]}, project="other"))["files"][0]["sha256"]
    call(service, "apply_edits", {"edits": [{"path": "app.py", "old_text": "1", "new_text": "2"}]}, project="other")

    wrong_project_sha = call(service, "write_file", {"path": "app.py", "content": "x\n", "expected_sha256": other_sha})
    assert wrong_project_sha["isError"] is True
    stale_other = call(service, "delete_file", {"path": "app.py", "expected_sha256": other_sha}, project="other")
    assert stale_other["isError"] is True
    assert (other / "app.py").read_text() == "other = 2\n"

    deleted = call(service, "delete_file", {"path": "app.py", "expected_sha256": main_sha})
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
    registry = write_registry(path, [{"id": "work", "root": str(first), "writable": True}])
    service = TunnelMcpService(lambda: ComputerUseSettings(), registry=registry, runtime_root=tmp_path / "rt")
    first_sha = content(call(service, "read_files", {"files": [{"path": "note.txt"}]}, project="work"))["files"][0]["sha256"]

    time.sleep(0.01)
    write_registry(path, [{"id": "work", "root": str(second), "writable": True}])
    reread = content(call(service, "read_files", {"files": [{"path": "note.txt"}]}, project="work"))
    assert reread["files"][0]["content"] == "1: second"
    stale = call(service, "delete_file", {"path": "note.txt", "expected_sha256": first_sha}, project="work")
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
    listed = call(service, "list_projects")
    assert listed["isError"] is True
    assert "No project is registered" in content(listed)["error"]
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
    assert content(call(service, "list_projects"))["projects"] == [
        {"id": "solo", "writable": True, "description": "The Agent's selected project.", "git": True}
    ]
    assert call(service, "project_overview", project="solo")["isError"] is False


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload, tmp: payload["projects"].append({"id": "nested", "root": str(tmp / "project" / "sub"), "writable": False}), "overlap"),
        (lambda payload, tmp: payload["projects"].append({"id": "Main", "root": str(tmp / "reference"), "writable": False}), "twice"),
        (lambda payload, tmp: payload["projects"][0].update({"shell": True}), "unknown fields"),
        (lambda payload, tmp: payload["projects"][0].update({"root": "relative/path"}), "absolute"),
        (lambda payload, tmp: payload["projects"][0].update({"root": str(Path.home())}), "too broad"),
        (lambda payload, tmp: payload["projects"][0].update({"writable": "yes"}), "true or false"),
        (lambda payload, tmp: payload["projects"][0].update({"id": "../main"}), "must start with a letter"),
        (lambda payload, tmp: payload.update({"schema_version": 2}), "schema_version"),
    ],
)
def test_invalid_registries_fail_closed(tmp_path: Path, workspace: Path, reference: Path, mutate, message: str) -> None:
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
    service = TunnelMcpService(lambda: ComputerUseSettings(workspace_path=str(workspace)), registry=ProjectRegistry(path), runtime_root=tmp_path / "rt")
    assert call(service, "read_files", {"files": [{"path": "app.py"}]})["isError"] is True
    assert call(service, "list_projects")["isError"] is True


# Phase 2: paginated Git inspection ---------------------------------------------


@pytest.fixture
def repo(workspace: Path) -> Path:
    git(workspace, "init", "-q")
    commit_all(workspace)
    return workspace


def collect_pages(service: TunnelMcpService, arguments: dict, project: str = "main") -> tuple[list[dict], list[dict]]:
    pages = [content(call(service, "git_diff_hunks", arguments, project=project))]
    while not pages[-1]["complete"]:
        assert pages[-1]["ok"] is True, pages[-1]
        follow = {"continuation": pages[-1]["continuation"]}
        for key in ("max_hunks", "max_hunk_lines"):
            if key in arguments:
                follow[key] = arguments[key]
        pages.append(content(call(service, "git_diff_hunks", follow, project=project)))
        assert len(pages) < 200
    return pages, [item for page in pages for item in page["items"]]


def test_show_changes_summarizes_without_a_patch(service: TunnelMcpService, repo: Path) -> None:
    (repo / "app.py").write_text('print("changed")\n')
    (repo / "AGENTS.md").write_text("# Rules\n\nStaged.\n")
    git(repo, "add", "AGENTS.md")
    (repo / "draft.py").write_text("x = 1\n")
    changes = content(call(service, "show_changes"))
    assert " M app.py" in changes["status"]
    assert "M  AGENTS.md" in changes["status"]
    assert "?? draft.py" in changes["status"]
    assert "app.py" in changes["unstaged_stat"]
    assert "AGENTS.md" in changes["staged_stat"]
    assert "patch" not in changes
    scoped = content(call(service, "show_changes", {"path": "AGENTS.md"}))
    assert "app.py" not in scoped["unstaged_stat"]
    assert call(service, "show_changes", {"path": "../outside"})["isError"] is True


def test_small_diff_is_one_complete_page(service: TunnelMcpService, repo: Path) -> None:
    (repo / "app.py").write_text('print("changed")\n')
    page = content(call(service, "git_diff_hunks"))
    assert page["complete"] is True
    assert "continuation" not in page
    assert page["files"] == 1 and page["hunks"] == 1
    assert page["items"][0]["path"] == "app.py"
    assert page["items"][0]["header"].startswith("@@ -1 +1 @@")
    assert '+print("changed")' in page["items"][0]["lines"]
    assert '-print("hi")' in page["items"][0]["lines"]


def test_large_diffs_are_fully_recoverable_through_continuation(service: TunnelMcpService, repo: Path) -> None:
    for index in range(6):
        (repo / f"module_{index}.py").write_text("".join(f"line {n}\n" for n in range(200)))
    commit_all(repo, "modules")
    for index in range(6):
        lines = [f"line {n}\n" for n in range(200)]
        for n in range(5, 200, 20):
            lines[n] = f"changed {index} {n}\n"
        (repo / f"module_{index}.py").write_text("".join(lines))
    full = git(repo, "diff")

    pages, items = collect_pages(service, {"max_hunks": 4})

    assert len(pages) > 2
    assert pages[0]["hunks"] == len(items) == full.count("\n@@ ")
    assert all(len(page["items"]) <= 4 for page in pages)
    added = [line for item in items for line in item["lines"].splitlines() if line.startswith("+")]
    assert added == [line for line in full.splitlines() if line.startswith("+") and not line.startswith("+++")]
    ranges = [(page["range"]["first_hunk"], page["range"]["last_hunk"]) for page in pages]
    assert ranges[0][0] == 1 and ranges[-1][1] == pages[0]["hunks"]
    assert all(later[0] == earlier[1] + 1 for earlier, later in zip(ranges, ranges[1:]))


def test_long_hunks_continue_in_line_segments(service: TunnelMcpService, repo: Path) -> None:
    (repo / "big.txt").write_text("".join(f"row {n}\n" for n in range(450)))
    git(repo, "add", "-N", "big.txt")
    pages, items = collect_pages(service, {"max_hunk_lines": 100})
    assert [item["segment"]["first_line"] for item in items] == [1, 101, 201, 301, 401]
    assert items[-1]["segment"]["last_line"] == items[-1]["segment"]["total_lines"] == 450
    lines = [line for item in items for line in item["lines"].splitlines()]
    assert lines[0] == "+row 0" and lines[-1] == "+row 449"
    assert items[0]["status"] == "added"
    assert "status" not in items[1]


def test_staged_unstaged_and_path_scoped_diffs(service: TunnelMcpService, repo: Path) -> None:
    (repo / "app.py").write_text('print("unstaged")\n')
    (repo / "AGENTS.md").write_text("# Rules\n\nStaged.\n")
    git(repo, "add", "AGENTS.md")
    unstaged = content(call(service, "git_diff_hunks"))
    assert [item["path"] for item in unstaged["items"]] == ["app.py"]
    staged = content(call(service, "git_diff_hunks", {"staged": True}))
    assert [item["path"] for item in staged["items"]] == ["AGENTS.md"]
    assert "+Staged." in staged["items"][0]["lines"]
    scoped = content(call(service, "git_diff_hunks", {"paths": ["AGENTS.md"]}))
    assert scoped["items"] == [] and scoped["complete"] is True
    both = content(call(service, "git_diff_hunks", {"paths": ["app.py", "AGENTS.md"], "base_commit": "HEAD"}))
    assert sorted(item["path"] for item in both["items"]) == ["AGENTS.md", "app.py"]


def test_commit_ranges_resolve_to_fixed_commits(service: TunnelMcpService, repo: Path) -> None:
    first = git(repo, "rev-parse", "HEAD").strip()
    (repo / "app.py").write_text('print("second")\n')
    commit_all(repo, "second")
    ranged = content(call(service, "git_diff_hunks", {"base_commit": first[:12], "head_commit": "HEAD"}))
    assert '+print("second")' in ranged["items"][0]["lines"]
    for bad in ("HEAD..main", "--output=/tmp/x", "nope"):
        assert call(service, "git_diff_hunks", {"base_commit": bad})["isError"] is True
    assert call(service, "git_diff_hunks", {"head_commit": "HEAD"})["isError"] is True
    assert call(service, "git_diff_hunks", {"base_commit": first, "head_commit": "HEAD", "staged": True})["isError"] is True


def test_continuation_fails_when_the_diff_changes(service: TunnelMcpService, repo: Path) -> None:
    for index in range(3):
        (repo / f"f{index}.txt").write_text(f"value {index}\n")
    git(repo, "add", "-N", ".")
    first = content(call(service, "git_diff_hunks", {"max_hunks": 1}))
    assert first["complete"] is False
    (repo / "f2.txt").write_text("edited meanwhile\n")
    resumed = call(service, "git_diff_hunks", {"continuation": first["continuation"]})
    assert resumed["isError"] is True
    assert "diff changed" in content(resumed)["error"]


def test_continuations_are_bound_to_project_and_request(
    service: TunnelMcpService,
    repo: Path,
    tmp_path: Path,
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    for root in (repo, other):
        for index in range(3):
            (root / f"f{index}.txt").write_text(f"value {index}\n")
    git(other, "init", "-q")
    git(repo, "add", "-N", ".")
    registry = write_registry(
        tmp_path / "pair.json",
        [
            {"id": "main", "root": str(repo), "writable": True},
            {"id": "other", "root": str(other), "writable": False},
        ],
    )
    service = TunnelMcpService(lambda: ComputerUseSettings(), registry=registry, runtime_root=tmp_path / "rt")
    first = content(call(service, "git_diff_hunks", {"max_hunks": 1, "base_commit": "HEAD"}))
    token = first["continuation"]

    moved = call(service, "git_diff_hunks", {"continuation": token}, project="other")
    assert moved["isError"] is True
    assert "different project" in content(moved)["error"]
    mixed = call(service, "git_diff_hunks", {"continuation": token, "staged": True})
    assert mixed["isError"] is True
    tampered = call(service, "git_diff_hunks", {"continuation": token[:-4] + "0000"})
    assert tampered["isError"] is True
    restarted = TunnelMcpService(lambda: ComputerUseSettings(), registry=registry, runtime_root=tmp_path / "rt")
    expired = call(restarted, "git_diff_hunks", {"continuation": token})
    assert expired["isError"] is True
    assert "expired" in content(expired)["error"]
    assert call(service, "git_diff_hunks", {"continuation": token})["isError"] is False


def test_diff_pages_stay_bounded(service: TunnelMcpService, repo: Path) -> None:
    (repo / "wide.txt").write_text("".join(("x" * 3_000) + f"{n}\n" for n in range(120)))
    (repo / "minified.js").write_text("y" * 50_000 + "\n")
    git(repo, "add", "-N", ".")
    pages, items = collect_pages(service, {"max_hunk_lines": 400, "max_hunks": 50})
    for page in pages:
        assert len(json.dumps(page)) < MAX_TOOL_TEXT_CHARACTERS
        assert len(json.dumps(page["items"])) < 70_000
    assert sum(len(item["lines"].splitlines()) for item in items) == 121
    assert any(page.get("shortened_lines") for page in pages)


def test_binary_and_sensitive_files_are_handled_safely(service: TunnelMcpService, repo: Path) -> None:
    (repo / "image.bin").write_bytes(b"\x00\x01\x02binary")
    (repo / ".env").write_text("TOKEN=before\n")
    commit_all(repo, "assets")
    (repo / "image.bin").write_bytes(b"\x00\x09\x08binary changed")
    (repo / ".env").write_text("TOKEN=secret-value\n")
    (repo / "name with space.txt").write_text("spaced\n")
    git(repo, "add", "name with space.txt")
    page = content(call(service, "git_diff_hunks", {"base_commit": "HEAD"}))
    rendered = json.dumps(page)
    assert "secret-value" not in rendered and ".env" not in rendered
    assert page["withheld_files"] == 1
    binary = [item for item in page["items"] if item["path"] == "image.bin"]
    assert binary == [{"path": "image.bin", "binary": True}]
    assert any(item["path"] == "name with space.txt" and item.get("status") == "added" for item in page["items"])


def test_unmerged_conflicts_are_attributed_to_their_own_file(service: TunnelMcpService, repo: Path) -> None:
    (repo / "notes.txt").write_text("base\n")
    commit_all(repo, "notes")
    git(repo, "checkout", "-q", "-b", "side")
    (repo / "notes.txt").write_text("side\n")
    commit_all(repo, "side")
    git(repo, "checkout", "-q", "-")
    (repo / "notes.txt").write_text("main\n")
    commit_all(repo, "main")
    subprocess.run(["git", "merge", "-q", "side"], cwd=repo, capture_output=True)
    (repo / "app.py").write_text('print("also changed")\n')

    items = content(call(service, "git_diff_hunks"))["items"]

    conflict = [item for item in items if item["path"] == "notes.txt"]
    assert conflict and conflict[0]["status"] == "unmerged"
    assert "<<<<<<<" in conflict[0]["lines"]
    changed = [item for item in items if item["path"] == "app.py"]
    assert changed and "<<<<<<<" not in changed[0]["lines"]


def test_git_log_is_bounded_and_pageable(service: TunnelMcpService, repo: Path) -> None:
    for index in range(4):
        (repo / "app.py").write_text(f"print({index})\n")
        commit_all(repo, f"change {index}")
    page = content(call(service, "git_log", {"limit": 2}))
    assert [commit["subject"] for commit in page["commits"]] == ["change 3", "change 2"]
    assert page["has_more"] is True
    assert set(page["commits"][0]) == {"commit", "date", "author", "subject"}
    last = content(call(service, "git_log", {"limit": 10, "skip": 4}))
    assert [commit["subject"] for commit in last["commits"]] == ["init"]
    assert last["has_more"] is False
    scoped = content(call(service, "git_log", {"path": "AGENTS.md"}))
    assert [commit["subject"] for commit in scoped["commits"]] == ["init"]
    assert call(service, "git_log", {"ref": "--all"})["isError"] is True


def test_non_git_projects_report_truthfully(service: TunnelMcpService, tmp_path: Path) -> None:
    for name, arguments in (("git_log", {}), ("git_diff_hunks", {}), ("show_changes", {})):
        result = call(service, name, arguments)
        assert result["isError"] is True
        assert "not a Git repository" in content(result)["error"]
    assert "not a Git repository" in content(call(service, "project_overview"))["git_status"]


def test_git_discovery_never_climbs_above_the_project_root(tmp_path: Path) -> None:
    parent = tmp_path / "desktop"
    child = parent / "notes"
    child.mkdir(parents=True)
    (child / "doc.md").write_text("notes\n")
    git(parent, "init", "-q")
    commit_all(parent)
    (child / "doc.md").write_text("changed\n")
    registry = write_registry(tmp_path / "r.json", [{"id": "notes", "root": str(child), "writable": False}])
    service = TunnelMcpService(lambda: ComputerUseSettings(), registry=registry, runtime_root=tmp_path / "rt")
    result = call(service, "git_diff_hunks", project="notes")
    assert result["isError"] is True
    assert "not a Git repository" in content(result)["error"]


# Phase 3: durable verification jobs --------------------------------------------


@pytest.fixture
def check_script(workspace: Path) -> Path:
    script = workspace / "scripts" / "test.sh"
    script.parent.mkdir()
    script.write_text('#!/bin/sh\necho "started $1"\nsleep "$1"\necho "finished"\nexit "$2"\n')
    script.chmod(0o755)
    return script


def wait_for_terminal(service: TunnelMcpService, job_id: str, project: str = "main", timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = call(service, "observe_check", {"job_id": job_id}, project=project)
        if content(result)["job"]["state"] not in {"starting", "running"}:
            return result
        time.sleep(0.1)
    raise AssertionError("check did not finish")


def wait_for_result_file(service: TunnelMcpService, job_id: str, timeout: float = 30) -> None:
    job_dirs = list(service._checks.root.glob(f"*/{job_id}"))
    assert len(job_dirs) == 1
    deadline = time.monotonic() + timeout
    while not (job_dirs[0] / "result.json").exists():
        assert time.monotonic() < deadline
        time.sleep(0.05)


@posix_only
def test_long_check_succeeds_and_records_verification(
    service: TunnelMcpService,
    workspace: Path,
    check_script: Path,
) -> None:
    call(service, "apply_edits", {"edits": [{"path": "app.py", "old_text": "hi", "new_text": "hey"}]})
    assert call(service, "review_changes")["isError"] is True
    started = call(service, "start_check", {"command": "scripts/test.sh 1 0", "idempotency_key": "check-success-1"})
    assert started["isError"] is False, content(started)
    job = content(started)["job"]
    assert job["state"] in {"starting", "running"}
    assert content(call(service, "project_overview"))["active_checks"] == [job["job_id"]]

    finished = wait_for_terminal(service, job["job_id"])

    assert finished["isError"] is False, content(finished)
    final = content(finished)["job"]
    assert final["state"] == "succeeded"
    assert final["exit_code"] == 0
    assert final["verification"] == "recorded"
    assert final["workspace_changed"] is False
    assert "finished" in final["output_tail"]
    assert call(service, "review_changes")["isError"] is False
    again = content(call(service, "observe_check", {"job_id": job["job_id"]}))["job"]
    assert again["verification"] == "recorded"


@posix_only
def test_failed_check_reports_exit_code_and_withdraws_verification(
    service: TunnelMcpService,
    check_script: Path,
) -> None:
    call(service, "apply_edits", {"edits": [{"path": "app.py", "old_text": "hi", "new_text": "hey"}]})
    assert call(service, "run_check", {"command": "scripts/test.sh 0 0"})["isError"] is False
    assert content(call(service, "project_overview"))["verification_current"] is True
    started = content(call(service, "start_check", {"command": "scripts/test.sh 0 3", "idempotency_key": "check-fail-01"}))

    finished = wait_for_terminal(service, started["job"]["job_id"])

    assert finished["isError"] is True
    job = content(finished)["job"]
    assert job["state"] == "failed" and job["exit_code"] == 3
    assert job["verification"] == "not_recorded"
    assert content(call(service, "project_overview"))["verification_current"] is False
    assert call(service, "review_changes")["isError"] is True


@pytest.mark.parametrize(
    "command",
    ["rm -rf .", "bash -c 'echo hi'", "curl https://example.com", "python3 -c 'print(1)'", "scripts/test.sh 1 0 > out.txt", "git status"],
)
def test_start_check_refuses_unapproved_commands(
    service: TunnelMcpService,
    check_script: Path,
    command: str,
) -> None:
    result = call(service, "start_check", {"command": command, "idempotency_key": "refused-key-1"})
    assert result["isError"] is True
    assert not service._checks.root.exists() or not list(service._checks.root.glob("*/*/metadata.json"))


@posix_only
def test_start_check_is_idempotent_and_single_flight(
    service: TunnelMcpService,
    check_script: Path,
) -> None:
    first = content(call(service, "start_check", {"command": "scripts/test.sh 3 0", "idempotency_key": "same-key-001"}))
    retry = content(call(service, "start_check", {"command": "scripts/test.sh 3 0", "idempotency_key": "same-key-001"}))
    assert retry["deduplicated"] is True
    assert retry["job"]["job_id"] == first["job"]["job_id"]
    reused = call(service, "start_check", {"command": "scripts/test.sh 1 0", "idempotency_key": "same-key-001"})
    assert reused["isError"] is True and "different check" in content(reused)["error"]
    second = call(service, "start_check", {"command": "scripts/test.sh 1 0", "idempotency_key": "other-key-01"})
    assert second["isError"] is True and "still running" in content(second)["error"]
    assert len(list(service._checks.root.glob("*/*/metadata.json"))) == 1
    call(service, "stop_check", {"job_id": first["job"]["job_id"]})


@posix_only
def test_stop_check_ends_the_owned_process_tree(
    service: TunnelMcpService,
    check_script: Path,
) -> None:
    job_id = content(call(service, "start_check", {"command": "scripts/test.sh 37 0", "idempotency_key": "stop-me-0001"}))["job"]["job_id"]
    deadline = time.monotonic() + 10
    while "started" not in content(call(service, "observe_check", {"job_id": job_id}))["job"]["output_tail"]:
        assert time.monotonic() < deadline
        time.sleep(0.1)

    stopped = call(service, "stop_check", {"job_id": job_id})

    assert stopped["isError"] is False, content(stopped)
    job = content(stopped)["job"]
    assert job["state"] == "stopped"
    assert job["verification"] == "not_recorded"
    assert "finished" not in job["output_tail"]
    survivors = subprocess.run(["pgrep", "-f", "sleep 37"], capture_output=True, text=True).stdout.strip()
    assert survivors == ""


@posix_only
def test_stop_check_never_signals_an_unverified_process(
    service: TunnelMcpService,
    check_script: Path,
) -> None:
    job_id = content(call(service, "start_check", {"command": "scripts/test.sh 30 0", "idempotency_key": "identity-001"}))["job"]["job_id"]
    job_dir = next(service._checks.root.glob(f"*/{job_id}"))
    metadata_path = job_dir / "metadata.json"
    metadata = json.loads(metadata_path.read_text())
    runner_pid = metadata["pid"]
    metadata["process_identity"] = "ps:forged"
    metadata_path.write_text(json.dumps(metadata))
    try:
        result = content(call(service, "stop_check", {"job_id": job_id}))
        assert result["job"]["state"] == "unknown"
        os.kill(runner_pid, 0)  # The real runner was not signaled.
        assert not (job_dir / "result.json").exists()
    finally:
        os.kill(runner_pid, 15)
        deadline = time.monotonic() + 10
        while not (job_dir / "result.json").exists() and time.monotonic() < deadline:
            time.sleep(0.05)


@posix_only
def test_check_timeout_is_enforced(tmp_path: Path, check_script: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tunnel_checks, "MIN_TIMEOUT_SECONDS", 1)
    store = TunnelCheckStore(tmp_path / "rt")
    job_dir, metadata, _ = store.start(
        project_id="main",
        project_root=workspace,
        argv=[str(check_script), "30", "0"],
        command="scripts/test.sh 30 0",
        idempotency_key="timeout-0001",
        timeout_seconds=1,
        evidence={},
    )
    deadline = time.monotonic() + 15
    while store.status(job_dir, metadata)["state"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.1)
    status = store.status(job_dir, metadata)
    assert status["state"] == "timeout"
    assert "limit" in status["message"]


@posix_only
def test_checks_are_isolated_per_project(service: TunnelMcpService, check_script: Path, tmp_path: Path, workspace: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    registry = write_registry(
        tmp_path / "isolated.json",
        [
            {"id": "main", "root": str(workspace), "writable": True},
            {"id": "other", "root": str(other), "writable": True},
        ],
    )
    service = TunnelMcpService(lambda: ComputerUseSettings(), registry=registry, runtime_root=tmp_path / "rt3")
    job_id = content(call(service, "start_check", {"command": "scripts/test.sh 0 0", "idempotency_key": "isolated-01"}))["job"]["job_id"]
    for name in ("observe_check", "stop_check"):
        foreign = call(service, name, {"job_id": job_id}, project="other")
        assert foreign["isError"] is True
        assert "Unknown check job" in content(foreign)["error"]
    assert call(service, "start_check", {"command": "scripts/test.sh 0 0", "idempotency_key": "isolated-02"}, project="other")["isError"] is True
    wait_for_terminal(service, job_id)


@posix_only
def test_workspace_change_during_a_check_is_reported(
    service: TunnelMcpService,
    workspace: Path,
    check_script: Path,
) -> None:
    job_id = content(call(service, "start_check", {"command": "scripts/test.sh 2 0", "idempotency_key": "mutation-01"}))["job"]["job_id"]
    call(service, "apply_edits", {"edits": [{"path": "app.py", "old_text": "hi", "new_text": "changed"}]})

    finished = wait_for_terminal(service, job_id)

    assert finished["isError"] is True
    job = content(finished)["job"]
    assert job["state"] == "succeeded"
    assert job["workspace_changed"] is True
    assert job["verification"] == "not_recorded"
    assert "changed while it ran" in content(finished)["error"]
    assert call(service, "review_changes")["isError"] is True


@posix_only
def test_check_evidence_never_applies_to_a_later_edit(
    service: TunnelMcpService,
    workspace: Path,
    check_script: Path,
) -> None:
    job_id = content(call(service, "start_check", {"command": "scripts/test.sh 0 0", "idempotency_key": "later-edit-1"}))["job"]["job_id"]
    wait_for_result_file(service, job_id)
    call(service, "apply_edits", {"edits": [{"path": "app.py", "old_text": "hi", "new_text": "after"}]})

    observed = content(call(service, "observe_check", {"job_id": job_id}))["job"]

    assert observed["state"] == "succeeded"
    assert observed["verification"] == "not_recorded"
    assert call(service, "review_changes")["isError"] is True


@posix_only
def test_check_state_survives_a_service_restart(
    service: TunnelMcpService,
    registry: ProjectRegistry,
    check_script: Path,
    tmp_path: Path,
) -> None:
    job_id = content(call(service, "start_check", {"command": "scripts/test.sh 1 0", "idempotency_key": "restart-0001"}))["job"]["job_id"]
    restarted = TunnelMcpService(lambda: ComputerUseSettings(), registry=registry, runtime_root=tmp_path / "runtime")
    assert content(call(restarted, "observe_check", {"job_id": job_id}))["job"]["state"] in {"running", "succeeded"}
    retried = content(call(restarted, "start_check", {"command": "scripts/test.sh 1 0", "idempotency_key": "restart-0001"}))
    assert retried["deduplicated"] is True and retried["job"]["job_id"] == job_id

    finished = content(wait_for_terminal(restarted, job_id))["job"]

    assert finished["state"] == "succeeded"
    assert finished["verification"] == "not_recorded"
    assert "restarted" in finished["reason"]


@posix_only
def test_runner_loss_is_unknown_and_its_orphaned_command_can_still_be_stopped(
    service: TunnelMcpService,
    check_script: Path,
) -> None:
    job_id = content(call(service, "start_check", {"command": "scripts/test.sh 41 0", "idempotency_key": "lost-runner-1"}))["job"]["job_id"]
    job_dir = next(service._checks.root.glob(f"*/{job_id}"))
    runner_pid = json.loads((job_dir / "metadata.json").read_text())["pid"]
    deadline = time.monotonic() + 10
    while not (job_dir / "child.json").exists():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    os.killpg(runner_pid, 9)
    deadline = time.monotonic() + 10
    while True:
        observed = call(service, "observe_check", {"job_id": job_id})
        if content(observed)["job"]["state"] != "running":
            break
        assert time.monotonic() < deadline
        time.sleep(0.1)
    assert observed["isError"] is True
    assert content(observed)["job"]["state"] == "unknown"
    assert content(observed)["job"]["verification"] == "not_recorded"
    child_pid = json.loads((job_dir / "child.json").read_text())["pid"]
    os.kill(child_pid, 0)  # The approved command outlived its runner.

    stopped = call(service, "stop_check", {"job_id": job_id})

    assert stopped["isError"] is False, content(stopped)
    assert content(stopped)["job"]["state"] == "stopped"
    assert subprocess.run(["pgrep", "-f", "sleep 41"], capture_output=True, text=True).stdout.strip() == ""


@posix_only
def test_runner_identity_matches_the_compute_job_identity() -> None:
    from app.core.agent.compute_jobs import _process_identity
    from app.core.tunnel_check_runner import _birth_identity

    assert _birth_identity(os.getpid()) == _process_identity(os.getpid()) != ""


# Phase 4: lock scope -------------------------------------------------------------


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
        other = threading.Thread(target=run, args=("ref", "read_files", {"files": [{"path": "app.py"}]}, "ref"))
        log = threading.Thread(target=run, args=("log", "git_log", {}, "main"))
        blocked = threading.Thread(target=run, args=("main", "read_files", {"files": [{"path": "app.py"}]}, "main"))
        for thread in (other, log, blocked):
            thread.start()
        other.join(timeout=10)
        log.join(timeout=10)
        blocked.join(timeout=0.3)
        assert results["ref"]["isError"] is False
        assert results["log"]["isError"] is False
        assert "main" not in results
    blocked.join(timeout=10)
    assert results["main"]["isError"] is False


# Route -------------------------------------------------------------------------


@pytest.fixture
def mcp_client(workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTIC_CONTEXT_SETTINGS_PATH", str(tmp_path / "settings" / "settings.json"))
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
        mcp_client.post("/mcp", json=request, headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )
    response = mcp_client.post("/mcp", json=request, headers={"Authorization": "Bearer test-token"})
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
