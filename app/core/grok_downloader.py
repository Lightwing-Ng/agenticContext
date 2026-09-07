"""Grok media sync helpers."""

# Code version: v1.18.1-codex.1

from __future__ import annotations

import contextlib
import hashlib
from http.client import IncompleteRead
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from .cache_timing import wait_for_cache_scan
from .browser_sessions import browser_descriptors, launch_chromium_context
from .config import MEDIA_STORE_ROOT, CrawlConfig, default_edge_user_data_dir, is_windows_host
from .local_media_browser import BrowserDeletionCatalog
from .resource_persistence import (
    GROK_CATALOG_FILENAME,
    GROK_CATALOG_SCHEMA,
    GROK_CATALOG_SCHEMA_VERSION,
    GROK_DOWNLOAD_MANIFEST_FILENAME,
    GROK_DOWNLOAD_MANIFEST_SCHEMA,
    GROK_DOWNLOAD_MANIFEST_SCHEMA_VERSION,
    GROK_WORK_QUEUE_FILENAME,
    GROK_WORK_QUEUE_SCHEMA,
    GROK_WORK_QUEUE_SCHEMA_VERSION,
    LEGACY_GROK_CATALOG_FILENAME,
    LEGACY_GROK_DOWNLOAD_MANIFEST_FILENAME,
    LEGACY_GROK_WORK_QUEUE_FILENAME,
    read_parquet_rows,
    retire_legacy_file,
    write_parquet_rows_atomic,
)
from .safari_automation import SafariContext
from .state import TaskSnapshot, TaskState, utc_now

try:  # pragma: no cover - depends on the local runtime
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - exercised in environments without Playwright
    PlaywrightError = RuntimeError
    sync_playwright = None


logger = logging.getLogger(__name__)

DEFAULT_GROK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    if is_windows_host()
    else "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
) + "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"

EDGE_USER_DATA_DIR = default_edge_user_data_dir()
EDGE_PROFILE_DIR = "Default"
GROK_TARGET_DIR = MEDIA_STORE_ROOT / "grok"
GROK_FILES_URL = "https://grok.com/files?sort=&fileType=&createdBy="
GROK_SCROLL_ROUNDS = 480
GROK_STALE_SCROLL_LIMIT = 12
GROK_KNOWN_PREFIX_STOP_COUNT = 0
DOWNLOAD_TIMEOUT_MS = 30_000
DOWNLOAD_CHUNK_BYTES = 512 * 1024
GROK_DOWNLOAD_WORKERS = 4
GROK_DOWNLOAD_BACKLOG = GROK_DOWNLOAD_WORKERS * 6
GROK_DOWNLOAD_RETRY_LIMIT = 4
GROK_QUEUE_RESOLUTION_RETRY_LIMIT = 2
GROK_LIBRARY_PAGE_POOL_SIZE = 5
GROK_LIBRARY_PAGE_OPEN_INTERVAL = 6
GROK_RESOLUTION_PAGE_POOL_SIZE = 2
GROK_RESOLUTION_BATCH_SIZE = 6
PAGE_GOTO_TIMEOUT_MS = 60_000
PAGE_IDLE_WAIT_TIMEOUT_MS = 5_000
INITIAL_PAGE_WAIT_SECONDS = 4.0
SAFARI_LIBRARY_READY_TIMEOUT_SECONDS = 20.0
SCROLL_WAIT_SECONDS = 0.8
TEMP_DOWNLOAD_DIRNAME = ".grok-partial"
LEGACY_ASSET_FILENAME_PATTERN = re.compile(
    r"^(?P<asset_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})_(?P<asset_name>.+)$",
    re.IGNORECASE,
)
STAMPED_ASSET_FILENAME_PATTERN = re.compile(
    r"^(?P<prefix>img|vid)_(?P<timestamp>\d{8}T\d{6}Z)_(?P<asset_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:_(?P<asset_name>.+))?$",
    re.IGNORECASE,
)
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}


@dataclass(slots=True)
class GrokMediaCandidate:
    """Describe one downloadable Grok asset discovered in the page."""

    source_url: str
    asset_id: str
    asset_name: str
    media_kind: str
    identity: str
    preview_url: str = ""
    expected_width: int = 0
    expected_height: int = 0
    expected_bytes: int = 0
    created_at: str = ""


@dataclass(slots=True)
class GrokCatalogEntry:
    """Persist the mapping from Grok asset identity to a local file."""

    identity: str
    relative_path: str
    media_kind: str
    content_sha256: str
    content_bytes: int
    source_url: str
    first_seen_at: str
    last_seen_at: str


@dataclass(slots=True)
class GrokSyncResult:
    """Capture the Grok sync outcome."""

    discovered_count: int = 0
    downloaded_count: int = 0
    downloaded_images: int = 0
    downloaded_videos: int = 0
    skipped_known: int = 0
    deduped_by_hash: int = 0
    failed_count: int = 0
    cached_count: int = 0
    cached_images: int = 0
    cached_videos: int = 0
    stopped: bool = False
    skipped_size: int = 0


@dataclass(slots=True)
class GrokResetResult:
    """Describe what was removed by a Grok local-state reset."""

    removed_media_files: int = 0
    removed_state_files: int = 0
    removed_partial_files: int = 0
    removed_partial_dirs: int = 0


@dataclass(slots=True)
class GrokManifestEntry:
    """Persist resumable download state for one Grok asset."""

    identity: str
    asset_id: str
    asset_name: str
    media_kind: str
    source_url: str
    status: str = "pending"
    relative_path: str = ""
    temp_relative_path: str = ""
    content_sha256: str = ""
    content_bytes: int = 0
    expected_bytes: int = 0
    created_at: str = ""
    attempts: int = 0
    last_error: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class GrokDownloadOutcome:
    """Describe one worker outcome for progress accounting."""

    candidate: GrokMediaCandidate
    downloaded: bool = False
    deduped: bool = False
    failed: bool = False
    stopped: bool = False
    resumed: bool = False
    error: str = ""
    skipped_size: bool = False


@dataclass(slots=True)
class GrokDownloadAuth:
    """Carry browser-derived request headers into worker downloads."""

    cookie_header: str = ""
    user_agent: str = DEFAULT_GROK_USER_AGENT


@dataclass(slots=True)
class GrokWorkQueueEntry:
    """Persist one local Grok work-queue item across scanning and downloading."""

    asset_id: str
    identity: str = ""
    asset_name: str = ""
    media_kind: str = "image"
    source_url: str = ""
    preview_url: str = ""
    expected_bytes: int = 0
    created_at: str = ""
    discovered_at: str = ""
    updated_at: str = ""
    status: str = "discovered"
    resolution_attempts: int = 0
    download_attempts: int = 0
    last_error: str = ""


class DownloadStoppedError(RuntimeError):
    """Raised when a cooperative stop interrupts an in-flight download."""


class DownloadPayloadIntegrityError(RuntimeError):
    """Raised when an HTTP-successful response is not a complete media file."""


class DownloadSizeLimitError(RuntimeError):
    """Raised when a Grok asset exceeds the universal cache file-size limit."""


def resolve_grok_download_worker_count(config: CrawlConfig, browser_engine: str) -> int:
    """Return the shared worker setting within Grok's safe concurrency boundary."""
    if browser_engine == "safari":
        return 1
    return max(1, min(int(config.download_workers), GROK_DOWNLOAD_WORKERS))


def normalize_asset_name(raw_name: str) -> str:
    """Normalize Grok asset names for stable local identity matching."""
    cleaned = (raw_name or "").split("?", 1)[0].split("#", 1)[0].strip()
    if not cleaned:
        return ""

    # Remote asset names retain the same identity regardless of the host filesystem.
    stem = PurePosixPath(cleaned).stem or cleaned
    stem = re.sub(r"[-_]+", "-", stem.lower())
    stem = re.sub(r"[^a-z0-9.-]+", "-", stem)
    return stem.strip("-.")


def classify_media_kind(raw_name: str, media_tag: str) -> str:
    """Infer whether the asset is an image or a video."""
    suffix = Path((raw_name or "").split("?", 1)[0]).suffix.lower()
    if suffix in VIDEO_SUFFIXES:
        return "video"
    if suffix in IMAGE_SUFFIXES:
        return "image"

    normalized_name = normalize_asset_name(raw_name)
    if "video" in normalized_name or (media_tag or "").lower() == "video":
        return "video"
    return "image"


