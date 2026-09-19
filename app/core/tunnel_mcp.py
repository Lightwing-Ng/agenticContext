"""MCP endpoint that ChatGPT reaches through the OpenAI Secure MCP Tunnel.

Code version: v2.1.0-codex.0

ChatGPT calls these tools through its Tunnel-connected app. Every project-scoped
tool names one explicitly registered project (see ``tunnel_projects``); a project
id is an identity, never a path, and write authority comes only from the registry.
File access reuses the Browser Agent's ``WorkspaceController`` path rules for that
project's root, and commands go through the same registry-validated approved-command
policy and bodycheck, so both connections share one safety boundary. Batch reads,
validated multi-file edits, guarded whole-file writes, bounded read-only Git
inspection, and approved verification commands make multi-step coding practical over
the Tunnel.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
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
from app.core.tunnel_git import (
    DiffRequest,
    bounded_patch,
    change_summary,
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
PROJECT_PROPERTY = _string("Registered project id configured by the user.", maximum=64, minimum=1)


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
        "project_overview",
        "",
        "Project overview",
        "Start here for each project you touch. Returns whether it is writable, its root "
        "instruction files (AGENTS.md and similar, which you must read and follow), nested "
        "AGENTS.md files to read before changing paths under their folders, and a bounded "
        "Git status.",
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
        "Run a relevant check after every edit. Writable projects only.",
        read_only=False,
    ),
    TunnelTool(
        "show_changes",
        "",
        "Show changes",
        "Review Git changes: branch status, changed and untracked files, and unstaged and "
        "staged diff stats. Set include_patch to receive a bounded unified diff, optionally "
        "for one path or for staged changes.",
        read_only=True,
        schema=_object(
            {
                "path": _string(
                    "Optional project-relative path to limit status and diffs.", maximum=1_000
                ),
                "include_patch": _boolean("Include the bounded unified diff."),
                "staged": _boolean("Show the staged patch instead of the unstaged patch."),
            }
        ),
        project_lock=False,
    ),
    TunnelTool(
        "review_changes",
        "bodycheck",
        "Review changes",
        "Final gate: checks the bounded diff and instruction files. In a writable project it "
        "requires a successful run_check after the latest edit.",
        read_only=True,
    ),
)
TUNNEL_TOOLS_BY_NAME = {tool.name: tool for tool in TUNNEL_TOOLS}


SERVER_INSTRUCTIONS = (
    "These tools work directly in explicitly registered local projects on the user's "
    "computer. Call project_overview first for the registered project you will work in, and "
    "follow the instruction files it lists. Every tool names its project; paths are "
    "relative to that project's root, and read-only projects (such as reference "
    "repositories) cannot be changed. Inspect with list_files, search_files, and read_files. "
    "Change code with apply_edits (exact replacements) or "
    "write_file (new files, or whole-file rewrites guarded by expected_sha256). Verify with "
    "run_check after every edit, review with show_changes, and finish with review_changes. "
    "Leave unrelated "
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


class TunnelMcpService:
    """Serve MCP JSON-RPC requests against explicitly registered projects."""

    def __init__(
        self,
        settings_provider: Callable[[], Any],
        *,
        registry: ProjectRegistry | None = None,
        runtime_root: Path | None = None,
    ) -> None:
        # ``runtime_root`` remains accepted for callers that construct the service
        # alongside other Agent runtimes; the synchronous ten-tool MCP contract does
        # not persist its own jobs.
        del runtime_root
        self._settings_provider = settings_provider
        self._registry = registry or ProjectRegistry()
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
                expected = str(arguments.get("expected_sha256") or "").strip().casefold()
                if (
                    receipt is not None
                    and not current
                    and receipt[0] == expected
                    and path.is_file()
                ):
                    # Any Tunnel edit advances the controller generation and ages every
                    # existing receipt. Refresh only that same-digest receipt; a client
                    # that never read this file must not gain a synthesized receipt.
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
            "git_status": status_summary(project.root, withheld=_withheld_diff_path),
            "workflow": SERVER_INSTRUCTIONS,
        }
        if nested_files:
            result["nested_instruction_files"] = nested_files
        if project.writable:
            result["verification_current"] = bool(controller.state.verification_current)
        return result

    def _tool_read_files(self, project: TunnelProject, arguments: dict[str, Any]) -> dict[str, Any]:
        results = []
        for item in arguments["files"]:
            payload = {key: item[key] for key in ("path", "start_line", "end_line") if key in item}
            results.append(self._execute_action(project, "read", payload))
        return {"ok": any(result.get("ok") for result in results), "files": results}

    def _tool_apply_edits(self, project: TunnelProject, arguments: dict[str, Any]) -> dict[str, Any]:
        controller = self._controller_for(project)
        planned: OrderedDict[Path, dict[str, Any]] = OrderedDict()
        for index, edit in enumerate(arguments["edits"], start=1):
            old_text = edit["old_text"]
            new_text = edit["new_text"]
            path = self._resolve(project, edit["path"])
            if path not in planned:
                source_bytes, _digest, _size, _identity = controller._current_file_snapshot(path)
                try:
                    source = source_bytes.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ValueError(f"{path.name} is not UTF-8 text.") from exc
                planned[path] = {
                    "source": source,
                    "text": source,
                    "replacements": 0,
                }
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
        controller._mark_edit()
        for path, plan in planned.items():
            data = plan["text"].encode("utf-8")
            relative_path = path.relative_to(project.root)
            controller._replace_text_file(relative_path, plan["source"], plan["text"])
            changed.append(
                {
                    "path": relative_path.as_posix(),
                    "replacements": plan["replacements"],
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
        return {"ok": True, "files": changed}

    def _tool_write_file(self, project: TunnelProject, arguments: dict[str, Any]) -> dict[str, Any]:
        content = arguments["content"]
        controller = self._controller_for(project)
        path = self._resolve(project, arguments["path"], allow_missing=True)
        relative_path = path.relative_to(project.root)
        relative = relative_path.as_posix()
        existed = path.exists()
        if existed:
            if not path.is_file():
                raise ValueError(f"{relative} is not a regular file.")
            source_bytes, current_sha, _size, _identity = controller._current_file_snapshot(path)
            expected = str(arguments.get("expected_sha256") or "")
            if not expected:
                raise ValueError(
                    f"{relative} already exists. Read it with read_files and pass its sha256 "
                    "as expected_sha256 to replace it, or use apply_edits."
                )
            if expected != current_sha:
                raise ValueError(f"{relative} changed since it was read. Read it again first.")
            try:
                source = source_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"{relative} is not UTF-8 text.") from exc
        data = content.encode("utf-8")
        controller._mark_edit()
        if existed:
            controller._replace_text_file(relative_path, source, content)
        else:
            controller._write_new_file(relative_path, data)
        return {
            "ok": True,
            "path": relative,
            "created": not existed,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    # Git observation --------------------------------------------------------

    def _tool_show_changes(self, project: TunnelProject, arguments: dict[str, Any]) -> dict[str, Any]:
        paths = (
            (self._relative(project, arguments["path"]),)
            if arguments.get("path")
            else ()
        )
        pathspec = [f":(literal){path}" for path in paths]
        result: dict[str, Any] = {
            "ok": True,
            **change_summary(project.root, pathspec, withheld=_withheld_diff_path),
        }
        if arguments.get("include_patch"):
            result.update(
                bounded_patch(
                    project.root,
                    DiffRequest(bool(arguments.get("staged")), "", "", paths),
                    withheld=_withheld_diff_path,
                )
            )
        return result

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


def _server_info() -> dict[str, str]:
    return {"name": PRODUCT_NAME, "version": APP_VERSION}


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
