"""MCP endpoint that ChatGPT reaches through the OpenAI Secure MCP Tunnel.

Code version: v2.0.0-claude.0

ChatGPT calls these tools through its Tunnel-connected app. Every project-scoped
tool names one explicitly registered project (see ``tunnel_projects``); a project
id is an identity, never a path, and write authority comes only from the registry.
File access reuses the Browser Agent's ``WorkspaceController`` path rules for that
project's root, and commands go through the same registry-validated approved-command
policy and bodycheck, so both connections share one safety boundary. Batch reads,
transactional multi-file edits, guarded whole-file writes, paginated read-only Git
inspection, and durable approved verification jobs make multi-step coding practical
over the Tunnel.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.agent.capability_registry import capability_for_action, validate_closed_schema
from app.core.brand import PRODUCT_NAME
from app.core.tunnel_checks import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS,
    TERMINAL_STATES,
    TunnelCheckStore,
)
from app.core.tunnel_git import (
    DEFAULT_MAX_HUNK_LINES,
    DEFAULT_MAX_HUNKS,
    ContinuationCodec,
    DiffRequest,
    change_summary,
    diff_page,
    git_log,
    request_from_continuation,
    resolve_commit,
    status_summary,
)
from app.core.tunnel_projects import ProjectRegistry, TunnelProject
from app.core.version import APP_VERSION

MCP_LEGACY_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
MCP_DEFAULT_PROTOCOL_VERSION = "2025-06-18"
MCP_STATELESS_PROTOCOL_VERSION = "2026-07-28"
MCP_PROTOCOL_VERSION_HEADER = "MCP-Protocol-Version"
MCP_PROTOCOL_VERSION_META = "io.modelcontextprotocol/protocolVersion"
MCP_SERVER_INFO_META = "io.modelcontextprotocol/serverInfo"
MAX_TOOL_TEXT_CHARACTERS = 200_000
MAX_WRITE_CHARACTERS = 1_000_000
TUNNEL_ACTIVITY_LIMIT = 20

# Routing metadata that a runtime gateway may attach to a tool call. Selecting a
# runtime is this server's concern, never the model's, so these keys are dropped
# before dispatch: every tool stays callable by its published schema alone, and no
# tool can ever depend on the caller sending a runtime selector.
ROUTING_METADATA_KEYS = frozenset(
    {
        "execution_mode",
        "runtime",
        "runtime_name",
        "runtime_gateway",
        "adaptive_runtime",
        "full_operator_runtime",
        "_meta",
    }
)

LOGGER = logging.getLogger(__name__)


def _string(description: str, *, maximum: int, minimum: int = 0) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string", "description": description, "maxLength": maximum}
    if minimum:
        schema["minLength"] = minimum
    return schema


def _integer(description: str, *, minimum: int, maximum: int) -> dict[str, Any]:
    return {"type": "integer", "description": description, "minimum": minimum, "maximum": maximum}


def _boolean(description: str) -> dict[str, Any]:
    return {"type": "boolean", "description": description}


def _object(properties: dict[str, Any], *required: str) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


PATH_PROPERTY = _string("Project-relative path.", maximum=1_000, minimum=1)
PROJECT_PROPERTY = _string("Registered project id from list_projects.", maximum=64, minimum=1)
JOB_ID_PROPERTY = _string("Check job id returned by start_check.", maximum=32, minimum=32)
REVISION_PROPERTY = _string("Commit SHA or ref name (not a range).", maximum=128, minimum=1)


@dataclass(frozen=True, slots=True)
class TunnelTool:
    """One model-facing tool.

    ``action`` names a ``WorkspaceController`` action whose registry schema is
    reused; an empty ``action`` means a local ``_tool_<name>`` handler with its own
    ``schema``. A tool that is not ``read_only`` runs only in writable projects.
    Project-scoped tools require the ``project`` argument.
    """

    name: str
    action: str
    title: str
    guidance: str
    read_only: bool
    destructive: bool = False
    schema: dict[str, Any] | None = None
    project_scoped: bool = True
    project_lock: bool = True


TUNNEL_TOOLS: tuple[TunnelTool, ...] = (
    TunnelTool(
        "list_projects",
        "",
        "List projects",
        "List the registered local projects you may work in, with whether each is writable. "
        "Every other tool takes one of these ids as project.",
        read_only=True,
        schema=_object({}),
        project_scoped=False,
        project_lock=False,
    ),
    TunnelTool(
        "project_overview",
        "",
        "Project overview",
        "Start here for each project you touch. Returns whether it is writable, its root "
        "instruction files (AGENTS.md and similar, which you must read and follow), nested "
        "AGENTS.md files to read before changing paths under their folders, active checks, "
        "and a bounded Git status.",
        read_only=True,
        schema=_object({}),
    ),
    TunnelTool(
        "list_files",
        "list",
        "List files",
        "Paths are relative to the project root.",
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
        "read_files",
        "",
        "Read files",
        "Read 1-8 files or line ranges in one call. Each result has numbered lines and the "
        "file's sha256; pass that sha256 to write_file or delete_file when replacing or "
        "deleting the file. Prefer targeted ranges for large files.",
        read_only=True,
        schema=_object(
            {
                "files": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 8,
                    "items": _object(
                        {
                            "path": PATH_PROPERTY,
                            "start_line": _integer("First one-based line.", minimum=1, maximum=1_000_000),
                            "end_line": _integer("Last one-based line.", minimum=1, maximum=1_000_000),
                        },
                        "path",
                    ),
                }
            },
            "files",
        ),
    ),
    TunnelTool(
        "apply_edits",
        "",
        "Apply exact edits",
        "Apply 1-16 exact text replacements across existing files as one batch. Every edit is "
        "checked before any file changes: old_text must appear exactly once unless replace_all "
        "is true, and edits to the same file apply in order. Include enough surrounding context "
        "to make old_text unique. Writable projects only.",
        read_only=False,
        schema=_object(
            {
                "edits": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 16,
                    "items": _object(
                        {
                            "path": PATH_PROPERTY,
                            "old_text": _string("Exact existing text.", maximum=200_000, minimum=1),
                            "new_text": _string("Replacement text; may be empty.", maximum=200_000),
                            "replace_all": _boolean("Replace every occurrence instead of exactly one."),
                        },
                        "path",
                        "old_text",
                        "new_text",
                    ),
                }
            },
            "edits",
        ),
    ),
    TunnelTool(
        "write_file",
        "",
        "Write a file",
        "Create a new file (parent folders are created) or replace a whole existing file. "
        "Replacing an existing file requires expected_sha256 from the latest read_files result, "
        "so stale content is never overwritten. Prefer apply_edits for small changes. Writable "
        "projects only.",
        read_only=False,
        destructive=True,
        schema=_object(
            {
                "path": PATH_PROPERTY,
                "content": _string("Complete UTF-8 file content.", maximum=MAX_WRITE_CHARACTERS),
                "expected_sha256": _string(
                    "sha256 of the current file from read_files; required to replace a file.",
                    maximum=64,
                    minimum=64,
                ),
            },
            "path",
            "content",
        ),
    ),
    TunnelTool(
        "delete_file",
        "delete",
        "Delete a file",
        "Pass the sha256 reported by the latest read_files call for this path. Writable "
        "projects only.",
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
        "Run a relevant check after every edit. For a check that may take minutes, use "
        "start_check instead. Writable projects only.",
        read_only=False,
    ),
    TunnelTool(
        "start_check",
        "",
        "Start a long verification",
        "Start one approved verification command (the same allowlist as run_check) as a "
        "durable background job and return its job_id immediately. Reuse the same "
        "idempotency_key when retrying an uncertain start; it never starts a second process. "
        "One check runs per project at a time. Follow with observe_check. Writable projects only.",
        read_only=False,
        schema=_object(
            {
                "command": _string("One approved verification command.", maximum=4_000, minimum=1),
                "idempotency_key": _string(
                    "Caller-chosen key (8-128 letters, digits, '.', '_', ':', '-') unique to this check.",
                    maximum=128,
                    minimum=8,
                ),
                "timeout_seconds": _integer(
                    f"Time limit; default {DEFAULT_TIMEOUT_SECONDS}.",
                    minimum=MIN_TIMEOUT_SECONDS,
                    maximum=MAX_TIMEOUT_SECONDS,
                ),
            },
            "command",
            "idempotency_key",
        ),
    ),
    TunnelTool(
        "observe_check",
        "",
        "Observe a verification job",
        "Return a check's state (starting, running, succeeded, failed, stopped, timeout, or "
        "unknown) and its output tail. A finished check counts as verification only if the "
        "project did not change while it ran; the result says whether it was recorded.",
        read_only=True,
        schema=_object({"job_id": JOB_ID_PROPERTY}, "job_id"),
    ),
    TunnelTool(
        "stop_check",
        "",
        "Stop a verification job",
        "Stop one running check and its process tree. Writable projects only.",
        read_only=False,
        schema=_object({"job_id": JOB_ID_PROPERTY}, "job_id"),
    ),
    TunnelTool(
        "show_changes",
        "",
        "Show changes",
        "Summarize Git changes: branch status, changed and untracked files, and unstaged and "
        "staged diff stats, optionally for one path. Use git_diff_hunks for the patch itself.",
        read_only=True,
        schema=_object(
            {"path": _string("Optional project-relative path to limit the summary.", maximum=1_000)}
        ),
        project_lock=False,
    ),
    TunnelTool(
        "git_log",
        "",
        "Git log",
        "List recent commits (newest first): full SHA, author date, author, and subject. Page "
        "with skip while has_more is true.",
        read_only=True,
        schema=_object(
            {
                "limit": _integer("Commits to return; default 20.", minimum=1, maximum=100),
                "skip": _integer("Commits to skip from the newest.", minimum=0, maximum=100_000),
                "ref": REVISION_PROPERTY,
                "path": _string("Optional project-relative path to limit history.", maximum=1_000),
            }
        ),
        project_lock=False,
    ),
    TunnelTool(
        "git_diff_hunks",
        "",
        "Git diff hunks",
        "Read the unified diff as bounded pages of hunks. Default: unstaged changes; staged "
        "compares the index with HEAD; base_commit alone compares it with the working tree (or "
        "the index when staged); base_commit with head_commit compares two commits. When "
        "complete is false, call again with only project and continuation (page sizes may "
        "change) until every hunk is read. A continuation fails instead of reading a diff that "
        "changed in the meantime.",
        read_only=True,
        schema=_object(
            {
                "paths": {
                    "type": "array",
                    "description": "Optional project-relative paths to limit the diff.",
                    "minItems": 1,
                    "maxItems": 16,
                    "items": PATH_PROPERTY,
                },
                "staged": _boolean("Diff the index instead of the working tree."),
                "base_commit": REVISION_PROPERTY,
                "head_commit": REVISION_PROPERTY,
                "max_hunks": _integer(f"Hunks per page; default {DEFAULT_MAX_HUNKS}.", minimum=1, maximum=50),
                "max_hunk_lines": _integer(
                    f"Lines per hunk segment; default {DEFAULT_MAX_HUNK_LINES}. Longer hunks continue "
                    "on the next page.",
                    minimum=20,
                    maximum=400,
                ),
                "continuation": _string("continuation from the previous page.", maximum=40_000, minimum=1),
            }
        ),
        project_lock=False,
    ),
    TunnelTool(
        "review_changes",
        "bodycheck",
        "Review changes",
        "Final gate: checks the bounded diff and instruction files. In a writable project it "
        "requires a successful run_check (or a recorded observe_check) after the latest edit.",
        read_only=True,
    ),
)
TUNNEL_TOOLS_BY_NAME = {tool.name: tool for tool in TUNNEL_TOOLS}
_DIFF_REQUEST_FIELDS = frozenset({"paths", "staged", "base_commit", "head_commit"})


SERVER_INSTRUCTIONS = (
    "These tools work directly in explicitly registered local projects on the user's "
    "computer. Call list_projects, then project_overview for the project you will work in, "
    "and follow the instruction files it lists. Every tool names its project; paths are "
    "relative to that project's root, and read-only projects (such as reference "
    "repositories) cannot be changed. Inspect with list_files, search_files, read_files, "
    "git_log, and git_diff_hunks. Change code with apply_edits (exact replacements) or "
    "write_file (new files, or whole-file rewrites guarded by expected_sha256). Verify with "
    "run_check after every edit, or start_check and observe_check for long checks; review "
    "with show_changes and git_diff_hunks, and finish with review_changes. Leave unrelated "
    "user changes intact."
)


class McpRequestError(Exception):
    """One JSON-RPC error answered to the caller."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _tool_input_schema(tool: TunnelTool) -> dict[str, Any]:
    """Return the published schema, reusing the controller registry for mapped actions."""
    if not tool.action:
        schema = deepcopy(tool.schema or _object({}))
    else:
        capability = capability_for_action(tool.action)
        if capability is None:
            raise RuntimeError(f"Tunnel tool {tool.name} maps to an unregistered action.")
        schema = deepcopy(capability.input_schema)
        schema.get("properties", {}).pop("action", None)
        schema["required"] = [name for name in schema.get("required", []) if name != "action"]
    if tool.project_scoped:
        schema["properties"] = {"project": deepcopy(PROJECT_PROPERTY), **schema.get("properties", {})}
        schema["required"] = ["project", *schema.get("required", [])]
    if not schema.get("required"):
        schema.pop("required", None)
    return schema