def sanitize_filename_part(value: str) -> str:
    """Return a deterministic filesystem-safe filename fragment."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    cleaned = cleaned.strip("._-")
    return cleaned or "asset"


def compute_sha256(content: bytes) -> str:
    """Return a SHA-256 digest for the given content."""
    return hashlib.sha256(content).hexdigest()


def compute_file_sha256(file_path: Path) -> str:
    """Return a SHA-256 digest for one local file."""
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        while True:
            chunk = handle.read(DOWNLOAD_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def build_download_headers(
    candidate: GrokMediaCandidate | None = None,
    auth: GrokDownloadAuth | None = None,
    range_start: int = 0,
) -> dict[str, str]:
    """Build conservative HTTP headers for Grok asset downloads."""
    headers = {
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Origin": "https://grok.com",
        "Pragma": "no-cache",
        "Referer": build_file_details_url(candidate.asset_id) if candidate is not None else "https://grok.com/files",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "User-Agent": auth.user_agent if auth is not None and auth.user_agent else GrokDownloadAuth().user_agent,
    }
    if auth is not None and auth.cookie_header:
        headers["Cookie"] = auth.cookie_header
    if range_start > 0:
        headers["Range"] = f"bytes={range_start}-"
    return headers


def read_file_signature(file_path: Path, size: int = 64) -> bytes:
    """Return the first bytes of one local file."""
    with file_path.open("rb") as handle:
        return handle.read(size)


def looks_like_image_signature(signature: bytes) -> bool:
    """Return whether the signature matches common raster image formats."""
    if signature.startswith(b"\xff\xd8\xff"):
        return True
    if signature.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if signature.startswith((b"GIF87a", b"GIF89a")):
        return True
    return len(signature) >= 12 and signature.startswith(b"RIFF") and signature[8:12] == b"WEBP"


def looks_like_video_signature(signature: bytes) -> bool:
    """Return whether the signature matches common Grok video containers."""
    if len(signature) >= 12 and signature[4:8] == b"ftyp":
        return True
    return signature.startswith(b"\x1a\x45\xdf\xa3")


def validate_media_file(
    file_path: Path,
    media_kind: str,
    content_type: str = "",
    expected_bytes: int = 0,
) -> bool:
    """Return whether a local file looks like a complete media payload."""
    return media_validation_error(file_path, media_kind, content_type, expected_bytes) is None


def media_validation_error(
    file_path: Path,
    media_kind: str,
    content_type: str = "",
    expected_bytes: int = 0,
) -> str | None:
    """Return a precise media validation failure reason, when one exists."""
    if not file_path.exists() or not file_path.is_file():
        return "the temporary file is missing"

    file_size = file_path.stat().st_size
    if file_size <= 0:
        return "the response body is empty"

    signature = read_file_signature(file_path)
    normalized_content_type = (content_type or "").split(";", 1)[0].strip().lower()
    problems: list[str] = []
    if expected_bytes > 0 and file_size != expected_bytes:
        problems.append(f"received {file_size:,} bytes, expected {expected_bytes:,} bytes")
    if normalized_content_type.startswith("text/") or normalized_content_type in {
        "application/json",
        "application/xml",
        "text/html",
    }:
        problems.append(f"server returned {normalized_content_type or 'a text response'} instead of media")

    if media_kind == "video":
        if not looks_like_video_signature(signature):
            problems.append("the payload does not have a recognized video signature")
    elif not looks_like_image_signature(signature):
        problems.append("the payload does not have a recognized image signature")

    return "; ".join(problems) if problems else None


def build_partial_relative_path(candidate: GrokMediaCandidate) -> str:
    """Return a deterministic partial-download path for one asset identity."""
    identity_digest = hashlib.sha1(candidate.identity.encode("utf-8")).hexdigest()[:16]
    asset_name = sanitize_filename_part(candidate.asset_name)
    filename = f"{candidate.asset_id}_{asset_name}_{identity_digest}.part"
    return f"{TEMP_DOWNLOAD_DIRNAME}/{filename}"


def parse_catalog_timestamp(value: str) -> datetime | None:
    """Parse an ISO-like timestamp string into UTC."""
    normalized = (value or "").strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def normalize_catalog_timestamp(value: str, fallback: str = "") -> str:
    """Normalize timestamps into second-precision UTC ISO format."""
    parsed = parse_catalog_timestamp(value)
    if parsed is None:
        return fallback
    return parsed.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def compact_timestamp_for_filename(value: str, fallback: str = "") -> str:
    """Convert a catalog timestamp into a lexicographically sortable filename fragment."""
    parsed = parse_catalog_timestamp(value)
    if parsed is None:
        return fallback
    return parsed.strftime("%Y%m%dT%H%M%SZ")


def isoformat_from_compact_timestamp(value: str) -> str:
    """Expand a compact filename timestamp into catalog ISO format."""
    parsed = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    return parsed.isoformat().replace("+00:00", "Z")


def default_asset_name_for_prefix(prefix: str) -> str:
    """Infer a stable asset name when only the media prefix is available."""
    return "generated-video" if str(prefix or "").lower() == "vid" else "image"


def extract_asset_name_from_identity(identity: str) -> str:
    """Return the asset-name portion from a catalog identity."""
    _asset_id, _separator, asset_name = str(identity or "").partition("/")
    return normalize_asset_name(asset_name)


def preferred_seen_at(candidate: GrokMediaCandidate, fallback: str = "") -> str:
    """Choose the best available timestamp for catalog ordering and filenames."""
    normalized_created_at = normalize_catalog_timestamp(candidate.created_at)
    if normalized_created_at:
        return normalized_created_at
    normalized_fallback = normalize_catalog_timestamp(fallback)
    if normalized_fallback:
        return normalized_fallback
    return utc_now()


def infer_extension(source_url: str, content_type: str, media_kind: str) -> str:
    """Choose a stable local file extension for a Grok asset."""
    url_suffix = Path(urlsplit(source_url).path).suffix.lower()
    if url_suffix in IMAGE_SUFFIXES | VIDEO_SUFFIXES:
        return url_suffix

    normalized_content_type = (content_type or "").split(";", 1)[0].strip().lower()
    guessed_extension = mimetypes.guess_extension(normalized_content_type)
    if guessed_extension == ".jpe":
        guessed_extension = ".jpg"
    if guessed_extension and guessed_extension.lower() in IMAGE_SUFFIXES | VIDEO_SUFFIXES:
        return guessed_extension.lower()

    return ".mp4" if media_kind == "video" else ".jpg"


def extract_response_content_type(response) -> str:
    """Read the content type from Playwright API responses across versions."""
    header_value = getattr(response, "header_value", None)
    if callable(header_value):
        return str(header_value("content-type") or "")

    headers = getattr(response, "headers", None)
    if callable(headers):
        headers = headers()
    if isinstance(headers, dict):
        return str(headers.get("content-type") or headers.get("Content-Type") or "")

    return ""


def build_candidate_from_versions_payload(
    payload: dict[str, object],
    fallback_candidate: GrokMediaCandidate,
) -> GrokMediaCandidate:
    """Resolve the canonical downloadable asset URL from the versions API payload."""
    assets = payload.get("assets") or []
    if not isinstance(assets, list) or not assets:
        return fallback_candidate

    ranked_candidates: list[tuple[tuple[int, int, int, int, int, int, str], GrokMediaCandidate]] = []
    for asset_row in assets:
        if not isinstance(asset_row, dict):
            continue

        is_deleted = bool(asset_row.get("isDeleted"))
        asset_key = str(asset_row.get("key") or "").strip()
        preview_key = str(asset_row.get("previewImageKey") or "").strip()
        remote_name = str(asset_row.get("name") or "").strip()
        mime_type = str(asset_row.get("mimeType") or "").strip().lower()
        expected_width = int(asset_row.get("width") or 0)
        expected_height = int(asset_row.get("height") or 0)
        expected_bytes = int(asset_row.get("sizeBytes") or 0)
        created_at = normalize_catalog_timestamp(str(asset_row.get("createTime") or "").strip())
        source_url = build_assets_url_from_key(asset_key)
        preview_url = build_assets_url_from_key(preview_key)

        media_kind = fallback_candidate.media_kind
        if mime_type.startswith("video/"):
            media_kind = "video"
        elif mime_type.startswith("image/"):
            media_kind = "image"

        if media_kind == "image" and not is_deleted and (not source_url or is_preview_asset_url(source_url)):
            source_url = preview_url or fallback_candidate.preview_url or fallback_candidate.source_url
        if not preview_url and not is_deleted:
            preview_url = fallback_candidate.preview_url or fallback_candidate.source_url

        if is_deleted and not source_url:
            preview_url = ""
        if is_deleted and media_kind == "image" and is_preview_asset_url(source_url):
            source_url = ""
        if media_kind == "image" and not is_deleted and (not source_url or is_preview_asset_url(source_url)):
            source_url = preview_url

        canonical_name = (
            normalize_asset_name(remote_name)
            or normalize_asset_name(Path(asset_key).name)
            or fallback_candidate.asset_name
        )
        identity = (
            f"{fallback_candidate.asset_id}/{canonical_name}"
            if fallback_candidate.asset_id and canonical_name
            else fallback_candidate.identity
        )
        candidate = GrokMediaCandidate(
            source_url=source_url if is_deleted else (source_url or fallback_candidate.source_url),
            asset_id=fallback_candidate.asset_id,
            asset_name=canonical_name,
            media_kind=media_kind,
            identity=identity,
            preview_url=preview_url,
            expected_width=expected_width,
            expected_height=expected_height,
            expected_bytes=expected_bytes,
            created_at=created_at,
        )
        ranking = (
            1 if not is_deleted else 0,
            1 if bool(asset_row.get("isLatest")) else 0,
            1 if candidate.source_url else 0,
            1 if candidate.media_kind != "image" or not is_preview_asset_url(candidate.source_url) else 0,
            candidate.expected_width * candidate.expected_height,
            candidate.expected_bytes,
            candidate.identity,
        )
        ranked_candidates.append((ranking, candidate))

    if not ranked_candidates:
        return fallback_candidate

    return max(ranked_candidates, key=lambda item: item[0])[1]


def resolve_candidate_from_versions(context, fallback_candidate: GrokMediaCandidate) -> GrokMediaCandidate:
    """Fetch the versions payload and upgrade a thumbnail candidate to the original asset."""
    try:
        response = context.request.get(build_versions_url(fallback_candidate.asset_id), timeout=DOWNLOAD_TIMEOUT_MS)
    except (PlaywrightError, RuntimeError) as exc:
        logger.warning(
            "Falling back to list candidate because the versions endpoint request failed.",
            extra={
                "asset_id": fallback_candidate.asset_id,
                "versions_url": build_versions_url(fallback_candidate.asset_id),
                "error": str(exc),
            },
        )
        return fallback_candidate
    if not response.ok:
        logger.warning(
            "Falling back to list candidate because the versions endpoint failed.",
            extra={
                "asset_id": fallback_candidate.asset_id,
                "versions_url": build_versions_url(fallback_candidate.asset_id),
                "status": response.status,
            },
        )
        return fallback_candidate

    with contextlib.suppress(json.JSONDecodeError):
        payload = json.loads(response.text())
        return build_candidate_from_versions_payload(payload, fallback_candidate)

    return fallback_candidate


def open_grok_page(page, url: str, settle_seconds: float = 1.0) -> None:
    """Open a Grok page without blocking on long-lived network activity."""
    page.goto(url, wait_until="domcontentloaded", timeout=PAGE_GOTO_TIMEOUT_MS)
    with contextlib.suppress(PlaywrightError):
        page.wait_for_load_state("networkidle", timeout=PAGE_IDLE_WAIT_TIMEOUT_MS)
    if settle_seconds > 0:
        time.sleep(settle_seconds)


def prepare_grok_library_page(context):
    """Prepare the Grok library page and close stray blank tabs."""
    page = None
    for _ in range(10):
        page = next((candidate for candidate in reversed(context.pages) if candidate.url.startswith("https://grok.com/files")), None)
        if page is not None:
            break
        time.sleep(0.2)

    if page is None:
        page = context.new_page()
        open_grok_page(page, GROK_FILES_URL, settle_seconds=0.0)

    for candidate in list(context.pages):
        if candidate is page or candidate.url != "about:blank":
            continue
        with contextlib.suppress(Exception):
            candidate.close()

    return page


def build_file_details_url(asset_id: str) -> str:
    """Return the Grok file details page for one asset."""
    return f"https://grok.com/files?file={asset_id}"


def resolve_candidate_from_file_details_page(
    context,
    fallback_candidate: GrokMediaCandidate,
    details_page=None,
) -> GrokMediaCandidate:
    """Read the active preview panel to recover the full-resolution asset URL."""
    if not fallback_candidate.asset_id:
        return fallback_candidate

    owns_page = details_page is None
    if details_page is None:
        details_page = context.new_page()
    try:
        open_grok_page(details_page, build_file_details_url(fallback_candidate.asset_id))
        payload = details_page.evaluate(
            """() => {
                const pickLargestImage = () => {
                    const images = Array.from(document.querySelectorAll('img')).filter((candidate) => {
                        const src = candidate.currentSrc || candidate.src || '';
                        return src.includes('assets.grok.com');
                    });
                    if (!images.length) {
                        return null;
                    }
                    return images.reduce((best, candidate) => {
                        const bestScore = (best.naturalWidth || 0) * (best.naturalHeight || 0);
                        const candidateScore = (candidate.naturalWidth || 0) * (candidate.naturalHeight || 0);
                        return candidateScore >= bestScore ? candidate : best;
                    });
                };

                const activeImage = document.querySelector('img[alt="active-image"]') || pickLargestImage();
                if (activeImage) {
                    return {
                        sourceUrl: activeImage.currentSrc || activeImage.src || '',
                        mediaTag: 'img',
                        width: activeImage.naturalWidth || 0,
                        height: activeImage.naturalHeight || 0,
                    };
                }

                const activeVideo = document.querySelector('video source') || document.querySelector('video');
                if (activeVideo) {
                    return {
                        sourceUrl: activeVideo.currentSrc || activeVideo.src || '',
                        mediaTag: 'video',
                        width: activeVideo.videoWidth || 0,
                        height: activeVideo.videoHeight || 0,
                    };
                }

                return null;
            }"""
        )
    except (PlaywrightError, RuntimeError) as exc:
        logger.warning(
            "Falling back to the discovered candidate because the Grok details page request failed.",
            extra={
                "asset_id": fallback_candidate.asset_id,
                "error": str(exc),
            },
        )
        return fallback_candidate
    finally:
        if owns_page:
            with contextlib.suppress(Exception):
                details_page.close()

    if not isinstance(payload, dict):
        return fallback_candidate

    source_url = str(payload.get("sourceUrl") or "").strip()
    if not source_url:
        return fallback_candidate

    active_candidate = candidate_from_url(source_url, str(payload.get("mediaTag") or ""))
    if active_candidate is None:
        return fallback_candidate

    return GrokMediaCandidate(
        source_url=active_candidate.source_url,
        asset_id=fallback_candidate.asset_id or active_candidate.asset_id,
        asset_name=active_candidate.asset_name,
        media_kind=active_candidate.media_kind,
        identity=f"{fallback_candidate.asset_id or active_candidate.asset_id}/{active_candidate.asset_name}",
        preview_url=fallback_candidate.preview_url or fallback_candidate.source_url,
        expected_width=int(payload.get("width") or 0),
        expected_height=int(payload.get("height") or 0),
        expected_bytes=fallback_candidate.expected_bytes,
        created_at=fallback_candidate.created_at,
    )


def clone_profile(source_user_data_dir: Path, source_profile_dir: Path) -> tuple[Path, tempfile.TemporaryDirectory[str]]:
    """Clone the signed-in Edge profile into a temporary workspace."""
    temp_dir = tempfile.TemporaryDirectory(prefix="grok-edge-")
    temp_root = Path(temp_dir.name)
    target_user_data_dir = temp_root / "EdgeUserData"
    target_profile_dir = target_user_data_dir / EDGE_PROFILE_DIR

    target_user_data_dir.mkdir(parents=True, exist_ok=True)

    local_state = source_user_data_dir / "Local State"
    if local_state.exists():
        shutil.copy2(local_state, target_user_data_dir / "Local State")

    def ignore_transient_files(_directory: str, names: list[str]) -> set[str]:
        ignored = {"SingletonCookie", "SingletonLock", "SingletonSocket", "lockfile"}
        ignored.update(name for name in names if name.endswith(".lock"))
        return ignored

    shutil.copytree(source_profile_dir, target_profile_dir, dirs_exist_ok=True, ignore=ignore_transient_files)
    return target_user_data_dir, temp_dir


def is_preview_asset_name(asset_name: str) -> bool:
    """Return whether the asset name points at a thumbnail preview."""
    normalized_name = normalize_asset_name(asset_name)
    return normalized_name.startswith("preview-image") or normalized_name.startswith("preview-image-")


def is_preview_asset_url(url: str) -> bool:
    """Return whether the URL points at a Grok preview image."""
    lowered = (url or "").lower()
    return "/preview-image" in lowered or "/preview_image" in lowered


def build_assets_url_from_key(asset_key: str) -> str:
    """Convert an asset key from the versions API into a downloadable URL."""
    cleaned_key = str(asset_key or "").lstrip("/")
    return f"https://assets.grok.com/{cleaned_key}" if cleaned_key else ""


def build_download_auth(context, page) -> GrokDownloadAuth:
    """Extract reusable browser-authenticated headers for worker downloads."""
    user_agent = GrokDownloadAuth().user_agent
    with contextlib.suppress(Exception):
        evaluated_user_agent = str(page.evaluate("() => navigator.userAgent") or "").strip()
        if evaluated_user_agent:
            user_agent = evaluated_user_agent

    cookie_pairs: list[tuple[str, str]] = []
    with contextlib.suppress(Exception):
        raw_cookies = context.cookies(["https://grok.com", "https://assets.grok.com"])
        for cookie in raw_cookies or []:
            name = str(cookie.get("name") or "").strip()
            value = str(cookie.get("value") or "").strip()
            if not name:
                continue
            cookie_pairs.append((name, value))

    deduped_cookie_pairs: list[tuple[str, str]] = []
    seen_cookie_names: set[str] = set()
    for name, value in cookie_pairs:
        if name in seen_cookie_names:
            continue
        seen_cookie_names.add(name)
        deduped_cookie_pairs.append((name, value))

    cookie_header = "; ".join(f"{name}={value}" for name, value in deduped_cookie_pairs if value)
    return GrokDownloadAuth(cookie_header=cookie_header, user_agent=user_agent)


def build_versions_url(asset_id: str) -> str:
    """Return the Grok versions endpoint for one asset."""
    return f"https://grok.com/rest/assets/{asset_id}/versions"


def extract_file_id_from_href(href: str) -> str:
    """Extract the Grok file ID from a file details link."""
    parsed = urlsplit(href or "")
    if parsed.netloc and parsed.netloc.lower() != "grok.com":
        return ""
    query = parsed.query or ""
    for fragment in query.split("&"):
        key, _separator, value = fragment.partition("=")
        if key == "file":
            candidate = value.strip().lower()
            if re.fullmatch(r"[0-9a-f-]{36}", candidate):
                return candidate
    return ""


def candidate_from_url(url: str, media_tag: str) -> GrokMediaCandidate | None:
    """Convert a page asset URL into a stable candidate record."""
    cleaned_url = (url or "").split("#", 1)[0].strip()
    if not cleaned_url:
        return None

    parsed = urlsplit(cleaned_url)
    if "assets.grok.com" not in parsed.netloc.lower():
        return None

    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) < 2:
        return None

    asset_id = path_parts[-2].lower()
    asset_name = normalize_asset_name(path_parts[-1])
    if not asset_id or not asset_name:
        return None

    if "profile" in asset_name and "picture" in asset_name:
        return None

    media_kind = classify_media_kind(path_parts[-1], media_tag)
    return GrokMediaCandidate(
        source_url=cleaned_url,
        asset_id=asset_id,
        asset_name=asset_name,
        media_kind=media_kind,
        identity=f"{asset_id}/{asset_name}",
        preview_url=cleaned_url if media_kind == "image" and is_preview_asset_url(cleaned_url) else "",
    )


def candidate_from_file_reference(file_id: str, preview_url: str, media_tag: str) -> GrokMediaCandidate | None:
    """Build a candidate from a file details link plus its thumbnail or video node."""
    asset_id = (file_id or "").strip().lower()
    if not asset_id:
        return None

    preview_candidate = candidate_from_url(preview_url, media_tag) if preview_url else None
    asset_name = preview_candidate.asset_name if preview_candidate is not None else "asset"
    media_kind = preview_candidate.media_kind if preview_candidate is not None else classify_media_kind("", media_tag)
    identity = preview_candidate.identity if preview_candidate is not None else f"{asset_id}/{asset_name}"
    return GrokMediaCandidate(
        source_url=preview_candidate.source_url if preview_candidate is not None else "",
        asset_id=asset_id,
        asset_name=asset_name,
        media_kind=media_kind,
        identity=identity,
        preview_url=preview_candidate.preview_url if preview_candidate is not None else preview_url,
    )


class GrokMediaCatalog:
    """Track known Grok assets and local content hashes."""

    def __init__(self, target_dir: Path) -> None:
        self.target_dir = target_dir
        self.catalog_path = target_dir / GROK_CATALOG_FILENAME
        self.entries_by_identity: dict[str, GrokCatalogEntry] = {}
        self.hash_to_relative_path: dict[str, str] = {}
        self.first_seen_by_relative_path: dict[str, str] = {}
        self.media_kind_by_relative_path: dict[str, str] = {}
        self.dirty = False
        self._lock = RLock()

    @classmethod
    def build(cls, target_dir: Path) -> GrokMediaCatalog:
        """Load the persisted Grok catalog or rebuild it from local files."""
        catalog = cls(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        if catalog._load():
            catalog.flush()
            return catalog

        catalog._rebuild_from_disk()
        catalog.flush()
        return catalog

    def contains_identity(self, identity: str) -> bool:
        """Return whether the given Grok asset is already known locally."""
        with self._lock:
            entry = self.entries_by_identity.get(identity)
            if entry is None:
                return False
            if self._entry_points_to_valid_file_unlocked(entry):
                return True
            self._drop_catalog_entry_unlocked(identity)
            self._flush_unlocked()
            return False

    def contains_asset_id(self, asset_id: str) -> bool:
        """Return whether any healthy catalog entry already exists for one Grok asset ID."""
        normalized_asset_id = str(asset_id or "").strip().lower()
        if not normalized_asset_id:
            return False

        with self._lock:
            stale_identities: list[str] = []
            for identity, entry in self.entries_by_identity.items():
                if extract_status_id_from_identity(identity) != normalized_asset_id:
                    continue
                if self._entry_points_to_valid_file_unlocked(entry):
                    return True
                stale_identities.append(identity)

            for identity in stale_identities:
                self._drop_catalog_entry_unlocked(identity)
            if stale_identities:
                self._flush_unlocked()
            return False

    def has_recorded_asset_id(self, asset_id: str) -> bool:
        """Return whether the loaded catalog records one Grok asset ID."""
        normalized_asset_id = str(asset_id or "").strip().lower()
        if not normalized_asset_id:
            return False

        with self._lock:
            return any(
                extract_status_id_from_identity(identity) == normalized_asset_id
                for identity in self.entries_by_identity
            )

    def get_entry(self, identity: str) -> GrokCatalogEntry | None:
        """Return one catalog entry when it still points to valid local content."""
        with self._lock:
            entry = self.entries_by_identity.get(identity)
            if entry is None:
                return None
            if self._entry_points_to_valid_file_unlocked(entry):
                return entry
            self._drop_catalog_entry_unlocked(identity)
            self._flush_unlocked()
            return None

    def lookup_relative_path_by_hash(self, content_sha256: str) -> str | None:
        """Return the local file path for an already cached payload hash."""
        with self._lock:
            relative_path = self.hash_to_relative_path.get(content_sha256)
            if not relative_path:
                return None

            absolute_path = self.target_dir / relative_path
            media_kind = self.media_kind_by_relative_path.get(relative_path, "image")
            if validate_media_file(absolute_path, media_kind):
                return relative_path

            self.hash_to_relative_path.pop(content_sha256, None)
            self.media_kind_by_relative_path.pop(relative_path, None)
            self.first_seen_by_relative_path.pop(relative_path, None)
            self.dirty = True
            self._flush_unlocked()
            return None

    def register_download(
        self,
        candidate: GrokMediaCandidate,
        relative_path: str,
        content_sha256: str,
        content_bytes: int,
        seen_at: str | None = None,
    ) -> None:
        """Register a newly saved Grok asset."""
        with self._lock:
            self._register_download_unlocked(
                candidate=candidate,
                relative_path=relative_path,
                content_sha256=content_sha256,
                content_bytes=content_bytes,
                seen_at=seen_at,
            )

    def register_alias(
        self,
        candidate: GrokMediaCandidate,
        relative_path: str,
        content_sha256: str,
        seen_at: str | None = None,
    ) -> None:
        """Register a new Grok identity that points at existing local content."""
        with self._lock:
            self._register_alias_unlocked(
                candidate=candidate,
                relative_path=relative_path,
                content_sha256=content_sha256,
                seen_at=seen_at,
            )

    def summarize(self) -> tuple[int, int, int]:
        """Return counts of unique cached files, images, and videos."""
        with self._lock:
            seen_relative_paths = set(self.media_kind_by_relative_path)
            image_count = sum(
                1
                for relative_path in seen_relative_paths
                if self.media_kind_by_relative_path.get(relative_path) == "image"
            )
            video_count = sum(
                1
                for relative_path in seen_relative_paths
                if self.media_kind_by_relative_path.get(relative_path) == "video"
            )
            return len(seen_relative_paths), image_count, video_count

    def snapshot_entries(self) -> list[GrokCatalogEntry]:
        """Return a stable snapshot of catalog entries for single-threaded inspection."""
        with self._lock:
            return list(self.entries_by_identity.values())

    def flush(self) -> None:
        """Persist the Grok catalog when it changed."""
        with self._lock:
            self._flush_unlocked()

    def _load(self) -> bool:
        """Load Parquet state, preferring a valid legacy JSON file for one-time migration."""
        with self._lock:
            rows = read_parquet_rows(self.catalog_path)
            legacy_path = self.target_dir / LEGACY_GROK_CATALOG_FILENAME
            migrated_legacy = False
            if rows is None and legacy_path.exists():
                try:
                    payload = json.loads(legacy_path.read_text())
                except (OSError, json.JSONDecodeError):
                    payload = None
                raw_entries = payload.get("entries") if isinstance(payload, dict) else None
                if isinstance(raw_entries, dict):
                    raw_entries = list(raw_entries.values())
                if isinstance(raw_entries, list):
                    rows = [dict(row) for row in raw_entries if isinstance(row, dict)]
                    migrated_legacy = True

            if rows is None:
                return False

            loaded_any = False
            for row in rows:
                relative_path = str(row.get("relative_path") or "").strip()
                identity = str(row.get("identity") or "").strip()
                content_sha256 = str(row.get("content_sha256") or "").strip()
                media_kind = str(row.get("media_kind") or "").strip() or "image"
                if not relative_path or not identity or not content_sha256:
                    continue

                absolute_path = self.target_dir / relative_path
                content_bytes = int(row.get("content_bytes") or 0)
                if not absolute_path.is_file() or (
                    content_bytes > 0 and absolute_path.stat().st_size != content_bytes
                ):
                    self.dirty = True
                    continue

                entry = GrokCatalogEntry(
                    identity=identity,
                    relative_path=relative_path,
                    media_kind=media_kind,
                    content_sha256=content_sha256,
                    content_bytes=content_bytes or absolute_path.stat().st_size,
                    source_url=str(row.get("source_url") or ""),
                    first_seen_at=normalize_catalog_timestamp(
                        str(row.get("first_seen_at") or ""),
                        fallback=utc_now(),
                    ),
                    last_seen_at=normalize_catalog_timestamp(
                        str(row.get("last_seen_at") or ""),
                        fallback=utc_now(),
                    ),
                )
                canonical_relative_path = self._preferred_relative_path(
                    content_sha256,
                    relative_path,
                    entry.first_seen_at,
                )
                if canonical_relative_path != relative_path:
                    entry = GrokCatalogEntry(
                        identity=entry.identity,
                        relative_path=canonical_relative_path,
                        media_kind=entry.media_kind,
                        content_sha256=entry.content_sha256,
                        content_bytes=entry.content_bytes,
                        source_url=entry.source_url,
                        first_seen_at=entry.first_seen_at,
                        last_seen_at=entry.last_seen_at,
                    )
                    self.dirty = True
                self.entries_by_identity[identity] = entry
                self.hash_to_relative_path[content_sha256] = canonical_relative_path
                self.first_seen_by_relative_path[canonical_relative_path] = min(
                    entry.first_seen_at,
                    self.first_seen_by_relative_path.get(canonical_relative_path, entry.first_seen_at),
                )
                self.media_kind_by_relative_path[canonical_relative_path] = media_kind
                loaded_any = True

            if migrated_legacy:
                self.dirty = True
            return loaded_any or migrated_legacy

    def _rebuild_from_disk(self) -> None:
        """Recreate the Grok catalog from existing local files."""
        with self._lock:
            for file_path in sorted(self.target_dir.iterdir(), key=lambda item: item.name):
                if not file_path.is_file() or file_path.name.startswith("."):
                    continue

                media_kind = classify_existing_file(file_path)
                if not media_kind or not validate_media_file(file_path, media_kind):
                    continue

                identity = derive_identity_from_filename(file_path.name)
                if not identity:
                    continue

                content_sha256 = compute_file_sha256(file_path)
                relative_path = file_path.relative_to(self.target_dir).as_posix()
                first_seen_at = (
                    derive_seen_at_from_filename(file_path.name)
                    or isoformat_from_timestamp(file_path.stat().st_mtime)
                )
                canonical_relative_path = self._preferred_relative_path(
                    content_sha256,
                    relative_path,
                    first_seen_at,
                )
                self.entries_by_identity[identity] = GrokCatalogEntry(
                    identity=identity,
                    relative_path=canonical_relative_path,
                    media_kind=media_kind,
                    content_sha256=content_sha256,
                    content_bytes=file_path.stat().st_size,
                    source_url="",
                    first_seen_at=first_seen_at,
                    last_seen_at=first_seen_at,
                )
                self.hash_to_relative_path[content_sha256] = canonical_relative_path
                self.first_seen_by_relative_path[canonical_relative_path] = min(
                    first_seen_at,
                    self.first_seen_by_relative_path.get(canonical_relative_path, first_seen_at),
                )
                self.media_kind_by_relative_path[canonical_relative_path] = media_kind
                self.dirty = True

    def _preferred_relative_path(
        self,
        content_sha256: str,
        candidate_relative_path: str,
        candidate_first_seen_at: str,
    ) -> str:
        """Return the canonical local file for one content hash, preferring the earliest asset."""
        existing_relative_path = self.hash_to_relative_path.get(content_sha256)
        if not existing_relative_path:
            return candidate_relative_path

        existing_first_seen_at = self.first_seen_by_relative_path.get(
            existing_relative_path,
            candidate_first_seen_at,
        )
        if compare_seen_at(candidate_first_seen_at, existing_first_seen_at) < 0:
            return candidate_relative_path
        if compare_seen_at(candidate_first_seen_at, existing_first_seen_at) > 0:
            return existing_relative_path
        return min(existing_relative_path, candidate_relative_path)

    def _entry_points_to_valid_file_unlocked(self, entry: GrokCatalogEntry) -> bool:
        """Return whether one catalog entry still points at healthy local media."""
        absolute_path = self.target_dir / entry.relative_path
        return validate_media_file(
            absolute_path,
            media_kind=entry.media_kind,
            expected_bytes=entry.content_bytes,
        )

    def _register_download_unlocked(
        self,
        candidate: GrokMediaCandidate,
        relative_path: str,
        content_sha256: str,
        content_bytes: int,
        seen_at: str | None = None,
    ) -> None:
        """Register a newly saved Grok asset while the catalog lock is held."""
        timestamp = normalize_catalog_timestamp(seen_at or "", fallback=utc_now())
        canonical_relative_path = self._preferred_relative_path(content_sha256, relative_path, timestamp)
        self.entries_by_identity[candidate.identity] = GrokCatalogEntry(
            identity=candidate.identity,
            relative_path=canonical_relative_path,
            media_kind=candidate.media_kind,
            content_sha256=content_sha256,
            content_bytes=content_bytes,
            source_url=candidate.source_url,
            first_seen_at=timestamp,
            last_seen_at=timestamp,
        )
        self.hash_to_relative_path[content_sha256] = canonical_relative_path
        self.first_seen_by_relative_path[canonical_relative_path] = min(
            timestamp,
            self.first_seen_by_relative_path.get(canonical_relative_path, timestamp),
        )
        self.media_kind_by_relative_path[canonical_relative_path] = candidate.media_kind
        self.dirty = True

    def _register_alias_unlocked(
        self,
        candidate: GrokMediaCandidate,
        relative_path: str,
        content_sha256: str,
        seen_at: str | None = None,
    ) -> None:
        """Register a new Grok identity that points at existing local content."""
        existing_entry = self.entries_by_identity.get(candidate.identity)
        timestamp = normalize_catalog_timestamp(seen_at or "", fallback=utc_now())
        canonical_relative_path = self._preferred_relative_path(content_sha256, relative_path, timestamp)
        first_seen_at = existing_entry.first_seen_at if existing_entry else timestamp
        content_bytes = (
            existing_entry.content_bytes
            if existing_entry
            else (self.target_dir / canonical_relative_path).stat().st_size
        )
        self.entries_by_identity[candidate.identity] = GrokCatalogEntry(
            identity=candidate.identity,
            relative_path=canonical_relative_path,
            media_kind=candidate.media_kind,
            content_sha256=content_sha256,
            content_bytes=content_bytes,
            source_url=candidate.source_url,
            first_seen_at=first_seen_at,
            last_seen_at=timestamp,
        )
        self.hash_to_relative_path[content_sha256] = canonical_relative_path
        self.first_seen_by_relative_path[canonical_relative_path] = min(
            first_seen_at,
            self.first_seen_by_relative_path.get(canonical_relative_path, first_seen_at),
        )
        self.media_kind_by_relative_path[canonical_relative_path] = candidate.media_kind
        self.dirty = True

    def _flush_unlocked(self) -> None:
        """Persist the catalog when it changed while the lock is held."""
        if not self.dirty:
            return

        rows = [
            {
                "schema_version": GROK_CATALOG_SCHEMA_VERSION,
                "identity": entry.identity,
                "relative_path": entry.relative_path,
                "media_kind": entry.media_kind,
                "content_sha256": entry.content_sha256,
                "content_bytes": entry.content_bytes,
                "source_url": entry.source_url,
                "first_seen_at": entry.first_seen_at,
                "last_seen_at": entry.last_seen_at,
            }
            for entry in sorted(self.entries_by_identity.values(), key=lambda item: item.identity)
        ]
        write_parquet_rows_atomic(self.catalog_path, rows, GROK_CATALOG_SCHEMA)
        retire_legacy_file(self.target_dir / LEGACY_GROK_CATALOG_FILENAME)
        self.dirty = False

    def _prune_unreferenced_relative_path_unlocked(self, relative_path: str) -> None:
        """Delete an obsolete local file once no catalog entries still reference it."""
        if not relative_path:
            return
        if any(entry.relative_path == relative_path for entry in self.entries_by_identity.values()):
            return

        absolute_path = self.target_dir / relative_path
        with contextlib.suppress(FileNotFoundError):
            absolute_path.unlink()

        self.media_kind_by_relative_path.pop(relative_path, None)
        self.first_seen_by_relative_path.pop(relative_path, None)
        if relative_path in set(self.hash_to_relative_path.values()):
            return
        self.dirty = True

    def _drop_catalog_entry_unlocked(self, identity: str) -> None:
        """Remove one obsolete catalog identity while the catalog lock is held."""
        entry = self.entries_by_identity.pop(identity, None)
        if entry is None:
            return

        remaining_same_hash = [
            candidate
            for candidate in self.entries_by_identity.values()
            if candidate.content_sha256 == entry.content_sha256
        ]
        if remaining_same_hash:
            preferred_entry = min(
                remaining_same_hash,
                key=lambda candidate: (parse_seen_at(candidate.first_seen_at), candidate.relative_path),
            )
            self.hash_to_relative_path[entry.content_sha256] = preferred_entry.relative_path
        else:
            self.hash_to_relative_path.pop(entry.content_sha256, None)

        self._prune_unreferenced_relative_path_unlocked(entry.relative_path)
        self.dirty = True


def isoformat_from_timestamp(timestamp: float) -> str:
    """Convert a filesystem timestamp into the catalog ISO format."""
    return datetime.fromtimestamp(timestamp, tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_seen_at(value: str) -> datetime:
    """Parse catalog timestamps robustly for canonical ordering."""
    parsed = parse_catalog_timestamp(value)
    if parsed is None:
        return datetime.max.replace(tzinfo=UTC)
    return parsed


def apply_preserved_file_timestamp(file_path: Path, timestamp_value: str) -> None:
    """Best-effort preserve the generated time on local files for photo-library imports."""
    parsed = parse_catalog_timestamp(timestamp_value)
    if parsed is None or not file_path.exists():
        return

    target_epoch = parsed.timestamp()
    with contextlib.suppress(OSError):
        current_mtime = file_path.stat().st_mtime
        if current_mtime > 0:
            target_epoch = min(target_epoch, current_mtime)

    with contextlib.suppress(OSError):
        os.utime(file_path, (target_epoch, target_epoch))

    set_file_path = shutil.which("SetFile")
    if not set_file_path:
        return

    local_timestamp = parsed.astimezone().strftime("%m/%d/%Y %H:%M:%S")
    for flag in ("-d", "-m"):
        try:
            completed = subprocess.run(
                [set_file_path, flag, local_timestamp, str(file_path)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError:
            continue
        if completed.returncode != 0:
            logger.warning(
                "SetFile timestamp update failed.",
                extra={
                    "file_path": str(file_path),
                    "timestamp": local_timestamp,
                    "flag": flag,
                    "stderr": (completed.stderr or "").strip(),
                },
            )


def compare_seen_at(left: str, right: str) -> int:
    """Compare two catalog timestamps."""
    left_dt = parse_seen_at(left)
    right_dt = parse_seen_at(right)
    if left_dt < right_dt:
        return -1
    if left_dt > right_dt:
        return 1
    return 0


def classify_existing_file(file_path: Path) -> str:
    """Return the media kind for an existing Grok file, or an empty string when unsupported."""
    suffix = file_path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        normalized_name = normalize_asset_name(file_path.name)
        if "profile" in normalized_name and "picture" in normalized_name:
            return ""
        return "image"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    return ""


def derive_identity_from_filename(filename: str) -> str:
    """Derive a stable Grok identity from the current flat-file naming convention."""
    stem = Path(filename).stem
    stamped_match = STAMPED_ASSET_FILENAME_PATTERN.match(stem)
    if stamped_match:
        asset_id = stamped_match.group("asset_id").lower()
        asset_name = normalize_asset_name(stamped_match.group("asset_name")) or default_asset_name_for_prefix(
            stamped_match.group("prefix")
        )
    else:
        legacy_match = LEGACY_ASSET_FILENAME_PATTERN.match(stem)
        if not legacy_match:
            return ""
        asset_id = legacy_match.group("asset_id").lower()
        asset_name = normalize_asset_name(legacy_match.group("asset_name"))

    if not asset_name or ("profile" in asset_name and "picture" in asset_name):
        return ""
    return f"{asset_id}/{asset_name}"


def build_destination_filename(candidate: GrokMediaCandidate, content_type: str) -> str:
    """Create a deterministic local filename for a new Grok asset."""
    extension = infer_extension(candidate.source_url, content_type, candidate.media_kind)
    prefix = "vid" if candidate.media_kind == "video" else "img"
    seen_at = preferred_seen_at(candidate)
    compact_timestamp = compact_timestamp_for_filename(seen_at, fallback="00010101T000000Z")
    asset_name = sanitize_filename_part(candidate.asset_name)
    return f"{prefix}_{compact_timestamp}_{candidate.asset_id}_{asset_name}{extension}"


def derive_seen_at_from_filename(filename: str) -> str:
    """Recover the remote creation timestamp encoded in stamped filenames."""
    stem = Path(filename).stem
    match = STAMPED_ASSET_FILENAME_PATTERN.match(stem)
    if not match:
        return ""
    return isoformat_from_compact_timestamp(match.group("timestamp"))


def prune_unreferenced_relative_path(catalog: GrokMediaCatalog, _target_dir: Path, relative_path: str) -> None:
    """Delete an obsolete local file once no catalog entries still reference it."""
    with catalog._lock:
        catalog._prune_unreferenced_relative_path_unlocked(relative_path)


def drop_catalog_entry(catalog: GrokMediaCatalog, _target_dir: Path, identity: str) -> None:
    """Remove one obsolete catalog identity and delete its file when unreferenced."""
    with catalog._lock:
        catalog._drop_catalog_entry_unlocked(identity)


def resolve_destination_path(target_dir: Path, preferred_filename: str) -> Path:
    """Return a writable destination path without clobbering existing content."""
    candidate_path = target_dir / preferred_filename
    if not candidate_path.exists():
        return candidate_path

    stem = candidate_path.stem
    suffix = candidate_path.suffix
    counter = 2
    while True:
        next_candidate = target_dir / f"{stem}-{counter}{suffix}"
        if not next_candidate.exists():
            return next_candidate
        counter += 1


class GrokDownloadManifest:
    """Persist resumable Grok download progress independently from the catalog."""

    def __init__(self, target_dir: Path) -> None:
        self.target_dir = target_dir
        self.manifest_path = target_dir / GROK_DOWNLOAD_MANIFEST_FILENAME
        self.entries_by_identity: dict[str, GrokManifestEntry] = {}
        self.dirty = False
        self._lock = RLock()

    @classmethod
    def build(cls, target_dir: Path, catalog: GrokMediaCatalog) -> GrokDownloadManifest:
        """Load and reconcile the persisted download manifest."""
        manifest = cls(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        manifest._load()
        manifest.reconcile_with_catalog(catalog)
        manifest.flush()
        return manifest

    def reconcile_with_catalog(self, catalog: GrokMediaCatalog) -> None:
        """Convert stale in-progress records into resumable or completed state."""
        with self._lock:
            for identity, entry in list(self.entries_by_identity.items()):
                catalog_entry = catalog.entries_by_identity.get(identity)
                if catalog_entry is not None:
                    entry.status = "completed"
                    entry.relative_path = catalog_entry.relative_path
                    entry.content_sha256 = catalog_entry.content_sha256
                    entry.content_bytes = catalog_entry.content_bytes
                    entry.temp_relative_path = ""
                    entry.last_error = ""
                    entry.updated_at = utc_now()
                    self.entries_by_identity[identity] = entry
                    self.dirty = True
                    continue

                if entry.status == "completed" and entry.relative_path:
                    completed_path = self.target_dir / entry.relative_path
                    if validate_media_file(completed_path, entry.media_kind, expected_bytes=entry.content_bytes):
                        content_sha256 = entry.content_sha256 or compute_file_sha256(completed_path)
                        content_bytes = entry.content_bytes or completed_path.stat().st_size
                        catalog.register_download(
                            candidate=GrokMediaCandidate(
                                source_url=entry.source_url,
                                asset_id=entry.asset_id,
                                asset_name=entry.asset_name,
                                media_kind=entry.media_kind,
                                identity=entry.identity,
                                created_at=entry.created_at,
                            ),
                            relative_path=entry.relative_path,
                            content_sha256=content_sha256,
                            content_bytes=content_bytes,
                            seen_at=entry.created_at,
                        )
                        entry.content_sha256 = content_sha256
                        entry.content_bytes = content_bytes
                        entry.updated_at = utc_now()
                        self.entries_by_identity[identity] = entry
                        self.dirty = True
                        continue
                    entry.status = "pending"
                    entry.relative_path = ""
                    entry.content_sha256 = ""
                    entry.content_bytes = 0
                    entry.updated_at = utc_now()
                    self.entries_by_identity[identity] = entry
                    self.dirty = True
                    continue

                if entry.status == "in_progress":
                    entry.status = "pending"
                    entry.updated_at = utc_now()
                    self.entries_by_identity[identity] = entry
                    self.dirty = True

    def prepare_candidates(
        self,
        candidates: list[GrokMediaCandidate],
        catalog: GrokMediaCatalog,
    ) -> list[GrokMediaCandidate]:
        """Update manifest metadata and return the candidates that still need work."""
        pending: list[GrokMediaCandidate] = []
        with self._lock:
            for candidate in candidates:
                entry = self.entries_by_identity.get(candidate.identity)
                if entry is None:
                    entry = GrokManifestEntry(
                        identity=candidate.identity,
                        asset_id=candidate.asset_id,
                        asset_name=candidate.asset_name,
                        media_kind=candidate.media_kind,
                        source_url=candidate.source_url,
                        expected_bytes=candidate.expected_bytes,
                        created_at=preferred_seen_at(candidate),
                        updated_at=utc_now(),
                    )
                    self.entries_by_identity[candidate.identity] = entry
                    self.dirty = True
                else:
                    entry.asset_id = candidate.asset_id
                    entry.asset_name = candidate.asset_name
                    entry.media_kind = candidate.media_kind
                    entry.source_url = candidate.source_url
                    entry.expected_bytes = candidate.expected_bytes
                    entry.created_at = preferred_seen_at(candidate, fallback=entry.created_at)
                    entry.updated_at = utc_now()
                    self.entries_by_identity[candidate.identity] = entry
                    self.dirty = True

                catalog_entry = catalog.get_entry(candidate.identity)
                if catalog_entry is not None:
                    entry.status = "completed"
                    entry.relative_path = catalog_entry.relative_path
                    entry.content_sha256 = catalog_entry.content_sha256
                    entry.content_bytes = catalog_entry.content_bytes
                    entry.temp_relative_path = ""
                    entry.last_error = ""
                    self.entries_by_identity[candidate.identity] = entry
                    self.dirty = True
                    continue

                if entry.status == "completed" and entry.relative_path:
                    completed_path = self.target_dir / entry.relative_path
                    if validate_media_file(completed_path, entry.media_kind, expected_bytes=entry.content_bytes):
                        continue
                    entry.status = "pending"
                    entry.relative_path = ""
                    entry.content_sha256 = ""
                    entry.content_bytes = 0
                    self.entries_by_identity[candidate.identity] = entry
                    self.dirty = True

                pending.append(candidate)

        self.flush()
        return pending

    def temp_path_for(self, candidate: GrokMediaCandidate) -> Path:
        """Return the absolute temporary path for one candidate."""
        relative_path = build_partial_relative_path(candidate)
        with self._lock:
            entry = self.entries_by_identity.get(candidate.identity)
            if entry is None:
                entry = GrokManifestEntry(
                    identity=candidate.identity,
                    asset_id=candidate.asset_id,
                    asset_name=candidate.asset_name,
                    media_kind=candidate.media_kind,
                    source_url=candidate.source_url,
                    expected_bytes=candidate.expected_bytes,
                    created_at=preferred_seen_at(candidate),
                    updated_at=utc_now(),
                )
            entry.temp_relative_path = entry.temp_relative_path or relative_path
            entry.updated_at = utc_now()
            self.entries_by_identity[candidate.identity] = entry
            self.dirty = True
            relative_path = entry.temp_relative_path
        self.flush()
        return self.target_dir / relative_path

    def mark_in_progress(self, candidate: GrokMediaCandidate, temp_relative_path: str) -> None:
        """Record that one worker has started or resumed downloading a candidate."""
        with self._lock:
            entry = self.entries_by_identity.get(candidate.identity)
            if entry is None:
                entry = GrokManifestEntry(
                    identity=candidate.identity,
                    asset_id=candidate.asset_id,
                    asset_name=candidate.asset_name,
                    media_kind=candidate.media_kind,
                    source_url=candidate.source_url,
                    expected_bytes=candidate.expected_bytes,
                    created_at=preferred_seen_at(candidate),
                )
            entry.status = "in_progress"
            entry.asset_id = candidate.asset_id
            entry.asset_name = candidate.asset_name
            entry.media_kind = candidate.media_kind
            entry.source_url = candidate.source_url
            entry.expected_bytes = candidate.expected_bytes
            entry.temp_relative_path = temp_relative_path
            entry.attempts += 1
            entry.updated_at = utc_now()
            self.entries_by_identity[candidate.identity] = entry
            self.dirty = True
        self.flush()

    def mark_pending_resume(self, candidate: GrokMediaCandidate, temp_relative_path: str, error: str = "") -> None:
        """Record a resumable partial download that should continue next run."""
        with self._lock:
            entry = self.entries_by_identity.get(candidate.identity)
            if entry is None:
                return
            entry.status = "pending"
            entry.temp_relative_path = temp_relative_path
            entry.last_error = error
            entry.updated_at = utc_now()
            self.entries_by_identity[candidate.identity] = entry
            self.dirty = True
        self.flush()

    def mark_failed(self, candidate: GrokMediaCandidate, temp_relative_path: str, error: str) -> None:
        """Record a failed download attempt that still remains eligible for retry."""
        with self._lock:
            entry = self.entries_by_identity.get(candidate.identity)
            if entry is None:
                return
            entry.status = "failed"
            entry.temp_relative_path = temp_relative_path
            entry.last_error = error
            entry.updated_at = utc_now()
            self.entries_by_identity[candidate.identity] = entry
            self.dirty = True
        self.flush()

    def mark_size_skipped(self, candidate: GrokMediaCandidate, temp_relative_path: str, error: str) -> None:
        """Record an asset skipped by the cache size limit without retaining a partial file."""
        with self._lock:
            entry = self.entries_by_identity.get(candidate.identity)
            if entry is None:
                return
            entry.status = "size_skipped"
            entry.temp_relative_path = temp_relative_path
            entry.last_error = error
            entry.updated_at = utc_now()
            self.entries_by_identity[candidate.identity] = entry
            self.dirty = True
        self.flush()

    def mark_completed(
        self,
        candidate: GrokMediaCandidate,
        relative_path: str,
        content_sha256: str,
        content_bytes: int,
    ) -> None:
        """Record a successfully committed local file."""
        with self._lock:
            entry = self.entries_by_identity.get(candidate.identity)
            if entry is None:
                entry = GrokManifestEntry(
                    identity=candidate.identity,
                    asset_id=candidate.asset_id,
                    asset_name=candidate.asset_name,
                    media_kind=candidate.media_kind,
                    source_url=candidate.source_url,
                    expected_bytes=candidate.expected_bytes,
                    created_at=preferred_seen_at(candidate),
                )
            entry.status = "completed"
            entry.relative_path = relative_path
            entry.temp_relative_path = ""
            entry.content_sha256 = content_sha256
            entry.content_bytes = content_bytes
            entry.last_error = ""
            entry.updated_at = utc_now()
            self.entries_by_identity[candidate.identity] = entry
            self.dirty = True
        self.flush()

    def flush(self) -> None:
        """Persist the manifest when it changed."""
        with self._lock:
            if not self.dirty:
                return
            rows = [
                {
                    "schema_version": GROK_DOWNLOAD_MANIFEST_SCHEMA_VERSION,
                    "identity": entry.identity,
                    "asset_id": entry.asset_id,
                    "asset_name": entry.asset_name,
                    "media_kind": entry.media_kind,
                    "source_url": entry.source_url,
                    "status": entry.status,
                    "relative_path": entry.relative_path,
                    "temp_relative_path": entry.temp_relative_path,
                    "content_sha256": entry.content_sha256,
                    "content_bytes": entry.content_bytes,
                    "expected_bytes": entry.expected_bytes,
                    "created_at": entry.created_at,
                    "attempts": entry.attempts,
                    "last_error": entry.last_error,
                    "updated_at": entry.updated_at,
                }
                for entry in sorted(self.entries_by_identity.values(), key=lambda item: item.identity)
            ]
            write_parquet_rows_atomic(
                self.manifest_path,
                rows,
                GROK_DOWNLOAD_MANIFEST_SCHEMA,
            )
            retire_legacy_file(self.target_dir / LEGACY_GROK_DOWNLOAD_MANIFEST_FILENAME)
            self.dirty = False

    def _load(self) -> None:
        """Load Parquet state and import a valid legacy JSON manifest when present."""
        rows = read_parquet_rows(self.manifest_path)
        legacy_path = self.target_dir / LEGACY_GROK_DOWNLOAD_MANIFEST_FILENAME
        migrated_legacy = False
        if rows is None and legacy_path.exists():
            try:
                payload = json.loads(legacy_path.read_text())
            except (OSError, json.JSONDecodeError):
                payload = None
            raw_entries = payload.get("entries") if isinstance(payload, dict) else None
            if isinstance(raw_entries, dict):
                raw_entries = list(raw_entries.values())
            if isinstance(raw_entries, list):
                rows = [dict(row) for row in raw_entries if isinstance(row, dict)]
                migrated_legacy = True

        for row in rows or []:
            identity = str(row.get("identity") or "").strip()
            if not identity:
                continue
            self.entries_by_identity[identity] = GrokManifestEntry(
                identity=identity,
                asset_id=str(row.get("asset_id") or "").strip(),
                asset_name=str(row.get("asset_name") or "").strip(),
                media_kind=str(row.get("media_kind") or "").strip() or "image",
                source_url=str(row.get("source_url") or "").strip(),
                status=str(row.get("status") or "pending").strip() or "pending",
                relative_path=str(row.get("relative_path") or "").strip(),
                temp_relative_path=str(row.get("temp_relative_path") or "").strip(),
                content_sha256=str(row.get("content_sha256") or "").strip(),
                content_bytes=int(row.get("content_bytes") or 0),
                expected_bytes=int(row.get("expected_bytes") or 0),
                created_at=normalize_catalog_timestamp(str(row.get("created_at") or "").strip(), fallback=""),
                attempts=int(row.get("attempts") or 0),
                last_error=str(row.get("last_error") or "").strip(),
                updated_at=normalize_catalog_timestamp(str(row.get("updated_at") or "").strip(), fallback=utc_now()),
            )
        if migrated_legacy:
            self.dirty = True


class GrokWorkQueue:
    """Persist discovered Grok assets so scanning and downloading stay decoupled."""

    def __init__(self, target_dir: Path) -> None:
        self.target_dir = target_dir
        self.queue_path = target_dir / GROK_WORK_QUEUE_FILENAME
        self.entries_by_asset_id: dict[str, GrokWorkQueueEntry] = {}
        self.dirty = False
        self._lock = RLock()

    @classmethod
    def build(cls, target_dir: Path, catalog: GrokMediaCatalog) -> GrokWorkQueue:
        """Load and reconcile the persisted Grok work queue."""
        queue = cls(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        queue._load()
        queue.reconcile(catalog)
        queue.flush()
        return queue

    def reconcile(self, catalog: GrokMediaCatalog) -> None:
        """Repair transient queue states after crashes or forced shutdowns."""
        with self._lock:
            for asset_id, entry in list(self.entries_by_asset_id.items()):
                if catalog.has_recorded_asset_id(asset_id):
                    entry.status = "completed"
                    entry.last_error = ""
                    entry.updated_at = utc_now()
                    self.entries_by_asset_id[asset_id] = entry
                    self.dirty = True
                    continue

                if entry.status in {"submitted", "download_failed"}:
                    entry.status = "ready" if candidate_is_downloadable(self._entry_to_candidate(entry)) else "discovered"
                    entry.updated_at = utc_now()
                    self.entries_by_asset_id[asset_id] = entry
                    self.dirty = True
                    continue

                if entry.status == "completed":
                    entry.status = "ready" if candidate_is_downloadable(self._entry_to_candidate(entry)) else "discovered"
                    entry.updated_at = utc_now()
                    self.entries_by_asset_id[asset_id] = entry
                    self.dirty = True
                    continue

                if entry.status in {"resolving", "resolution_failed"}:
                    entry.status = "discovered"
                    entry.updated_at = utc_now()
                    self.entries_by_asset_id[asset_id] = entry
                    self.dirty = True

    def total_count(self) -> int:
        """Return the number of tracked Grok asset IDs."""
        with self._lock:
            return len(self.entries_by_asset_id)

    def has_pending_pipeline_work(self) -> bool:
        """Return whether unresolved or unsent queue items remain."""
        with self._lock:
            return any(
                entry.status in {"discovered", "resolution_failed", "ready", "submitted"}
                for entry in self.entries_by_asset_id.values()
            )

    def register_discovered(self, candidates: list[GrokMediaCandidate]) -> int:
        """Persist newly discovered page assets without blocking downloads."""
        newly_added = 0
        with self._lock:
            for candidate in candidates:
                asset_id = str(candidate.asset_id or "").strip().lower()
                if not asset_id:
                    continue

                entry = self.entries_by_asset_id.get(asset_id)
                if entry is None:
                    entry = GrokWorkQueueEntry(
                        asset_id=asset_id,
                        identity=candidate.identity,
                        asset_name=candidate.asset_name,
                        media_kind=candidate.media_kind,
                        source_url=candidate.source_url,
                        preview_url=candidate.preview_url,
                        expected_bytes=candidate.expected_bytes,
                        created_at=preferred_seen_at(candidate),
                        discovered_at=utc_now(),
                        updated_at=utc_now(),
                        status="discovered",
                    )
                    self.entries_by_asset_id[asset_id] = entry
                    self.dirty = True
                    newly_added += 1
                    continue

                entry.identity = entry.identity or candidate.identity
                entry.asset_name = entry.asset_name or candidate.asset_name
                entry.media_kind = entry.media_kind or candidate.media_kind or "image"
                entry.preview_url = entry.preview_url or candidate.preview_url
                if not entry.source_url or is_preview_asset_url(entry.source_url):
                    entry.source_url = candidate.source_url or entry.source_url
                entry.expected_bytes = max(entry.expected_bytes, candidate.expected_bytes)
                entry.created_at = preferred_seen_at(candidate, fallback=entry.created_at)
                entry.updated_at = utc_now()
                if entry.status in {"resolution_failed", "size_skipped"}:
                    entry.status = "discovered"
                self.entries_by_asset_id[asset_id] = entry
                self.dirty = True

        self.flush()
        return newly_added

    def claim_for_resolution(
        self,
        limit: int,
        excluded_asset_ids: set[str] | None = None,
    ) -> list[GrokMediaCandidate]:
        """Claim raw discoveries that still need canonical download URLs."""
        excluded = {str(asset_id or "").strip().lower() for asset_id in (excluded_asset_ids or set())}
        claimed: list[GrokMediaCandidate] = []
        with self._lock:
            ordered_entries = sorted(
                self.entries_by_asset_id.values(),
                key=lambda entry: (entry.created_at or entry.discovered_at or "", entry.asset_id),
                reverse=True,
            )
            for entry in ordered_entries:
                if len(claimed) >= limit:
                    break
                if entry.asset_id in excluded:
                    continue
                if entry.status not in {"discovered", "resolution_failed"}:
                    continue
                entry.status = "resolving"
                entry.resolution_attempts += 1
                entry.last_error = ""
                entry.updated_at = utc_now()
                self.entries_by_asset_id[entry.asset_id] = entry
                self.dirty = True
                claimed.append(self._entry_to_candidate(entry))

        self.flush()
        return claimed

    def mark_resolved(self, asset_id: str, candidate: GrokMediaCandidate) -> None:
        """Persist the canonical download metadata for one asset."""
        normalized_asset_id = str(asset_id or candidate.asset_id or "").strip().lower()
        if not normalized_asset_id:
            return

        with self._lock:
            entry = self.entries_by_asset_id.get(normalized_asset_id) or GrokWorkQueueEntry(asset_id=normalized_asset_id)
            entry.identity = candidate.identity
            entry.asset_name = candidate.asset_name
            entry.media_kind = candidate.media_kind
            entry.source_url = candidate.source_url
            entry.preview_url = candidate.preview_url
            entry.expected_bytes = candidate.expected_bytes
            entry.created_at = preferred_seen_at(candidate, fallback=entry.created_at)
            entry.discovered_at = entry.discovered_at or utc_now()
            entry.updated_at = utc_now()
            entry.status = "ready"
            entry.last_error = ""
            self.entries_by_asset_id[normalized_asset_id] = entry
            self.dirty = True
        self.flush()

    def mark_resolution_failed(self, asset_id: str, error: str) -> None:
        """Persist a resolution failure so the queue can retry or skip deliberately."""
        normalized_asset_id = str(asset_id or "").strip().lower()
        if not normalized_asset_id:
            return

        with self._lock:
            entry = self.entries_by_asset_id.get(normalized_asset_id)
            if entry is None:
                return
            entry.status = (
                "skipped"
                if entry.resolution_attempts >= GROK_QUEUE_RESOLUTION_RETRY_LIMIT
                else "resolution_failed"
            )
            entry.last_error = error
            entry.updated_at = utc_now()
            self.entries_by_asset_id[normalized_asset_id] = entry
            self.dirty = True
        self.flush()

    def claim_ready_for_download(
        self,
        limit: int,
        excluded_asset_ids: set[str] | None = None,
    ) -> list[GrokMediaCandidate]:
        """Claim resolved queue entries and hand them to download workers."""
        excluded = {str(asset_id or "").strip().lower() for asset_id in (excluded_asset_ids or set())}
        claimed: list[GrokMediaCandidate] = []
        with self._lock:
            ordered_entries = sorted(
                self.entries_by_asset_id.values(),
                key=lambda entry: (entry.created_at or entry.discovered_at or "", entry.asset_id),
                reverse=True,
            )
            for entry in ordered_entries:
                if len(claimed) >= limit:
                    break
                if entry.asset_id in excluded:
                    continue
                if entry.status != "ready":
                    continue
                entry.status = "submitted"
                entry.download_attempts += 1
                entry.last_error = ""
                entry.updated_at = utc_now()
                self.entries_by_asset_id[entry.asset_id] = entry
                self.dirty = True
                claimed.append(self._entry_to_candidate(entry))

        self.flush()
        return claimed

    def mark_completed(self, candidate: GrokMediaCandidate) -> None:
        """Persist that one asset has been fully cached or aliased locally."""
        normalized_asset_id = str(candidate.asset_id or "").strip().lower()
        if not normalized_asset_id:
            return

        with self._lock:
            entry = self.entries_by_asset_id.get(normalized_asset_id) or GrokWorkQueueEntry(asset_id=normalized_asset_id)
            entry.identity = candidate.identity
            entry.asset_name = candidate.asset_name
            entry.media_kind = candidate.media_kind
            entry.source_url = candidate.source_url
            entry.preview_url = candidate.preview_url
            entry.expected_bytes = candidate.expected_bytes
            entry.created_at = preferred_seen_at(candidate, fallback=entry.created_at)
            entry.discovered_at = entry.discovered_at or utc_now()
            entry.updated_at = utc_now()
            entry.status = "completed"
            entry.last_error = ""
            self.entries_by_asset_id[normalized_asset_id] = entry
            self.dirty = True
        self.flush()

    def mark_download_failed(self, candidate: GrokMediaCandidate, error: str) -> None:
        """Persist a worker failure without losing the resolved download URL."""
        normalized_asset_id = str(candidate.asset_id or "").strip().lower()
        if not normalized_asset_id:
            return

        with self._lock:
            entry = self.entries_by_asset_id.get(normalized_asset_id)
            if entry is None:
                return
            entry.status = "download_failed"
            entry.last_error = error
            entry.updated_at = utc_now()
            self.entries_by_asset_id[normalized_asset_id] = entry
            self.dirty = True
        self.flush()

    def mark_size_skipped(self, candidate: GrokMediaCandidate, error: str) -> None:
        """Persist a size-limited asset as skipped until the next discovery pass."""
        normalized_asset_id = str(candidate.asset_id or "").strip().lower()
        if not normalized_asset_id:
            return

        with self._lock:
            entry = self.entries_by_asset_id.get(normalized_asset_id)
            if entry is None:
                return
            entry.status = "size_skipped"
            entry.last_error = error
            entry.updated_at = utc_now()
            self.entries_by_asset_id[normalized_asset_id] = entry
            self.dirty = True
        self.flush()

    def mark_download_interrupted(self, candidate: GrokMediaCandidate, error: str) -> None:
        """Return an interrupted worker item to the ready queue for resume."""
        normalized_asset_id = str(candidate.asset_id or "").strip().lower()
        if not normalized_asset_id:
            return

        with self._lock:
            entry = self.entries_by_asset_id.get(normalized_asset_id)
            if entry is None:
                return
            entry.status = "ready"
            entry.last_error = error
            entry.updated_at = utc_now()
            self.entries_by_asset_id[normalized_asset_id] = entry
            self.dirty = True
        self.flush()

    def flush(self) -> None:
        """Persist the work queue when it changed."""
        with self._lock:
            if not self.dirty:
                return

            rows = [
                {
                    "schema_version": GROK_WORK_QUEUE_SCHEMA_VERSION,
                    "asset_id": entry.asset_id,
                    "identity": entry.identity,
                    "asset_name": entry.asset_name,
                    "media_kind": entry.media_kind,
                    "source_url": entry.source_url,
                    "preview_url": entry.preview_url,
                    "expected_bytes": entry.expected_bytes,
                    "created_at": entry.created_at,
                    "discovered_at": entry.discovered_at,
                    "updated_at": entry.updated_at,
                    "status": entry.status,
                    "resolution_attempts": entry.resolution_attempts,
                    "download_attempts": entry.download_attempts,
                    "last_error": entry.last_error,
                }
                for entry in sorted(self.entries_by_asset_id.values(), key=lambda item: item.asset_id)
            ]
            write_parquet_rows_atomic(self.queue_path, rows, GROK_WORK_QUEUE_SCHEMA)
            retire_legacy_file(self.target_dir / LEGACY_GROK_WORK_QUEUE_FILENAME)
            self.dirty = False

    def _load(self) -> None:
        """Load Parquet state and import a valid legacy JSON work queue when present."""
        rows = read_parquet_rows(self.queue_path)
        legacy_path = self.target_dir / LEGACY_GROK_WORK_QUEUE_FILENAME
        migrated_legacy = False
        if rows is None and legacy_path.exists():
            try:
                payload = json.loads(legacy_path.read_text())
            except (OSError, json.JSONDecodeError):
                payload = None
            raw_entries = payload.get("entries") if isinstance(payload, dict) else None
            if isinstance(raw_entries, dict):
                raw_entries = list(raw_entries.values())
            if isinstance(raw_entries, list):
                rows = [dict(row) for row in raw_entries if isinstance(row, dict)]
                migrated_legacy = True

        for row in rows or []:
            asset_id = str(row.get("asset_id") or "").strip().lower()
            if not asset_id:
                continue
            self.entries_by_asset_id[asset_id] = GrokWorkQueueEntry(
                asset_id=asset_id,
                identity=str(row.get("identity") or "").strip(),
                asset_name=str(row.get("asset_name") or "").strip(),
                media_kind=str(row.get("media_kind") or "").strip() or "image",
                source_url=str(row.get("source_url") or "").strip(),
                preview_url=str(row.get("preview_url") or "").strip(),
                expected_bytes=int(row.get("expected_bytes") or 0),
                created_at=normalize_catalog_timestamp(str(row.get("created_at") or "").strip(), fallback=""),
                discovered_at=normalize_catalog_timestamp(str(row.get("discovered_at") or "").strip(), fallback=""),
                updated_at=normalize_catalog_timestamp(str(row.get("updated_at") or "").strip(), fallback=utc_now()),
                status=str(row.get("status") or "discovered").strip() or "discovered",
                resolution_attempts=int(row.get("resolution_attempts") or 0),
                download_attempts=int(row.get("download_attempts") or 0),
                last_error=str(row.get("last_error") or "").strip(),
            )
        if migrated_legacy:
            self.dirty = True

    def _entry_to_candidate(self, entry: GrokWorkQueueEntry) -> GrokMediaCandidate:
        """Convert one queue entry back into a Grok media candidate."""
        asset_name = normalize_asset_name(entry.asset_name) or default_asset_name_for_prefix(
            "vid" if entry.media_kind == "video" else "img"
        )
        source_url = entry.source_url or entry.preview_url
        return GrokMediaCandidate(
            source_url=source_url,
            asset_id=entry.asset_id,
            asset_name=asset_name,
            media_kind=entry.media_kind,
            identity=entry.identity or f"{entry.asset_id}/{asset_name}",
            preview_url=entry.preview_url,
            expected_width=0,
            expected_height=0,
            expected_bytes=entry.expected_bytes,
            created_at=entry.created_at,
        )


def build_grok_initial_snapshot(version: str, target_dir: Path | None = None) -> TaskSnapshot:
    """Hydrate the Grok page with existing local cache metrics."""
    resolved_target_dir = target_dir or GROK_TARGET_DIR
    snapshot = TaskSnapshot(version=version, account_name="Grok", output_dir=str(resolved_target_dir))
    catalog = GrokMediaCatalog.build(resolved_target_dir)
    cached_count, cached_images, cached_videos = catalog.summarize()
    if cached_count == 0:
        snapshot.message = "Ready. No Grok media has been cached yet."
        return snapshot

    snapshot.downloaded_posts = cached_count
    snapshot.downloaded_tweets = cached_count
    snapshot.downloaded_images = cached_images
    snapshot.downloaded_videos = cached_videos
    snapshot.message = (
        f"Ready. Found existing Grok cache: {cached_count} assets, "
        f"{cached_images} images, {cached_videos} videos."
    )
    return snapshot


def reset_grok_state(target_dir: Path | None = None) -> GrokResetResult:
    """Delete cached Grok media plus resumable local state for a full resync."""
    resolved_target_dir = target_dir or GROK_TARGET_DIR
    result = GrokResetResult()
    resolved_target_dir.mkdir(parents=True, exist_ok=True)

    state_paths = [
        resolved_target_dir / GROK_CATALOG_FILENAME,
        resolved_target_dir / GROK_DOWNLOAD_MANIFEST_FILENAME,
        resolved_target_dir / GROK_WORK_QUEUE_FILENAME,
        resolved_target_dir / LEGACY_GROK_CATALOG_FILENAME,
        resolved_target_dir / LEGACY_GROK_DOWNLOAD_MANIFEST_FILENAME,
        resolved_target_dir / LEGACY_GROK_WORK_QUEUE_FILENAME,
    ]
    for state_path in state_paths:
        if not state_path.exists():
            continue
        if state_path.is_file():
            state_path.unlink()
            result.removed_state_files += 1

    partial_dir = resolved_target_dir / TEMP_DOWNLOAD_DIRNAME
    if partial_dir.exists() and partial_dir.is_dir():
        for partial_path in partial_dir.rglob("*"):
            if partial_path.is_file():
                result.removed_partial_files += 1
        shutil.rmtree(partial_dir, ignore_errors=False)
        result.removed_partial_dirs += 1

    for file_path in resolved_target_dir.iterdir():
        if not file_path.is_file():
            continue
        if file_path.name.startswith("."):
            continue
        file_path.unlink()
        result.removed_media_files += 1

    return result


def backfill_grok_file_timestamps(
    target_dir: Path,
    catalog: GrokMediaCatalog,
    manifest: GrokDownloadManifest,
) -> int:
    """Backfill creation and modification timestamps for previously cached media."""
    fixed_count = 0
    timestamp_by_relative_path: dict[str, str] = {}
    for entry in catalog.snapshot_entries():
        if entry.relative_path:
            timestamp_by_relative_path[entry.relative_path] = entry.first_seen_at or derive_seen_at_from_filename(entry.relative_path)

    for entry in manifest.entries_by_identity.values():
        if entry.status != "completed" or not entry.relative_path:
            continue
        timestamp_by_relative_path.setdefault(
            entry.relative_path,
            entry.created_at or derive_seen_at_from_filename(entry.relative_path),
        )

    for relative_path, timestamp_value in sorted(timestamp_by_relative_path.items()):
        file_path = target_dir / relative_path
        if not file_path.exists():
            continue
        if not timestamp_value:
            timestamp_value = derive_seen_at_from_filename(file_path.name)
        if not timestamp_value:
            continue

        parsed_timestamp = parse_catalog_timestamp(timestamp_value)
        if parsed_timestamp is None:
            continue

        try:
            current_mtime = file_path.stat().st_mtime
        except OSError:
            continue

        target_epoch = min(parsed_timestamp.timestamp(), current_mtime)
        if abs(current_mtime - target_epoch) < 1:
            continue

        apply_preserved_file_timestamp(file_path, timestamp_value)
        fixed_count += 1

    return fixed_count


def entry_needs_remote_image_upgrade(
    entry: GrokCatalogEntry,
    remote_candidate: GrokMediaCandidate | None = None,
) -> bool:
    """Return whether a cached image looks like a low-resolution preview copy."""
    if entry.media_kind != "image":
        return False

    if remote_candidate is not None and remote_candidate.media_kind == "image" and remote_candidate.expected_bytes > 0:
        if entry.content_bytes < max(1, int(remote_candidate.expected_bytes * 0.98)):
            return True

    if is_preview_asset_url(entry.source_url):
        return True
    lowered_path = entry.relative_path.lower()
    return "preview-image" in lowered_path or "preview_image" in lowered_path


def extract_visible_candidates(page) -> list[GrokMediaCandidate]:
    """Extract currently visible Grok media candidates from the page."""
    raw_items = page.evaluate(
        """() => {
            const results = [];
            const seenKeys = new Set();

            const isVisible = (element) => {
                if (!(element instanceof Element)) {
                    return false;
                }
                const rect = element.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            };

            const mediaPayloadFromNode = (mediaNode) => {
                if (!(mediaNode instanceof Element)) {
                    return null;
                }
                const currentSrc = mediaNode.currentSrc || mediaNode.src || '';
                const previewUrl = currentSrc.split('#')[0];
                if (!previewUrl || !previewUrl.includes('assets.grok.com')) {
                    return null;
                }
                const tagName = mediaNode.tagName.toLowerCase() === 'source'
                    ? ((mediaNode.parentElement && mediaNode.parentElement.tagName) || 'video').toLowerCase()
                    : mediaNode.tagName.toLowerCase();
                return {
                    previewUrl,
                    mediaTag: tagName,
                };
            };

            const pushResult = (fileId, previewUrl, mediaTag) => {
                const dedupeKey = fileId || previewUrl;
                if (!dedupeKey || seenKeys.has(dedupeKey)) {
                    return;
                }
                seenKeys.add(dedupeKey);
                results.push({
                    fileId: fileId || '',
                    previewUrl: previewUrl || '',
                    mediaTag: mediaTag || '',
                });
            };

            const anchors = Array.from(document.querySelectorAll('a[href*="/files?file="]'));
            anchors.forEach((anchor) => {
                const href = anchor.href || '';
                const fileId = new URL(href).searchParams.get('file') || '';
                const mediaNode = anchor.querySelector('img, video source, video');
                const mediaPayload = mediaPayloadFromNode(mediaNode);
                if (!fileId && !mediaPayload) {
                    return;
                }
                pushResult(fileId, mediaPayload ? mediaPayload.previewUrl : '', mediaPayload ? mediaPayload.mediaTag : '');
            });

            const cardSelectors = [
                'div[tabindex="0"]',
                '[role="button"]',
                'button',
            ];
            const cards = Array.from(document.querySelectorAll(cardSelectors.join(',')));
            cards.forEach((card) => {
                if (!isVisible(card)) {
                    return;
                }
                const mediaNode = card.querySelector('img, video source, video');
                const mediaPayload = mediaPayloadFromNode(mediaNode);
                if (!mediaPayload) {
                    return;
                }
                const linkedAnchor = card.closest('a[href*="/files?file="]') || card.querySelector('a[href*="/files?file="]');
                const fileId = linkedAnchor ? (new URL(linkedAnchor.href || '').searchParams.get('file') || '') : '';
                pushResult(fileId, mediaPayload.previewUrl, mediaPayload.mediaTag);
            });

            return results;
        }"""
    )

    candidates: list[GrokMediaCandidate] = []
    seen_identities: set[str] = set()
    for item in raw_items:
        file_id = str(item.get("fileId") or "")
        preview_url = str(item.get("previewUrl") or "")
        media_tag = str(item.get("mediaTag") or "")
        candidate = candidate_from_file_reference(file_id, preview_url, media_tag)
        if candidate is None and preview_url:
            candidate = candidate_from_url(preview_url, media_tag)
        if candidate is None or candidate.identity in seen_identities:
            continue
        seen_identities.add(candidate.identity)
        candidates.append(candidate)
    return candidates


def wait_for_grok_library_candidates(page, timeout_seconds: float) -> bool:
    """Wait for Grok's client-rendered library cards to become discoverable."""
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while time.monotonic() < deadline:
        if extract_visible_candidates(page):
            return True
        time.sleep(0.5)
    return False


