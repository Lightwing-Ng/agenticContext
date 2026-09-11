"""Local media discovery, deletion tombstones, and pagination."""

# Code version: v1.24.1-codex.1

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from threading import Condition, RLock
from time import monotonic
from typing import Any, Iterable, Iterator, Mapping
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

from . import config
from .cache_catalog import LocalTweetCacheIndex
from .resource_persistence import (
    CHATGPT_CATALOG_FILENAME,
    DELETED_MEDIA_FILENAME,
    DELETED_MEDIA_SCHEMA,
    DELETED_MEDIA_SCHEMA_VERSION,
    GROK_CATALOG_FILENAME,
    LEGACY_CHATGPT_CATALOG_FILENAME,
    LEGACY_DELETED_MEDIA_FILENAME,
    LEGACY_GROK_CATALOG_FILENAME,
    read_parquet_rows,
    retire_legacy_file,
    write_parquet_rows_atomic,
)


IMAGE_SUFFIXES = frozenset({".avif", ".gif", ".heic", ".jpeg", ".jpg", ".png", ".webp"})
VIDEO_SUFFIXES = frozenset({".m4v", ".mkv", ".mov", ".mp4", ".webm"})
MEDIA_SUFFIXES = IMAGE_SUFFIXES | VIDEO_SUFFIXES
SOURCE_VALUES = frozenset({"all", "x", "grok", "chatgpt"})
TEXT_SOURCE_VALUES = frozenset(
    {"all", "chatgpt", "claude", "gemini", "grok", "zhihu"}
)
# Gemini has no media cache source. Treat a legacy URL that names Gemini in
# Media mode as the ChatGPT media view instead of silently showing all media.
TEXT_ONLY_SOURCE_VALUES = frozenset({"claude", "gemini", "zhihu"})
MEDIA_KIND_VALUES = frozenset({"all", "image", "video"})
SORT_VALUES = frozenset({"newest", "oldest", "name"})
VIEW_VALUES = frozenset({"media", "text", "prompts"})
PAGE_SIZE = 24
DEFAULT_TTL_SECONDS = 5.0
CHATGPT_TEMPORARY_PROJECT_NAMES = frozenset({"forprompts"})
_DATE_RE = re.compile(r"^\d{8}$")
_NUMERIC_RE = re.compile(r"^\d+(?:\.\d+)?$")
_CHATGPT_BRANCH_MARKER_RE = re.compile(r"\bbranch\b", re.IGNORECASE)
_CHATGPT_BRANCH_LABEL_RE = re.compile(r"\bbranch\b\s*(?:[·•]|[-–—])?\s*", re.IGNORECASE)
_CHATGPT_MASTER_REVISION_RE = re.compile(r"\bmaster\s+([0-9]{4}[a-z0-9._-]*)\b", re.IGNORECASE)
_DATE_ONLY_DISPLAY_RE = re.compile(r"^(?:\d{4}[-/]\d{2}[-/]\d{2}|\d{8})$")
DISPLAY_TIMEZONE = ZoneInfo("Asia/Hong_Kong")


@dataclass(frozen=True, slots=True)
class LocalMediaItem:
    """Describe one readable media file without exposing its absolute path."""

    stable_id: str
    source: str
    media_kind: str
    relative_path: str
    filename: str
    title: str
    description: str
    creator: str
    source_url: str
    captured_at: str
    captured_at_label: str
    content_bytes: int
    project_name: str
    alt_text: str = ""
    prompt_markdown: str = ""
    width: int = 0
    height: int = 0
    resource_key: str = ""
    chatgpt_session_key: str = ""
    chatgpt_branch_key: str = ""
    is_deleted: bool = False
    deleted_at: str = ""
    preview_relative_path: str = ""


@dataclass(frozen=True, slots=True)
class LocalMediaPaginationItem:
    """Describe one server-backed control in the shared local-store pagination UI."""

    kind: str
    page: int = 0
    is_active: bool = False
    position: str = ""
    ranges: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True, slots=True)
class LocalMediaPage:
    """Contain a filtered, sorted, and paginated media result."""

    items: tuple[LocalMediaItem, ...]
    total_count: int
    image_count: int
    video_count: int
    current_page: int
    total_pages: int
    page_size: int = PAGE_SIZE
    pagination_unit: str = "media"
    session_count: int = 0
    current_session_key: str = ""
    current_session_label: str = ""
    current_session_latest_at: str = ""
    current_session_url: str = ""

    @property
    def pagination_items(self) -> tuple[LocalMediaPaginationItem, ...]:
        """Return the same five-page pagination sequence used by the investment table."""
        return build_local_store_pagination(self.total_pages, self.current_page)

    @property
    def pagination_active_index(self) -> int:
        """Return the zero-based control index used to place the active indicator."""
        for index, item in enumerate(self.pagination_items):
            if item.is_active:
                return index
        return 0


def stable_media_id(relative_path: str) -> str:
    """Return a deterministic identifier derived from a POSIX relative path."""
    normalized_path = str(relative_path or "").replace("\\", "/")
    digest = hashlib.sha256(normalized_path.encode("utf-8")).hexdigest()
    return f"media-{digest[:24]}"


def format_captured_at_label(value: str | datetime | None) -> str:
    """Format a media date as ``dd/mm/yyyy`` for the UI."""
    return format_datetime_label(value, date_only=True)


def format_captured_at_timestamp_label(value: str | datetime | None) -> str:
    """Format a media date or timestamp according to the UI datetime contract."""
    return format_datetime_label(value)


def format_datetime_label(
    value: str | datetime | None,
    *,
    date_only: bool = False,
) -> str:
    """Format one user-visible date or timestamp in Hong Kong time."""
    parsed = _parse_datetime(value)
    if parsed is None:
        return "Unknown date"
    if date_only or _is_date_only_value(value):
        return f"{parsed.day:02d}/{parsed.month:02d}/{parsed.year:04d}"

    localized = parsed.astimezone(DISPLAY_TIMEZONE)
    timezone_code = _timezone_code(localized)
    return (
        f"{localized.day:02d}/{localized.month:02d}/{localized.year:04d} "
        f"{localized.hour:02d}:{localized.minute:02d}:{localized.second:02d} "
        f"({timezone_code})"
    )


