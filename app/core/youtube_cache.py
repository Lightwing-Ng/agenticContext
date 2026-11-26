"""Cache a single YouTube video or its audio in the local media store."""

# Code version: v1.4.1-codex.0

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, replace
from ntpath import isreserved
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, NotRequired, TypedDict
from urllib.parse import parse_qs, urlsplit
from urllib.request import getproxies

from .config import MEDIA_STORE_DIRNAME
from .downloader import (
    YtDlpInterrupted,
    ensure_yt_dlp_available,
    is_max_file_size_skip_output,
    run_yt_dlp_with_retries,
)


_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
_EXTENSIONS = {"video": "mp4", "audio": "m4a"}
_BAD_URL = "Enter a valid link to one YouTube video."
_STOPPED = "The YouTube download stopped."
_MAX_DOWNLOAD_SECONDS = 300.0
_MAX_METADATA_LOOKUP_SECONDS = 25.0
_METADATA_FILE = ".title.json"
_METADATA_TEMPLATE = "%(.{title,uploader,channel})j"
_MAX_FILENAME_BYTES = 255
_RECENT_MEDIA_LIMIT = 20
_FILENAME_SEPARATORS = str.maketrans({character: " " for character in '<>:"/\\|?*'})
_PROGRESS_PREFIX = "__CACHELIKES_YOUTUBE_PROGRESS__:"
_PROGRESS_LINE = re.compile(rf"^(?:[0-9]+: )?{_PROGRESS_PREFIX}(download|postprocess):")
_DOWNLOAD_PROGRESS_TEMPLATE = (
    f"download:{_PROGRESS_PREFIX}download:"
    "%(progress.{status,downloaded_bytes,total_bytes})j\t%(info.{vcodec,acodec})j"
)
_POSTPROCESS_PROGRESS_TEMPLATE = (
    f"postprocess:{_PROGRESS_PREFIX}postprocess:%(progress.{{status,postprocessor}})j"
)
_FILE_POSTPROCESSORS = {"ExtractAudio", "Merger", "FixupM3u8", "FixupM4a", "FixupTimestamp", "FixupDuration", "MoveFiles"}


class YouTubeCacheError(RuntimeError):
    """Carry a concise, user-facing cache error without downloader diagnostics."""


class YouTubeCacheProgress(TypedDict):
    phase: Literal["downloading", "processing"]
    downloaded_bytes: int
    total_bytes: int | None
    stream: NotRequired[Literal["audio", "video", "media"]]


@dataclass(frozen=True, slots=True)
class _YouTubeMetadata:
    title: str | None
    uploader: str | None = None
    uploader_checked: bool = False


@dataclass(frozen=True, slots=True)
class YouTubeCacheResult:
    path: Path
    size_bytes: int
    cached: bool
    metadata: _YouTubeMetadata | None = None

    @property
    def needs_metadata_lookup(self) -> bool:
        return self.metadata is None or not self.metadata.uploader_checked

    @property
    def filename(self) -> str:
        """Expose a descriptive attachment name while keeping the stable media path."""
        metadata = self.metadata
        extension = self.path.suffix
        media_type = "Audio" if extension == f".{_EXTENSIONS['audio']}" else "Video"
        prefix = f"YouTube {media_type} · "
        title = metadata.title if metadata and metadata.title else self.path.stem.removeprefix("youtube_")
        uploader = f" · {metadata.uploader}" if metadata and metadata.uploader else ""
        ending = f"{uploader}{extension}"
        title_budget = _MAX_FILENAME_BYTES - len(f"{prefix}{ending}".encode("utf-8"))
        return f"{prefix}{_limit_filename_part(title, title_budget)}{ending}"


def _limit_filename_part(value: str, max_bytes: int) -> str:
    return value.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore").rstrip(" .")


def _sanitize_filename_part(value: object, max_bytes: int) -> str | None:
    if not isinstance(value, str):
        return None
    name = unicodedata.normalize("NFC", value).translate(_FILENAME_SEPARATORS)
    name = "".join(
        character for character in name
        if character in "\u200c\u200d" or unicodedata.category(character) not in {"Cc", "Cf", "Cs"}
    )
    name = _limit_filename_part(" ".join(name.split()).strip(" ."), max_bytes)
    if isreserved(name):
        return f"_{name}"
    return name or None


