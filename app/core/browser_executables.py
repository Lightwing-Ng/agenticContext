"""Host browser executable discovery shared by browser transports.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import os
from pathlib import Path
import re


def _expand_windows_browser_candidate(candidate: object) -> str:
    """Expand environment variables and normalize a Windows executable candidate."""
    normalized = str(candidate or "").strip()
    if len(normalized) >= 2 and normalized.startswith('"') and normalized.endswith('"'):
        normalized = normalized[1:-1].strip()
    expanded = os.path.expandvars(normalized)
    return re.sub(
        r"%([^%]+)%",
        lambda match: os.environ.get(match.group(1), match.group(0)),
        expanded,
    )


def _existing_windows_browser_file(candidate: object) -> str | None:
    """Return an absolute executable path only when it resolves to a real file."""
    expanded = _expand_windows_browser_candidate(candidate)
    if not expanded:
        return None
    try:
        executable = Path(expanded).expanduser()
        if not executable.is_absolute() or not executable.is_file():
            return None
    except (OSError, ValueError):
        return None
    return str(executable)


def _windows_registry_browser_candidate(executable_name: str) -> str | None:
    """Read one Windows App Paths entry without importing winreg on other hosts."""
    try:
        import winreg
    except ImportError:
        return None

    try:
        key_read = winreg.KEY_READ
        local_machine = winreg.HKEY_LOCAL_MACHINE
    except AttributeError:
        return None

    view_flags = (
        getattr(winreg, "KEY_WOW64_32KEY", 0),
        getattr(winreg, "KEY_WOW64_64KEY", 0),
        0,
    )
    access_modes: list[int] = []
    for view_flag in view_flags:
        access = key_read | view_flag
        if access not in access_modes:
            access_modes.append(access)

    key_path = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{executable_name}"
    for access in access_modes:
        try:
            with winreg.OpenKey(local_machine, key_path, 0, access) as key:
                value, _value_type = winreg.QueryValueEx(key, "")
        except (AttributeError, FileNotFoundError, OSError, PermissionError, TypeError, ValueError):
            continue
        resolved = _existing_windows_browser_file(value)
        if resolved is not None:
            return resolved
    return None


def resolve_windows_browser_executable(selected_browser: str) -> str | None:
    """Resolve an installed Windows Chromium browser executable."""
    browser = str(selected_browser or "").strip().lower()
    if browser not in {"edge", "chrome"}:
        return None

    program_files_x86 = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    program_files = os.environ.get("ProgramFiles") or r"C:\Program Files"
    local_app_data = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    if browser == "edge":
        executable_name = "msedge.exe"
        path_candidates = (
            Path(program_files_x86) / "Microsoft" / "Edge" / "Application" / executable_name,
            Path(program_files) / "Microsoft" / "Edge" / "Application" / executable_name,
        )
    else:
        executable_name = "chrome.exe"
        path_candidates = (
            Path(local_app_data) / "Google" / "Chrome" / "Application" / executable_name,
            Path(program_files) / "Google" / "Chrome" / "Application" / executable_name,
            Path(program_files_x86) / "Google" / "Chrome" / "Application" / executable_name,
        )

    for candidate in path_candidates:
        resolved = _existing_windows_browser_file(candidate)
        if resolved is not None:
            return resolved
    return _windows_registry_browser_candidate(executable_name)


__all__ = ["resolve_windows_browser_executable"]