def resolve_local_media_path(local_store_root: Path | str, relative_path: str) -> Path | None:
    """Resolve a media path only when it remains a readable file inside the cache."""
    root = Path(local_store_root).expanduser().resolve(strict=False)
    decoded_path = _decode_relative_path(relative_path)
    if decoded_path is None:
        return None

    relative = PurePosixPath(decoded_path)
    if relative.is_absolute() or not relative.parts:
        return None
    if any(part in {"", ".", ".."} or part.startswith(".") for part in relative.parts):
        return None

    candidate = root.joinpath(*relative.parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved_relative = resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None

    if any(part.startswith(".") for part in resolved_relative.parts):
        return None
    if resolved.suffix.lower() not in MEDIA_SUFFIXES:
        return None
    try:
        stat_result = resolved.stat()
    except OSError:
        return None
    if not resolved.is_file() or stat_result.st_size <= 0 or not os.access(resolved, os.R_OK):
        return None
    return resolved


def media_route_relative_path(relative_path: str) -> str:
    """Return the stable public URL path for a stored media relative path."""
    decoded_path = _decode_relative_path(relative_path)
    if decoded_path is None:
        return ""

    parts = PurePosixPath(decoded_path).parts
    if (
        len(parts) >= 3
        and parts[0].casefold() == config.MEDIA_STORE_DIRNAME.casefold()
        and parts[1] in {"chatgpt", "grok"}
    ):
        return PurePosixPath(*parts[1:]).as_posix()
    return decoded_path


def resolve_browser_media_path(local_store_root: Path | str, relative_path: str) -> Path | None:
    """Resolve a public browser path against the current media storage layout."""
    resolved_path = resolve_local_media_path(local_store_root, relative_path)
    if resolved_path is not None:
        return resolved_path

    decoded_path = _decode_relative_path(relative_path)
    if decoded_path is None:
        return None
    parts = PurePosixPath(decoded_path).parts
    if not parts or parts[0] not in {"chatgpt", "grok"}:
        return None
    stored_path = PurePosixPath(config.MEDIA_STORE_DIRNAME, *parts).as_posix()
    return resolve_local_media_path(local_store_root, stored_path)


def local_file_manager_label(*, platform_name: str | None = None, os_name: str | None = None) -> str:
    """Return the platform-native file manager name for interface copy."""
    resolved_platform = sys.platform if platform_name is None else platform_name
    resolved_os_name = os.name if os_name is None else os_name
    if resolved_platform == "darwin":
        return "Finder"
    if resolved_os_name == "nt":
        return "File Explorer"
    return "file manager"


def file_manager_reveal_command(
    media_path: Path | str,
    *,
    platform_name: str | None = None,
    os_name: str | None = None,
) -> list[str]:
    """Build the native command that reveals one trusted local media path."""
    resolved_path = Path(media_path).expanduser().resolve(strict=True)
    resolved_platform = sys.platform if platform_name is None else platform_name
    resolved_os_name = os.name if os_name is None else os_name
    if resolved_platform == "darwin":
        return ["open", "-R", str(resolved_path)]
    if resolved_os_name == "nt":
        return ["explorer.exe", f"/select,{resolved_path}"]
    return ["xdg-open", str(resolved_path.parent)]


def file_manager_open_directory_command(
    directory_path: Path | str,
    *,
    platform_name: str | None = None,
    os_name: str | None = None,
) -> list[str]:
    """Build the platform-native command that opens one trusted directory."""
    resolved_path = Path(directory_path).expanduser().resolve(strict=True)
    if not resolved_path.is_dir():
        raise NotADirectoryError(str(resolved_path))
    resolved_platform = sys.platform if platform_name is None else platform_name
    resolved_os_name = os.name if os_name is None else os_name
    if resolved_platform == "darwin":
        return ["open", str(resolved_path)]
    if resolved_os_name == "nt":
        return ["explorer.exe", str(resolved_path)]
    return ["xdg-open", str(resolved_path)]


def _launch_file_manager(command: list[str]) -> None:
    """Launch a native file-manager command without binding it to the web process."""
    process_options: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if config.is_windows_host():
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess,
            "DETACHED_PROCESS",
            0,
        )
        if creation_flags:
            process_options["creationflags"] = creation_flags
    else:
        process_options["start_new_session"] = True
    subprocess.Popen(command, **process_options)


def reveal_media_path(media_path: Path | str) -> None:
    """Open a trusted media path in the host operating system's file manager."""
    _launch_file_manager(file_manager_reveal_command(media_path))


def open_directory_path(directory_path: Path | str) -> None:
    """Open a trusted local directory in the host operating system's file manager."""
    _launch_file_manager(file_manager_open_directory_command(directory_path))


DELETED_MEDIA_DIRNAME = ".browser-trash"


def normalize_resource_key(source: str, value: str) -> str:
    """Normalize a downloader identity before persisting or comparing exclusions."""
    normalized_source = str(source or "").strip().lower()
    text = str(value or "").strip()
    if normalized_source == "x":
        parsed = urlsplit(text)
        hostname = (parsed.hostname or "").lower()
        if hostname in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"} and parsed.path:
            return f"https://x.com{parsed.path.rstrip('/')}".lower()
    return text.casefold() if normalized_source in {"grok", "chatgpt"} else text


def is_exempt_media_path(relative_path: str) -> bool:
    """Return whether a path belongs to the ChatGPT temporary work directory."""
    parts = _chatgpt_project_relative_parts(relative_path)
    return len(parts) >= 1 and parts[0].casefold() in CHATGPT_TEMPORARY_PROJECT_NAMES


def _chatgpt_project_relative_parts(relative_path: str | None) -> tuple[str, ...]:
    """Return project-relative ChatGPT path parts for current and legacy layouts."""
    parts = PurePosixPath(str(relative_path or "").replace("\\", "/")).parts
    if len(parts) >= 3 and parts[0].casefold() == config.MEDIA_STORE_DIRNAME and parts[1].casefold() == "chatgpt":
        return tuple(parts[2:])
    if len(parts) >= 2 and parts[0].casefold() == "chatgpt":
        return tuple(parts[1:])
    return ()


