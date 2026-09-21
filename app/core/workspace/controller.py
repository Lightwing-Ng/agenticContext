"""The workspace controller: one action protocol over one selected project root."""

# Code version: v1.0.2-codex.0

from __future__ import annotations

import base64
import binascii
from collections import deque
import hashlib
import os
from pathlib import Path
from queue import Empty, Queue
import re
import secrets
import stat as stat_module
import subprocess
import tempfile
from threading import Event, Thread
import time
from typing import Any, Callable, Protocol, Sequence, runtime_checkable

from ..agent.capability_registry import validate_controller_action_payload
from ..config import (
    DEFAULT_PORT,
    default_settings_path,
    is_windows_host,
    resolve_runtime_root,
    runtime_root_is_overridden,
)
from .action_state import ActionState
from .capabilities import FileSnapshot, TextReplacement
from .command_policy import inspection_command_parts
from .evidence import (
    WORKSPACE_FINGERPRINT_TIMEOUT_SECONDS,
    _filtered_git_status,
    _safe_untracked_paths_from_status,
    _workspace_mutation_fingerprint,
)
from .executables import _trusted_system_executable
from .paths import (
    _close_windows_directory_handle,
    _collect_instruction_files,
    _ensure_windows_workspace_parent,
    _is_safe_context_directory,
    _is_safe_context_file,
    _open_windows_directory_identity,
    _path_has_controller_internal_file,
    _path_has_ignored_part,
    _path_has_sensitive_part,
    _path_is_link_like,
    _uses_windows_directory_handles,
    _windows_directory_identity,
    _windows_workspace_mutation_guard,
    _workspace_audit_metadata,
)
from .process_io import (
    MAX_ACTION_OUTPUT_CHARS,
    SEARCH_STDOUT_QUEUE_SIZE,
    _STREAM_READ_FAILED,
    _SUBPROCESS_POPEN_TYPE,
    _bounded_devnull_process,
    _bounded_verification_process_output,
    _discard_text_stream,
    _process_group_options,
    _queue_text_lines,
    _stop_process,
    _truncate_text,
)
from .search import (
    MAX_SEARCH_QUERY_CHARS,
    SEARCH_MAX_FILE_BYTES,
    SEARCH_MAX_RAW_EVENTS,
    SEARCH_TIMEOUT_SECONDS,
    _fallback_search_matches,
    _is_confined_search_match,
    _parse_rg_search_match,
    _path_matches_search_glob,
    _search_exclusion_globs,
    _search_include_globs,
)

if os.name == "posix":
    import fcntl
else:  # pragma: no cover - Windows uses a fail-closed delete path.
    fcntl = None


@runtime_checkable
class WorkspaceCommandSettings(Protocol):
    """The only task setting a workspace controller reads."""

    @property
    def command_timeout_seconds(self) -> int:
        """Return the wall-clock budget for one approved verification command."""


DEFAULT_AGENT_RUNTIME_ROOT = (
    resolve_runtime_root() / ".computer-use-agent"
    if runtime_root_is_overridden()
    else default_settings_path().parent / "computer-use-agent"
)


MAX_FILE_READ_CHARS = 120_000


MAX_CONTROLLER_DELETE_BYTES = 20 * 1_024 * 1_024


MAX_BASE64_DECODED_BYTES = MAX_FILE_READ_CHARS


_ANCHORED_DELETE_SUPPORTED = bool(
    os.name == "posix"
    and fcntl is not None
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.link in getattr(os, "supports_dir_fd", set())
    and os.open in getattr(os, "supports_dir_fd", set())
    and os.rename in getattr(os, "supports_dir_fd", set())
    and os.unlink in getattr(os, "supports_dir_fd", set())
    and os.stat in getattr(os, "supports_dir_fd", set())
    and os.stat in getattr(os, "supports_follow_symlinks", set())
)


_ANCHORED_MUTATION_SUPPORTED = bool(
    _ANCHORED_DELETE_SUPPORTED
    and os.mkdir in getattr(os, "supports_dir_fd", set())
)


def _registered_action_capability(action_name: str):
    """Resolve an Agent Action lazily to avoid coupling the core module to its facade package."""
    from ..agent.capability_registry import capability_for_action

    return capability_for_action(action_name)


def _decode_base64_utf8(
    value: str,
    *,
    field_name: str,
    allow_empty: bool = False,
) -> str:
    """Decode one controller base64 field with strict validation and a size cap."""
    raw = str(value or "")
    if not raw:
        if allow_empty:
            return ""
        raise ValueError(f"The {field_name} field requires non-empty base64.")
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"Invalid base64 encoding in {field_name}.") from exc
    if len(decoded) > MAX_BASE64_DECODED_BYTES:
        raise ValueError(f"Decoded {field_name} exceeds the controller size limit.")
    try:
        return decoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Decoded {field_name} is not valid UTF-8.") from exc


