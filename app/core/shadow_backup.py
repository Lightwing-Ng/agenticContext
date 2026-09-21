"""One-way shadow cloud backup for the local cache.

Code version: v1.2.1-codex.0
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from functools import partial
import hashlib
import logging
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from threading import RLock, Thread

from .config import CrawlConfig, is_windows_host
from .job_lock import CacheTaskLock, SHARED_CACHE_TASK_LOCK
from .state import utc_now


logger = logging.getLogger(__name__)
SHADOW_BACKUP_COPY_WORKERS = 4
SHADOW_BACKUP_RUNTIME_LOCK_NAME = ".cache_task.lock"


class ShadowBackupError(RuntimeError):
    """Raised when a shadow backup cannot safely be completed."""


@dataclass(frozen=True, slots=True)
class ShadowBackupResult:
    """Summarize one completed shadow backup run."""

    copied_files: int = 0
    copied_bytes: int = 0
    unchanged_files: int = 0
    deleted_files: int = 0
    deleted_directories: int = 0

    def summary(self) -> str:
        """Return a concise status line suitable for the local web console."""
        copied_label = "file" if self.copied_files == 1 else "files"
        unchanged_label = "file" if self.unchanged_files == 1 else "files"
        parts = [
            f"copied {self.copied_files:,} {copied_label}",
            f"verified {self.unchanged_files:,} unchanged {unchanged_label}",
        ]
        if self.deleted_files or self.deleted_directories:
            deleted_label = "file" if self.deleted_files == 1 else "files"
            parts.append(
                f"removed {self.deleted_files:,} cloud-only {deleted_label} and "
                f"{self.deleted_directories:,} empty director{'y' if self.deleted_directories == 1 else 'ies'}"
            )
        return "; ".join(parts) + "."


def sync_shadow_backup(
    source_root: Path,
    destination_root: Path,
    *,
    mirror_deletions: bool,
) -> ShadowBackupResult:
    """Synchronize the source cache to its cloud shadow destination.

    The source is authoritative. Files are written atomically into the destination so
    OneDrive never observes a partially copied media file. Cloud-only files are kept
    unless ``mirror_deletions`` is explicitly enabled.
    """
    source_root, destination_root = _validate_backup_roots(source_root, destination_root)
    destination_root.mkdir(parents=True, exist_ok=True)

    source_files, source_directories = _scan_source_tree(source_root)
    copied_files = 0
    copied_bytes = 0
    unchanged_files = 0

    for relative_path in sorted(source_directories):
        destination_path = destination_root / relative_path
        if destination_path.is_symlink() or destination_path.is_file():
            if not mirror_deletions:
                raise ShadowBackupError(
                    f"Cloud destination contains a file where a source directory is required: {destination_path}"
                )
            destination_path.unlink()
        destination_path.mkdir(parents=True, exist_ok=True)

    copy_one_file = partial(
        _sync_one_source_file,
        destination_root=destination_root,
        mirror_deletions=mirror_deletions,
    )
    with ThreadPoolExecutor(
        max_workers=min(SHADOW_BACKUP_COPY_WORKERS, max(1, len(source_files))),
        thread_name_prefix="shadow-backup",
    ) as executor:
        for copied, copied_size, unchanged in executor.map(copy_one_file, source_files.items()):
            copied_files += copied
            copied_bytes += copied_size
            unchanged_files += unchanged

    deleted_files = 0
    deleted_directories = 0
    if mirror_deletions:
        deleted_files, deleted_directories = _remove_cloud_only_entries(
            destination_root,
            source_files=set(source_files),
            source_directories=source_directories,
        )

    return ShadowBackupResult(
        copied_files=copied_files,
        copied_bytes=copied_bytes,
        unchanged_files=unchanged_files,
        deleted_files=deleted_files,
        deleted_directories=deleted_directories,
    )


MACOS_DIRECTORY_PICKER_APPLESCRIPT = """
on restorePreviousApplication(previousFrontmostProcessName)
    if previousFrontmostProcessName is "" or previousFrontmostProcessName is "Finder" then return
    tell application "System Events"
        try
            set currentFrontmostProcessName to name of first application process whose frontmost is true
            if currentFrontmostProcessName is "Finder" then
                set frontmost of process previousFrontmostProcessName to true
            end if
        end try
    end tell
end restorePreviousApplication

on run argv
    set pickerPrompt to item 1 of argv
    set defaultPath to item 2 of argv
    set previousFrontmostProcessName to ""
    tell application "System Events"
        try
            set previousFrontmostProcessName to name of first application process whose frontmost is true
        end try
    end tell
    try
        tell application "Finder"
            activate
            set selectedFolder to choose folder with prompt pickerPrompt default location POSIX file defaultPath
        end tell
    on error errorMessage number errorNumber
        my restorePreviousApplication(previousFrontmostProcessName)
        error errorMessage number errorNumber
    end try
    my restorePreviousApplication(previousFrontmostProcessName)
    return POSIX path of selectedFolder
