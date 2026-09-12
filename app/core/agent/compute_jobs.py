"""Durable local compute jobs for approved optimization entrypoints.

Code version: v1.6.1-codex.1
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import secrets
import signal
import stat
import subprocess
import sys
from threading import Event, Lock, Thread
import time
from typing import Any, Callable, Iterator


APPROVAL_FILENAME = ".agenticContext-compute.json"
LEGACY_APPROVAL_FILENAME = ".cachelikes-compute.json"
COMPUTE_JOBS_DIRNAME = "compute-jobs"
COMPUTE_JOB_LOCKS_DIRNAME = "compute-job-locks"
DEFAULT_MAX_RUNTIME_SECONDS = 12 * 60 * 60
MAX_MAX_RUNTIME_SECONDS = 24 * 60 * 60
MAX_CONFIG_BYTES = 1 * 1024 * 1024
MAX_LOG_BYTES = 5 * 1024 * 1024
MAX_LOG_TAIL_CHARS = 4_000
MAX_PROGRESS_BYTES = 64 * 1024
MAX_STATUS_TEXT_CHARS = 1_000
MAX_METADATA_BYTES = 128 * 1024
MAX_ENTRYPOINT_BYTES = 16 * 1024 * 1024
MAX_METADATA_SCAN_WORKSPACES = 512
MAX_METADATA_SCAN_RECORDS = 2_048
MAX_POSIX_JOB_MARKER_SCAN_PROCESSES = 65_536
MAX_POSIX_JOB_MARKER_SCAN_BYTES = 32 * 1024 * 1024
MAX_POSIX_PROCESS_ENVIRONMENT_BYTES = 2 * 1024 * 1024
MAX_POSIX_JOB_MARKER_ENVIRONMENT_BYTES = 64 * 1024 * 1024
POSIX_JOB_MARKER_SCAN_SECONDS = 2.0
WORKER_OWNERSHIP_HANDSHAKE_SECONDS = 30.0
MACOS_SANDBOX_EXECUTABLE = Path("/usr/bin/sandbox-exec")
MACOS_COMPUTE_SANDBOX_PROFILE = (
    "(version 1) (allow default) (deny network*) (deny process-fork)"
)
LINUX_CGROUP_FILESYSTEM = Path("/sys/fs/cgroup")
MAX_LINUX_CGROUP_FILE_BYTES = 1 * 1024 * 1024
MAX_LINUX_CGROUP_PROCESSES = 65_536
PROGRESS_FIELDS = frozenset(
    {
        "generation",
        "iteration",
        "evaluations_completed",
        "evaluations_total",
        "best_objective",
        "elapsed_seconds",
        "eta_seconds",
        "summary",
    }
)
TERMINAL_STATES = frozenset({"succeeded", "failed", "stopped", "interrupted"})
ACTIVE_STATES = frozenset({"starting", "running", "stopping"})
_ENTRYPOINT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_IDEMPOTENCY_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z")
_WORKSPACE_KEY_RE = re.compile(r"[0-9a-f]{24}\Z")
_JOB_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_PROCESS_WORKSPACE_LOCKS_GUARD = Lock()
_PROCESS_WORKSPACE_LOCKS: dict[str, Any] = {}


def _platform_compute_containment_kind() -> str:
    """Return the containment contract required before an approved child can run."""
    if os.name == "nt":
        return "windows-job-object"
    if sys.platform == "darwin":
        return "macos-sandbox-no-fork"
    if sys.platform.startswith("linux"):
        return "linux-cgroup-v2"
    return ""


class ComputeJobError(RuntimeError):
    """Raised when a durable compute-job request violates its contract."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fsync_directory(directory: Path) -> None:
    """Persist a directory-entry change where the host exposes directory fsync."""
    if os.name != "posix":
        return
    descriptor = os.open(
        directory,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Replace one JSON record atomically without following a linked leaf."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ComputeJobError("Compute-job metadata cannot use a symbolic link.")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _write_runtime_snapshot(path: Path, content: bytes) -> None:
    """Create and persist one immutable-by-contract job input snapshot."""
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, 0o600)
    _fsync_directory(path.parent)


@contextmanager
def _persistent_workspace_lock(lock_path: Path) -> Iterator[None]:
    """Serialize one workspace bucket across threads and application processes."""
    lock_key = str(lock_path.absolute())
    with _PROCESS_WORKSPACE_LOCKS_GUARD:
        process_lock = _PROCESS_WORKSPACE_LOCKS.setdefault(lock_key, Lock())
    with process_lock:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if lock_path.is_symlink():
            raise ComputeJobError("Compute-job workspace lock cannot use a symbolic link.")
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOINHERIT", 0)
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise ComputeJobError("Compute-job workspace lock is unavailable.") from exc
        with os.fdopen(descriptor, "r+b") as handle:
            lock_metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(lock_metadata.st_mode):
                raise ComputeJobError(
                    "Compute-job workspace lock must be a regular file."
                )
            created = lock_metadata.st_size == 0
            if created:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
                _fsync_directory(lock_path.parent)
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                return
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _cleanup_staged_job(staged_job_root: Path) -> None:
    """Remove only known pre-publication artifacts from one unique staging directory."""
    for filename in (
        "entrypoint.py",
        "config.json",
        "resume-checkpoint.json",
        "metadata.json",
        "metadata.lock",
        "launch.ready",
    ):
        try:
            (staged_job_root / filename).unlink(missing_ok=True)
        except OSError:
            pass
    try:
        staged_job_root.rmdir()
    except OSError:
        pass


