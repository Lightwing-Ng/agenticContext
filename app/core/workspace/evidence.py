"""Workspace evidence: bounded Git status and mutation fingerprints."""

# Code version: v1.0.1-codex.0

from __future__ import annotations

from collections import deque
import hashlib
import json
import os
from pathlib import Path
from queue import Empty, Queue
import stat as stat_module
import subprocess
from threading import Event, Thread
import time
from typing import Any, Callable

from .executables import _trusted_system_executable
from .paths import (
    _path_has_controller_internal_file,
    _path_has_ignored_part,
    _path_has_sensitive_part,
    _path_is_ignored_fingerprint_artifact,
    _path_is_link_like,
)
from .process_io import (
    SEARCH_STDOUT_QUEUE_SIZE,
    _STREAM_READ_FAILED,
    _SUBPROCESS_POPEN_TYPE,
    _process_group_options,
    _queue_text_chunks,
    _stop_process,
)


GIT_STATUS_MAX_RAW_CHARS = 2 * 1_024 * 1_024


GIT_STATUS_TIMEOUT_SECONDS = 10


WORKSPACE_FINGERPRINT_MAX_FILES = 12_000


WORKSPACE_FINGERPRINT_MAX_DIRECTORIES = 12_000


WORKSPACE_FINGERPRINT_MAX_BYTES = 512 * 1_024 * 1_024


WORKSPACE_FINGERPRINT_TIMEOUT_SECONDS = 15


_REFERENCE_ARTIFACT_DIRECTORIES = (Path("forPrompts"), Path("docs/forPrompts"))