def candidate_is_downloadable(candidate: GrokMediaCandidate) -> bool:
    """Return whether the candidate has a safe canonical download URL."""
    if not candidate.source_url:
        return False
    if candidate.media_kind == "image" and is_preview_asset_url(candidate.source_url):
        return False
    return True


def resolve_candidate_for_download(context, candidate: GrokMediaCandidate, details_page=None) -> GrokMediaCandidate | None:
    """Upgrade one raw page candidate into a canonical downloadable asset."""
    resolved_candidate = resolve_candidate_from_versions(context, candidate)
    if resolved_candidate.media_kind == "image" and (
        not resolved_candidate.source_url or is_preview_asset_url(resolved_candidate.source_url)
    ):
        resolved_candidate = resolve_candidate_from_file_details_page(
            context,
            resolved_candidate,
            details_page=details_page,
        )
    if not candidate_is_downloadable(resolved_candidate):
        return None
    return resolved_candidate


def scroll_media_page(
    page, scan_wait_seconds: float = SCROLL_WAIT_SECONDS, should_stop=lambda: False,
) -> None:
    """Advance the Grok media page enough to reveal more file cards."""
    page.evaluate(
        """() => {
            window.scrollBy(0, Math.max(window.innerHeight, document.body.scrollHeight * 0.8));
            const scrollables = Array.from(document.querySelectorAll("*")).filter(
                (element) => element.scrollHeight > element.clientHeight + 24
            );
            scrollables.forEach((element) => {
                element.scrollBy(0, Math.max(element.clientHeight, element.scrollHeight * 0.8));
            });
        }"""
    )
    wait_for_cache_scan(scan_wait_seconds, should_stop)