def _stable_regular_file_bytes(
    path: Path,
    *,
    maximum_bytes: int,
    label: str,
) -> tuple[bytes, str]:
    """Read one regular file while proving its path and open handle stayed identical."""
    try:
        before_path = path.lstat()
    except OSError as exc:
        raise ComputeJobError(f"{label} is unavailable.") from exc
    if (
        not stat.S_ISREG(before_path.st_mode)
        or stat.S_ISLNK(before_path.st_mode)
        or before_path.st_nlink != 1
    ):
        raise ComputeJobError(f"{label} must be one unlinked regular file.")
    if before_path.st_size > maximum_bytes:
        raise ComputeJobError(f"{label} exceeds the {maximum_bytes:,}-byte limit.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ComputeJobError(f"{label} could not be opened safely.") from exc
    try:
        before_handle = os.fstat(descriptor)
        content = bytearray()
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > maximum_bytes:
                raise ComputeJobError(
                    f"{label} exceeds the {maximum_bytes:,}-byte limit."
                )
        after_handle = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        after_path = path.lstat()
    except OSError as exc:
        raise ComputeJobError(f"{label} changed while it was being read.") from exc
    identity_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    identities = [
        tuple(int(getattr(metadata, field)) for field in identity_fields)
        for metadata in (before_path, before_handle, after_handle, after_path)
    ]
    if any(identity != identities[0] for identity in identities[1:]):
        raise ComputeJobError(f"{label} changed while it was being read.")
    payload = bytes(content)
    return payload, hashlib.sha256(payload).hexdigest()


@contextmanager
def _metadata_file_lock(metadata_path: Path) -> Iterator[None]:
    """Serialize metadata state transitions across the launcher and worker."""
    lock_path = metadata_path.with_name("metadata.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _update_metadata_file(
    metadata_path: Path,
    updater: Callable[[dict[str, Any]], dict[str, Any] | None],
) -> dict[str, Any]:
    """Apply one locked read-modify-write transition without stale overwrites."""
    expected_job_id = metadata_path.parent.name
    with _metadata_file_lock(metadata_path):
        current = _validate_compute_job_metadata(
            _read_json_object(metadata_path, maximum_bytes=MAX_METADATA_BYTES),
            expected_job_id=expected_job_id,
        )
        updated = updater(dict(current))
        if updated is None:
            return current
        revision = current.get("revision", 0)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ComputeJobError("Compute-job metadata revision is invalid.")
        updated["revision"] = revision + 1
        _atomic_write_json(metadata_path, updated)
        return updated


def _read_json_object(path: Path, *, maximum_bytes: int) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ComputeJobError(f"Required regular JSON file is unavailable: {path.name}")
    if path.stat().st_size > maximum_bytes:
        raise ComputeJobError(f"JSON file exceeds the {maximum_bytes:,}-byte limit: {path.name}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ComputeJobError(f"Invalid JSON file: {path.name}") from exc
    if not isinstance(payload, dict):
        raise ComputeJobError(f"JSON file must contain one object: {path.name}")
    return payload


def compute_job_workspace_identity(metadata: dict[str, Any]) -> tuple[int, int] | None:
    """Return a persisted workspace identity, accepting legacy path-only records."""
    has_device = "workspace_device" in metadata
    has_inode = "workspace_inode" in metadata
    if not has_device and not has_inode:
        return None
    device = metadata.get("workspace_device")
    inode = metadata.get("workspace_inode")
    if (
        not has_device
        or not has_inode
        or isinstance(device, bool)
        or not isinstance(device, int)
        or device < 0
        or isinstance(inode, bool)
        or not isinstance(inode, int)
        or inode < 0
    ):
        raise ComputeJobError("Compute-job workspace identity is invalid.")
    return int(device), int(inode)


def _validate_compute_job_metadata(
    metadata: dict[str, Any],
    *,
    expected_job_id: str,
) -> dict[str, Any]:
    """Validate identity and routing fields needed for safe admission scans."""
    schema_version = metadata.get("schema_version")
    if isinstance(schema_version, bool) or schema_version != 1:
        raise ComputeJobError("Compute-job metadata schema_version must be 1.")
    if metadata.get("job_id") != expected_job_id:
        raise ComputeJobError("Compute-job metadata does not match its job directory.")
    revision = metadata.get("revision", 0)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ComputeJobError("Compute-job metadata revision is invalid.")
    workspace_value = metadata.get("workspace")
    if not isinstance(workspace_value, str):
        raise ComputeJobError("Compute-job metadata workspace must be an absolute path.")
    workspace = workspace_value.strip()
    if not workspace or not Path(workspace).expanduser().is_absolute():
        raise ComputeJobError("Compute-job metadata workspace must be an absolute path.")
    state = metadata.get("state")
    if state not in ACTIVE_STATES | TERMINAL_STATES:
        raise ComputeJobError("Compute-job metadata state is invalid.")
    containment_cleared_at = metadata.get("containment_cleared_at", "")
    if (
        not isinstance(containment_cleared_at, str)
        or len(containment_cleared_at) > 128
        or state in ACTIVE_STATES
        and containment_cleared_at
    ):
        raise ComputeJobError(
            "Compute-job metadata containment receipt is invalid."
        )
    containment_kind = metadata.get("containment_kind", "")
    child_cgroup = metadata.get("child_cgroup", "")
    if (
        not isinstance(containment_kind, str)
        or containment_kind
        not in {
            "",
            "linux-cgroup-v2",
            "macos-sandbox-no-fork",
            "windows-job-object",
        }
        or not isinstance(child_cgroup, str)
        or len(child_cgroup) > 4_096
    ):
        raise ComputeJobError("Compute-job containment metadata is invalid.")
    compute_job_workspace_identity(metadata)
    if state in ACTIVE_STATES:
        pid = metadata.get("pid")
        process_identity = metadata.get("process_identity")
        updated_at = metadata.get("updated_at")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid < 0:
            raise ComputeJobError("Active compute-job metadata pid is invalid.")
        if not isinstance(process_identity, str) or len(process_identity) > 512:
            raise ComputeJobError(
                "Active compute-job metadata process identity is invalid."
            )
        if state != "starting" and (pid <= 0 or not process_identity):
            raise ComputeJobError(
                "Active compute-job metadata is missing its worker identity."
            )
        if not isinstance(updated_at, str) or not updated_at.strip():
            raise ComputeJobError(
                "Active compute-job metadata updated_at is invalid."
            )
        _active_compute_job_age_seconds(metadata)
        child_pid = metadata.get("child_pid", 0)
        child_identity = metadata.get("child_process_identity", "")
        child_group = metadata.get("child_process_group", 0)
        if (
            isinstance(child_pid, bool)
            or not isinstance(child_pid, int)
            or child_pid < 0
            or not isinstance(child_identity, str)
            or len(child_identity) > 512
            or isinstance(child_group, bool)
            or not isinstance(child_group, int)
            or child_group < 0
        ):
            raise ComputeJobError(
                "Active compute-job child containment metadata is invalid."
            )
        if child_pid > 0 and not child_identity:
            raise ComputeJobError(
                "Active compute-job child identity is incomplete."
            )
    return metadata


def _bounded_directory_entries(
    directory: Path,
    *,
    maximum: int,
    label: str,
) -> tuple[Path, ...]:
    """Read one deterministic, explicitly bounded application-owned directory."""
    entries: list[Path] = []
    try:
        for entry in directory.iterdir():
            if entry.name == ".DS_Store":
                continue
            entries.append(entry)
            if len(entries) > maximum:
                raise ComputeJobError(
                    f"Compute-job {label} exceeds the bounded scan limit of {maximum:,}."
                )
    except ComputeJobError:
        raise
    except OSError as exc:
        raise ComputeJobError(f"Compute-job {label} could not be inspected safely.") from exc
    return tuple(sorted(entries, key=lambda item: item.name))


def _scan_workspace_job_metadata(
    jobs_root: Path,
    *,
    maximum_records: int,
    reconcile_liveness: bool = False,
) -> tuple[dict[str, Any], ...]:
    """Read validated metadata from one workspace bucket without silent skips."""
    records: list[dict[str, Any]] = []
    for job_root in _bounded_directory_entries(
        jobs_root,
        maximum=maximum_records,
        label="record count",
    ):
        if (
            not _JOB_ID_RE.fullmatch(job_root.name)
            or job_root.is_symlink()
            or not job_root.is_dir()
        ):
            raise ComputeJobError("Compute-job runtime contains an invalid job directory.")
        metadata = _read_json_object(
            job_root / "metadata.json",
            maximum_bytes=MAX_METADATA_BYTES,
        )
        metadata = _validate_compute_job_metadata(
            metadata,
            expected_job_id=job_root.name,
        )
        if reconcile_liveness:
            metadata = _reconcile_scanned_compute_job_metadata(
                job_root / "metadata.json",
                metadata,
            )
        records.append(metadata)
    return tuple(records)


def scan_compute_job_metadata(
    runtime_root: Path,
    *,
    maximum_workspace_roots: int = MAX_METADATA_SCAN_WORKSPACES,
    maximum_records: int = MAX_METADATA_SCAN_RECORDS,
    reconcile_liveness: bool = False,
) -> tuple[dict[str, Any], ...]:
    """Scan bounded job records and optionally reconcile stale active workers."""
    if (
        isinstance(maximum_workspace_roots, bool)
        or not isinstance(maximum_workspace_roots, int)
        or maximum_workspace_roots < 1
        or isinstance(maximum_records, bool)
        or not isinstance(maximum_records, int)
        or maximum_records < 1
        or not isinstance(reconcile_liveness, bool)
    ):
        raise ValueError(
            "Compute-job metadata scan limits and reconciliation policy are invalid."
        )
    compute_root = Path(runtime_root).expanduser() / COMPUTE_JOBS_DIRNAME
    try:
        if compute_root.is_symlink():
            raise ComputeJobError("Compute-job runtime root is not a trusted directory.")
        if not compute_root.exists():
            return ()
        if not compute_root.is_dir():
            raise ComputeJobError("Compute-job runtime root is not a trusted directory.")
    except ComputeJobError:
        raise
    except OSError as exc:
        raise ComputeJobError("Compute-job runtime root could not be inspected safely.") from exc

    records: list[dict[str, Any]] = []
    workspace_roots = _bounded_directory_entries(
        compute_root,
        maximum=maximum_workspace_roots,
        label="workspace count",
    )
    for jobs_root in workspace_roots:
        if (
            not _WORKSPACE_KEY_RE.fullmatch(jobs_root.name)
            or jobs_root.is_symlink()
            or not jobs_root.is_dir()
        ):
            raise ComputeJobError("Compute-job runtime contains an invalid workspace directory.")
        remaining = maximum_records - len(records)
        workspace_records = _scan_workspace_job_metadata(
            jobs_root,
            maximum_records=remaining,
            reconcile_liveness=reconcile_liveness,
        )
        records.extend(workspace_records)
    return tuple(records)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _confined_regular_file(workspace: Path, raw_path: str, *, suffix: str) -> Path:
    candidate = Path(str(raw_path or "").strip())
    if not candidate.as_posix() or candidate.is_absolute() or ".." in candidate.parts:
        raise ComputeJobError("Compute-job paths must be relative and remain inside the workspace.")
    current = workspace
    for part in candidate.parts:
        current /= part
        if current.is_symlink():
            raise ComputeJobError("Compute-job paths cannot traverse symbolic links.")
    try:
        resolved = (workspace / candidate).resolve(strict=True)
    except OSError as exc:
        raise ComputeJobError("Compute-job path does not name an existing approved file.") from exc
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise ComputeJobError("Compute-job paths must remain inside the workspace.") from exc
    metadata = resolved.stat()
    if not stat.S_ISREG(metadata.st_mode) or resolved.suffix.casefold() != suffix:
        raise ComputeJobError(f"Compute-job path must be a regular {suffix} file.")
    return resolved


def _process_identity(pid: int) -> str:
    """Return a stable-enough birth identity used to reject PID reuse."""
    if pid <= 0:
        return ""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.DWORD),
            ]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            ]
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return ""
            try:
                exit_code = wintypes.DWORD()
                if (
                    not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                    or exit_code.value != 259
                ):
                    return ""
                creation = wintypes.FILETIME()
                exit_time = wintypes.FILETIME()
                kernel_time = wintypes.FILETIME()
                user_time = wintypes.FILETIME()
                if not kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(creation),
                    ctypes.byref(exit_time),
                    ctypes.byref(kernel_time),
                    ctypes.byref(user_time),
                ):
                    return ""
                creation_ticks = (
                    int(creation.dwHighDateTime) << 32
                ) | int(creation.dwLowDateTime)
                return f"win:{pid}:{creation_ticks}"
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError, TypeError, ValueError):
            return ""
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        fields = proc_stat.read_text(encoding="utf-8").split()
        if len(fields) > 21:
            return f"proc:{fields[21]}"
    except (OSError, UnicodeError):
        pass
    return _ps_process_identity(pid)


def _ps_process_identity(pid: int) -> str:
    """Read one locale- and timezone-stable POSIX process birth identity."""
    probe_environment = dict(os.environ)
    probe_environment["LC_ALL"] = "C"
    probe_environment["TZ"] = "UTC"
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
            check=False,
            env=probe_environment,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        return ""
    return f"ps:{pid}:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _identity_matches(pid: Any, expected: Any) -> bool:
    try:
        normalized_pid = int(pid)
    except (TypeError, ValueError):
        return False
    expected_text = str(expected or "")
    return bool(expected_text) and _process_identity(normalized_pid) == expected_text


def _active_compute_job_age_seconds(metadata: dict[str, Any]) -> float:
    """Return the age of a validated active record or reject ambiguous time data."""
    updated_at = str(metadata.get("updated_at") or "").strip()
    try:
        timestamp = datetime.fromisoformat(updated_at)
        if timestamp.tzinfo is None:
            raise ValueError
        age_seconds = (datetime.now(timezone.utc) - timestamp).total_seconds()
    except (TypeError, ValueError) as exc:
        raise ComputeJobError(
            "Active compute-job metadata updated_at is invalid."
        ) from exc
    if age_seconds < -300.0:
        raise ComputeJobError(
            "Active compute-job metadata updated_at is implausibly in the future."
        )
    return age_seconds


def _release_sleep_assertion(metadata: dict[str, Any]) -> None:
    """Release only the sleep assertion whose persisted birth identity still matches."""
    pid = metadata.get("sleep_assertion_pid")
    identity = metadata.get("sleep_assertion_identity")
    if not _identity_matches(pid, identity):
        return
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (OSError, TypeError, ValueError):
        return