TOOL_SCHEMAS = {tool.name: _tool_input_schema(tool) for tool in TUNNEL_TOOLS}


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
            "inputSchema": deepcopy(TOOL_SCHEMAS[tool.name]),
            "annotations": {
                "title": tool.title,
                "readOnlyHint": tool.read_only,
                "destructiveHint": tool.destructive,
                "openWorldHint": False,
            },
        }
        for tool in TUNNEL_TOOLS
    ]


@dataclass(slots=True)
class _ProjectController:
    root: Path
    writable: bool
    controller: Any
    epoch: str


class TunnelMcpService:
    """Serve MCP JSON-RPC requests against explicitly registered projects."""

    def __init__(
        self,
        settings_provider: Callable[[], Any],
        *,
        registry: ProjectRegistry | None = None,
        runtime_root: Path | None = None,
    ) -> None:
        if runtime_root is None:
            from app.core.computer_use_agent import DEFAULT_AGENT_RUNTIME_ROOT

            runtime_root = DEFAULT_AGENT_RUNTIME_ROOT
        self._settings_provider = settings_provider
        self._registry = registry or ProjectRegistry()
        self._checks = TunnelCheckStore(runtime_root)
        self._continuations = ContinuationCodec(secrets.token_bytes(32))
        # Tools for one project run one at a time, in arrival order, so controller
        # generations, read receipts, and verification ordering stay linear. Projects
        # do not block each other, and read-only Git observation takes no project lock.
        self._project_locks: dict[str, threading.Lock] = {}
        self._controllers: dict[str, _ProjectController] = {}
        self._controllers_lock = threading.Lock()
        self._activity_lock = threading.Lock()
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
        name = str(params.get("name") or "").strip()
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise McpRequestError(-32602, "Tool arguments must be an object.")

        if "action" in arguments:
            # The tool name selects the controller action; never let arguments override it.
            raise McpRequestError(-32602, "Unexpected tool argument: action")

        if arguments.keys() & ROUTING_METADATA_KEYS:
            # A runtime gateway may wrap the call with its own routing selector. Drop it
            # so the tool runs on its own schema and the caller never needs to send one.
            arguments = {
                key: value for key, value in arguments.items() if key not in ROUTING_METADATA_KEYS
            }

        tool = TUNNEL_TOOLS_BY_NAME.get(name)
        if tool is None:
            raise McpRequestError(-32602, f"Unknown tool: {name or '[missing]'}")

        started = time.monotonic()
        try:
            observation = self._run_tool(tool, arguments)
        except (OSError, RuntimeError, ValueError) as exc:
            observation = {"ok": False, "error": str(exc)[:2_000]}
        ok = bool(observation.get("ok"))
        duration = time.monotonic() - started
        self._record_activity(tool, arguments, ok, duration)
        LOGGER.info(
            "Tunnel tool %s ok=%s duration=%.2fs project=%s target=%s%s",
            tool.name,
            ok,
            duration,
            str(arguments.get("project") or "")[:64],
            _activity_target(arguments)[:200],
            "" if ok else f" error={str(observation.get('error') or '')[:200]}",
        )
        text = json.dumps(observation, ensure_ascii=False)
        if len(text) > MAX_TOOL_TEXT_CHARACTERS:
            text = text[:MAX_TOOL_TEXT_CHARACTERS] + "…"
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": observation,
            "isError": not ok,
        }

    def _run_tool(self, tool: TunnelTool, arguments: dict[str, Any]) -> dict[str, Any]:
        """Validate one call, resolve its project, and run it under that project's lock."""
        validate_closed_schema(arguments, TOOL_SCHEMAS[tool.name], subject="Tool arguments")
        _reject_absolute_paths(arguments)
        if not tool.project_scoped:
            return getattr(self, f"_tool_{tool.name}")(arguments)
        project = self._registry.resolve(arguments["project"], self._fallback_workspace())
        if not tool.read_only and not project.writable:
            raise ValueError(
                f"Project {project.id} is read-only; {tool.name} is not allowed there."
            )
        arguments = {key: value for key, value in arguments.items() if key != "project"}
        if not tool.project_lock:
            return self._dispatch_tool(tool, project, arguments)
        with self._project_lock(project):
            return self._dispatch_tool(tool, project, arguments)

    def _dispatch_tool(
        self,
        tool: TunnelTool,
        project: TunnelProject,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        if tool.action:
            return self._execute_action(project, tool.action, arguments)
        return getattr(self, f"_tool_{tool.name}")(project, arguments)

    def _fallback_workspace(self) -> str:
        return str(getattr(self._settings_provider(), "workspace_path", "") or "")

    def _project_lock(self, project: TunnelProject) -> threading.Lock:
        with self._controllers_lock:
            return self._project_locks.setdefault(f"{project.id}\0{project.root}", threading.Lock())

    def _binding(self, project: TunnelProject) -> _ProjectController:
        """Return this project's controller, rebuilt whenever its registration changes."""
        with self._controllers_lock:
            binding = self._controllers.get(project.id)
            if (
                binding is None
                or binding.root != project.root
                or binding.writable != project.writable
            ):
                from app.core.computer_use_agent import WorkspaceController

                binding = _ProjectController(
                    project.root,
                    project.writable,
                    WorkspaceController(
                        project.root,
                        self._settings_provider(),
                        lambda: self._stopping,
                        read_only=not project.writable,
                    ),
                    secrets.token_hex(8),
                )
                self._controllers[project.id] = binding
            return binding

    def _controller_for(self, project: TunnelProject) -> Any:
        return self._binding(project).controller

    def _discard_controller(self, project: TunnelProject) -> None:
        with self._controllers_lock:
            self._controllers.pop(project.id, None)

    def _execute_action(
        self,
        project: TunnelProject,
        action: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        controller = self._controller_for(project)
        if action == "delete":
            raw_path = arguments.get("path")
            if raw_path:
                path = self._resolve(project, raw_path, allow_missing=True)
                relative_key = path.relative_to(project.root).as_posix()
                receipt = controller.state.read_receipts.get(relative_key)
                current = (
                    receipt is not None
                    and receipt[2] == controller.state.workspace_generation
                )
                if not current and path.is_file():
                    # Any Tunnel edit advances the controller generation and ages every
                    # receipt. Refresh this one so expected_sha256, which the controller
                    # still compares with the file's bytes under its lock, stays the
                    # single stale-delete guard.
                    controller.execute({"action": "read", "path": relative_key})
        payload = {**arguments, "action": action}
        observation = controller.execute(payload)
        if "start a new task" in str(observation.get("error") or ""):
            # The project folder was replaced on disk; bind a fresh controller once.
            self._discard_controller(project)
            observation = self._controller_for(project).execute(payload)
        return observation

    def _resolve(self, project: TunnelProject, raw_path: Any, *, allow_missing: bool = False) -> Path:
        """Resolve one project path through the Browser Agent's path rules."""
        return self._controller_for(project)._resolve_path(raw_path, allow_missing=allow_missing)

    def _relative(self, project: TunnelProject, raw_path: Any) -> str:
        return self._resolve(project, raw_path, allow_missing=True).relative_to(project.root).as_posix()

    def _tool_list_projects(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        projects = self._registry.projects(self._fallback_workspace())
        if not projects:
            return {
                "ok": False,
                "error": (
                    "No project is registered. Ask the user to add projects to "
                    "tunnel-projects.json beside the AgenticContext settings file."
                ),
            }
        return {
            "ok": True,
            "projects": [
                {**project.public_record(), "git": (project.root / ".git").exists()}
                for project in projects
            ],
        }

    def _tool_project_overview(self, project: TunnelProject, _arguments: dict[str, Any]) -> dict[str, Any]:
        listing = self._execute_action(project, "list", {"path": ".", "depth": 1})
        root_files, nested_files = _project_instruction_files(project.root)
        controller = self._controller_for(project)
        result: dict[str, Any] = {
            "ok": True,
            "project": project.id,
            "writable": project.writable,
            "instruction_files": root_files,
            "top_level_entries": listing.get("entries", []),
            "git_status": status_summary(project.root),
            "workflow": SERVER_INSTRUCTIONS,
        }
        if nested_files:
            result["nested_instruction_files"] = nested_files
        if project.writable:
            result["verification_current"] = bool(controller.state.verification_current)
            active = self._checks.active_jobs(project.id, project.root)
            if active:
                result["active_checks"] = active
        return result

    def _tool_read_files(self, project: TunnelProject, arguments: dict[str, Any]) -> dict[str, Any]:
        results = []
        for item in arguments["files"]:
            payload = {key: item[key] for key in ("path", "start_line", "end_line") if key in item}
            results.append(self._execute_action(project, "read", payload))
        return {"ok": any(result.get("ok") for result in results), "files": results}

    def _tool_apply_edits(self, project: TunnelProject, arguments: dict[str, Any]) -> dict[str, Any]:
        planned: OrderedDict[Path, dict[str, Any]] = OrderedDict()
        for index, edit in enumerate(arguments["edits"], start=1):
            old_text = edit["old_text"]
            new_text = edit["new_text"]
            path = self._resolve(project, edit["path"])
            if path not in planned:
                planned[path] = {"text": _read_text_file(path), "replacements": 0}
            current = planned[path]["text"]
            occurrences = current.count(old_text)
            relative = path.relative_to(project.root).as_posix()
            if occurrences == 0:
                raise ValueError(
                    f"Edit {index}: old_text was not found in {relative}. Read the file again "
                    "and copy the exact current text."
                )
            if occurrences > 1 and not edit.get("replace_all"):
                raise ValueError(
                    f"Edit {index}: old_text appears {occurrences} times in {relative}. Add "
                    "surrounding context or set replace_all."
                )
            count = occurrences if edit.get("replace_all") else 1
            planned[path]["text"] = current.replace(old_text, new_text, count)
            planned[path]["replacements"] += count
        changed = []
        for path, plan in planned.items():
            data = plan["text"].encode("utf-8")
            _atomic_replace(path, data)
            changed.append(
                {
                    "path": path.relative_to(project.root).as_posix(),
                    "replacements": plan["replacements"],
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
        self._controller_for(project)._mark_edit()
        return {"ok": True, "files": changed}

    def _tool_write_file(self, project: TunnelProject, arguments: dict[str, Any]) -> dict[str, Any]:
        content = arguments["content"]
        path = self._resolve(project, arguments["path"], allow_missing=True)
        relative = path.relative_to(project.root).as_posix()
        existed = path.exists()
        if existed:
            if not path.is_file():
                raise ValueError(f"{relative} is not a regular file.")
            current_sha = hashlib.sha256(path.read_bytes()).hexdigest()
            expected = str(arguments.get("expected_sha256") or "")
            if not expected:
                raise ValueError(
                    f"{relative} already exists. Read it with read_files and pass its sha256 "
                    "as expected_sha256 to replace it, or use apply_edits."
                )
            if expected != current_sha:
                raise ValueError(f"{relative} changed since it was read. Read it again first.")
        data = content.encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_replace(path, data)
        self._controller_for(project)._mark_edit()
        return {
            "ok": True,
            "path": relative,
            "created": not existed,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    # Git observation --------------------------------------------------------

    def _tool_show_changes(self, project: TunnelProject, arguments: dict[str, Any]) -> dict[str, Any]:
        pathspec = [f":(literal){self._relative(project, arguments['path'])}"] if arguments.get("path") else []
        return {"ok": True, **change_summary(project.root, pathspec)}

    def _tool_git_log(self, project: TunnelProject, arguments: dict[str, Any]) -> dict[str, Any]:
        pathspec = [f":(literal){self._relative(project, arguments['path'])}"] if arguments.get("path") else []
        return {
            "ok": True,
            **git_log(
                project.root,
                limit=int(arguments.get("limit") or 20),
                skip=int(arguments.get("skip") or 0),
                ref=str(arguments.get("ref") or ""),
                pathspec=pathspec,
            ),
        }

    def _tool_git_diff_hunks(self, project: TunnelProject, arguments: dict[str, Any]) -> dict[str, Any]:
        token = arguments.get("continuation")
        continuation: dict[str, Any] | None = None
        if token:
            if arguments.keys() & _DIFF_REQUEST_FIELDS:
                raise ValueError(
                    "A continuation already fixes the diff request; pass only project, "
                    "continuation, max_hunks, and max_hunk_lines."
                )
            continuation = self._continuations.decode(token)
            request = request_from_continuation(continuation, project_id=project.id, root=project.root)
        else:
            staged = bool(arguments.get("staged"))
            base = str(arguments.get("base_commit") or "")
            head = str(arguments.get("head_commit") or "")
            if head and not base:
                raise ValueError("head_commit needs base_commit.")
            if head and staged:
                raise ValueError("staged compares the index, so it cannot be combined with head_commit.")
            paths = tuple(
                dict.fromkeys(self._relative(project, raw) for raw in arguments.get("paths") or [])
            )
            request = DiffRequest(
                staged,
                resolve_commit(project.root, base, "base_commit") if base else "",
                resolve_commit(project.root, head, "head_commit") if head else "",
                paths,
            )
        return diff_page(
            project_id=project.id,
            root=project.root,
            request=request,
            withheld=_withheld_diff_path,
            codec=self._continuations,
            continuation=continuation,
            max_hunks=int(arguments.get("max_hunks") or DEFAULT_MAX_HUNKS),
            max_hunk_lines=int(arguments.get("max_hunk_lines") or DEFAULT_MAX_HUNK_LINES),
        )

    # Durable verification ---------------------------------------------------

    def _tool_start_check(self, project: TunnelProject, arguments: dict[str, Any]) -> dict[str, Any]:
        from app.core.computer_use_agent import (
            _workspace_mutation_fingerprint,
            inspection_command_parts,
        )

        command = arguments["command"].strip()
        argv = inspection_command_parts(command, workspace=project.root)
        if argv[:2] == ["git", "status"]:
            raise ValueError("Use run_check for git status; start_check is for long checks.")
        binding = self._binding(project)
        snapshot, complete = _workspace_mutation_fingerprint(
            project.root, should_stop=lambda: self._stopping
        )
        if not complete:
            raise RuntimeError(
                "The check was not started because the project could not be fingerprinted "
                "completely; its result could not be tied to the checked state."
            )
        controller = binding.controller
        controller._record_workspace_snapshot(snapshot, complete=True)
        job_dir, metadata, deduplicated = self._checks.start(
            project_id=project.id,
            project_root=project.root,
            argv=argv,
            command=command,
            idempotency_key=arguments["idempotency_key"],
            timeout_seconds=int(arguments.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS),
            evidence={
                "snapshot_id": snapshot,
                "edit_generation": controller.state.edit_generation,
                "workspace_generation": controller.state.workspace_generation,
                "controller_epoch": binding.epoch,
            },
        )
        result: dict[str, Any] = {"ok": True, "job": self._checks.status(job_dir, metadata)}
        if deduplicated:
            result["deduplicated"] = True
        return result

    def _tool_observe_check(self, project: TunnelProject, arguments: dict[str, Any]) -> dict[str, Any]:
        job_dir, metadata = self._checks.find(project.id, project.root, arguments["job_id"])
        return self._check_observation(project, job_dir, metadata, self._checks.status(job_dir, metadata))

    def _tool_stop_check(self, project: TunnelProject, arguments: dict[str, Any]) -> dict[str, Any]:
        job_dir, metadata = self._checks.find(project.id, project.root, arguments["job_id"])
        status = self._checks.stop(job_dir, metadata)
        observation = self._check_observation(project, job_dir, metadata, status)
        if status["state"] not in TERMINAL_STATES:
            observation["ok"] = False
            observation["error"] = "The check has not stopped yet; observe it again shortly."
        elif status["state"] != "unknown":
            observation["ok"] = True
            observation.pop("error", None)
        return observation

    def _check_observation(
        self,
        project: TunnelProject,
        job_dir: Path,
        metadata: dict[str, Any],
        status: dict[str, Any],
    ) -> dict[str, Any]:
        """Map one job state to a truthful result, recording evidence exactly once."""
        state = status["state"]
        if state not in TERMINAL_STATES:
            return {"ok": True, "job": status}
        evaluation = self._evaluate_check(project, job_dir, metadata, state)
        status.update(evaluation)
        if state == "succeeded" and not evaluation.get("workspace_changed"):
            return {"ok": True, "job": status}
        if state == "succeeded":
            error = (
                "The command passed, but the project changed while it ran, so it does not "
                "verify the current files. Run the check again."
            )
        elif state == "unknown":
            error = "The check outcome is unknown; treat the project as unverified."
        else:
            error = f"The check ended as {state}; the project is not verified."
        return {"ok": False, "error": error, "job": status}

    def _evaluate_check(
        self,
        project: TunnelProject,
        job_dir: Path,
        metadata: dict[str, Any],
        state: str,
    ) -> dict[str, Any]:
        from app.core.computer_use_agent import _workspace_mutation_fingerprint

        existing = self._checks.evaluation(job_dir)
        if existing is not None:
            return {key: existing[key] for key in ("verification", "workspace_changed", "reason") if key in existing}
        binding = self._binding(project)
        controller = binding.controller
        evidence = metadata.get("evidence") if isinstance(metadata.get("evidence"), dict) else {}
        after, complete = _workspace_mutation_fingerprint(
            project.root, should_stop=lambda: self._stopping
        )
        workspace_changed = not complete or after != evidence.get("snapshot_id")
        same_session = evidence.get("controller_epoch") == binding.epoch
        same_generation = bool(
            same_session
            and evidence.get("edit_generation") == controller.state.edit_generation
            and evidence.get("workspace_generation") == controller.state.workspace_generation
        )
        evaluation: dict[str, Any] = {"verification": "not_recorded", "workspace_changed": workspace_changed}
        if workspace_changed:
            # Newer files than the checked ones: age every older piece of evidence too.
            controller._record_workspace_snapshot(after, complete=complete)
            evaluation["reason"] = "The project changed while the check ran."
        elif state == "succeeded" and same_generation:
            controller._record_workspace_snapshot(after, complete=True)
            controller.state.verification_generation = controller.state.edit_generation
            controller.state.verification_workspace_generation = controller.state.workspace_generation
            controller.state.verification_snapshot_id = after
            controller.state.successful_checks.append(str(metadata.get("command") or ""))
            evaluation["verification"] = "recorded"
        elif state == "succeeded":
            evaluation["reason"] = (
                "The project was edited after the check started."
                if same_session
                else "The check started before the service restarted."
            )
        elif same_generation:
            # A failing check of the current files withdraws any earlier pass for them.
            controller._invalidate_verification_order()
        self._checks.record_evaluation(job_dir, evaluation)
        return evaluation

    def _record_activity(
        self,
        tool: TunnelTool,
        arguments: dict[str, Any],
        ok: bool,
        duration: float,
    ) -> None:
        target = _activity_target(arguments)
        project = str(arguments.get("project") or "")
        if project and isinstance(arguments.get("project"), str):
            target = f"{project[:64]}: {target}" if target else project[:64]
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


def _activity_target(arguments: dict[str, Any]) -> str:
    for key in ("path", "command", "query", "job_id"):
        if arguments.get(key):
            return str(arguments[key])
    for key in ("files", "edits"):
        items = arguments.get(key)
        if isinstance(items, list) and items and isinstance(items[0], dict):
            first = str(items[0].get("path") or "")
            return first if len(items) == 1 else f"{first} (+{len(items) - 1})"
    return ""


def _reject_absolute_paths(arguments: dict[str, Any]) -> None:
    """Refuse absolute or home-relative paths so none can bypass project resolution."""
    candidates: list[Any] = [arguments.get("path")]
    candidates.extend(arguments.get("paths") or [])
    for key in ("files", "edits"):
        for item in arguments.get(key) or []:
            if isinstance(item, dict):
                candidates.append(item.get("path"))
    for raw in candidates:
        if not isinstance(raw, str) or not raw:
            continue
        if raw.startswith(("/", "\\", "~")) or Path(raw).is_absolute() or re.match(r"^[A-Za-z]:", raw):
            raise ValueError(
                "Paths must be relative to the project root; absolute paths are not accepted."
            )


def _withheld_diff_path(relative: str) -> bool:
    """Keep credential and controller-internal files out of model-visible diffs."""
    from app.core.computer_use_agent import (
        _path_has_controller_internal_file,
        _path_has_sensitive_part,
    )

    path = Path(relative)
    return (
        _path_has_sensitive_part(path)
        or _path_has_controller_internal_file(path)
        or ".computer-use-agent" in path.parts
    )


def _project_instruction_files(root: Path) -> tuple[list[str], list[str]]:
    """Return root and nested instruction files that belong to this project only.

    A nested directory with its own ``.git`` is another repository, so its
    instruction files are excluded along with everything beneath it.
    """
    from app.core.computer_use_agent import _collect_instruction_files

    root_files: list[str] = []
    nested_files: list[str] = []
    for path in _collect_instruction_files(root):
        relative = path.relative_to(root)
        if len(relative.parts) == 1:
            root_files.append(relative.as_posix())
            continue
        crosses_repository = any(
            (root.joinpath(*relative.parts[:depth]) / ".git").exists()
            for depth in range(1, len(relative.parts))
        )
        if not crosses_repository:
            nested_files.append(relative.as_posix())
    return root_files, nested_files


def _read_text_file(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"{path.name} is not a regular file.")
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path.name} is not UTF-8 text.") from exc


def _atomic_replace(path: Path, data: bytes) -> None:
    """Write bytes beside the target and swap them in, keeping its permissions."""
    mode = path.stat().st_mode & 0o7777 if path.exists() else 0o644
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tunnel-tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _server_info() -> dict[str, str]:
    return {"name": PRODUCT_NAME, "version": APP_VERSION}


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
