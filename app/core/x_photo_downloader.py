"""Download original X photos from media URLs observed in the authorized page."""

# Code version: v1.0.0-codex.0

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from PIL import Image


PLAN_FILENAME = ".x-media-plan.json"
PHOTO_FORMATS = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "WEBP": ".webp"}
MAX_PHOTOS_PER_POST = 16


@dataclass(frozen=True, slots=True)
class XMediaPost:
    """Bind one visible primary post to its observed photos and video presence."""

    tweet_url: str
    photo_urls: tuple[str, ...]
    has_video: bool = False


@dataclass(frozen=True, slots=True)
class XPhotoDownloadResult:
    """Retain validated existing and new photos, with explicit size skips."""

    paths: tuple[Path, ...] = ()
    downloaded_count: int = 0
    skipped_size: int = 0


class XPhotoDownloadInterrupted(RuntimeError):
    """Preserve completed photos when the shared deadline or stop flag fires."""

    def __init__(self, reason: str, result: XPhotoDownloadResult) -> None:
        super().__init__(f"X photo download {reason}.")
        self.reason = reason
        self.result = result


def normalize_x_photo_url(value: str) -> str:
    """Accept exact HTTPS X photo CDN URLs and request their original bytes."""
    if not isinstance(value, str) or any(ord(char) < 33 for char in value):
        return ""
    try:
        parts = urlsplit(value)
        if (
            parts.scheme != "https"
            or parts.netloc != "pbs.twimg.com"
            or parts.fragment
            or not re.fullmatch(r"/media/[A-Za-z0-9_-]+(?:\.(?:jpg|jpeg|png|gif|webp))?", parts.path)
        ):
            return ""
        query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True)
    except (TypeError, ValueError):
        return ""
    if set(query) - {"format", "name"} or any(len(values) != 1 for values in query.values()):
        return ""
    extension = parts.path.rsplit(".", 1)[1] if "." in parts.path else ""
    image_format = query.get("format", [extension])[0]
    if image_format not in {"jpg", "jpeg", "png", "gif", "webp"}:
        return ""
    if extension and query.get("format", [extension])[0] != extension:
        return ""
    path = parts.path.rsplit(".", 1)[0] if extension else parts.path
    return f"https://pbs.twimg.com{path}?{urlencode({'format': image_format, 'name': 'orig'})}"


def validate_media_post(plan: XMediaPost, tweet_url: str | None = None) -> XMediaPost:
    """Require one canonical X status binding and a bounded trusted photo list."""
    if (
        not isinstance(plan, XMediaPost)
        or not isinstance(plan.has_video, bool)
        or not isinstance(plan.tweet_url, str)
        or any(ord(char) < 33 for char in plan.tweet_url)
    ):
        raise RuntimeError("Invalid X media plan.")
    try:
        parts = urlsplit(plan.tweet_url)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Invalid X media post URL.") from exc
    if (
        parts.scheme != "https"
        or parts.netloc != "x.com"
        or parts.query
        or parts.fragment
        or not re.fullmatch(r"/[A-Za-z0-9_]+/status/[0-9]+", parts.path)
        or (tweet_url is not None and plan.tweet_url != tweet_url)
    ):
        raise RuntimeError("X media plan is not bound to the requested post.")
    if not isinstance(plan.photo_urls, tuple) or len(plan.photo_urls) > MAX_PHOTOS_PER_POST:
        raise RuntimeError("Invalid X photo plan size.")
    normalized = tuple(normalize_x_photo_url(url) for url in plan.photo_urls)
    if any(not url for url in normalized):
        raise RuntimeError("X media plan contains an unsafe photo URL.")
    return XMediaPost(plan.tweet_url, tuple(dict.fromkeys(normalized)), plan.has_video)


def safe_photo_directory(plan: XMediaPost, output_dir: Path) -> Path:
    """Create only real directories below a symlink-free output root."""
    absolute = Path(os.path.abspath(output_dir))
    for component in (absolute, *absolute.parents):
        if component.is_symlink():
            raise RuntimeError("X photo cache path must not contain symlinks.")
    status_id = plan.tweet_url.rsplit("/", 1)[1]
    directory = absolute / "photos" / status_id
    for component in (absolute, absolute / "photos", directory):
        if component.is_symlink() or (component.exists() and not component.is_dir()):
            raise RuntimeError("Unsafe X photo cache directory.")
        component.mkdir(parents=True, exist_ok=True)
    return directory