class WorkspaceController:
    """Execute a narrow action protocol inside one selected project."""

    def __init__(
        self,
        workspace: Path,
        settings: WorkspaceCommandSettings,
        should_stop: Callable[[], bool],
        process_changed: Callable[[subprocess.Popen[str] | None], None] | None = None,
        read_only: bool = False,
        compute_job_runtime_root: Path | None = None,
        action_checkpoint: dict[str, Any] | None = None,
        expected_workspace_identity: tuple[int, int] | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        windows_root_handle: Any | None = None
        windows_workspace_identity: tuple[int, int] | None = None
        try:
            if _uses_windows_directory_handles():
                windows_root_handle, windows_workspace_identity = (
                    _open_windows_directory_identity(
                        self.workspace,
                        deny_delete=True,
                    )
                )
            workspace_metadata = self.workspace.stat()
            if not stat_module.S_ISDIR(workspace_metadata.st_mode):
                raise ValueError("The selected Agent workspace must be a directory.")
            self._workspace_identity = (
                int(workspace_metadata.st_dev),
                int(workspace_metadata.st_ino),
            )
            if (
                expected_workspace_identity is not None
                and self._workspace_identity != expected_workspace_identity
            ):
                raise RuntimeError(
                    "The Agent workspace identity changed after admission; the task was stopped "
                    "before opening its browser."
                )
            if (
                windows_workspace_identity is not None
                and _windows_directory_identity(self.workspace)
                != windows_workspace_identity
            ):
                raise RuntimeError(
                    "The Agent workspace identity changed while its Windows root handle was "
                    "acquired; the task was stopped before opening its browser."
                )
            self._windows_workspace_identity = windows_workspace_identity
            self._workspace_audit_metadata = _workspace_audit_metadata(
                self.workspace,
                metadata=workspace_metadata,
            )
        finally:
            if windows_root_handle is not None:
                _close_windows_directory_handle(windows_root_handle)
        self.settings = settings
        self.state = ActionState.from_checkpoint(action_checkpoint)
        self.should_stop = should_stop
        self.process_changed = process_changed or (lambda _process: None)
        self.read_only = read_only
        self._compute_job_runtime_root = compute_job_runtime_root or DEFAULT_AGENT_RUNTIME_ROOT
        self._compute_jobs: Any | None = None

    def _compute_job_manager(self):
        """Resolve the durable compute boundary only when a job action is requested."""
        if self._compute_jobs is None:
            from ..agent.compute_jobs import ComputeJobManager

            self._compute_jobs = ComputeJobManager(
                self.workspace,
                self._compute_job_runtime_root,
            )
        return self._compute_jobs

    def event_chain_start_metadata(self) -> dict[str, Any]:
        """Return immutable workspace evidence for the root event of this run."""
        return dict(self._workspace_audit_metadata)

    def action_event_metadata(
        self,
        payload: Any,
        *,
        include_read_receipt: bool = True,
    ) -> dict[str, Any]:
        """Return bounded receipt provenance for one controller action event."""
        metadata = dict(self._workspace_audit_metadata)
        if not isinstance(payload, dict):
            return metadata
        action_name = str(payload.get("action") or "").strip().lower()
        if action_name not in {"read", "delete"}:
            return metadata
        if action_name == "read" and not include_read_receipt:
            return metadata
        try:
            path = self._resolve_path(
                payload.get("path"),
                allow_missing=action_name == "delete",
            )
            relative_path = path.relative_to(self.workspace).as_posix()
        except (OSError, ValueError):
            return metadata
        receipt = self.state.read_receipts.get(relative_path)
        if receipt is None:
            return metadata
        digest, identity, generation = receipt
        metadata["read_receipt"] = {
            "sha256": digest,
            "generation": int(generation),
            "file_identity": {
                "device": int(identity[0]),
                "inode": int(identity[1]),
                "size": int(identity[2]),
                "mtime_ns": int(identity[3]),
                "mode": int(identity[4]),
            },
        }
        if action_name == "delete":
            metadata["delete_digest"] = digest
        return metadata

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Execute one validated action and return a compact observation."""
        if self.should_stop():
            return {"ok": False, "stopped": True, "error": "Stop requested."}
        if not isinstance(payload, dict):
            return {
                "ok": False,
                "action": "",
                "error": "Agent action payload must be an object.",
            }
        action = str(payload.get("action") or "").strip().lower()
        registered_capability = _registered_action_capability(action)
        if registered_capability is None:
            return {
                "ok": False,
                "action": action,
                "error": f"Unsupported controller action: {action or '[missing]'}",
            }
        try:
            validate_controller_action_payload(registered_capability, payload)
        except ValueError as exc:
            return {"ok": False, "action": action, "error": str(exc)[:2_000]}
        if self.read_only and not registered_capability.read_only_task_allowed:
            return {
                "ok": False,
                "action": action,
                "error": "This Agent task is read-only; only list, read, search, job_status, and bodycheck are allowed.",
            }
        handler = getattr(self, registered_capability.handler_name, None)
        if not callable(handler):
            return {
                "ok": False,
                "action": action,
                "error": f"Registered action has no controller handler: {action}",
            }
        try:
            if self._compute_jobs is not None and action in {
                "replace",
                "replace_base64",
                "write",
                "write_base64",
                "delete",
                "run",
                "browser_acceptance",
            }:
                compute_status = self._compute_job_manager().status()
                if bool(compute_status.get("active")):
                    raise RuntimeError(
                        "A durable compute job is active for this workspace. Wait for or stop "
                        "that job before running controller mutations or verification commands."
                    )
            return handler(payload)
        except (OSError, RuntimeError, ValueError) as exc:
            return {"ok": False, "action": action, "error": str(exc)[:2_000]}

    def _require_workspace_identity(self) -> None:
        """Reject a task after its selected root is rebound to another directory."""
        if _path_is_link_like(self.workspace):
            raise RuntimeError(
                "The Agent workspace changed after admission; start a new task."
            )
        metadata = self.workspace.stat()
        if (
            not stat_module.S_ISDIR(metadata.st_mode)
            or (int(metadata.st_dev), int(metadata.st_ino))
            != self._workspace_identity
        ):
            raise RuntimeError(
                "The Agent workspace changed after admission; start a new task."
            )
        if os.name == "nt":
            expected_windows_identity = self._windows_workspace_identity
            if (
                expected_windows_identity is None
                or _windows_directory_identity(self.workspace)
                != expected_windows_identity
            ):
                raise RuntimeError(
                    "The Agent workspace changed after admission; start a new task."
                )

    def _resolve_path(self, raw_path: Any, *, allow_missing: bool = False) -> Path:
        self._require_workspace_identity()
        candidate = Path(str(raw_path or "."))
        lexical = candidate.expanduser() if candidate.is_absolute() else self.workspace / candidate
        try:
            lexical_relative = lexical.relative_to(self.workspace)
        except ValueError:
            lexical_relative = Path()
        if ".." in lexical_relative.parts:
            raise ValueError("Controller paths must stay inside the selected project.")
        current = self.workspace
        for part in lexical_relative.parts:
            current /= part
            if _path_is_link_like(current):
                raise ValueError(
                    "Controller paths cannot traverse linked files or directories."
                )
            if not current.exists():
                break
        if candidate.is_absolute():
            resolved = candidate.expanduser().resolve(strict=not allow_missing)
        else:
            resolved = (self.workspace / candidate).resolve(strict=not allow_missing)
        try:
            resolved.relative_to(self.workspace)
        except ValueError as exc:
            raise ValueError("Controller paths must stay inside the selected project.") from exc
        relative = resolved.relative_to(self.workspace)
        if any(
            part.casefold() in {".git", ".computer-use-agent"}
            for part in relative.parts
        ):
            raise ValueError("Controller access to internal metadata is not allowed.")
        if _path_has_ignored_part(relative):
            raise ValueError(
                "Controller access to ignored, generated, or runtime directories is not allowed."
            )
        if _path_has_controller_internal_file(relative):
            raise ValueError("Controller access to internal recovery files is not allowed.")
        if _path_has_sensitive_part(relative):
            raise ValueError(
                "Controller access to credentials and private-key files is not allowed."
            )
        self._require_workspace_identity()
        return resolved

    @staticmethod
    def _stable_file_identity(path: Path) -> tuple[int, int, int, int, int]:
        """Return one regular-file identity used to guard a destructive action."""
        return WorkspaceController._stable_file_identity_from_stat(path.lstat())

    @staticmethod
    def _stable_file_identity_from_stat(
        metadata: os.stat_result,
    ) -> tuple[int, int, int, int, int]:
        """Validate and normalize one bounded regular-file identity."""
        if (
            not stat_module.S_ISREG(metadata.st_mode)
            or stat_module.S_ISLNK(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise ValueError("The controller action requires one unlinked regular file.")
        if metadata.st_size > MAX_CONTROLLER_DELETE_BYTES:
            raise ValueError(
                "The controller action refuses files larger than "
                f"{MAX_CONTROLLER_DELETE_BYTES:,} bytes."
            )
        return (
            int(metadata.st_dev),
            int(metadata.st_ino),
            int(metadata.st_size),
            int(metadata.st_mtime_ns),
            int(metadata.st_mode),
        )

    def _open_anchored_parent(
        self,
        relative: Path,
        *,
        create_parents: bool = False,
    ) -> tuple[int, str, bool]:
        """Open a workspace-confined parent directory without following links."""
        if not _ANCHORED_DELETE_SUPPORTED or (
            create_parents and not _ANCHORED_MUTATION_SUPPORTED
        ):
            raise RuntimeError(
                "Anchored workspace mutations are unavailable on this host."
            )
        if relative.is_absolute() or len(relative.parts) < 1 or ".." in relative.parts:
            raise ValueError("The controller action requires one workspace-relative file path.")
        leaf_name = relative.name
        if leaf_name in {"", ".", ".."}:
            raise ValueError("The controller action requires one regular file.")
        directory_flags = (
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        directory_fd = os.open(self.workspace, directory_flags)
        created_parent = False
        try:
            root_metadata = os.fstat(directory_fd)
            if (
                int(root_metadata.st_dev),
                int(root_metadata.st_ino),
            ) != self._workspace_identity:
                raise RuntimeError(
                    "The Agent workspace changed before mutation; start a new task."
                )
            for component in relative.parts[:-1]:
                try:
                    next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
                except FileNotFoundError:
                    if not create_parents:
                        raise
                    os.mkdir(component, mode=0o755, dir_fd=directory_fd)
                    created_parent = True
                    next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            return directory_fd, leaf_name, created_parent
        except BaseException:
            os.close(directory_fd)
            raise

    def _open_anchored_delete_parent(self, relative: Path) -> tuple[int, str]:
        """Open the anchored parent used by the read-receipt delete contract."""
        if not _ANCHORED_DELETE_SUPPORTED:
            raise RuntimeError(
                "Safe delete is unavailable on this host because anchored directory operations "
                "are not supported."
            )
        directory_fd, leaf_name, _created = self._open_anchored_parent(relative)
        return directory_fd, leaf_name

    @staticmethod
    def _write_descriptor(descriptor: int, content: bytes) -> None:
        """Write and fsync all bytes to one already confined descriptor."""
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise OSError("The controller could not complete the file write.")
            offset += written
        os.fsync(descriptor)

    @staticmethod
    def _read_anchored_file(
        directory_fd: int,
        leaf_name: str,
    ) -> tuple[bytes, str, int, tuple[int, int, int, int, int], int]:
        """Read one no-follow regular file through its anchored parent."""
        file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        file_fd = os.open(leaf_name, file_flags, dir_fd=directory_fd)
        try:
            before = WorkspaceController._stable_file_identity_from_stat(
                os.fstat(file_fd)
            )
            content = bytearray()
            digest = hashlib.sha256()
            while True:
                chunk = os.read(file_fd, 64 * 1_024)
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > MAX_CONTROLLER_DELETE_BYTES:
                    raise ValueError(
                        "The controller action refuses files larger than "
                        f"{MAX_CONTROLLER_DELETE_BYTES:,} bytes."
                    )
                digest.update(chunk)
            after = WorkspaceController._stable_file_identity_from_stat(
                os.fstat(file_fd)
            )
            entry = WorkspaceController._stable_file_identity_from_stat(
                os.stat(leaf_name, dir_fd=directory_fd, follow_symlinks=False)
            )
            if after != before or entry != before:
                raise RuntimeError(
                    "The file changed while the controller was checking it; read it again "
                    "before retrying."
                )
            return bytes(content), digest.hexdigest(), before[2], before, file_fd
        except BaseException:
            os.close(file_fd)
            raise

    @staticmethod
    def _hash_anchored_file(
        directory_fd: int,
        leaf_name: str,
    ) -> tuple[str, int, tuple[int, int, int, int, int], int]:
        """Open and hash one no-follow file relative to an anchored parent."""
        _content, digest, file_bytes, identity, file_fd = (
            WorkspaceController._read_anchored_file(directory_fd, leaf_name)
        )
        return digest, file_bytes, identity, file_fd

    @staticmethod
    def _restore_quarantined_entry(
        directory_fd: int,
        quarantine_name: str,
        leaf_name: str,
    ) -> bool:
        """Restore a moved entry only while its original name remains unoccupied."""
        try:
            os.link(
                quarantine_name,
                leaf_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            return False
        os.unlink(quarantine_name, dir_fd=directory_fd)
        os.fsync(directory_fd)
        return True

    @staticmethod
    def _restore_path_quarantine(quarantine_path: Path, target_path: Path) -> bool:
        """Restore a path-based quarantine without replacing a concurrent target."""
        try:
            os.link(quarantine_path, target_path)
        except FileExistsError:
            return False
        except OSError:
            if os.name != "nt":
                raise
            try:
                quarantine_path.rename(target_path)
            except FileExistsError:
                return False
            return True
        quarantine_path.unlink()
        return True

    def _replace_text_file(self, relative: Path, old: str, new: str) -> str:
        """Compare and atomically replace one existing text file."""
        if _ANCHORED_MUTATION_SUPPORTED:
            directory_fd, leaf_name, _created = self._open_anchored_parent(relative)
            source_fd = -1
            temporary_fd = -1
            temporary_name = f".{leaf_name}.agent-{secrets.token_hex(8)}.tmp"
            backup_name = f".{leaf_name}.agent-backup-{secrets.token_hex(8)}.tmp"
            temporary_exists = False
            backup_exists = False
            replacement_published = False
            directory_lock_held = False
            try:
                fcntl.flock(directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                directory_lock_held = True
                source_bytes, source_digest, _source_size, source_identity, source_fd = (
                    self._read_anchored_file(directory_fd, leaf_name)
                )
                source = source_bytes.decode("utf-8")
                occurrences = source.count(old)
                if occurrences != 1:
                    raise ValueError(
                        "Replace text must appear exactly once; found "
                        f"{occurrences:,} occurrences."
                    )
                replacement = source.replace(old, new, 1).encode("utf-8")
                temporary_fd = os.open(
                    temporary_name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_NOFOLLOW
                    | getattr(os, "O_CLOEXEC", 0),
                    stat_module.S_IMODE(source_identity[4]),
                    dir_fd=directory_fd,
                )
                temporary_exists = True
                os.fchmod(temporary_fd, stat_module.S_IMODE(source_identity[4]))
                self._write_descriptor(temporary_fd, replacement)
                replacement_identity = self._stable_file_identity_from_stat(
                    os.fstat(temporary_fd)
                )
                os.close(temporary_fd)
                temporary_fd = -1
                current_digest, _current_size, current_identity, current_fd = (
                    self._hash_anchored_file(directory_fd, leaf_name)
                )
                try:
                    original_identity = self._stable_file_identity_from_stat(
                        os.fstat(source_fd)
                    )
                finally:
                    os.close(current_fd)
                if (
                    current_digest != source_digest
                    or current_identity != source_identity
                    or original_identity != source_identity
                ):
                    raise RuntimeError(
                        "The file changed before replacement; read it again before retrying."
                    )
                os.rename(
                    leaf_name,
                    backup_name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
                backup_exists = True
                committed_digest, _committed_size, committed_identity, committed_fd = (
                    self._hash_anchored_file(directory_fd, backup_name)
                )
                os.close(committed_fd)
                if (
                    committed_digest != source_digest
                    or committed_identity != source_identity
                ):
                    raise RuntimeError(
                        "The file changed at the replacement commit boundary; the concurrent "
                        "version was preserved."
                    )
                try:
                    os.link(
                        temporary_name,
                        leaf_name,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError as exc:
                    raise RuntimeError(
                        "A concurrent file appeared at the replacement commit boundary; it "
                        "was preserved."
                    ) from exc
                replacement_published = True
                os.unlink(temporary_name, dir_fd=directory_fd)
                temporary_exists = False
                os.fsync(directory_fd)
                try:
                    rebound = self._resolve_path(relative)
                    rebound_digest, _rebound_size, rebound_identity = (
                        self._current_file_sha256(rebound)
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    raise RuntimeError(
                        "The replaced file could not be rebound to the selected workspace safely."
                    ) from exc
                if (
                    rebound_digest != hashlib.sha256(replacement).hexdigest()
                    or rebound_identity != replacement_identity
                ):
                    raise RuntimeError(
                        "The replaced file changed before the controller could verify it."
                    )
                final_backup_digest, _final_backup_size, final_backup_identity, final_backup_fd = (
                    self._hash_anchored_file(directory_fd, backup_name)
                )
                try:
                    if (
                        final_backup_digest != source_digest
                        or final_backup_identity != source_identity
                    ):
                        raise RuntimeError(
                            "The prior file received a late edit after replacement publication."
                        )
                finally:
                    os.close(final_backup_fd)
                os.fsync(directory_fd)
            except BaseException as exc:
                if backup_exists and not replacement_published:
                    try:
                        restored = self._restore_quarantined_entry(
                            directory_fd,
                            backup_name,
                            leaf_name,
                        )
                    except OSError:
                        restored = False
                    if restored:
                        backup_exists = False
                    else:
                        raise RuntimeError(
                            "The replacement was cancelled after a concurrent change. The "
                            f"displaced version was preserved as {backup_name}."
                        ) from exc
                elif backup_exists and replacement_published:
                    raise RuntimeError(
                        "The replacement could not be verified. The prior version was preserved "
                        f"as {backup_name}."
                    ) from exc
                raise
            finally:
                if temporary_fd >= 0:
                    os.close(temporary_fd)
                if source_fd >= 0:
                    os.close(source_fd)
                if temporary_exists:
                    try:
                        os.unlink(temporary_name, dir_fd=directory_fd)
                    except OSError:
                        pass
                if directory_lock_held:
                    fcntl.flock(directory_fd, fcntl.LOCK_UN)
                os.close(directory_fd)
            return (relative.parent / backup_name).as_posix()

        if os.name != "nt":
            raise RuntimeError(
                "Safe workspace replacement is unavailable without anchored directory "
                "operations or Windows directory handles."
            )
        return self._replace_text_file_windows(relative, old, new)

    def _replace_text_file_windows(self, relative: Path, old: str, new: str) -> str:
        """Hold the Windows workspace path stable through a guarded replacement."""
        path = self._resolve_path(relative)
        workspace_identity = self._windows_workspace_identity
        if workspace_identity is None:
            raise RuntimeError("The Windows workspace identity is unavailable.")
        parent_identity = _windows_directory_identity(path.parent)
        with _windows_workspace_mutation_guard(
            self.workspace,
            path.parent,
            expected_workspace_identity=workspace_identity,
            expected_parent_identity=parent_identity,
        ):
            return self._replace_text_file_path_guarded(relative, old, new)

    def _replace_text_file_path_guarded(
        self,
        relative: Path,
        old: str,
        new: str,
    ) -> str:
        """Replace one file after a platform guard has fenced every parent path."""
        path = self._resolve_path(relative)
        source_bytes, source_digest, _source_size, source_identity = (
            self._current_file_snapshot(path)
        )
        source = source_bytes.decode("utf-8")
        occurrences = source.count(old)
        if occurrences != 1:
            raise ValueError(
                f"Replace text must appear exactly once; found {occurrences:,} occurrences."
            )
        replacement = source.replace(old, new, 1).encode("utf-8")
        parent_identity = (
            int(path.parent.stat().st_dev),
            int(path.parent.stat().st_ino),
        )
        descriptor, raw_temporary_path = tempfile.mkstemp(
            prefix=f".{path.name}.agent-",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary_path = Path(raw_temporary_path)
        temporary_stat = os.fstat(descriptor)
        temporary_locator = (
            int(temporary_stat.st_dev),
            int(temporary_stat.st_ino),
        )
        backup_path = path.with_name(
            f".{path.name}.agent-backup-{secrets.token_hex(8)}.tmp"
        )
        temporary_exists = True
        backup_exists = False
        replacement_published = False
        replacement_identity: tuple[int, int, int, int, int] | None = None
        try:
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(replacement)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, stat_module.S_IMODE(source_identity[4]))
            replacement_identity = self._stable_file_identity(temporary_path)
            current_path = self._resolve_path(relative)
            current_parent_identity = (
                int(current_path.parent.stat().st_dev),
                int(current_path.parent.stat().st_ino),
            )
            current_digest, _current_size, current_identity = (
                self._current_file_sha256(current_path)
            )
            if (
                current_path != path
                or current_parent_identity != parent_identity
                or current_identity != source_identity
                or current_digest != source_digest
            ):
                raise RuntimeError(
                    "The file or its parent changed before replacement; read it again before retrying."
                )
            current_path.rename(backup_path)
            backup_exists = True
            committed_digest, _committed_size, committed_identity = (
                self._current_file_sha256(backup_path)
            )
            if committed_digest != source_digest or committed_identity != source_identity:
                raise RuntimeError(
                    "The file changed at the replacement commit boundary; the concurrent "
                    "version was preserved."
                )
            publish_path = self._resolve_path(relative, allow_missing=True)
            publish_parent_identity = (
                int(publish_path.parent.stat().st_dev),
                int(publish_path.parent.stat().st_ino),
            )
            if publish_path != path or publish_parent_identity != parent_identity:
                raise RuntimeError(
                    "The file parent changed at the replacement commit boundary; the prior "
                    "version was preserved."
                )
            try:
                os.link(temporary_path, publish_path)
            except FileExistsError as exc:
                raise RuntimeError(
                    "A concurrent file appeared at the replacement commit boundary; it was "
                    "preserved."
                ) from exc
            replacement_published = True
            temporary_path.unlink()
            temporary_exists = False
            rebound = self._resolve_path(relative)
            rebound_digest, _rebound_size, rebound_identity = (
                self._current_file_sha256(rebound)
            )
            if (
                rebound_digest != hashlib.sha256(replacement).hexdigest()
                or rebound_identity != replacement_identity
            ):
                raise RuntimeError(
                    "The replaced file changed before the controller could verify it."
                )
            final_backup_digest, _final_backup_size, final_backup_identity = (
                self._current_file_sha256(backup_path)
            )
            if (
                final_backup_digest != source_digest
                or final_backup_identity != source_identity
            ):
                raise RuntimeError(
                    "The prior file received a late edit after replacement publication."
                )
            return backup_path.relative_to(self.workspace).as_posix()
        except BaseException as exc:
            if backup_exists and not replacement_published:
                try:
                    restored = self._restore_path_quarantine(backup_path, path)
                except OSError:
                    restored = False
                if restored:
                    backup_exists = False
                else:
                    raise RuntimeError(
                        "The replacement was cancelled after a concurrent change. The "
                        f"displaced version was preserved as {backup_path.name}."
                    ) from exc
            elif backup_exists and replacement_published:
                raise RuntimeError(
                    "The replacement could not be verified. The prior version was preserved "
                    f"as {backup_path.name}."
                ) from exc
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_exists:
                try:
                    temporary_stat = temporary_path.lstat()
                    if (
                        int(temporary_stat.st_dev),
                        int(temporary_stat.st_ino),
                    ) == temporary_locator:
                        temporary_path.unlink()
                except (FileNotFoundError, OSError, RuntimeError, ValueError):
                    pass

    def _write_new_file(self, relative: Path, content: bytes) -> int:
        """Create one new file without following or replacing an existing entry."""
        if _ANCHORED_MUTATION_SUPPORTED:
            directory_fd, leaf_name, _created = self._open_anchored_parent(
                relative,
                create_parents=True,
            )
            file_fd = -1
            created = False
            write_completed = False
            file_identity: tuple[int, int, int, int, int] | None = None
            cleanup_name = f".{leaf_name}.agent-cleanup-{secrets.token_hex(8)}.tmp"
            cleanup_exists = False
            directory_lock_held = False
            try:
                fcntl.flock(directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                directory_lock_held = True
                file_fd = os.open(
                    leaf_name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_NOFOLLOW
                    | getattr(os, "O_CLOEXEC", 0),
                    0o644,
                    dir_fd=directory_fd,
                )
                created = True
                self._write_descriptor(file_fd, content)
                file_identity = self._stable_file_identity_from_stat(os.fstat(file_fd))
                write_completed = True
                if file_identity[2] != len(content):
                    raise OSError("The controller could not verify the completed file write.")
                os.fsync(directory_fd)
                try:
                    rebound = self._resolve_path(relative)
                    rebound_identity = self._stable_file_identity(rebound)
                except (OSError, RuntimeError, ValueError) as exc:
                    raise RuntimeError(
                        "The created file could not be rebound to the selected workspace safely."
                    ) from exc
                if rebound_identity != file_identity:
                    raise RuntimeError(
                        "The created file identity changed before the controller verified it."
                    )
                os.close(file_fd)
                file_fd = -1
                return len(content)
            except FileExistsError as exc:
                raise ValueError(
                    "The write action creates new files only; use replace for an existing file."
                ) from exc
            except BaseException as exc:
                if created:
                    try:
                        os.rename(
                            leaf_name,
                            cleanup_name,
                            src_dir_fd=directory_fd,
                            dst_dir_fd=directory_fd,
                        )
                        cleanup_exists = True
                        current_digest, _size, current_identity, current_fd = (
                            self._hash_anchored_file(directory_fd, cleanup_name)
                        )
                        try:
                            cleanup_handle_identity = (
                                self._stable_file_identity_from_stat(os.fstat(file_fd))
                                if file_fd >= 0
                                else file_identity
                            )
                        finally:
                            os.close(current_fd)
                        if (
                            cleanup_handle_identity is not None
                            and current_identity == cleanup_handle_identity
                            and (
                                not write_completed
                                or current_digest == hashlib.sha256(content).hexdigest()
                            )
                        ):
                            os.unlink(cleanup_name, dir_fd=directory_fd)
                            cleanup_exists = False
                            os.fsync(directory_fd)
                        else:
                            restored = self._restore_quarantined_entry(
                                directory_fd,
                                cleanup_name,
                                leaf_name,
                            )
                            cleanup_exists = not restored
                            if not restored:
                                raise RuntimeError(
                                    "The failed write encountered a concurrent file. The "
                                    f"displaced version was preserved as {cleanup_name}."
                                ) from exc
                    except FileNotFoundError:
                        pass
                    except (OSError, RuntimeError, ValueError) as cleanup_error:
                        if cleanup_exists:
                            try:
                                restored = self._restore_quarantined_entry(
                                    directory_fd,
                                    cleanup_name,
                                    leaf_name,
                                )
                            except OSError:
                                restored = False
                            cleanup_exists = not restored
                            if not restored:
                                raise RuntimeError(
                                    "The failed write could not restore a concurrently changed "
                                    f"file. It was preserved as {cleanup_name}."
                                ) from cleanup_error
                raise
            finally:
                if file_fd >= 0:
                    os.close(file_fd)
                if directory_lock_held:
                    fcntl.flock(directory_fd, fcntl.LOCK_UN)
                os.close(directory_fd)

        if os.name != "nt":
            raise RuntimeError(
                "Safe workspace creation is unavailable without anchored directory operations "
                "or Windows directory handles."
            )
        return self._write_new_file_windows(relative, content)

    def _write_new_file_windows(self, relative: Path, content: bytes) -> int:
        """Create one Windows file while every parent remains guarded."""
        if relative.is_absolute() or not relative.name or ".." in relative.parts:
            raise ValueError("The write action requires one workspace-relative file path.")
        workspace_identity = self._windows_workspace_identity
        if workspace_identity is None:
            raise RuntimeError("The Windows workspace identity is unavailable.")
        parent, parent_identity = _ensure_windows_workspace_parent(
            self.workspace,
            relative.parent,
            expected_workspace_identity=workspace_identity,
        )
        with _windows_workspace_mutation_guard(
            self.workspace,
            parent,
            expected_workspace_identity=workspace_identity,
            expected_parent_identity=parent_identity,
        ):
            return self._write_new_file_path_guarded(relative, content)

    def _write_new_file_path_guarded(self, relative: Path, content: bytes) -> int:
        """Create one file after a platform guard has fenced every parent path."""
        path = self._resolve_path(relative, allow_missing=True)
        checked_path = self._resolve_path(relative, allow_missing=True)
        if checked_path != path:
            raise RuntimeError(
                "The file parent changed before creation; start a new task before retrying."
            )
        descriptor = -1
        created = False
        created_locator: tuple[int, int] | None = None
        failure_identity: tuple[int, int, int, int, int] | None = None
        cleanup_path = checked_path.with_name(
            f".{checked_path.name}.agent-cleanup-{secrets.token_hex(8)}.tmp"
        )
        cleanup_exists = False
        try:
            descriptor = os.open(
                checked_path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o644,
            )
            created = True
            created_stat = os.fstat(descriptor)
            created_locator = (int(created_stat.st_dev), int(created_stat.st_ino))
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
                created_identity = self._stable_file_identity_from_stat(
                    os.fstat(handle.fileno())
                )
            final_path = self._resolve_path(relative)
            final_content, _final_digest, _final_size, final_identity = (
                self._current_file_snapshot(final_path)
            )
            if (
                final_path != checked_path
                or final_identity != created_identity
                or final_content != content
            ):
                raise RuntimeError(
                    "The created file could not be rebound to the selected workspace safely."
                )
            return len(content)
        except FileExistsError as exc:
            raise ValueError(
                "The write action creates new files only; use replace for an existing file."
            ) from exc
        except BaseException as exc:
            if descriptor >= 0:
                try:
                    failure_identity = self._stable_file_identity_from_stat(
                        os.fstat(descriptor)
                    )
                except (OSError, RuntimeError, ValueError):
                    failure_identity = None
                os.close(descriptor)
                descriptor = -1
            elif created:
                try:
                    failure_identity = self._stable_file_identity(checked_path)
                except (OSError, RuntimeError, ValueError):
                    failure_identity = None
            if created and created_locator is not None:
                try:
                    checked_path.rename(cleanup_path)
                    cleanup_exists = True
                    cleanup_identity = self._stable_file_identity(cleanup_path)
                    cleanup_locator = (
                        int(cleanup_identity[0]),
                        int(cleanup_identity[1]),
                    )
                    if (
                        cleanup_locator == created_locator
                        and failure_identity is not None
                        and cleanup_identity == failure_identity
                    ):
                        cleanup_path.unlink()
                        cleanup_exists = False
                    else:
                        restored = self._restore_path_quarantine(
                            cleanup_path,
                            checked_path,
                        )
                        cleanup_exists = not restored
                        if not restored:
                            raise RuntimeError(
                                "The failed write encountered a concurrent file. The displaced "
                                f"version was preserved as {cleanup_path.name}."
                            ) from exc
                except FileNotFoundError:
                    pass
                except (OSError, RuntimeError, ValueError) as cleanup_error:
                    if cleanup_exists:
                        try:
                            restored = self._restore_path_quarantine(
                                cleanup_path,
                                checked_path,
                            )
                        except OSError:
                            restored = False
                        if not restored:
                            raise RuntimeError(
                                "The failed write could not restore a concurrently changed file. "
                                f"It was preserved as {cleanup_path.name}."
                            ) from cleanup_error
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    # Public capability boundary -------------------------------------------
    #
    # Everything below implements ``workspace.capabilities.WorkspaceAccess`` for
    # connections that are not the Browser Agent run loop. They keep path
    # admission, read receipts, and edit-generation bookkeeping inside this class
    # so no other module has to reach for a private method or a mutable field.

    def _require_not_stopped(self) -> None:
        """Refuse direct capability work after the owning service has stopped."""
        if self.should_stop():
            raise RuntimeError("Stop requested.")

    @property
    def root(self) -> Path:
        """Return the resolved project root this controller is bound to."""
        return self.workspace

    @property
    def verification_current(self) -> bool:
        """Return whether verification evidence still matches the current workspace."""
        return bool(self.state.verification_current)

    def project_relative_path(self, raw_path: Any, *, allow_missing: bool = False) -> str:
        """Admit one caller-supplied path and return it relative to the project root."""
        self._require_not_stopped()
        resolved = self._resolve_path(raw_path, allow_missing=allow_missing)
        return resolved.relative_to(self.workspace).as_posix()

    def refresh_stale_read_receipt(self, relative_path: str, *, expected_sha256: str) -> bool:
        """Re-read a file whose receipt aged out but still matches the caller's digest.

        Any local edit advances the workspace generation and ages every existing
        receipt. Only a receipt this caller already holds, with the same digest, is
        refreshed; a caller that never read the file gains no synthesized receipt.
        """
        self._require_not_stopped()
        receipt = self.state.read_receipts.get(relative_path)
        if receipt is None or receipt[2] == self.state.workspace_generation:
            return False
        if receipt[0] != str(expected_sha256 or "").strip().casefold():
            return False
        if not (self.workspace / relative_path).is_file():
            return False
        observation = self.execute({"action": "read", "path": relative_path})
        return bool(observation.get("ok"))

    def file_snapshot(self, relative_path: str) -> FileSnapshot:
        """Read and hash one existing regular file in a single consistent pass."""
        self._require_not_stopped()
        path = self._resolve_path(relative_path)
        data, digest, size, _identity = self._current_file_snapshot(path)
        return FileSnapshot(
            relative_path=path.relative_to(self.workspace).as_posix(),
            data=data,
            sha256=digest,
            size=size,
        )

    def existing_file_snapshot(self, relative_path: str) -> FileSnapshot | None:
        """Return the snapshot of an existing regular file, or ``None`` when absent."""
        self._require_not_stopped()
        path = self._resolve_path(relative_path, allow_missing=True)
        relative = path.relative_to(self.workspace).as_posix()
        if not path.exists():
            return None
        if not path.is_file():
            raise ValueError(f"{relative} is not a regular file.")
        data, digest, size, _identity = self._current_file_snapshot(path)
        return FileSnapshot(
            relative_path=relative,
            data=data,
            sha256=digest,
            size=size,
        )

    def current_read_receipt_snapshot(
        self,
        relative_path: str,
        *,
        expected_sha256: str,
    ) -> FileSnapshot:
        """Return the current file only when the caller holds its active read receipt."""
        self._require_not_stopped()
        path = self._resolve_path(relative_path)
        relative = path.relative_to(self.workspace).as_posix()
        expected = str(expected_sha256 or "").strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError(
                "Replacing a file requires the lowercase SHA-256 from a current read action."
            )
        receipt = self.state.read_receipts.get(relative)
        if receipt is None or receipt[2] != self.state.workspace_generation:
            raise ValueError(
                "Replacing a file requires this workspace to read the current file first."
            )
        if receipt[0] != expected:
            raise ValueError(
                "The supplied SHA-256 does not match this workspace's current read receipt."
            )
        data, digest, size, identity = self._current_file_snapshot(path)
        if digest != expected or identity != receipt[1]:
            raise ValueError(
                "The file no longer matches the current read receipt; read it again before "
                "replacing it."
            )
        return FileSnapshot(
            relative_path=relative,
            data=data,
            sha256=digest,
            size=size,
        )

    def _require_writable(self) -> None:
        """Refuse a boundary mutation on a read-only task.

        ``execute`` rejects mutating actions through the capability registry. The
        public capability methods do not pass through that dispatch, so they check
        the same rule here instead of trusting each caller to check it first.
        """
        if self.read_only:
            raise RuntimeError(
                "This Agent task is read-only; only list, read, search, job_status, "
                "and bodycheck are allowed."
            )

    def apply_text_replacements(self, replacements: Sequence[TextReplacement]) -> None:
        """Apply one atomic batch of whole-text replacements as a single edit."""
        self._require_not_stopped()
        self._require_writable()
        planned = [
            (Path(self.project_relative_path(item.relative_path)), item)
            for item in replacements
        ]
        self._require_not_stopped()
        self._mark_edit()
        for relative, item in planned:
            self._replace_text_file(relative, item.source, item.text)

    def overwrite_text_file(self, relative_path: str, *, source: str, content: str) -> None:
        """Replace one existing text file's contents as a single edit."""
        self._require_not_stopped()
        self._require_writable()
        relative = Path(self.project_relative_path(relative_path))
        self._require_not_stopped()
        self._mark_edit()
        self._replace_text_file(relative, source, content)

    def create_file(self, relative_path: str, data: bytes) -> int:
        """Create one new file that does not exist yet and return its byte count."""
        self._require_not_stopped()
        self._require_writable()
        relative = Path(self.project_relative_path(relative_path, allow_missing=True))
        self._require_not_stopped()
        self._mark_edit()
        return self._write_new_file(relative, data)

    def _mark_edit(self) -> None:
        """Advance controller and workspace generations after one local mutation."""
        self.state.edit_generation += 1
        self.state.workspace_generation += 1
        self.state.evidence_complete = False
        self.state.successful_checks.clear()

    @property
    def verification_required(self) -> bool:
        """Return whether this write-capable run owes verification evidence."""
        return bool(
            not self.read_only
            and (
                self.state.edit_generation > 0
                or self.state.workspace_generation > 0
            )
        )

    def _invalidate_verification_order(self) -> None:
        """Require a fresh verification followed by a fresh bodycheck."""
        self.state.verification_generation = -1
        self.state.verification_workspace_generation = -1
        self.state.verification_snapshot_id = ""
        self.state.bodycheck_generation = -1
        self.state.bodycheck_workspace_generation = -1
        self.state.bodycheck_snapshot_id = ""
        self.state.successful_checks.clear()

    def _record_workspace_snapshot(
        self,
        snapshot_id: str,
        *,
        complete: bool,
    ) -> bool:
        """Record one observed workspace version and invalidate stale evidence."""
        previous = self.state.workspace_snapshot_id
        changed = bool(complete and previous and previous != snapshot_id)
        if changed:
            self.state.workspace_generation += 1
            self.state.successful_checks.clear()
        if not complete:
            if self.state.evidence_complete or previous:
                self.state.workspace_generation += 1
                self.state.successful_checks.clear()
            self.state.workspace_snapshot_id = ""
            self.state.evidence_complete = False
            return False
        self.state.workspace_snapshot_id = snapshot_id
        self.state.evidence_complete = True
        return changed

    def refresh_workspace_evidence(
        self,
        *,
        timeout_seconds: float = WORKSPACE_FINGERPRINT_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """Refresh the point-in-time content evidence used by finalization gates."""
        self._require_workspace_identity()
        snapshot_id, complete = _workspace_mutation_fingerprint(
            self.workspace,
            should_stop=self.should_stop,
            timeout_seconds=timeout_seconds,
        )
        self._require_workspace_identity()
        changed = self._record_workspace_snapshot(
            snapshot_id,
            complete=complete,
        )
        return {
            **self.evidence_metadata(),
            "workspace_changed": changed,
        }

    def evidence_metadata(self) -> dict[str, Any]:
        """Return content-free evidence identifiers for observations and checkpoints."""
        return {
            "edit_generation": self.state.edit_generation,
            "workspace_generation": self.state.workspace_generation,
            "snapshot_id": self.state.workspace_snapshot_id,
            "evidence_complete": self.state.evidence_complete,
            "bodycheck_current": self.state.bodycheck_current,
            "verification_current": self.state.verification_current,
        }

    def _current_file_sha256(self, path: Path) -> tuple[str, int, tuple[int, int, int, int, int]]:
        """Hash one bounded regular file and reject changes while it is being read."""
        _content, digest, file_bytes, identity = self._current_file_snapshot(path)
        return digest, file_bytes, identity

    def _current_file_snapshot(
        self,
        path: Path,
    ) -> tuple[bytes, str, int, tuple[int, int, int, int, int]]:
        """Read one bounded file once so returned text and SHA-256 share one snapshot."""
        before = self._stable_file_identity(path)
        content = bytearray()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(64 * 1_024)
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > MAX_CONTROLLER_DELETE_BYTES:
                    raise ValueError(
                        "The controller action refuses files larger than "
                        f"{MAX_CONTROLLER_DELETE_BYTES:,} bytes."
                    )
                digest.update(chunk)
            after_handle = self._stable_file_identity_from_stat(os.fstat(handle.fileno()))
        after_path = self._stable_file_identity(path)
        if after_handle != before or after_path != before:
            raise RuntimeError(
                "The file changed while the controller was checking it; read it again before retrying."
            )
        return bytes(content), digest.hexdigest(), before[2], before

    def _untracked_files_pass_whitespace_check(self, status: str) -> bool:
        """Apply Git-style trailing-whitespace checks to safe untracked files."""
        for relative in _safe_untracked_paths_from_status(status):
            path = self._resolve_path(relative)
            if not path.is_file():
                raise RuntimeError(
                    "An untracked path changed while bodycheck was inspecting it."
                )
            content, _digest, _file_bytes, _identity = self._current_file_snapshot(path)
            if b"\x00" in content:
                continue
            if any(
                line.rstrip(b"\r\n").endswith((b" ", b"\t"))
                for line in content.splitlines(keepends=True)
            ) or re.search(rb"(?:\r?\n){2,}\Z", content):
                return False
        return True

    def _list(self, payload: dict[str, Any]) -> dict[str, Any]:
        root = self._resolve_path(payload.get("path", "."))
        if not root.is_dir():
            raise ValueError("The list action requires a directory.")
        depth = max(1, min(6, int(payload.get("depth", 2))))
        rows: list[str] = []
        pending = deque([(root, 0)])
        inspected_directories = 0
        scan_truncated = False
        while pending and len(rows) < 2_000:
            directory, directory_depth = pending.popleft()
            inspected_directories += 1
            if inspected_directories > 12_000:
                scan_truncated = True
                break
            try:
                entries = sorted(
                    directory.iterdir(),
                    key=lambda item: item.name.casefold(),
                )
            except OSError:
                continue
            for path in entries:
                relative_to_workspace = path.relative_to(self.workspace)
                if (
                    _path_has_ignored_part(relative_to_workspace)
                    or _path_has_controller_internal_file(relative_to_workspace)
                    or _path_has_sensitive_part(relative_to_workspace)
                    or _path_is_link_like(path)
                ):
                    continue
                if _is_safe_context_directory(self.workspace, path):
                    rows.append(relative_to_workspace.as_posix() + "/")
                    if directory_depth + 1 < depth:
                        pending.append((path, directory_depth + 1))
                elif _is_safe_context_file(self.workspace, path):
                    rows.append(relative_to_workspace.as_posix())
                if len(rows) >= 2_000:
                    scan_truncated = True
                    break
        rows.sort(key=str.casefold)
        return {
            "ok": True,
            "action": "list",
            "entries": rows,
            "truncated": scan_truncated or bool(pending),
        }

    def _read(self, payload: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_path(payload.get("path"))
        if not path.is_file():
            raise ValueError("The read action requires a regular file.")
        if path.stat().st_size > MAX_CONTROLLER_DELETE_BYTES:
            raise ValueError(
                "The requested file is too large for a text read. Treat it as a binary or large asset; "
                "do not retry the same read. Use a controller-readable manifest or an approved verification "
                "script instead."
            )
        content_bytes, sha256, _file_bytes, identity = self._current_file_snapshot(path)
        text = content_bytes.decode("utf-8", errors="replace").splitlines()
        start = max(1, int(payload.get("start_line", 1)))
        end = min(len(text), max(start, int(payload.get("end_line", start + 239))))
        lines = [f"{index}: {text[index - 1]}" for index in range(start, end + 1)]
        content = _truncate_text("\n".join(lines), MAX_FILE_READ_CHARS)
        relative_path = path.relative_to(self.workspace).as_posix()
        self.state.read_receipts[relative_path] = (
            sha256,
            identity,
            self.state.workspace_generation,
        )
        return {
            "ok": True,
            "action": "read",
            "path": relative_path,
            "start_line": start,
            "end_line": end,
            "total_lines": len(text),
            "content": content,
            "sha256": sha256,
        }

    def _search(self, payload: dict[str, Any]) -> dict[str, Any]:
        query = str(payload.get("query") or "").strip()
        if not query:
            raise ValueError("The search action requires a query.")
        if (
            len(query) > MAX_SEARCH_QUERY_CHARS
            or "\x00" in query
            or "\n" in query
            or "\r" in query
        ):
            raise ValueError(
                "The search query is invalid or exceeds the controller limit."
            )
        root = self._resolve_path(payload.get("path", "."))
        max_results = max(1, min(300, int(payload.get("max_results", 80))))
        glob = str(payload.get("glob") or "").strip()
        if len(glob) > 1_000 or "\x00" in glob or "\n" in glob or "\r" in glob:
            raise ValueError("The search glob is invalid or exceeds the controller limit.")
        if glob.startswith("!"):
            raise ValueError("The search glob must be an inclusive pattern.")
        if any(character in glob for character in "{}[]"):
            raise ValueError(
                "The search glob supports literals, path separators, *, ?, and ** only."
            )
        ripgrep = _trusted_system_executable("rg", forbidden_root=self.workspace)
        if ripgrep is None:
            matches = _fallback_search_matches(
                workspace=self.workspace,
                root=root,
                query=query,
                glob=glob,
                max_results=max_results,
                should_stop=self.should_stop,
            )
            if self.should_stop():
                return {
                    "ok": False,
                    "action": "search",
                    "stopped": True,
                    "error": "Stop requested.",
                }
            return {
                "ok": True,
                "action": "search",
                "matches": matches,
                "truncated": len(matches) >= max_results,
                "engine": "python-fallback",
            }
        command = [
            str(ripgrep),
            "--no-config",
            "--json",
            "--line-number",
            "--with-filename",
            "--fixed-strings",
            "--color",
            "never",
            "--hidden",
            "--no-ignore",
            "--no-follow",
            "--no-messages",
            "--max-count",
            str(max_results),
            "--max-filesize",
            str(SEARCH_MAX_FILE_BYTES),
        ]
        include_glob_option = "--iglob" if is_windows_host() else "--glob"
        for included_glob in _search_include_globs(glob, root, self.workspace):
            command.extend([include_glob_option, included_glob])
        for excluded_glob in _search_exclusion_globs():
            command.extend(["--iglob", excluded_glob])
        command.extend(["--", query, str(root.relative_to(self.workspace) or ".")])
        if self.should_stop():
            return {
                "ok": False,
                "action": "search",
                "stopped": True,
                "error": "Stop requested.",
            }
        try:
            process = subprocess.Popen(
                command,
                cwd=self.workspace,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **_process_group_options(),
            )
        except FileNotFoundError:
            matches = _fallback_search_matches(
                workspace=self.workspace,
                root=root,
                query=query,
                glob=glob,
                max_results=max_results,
                should_stop=self.should_stop,
            )
            if self.should_stop():
                return {
                    "ok": False,
                    "action": "search",
                    "stopped": True,
                    "error": "Stop requested.",
                }
            return {
                "ok": True,
                "action": "search",
                "matches": matches,
                "truncated": len(matches) >= max_results,
                "engine": "python-fallback",
            }

        if process.stdout is None or process.stderr is None:
            _stop_process(process, timeout=1)
            raise RuntimeError("Search could not open bounded ripgrep output streams.")
        matches: list[str] = []
        raw_events = 0
        truncated = False
        stopped = False
        timed_out = False
        stream_failed = False
        terminated_early = False
        loop_completed = False
        discard_output: Event | None = None
        stdout_thread: Thread | None = None
        stderr_thread: Thread | None = None
        deadline = time.monotonic() + SEARCH_TIMEOUT_SECONDS
        try:
            output_queue: Queue[Any] = Queue(maxsize=SEARCH_STDOUT_QUEUE_SIZE)
            discard_output = Event()
            stdout_thread = Thread(
                target=_queue_text_lines,
                args=(process.stdout, output_queue, discard_output),
                daemon=True,
            )
            stderr_thread = Thread(
                target=_discard_text_stream,
                args=(process.stderr,),
                daemon=True,
            )
            self.process_changed(process)
            stdout_thread.start()
            stderr_thread.start()
            while True:
                if self.should_stop():
                    stopped = True
                    terminated_early = True
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    terminated_early = True
                    break
                try:
                    remaining = max(0.001, deadline - time.monotonic())
                    value = output_queue.get(timeout=min(0.05, remaining))
                except Empty:
                    if not stdout_thread.is_alive() and output_queue.empty():
                        break
                    continue
                if value is _STREAM_READ_FAILED:
                    stream_failed = True
                    terminated_early = True
                    break
                if value is None:
                    break
                if not isinstance(value, str):
                    stream_failed = True
                    terminated_early = True
                    break
                raw_events += 1
                if raw_events > SEARCH_MAX_RAW_EVENTS:
                    truncated = True
                    terminated_early = True
                    break
                normalized = _parse_rg_search_match(value)
                if normalized is None:
                    continue
                relative_path, normalized_value = normalized
                candidate_path = self.workspace / relative_path
                if (
                    _path_has_ignored_part(relative_path)
                    or _path_has_controller_internal_file(relative_path)
                    or _path_has_sensitive_part(relative_path)
                ):
                    continue
                try:
                    if root.is_file():
                        if candidate_path != root:
                            continue
                    else:
                        candidate_path.relative_to(root)
                except ValueError:
                    continue
                if (
                    not _path_matches_search_glob(
                        candidate_path,
                        root,
                        glob,
                        workspace=self.workspace,
                    )
                    or not _is_confined_search_match(
                        self.workspace,
                        root,
                        relative_path,
                    )
                ):
                    continue
                matches.append(normalized_value)
                if len(matches) >= max_results:
                    truncated = True
                    terminated_early = True
                    break
            loop_completed = True
        finally:
            if terminated_early or stream_failed or not loop_completed:
                if discard_output is not None:
                    discard_output.set()
                _stop_process(process, timeout=1)
            try:
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired, ValueError):
                if discard_output is not None:
                    discard_output.set()
                _stop_process(process, timeout=1)
            if isinstance(process, _SUBPROCESS_POPEN_TYPE):
                _stop_process(process, timeout=0.25)
            if discard_output is not None:
                discard_output.set()
            if stdout_thread is not None and stdout_thread.ident is not None:
                stdout_thread.join(timeout=1)
            if stderr_thread is not None and stderr_thread.ident is not None:
                stderr_thread.join(timeout=1)
            streams_and_readers = (
                (process.stdout, stdout_thread),
                (process.stderr, stderr_thread),
            )
            for stream, reader_thread in streams_and_readers:
                if reader_thread is not None and reader_thread.is_alive():
                    continue
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
            self.process_changed(None)

        if stopped:
            return {
                "ok": False,
                "action": "search",
                "stopped": True,
                "error": "Stop requested.",
            }
        if timed_out:
            raise RuntimeError(
                f"Search exceeded the {SEARCH_TIMEOUT_SECONDS}-second controller limit."
            )
        if stream_failed:
            raise RuntimeError("Search output could not be read safely.")
        if not terminated_early and process.returncode not in {0, 1}:
            raise RuntimeError(
                f"Search failed with ripgrep exit code {process.returncode}."
            )
        return {
            "ok": True,
            "action": "search",
            "matches": matches,
            "truncated": truncated,
            "engine": "rg",
        }

    def _replace(self, payload: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_path(payload.get("path"))
        if not path.is_file():
            raise ValueError("The replace action requires an existing file.")
        relative = path.relative_to(self.workspace)
        old = str(payload.get("old") or "")
        new = str(payload.get("new") or "")
        if not old:
            raise ValueError("The replace action requires non-empty old text.")
        self._mark_edit()
        recovery_path = self._replace_text_file(relative, old, new)
        return {
            "ok": True,
            "action": "replace",
            "path": relative.as_posix(),
            "changed_characters": len(new) - len(old),
            "recovery_path": recovery_path,
        }

    def _write(self, payload: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_path(payload.get("path"), allow_missing=True)
        if path.exists():
            raise ValueError("The write action creates new files only; use replace for an existing file.")
        content = str(payload.get("content") or "")
        if not content:
            raise ValueError("The write action requires file content.")
        self._mark_edit()
        written_bytes = self._write_new_file(
            path.relative_to(self.workspace),
            content.encode("utf-8"),
        )
        return {
            "ok": True,
            "action": "write",
            "path": path.relative_to(self.workspace).as_posix(),
            "bytes": written_bytes,
        }

    def _replace_base64(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Replace with base64-encoded old/new to avoid HTML quote corruption."""
        path = self._resolve_path(payload.get("path"))
        if not path.is_file():
            raise ValueError("The replace_base64 action requires an existing file.")
        relative = path.relative_to(self.workspace)
        old = _decode_base64_utf8(
            str(payload.get("old_base64") or ""),
            field_name="old_base64",
            allow_empty=False,
        )
        if not old:
            raise ValueError("The replace_base64 action requires non-empty old text.")
        new = _decode_base64_utf8(
            str(payload.get("new_base64") or ""),
            field_name="new_base64",
            allow_empty=True,
        )
        self._mark_edit()
        recovery_path = self._replace_text_file(relative, old, new)
        return {
            "ok": True,
            "action": "replace_base64",
            "path": relative.as_posix(),
            "changed_characters": len(new) - len(old),
            "recovery_path": recovery_path,
        }

    def _write_base64(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Write a new file with base64-encoded content to avoid quote corruption."""
        path = self._resolve_path(payload.get("path"), allow_missing=True)
        if path.exists():
            raise ValueError("The write_base64 action creates new files only; use replace_base64 for an existing file.")
        content = _decode_base64_utf8(
            str(payload.get("content_base64") or ""),
            field_name="content_base64",
            allow_empty=False,
        )
        if not content:
            raise ValueError("The write_base64 action requires non-empty decoded content.")
        self._mark_edit()
        written_bytes = self._write_new_file(
            path.relative_to(self.workspace),
            content.encode("utf-8"),
        )
        return {
            "ok": True,
            "action": "write_base64",
            "path": path.relative_to(self.workspace).as_posix(),
            "bytes": written_bytes,
        }

    def _delete(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Delete one read-verified regular file without accepting recursive targets."""
        path = self._resolve_path(payload.get("path"))
        if not path.is_file():
            raise ValueError("The delete action requires an existing regular file.")
        relative = path.relative_to(self.workspace)
        relative_key = relative.as_posix()
        expected_sha256 = str(payload.get("expected_sha256") or "").strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ValueError(
                "The delete action requires the lowercase SHA-256 from a current read action."
            )
        receipt = self.state.read_receipts.get(relative_key)
        if receipt is None or receipt[2] != self.state.workspace_generation:
            raise ValueError(
                "The delete action requires this controller to read the current file first."
            )
        if receipt[0] != expected_sha256:
            raise ValueError(
                "The supplied SHA-256 does not match this controller's current read receipt."
            )
        directory_fd, leaf_name = self._open_anchored_delete_parent(relative)
        file_fd = -1
        tombstone_name = f".{leaf_name}.agent-delete-{secrets.token_hex(8)}.tmp"
        tombstone_exists = False
        directory_lock_held = False
        try:
            try:
                fcntl.flock(directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                directory_lock_held = True
            except OSError as exc:
                raise RuntimeError(
                    "The controller could not acquire the workspace directory lock; "
                    "the file was not deleted."
                ) from exc
            current_sha256, deleted_bytes, identity, file_fd = self._hash_anchored_file(
                directory_fd,
                leaf_name,
            )
            if current_sha256 != expected_sha256 or identity != receipt[1]:
                raise ValueError(
                    "The file no longer matches the current read receipt; read it again "
                    "before deleting."
                )
            current_entry = self._stable_file_identity_from_stat(
                os.stat(leaf_name, dir_fd=directory_fd, follow_symlinks=False)
            )
            if current_entry != identity:
                raise RuntimeError(
                    "The file identity changed before deletion; read it again before retrying."
                )
            self._mark_edit()
            os.rename(
                leaf_name,
                tombstone_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            tombstone_exists = True
            committed_digest, _committed_size, committed_identity, committed_fd = (
                self._hash_anchored_file(directory_fd, tombstone_name)
            )
            os.close(committed_fd)
            if (
                committed_digest != expected_sha256
                or committed_identity != identity
                or self._stable_file_identity_from_stat(os.fstat(file_fd)) != identity
            ):
                raise RuntimeError(
                    "The file identity changed before deletion; the concurrent version was "
                    "preserved."
                )
            os.unlink(tombstone_name, dir_fd=directory_fd)
            tombstone_exists = False
            os.fsync(directory_fd)
        except BaseException as exc:
            if tombstone_exists:
                try:
                    restored = self._restore_quarantined_entry(
                        directory_fd,
                        tombstone_name,
                        leaf_name,
                    )
                except OSError:
                    restored = False
                if restored:
                    tombstone_exists = False
                else:
                    raise RuntimeError(
                        "Deletion was cancelled after a concurrent change. The displaced "
                        f"version was preserved as {tombstone_name}."
                    ) from exc
            raise
        finally:
            if file_fd >= 0:
                os.close(file_fd)
            if directory_lock_held:
                fcntl.flock(directory_fd, fcntl.LOCK_UN)
            os.close(directory_fd)
        return {
            "ok": True,
            "action": "delete",
            "path": relative_key,
            "deleted_bytes": deleted_bytes,
        }

    def _run(self, payload: dict[str, Any]) -> dict[str, Any]:
        command = str(payload.get("command") or "").strip()
        command_parts = inspection_command_parts(command, workspace=self.workspace)
        if command_parts[:2] == ["git", "status"]:
            started = time.monotonic()
            try:
                output = _filtered_git_status(
                    self.workspace,
                    should_stop=self.should_stop,
                    process_changed=self.process_changed,
                )
            except RuntimeError:
                if self.should_stop():
                    return {
                        "ok": False,
                        "action": "run",
                        "stopped": True,
                        "error": "Stop requested.",
                    }
                raise
            return {
                "ok": True,
                "action": "run",
                "exit_code": 0,
                "duration_seconds": round(time.monotonic() - started, 2),
                "output": _truncate_text(output, MAX_ACTION_OUTPUT_CHARS),
                "mutated_workspace": False,
                "error": "",
            }
        before_fingerprint, before_scan_complete = (
            _workspace_mutation_fingerprint(
                self.workspace,
                should_stop=self.should_stop,
            )
        )
        if self.should_stop():
            return {
                "ok": False,
                "action": "run",
                "stopped": True,
                "error": "Stop requested.",
            }
        if not before_scan_complete:
            self._record_workspace_snapshot(before_fingerprint, complete=False)
            raise RuntimeError(
                "The verification command was not started because the controller could not "
                "create a complete bounded workspace fingerprint. Narrow the selected "
                "workspace before continuing."
            )
        self._record_workspace_snapshot(before_fingerprint, complete=True)
        self._invalidate_verification_order()
        started = time.monotonic()
        process = subprocess.Popen(
            command_parts,
            cwd=self.workspace,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            **_process_group_options(),
        )
        output = ""
        returncode = -1
        output_truncated = False
        stopped = False
        timed_out = False
        run_error: OSError | RuntimeError | ValueError | None = None
        try:
            self.process_changed(process)
            output, returncode, output_truncated, stopped, timed_out = (
                _bounded_verification_process_output(
                    process,
                    timeout_seconds=self.settings.command_timeout_seconds,
                    should_stop=self.should_stop,
                )
            )
        except (OSError, RuntimeError, ValueError) as exc:
            run_error = exc
        finally:
            self.process_changed(None)
        after_fingerprint, after_scan_complete = (
            _workspace_mutation_fingerprint(
                self.workspace,
                should_stop=self.should_stop,
            )
        )
        workspace_scan_complete = before_scan_complete and after_scan_complete
        mutated_workspace = after_fingerprint != before_fingerprint
        if mutated_workspace or not workspace_scan_complete:
            self._mark_edit()
        elif run_error is None and not stopped and not timed_out and returncode == 0:
            self._record_workspace_snapshot(after_fingerprint, complete=True)
            self.state.verification_generation = self.state.edit_generation
            self.state.verification_workspace_generation = (
                self.state.workspace_generation
            )
            self.state.verification_snapshot_id = after_fingerprint
            self.state.successful_checks.append(command)
        if run_error is not None:
            raise run_error
        if stopped:
            return {
                "ok": False,
                "action": "run",
                "stopped": True,
                "error": "Stop requested.",
                "output": output,
                "output_truncated": output_truncated,
                "mutated_workspace": mutated_workspace,
                "workspace_scan_complete": workspace_scan_complete,
            }
        if timed_out:
            raise RuntimeError(
                f"Command timed out after {self.settings.command_timeout_seconds:,} seconds.\n"
                + output
            )
        return {
            "ok": returncode == 0 and not mutated_workspace and workspace_scan_complete,
            "action": "run",
            "exit_code": returncode,
            "duration_seconds": round(time.monotonic() - started, 2),
            "output": output,
            "output_truncated": output_truncated,
            "mutated_workspace": mutated_workspace,
            "workspace_scan_complete": workspace_scan_complete,
            **self.evidence_metadata(),
            "error": (
                "The verification command changed project files; the prior bodycheck is stale. "
                "Inspect those changes before continuing."
                if mutated_workspace
                else (
                    "The controller could not prove that the verification command left the "
                    "project unchanged within its bounded workspace scan; the prior bodycheck "
                    "is stale. Narrow the selected workspace before continuing."
                    if not workspace_scan_complete
                    else ""
                )
            ),
        }

    def _browser_acceptance(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run clean local Chromium acceptance without using any signed-in browser profile."""
        from ..agent.browser_acceptance import (
            BrowserAcceptanceRequest,
            load_project_protected_ports,
            run_browser_acceptance,
        )
        from ..browser_sessions import sync_playwright_or_error

        root = self._resolve_path(payload.get("root") or ".")
        if not root.is_dir():
            raise ValueError("Browser acceptance root must be a workspace directory.")
        before_fingerprint, before_scan_complete = _workspace_mutation_fingerprint(
            self.workspace,
            should_stop=self.should_stop,
        )
        artifact_root = self._compute_job_runtime_root / "browser-acceptance"
        acceptance_result: dict[str, Any] = {}
        acceptance_failure: BaseException | None = None

        def run_acceptance() -> None:
            nonlocal acceptance_failure
            try:
                acceptance_result.update(
                    run_browser_acceptance(
                        BrowserAcceptanceRequest(
                            root=root,
                            target=str(payload.get("target") or "/"),
                            port=int(payload.get("port") or 0),
                            expected_text=tuple(
                                str(item) for item in (payload.get("expected_text") or [])
                            ),
                            expected_selectors=tuple(
                                str(item) for item in (payload.get("expected_selectors") or [])
                            ),
                            protected_ports=frozenset({DEFAULT_PORT})
                            | load_project_protected_ports(self.workspace),
                            timeout_seconds=float(self.settings.command_timeout_seconds),
                            artifact_root=artifact_root,
                        ),
                        playwright_context=sync_playwright_or_error,
                        process_group_options=_process_group_options,
                        stop_process=_stop_process,
                        process_changed=self.process_changed,
                        should_stop=self.should_stop,
                    )
                )
            except BaseException as exc:  # Re-raised on the controller thread below.
                acceptance_failure = exc

        # The controller thread already owns one Playwright sync connection for
        # its provider browser, and that connection keeps an asyncio loop
        # running in this thread. Opening a second sync_playwright() in the same
        # thread is rejected ("Sync API inside the asyncio loop"), so browser
        # acceptance runs on a dedicated worker thread with its own loop state.
        acceptance_thread = Thread(
            target=run_acceptance,
            name="agent-browser-acceptance",
            daemon=True,
        )
        acceptance_thread.start()
        acceptance_thread.join()
        if acceptance_failure is not None:
            raise acceptance_failure
        result = acceptance_result
        after_fingerprint, after_scan_complete = _workspace_mutation_fingerprint(
            self.workspace,
            should_stop=self.should_stop,
        )
        workspace_scan_complete = before_scan_complete and after_scan_complete
        mutated_workspace = after_fingerprint != before_fingerprint
        if mutated_workspace or not workspace_scan_complete:
            self._mark_edit()
            result["ok"] = False
            result["error"] = (
                "Browser acceptance changed project files; verification evidence is stale."
                if mutated_workspace
                else "Browser acceptance could not prove the project stayed unchanged."
            )
        elif bool(result.get("ok")):
            self._record_workspace_snapshot(after_fingerprint, complete=True)
            self.state.verification_generation = self.state.edit_generation
            self.state.verification_workspace_generation = self.state.workspace_generation
            self.state.verification_snapshot_id = after_fingerprint
            self.state.successful_checks.append("browser_acceptance")
        result["mutated_workspace"] = mutated_workspace
        result["workspace_scan_complete"] = workspace_scan_complete
        result.update(self.evidence_metadata())
        return result

    def _job_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Start one approved worker without borrowing verification-run semantics."""
        status = self._compute_job_manager().start(
            entrypoint_id=str(payload.get("entrypoint") or ""),
            config_path=str(payload.get("config_path") or ""),
            idempotency_key=str(payload.get("idempotency_key") or ""),
            resume_job_id=str(payload.get("resume_job_id") or ""),
        )
        return {"ok": True, "action": "job_start", "job": status}

    def _job_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return one bounded job status observation."""
        status = self._compute_job_manager().status(str(payload.get("job_id") or ""))
        return {"ok": True, "action": "job_status", "job": status}

    def _job_stop(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Stop only the process tree owned by one identity-verified job."""
        status = self._compute_job_manager().stop(str(payload.get("job_id") or ""))
        return {"ok": True, "action": "job_stop", "job": status}

    def _bodycheck(self, _payload: dict[str, Any]) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        before_snapshot_id, before_complete = _workspace_mutation_fingerprint(
            self.workspace,
            should_stop=self.should_stop,
        )
        self._record_workspace_snapshot(
            before_snapshot_id,
            complete=before_complete,
        )
        if not before_complete:
            return {
                "ok": False,
                "action": "bodycheck",
                "error": (
                    "Bodycheck could not create a complete bounded workspace snapshot. "
                    "Narrow the selected workspace before continuing."
                ),
                "checks": [
                    {
                        "name": "workspace evidence snapshot",
                        "ok": False,
                        "output": "The bounded workspace scan was incomplete.",
                    }
                ],
                **self.evidence_metadata(),
            }
        if self.verification_required and not self.state.verification_current:
            return {
                "ok": False,
                "action": "bodycheck",
                "error": (
                    "Bodycheck requires one successful approved verification command after "
                    "the latest edit or workspace change."
                ),
                "checks": [
                    {
                        "name": "current verification",
                        "ok": False,
                        "output": "Run an approved verification command before bodycheck.",
                    }
                ],
                **self.evidence_metadata(),
            }
        if (self.workspace / ".git").exists():
            try:
                status = _filtered_git_status(
                    self.workspace,
                    should_stop=self.should_stop,
                    process_changed=self.process_changed,
                )
            except RuntimeError:
                if self.should_stop():
                    return {
                        "ok": False,
                        "action": "bodycheck",
                        "stopped": True,
                        "error": "Stop requested.",
                    }
                raise
            git = _trusted_system_executable("git", forbidden_root=self.workspace)
            if git is None:
                raise RuntimeError("Git is unavailable for bounded diff inspection.")
            diff_returncode, diff_stopped, diff_check_timed_out = (
                _bounded_devnull_process(
                    [
                        str(git),
                        "-c",
                        "core.fsmonitor=false",
                        "-c",
                        "core.untrackedCache=false",
                        "diff",
                        "--check",
                    ],
                    workspace=self.workspace,
                    timeout_seconds=30,
                    should_stop=self.should_stop,
                    process_changed=self.process_changed,
                )
            )
            if diff_stopped:
                return {
                    "ok": False,
                    "action": "bodycheck",
                    "stopped": True,
                    "error": "Stop requested.",
                }
            diff_check_ok = diff_returncode == 0 and not diff_check_timed_out
            cached_returncode, cached_stopped, cached_check_timed_out = (
                _bounded_devnull_process(
                    [
                        str(git),
                        "-c",
                        "core.fsmonitor=false",
                        "-c",
                        "core.untrackedCache=false",
                        "diff",
                        "--cached",
                        "--check",
                    ],
                    workspace=self.workspace,
                    timeout_seconds=30,
                    should_stop=self.should_stop,
                    process_changed=self.process_changed,
                )
            )
            if cached_stopped:
                return {
                    "ok": False,
                    "action": "bodycheck",
                    "stopped": True,
                    "error": "Stop requested.",
                }
            cached_check_ok = (
                cached_returncode == 0 and not cached_check_timed_out
            )
            untracked_check_ok = self._untracked_files_pass_whitespace_check(status)
            status_complete = "[status truncated at the controller output limit]" not in status
            checks.append(
                {
                    "name": "git status --short",
                    "ok": status_complete,
                    "output": (
                        _truncate_text(status, 16_000)
                        if status_complete
                        else "Git status exceeded the bounded controller output limit."
                    ),
                }
            )
            checks.append(
                {
                    "name": "git diff --check",
                    "ok": diff_check_ok,
                    "output": (
                        ""
                        if diff_check_ok
                        else (
                            "Git diff checking exceeded the 30-second controller limit."
                            if diff_check_timed_out
                            else "Git found whitespace errors in the current project diff."
                        )
                    ),
                }
            )
            checks.append(
                {
                    "name": "git diff --cached --check",
                    "ok": cached_check_ok,
                    "output": (
                        ""
                        if cached_check_ok
                        else (
                            "Git staged-diff checking exceeded the 30-second controller limit."
                            if cached_check_timed_out
                            else "Git found whitespace errors in the staged project diff."
                        )
                    ),
                }
            )
            checks.append(
                {
                    "name": "untracked file whitespace",
                    "ok": untracked_check_ok,
                    "output": (
                        ""
                        if untracked_check_ok
                        else "Git-style whitespace errors were found in an untracked project file."
                    ),
                }
            )
        after_snapshot_id, after_complete = _workspace_mutation_fingerprint(
            self.workspace,
            should_stop=self.should_stop,
        )
        if self.should_stop():
            return {
                "ok": False,
                "action": "bodycheck",
                "stopped": True,
                "error": "Stop requested.",
            }
        workspace_stable = bool(
            after_complete and after_snapshot_id == before_snapshot_id
        )
        self._record_workspace_snapshot(
            after_snapshot_id,
            complete=after_complete,
        )
        checks.append(
            {
                "name": "workspace evidence snapshot",
                "ok": workspace_stable,
                "output": (
                    ""
                    if workspace_stable
                    else (
                        "The workspace changed while bodycheck was running."
                        if after_complete
                        else "The final bounded workspace scan was incomplete."
                    )
                ),
            }
        )
        instructions = [path.relative_to(self.workspace).as_posix() for path in _collect_instruction_files(self.workspace)]
        passed = all(check["ok"] for check in checks)
        if passed:
            self.state.bodycheck_generation = self.state.edit_generation
            self.state.bodycheck_workspace_generation = self.state.workspace_generation
            self.state.bodycheck_snapshot_id = after_snapshot_id
        return {
            "ok": passed,
            "action": "bodycheck",
            "successful_checks": self.state.successful_checks[-20:],
            "instruction_files": instructions,
            "checks": checks,
            **self.evidence_metadata(),
        }
