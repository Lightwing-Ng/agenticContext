"""Tunnel MCP adapter and /mcp route tests.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

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
    assert chatgpt["serverInfo"]["name"] == "agenticContext"
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
    assert body["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "agenticContext"


def test_tool_list_reuses_registry_schemas_without_the_action_field() -> None:
    tools = {tool["name"]: tool for tool in tunnel_tool_definitions()}
    assert list(tools) == [tool.name for tool in TUNNEL_TOOLS]
    for tool in tools.values():
        assert "action" not in tool["inputSchema"].get("properties", {})
        assert "action" not in tool["inputSchema"].get("required", [])
        assert tool["inputSchema"]["additionalProperties"] is False
    assert tools["read_file"]["inputSchema"]["required"] == ["path"]
    assert tools["read_file"]["annotations"]["readOnlyHint"] is True
    assert tools["delete_file"]["annotations"]["destructiveHint"] is True


def test_tools_run_through_the_workspace_controller(
    service: TunnelMcpService,
    workspace: Path,
) -> None:
    overview = call(service, "project_overview")
    assert overview["isError"] is False
    assert overview["structuredContent"]["instruction_files"] == ["AGENTS.md"]
    assert overview["structuredContent"]["project"] == "project"

    read = call(service, "read_file", {"path": "app.py"})
    assert read["structuredContent"]["content"] == '1: print("hi")'

    edited = call(service, "replace_in_file", {"path": "app.py", "old": "hi", "new": "hello"})
    assert edited["isError"] is False
    assert (workspace / "app.py").read_text() == 'print("hello")\n'

    escaped = call(service, "read_file", {"path": "../outside.txt"})
    assert escaped["isError"] is True
    assert "inside the selected project" in escaped["structuredContent"]["error"]

    refused = call(service, "run_check", {"command": "rm -rf ."})
    assert refused["isError"] is True

    smuggled = rpc(service, "tools/call", {"name": "read_file", "arguments": {"path": "x", "action": "write"}})
    assert smuggled["error"]["code"] == -32602

    activity = service.activity_snapshot()
    assert activity["call_count"] == 5
    assert activity["recent_calls"][0]["tool"] == "run_check"


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