def _metadata_from_output(output: str) -> _YouTubeMetadata | None:
    for line in reversed(output.splitlines()):
        if not line.startswith(('"', "{")):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            title = _sanitize_filename_part(payload.get("title"), 200)
            uploader = (
                _sanitize_filename_part(payload.get("uploader"), 80)
                or _sanitize_filename_part(payload.get("channel"), 80)
            )
            if title or uploader:
                return _YouTubeMetadata(title, uploader, uploader_checked=True)
        elif title := _sanitize_filename_part(payload, 200):
            return _YouTubeMetadata(title)
    return None


def _read_metadata_file(path: Path) -> _YouTubeMetadata | None:
    try:
        if stat.S_ISREG(path.lstat().st_mode):
            with path.open(encoding="utf-8") as handle:
                return _metadata_from_output(handle.read(16_384))
    except (OSError, UnicodeError):
        pass
    return None


def _publish_metadata(
    media_path: Path, metadata: _YouTubeMetadata | None,
) -> _YouTubeMetadata | None:
    target = media_path.parent / _METADATA_FILE
    stored = _read_metadata_file(target)
    if metadata is None or not metadata.uploader_checked:
        return stored
    if stored is not None:
        metadata = replace(
            metadata, title=metadata.title or stored.title, uploader=metadata.uploader or stored.uploader,
        )
    if metadata == stored:
        return stored
    try:
        with TemporaryDirectory(prefix=".title-", dir=media_path.parent) as work:
            staged = Path(work) / _METADATA_FILE
            staged.write_text(
                json.dumps({"title": metadata.title, "uploader": metadata.uploader}), encoding="utf-8",
            )
            staged.replace(target)
    except OSError:
        return stored
    return metadata


def parse_youtube_video_id(value: object) -> str:
    """Accept explicit YouTube video links and discard playlist/share parameters."""
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError(_BAD_URL)
    try:
        url = urlsplit(value.strip())
    except ValueError as exc:
        raise ValueError(_BAD_URL) from exc
    if url.scheme not in {"http", "https"} or url.netloc.lower() not in {*_YOUTUBE_HOSTS, "youtu.be"}:
        raise ValueError(_BAD_URL)

    video_id = ""
    if url.hostname == "youtu.be":
        video_id = url.path.removeprefix("/").removesuffix("/")
    elif url.path.rstrip("/") == "/watch":
        values = parse_qs(url.query, keep_blank_values=True).get("v", [])
        if len(values) == 1:
            video_id = values[0]
    else:
        match = re.fullmatch(r"/(?:shorts|live|embed)/([A-Za-z0-9_-]{11})/?", url.path)
        if match:
            video_id = match[1]
    if not _VIDEO_ID.fullmatch(video_id):
        raise ValueError(_BAD_URL)
    return video_id


def youtube_cache_directory(root: Path) -> Path:
    """Return the YouTube cache partition of the project's media store."""
    return (root / MEDIA_STORE_DIRNAME).resolve() / "youtube"


def youtube_cache_path(root: Path, video_id: str, mode: str) -> Path:
    """Resolve an expected cache filename from a validated video ID and mode."""
    if not _VIDEO_ID.fullmatch(video_id) or mode not in _EXTENSIONS:
        raise ValueError("Invalid YouTube video ID or cache mode.")
    cache_root = youtube_cache_directory(root)
    path = cache_root / video_id / f"youtube_{video_id}.{_EXTENSIONS[mode]}"
    if cache_root.is_symlink() or path.resolve() != path:
        raise ValueError("The YouTube cache path is unavailable.")
    return path


def _cached_media_stat(path: Path) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
        raise YouTubeCacheError("The YouTube cache destination is occupied by an invalid file.")
    return info


def read_cached_youtube_media(path: Path) -> YouTubeCacheResult | None:
    """Treat only atomically published regular files as cache hits."""
    info = _cached_media_stat(path)
    if info is None:
        return None
    metadata = _read_metadata_file(path.parent / _METADATA_FILE)
    return YouTubeCacheResult(path, info.st_size, cached=True, metadata=metadata)


