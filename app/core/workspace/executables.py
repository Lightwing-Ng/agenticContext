"""Trusted system executable resolution for workspace commands."""

# Code version: v1.0.0-claude.0

from __future__ import annotations

import os
from pathlib import Path
import shutil


def _portable_executable_names(value: str) -> tuple[str, str]:
    """Return the portable filename and normalized tool name for one argv token."""
    executable_name = value.replace("\\", "/").rsplit("/", 1)[-1]
    if not executable_name:
        raise ValueError("Run requires a named executable.")
    executable = (
        executable_name[:-4]
        if executable_name.casefold().endswith(".exe")
        else executable_name
    )
    return executable_name, executable.casefold()


def _trusted_system_executable(
    executable_name: str,
    *,
    forbidden_root: Path | None = None,
) -> Path | None:
    """Resolve one PATH tool once so workspace cwd cannot replace it at launch."""
    located = shutil.which(executable_name)
    if not located:
        return None
    try:
        resolved = Path(located).resolve(strict=True)
    except (OSError, ValueError):
        return None
    if not resolved.is_file():
        return None
    if forbidden_root is not None:
        try:
            resolved.relative_to(forbidden_root.resolve(strict=True))
        except (OSError, ValueError):
            pass
        else:
            return None
    return resolved


def _trusted_windows_taskkill() -> Path | None:
    """Resolve taskkill only from the Windows system directory."""
    system_root = os.environ.get("SystemRoot", "").strip()
    if not system_root:
        return None
    try:
        candidate = (Path(system_root) / "System32" / "taskkill.exe").resolve(
            strict=True
        )
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None