end run
""".strip()


def choose_settings_directory(initial_path: Path, prompt: str) -> Path | None:
    """Open the host-native folder picker and return the selected directory.

    ``None`` represents a user-cancelled dialog. The app only invokes this from its
    local Settings page, where the browser and filesystem belong to the same host.
    """
    default_location = _nearest_existing_directory(initial_path)
    if is_windows_host():
        try:
            from tkinter import Tk
            from tkinter.filedialog import askdirectory
        except ImportError as exc:
            raise ShadowBackupError("Windows could not load its folder picker.") from exc

        root = Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            selected_folder = askdirectory(
                parent=root,
                initialdir=str(default_location),
                title=prompt,
            )
        finally:
            root.destroy()
        return Path(selected_folder).expanduser().resolve(strict=False) if selected_folder else None

    completed = subprocess.run(
        [
            "/usr/bin/osascript",
            "-e",
            MACOS_DIRECTORY_PICKER_APPLESCRIPT,
            prompt,
            default_location.as_posix(),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0:
        selected_path = completed.stdout.strip()
        if selected_path:
            return Path(selected_path).expanduser().resolve(strict=False)
        return None

    error_text = (completed.stderr or "").strip()
    if "User canceled" in error_text or "-128" in error_text:
        return None
    raise ShadowBackupError(error_text or "macOS could not open the folder picker.")


def choose_shadow_backup_destination(initial_path: Path) -> Path | None:
    """Open the native picker for the shadow backup destination."""
    return choose_settings_directory(
        initial_path,
        "Select shadow cloud backup destination",
    )


class ShadowBackupService:
    """Run manually requested shadow backups without overlapping a cache task."""

    def __init__(self, source_root: Path, task_lock: CacheTaskLock | None = None) -> None:
        self._source_root = source_root
        self._task_lock = task_lock or SHARED_CACHE_TASK_LOCK
        self._lifecycle_lock = RLock()
        self._worker: Thread | None = None
        self._owns_task_lock = False
        self._snapshot = {
            "running": False,
            "phase": "idle",
            "message": "Shadow cloud backup has not run yet.",
            "destination": "",
            "last_synced_at": "",
            "copied_files": 0,
            "unchanged_files": 0,
            "deleted_files": 0,
        }

    def snapshot(self) -> dict[str, object]:
        """Return a copy of the current sync status."""
        with self._lifecycle_lock:
            return dict(self._snapshot)

    def is_running(self) -> bool:
        """Return whether a manual or automatic backup is active."""
        return bool(self.snapshot()["running"])

    def start(self, config: CrawlConfig) -> None:
        """Start a manual backup task after acquiring the shared cache lock."""
        if not config.shadow_backup_enabled:
            raise ShadowBackupError("Enable shadow cloud backup before starting a sync.")

        with self._lifecycle_lock:
            if self._snapshot["running"]:
                raise ShadowBackupError("A shadow cloud backup is already running.")
            if not self._task_lock.acquire("shadow-cloud-backup"):
                raise ShadowBackupError(
                    "A cache task is active. Start the shadow cloud backup after it finishes."
                )

            self._owns_task_lock = True
            self._set_snapshot(
                running=True,
                phase="syncing",
                message="Starting shadow cloud backup.",
                destination=str(config.shadow_backup_destination),
            )
            try:
                self._worker = Thread(target=self._run_manual, args=(config,), daemon=True)
                self._worker.start()
            except Exception:
                self._owns_task_lock = False
                self._task_lock.release()
                raise

    def sync_after_cache_task(self, config: CrawlConfig) -> str | None:
        """Synchronize after a successful cache task when the user enabled it."""
        if not (config.shadow_backup_enabled and config.shadow_backup_auto_sync):
            return None

        result = self._perform_sync(config)
        if result is None:
            return f"Shadow cloud backup failed: {self.snapshot()['message']}"
        return f"Shadow cloud backup {result.summary()}"

    def record_start_error(self, error: Exception) -> None:
        """Expose a rejected manual sync request in the Settings status area."""
        self._set_snapshot(running=False, phase="failed", message=str(error))

    def _run_manual(self, config: CrawlConfig) -> None:
        try:
            self._perform_sync(config)
        finally:
            with self._lifecycle_lock:
                if self._owns_task_lock:
                    self._owns_task_lock = False
                    self._task_lock.release()

    def _perform_sync(self, config: CrawlConfig) -> ShadowBackupResult | None:
        self._set_snapshot(
            running=True,
            phase="syncing",
            message="Synchronizing local cache to the shadow cloud backup.",
            destination=str(config.shadow_backup_destination),
        )
        try:
            result = sync_shadow_backup(
                self._source_root,
                config.shadow_backup_destination,
                mirror_deletions=config.shadow_backup_mirror_deletions,
            )
        except (OSError, ShadowBackupError) as exc:
            logger.exception("Shadow cloud backup failed.")
            self._set_snapshot(
                running=False,
                phase="failed",
                message=str(exc),
                destination=str(config.shadow_backup_destination),
            )
            return None

        self._set_snapshot(
            running=False,
            phase="finished",
            message=f"Shadow cloud backup complete: {result.summary()}",
            destination=str(config.shadow_backup_destination),
            last_synced_at=utc_now(),
            copied_files=result.copied_files,
            unchanged_files=result.unchanged_files,
            deleted_files=result.deleted_files,
        )
        logger.info(
            "Shadow cloud backup finished.",
            extra={"destination": str(config.shadow_backup_destination), **asdict(result)},
        )
        return result

    def _set_snapshot(self, **updates: object) -> None:
        with self._lifecycle_lock:
            self._snapshot.update(updates)


def _validate_backup_roots(source_root: Path, destination_root: Path) -> tuple[Path, Path]:
    """Resolve and validate the two roots before touching the cloud destination."""
    source_root = source_root.expanduser().resolve(strict=False)
    destination_root = destination_root.expanduser().resolve(strict=False)
    if not source_root.is_dir():
        raise ShadowBackupError(f"Local cache source is unavailable: {source_root}")
    if destination_root.exists() and not destination_root.is_dir():
        raise ShadowBackupError(f"Shadow backup destination must be a folder: {destination_root}")
    if source_root == destination_root or source_root in destination_root.parents or destination_root in source_root.parents:
        raise ShadowBackupError("The shadow backup destination must be separate from the local cache source.")
    return source_root, destination_root


def _scan_source_tree(source_root: Path) -> tuple[dict[Path, Path], set[Path]]:
    """Collect regular source files and directories without following symlinks."""
    source_files: dict[Path, Path] = {}
    source_directories: set[Path] = set()
    for source_path in sorted(source_root.rglob("*")):
        if source_path == source_root / SHADOW_BACKUP_RUNTIME_LOCK_NAME:
            continue
        relative_path = source_path.relative_to(source_root)
        if source_path.is_symlink():
            raise ShadowBackupError(f"Symlinks are not supported in the local cache backup: {source_path}")
        if source_path.is_dir():
            source_directories.add(relative_path)
        elif source_path.is_file():
            source_files[relative_path] = source_path
        else:
            raise ShadowBackupError(f"Unsupported local cache entry: {source_path}")
    return source_files, source_directories


def _files_match(source_path: Path, destination_path: Path) -> bool:
    """Compare regular files by content, not just potentially lossy cloud timestamps."""
    if source_path.stat().st_size != destination_path.stat().st_size:
        return False
    return _file_digest(source_path) == _file_digest(destination_path)


def _sync_one_source_file(
    item: tuple[Path, Path],
    *,
    destination_root: Path,
    mirror_deletions: bool,
) -> tuple[int, int, int]:
    """Synchronize one source file and return copied count, bytes, and unchanged count."""
    relative_path, source_path = item
    destination_path = destination_root / relative_path
    if destination_path.is_symlink():
        if not mirror_deletions:
            raise ShadowBackupError(
                f"Cloud destination contains a symlink where a source file is required: {destination_path}"
            )
        destination_path.unlink()
    if destination_path.exists() and destination_path.is_dir():
        if not mirror_deletions:
            raise ShadowBackupError(
                f"Cloud destination contains a directory where a source file is required: {destination_path}"
            )
        shutil.rmtree(destination_path)

    if destination_path.is_file() and _files_match(source_path, destination_path):
        return 0, 0, 1

    _copy_file_atomically(source_path, destination_path)
    return 1, source_path.stat().st_size, 0


def _file_digest(path: Path) -> bytes:
    """Return a streaming SHA-256 digest for one cache file."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1_048_576), b""):
            digest.update(chunk)
    return digest.digest()


