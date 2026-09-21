"""Path admission rules for one selected workspace root.

Every caller that reaches a project's files - the Browser Agent controller and the
Tunnel MCP server alike - shares these rules. They are the only place that decides
whether a path is inside the selected root, whether it crosses a link-like
component, and whether it names credential or controller-internal material.
"""

# Code version: v1.0.0-claude.0

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat as stat_module
from typing import Any, Iterator


_IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".computer-use-agent",
        ".git",
        ".idea",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "htmlcov",
        "local_store",
        "logs",
        "node_modules",
        "playwright-report",
        "test-results",
        "vendor",
        "venv",
    }
)


_IGNORED_WORKSPACE_FILE_NAMES = frozenset({".coverage", "coverage.json"})


_IGNORED_WORKSPACE_FILE_PREFIXES = (".coverage.", ".coverage ")


_IGNORED_WORKSPACE_METADATA_FILE_NAMES = frozenset({".ds_store"})


_CONTROLLER_INTERNAL_FILE_PATTERN = re.compile(
    r"\..+\.agent-(?:(?:backup|cleanup|delete)-)?[0-9a-f]{16}\.tmp",
    re.IGNORECASE | re.DOTALL,
)


_SENSITIVE_PATH_NAMES = frozenset(
    {
        ".aws",
        ".env",
        ".git-credentials",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".ssh",
        "cookies",
        "cookies.json",
        "credential",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ed25519",
        "id_ecdsa",
        "id_rsa",
        "secret",
        "secrets",
        "secrets.json",
    }
)


_SENSITIVE_PATH_SUFFIXES = (".key", ".p12", ".pem", ".pfx")


def _path_has_ignored_part(relative: Path) -> bool:
    return any(part.casefold() in _IGNORED_DIRECTORY_NAMES for part in relative.parts)


def _path_is_ignored_fingerprint_artifact(relative: Path) -> bool:
    """Exclude inert platform metadata and root verification outputs."""
    filename = relative.name.casefold()
    if filename in _IGNORED_WORKSPACE_METADATA_FILE_NAMES:
        return True
    if len(relative.parts) != 1:
        return False
    return filename in _IGNORED_WORKSPACE_FILE_NAMES or filename.startswith(
        _IGNORED_WORKSPACE_FILE_PREFIXES
    )


def _path_has_controller_internal_file(relative: Path) -> bool:
    """Reserve unpredictable controller temporary and recovery filenames."""
    return any(
        _CONTROLLER_INTERNAL_FILE_PATTERN.fullmatch(part) is not None
        for part in relative.parts
    )


def _path_has_sensitive_part(relative: Path) -> bool:
    """Keep credentials and private keys outside Web-visible controller context."""
    for part in relative.parts:
        normalized = part.casefold()
        if (
            normalized in _SENSITIVE_PATH_NAMES
            or normalized.startswith(".env.")
            or normalized.endswith(_SENSITIVE_PATH_SUFFIXES)
        ):
            return True
    return False