class BrowserDeletionCatalog:
    """Persist removed media as recoverable tombstones with local previews."""

    def __init__(self, local_store_root: Path | str) -> None:
        self.local_store_root = Path(local_store_root).expanduser().resolve(strict=False)
        self.catalog_path = self.local_store_root / DELETED_MEDIA_FILENAME
        self.trash_root = self.local_store_root / DELETED_MEDIA_DIRNAME
        self._lock = RLock()
        self._entries = self._load_entries()

    def is_excluded(self, source: str, resource_key: str) -> bool:
        """Return whether a source resource has been removed by the operator."""
        normalized_source = str(source or "").strip().lower()
        normalized_key = normalize_resource_key(normalized_source, resource_key)
        if not normalized_source or not normalized_key:
            return False
        with self._lock:
            return any(
                entry.get("source") == normalized_source
                and normalize_resource_key(normalized_source, str(entry.get("resource_key") or ""))
                == normalized_key
                for entry in self._entries.values()
            )

    def delete(self, item: LocalMediaItem) -> LocalMediaItem:
        """Move one active file to recoverable storage and record its exclusion."""
        if item.is_deleted:
            return item
        if is_exempt_media_path(item.relative_path):
            raise PermissionError("ChatGPT temporary media is exempt from browser deletion.")

        with self._lock:
            existing = self._entries.get(item.stable_id)
            if existing is not None:
                return self._item_from_entry(existing)

            media_path = resolve_local_media_path(self.local_store_root, item.relative_path)
            if media_path is None:
                raise FileNotFoundError("Cached media is no longer available.")

            trash_relative_path = f"{DELETED_MEDIA_DIRNAME}/{item.stable_id}{media_path.suffix.lower()}"
            trash_path = self._resolve_storage_path(trash_relative_path, allow_hidden=True)
            if trash_path is None:
                raise ValueError("Invalid deleted-media path.")
            trash_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(media_path), str(trash_path))

            deleted_at = (
                datetime.now(UTC)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )
            payload = asdict(item)
            payload["resource_key"] = item.resource_key or item.source_url or item.relative_path
            payload["deleted_at"] = deleted_at
            entry = {
                "stable_id": item.stable_id,
                "source": item.source,
                "resource_key": payload["resource_key"],
                "original_relative_path": item.relative_path,
                "preview_relative_path": trash_relative_path,
                "item": payload,
                "deleted_at": deleted_at,
            }
            self._entries[item.stable_id] = entry
            self._save_entries()
            return self._item_from_entry(entry)

    def restore(self, stable_id: str) -> LocalMediaItem:
        """Restore one removed file to its original path and clear its exclusion."""
        with self._lock:
            entry = self._entries.get(str(stable_id or ""))
            if entry is None:
                raise KeyError("Removed media was not found.")

            original_relative_path = str(entry.get("original_relative_path") or "")
            original_path = self._resolve_storage_path(original_relative_path, allow_hidden=False)
            preview_relative_path = str(entry.get("preview_relative_path") or "")
            preview_path = self._resolve_storage_path(preview_relative_path, allow_hidden=True)
            if original_path is None or preview_path is None:
                raise ValueError("Removed media has an invalid restore path.")
            if original_path.exists():
                raise FileExistsError("The original cache path is occupied.")
            if not preview_path.is_file():
                raise FileNotFoundError("The retained preview is no longer available.")

            original_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(preview_path), str(original_path))
            del self._entries[str(stable_id)]
            self._save_entries()
            payload = dict(entry.get("item") or {})
            payload["is_deleted"] = False
            payload["deleted_at"] = ""
            payload["preview_relative_path"] = ""
            return self._item_from_payload(payload)

    def preview_path(self, stable_id: str) -> Path | None:
        """Return a retained preview path without exposing arbitrary hidden files."""
        with self._lock:
            entry = self._entries.get(str(stable_id or ""))
            if entry is None:
                return None
            candidate = self._resolve_storage_path(str(entry.get("preview_relative_path") or ""), allow_hidden=True)
            if candidate is None or not candidate.is_file() or candidate.suffix.lower() not in MEDIA_SUFFIXES:
                return None
            return candidate

    def deleted_items(self) -> list[LocalMediaItem]:
        """Return tombstones whose retained media is still available for preview."""
        with self._lock:
            items: list[LocalMediaItem] = []
            for entry in self._entries.values():
                item = self._item_from_entry(entry)
                if is_exempt_media_path(item.relative_path):
                    continue
                if self.preview_path(str(entry.get("stable_id") or "")) is not None:
                    items.append(item)
            return items

    def _load_entries(self) -> dict[str, dict[str, Any]]:
        rows = read_parquet_rows(self.catalog_path)
        legacy_path = self.local_store_root / LEGACY_DELETED_MEDIA_FILENAME
        if rows is None and legacy_path.exists():
            try:
                payload = json.loads(legacy_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeError):
                payload = None
            raw_entries = payload.get("entries") if isinstance(payload, dict) else None
            if isinstance(raw_entries, dict):
                entries = {
                    str(stable_id): dict(entry)
                    for stable_id, entry in raw_entries.items()
                    if isinstance(entry, dict) and str(stable_id).strip()
                }
                self._entries = entries
                self._save_entries()
                retire_legacy_file(legacy_path)
                return entries

        entries: dict[str, dict[str, Any]] = {}
        for row in rows or []:
            stable_id = str(row.get("stable_id") or "").strip()
            if not stable_id:
                continue
            original_relative_path = str(row.get("original_relative_path") or "")
            resource_key = str(row.get("resource_key") or "")
            deleted_at = str(row.get("deleted_at") or "")
            preview_relative_path = str(row.get("preview_relative_path") or "")
            item_payload = {
                "stable_id": stable_id,
                "source": str(row.get("source") or ""),
                "media_kind": str(row.get("media_kind") or ""),
                "relative_path": original_relative_path,
                "filename": str(row.get("filename") or ""),
                "title": str(row.get("title") or ""),
                "description": str(row.get("description") or ""),
                "creator": str(row.get("creator") or ""),
                "source_url": str(row.get("source_url") or ""),
                "captured_at": str(row.get("captured_at") or ""),
                "captured_at_label": str(row.get("captured_at_label") or ""),
                "content_bytes": int(row.get("content_bytes") or 0),
                "project_name": str(row.get("project_name") or ""),
                "alt_text": str(row.get("alt_text") or ""),
                "prompt_markdown": str(row.get("prompt_markdown") or ""),
                "width": int(row.get("width") or 0),
                "height": int(row.get("height") or 0),
                "resource_key": resource_key,
                "chatgpt_session_key": str(row.get("chatgpt_session_key") or ""),
                "chatgpt_branch_key": str(row.get("chatgpt_branch_key") or ""),
                "is_deleted": True,
                "deleted_at": deleted_at,
                "preview_relative_path": preview_relative_path,
            }
            entries[stable_id] = {
                "stable_id": stable_id,
                "source": item_payload["source"],
                "resource_key": resource_key,
                "original_relative_path": original_relative_path,
                "preview_relative_path": preview_relative_path,
                "item": item_payload,
                "deleted_at": deleted_at,
            }
        return entries

    def _save_entries(self) -> None:
        rows: list[dict[str, Any]] = []
        for entry in sorted(self._entries.values(), key=lambda item: str(item.get("stable_id") or "")):
            item_payload = dict(entry.get("item") or {})
            rows.append(
                {
                    "schema_version": DELETED_MEDIA_SCHEMA_VERSION,
                    "stable_id": str(entry.get("stable_id") or item_payload.get("stable_id") or ""),
                    "source": str(entry.get("source") or item_payload.get("source") or ""),
                    "resource_key": str(entry.get("resource_key") or item_payload.get("resource_key") or ""),
                    "original_relative_path": str(
                        entry.get("original_relative_path") or item_payload.get("relative_path") or ""
                    ),
                    "preview_relative_path": str(entry.get("preview_relative_path") or ""),
                    "deleted_at": str(entry.get("deleted_at") or item_payload.get("deleted_at") or ""),
                    "media_kind": str(item_payload.get("media_kind") or ""),
                    "filename": str(item_payload.get("filename") or ""),
                    "title": str(item_payload.get("title") or ""),
                    "description": str(item_payload.get("description") or ""),
                    "creator": str(item_payload.get("creator") or ""),
                    "source_url": str(item_payload.get("source_url") or ""),
                    "captured_at": str(item_payload.get("captured_at") or ""),
                    "captured_at_label": str(item_payload.get("captured_at_label") or ""),
                    "content_bytes": int(item_payload.get("content_bytes") or 0),
                    "project_name": str(item_payload.get("project_name") or ""),
                    "alt_text": str(item_payload.get("alt_text") or ""),
                    "prompt_markdown": str(item_payload.get("prompt_markdown") or ""),
                    "width": int(item_payload.get("width") or 0),
                    "height": int(item_payload.get("height") or 0),
                    "chatgpt_session_key": str(item_payload.get("chatgpt_session_key") or ""),
                    "chatgpt_branch_key": str(item_payload.get("chatgpt_branch_key") or ""),
                }
            )
        write_parquet_rows_atomic(
            self.catalog_path,
            rows,
            DELETED_MEDIA_SCHEMA,
        )
        retire_legacy_file(self.local_store_root / LEGACY_DELETED_MEDIA_FILENAME)

    def _resolve_storage_path(self, relative_path: str, *, allow_hidden: bool) -> Path | None:
        decoded_path = _decode_relative_path(relative_path)
        if decoded_path is None:
            return None
        relative = PurePosixPath(decoded_path)
        if relative.is_absolute() or not relative.parts:
            return None
        if any(part in {"", ".", ".."} or (part.startswith(".") and not allow_hidden) for part in relative.parts):
            return None
        candidate = self.local_store_root.joinpath(*relative.parts)
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(self.local_store_root)
        except (OSError, RuntimeError, ValueError):
            return None
        return resolved

    @staticmethod
    def _item_from_payload(payload: Mapping[str, Any]) -> LocalMediaItem:
        fields = {field.name for field in LocalMediaItem.__dataclass_fields__.values()}
        normalized_payload = {key: payload.get(key) for key in fields if key in payload}
        captured_at = normalized_payload.get("captured_at")
        if captured_at:
            normalized_payload["captured_at_label"] = format_captured_at_label(captured_at)
        return LocalMediaItem(**normalized_payload)

    def _item_from_entry(self, entry: Mapping[str, Any]) -> LocalMediaItem:
        payload = dict(entry.get("item") or {})
        payload["stable_id"] = str(entry.get("stable_id") or payload.get("stable_id") or "")
        payload["relative_path"] = str(entry.get("original_relative_path") or payload.get("relative_path") or "")
        payload["resource_key"] = str(entry.get("resource_key") or payload.get("resource_key") or "")
        payload["is_deleted"] = True
        payload["deleted_at"] = str(entry.get("deleted_at") or payload.get("deleted_at") or "")
        payload["preview_relative_path"] = str(entry.get("preview_relative_path") or "")
        return self._item_from_payload(payload)


