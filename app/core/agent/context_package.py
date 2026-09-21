"""Build the bounded Markdown context package for one fresh Agent conversation.

This is pure file selection and Markdown assembly over one already-admitted project
root. Path admission stays in ``app.core.workspace``; this module only decides what is
worth including and how to stay inside the configured byte budget.
"""

# Code version: v1.0.0-claude.0

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..workspace import (
    MAX_FILE_READ_CHARS,
    collect_instruction_files,
    filtered_git_status,
    is_safe_context_directory,
    is_safe_context_file,
)
from .platform_catalog import (
    AGENT_PLATFORM_BY_KEY,
    DEFAULT_AGENT_PLATFORM,
    detect_host_operating_system,
)

LOGGER = logging.getLogger(__name__)


@runtime_checkable
class ContextPackageSettings(Protocol):
    """The task settings a context package reads, and nothing else."""

    @property
    def context_limit_mib(self) -> int:
        """Return the byte budget, in MiB, for the assembled bundle."""

    @property
    def operating_system(self) -> str:
        """Return the requested controller environment."""

    @property
    def platform(self) -> str:
        """Return the selected Web provider key."""

    @property
    def system_prompt(self) -> str:
        """Return the prompt configured for the selected operating system."""

_CONTEXT_PRIORITY_NAMES = (
    "AGENTS.md",
    "CLAUDE.md",
    "CODEX.md",
    "README.md",
    "pyproject.toml",
    "package.json",
    "requirements.txt",
    "Cargo.toml",
    "go.mod",
)

def _format_binary_size(byte_count: int) -> str:
    """Format a byte count with IEC binary units for user-facing status text."""
    size = max(0, int(byte_count))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    unit_index = 0
    value = float(size)
    while value >= 1_024 and unit_index < len(units) - 1:
        value /= 1_024
        unit_index += 1
    if unit_index == 0:
        return f"{size:,} {units[unit_index]}"
    return f"{value:,.2f} {units[unit_index]}"

def _utf8_prefix(value: bytes, maximum: int) -> bytes:
    return value[: max(0, maximum)].decode("utf-8", errors="ignore").encode("utf-8")

def _project_file_index(workspace: Path) -> list[str]:
    """Build a deterministic, bounded file index without subprocess buffering."""
    paths: list[str] = []
    inspected_directories = 0
    try:
        for raw_directory, directory_names, file_names in os.walk(
            workspace,
            topdown=True,
            followlinks=False,
        ):
            inspected_directories += 1
            if inspected_directories > 12_000:
                break
            directory = Path(raw_directory)
            allowed_directories: list[str] = []
            for name in sorted(directory_names, key=str.casefold):
                candidate = directory / name
                if not is_safe_context_directory(workspace, candidate):
                    continue
                allowed_directories.append(name)
            directory_names[:] = allowed_directories
            for name in sorted(file_names, key=str.casefold):
                path = directory / name
                relative = path.relative_to(workspace)
                if not is_safe_context_file(workspace, path):
                    continue
                paths.append(relative.as_posix())
                if len(paths) >= 12_000:
                    return paths
    except OSError:
        return paths
    return paths

def _priority_context_files(workspace: Path, instructions: list[Path]) -> list[Path]:
    instruction_set = set(instructions)
    files: list[Path] = []
    for name in _CONTEXT_PRIORITY_NAMES:
        candidate = workspace / name
        if (
            is_safe_context_file(workspace, candidate)
            and candidate not in instruction_set
        ):
            files.append(candidate)
    return files[:12]

def _markdown_file_section(workspace: Path, path: Path, maximum_chars: int) -> str:
    if not is_safe_context_file(workspace, path):
        return ""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            content = handle.read(maximum_chars)
    except OSError as exc:
        content = f"[Could not read file: {exc}]"
    suffix = path.suffix.lstrip(".") or "text"
    relative = path.relative_to(workspace).as_posix()
    return f"\n### `{relative}`\n\n```{suffix}\n{content}\n```\n"

def build_context_markdown(
    workspace: Path,
    user_request: str,
    settings: ContextPackageSettings,
    destination: Path,
) -> tuple[Path, int]:
    """Build a bounded initial context bundle for a fresh Web Agent conversation."""
    byte_limit = settings.context_limit_mib * 1_024 * 1_024
    platform_label = AGENT_PLATFORM_BY_KEY.get(
        settings.platform,
        AGENT_PLATFORM_BY_KEY[DEFAULT_AGENT_PLATFORM],
    )["label"]
    sections = [
        "# Local Computer Use task\n",
        "## Request\n\n" + user_request.strip() + "\n",
        "## Execution environment\n\n"
        f"- Host controller: {('Windows' if detect_host_operating_system() == 'windows' else 'macOS')}\n"
        f"- Requested environment: {settings.operating_system}\n"
        f"- Project name: {workspace.name}\n"
        f"- Project root: `{workspace}`\n"
        f"- The local controller, not {platform_label} Web, performs every file and command action.\n"
        "- Treat each controller result as the only evidence that an action succeeded.\n",
        "## Controller contract\n\n" + settings.system_prompt + "\n",
    ]
    instructions = collect_instruction_files(workspace)
    if instructions:
        sections.append("## Repository instructions\n")
        for path in instructions:
            sections.append(_markdown_file_section(workspace, path, MAX_FILE_READ_CHARS))

    if (workspace / ".git").exists():
        status = filtered_git_status(workspace)
        if status.strip():
            sections.append("## Existing working tree\n\n```text\n" + status.strip() + "\n```\n")

    file_index = _project_file_index(workspace)
    sections.append("## Project file index\n\n```text\n" + "\n".join(file_index) + "\n```\n")

    priority_files = _priority_context_files(workspace, instructions)
    if priority_files:
        sections.append("## Project entry files\n")
        for path in priority_files:
            sections.append(_markdown_file_section(workspace, path, 80_000))

    encoded_parts: list[bytes] = []
    used = 0
    truncation_note = b"\n## Context limit\n\nThe local bundle reached its configured byte limit. Request additional files with controller actions.\n"
    for section in sections:
        encoded = section.encode("utf-8", errors="replace")
        if used + len(encoded) <= byte_limit:
            encoded_parts.append(encoded)
            used += len(encoded)
            continue
        remaining = byte_limit - used - len(truncation_note)
        if remaining > 0:
            encoded_parts.append(_utf8_prefix(encoded, remaining))
        encoded_parts.append(truncation_note)
        break

    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.parent.chmod(0o700)
        destination.write_bytes(b"".join(encoded_parts))
        destination.chmod(0o600)
        byte_count = destination.stat().st_size
    except Exception:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            LOGGER.exception(
                "Could not remove a partially prepared Agent context: %s",
                destination,
            )
        raise
    return destination, byte_count


__all__ = [
    "ContextPackageSettings",
    "build_context_markdown",
]