def _cache_creation_time_ns(info: os.stat_result) -> int:
    birth_ns = getattr(info, "st_birthtime_ns", 0)
    if not birth_ns:
        birth_ns = int(getattr(info, "st_birthtime", 0.0) * 1_000_000_000)
    # macOS can backdate birth time to yt-dlp's remote modification time.
    if sys.platform == "darwin" and abs(birth_ns - info.st_mtime_ns) <= 1_000:
        return info.st_ctime_ns
    return birth_ns or info.st_ctime_ns


def recent_cached_youtube_media(root: Path) -> list[tuple[str, str, YouTubeCacheResult]]:
    """List completed media by local creation time without making network requests."""
    cache_root = youtube_cache_directory(root)
    if not cache_root.is_dir(follow_symlinks=False):
        return []
    try:
        directories = list(cache_root.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return []

    candidates = []
    for directory in directories:
        video_id = directory.name
        if not _VIDEO_ID.fullmatch(video_id) or not directory.is_dir(follow_symlinks=False):
            continue
        for mode in _EXTENSIONS:
            try:
                path = youtube_cache_path(root, video_id, mode)
                info = _cached_media_stat(path)
            except (OSError, ValueError, YouTubeCacheError):
                continue
            if info is not None:
                candidates.append((_cache_creation_time_ns(info), video_id, mode, path))

    recent = []
    for _, video_id, mode, path in sorted(candidates, reverse=True):
        try:
            media = read_cached_youtube_media(path)
        except (OSError, YouTubeCacheError):
            continue
        if media is not None:
            recent.append((video_id, mode, media))
            if len(recent) == _RECENT_MEDIA_LIMIT:
                break
    return recent


def _downloader_runtime() -> tuple[list[str], list[str], dict[str, str] | None]:
    """Use the optional machine-local YouTube runtime when it has been provisioned."""
    override = os.environ.get("AGENTIC_CONTEXT_YOUTUBE_RUNTIME")
    default = Path.home() / "Library" / "Caches" / "agenticContext" / "youtube-runtime"
    runtime = Path(override).expanduser() if override else default
    python_name = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    python = runtime / "venv" / python_name
    provider = runtime / "provider" / "server"
    runtime_args = ["--js-runtimes", "node"] if shutil.which("node") else []
    if (override or sys.platform == "darwin") and python.is_file() and (provider / "build").is_dir():
        if not runtime_args:
            raise YouTubeCacheError("Node.js is required by the configured YouTube downloader.")
        runtime_args += [
            "--extractor-args", "youtube:player_client=default,mweb,tv,web_safari,web_embedded",
            "--extractor-args", f"youtubepot-bgutilscript:server_home={provider}",
        ]
        env = dict(os.environ)
        proxies = getproxies()
        for scheme in ("http", "https"):
            if proxy := proxies.get(scheme):
                env.setdefault(f"{scheme.upper()}_PROXY", proxy)
        bypass = proxies.get("no", "")
        env["NO_PROXY"] = ",".join(filter(None, (bypass, "localhost", "127.0.0.1")))
        env["NODE_USE_ENV_PROXY"] = "1"
        return [str(python), "-m", "yt_dlp"], runtime_args, env
    if override:
        raise YouTubeCacheError("The configured YouTube downloader runtime is incomplete.")
    try:
        return ensure_yt_dlp_available(), runtime_args, None
    except RuntimeError as exc:
        raise YouTubeCacheError("Install the project's yt-dlp dependency to cache YouTube media.") from exc


def _backfill_cached_metadata(
    cached: YouTubeCacheResult, url: str, should_stop: Callable[[], bool],
) -> YouTubeCacheResult:
    """Look up missing metadata while keeping the existing media available."""
    if not cached.needs_metadata_lookup:
        return cached
    try:
        command, runtime_args, env = _downloader_runtime()
        result = run_yt_dlp_with_retries(
            [*command, "--ignore-config", "--no-update", "--no-playlist", "--skip-download",
             "--ignore-no-formats-error", "--socket-timeout", "8", "--retries", "0",
             *runtime_args, "--print", _METADATA_TEMPLATE, "--", url],
            url, should_stop=should_stop, timeout_seconds=_MAX_METADATA_LOOKUP_SECONDS, env=env,
        )
    except YtDlpInterrupted as exc:
        if exc.reason == "stopped":
            raise YouTubeCacheError(_STOPPED) from exc
        return cached
    except (OSError, YouTubeCacheError):
        return cached
    if should_stop():
        raise YouTubeCacheError(_STOPPED)
    if result.returncode:
        return cached
    metadata = _publish_metadata(cached.path, _metadata_from_output(result.stdout or ""))
    return replace(cached, metadata=metadata)


def _format_selector(mode: str, max_bytes: int) -> str:
    size = f"[filesize<=?{max_bytes}][filesize_approx<=?{max_bytes}]"
    if mode == "audio":
        # A muxed HLS fallback keeps transfer size low while preserving its AAC audio.
        return (
            f"bestaudio[ext=m4a]{size}/bestaudio{size}/"
            f"best[ext=mp4][height<=480]{size}/worst[ext=mp4]{size}"
        )
    video_bytes = max_bytes * 95 // 100
    video_size = f"[filesize<=?{video_bytes}][filesize_approx<=?{video_bytes}]"
    return (
        f"best[ext=mp4][vcodec^=avc1]{size}/"
        f"bestvideo[ext=mp4][vcodec^=avc1]{video_size}+bestaudio[ext=m4a]{size}/"
        f"best[ext=mp4]{size}"
    )


def _verify_download(ffprobe: str, path: Path, mode: str) -> None:
    """Check that the staged container has the requested playable streams."""
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "stream=codec_type:format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, check=True, timeout=8,
        )
        metadata = json.loads(result.stdout)
        streams = {stream["codec_type"] for stream in metadata["streams"]}
        duration = float(metadata["format"]["duration"])
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError) as exc:
        raise YouTubeCacheError("The downloaded YouTube file could not be verified.") from exc
    expected = {"audio", "video"} if mode == "video" else {"audio"}
    if not duration > 0 or streams != expected:
        raise YouTubeCacheError("The downloaded YouTube file did not contain the requested streams.")


