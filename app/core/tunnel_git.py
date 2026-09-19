"""Read-only, bounded Git inspection for the Secure MCP Tunnel.

Code version: v1.2.1-codex.0

Every call runs one trusted ``git`` executable directly (never a shell) inside one
registered project root. Discovery cannot climb above that root, repository-level
hooks such as fsmonitor, external diff drivers, and textconv filters are disabled,
and output is read with a hard byte cap. ``show_changes`` can return a
60,000-character unified patch with an explicit truncation marker while protected
file segments remain withheld.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_GIT_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_DIFF_BYTES = 16 * 1024 * 1024
MAX_STATUS_CHARACTERS = 6_000
MAX_STAT_CHARACTERS = 8_000
MAX_PAGE_CHARACTERS = 60_000
GIT_TIMEOUT_SECONDS = 30

# Repository configuration can name programs; none of them may run during inspection.
_SAFE_GIT_OPTIONS = (
    "--no-pager",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.untrackedCache=false",
    "-c",
    "core.quotePath=false",
    "-c",
    "color.ui=never",
    "-c",
    "log.showSignature=false",
    "-c",
    "diff.noprefix=false",
    "-c",
    "diff.mnemonicPrefix=false",
    "-c",
    "diff.relative=false",
)


class GitInspectionError(ValueError):
    """Raised with an actionable, content-free explanation."""


@dataclass(slots=True)
class GitResult:
    returncode: int
    stdout: bytes
    overflow: bool = False
    timed_out: bool = False


def _git_environment(root: Path) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
            # Never discover a repository above the project root (for example a
            # Desktop-level repository around a non-Git project).
            "GIT_CEILING_DIRECTORIES": str(root.parent),
        }
    )
    return environment


def run_git(
    root: Path,
    *args: str,
    max_bytes: int = MAX_GIT_OUTPUT_BYTES,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> GitResult:
    """Run one read-only Git query with bounded output and time."""
    from app.core.computer_use_agent import _trusted_system_executable

    git = _trusted_system_executable("git", forbidden_root=root)
    if git is None:
        raise GitInspectionError("Git is unavailable on this computer.")
    try:
        process = subprocess.Popen(
            [str(git), *_SAFE_GIT_OPTIONS, *args],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_git_environment(root),
        )
    except OSError as exc:
        raise GitInspectionError("Git could not be started.") from exc
    timed_out = threading.Event()

    def expire() -> None:
        timed_out.set()
        process.kill()

    timer = threading.Timer(timeout, expire)
    timer.start()
    try:
        assert process.stdout is not None
        data = process.stdout.read(max_bytes + 1)
        overflow = len(data) > max_bytes
        if overflow:
            process.kill()
        process.stdout.close()
        returncode = process.wait()
    finally:
        timer.cancel()
    return GitResult(returncode, data[:max_bytes], overflow, timed_out.is_set())


def _git_text(
    root: Path, *args: str, max_bytes: int = MAX_GIT_OUTPUT_BYTES
) -> str | None:
    result = run_git(root, *args, max_bytes=max_bytes)
    if result.returncode != 0 and not result.overflow:
        return None
    return result.stdout.decode("utf-8", errors="replace")


def is_git_root(root: Path) -> bool:
    """Return whether the project root is itself a Git work-tree root."""
    if not (root / ".git").exists():
        return False
    top = _git_text(root, "rev-parse", "--show-toplevel")
    if not top:
        return False
    try:
        return Path(top.strip()).resolve() == root
    except OSError:
        return False


def _require_git_root(root: Path) -> None:
    if not is_git_root(root):
        raise GitInspectionError("This project is not a Git repository.")


def _bounded(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    marker = "\n…"
    return text[: max(0, limit - len(marker))] + marker, True


def _display_path(path: str) -> str:
    """Render one raw Git path without creating status-format ambiguity."""
    if (
        " -> " in path
        or path != path.strip(" ")
        or any(character.isspace() and character != " " for character in path)
        or any(character in {'"', "\\"} for character in path)
        or not path.isprintable()
    ):
        return json.dumps(path, ensure_ascii=True)
    return path


def _format_status(raw: bytes, withheld: Any | None) -> str | None:
    """Parse porcelain-v1 ``-z`` records and omit protected paths structurally."""
    if raw and not raw.endswith(b"\0"):
        return None
    records = raw.split(b"\0")
    visible: list[str] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if record.startswith(b"## "):
            visible.append(record.decode("utf-8", errors="replace"))
            continue
        if len(record) < 4 or record[2:3] != b" ":
            return None
        try:
            status = record[:2].decode("ascii")
        except UnicodeDecodeError:
            return None
        destination = os.fsdecode(record[3:])
        paths = [destination]
        renamed_or_copied = "R" in status or "C" in status
        if renamed_or_copied:
            if index >= len(records) or not records[index]:
                return None
            source = os.fsdecode(records[index])
            index += 1
            paths.append(source)
        if withheld is not None and any(withheld(path) for path in paths):
            continue
        if renamed_or_copied:
            visible.append(
                f"{status} {_display_path(paths[1])} -> {_display_path(paths[0])}"
            )
        else:
            visible.append(f"{status} {_display_path(destination)}")
    return "\n".join(visible)


def _status_text(
    root: Path,
    pathspec: list[str],
    *,
    withheld: Any | None,
) -> str | None:
    """Return safely parsed status text from Git's unambiguous NUL format."""
    args = ["status", "--porcelain=v1", "--branch", "--untracked-files=all", "-z"]
    if pathspec:
        args.extend(["--", *pathspec])
    result = run_git(root, *args)
    if result.returncode != 0 or result.overflow or result.timed_out:
        return None
    return _format_status(result.stdout, withheld)