def _ignored_reference_artifact_directories(
    workspace: Path,
    *,
    deadline: float,
    should_stop: Callable[[], bool],
) -> frozenset[Path]:
    """Exclude only ignored, wholly untracked reference directories at known locations.

    Git ignore rules alone never define the source fingerprint. These two explicit
    reference-material locations may contain cloud placeholders; all other ignored
    source and fixture paths retain the ordinary content checks.
    """
    candidates: list[Path] = []
    for relative in _REFERENCE_ARTIFACT_DIRECTORIES:
        current = workspace
        for part in relative.parts:
            current /= part
            if _path_is_link_like(current) or not current.is_dir():
                break
        else:
            candidates.append(relative)
    if not candidates or not (workspace / ".git").exists():
        return frozenset()
    git = _trusted_system_executable("git", forbidden_root=workspace)
    if git is None:
        return frozenset()
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"}
    }
    excluded: set[Path] = set()
    for relative in candidates:
        for arguments in (
            ["check-ignore", "--quiet", "--", relative.as_posix()],
            ["ls-files", "--error-unmatch", "--", relative.as_posix()],
        ):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or should_stop():
                return frozenset()
            try:
                result = subprocess.run(
                    [
                        str(git),
                        "-c",
                        "core.fsmonitor=false",
                        "-c",
                        "core.untrackedCache=false",
                        *arguments,
                    ],
                    cwd=workspace,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=remaining,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                return frozenset()
            if result.returncode not in {0, 1}:
                return frozenset()
            if arguments[0] == "check-ignore" and result.returncode != 0:
                break
            if arguments[0] == "ls-files" and result.returncode == 1:
                excluded.add(relative)
    return frozenset(excluded)


def _safe_untracked_paths_from_status(status: str) -> list[Path]:
    """Recover safe untracked file paths from the already filtered status view."""
    paths: list[Path] = []
    for line in str(status or "").splitlines():
        if not line.startswith("?? "):
            continue
        try:
            value = json.loads(line[3:])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(value, str):
            continue
        candidate = Path(value)
        if (
            not value
            or candidate.is_absolute()
            or ".." in candidate.parts
            or _path_has_ignored_part(candidate)
            or _path_has_controller_internal_file(candidate)
            or _path_has_sensitive_part(candidate)
        ):
            continue
        paths.append(candidate)
    return paths


def _bounded_git_status_output(
    workspace: Path,
    *,
    should_stop: Callable[[], bool] | None = None,
    process_changed: Callable[[subprocess.Popen[str] | None], None] | None = None,
) -> tuple[str, bool]:
    """Stream a fixed Git porcelain command with a global memory and time limit."""
    stop_requested = should_stop or (lambda: False)
    publish_process = process_changed or (lambda _process: None)
    git = _trusted_system_executable("git", forbidden_root=workspace)
    if git is None:
        raise RuntimeError("Git is unavailable for bounded working-tree inspection.")
    command = [
        str(git),
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    ]
    try:
        process = subprocess.Popen(
            command,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **_process_group_options(),
        )
    except OSError as exc:
        raise RuntimeError("Could not inspect the bounded Git working-tree status.") from exc
    if process.stdout is None:
        _stop_process(process, timeout=1)
        raise RuntimeError("Could not open the bounded Git status stream.")

    chunks: list[str] = []
    used = 0
    truncated = False
    timed_out = False
    stopped = False
    stream_failed = False
    loop_completed = False
    discard_output: Event | None = None
    reader: Thread | None = None
    deadline = time.monotonic() + GIT_STATUS_TIMEOUT_SECONDS
    try:
        output_queue: Queue[Any] = Queue(maxsize=SEARCH_STDOUT_QUEUE_SIZE)
        discard_output = Event()
        reader = Thread(
            target=_queue_text_chunks,
            args=(process.stdout, output_queue, discard_output),
            daemon=True,
        )
        publish_process(process)
        reader.start()
        while True:
            if stop_requested():
                stopped = True
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            try:
                remaining_time = max(0.001, deadline - time.monotonic())
                chunk = output_queue.get(timeout=min(0.05, remaining_time))
            except Empty:
                if not reader.is_alive() and output_queue.empty():
                    break
                continue
            if chunk is _STREAM_READ_FAILED:
                stream_failed = True
                break
            if chunk is None:
                break
            if not isinstance(chunk, str):
                stream_failed = True
                break
            remaining_chars = GIT_STATUS_MAX_RAW_CHARS - used
            if len(chunk) >= remaining_chars:
                if remaining_chars > 0:
                    chunks.append(chunk[:remaining_chars])
                used += max(0, remaining_chars)
                truncated = True
                break
            chunks.append(chunk)
            used += len(chunk)
        loop_completed = True
    finally:
        if (
            truncated
            or timed_out
            or stopped
            or stream_failed
            or not loop_completed
        ):
            if discard_output is not None:
                discard_output.set()
            _stop_process(process, timeout=1)
        try:
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            if discard_output is not None:
                discard_output.set()
            _stop_process(process, timeout=1)
        if isinstance(process, _SUBPROCESS_POPEN_TYPE):
            _stop_process(process, timeout=0.25)
        if discard_output is not None:
            discard_output.set()
        if reader is not None and reader.ident is not None:
            reader.join(timeout=1)
        if reader is None or not reader.is_alive():
            try:
                process.stdout.close()
            except (OSError, ValueError):
                pass
        publish_process(None)

    if stopped:
        raise RuntimeError("Stop requested.")
    if stream_failed:
        raise RuntimeError("Could not read the bounded Git status stream safely.")
    if timed_out:
        raise RuntimeError(
            f"Git status exceeded the {GIT_STATUS_TIMEOUT_SECONDS}-second controller limit."
        )
    if not truncated and process.returncode != 0:
        raise RuntimeError(
            f"Git status failed with exit code {process.returncode}."
        )
    return "".join(chunks), truncated


def _filtered_git_status(
    workspace: Path,
    *,
    should_stop: Callable[[], bool] | None = None,
    process_changed: Callable[[subprocess.Popen[str] | None], None] | None = None,
) -> str:
    """Return porcelain status rows after excluding every protected path."""
    output, truncated = _bounded_git_status_output(
        workspace,
        should_stop=should_stop,
        process_changed=process_changed,
    )
    if truncated and not output.endswith("\x00"):
        output = output.rsplit("\x00", 1)[0] + "\x00" if "\x00" in output else ""
    values = output.split("\x00")
    rows: list[str] = []
    index = 0
    while index < len(values):
        record = values[index]
        index += 1
        if len(record) < 4 or record[2] != " ":
            continue
        status = record[:2]
        paths = [record[3:]]
        if ("R" in status or "C" in status) and index < len(values):
            paths.append(values[index])
            index += 1
        normalized_paths: list[str] = []
        safe = True
        for value in paths:
            candidate = Path(value)
            if (
                not value
                or candidate.is_absolute()
                or ".." in candidate.parts
                or _path_has_ignored_part(candidate)
                or _path_has_controller_internal_file(candidate)
                or _path_has_sensitive_part(candidate)
            ):
                safe = False
                break
            normalized_paths.append(candidate.as_posix())
        if not safe:
            continue
        rendered_paths = [json.dumps(path, ensure_ascii=False) for path in normalized_paths]
        if len(rendered_paths) == 2:
            rows.append(f"{status} {rendered_paths[1]} -> {rendered_paths[0]}")
        else:
            rows.append(f"{status} {rendered_paths[0]}")
        if len(rows) >= 12_000:
            break
    if truncated:
        rows.append("!! [status truncated at the controller output limit]")
    return "\n".join(rows)


def _workspace_mutation_fingerprint(
    workspace: Path,
    *,
    should_stop: Callable[[], bool] | None = None,
    timeout_seconds: float = WORKSPACE_FINGERPRINT_TIMEOUT_SECONDS,
) -> tuple[str, bool]:
    """Return a bounded content fingerprint and whether its scan was complete."""
    digest = hashlib.sha256()
    inspected_files = 0
    inspected_directories = 0
    inspected_bytes = 0
    pending = deque([workspace])
    observed_entries: list[tuple[Path, tuple[int, int, int, int, int, int]]] = []
    observed_directory_entries: dict[
        Path,
        tuple[tuple[str, int, int, int], ...],
    ] = {}
    deadline = time.monotonic() + max(0.001, float(timeout_seconds))
    stop_requested = should_stop or (lambda: False)
    ignored_references = _ignored_reference_artifact_directories(
        workspace,
        deadline=deadline,
        should_stop=stop_requested,
    )
    try:
        resolved_workspace = workspace.resolve(strict=True)
        root_stat = os.stat(workspace, follow_symlinks=False)
    except (OSError, ValueError):
        return digest.hexdigest(), False
    observed_entries.append(
        (
            workspace,
            (
                int(root_stat.st_dev),
                int(root_stat.st_ino),
                int(root_stat.st_mode),
                int(root_stat.st_size),
                int(root_stat.st_mtime_ns),
                int(root_stat.st_ctime_ns),
            ),
        )
    )

    while pending:
        if (
            inspected_directories >= WORKSPACE_FINGERPRINT_MAX_DIRECTORIES
            or time.monotonic() >= deadline
            or stop_requested()
        ):
            return digest.hexdigest(), False
        directory = pending.popleft()
        try:
            entries = sorted(
                os.scandir(directory),
                key=lambda entry: entry.name.casefold(),
            )
        except OSError:
            return digest.hexdigest(), False
        inspected_directories += 1
        directory_entries: list[tuple[str, int, int, int]] = []
        for entry in entries:
            if time.monotonic() >= deadline or stop_requested():
                return digest.hexdigest(), False
            path = Path(entry.path)
            try:
                relative = path.relative_to(workspace)
            except ValueError:
                return digest.hexdigest(), False
            if (
                _path_has_ignored_part(relative)
                or _path_is_ignored_fingerprint_artifact(relative)
                or _path_has_controller_internal_file(relative)
                or relative in ignored_references
            ):
                continue
            try:
                # Query the real file stat instead of the cached scandir stat:
                # on Windows (Python 3.12+) the cached stat reports placeholder
                # zeros for st_nlink/st_dev/st_ino, which would fail the hard
                # link check and the before/after identity comparison below.
                initial_stat = os.stat(path, follow_symlinks=False)
            except OSError:
                return digest.hexdigest(), False
            directory_entries.append(
                (
                    entry.name,
                    int(initial_stat.st_dev),
                    int(initial_stat.st_ino),
                    int(initial_stat.st_mode),
                )
            )
            relative_bytes = relative.as_posix().encode(
                "utf-8",
                errors="replace",
            )
            digest.update(relative_bytes)
            digest.update(
                f"\0mode:{initial_stat.st_mode}\0size:{initial_stat.st_size}\0".encode()
            )
            if stat_module.S_ISLNK(initial_stat.st_mode) or (
                stat_module.S_ISREG(initial_stat.st_mode)
                and initial_stat.st_nlink != 1
            ):
                return digest.hexdigest(), False
            if stat_module.S_ISDIR(initial_stat.st_mode) and _path_is_link_like(path):
                return digest.hexdigest(), False
            try:
                path.resolve(strict=True).relative_to(resolved_workspace)
            except (OSError, ValueError):
                return digest.hexdigest(), False
            if stat_module.S_ISDIR(initial_stat.st_mode):
                digest.update(b"directory\0")
                observed_entries.append(
                    (
                        path,
                        (
                            int(initial_stat.st_dev),
                            int(initial_stat.st_ino),
                            int(initial_stat.st_mode),
                            int(initial_stat.st_size),
                            int(initial_stat.st_mtime_ns),
                            int(initial_stat.st_ctime_ns),
                        ),
                    )
                )
                pending.append(path)
                continue
            if not stat_module.S_ISREG(initial_stat.st_mode):
                return digest.hexdigest(), False
            if inspected_files >= WORKSPACE_FINGERPRINT_MAX_FILES:
                return digest.hexdigest(), False
            if (
                inspected_bytes + initial_stat.st_size
                > WORKSPACE_FINGERPRINT_MAX_BYTES
            ):
                return digest.hexdigest(), False
            digest.update(b"file\0")
            try:
                with path.open("rb") as handle:
                    while True:
                        if time.monotonic() >= deadline or stop_requested():
                            return digest.hexdigest(), False
                        chunk = handle.read(64 * 1_024)
                        if not chunk:
                            break
                        digest.update(chunk)
                final_stat = path.stat()
            except OSError:
                return digest.hexdigest(), False
            if (
                initial_stat.st_dev,
                initial_stat.st_ino,
                initial_stat.st_mode,
                initial_stat.st_size,
                initial_stat.st_mtime_ns,
                initial_stat.st_ctime_ns,
            ) != (
                final_stat.st_dev,
                final_stat.st_ino,
                final_stat.st_mode,
                final_stat.st_size,
                final_stat.st_mtime_ns,
                final_stat.st_ctime_ns,
            ):
                return digest.hexdigest(), False
            observed_entries.append(
                (
                    path,
                    (
                        int(final_stat.st_dev),
                        int(final_stat.st_ino),
                        int(final_stat.st_mode),
                        int(final_stat.st_size),
                        int(final_stat.st_mtime_ns),
                        int(final_stat.st_ctime_ns),
                    ),
                )
            )
            inspected_files += 1
            inspected_bytes += initial_stat.st_size
        observed_directory_entries[directory] = tuple(directory_entries)

    for path, expected_identity in observed_entries:
        if time.monotonic() >= deadline or stop_requested():
            return digest.hexdigest(), False
        try:
            current_stat = os.stat(path, follow_symlinks=False)
        except OSError:
            return digest.hexdigest(), False
        current_identity = (
            int(current_stat.st_dev),
            int(current_stat.st_ino),
            int(current_stat.st_mode),
            int(current_stat.st_size),
            int(current_stat.st_mtime_ns),
            int(current_stat.st_ctime_ns),
        )
        if current_identity == expected_identity:
            continue
        if (
            current_identity[:3] != expected_identity[:3]
            or not stat_module.S_ISDIR(expected_identity[2])
        ):
            return digest.hexdigest(), False
        try:
            current_entries = sorted(
                os.scandir(path),
                key=lambda entry: entry.name.casefold(),
            )
        except OSError:
            return digest.hexdigest(), False
        current_directory_entries: list[tuple[str, int, int, int]] = []
        for entry in current_entries:
            if time.monotonic() >= deadline or stop_requested():
                return digest.hexdigest(), False
            entry_path = Path(entry.path)
            try:
                relative = entry_path.relative_to(workspace)
            except ValueError:
                return digest.hexdigest(), False
            if (
                _path_has_ignored_part(relative)
                or _path_is_ignored_fingerprint_artifact(relative)
                or _path_has_controller_internal_file(relative)
                or relative in ignored_references
            ):
                continue
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError:
                return digest.hexdigest(), False
            current_directory_entries.append(
                (
                    entry.name,
                    int(entry_stat.st_dev),
                    int(entry_stat.st_ino),
                    int(entry_stat.st_mode),
                )
            )
        if tuple(current_directory_entries) != observed_directory_entries.get(path):
            return digest.hexdigest(), False

    if ignored_references and ignored_references != _ignored_reference_artifact_directories(
        workspace,
        deadline=deadline,
        should_stop=stop_requested,
    ):
        return digest.hexdigest(), False
    if time.monotonic() >= deadline or stop_requested():
        return digest.hexdigest(), False
    digest.update(
        (
            f"files:{inspected_files}\0directories:{inspected_directories}"
            f"\0bytes:{inspected_bytes}"
        ).encode()
    )
    return digest.hexdigest(), True
