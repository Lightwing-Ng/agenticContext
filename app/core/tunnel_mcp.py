"""MCP endpoint that ChatGPT reaches through the OpenAI Secure MCP Tunnel.

Code version: v1.0.0-codex.0

ChatGPT calls these tools through its Tunnel-connected app. Every tool is a thin
adapter over the same registry-validated ``WorkspaceController`` actions the
Browser Agent uses, so path confinement, sensitive-file rules, the approved
command policy, and bodycheck evidence stay identical for both connections.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.agent.capability_registry import capability_for_action
from app.core.brand import PRODUCT_NAME
from app.core.version import APP_VERSION

MCP_LEGACY_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
MCP_DEFAULT_PROTOCOL_VERSION = "2025-06-18"
MCP_STATELESS_PROTOCOL_VERSION = "2026-07-28"
MCP_PROTOCOL_VERSION_HEADER = "MCP-Protocol-Version"
MCP_PROTOCOL_VERSION_META = "io.modelcontextprotocol/protocolVersion"
MCP_SERVER_INFO_META = "io.modelcontextprotocol/serverInfo"
MAX_TOOL_TEXT_CHARACTERS = 200_000
MAX_GIT_STATUS_CHARACTERS = 6_000
TUNNEL_ACTIVITY_LIMIT = 20


@dataclass(frozen=True, slots=True)
class TunnelTool:
    """One model-facing tool mapped onto a controller action."""

    name: str
    action: str
    title: str
    guidance: str
    read_only: bool
    destructive: bool = False


TUNNEL_TOOLS: tuple[TunnelTool, ...] = (
    TunnelTool(
        "project_overview",
        "",
        "Project overview",
        "Start here. Returns the selected local project, its instruction files "
        "(AGENTS.md and similar, which you must read and follow), and a bounded Git status.",
        read_only=True,
    ),
    TunnelTool(
        "list_files",
        "list",
        "List files",
        "Paths are relative to the project root.",
        read_only=True,
    ),
    TunnelTool(
        "read_file",
        "read",
        "Read a file",
        "Returns numbered lines and the file's SHA-256. Read a file before editing or deleting it.",
        read_only=True,
    ),
    TunnelTool(
        "search_files",
        "search",
        "Search files",
        "The query is literal text, not a regular expression.",
        read_only=True,
    ),
    TunnelTool(
        "replace_in_file",
        "replace",
        "Replace text in a file",
        "The old text must appear exactly once; include enough surrounding context to make it unique.",
        read_only=False,
    ),
    TunnelTool(
        "create_file",
        "write",
        "Create a file",
        "Only creates new files; use replace_in_file to change an existing file.",
        read_only=False,
    ),
    TunnelTool(
        "delete_file",
        "delete",
        "Delete a file",
        "Pass the SHA-256 reported by the latest read_file call for this path.",
        read_only=False,
        destructive=True,
    ),
    TunnelTool(
        "run_check",
        "run",
        "Run a verification command",
        "Approved commands only: git status; pytest, ruff, mypy, pyright, eslint; tsc --noEmit; "
        "python -m <test module>; node --check <file>; npm/pnpm/yarn/bun test or run <check script>; "
        "go test/vet; cargo check/clippy/test; make <check target>; or a project verification "
        "script such as scripts/test.sh. Shells, pipes, and file-changing commands are refused. "
        "Run a relevant check after every edit.",
        read_only=False,
    ),
    TunnelTool(
        "review_changes",
        "bodycheck",
        "Review changes",
        "Final gate: checks the bounded diff and instruction files. It requires a successful "
        "run_check after the latest edit.",
        read_only=True,
    ),
)
TUNNEL_TOOLS_BY_NAME = {tool.name: tool for tool in TUNNEL_TOOLS}

SERVER_INSTRUCTIONS = (
    "These tools operate on one local project on the user's computer. Call project_overview "
    "first, read and follow the instruction files it lists, then inspect with list_files, "
    "search_files, and read_file. Edit with replace_in_file or create_file, verify with "
    "run_check, and finish with review_changes. Paths are always relative to the project root."
)


class McpRequestError(Exception):
    """One JSON-RPC error answered to the caller."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _tool_input_schema(tool: TunnelTool) -> dict[str, Any]:
    """Return the registry schema without the controller's action discriminator."""
    if not tool.action:
        return {"type": "object", "properties": {}, "additionalProperties": False}
    capability = capability_for_action(tool.action)
    if capability is None:
        raise RuntimeError(f"Tunnel tool {tool.name} maps to an unregistered action.")
    schema = deepcopy(capability.input_schema)
    schema.get("properties", {}).pop("action", None)
    schema["required"] = [name for name in schema.get("required", []) if name != "action"]
    if not schema["required"]:
        schema.pop("required")
    return schema