def status_summary(root: Path, *, withheld: Any | None = None) -> str:
    """Return a short bounded branch/status summary, or an explanation."""
    if not is_git_root(root):
        return "This project is not a Git repository."
    status = _status_text(root, [], withheld=withheld)
    if status is None:
        return "Git status is unavailable."
    return _bounded(status.strip(), MAX_STATUS_CHARACTERS)[0]


def _diff_path_groups(
    root: Path,
    request: "DiffRequest",
) -> list[tuple[str, ...]]:
    """Return exact per-change paths from Git's unambiguous NUL format."""
    args = ["diff", "--no-ext-diff", "--no-textconv", "-M", "--name-status", "-z"]
    if request.staged:
        args.append("--cached")
    if request.base:
        args.append(request.base)
    if request.head:
        args.append(request.head)
    args.append("--")
    args.extend(f":(literal){path}" for path in request.paths)
    result = run_git(root, *args)
    if result.returncode != 0 or result.overflow or result.timed_out:
        raise GitInspectionError("Git changed-file discovery is unavailable.")
    if result.stdout and not result.stdout.endswith(b"\0"):
        raise GitInspectionError("Git changed-file discovery returned incomplete data.")
    fields = result.stdout.split(b"\0")
    groups: list[tuple[str, ...]] = []
    index = 0
    while index < len(fields):
        raw_status = fields[index]
        index += 1
        if not raw_status:
            continue
        try:
            status = raw_status.decode("ascii")
        except UnicodeDecodeError as exc:
            raise GitInspectionError(
                "Git changed-file discovery returned invalid data."
            ) from exc
        path_count = 2 if status[:1] in {"R", "C"} else 1
        if index + path_count > len(fields):
            raise GitInspectionError(
                "Git changed-file discovery returned incomplete data."
            )
        raw_paths = fields[index : index + path_count]
        index += path_count
        if any(not path for path in raw_paths):
            raise GitInspectionError(
                "Git changed-file discovery returned incomplete data."
            )
        groups.append(tuple(os.fsdecode(path) for path in raw_paths))
    return groups


def _visible_diff_paths(
    root: Path,
    pathspec: list[str],
    *,
    staged: bool,
    withheld: Any,
) -> list[str]:
    """Return changed paths after filtering protected names structurally."""
    args = [
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--name-only",
        "-z",
    ]
    if staged:
        args.append("--cached")
    args.extend(["--", *pathspec])
    result = run_git(root, *args)
    if result.returncode != 0 or result.overflow or result.timed_out:
        raise GitInspectionError("Git changed-file discovery is unavailable.")
    if result.stdout and not result.stdout.endswith(b"\0"):
        raise GitInspectionError("Git changed-file discovery returned incomplete data.")
    return [
        os.fsdecode(name)
        for name in result.stdout.split(b"\0")
        if name and not withheld(os.fsdecode(name))
    ]