def _download_progress(payload: str) -> YouTubeCacheProgress | None:
    raw_progress, separator, raw_codecs = payload.partition("\t")
    if not separator:
        return None
    try:
        report = json.loads(raw_progress)
        codecs = json.loads(raw_codecs)
    except ValueError:
        return None
    if not isinstance(report, dict) or not isinstance(codecs, dict):
        return None
    if report.get("status") not in ("downloading", "finished"):
        return None
    downloaded = report.get("downloaded_bytes")
    total = report.get("total_bytes")
    if type(downloaded) is not int or downloaded < 0:
        return None
    if type(total) is not int or total <= 0 or total < downloaded:
        total = None
    stream: Literal["audio", "video", "media"] = "media"
    video, audio = codecs.get("vcodec"), codecs.get("acodec")
    if video == "none" and isinstance(audio, str) and audio != "none":
        stream = "audio"
    elif audio == "none" and isinstance(video, str) and video != "none":
        stream = "video"
    return {"phase": "downloading", "downloaded_bytes": downloaded, "total_bytes": total, "stream": stream}


def _file_processing_started(payload: str) -> bool:
    try:
        report = json.loads(payload)
    except ValueError:
        return False
    if not isinstance(report, dict) or report.get("status") != "started":
        return False
    postprocessor = report.get("postprocessor")
    return isinstance(postprocessor, str) and postprocessor in _FILE_POSTPROCESSORS