def normalize_browser_filters(
    source: str | None = None,
    media_kind: str | None = None,
    query: str | None = None,
    sort: str | None = None,
    page: object = 1,
    session: str | None = None,
    session_view: object = None,
    view: str | None = None,
    media_id: str | None = None,
    session_page: object = 1,
    answerer: str | None = None,
) -> dict[str, Any]:
    """Normalize user-controlled browser filters to safe allowlisted values."""
    normalized_source = str(source or "").strip().lower()
    normalized_kind = str(media_kind or "").strip().lower()
    normalized_sort = str(sort or "").strip().lower()
    normalized_query = str(query or "").strip()[:120]
    normalized_session_view = str(session_view or "").strip().lower()
    normalized_view = str(view or "").strip().lower()
    if not normalized_view:
        normalized_view = "text"
    elif normalized_view not in VIEW_VALUES:
        normalized_view = "media"
    source_values = SOURCE_VALUES if normalized_view == "media" else TEXT_SOURCE_VALUES
    if normalized_view == "media" and normalized_source in TEXT_ONLY_SOURCE_VALUES:
        normalized_source = "chatgpt"
    safe_source = normalized_source if normalized_source in source_values else "all"
    normalized_answerer = str(answerer or "").replace("\x00", "").strip()[:160]
    if normalized_view != "text" or safe_source != "zhihu":
        normalized_answerer = ""
    return {
        "source": safe_source,
        "kind": normalized_kind if normalized_kind in MEDIA_KIND_VALUES else "all",
        "q": normalized_query,
        "sort": normalized_sort if normalized_sort in SORT_VALUES else "newest",
        "page": _coerce_positive_page(page),
        "session": str(session or "").strip()[:160],
        "session_view": normalized_session_view not in {"0", "false", "off"},
        "session_page": _coerce_positive_page(session_page),
        "view": normalized_view,
        "media_id": str(media_id or "").strip()[:96],
        "answerer": normalized_answerer,
    }


def filter_media_items(
    items: Iterable[LocalMediaItem],
    *,
    source: str = "all",
    media_kind: str = "all",
    query: str = "",
    media_id: str = "",
) -> tuple[LocalMediaItem, ...]:
    """Filter media items by source, kind, and case-insensitive text search."""
    normalized = normalize_browser_filters(source, media_kind, query, "newest", 1, view="media")
    search_terms = tuple(normalized["q"].casefold().split())
    filtered: list[LocalMediaItem] = []
    for item in items:
        if normalized["source"] != "all" and item.source != normalized["source"]:
            continue
        if normalized["kind"] != "all" and item.media_kind != normalized["kind"]:
            continue
        if media_id and item.stable_id != media_id:
            continue
        if search_terms:
            searchable = " ".join(
                (
                    item.filename,
                    item.title,
                    item.description,
                    item.prompt_markdown,
                    item.creator,
                    item.project_name,
                )
            ).casefold()
            if not all(term in searchable for term in search_terms):
                continue
        filtered.append(item)
    return tuple(filtered)


def sort_media_items(items: Iterable[LocalMediaItem], sort: str = "newest") -> tuple[LocalMediaItem, ...]:
    """Sort active media by user order while keeping ChatGPT session families contiguous."""
    normalized_sort = str(sort or "").strip().lower()
    if normalized_sort not in SORT_VALUES:
        normalized_sort = "newest"

    if normalized_sort == "name":
        def key(item: LocalMediaItem) -> tuple[str, str]:
            return (item.filename.casefold(), item.relative_path)
    else:
        direction = -1 if normalized_sort == "newest" else 1

        def key(item: LocalMediaItem) -> tuple[float, str]:
            timestamp = _parse_datetime(item.captured_at)
            timestamp_value = timestamp.timestamp() if timestamp is not None else 0.0
            return (direction * timestamp_value, item.relative_path)

    active_items: list[LocalMediaItem] = []
    deleted_items: list[LocalMediaItem] = []
    for item in items:
        (deleted_items if item.is_deleted else active_items).append(item)

    active_groups: dict[str, list[LocalMediaItem]] = {}
    for item in active_items:
        group_key = _media_sort_group_key(item)
        active_groups.setdefault(group_key, []).append(item)

    def deleted_key(item: LocalMediaItem) -> tuple[float, str]:
        deleted_at = _parse_datetime(item.deleted_at)
        deleted_timestamp = deleted_at.timestamp() if deleted_at is not None else 0.0
        return (deleted_timestamp, item.relative_path)

    sorted_active_groups = sorted(
        active_groups.values(),
        key=lambda group: min(key(item) for item in group),
    )
    sorted_active_items = [
        item
        for group in sorted_active_groups
        for item in sorted(group, key=key)
    ]
    return tuple(sorted_active_items + sorted(deleted_items, key=deleted_key))


def sort_media_items_absolute(items: Iterable[LocalMediaItem], sort: str = "newest") -> tuple[LocalMediaItem, ...]:
    """Sort media independently by absolute generation time without session grouping."""
    materialized = tuple(items)
    normalized_sort = str(sort or "").strip().lower()
    if normalized_sort == "name":
        return tuple(sorted(materialized, key=lambda item: (item.filename.casefold(), item.relative_path)))

    direction = 1 if normalized_sort == "oldest" else -1

    def key(item: LocalMediaItem) -> tuple[float, str]:
        timestamp = _parse_datetime(item.captured_at)
        timestamp_value = timestamp.timestamp() if timestamp is not None else 0.0
        return (direction * timestamp_value, item.relative_path)

    return tuple(sorted(materialized, key=key))