def collect_candidates(
    context,
    page,
    catalog: GrokMediaCatalog,
    work_queue: GrokWorkQueue,
    state: TaskState,
    should_stop,
    on_pipeline_tick=None,
    max_library_pages: int = GROK_LIBRARY_PAGE_POOL_SIZE,
    scan_wait_seconds: float = SCROLL_WAIT_SECONDS,
) -> tuple[list[GrokMediaCandidate], list[object]]:
    """Incrementally collect Grok assets while keeping the local queue populated."""
    ordered_candidates: list[GrokMediaCandidate] = []
    seen_asset_ids: set[str] = set()
    stale_rounds = 0
    library_pages = [page]

    for round_index in range(1, GROK_SCROLL_ROUNDS + 1):
        if should_stop():
            return ordered_candidates, library_pages

        active_page = library_pages[(round_index - 1) % len(library_pages)]

        if callable(on_pipeline_tick):
            on_pipeline_tick()

        current_candidates = extract_visible_candidates(active_page)
        new_in_round = 0
        new_candidates: list[GrokMediaCandidate] = []
        for candidate in current_candidates:
            if candidate.asset_id in seen_asset_ids:
                continue
            seen_asset_ids.add(candidate.asset_id)
            ordered_candidates.append(candidate)
            new_candidates.append(candidate)
            new_in_round += 1

        if new_candidates:
            work_queue.register_discovered(new_candidates)
            if callable(on_pipeline_tick):
                on_pipeline_tick()

        leading_known = 0
        for candidate in ordered_candidates:
            if catalog.contains_asset_id(candidate.asset_id):
                leading_known += 1
                continue
            break

        state.append_event(
            f"Grok scan round {round_index}: discovered {len(ordered_candidates)} assets, "
            f"{leading_known} latest assets already cached."
        )
        state.update(discovered_tweets=len(ordered_candidates), skipped_tweets=leading_known)

        if new_in_round == 0:
            stale_rounds += 1
        else:
            stale_rounds = 0

        should_open_library_helper = (
            len(library_pages) < max(1, max_library_pages)
            and (stale_rounds >= 1 or round_index % GROK_LIBRARY_PAGE_OPEN_INTERVAL == 0)
        )
        if should_open_library_helper:
            helper_page = context.new_page()
            state.append_event(
                "Opening an additional Grok files tab because the site is slow; discovery will continue across tabs."
            )
            open_grok_page(helper_page, GROK_FILES_URL, settle_seconds=1.2)
            library_pages.append(helper_page)
            if callable(on_pipeline_tick):
                on_pipeline_tick()

        if GROK_KNOWN_PREFIX_STOP_COUNT > 0 and leading_known >= GROK_KNOWN_PREFIX_STOP_COUNT and round_index > 1:
            logger.info(
                "Stopping Grok scan after encountering a fully cached latest prefix.",
                extra={
                    "discovered_count": len(ordered_candidates),
                    "leading_known_count": leading_known,
                    "round_index": round_index,
                },
            )
            break

        if stale_rounds >= GROK_STALE_SCROLL_LIMIT:
            logger.info(
                "Stopping Grok scan after stale rounds.",
                extra={
                    "discovered_count": len(ordered_candidates),
                    "stale_rounds": stale_rounds,
                    "round_index": round_index,
                },
            )
            break

        scroll_media_page(active_page, scan_wait_seconds, should_stop)
        if callable(on_pipeline_tick):
            on_pipeline_tick()

    return ordered_candidates, library_pages


