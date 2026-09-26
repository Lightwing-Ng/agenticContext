"""Download media from tweet URLs with yt-dlp."""

# Code version: v1.11.0-codex.0

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .browser_sessions import browser_descriptors
from .cache_catalog import LocalTweetCacheIndex
from .config import CrawlConfig, is_windows_host
from .local_media_browser import BrowserDeletionCatalog
from .state import TaskState
from .x_photo_downloader import (
    PLAN_FILENAME,
    XMediaPost,
    XPhotoDownloadInterrupted,
    download_x_photos,
    photo_plan_fingerprint,
    photo_plan_is_complete,
    read_photo_plan,
    safe_photo_directory,
    validate_media_post,
    write_photo_plan,
)


MEDIA_MARKER_PREFIX = "__CACHELIKES_MEDIA__:"
METADATA_MARKER_PREFIX = "__CACHELIKES_X_METADATA__:"
SUCCESS_SKIP_MARKERS = (
    "has already been recorded in the archive",
    "has already been downloaded",
    "already exists",
    "file already exists",
    "not overwriting",
    "has been downloaded",
)
MISSING_MEDIA_SKIP_MARKERS = (
    "no video could be found in this tweet",
)
UNSUPPORTED_EXTERNAL_URL_MARKERS = (
    "unsupported url:",
)
NOT_FOUND_SKIP_MARKERS = (
    "http error 404",
    "404: not found",
    "unable to download webpage: http error 404",
)
SUSPENDED_SKIP_MARKERS = (
    ": suspended",
)
TRANSIENT_ERROR_MARKERS = (
    "timed out",
    "remote end closed connection without response",
    "transporterror(",
    "proxyerror(",
    "tunnel connection failed",
    "service unavailable",
)
CONFLICT_ERROR_MARKERS = (
    "file exists",
    "already exists",
    "unable to rename",
    "cannot move file",
    "not overwriting",
)
MAX_FILE_SIZE_SKIP_MARKERS = (
    "max-filesize",
    "file is larger than",
    "filesize is larger than",
)
DOWNLOAD_RETRY_ATTEMPTS = 3
DOWNLOAD_RETRY_DELAY_SECONDS = 1.5
YT_DLP_TWEET_TIMEOUT_SECONDS = 1_800.0
YT_DLP_STOP_POLL_SECONDS = 0.25
YT_DLP_TERMINATE_GRACE_SECONDS = 5.0
YT_DLP_VERSION_TIMEOUT_SECONDS = 10.0
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DownloadResult:
    """Capture the outcome for a single tweet download."""

    downloaded_media_count: int = 0
    downloaded_post_count: int = 0
    downloaded_image_count: int = 0
    downloaded_video_count: int = 0
    skipped: bool = False
    skipped_oversized_media_count: int = 0
    stopped: bool = False
    timed_out: bool = False


class YtDlpInterrupted(RuntimeError):
    """Carry completed yt-dlp output when this task stops or times out."""

    def __init__(self, reason: str, stdout: str = "", stderr: str = "") -> None:
        super().__init__("X media download stopped." if reason == "stopped" else "X media download timed out.")
        self.reason = reason
        self.stdout = stdout
        self.stderr = stderr


IMAGE_SUFFIXES = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
}
VIDEO_SUFFIXES = {
    ".mp4",
    ".mov",
    ".m4v",
    ".webm",
    ".mkv",
}


def parse_downloaded_paths(command_output: str) -> list[Path]:
    """Extract local file paths reported by yt-dlp after successful writes."""
    downloaded_paths: list[Path] = []
    for line in command_output.splitlines():
        if line.startswith(MEDIA_MARKER_PREFIX):
            downloaded_paths.append(Path(line.removeprefix(MEDIA_MARKER_PREFIX)))
    return downloaded_paths


def parse_download_metadata(command_output: str) -> list[dict[str, object]]:
    """Extract yt-dlp metadata objects emitted after each completed media move."""
    metadata_rows: list[dict[str, object]] = []
    for line in command_output.splitlines():
        if not line.startswith(METADATA_MARKER_PREFIX):
            continue
        try:
            payload = json.loads(line.removeprefix(METADATA_MARKER_PREFIX))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            metadata_rows.append(payload)
    return metadata_rows


