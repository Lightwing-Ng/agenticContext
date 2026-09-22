"""Workspace control: one project root, one set of safety rules, two connections.

This package owns everything that decides what may happen inside a user's selected
project: path admission, symlink and file-identity checks, read receipts and expired
writes, the approved-command allow-list, bounded process output, and workspace
evidence. The Browser Agent run loop and the Tunnel MCP server are two callers of the
same rules, never two copies of them.

Depend on :class:`WorkspaceAccess` unless you are the Agent run loop itself; it is the
narrow surface that keeps controller internals out of other modules.
"""

# Code version: v1.1.0-claude.0

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .action_state import ActionState
from .capabilities import (
    FileSnapshot,
    TextReplacement,
    WorkspaceAccess,
    describe_workspace_error,
)
from .command_policy import inspection_command_parts, validate_inspection_command
from .controller import (
    MAX_CONTROLLER_DELETE_BYTES as MAX_WORKSPACE_FILE_BYTES,
    MAX_FILE_READ_CHARS,
    WorkspaceCommandSettings,
    WorkspaceController,
)
from .evidence import _filtered_git_status as filtered_git_status
from .executables import _trusted_system_executable as trusted_system_executable
from .process_io import _truncate_text as truncate_text
from .paths import (
    _collect_instruction_files as collect_instruction_files,
    _is_safe_context_directory as is_safe_context_directory,
    _is_safe_context_file as is_safe_context_file,
    _path_has_controller_internal_file,
    _path_has_sensitive_part,
)


def open_workspace(
    root: Path,
    settings: WorkspaceCommandSettings,
    should_stop: Callable[[], bool],
    *,
    read_only: bool = False,
) -> WorkspaceAccess:
    """Open one project root through the shared safety rules.

    Callers that are not the Agent run loop use this instead of constructing a
    controller, so they receive the narrow :class:`WorkspaceAccess` surface and
    cannot drift onto controller internals.
    """
    return WorkspaceController(
        root,
        settings,
        should_stop,
        read_only=read_only,
    )


def is_withheld_workspace_path(relative: Path) -> bool:
    """Return whether a project-relative path must stay out of model-visible output.

    Credential material and the controller's own temporary files are withheld from
    every surface that shows workspace content back to a model.
    """
    return _path_has_sensitive_part(relative) or _path_has_controller_internal_file(relative)


__all__ = [
    "MAX_FILE_READ_CHARS",
    "MAX_WORKSPACE_FILE_BYTES",
    "ActionState",
    "FileSnapshot",
    "TextReplacement",
    "WorkspaceAccess",
    "WorkspaceCommandSettings",
    "WorkspaceController",
    "collect_instruction_files",
    "describe_workspace_error",
    "filtered_git_status",
    "inspection_command_parts",
    "is_safe_context_directory",
    "is_safe_context_file",
    "is_withheld_workspace_path",
    "open_workspace",
    "truncate_text",
    "trusted_system_executable",
    "validate_inspection_command",
]