def _tool_description(tool: TunnelTool) -> str:
    if not tool.action:
        return tool.guidance
    capability = capability_for_action(tool.action)
    base = capability.description if capability is not None else ""
    return f"{base} {tool.guidance}".strip()


def tunnel_tool_definitions() -> list[dict[str, Any]]:
    """Return the MCP ``tools/list`` records."""
    return [
        {
            "name": tool.name,
            "title": tool.title,
            "description": _tool_description(tool),
            "inputSchema": _tool_input_schema(tool),
            "annotations": {
                "title": tool.title,
                "readOnlyHint": tool.read_only,
                "destructiveHint": tool.destructive,
                "openWorldHint": False,
            },
        }
        for tool in TUNNEL_TOOLS
    ]


class TunnelMcpService:
    """Serve MCP JSON-RPC requests against the Agent's current project."""

    def __init__(self, settings_provider: Callable[[], Any]) -> None:
        self._settings_provider = settings_provider
        self._lock = threading.Lock()
        self._activity_lock = threading.Lock()
        self._controller: Any | None = None
        self._controller_workspace: Path | None = None
        self._activity: deque[dict[str, Any]] = deque(maxlen=TUNNEL_ACTIVITY_LIMIT)
        self._call_count = 0
        self._stopping = False

    def stop(self) -> None:
        """Make any in-flight controller command stop at its next check."""
        self._stopping = True

    def activity_snapshot(self) -> dict[str, Any]:
        """Return recent tool calls for the Agent and Settings status views."""
        with self._activity_lock:
            return {"call_count": self._call_count, "recent_calls": list(self._activity)}

    # JSON-RPC transport -------------------------------------------------

    def handle(self, body: Any, headers: Mapping[str, str]) -> tuple[int, Any | None]:
        """Return an HTTP status and JSON body (``None`` means no body)."""
        header_version = str(headers.get(MCP_PROTOCOL_VERSION_HEADER) or "").strip()
        if isinstance(body, list):
            if not body:
                return 400, _rpc_error(None, -32600, "Empty JSON-RPC batch.")
            responses = [
                response
                for item in body
                if (response := self._handle_one(item, header_version)) is not None
            ]
            return (200, responses) if responses else (202, None)
        response = self._handle_one(body, header_version)
        if response is None:
            return 202, None
        return 200, response

    def _handle_one(self, request: Any, header_version: str) -> dict[str, Any] | None:
        if not isinstance(request, dict) or not isinstance(request.get("method"), str):
            return _rpc_error(
                request.get("id") if isinstance(request, dict) else None,
                -32600,
                "Invalid JSON-RPC request.",
            )
        request_id = request.get("id")
        method = request["method"]
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        if "id" not in request:
            # Notifications such as notifications/initialized never get a response body.
            return None
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        stateless = MCP_STATELESS_PROTOCOL_VERSION in {
            header_version,
            str(meta.get(MCP_PROTOCOL_VERSION_META) or ""),
        }
        try:
            result = self._dispatch(method, params, stateless)
        except McpRequestError as exc:
            return _rpc_error(request_id, exc.code, exc.message)
        if stateless:
            result.setdefault("resultType", "complete")
            result.setdefault("_meta", {}).setdefault(MCP_SERVER_INFO_META, _server_info())
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _dispatch(self, method: str, params: dict[str, Any], stateless: bool) -> dict[str, Any]:
        if method == "initialize":
            requested = str(params.get("protocolVersion") or "")
            return {
                "protocolVersion": (
                    requested
                    if requested in MCP_LEGACY_PROTOCOL_VERSIONS
                    else MCP_DEFAULT_PROTOCOL_VERSION
                ),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": _server_info(),
                "instructions": SERVER_INSTRUCTIONS,
            }
        if method == "server/discover":
            return {
                "ttlMs": 0,
                "cacheScope": "private",
                "supportedVersions": [
                    MCP_STATELESS_PROTOCOL_VERSION,
                    *MCP_LEGACY_PROTOCOL_VERSIONS,
                ],
                "capabilities": {"tools": {"listChanged": False}},
                "instructions": SERVER_INSTRUCTIONS,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            result: dict[str, Any] = {"tools": tunnel_tool_definitions()}
            if stateless:
                result.update({"ttlMs": 0, "cacheScope": "private"})
            return result
        if method == "tools/call":
            return self._call_tool(params)
        if method == "resources/list":
            return {"resources": []}
        if method == "resources/templates/list":
            return {"resourceTemplates": []}
        if method == "prompts/list":
            return {"prompts": []}
        raise McpRequestError(-32601, f"Method not found: {method}")

    # Tools --------------------------------------------------------------

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "")
        tool = TUNNEL_TOOLS_BY_NAME.get(name)
        if tool is None:
            raise McpRequestError(-32602, f"Unknown tool: {name or '[missing]'}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise McpRequestError(-32602, "Tool arguments must be an object.")
        if "action" in arguments:
            # The tool name selects the controller action; never let arguments override it.
            raise McpRequestError(-32602, "Unexpected tool argument: action")
        started = time.monotonic()
        with self._lock:
            try:
                if tool.action:
                    observation = self._execute_action(tool.action, arguments)
                else:
                    observation = self._project_overview()
            except (OSError, RuntimeError, ValueError) as exc:
                observation = {"ok": False, "error": str(exc)[:2_000]}
        ok = bool(observation.get("ok"))
        self._record_activity(tool, arguments, ok, time.monotonic() - started)
        text = json.dumps(observation, ensure_ascii=False)
        if len(text) > MAX_TOOL_TEXT_CHARACTERS:
            text = text[:MAX_TOOL_TEXT_CHARACTERS] + "…"
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": observation,
            "isError": not ok,
        }

    def _workspace(self) -> Path:
        settings = self._settings_provider()
        workspace = Path(str(settings.workspace_path or "")).expanduser()
        if not workspace.is_dir():
            raise RuntimeError(
                "The Agent project folder is unavailable. Choose a project on the Agent page."
            )
        return workspace.resolve()

    def _controller_for(self, workspace: Path) -> Any:
        if self._controller is None or self._controller_workspace != workspace:
            from app.core.computer_use_agent import WorkspaceController

            self._controller = WorkspaceController(
                workspace,
                self._settings_provider(),
                lambda: self._stopping,
            )
            self._controller_workspace = workspace
        return self._controller

    def _execute_action(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        workspace = self._workspace()
        payload = {**arguments, "action": action}
        observation = self._controller_for(workspace).execute(payload)
        if "start a new task" in str(observation.get("error") or ""):
            # The project folder was replaced on disk; bind a fresh controller once.
            self._controller = None
            observation = self._controller_for(workspace).execute(payload)
        return observation

    def _project_overview(self) -> dict[str, Any]:
        from app.core.computer_use_agent import _collect_instruction_files

        workspace = self._workspace()
        listing = self._execute_action("list", {"path": ".", "depth": 1})
        return {
            "ok": True,
            "project": workspace.name,
            "project_root": str(workspace),
            "instruction_files": [
                path.relative_to(workspace).as_posix()
                for path in _collect_instruction_files(workspace)
            ],
            "top_level_entries": listing.get("entries", []),
            "git_status": _git_status(workspace),
            "workflow": SERVER_INSTRUCTIONS,
        }

    def _record_activity(
        self,
        tool: TunnelTool,
        arguments: dict[str, Any],
        ok: bool,
        duration: float,
    ) -> None:
        target = str(arguments.get("path") or arguments.get("command") or arguments.get("query") or "")
        with self._activity_lock:
            self._call_count += 1
            self._activity.appendleft(
                {
                    "tool": tool.name,
                    "target": target[:160],
                    "ok": ok,
                    "duration_seconds": round(duration, 2),
                    "at": time.time(),
                }
            )


def _git_status(workspace: Path) -> str:
    """Return a bounded ``git status`` summary, or an explanation."""
    try:
        completed = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "Git status is unavailable."
    output = (completed.stdout or completed.stderr).strip()
    if completed.returncode != 0:
        return "This project is not a Git repository."
    return output[:MAX_GIT_STATUS_CHARACTERS]


def _server_info() -> dict[str, str]:
    return {"name": PRODUCT_NAME, "version": APP_VERSION}


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
