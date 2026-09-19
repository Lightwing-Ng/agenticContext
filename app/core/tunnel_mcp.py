"""MCP endpoint that ChatGPT reaches through the OpenAI Secure MCP Tunnel.

Code version: v1.4.1-codex.0

ChatGPT calls these tools through its Tunnel-connected app. File access reuses the
Browser Agent's ``WorkspaceController`` path rules, and commands go through the same
registry-validated approved-command policy and bodycheck, so both connections share
one safety boundary. Batch reads, transactional multi-file edits, guarded whole-file
writes, and read-only Git review make multi-step coding practical over the Tunnel.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import subprocess
import threading
import time
from collections import OrderedDict, deque
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
MAX_GIT_PATCH_CHARACTERS = 60_000
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


@dataclass(frozen=True, slots=True)
class TunnelTool:
    """One model-facing tool.

    ``action`` names a ``WorkspaceController`` action whose registry schema is
    reused; an empty ``action`` means a local ``_tool_<name>`` handler with its own
    ``schema``.
    """

    name: str
    action: str
    title: str
    guidance: str
    read_only: bool
    destructive: bool = False
    schema: dict[str, Any] | None = None


TUNNEL_TOOLS: tuple[TunnelTool, ...] = (
    TunnelTool(
        "project_overview",
        "",
        "Project overview",
        "Start here. Returns the selected local project, its instruction files "
        "(AGENTS.md and similar, which you must read and follow), and a bounded Git status.",
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
        "to make old_text unique.",
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
                            "replace_all": {
                                "type": "boolean",
                                "description": "Replace every occurrence instead of exactly one.",
                            },
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
        "so stale content is never overwritten. Prefer apply_edits for small changes.",
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
        "Pass the sha256 reported by the latest read_files call for this path.",
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
        "Run a relevant check after every edit; use show_changes for diffs.",
        read_only=False,
    ),
    TunnelTool(
        "show_changes",
        "",
        "Show changes",
        "Review Git changes: branch status, changed and untracked files, and diff stats. Set "
        "include_patch to receive the bounded unified diff, optionally for one path or for "
        "staged changes.",
        read_only=True,
        schema=_object(
            {
                "path": _string("Optional project-relative path to limit the diff.", maximum=1_000),
                "include_patch": {"type": "boolean", "description": "Include the unified diff."},
                "staged": {"type": "boolean", "description": "Show staged instead of unstaged changes."},
            }
        ),
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
    "These tools work directly in one local project on the user's computer. Call "
    "project_overview first and follow the instruction files it lists. Inspect with "
    "list_files, search_files, and read_files. Change code with apply_edits (exact "
    "replacements) or write_file (new files, or whole-file rewrites guarded by "
    "expected_sha256). Verify with run_check after every edit, review the diff with "
    "show_changes, and finish with review_changes. Paths are relative to the project root, "
    "and unrelated user changes must be left intact."
)


class McpRequestError(Exception):
    """One JSON-RPC error answered to the caller."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _tool_input_schema(tool: TunnelTool) -> dict[str, Any]:
    """Return the tool schema, reusing the controller registry for mapped actions."""
    if not tool.action:
        return deepcopy(tool.schema or _object({}))
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
        with self._lock:
            try:
                if tool.action:
                    observation = self._execute_action(tool.action, arguments)
                else:
                    observation = getattr(self, f"_tool_{tool.name}")(arguments)
            except (OSError, RuntimeError, ValueError) as exc:
                observation = {"ok": False, "error": str(exc)[:2_000]}
        ok = bool(observation.get("ok"))
        duration = time.monotonic() - started
        self._record_activity(tool, arguments, ok, duration)
        LOGGER.info(
            "Tunnel tool %s ok=%s duration=%.2fs target=%s%s",
            tool.name,
            ok,
            duration,
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
        controller = self._controller_for(workspace)
        if action == "delete":
            raw_path = arguments.get("path")
            if raw_path:
                _, path = self._resolve(raw_path, allow_missing=True)
                relative_key = path.relative_to(workspace).as_posix()
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
            self._controller = None
            observation = self._controller_for(workspace).execute(payload)
        return observation

    def _tool_project_overview(self, _arguments: dict[str, Any]) -> dict[str, Any]:
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

    def _resolve(self, raw_path: Any, *, allow_missing: bool = False) -> tuple[Path, Path]:
        """Resolve one project path through the Browser Agent's path rules."""
        workspace = self._workspace()
        controller = self._controller_for(workspace)
        return workspace, controller._resolve_path(raw_path, allow_missing=allow_missing)

    def _tool_read_files(self, arguments: dict[str, Any]) -> dict[str, Any]:
        items = arguments.get("files")
        if not isinstance(items, list) or not 1 <= len(items) <= 8:
            raise ValueError("files must list 1 to 8 entries.")
        results = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                results.append({"ok": False, "error": "Each entry needs a path."})
                continue
            payload = {key: item[key] for key in ("path", "start_line", "end_line") if key in item}
            results.append(self._execute_action("read", payload))
        return {"ok": any(result.get("ok") for result in results), "files": results}

    def _tool_apply_edits(self, arguments: dict[str, Any]) -> dict[str, Any]:
        edits = arguments.get("edits")
        if not isinstance(edits, list) or not 1 <= len(edits) <= 16:
            raise ValueError("edits must list 1 to 16 entries.")
        workspace = self._workspace()
        planned: OrderedDict[Path, dict[str, Any]] = OrderedDict()
        for index, edit in enumerate(edits, start=1):
            if not isinstance(edit, dict):
                raise ValueError(f"Edit {index} must be an object.")
            old_text = edit.get("old_text")
            new_text = edit.get("new_text")
            if not isinstance(old_text, str) or not old_text:
                raise ValueError(f"Edit {index} needs non-empty old_text.")
            if not isinstance(new_text, str):
                raise ValueError(f"Edit {index} needs new_text.")
            _, path = self._resolve(edit.get("path"))
            if path not in planned:
                planned[path] = {"text": _read_text_file(path), "replacements": 0}
            current = planned[path]["text"]
            occurrences = current.count(old_text)
            relative = path.relative_to(workspace).as_posix()
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
                    "path": path.relative_to(workspace).as_posix(),
                    "replacements": plan["replacements"],
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
        self._controller_for(workspace)._mark_edit()
        return {"ok": True, "files": changed}

    def _tool_write_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        content = arguments.get("content")
        if not isinstance(content, str) or len(content) > MAX_WRITE_CHARACTERS:
            raise ValueError("content must be a string of at most 1,000,000 characters.")
        workspace, path = self._resolve(arguments.get("path"), allow_missing=True)
        relative = path.relative_to(workspace).as_posix()
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
        self._controller_for(workspace)._mark_edit()
        return {
            "ok": True,
            "path": relative,
            "created": not existed,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    def _tool_show_changes(self, arguments: dict[str, Any]) -> dict[str, Any]:
        workspace = self._workspace()
        pathspec: list[str] = []
        if arguments.get("path"):
            _, path = self._resolve(arguments.get("path"), allow_missing=True)
            pathspec = ["--", path.relative_to(workspace).as_posix() or "."]
        status = _git(workspace, "status", "--porcelain=v1", "--branch", "--untracked-files=all")
        if status is None:
            return {"ok": False, "error": "This project is not a Git repository."}
        result: dict[str, Any] = {
            "ok": True,
            "status": _truncate(status, MAX_GIT_STATUS_CHARACTERS),
            "unstaged_stat": _truncate(_git(workspace, "diff", "--stat", *pathspec) or "", 8_000),
            "staged_stat": _truncate(
                _git(workspace, "diff", "--cached", "--stat", *pathspec) or "", 8_000
            ),
        }
        if arguments.get("include_patch"):
            staged = ["--cached"] if arguments.get("staged") else []
            patch = _git(workspace, "diff", "--no-ext-diff", *staged, *pathspec) or ""
            result["patch"] = _truncate(patch, MAX_GIT_PATCH_CHARACTERS)
            result["patch_truncated"] = len(patch) > MAX_GIT_PATCH_CHARACTERS
        return result

    def _record_activity(
        self,
        tool: TunnelTool,
        arguments: dict[str, Any],
        ok: bool,
        duration: float,
    ) -> None:
        target = _activity_target(arguments)
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
    for key in ("path", "command", "query"):
        if arguments.get(key):
            return str(arguments[key])
    for key in ("files", "edits"):
        items = arguments.get(key)
        if isinstance(items, list) and items and isinstance(items[0], dict):
            first = str(items[0].get("path") or "")
            return first if len(items) == 1 else f"{first} (+{len(items) - 1})"
    return ""


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…"


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


def _git(workspace: Path, *args: str) -> str | None:
    """Run one read-only Git query and return stdout, or None when Git fails."""
    try:
        completed = subprocess.run(
            ["git", "-c", "color.ui=never", *args],
            cwd=workspace,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=20,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat"},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


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