def photo_plan_fingerprint(plan: XMediaPost, max_bytes: int) -> str:
    """Include size policy so a larger limit retries previously skipped photos."""
    value = json.dumps(
        [plan.tweet_url, sorted(plan.photo_urls), plan.has_video, max_bytes],
        separators=(",", ":"),
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def write_photo_plan(
    directory: Path,
    fingerprint: str,
    *,
    complete: bool,
    video_complete: bool = False,
    max_bytes: int = 0,
) -> None:
    """Atomically publish the plan's completion state without following links."""
    target = directory / PLAN_FILENAME
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise RuntimeError("Unsafe X media completion marker.")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, prefix=".x-plan-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({
                "version": 1, "fingerprint": fingerprint, "complete": complete,
                "video_complete": video_complete, "max_bytes": max_bytes,
            }, stream)
            stream.flush()
            os.fsync(stream.fileno())
        if target.is_symlink() or directory.is_symlink():
            raise RuntimeError("Unsafe X media completion marker.")
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_photo_plan(directory: Path) -> dict:
    """Read only a bounded regular marker, retaining unknown state as pending."""
    path = directory / PLAN_FILENAME
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def photo_plan_is_complete(directory: Path, fingerprint: str) -> bool:
    """Require a bounded regular marker for exactly this planned request."""
    value = read_photo_plan(directory)
    return value.get("complete") is True and value.get("fingerprint") == fingerprint


class _TrustedPhotoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not normalize_x_photo_url(newurl):
            raise RuntimeError("X photo redirect left the trusted media origin.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_photo_url(url: str, timeout: float):
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "image/*"})
    return build_opener(_TrustedPhotoRedirects()).open(request, timeout=timeout)


def _verify_image(path: Path, max_bytes: int) -> str:
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= max_bytes:
        raise RuntimeError("Invalid or oversized X photo cache file.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                extension = PHOTO_FORMATS.get(image.format or "")
                if not extension or image.width * image.height > 40_000_000:
                    raise RuntimeError("Unsupported X photo image format or dimensions.")
                image.verify()
            with Image.open(path) as image:
                image.load()
        return extension
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise RuntimeError("X photo payload is not a valid image.") from exc


def download_x_photos(
    plan: XMediaPost,
    output_dir: Path,
    max_bytes: int,
    should_stop: Callable[[], bool] | None,
    deadline: float,
    *,
    max_new_items: int | None = None,
) -> XPhotoDownloadResult:
    """Stream bounded photos, validate actual image bytes, and preserve originals."""
    plan = validate_media_post(plan)
    if max_bytes <= 0:
        raise RuntimeError("Invalid X photo size limit.")
    directory = safe_photo_directory(plan, output_dir)
    paths: list[Path] = []
    downloaded_count = 0
    skipped_size = 0

    def check_interruption() -> None:
        reason = "stopped" if should_stop is not None and should_stop() else "timed_out" if time.monotonic() >= deadline else ""
        if reason:
            raise XPhotoDownloadInterrupted(reason, XPhotoDownloadResult(tuple(paths), downloaded_count, skipped_size))

    for url in plan.photo_urls:
        check_interruption()
        key = "image_" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        existing = [directory / (key + extension) for extension in PHOTO_FORMATS.values()]
        cached = [path for path in existing if path.exists() or path.is_symlink()]
        if cached:
            if len(cached) != 1 or cached[0].is_symlink() or not cached[0].is_file():
                raise RuntimeError("Invalid existing X photo cache file.")
            if cached[0].stat().st_size > max_bytes:
                skipped_size += 1
                continue
            if _verify_image(cached[0], max_bytes) != cached[0].suffix:
                raise RuntimeError("Invalid existing X photo cache file.")
            paths.append(cached[0])
            continue
        if max_new_items is not None and downloaded_count >= max(0, max_new_items):
            break
        temporary: Path | None = None
        try:
            timeout = max(0.01, min(30.0, deadline - time.monotonic()))
            with _open_photo_url(url, timeout) as response:
                if not normalize_x_photo_url(response.geturl()):
                    raise RuntimeError("X photo response left the trusted media origin.")
                content_length = response.headers.get("Content-Length", "")
                if str(content_length).isdigit() and int(content_length) > max_bytes:
                    skipped_size += 1
                    continue
                with tempfile.NamedTemporaryFile(dir=directory, prefix=".x-photo-", delete=False) as stream:
                    temporary = Path(stream.name)
                    total = 0
                    while True:
                        check_interruption()
                        chunk = response.read(min(64 * 1024, max_bytes - total + 1))
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            break
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                if total > max_bytes:
                    skipped_size += 1
                    continue
            extension = _verify_image(temporary, max_bytes)
            destination = directory / (key + extension)
            safe_photo_directory(plan, output_dir)
            if destination.exists() or destination.is_symlink():
                raise RuntimeError("X photo destination changed during download.")
            os.replace(temporary, destination)
            paths.append(destination)
            downloaded_count += 1
        except XPhotoDownloadInterrupted:
            raise
        except RuntimeError:
            raise
        except (OSError, ValueError) as exc:
            raise RuntimeError("X photo download failed.") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return XPhotoDownloadResult(tuple(paths), downloaded_count, skipped_size)
