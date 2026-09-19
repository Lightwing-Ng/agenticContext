"""Tunnel MCP adapter and /mcp route tests.

Code version: v1.2.0-codex.0
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.computer_use_agent import ComputerUseSettings
from app.core.tunnel_mcp import (
    MCP_STATELESS_PROTOCOL_VERSION,
    TUNNEL_TOOLS,
    TunnelMcpService,
    tunnel_tool_definitions,
)
from app.web.app import create_app


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("# Rules\n")
    (project / "app.py").write_text('print("hi")\n')
    return project


@pytest.fixture
def service(workspace: Path) -> TunnelMcpService:
    settings = ComputerUseSettings(workspace_path=str(workspace))
    return TunnelMcpService(lambda: settings)


def rpc(service: TunnelMcpService, method: str, params: dict | None = None, **headers):
    status, body = service.handle(
        {"jsonrpc": "2.0", "id": 7, "method": method, "params": params or {}},
        headers,
    )
    assert status == 200
    return body


def call(service: TunnelMcpService, name: str, arguments: dict | None = None) -> dict:
    return rpc(service, "tools/call", {"name": name, "arguments": arguments or {}})["result"]


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


def test_tool_list_reuses_registry_schemas_without_the_action_field() -> None:
    expected_names = [
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
    tools = {tool["name"]: tool for tool in tunnel_tool_definitions()}
    assert list(tools) == expected_names
    assert [tool.name for tool in TUNNEL_TOOLS] == expected_names
    for tool in tools.values():
        assert "action" not in tool["inputSchema"].get("properties", {})
        assert "action" not in tool["inputSchema"].get("required", [])
        assert tool["inputSchema"]["additionalProperties"] is False
    assert tools["delete_file"]["inputSchema"]["required"] == ["path", "expected_sha256"]
    assert tools["read_files"]["inputSchema"]["required"] == ["files"]
    assert tools["read_files"]["annotations"]["readOnlyHint"] is True
    assert tools["write_file"]["annotations"]["destructiveHint"] is True
    assert tools["delete_file"]["annotations"]["destructiveHint"] is True


def test_tools_run_through_the_workspace_controller(
    service: TunnelMcpService,
    workspace: Path,
) -> None:
    overview = call(service, "project_overview")
    assert overview["isError"] is False
    assert overview["structuredContent"]["instruction_files"] == ["AGENTS.md"]
    assert overview["structuredContent"]["project"] == "project"

    read = call(service, "read_files", {"files": [{"path": "app.py"}, {"path": "AGENTS.md"}]})
    assert [item["content"] for item in read["structuredContent"]["files"]] == [
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
    assert "inside the selected project" in escaped["structuredContent"]["files"][0]["error"]

    refused = call(service, "run_check", {"command": "rm -rf ."})
    assert refused["isError"] is True

    smuggled = rpc(service, "tools/call", {"name": "delete_file", "arguments": {"path": "x", "action": "write"}})
    assert smuggled["error"]["code"] == -32602

    activity = service.activity_snapshot()
    assert activity["call_count"] == 5
    assert activity["recent_calls"][0]["tool"] == "run_check"


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
    assert "appears 2 times in lib.py" in result["structuredContent"]["error"]
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
    assert [item["replacements"] for item in batch["structuredContent"]["files"]] == [1, 2]
    assert (workspace / "lib.py").read_text() == "value = 2\nvalue = 2\n"


def test_write_file_requires_the_current_sha_to_replace(
    service: TunnelMcpService,
    workspace: Path,
) -> None:
    created = call(service, "write_file", {"path": "pkg/new.py", "content": "x = 1\n"})
    assert created["structuredContent"]["created"] is True
    assert (workspace / "pkg" / "new.py").read_text() == "x = 1\n"
    assert not (workspace / "pkg" / "new.py").stat().st_mode & 0o111

    blind = call(service, "write_file", {"path": "app.py", "content": "replaced\n"})
    assert blind["isError"] is True
    assert "expected_sha256" in blind["structuredContent"]["error"]

    stale = call(
        service,
        "write_file",
        {"path": "app.py", "content": "replaced\n", "expected_sha256": "0" * 64},
    )
    assert stale["isError"] is True

    current = call(service, "read_files", {"files": [{"path": "app.py"}]})
    sha = current["structuredContent"]["files"][0]["sha256"]
    replaced = call(
        service,
        "write_file",
        {"path": "app.py", "content": "replaced\n", "expected_sha256": sha},
    )
    assert replaced["structuredContent"]["created"] is False
    assert (workspace / "app.py").read_text() == "replaced\n"

    outside = call(service, "write_file", {"path": "../escape.py", "content": "x"})
    assert outside["isError"] is True
    secret = call(service, "write_file", {"path": ".git/config", "content": "x"})
    assert secret["isError"] is True


def test_delete_file_rejects_stale_sha_and_accepts_current_sha(
    service: TunnelMcpService,
    workspace: Path,
) -> None:
    target = workspace / "delete-me.txt"
    target.write_text("delete me\n")

    current = call(service, "read_files", {"files": [{"path": "delete-me.txt"}]})
    sha = current["structuredContent"]["files"][0]["sha256"]

    stale = call(
        service,
        "delete_file",
        {"path": "delete-me.txt", "expected_sha256": "0" * 64},
    )
    assert stale["isError"] is True
    assert "does not match" in stale["structuredContent"]["error"]
    assert target.read_text() == "delete me\n"

    current = call(service, "read_files", {"files": [{"path": "delete-me.txt"}]})
    sha = current["structuredContent"]["files"][0]["sha256"]
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
    sha = call(service, "read_files", {"files": [{"path": "obsolete.txt"}]})[
        "structuredContent"
    ]["files"][0]["sha256"]
    edited = call(
        service,
        "apply_edits",
        {"edits": [{"path": "app.py", "old_text": "hi", "new_text": "hello"}]},
    )
    assert edited["isError"] is False

    deleted = call(service, "delete_file", {"path": "obsolete.txt", "expected_sha256": sha})

    assert deleted["isError"] is False, deleted["structuredContent"]
    assert not target.exists()


def test_delete_file_rejects_a_file_changed_after_it_was_read(
    service: TunnelMcpService,
    workspace: Path,
) -> None:
    target = workspace / "shared.txt"
    target.write_text("original\n")
    stale_sha = call(service, "read_files", {"files": [{"path": "shared.txt"}]})[
        "structuredContent"
    ]["files"][0]["sha256"]
    target.write_text("changed by the user\n")
    call(service, "write_file", {"path": "unrelated.txt", "content": "x\n"})

    refused = call(service, "delete_file", {"path": "shared.txt", "expected_sha256": stale_sha})

    assert refused["isError"] is True
    assert target.read_text() == "changed by the user\n"


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/hosts", ".git/config"])
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
    assert ordered["structuredContent"]["files"] == [
        {
            "path": "seq.txt",
            "replacements": 2,
            "sha256": ordered["structuredContent"]["files"][0]["sha256"],
        }
    ]
    assert (workspace / "seq.txt").read_text() == "three\n"


def test_show_changes_reports_git_status_and_patch(
    service: TunnelMcpService,
    workspace: Path,
) -> None:
    assert call(service, "show_changes")["isError"] is True
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=workspace,
        check=True,
    )
    (workspace / "app.py").write_text('print("changed")\n')
    (workspace / "AGENTS.md").write_text("# Rules\n\nStaged.\n")
    subprocess.run(["git", "add", "AGENTS.md"], cwd=workspace, check=True)
    (workspace / "draft.py").write_text("x = 1\n")
    changes = call(service, "show_changes", {"include_patch": True})["structuredContent"]
    assert " M app.py" in changes["status"]
    assert "M  AGENTS.md" in changes["status"]
    assert "?? draft.py" in changes["status"]
    assert "app.py" in changes["unstaged_stat"]
    assert "AGENTS.md" in changes["staged_stat"]
    assert '+print("changed")' in changes["patch"]
    assert "Staged." not in changes["patch"]

    staged = call(service, "show_changes", {"include_patch": True, "staged": True})
    assert "+Staged." in staged["structuredContent"]["patch"]
    assert "app.py" not in staged["structuredContent"]["patch"]

    scoped = call(service, "show_changes", {"include_patch": True, "path": "AGENTS.md"})
    assert scoped["structuredContent"]["patch"] == ""
    assert "app.py" not in scoped["structuredContent"]["unstaged_stat"]
    assert call(service, "show_changes", {"path": "../outside"})["isError"] is True


def test_review_changes_requires_current_verification_and_leaves_the_tree_intact(
    service: TunnelMcpService,
    workspace: Path,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=workspace,
        check=True,
    )
    (workspace / "user_notes.txt").write_text("pre-existing user change\n")
    call(service, "apply_edits", {"edits": [{"path": "app.py", "old_text": "hi", "new_text": "hey"}]})
    before = subprocess.run(
        ["git", "status", "--porcelain"], cwd=workspace, capture_output=True, text=True, check=True
    ).stdout

    unverified = call(service, "review_changes")

    assert unverified["isError"] is True
    assert "verification" in unverified["structuredContent"]["error"]
    status = call(service, "run_check", {"command": "git status"})
    assert status["isError"] is False
    assert "user_notes.txt" in status["structuredContent"]["output"]
    after = subprocess.run(
        ["git", "status", "--porcelain"], cwd=workspace, capture_output=True, text=True, check=True
    ).stdout
    assert after == before
    assert (workspace / "user_notes.txt").read_text() == "pre-existing user change\n"


def test_service_rebinds_when_selected_workspace_changes(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for project in (first, second):
        project.mkdir()
        (project / "AGENTS.md").write_text("# Rules\n")

    current = {"settings": ComputerUseSettings(workspace_path=str(first))}
    service = TunnelMcpService(lambda: current["settings"])

    assert call(service, "project_overview")["structuredContent"]["project_root"] == str(first)
    current["settings"] = ComputerUseSettings(workspace_path=str(second))
    assert call(service, "project_overview")["structuredContent"]["project_root"] == str(second)


@pytest.mark.parametrize(
    "name",
    ["read_file", "replace_in_file", "create_file", "call_runtime_tool"],
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