def paginate_media_items(
    items: Iterable[LocalMediaItem],
    page: object = 1,
    page_size: int = PAGE_SIZE,
) -> LocalMediaPage:
    """Return one bounded page and normalized page metadata."""
    materialized = tuple(items)
    safe_page_size = max(1, int(page_size))
    total_count = len(materialized)
    total_pages = max(1, (total_count + safe_page_size - 1) // safe_page_size)
    current_page = min(total_pages, _coerce_positive_page(page))
    start = (current_page - 1) * safe_page_size
    page_items = materialized[start : start + safe_page_size]
    return LocalMediaPage(
        items=page_items,
        total_count=total_count,
        image_count=sum(1 for item in materialized if item.media_kind == "image"),
        video_count=sum(1 for item in materialized if item.media_kind == "video"),
        current_page=current_page,
        total_pages=total_pages,
        page_size=safe_page_size,
    )


def paginate_chatgpt_sessions(
    items: Iterable[LocalMediaItem],
    page: object = 1,
    sort: str = "newest",
    target_session_key: str = "",
) -> LocalMediaPage:
    """Return one whole ChatGPT session per page, ordered by its latest image."""
    materialized = tuple(items)
    groups: dict[str, list[LocalMediaItem]] = {}
    for item in materialized:
        groups.setdefault(_chatgpt_session_page_key(item), []).append(item)

    def timestamp_value(item: LocalMediaItem) -> float:
        timestamp = _parse_datetime(item.captured_at)
        return timestamp.timestamp() if timestamp is not None else 0.0

    def latest_image(group: list[LocalMediaItem]) -> LocalMediaItem:
        images = [item for item in group if item.media_kind == "image"]
        candidates = images or group
        return max(candidates, key=lambda item: (timestamp_value(item), item.relative_path))

    ordered_groups = sorted(
        groups.items(),
        key=lambda entry: (
            -timestamp_value(latest_image(entry[1])),
            entry[0],
        ),
    )
    session_count = len(ordered_groups)
    total_pages = max(1, session_count)
    current_page = min(total_pages, _coerce_positive_page(page))
    normalized_target = str(target_session_key or "").strip()
    if normalized_target:
        current_page = next(
            (
                index
                for index, (session_key, group) in enumerate(ordered_groups, start=1)
                if session_key == normalized_target
                or any(item.chatgpt_session_key == normalized_target for item in group)
            ),
            current_page,
        )
    if ordered_groups:
        current_session_key, current_group = ordered_groups[current_page - 1]
        latest = latest_image(current_group)
        page_items = sort_media_items(current_group, sort)
        explicit_branch_titles = [
            item
            for item in current_group
            if _CHATGPT_BRANCH_MARKER_RE.search(_display_text(item.creator))
        ]
        branch_title = max(
            explicit_branch_titles,
            key=lambda item: (timestamp_value(item), item.relative_path),
            default=None,
        )
        current_session_label = (
            branch_title.creator
            if branch_title is not None and branch_title.creator
            else latest.creator or latest.project_name or "Unknown session"
        )
        current_session_latest_at = latest.captured_at
        current_session_url = latest.source_url
    else:
        current_session_key = ""
        page_items = ()
        current_session_label = ""
        current_session_latest_at = ""
        current_session_url = ""

    return LocalMediaPage(
        items=tuple(page_items),
        total_count=len(materialized),
        image_count=sum(1 for item in materialized if item.media_kind == "image"),
        video_count=sum(1 for item in materialized if item.media_kind == "video"),
        current_page=current_page,
        total_pages=total_pages,
        pagination_unit="session",
        session_count=session_count,
        current_session_key=current_session_key,
        current_session_label=current_session_label,
        current_session_latest_at=current_session_latest_at,
        current_session_url=current_session_url,
    )


def build_local_store_pagination(
    total_pages: int,
    current_page: int,
) -> tuple[LocalMediaPaginationItem, ...]:
    """Build shared five-page controls, returning no controls for a single page."""
    normalized_total_pages = max(1, int(total_pages))
    normalized_current_page = min(normalized_total_pages, max(1, int(current_page)))
    if normalized_total_pages <= 1:
        return ()

    chunk_size = 5
    start_page = ((normalized_current_page - 1) // chunk_size) * chunk_size + 1
    end_page = min(start_page + chunk_size - 1, normalized_total_pages)
    items: list[LocalMediaPaginationItem] = []

    if start_page > 1:
        items.append(LocalMediaPaginationItem(kind="previous", page=start_page - 1))
        items.append(LocalMediaPaginationItem(kind="page", page=1))
        items.append(
            LocalMediaPaginationItem(
                kind="ellipsis",
                position="leading",
                ranges=_build_pagination_ranges(1, start_page - 1, chunk_size),
            )
        )

    for page in range(start_page, end_page + 1):
        items.append(
            LocalMediaPaginationItem(
                kind="page",
                page=page,
                is_active=page == normalized_current_page,
            )
        )

    if end_page < normalized_total_pages:
        items.append(
            LocalMediaPaginationItem(
                kind="ellipsis",
                position="trailing",
                ranges=_build_pagination_ranges(end_page + 1, normalized_total_pages, chunk_size),
            )
        )
        items.append(LocalMediaPaginationItem(kind="page", page=normalized_total_pages))
        items.append(LocalMediaPaginationItem(kind="next", page=end_page + 1))

    return tuple(items)


def _build_pagination_ranges(
    first_page: int,
    last_page: int,
    chunk_size: int,
) -> tuple[tuple[int, int], ...]:
    """Group hidden pages and merge a short final fragment into its preceding range."""
    if first_page > last_page:
        return ()
    ranges = [
        (start_page, min(start_page + chunk_size - 1, last_page))
        for start_page in range(first_page, last_page + 1, chunk_size)
    ]
    if len(ranges) > 1 and ranges[-1][1] - ranges[-1][0] + 1 < chunk_size:
        ranges[-2] = (ranges[-2][0], ranges[-1][1])
        ranges.pop()
    return tuple(ranges)


class LocalMediaCatalog:
    """Scan local media and manage recoverable browser deletions."""

    def __init__(
        self,
        local_store_root: Path | str | None = None,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.local_store_root = Path(local_store_root or config.LOCAL_STORE_ROOT).expanduser().resolve(strict=False)
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self._lock = RLock()
        self._refresh_condition = Condition(self._lock)
        self._deletion_catalog = BrowserDeletionCatalog(self.local_store_root)
        self._cached_snapshot: tuple[LocalMediaItem, ...] = ()
        self._cached_at = 0.0
        self._has_snapshot = False
        self._refreshing = False
        self._generation = 0
        self._x_cache_index: LocalTweetCacheIndex | None = None

    def snapshot(self, force_refresh: bool = False) -> tuple[LocalMediaItem, ...]:
        """Return a short-lived, thread-safe snapshot of readable local media."""
        while True:
            with self._refresh_condition:
                now = monotonic()
                if not force_refresh and self._has_snapshot and now - self._cached_at < self.ttl_seconds:
                    return self._cached_snapshot

                if self._refreshing:
                    if self._has_snapshot and not force_refresh:
                        return self._cached_snapshot
                    while self._refreshing:
                        self._refresh_condition.wait()
                    if self._has_snapshot:
                        return self._cached_snapshot
                    continue

                refresh_generation = self._generation
                self._refreshing = True

            try:
                refreshed_snapshot = self._build_snapshot()
            except Exception:
                with self._refresh_condition:
                    self._refreshing = False
                    self._refresh_condition.notify_all()
                raise

            with self._refresh_condition:
                self._refreshing = False
                if refresh_generation == self._generation:
                    self._cached_snapshot = refreshed_snapshot
                    self._cached_at = monotonic()
                    self._has_snapshot = True
                    self._refresh_condition.notify_all()
                    return self._cached_snapshot
                self._refresh_condition.notify_all()

    def _build_snapshot(self) -> tuple[LocalMediaItem, ...]:
        """Scan the cache outside the snapshot-state lock."""
        items: list[LocalMediaItem] = []
        for scanner in (self._scan_x, self._scan_grok, self._scan_chatgpt):
            try:
                items.extend(scanner())
            except Exception:
                continue

        items.extend(self._hydrate_chatgpt_deleted_items(self._deletion_catalog.deleted_items()))
        return tuple(sorted(items, key=lambda item: (item.relative_path.casefold(), item.relative_path)))

    def _hydrate_chatgpt_deleted_items(
        self,
        items: Iterable[LocalMediaItem],
    ) -> list[LocalMediaItem]:
        """Refresh legacy ChatGPT tombstones from their durable project catalog."""
        hydrated_items: list[LocalMediaItem] = []
        catalogs_by_project: dict[
            str,
            tuple[dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]]],
        ] = {}
        for item in items:
            if item.source != "chatgpt":
                hydrated_items.append(item)
                continue

            decoded_path = _decode_relative_path(item.relative_path)
            project_relative_parts = _chatgpt_project_relative_parts(decoded_path)
            if len(project_relative_parts) < 2:
                hydrated_items.append(item)
                continue

            project_directory_name = project_relative_parts[0]
            if project_directory_name not in catalogs_by_project:
                project_dir = (
                    self.local_store_root
                    / config.MEDIA_STORE_DIRNAME
                    / "chatgpt"
                    / project_directory_name
                )
                if self._resolve_inside(project_dir, require_directory=True) is None:
                    catalogs_by_project[project_directory_name] = ({}, {})
                else:
                    entries_by_path = self._load_chatgpt_catalog(project_dir, include_missing=True)
                    entries_by_file_id = {
                        _display_text(entry.get("file_id")): entry
                        for entry in entries_by_path.values()
                        if _display_text(entry.get("file_id"))
                    }
                    catalogs_by_project[project_directory_name] = (entries_by_path, entries_by_file_id)

            entries_by_path, entries_by_file_id = catalogs_by_project[project_directory_name]
            project_relative_path = PurePosixPath(*project_relative_parts[1:]).as_posix()
            entry = entries_by_file_id.get(item.resource_key) or entries_by_path.get(project_relative_path)
            if entry is None:
                hydrated_items.append(item)
                continue

            source_url = _safe_source_url(entry.get("conversation_url")) or item.source_url
            conversation_title = _display_text(entry.get("conversation_title"))
            prompt_markdown = _display_text(entry.get("prompt_markdown"))
            created_at = _parse_datetime(entry.get("created_at"))
            hydrated_items.append(
                replace(
                    item,
                    creator=conversation_title or item.creator,
                    prompt_markdown=prompt_markdown or item.prompt_markdown,
                    source_url=source_url,
                    captured_at=_isoformat(created_at) if created_at is not None else item.captured_at,
                    captured_at_label=(
                        format_captured_at_label(created_at)
                        if created_at is not None
                        else item.captured_at_label
                    ),
                    chatgpt_session_key=item.chatgpt_session_key or _chatgpt_session_key(source_url),
                    chatgpt_branch_key=(
                        _chatgpt_branch_key(item.project_name, conversation_title)
                        if conversation_title
                        else item.chatgpt_branch_key
                    ),
                )
            )
        return hydrated_items

    def invalidate(self) -> None:
        """Discard the in-memory snapshot without touching the local cache."""
        with self._refresh_condition:
            self._invalidate_locked()

    def delete(self, stable_id: str) -> LocalMediaItem:
        """Remove one cached file from active storage while retaining an undo preview."""
        item = next(
            (candidate for candidate in self.snapshot(force_refresh=True) if candidate.stable_id == stable_id),
            None,
        )
        if item is None:
            raise KeyError("Cached media was not found.")

        with self._refresh_condition:
            deleted_item = self._deletion_catalog.delete(item)
            self._invalidate_locked()
            return deleted_item

    def restore(self, stable_id: str) -> LocalMediaItem:
        """Restore one removed media file and make it eligible for future downloads."""
        with self._refresh_condition:
            restored_item = self._deletion_catalog.restore(stable_id)
            self._invalidate_locked()
            return restored_item

    def _invalidate_locked(self) -> None:
        """Discard cache state while preventing an older scan from publishing."""
        self._generation += 1
        self._cached_snapshot = ()
        self._cached_at = 0.0
        self._has_snapshot = False

    def deleted_preview_path(self, stable_id: str) -> Path | None:
        """Return the retained preview path for one deleted media item."""
        return self._deletion_catalog.preview_path(stable_id)

    def resolved_media_path(self, stable_id: str) -> Path | None:
        """Resolve one active media file or retained deleted preview by stable identifier."""
        item = next(
            (candidate for candidate in self.snapshot(force_refresh=True) if candidate.stable_id == stable_id),
            None,
        )
        if item is None:
            return None
        if item.is_deleted:
            return self.deleted_preview_path(stable_id)
        return resolve_local_media_path(self.local_store_root, item.relative_path)

    def is_excluded(self, source: str, resource_key: str) -> bool:
        """Return whether a source resource is persistently excluded from downloads."""
        return self._deletion_catalog.is_excluded(source, resource_key)

    def query(
        self,
        *,
        source: str = "all",
        media_kind: str = "all",
        query: str = "",
        sort: str = "newest",
        page: object = 1,
        force_refresh: bool = False,
        chatgpt_session_key: str = "",
        chatgpt_session_view: bool = True,
        media_id: str = "",
    ) -> LocalMediaPage:
        """Return a safe filtered, sorted, and paginated view of the snapshot."""
        filters = normalize_browser_filters(source, media_kind, query, sort, page, view="media")
        items = self.snapshot(force_refresh=force_refresh)
        # Keep covers addressable, but show the matching video in ordinary gallery results.
        video_stems = {
            str(Path(item.relative_path).with_suffix(""))
            for item in items if item.source == "x" and item.media_kind == "video" and not item.is_deleted
        }
        if not media_id:
            items = tuple(
                item for item in items
                if not (
                    item.source == "x" and item.media_kind == "image"
                    and str(Path(item.relative_path).with_suffix("")) in video_stems
                )
            )
        filtered = filter_media_items(
            items,
            source=filters["source"],
            media_kind=filters["kind"],
            query=filters["q"],
            media_id=filters["media_id"] or media_id,
        )
        if filters["source"] == "chatgpt" and chatgpt_session_view:
            return paginate_chatgpt_sessions(
                filtered,
                filters["page"],
                filters["sort"],
                chatgpt_session_key,
            )
        if filters["source"] == "chatgpt":
            return paginate_media_items(
                sort_media_items_absolute(filtered, filters["sort"]),
                filters["page"],
            )
        return paginate_media_items(sort_media_items(filtered, filters["sort"]), filters["page"])

    def _scan_x(self) -> list[LocalMediaItem]:
        root = self.local_store_root / "x"
        self._x_cache_index = LocalTweetCacheIndex.build(root)
        items: list[LocalMediaItem] = []
        metadata_by_directory: dict[Path, Mapping[str, Any]] = {}
        try:
            for media_path in self._iter_media_files(root):
                file_info = _stat_media_file(media_path)
                if file_info is None:
                    continue
                directory = media_path.parent
                if directory not in metadata_by_directory:
                    metadata_by_directory[directory] = self._load_x_metadata(directory)
                metadata = metadata_by_directory[directory]
                uploader = _display_text(metadata.get("uploader") or metadata.get("uploader_id"))
                uploader_id = _display_text(metadata.get("uploader_id"))
                display_id = _display_text(metadata.get("display_id"))
                source_url = _safe_source_url(metadata.get("webpage_url"))
                if not source_url and uploader_id and display_id:
                    source_url = f"https://x.com/{uploader_id}/status/{display_id}"
                items.append(
                    self._build_item(
                        media_path,
                        source="x",
                        title=metadata.get("title"),
                        description=metadata.get("description"),
                        creator=uploader or "X",
                        source_url=source_url,
                        resource_key=source_url,
                        captured_value=metadata.get("timestamp") or metadata.get("upload_date"),
                        stat_result=file_info,
                    )
                )
        finally:
            self._x_cache_index = None
        return items

    def _scan_grok(self) -> list[LocalMediaItem]:
        root = self.local_store_root / config.MEDIA_STORE_DIRNAME / "grok"
        files = self._media_files_by_relative_path(root)
        catalog_entries = self._load_grok_catalog(root)
        items: list[LocalMediaItem] = []
        for relative_path, media_path in files.items():
            file_info = _stat_media_file(media_path)
            if file_info is None:
                continue
            entry = catalog_entries.get(relative_path, {})
            identity = _display_text(entry.get("identity"))
            title = identity.rsplit("/", 1)[-1] if identity else media_path.name
            resource_key = identity.split("/", 1)[0] if identity else ""
            items.append(
                self._build_item(
                    media_path,
                    source="grok",
                    title=title,
                    description=identity,
                    creator="Grok",
                    source_url=_safe_source_url(entry.get("source_url")),
                    resource_key=resource_key,
                    captured_value=entry.get("last_seen_at") or entry.get("first_seen_at"),
                    stat_result=file_info,
                )
            )
        return items

    def _scan_chatgpt(self) -> list[LocalMediaItem]:
        root = self.local_store_root / config.MEDIA_STORE_DIRNAME / "chatgpt"
        items: list[LocalMediaItem] = []
        for project_dir in self._iter_visible_directories(root):
            if project_dir.name.casefold() in CHATGPT_TEMPORARY_PROJECT_NAMES:
                continue
            project_name = project_dir.name
            files = self._media_files_by_relative_path(project_dir)
            catalog_entries = self._load_chatgpt_catalog(project_dir)
            for relative_path, media_path in files.items():
                file_info = _stat_media_file(media_path)
                if file_info is None:
                    continue
                entry = catalog_entries.get(relative_path, {})
                alt_text = _display_text(entry.get("alt_text"))
                prompt_markdown = _display_text(entry.get("prompt_markdown"))
                try:
                    width = max(0, int(entry.get("width") or 0))
                    height = max(0, int(entry.get("height") or 0))
                except (TypeError, ValueError):
                    width = 0
                    height = 0
                conversation_url = _safe_source_url(entry.get("conversation_url"))
                conversation_title = _display_text(entry.get("conversation_title"))
                collection_name = conversation_title or project_name
                items.append(
                    self._build_item(
                        media_path,
                        source="chatgpt",
                        title=alt_text or media_path.name,
                        description=alt_text,
                        creator=collection_name,
                        source_url=conversation_url,
                        resource_key=_display_text(entry.get("file_id")) or media_path.stem.removeprefix("img_"),
                        captured_value=(
                            entry.get("created_at")
                            or entry.get("first_seen_at")
                            or entry.get("last_seen_at")
                        ),
                        project_name=project_name,
                        alt_text=alt_text,
                        prompt_markdown=prompt_markdown,
                        width=width,
                        height=height,
                        chatgpt_session_key=_chatgpt_session_key(conversation_url),
                        chatgpt_branch_key=_chatgpt_branch_key(project_name, conversation_title),
                        stat_result=file_info,
                    )
                )
        return items

    def _build_item(
        self,
        media_path: Path,
        *,
        source: str,
        title: Any,
        description: Any,
        creator: Any,
        source_url: str,
        captured_value: Any,
        stat_result: os.stat_result,
        resource_key: str = "",
        project_name: str = "",
        alt_text: str = "",
        prompt_markdown: str = "",
        width: int = 0,
        height: int = 0,
        chatgpt_session_key: str = "",
        chatgpt_branch_key: str = "",
    ) -> LocalMediaItem:
        relative_path = media_path.relative_to(self.local_store_root).as_posix()
        captured_at_dt = _parse_datetime(captured_value) or datetime.fromtimestamp(stat_result.st_mtime, tz=UTC)
        captured_at = _isoformat(captured_at_dt)
        safe_title = _redact_local_root(_display_text(title) or media_path.name, self.local_store_root)
        safe_description = _redact_local_root(_display_text(description), self.local_store_root)
        safe_creator = _redact_local_root(_display_text(creator) or source.title(), self.local_store_root)
        return LocalMediaItem(
            stable_id=stable_media_id(relative_path),
            source=source,
            media_kind="video" if media_path.suffix.lower() in VIDEO_SUFFIXES else "image",
            relative_path=relative_path,
            filename=media_path.name,
            title=safe_title,
            description=safe_description,
            creator=safe_creator,
            source_url=source_url,
            captured_at=captured_at,
            captured_at_label=format_captured_at_label(captured_at_dt),
            content_bytes=int(stat_result.st_size),
            project_name=_redact_local_root(_display_text(project_name), self.local_store_root),
            alt_text=_redact_local_root(_display_text(alt_text), self.local_store_root),
            prompt_markdown=_redact_local_root(_display_text(prompt_markdown), self.local_store_root),
            width=width,
            height=height,
            resource_key=_redact_local_root(_display_text(resource_key), self.local_store_root),
            chatgpt_session_key=_redact_local_root(_display_text(chatgpt_session_key), self.local_store_root),
            chatgpt_branch_key=_redact_local_root(_display_text(chatgpt_branch_key), self.local_store_root),
        )

    def _media_files_by_relative_path(self, root: Path) -> dict[str, Path]:
        files: dict[str, Path] = {}
        for media_path in self._iter_media_files(root):
            try:
                relative_path = media_path.relative_to(root).as_posix()
            except ValueError:
                continue
            files.setdefault(relative_path, media_path)
        return files

    def _iter_media_files(self, root: Path) -> Iterator[Path]:
        if not root.exists() or not root.is_dir():
            return
        pending = [root]
        visited_directories: set[Path] = set()
        while pending:
            directory = pending.pop()
            safe_directory = self._resolve_inside(directory, require_directory=True)
            if safe_directory is None:
                continue
            if safe_directory in visited_directories:
                continue
            visited_directories.add(safe_directory)
            try:
                children = sorted(directory.iterdir(), key=lambda item: (item.name.casefold(), item.name))
            except OSError:
                continue
            for child in children:
                if child.name.startswith("."):
                    continue
                if child.is_symlink():
                    resolved_child = self._resolve_inside(child)
                    if resolved_child is None or resolved_child.is_dir():
                        continue
                    if resolved_child.is_file() and child.suffix.lower() in MEDIA_SUFFIXES:
                        yield child
                    continue
                try:
                    if child.is_dir():
                        pending.append(child)
                    elif child.is_file() and child.suffix.lower() in MEDIA_SUFFIXES:
                        if self._resolve_inside(child) is not None:
                            yield child
                except OSError:
                    continue

    def _iter_visible_directories(self, root: Path) -> Iterator[Path]:
        if not root.exists() or not root.is_dir():
            return
        try:
            children = sorted(root.iterdir(), key=lambda item: (item.name.casefold(), item.name))
        except OSError:
            return
        for child in children:
            if child.name.startswith(".") or child.is_symlink():
                continue
            try:
                if child.is_dir() and self._resolve_inside(child, require_directory=True) is not None:
                    yield child
            except OSError:
                continue

    def _resolve_inside(self, path: Path, require_directory: bool = False) -> Path | None:
        try:
            lexical_relative = path.relative_to(self.local_store_root)
            if any(part.startswith(".") for part in lexical_relative.parts):
                return None
            resolved = path.resolve(strict=True)
            resolved_relative = resolved.relative_to(self.local_store_root)
        except (OSError, RuntimeError, ValueError):
            return None
        if any(part.startswith(".") for part in resolved_relative.parts):
            return None
        if require_directory and not resolved.is_dir():
            return None
        if not require_directory and not resolved.exists():
            return None
        return resolved

    def _load_x_metadata(self, directory: Path) -> Mapping[str, Any]:
        """Read X display metadata from the account-level Parquet catalog."""
        cache_index = self._x_cache_index or LocalTweetCacheIndex.build(self.local_store_root / "x")
        return cache_index.metadata_for_directory(directory)

    def _load_grok_catalog(self, root: Path) -> dict[str, Mapping[str, Any]]:
        raw_entries: object = read_parquet_rows(root / GROK_CATALOG_FILENAME)
        legacy_path = root / LEGACY_GROK_CATALOG_FILENAME
        if raw_entries is None and legacy_path.exists():
            payload = _read_json_object(legacy_path, self.local_store_root, allow_hidden=True)
            legacy_entries = payload.get("entries") if payload else None
            if isinstance(legacy_entries, Mapping):
                legacy_entries = list(legacy_entries.values())
            if isinstance(legacy_entries, list):
                raw_entries = legacy_entries
        if not isinstance(raw_entries, list):
            return {}
        entries: dict[str, Mapping[str, Any]] = {}
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, Mapping):
                continue
            relative_path = _safe_catalog_relative_path(raw_entry.get("relative_path"))
            if not relative_path:
                continue
            candidate = root / relative_path
            if self._resolve_inside(candidate) is None:
                continue
            try:
                local_relative = candidate.relative_to(root).as_posix()
            except ValueError:
                continue
            entries.setdefault(local_relative, raw_entry)
        return entries

    def _load_chatgpt_catalog(
        self,
        project_dir: Path,
        *,
        include_missing: bool = False,
    ) -> dict[str, Mapping[str, Any]]:
        raw_entries: object = read_parquet_rows(project_dir / CHATGPT_CATALOG_FILENAME)
        legacy_path = project_dir / LEGACY_CHATGPT_CATALOG_FILENAME
        if raw_entries is None and legacy_path.exists():
            payload = _read_json_object(legacy_path, self.local_store_root, allow_hidden=True)
            legacy_entries = payload.get("entries") if payload else None
            if isinstance(legacy_entries, Mapping):
                raw_entries = list(legacy_entries.values())
        if not isinstance(raw_entries, list):
            return {}
        entries: dict[str, Mapping[str, Any]] = {}
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, Mapping):
                continue
            relative_path = _safe_catalog_relative_path(raw_entry.get("relative_path"))
            if not relative_path:
                continue
            candidate = project_dir / relative_path
            if include_missing:
                try:
                    candidate.resolve(strict=False).relative_to(self.local_store_root)
                except (OSError, RuntimeError, ValueError):
                    continue
            elif self._resolve_inside(candidate) is None:
                continue
            try:
                local_relative = candidate.relative_to(project_dir).as_posix()
            except ValueError:
                continue
            entries.setdefault(local_relative, raw_entry)
        return entries


