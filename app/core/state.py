"""Shared task state for the web UI and worker."""

# Code version: v1.6.0-codex.0

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from .cache_catalog import summarize_local_store_root
from .config import LOCAL_STORE_ROOT, X_LOCAL_STORE_DIRNAME
from .x_text_history import XTextHistoryStore, x_text_history_path


DEFAULT_OUTPUT_DIR_TEMPLATE = str(LOCAL_STORE_ROOT / X_LOCAL_STORE_DIRNAME)


def utc_now() -> str:
    """Return an ISO formatted UTC timestamp."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_initial_snapshot(version: str) -> TaskSnapshot:
    """Hydrate the initial X idle snapshot from the fixed local cache directory."""
    snapshot = TaskSnapshot(version=version)
    try:
        snapshot.cached_text_posts = XTextHistoryStore(
            x_text_history_path(LOCAL_STORE_ROOT)
        ).cached_posts
    except RuntimeError as exc:
        snapshot.last_error = str(exc)
    summaries = summarize_local_store_root(LOCAL_STORE_ROOT)
    x_summary = next((summary for summary in summaries if summary.account_name == X_LOCAL_STORE_DIRNAME), None)
    downloaded_posts = x_summary.downloaded_posts if x_summary is not None else 0
    downloaded_images = x_summary.downloaded_images if x_summary is not None else 0
    downloaded_videos = x_summary.downloaded_videos if x_summary is not None else 0

    if (
        downloaded_posts == 0
        and downloaded_images == 0
        and downloaded_videos == 0
        and snapshot.cached_text_posts == 0
    ):
        return snapshot

    snapshot.account_name = X_LOCAL_STORE_DIRNAME
    snapshot.output_dir = str(LOCAL_STORE_ROOT / X_LOCAL_STORE_DIRNAME)
    snapshot.downloaded_posts = downloaded_posts
    snapshot.downloaded_images = downloaded_images
    snapshot.downloaded_videos = downloaded_videos
    snapshot.downloaded_tweets = downloaded_images + downloaded_videos
    snapshot.message = (
        f"Ready. Found existing cache: {downloaded_posts:,} media posts, "
        f"{snapshot.cached_text_posts:,} text posts, "
        f"{downloaded_images:,} images, {downloaded_videos:,} videos."
    )
    return snapshot


def build_x_text_snapshot(version: str, local_store_root: Path | str | None = None) -> TaskSnapshot:
    """Hydrate X text independently of media files and media counters."""
    history_path = x_text_history_path(LOCAL_STORE_ROOT if local_store_root is None else local_store_root)
    snapshot = TaskSnapshot(
        version=version,
        output_dir=str(history_path.parent),
        performance_metrics={"content_mode": "text"},
    )
    try:
        snapshot.cached_text_posts = XTextHistoryStore(history_path).cached_posts
    except RuntimeError as exc:
        snapshot.last_error = str(exc)
        return snapshot
    if snapshot.cached_text_posts:
        snapshot.message = f"Ready. Found existing X text cache: {snapshot.cached_text_posts:,} liked posts."
    return snapshot


@dataclass(slots=True)
class TaskSnapshot:
    """Serializable task state."""

    version: str
    running: bool = False
    phase: str = "idle"
    message: str = "Ready."
    account_name: str = ""
    started_at: str = ""
    finished_at: str = ""
    discovered_tweets: int = 0
    discovered_images: int = 0
    queued_tweets: int = 0
    processed_tweets: int = 0
    progress_unit: str = "items"
    discovery_complete: bool = False
    downloaded_tweets: int = 0
    downloaded_posts: int = 0
    downloaded_images: int = 0
    downloaded_videos: int = 0
    cached_text_posts: int = 0
    skipped_tweets: int = 0
    failed_tweets: int = 0
    task_failures: int = 0
    output_dir: str = DEFAULT_OUTPUT_DIR_TEMPLATE
    last_error: str = ""
    recent_events: list[str] = field(default_factory=list)
    performance_metrics: dict[str, Any] = field(default_factory=dict)


class TaskState:
    """Thread-safe state container."""

    def __init__(self, version: str, snapshot_factory: Callable[[str], TaskSnapshot] | None = None) -> None:
        self._lock = Lock()
        self._snapshot = snapshot_factory(version) if snapshot_factory is not None else build_initial_snapshot(version)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return asdict(self._snapshot)

    def replace_snapshot(self, snapshot: TaskSnapshot) -> None:
        """Replace the entire snapshot atomically."""
        with self._lock:
            self._snapshot = snapshot

    def reset_for_run(self) -> None:
        with self._lock:
            version = self._snapshot.version
            self._snapshot = TaskSnapshot(
                version=version,
                running=True,
                phase="starting",
                message="Initializing job.",
                started_at=utc_now(),
            )

    def update(self, **kwargs: Any) -> None:
        with self._lock:
            for key, value in kwargs.items():
                setattr(self._snapshot, key, value)

    def append_event(self, message: str) -> None:
        with self._lock:
            self._snapshot.recent_events.append(f"[{utc_now()}] {message}")
            self._snapshot.recent_events = self._snapshot.recent_events[-50:]
            self._snapshot.message = message

    def finish_success(self, message: str) -> None:
        with self._lock:
            self._snapshot.running = False
            self._snapshot.phase = "finished"
            self._snapshot.message = message
            self._snapshot.finished_at = utc_now()

    def finish_error(self, message: str) -> None:
        with self._lock:
            self._snapshot.running = False
            self._snapshot.phase = "failed"
            self._snapshot.message = message
            self._snapshot.last_error = message
            self._snapshot.task_failures = 1
            self._snapshot.finished_at = utc_now()

    def finish_stopped(self, message: str) -> None:
        with self._lock:
            self._snapshot.running = False
            self._snapshot.phase = "stopped"
            self._snapshot.message = message
            self._snapshot.finished_at = utc_now()
