"""Shared MCP endpoint reached through authenticated provider transports.

Code version: v2.11.0-codex.0

ChatGPT and Gemini call the same tool catalog through separate authenticated
transports. Every project-scoped
tool names one explicitly registered project (see ``tunnel_projects``); a project
id is an identity, never a path, and write authority comes only from the registry.
File access goes through ``app.core.workspace``'s public ``WorkspaceAccess`` boundary
for that project's root, and commands go through the same registry-validated
approved-command policy and bodycheck, so both connections share one safety boundary
without this module reaching into controller internals. Batch reads,
validated multi-file edits, guarded whole-file writes, bounded read-only Git
inspection, and approved verification commands make multi-step coding practical over
the Tunnel.

``current_project`` lets a model that was only told "check the current project"
discover the exact registered id selected on the local Tunnel page, without a path
and without any other argument. Every other project-scoped tool still names its
project explicitly, so a later change of the local selection never redirects a task
that already resolved its project.

Mutating file tools require a ``request_id``. A repeated request with the
same id and arguments returns the recorded outcome instead of editing again, and a
request whose outcome was never recorded (for example because the service stopped
mid-write) is refused with instructions to read the files before retrying.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.agent.capability_registry import capability_for_action, validate_closed_schema
from app.core.brand import PRODUCT_NAME
from app.core.tunnel_checks import (
    DEFAULT_TIMEOUT_SECONDS as CHECK_DEFAULT_TIMEOUT_SECONDS,
    MAX_TIMEOUT_SECONDS as CHECK_MAX_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS as CHECK_MIN_TIMEOUT_SECONDS,
    TERMINAL_STATES as CHECK_TERMINAL_STATES,
    CheckJobError,
    TunnelCheckStore,
)
from app.core.tunnel_git import (
    DiffRequest,
    bounded_patch,
    change_summary,
    status_summary,
)
from app.core.tunnel_projects import (
    ProjectRegistry,
    ProjectRegistryError,
    ProjectSelectionStore,
    TunnelProject,
    project_availability,
    resolve_current_project,
    selected_projects,
)
from app.core.token_usage import openai_agentic_token_encoding
from app.core.version import APP_VERSION
from app.core.workspace import (
    MAX_FILE_READ_CHARS,
    MAX_WORKSPACE_FILE_BYTES,
    TextReplacement,
    WorkspaceAccess,
    collect_instruction_files,
    describe_workspace_error,
    inspection_command_parts,
    is_withheld_workspace_path,
    open_workspace,
)

MCP_LEGACY_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
MCP_DEFAULT_PROTOCOL_VERSION = "2025-06-18"
MCP_STATELESS_PROTOCOL_VERSION = "2026-07-28"
MCP_PROTOCOL_VERSION_HEADER = "MCP-Protocol-Version"
MCP_PROTOCOL_VERSION_META = "io.modelcontextprotocol/protocolVersion"
MCP_SERVER_INFO_META = "io.modelcontextprotocol/serverInfo"
MAX_TOOL_TEXT_CHARACTERS = 200_000
MAX_READ_BATCH_CONTENT_CHARACTERS = 150_000
MAX_WRITE_CHARACTERS = 1_000_000
MAX_READ_FILES = 8
MAX_EDITS = 16
MAX_MCP_BATCH_ITEMS = 8
DEFAULT_READ_LINES = 240
TUNNEL_ACTIVITY_LIMIT = 20
REQUEST_JOURNAL_DIRNAME = "tunnel-requests"
REQUEST_JOURNAL_LIMIT = 256
REQUEST_JOURNAL_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
REQUEST_TOMBSTONE_LIMIT = 100_000
REQUEST_TOMBSTONE_FILENAME = "expired-ids.log"
REQUEST_TOMBSTONE_MAX_BYTES = REQUEST_TOMBSTONE_LIMIT * 106
REQUEST_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"
_REQUEST_ID_RE = re.compile(REQUEST_ID_PATTERN)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")

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

_TOOL_ENCODING_LOCK = threading.Lock()
_TOOL_ENCODING: Any | None = None
_TOOL_ENCODING_STARTED = False


def _load_tool_encoding() -> None:
    # Reuse the existing tokenizer cache, but never block a tool on its download.
    global _TOOL_ENCODING
    _TOOL_ENCODING = openai_agentic_token_encoding()


def _estimated_tool_tokens(text: str) -> int | None:
    """Estimate observable tool text only; unavailable is never a byte heuristic."""
    global _TOOL_ENCODING_STARTED
    with _TOOL_ENCODING_LOCK:
        if not _TOOL_ENCODING_STARTED:
            _TOOL_ENCODING_STARTED = True
            threading.Thread(target=_load_tool_encoding, daemon=True).start()
        encoding = _TOOL_ENCODING
    if encoding is None or len(text) > MAX_WRITE_CHARACTERS:
        return None
    try:
        return len(encoding.encode(text, disallowed_special=()))
    except Exception:
        # Metrics cannot fail a workspace operation.
        return None


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
PROJECT_PROPERTY = _string(
    "Registered project id (from current_project or the user). Keep using the same id "
    "for the whole task.",
    maximum=64,
    minimum=1,
)
PROJECT_IDENTITY_PROPERTY = _string(
    "Identity returned by current_project or project_overview. It is required after "
    "discovery, and the call is refused if the project's registration changed since then.",
    maximum=16,
    minimum=16,
)
REQUEST_ID_PROPERTY = _string(
    "Required unique id for this change (8-128 letters, digits, '.', '_', ':', '-'). "
    "Resending the same request_id within the durable retry window returns the recorded "
    "outcome instead of applying the change twice.",
    maximum=128,
    minimum=8,
)
SHA256_PROPERTY = _string("Lowercase sha256 from read_files.", maximum=64, minimum=64)
JOB_ID_PROPERTY = _string("Check job id returned by start_check.", maximum=32, minimum=32)


@dataclass(frozen=True, slots=True)
class TunnelTool:
    """One model-facing tool.

    ``action`` names a workspace controller action whose registry schema is
    reused; an empty ``action`` means a local ``_tool_<name>`` handler with its own
    ``schema``. A tool that is not ``read_only`` runs only in writable projects.
    Project-scoped tools require the ``project`` argument. ``project_lock`` tools run
    one at a time per project; read-only observation runs without that lock so a long
    verification command never blocks reading. ``journaled`` tools require a
    ``request_id`` for duplicate-safe retries.
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
    journaled: bool = False


