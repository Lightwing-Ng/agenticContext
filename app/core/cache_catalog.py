"""Persistent local X cache catalog backed by typed Parquet rows."""

# Code version: v1.6.0-codex.0

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Mapping

from .resource_persistence import (
    X_CACHE_CATALOG_FILENAME,
    X_CACHE_CATALOG_SCHEMA,
    X_CACHE_CATALOG_SCHEMA_VERSION,
    read_parquet_rows,
    retire_legacy_file,
    write_parquet_rows_atomic,
)


CATALOG_FILENAME = X_CACHE_CATALOG_FILENAME
INFO_JSON_SUFFIXES = (".info.json", ".info.json.info.json")
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
CATALOG_SCHEMA_VERSION = X_CACHE_CATALOG_SCHEMA_VERSION
X_METADATA_COLUMNS = (
    "title",
    "description",
    "uploader",
    "uploader_id",
    "display_id",
    "webpage_url",
    "upload_date",
)


@dataclass(slots=True)
class AccountCacheSummary:
    """Capture local cache totals for one output directory."""

    account_name: str
    output_dir: Path
    downloaded_posts: int = 0
    downloaded_images: int = 0
    downloaded_videos: int = 0


def canonicalize_tweet_url(tweet_url: str) -> str:
    """Normalize X or Twitter status URLs so duplicate matches are stable."""
    text = (tweet_url or "").strip()
    if not text:
        return ""

    text = text.split("?", 1)[0].rstrip("/")
    if not text:
        return ""

    if "://" not in text:
        text = f"https://x.com/{text.lstrip('/')}"

    _, _, remainder = text.partition("://")
    host, _, path = remainder.partition("/")
    host = host.lower()
    if host in {"twitter.com", "www.twitter.com", "mobile.twitter.com", "www.x.com", "mobile.x.com"}:
        host = "x.com"

    normalized_path = "/" + path.lstrip("/") if path else ""
    return f"https://{host}{normalized_path}".rstrip("/")


def extract_status_id(tweet_url: str) -> str:
    """Extract the tweet status ID from a URL if present."""
    marker = "/status/"
    if marker not in (tweet_url or ""):
        return ""
    suffix = (tweet_url or "").split(marker, 1)[1]
    status_id = suffix.split("/", 1)[0].split("?", 1)[0].strip()
    return status_id if status_id.isdigit() else ""


def is_info_json_path(path: Path) -> bool:
    """Return whether a path is a legacy yt-dlp metadata sidecar."""
    return path.is_file() and path.name.endswith(INFO_JSON_SUFFIXES)


def load_info_payload(tweet_dir: Path) -> dict[str, object]:
    """Load the first readable legacy info JSON payload for migration."""
    try:
        children = sorted(tweet_dir.iterdir(), key=lambda path: (path.name.casefold(), path.name))
    except OSError:
        return {}
    for child in children:
        if not is_info_json_path(child):
            continue
        try:
            payload = json.loads(child.read_text())
        except (OSError, json.JSONDecodeError, UnicodeError):
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def summarize_cached_tweet_dir(
    tweet_dir: Path,
    metadata: Mapping[str, object] | None = None,
) -> tuple[bool, int, int]:
    """Return whether a tweet dir is cached plus its image and video counts."""
    if not tweet_dir.exists() or not tweet_dir.is_dir():
        return False, 0, 0

    try:
        children = list(tweet_dir.iterdir())
    except OSError:
        return False, 0, 0
    media_files = [
        child
        for child in children
        if child.is_file()
        and child.suffix.lower() in IMAGE_SUFFIXES | VIDEO_SUFFIXES
    ]
    if not media_files:
        return False, 0, 0

    image_count = sum(child.suffix.lower() in IMAGE_SUFFIXES for child in media_files)
    video_count = sum(child.suffix.lower() in VIDEO_SUFFIXES for child in media_files)
    payload = dict(metadata) if metadata is not None else load_info_payload(tweet_dir)
    if str(payload.get("_type") or "").lower() == "video" and video_count > 0:
        image_count = 0

    return True, image_count, video_count