def _posix_process_group_exists(group_id: int) -> bool:
    """Return whether a POSIX process group still has at least one member."""
    if os.name != "posix" or group_id <= 0:
        return False
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _terminate_posix_process_group(
    group_id: int,
    *,
    timeout: float,
    reap_process: Callable[[], Any] | None = None,
) -> bool:
    """Terminate a job-owned process group and prove that it became empty."""
    if reap_process is not None:
        reap_process()
    if not _posix_process_group_exists(group_id):
        return True
    try:
        os.killpg(group_id, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + max(0.05, timeout)
    while time.monotonic() < deadline:
        if reap_process is not None:
            reap_process()
        if not _posix_process_group_exists(group_id):
            return True
        time.sleep(0.05)
    try:
        os.killpg(group_id, signal.SIGKILL)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + max(0.05, timeout)
    while time.monotonic() < deadline:
        if reap_process is not None:
            reap_process()
        if not _posix_process_group_exists(group_id):
            return True
        time.sleep(0.05)
    return not _posix_process_group_exists(group_id)


def _bounded_posix_process_listing(*, timeout: float) -> bytes:
    """Read a bounded process-and-environment snapshot without inheriting job markers."""
    if os.name != "posix":
        raise ComputeJobError("POSIX process inspection is unavailable on this host.")
    executable = next(
        (
            candidate
            for candidate in (Path("/bin/ps"), Path("/usr/bin/ps"))
            if candidate.is_file() and os.access(candidate, os.X_OK)
        ),
        None,
    )
    if executable is None:
        raise ComputeJobError(
            "POSIX job-marker inspection is unavailable; writer admission remains blocked."
        )
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"LANG", "LC_ALL", "LC_CTYPE", "PATH", "TMPDIR"}
        and key != "AGENTIC_CONTEXT_COMPUTE_JOB"
    }
    environment["LANG"] = "C"
    environment["LC_ALL"] = "C"
    environment["PATH"] = "/usr/bin:/bin"
    process: subprocess.Popen[bytes] | None = None
    output = bytearray()
    deadline = time.monotonic() + max(0.05, min(timeout, POSIX_JOB_MARKER_SCAN_SECONDS))
    try:
        process = subprocess.Popen(
            [str(executable), "eww", "-axo", "pid=,lstart=,command="],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        if process.stdout is None:
            raise ComputeJobError(
                "POSIX job-marker inspection output is unavailable; writer admission remains "
                "blocked."
            )
        descriptor = process.stdout.fileno()
        os.set_blocking(descriptor, False)
        import select

        while True:
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                raise ComputeJobError(
                    "POSIX job-marker inspection timed out; writer admission remains blocked."
                )
            readable, _, _ = select.select(
                [descriptor],
                [],
                [],
                min(0.1, remaining_time),
            )
            if not readable:
                continue
            try:
                chunk = os.read(
                    descriptor,
                    min(
                        64 * 1024,
                        MAX_POSIX_JOB_MARKER_SCAN_BYTES + 1 - len(output),
                    ),
                )
            except BlockingIOError:
                continue
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > MAX_POSIX_JOB_MARKER_SCAN_BYTES:
                raise ComputeJobError(
                    "POSIX job-marker inspection exceeded its byte limit; writer admission "
                    "remains blocked."
                )
        remaining_time = deadline - time.monotonic()
        if remaining_time <= 0:
            raise ComputeJobError(
                "POSIX job-marker inspection timed out; writer admission remains blocked."
            )
        try:
            return_code = process.wait(timeout=remaining_time)
        except subprocess.TimeoutExpired as exc:
            raise ComputeJobError(
                "POSIX job-marker inspection timed out; writer admission remains blocked."
            ) from exc
        if return_code != 0:
            raise ComputeJobError(
                "POSIX job-marker inspection failed; writer admission remains blocked."
            )
        return bytes(output)
    except OSError as exc:
        raise ComputeJobError(
            "POSIX job-marker inspection failed; writer admission remains blocked."
        ) from exc
    finally:
        if process is not None:
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=0.5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if process.stdout is not None:
                process.stdout.close()


def _scan_linux_job_marker_processes(
    job_id: str,
    *,
    exclude_pids: frozenset[int],
    timeout: float,
) -> dict[int, str]:
    """Find same-user Linux processes carrying one exact inherited job marker."""
    marker = f"AGENTIC_CONTEXT_COMPUTE_JOB={job_id}".encode("ascii")
    deadline = time.monotonic() + max(0.05, min(timeout, POSIX_JOB_MARKER_SCAN_SECONDS))
    records: dict[int, str] = {}
    process_count = 0
    environment_bytes = 0
    try:
        iterator = os.scandir("/proc")
    except OSError as exc:
        raise ComputeJobError(
            "Linux job-marker inspection is unavailable; writer admission remains blocked."
        ) from exc
    with iterator:
        for entry in iterator:
            if not entry.name.isdecimal():
                continue
            process_count += 1
            if process_count > MAX_POSIX_JOB_MARKER_SCAN_PROCESSES:
                raise ComputeJobError(
                    "Linux job-marker inspection exceeded its process limit; writer admission "
                    "remains blocked."
                )
            if time.monotonic() >= deadline:
                raise ComputeJobError(
                    "Linux job-marker inspection timed out; writer admission remains blocked."
                )
            pid = int(entry.name)
            if pid in exclude_pids:
                continue
            try:
                process_metadata = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ComputeJobError(
                    "Linux job-marker ownership could not be inspected; writer admission remains "
                    "blocked."
                ) from exc
            if process_metadata.st_uid != os.geteuid():
                continue
            before_identity = _process_identity(pid)
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            try:
                descriptor = os.open(f"/proc/{pid}/environ", flags)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ComputeJobError(
                    "Linux job-marker environment could not be inspected; writer admission "
                    "remains blocked."
                ) from exc
            try:
                environment = bytearray()
                while True:
                    chunk = os.read(
                        descriptor,
                        min(
                            64 * 1024,
                            MAX_POSIX_PROCESS_ENVIRONMENT_BYTES + 1 - len(environment),
                        ),
                    )
                    if not chunk:
                        break
                    environment.extend(chunk)
                    environment_bytes += len(chunk)
                    if len(environment) > MAX_POSIX_PROCESS_ENVIRONMENT_BYTES:
                        raise ComputeJobError(
                            "Linux process environment exceeded its inspection limit; writer "
                            "admission remains blocked."
                        )
                    if environment_bytes > MAX_POSIX_JOB_MARKER_ENVIRONMENT_BYTES:
                        raise ComputeJobError(
                            "Linux job-marker inspection exceeded its byte limit; writer "
                            "admission remains blocked."
                        )
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ComputeJobError(
                    "Linux job-marker environment could not be inspected; writer admission "
                    "remains blocked."
                ) from exc
            finally:
                os.close(descriptor)
            if marker not in bytes(environment).split(b"\0"):
                continue
            after_identity = _process_identity(pid)
            if not before_identity or before_identity != after_identity:
                raise ComputeJobError(
                    "A job-marker process changed identity during inspection; writer admission "
                    "remains blocked."
                )
            records[pid] = before_identity
    return records


def _scan_ps_job_marker_processes(
    job_id: str,
    *,
    exclude_pids: frozenset[int],
    timeout: float,
) -> dict[int, str]:
    """Find marker-bearing processes in one bounded BSD-style process snapshot."""
    marker = f"AGENTIC_CONTEXT_COMPUTE_JOB={job_id}".encode("ascii")
    output = _bounded_posix_process_listing(timeout=timeout)
    records: dict[int, str] = {}
    process_count = 0
    for line in output.splitlines():
        process_count += 1
        if process_count > MAX_POSIX_JOB_MARKER_SCAN_PROCESSES:
            raise ComputeJobError(
                "POSIX job-marker inspection exceeded its process limit; writer admission "
                "remains blocked."
            )
        match = re.fullmatch(rb"\s*(\d+)\s+(.{24})\s+(.*)", line)
        if match is None:
            raise ComputeJobError(
                "POSIX job-marker inspection returned ambiguous output; writer admission remains "
                "blocked."
            )
        pid = int(match.group(1))
        if pid in exclude_pids:
            continue
        fields = re.split(rb"[\t ]+", match.group(3).strip())
        if marker not in fields:
            continue
        if pid in records:
            raise ComputeJobError(
                "POSIX job-marker inspection returned a duplicate PID; writer admission remains "
                "blocked."
            )
        lstart = match.group(2)
        identity = f"ps:{pid}:" + hashlib.sha256(lstart).hexdigest()
        records[pid] = identity
    return records


def _scan_posix_job_marker_processes(
    job_id: str,
    *,
    exclude_pids: frozenset[int] = frozenset(),
    timeout: float = POSIX_JOB_MARKER_SCAN_SECONDS,
) -> dict[int, str]:
    """Return identity-bound processes carrying the exact cooperative job marker."""
    if os.name != "posix" or not _JOB_ID_RE.fullmatch(job_id):
        raise ComputeJobError("POSIX job-marker inspection request is invalid.")
    if sys.platform.startswith("linux") and Path("/proc").is_dir():
        return _scan_linux_job_marker_processes(
            job_id,
            exclude_pids=exclude_pids,
            timeout=timeout,
        )
    return _scan_ps_job_marker_processes(
        job_id,
        exclude_pids=exclude_pids,
        timeout=timeout,
    )


def _terminate_posix_job_marker_processes(
    job_id: str,
    *,
    exclude_pids: frozenset[int] = frozenset(),
    timeout: float,
) -> tuple[bool, bool]:
    """Terminate identity-verified marker processes and prove two empty scans."""
    if os.name != "posix":
        return True, False
    started = time.monotonic()
    deadline = started + max(0.1, timeout)
    force_deadline = started + max(0.05, timeout / 2)
    descendants_detected = False
    empty_scans = 0
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        matches = _scan_posix_job_marker_processes(
            job_id,
            exclude_pids=exclude_pids,
            timeout=max(0.05, remaining),
        )
        if not matches:
            empty_scans += 1
            if empty_scans >= 2:
                return True, descendants_detected
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            continue
        descendants_detected = True
        empty_scans = 0
        requested_signal = (
            signal.SIGKILL if time.monotonic() >= force_deadline else signal.SIGTERM
        )
        for pid, expected_identity in matches.items():
            if not _identity_matches(pid, expected_identity):
                continue
            try:
                os.kill(pid, requested_signal)
            except ProcessLookupError:
                continue
            except (OSError, PermissionError) as exc:
                raise ComputeJobError(
                    "A job-marker process could not be terminated safely; writer admission "
                    "remains blocked."
                ) from exc
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    matches = _scan_posix_job_marker_processes(
        job_id,
        exclude_pids=exclude_pids,
        timeout=0.1,
    )
    if not matches:
        empty_scans += 1
    return empty_scans >= 2, descendants_detected


def _read_linux_cgroup_file(path: Path) -> bytes:
    """Read one cgroup control file without links or unbounded allocation."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ComputeJobError("Linux cgroup state could not be inspected safely.") from exc
    try:
        payload = bytearray()
        while True:
            chunk = os.read(
                descriptor,
                min(64 * 1024, MAX_LINUX_CGROUP_FILE_BYTES + 1 - len(payload)),
            )
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > MAX_LINUX_CGROUP_FILE_BYTES:
                raise ComputeJobError("Linux cgroup state exceeded its bounded size.")
        return bytes(payload)
    except OSError as exc:
        raise ComputeJobError("Linux cgroup state could not be inspected safely.") from exc
    finally:
        os.close(descriptor)


def _write_linux_cgroup_file(path: Path, payload: bytes) -> None:
    """Write one complete command to a non-linked cgroup control file."""
    flags = os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            written = os.write(descriptor, payload)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ComputeJobError("Linux cgroup control could not be updated safely.") from exc
    if written != len(payload):
        raise ComputeJobError("Linux cgroup control write was incomplete.")


class _LinuxCgroup:
    """Own one verified cgroup v2 used as the Linux process-tree receipt."""

    def __init__(self, path: Path, job_id: str) -> None:
        self.path = path
        self.job_id = job_id

    @classmethod
    def create(cls, job_id: str) -> _LinuxCgroup:
        if not sys.platform.startswith("linux") or not _JOB_ID_RE.fullmatch(job_id):
            raise ComputeJobError("Linux cgroup containment request is invalid.")
        try:
            filesystem = LINUX_CGROUP_FILESYSTEM.resolve(strict=True)
            if not (filesystem / "cgroup.controllers").is_file():
                raise ComputeJobError("The host does not expose cgroup v2 containment.")
            membership = _read_linux_cgroup_file(Path("/proc/self/cgroup"))
        except OSError as exc:
            raise ComputeJobError("Linux cgroup v2 containment is unavailable.") from exc
        entries = []
        try:
            for line in membership.decode("ascii").splitlines():
                hierarchy, controllers, relative = line.split(":", 2)
                if hierarchy == "0" and not controllers and relative.startswith("/"):
                    entries.append(relative)
        except (UnicodeError, ValueError) as exc:
            raise ComputeJobError("Linux cgroup v2 membership is invalid.") from exc
        if len(entries) != 1:
            raise ComputeJobError("Linux cgroup v2 membership is ambiguous.")
        relative = Path(entries[0].lstrip("/"))
        if ".." in relative.parts:
            raise ComputeJobError("Linux cgroup v2 membership escaped its filesystem.")
        try:
            parent = (filesystem / relative).resolve(strict=True)
            parent.relative_to(filesystem)
        except (OSError, ValueError) as exc:
            raise ComputeJobError("Linux cgroup v2 membership is unavailable.") from exc
        name = f"agentic-context-{job_id}-{secrets.token_hex(8)}"
        path = parent / name
        try:
            path.mkdir(mode=0o700)
        except OSError as exc:
            raise ComputeJobError(
                "Linux cgroup v2 is not delegated to the compute worker."
            ) from exc
        cgroup = cls(path, job_id)
        try:
            cgroup._validate_controls()
        except ComputeJobError:
            try:
                path.rmdir()
            except OSError:
                pass
            raise
        return cgroup

    @classmethod
    def from_metadata(cls, job_id: str, raw_path: str) -> _LinuxCgroup | None:
        if not _JOB_ID_RE.fullmatch(job_id) or not raw_path:
            return None
        path = Path(raw_path)
        expected_name = re.fullmatch(
            rf"agentic-context-{re.escape(job_id)}-[0-9a-f]{{16}}",
            path.name,
        )
        if not path.is_absolute() or expected_name is None:
            raise ComputeJobError("Persisted Linux cgroup containment path is invalid.")
        try:
            filesystem = LINUX_CGROUP_FILESYSTEM.resolve(strict=True)
            parent = path.parent.resolve(strict=True)
            parent.relative_to(filesystem)
        except (OSError, ValueError) as exc:
            raise ComputeJobError("Persisted Linux cgroup containment path is invalid.") from exc
        if not path.exists():
            return None
        cgroup = cls(path, job_id)
        cgroup._validate_controls()
        return cgroup

    def _validate_controls(self) -> None:
        try:
            if self.path.is_symlink() or not self.path.is_dir():
                raise ComputeJobError("Linux cgroup containment directory is invalid.")
            controls = ("cgroup.events", "cgroup.kill", "cgroup.procs")
            if any(not (self.path / name).is_file() for name in controls):
                raise ComputeJobError("Linux cgroup v2 lacks required containment controls.")
            if any(not os.access(self.path / name, os.R_OK) for name in ("cgroup.events", "cgroup.procs")):
                raise ComputeJobError("Linux cgroup v2 state is not readable.")
            if any(not os.access(self.path / name, os.W_OK) for name in ("cgroup.kill", "cgroup.procs")):
                raise ComputeJobError("Linux cgroup v2 controls are not writable.")
        except OSError as exc:
            raise ComputeJobError("Linux cgroup controls could not be verified.") from exc

    def pids(self) -> tuple[int, ...]:
        if not self.path.exists():
            return ()
        payload = _read_linux_cgroup_file(self.path / "cgroup.procs")
        pids: list[int] = []
        try:
            for line in payload.decode("ascii").splitlines():
                if not line.isdecimal():
                    raise ComputeJobError("Linux cgroup process membership is invalid.")
                pids.append(int(line))
                if len(pids) > MAX_LINUX_CGROUP_PROCESSES:
                    raise ComputeJobError("Linux cgroup process membership exceeded its limit.")
        except UnicodeError as exc:
            raise ComputeJobError("Linux cgroup process membership is invalid.") from exc
        if len(set(pids)) != len(pids):
            raise ComputeJobError("Linux cgroup process membership contains duplicate PIDs.")
        return tuple(pids)

    def populated(self) -> bool:
        if not self.path.exists():
            return False
        payload = _read_linux_cgroup_file(self.path / "cgroup.events")
        fields: dict[str, str] = {}
        try:
            for line in payload.decode("ascii").splitlines():
                name, value = line.split(maxsplit=1)
                fields[name] = value
        except (UnicodeError, ValueError) as exc:
            raise ComputeJobError("Linux cgroup event state is invalid.") from exc
        if fields.get("populated") not in {"0", "1"}:
            raise ComputeJobError("Linux cgroup populated state is unavailable.")
        return fields["populated"] == "1"

    def assign(self, pid: int, expected_identity: str) -> None:
        if not _identity_matches(pid, expected_identity):
            raise ComputeJobError("Linux cgroup child identity changed before assignment.")
        _write_linux_cgroup_file(self.path / "cgroup.procs", str(pid).encode("ascii"))
        if (
            not _identity_matches(pid, expected_identity)
            or pid not in self.pids()
            or not self.populated()
        ):
            raise ComputeJobError("Linux cgroup child assignment could not be verified.")

    def terminate_and_remove(self, *, timeout: float) -> tuple[bool, bool]:
        detected = self.populated()
        deadline = time.monotonic() + max(0.1, timeout)
        empty_scans = 0
        while time.monotonic() < deadline:
            if self.populated():
                detected = True
                empty_scans = 0
                _write_linux_cgroup_file(self.path / "cgroup.kill", b"1")
            else:
                empty_scans += 1
                if empty_scans >= 2:
                    try:
                        self.path.rmdir()
                    except FileNotFoundError:
                        return True, detected
                    except OSError:
                        return False, detected
                    return True, detected
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        return False, detected


class _WindowsJob:
    """Own one Windows child tree with kill-on-close containment."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows Job Objects are unavailable on this host.")
        import ctypes
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class BasicAccountingInformation(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong),
                ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        limits = ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(
            handle,
            9,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise OSError(error, "SetInformationJobObject failed")
        self._ctypes = ctypes
        self._wintypes = wintypes
        self._kernel32 = kernel32
        self._accounting_type = BasicAccountingInformation
        self._handle = handle

    def assign(self, process: subprocess.Popen[Any]) -> None:
        process_handle = self._wintypes.HANDLE(int(process._handle))
        if not self._kernel32.AssignProcessToJobObject(self._handle, process_handle):
            raise OSError(
                self._ctypes.get_last_error(),
                "AssignProcessToJobObject failed",
            )

    def active_processes(self) -> int:
        accounting = self._accounting_type()
        if not self._kernel32.QueryInformationJobObject(
            self._handle,
            1,
            self._ctypes.byref(accounting),
            self._ctypes.sizeof(accounting),
            None,
        ):
            raise OSError(
                self._ctypes.get_last_error(),
                "QueryInformationJobObject failed",
            )
        return int(accounting.ActiveProcesses)

    def terminate(self, exit_code: int = 1) -> None:
        if not self._kernel32.TerminateJobObject(self._handle, exit_code):
            raise OSError(
                self._ctypes.get_last_error(),
                "TerminateJobObject failed",
            )

    def wait_empty(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.05, timeout)
        while time.monotonic() < deadline:
            if self.active_processes() == 0:
                return True
            time.sleep(0.05)
        return self.active_processes() == 0

    def close(self) -> None:
        handle = self._handle
        if handle:
            self._handle = None
            self._kernel32.CloseHandle(handle)


def _read_process_output(stream: Any, output_queue: Queue[bytes | None]) -> None:
    """Move pipe bytes into a bounded queue on every supported host."""
    try:
        while True:
            try:
                chunk = os.read(stream.fileno(), 64 * 1024)
            except OSError:
                break
            if not chunk:
                break
            output_queue.put(chunk)
    finally:
        output_queue.put(None)


def _wait_for_process_identity(pid: int, *, attempts: int = 50) -> str:
    """Require two matching birth-identity observations for one new process."""
    identity = ""
    previous_identity = ""
    for _ in range(attempts):
        current_identity = _process_identity(pid)
        if current_identity and current_identity == previous_identity:
            identity = current_identity
            break
        previous_identity = current_identity
        time.sleep(0.02)
    return identity


def _reconcile_scanned_compute_job_metadata(
    metadata_path: Path,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Reconcile one active record before it participates in writer admission."""
    if metadata.get("state") not in ACTIVE_STATES:
        return metadata

    interrupted = False

    def reconcile(current: dict[str, Any]) -> dict[str, Any] | None:
        nonlocal interrupted
        if current.get("state") not in ACTIVE_STATES:
            return None
        pid = int(current.get("pid") or 0)
        expected_identity = str(current.get("process_identity") or "")
        if pid <= 0 or not expected_identity:
            if _active_compute_job_age_seconds(current) < 2.0:
                return None
            raise ComputeJobError(
                "Compute-job launch ownership is unresolved; writer admission remains blocked."
            )
        observed_identity = _process_identity(pid)
        if observed_identity == expected_identity:
            if current.get("state") != "starting":
                return None
            current["state"] = "running"
            current["updated_at"] = _utc_now()
            return current
        if _active_compute_job_age_seconds(current) < 2.0:
            return None
        child_pid = int(current.get("child_pid") or 0)
        child_identity = str(current.get("child_process_identity") or "")
        child_group = int(current.get("child_process_group") or 0)
        if child_pid > 0 and _identity_matches(child_pid, child_identity):
            raise ComputeJobError(
                "Compute-job wrapper ended while its approved child is still active; writer "
                "admission remains blocked."
            )
        if (
            os.name == "posix"
            and child_group > 0
            and _posix_process_group_exists(child_group)
        ):
            raise ComputeJobError(
                "Compute-job wrapper ended while its child process group is still active; "
                "writer admission remains blocked."
            )
        if os.name == "posix":
            containment_kind = str(current.get("containment_kind") or "")
            if sys.platform == "darwin":
                if containment_kind != "macos-sandbox-no-fork":
                    raise ComputeJobError(
                        "Compute-job fork containment provenance is unavailable; writer "
                        "admission remains blocked."
                    )
            elif sys.platform.startswith("linux"):
                if containment_kind != "linux-cgroup-v2":
                    raise ComputeJobError(
                        "Compute-job cgroup containment provenance is unavailable; writer "
                        "admission remains blocked."
                    )
                raw_cgroup = str(current.get("child_cgroup") or "")
                cgroup = _LinuxCgroup.from_metadata(
                    str(current.get("job_id") or ""),
                    raw_cgroup,
                )
                if cgroup is not None:
                    if cgroup.populated():
                        raise ComputeJobError(
                            "Compute-job wrapper ended while its Linux cgroup is still populated; "
                            "writer admission remains blocked."
                        )
                    cgroup_cleared, _cgroup_detected = cgroup.terminate_and_remove(
                        timeout=0.2,
                    )
                    if not cgroup_cleared:
                        raise ComputeJobError(
                            "Compute-job Linux cgroup cleanup is incomplete; writer admission "
                            "remains blocked."
                        )
                elif child_pid > 0 and not raw_cgroup:
                    raise ComputeJobError(
                        "Compute-job Linux cgroup state is ambiguous; writer admission remains "
                        "blocked."
                    )
            else:
                raise ComputeJobError(
                    "This POSIX host has no durable process-tree containment receipt; writer "
                    "admission remains blocked."
                )
            marker_processes = _scan_posix_job_marker_processes(
                str(current.get("job_id") or ""),
            )
            if marker_processes:
                raise ComputeJobError(
                    "Compute-job wrapper ended while job-marker descendants are still active; "
                    "writer admission remains blocked."
                )
        if (
            os.name == "nt"
            and current.get("containment_kind") != "windows-job-object"
        ):
            raise ComputeJobError(
                "Compute-job Windows Job Object provenance is unavailable; writer admission "
                "remains blocked."
            )
        if os.name == "nt" and child_pid > 0:
            if str(current.get("containment_cleared_at") or ""):
                raise ComputeJobError(
                    "Active compute-job metadata has an invalid containment receipt."
                )
            raise ComputeJobError(
                "Compute-job wrapper ended before its Windows Job Object published a "
                "containment receipt; writer admission remains blocked."
            )
        definitely_stale = bool(
            observed_identity and observed_identity != expected_identity
        )
        if not definitely_stale and os.name == "posix":
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                definitely_stale = True
            except PermissionError:
                definitely_stale = False
            except OSError as exc:
                raise ComputeJobError(
                    "Active compute-job worker liveness could not be verified safely."
                ) from exc
        elif not definitely_stale and os.name == "nt":
            definitely_stale = not bool(observed_identity)
        if not definitely_stale:
            raise ComputeJobError(
                "Active compute-job worker liveness could not be verified safely."
            )
        current["state"] = "interrupted"
        current["updated_at"] = _utc_now()
        current["ended_at"] = current.get("ended_at") or current["updated_at"]
        current["message"] = (
            "Worker identity is no longer present; explicit resume is available when a valid "
            "checkpoint exists."
        )
        interrupted = True
        return current

    updated = _update_metadata_file(metadata_path, reconcile)
    if interrupted:
        _release_sleep_assertion(updated)
    return updated


def _terminate_process_group(
    pid: int,
    *,
    timeout: float = 5.0,
    expected_identity: str = "",
) -> bool:
    """Terminate one wrapper after checking its persisted birth identity."""
    if not expected_identity:
        return False
    if expected_identity and not _identity_matches(pid, expected_identity):
        return True
    if os.name == "posix":
        for requested_signal in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, requested_signal)
            except ProcessLookupError:
                return True
            except PermissionError:
                return bool(expected_identity) and not _identity_matches(
                    pid,
                    expected_identity,
                )
            deadline = time.monotonic() + max(0.05, timeout)
            while time.monotonic() < deadline:
                if expected_identity:
                    if not _identity_matches(pid, expected_identity):
                        return True
                else:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        return True
                    except PermissionError:
                        return False
                time.sleep(0.05)
        return False
    try:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0 and _identity_matches(pid, expected_identity):
        return False
    deadline = time.monotonic() + max(0.05, timeout)
    while time.monotonic() < deadline:
        if not _identity_matches(pid, expected_identity):
            return True
        time.sleep(0.05)
    return not _identity_matches(pid, expected_identity)


def validate_optimizer_checkpoint(payload: Any) -> dict[str, Any]:
    """Validate the portable minimum checkpoint contract and return a copy."""
    if not isinstance(payload, dict):
        raise ComputeJobError("Optimizer checkpoint must be one JSON object.")
    required = {
        "schema_version",
        "optimizer_version",
        "iteration",
        "rng_state",
        "seed",
        "best_objective",
        "best_parameters",
        "evaluation_count",
    }
    missing = sorted(required.difference(payload))
    if missing:
        raise ComputeJobError("Optimizer checkpoint is missing: " + ", ".join(missing))
    if payload.get("schema_version") != 1:
        raise ComputeJobError("Optimizer checkpoint schema_version must be 1.")
    for field_name in ("iteration", "evaluation_count"):
        value = payload.get(field_name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ComputeJobError(f"Optimizer checkpoint {field_name} must be a non-negative integer.")
    if not isinstance(payload.get("best_parameters"), dict):
        raise ComputeJobError("Optimizer checkpoint best_parameters must be an object.")
    if "population" not in payload and "optimizer_state" not in payload:
        raise ComputeJobError("Optimizer checkpoint must include population or optimizer_state.")
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ComputeJobError("Optimizer checkpoint exceeds the bounded checkpoint size.")
    return dict(payload)


def write_optimizer_checkpoint_atomic(job_runtime: Path, payload: dict[str, Any]) -> Path:
    """Validate and atomically publish the latest complete optimizer checkpoint."""
    runtime = Path(job_runtime).resolve(strict=True)
    validated = validate_optimizer_checkpoint(payload)
    checkpoint_path = runtime / "checkpoint.json"
    _atomic_write_json(checkpoint_path, validated)
    return checkpoint_path


def write_compute_progress_atomic(job_runtime: Path, payload: dict[str, Any]) -> Path:
    """Publish one bounded progress heartbeat for job_status."""
    if not isinstance(payload, dict):
        raise ComputeJobError("Compute progress must be one JSON object.")
    unknown = sorted(set(payload).difference(PROGRESS_FIELDS))
    if unknown:
        raise ComputeJobError("Compute progress contains unsupported fields: " + ", ".join(unknown))
    normalized = dict(payload)
    if "summary" in normalized:
        normalized["summary"] = str(normalized["summary"])[:MAX_STATUS_TEXT_CHARS]
    encoded = json.dumps(normalized, ensure_ascii=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_PROGRESS_BYTES:
        raise ComputeJobError("Compute progress exceeds the bounded progress size.")
    runtime = Path(job_runtime).resolve(strict=True)
    progress_path = runtime / "progress.json"
    _atomic_write_json(progress_path, normalized)
    return progress_path


class ComputeJobManager:
    """Create, reconcile, inspect, and stop durable local compute workers."""

    def __init__(
        self,
        workspace: Path,
        runtime_root: Path,
        *,
        caffeinate_executable: Path = Path("/usr/bin/caffeinate"),
    ) -> None:
        self.workspace = Path(workspace).resolve(strict=True)
        workspace_metadata = self.workspace.stat()
        if not stat.S_ISDIR(workspace_metadata.st_mode):
            raise ComputeJobError("Compute-job workspace must be a directory.")
        self.workspace_device = int(workspace_metadata.st_dev)
        self.workspace_inode = int(workspace_metadata.st_ino)
        self.runtime_root = Path(runtime_root).expanduser().resolve(strict=False) / COMPUTE_JOBS_DIRNAME
        self.workspace_key = hashlib.sha256(str(self.workspace).encode("utf-8")).hexdigest()[:24]
        self.jobs_root = self.runtime_root / self.workspace_key
        self.staging_root = self.runtime_root.parent / "compute-job-staging" / self.workspace_key
        self.workspace_lock_path = (
            self.runtime_root.parent
            / COMPUTE_JOB_LOCKS_DIRNAME
            / f"{self.workspace_key}.lock"
        )
        self._caffeinate_executable = caffeinate_executable
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self.reconcile()

    def _metadata_path(self, job_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", str(job_id or "")):
            raise ComputeJobError("Unknown compute job_id.")
        return self.jobs_root / job_id / "metadata.json"

    def _load_metadata(self, job_id: str) -> dict[str, Any]:
        metadata = _read_json_object(
            self._metadata_path(job_id),
            maximum_bytes=MAX_METADATA_BYTES,
        )
        return _validate_compute_job_metadata(
            metadata,
            expected_job_id=job_id,
        )

    def _save_metadata(self, metadata: dict[str, Any]) -> None:
        job_id = str(metadata.get("job_id") or "")
        _atomic_write_json(self._metadata_path(job_id), metadata)

    def _transition_metadata(
        self,
        job_id: str,
        updater: Callable[[dict[str, Any]], dict[str, Any] | None],
    ) -> dict[str, Any]:
        """Apply one serialized state transition to an already published job."""
        return _update_metadata_file(self._metadata_path(job_id), updater)

    def _all_metadata(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        expected_identity = (self.workspace_device, self.workspace_inode)
        for record in _scan_workspace_job_metadata(
            self.jobs_root,
            maximum_records=MAX_METADATA_SCAN_RECORDS,
        ):
            identity = compute_job_workspace_identity(record)
            if identity is not None:
                if identity == expected_identity:
                    records.append(record)
                continue
            if record.get("workspace") == str(self.workspace):
                records.append(record)
                continue
            raise ComputeJobError(
                "Legacy compute-job metadata does not match its workspace bucket."
            )
        return records

    def _approved_entrypoint(
        self,
        entrypoint_id: str,
    ) -> tuple[Path, dict[str, Any], bytes, str]:
        if not _ENTRYPOINT_ID_RE.fullmatch(entrypoint_id):
            raise ComputeJobError("Compute entrypoint id is invalid.")
        approval_paths = [
            self.workspace / APPROVAL_FILENAME,
            self.workspace / LEGACY_APPROVAL_FILENAME,
        ]
        approval_path = next(
            (candidate for candidate in approval_paths if candidate.is_file()),
            approval_paths[0],
        )
        approval = _read_json_object(approval_path, maximum_bytes=128 * 1024)
        if approval.get("schema_version") != 1 or not isinstance(approval.get("entrypoints"), list):
            raise ComputeJobError("Compute approval manifest must use schema_version 1.")
        matches = [
            item
            for item in approval["entrypoints"]
            if isinstance(item, dict) and item.get("id") == entrypoint_id
        ]
        if len(matches) != 1:
            raise ComputeJobError("Compute entrypoint is not uniquely approved by the workspace manifest.")
        record = dict(matches[0])
        entrypoint = _confined_regular_file(self.workspace, str(record.get("path") or ""), suffix=".py")
        expected_sha256 = str(record.get("sha256") or "").casefold()
        entrypoint_bytes, entrypoint_sha256 = _stable_regular_file_bytes(
            entrypoint,
            maximum_bytes=MAX_ENTRYPOINT_BYTES,
            label="Compute entrypoint",
        )
        if (
            not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
            or entrypoint_sha256 != expected_sha256
        ):
            raise ComputeJobError("Compute entrypoint bytes do not match the approved SHA-256.")
        return entrypoint, record, entrypoint_bytes, entrypoint_sha256

    def _reconcile_unlocked(self) -> None:
        """Reconcile this bucket while the caller owns its admission lock."""
        for metadata in self._all_metadata():
            if metadata.get("state") in ACTIVE_STATES:
                metadata = _reconcile_scanned_compute_job_metadata(
                    self._metadata_path(str(metadata.get("job_id") or "")),
                    metadata,
                )
            if metadata.get("state") in ACTIVE_STATES:
                continue
            self._release_assertion(metadata)

    def reconcile(self) -> None:
        """Rebind live jobs under the workspace bucket admission lock."""
        with _persistent_workspace_lock(self.workspace_lock_path):
            self._reconcile_unlocked()

    def _release_assertion(self, metadata: dict[str, Any]) -> None:
        _release_sleep_assertion(metadata)

    def _reserve_starting_job(
        self,
        *,
        metadata: dict[str, Any],
        entrypoint_bytes: bytes,
        config_bytes: bytes,
        checkpoint_bytes: bytes | None,
    ) -> str:
        """Deduplicate, admit, and durably publish one starting job under one lock."""
        job_id = str(metadata["job_id"])
        idempotency_key = str(metadata["idempotency_key"])
        request_fingerprint = str(metadata["request_fingerprint"])
        with _persistent_workspace_lock(self.workspace_lock_path):
            self._reconcile_unlocked()
            records = self._all_metadata()
            for existing in records:
                if existing.get("idempotency_key") != idempotency_key:
                    continue
                if existing.get("request_fingerprint") != request_fingerprint:
                    raise ComputeJobError(
                        "Idempotency key was already used for a different compute request."
                    )
                return str(existing["job_id"])
            active = [
                item for item in records if item.get("state") in ACTIVE_STATES
            ]
            if active:
                raise ComputeJobError(
                    f"Compute job {active[0]['job_id']} is already active; "
                    "concurrency is limited to 1."
                )

            job_root = self.jobs_root / job_id
            staged_job_root = self.staging_root / job_id
            staged_job_root.mkdir(mode=0o700, parents=False, exist_ok=False)
            published = False
            try:
                _write_runtime_snapshot(
                    staged_job_root / "entrypoint.py",
                    entrypoint_bytes,
                )
                _write_runtime_snapshot(
                    staged_job_root / "config.json",
                    config_bytes,
                )
                if checkpoint_bytes is not None:
                    _write_runtime_snapshot(
                        staged_job_root / "resume-checkpoint.json",
                        checkpoint_bytes,
                    )
                _atomic_write_json(staged_job_root / "metadata.json", metadata)
                os.rename(staged_job_root, job_root)
                published = True
            finally:
                if not published:
                    _cleanup_staged_job(staged_job_root)
            _fsync_directory(self.staging_root)
            _fsync_directory(self.jobs_root)
        return job_id

    def start(
        self,
        *,
        entrypoint_id: str,
        config_path: str,
        idempotency_key: str,
        resume_job_id: str = "",
    ) -> dict[str, Any]:
        """Start one detached worker after approval, confinement, and deduplication checks."""
        if not _IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
            raise ComputeJobError("Compute job idempotency_key must contain 8 to 128 safe characters.")
        entrypoint, approval, entrypoint_bytes, entrypoint_sha256 = (
            self._approved_entrypoint(entrypoint_id)
        )
        config = _confined_regular_file(self.workspace, config_path, suffix=".json")
        config_bytes, config_sha256 = _stable_regular_file_bytes(
            config,
            maximum_bytes=MAX_CONFIG_BYTES,
            label="Compute job config",
        )
        try:
            config_payload = json.loads(config_bytes.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise ComputeJobError("Compute job config is not valid UTF-8 JSON.") from exc
        if not isinstance(config_payload, dict):
            raise ComputeJobError("Compute job config must contain one JSON object.")
        max_runtime = approval.get("max_runtime_seconds", DEFAULT_MAX_RUNTIME_SECONDS)
        if isinstance(max_runtime, bool) or not isinstance(max_runtime, int):
            raise ComputeJobError("Approved max_runtime_seconds must be an integer.")
        if not DEFAULT_MAX_RUNTIME_SECONDS <= max_runtime <= MAX_MAX_RUNTIME_SECONDS:
            raise ComputeJobError("Approved max_runtime_seconds must be between 43,200 and 86,400 seconds.")
        checkpoint_bytes: bytes | None = None
        checkpoint_sha256 = ""
        if resume_job_id:
            source = self._load_metadata(resume_job_id)
            if source.get("state") not in TERMINAL_STATES:
                raise ComputeJobError("Only a terminal compute job can be resumed.")
            if source.get("entrypoint") != entrypoint_id:
                raise ComputeJobError("Resume entrypoint does not match the prior job.")
            source_checkpoint_path = self.jobs_root / resume_job_id / "checkpoint.json"
            checkpoint_bytes, checkpoint_sha256 = _stable_regular_file_bytes(
                source_checkpoint_path,
                maximum_bytes=MAX_CONFIG_BYTES,
                label="Compute resume checkpoint",
            )
            try:
                checkpoint = json.loads(checkpoint_bytes.decode("utf-8"))
            except (UnicodeError, ValueError) as exc:
                raise ComputeJobError(
                    "Compute resume checkpoint is not valid UTF-8 JSON."
                ) from exc
            validate_optimizer_checkpoint(checkpoint)

        request_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "entrypoint": entrypoint_id,
                    "entrypoint_sha256": entrypoint_sha256,
                    "config": config.relative_to(self.workspace).as_posix(),
                    "config_sha256": config_sha256,
                    "resume_job_id": resume_job_id,
                    "resume_checkpoint_sha256": checkpoint_sha256,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        job_id = secrets.token_hex(16)
        now = _utc_now()
        metadata: dict[str, Any] = {
            "schema_version": 1,
            "revision": 1,
            "job_id": job_id,
            "workspace": str(self.workspace),
            "workspace_device": self.workspace_device,
            "workspace_inode": self.workspace_inode,
            "state": "starting",
            "pid": 0,
            "process_identity": "",
            "child_pid": 0,
            "child_process_identity": "",
            "child_process_group": 0,
            "child_cgroup": "",
            "containment_kind": _platform_compute_containment_kind(),
            "containment_cleared_at": "",
            "started_at": now,
            "updated_at": now,
            "ended_at": "",
            "entrypoint": entrypoint_id,
            "entrypoint_path": entrypoint.relative_to(self.workspace).as_posix(),
            "entrypoint_snapshot_path": "entrypoint.py",
            "entrypoint_sha256": entrypoint_sha256,
            "config_path": config.relative_to(self.workspace).as_posix(),
            "config_sha256": config_sha256,
            "resume_checkpoint_path": (
                "resume-checkpoint.json" if checkpoint_bytes is not None else ""
            ),
            "resume_checkpoint_sha256": checkpoint_sha256,
            "checkpoint_path": "checkpoint.json",
            "result_path": "result.json",
            "exit_status": None,
            "idempotency_key": idempotency_key,
            "request_fingerprint": request_fingerprint,
            "resumed_from": resume_job_id,
            "max_runtime_seconds": max_runtime,
            "message": "Starting approved compute worker.",
            "sleep_assertion_pid": 0,
            "sleep_assertion_identity": "",
        }
        reserved_job_id = self._reserve_starting_job(
            metadata=metadata,
            entrypoint_bytes=entrypoint_bytes,
            config_bytes=config_bytes,
            checkpoint_bytes=checkpoint_bytes,
        )
        if reserved_job_id != job_id:
            return self.status(reserved_job_id)
        job_root = self.jobs_root / job_id
        entrypoint_snapshot = job_root / "entrypoint.py"
        config_snapshot = job_root / "config.json"
        checkpoint_path = (
            job_root / "resume-checkpoint.json"
            if checkpoint_bytes is not None
            else None
        )
        command = [
            sys.executable,
            "-m",
            "app.core.agent.compute_jobs",
            "--worker",
            str(job_root / "metadata.json"),
            str(entrypoint_snapshot),
            str(config_snapshot),
            str(max_runtime),
        ]
        if checkpoint_path is not None:
            command.append(str(checkpoint_path))
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "SYSTEMROOT", "WINDIR"}
        }
        package_root = Path(__file__).resolve().parents[3]
        environment["PYTHONPATH"] = str(package_root)
        environment["PYTHONUNBUFFERED"] = "1"
        environment["AGENTIC_CONTEXT_COMPUTE_JOB"] = job_id
        try:
            process = subprocess.Popen(
                command,
                cwd=self.workspace,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            def mark_launch_failed(current: dict[str, Any]) -> dict[str, Any]:
                current["state"] = "failed"
                current["updated_at"] = _utc_now()
                current["ended_at"] = current["updated_at"]
                current["message"] = "Approved compute worker could not start."
                return current

            self._transition_metadata(job_id, mark_launch_failed)
            raise ComputeJobError("Approved compute worker could not start.") from exc
        identity = ""
        previous_identity = ""
        for _ in range(50):
            if process.poll() is not None:
                break
            current_identity = _process_identity(process.pid)
            if current_identity and current_identity == previous_identity:
                identity = current_identity
                break
            previous_identity = current_identity
            time.sleep(0.02)
        current_metadata = self._load_metadata(job_id)
        if current_metadata.get("state") in TERMINAL_STATES:
            return self.status(job_id)
        if not identity or process.poll() is not None:
            wrapper_ended = process.poll() is not None
            if not wrapper_ended:
                try:
                    process.kill()
                    process.wait(timeout=2.0)
                    wrapper_ended = True
                except (OSError, subprocess.TimeoutExpired):
                    wrapper_ended = process.poll() is not None

            def mark_identity_failed(current: dict[str, Any]) -> dict[str, Any] | None:
                if not wrapper_ended:
                    return None
                if current.get("state") in TERMINAL_STATES:
                    return None
                current["state"] = "failed"
                current["updated_at"] = _utc_now()
                current["ended_at"] = current["updated_at"]
                current["message"] = "Compute worker identity could not be verified."
                return current

            self._transition_metadata(job_id, mark_identity_failed)
            if not wrapper_ended:
                raise ComputeJobError(
                    "Compute worker identity and termination could not be verified; writer "
                    "admission remains blocked."
                )
            raise ComputeJobError("Compute worker identity could not be verified.")

        def commit_running(current: dict[str, Any]) -> dict[str, Any] | None:
            if current.get("state") in TERMINAL_STATES:
                return None
            if (
                current.get("state") != "starting"
                or int(current.get("pid") or 0) != 0
                or str(current.get("process_identity") or "")
            ):
                raise ComputeJobError(
                    "Compute worker ownership changed before launch could be committed."
                )
            current["pid"] = process.pid
            current["process_identity"] = identity
            current["state"] = "running"
            current["updated_at"] = _utc_now()
            current["message"] = (
                "Approved compute worker is running independently of the provider turn."
            )
            return current

        running_metadata = self._transition_metadata(job_id, commit_running)
        assertion_metadata = dict(running_metadata)
        self._start_sleep_assertion(assertion_metadata)
        assertion_pid = int(assertion_metadata.get("sleep_assertion_pid") or 0)
        assertion_identity = str(
            assertion_metadata.get("sleep_assertion_identity") or ""
        )
        if assertion_pid > 0 and assertion_identity:
            assertion_attached = False

            def attach_assertion(current: dict[str, Any]) -> dict[str, Any] | None:
                nonlocal assertion_attached
                if (
                    current.get("state") != "running"
                    or int(current.get("pid") or 0) != process.pid
                    or current.get("process_identity") != identity
                ):
                    return None
                current["sleep_assertion_pid"] = assertion_pid
                current["sleep_assertion_identity"] = assertion_identity
                assertion_attached = True
                return current

            self._transition_metadata(job_id, attach_assertion)
            if not assertion_attached:
                _release_sleep_assertion(assertion_metadata)
        return self.status(job_id)

    def _start_sleep_assertion(self, metadata: dict[str, Any]) -> None:
        if sys.platform != "darwin" or not self._caffeinate_executable.is_file():
            return
        try:
            process = subprocess.Popen(
                [str(self._caffeinate_executable), "-i", "-w", str(metadata["pid"])],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        except OSError:
            return
        identity = _process_identity(process.pid)
        if identity:
            metadata["sleep_assertion_pid"] = process.pid
            metadata["sleep_assertion_identity"] = identity

    def status(self, job_id: str = "") -> dict[str, Any]:
        """Return bounded metadata, progress, and a small log tail."""
        self.reconcile()
        records = self._all_metadata()
        if not job_id:
            if not records:
                return {"state": "idle", "active": False}
            records.sort(key=lambda item: str(item.get("started_at") or ""), reverse=True)
            metadata = records[0]
        else:
            metadata = self._load_metadata(job_id)
        job_root = self.jobs_root / str(metadata["job_id"])
        progress: dict[str, Any] = {}
        progress_path = job_root / "progress.json"
        if progress_path.exists():
            try:
                raw_progress = _read_json_object(
                    progress_path,
                    maximum_bytes=MAX_PROGRESS_BYTES,
                )
                progress = {
                    key: value
                    for key, value in raw_progress.items()
                    if key in PROGRESS_FIELDS
                }
                if "summary" in progress:
                    progress["summary"] = str(progress["summary"])[
                        :MAX_STATUS_TEXT_CHARS
                    ]
            except ComputeJobError:
                progress = {"summary": "Latest progress heartbeat is invalid."}
        log_tail = ""
        log_path = job_root / "worker.log"
        try:
            with log_path.open("rb") as handle:
                handle.seek(max(0, log_path.stat().st_size - MAX_LOG_TAIL_CHARS * 4))
                log_tail = handle.read().decode("utf-8", errors="replace")[-MAX_LOG_TAIL_CHARS:]
        except OSError:
            pass
        can_resume = False
        checkpoint_path = job_root / "checkpoint.json"
        if metadata.get("state") in TERMINAL_STATES and checkpoint_path.is_file():
            try:
                validate_optimizer_checkpoint(
                    _read_json_object(checkpoint_path, maximum_bytes=MAX_CONFIG_BYTES)
                )
                can_resume = True
            except ComputeJobError:
                can_resume = False
        allowed = {
            key: metadata.get(key)
            for key in (
                "job_id",
                "state",
                "started_at",
                "updated_at",
                "ended_at",
                "entrypoint",
                "config_path",
                "exit_status",
                "checkpoint_path",
                "result_path",
                "resumed_from",
                "max_runtime_seconds",
                "message",
            )
        }
        allowed.update(
            {
                "active": metadata.get("state") in ACTIVE_STATES,
                "progress": progress,
                "log_tail": log_tail,
                "can_resume": can_resume,
            }
        )
        return allowed

    def stop(self, job_id: str) -> dict[str, Any]:
        """Request worker-owned containment and fall back only where it is provable."""
        stop_committed = False
        identity_mismatch = False

        def commit_stop(current: dict[str, Any]) -> dict[str, Any] | None:
            nonlocal identity_mismatch, stop_committed
            if current.get("state") not in ACTIVE_STATES:
                return None
            if not _identity_matches(
                current.get("pid"),
                current.get("process_identity"),
            ):
                identity_mismatch = True
                return None
            current["state"] = "stopping"
            current["updated_at"] = _utc_now()
            current["message"] = "Stop requested; the worker is clearing its process container."
            stop_committed = True
            return current

        metadata = self._transition_metadata(job_id, commit_stop)
        if identity_mismatch:
            _reconcile_scanned_compute_job_metadata(
                self._metadata_path(job_id),
                metadata,
            )
            raise ComputeJobError(
                "Compute worker identity changed; refusing to terminate a reused PID."
            )
        if not stop_committed:
            return self.status(job_id)
        worker_pid = int(metadata["pid"])
        worker_identity = str(metadata["process_identity"])
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            current = self._load_metadata(job_id)
            if current.get("state") in TERMINAL_STATES:
                self._release_assertion(current)
                return self.status(job_id)
            if not _identity_matches(worker_pid, worker_identity):
                reconciled = _reconcile_scanned_compute_job_metadata(
                    self._metadata_path(job_id),
                    current,
                )
                if reconciled.get("state") in TERMINAL_STATES:
                    self._release_assertion(reconciled)
                    return self.status(job_id)
                break
            time.sleep(0.05)

        if os.name != "posix":
            raise ComputeJobError(
                "The Windows worker did not publish a Job Object containment receipt; writer "
                "admission remains blocked."
            )

        current = self._load_metadata(job_id)
        child_pid = int(current.get("child_pid") or 0)
        if sys.platform == "darwin":
            if current.get("containment_kind") != "macos-sandbox-no-fork":
                raise ComputeJobError(
                    "Compute-job fork containment provenance is unavailable; writer admission "
                    "remains blocked."
                )
        elif sys.platform.startswith("linux"):
            if current.get("containment_kind") != "linux-cgroup-v2":
                raise ComputeJobError(
                    "Compute-job cgroup containment provenance is unavailable; writer admission "
                    "remains blocked."
                )
            raw_cgroup = str(current.get("child_cgroup") or "")
            if child_pid > 0 and not raw_cgroup:
                raise ComputeJobError(
                    "Compute-job Linux cgroup record is incomplete; writer admission remains "
                    "blocked."
                )
            cgroup = _LinuxCgroup.from_metadata(job_id, raw_cgroup)
            if cgroup is not None:
                cgroup_cleared, _cgroup_detected = cgroup.terminate_and_remove(
                    timeout=2.0,
                )
                if not cgroup_cleared:
                    raise ComputeJobError(
                        "Compute child cgroup could not be cleared; writer admission remains "
                        "blocked."
                    )
        child_group = int(current.get("child_process_group") or 0)
        if child_group > 0 and not _terminate_posix_process_group(
            child_group,
            timeout=2.0,
        ):
            raise ComputeJobError(
                "Compute child process containment could not be cleared; writer admission "
                "remains blocked."
            )
        if not _terminate_process_group(
            worker_pid,
            timeout=2.0,
            expected_identity=worker_identity,
        ):
            raise ComputeJobError(
                "Compute wrapper termination could not be verified; writer admission remains "
                "blocked."
            )
        marker_processes_cleared, _marker_processes_detected = (
            _terminate_posix_job_marker_processes(
                job_id,
                timeout=2.0,
            )
        )
        if not marker_processes_cleared:
            raise ComputeJobError(
                "Compute job-marker descendants could not be cleared; writer admission remains "
                "blocked."
            )
        child_identity = str(current.get("child_process_identity") or "")
        if (
            child_pid > 0
            and _identity_matches(child_pid, child_identity)
            or child_group > 0
            and _posix_process_group_exists(child_group)
        ):
            raise ComputeJobError(
                "Compute child process containment is still active; writer admission remains "
                "blocked."
            )

        def finish_stop(current: dict[str, Any]) -> dict[str, Any] | None:
            if current.get("state") in TERMINAL_STATES:
                return None
            if current.get("state") != "stopping":
                raise ComputeJobError(
                    "Compute job state changed before Stop could be finalized."
                )
            current["state"] = "stopped"
            current["updated_at"] = _utc_now()
            current["ended_at"] = current["updated_at"]
            current["containment_cleared_at"] = current["updated_at"]
            current["message"] = "Compute job was stopped with its owned process tree."
            return current

        metadata = self._transition_metadata(job_id, finish_stop)
        self._release_assertion(metadata)
        return self.status(job_id)


def _bounded_log_append(log_path: Path, data: bytes) -> None:
    with log_path.open("ab") as handle:
        handle.write(data)
    size = log_path.stat().st_size
    if size <= MAX_LOG_BYTES:
        return
    with log_path.open("rb") as handle:
        handle.seek(size - MAX_LOG_BYTES)
        retained = handle.read()
    with log_path.open("wb") as handle:
        handle.write(retained)


def _wait_for_worker_ownership(metadata_path: Path) -> dict[str, Any] | None:
    """Wait until the launcher durably binds this wrapper PID and birth identity."""
    deadline = time.monotonic() + WORKER_OWNERSHIP_HANDSHAKE_SECONDS
    while time.monotonic() < deadline:
        try:
            metadata = _validate_compute_job_metadata(
                _read_json_object(metadata_path, maximum_bytes=MAX_METADATA_BYTES),
                expected_job_id=metadata_path.parent.name,
            )
        except ComputeJobError:
            return None
        if metadata.get("state") in TERMINAL_STATES:
            return None
        if (
            metadata.get("state") in {"running", "stopping"}
            and int(metadata.get("pid") or 0) == os.getpid()
            and _identity_matches(os.getpid(), metadata.get("process_identity"))
        ):
            return metadata
        time.sleep(0.02)
    return None


def _approved_child_main(arguments: list[str]) -> int:
    """Wait for containment publication, then replace this process with approved bytes."""
    if len(arguments) not in {6, 7}:
        return 64
    ready_path = Path(arguments[0]).resolve(strict=False)
    ready_token = str(arguments[1])
    entrypoint = Path(arguments[2]).resolve(strict=True)
    source_directory = Path(arguments[3]).resolve(strict=True)
    config = Path(arguments[4]).resolve(strict=True)
    job_root = Path(arguments[5]).resolve(strict=True)
    checkpoint = Path(arguments[6]).resolve(strict=True) if len(arguments) == 7 else None
    if (
        ready_path.parent.resolve(strict=True) != job_root
        or entrypoint.parent != job_root
        or config.parent != job_root
        or not source_directory.is_dir()
    ):
        return 65
    deadline = time.monotonic() + WORKER_OWNERSHIP_HANDSHAKE_SECONDS
    while time.monotonic() < deadline:
        try:
            if ready_path.read_text(encoding="ascii") == ready_token:
                break
        except (OSError, UnicodeError):
            pass
        time.sleep(0.02)
    else:
        return 75
    try:
        ready_path.unlink()
    except OSError:
        return 75
    command = [
        sys.executable,
        str(entrypoint),
        "--config",
        str(config),
        "--job-runtime",
        str(job_root),
    ]
    if checkpoint is not None:
        command.extend(["--resume", str(checkpoint)])
    if sys.platform == "darwin":
        if not MACOS_SANDBOX_EXECUTABLE.is_file():
            return 77
        command = [
            str(MACOS_SANDBOX_EXECUTABLE),
            "-p",
            MACOS_COMPUTE_SANDBOX_PROFILE,
            *command,
        ]
    environment = dict(os.environ)
    environment["AGENTIC_CONTEXT_COMPUTE_JOB_RUNTIME"] = str(job_root)
    os.chdir(source_directory)
    try:
        os.execve(command[0], command, environment)
    except OSError:
        return 71


def _worker_main(arguments: list[str]) -> int:
    """Run one approved snapshot and publish terminal state after tree containment."""
    if len(arguments) not in {4, 5}:
        return 64
    metadata_path = Path(arguments[0]).resolve(strict=True)
    entrypoint = Path(arguments[1]).resolve(strict=True)
    config = Path(arguments[2]).resolve(strict=True)
    max_runtime = int(arguments[3])
    checkpoint = Path(arguments[4]).resolve(strict=True) if len(arguments) == 5 else None
    job_root = metadata_path.parent
    metadata = _wait_for_worker_ownership(metadata_path)
    if metadata is None:
        return 75

    def publish_failure(
        message: str,
        exit_status: int = 70,
        *,
        containment_cleared: bool = True,
    ) -> int:
        def fail(current: dict[str, Any]) -> dict[str, Any] | None:
            state = current.get("state")
            if state not in {"running", "stopping"} or not containment_cleared:
                return None
            if (
                int(current.get("pid") or 0) != os.getpid()
                or not _identity_matches(
                    os.getpid(),
                    current.get("process_identity"),
                )
            ):
                raise ComputeJobError("Compute wrapper ownership changed before failure publication.")
            current["exit_status"] = exit_status
            current["state"] = "stopped" if state == "stopping" else "failed"
            current["updated_at"] = _utc_now()
            current["ended_at"] = current["updated_at"]
            current["containment_cleared_at"] = current["updated_at"]
            current["message"] = (
                "Compute job was stopped with its owned process tree."
                if state == "stopping"
                else message
            )
            return current

        try:
            _update_metadata_file(metadata_path, fail)
        except ComputeJobError:
            pass
        return exit_status

    if metadata.get("state") == "stopping":
        return publish_failure(
            "Compute job was stopped before approved child launch.",
            exit_status=0,
        )

    expected_entrypoint = job_root / str(
        metadata.get("entrypoint_snapshot_path") or "entrypoint.py"
    )
    if entrypoint != expected_entrypoint:
        return publish_failure("Compute entrypoint snapshot path is invalid.")
    try:
        _entrypoint_bytes, entrypoint_sha256 = _stable_regular_file_bytes(
            entrypoint,
            maximum_bytes=MAX_ENTRYPOINT_BYTES,
            label="Compute entrypoint snapshot",
        )
        _config_bytes, config_sha256 = _stable_regular_file_bytes(
            config,
            maximum_bytes=MAX_CONFIG_BYTES,
            label="Compute config snapshot",
        )
    except ComputeJobError:
        return publish_failure("Compute job input snapshots could not be verified.")
    if (
        entrypoint_sha256 != metadata.get("entrypoint_sha256")
        or config_sha256 != metadata.get("config_sha256")
    ):
        return publish_failure("Compute job input snapshot SHA-256 verification failed.")
    if checkpoint is not None:
        if checkpoint != job_root / str(metadata.get("resume_checkpoint_path") or ""):
            return publish_failure("Compute resume checkpoint snapshot path is invalid.")
        try:
            _checkpoint_bytes, checkpoint_sha256 = _stable_regular_file_bytes(
                checkpoint,
                maximum_bytes=MAX_CONFIG_BYTES,
                label="Compute resume checkpoint snapshot",
            )
        except ComputeJobError:
            return publish_failure("Compute resume checkpoint snapshot could not be verified.")
        if checkpoint_sha256 != metadata.get("resume_checkpoint_sha256"):
            return publish_failure(
                "Compute resume checkpoint snapshot SHA-256 verification failed."
            )
    try:
        workspace = Path(str(metadata.get("workspace") or "")).resolve(strict=True)
        workspace_metadata = workspace.stat()
        source_relative = Path(str(metadata.get("entrypoint_path") or ""))
        source_directory = (workspace / source_relative).parent.resolve(strict=True)
    except OSError:
        return publish_failure("Compute entrypoint source directory is unavailable.")
    if (
        int(workspace_metadata.st_dev) != metadata.get("workspace_device")
        or int(workspace_metadata.st_ino) != metadata.get("workspace_inode")
    ):
        return publish_failure("Compute workspace identity changed before execution.")
    try:
        source_directory.relative_to(workspace)
    except ValueError:
        return publish_failure("Compute entrypoint source directory is outside the workspace.")

    stop_requested = Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_requested.set()

    if os.name == "posix":
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)

    ready_token = secrets.token_hex(32)
    ready_path = job_root / "launch.ready"
    child_command = [
        sys.executable,
        "-m",
        "app.core.agent.compute_jobs",
        "--approved-child",
        str(ready_path),
        ready_token,
        str(entrypoint),
        str(source_directory),
        str(config),
        str(job_root),
    ]
    if checkpoint is not None:
        child_command.append(str(checkpoint))
    required_containment_kind = _platform_compute_containment_kind()
    if metadata.get("containment_kind") != required_containment_kind:
        return publish_failure("Compute process-tree containment provenance is invalid.")
    child_options: dict[str, Any] = {}
    windows_job: _WindowsJob | None = None
    linux_cgroup: _LinuxCgroup | None = None
    if os.name == "posix":
        child_options["start_new_session"] = True
        if sys.platform == "darwin":
            if not MACOS_SANDBOX_EXECUTABLE.is_file():
                return publish_failure(
                    "macOS compute fork containment is unavailable; approved code was not run."
                )
        elif sys.platform.startswith("linux"):
            try:
                linux_cgroup = _LinuxCgroup.create(str(metadata.get("job_id") or ""))
            except ComputeJobError:
                return publish_failure(
                    "Linux cgroup v2 containment is unavailable; approved code was not run."
                )
        else:
            return publish_failure(
                "This POSIX host lacks an approved process-tree containment boundary."
            )
    elif os.name == "nt":
        child_options["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        try:
            windows_job = _WindowsJob()
        except OSError:
            return publish_failure("Windows process-tree containment could not be created.")
    try:
        child = subprocess.Popen(
            child_command,
            cwd=job_root,
            env=dict(os.environ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            close_fds=True,
            **child_options,
        )
    except OSError:
        if windows_job is not None:
            windows_job.close()
        containment_cleared = True
        if linux_cgroup is not None:
            try:
                containment_cleared, _detected = linux_cgroup.terminate_and_remove(
                    timeout=0.5,
                )
            except ComputeJobError:
                containment_cleared = False
        return publish_failure(
            "Approved compute child could not be started.",
            containment_cleared=containment_cleared,
        )

    posix_marker_descendants_detected = False

    def clear_child_containment(timeout: float) -> bool:
        """Terminate the child container and prove that it no longer has members."""
        nonlocal posix_marker_descendants_detected
        if windows_job is not None:
            try:
                if windows_job.active_processes() > 0:
                    windows_job.terminate()
                return windows_job.wait_empty(timeout)
            except OSError:
                return False
        if os.name == "posix":
            cgroup_cleared = True
            cgroup_processes_detected = False
            if linux_cgroup is not None:
                try:
                    cgroup_cleared, cgroup_processes_detected = (
                        linux_cgroup.terminate_and_remove(timeout=timeout)
                    )
                except ComputeJobError:
                    cgroup_cleared = False
                posix_marker_descendants_detected = (
                    posix_marker_descendants_detected or cgroup_processes_detected
                )
            process_group_cleared = _terminate_posix_process_group(
                child.pid,
                timeout=timeout,
                reap_process=child.poll,
            )
            try:
                marker_processes_cleared, marker_processes_detected = (
                    _terminate_posix_job_marker_processes(
                        str(metadata.get("job_id") or ""),
                        exclude_pids=frozenset({os.getpid()}),
                        timeout=timeout,
                    )
                )
            except ComputeJobError:
                return False
            posix_marker_descendants_detected = (
                posix_marker_descendants_detected or marker_processes_detected
            )
            return (
                cgroup_cleared
                and process_group_cleared
                and marker_processes_cleared
            )
        return child.poll() is not None

    try:
        if windows_job is not None:
            try:
                windows_job.assign(child)
            except OSError:
                child.kill()
                child.wait()
                return publish_failure(
                    "Approved compute child could not enter Windows process-tree containment."
                )
        child_identity = _wait_for_process_identity(child.pid)
        if not child_identity:
            containment_cleared = clear_child_containment(2.0)
            return publish_failure(
                "Approved compute child identity could not be verified.",
                containment_cleared=containment_cleared,
            )
        if linux_cgroup is not None:
            try:
                linux_cgroup.assign(child.pid, child_identity)
            except ComputeJobError:
                containment_cleared = clear_child_containment(2.0)
                return publish_failure(
                    "Approved compute child could not enter Linux cgroup containment.",
                    containment_cleared=containment_cleared,
                )
        child_registered = False

        def register_child(current: dict[str, Any]) -> dict[str, Any] | None:
            nonlocal child_registered
            if current.get("state") != "running":
                return None
            if (
                int(current.get("pid") or 0) != os.getpid()
                or not _identity_matches(
                    os.getpid(),
                    current.get("process_identity"),
                )
            ):
                raise ComputeJobError(
                    "Compute wrapper ownership changed before child registration."
                )
            current["child_pid"] = child.pid
            current["child_process_identity"] = child_identity
            current["child_process_group"] = child.pid
            current["child_cgroup"] = (
                str(linux_cgroup.path) if linux_cgroup is not None else ""
            )
            current["updated_at"] = _utc_now()
            child_registered = True
            return current

        _update_metadata_file(metadata_path, register_child)
        if not child_registered:
            containment_cleared = clear_child_containment(2.0)
            if containment_cleared:
                return child.wait()
            return publish_failure(
                "Compute child containment could not be cleared after ownership changed.",
                containment_cleared=False,
            )

        output_queue: Queue[bytes | None] = Queue(maxsize=8)
        if child.stdout is None:
            containment_cleared = clear_child_containment(2.0)
            return publish_failure(
                "Approved compute child output could not be captured.",
                containment_cleared=containment_cleared,
            )
        output_reader = Thread(
            target=_read_process_output,
            args=(child.stdout, output_queue),
            daemon=True,
        )
        output_reader.start()
        _write_runtime_snapshot(ready_path, ready_token.encode("ascii"))
        log_path = job_root / "worker.log"
        started = time.monotonic()
        timed_out = False
        interrupted = False
        stop_committed = False
        descendants_detected = posix_marker_descendants_detected
        containment_cleared = True
        containment_attempted = False
        reader_finished = False
        pipe_close_deadline: float | None = None
        last_state_check = 0.0
        while True:
            while True:
                try:
                    chunk = output_queue.get_nowait()
                except Empty:
                    break
                if chunk is None:
                    reader_finished = True
                    break
                _bounded_log_append(log_path, chunk)
            now = time.monotonic()
            if now - last_state_check >= 0.1:
                current = _validate_compute_job_metadata(
                    _read_json_object(
                        metadata_path,
                        maximum_bytes=MAX_METADATA_BYTES,
                    ),
                    expected_job_id=metadata_path.parent.name,
                )
                if current.get("state") == "stopping":
                    stop_committed = True
                elif current.get("state") != "running":
                    raise ComputeJobError(
                        "Compute-job state changed outside the worker lifecycle."
                    )
                if (
                    int(current.get("pid") or 0) != os.getpid()
                    or not _identity_matches(
                        os.getpid(),
                        current.get("process_identity"),
                    )
                ):
                    raise ComputeJobError(
                        "Compute wrapper ownership changed while the child was active."
                    )
                last_state_check = now
            if stop_requested.is_set():
                interrupted = True
            if now - started >= max_runtime:
                timed_out = True
            direct_exit_status = child.poll()
            containment_active = False
            if direct_exit_status is not None and not containment_attempted:
                if windows_job is not None:
                    containment_active = windows_job.active_processes() > 0
                elif os.name == "posix":
                    containment_active = _posix_process_group_exists(child.pid)
                if containment_active:
                    descendants_detected = True
            if (
                (stop_committed or interrupted or timed_out or containment_active)
                and not containment_attempted
            ):
                containment_attempted = True
                containment_cleared = clear_child_containment(2.0)
                pipe_close_deadline = time.monotonic() + 2.0
                if not containment_cleared:
                    break
            if direct_exit_status is not None and pipe_close_deadline is None:
                pipe_close_deadline = time.monotonic() + 2.0
            if direct_exit_status is not None and reader_finished:
                break
            if (
                direct_exit_status is not None
                and pipe_close_deadline is not None
                and time.monotonic() >= pipe_close_deadline
            ):
                containment_cleared = False
                break
            time.sleep(0.05)
        exit_status = child.poll()
        if exit_status is None and containment_cleared:
            try:
                exit_status = child.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                containment_cleared = False
        if exit_status is None:
            exit_status = 70
        output_reader.join(timeout=2.0)
        if containment_cleared and os.name == "posix":
            containment_cleared = clear_child_containment(2.0)
            descendants_detected = (
                descendants_detected or posix_marker_descendants_detected
            )
        if not containment_cleared:
            return publish_failure(
                "Compute child containment could not be proved empty; writer admission remains "
                "blocked.",
                containment_cleared=False,
            )

        def finish(current: dict[str, Any]) -> dict[str, Any] | None:
            state = current.get("state")
            if state not in {"running", "stopping"}:
                return None
            if (
                int(current.get("pid") or 0) != os.getpid()
                or not _identity_matches(
                    os.getpid(),
                    current.get("process_identity"),
                )
            ):
                raise ComputeJobError(
                    "Compute wrapper ownership changed before terminal publication."
                )
            current["exit_status"] = exit_status
            current["state"] = (
                "stopped"
                if state == "stopping"
                else (
                    "succeeded"
                    if exit_status == 0
                    and not timed_out
                    and not interrupted
                    and not descendants_detected
                    else ("interrupted" if interrupted else "failed")
                )
            )
            current["updated_at"] = _utc_now()
            current["ended_at"] = current["updated_at"]
            current["containment_cleared_at"] = current["updated_at"]
            current["message"] = (
                "Compute job was stopped with its owned process tree."
                if state == "stopping"
                else (
                    "Compute job exceeded its approved runtime limit."
                    if timed_out
                    else (
                        "Compute job left descendant processes; they were terminated before the "
                        "workspace lease was released."
                        if descendants_detected
                        else (
                            "Compute job was interrupted and its process tree was cleared."
                            if interrupted
                            else (
                                "Compute job completed."
                                if exit_status == 0
                                else "Compute job exited with a failure status."
                            )
                        )
                    )
                )
            )
            return current

        _update_metadata_file(metadata_path, finish)
        return exit_status
    except (ComputeJobError, OSError, RuntimeError, ValueError):
        containment_cleared = clear_child_containment(2.0)
        return publish_failure(
            "Compute worker failed before terminal publication.",
            containment_cleared=containment_cleared,
        )
    finally:
        try:
            ready_path.unlink(missing_ok=True)
        except OSError:
            pass
        if windows_job is not None:
            windows_job.close()


def _main(argv: list[str]) -> int:
    if not argv:
        return 64
    if argv[0] == "--worker":
        return _worker_main(argv[1:])
    if argv[0] == "--approved-child":
        return _approved_child_main(argv[1:])
    return 64


if __name__ == "__main__":  # pragma: no cover - exercised through detached workers.
    raise SystemExit(_main(sys.argv[1:]))
