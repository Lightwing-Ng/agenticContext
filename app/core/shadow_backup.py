"""One-way shadow cloud backup for the local cache.

Code version: v1.3.0-codex.0
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


class SettingsDirectoryBrowserError(RuntimeError):
    """Raised when the local Settings directory browser cannot read a location."""


@dataclass(frozen=True, slots=True)
class SettingsDirectoryEntry:
    """Describe one child directory without reading any file content."""

    name: str
    path: Path
    is_symlink: bool = False
    accessible: bool = True
    reason: str = ""


@dataclass(frozen=True, slots=True)
class SettingsDirectoryListing:
    """Represent one canonical directory and its immediate child directories."""

    path: Path
    parent: Path | None
    breadcrumbs: tuple[tuple[str, Path], ...]
    directories: tuple[SettingsDirectoryEntry, ...]
    recovered_from: str = ""


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


def browse_settings_directory(
    requested_path: Path,
    *,
    fallback_path: Path,
    recover_invalid: bool = False,
) -> SettingsDirectoryListing:
    """List one local directory for the in-page Settings folder browser.

    The result contains directory names and canonical paths only. It never reads file
    content, changes the selected Agent workspace, or widens workspace authorization.
    """
    expanded = requested_path.expanduser()
    recovered_from = ""
    if not expanded.is_absolute():
        if not recover_invalid:
            raise SettingsDirectoryBrowserError("The directory path must be absolute.")
        recovered_from = str(requested_path)
        expanded = fallback_path.expanduser()

    try:
        directory = _resolve_browsable_directory(expanded)
    except SettingsDirectoryBrowserError:
        if not recover_invalid:
            raise
        recovered_from = str(requested_path)
        directory = _nearest_browsable_directory(expanded, fallback_path)

    try:
        children = tuple(directory.iterdir())
    except PermissionError as exc:
        raise SettingsDirectoryBrowserError("Permission denied for this directory.") from exc
    except OSError as exc:
        raise SettingsDirectoryBrowserError(str(exc)[:200]) from exc

    entries: list[SettingsDirectoryEntry] = []
    access_mode = os.R_OK | (0 if is_windows_host() else os.X_OK)
    for child in children:
        is_symlink = child.is_symlink()
        try:
            resolved = child.resolve(strict=True)
        except (OSError, RuntimeError):
            if is_symlink:
                entries.append(
                    SettingsDirectoryEntry(
                        name=child.name,
                        path=child.absolute(),
                        is_symlink=True,
                        accessible=False,
                        reason="The symbolic link target is unavailable.",
                    )
                )
            continue
        try:
            is_directory = resolved.is_dir()
        except OSError:
            is_directory = False
        if not is_directory:
            continue
        accessible = os.access(resolved, access_mode)
        entries.append(
            SettingsDirectoryEntry(
                name=child.name,
                path=resolved,
                is_symlink=is_symlink,
                accessible=accessible,
                reason="" if accessible else "Permission denied.",
            )
        )

    entries.sort(key=lambda item: (item.name.casefold(), item.name))
    parent = None if directory.parent == directory else directory.parent
    return SettingsDirectoryListing(
        path=directory,
        parent=parent,
        breadcrumbs=_directory_breadcrumbs(directory),
        directories=tuple(entries),
        recovered_from=recovered_from,
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


def _resolve_browsable_directory(path: Path) -> Path:
    """Resolve one existing, readable directory without guessing its identity."""
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise SettingsDirectoryBrowserError("The directory no longer exists.") from exc
    except (OSError, RuntimeError) as exc:
        raise SettingsDirectoryBrowserError(str(exc)[:200]) from exc
    if not resolved.is_dir():
        raise SettingsDirectoryBrowserError("The path is not a directory.")
    access_mode = os.R_OK | (0 if is_windows_host() else os.X_OK)
    if not os.access(resolved, access_mode):
        raise SettingsDirectoryBrowserError("Permission denied for this directory.")
    return resolved


def _nearest_browsable_directory(path: Path, fallback_path: Path) -> Path:
    """Recover an invalid initial path to its nearest readable existing parent."""
    candidates: list[Path] = []
    for starting_path in (path, fallback_path.expanduser(), Path.home(), Path.cwd()):
        try:
            candidate = starting_path.resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        candidates.extend((candidate, *candidate.parents))
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            return _resolve_browsable_directory(candidate)
        except SettingsDirectoryBrowserError:
            continue
    raise SettingsDirectoryBrowserError("No readable parent directory is available.")


def _directory_breadcrumbs(path: Path) -> tuple[tuple[str, Path], ...]:
    """Build root-to-current path navigation without platform-specific parsing in JavaScript."""
    directories = tuple(reversed((path, *path.parents)))
    return tuple(
        (
            directory.name or directory.anchor or str(directory),
            directory,
        )
        for directory in directories
    )