def commit_downloaded_candidate(
    catalog: GrokMediaCatalog,
    manifest: GrokDownloadManifest,
    target_dir: Path,
    candidate: GrokMediaCandidate,
    temp_path: Path,
    content_type: str,
) -> tuple[bool, bool]:
    """Validate, dedupe, and atomically commit one downloaded asset."""
    validation_error = media_validation_error(
        temp_path,
        media_kind=candidate.media_kind,
        content_type=content_type,
        expected_bytes=candidate.expected_bytes or 0,
    )
    if validation_error is not None:
        raise RuntimeError(
            f"Downloaded payload for {candidate.asset_id} failed integrity validation: {validation_error}."
        )

    content_bytes = temp_path.stat().st_size
    content_sha256 = compute_file_sha256(temp_path)
    existing_entry = catalog.get_entry(candidate.identity)
    previous_relative_path = existing_entry.relative_path if existing_entry is not None else ""
    seen_at = preferred_seen_at(candidate, fallback=existing_entry.first_seen_at if existing_entry is not None else "")
    relative_path = ""
    downloaded = False
    deduped = False

    with catalog._lock:
        existing_relative_path = catalog.lookup_relative_path_by_hash(content_sha256)
        if existing_relative_path is not None:
            relative_path = existing_relative_path
            catalog._register_alias_unlocked(candidate, relative_path, content_sha256, seen_at=seen_at)
            updated_entry = catalog.entries_by_identity.get(candidate.identity)
            if updated_entry is not None and previous_relative_path and previous_relative_path != updated_entry.relative_path:
                catalog._prune_unreferenced_relative_path_unlocked(previous_relative_path)
            catalog._flush_unlocked()
            deduped = True
        else:
            destination_filename = build_destination_filename(candidate, content_type)
            destination_path = resolve_destination_path(target_dir, destination_filename)
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temp_path, destination_path)
            relative_path = destination_path.relative_to(target_dir).as_posix()
            catalog._register_download_unlocked(
                candidate=candidate,
                relative_path=relative_path,
                content_sha256=content_sha256,
                content_bytes=content_bytes,
                seen_at=seen_at,
            )
            updated_entry = catalog.entries_by_identity.get(candidate.identity)
            if updated_entry is not None and previous_relative_path and previous_relative_path != updated_entry.relative_path:
                catalog._prune_unreferenced_relative_path_unlocked(previous_relative_path)
            catalog._flush_unlocked()
            downloaded = True

    apply_preserved_file_timestamp(target_dir / relative_path, seen_at)
    manifest.mark_completed(candidate, relative_path, content_sha256, content_bytes)
    if deduped:
        with contextlib.suppress(FileNotFoundError):
            temp_path.unlink()
    return downloaded, deduped