def _visible_diff_stat(
    root: Path,
    pathspec: list[str],
    *,
    staged: bool,
    withheld: Any | None,
) -> str:
    """Return a stat limited to visible exact paths."""
    if withheld is None:
        visible_spec = pathspec
    else:
        paths = _visible_diff_paths(
            root,
            pathspec,
            staged=staged,
            withheld=withheld,
        )
        if not paths:
            return ""
        visible_spec = [f":(literal){path}" for path in paths]
    args = ["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--stat"]
    if staged:
        args.append("--cached")
    args.extend(["--", *visible_spec])
    return _git_text(root, *args) or ""


def change_summary(
    root: Path,
    pathspec: list[str],
    *,
    withheld: Any | None = None,
) -> dict[str, Any]:
    """Return path-scoped branch status and diff stats, with no patch content."""
    _require_git_root(root)
    status = _status_text(root, pathspec, withheld=withheld)
    if status is None:
        raise GitInspectionError("Git status is unavailable.")
    unstaged = _visible_diff_stat(
        root,
        pathspec,
        staged=False,
        withheld=withheld,
    )
    staged = _visible_diff_stat(
        root,
        pathspec,
        staged=True,
        withheld=withheld,
    )
    return {
        "status": _bounded(status, MAX_STATUS_CHARACTERS)[0],
        "unstaged_stat": _bounded(unstaged, MAX_STAT_CHARACTERS)[0],
        "staged_stat": _bounded(staged, MAX_STAT_CHARACTERS)[0],
    }


def bounded_patch(
    root: Path,
    request: "DiffRequest",
    *,
    withheld: Any,
) -> dict[str, Any]:
    """Return one bounded patch selected only from exact, visible Git paths."""
    path_groups = _diff_path_groups(root, request)
    visible_groups = [
        group for group in path_groups if not any(withheld(path) for path in group)
    ]
    withheld_files = len(path_groups) - len(visible_groups)
    visible_paths = list(
        dict.fromkeys(path for group in visible_groups for path in group)
    )
    if visible_paths:
        visible_request = DiffRequest(
            request.staged,
            request.base,
            request.head,
            tuple(visible_paths),
        )
        raw, _files = read_diff(root, visible_request)
        visible = raw.decode("utf-8", errors="replace")
    else:
        visible = ""
    patch, truncated = _bounded(visible, MAX_PAGE_CHARACTERS)
    result: dict[str, Any] = {
        "patch": patch,
        "patch_truncated": truncated,
    }
    if withheld_files:
        result["withheld_files"] = withheld_files
    return result


# Diff parsing ------------------------------------------------------------


@dataclass(slots=True)
class DiffUnit:
    """One hunk, or one file-level change with no text hunk (binary, mode, rename)."""

    path: str
    old_path: str
    status: str
    binary: bool
    header: str
    lines: list[str] = field(default_factory=list)
    first_of_file: bool = False


_C_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "t": "\t",
    "n": "\n",
    "v": "\v",
    "f": "\f",
    "r": "\r",
    '"': '"',
    "\\": "\\",
}


def _unquote(name: str) -> str:
    """Decode one Git C-style quoted path."""
    if not (len(name) >= 2 and name[0] == name[-1] == '"'):
        return name
    body = name[1:-1]
    output = bytearray()
    index = 0
    while index < len(body):
        char = body[index]
        if char == "\\" and index + 1 < len(body):
            following = body[index + 1]
            if following in _C_ESCAPES:
                output.extend(_C_ESCAPES[following].encode())
                index += 2
                continue
            octal = body[index + 1 : index + 4]
            if re.fullmatch(r"[0-7]{3}", octal):
                output.append(int(octal, 8))
                index += 4
                continue
        output.extend(char.encode("utf-8"))
        index += 1
    return output.decode("utf-8", errors="replace")


def _strip_prefix(name: str, prefix: str) -> str:
    name = _unquote(name.rstrip("\t"))
    return name[len(prefix) :] if name.startswith(prefix) else name


def _header_path(rest: str) -> str:
    """Return the path from ``diff --git a/P b/P`` when both sides are equal."""
    if rest.startswith('"'):
        index = 1
        while index < len(rest):
            if rest[index] == "\\":
                index += 2
                continue
            if rest[index] == '"':
                return _strip_prefix(rest[: index + 1], "a/")
            index += 1
        return rest
    if len(rest) >= 5 and (len(rest) - 5) % 2 == 0:
        half = (len(rest) - 5) // 2
        return rest[2 : 2 + half]
    return rest


