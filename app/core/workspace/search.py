"""Confined text search over one workspace root."""

# Code version: v1.0.0-claude.0

from __future__ import annotations

from glob import translate as translate_glob
import json
from pathlib import Path
import re
import time
from typing import Callable

from ..config import is_windows_host
from .paths import (
    _IGNORED_DIRECTORY_NAMES,
    _SENSITIVE_PATH_NAMES,
    _SENSITIVE_PATH_SUFFIXES,
    _path_has_controller_internal_file,
    _path_has_ignored_part,
    _path_has_sensitive_part,
    _path_is_link_like,
)


SEARCH_MAX_FILE_BYTES = 2 * 1_024 * 1_024


SEARCH_MAX_RAW_EVENTS = 12_000


SEARCH_MAX_MATCH_TEXT_CHARS = 4_000


SEARCH_TIMEOUT_SECONDS = 30


MAX_SEARCH_QUERY_CHARS = 8_000


def _search_exclusion_globs() -> tuple[str, ...]:
    """Return case-insensitive command-layer exclusions for every forbidden path."""
    names = _IGNORED_DIRECTORY_NAMES | _SENSITIVE_PATH_NAMES
    patterns = {
        pattern
        for name in names
        for pattern in (
            f"!{name}",
            f"!{name}/**",
            f"!**/{name}",
            f"!**/{name}/**",
        )
    }
    patterns.update({"!.env.*", "!**/.env.*"})
    patterns.update({"!.*.agent-*.tmp", "!**/.*.agent-*.tmp"})
    for suffix in _SENSITIVE_PATH_SUFFIXES:
        patterns.update({f"!*{suffix}", f"!**/*{suffix}"})
    return tuple(sorted(patterns, key=str.casefold))


def _search_include_globs(
    glob: str,
    root: Path,
    workspace: Path,
) -> tuple[str, ...]:
    """Translate controller glob candidates into conservative rg include globs."""
    if not glob:
        return ()
    normalized = (
        glob.replace("\\", "/")
        if is_windows_host()
        else glob.replace("\\", "\\\\")
    )
    patterns = {normalized}
    if root.is_dir():
        try:
            root_relative = root.relative_to(workspace).as_posix()
        except ValueError:
            root_relative = "."
        if root_relative not in {"", "."}:
            patterns.add(f"{root_relative}/{normalized}")
    return tuple(sorted(patterns, key=str.casefold))


def _path_matches_search_glob(
    path: Path,
    root: Path,
    glob: str,
    *,
    workspace: Path,
) -> bool:
    """Match glob against basename, workspace-relative path, and root-relative path."""
    if not glob:
        return True
    normalized_glob = glob.replace("\\", "/") if is_windows_host() else glob
    candidates = [path.name]
    try:
        workspace_relative = path.relative_to(workspace).as_posix()
        if workspace_relative not in candidates:
            candidates.append(workspace_relative)
    except ValueError:
        pass
    if not root.is_file():
        try:
            root_relative = path.relative_to(root).as_posix()
            if root_relative not in candidates:
                candidates.append(root_relative)
        except ValueError:
            pass
    translated_glob = translate_glob(
        normalized_glob,
        recursive=True,
        include_hidden=True,
        seps="/",
    )
    flags = re.IGNORECASE if is_windows_host() else 0
    return any(
        re.fullmatch(translated_glob, candidate, flags=flags) is not None
        for candidate in candidates
    )