def response_expected_total_bytes(headers, range_start: int) -> int:
    """Return the response's advertised final size when it can be determined."""
    content_range = str(headers.get("Content-Range") or "")
    range_match = re.fullmatch(r"bytes\s+\d+-\d+/(\d+)", content_range.strip(), flags=re.IGNORECASE)
    if range_match:
        return int(range_match.group(1))

    raw_length = str(headers.get("Content-Length") or "").strip()
    if raw_length.isdigit():
        return range_start + int(raw_length)
    return 0


def response_matches_requested_range(headers, range_start: int) -> bool:
    """Return whether a partial response begins at the requested byte offset."""
    content_range = str(headers.get("Content-Range") or "")
    range_match = re.fullmatch(r"bytes\s+(\d+)-\d+/(?:\d+|\*)", content_range.strip(), flags=re.IGNORECASE)
    return range_match is not None and int(range_match.group(1)) == range_start


def download_retry_delay(attempt_index: int, retry_after: str = "") -> float:
    """Return a bounded retry delay while honoring a numeric server instruction."""
    if retry_after.strip().isdigit():
        return min(float(retry_after.strip()), 30.0)
    return min(float(2 ** (attempt_index - 1)), 8.0)


def write_download_response(response, temp_path: Path, range_start: int, should_stop) -> tuple[str, bool, bool, int]:
    """Write one HTTP response and report whether a full restart is required."""
    status = getattr(response, "status", None)
    if status is None:
        status = response.getcode()
    content_type = str(response.headers.get("Content-Type") or "")
    if range_start > 0 and status != 206:
        return content_type, False, True, 0
    if range_start > 0 and not response_matches_requested_range(response.headers, range_start):
        raise DownloadPayloadIntegrityError(
            f"the server returned an unexpected Content-Range for byte {range_start:,}"
        )
    if range_start == 0 and status != 200:
        raise DownloadPayloadIntegrityError(f"the server returned unexpected HTTP status {status}")

    resumed = range_start > 0
    with temp_path.open("ab" if resumed else "wb") as handle:
        while True:
            if should_stop():
                raise DownloadStoppedError("Stop requested while downloading Grok media.")
            chunk = response.read(DOWNLOAD_CHUNK_BYTES)
            if not chunk:
                break
            handle.write(chunk)
    return content_type, resumed, False, response_expected_total_bytes(response.headers, range_start)