def _decode_relative_path(value: str | None) -> str | None:
    """Decode URL path escapes enough times to reject encoded traversal."""
    if value is None:
        return None
    decoded = str(value)
    for _ in range(4):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    if not decoded or "\x00" in decoded or "\\" in decoded:
        return None
    return decoded


def _safe_catalog_relative_path(value: Any) -> str | None:
    text = _decode_relative_path(str(value or ""))
    if text is None:
        return None
    relative = PurePosixPath(text)
    if relative.is_absolute() or not relative.parts:
        return None
    if any(part in {"", ".", ".."} or part.startswith(".") for part in relative.parts):
        return None
    return relative.as_posix()


def _read_json_object(
    path: Path,
    local_store_root: Path,
    *,
    allow_hidden: bool = False,
) -> dict[str, Any]:
    """Read one local JSON object without following links outside the cache."""
    try:
        resolved = path.resolve(strict=True)
        resolved_relative = resolved.relative_to(local_store_root)
        if not allow_hidden and any(part.startswith(".") for part in resolved_relative.parts):
            return {}
        if not resolved.is_file() or not os.access(resolved, os.R_OK):
            return {}
        with resolved.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, RuntimeError, ValueError, TypeError, UnicodeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _stat_media_file(path: Path) -> os.stat_result | None:
    """Return stat data for a non-empty, readable regular file."""
    try:
        stat_result = path.stat()
    except OSError:
        return None
    if not path.is_file() or stat_result.st_size <= 0 or not os.access(path, os.R_OK):
        return None
    return stat_result