def _is_confined_search_match(
    workspace: Path,
    root: Path,
    relative_path: Path,
) -> bool:
    """Confirm an rg-reported file resolves inside both workspace and search root."""
    candidate = workspace / relative_path
    try:
        current = workspace
        for part in relative_path.parts:
            current /= part
            if _path_is_link_like(current):
                return False
        if not candidate.is_file():
            return False
        resolved_candidate = candidate.resolve(strict=True)
        resolved_workspace = workspace.resolve(strict=True)
        resolved_relative = resolved_candidate.relative_to(resolved_workspace)
        if (
            _path_has_ignored_part(resolved_relative)
            or _path_has_controller_internal_file(resolved_relative)
            or _path_has_sensitive_part(resolved_relative)
        ):
            return False
        resolved_root = root.resolve(strict=True)
        if root.is_file():
            return resolved_candidate == resolved_root
        resolved_candidate.relative_to(resolved_root)
    except (OSError, ValueError):
        return False
    return True


def _truncate_search_match_text(value: str) -> str:
    """Bound one search line without introducing a second observation line."""
    if len(value) <= SEARCH_MAX_MATCH_TEXT_CHARS:
        return value
    omitted = len(value) - SEARCH_MAX_MATCH_TEXT_CHARS
    return (
        value[:SEARCH_MAX_MATCH_TEXT_CHARS]
        + f" [truncated {omitted:,} characters]"
    )


def _parse_rg_search_match(value: str) -> tuple[Path, str] | None:
    """Parse one structured ripgrep match in fallback-compatible form."""
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("type") != "match":
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    path_data = data.get("path")
    lines_data = data.get("lines")
    line_number = data.get("line_number")
    if (
        not isinstance(path_data, dict)
        or not isinstance(lines_data, dict)
        or not isinstance(path_data.get("text"), str)
        or not isinstance(lines_data.get("text"), str)
        or not isinstance(line_number, int)
        or isinstance(line_number, bool)
        or line_number < 1
    ):
        return None
    raw_path = path_data["text"]
    if not raw_path.strip() or any(character in raw_path for character in "\x00\r\n"):
        return None
    native_path = raw_path.replace("\\", "/") if is_windows_host() else raw_path
    relative = Path(native_path)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    normalized_path = relative.as_posix()
    if normalized_path in {"", "."}:
        return None
    line_text = _truncate_search_match_text(lines_data["text"].rstrip("\r\n"))
    return relative, f"{normalized_path}:{line_number}:{line_text}"


def _fallback_search_matches(
    *,
    workspace: Path,
    root: Path,
    query: str,
    glob: str,
    max_results: int,
    should_stop: Callable[[], bool] | None = None,
) -> list[str]:
    """Search text files in Python when ripgrep is unavailable to the service."""
    matches: list[str] = []
    inspected_files = 0
    deadline = time.monotonic() + SEARCH_TIMEOUT_SECONDS
    try:
        root_is_file = root.is_file()
        candidates = (root,) if root_is_file else root.rglob("*")
        for path in candidates:
            if callable(should_stop) and should_stop():
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Search exceeded the {SEARCH_TIMEOUT_SECONDS}-second controller limit."
                )
            if inspected_files >= 12_000 or len(matches) >= max_results:
                break
            try:
                relative_to_workspace = path.relative_to(workspace)
                if (
                    _path_has_ignored_part(relative_to_workspace)
                    or _path_has_controller_internal_file(relative_to_workspace)
                    or _path_has_sensitive_part(relative_to_workspace)
                    or not _is_confined_search_match(
                        workspace,
                        root,
                        relative_to_workspace,
                    )
                    or path.stat().st_size > SEARCH_MAX_FILE_BYTES
                ):
                    continue
            except (OSError, ValueError):
                continue

            if not _path_matches_search_glob(path, root, glob, workspace=workspace):
                continue

            inspected_files += 1
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line_number, line in enumerate(lines, start=1):
                if callable(should_stop) and should_stop():
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"Search exceeded the {SEARCH_TIMEOUT_SECONDS}-second controller limit."
                    )
                if query not in line:
                    continue
                relative = path.relative_to(workspace).as_posix()
                matches.append(
                    f"{relative}:{line_number}:{_truncate_search_match_text(line)}"
                )
                if len(matches) >= max_results:
                    break
    except OSError:
        return matches
    return matches