def stream_candidate_download(
    candidate: GrokMediaCandidate,
    auth: GrokDownloadAuth,
    temp_path: Path,
    should_stop,
    max_file_size_bytes: int = 0,
) -> tuple[str, bool]:
    """Download one verified asset, retrying transient and malformed HTTP responses."""
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    if max_file_size_bytes > 0 and candidate.expected_bytes > max_file_size_bytes:
        raise DownloadSizeLimitError(
            f"Grok asset {candidate.asset_id} is {candidate.expected_bytes:,} bytes, above the "
            f"{max_file_size_bytes:,}-byte cache limit."
        )
    resumed = False
    last_error: Exception | None = None

    for attempt_index in range(1, GROK_DOWNLOAD_RETRY_LIMIT + 1):
        existing_bytes = temp_path.stat().st_size if temp_path.exists() else 0
        if max_file_size_bytes > 0 and existing_bytes > max_file_size_bytes:
            raise DownloadSizeLimitError(
                f"Grok asset {candidate.asset_id} has a partial file of {existing_bytes:,} bytes, above the "
                f"{max_file_size_bytes:,}-byte cache limit."
            )
        range_start = existing_bytes if existing_bytes > 0 else 0
        try:
            request = Request(candidate.source_url, headers=build_download_headers(candidate, auth, range_start))
            with urlopen(request, timeout=DOWNLOAD_TIMEOUT_MS / 1_000) as response:
                advertised_total = response_expected_total_bytes(response.headers, range_start)
                if max_file_size_bytes > 0 and advertised_total > max_file_size_bytes:
                    raise DownloadSizeLimitError(
                        f"Grok asset {candidate.asset_id} is advertised as {advertised_total:,} bytes, above the "
                        f"{max_file_size_bytes:,}-byte cache limit."
                    )
                content_type, did_resume, restart_full_download, response_total_bytes = write_download_response(
                    response,
                    temp_path,
                    range_start,
                    should_stop,
                )

            if restart_full_download:
                with contextlib.suppress(FileNotFoundError):
                    temp_path.unlink()
                request = Request(candidate.source_url, headers=build_download_headers(candidate, auth, 0))
                with urlopen(request, timeout=DOWNLOAD_TIMEOUT_MS / 1_000) as response:
                    advertised_total = response_expected_total_bytes(response.headers, 0)
                    if max_file_size_bytes > 0 and advertised_total > max_file_size_bytes:
                        raise DownloadSizeLimitError(
                            f"Grok asset {candidate.asset_id} is advertised as {advertised_total:,} bytes, above the "
                            f"{max_file_size_bytes:,}-byte cache limit."
                        )
                    content_type, did_resume, restart_full_download, response_total_bytes = write_download_response(
                        response,
                        temp_path,
                        0,
                        should_stop,
                    )
                if restart_full_download:
                    raise DownloadPayloadIntegrityError("the server did not provide a complete media response")

            expected_bytes = candidate.expected_bytes or response_total_bytes
            validation_error = media_validation_error(
                temp_path,
                candidate.media_kind,
                content_type,
                expected_bytes,
            )
            if validation_error is not None:
                raise DownloadPayloadIntegrityError(validation_error)
            if max_file_size_bytes > 0 and temp_path.stat().st_size > max_file_size_bytes:
                raise DownloadSizeLimitError(
                    f"Grok asset {candidate.asset_id} exceeded the {max_file_size_bytes:,}-byte cache limit."
                )

            return content_type, resumed or did_resume
        except DownloadStoppedError:
            raise
        except DownloadSizeLimitError:
            with contextlib.suppress(FileNotFoundError):
                temp_path.unlink()
            raise
        except DownloadPayloadIntegrityError as exc:
            last_error = exc
            with contextlib.suppress(FileNotFoundError):
                temp_path.unlink()
            logger.warning(
                "Discarding invalid Grok download response before retrying.",
                extra={
                    "asset_id": candidate.asset_id,
                    "attempt_index": attempt_index,
                    "error": str(exc),
                },
            )
        except HTTPError as exc:
            last_error = exc
            if exc.code == 416:
                with contextlib.suppress(FileNotFoundError):
                    temp_path.unlink()
            else:
                with contextlib.suppress(FileNotFoundError):
                    if temp_path.exists() and temp_path.stat().st_size == 0:
                        temp_path.unlink()
        except (IncompleteRead, OSError, TimeoutError, URLError) as exc:
            last_error = exc

        if attempt_index < GROK_DOWNLOAD_RETRY_LIMIT:
            error_headers = getattr(last_error, "headers", None)
            retry_after = str(error_headers.get("Retry-After") or "") if error_headers is not None else ""
            time.sleep(download_retry_delay(attempt_index, retry_after))

    raise RuntimeError(
        f"Failed to download a valid payload for {candidate.asset_id} after {GROK_DOWNLOAD_RETRY_LIMIT} attempts: {last_error}"
    )


def download_candidate(
    catalog: GrokMediaCatalog,
    manifest: GrokDownloadManifest,
    target_dir: Path,
    candidate: GrokMediaCandidate,
    auth: GrokDownloadAuth,
    should_stop,
    browser_streamer: Callable[[GrokMediaCandidate, Path, object], tuple[str, bool]] | None = None,
    max_file_size_bytes: int = 0,
) -> tuple[bool, bool, bool]:
    """Download one Grok asset with resume, integrity checks, and manifest tracking."""
    if BrowserDeletionCatalog(target_dir.parent).is_excluded("grok", candidate.asset_id or candidate.identity):
        return False, False, False
    if not candidate.source_url:
        raise RuntimeError(f"No canonical download URL is available for {candidate.asset_id}.")
    if candidate.media_kind == "image" and is_preview_asset_url(candidate.source_url):
        raise RuntimeError(
            f"Refusing to cache preview-quality image for {candidate.asset_id}; "
            "resolve the original image URL first."
        )
    existing_entry = catalog.get_entry(candidate.identity)
    if existing_entry is not None:
        apply_preserved_file_timestamp(
            target_dir / existing_entry.relative_path,
            preferred_seen_at(candidate, fallback=existing_entry.first_seen_at),
        )
        manifest.mark_completed(
            candidate,
            existing_entry.relative_path,
            existing_entry.content_sha256,
            existing_entry.content_bytes,
        )
        return False, False, False

    if max_file_size_bytes > 0 and candidate.expected_bytes > max_file_size_bytes:
        raise DownloadSizeLimitError(
            f"Grok asset {candidate.asset_id} is {candidate.expected_bytes:,} bytes, above the "
            f"{max_file_size_bytes:,}-byte cache limit."
        )

    temp_path = manifest.temp_path_for(candidate)
    temp_relative_path = temp_path.relative_to(target_dir).as_posix()
    manifest.mark_in_progress(candidate, temp_relative_path)
    try:
        if browser_streamer is None:
            stream_args = (candidate, auth, temp_path, should_stop)
            if max_file_size_bytes > 0:
                content_type, resumed = stream_candidate_download(
                    *stream_args,
                    max_file_size_bytes=max_file_size_bytes,
                )
            else:
                content_type, resumed = stream_candidate_download(*stream_args)
        else:
            content_type, resumed = browser_streamer(candidate, temp_path, should_stop)
            if max_file_size_bytes > 0 and temp_path.stat().st_size > max_file_size_bytes:
                raise DownloadSizeLimitError(
                    f"Grok asset {candidate.asset_id} exceeded the {max_file_size_bytes:,}-byte cache limit."
                )
    except DownloadStoppedError as exc:
        manifest.mark_pending_resume(candidate, temp_relative_path, str(exc))
        raise
    except DownloadSizeLimitError as exc:
        with contextlib.suppress(FileNotFoundError):
            temp_path.unlink()
        manifest.mark_size_skipped(candidate, temp_relative_path, str(exc))
        raise
    except Exception as exc:
        manifest.mark_failed(candidate, temp_relative_path, str(exc))
        raise

    try:
        downloaded, deduped = commit_downloaded_candidate(
            catalog=catalog,
            manifest=manifest,
            target_dir=target_dir,
            candidate=candidate,
            temp_path=temp_path,
            content_type=content_type,
        )
    except Exception as exc:
        with contextlib.suppress(FileNotFoundError):
            temp_path.unlink()
        manifest.mark_failed(candidate, temp_relative_path, str(exc))
        raise
    return downloaded, deduped, resumed


def repair_cached_preview_images(
    context,
    catalog: GrokMediaCatalog,
    manifest: GrokDownloadManifest,
    target_dir: Path,
    auth: GrokDownloadAuth,
    state: TaskState,
    should_stop,
    browser_streamer: Callable[[GrokMediaCandidate, Path, object], tuple[str, bool]] | None = None,
    max_file_size_bytes: int = 0,
) -> tuple[int, int, int, int]:
    """Replace cached preview-quality images with original-resolution assets."""
    repaired_count = 0
    deduped_count = 0
    failed_count = 0
    size_skipped_count = 0
    deletion_catalog = BrowserDeletionCatalog(target_dir.parent)
    image_entries_by_asset_id: dict[str, list[GrokCatalogEntry]] = {}
    for entry in catalog.snapshot_entries():
        asset_id = extract_status_id_from_identity(entry.identity)
        if (
            entry.media_kind != "image"
            or not asset_id
            or not entry_needs_remote_image_upgrade(entry)
        ):
            continue
        image_entries_by_asset_id.setdefault(asset_id, []).append(entry)

    if not image_entries_by_asset_id:
        return repaired_count, deduped_count, failed_count, size_skipped_count

    ordered_asset_ids = sorted(image_entries_by_asset_id)
    for index, asset_id in enumerate(ordered_asset_ids, start=1):
        if should_stop():
            break
        if deletion_catalog.is_excluded("grok", asset_id):
            continue

        entries = image_entries_by_asset_id[asset_id]
        representative_entry = min(
            entries,
            key=lambda entry: (
                0 if entry_needs_remote_image_upgrade(entry) else 1,
                parse_seen_at(entry.first_seen_at),
                entry.identity,
            ),
        )
        fallback_candidate = GrokMediaCandidate(
            source_url=representative_entry.source_url,
            asset_id=asset_id,
            asset_name=extract_asset_name_from_identity(representative_entry.identity) or "image",
            media_kind=representative_entry.media_kind,
            identity=representative_entry.identity,
            preview_url=representative_entry.source_url,
            created_at=representative_entry.first_seen_at,
        )

        state.append_event(
            f"Auditing Grok image quality {index}/{len(ordered_asset_ids)}: {fallback_candidate.asset_id}"
        )
        resolved_candidate = resolve_candidate_from_versions(context, fallback_candidate)
        if not resolved_candidate.source_url or is_preview_asset_url(resolved_candidate.source_url):
            resolved_candidate = resolve_candidate_from_file_details_page(context, resolved_candidate)
        if not resolved_candidate.source_url or is_preview_asset_url(resolved_candidate.source_url):
            continue

        needs_upgrade = any(entry_needs_remote_image_upgrade(entry, resolved_candidate) for entry in entries)
        has_canonical_entry = any(entry.identity == resolved_candidate.identity for entry in entries)
        if not needs_upgrade and has_canonical_entry:
            for entry in entries:
                if entry.identity != resolved_candidate.identity:
                    drop_catalog_entry(catalog, target_dir, entry.identity)
            continue

        try:
            downloaded, deduped, _resumed = download_candidate(
                catalog,
                manifest,
                target_dir,
                resolved_candidate,
                auth,
                should_stop,
                browser_streamer=browser_streamer,
                max_file_size_bytes=max_file_size_bytes,
            )
        except DownloadStoppedError:
            break
        except DownloadSizeLimitError:
            size_skipped_count += 1
            state.append_event(
                f"Skipped Grok image repair for {asset_id}: above the "
                f"{max_file_size_bytes // (1024 * 1024):,} MiB cache limit."
            )
            continue
        except Exception as exc:  # pragma: no cover
            failed_count += 1
            state.append_event(f"Skipped Grok image repair for {asset_id}: {exc}")
            logger.exception(
                "Grok cached image repair failed.",
                extra={
                    "asset_id": asset_id,
                    "identity": resolved_candidate.identity,
                },
            )
            continue
        repaired_count += 1 if downloaded else 0
        deduped_count += 1 if deduped else 0

        for entry in entries:
            if entry.identity != resolved_candidate.identity:
                drop_catalog_entry(catalog, target_dir, entry.identity)

    catalog.flush()
    return repaired_count, deduped_count, failed_count, size_skipped_count


def extract_status_id_from_identity(identity: str) -> str:
    """Extract the Grok asset ID from a catalog identity string."""
    prefix = str(identity or "").split("/", 1)[0].strip().lower()
    return prefix if re.fullmatch(r"[0-9a-f-]{36}", prefix) else ""


def run_download_worker(
    catalog: GrokMediaCatalog,
    manifest: GrokDownloadManifest,
    target_dir: Path,
    candidate: GrokMediaCandidate,
    auth: GrokDownloadAuth,
    should_stop,
    browser_streamer: Callable[[GrokMediaCandidate, Path, object], tuple[str, bool]] | None = None,
    max_file_size_bytes: int = 0,
) -> GrokDownloadOutcome:
    """Execute one Grok download worker task."""
    try:
        downloaded, deduped, resumed = download_candidate(
            catalog=catalog,
            manifest=manifest,
            target_dir=target_dir,
            candidate=candidate,
            auth=auth,
            should_stop=should_stop,
            browser_streamer=browser_streamer,
            max_file_size_bytes=max_file_size_bytes,
        )
        return GrokDownloadOutcome(
            candidate=candidate,
            downloaded=downloaded,
            deduped=deduped,
            resumed=resumed,
        )
    except DownloadStoppedError:
        return GrokDownloadOutcome(candidate=candidate, stopped=True)
    except DownloadSizeLimitError as exc:
        return GrokDownloadOutcome(candidate=candidate, skipped_size=True, error=str(exc))
    except Exception as exc:  # pragma: no cover
        logger.exception(
            "Grok asset download failed.",
            extra={
                "asset_id": candidate.asset_id,
                "asset_name": candidate.asset_name,
                "identity": candidate.identity,
            },
        )
        return GrokDownloadOutcome(candidate=candidate, failed=True, error=str(exc))