def _copy_file_atomically(source_path: Path, destination_path: Path) -> None:
    """Copy one source file through a sibling temporary file before replacement."""
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination_path.name}.cachelikes-",
        suffix=".tmp",
        dir=destination_path.parent,
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    try:
        shutil.copy2(source_path, temporary_path)
        os.replace(temporary_path, destination_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _remove_cloud_only_entries(
    destination_root: Path,
    *,
    source_files: set[Path],
    source_directories: set[Path],
) -> tuple[int, int]:
    """Delete cloud-only entries only after all source files have been copied."""
    deleted_files = 0
    deleted_directories = 0
    destination_entries = sorted(
        destination_root.rglob("*"),
        key=lambda path: (len(path.relative_to(destination_root).parts), str(path)),
        reverse=True,
    )
    for destination_path in destination_entries:
        relative_path = destination_path.relative_to(destination_root)
        if destination_path.is_symlink() or destination_path.is_file():
            if relative_path not in source_files:
                destination_path.unlink()
                deleted_files += 1
            continue
        if destination_path.is_dir() and relative_path not in source_directories:
            destination_path.rmdir()
            deleted_directories += 1
    return deleted_files, deleted_directories


def _nearest_existing_directory(path: Path) -> Path:
    """Find a safe initial location for the native folder chooser."""
    candidate = path.expanduser().resolve(strict=False)
    for directory in (candidate, *candidate.parents):
        if directory.is_dir():
            return directory
    return Path("/")