def _display_text(value: Any) -> str:
    """Convert metadata to bounded, safe display text."""
    if value is None:
        return ""
    return str(value).replace("\x00", "").strip()


def _redact_local_root(value: str, local_store_root: Path) -> str:
    """Prevent the configured absolute cache path from entering page data."""
    text = str(value or "")
    root_text = str(local_store_root)
    alternate_root_text = root_text.replace("/private/var/", "/var/")
    for candidate in {root_text, alternate_root_text}:
        if candidate:
            text = text.replace(candidate, "[local cache]")
    return text


def _safe_source_url(value: Any) -> str:
    """Keep only source URLs that can be safely rendered as external links."""
    text = _display_text(value)
    parsed = urlsplit(text)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    return text


def _chatgpt_session_key(value: str) -> str:
    """Return the stable conversation ID embedded in one ChatGPT URL."""
    parsed = urlsplit(value)
    path_parts = [part for part in parsed.path.split("/") if part]
    if parsed.scheme.lower() != "https" or parsed.netloc.lower() != "chatgpt.com":
        return ""
    try:
        conversation_index = len(path_parts) - 1 - path_parts[::-1].index("c")
    except ValueError:
        return ""
    return path_parts[conversation_index + 1] if conversation_index + 1 < len(path_parts) else ""