def tweet_dir_has_cached_media(tweet_dir: Path) -> bool:
    """Return whether a tweet directory has reusable cached media."""
    is_cached, _image_count, _video_count = summarize_cached_tweet_dir(tweet_dir)
    return is_cached


def candidate_urls_from_payload(payload: Mapping[str, object]) -> list[str]:
    """Derive stable tweet URLs from stored yt-dlp metadata."""
    candidate_urls = [str(payload.get("webpage_url") or "").strip()]
    uploader_id = str(payload.get("uploader_id") or "").strip()
    display_id = str(payload.get("display_id") or "").strip()
    if uploader_id and display_id:
        candidate_urls.append(f"https://x.com/{uploader_id}/status/{display_id}")
    return [url for url in candidate_urls if url]


@dataclass(slots=True)
class LocalTweetCacheIndex:
    """Track cached tweets and their display metadata in one Parquet catalog."""

    output_dir: Path
    catalog_path: Path
    directories_by_status_id: dict[str, set[Path]] = field(default_factory=dict)
    directories_by_url: dict[str, set[Path]] = field(default_factory=dict)
    media_counts_by_directory: dict[Path, tuple[int, int]] = field(default_factory=dict)
    metadata_by_directory: dict[Path, dict[str, object]] = field(default_factory=dict)
    dirty: bool = False
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)
    _inflight_keys: set[str] = field(default_factory=set, init=False, repr=False)

    @classmethod
    def build(cls, output_dir: Path) -> "LocalTweetCacheIndex":
        """Load Parquet state and retire imported yt-dlp JSON sidecars."""
        index = cls(output_dir=output_dir, catalog_path=output_dir / CATALOG_FILENAME)
        if not output_dir.exists():
            return index

        legacy_paths = index._legacy_info_paths()
        if index._load_parquet_catalog():
            if legacy_paths:
                index._merge_legacy_info_sidecars(legacy_paths)
            if index.dirty:
                index.flush()
            index._retire_legacy_info_sidecars(legacy_paths)
            return index

        index._rebuild_from_disk(legacy_paths)
        index.flush()
        index._retire_legacy_info_sidecars(legacy_paths)
        return index

    def register(
        self,
        tweet_url: str,
        tweet_dir: Path | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        """Remember a tweet URL plus optional local directory and source metadata."""
        with self._lock:
            canonical_url = canonicalize_tweet_url(tweet_url)
            status_id = extract_status_id(tweet_url)
            if canonical_url:
                self.directories_by_url.setdefault(canonical_url, set())
            if status_id:
                self.directories_by_status_id.setdefault(status_id, set())

            if tweet_dir is None:
                return

            normalized_metadata = _normalize_metadata(metadata)
            is_cached, image_count, video_count = summarize_cached_tweet_dir(
                tweet_dir,
                normalized_metadata if normalized_metadata else None,
            )
            if not is_cached:
                return

            self._remember_directory(tweet_dir, image_count, video_count)
            self._remember_url(tweet_url, tweet_dir)
            if normalized_metadata:
                self._remember_metadata(tweet_dir, normalized_metadata)
            self.dirty = True
            self.flush()

    def metadata_for_directory(self, tweet_dir: Path) -> dict[str, object]:
        """Return one copy of the source metadata associated with a cached directory."""
        with self._lock:
            metadata = self.metadata_by_directory.get(tweet_dir)
            if metadata is None:
                resolved = tweet_dir.resolve(strict=False)
                metadata = next(
                    (
                        candidate_metadata
                        for candidate_dir, candidate_metadata in self.metadata_by_directory.items()
                        if candidate_dir.resolve(strict=False) == resolved
                    ),
                    {},
                )
            return dict(metadata)

    def contains_complete_cache(self, tweet_url: str) -> bool:
        """Return whether the given tweet already has reusable local media."""
        with self._lock:
            return self._contains_complete_cache_unlocked(tweet_url)

    def claim(self, tweet_url: str, *, allow_cached: bool = False) -> bool:
        """Claim one tweet URL for active processing inside the current process."""
        claim_keys = self._claim_keys(tweet_url)
        with self._lock:
            if not allow_cached and self._contains_complete_cache_unlocked(tweet_url):
                return False
            if any(key in self._inflight_keys for key in claim_keys):
                return False
            self._inflight_keys.update(claim_keys)
            return True

    def release_claim(self, tweet_url: str) -> None:
        """Release one in-flight claim after the worker finishes."""
        claim_keys = self._claim_keys(tweet_url)
        with self._lock:
            self._inflight_keys.difference_update(claim_keys)

    def lookup_directories(self, tweet_url: str) -> set[Path]:
        """Return directories associated with a tweet URL or status ID."""
        with self._lock:
            return set(self._lookup_directories_unlocked(tweet_url))

    def summarize(self) -> tuple[int, int, int]:
        """Return cached posts, images, and videos for the indexed output directory."""
        with self._lock:
            status_by_directory = {
                directory: status_id
                for status_id, directories in self.directories_by_status_id.items()
                for directory in directories
            }
            downloaded_posts = len({
                ("status", status_by_directory[directory])
                if directory in status_by_directory else ("directory", str(directory))
                for directory in self.media_counts_by_directory
            })
            downloaded_images = sum(image_count for image_count, _video_count in self.media_counts_by_directory.values())
            downloaded_videos = sum(video_count for _image_count, video_count in self.media_counts_by_directory.values())
            return downloaded_posts, downloaded_images, downloaded_videos

    def flush(self) -> None:
        """Atomically persist the current catalog with native Parquet list columns."""
        with self._lock:
            if not self.dirty:
                return

            rows: list[dict[str, object]] = []
            for tweet_dir in sorted(self.media_counts_by_directory, key=lambda path: str(path)):
                try:
                    relative_tweet_dir = tweet_dir.relative_to(self.output_dir).as_posix()
                except ValueError:
                    continue
                image_count, video_count = self.media_counts_by_directory[tweet_dir]
                canonical_urls = sorted(
                    url for url, directories in self.directories_by_url.items() if tweet_dir in directories
                )
                status_ids = sorted(
                    status_id for status_id, directories in self.directories_by_status_id.items() if tweet_dir in directories
                )
                metadata = self.metadata_by_directory.get(tweet_dir, {})
                rows.append(
                    {
                        "schema_version": CATALOG_SCHEMA_VERSION,
                        "relative_tweet_dir": relative_tweet_dir,
                        "canonical_urls": canonical_urls,
                        "status_ids": status_ids,
                        "image_count": image_count,
                        "video_count": video_count,
                        "title": str(metadata.get("title") or ""),
                        "description": str(metadata.get("description") or ""),
                        "uploader": str(metadata.get("uploader") or ""),
                        "uploader_id": str(metadata.get("uploader_id") or ""),
                        "display_id": str(metadata.get("display_id") or ""),
                        "webpage_url": str(metadata.get("webpage_url") or ""),
                        "timestamp": _coerce_float(metadata.get("timestamp")),
                        "upload_date": str(metadata.get("upload_date") or ""),
                        "resource_type": str(metadata.get("_type") or ""),
                    }
                )

            write_parquet_rows_atomic(self.catalog_path, rows, X_CACHE_CATALOG_SCHEMA)
            self.dirty = False

    def _load_parquet_catalog(self) -> bool:
        """Load the account catalog, including the previous Parquet schema."""
        rows = read_parquet_rows(self.catalog_path)
        if rows is None:
            return False

        for row in rows:
            relative_tweet_dir = str(row.get("relative_tweet_dir") or "").strip()
            if not relative_tweet_dir:
                continue
            tweet_dir = self.output_dir / relative_tweet_dir
            if not tweet_dir.exists() or not tweet_dir.is_dir():
                self.dirty = True
                continue

            image_count = int(row.get("image_count") or 0)
            video_count = int(row.get("video_count") or 0)
            self._remember_directory(tweet_dir, image_count, video_count)
            for url in _row_string_list(row, "canonical_urls", "canonical_urls_json"):
                self._remember_url(url, tweet_dir)
            for status_id in _row_string_list(row, "status_ids", "status_ids_json"):
                if status_id:
                    self.directories_by_status_id.setdefault(status_id, set()).add(tweet_dir)

            metadata = _metadata_from_catalog_row(row)
            if metadata:
                self._remember_metadata(tweet_dir, metadata)
            if int(row.get("schema_version") or 1) < CATALOG_SCHEMA_VERSION:
                self.dirty = True

        return True

    def _rebuild_from_disk(self, legacy_paths: list[Path]) -> None:
        """Recreate Parquet rows from legacy metadata and all media-bearing directories."""
        self._merge_legacy_info_sidecars(legacy_paths)
        media_directories = {
            path.parent
            for path in self.output_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES | VIDEO_SUFFIXES
        }
        for tweet_dir in media_directories:
            if tweet_dir in self.media_counts_by_directory:
                continue
            is_cached, image_count, video_count = summarize_cached_tweet_dir(tweet_dir, {})
            if not is_cached:
                continue
            self._remember_directory(tweet_dir, image_count, video_count)
            self.dirty = True

    def _merge_legacy_info_sidecars(self, legacy_paths: list[Path]) -> None:
        """Merge each readable yt-dlp sidecar before its verified retirement."""
        processed_directories: set[Path] = set()
        for info_json_path in legacy_paths:
            tweet_dir = info_json_path.parent
            if tweet_dir in processed_directories:
                continue
            processed_directories.add(tweet_dir)
            payload = load_info_payload(tweet_dir)
            is_cached, image_count, video_count = summarize_cached_tweet_dir(tweet_dir, payload)
            if not is_cached:
                continue
            self._remember_directory(tweet_dir, image_count, video_count)
            if payload:
                self._remember_metadata(tweet_dir, payload)
                for candidate_url in candidate_urls_from_payload(payload):
                    self._remember_url(candidate_url, tweet_dir)
            self.dirty = True

    def _legacy_info_paths(self) -> list[Path]:
        """Return every legacy metadata sidecar under this account directory."""
        return sorted(
            (path for path in self.output_dir.rglob("*.info.json*") if is_info_json_path(path)),
            key=lambda path: path.as_posix(),
        )

    def _retire_legacy_info_sidecars(self, legacy_paths: list[Path]) -> None:
        """Delete imported sidecars only after the Parquet replacement can be read back."""
        if not legacy_paths or read_parquet_rows(self.catalog_path) is None:
            return
        for legacy_path in legacy_paths:
            retire_legacy_file(legacy_path)

    def _remember_directory(self, tweet_dir: Path, image_count: int, video_count: int) -> None:
        """Track one cached tweet directory and its media totals."""
        self.media_counts_by_directory[tweet_dir] = (image_count, video_count)

    def _remember_metadata(self, tweet_dir: Path, metadata: Mapping[str, object]) -> None:
        """Merge source metadata without replacing useful values with blanks."""
        current = self.metadata_by_directory.setdefault(tweet_dir, {})
        for key, value in metadata.items():
            if value not in (None, "", [], {}):
                current[str(key)] = value

    def _remember_url(self, tweet_url: str, tweet_dir: Path) -> None:
        """Associate a canonical URL or status ID with one cached tweet directory."""
        canonical_url = canonicalize_tweet_url(tweet_url)
        status_id = extract_status_id(tweet_url)
        if canonical_url:
            self.directories_by_url.setdefault(canonical_url, set()).add(tweet_dir)
        if status_id:
            self.directories_by_status_id.setdefault(status_id, set()).add(tweet_dir)

    def _lookup_directories_unlocked(self, tweet_url: str) -> set[Path]:
        directories: set[Path] = set()
        canonical_url = canonicalize_tweet_url(tweet_url)
        if canonical_url:
            directories.update(self.directories_by_url.get(canonical_url, set()))

        status_id = extract_status_id(tweet_url)
        if status_id:
            directories.update(self.directories_by_status_id.get(status_id, set()))

        return directories

    def _contains_complete_cache_unlocked(self, tweet_url: str) -> bool:
        status_id = extract_status_id(tweet_url)
        if status_id:
            photo_root = self.output_dir / "photos"
            photo_dir = photo_root / status_id
            marker = photo_dir / ".x-media-plan.json"
            if photo_root.is_symlink() or photo_dir.is_symlink() or marker.is_symlink():
                return False
            if marker.exists():
                try:
                    if not marker.is_file() or marker.stat().st_size > 4096:
                        return False
                    plan = json.loads(marker.read_text(encoding="utf-8"))
                    if not isinstance(plan, dict) or plan.get("complete") is not True:
                        return False
                except (OSError, ValueError):
                    return False
        return any(tweet_dir_has_cached_media(tweet_dir) for tweet_dir in self._lookup_directories_unlocked(tweet_url))

    def _claim_keys(self, tweet_url: str) -> tuple[str, ...]:
        canonical_url = canonicalize_tweet_url(tweet_url)
        status_id = extract_status_id(tweet_url)
        keys = []
        if canonical_url:
            keys.append(f"url:{canonical_url}")
        if status_id:
            keys.append(f"status:{status_id}")
        if not keys:
            keys.append(f"raw:{tweet_url.strip()}")
        return tuple(keys)


def _normalize_metadata(metadata: Mapping[str, object] | None) -> dict[str, object]:
    """Materialize JSON-compatible metadata while retaining unmodeled yt-dlp fields."""
    if not isinstance(metadata, Mapping):
        return {}
    try:
        serialized = json.dumps(dict(metadata), ensure_ascii=False, sort_keys=True, default=str)
        payload = json.loads(serialized)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _coerce_float(value: object) -> float | None:
    """Convert one optional timestamp-like value for the Parquet float column."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _row_string_list(row: Mapping[str, Any], native_key: str, legacy_json_key: str) -> list[str]:
    """Read a native Parquet string list or its schema-v1 JSON-string predecessor."""
    native_value = row.get(native_key)
    if isinstance(native_value, list):
        return [str(value) for value in native_value if str(value).strip()]
    legacy_value = row.get(legacy_json_key)
    if not legacy_value:
        return []
    try:
        parsed = json.loads(str(legacy_value))
    except json.JSONDecodeError:
        return []
    return [str(value) for value in parsed if str(value).strip()] if isinstance(parsed, list) else []


def _metadata_from_catalog_row(row: Mapping[str, Any]) -> dict[str, object]:
    """Rehydrate full metadata while allowing typed columns to repair missing values."""
    metadata: dict[str, object] = {}
    raw_metadata = str(row.get("raw_metadata_json") or "")
    if raw_metadata:
        try:
            parsed = json.loads(raw_metadata)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            metadata.update(parsed)
    for key in X_METADATA_COLUMNS:
        value = row.get(key)
        if value not in (None, ""):
            metadata[key] = value
    if row.get("timestamp") is not None:
        metadata["timestamp"] = row["timestamp"]
    if row.get("resource_type"):
        metadata["_type"] = row["resource_type"]
    return metadata


def summarize_local_store_root(local_store_root: Path) -> list[AccountCacheSummary]:
    """Return cached media totals for each local account directory."""
    if not local_store_root.exists():
        return []

    summaries: list[AccountCacheSummary] = []
    for account_dir in sorted(
        child for child in local_store_root.iterdir() if child.is_dir() and not child.name.startswith(".")
    ):
        index = LocalTweetCacheIndex.build(account_dir)
        downloaded_posts, downloaded_images, downloaded_videos = index.summarize()
        summaries.append(
            AccountCacheSummary(
                account_name=account_dir.name,
                output_dir=account_dir,
                downloaded_posts=downloaded_posts,
                downloaded_images=downloaded_images,
                downloaded_videos=downloaded_videos,
            )
        )
    return summaries