def metadata_for_downloaded_path(
    downloaded_path: Path,
    metadata_rows: list[dict[str, object]],
) -> dict[str, object]:
    """Match one emitted metadata object to a downloaded media directory."""
    target_directory = downloaded_path.parent.resolve(strict=False)
    for row in metadata_rows:
        filepath = str(row.get("filepath") or row.get("_filename") or "").strip()
        if filepath and Path(filepath).parent.resolve(strict=False) == target_directory:
            return row
    directory_id = downloaded_path.parent.name
    for row in metadata_rows:
        if str(row.get("display_id") or row.get("id") or "").strip() == directory_id:
            return row
    return metadata_rows[0] if len(metadata_rows) == 1 else {}


def count_downloaded_media_types(downloaded_paths: list[Path]) -> tuple[int, int]:
    """Return image and video counts from downloaded output paths."""
    image_count = 0
    video_count = 0
    for path in downloaded_paths:
        suffix = path.suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            image_count += 1
        elif suffix in VIDEO_SUFFIXES:
            video_count += 1
    return image_count, video_count


def is_successful_skip_output(command_output: str) -> bool:
    """Return whether yt-dlp reported a no-op success that should be counted as skipped."""
    lowered = command_output.lower()
    return any(marker in lowered for marker in SUCCESS_SKIP_MARKERS)


def is_existing_file_conflict(command_output: str) -> bool:
    """Return whether the failure looks like a local file collision."""
    lowered = command_output.lower()
    return any(marker in lowered for marker in CONFLICT_ERROR_MARKERS)


def is_max_file_size_skip_output(command_output: str) -> bool:
    """Return whether yt-dlp rejected media because it exceeded the configured limit."""
    lowered = command_output.lower()
    return any(marker in lowered for marker in MAX_FILE_SIZE_SKIP_MARKERS)


def discard_oversized_downloads(
    downloaded_paths: list[Path],
    max_file_size_bytes: int,
) -> tuple[list[Path], list[Path]]:
    """Remove newly written media files above the universal cache size limit."""
    if max_file_size_bytes <= 0:
        return downloaded_paths, []

    accepted_paths: list[Path] = []
    oversized_paths: list[Path] = []
    for downloaded_path in downloaded_paths:
        try:
            is_oversized = downloaded_path.is_file() and downloaded_path.stat().st_size > max_file_size_bytes
        except OSError:
            is_oversized = False
        if not is_oversized:
            accepted_paths.append(downloaded_path)
            continue
        try:
            downloaded_path.unlink()
        except OSError as exc:
            raise RuntimeError(
                f"Unable to remove oversized X download {downloaded_path} above the "
                f"{max_file_size_bytes:,}-byte cache limit."
            ) from exc
        oversized_paths.append(downloaded_path)
    return accepted_paths, oversized_paths


def is_missing_media_skip_output(command_output: str) -> bool:
    """Return whether yt-dlp reported no downloadable media for the tweet."""
    lowered = command_output.lower()
    return any(marker in lowered for marker in MISSING_MEDIA_SKIP_MARKERS)


def is_unsupported_external_url_skip_output(command_output: str) -> bool:
    """Return whether yt-dlp followed a tweet external link that we do not support."""
    lowered = command_output.lower()
    if not any(marker in lowered for marker in UNSUPPORTED_EXTERNAL_URL_MARKERS):
        return False

    return "unsupported url: https://x.com/" not in lowered and "unsupported url: https://twitter.com/" not in lowered


def is_not_found_skip_output(command_output: str) -> bool:
    """Return whether the tweet target is no longer available."""
    lowered = command_output.lower()
    return any(marker in lowered for marker in NOT_FOUND_SKIP_MARKERS)


def is_suspended_skip_output(command_output: str) -> bool:
    """Return whether the tweet or account is suspended and cannot be downloaded."""
    lowered = command_output.lower()
    return any(marker in lowered for marker in SUSPENDED_SKIP_MARKERS)


def is_transient_retryable_output(command_output: str) -> bool:
    """Return whether the error looks transient enough to retry briefly."""
    lowered = command_output.lower()
    return any(marker in lowered for marker in TRANSIENT_ERROR_MARKERS)