TUNNEL_TOOLS: tuple[TunnelTool, ...] = (
    TunnelTool(
        "current_project",
        "",
        "Current project",
        "Call this first when the user has not given a project id, for example \"check "
        "the current project\". Returns the project the user selected on the local "
        "AgenticContext Tunnel page (id, write access, identity, selection revision), the "
        "other registered projects, and the file limits on this computer. Then call "
        "project_overview with that id and keep using the same id and identity for the "
        "whole task.",
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
        "AGENTS.md files to read before changing paths under their folders, a bounded Git "
        "status, running checks, and file limits.",
        read_only=True,
        schema=_object({}),
        project_lock=False,
    ),
    TunnelTool(
        "list_files",
        "list",
        "List files",
        "Paths are relative to the project root. truncated=true means more entries exist; "
        "list a subfolder to see them.",
        read_only=True,
        project_lock=False,
    ),
    TunnelTool(
        "search_files",
        "search",
        "Search files",
        "The query is literal text, not a regular expression. If truncated is true, "
        "repeat with a narrower project-relative path or inclusive glob (or raise "
        "max_results up to 300).",
        read_only=True,
        project_lock=False,
    ),
    TunnelTool(
        "read_files",
        "",
        "Read files",
        f"Read 1-{MAX_READ_FILES} UTF-8 text files or line ranges in one call (about "
        f"{DEFAULT_READ_LINES} lines per file by default). Each result has numbered lines, "
        "the file's sha256, total_lines, and has_more/next_start_line when more lines "
        "remain; read again from next_start_line to continue. outcome is all_succeeded, "
        "partial, or all_failed. Pass a file's sha256 to write_file or delete_file when "
        "replacing or deleting it.",
        read_only=True,
        project_lock=False,
        schema=_object(
            {
                "files": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_READ_FILES,
                    "items": _object(
                        {
                            "path": PATH_PROPERTY,
                            "start_line": _integer("First one-based line.", minimum=1, maximum=1_000_000),
                            "end_line": _integer("Last one-based line.", minimum=1, maximum=1_000_000),
                            "start_character": _integer(
                                "Zero-based character offset returned when one line is segmented.",
                                minimum=0,
                                maximum=MAX_WORKSPACE_FILE_BYTES,
                            ),
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
        f"Apply 1-{MAX_EDITS} exact text replacements across existing files as one batch. "
        "Every edit is checked before any file changes: old_text must appear exactly once "
        "unless replace_all is true, and edits to the same file apply in order. Include "
        "enough surrounding context to make old_text unique, and pass expected_sha256 "
        "from read_files so a changed file is refused. If a later file cannot be written, "
        "earlier files are restored; outcome is committed, rolled_back, or partial with "
        "each file's status. Writable projects only.",
        read_only=False,
        schema=_object(
            {
                "edits": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_EDITS,
                    "items": _object(
                        {
                            "path": PATH_PROPERTY,
                            "old_text": _string("Exact existing text.", maximum=200_000, minimum=1),
                            "new_text": _string("Replacement text; may be empty.", maximum=200_000),
                            "replace_all": _boolean("Replace every occurrence instead of exactly one."),
                            "expected_sha256": SHA256_PROPERTY,
                        },
                        "path",
                        "old_text",
                        "new_text",
                    ),
                }
            },
            "edits",
        ),
        journaled=True,
    ),
    TunnelTool(
        "write_file",
        "",
        "Write a file",
        "Create a new UTF-8 file (missing parent folders inside the project are created) "
        "or replace a whole existing file. An existing file is never overwritten by "
        "accident: replacing it requires expected_sha256 from the latest read_files "
        "result. The result reports the sha256 read back from disk. Prefer apply_edits "
        "for small changes. Writable projects only.",
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
        journaled=True,
    ),
    TunnelTool(
        "delete_file",
        "delete",
        "Delete a file",
        "Deletes one regular file. Pass the sha256 reported by the latest read_files call "
        "for this path; a file that changed since is kept. The result confirms the file is "
        "gone. Writable projects only.",
        read_only=False,
        destructive=True,
        journaled=True,
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
        "Start a background check",
        "Start one approved verification command (the same allowlist as run_check) in the "
        "background for checks that may outlast one request. Starting the same command "
        "again while it runs, or for unchanged files, returns the same job. One check runs "
        "per project at a time. Follow with observe_check. Writable projects only.",
        read_only=False,
        schema=_object(
            {
                "command": _string("Approved verification command.", maximum=4_000, minimum=1),
                "idempotency_key": _string(
                    "Optional key (8-128 letters, digits, '.', '_', ':', '-'); a new key "
                    "starts a fresh run of the same command.",
                    maximum=128,
                    minimum=8,
                ),
                "timeout_seconds": _integer(
                    "Stop the check after this many seconds.",
                    minimum=CHECK_MIN_TIMEOUT_SECONDS,
                    maximum=CHECK_MAX_TIMEOUT_SECONDS,
                ),
            },
            "command",
        ),
    ),
    TunnelTool(
        "observe_check",
        "",
        "Observe a background check",
        "Return a check's state (starting, running, succeeded, failed, stopped, timeout, or "
        "unknown), exit code, and output tail. A finished check counts as verification "
        "only if the project's files did not change after it started.",
        read_only=True,
        schema=_object({"job_id": JOB_ID_PROPERTY}, "job_id"),
        project_lock=False,
    ),
    TunnelTool(
        "stop_check",
        "",
        "Stop a background check",
        "Stop one running check and its process tree. Writable projects only.",
        read_only=False,
        schema=_object({"job_id": JOB_ID_PROPERTY}, "job_id"),
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
        "requires a successful run_check or observed start_check whose checked files are "
        "still the current files.",
        read_only=True,
    ),
)
TUNNEL_TOOLS_BY_NAME = {tool.name: tool for tool in TUNNEL_TOOLS}


SERVER_INSTRUCTIONS = (
    "These tools work directly in explicitly registered local projects on the user's "
    "computer. If the user did not name a project id, call current_project first to get "
    "the project selected on the local AgenticContext page, then call project_overview "
    "for that id and follow the instruction files it lists. Every other tool names its "
    "project and requires its identity; keep both fixed for the whole task. Paths are "
    "relative to that project's "
    "root, and read-only projects (such as reference repositories) cannot be changed. "
    "Inspect with list_files, search_files, and read_files. Change code with apply_edits "
    "(exact replacements) or write_file (new files, or whole-file rewrites guarded by "
    "expected_sha256); give each change a request_id so a retry after a lost response "
    "never applies it twice. Re-read changed files to confirm the result. Verify with "
    "run_check (or start_check and observe_check for long checks) after every edit, "
    "review with show_changes, and finish with review_changes. Report the actual tool "
    "results. Leave unrelated user changes intact."
)


class McpRequestError(Exception):
    """One JSON-RPC error answered to the caller."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ToolFailure(Exception):
    """A tool refusal with a stable ``code`` and extra structured detail."""

    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def observation(self) -> dict[str, Any]:
        return {"ok": False, "code": self.code, "error": self.message, **self.detail}


_ERROR_CODE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("invalid_arguments", ("Tool arguments",)),
    ("unknown_project", ("Unknown project", "Name a registered project")),
    ("project_unavailable", ("unavailable on this computer", "root changed", "project registry")),
    ("read_only_project", ("is read-only", "read-only;")),
    (
        "path_refused",
        (
            "inside the selected project",
            "linked files",
            "internal metadata",
            "ignored, generated",
            "credentials and private-key",
            "internal recovery files",
            "absolute paths",
        ),
    ),
    ("already_exists", ("already exists", "creates new files only")),
    (
        "stale_file",
        (
            "read receipt",
            "SHA-256",
            "sha256",
            "changed since it was read",
            "changed before",
            "changed while",
            "read it again",
            "read the current file first",
        ),
    ),
    ("edit_mismatch", ("old_text", "must appear exactly once")),
    ("not_found", ("requires an existing", "requires a regular file", "no longer exists")),
    ("too_large", ("too large", "larger than")),
    ("not_text", ("not UTF-8",)),
    ("verification_required", ("verification", "Bodycheck requires")),
    ("command_refused", ("approved", "not allowed", "refused")),
)


def _error_code(message: str) -> str:
    for code, fragments in _ERROR_CODE_RULES:
        if any(fragment in message for fragment in fragments):
            return code
    return "failed"


def _os_error_code(error: OSError) -> str:
    import errno as errno_module

    if isinstance(error, PermissionError):
        return "permission_denied"
    if isinstance(error, FileNotFoundError):
        return "not_found"
    if error.errno in {errno_module.ENOSPC, getattr(errno_module, "EDQUOT", -1)}:
        return "disk_full"
    return "os_error"


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
        schema["properties"] = {
            "project": deepcopy(PROJECT_PROPERTY),
            **schema.get("properties", {}),
            "project_identity": deepcopy(PROJECT_IDENTITY_PROPERTY),
        }
        identity_required = [] if tool.name == "project_overview" else ["project_identity"]
        schema["required"] = ["project", *identity_required, *schema.get("required", [])]
    if tool.journaled:
        schema["properties"]["request_id"] = deepcopy(REQUEST_ID_PROPERTY)
        schema["required"] = [*schema.get("required", []), "request_id"]
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


def host_file_limits() -> dict[str, Any]:
    """Describe what the file tools support on this host, truthfully and path-free."""
    return {
        "text_encoding": "UTF-8 text files only; other files can be listed but not edited.",
        "max_file_bytes": MAX_WORKSPACE_FILE_BYTES,
        "max_read_characters_per_file": MAX_FILE_READ_CHARS,
        "default_read_lines": DEFAULT_READ_LINES,
        "max_files_per_read": MAX_READ_FILES,
        "max_write_characters": MAX_WRITE_CHARACTERS,
        "max_edits_per_batch": MAX_EDITS,
        "links": "Paths cannot traverse symbolic links or other linked entries.",
        "withheld": (
            "Credential and private-key files, .git internals, generated or dependency "
            "folders, and controller recovery files are not accessible."
        ),
        "shell": "Not available; only approved verification commands run.",
        "background_checks": True,
        "host_platform": sys.platform,
    }


@dataclass(slots=True)
class _ProjectWorkspace:
    """One registered project bound to the shared workspace safety rules."""

    root: Path
    writable: bool
    filesystem_identity: tuple[int, int, int] | None
    access: WorkspaceAccess
    epoch: str = field(default_factory=lambda: secrets.token_hex(8))


class _RequestJournalError(RuntimeError):
    """Raised before a mutation when its durable retry record is unavailable."""


class _RequestOutcomeUnknown(_RequestJournalError):
    """Raised when an existing durable request record cannot prove its outcome."""


class _RequestJournal:
    """Remember mutating requests by id so a retry never applies a change twice.

    Records live in memory and, when a runtime root exists, as small files that
    survive a service restart. A record left in the ``started`` state means the
    service stopped before it knew the outcome.
    """

    def __init__(self, root: Path | None) -> None:
        self._root = root / REQUEST_JOURNAL_DIRNAME if root is not None else None
        self._memory: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._tombstones: dict[str, str] = {}
        self._tombstones_loaded = False
        self._lock = threading.RLock()

    def _path(self, key: str) -> Path | None:
        return self._root / f"{key}.json" if self._root is not None else None

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            self._load_tombstones()
            fingerprint = self._tombstones.get(key)
            if fingerprint is not None:
                return {"fingerprint": fingerprint, "state": "expired"}
            if key in self._memory:
                record = deepcopy(self._memory[key])
                self._validate_record(record)
                return record
            path = self._path(key)
            if path is None:
                return None
            if self._root is not None and self._root.exists() and (
                self._root.is_symlink() or not self._root.is_dir()
            ):
                raise _RequestJournalError(
                    "The durable Tunnel request journal is unavailable. Nothing was "
                    "retried; fix the local runtime directory and try again."
                )
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise _RequestJournalError(
                    "The durable Tunnel request journal cannot be read safely. Nothing "
                    "was retried; fix the local runtime directory and try again."
                ) from exc
            try:
                if path.is_symlink() or not path.is_file() or metadata.st_size > 512 * 1024:
                    raise self._unknown_record_error()
                record = json.loads(path.read_text(encoding="utf-8"))
                self._validate_record(record)
            except _RequestJournalError:
                raise
            except (OSError, UnicodeError, ValueError) as exc:
                raise self._unknown_record_error() from exc
            self.remember(key, record)
            return deepcopy(record)

    @staticmethod
    def _unknown_record_error() -> _RequestOutcomeUnknown:
        return _RequestOutcomeUnknown(
            "A durable record already exists for this request_id, but it cannot be "
            "read or validated. The earlier outcome is unknown, so nothing was retried. "
            "Read the affected paths and compare them with the intended change before "
            "using a new request_id."
        )

    @staticmethod
    def _validate_record(record: Any) -> None:
        if not isinstance(record, dict):
            raise _RequestJournal._unknown_record_error()
        state = record.get("state")
        fingerprint = record.get("fingerprint")
        if (
            state not in {"started", "completed", "expired"}
            or not isinstance(fingerprint, str)
            or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
            or (state == "completed" and not isinstance(record.get("result"), dict))
        ):
            raise _RequestJournal._unknown_record_error()

    @property
    def _tombstone_path(self) -> Path | None:
        return self._root / REQUEST_TOMBSTONE_FILENAME if self._root is not None else None

    def _load_tombstones(self) -> None:
        """Load compact expired-request receipts before any mutation can be retried."""
        with self._lock:
            if self._tombstones_loaded:
                return
            path = self._tombstone_path
            if path is None:
                self._tombstones_loaded = True
                return
            if not path.parent.exists():
                self._tombstones_loaded = True
                return
            if path.parent.is_symlink() or not path.parent.is_dir():
                raise _RequestJournalError(
                    "The durable Tunnel request journal is unavailable. Nothing was "
                    "retried; fix the local runtime directory and try again."
                )
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                self._tombstones_loaded = True
                return
            except OSError as exc:
                raise _RequestJournalError(
                    "The expired-request journal cannot be read safely. No change was attempted."
                ) from exc
            if (
                path.is_symlink()
                or not path.is_file()
                or metadata.st_size > REQUEST_TOMBSTONE_MAX_BYTES
            ):
                raise _RequestJournalError(
                    "The expired-request journal is invalid or exceeds its safe limit. "
                    "No change was attempted."
                )
            try:
                payload = path.read_bytes()
                text = payload.decode("ascii")
            except (OSError, UnicodeError) as exc:
                raise _RequestJournalError(
                    "The expired-request journal cannot be validated. No change was attempted."
                ) from exc
            tombstones: dict[str, str] = {}
            for line in text.splitlines():
                key, separator, fingerprint = line.partition(" ")
                if (
                    not separator
                    or not re.fullmatch(r"[0-9a-f]{40}", key)
                    or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
                    or key in tombstones
                ):
                    raise _RequestJournalError(
                        "The expired-request journal is corrupt. No change was attempted."
                    )
                tombstones[key] = fingerprint
            if len(tombstones) > REQUEST_TOMBSTONE_LIMIT:
                raise _RequestJournalError(
                    "The expired-request journal exceeds its safe record limit. "
                    "No change was attempted."
                )
            self._tombstones = tombstones
            self._tombstones_loaded = True

    def _remember_tombstone(self, key: str, fingerprint: str) -> None:
        """Persist an expired id before deleting its replayable result record."""
        with self._lock:
            self._load_tombstones()
            existing = self._tombstones.get(key)
            if existing is not None:
                if existing != fingerprint:
                    raise _RequestOutcomeUnknown(
                        "The expired-request journal contains a conflicting fingerprint. "
                        "No change was attempted."
                    )
                return
            if len(self._tombstones) >= REQUEST_TOMBSTONE_LIMIT:
                raise _RequestJournalError(
                    f"The durable expired-request journal reached its {REQUEST_TOMBSTONE_LIMIT:,} "
                    "record safety limit. No further mutation will be attempted until the "
                    "journal is reviewed."
                )
            path = self._tombstone_path
            if path is None:
                raise _RequestJournalError(
                    "The durable Tunnel request journal is not configured. No change was attempted."
                )
            line = f"{key} {fingerprint}\n".encode("ascii")
            if path.exists() and path.stat().st_size + len(line) > REQUEST_TOMBSTONE_MAX_BYTES:
                raise _RequestJournalError(
                    "The expired-request journal reached its safe byte limit. "
                    "No change was attempted."
                )
            descriptor = os.open(
                path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_APPEND
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            try:
                if os.name == "posix":
                    os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "ab") as handle:
                    descriptor = -1
                    written = handle.write(line)
                    if written != len(line):
                        raise OSError("Incomplete expired-request journal write.")
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            self._tombstones[key] = fingerprint
            self._memory.pop(key, None)

    def remember(self, key: str, record: dict[str, Any]) -> None:
        """Retain one record for retries in the current service process."""
        with self._lock:
            self._memory[key] = deepcopy(record)
            self._memory.move_to_end(key)
            while len(self._memory) > REQUEST_JOURNAL_LIMIT:
                self._memory.popitem(last=False)

    def put(self, key: str, record: dict[str, Any]) -> None:
        """Remember and durably replace one request record."""
        path = self._path(key)
        if path is None:
            raise _RequestJournalError(
                "The durable Tunnel request journal is not configured. No unrecorded "
                "change will be attempted; configure a runtime directory and retry."
            )
        self._validate_record(record)
        with self._lock:
            temporary: Path | None = None
            try:
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if path.parent.is_symlink() or not path.parent.is_dir() or path.is_symlink():
                    raise OSError("The Tunnel request journal path is not a safe directory.")
                if record.get("state") == "started" and not os.path.lexists(path):
                    retained = self._prune(required_slots=1)
                    if retained >= REQUEST_JOURNAL_LIMIT:
                        raise _RequestJournalError(
                            f"The durable Tunnel request journal already contains "
                            f"{REQUEST_JOURNAL_LIMIT:,} unresolved records. No new change "
                            "was attempted. Read and reconcile older uncertain requests, then "
                            "move their journal records aside before retrying."
                        )
                temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                )
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(record, handle, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                temporary = None
                if os.name == "posix":
                    directory_fd = os.open(
                        path.parent,
                        os.O_RDONLY
                        | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_CLOEXEC", 0),
                    )
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                self.remember(key, record)
                self._prune()
            except _RequestJournalError:
                raise
            except OSError as exc:
                raise _RequestJournalError(
                    "The durable Tunnel request journal is unavailable. No unrecorded change "
                    "will be attempted. Fix the local runtime directory and retry."
                ) from exc
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass

    def remove(self, key: str) -> None:
        with self._lock:
            self._memory.pop(key, None)
        path = self._path(key)
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass

    def _prune(self, *, required_slots: int = 0) -> int:
        if self._root is None:
            return 0
        self._load_tombstones()
        try:
            entries: list[tuple[float, Path, str, str | None]] = []
            for entry in self._root.iterdir():
                if entry.suffix != ".json":
                    continue
                state = ""
                fingerprint = None
                if not entry.is_symlink() and entry.is_file():
                    try:
                        payload = json.loads(entry.read_text(encoding="utf-8"))
                        self._validate_record(payload)
                        state = str(payload["state"])
                        fingerprint = str(payload["fingerprint"])
                    except (OSError, UnicodeError, ValueError, _RequestJournalError):
                        pass
                if state not in {"completed", "started"}:
                    # An unreadable or malformed record is an uncertainty tombstone.
                    state = "started"
                try:
                    modified = entry.lstat().st_mtime
                except OSError as exc:
                    raise _RequestJournalError(
                        "The durable Tunnel request journal cannot be inventoried safely. "
                        "No new change was attempted."
                    ) from exc
                entries.append((modified, entry, state, fingerprint))
            entries.sort(key=lambda item: item[0])
        except _RequestJournalError:
            raise
        except OSError as exc:
            raise _RequestJournalError(
                "The durable Tunnel request journal cannot be inventoried safely. No new "
                "change was attempted."
            ) from exc
        cutoff = time.time() - REQUEST_JOURNAL_MAX_AGE_SECONDS
        remaining = len(entries)
        excess = max(0, remaining - max(0, REQUEST_JOURNAL_LIMIT - required_slots))
        for modified, entry, state, fingerprint in entries:
            if state == "started":
                continue
            if excess > 0 or modified < cutoff:
                if fingerprint is None:
                    continue
                try:
                    self._remember_tombstone(entry.stem, fingerprint)
                    entry.unlink()
                except (OSError, _RequestJournalError):
                    continue
                self._memory.pop(entry.stem, None)
                remaining -= 1
                if excess > 0:
                    excess -= 1
        return remaining


class TunnelMcpService:
    """Serve MCP JSON-RPC requests against explicitly registered projects."""

    def __init__(
        self,
        settings_provider: Callable[[], Any],
        *,
        registry: ProjectRegistry | None = None,
        selection_store: ProjectSelectionStore | None = None,
        runtime_root: Path | None = None,
    ) -> None:
        self._settings_provider = settings_provider
        self._registry = registry or ProjectRegistry()
        if selection_store is None:
            selection_store = ProjectSelectionStore(
                self._registry.path.with_name("tunnel-selection.json")
                if registry is not None
                else None
            )
        self._selection_store = selection_store
        # Durable check jobs and request records live beside the other Tunnel state.
        self._checks = TunnelCheckStore(runtime_root) if runtime_root is not None else None
        self._journal = _RequestJournal(runtime_root)
        # Mutations, verification, and final review for one project run one at a time,
        # in arrival order, so controller generations, read receipts, and verification
        # ordering stay linear. Projects do not block each other, and read-only
        # observation takes no project lock, so a long check never blocks reading.
        self._project_locks: dict[str, threading.Lock] = {}
        self._controllers: dict[str, _ProjectWorkspace] = {}
        self._controllers_lock = threading.Lock()
        self._activity_lock = threading.Lock()
        self._activity: deque[dict[str, Any]] = deque(maxlen=TUNNEL_ACTIVITY_LIMIT)
        self._call_count = 0
        self._provider_call_counts: dict[str, int] = {}
        self._last_success: dict[tuple[str, str], dict[str, Any]] = {}
        self._call_sequence = 0
        self._active_calls: dict[int, dict[str, Any]] = {}
        self._stopping = False
        _estimated_tool_tokens("")

    @property
    def registry(self) -> ProjectRegistry:
        return self._registry

    @property
    def selection_store(self) -> ProjectSelectionStore:
        return self._selection_store

    def stop(self) -> None:
        """Make any in-flight controller command stop at its next check."""
        self._stopping = True

    def activity_snapshot(self, provider: str | None = None) -> dict[str, Any]:
        """Return recent tool calls, optionally scoped to one trusted ingress.

        ``recent_usage`` sums the estimated tool text of the retained recent calls. It
        is a tool-text estimate, not model billing: MCP exposes neither conversation
        boundaries nor the model's own token usage.
        """
        normalized_provider = str(provider or "").strip().lower()
        with self._activity_lock:
            active_calls = list(self._active_calls.values())
            recent_calls = list(self._activity)
            if normalized_provider:
                active_calls = [
                    record
                    for record in active_calls
                    if record.get("provider") == normalized_provider
                ]
                recent_calls = [
                    record
                    for record in recent_calls
                    if record.get("provider") == normalized_provider
                ]
            estimates = [record.get("estimated_tokens") for record in recent_calls]
            known = [value for value in estimates if isinstance(value, int)]
            last_success = {
                project: dict(record)
                for (record_provider, project), record in self._last_success.items()
                if not normalized_provider or record_provider == normalized_provider
            }
            return {
                "call_count": (
                    self._provider_call_counts.get(normalized_provider, 0)
                    if normalized_provider
                    else self._call_count
                ),
                "active_calls": deepcopy(active_calls),
                "recent_calls": deepcopy(recent_calls),
                "recent_usage": {
                    "calls": len(recent_calls),
                    "estimated_tokens": sum(known) if known else (0 if not estimates else None),
                    "complete": len(known) == len(estimates),
                    "scope": "tool_text_estimate",
                },
                "last_success_by_project": last_success,
                # MCP exposes neither conversation task boundaries nor model billing.
                "task_usage": None,
                "usage_scope": "tool_call",
                "usage_encoding": "o200k_base",
            }

    # JSON-RPC transport -------------------------------------------------

    def handle(
        self,
        body: Any,
        headers: Mapping[str, str],
        *,
        provider: str = "chatgpt",
    ) -> tuple[int, Any | None]:
        """Return an HTTP status and JSON body (``None`` means no body)."""
        normalized_provider = str(provider or "").strip().lower()
        if normalized_provider not in {"chatgpt", "gemini"}:
            raise ValueError("Unknown authenticated MCP provider.")
        header_version = str(headers.get(MCP_PROTOCOL_VERSION_HEADER) or "").strip()
        if isinstance(body, list):
            if not body:
                return 400, _rpc_error(None, -32600, "Empty JSON-RPC batch.")
            if len(body) > MAX_MCP_BATCH_ITEMS:
                return 400, _rpc_error(
                    None,
                    -32600,
                    f"JSON-RPC batches are limited to {MAX_MCP_BATCH_ITEMS} items.",
                )
            responses = [
                response
                for item in body
                if (
                    response := self._handle_one(
                        item,
                        header_version,
                        normalized_provider,
                    )
                ) is not None
            ]
            return (200, responses) if responses else (202, None)
        response = self._handle_one(body, header_version, normalized_provider)
        if response is None:
            return 202, None
        return 200, response

    def _handle_one(
        self,
        request: Any,
        header_version: str,
        provider: str,
    ) -> dict[str, Any] | None:
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
            result = self._dispatch(method, params, stateless, provider)
        except McpRequestError as exc:
            return _rpc_error(request_id, exc.code, exc.message)
        if stateless:
            result.setdefault("resultType", "complete")
            result.setdefault("_meta", {}).setdefault(MCP_SERVER_INFO_META, _server_info())
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _dispatch(
        self,
        method: str,
        params: dict[str, Any],
        stateless: bool,
        provider: str,
    ) -> dict[str, Any]:
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
            return self._call_tool(params, provider)
        if method == "resources/list":
            return {"resources": []}
        if method == "resources/templates/list":
            return {"resourceTemplates": []}
        if method == "prompts/list":
            return {"prompts": []}
        raise McpRequestError(-32601, f"Method not found: {method}")

    # Tools --------------------------------------------------------------

    def _call_tool(self, params: dict[str, Any], provider: str) -> dict[str, Any]:
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
        call_id = self._start_activity(tool, arguments, provider=provider)
        response_text = None
        ok = False
        observation: dict[str, Any] = {}
        try:
            try:
                observation = self._run_tool(tool, arguments)
            except ToolFailure as exc:
                observation = exc.observation()
            except OSError as exc:
                observation = {
                    "ok": False,
                    "code": _os_error_code(exc),
                    "error": describe_workspace_error(exc),
                }
            except (RuntimeError, ValueError) as exc:
                message = str(exc)[:2_000]
                observation = {"ok": False, "code": _error_code(message), "error": message}
            if not observation.get("ok") and not observation.get("code"):
                observation["code"] = _error_code(str(observation.get("error") or ""))
            response_text = json.dumps(observation, ensure_ascii=False)
            if len(response_text) > MAX_TOOL_TEXT_CHARACTERS:
                response_text = json.dumps(
                    {
                        "ok": bool(observation.get("ok")),
                        "code": "text_projection_truncated",
                        "response_truncated": True,
                        "message": (
                            "The complete bounded result is available in structuredContent, "
                            "but its text projection exceeded the transport display limit."
                        ),
                        "next_step": (
                            "Request fewer files, a narrower search/path, or a smaller line "
                            "range before continuing."
                        ),
                    },
                    ensure_ascii=False,
                )
            ok = bool(observation.get("ok"))
        finally:
            duration = time.monotonic() - started
            self._finish_activity(call_id, ok, duration, response_text)
        LOGGER.info(
            "Tunnel tool %s provider=%s ok=%s duration=%.2fs project=%s target=%s%s",
            tool.name,
            provider,
            ok,
            duration,
            str(arguments.get("project") or "")[:64],
            _activity_target(arguments)[:200],
            "" if ok else f" error={str(observation.get('error') or '')[:200]}",
        )
        return {
            "content": [{"type": "text", "text": response_text}],
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
        availability_problem = project_availability(project)
        if availability_problem:
            if "replaced after this project identity" in availability_problem:
                raise ToolFailure(
                    "project_changed",
                    f"Project {project.id} changed on disk after it was resolved, so the "
                    "call was refused. Call current_project or project_overview again, "
                    "then start a new task with the new project identity.",
                    project=project.id,
                )
            raise ToolFailure(
                "project_unavailable",
                availability_problem,
                project=project.id,
            )
        pinned = arguments.get("project_identity")
        if pinned and pinned != project.identity:
            raise ToolFailure(
                "project_changed",
                f"Project {project.id} was re-registered (different folder or write access) "
                "since this task resolved it, so the call was refused. Call current_project "
                "or project_overview again and confirm the project with the user.",
                project=project.id,
            )
        if not tool.read_only and not project.writable:
            raise ToolFailure(
                "read_only_project",
                f"Project {project.id} is read-only; {tool.name} is not allowed there.",
                project=project.id,
            )
        request_id = arguments.get("request_id")
        arguments = {
            key: value
            for key, value in arguments.items()
            if key not in {"project", "project_identity", "request_id"}
        }
        if request_id:
            return self._run_journaled(tool, project, arguments, str(request_id))
        if not tool.project_lock:
            project = self._revalidate_project(project, tool)
            return self._dispatch_tool(tool, project, arguments)
        with self._project_lock(project):
            project = self._revalidate_project(project, tool)
            return self._dispatch_tool(tool, project, arguments)

    def _revalidate_project(
        self,
        project: TunnelProject,
        tool: TunnelTool | None = None,
    ) -> TunnelProject:
        """Recheck registry authority after any wait and before touching project state."""
        try:
            current = self._registry.resolve(project.id, self._fallback_workspace())
        except ProjectRegistryError as exc:
            raise ToolFailure(
                "project_changed",
                f"Project {project.id} is no longer authorized by the current registry. "
                "Call current_project or project_overview again before making another call.",
                project=project.id,
            ) from exc
        if current.identity != project.identity:
            raise ToolFailure(
                "project_changed",
                f"Project {project.id} changed while this call was queued, so it was refused. "
                "nothing was replayed. Call current_project or project_overview again "
                "and confirm the current project identity before retrying.",
                project=project.id,
            )
        availability_problem = project_availability(current)
        if availability_problem:
            code = (
                "project_changed"
                if "replaced after this project identity" in availability_problem
                else "project_unavailable"
            )
            raise ToolFailure(code, availability_problem, project=project.id)
        if tool is not None and not tool.read_only and not current.writable:
            raise ToolFailure(
                "read_only_project",
                f"Project {project.id} is read-only; {tool.name} is not allowed there.",
                project=project.id,
            )
        return current

    def _run_journaled(
        self,
        tool: TunnelTool,
        project: TunnelProject,
        arguments: dict[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        """Run one mutation at most once per request id."""
        if not _REQUEST_ID_RE.fullmatch(request_id):
            raise ToolFailure(
                "invalid_arguments",
                "request_id must contain 8 to 128 letters, digits, '.', '_', ':', or '-'.",
            )
        key = hashlib.sha256(
            f"{project.id}\0{project.identity}\0{request_id}".encode("utf-8")
        ).hexdigest()[:40]
        fingerprint = hashlib.sha256(
            json.dumps({"tool": tool.name, "arguments": arguments}, sort_keys=True).encode("utf-8")
        ).hexdigest()
        with self._project_lock(project):
            project = self._revalidate_project(project, tool)
            try:
                existing = self._journal.get(key)
            except _RequestOutcomeUnknown as exc:
                raise ToolFailure("outcome_unknown", str(exc)) from exc
            except _RequestJournalError as exc:
                raise ToolFailure("journal_unavailable", str(exc)) from exc
            if existing is not None:
                if existing.get("fingerprint") != fingerprint:
                    raise ToolFailure(
                        "request_id_reused",
                        "This request_id was already used for a different change. Use a new "
                        "request_id for a new change.",
                    )
                if existing.get("state") == "completed" and isinstance(existing.get("result"), dict):
                    return {
                        **existing["result"],
                        "replayed": True,
                        "note": (
                            "This request_id was already processed; nothing was changed again. "
                            "This is the recorded result of the original request."
                        ),
                    }
                if existing.get("state") == "expired":
                    raise ToolFailure(
                        "outcome_unknown",
                        "The result for this request_id has expired from replay storage. "
                        "Nothing was retried. Read the affected paths and reconcile the change "
                        "before continuing with a new request_id.",
                    )
                raise ToolFailure(
                    "outcome_unknown",
                    "An earlier attempt with this request_id started but its outcome was not "
                    "recorded (the service may have stopped mid-change). Nothing was retried. "
                    "Read the affected files, compare them with the intended change, and use a "
                    "new request_id only if the change is still missing.",
                )
            try:
                self._journal.put(
                    key,
                    {
                        "fingerprint": fingerprint,
                        "tool": tool.name,
                        "state": "started",
                        "at": time.time(),
                    },
                )
            except _RequestJournalError as exc:
                self._journal.remove(key)
                raise ToolFailure("journal_unavailable", str(exc)) from exc
            try:
                result = self._dispatch_tool(tool, project, arguments)
            except ToolFailure as exc:
                result = exc.observation()
            except OSError as exc:
                result = {
                    "ok": False,
                    "code": _os_error_code(exc),
                    "error": describe_workspace_error(exc),
                }
            except (RuntimeError, ValueError) as exc:
                message = str(exc)[:2_000]
                result = {"ok": False, "code": _error_code(message), "error": message}
            completed = {
                "fingerprint": fingerprint,
                "tool": tool.name,
                "state": "completed",
                "at": time.time(),
                "result": result,
            }
            try:
                self._journal.put(
                    key,
                    completed,
                )
            except _RequestJournalError:
                # The durable "started" record remains on disk, so a restarted service
                # refuses to guess or replay. This process can still return and replay the
                # in-memory completed result.
                result = {
                    **result,
                    "request_record_persisted": False,
                    "retry_warning": (
                        "The change result could not replace its durable started record. "
                        "Do not retry after a service restart; read the affected paths first."
                    ),
                }
                completed["result"] = result
                self._journal.remember(key, completed)
            return result

    def _dispatch_tool(
        self,
        tool: TunnelTool,
        project: TunnelProject,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            if tool.action:
                if tool.name == "delete_file":
                    return self._tool_delete_file(project, arguments)
                if tool.name == "review_changes":
                    return self._tool_review_changes(project, arguments)
                return self._execute_action(project, tool.action, arguments)
            return getattr(self, f"_tool_{tool.name}")(project, arguments)
        except RuntimeError as exc:
            if "start a new task" not in str(exc):
                raise
            self._discard_controller(project)
            raise ToolFailure(
                "project_changed",
                f"Project {project.id} changed on disk during this request, so nothing "
                "was replayed against the replacement folder. Call current_project or "
                "project_overview again, then start a new task with the new project "
                "identity.",
                project=project.id,
            ) from exc

    def _fallback_workspace(self) -> str:
        return str(getattr(self._settings_provider(), "workspace_path", "") or "")

    def _host_file_limits(self) -> dict[str, Any]:
        """Return capabilities that are true for this service instance."""
        limits = host_file_limits()
        limits["background_checks"] = self._checks is not None
        return limits

    def _project_lock(self, project: TunnelProject) -> threading.Lock:
        with self._controllers_lock:
            return self._project_locks.setdefault(f"{project.id}\0{project.root}", threading.Lock())

    def _binding(self, project: TunnelProject) -> _ProjectWorkspace:
        """Return this project's workspace access, rebuilt whenever its registration changes."""
        with self._controllers_lock:
            binding = self._controllers.get(project.id)
            if (
                binding is None
                or binding.root != project.root
                or binding.writable != project.writable
                or binding.filesystem_identity != project.filesystem_identity
            ):
                if not project.filesystem_identity_matches():
                    raise ToolFailure(
                        "project_changed",
                        f"Project {project.id} changed on disk while this request was "
                        "starting, so the call was refused. Call current_project or "
                        "project_overview again, then start a new task with the new "
                        "project identity.",
                        project=project.id,
                    )
                binding = _ProjectWorkspace(
                    project.root,
                    project.writable,
                    project.filesystem_identity,
                    open_workspace(
                        project.root,
                        self._settings_provider(),
                        lambda: self._stopping,
                        read_only=not project.writable,
                    ),
                )
                if not project.filesystem_identity_matches():
                    raise ToolFailure(
                        "project_changed",
                        f"Project {project.id} changed on disk while its workspace was "
                        "opening, so the call was refused. Call current_project or "
                        "project_overview again, then start a new task with the new "
                        "project identity.",
                        project=project.id,
                    )
                self._controllers[project.id] = binding
            return binding

    def _workspace_for(self, project: TunnelProject) -> WorkspaceAccess:
        return self._binding(project).access

    def _discard_controller(self, project: TunnelProject) -> None:
        with self._controllers_lock:
            self._controllers.pop(project.id, None)

    def _execute_action(
        self,
        project: TunnelProject,
        action: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        workspace = self._workspace_for(project)
        try:
            if action == "delete":
                raw_path = arguments.get("path")
                if raw_path:
                    # Any Tunnel edit advances the workspace generation and ages every
                    # existing receipt. The workspace refreshes only a receipt this client
                    # already holds with the same digest, so a client that never read the
                    # file gains no synthesized receipt.
                    workspace.refresh_stale_read_receipt(
                        self._relative(project, raw_path),
                        expected_sha256=str(arguments.get("expected_sha256") or ""),
                    )
            payload = {**arguments, "action": action}
            observation = workspace.execute(payload)
        except RuntimeError as exc:
            if "start a new task" not in str(exc):
                raise
            self._discard_controller(project)
            raise ToolFailure(
                "project_changed",
                f"Project {project.id} changed on disk during this request, so nothing "
                "was replayed against the replacement folder. Call current_project or "
                "project_overview again, then start a new task with the new project "
                "identity.",
                project=project.id,
            ) from exc
        if "start a new task" in str(observation.get("error") or ""):
            self._discard_controller(project)
            raise ToolFailure(
                "project_changed",
                f"Project {project.id} changed on disk during this request, so nothing "
                "was replayed against the replacement folder. Call current_project or "
                "project_overview again, then start a new task with the new project "
                "identity.",
                project=project.id,
            )
        return observation

    def _relative(self, project: TunnelProject, raw_path: Any) -> str:
        """Admit one project path through the shared workspace path rules."""
        return self._workspace_for(project).project_relative_path(raw_path, allow_missing=True)

    # Discovery ------------------------------------------------------------

    def current_project_record(self) -> dict[str, Any]:
        """Return the model-facing current-project discovery result."""
        fallback = self._fallback_workspace()
        try:
            projects = self._registry.projects(fallback)
        except ProjectRegistryError as exc:
            return {
                "ok": False,
                "code": "project_unavailable",
                "error": f"{exc} Fix tunnel-projects.json on this computer.",
            }
        selection = self._selection_store.load()
        current = resolve_current_project(projects, selection, fallback)
        registry_configured = not self._registry.uses_fallback()
        preferred_projects = selected_projects(projects, selection)
        selected_ids = {project.id for project in preferred_projects}

        def record(project: TunnelProject) -> dict[str, Any]:
            problem = project_availability(project)
            item = {
                **project.public_record(),
                "registered": registry_configured,
                "available": not problem,
                "selected": project.id in selected_ids,
            }
            if problem:
                item["problem"] = problem
            return item

        result: dict[str, Any] = {
            "ok": True,
            "current_project": record(current.project) if current.project else None,
            "selection": {
                "revision": selection.revision,
                "source": current.source,
                "selected_at": _iso(selection.selected_at) if selection.selected_at else None,
                "selected_project_ids": [
                    project.id for project in preferred_projects
                ],
            },
            "projects": [record(project) for project in projects],
            "limits": self._host_file_limits(),
        }
        if self._registry.uses_fallback():
            result["registry"] = (
                "No project registry exists, so only the Agent's selected Git folder is "
                "offered, read-only."
            )
        if current.project is not None:
            result["next_step"] = (
                f'Call project_overview with project="{current.project.id}", read its '
                "instruction files, and keep this id and identity for the whole task."
            )
        else:
            result["problem"] = current.problem
            result["next_step"] = (
                "Ask the user which registered project to use, or ask them to select one on "
                "the local AgenticContext Tunnel page, then call current_project again."
            )
        return result

    def _tool_current_project(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        return self.current_project_record()

    def _tool_project_overview(self, project: TunnelProject, _arguments: dict[str, Any]) -> dict[str, Any]:
        listing = self._execute_action(project, "list", {"path": ".", "depth": 1})
        if not listing.get("ok"):
            return {
                **listing,
                "ok": False,
                "project": project.id,
                "identity": project.identity,
            }
        root_files, nested_files = _project_instruction_files(project.root)
        result: dict[str, Any] = {
            "ok": True,
            "project": project.id,
            "identity": project.identity,
            "writable": project.writable,
            "instruction_files": root_files,
            "top_level_entries": listing.get("entries", []),
            "git_status": status_summary(project.root, withheld=_withheld_diff_path),
            "limits": self._host_file_limits(),
            "workflow": SERVER_INSTRUCTIONS,
        }
        if nested_files:
            result["nested_instruction_files"] = nested_files
        if project.writable:
            result["verification_current"] = self._workspace_for(project).verification_current
            if self._checks is not None:
                active = self._checks.active_jobs(project.id, project.root)
                if active:
                    result["active_checks"] = active
        return result

    # Files ------------------------------------------------------------------

    def _tool_read_files(self, project: TunnelProject, arguments: dict[str, Any]) -> dict[str, Any]:
        results = []
        remaining_content = MAX_READ_BATCH_CONTENT_CHARACTERS
        requested_files = arguments["files"]
        for index, item in enumerate(requested_files):
            files_left = len(requested_files) - index
            item_budget = max(
                1,
                min(MAX_FILE_READ_CHARS, remaining_content // files_left),
            )
            payload = {
                key: item[key]
                for key in ("path", "start_line", "end_line", "start_character")
                if key in item
            }
            payload["max_characters"] = item_budget
            observation = self._execute_action(project, "read", payload)
            remaining_content = max(
                0,
                remaining_content - len(str(observation.get("content") or "")),
            )
            if not observation.get("ok"):
                observation.setdefault("path", str(item.get("path") or ""))
                observation.setdefault("code", _error_code(str(observation.get("error") or "")))
            results.append(observation)
        succeeded = sum(1 for result in results if result.get("ok"))
        if succeeded == len(results):
            outcome = "all_succeeded"
        elif succeeded:
            outcome = "partial"
        else:
            outcome = "all_failed"
        result: dict[str, Any] = {
            "ok": outcome == "all_succeeded",
            "outcome": outcome,
            "succeeded": succeeded,
            "failed": len(results) - succeeded,
            "files": results,
        }
        if outcome != "all_succeeded":
            if outcome == "partial":
                result["error"] = (
                    f"{len(results) - succeeded} of {len(results)} files could not be read; "
                    "see each file's error. The successful file results remain usable."
                )
            else:
                result["error"] = (
                    f"All {len(results)} requested files failed to read; see each file's error."
                )
        return result

    def _tool_apply_edits(self, project: TunnelProject, arguments: dict[str, Any]) -> dict[str, Any]:
        workspace = self._workspace_for(project)
        planned: OrderedDict[str, dict[str, Any]] = OrderedDict()
        for index, edit in enumerate(arguments["edits"], start=1):
            old_text = edit["old_text"]
            new_text = edit["new_text"]
            relative = workspace.project_relative_path(edit["path"])
            if relative not in planned:
                snapshot = workspace.file_snapshot(relative)
                planned[relative] = {
                    "source": snapshot.as_text(),
                    "sha256": snapshot.sha256,
                    "text": snapshot.as_text(),
                    "replacements": 0,
                }
            expected = edit.get("expected_sha256")
            if expected is not None and expected != planned[relative]["sha256"]:
                if not _SHA256_RE.fullmatch(expected):
                    raise ToolFailure("invalid_arguments", "expected_sha256 must be lowercase hex.")
                raise ToolFailure(
                    "stale_file",
                    f"Edit {index}: {relative} changed since you read it, so no file was "
                    "changed. If an earlier apply_edits response was lost, it may already be "
                    "applied: read the file again and compare before retrying.",
                    path=relative,
                    current_sha256=planned[relative]["sha256"],
                )
            current = planned[relative]["text"]
            occurrences = current.count(old_text)
            if occurrences == 0:
                raise ToolFailure(
                    "edit_mismatch",
                    f"Edit {index}: old_text was not found in {relative}, so no file was "
                    "changed. Read the file again and copy the exact current text.",
                    path=relative,
                )
            if occurrences > 1 and not edit.get("replace_all"):
                raise ToolFailure(
                    "edit_mismatch",
                    f"Edit {index}: old_text appears {occurrences} times in {relative}, so no "
                    "file was changed. Add surrounding context or set replace_all.",
                    path=relative,
                )
            count = occurrences if edit.get("replace_all") else 1
            planned[relative]["text"] = current.replace(old_text, new_text, count)
            planned[relative]["replacements"] += count
        oversized = [
            relative
            for relative, plan in planned.items()
            if len(plan["text"].encode("utf-8")) > MAX_WORKSPACE_FILE_BYTES
        ]
        if oversized:
            raise ToolFailure(
                "too_large",
                f"{oversized[0]} would exceed the {MAX_WORKSPACE_FILE_BYTES:,}-byte file "
                "limit, so no file was changed.",
                path=oversized[0],
            )
        batch = workspace.apply_text_replacement_batch(
            [
                TextReplacement(relative, plan["source"], plan["text"])
                for relative, plan in planned.items()
            ]
        )
        files = batch["files"]
        for record in files:
            plan = planned.get(record["path"])
            if plan is not None and record["status"] == "written":
                record["replacements"] = plan["replacements"]
        if batch["outcome"] != "committed":
            return {
                "ok": False,
                "code": "batch_rolled_back" if batch["outcome"] == "rolled_back" else "batch_partial",
                "outcome": batch["outcome"],
                "error": batch.get("error") or "The edit batch was not applied.",
                "files": files,
                "next_step": (
                    "No file changed. Fix the cause, read the files again, and retry."
                    if batch["outcome"] == "rolled_back"
                    else "Some files are in a mixed state. Read every listed file, keep the "
                    "concurrent edits, and apply only what is still missing. recovery_path "
                    "holds a file's text from before this batch."
                ),
            }
        if not self._confirm_written(workspace, files):
            return {
                "ok": False,
                "code": "write_confirmation_failed",
                "outcome": "partial",
                "error": (
                    "The batch published every planned write, but at least one file could "
                    "not be read back with the expected SHA-256. Read every listed file "
                    "before deciding what remains to do."
                ),
                "files": files,
            }
        return {"ok": True, "outcome": "committed", "files": files}

    @staticmethod
    def _confirm_written(workspace: WorkspaceAccess, files: list[dict[str, Any]]) -> bool:
        """Read each written file back so the reported digest is what is on disk."""
        confirmed = True
        for record in files:
            try:
                on_disk = workspace.file_snapshot(record["path"]).sha256
            except (OSError, RuntimeError, ValueError) as exc:
                record["verified"] = False
                record["verify_error"] = describe_workspace_error(exc)
                confirmed = False
                continue
            record["verified"] = on_disk == record.get("sha256")
            if not record["verified"]:
                record["current_sha256"] = on_disk
                record["verify_error"] = "The file changed again right after this write."
                confirmed = False
        return confirmed

    def _tool_write_file(self, project: TunnelProject, arguments: dict[str, Any]) -> dict[str, Any]:
        content = arguments["content"]
        workspace = self._workspace_for(project)
        relative = workspace.project_relative_path(arguments["path"], allow_missing=True)
        snapshot = workspace.existing_file_snapshot(relative)
        expected = str(arguments.get("expected_sha256") or "")
        if snapshot is not None:
            if not expected:
                raise ToolFailure(
                    "already_exists",
                    f"{relative} already exists and was not changed. Read it with read_files "
                    "and pass its sha256 as expected_sha256 to replace it, or use apply_edits.",
                    path=relative,
                    current_sha256=snapshot.sha256,
                )
            if expected != snapshot.sha256:
                raise ToolFailure(
                    "stale_file",
                    f"{relative} changed since you read it, so it was not replaced. If an "
                    "earlier write_file response was lost, it may already be written: read the "
                    "file and compare before retrying.",
                    path=relative,
                    current_sha256=snapshot.sha256,
                )
            workspace.refresh_stale_read_receipt(
                relative,
                expected_sha256=expected,
            )
            snapshot = workspace.current_read_receipt_snapshot(
                relative,
                expected_sha256=expected,
            )
            source = snapshot.as_text()
        elif expected:
            raise ToolFailure(
                "not_found",
                f"{relative} no longer exists, so nothing was written. List the folder and "
                "read again; omit expected_sha256 only to create a new file on purpose.",
                path=relative,
            )
        data = content.encode("utf-8")
        if snapshot is not None:
            workspace.overwrite_text_file(relative, source=source, content=content)
        else:
            workspace.create_file(relative, data)
        record = {
            "path": relative,
            "created": snapshot is None,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        if not self._confirm_written(workspace, [record]):
            return {
                "ok": False,
                "code": "write_confirmation_failed",
                "outcome": "uncertain",
                "error": (
                    "The write was published but could not be read back with the expected "
                    "SHA-256. Read this path before retrying."
                ),
                **record,
            }
        return {"ok": True, "outcome": "committed", **record}

    def _tool_delete_file(self, project: TunnelProject, arguments: dict[str, Any]) -> dict[str, Any]:
        observation = self._execute_action(project, "delete", arguments)
        if observation.get("ok"):
            workspace = self._workspace_for(project)
            try:
                gone = workspace.existing_file_snapshot(observation["path"]) is None
            except (OSError, RuntimeError, ValueError) as exc:
                observation["ok"] = False
                observation["code"] = "delete_confirmation_failed"
                observation["outcome"] = "uncertain"
                observation["exists_after"] = None
                observation["error"] = (
                    "The delete was published, but the path could not be checked afterward: "
                    f"{describe_workspace_error(exc)} Read or list the path before retrying."
                )
                return observation
            observation["exists_after"] = not gone
            if not gone:
                observation["ok"] = False
                observation["code"] = "changed_after_write"
                observation["error"] = (
                    "The file was deleted, but a file with this name exists again. Read it "
                    "before continuing."
                )
        return observation

    # Verification -----------------------------------------------------------

    def _tool_review_changes(self, project: TunnelProject, arguments: dict[str, Any]) -> dict[str, Any]:
        observation = self._execute_action(project, "bodycheck", arguments)
        if project.writable and observation.get("ok"):
            workspace = self._workspace_for(project)
            if not workspace.verification_current:
                # Bodycheck refreshed the workspace fingerprint; a check that verified
                # older files (or none since this service started) cannot pass review.
                return {
                    **observation,
                    "ok": False,
                    "code": "verification_required",
                    "error": (
                        "No successful check covers the current files. Run run_check (or "
                        "start_check and observe_check) after the latest change, then review "
                        "again."
                    ),
                }
        return observation

    def _require_checks(self) -> TunnelCheckStore:
        if self._checks is None:
            raise ToolFailure(
                "unsupported",
                "Background checks are unavailable in this service; use run_check.",
            )
        return self._checks

    def _tool_start_check(self, project: TunnelProject, arguments: dict[str, Any]) -> dict[str, Any]:
        checks = self._require_checks()
        command = arguments["command"].strip()
        argv = inspection_command_parts(command, workspace=project.root)
        if argv[:2] == ["git", "status"]:
            raise ToolFailure(
                "command_refused",
                "Use run_check for git status; start_check is for long checks.",
            )
        binding = self._binding(project)
        evidence = binding.access.begin_external_verification()
        evidence["binding_epoch"] = binding.epoch
        evidence["project_identity"] = project.identity
        key = str(arguments.get("idempotency_key") or "")
        if key and not _IDEMPOTENCY_KEY_RE.fullmatch(key):
            raise ToolFailure(
                "invalid_arguments",
                "idempotency_key must contain 8 to 128 letters, digits, '.', '_', ':', or '-'.",
            )
        timeout = int(arguments.get("timeout_seconds") or CHECK_DEFAULT_TIMEOUT_SECONDS)
        if not key:
            # The same command for the same files shares one job instead of rerunning.
            key = "auto-" + hashlib.sha256(
                json.dumps([argv, timeout, evidence["snapshot_id"]]).encode("utf-8")
            ).hexdigest()[:40]
        try:
            job_dir, metadata, deduplicated = checks.start(
                project_id=project.id,
                project_root=project.root,
                argv=argv,
                command=command,
                idempotency_key=key,
                timeout_seconds=timeout,
                evidence=evidence,
            )
        except CheckJobError as exc:
            raise ToolFailure("check_refused", str(exc)) from exc
        status = checks.status(job_dir, metadata)
        if status["state"] in CHECK_TERMINAL_STATES:
            return self._check_observation(project, job_dir, metadata, status, deduplicated)
        result: dict[str, Any] = {
            "ok": True,
            "job": status,
            "next_step": f'Call observe_check with job_id="{status["job_id"]}" until it finishes.',
        }
        if deduplicated:
            result["deduplicated"] = True
        return result

    def _tool_observe_check(self, project: TunnelProject, arguments: dict[str, Any]) -> dict[str, Any]:
        checks = self._require_checks()
        try:
            job_dir, metadata = checks.find(project.id, project.root, arguments["job_id"])
        except CheckJobError as exc:
            raise ToolFailure("not_found", str(exc)) from exc
        status = checks.status(job_dir, metadata)
        if status["state"] not in CHECK_TERMINAL_STATES:
            return {"ok": True, "job": status}
        # Recording terminal verification evidence mutates the workspace evidence
        # state, so only that short reconciliation step joins the mutation lock.
        with self._project_lock(project):
            project = self._revalidate_project(project)
            return self._check_observation(project, job_dir, metadata, status)

    def _tool_stop_check(self, project: TunnelProject, arguments: dict[str, Any]) -> dict[str, Any]:
        checks = self._require_checks()
        try:
            job_dir, metadata = checks.find(project.id, project.root, arguments["job_id"])
        except CheckJobError as exc:
            raise ToolFailure("not_found", str(exc)) from exc
        status = checks.stop(job_dir, metadata)
        observation = self._check_observation(project, job_dir, metadata, status)
        if status["state"] not in CHECK_TERMINAL_STATES:
            observation["ok"] = False
            observation["code"] = "check_running"
            observation["error"] = "The check has not stopped yet; observe it again shortly."
        elif status["state"] != "unknown":
            observation["ok"] = True
            observation.pop("error", None)
            observation.pop("code", None)
        return observation

    def _check_observation(
        self,
        project: TunnelProject,
        job_dir: Path,
        metadata: dict[str, Any],
        status: dict[str, Any],
        deduplicated: bool = False,
    ) -> dict[str, Any]:
        """Map one job state to a truthful result, recording evidence exactly once."""
        state = status["state"]
        extra = {"deduplicated": True} if deduplicated else {}
        if state not in CHECK_TERMINAL_STATES:
            return {"ok": True, "job": status, **extra}
        evaluation = self._evaluate_check(project, job_dir, metadata, state)
        status.update(evaluation)
        if state == "succeeded" and evaluation.get("verification") == "recorded":
            return {"ok": True, "job": status, **extra}
        if state == "succeeded":
            error = (
                "The command passed, but it does not verify the current files: "
                f"{evaluation.get('reason') or 'the project changed'}. Run the check again."
            )
            code = "stale_verification"
        elif state == "unknown":
            error = "The check outcome is unknown; treat the project as unverified."
            code = "check_unknown"
        else:
            error = f"The check ended as {state}; the project is not verified."
            code = "check_failed"
        return {"ok": False, "code": code, "error": error, "job": status, **extra}

    def _evaluate_check(
        self,
        project: TunnelProject,
        job_dir: Path,
        metadata: dict[str, Any],
        state: str,
    ) -> dict[str, Any]:
        checks = self._require_checks()
        binding = self._binding(project)
        evidence = metadata.get("evidence") if isinstance(metadata.get("evidence"), dict) else {}
        same_binding = evidence.get("binding_epoch") == binding.epoch
        recorded_identity = str(evidence.get("project_identity") or "")
        if recorded_identity and recorded_identity != project.identity:
            evaluation: dict[str, Any] = {
                "verification": "not_recorded",
                "reason": "the project was re-registered after the check started",
            }
        elif not recorded_identity and not same_binding:
            # A pre-upgrade record has no durable authority fingerprint. Its command
            # result remains queryable, but a new service cannot promote it to current
            # verification evidence.
            evaluation = {
                "verification": "not_recorded",
                "reason": "this older check record cannot prove its project identity",
            }
        else:
            recorded = checks.evaluation(job_dir)
            if recorded is not None and recorded.get("verification") != "recorded":
                # Terminal failure reconciliation is monotonic. Re-observing an
                # older failed job must not withdraw verification recorded later by
                # a newer successful job for the same workspace snapshot.
                return recorded
            if recorded is not None:
                current = binding.access.refresh_workspace_evidence()
                if (
                    not current.get("evidence_complete")
                    or current.get("snapshot_id") != evidence.get("snapshot_id")
                ):
                    return {
                        **recorded,
                        "verification": "not_recorded",
                        "workspace_changed": True,
                        "reason": "The project changed after the check started.",
                    }
                if (
                    not same_binding
                    and not binding.access.verification_current
                    and checks.latest_evaluated_job(project.id, project.root)
                    == metadata.get("job_id")
                ):
                    # After a service restart, only the newest reconciled successful
                    # job may restore evidence, and only after an exact fingerprint
                    # match. Older successes and all failures remain observation-only.
                    rebound = binding.access.finish_external_verification(
                        evidence,
                        command=str(metadata.get("command") or ""),
                        succeeded=True,
                        allow_generation_rebind=True,
                    )
                    if rebound.get("verification") != "recorded":
                        return rebound
                if binding.access.verification_current:
                    return recorded
                return {
                    **recorded,
                    "verification": "not_recorded",
                    "workspace_changed": False,
                    "reason": (
                        "This check's persisted pass is no longer the current verification; "
                        "run the check again."
                    ),
                }
            evaluation = binding.access.finish_external_verification(
                evidence,
                command=str(metadata.get("command") or ""),
                succeeded=state == "succeeded",
                allow_generation_rebind=not same_binding,
            )
        checks.record_evaluation(job_dir, evaluation)
        return evaluation

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

    # Activity ---------------------------------------------------------------

    def _start_activity(
        self,
        tool: TunnelTool,
        arguments: dict[str, Any],
        *,
        provider: str = "chatgpt",
    ) -> int:
        """Assign a call identity before execution, including time waiting for its lock."""
        target = _activity_target(arguments)
        project = str(arguments.get("project") or "")
        if project and isinstance(arguments.get("project"), str):
            target = f"{project[:64]}: {target}" if target else project[:64]
        request_tokens = _estimated_tool_tokens(
            json.dumps({"name": tool.name, "arguments": arguments}, ensure_ascii=False)
        )
        with self._activity_lock:
            self._call_sequence += 1
            call_id = self._call_sequence
            self._active_calls[call_id] = {
                "call_id": call_id,
                "provider": provider,
                "tool": tool.name,
                "project": project[:64],
                "target": target[:160],
                "state": "running",
                "started_at": time.time(),
                "ok": None,
                "request_tokens": request_tokens,
                "response_tokens": None,
                "estimated_tokens": request_tokens,
                "usage_partial": True,
            }
            return call_id

    def _finish_activity(
        self, call_id: int, ok: bool, duration: float, response_text: str | None
    ) -> None:
        # Count the visible text once, not its duplicate structuredContent envelope.
        response_tokens = (
            _estimated_tool_tokens(response_text) if response_text is not None else None
        )
        with self._activity_lock:
            record = self._active_calls.pop(call_id)
            request_tokens = record["request_tokens"]
            record.update({
                "state": "completed" if ok else "failed",
                "ok": ok,
                "duration_seconds": round(duration, 2),
                "at": time.time(),
                "response_tokens": response_tokens,
                "estimated_tokens": (
                    request_tokens + response_tokens
                    if request_tokens is not None and response_tokens is not None
                    else None
                ),
                "usage_partial": False,
            })
            self._call_count += 1
            provider = str(record.get("provider") or "chatgpt")
            self._provider_call_counts[provider] = (
                self._provider_call_counts.get(provider, 0) + 1
            )
            if ok and record.get("project"):
                self._last_success[(provider, record["project"])] = {
                    "tool": record["tool"],
                    "at": record["at"],
                }
            self._activity.appendleft(record)


def _iso(timestamp: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


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
            raise ToolFailure(
                "path_refused",
                "Paths must be relative to the project root; absolute paths are not accepted.",
            )


def _withheld_diff_path(relative: str) -> bool:
    """Keep credential and controller-internal files out of model-visible diffs."""
    path = Path(relative)
    return is_withheld_workspace_path(path) or ".computer-use-agent" in path.parts


def _project_instruction_files(root: Path) -> tuple[list[str], list[str]]:
    """Return root and nested instruction files that belong to this project only.

    A nested directory with its own ``.git`` is another repository, so its
    instruction files are excluded along with everything beneath it.
    """
    root_files: list[str] = []
    nested_files: list[str] = []
    for path in collect_instruction_files(root):
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