def sync_grok_media(
    state: TaskState,
    config: CrawlConfig | None = None,
    target_dir: Path = GROK_TARGET_DIR,
    should_stop=lambda: False,
) -> GrokSyncResult:
    """Sync the latest Grok-generated media into the local cache."""
    runtime_config = config or CrawlConfig()
    descriptor = browser_descriptors(runtime_config).get(runtime_config.grok_browser)
    if descriptor is None:
        raise RuntimeError(f"Unsupported Grok browser: {runtime_config.grok_browser}")
    if descriptor.engine not in {"chromium", "safari"}:
        raise RuntimeError(f"Grok sync does not support the {descriptor.label} automation engine.")

    catalog = GrokMediaCatalog.build(target_dir)
    manifest = GrokDownloadManifest.build(target_dir, catalog)
    work_queue = GrokWorkQueue.build(target_dir, catalog)
    backfilled_timestamps = backfill_grok_file_timestamps(target_dir, catalog, manifest)
    cached_count, cached_images, cached_videos = catalog.summarize()
    state.update(
        account_name="Grok",
        output_dir=str(target_dir),
        queued_tweets=0,
        processed_tweets=0,
        discovery_complete=False,
        downloaded_posts=cached_count,
        downloaded_tweets=cached_count,
        downloaded_images=cached_images,
        downloaded_videos=cached_videos,
    )
    state.append_event(
        f"Prepared Grok catalog with {cached_count} cached assets ({cached_images} images, {cached_videos} videos)."
    )
    if backfilled_timestamps > 0:
        state.append_event(
            f"Backfilled created-time metadata on {backfilled_timestamps} cached Grok files for Apple Photos import ordering."
        )
    state.append_event(
        f"Loaded Grok work queue with {work_queue.total_count()} tracked asset IDs for streaming discovery and download."
    )

    if should_stop():
        return GrokSyncResult(
            cached_count=cached_count,
            cached_images=cached_images,
            cached_videos=cached_videos,
            stopped=True,
        )

    if descriptor.engine == "chromium" and sync_playwright is None:
        raise RuntimeError(
            "Playwright is not installed. Run `python3 -m pip install -r requirements.txt` "
            "and `python3 -m playwright install chromium`."
        )

    context = None
    details_pages: list[object] = []
    library_pages_to_close: list[object] = []
    download_auth = GrokDownloadAuth()
    browser_streamer: Callable[[GrokMediaCandidate, Path, object], tuple[str, bool]] | None = None
    max_library_pages = GROK_LIBRARY_PAGE_POOL_SIZE
    download_worker_count = resolve_grok_download_worker_count(runtime_config, descriptor.engine)

    try:
        state.update(phase="collecting")
        with contextlib.ExitStack() as browser_stack:
            if descriptor.engine == "safari":
                state.append_event("Opening an authenticated offscreen Safari window for Grok sync.")
                context = browser_stack.enter_context(SafariContext(GROK_FILES_URL))
                max_library_pages = 1
            else:
                state.append_event(f"Launching {descriptor.label} in the background for Grok sync.")
                playwright = browser_stack.enter_context(sync_playwright())
                context = browser_stack.enter_context(
                    launch_chromium_context(
                        playwright,
                        descriptor,
                        headless=True,
                        clone_profile_first=True,
                        background_window=True,
                    )
                )
            page = prepare_grok_library_page(context)
            if descriptor.engine == "safari":
                def stream_from_safari(
                    candidate: GrokMediaCandidate,
                    temp_path: Path,
                    stop_requested,
                ) -> tuple[str, bool]:
                    if (
                        temp_path.exists()
                        and candidate.expected_bytes > 0
                        and temp_path.stat().st_size >= candidate.expected_bytes
                    ):
                        if validate_media_file(
                            temp_path,
                            candidate.media_kind,
                            expected_bytes=candidate.expected_bytes,
                        ):
                            return "", True
                        temp_path.unlink()
                    try:
                        return page.download_to_path(candidate.source_url, temp_path, stop_requested)
                    except RuntimeError as exc:
                        if stop_requested():
                            raise DownloadStoppedError(str(exc)) from exc
                        raise

                browser_streamer = stream_from_safari
            details_pages = [context.new_page() for _ in range(GROK_RESOLUTION_PAGE_POOL_SIZE)]
            next_details_page_index = 0

            state.append_event("Opening the Grok files library.")
            open_grok_page(page, GROK_FILES_URL, settle_seconds=INITIAL_PAGE_WAIT_SECONDS)
            if descriptor.engine == "safari" and not wait_for_grok_library_candidates(
                page,
                SAFARI_LIBRARY_READY_TIMEOUT_SECONDS,
            ):
                state.append_event(
                    "Safari opened the Grok library, but no file cards were visible before discovery began."
                )
            download_auth = build_download_auth(context, page)
            state.append_event(
                "Grok is slow, so this run may keep scrolling and open extra Grok tabs while downloads continue in parallel."
            )

            repaired_images, deduped_repairs, failed_count, repaired_size_skipped = repair_cached_preview_images(
                context,
                catalog,
                manifest,
                target_dir,
                download_auth,
                state,
                should_stop,
                browser_streamer=browser_streamer,
                max_file_size_bytes=runtime_config.max_media_file_size_bytes,
            )
            cached_count, cached_images, cached_videos = catalog.summarize()
            state.update(
                downloaded_posts=cached_count,
                downloaded_tweets=cached_count,
                downloaded_images=cached_images,
                downloaded_videos=cached_videos,
                failed_tweets=failed_count,
            )
            if repaired_images or deduped_repairs:
                state.append_event(
                    f"Audited Grok images. Repaired {repaired_images} preview files and deduped {deduped_repairs} replacements."
                )

            downloaded_count = 0
            downloaded_images = 0
            downloaded_videos = 0
            deduped_by_hash = 0
            size_skipped_count = repaired_size_skipped
            completed_workers = 0
            queued_candidates = 0
            ordered_candidates: list[GrokMediaCandidate] = []
            futures: dict[Future[GrokDownloadOutcome], GrokMediaCandidate] = {}
            submitted_identities: set[str] = set()
            submitted_asset_ids: set[str] = set()

            def next_details_page():
                nonlocal next_details_page_index
                if not details_pages:
                    return None
                page_candidate = details_pages[next_details_page_index % len(details_pages)]
                next_details_page_index += 1
                return page_candidate

            def drain_ready_downloads(wait_for_all: bool = False) -> bool:
                nonlocal downloaded_count
                nonlocal downloaded_images
                nonlocal downloaded_videos
                nonlocal deduped_by_hash
                nonlocal size_skipped_count
                nonlocal failed_count
                nonlocal completed_workers

                stop_detected = False
                while True:
                    ready_futures = [future for future in futures if future.done()]
                    if wait_for_all and not ready_futures and futures:
                        first_future = next(as_completed(tuple(futures)))
                        ready_futures = [first_future]
                    if not ready_futures:
                        break

                    for future in ready_futures:
                        candidate = futures.pop(future)
                        completed_workers += 1
                        with contextlib.suppress(KeyError):
                            submitted_identities.remove(candidate.identity)
                        with contextlib.suppress(KeyError):
                            submitted_asset_ids.remove(candidate.asset_id)

                        try:
                            outcome = future.result()
                        except Exception as exc:  # pragma: no cover
                            outcome = GrokDownloadOutcome(candidate=candidate, failed=True, error=str(exc))

                        if outcome.stopped:
                            work_queue.mark_download_interrupted(
                                candidate,
                                "Stop requested while downloading Grok media.",
                            )
                            stop_detected = True
                        elif outcome.skipped_size:
                            size_skipped_count += 1
                            work_queue.mark_size_skipped(candidate, outcome.error)
                            state.append_event(
                                f"Skipped Grok asset {candidate.asset_id}/{candidate.asset_name}: "
                                f"above the {runtime_config.max_media_file_size_mib:,} MiB cache limit."
                            )
                        elif outcome.failed:
                            failed_count += 1
                            work_queue.mark_download_failed(candidate, outcome.error)
                            state.append_event(
                                f"Failed Grok asset {candidate.asset_id}/{candidate.asset_name}: {outcome.error}"
                            )
                        else:
                            work_queue.mark_completed(candidate)
                            action = "deduped" if outcome.deduped else "cached"
                            resume_note = " after resuming" if outcome.resumed else ""
                            state.append_event(
                                f"{action.title()} Grok asset {completed_workers}/{max(queued_candidates, 1)}: "
                                f"{candidate.asset_id}/{candidate.asset_name}{resume_note}."
                            )

                        if outcome.downloaded:
                            downloaded_count += 1
                            if candidate.media_kind == "video":
                                downloaded_videos += 1
                            else:
                                downloaded_images += 1
                        if outcome.deduped:
                            deduped_by_hash += 1

                        cached_count, cached_images, cached_videos = catalog.summarize()
                        state.update(
                            phase="downloading" if (futures or queued_candidates > 0) else state.snapshot()["phase"],
                            discovered_tweets=len(ordered_candidates),
                            queued_tweets=queued_candidates,
                            processed_tweets=completed_workers,
                            downloaded_posts=cached_count,
                            downloaded_tweets=cached_count,
                            downloaded_images=cached_images,
                            downloaded_videos=cached_videos,
                            skipped_tweets=deduped_by_hash + size_skipped_count,
                            failed_tweets=failed_count,
                        )
                    if not wait_for_all:
                        break

                return stop_detected

            with ThreadPoolExecutor(max_workers=download_worker_count, thread_name_prefix="grok-download") as executor:
                download_workers_announced = False

                def schedule_queue_pipeline() -> bool:
                    nonlocal queued_candidates
                    nonlocal download_workers_announced

                    if should_stop():
                        return True
                    if drain_ready_downloads(wait_for_all=False):
                        return True

                    resolution_budget = min(
                        GROK_RESOLUTION_BATCH_SIZE,
                        max(0, GROK_DOWNLOAD_BACKLOG - len(futures)),
                    )
                    raw_candidates = work_queue.claim_for_resolution(
                        limit=resolution_budget,
                        excluded_asset_ids=submitted_asset_ids,
                    )
                    for raw_candidate in raw_candidates:
                        if should_stop():
                            return True
                        try:
                            resolved_candidate = resolve_candidate_for_download(
                                context,
                                raw_candidate,
                                details_page=next_details_page(),
                            )
                        except Exception as exc:  # pragma: no cover
                            work_queue.mark_resolution_failed(raw_candidate.asset_id, str(exc))
                            state.append_event(
                                f"Failed to resolve Grok asset {raw_candidate.asset_id}: {exc}"
                            )
                            logger.exception(
                                "Grok asset resolution failed.",
                                extra={"asset_id": raw_candidate.asset_id, "identity": raw_candidate.identity},
                            )
                            continue

                        if resolved_candidate is None:
                            work_queue.mark_resolution_failed(
                                raw_candidate.asset_id,
                                "No canonical source URL is available.",
                            )
                            state.append_event(
                                f"Skipping non-downloadable Grok asset {raw_candidate.asset_id}: no canonical source URL is available."
                            )
                            continue

                        work_queue.mark_resolved(raw_candidate.asset_id, resolved_candidate)

                    ready_candidates = work_queue.claim_ready_for_download(
                        limit=max(0, GROK_DOWNLOAD_BACKLOG - len(futures)),
                        excluded_asset_ids=submitted_asset_ids,
                    )
                    for ready_candidate in ready_candidates:
                        pending_candidates = manifest.prepare_candidates([ready_candidate], catalog)
                        if not pending_candidates:
                            work_queue.mark_completed(ready_candidate)
                            continue

                        candidate = pending_candidates[0]
                        if candidate.identity in submitted_identities or candidate.asset_id in submitted_asset_ids:
                            continue

                        if not download_workers_announced:
                            state.append_event(
                                f"Starting {download_worker_count} Grok download "
                                f"{'worker' if download_worker_count == 1 else 'workers'} while discovery continues."
                            )
                            download_workers_announced = True

                        state.update(phase="downloading")
                        queued_candidates += 1
                        state.update(
                            queued_tweets=queued_candidates,
                            processed_tweets=completed_workers,
                        )
                        submitted_identities.add(candidate.identity)
                        submitted_asset_ids.add(candidate.asset_id)
                        futures[
                            executor.submit(
                                run_download_worker,
                                catalog,
                                manifest,
                                target_dir,
                                candidate,
                                download_auth,
                                should_stop,
                                browser_streamer,
                                runtime_config.max_media_file_size_bytes,
                            )
                        ] = candidate

                    return False

                schedule_queue_pipeline()
                ordered_candidates, library_pages = collect_candidates(
                    context,
                    page,
                    catalog,
                    work_queue,
                    state,
                    should_stop,
                    on_pipeline_tick=schedule_queue_pipeline,
                    max_library_pages=max_library_pages,
                    scan_wait_seconds=runtime_config.cache_scan_wait("grok", "media"),
                )
                library_pages_to_close = [candidate for candidate in library_pages if candidate is not page]
                state.update(
                    discovered_tweets=len(ordered_candidates),
                    queued_tweets=queued_candidates,
                    processed_tweets=completed_workers,
                    discovery_complete=True,
                )

                stop_detected = False
                while not should_stop():
                    stop_detected = schedule_queue_pipeline()
                    if stop_detected:
                        break
                    if not futures and not work_queue.has_pending_pipeline_work():
                        break
                    if futures and drain_ready_downloads(wait_for_all=True):
                        stop_detected = True
                        break

            state.update(
                phase="downloading" if queued_candidates > 0 else "collecting",
                discovered_tweets=len(ordered_candidates),
                queued_tweets=queued_candidates,
                processed_tweets=completed_workers,
                discovery_complete=True,
            )

            if not queued_candidates and failed_count == 0:
                cached_count, cached_images, cached_videos = catalog.summarize()
                state.update(
                    downloaded_posts=cached_count,
                    downloaded_tweets=cached_count,
                    downloaded_images=cached_images,
                    downloaded_videos=cached_videos,
                    skipped_tweets=len(ordered_candidates),
                )
                state.append_event("Grok cache is already up to date with the latest discovered assets.")
                return GrokSyncResult(
                    discovered_count=len(ordered_candidates),
                    skipped_known=len(ordered_candidates),
                    cached_count=cached_count,
                    cached_images=cached_images,
                    cached_videos=cached_videos,
                )

            if should_stop() or stop_detected:
                cached_count, cached_images, cached_videos = catalog.summarize()
                state.update(
                    downloaded_posts=cached_count,
                    downloaded_tweets=cached_count,
                    downloaded_images=cached_images,
                    downloaded_videos=cached_videos,
                )
                return GrokSyncResult(
                    discovered_count=len(ordered_candidates),
                    downloaded_count=downloaded_count,
                    downloaded_images=downloaded_images,
                    downloaded_videos=downloaded_videos,
                    skipped_known=max(0, len(ordered_candidates) - queued_candidates) + deduped_by_hash,
                    deduped_by_hash=deduped_by_hash,
                    skipped_size=size_skipped_count,
                    failed_count=failed_count,
                    cached_count=cached_count,
                    cached_images=cached_images,
                    cached_videos=cached_videos,
                    stopped=True,
                )

            cached_count, cached_images, cached_videos = catalog.summarize()
            return GrokSyncResult(
                discovered_count=len(ordered_candidates),
                downloaded_count=downloaded_count,
                downloaded_images=downloaded_images,
                downloaded_videos=downloaded_videos,
                skipped_known=max(0, len(ordered_candidates) - queued_candidates) + deduped_by_hash,
                deduped_by_hash=deduped_by_hash,
                skipped_size=size_skipped_count,
                failed_count=failed_count,
                cached_count=cached_count,
                cached_images=cached_images,
                cached_videos=cached_videos,
            )
    except PlaywrightError as exc:
        raise RuntimeError(f"Grok browser automation failed: {exc}") from exc
    finally:
        for candidate in library_pages_to_close:
            with contextlib.suppress(Exception):
                candidate.close()
        for candidate in details_pages:
            with contextlib.suppress(Exception):
                candidate.close()