def download_youtube_media(
    root: Path, video_id: str, mode: str, max_file_size_bytes: int,
    *, should_stop: Callable[[], bool] | None = None,
    on_progress: Callable[[YouTubeCacheProgress | None], None] | None = None,
) -> YouTubeCacheResult:
    """Download and verify a single item before publishing it without overwrites."""
    stop_requested = should_stop or (lambda: False)
    if stop_requested():
        raise YouTubeCacheError(_STOPPED)
    destination = youtube_cache_path(root, video_id, mode)
    url = f"https://www.youtube.com/watch?v={video_id}"
    existing = read_cached_youtube_media(destination)
    if existing is not None:
        return _backfill_cached_metadata(existing, url, stop_requested)
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not shutil.which("ffmpeg"):
        raise YouTubeCacheError("Install FFmpeg and ffprobe to cache YouTube media.")
    command, runtime_args, env = _downloader_runtime()
    destination.parent.mkdir(parents=True, exist_ok=True)
    size_error = "This YouTube media exceeds the configured maximum file size."
    last_progress: YouTubeCacheProgress | None = None

    def report_progress(progress: YouTubeCacheProgress | None) -> None:
        nonlocal last_progress
        if progress != last_progress:
            last_progress = progress
            if on_progress is not None:
                on_progress(progress)

    def report_processing() -> None:
        report_progress({
            "phase": "processing", "total_bytes": None,
            "downloaded_bytes": last_progress["downloaded_bytes"] if last_progress is not None else 0,
        })

    def consume_progress(line: str) -> bool:
        marker = _PROGRESS_LINE.match(line)
        if marker is None:
            return False
        payload = line[marker.end():]
        if marker[1] == "download":
            progress = _download_progress(payload)
            if progress is not None:
                report_progress(progress)
        elif _file_processing_started(payload):
            report_processing()
        return True

    with TemporaryDirectory(prefix=".download-", dir=destination.parent) as work:
        work_path = Path(work)
        staged = work_path / destination.name
        staged_metadata = work_path / _METADATA_FILE
        format_args = ["--merge-output-format", "mp4"] if mode == "video" else [
            "--extract-audio", "--audio-format", "m4a",
        ]
        args = [
            *command, "--ignore-config", "--no-update", "--no-playlist", "--no-mtime",
            "--newline", "--progress", "--progress-delta", "0.25",
            "--progress-template", _DOWNLOAD_PROGRESS_TEMPLATE,
            "--progress-template", _POSTPROCESS_PROGRESS_TEMPLATE,
            "--abort-on-unavailable-fragments", "--socket-timeout", "15", "--retries", "2",
            "--fragment-retries", "2", "--match-filter", "!is_live & !is_upcoming",
            "--compat-options", "manifest-filesize-approx",
            "--max-filesize", str(max_file_size_bytes),
            "--print-to-file", f"after_move:{_METADATA_TEMPLATE}", str(staged_metadata).replace("%", "%%"),
            *runtime_args, *format_args, "--output", str(staged.with_suffix(".%(ext)s")),
        ]
        try:
            result = run_yt_dlp_with_retries(
                [*args, "--format", _format_selector(mode, max_file_size_bytes), "--", url],
                url, should_stop=stop_requested, timeout_seconds=_MAX_DOWNLOAD_SECONDS, env=env,
                consume_stdout=consume_progress, on_retry=lambda: report_progress(None),
            )
        except YtDlpInterrupted as exc:
            message = _STOPPED if exc.reason == "stopped" else "The YouTube download timed out. Please try again."
            raise YouTubeCacheError(message) from exc

        output = f"{result.stdout or ''}\n{result.stderr or ''}"
        if is_max_file_size_skip_output(output):
            raise YouTubeCacheError(size_error)
        if result.returncode or not staged.is_file():
            if re.search(r"HTTP Error (?:403|429)|Sign in to confirm", output, re.IGNORECASE):
                raise YouTubeCacheError("YouTube rejected the download for this connection. Please try again later.")
            if "Requested format is not available" in output:
                raise YouTubeCacheError("No compatible YouTube stream is available within the configured size limit.")
            raise YouTubeCacheError("This YouTube video could not be cached.")
        if staged.stat().st_size > max_file_size_bytes:
            raise YouTubeCacheError(size_error)
        if stop_requested():
            raise YouTubeCacheError(_STOPPED)
        report_processing()
        _verify_download(ffprobe, staged, mode)
        if stop_requested():
            raise YouTubeCacheError(_STOPPED)
        try:
            destination.hardlink_to(staged)
        except FileExistsError:
            existing = read_cached_youtube_media(destination)
            if existing is not None:
                return existing
            raise
        metadata = _publish_metadata(destination, _read_metadata_file(staged_metadata))
        return YouTubeCacheResult(destination, staged.stat().st_size, cached=False, metadata=metadata)