def _chatgpt_branch_key(project_name: str, conversation_title: str) -> str:
    """Return a conservative family key for ChatGPT branch titles."""
    title = _display_text(conversation_title)
    if not title:
        return ""
    project_key = _display_text(project_name).casefold()
    master_match = _CHATGPT_MASTER_REVISION_RE.search(title)
    if master_match:
        return f"{project_key}:master:{master_match.group(1).casefold()}"
    if not _CHATGPT_BRANCH_MARKER_RE.search(title):
        return ""
    base_title = _CHATGPT_BRANCH_LABEL_RE.sub("", title)
    normalized_title = re.sub(r"[^a-z0-9]+", " ", base_title.casefold()).strip()
    return f"{project_key}:branch:{normalized_title}" if normalized_title else ""


def _media_sort_group_key(item: LocalMediaItem) -> str:
    """Return one grouping key that keeps related ChatGPT iterations adjacent."""
    if item.source != "chatgpt":
        return f"media:{item.stable_id}"
    project_key = item.project_name.casefold()
    if item.chatgpt_branch_key:
        return f"chatgpt:branch:{item.chatgpt_branch_key}"
    if item.chatgpt_session_key:
        return f"chatgpt:session:{project_key}:{item.chatgpt_session_key}"
    return f"media:{item.stable_id}"


def _chatgpt_session_page_key(item: LocalMediaItem) -> str:
    """Return the strict session key used by ChatGPT-specific pagination."""
    project_key = item.project_name.casefold()
    session_key = item.chatgpt_session_key or _chatgpt_session_key(item.source_url)
    if session_key:
        return f"chatgpt:session:{project_key}:{session_key}"
    if item.source_url:
        return f"chatgpt:url:{project_key}:{item.source_url.casefold()}"
    fallback_label = (item.creator or item.project_name).casefold()
    if fallback_label:
        return f"chatgpt:unknown:{project_key}:{fallback_label}"
    return f"chatgpt:orphan:{item.stable_id}"


def _parse_datetime(value: str | datetime | Any) -> datetime | None:
    """Parse epoch, yyyymmdd, ISO, and date-only metadata as UTC."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, bool) or value is None:
        return None
    elif isinstance(value, (int, float)):
        try:
            parsed = datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        text = str(value).strip()
        if not text:
            return None
        if _DATE_RE.fullmatch(text):
            try:
                parsed = datetime.strptime(text, "%Y%m%d").replace(tzinfo=UTC)
            except ValueError:
                return None
        elif _NUMERIC_RE.fullmatch(text):
            try:
                parsed = datetime.fromtimestamp(float(text), tz=UTC)
            except (OverflowError, OSError, ValueError):
                return None
        else:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _is_date_only_value(value: str | datetime | None) -> bool:
    """Return whether a value explicitly contains a date without a time."""
    if isinstance(value, datetime) or value is None:
        return False
    return bool(_DATE_ONLY_DISPLAY_RE.fullmatch(str(value).strip()))


def _timezone_code(value: datetime) -> str:
    """Return a three-letter timezone code for one localized datetime."""
    timezone_name = (value.tzname() or "").strip()
    if len(timezone_name) == 3 and timezone_name.isalpha():
        return timezone_name.upper()
    offset = value.utcoffset()
    if offset is not None and offset.total_seconds() == 8 * 60 * 60:
        return "HKT"
    if offset is not None and offset.total_seconds() == 0:
        return "UTC"
    return "UTC"


def _isoformat(value: datetime) -> str:
    """Normalize one datetime to a stable UTC ISO representation."""
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _coerce_positive_page(value: object) -> int:
    try:
        numeric = int(str(value).strip())
    except (TypeError, ValueError):
        return 1
    return max(1, numeric)