def parse_unified_diff(text: str) -> list[list[DiffUnit]]:
    """Split ``git diff`` output into files, each a list of units."""
    files: list[list[DiffUnit]] = []
    state: dict[str, Any] = {}
    units: list[DiffUnit] = []
    current: DiffUnit | None = None

    def finish() -> None:
        if not state:
            return
        path = state.get("new") or state.get("old") or state.get("header", "")
        old_path = state.get("old", "")
        status = state.get("status", "modified")
        if status == "renamed" and old_path == path:
            status = "modified"
        binary = bool(state.get("binary"))
        file_units = units or [DiffUnit(path, old_path, status, binary, "", [])]
        for unit in file_units:
            unit.path, unit.old_path, unit.status, unit.binary = (
                path,
                old_path,
                status,
                binary,
            )
        file_units[0].first_of_file = True
        files.append(list(file_units))

    for line in text.split("\n"):
        if line.startswith("diff --git "):
            finish()
            state = {"header": _header_path(line[len("diff --git ") :])}
            units = []
            current = None
            continue
        if line.startswith(("diff --cc ", "diff --combined ")):
            # An unmerged path during a conflict: one combined diff of its stages.
            finish()
            state = {"header": _unquote(line.split(" ", 2)[2]), "status": "unmerged"}
            units = []
            current = None
            continue
        if not state:
            continue
        if current is not None and line[:1] in {" ", "+", "-", "\\"}:
            # Inside a hunk every content line starts with one of these markers.
            current.lines.append(line)
            continue
        if line.startswith("@@"):
            current = DiffUnit("", "", "", False, line, [])
            units.append(current)
        elif current is None:
            if line.startswith("new file mode"):
                state["status"] = "added"
            elif line.startswith("deleted file mode"):
                state["status"] = "deleted"
            elif line.startswith("old mode") and "status" not in state:
                state["status"] = "mode_changed"
            elif line.startswith("rename from "):
                state["status"], state["old"] = "renamed", _unquote(line[12:])
            elif line.startswith("rename to "):
                state["new"] = _unquote(line[10:])
            elif line.startswith("copy from "):
                state["status"], state["old"] = "copied", _unquote(line[10:])
            elif line.startswith("copy to "):
                state["new"] = _unquote(line[8:])
            elif line.startswith("Binary files ") or line.startswith(
                "GIT binary patch"
            ):
                state["binary"] = True
            elif line.startswith("--- ") and state.get("status") != "unmerged":
                name = line[4:].rstrip("\t")
                if name != "/dev/null":
                    state.setdefault("old", _strip_prefix(name, "a/"))
            elif line.startswith("+++ ") and state.get("status") != "unmerged":
                name = line[4:].rstrip("\t")
                if name != "/dev/null":
                    state["new"] = _strip_prefix(name, "b/")
    finish()
    return files


# Pagination -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DiffRequest:
    staged: bool
    base: str
    head: str
    paths: tuple[str, ...]


def read_diff(root: Path, request: DiffRequest) -> tuple[bytes, list[list[DiffUnit]]]:
    """Return the exact diff bytes for one request and its parsed files."""
    _require_git_root(root)
    args = ["diff", "--no-color", "--no-ext-diff", "--no-textconv", "-M", "--unified=3"]
    if request.staged:
        args.append("--cached")
    if request.base:
        args.append(request.base)
    if request.head:
        args.append(request.head)
    args.append("--")
    args.extend(f":(literal){path}" for path in request.paths)
    result = run_git(root, *args, max_bytes=MAX_DIFF_BYTES, timeout=60)
    if result.timed_out:
        raise GitInspectionError(
            "Git diff exceeded the time limit; narrow it with paths."
        )
    if result.overflow:
        raise GitInspectionError(
            f"The diff exceeds {MAX_DIFF_BYTES // (1024 * 1024)} MiB; narrow it with paths."
        )
    if result.returncode != 0:
        raise GitInspectionError("Git diff failed for this request.")
    return result.stdout, parse_unified_diff(
        result.stdout.decode("utf-8", errors="replace")
    )