def _path_is_link_like(path: Path) -> bool:
    """Reject symbolic, junction, hard-linked, and special trust-boundary paths."""
    try:
        is_junction = getattr(path, "is_junction", None)
        if path.is_symlink() or bool(callable(is_junction) and is_junction()):
            return True
        path_stat = path.stat()
        if stat_module.S_ISDIR(path_stat.st_mode):
            return False
        return not stat_module.S_ISREG(path_stat.st_mode) or path_stat.st_nlink != 1
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _is_safe_context_file(workspace: Path, path: Path) -> bool:
    """Return whether one context source is a regular in-workspace non-secret file."""
    try:
        relative = path.relative_to(workspace)
        if (
            _path_is_link_like(path)
            or not path.is_file()
            or _path_has_ignored_part(relative)
            or _path_has_controller_internal_file(relative)
            or _path_has_sensitive_part(relative)
        ):
            return False
        path.resolve(strict=True).relative_to(workspace.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


def _is_safe_context_directory(workspace: Path, path: Path) -> bool:
    """Return whether traversal may enter one real directory inside the workspace."""
    try:
        relative = path.relative_to(workspace)
        if (
            _path_is_link_like(path)
            or not path.is_dir()
            or _path_has_ignored_part(relative)
            or _path_has_controller_internal_file(relative)
            or _path_has_sensitive_part(relative)
        ):
            return False
        path.resolve(strict=True).relative_to(workspace.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


def _collect_instruction_files(workspace: Path) -> list[Path]:
    candidates: list[Path] = []
    for name in ("AGENTS.md", "CLAUDE.md", "CODEX.md"):
        root_file = workspace / name
        if _is_safe_context_file(workspace, root_file):
            candidates.append(root_file)
    nested: list[Path] = []
    pending = deque([workspace])
    inspected_directories = 0
    while pending and inspected_directories < 12_000 and len(nested) < 256:
        directory = pending.popleft()
        inspected_directories += 1
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
        except OSError:
            continue
        child_directories: list[Path] = []
        for path in entries:
            if path.name == "AGENTS.md" and _is_safe_context_file(workspace, path):
                nested.append(path)
                if len(nested) >= 256:
                    break
            elif _is_safe_context_directory(workspace, path):
                child_directories.append(path)
        pending.extend(child_directories)
    for path in nested:
        if path not in candidates:
            candidates.append(path)
    return candidates[:24]


def _workspace_audit_metadata(
    workspace: Path,
    *,
    metadata: os.stat_result | None = None,
) -> dict[str, Any]:
    """Return the canonical workspace identity retained in owner-only audit records."""
    canonical_workspace = Path(workspace).resolve()
    workspace_metadata = metadata or canonical_workspace.stat()
    if not stat_module.S_ISDIR(workspace_metadata.st_mode):
        raise ValueError("The selected Agent workspace must be a directory.")
    return {
        "workspace_identity": {
            "device": int(workspace_metadata.st_dev),
            "inode": int(workspace_metadata.st_ino),
        },
    }


def _open_windows_directory_identity(
    directory: Path,
    *,
    deny_delete: bool,
) -> tuple[Any, tuple[int, int]]:
    """Open one Windows directory without following a reparse point."""
    if os.name != "nt":
        raise RuntimeError("Windows directory handles are unavailable on this host.")
    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("FileAttributes", wintypes.DWORD),
            ("CreationTime", wintypes.FILETIME),
            ("LastAccessTime", wintypes.FILETIME),
            ("LastWriteTime", wintypes.FILETIME),
            ("VolumeSerialNumber", wintypes.DWORD),
            ("FileSizeHigh", wintypes.DWORD),
            ("FileSizeLow", wintypes.DWORD),
            ("NumberOfLinks", wintypes.DWORD),
            ("FileIndexHigh", wintypes.DWORD),
            ("FileIndexLow", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ByHandleFileInformation),
    ]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    share_mode = 0x00000001 | 0x00000002
    if not deny_delete:
        share_mode |= 0x00000004
    handle = kernel32.CreateFileW(
        str(directory),
        0x0080,
        share_mode,
        None,
        3,
        0x00200000 | 0x02000000,
        None,
    )
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        information = ByHandleFileInformation()
        if not kernel32.GetFileInformationByHandle(
            handle,
            ctypes.byref(information),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if information.FileAttributes & 0x00000400:
            raise RuntimeError(
                "The mutation path changed into a Windows reparse point."
            )
        identity = (
            int(information.VolumeSerialNumber),
            (int(information.FileIndexHigh) << 32) | int(information.FileIndexLow),
        )
        return handle, identity
    except BaseException:
        kernel32.CloseHandle(handle)
        raise


def _uses_windows_directory_handles() -> bool:
    """Return whether controller mutations require native Windows handles."""
    return os.name == "nt"


def _close_windows_directory_handle(handle: Any) -> None:
    """Close one handle returned by the Windows directory identity helper."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle(handle)


def _windows_directory_identity(directory: Path) -> tuple[int, int]:
    """Read one stable Windows volume-and-file-index directory identity."""
    handle, identity = _open_windows_directory_identity(
        directory,
        deny_delete=False,
    )
    try:
        return identity
    finally:
        _close_windows_directory_handle(handle)


@contextmanager
def _windows_workspace_mutation_guard(
    workspace: Path,
    parent: Path,
    *,
    expected_workspace_identity: tuple[int, int],
    expected_parent_identity: tuple[int, int],
) -> Iterator[None]:
    """Hold every Windows parent and verify its pre-mutation native identity."""
    if os.name != "nt":
        raise RuntimeError(
            "Safe path-based workspace mutation is available only with Windows directory "
            "handles."
        )
    try:
        relative_parent = parent.relative_to(workspace)
    except ValueError as exc:
        raise RuntimeError(
            "The mutation parent is outside the selected Agent workspace."
        ) from exc
    directories = [workspace]
    current = workspace
    for component in relative_parent.parts:
        current /= component
        directories.append(current)

    handles: list[Any] = []
    identities: list[tuple[Path, tuple[int, int]]] = []
    try:
        for directory in directories:
            handle, identity = _open_windows_directory_identity(
                directory,
                deny_delete=True,
            )
            handles.append(handle)
            if _path_is_link_like(directory) or not directory.is_dir():
                raise RuntimeError(
                    "The mutation path no longer names a regular workspace directory."
                )
            identities.append((directory, identity))
        if identities[0][1] != expected_workspace_identity:
            raise RuntimeError(
                "The selected Agent workspace changed before the Windows mutation guard."
            )
        if identities[-1][1] != expected_parent_identity:
            raise RuntimeError(
                "The mutation parent changed before the Windows mutation guard."
            )
        for directory, identity in identities:
            if _windows_directory_identity(directory) != identity:
                raise RuntimeError(
                    "The mutation path changed while Windows directory handles were acquired."
                )
        yield
        for directory, identity in identities:
            if _windows_directory_identity(directory) != identity:
                raise RuntimeError(
                    "The mutation path changed before its Windows directory guard was released."
                )
    finally:
        for handle in reversed(handles):
            _close_windows_directory_handle(handle)


def _ensure_windows_workspace_parent(
    workspace: Path,
    relative_parent: Path,
    *,
    expected_workspace_identity: tuple[int, int],
) -> tuple[Path, tuple[int, int]]:
    """Create missing Windows parents while each existing ancestor is guarded."""
    if os.name != "nt" or relative_parent.is_absolute() or ".." in relative_parent.parts:
        raise RuntimeError("The Windows mutation parent is invalid.")
    current = workspace
    current_identity = expected_workspace_identity
    for component in relative_parent.parts:
        with _windows_workspace_mutation_guard(
            workspace,
            current,
            expected_workspace_identity=expected_workspace_identity,
            expected_parent_identity=current_identity,
        ):
            candidate = current / component
            try:
                candidate.mkdir(mode=0o755)
            except FileExistsError:
                pass
            if _path_is_link_like(candidate) or not candidate.is_dir():
                raise RuntimeError(
                    "The Windows mutation parent changed into a linked or non-directory path."
                )
            resolved = candidate.resolve(strict=True)
            try:
                resolved.relative_to(workspace)
            except ValueError as exc:
                raise RuntimeError(
                    "The Windows mutation parent left the selected Agent workspace."
                ) from exc
            current = resolved
            current_identity = _windows_directory_identity(current)
    return current, current_identity
