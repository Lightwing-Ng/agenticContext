"""Read-only, bounded Git inspection for the Secure MCP Tunnel.

Code version: v1.0.0-claude.0

Every call runs one trusted ``git`` executable directly (never a shell) inside one
registered project root. Discovery cannot climb above that root, repository-level
hooks such as fsmonitor, external diff drivers, and textconv filters are disabled,
and output is read with a hard byte cap. Large diffs are served as pages of hunks
with a signed continuation that is bound to the exact project, request, and diff
bytes, so later evidence is recoverable instead of truncated, and a changed diff is
reported instead of silently read.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
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
MAX_LINE_CHARACTERS = 4_000
DEFAULT_MAX_HUNKS = 20
DEFAULT_MAX_HUNK_LINES = 200
GIT_TIMEOUT_SECONDS = 30
CONTINUATION_VERSION = 1
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/~^@{}-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")

# Repository configuration can name programs; none of them may run during inspection.
_SAFE_GIT_OPTIONS = (
    "--no-pager",
    "-c", "core.fsmonitor=false",
    "-c", "core.untrackedCache=false",
    "-c", "core.quotePath=false",
    "-c", "color.ui=never",
    "-c", "log.showSignature=false",
    "-c", "diff.noprefix=false",
    "-c", "diff.mnemonicPrefix=false",
    "-c", "diff.relative=false",
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
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
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


def _git_text(root: Path, *args: str, max_bytes: int = MAX_GIT_OUTPUT_BYTES) -> str | None:
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
    return (text, False) if len(text) <= limit else (text[:limit] + "\n…", True)


def status_summary(root: Path) -> str:
    """Return a short bounded branch/status summary, or an explanation."""
    if not is_git_root(root):
        return "This project is not a Git repository."
    status = _git_text(root, "status", "--short", "--branch")
    if status is None:
        return "Git status is unavailable."
    return _bounded(status.strip(), MAX_STATUS_CHARACTERS)[0]


def change_summary(root: Path, pathspec: list[str]) -> dict[str, Any]:
    """Return branch status and diff stats, with no patch content."""
    _require_git_root(root)
    status = _git_text(root, "status", "--porcelain=v1", "--branch", "--untracked-files=all")
    if status is None:
        raise GitInspectionError("Git status is unavailable.")
    spec = ["--", *pathspec] if pathspec else []
    unstaged = _git_text(root, "diff", "--no-ext-diff", "--no-textconv", "--stat", *spec) or ""
    staged = _git_text(
        root, "diff", "--no-ext-diff", "--no-textconv", "--cached", "--stat", *spec
    ) or ""
    return {
        "status": _bounded(status, MAX_STATUS_CHARACTERS)[0],
        "unstaged_stat": _bounded(unstaged, MAX_STAT_CHARACTERS)[0],
        "staged_stat": _bounded(staged, MAX_STAT_CHARACTERS)[0],
    }


def resolve_commit(root: Path, ref: str, label: str) -> str:
    """Resolve one model-supplied revision to a full commit SHA."""
    if not _REF_RE.fullmatch(ref) or ".." in ref:
        raise GitInspectionError(f"{label} must be one commit or ref name, not a range or option.")
    resolved = _git_text(root, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}")
    sha = (resolved or "").strip()
    if not _SHA_RE.fullmatch(sha):
        raise GitInspectionError(f"{label} {ref} is not a commit in this project.")
    return sha


def git_log(
    root: Path,
    *,
    limit: int,
    skip: int,
    ref: str,
    pathspec: list[str],
) -> dict[str, Any]:
    """Return a bounded page of commit summaries."""
    _require_git_root(root)
    head = resolve_commit(root, ref, "ref") if ref else ""
    if not head and _git_text(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}") is None:
        return {"commits": [], "has_more": False}
    output = _git_text(
        root,
        "log",
        "--no-decorate",
        "--format=%H%x1f%aI%x1f%an%x1f%s%x1e",
        f"--max-count={limit + 1}",
        f"--skip={skip}",
        head or "HEAD",
        "--",
        *pathspec,
    )
    if output is None:
        raise GitInspectionError("Git log is unavailable.")
    commits = []
    for record in output.split("\x1e"):
        fields = record.strip("\n").split("\x1f")
        if len(fields) != 4:
            continue
        commits.append(
            {"commit": fields[0], "date": fields[1], "author": fields[2][:120], "subject": fields[3][:300]}
        )
    return {"commits": commits[:limit], "has_more": len(commits) > limit}


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


_C_ESCAPES = {"a": "\a", "b": "\b", "t": "\t", "n": "\n", "v": "\v", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}


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
    return name[len(prefix):] if name.startswith(prefix) else name


def _header_path(rest: str) -> str:
    """Return the path from ``diff --git a/P b/P`` when both sides are equal."""
    if rest.startswith('"'):
        closing = rest.find('" ', 1)
        return _strip_prefix(rest[: closing + 1], "a/") if closing > 0 else rest
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
        file_units = units or [
            DiffUnit(path, old_path, status, binary, "", [])
        ]
        for unit in file_units:
            unit.path, unit.old_path, unit.status, unit.binary = path, old_path, status, binary
        file_units[0].first_of_file = True
        files.append(list(file_units))

    for line in text.split("\n"):
        if line.startswith("diff --git "):
            finish()
            state = {"header": _header_path(line[len("diff --git "):])}
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
            elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
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

    def as_dict(self) -> dict[str, Any]:
        return {"staged": self.staged, "base": self.base, "head": self.head, "paths": list(self.paths)}


class ContinuationCodec:
    """Sign continuations so they cannot be forged or moved to another project."""

    def __init__(self, key: bytes) -> None:
        self._key = key

    def encode(self, payload: dict[str, Any]) -> str:
        body = base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).decode("ascii").rstrip("=")
        return f"{body}.{self._signature(body)}"

    def decode(self, token: str) -> dict[str, Any]:
        body, _, signature = token.partition(".")
        if not body or not hmac.compare_digest(signature, self._signature(body)):
            raise GitInspectionError(
                "The continuation is invalid or expired (for example, after a service restart). "
                "Request the diff again without continuation."
            )
        try:
            padded = body + "=" * (-len(body) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        except (ValueError, UnicodeError) as exc:
            raise GitInspectionError("The continuation is malformed.") from exc
        if not isinstance(payload, dict) or payload.get("v") != CONTINUATION_VERSION:
            raise GitInspectionError("The continuation is malformed.")
        return payload

    def _signature(self, body: str) -> str:
        return hmac.new(self._key, body.encode("ascii"), hashlib.sha256).hexdigest()[:32]


def _root_key(root: Path) -> str:
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]


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
        raise GitInspectionError("Git diff exceeded the time limit; narrow it with paths.")
    if result.overflow:
        raise GitInspectionError(
            f"The diff exceeds {MAX_DIFF_BYTES // (1024 * 1024)} MiB; narrow it with paths."
        )
    if result.returncode != 0:
        raise GitInspectionError("Git diff failed for this request.")
    return result.stdout, parse_unified_diff(result.stdout.decode("utf-8", errors="replace"))


def diff_page(
    *,
    project_id: str,
    root: Path,
    request: DiffRequest,
    withheld: Any,
    codec: ContinuationCodec,
    continuation: dict[str, Any] | None,
    max_hunks: int,
    max_hunk_lines: int,
) -> dict[str, Any]:
    """Return one deterministic page of diff hunks and a continuation when more remain."""
    raw, files = read_diff(root, request)
    diff_id = hashlib.sha256(raw).hexdigest()
    visible: list[DiffUnit] = []
    withheld_files = 0
    for file_units in files:
        if withheld(file_units[0].path) or (file_units[0].old_path and withheld(file_units[0].old_path)):
            withheld_files += 1
            continue
        visible.extend(file_units)
    start_unit, start_line = 0, 0
    if continuation is not None:
        if continuation.get("d") != diff_id:
            raise GitInspectionError(
                "The diff changed after this continuation was issued, so the next page "
                "would describe different changes. Request the diff again without continuation."
            )
        start_unit, start_line = int(continuation.get("u", 0)), int(continuation.get("l", 0))
        if not 0 <= start_unit <= len(visible):
            raise GitInspectionError("The continuation is malformed.")
    items: list[dict[str, Any]] = []
    characters = 0
    unit_index, line_offset = start_unit, start_line
    long_lines = 0
    while unit_index < len(visible) and len(items) < max_hunks:
        unit = visible[unit_index]
        item: dict[str, Any] = {"path": unit.path}
        if unit.first_of_file and line_offset == 0:
            if unit.status != "modified":
                item["status"] = unit.status
            if unit.old_path and unit.old_path != unit.path:
                item["old_path"] = unit.old_path
        if unit.binary:
            item["binary"] = True
        if not unit.header:
            characters += len(json.dumps(item))
            items.append(item)
            unit_index, line_offset = unit_index + 1, 0
            continue
        item["header"] = unit.header[:MAX_LINE_CHARACTERS]
        segment: list[str] = []
        cursor = line_offset
        segment_characters = 0
        while cursor < len(unit.lines) and len(segment) < max_hunk_lines:
            line = unit.lines[cursor]
            if len(line) > MAX_LINE_CHARACTERS:
                line = line[:MAX_LINE_CHARACTERS] + " …[line shortened]"
                long_lines += 1
            if segment and characters + segment_characters + len(line) > MAX_PAGE_CHARACTERS:
                break
            segment.append(line)
            segment_characters += len(line) + 1
            cursor += 1
        if items and characters + segment_characters > MAX_PAGE_CHARACTERS:
            break
        item["lines"] = "\n".join(segment)
        if line_offset or cursor < len(unit.lines):
            item["segment"] = {
                "first_line": line_offset + 1,
                "last_line": cursor,
                "total_lines": len(unit.lines),
            }
        characters += segment_characters + len(item["header"]) + len(unit.path)
        items.append(item)
        if cursor < len(unit.lines):
            line_offset = cursor
            break
        unit_index, line_offset = unit_index + 1, 0
    complete = unit_index >= len(visible)
    result: dict[str, Any] = {
        "ok": True,
        "project": project_id,
        "diff_id": diff_id[:16],
        "files": len({unit.path for unit in visible}),
        "hunks": len(visible),
        "range": {"first_hunk": min(start_unit + 1, len(visible)), "last_hunk": unit_index if line_offset == 0 else unit_index + 1},
        "items": items,
        "complete": complete,
    }
    if withheld_files:
        result["withheld_files"] = withheld_files
    if long_lines:
        result["shortened_lines"] = long_lines
    if not complete:
        result["continuation"] = codec.encode(
            {
                "v": CONTINUATION_VERSION,
                "p": project_id,
                "r": _root_key(root),
                "d": diff_id,
                "u": unit_index,
                "l": line_offset,
                **request.as_dict(),
            }
        )
    return result


def request_from_continuation(
    payload: dict[str, Any],
    *,
    project_id: str,
    root: Path,
) -> DiffRequest:
    """Return the request a verified continuation was issued for, in this project only."""
    if payload.get("p") != project_id or payload.get("r") != _root_key(root):
        raise GitInspectionError(
            "This continuation belongs to a different project. Request the diff again "
            "without continuation."
        )
    paths = payload.get("paths")
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        raise GitInspectionError("The continuation is malformed.")
    return DiffRequest(
        bool(payload.get("staged")),
        str(payload.get("base") or ""),
        str(payload.get("head") or ""),
        tuple(paths),
    )