def resolve_yt_dlp_command() -> list[str]:
    """Return the preferred yt-dlp invocation for the current environment."""
    module_command = [sys.executable, "-m", "yt_dlp"]
    probe = subprocess.run(
        module_command + ["--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=YT_DLP_VERSION_TIMEOUT_SECONDS,
    )
    if probe.returncode == 0:
        return module_command

    binary = shutil.which("yt-dlp")
    if binary:
        return [binary]

    if is_windows_host():
        raise RuntimeError(
            "yt-dlp is not installed. Run `py -3.13 -m pip install -r requirements.txt`."
        )
    raise RuntimeError(
        "yt-dlp is not installed. Run `python3 -m pip install -r requirements.txt` "
        "or `brew install yt-dlp`."
    )


def ensure_yt_dlp_available() -> list[str]:
    """Raise a clear error when yt-dlp is unavailable."""
    try:
        return resolve_yt_dlp_command()
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("yt-dlp version check timed out after 10 seconds.") from exc
    except RuntimeError as exc:
        if is_windows_host():
            raise RuntimeError(
                "yt-dlp is not installed. Run `py -3.13 -m pip install -r requirements.txt`."
            ) from exc
        raise RuntimeError(
            "yt-dlp is not installed. Run `python3 -m pip install -r requirements.txt` "
            "or `brew install yt-dlp`."
        ) from exc


def build_cookies_from_browser_arg(config: CrawlConfig) -> str:
    """Match yt-dlp's browser cookies source to the selected X browser."""
    selected_browser = str(config.x_browser or "").strip().lower()
    # yt-dlp accepts Safari as a cookie backend even when the host-aware browser
    # registry does not expose Safari for automation on this platform. The
    # collection and browser-session paths still validate Safari availability
    # through browser_descriptors before attempting to launch it.
    if selected_browser == "safari":
        return "safari"

    descriptor = browser_descriptors(config).get(selected_browser)
    if descriptor is None:
        raise RuntimeError(f"Unsupported X browser: {config.x_browser}")
    if descriptor.engine == "safari":
        return "safari"
    if descriptor.user_data_dir is None:
        raise RuntimeError(f"{descriptor.label} does not expose a supported cookie source.")
    profile_path = descriptor.user_data_dir / descriptor.profile_directory
    return f"{descriptor.browser_id}:{profile_path}"


def _terminate_yt_dlp_process(process: subprocess.Popen[str]) -> tuple[str, str]:
    """Stop only this task's subprocess group and collect any completed output."""

    if os.name == "posix":
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    elif process.poll() is None:
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        except (AttributeError, OSError):
            process.terminate()
    try:
        return process.communicate(timeout=YT_DLP_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        else:
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                    capture_output=True,
                    check=False,
                    timeout=YT_DLP_TERMINATE_GRACE_SECONDS,
                )
            with contextlib.suppress(OSError):
                process.kill()
        return process.communicate(timeout=YT_DLP_TERMINATE_GRACE_SECONDS)


def _run_yt_dlp_attempt(
    command: list[str],
    deadline: float,
    should_stop: Callable[[], bool],
) -> subprocess.CompletedProcess[str]:
    """Poll a task-owned yt-dlp process so Stop and the deadline can interrupt it."""

    if should_stop():
        raise YtDlpInterrupted("stopped")
    if time.monotonic() >= deadline:
        raise YtDlpInterrupted("timed_out")
    kwargs: dict[str, object] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True
    elif os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(command, **kwargs)
    while True:
        reason = "stopped" if should_stop() else "timed_out" if time.monotonic() >= deadline else ""
        if reason:
            stdout, stderr = _terminate_yt_dlp_process(process)
            raise YtDlpInterrupted(reason, stdout, stderr)
        try:
            stdout, stderr = process.communicate(
                timeout=min(YT_DLP_STOP_POLL_SECONDS, max(0.001, deadline - time.monotonic()))
            )
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            continue


def run_yt_dlp_with_retries(
    command: list[str],
    tweet_url: str,
    *,
    should_stop: Callable[[], bool] | None = None,
    timeout_seconds: float = YT_DLP_TWEET_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run yt-dlp with a bounded total deadline and cooperative Stop."""

    attempt = 0
    last_result: subprocess.CompletedProcess[str] | None = None
    previous_stdout: list[str] = []
    previous_stderr: list[str] = []
    stop_requested = should_stop or (lambda: False)
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))

    while attempt < DOWNLOAD_RETRY_ATTEMPTS:
        attempt += 1
        try:
            result = _run_yt_dlp_attempt(command, deadline, stop_requested)
        except YtDlpInterrupted as exc:
            raise YtDlpInterrupted(
                exc.reason,
                "\n".join([*previous_stdout, exc.stdout]),
                "\n".join([*previous_stderr, exc.stderr]),
            ) from exc
        last_result = result
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        combined = "\n".join(part for part in (stdout.strip(), stderr.strip()) if part).strip()

        if result.returncode == 0 or not is_transient_retryable_output(combined) or attempt >= DOWNLOAD_RETRY_ATTEMPTS:
            return subprocess.CompletedProcess(
                command,
                result.returncode,
                "\n".join([*previous_stdout, stdout]),
                stderr,
            )

        previous_stdout.append(stdout)
        previous_stderr.append(stderr)

        logger.warning(
            "Retrying yt-dlp after transient failure.",
            extra={
                "tweet_url": tweet_url,
                "attempt": attempt,
                "max_attempts": DOWNLOAD_RETRY_ATTEMPTS,
                "command_output_excerpt": combined[:2_000],
            },
        )
        retry_deadline = min(deadline, time.monotonic() + DOWNLOAD_RETRY_DELAY_SECONDS)
        while True:
            if stop_requested():
                raise YtDlpInterrupted("stopped", "\n".join(previous_stdout), "\n".join(previous_stderr))
            remaining_delay = retry_deadline - time.monotonic()
            if remaining_delay <= 0:
                break
            time.sleep(min(YT_DLP_STOP_POLL_SECONDS, remaining_delay))

    if last_result is None:
        raise RuntimeError(f"yt-dlp did not execute for {tweet_url}")
    return last_result


class _PendingXCacheIndex:
    """Persist completed video bindings while the plan marker keeps the post pending."""

    def __init__(self, cache: LocalTweetCacheIndex, tweet_url: str) -> None:
        self.cache = cache
        self.completed_successfully = False
        self.budget_stopped = False
        self.archive_path: Path | None = None
        self.existing_paths: set[Path] = set()
        self.archive_video_paths: set[Path] = set()
        root = cache.output_dir.resolve(strict=False)
        for directory in cache.lookup_directories(tweet_url):
            if (
                directory.is_symlink()
                or not directory.is_dir()
                or not directory.resolve().is_relative_to(root)
                or any(parent.is_symlink() for parent in directory.parents)
            ):
                continue
            for path in directory.iterdir():
                if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
                    continue
                if path.suffix.lower() in IMAGE_SUFFIXES | VIDEO_SUFFIXES:
                    self.existing_paths.add(path.resolve())
                # The fixed output template binds both names to the extractor media ID.
                if path.suffix.lower() in VIDEO_SUFFIXES and path.stem == directory.name and path.stem.isdigit():
                    self.archive_video_paths.add(path)

    def prepare_archive(self, directory: Path) -> None:
        """Exclude only completed, bound media before yt-dlp counts its download budget."""
        entries = sorted({f"twitter {path.stem}" for path in self.archive_video_paths})
        if len(entries) > 256:
            raise RuntimeError("X video retry archive exceeds the per-post bound.")
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, prefix=".x-video-archive-", delete=False) as stream:
            self.archive_path = Path(stream.name)
            stream.write("".join(f"{entry}\n" for entry in entries))

    def close(self) -> None:
        if self.archive_path is not None:
            self.archive_path.unlink(missing_ok=True)

    def claim(self, _tweet_url: str) -> bool:
        return True

    def release_claim(self, _tweet_url: str) -> None:
        return None

    def contains_complete_cache(self, tweet_url: str) -> bool:
        return self.cache.contains_complete_cache(tweet_url)

    def register(self, tweet_url: str, tweet_dir: Path | None = None, metadata: dict | None = None) -> None:
        if tweet_dir is not None:
            root = self.cache.output_dir.resolve(strict=False)
            if (
                tweet_dir.is_symlink()
                or not tweet_dir.is_dir()
                or not tweet_dir.resolve().is_relative_to(root)
                or any(parent.is_symlink() for parent in tweet_dir.parents)
            ):
                raise RuntimeError("Unsafe X video cache directory.")
        self.cache.register(tweet_url, tweet_dir, metadata)


def _cached_video_directories(cache: LocalTweetCacheIndex, tweet_url: str, max_bytes: int) -> set[Path]:
    """Find finished video files for this status, including an interrupted prior run."""
    root = cache.output_dir.resolve(strict=False)
    status_id = tweet_url.rsplit("/", 1)[1]
    candidates = cache.lookup_directories(tweet_url)
    candidates.update(
        uploader / status_id
        for uploader in root.iterdir()
        if uploader.is_dir() and not uploader.is_symlink()
    )
    found: set[Path] = set()
    for directory in candidates:
        if directory.is_symlink() or not directory.is_dir() or not directory.resolve().is_relative_to(root):
            continue
        if any(parent.is_symlink() for parent in directory.parents):
            continue
        if any(
            path.suffix.lower() in VIDEO_SUFFIXES
            and not path.is_symlink()
            and path.is_file()
            and 0 < path.stat().st_size <= max_bytes
            for path in directory.iterdir()
        ):
            found.add(directory)
    return found


def download_tweet_media(
    tweet_url: str,
    output_dir: Path,
    config: CrawlConfig,
    state: TaskState,
    remaining_media_items: int | None = None,
    cache_index: LocalTweetCacheIndex | None = None,
    should_stop: Callable[[], bool] | None = None,
    timeout_seconds: float = YT_DLP_TWEET_TIMEOUT_SECONDS,
    *,
    media_post: XMediaPost | None = None,
) -> DownloadResult:
    """Download a bound photo plan and retain the existing yt-dlp video path."""
    plan = validate_media_post(media_post, tweet_url) if media_post is not None else None
    if plan is None or (not plan.photo_urls and not plan.has_video):
        return _download_tweet_media_legacy(
            tweet_url, output_dir, config, state, remaining_media_items,
            cache_index, should_stop, timeout_seconds,
        )
    if BrowserDeletionCatalog(output_dir.parent).is_excluded("x", tweet_url):
        state.append_event(f"Skipped removed X resource {tweet_url}")
        return DownloadResult(skipped=True)
    directory = safe_photo_directory(plan, output_dir)
    local_cache = cache_index or LocalTweetCacheIndex.build(output_dir)
    if not local_cache.claim(tweet_url, allow_cached=True):
        return DownloadResult(skipped=True)
    try:
        if should_stop is not None and should_stop():
            return DownloadResult(stopped=True)
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        fingerprint = photo_plan_fingerprint(plan, config.max_media_file_size_bytes)
        previously_complete = photo_plan_is_complete(directory, fingerprint)
        previous_plan = read_photo_plan(directory)
        videos = _cached_video_directories(local_cache, tweet_url, config.max_media_file_size_bytes) if plan.has_video else set()
        video_complete = bool(videos) and (
            not (directory / PLAN_FILENAME).exists()
            or (
                previous_plan.get("video_complete") is True
                and previous_plan.get("max_bytes") == config.max_media_file_size_bytes
            )
        )
        write_photo_plan(
            directory, fingerprint, complete=False, video_complete=video_complete,
            max_bytes=config.max_media_file_size_bytes,
        )
        try:
            photos = download_x_photos(
                plan, output_dir, config.max_media_file_size_bytes, should_stop, deadline,
                max_new_items=remaining_media_items,
            )
        except XPhotoDownloadInterrupted as exc:
            return DownloadResult(
                downloaded_media_count=exc.result.downloaded_count,
                downloaded_post_count=int(bool(exc.result.downloaded_count)),
                downloaded_image_count=exc.result.downloaded_count,
                skipped_oversized_media_count=exc.result.skipped_size,
                stopped=exc.reason == "stopped",
                timed_out=exc.reason == "timed_out",
            )
        result = DownloadResult(
            downloaded_media_count=photos.downloaded_count,
            downloaded_image_count=photos.downloaded_count,
            downloaded_post_count=int(bool(photos.downloaded_count)),
            skipped_oversized_media_count=photos.skipped_size,
        )
        photos_complete = len(photos.paths) + photos.skipped_size == len(plan.photo_urls)
        if not photos_complete:
            result.skipped = not bool(result.downloaded_media_count)
            return result
        pending = _PendingXCacheIndex(local_cache, tweet_url)
        if plan.has_video and not video_complete:
            remaining = None if remaining_media_items is None else remaining_media_items - photos.downloaded_count
            if remaining is not None and remaining <= 0:
                result.skipped = not bool(result.downloaded_media_count)
                return result
            try:
                pending.prepare_archive(directory)
                video_result = _download_tweet_media_legacy(
                    tweet_url, output_dir, config, state, remaining, pending,
                    should_stop, max(0.0, deadline - time.monotonic()),
                )
            finally:
                pending.close()
            video_result.skipped_oversized_media_count += sum(
                path.stat().st_size > config.max_media_file_size_bytes
                for path in pending.archive_video_paths
            )
            result.downloaded_media_count += video_result.downloaded_media_count
            result.downloaded_image_count += video_result.downloaded_image_count
            result.downloaded_video_count += video_result.downloaded_video_count
            result.downloaded_post_count = int(bool(result.downloaded_media_count))
            result.skipped_oversized_media_count += video_result.skipped_oversized_media_count
            result.stopped = video_result.stopped
            result.timed_out = video_result.timed_out
            if result.stopped or result.timed_out:
                return result
            videos = _cached_video_directories(local_cache, tweet_url, config.max_media_file_size_bytes)
            if pending.budget_stopped and videos and video_result.downloaded_video_count:
                return result
            if not pending.completed_successfully or (not videos and not video_result.skipped_oversized_media_count):
                raise RuntimeError("X media download did not complete the observed video.")
            video_complete = True
        for video_dir in videos:
            if video_dir not in local_cache.lookup_directories(tweet_url):
                local_cache.register(tweet_url, video_dir, {"_type": "video", "display_id": tweet_url.rsplit("/", 1)[1]})
        if photos.paths:
            local_cache.register(tweet_url, directory, {
                "_type": "image", "display_id": tweet_url.rsplit("/", 1)[1],
                "uploader_id": tweet_url.split("/")[3], "webpage_url": tweet_url,
            })
        write_photo_plan(
            directory, fingerprint, complete=True, video_complete=video_complete,
            max_bytes=config.max_media_file_size_bytes,
        )
        result.skipped = not bool(result.downloaded_media_count)
        if result.downloaded_media_count:
            state.append_event(f"Downloaded media for {tweet_url}")
        elif previously_complete:
            state.append_event(f"Skipped cached X media for {tweet_url}")
        return result
    finally:
        local_cache.release_claim(tweet_url)


def _download_tweet_media_legacy(
    tweet_url: str,
    output_dir: Path,
    config: CrawlConfig,
    state: TaskState,
    remaining_media_items: int | None = None,
    cache_index: LocalTweetCacheIndex | None = None,
    should_stop: Callable[[], bool] | None = None,
    timeout_seconds: float = YT_DLP_TWEET_TIMEOUT_SECONDS,
) -> DownloadResult:
    """Download media for one tweet URL."""
    output_dir.mkdir(parents=True, exist_ok=True)
    deletion_catalog = BrowserDeletionCatalog(output_dir.parent)
    if deletion_catalog.is_excluded("x", tweet_url):
        state.append_event(f"Skipped removed X resource {tweet_url}")
        logger.info(
            "Skipped X resource because it was removed from the local browser.",
            extra={"tweet_url": tweet_url, "output_dir": str(output_dir)},
        )
        return DownloadResult(skipped=True)

    local_cache = cache_index or LocalTweetCacheIndex.build(output_dir)
    if not local_cache.claim(tweet_url):
        state.append_event(f"Skipped cached or in-flight tweet {tweet_url}")
        logger.info(
            "Skipped tweet because a complete local cache exists or another worker already claimed it.",
            extra={
                "tweet_url": tweet_url,
                "output_dir": str(output_dir),
            },
        )
        return DownloadResult(skipped=True)

    try:
        if should_stop is not None and should_stop():
            return DownloadResult(stopped=True)
        yt_dlp_command = ensure_yt_dlp_available()

        command = yt_dlp_command + [
            "--cookies-from-browser",
            build_cookies_from_browser_arg(config),
            "--output",
            str(output_dir / "%(uploader_id|unknown_uploader)s" / "%(id)s" / "%(id)s.%(ext)s"),
            "--write-thumbnail",
            "--no-progress",
            "--restrict-filenames",
            "--no-overwrites",
            "--max-filesize",
            str(config.max_media_file_size_bytes),
            "--print",
            f"after_move:{MEDIA_MARKER_PREFIX}%(filepath)s",
            "--print",
            f"after_move:{METADATA_MARKER_PREFIX}%()j",
        ]
        if remaining_media_items is not None:
            command.extend(["--max-downloads", str(max(1, remaining_media_items))])
        if isinstance(local_cache, _PendingXCacheIndex) and local_cache.archive_path is not None:
            command.extend(["--download-archive", str(local_cache.archive_path)])
        command.append(tweet_url)

        logger.info(
            "Invoking yt-dlp for tweet media download.",
            extra={
                "tweet_url": tweet_url,
                "output_dir": str(output_dir),
                "remaining_media_items": remaining_media_items,
                "yt_dlp_command": yt_dlp_command,
            },
        )
        try:
            result = run_yt_dlp_with_retries(
                command,
                tweet_url,
                should_stop=should_stop,
                timeout_seconds=timeout_seconds,
            )
        except YtDlpInterrupted as exc:
            output_root = output_dir.resolve(strict=False)
            completed_paths = [
                path
                for path in parse_downloaded_paths(exc.stdout)
                if path.is_file() and path.resolve(strict=False).is_relative_to(output_root)
                and (not isinstance(local_cache, _PendingXCacheIndex) or path.resolve() not in local_cache.existing_paths)
            ]
            oversized_paths = [
                path for path in completed_paths
                if path.stat().st_size > config.max_media_file_size_bytes
            ]
            completed_paths = [path for path in completed_paths if path not in oversized_paths]
            metadata_rows = parse_download_metadata(exc.stdout)
            for completed_path in completed_paths:
                local_cache.register(
                    tweet_url,
                    completed_path.parent,
                    metadata_for_downloaded_path(completed_path, metadata_rows),
                )
            image_count, video_count = count_downloaded_media_types(completed_paths)
            state.append_event(
                f"X media download {exc.reason} for {tweet_url}; "
                f"retained {len(completed_paths):,} completed file(s)."
            )
            return DownloadResult(
                downloaded_media_count=len(completed_paths),
                downloaded_post_count=int(bool(completed_paths)),
                downloaded_image_count=image_count,
                downloaded_video_count=video_count,
                skipped_oversized_media_count=len(oversized_paths),
                stopped=exc.reason == "stopped",
                timed_out=exc.reason == "timed_out",
            )
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        combined = "\n".join(part for part in (stdout.strip(), stderr.strip()) if part).strip()
        if isinstance(local_cache, _PendingXCacheIndex):
            local_cache.completed_successfully = result.returncode == 0
            local_cache.budget_stopped = (
                result.returncode == 101
                and remaining_media_items is not None
                and "maximum number of downloads reached" in combined.lower()
                and "--max-downloads" in combined.lower()
            )
        downloaded_paths = parse_downloaded_paths(stdout)
        if isinstance(local_cache, _PendingXCacheIndex):
            downloaded_paths = [
                path for path in downloaded_paths
                if path.resolve(strict=False) not in local_cache.existing_paths
            ]
        metadata_rows = parse_download_metadata(stdout)
        downloaded_paths, oversized_paths = discard_oversized_downloads(
            downloaded_paths,
            config.max_media_file_size_bytes,
        )
        if result.returncode != 0 and isinstance(local_cache, _PendingXCacheIndex):
            for completed_path in downloaded_paths:
                if completed_path.is_file() and not completed_path.is_symlink() and completed_path.stat().st_size > 0:
                    local_cache.register(
                        tweet_url,
                        completed_path.parent,
                        metadata_for_downloaded_path(completed_path, metadata_rows),
                    )
            if local_cache.budget_stopped and downloaded_paths:
                image_count, video_count = count_downloaded_media_types(downloaded_paths)
                return DownloadResult(
                    downloaded_media_count=len(downloaded_paths),
                    downloaded_post_count=1,
                    downloaded_image_count=image_count,
                    downloaded_video_count=video_count,
                    skipped_oversized_media_count=len(oversized_paths),
                )
        if oversized_paths:
            state.append_event(
                f"Skipped {len(oversized_paths):,} X media file(s) above the {config.max_media_file_size_mib:,} MiB cache limit."
            )
            logger.info(
                "Skipped oversized X media files.",
                extra={
                    "tweet_url": tweet_url,
                    "max_file_size_bytes": config.max_media_file_size_bytes,
                    "oversized_paths": [str(path) for path in oversized_paths],
                },
            )

        if result.returncode == 0 or is_max_file_size_skip_output(combined):
            if downloaded_paths:
                image_count, video_count = count_downloaded_media_types(downloaded_paths)
                for downloaded_path in downloaded_paths:
                    local_cache.register(
                        tweet_url,
                        downloaded_path.parent,
                        metadata_for_downloaded_path(downloaded_path, metadata_rows),
                    )
                state.append_event(f"Downloaded media for {tweet_url}")
                logger.info(
                    "yt-dlp downloaded media successfully.",
                    extra={
                        "tweet_url": tweet_url,
                        "downloaded_media_count": len(downloaded_paths),
                        "downloaded_image_count": image_count,
                        "downloaded_video_count": video_count,
                        "downloaded_paths": [str(path) for path in downloaded_paths],
                    },
                )
                return DownloadResult(
                    downloaded_media_count=len(downloaded_paths),
                    downloaded_post_count=1,
                    downloaded_image_count=image_count,
                    downloaded_video_count=video_count,
                    skipped_oversized_media_count=len(oversized_paths),
                )

            if oversized_paths or is_max_file_size_skip_output(combined):
                return DownloadResult(
                    skipped=True,
                    skipped_oversized_media_count=len(oversized_paths) or 1,
                )

            if is_successful_skip_output(combined) or local_cache.contains_complete_cache(tweet_url):
                local_cache.register(tweet_url)
                state.append_event(f"Skipped already cached tweet {tweet_url}")
                logger.info(
                    "yt-dlp reported a cache hit or no-op success.",
                    extra={
                        "tweet_url": tweet_url,
                        "returncode": result.returncode,
                        "command_output_excerpt": combined[:2_000],
                    },
                )
                return DownloadResult(skipped=True)

            state.append_event(f"No new media files were produced for {tweet_url}")
            logger.warning(
                "yt-dlp succeeded but produced no new media files.",
                extra={
                    "tweet_url": tweet_url,
                    "returncode": result.returncode,
                    "command_output_excerpt": combined[:2_000],
                },
            )
            return DownloadResult(skipped=True)

        if is_existing_file_conflict(combined) and local_cache.contains_complete_cache(tweet_url):
            local_cache.register(tweet_url)
            state.append_event(f"Skipped existing local conflict for {tweet_url}")
            logger.warning(
                "Downgraded local file conflict to skip because cache is already complete.",
                extra={
                    "tweet_url": tweet_url,
                    "returncode": result.returncode,
                    "command_output_excerpt": combined[:2_000],
                },
            )
            return DownloadResult(skipped=True)

        if is_missing_media_skip_output(combined):
            local_cache.register(tweet_url)
            state.append_event(f"Skipped tweet with no downloadable media {tweet_url}")
            logger.info(
                "Downgraded missing media response to skip.",
                extra={
                    "tweet_url": tweet_url,
                    "returncode": result.returncode,
                    "command_output_excerpt": combined[:2_000],
                },
            )
            return DownloadResult(skipped=True)

        if is_unsupported_external_url_skip_output(combined):
            local_cache.register(tweet_url)
            state.append_event(f"Skipped unsupported external media target for {tweet_url}")
            logger.info(
                "Downgraded unsupported external URL response to skip.",
                extra={
                    "tweet_url": tweet_url,
                    "returncode": result.returncode,
                    "command_output_excerpt": combined[:2_000],
                },
            )
            return DownloadResult(skipped=True)

        if is_not_found_skip_output(combined):
            local_cache.register(tweet_url)
            state.append_event(f"Skipped unavailable tweet target for {tweet_url}")
            logger.info(
                "Downgraded missing remote tweet target to skip.",
                extra={
                    "tweet_url": tweet_url,
                    "returncode": result.returncode,
                    "command_output_excerpt": combined[:2_000],
                },
            )
            return DownloadResult(skipped=True)

        if is_suspended_skip_output(combined):
            local_cache.register(tweet_url)
            state.append_event(f"Skipped suspended tweet target for {tweet_url}")
            logger.info(
                "Downgraded suspended tweet target to skip.",
                extra={
                    "tweet_url": tweet_url,
                    "returncode": result.returncode,
                    "command_output_excerpt": combined[:2_000],
                },
            )
            return DownloadResult(skipped=True)

        if is_transient_retryable_output(combined):
            local_cache.register(tweet_url)
            state.append_event(f"Skipped transient network failure after retries for {tweet_url}")
            logger.warning(
                "Downgraded transient yt-dlp failure to skip after retry budget was exhausted.",
                extra={
                    "tweet_url": tweet_url,
                    "returncode": result.returncode,
                    "command_output_excerpt": combined[:2_000],
                    "retry_attempts": DOWNLOAD_RETRY_ATTEMPTS,
                },
            )
            return DownloadResult(skipped=True)

        logger.error(
            "yt-dlp failed for tweet media download.",
            extra={
                "tweet_url": tweet_url,
                "returncode": result.returncode,
                "stdout_excerpt": stdout[:2_000],
                "stderr_excerpt": stderr[:2_000],
            },
        )
        raise RuntimeError(f"yt-dlp failed for {tweet_url}: {combined}")
    finally:
        local_cache.release_claim(tweet_url)
