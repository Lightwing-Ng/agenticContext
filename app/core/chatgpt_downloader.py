"""ChatGPT project image cache helpers."""

# Code version: v1.49.2-codex.1

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import logging
import mimetypes
import os
import re
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty
from threading import Event, RLock, Thread
from typing import Any, Callable, Iterable, Iterator
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from PIL import Image, UnidentifiedImageError

from .cache_timing import wait_for_cache_scan
from .chatgpt_history_cleanup import is_chatgpt_tool_trace
from .browser_sessions import browser_descriptors, launch_chromium_context
from .compute_backend import analyze_image_paths, analyze_image_payload
from .compute_metrics import PerformanceMetrics
from .compute_queue import BoundedWorkQueue
from .config import (
    DEFAULT_CHATGPT_SCAN_WAIT_SECONDS,
    DEFAULT_CHATGPT_PROJECT_NAME,
    DEFAULT_CHATGPT_PROJECT_URL,
    DEFAULT_CHATGPT_STARTUP_TIMEOUT_SECONDS,
    LOCAL_STORE_ROOT,
    MEDIA_STORE_ROOT,
    MAX_CHATGPT_SCAN_WAIT_SECONDS,
    MAX_CHATGPT_STARTUP_TIMEOUT_SECONDS,
    MIN_CHATGPT_SCAN_WAIT_SECONDS,
    MIN_CHATGPT_STARTUP_TIMEOUT_SECONDS,
    CrawlConfig,
)
from .history_rows import partition_conversation_rows, sort_history_rows
from .local_media_browser import BrowserDeletionCatalog
from .resource_persistence import (
    CHATGPT_CATALOG_FILENAME,
    CHATGPT_CATALOG_SCHEMA,
    CHATGPT_CATALOG_SCHEMA_VERSION,
    CHATGPT_HISTORY_FILENAME,
    CHATGPT_HISTORY_SCHEMA,
    CHATGPT_HISTORY_SCHEMA_VERSION,
    LEGACY_CHATGPT_CATALOG_FILENAME,
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


CHATGPT_TARGET_DIR = MEDIA_STORE_ROOT / "chatgpt" / DEFAULT_CHATGPT_PROJECT_NAME
CHATGPT_PARTIAL_DIRNAME = ".chatgpt-partial"
CHATGPT_PAGE_GOTO_TIMEOUT_MS = 120_000
CHATGPT_IMAGE_TIMEOUT_MS = 60_000
CHATGPT_IMAGE_DOWNLOAD_RETRY_LIMIT = 5
CHATGPT_IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS = 0.5
CHATGPT_SAFARI_WORKER_COUNT = 1
CHATGPT_WORKER_JOIN_TIMEOUT_SECONDS = 5.0
CHATGPT_WORKER_START_RETRY_LIMIT = 3
CHATGPT_WORKER_START_RETRY_DELAY_SECONDS = 0.5
CHATGPT_PROJECT_LOAD_ROUNDS = 64
CHATGPT_PROJECT_LINK_WAIT_ROUNDS = 30
CHATGPT_SCROLL_ROUNDS = 80
CHATGPT_SCAN_WAIT_SECONDS = DEFAULT_CHATGPT_SCAN_WAIT_SECONDS
CHATGPT_PROJECT_SCAN_WAIT_SECONDS = 2.0
CHATGPT_SCROLL_STEP_RATIO = 0.8
CHATGPT_PAGE_RECYCLE_INTERVAL = 25
CHATGPT_MAX_CONVERSATION_WORKERS = 3
CHATGPT_RESULT_QUEUE_SIZE = CHATGPT_MAX_CONVERSATION_WORKERS * 4
CHATGPT_PROMPT_METADATA_QUEUE_SIZE = CHATGPT_MAX_CONVERSATION_WORKERS * 4
CHATGPT_CONVERSATION_RESPONSE_WAIT_MS = 5_000
CHATGPT_RENDERED_IMAGE_WAIT_MS = 5_500
CHATGPT_RENDERED_IMAGE_POLL_MS = 250
CHATGPT_RENDERED_IMAGE_STABLE_ROUNDS = 3
CHATGPT_PROJECT_API_PAGE_SIZE = 10
CHATGPT_HISTORY_API_PAGE_SIZE = 100
CHATGPT_RECENT_IMAGE_PAGE_SIZE = 25
CHATGPT_API_PAGE_LIMIT = 1_000
CHATGPT_API_RETRY_LIMIT = 3
CHATGPT_API_RETRY_DELAY_SECONDS = 1.0
CHATGPT_API_RATE_LIMIT_RETRY_LIMIT = 6
CHATGPT_API_RATE_LIMIT_MAX_DELAY_SECONDS = 30.0
CHATGPT_PROMPT_METADATA_PERSIST_BATCH_SIZE = 25
CHATGPT_PROMPT_METADATA_WORKER_JOIN_TIMEOUT_SECONDS = 5.0
CHATGPT_HISTORY_RELATIVE_DIR = Path("llm") / "chatgpt"
CHATGPT_RECOVERABLE_PAGE_ERROR_MARKERS = (
    "Page crashed",
    "frame was detached",
    "ERR_ABORTED",
    "ERR_SSL_BAD_RECORD_MAC_ALERT",
    "ERR_SSL_VERSION_OR_CIPHER_MISMATCH",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "chrome-error://chromewebdata",
    "is interrupted by another navigation",
    "startup timed out",
    "Safari did not finish loading",
    "Safari did not reach load state",
    "Safari window is already closed",
)
CHATGPT_IMAGE_SUFFIXES = {".avif", ".gif", ".heic", ".jpeg", ".jpg", ".png", ".webp"}
CHATGPT_VISUAL_HASH_WIDTH = 32
CHATGPT_VISUAL_HASH_HEIGHT = 32
CHATGPT_VISUAL_HASH_DISTANCE_LIMIT = 48
CHATGPT_VISUAL_ASPECT_RATIO_TOLERANCE = 0.01
CHATGPT_CONVERSATION_API_PATH = "/backend-api/conversation/"
CHATGPT_FILE_DOWNLOAD_PATH = "/backend-api/files/download/"
CHATGPT_HOME_URL = "https://chatgpt.com/"
CHATGPT_AUTH_SESSION_URL = "https://chatgpt.com/api/auth/session"
CHATGPT_DOWNLOAD_AUTH_HEADER_NAMES = {
    "authorization",
    "oai-client-version",
    "oai-device-id",
    "oai-language",
}
CHATGPT_FILE_ID_PATTERN = re.compile(r"file_[A-Za-z0-9_-]+")
CHATGPT_GIZMO_PROJECT_ID_PATTERN = re.compile(r"^(g-p-[0-9a-f]{32})(?:-|$)", re.IGNORECASE)


logger = logging.getLogger(__name__)


def _put_chatgpt_result(
    result_queue: Any,
    result: Any,
    should_stop: Callable[[], bool],
) -> bool:
    """Publish one browser-worker result with bounded, Stop-aware backpressure."""
    if isinstance(result_queue, BoundedWorkQueue):
        return result_queue.put(result, should_stop=should_stop)
    # Preserve compatibility for focused tests and private callers that inject a
    # standard Queue; all production queues are constructed as BoundedWorkQueue.
    result_queue.put(result)
    return True


class ChatGPTRateLimitError(RuntimeError):
    """Signal that one ChatGPT API route should be deferred without aborting the sync."""


@dataclass(slots=True)
class ChatGPTImageCandidate:
    """Describe one original-resolution image found in a ChatGPT conversation."""

    source_url: str
    file_id: str
    conversation_url: str
    alt_text: str = ""
    prompt_markdown: str = ""
    width: int = 0
    height: int = 0
    message_role: str = ""
    conversation_title: str = ""
    created_at: str = ""
    prompt_metadata_authoritative: bool = False
    request_headers: dict[str, str] = field(default_factory=dict, repr=False)


@dataclass(slots=True)
class ChatGPTCatalogEntry:
    """Persist the local mapping for one ChatGPT image file."""

    file_id: str
    relative_path: str
    content_sha256: str
    content_bytes: int
    source_url: str
    conversation_url: str
    alt_text: str
    width: int
    height: int
    first_seen_at: str
    last_seen_at: str
    prompt_markdown: str = ""
    conversation_title: str = ""
    created_at: str = ""
    visual_signature: str = ""


@dataclass(frozen=True, slots=True)
class ChatGPTDuplicateCleanupResult:
    """Describe lower-quality ChatGPT duplicates removed from the cache."""

    removed_file_ids: tuple[str, ...] = ()
    reclaimed_bytes: int = 0

    @property
    def removed_count(self) -> int:
        """Return the number of removed cache files."""
        return len(self.removed_file_ids)


@dataclass(frozen=True, slots=True)
class ChatGPTCatalogRepairResult:
    """Describe incomplete ChatGPT catalog records removed for self-healing."""

    removed_file_ids: tuple[str, ...] = ()
    removed_local_files: int = 0
    reclaimed_bytes: int = 0

    @property
    def removed_count(self) -> int:
        """Return the number of incomplete catalog entries removed."""
        return len(self.removed_file_ids)


@dataclass(slots=True)
class ChatGPTSyncResult:
    """Capture the outcome of one ChatGPT project sync."""

    discovered_conversations: int = 0
    discovered_images: int = 0
    downloaded_count: int = 0
    skipped_known: int = 0
    failed_count: int = 0
    cached_count: int = 0
    stopped: bool = False
    skipped_size: int = 0
    cached_messages: int = 0
    incomplete: bool = False


def chatgpt_history_path(local_store_root: Path | str = LOCAL_STORE_ROOT) -> Path:
    """Return the typed Parquet file used for ChatGPT text history."""
    return Path(local_store_root) / CHATGPT_HISTORY_RELATIVE_DIR / CHATGPT_HISTORY_FILENAME


def _chatgpt_local_store_root(target_dir: Path) -> Path:
    """Resolve shared history and exclusions outside the project media directory."""
    if target_dir.parent.name == "chatgpt" and target_dir.parent.parent.name == "media":
        return target_dir.parents[2]
    return target_dir.parent.parent


def chatgpt_history_path_for_target(target_dir: Path) -> Path:
    """Resolve history independently of the project below the canonical media tree."""
    return chatgpt_history_path(_chatgpt_local_store_root(target_dir))


def chatgpt_history_counts(local_store_root: Path | str = LOCAL_STORE_ROOT) -> dict[str, int]:
    """Read durable text counters without replacing the active media/task snapshot."""
    rows = read_parquet_rows(chatgpt_history_path(local_store_root))
    if rows is None:
        rows = []
    return {
        "cached_sessions": len({row["conversation_id"] for row in rows}),
        "cached_messages": len(rows),
    }


def _chatgpt_message_text(message: object) -> str:
    """Extract readable text from one ChatGPT mapping message."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if not isinstance(parts, list):
        parts = [parts] if parts is not None else []
    values: list[str] = []
    direct_text = content.get("text")
    if isinstance(direct_text, str) and direct_text.strip():
        values.append(direct_text.strip())
    for part in parts:
        if isinstance(part, str) and part.strip():
            values.append(part.strip())
        elif isinstance(part, dict):
            text = part.get("text") or part.get("content")
            if isinstance(text, str) and text.strip():
                values.append(text.strip())
    return "\n\n".join(dict.fromkeys(values))


def _chatgpt_message_timestamp(message: object, fallback: str) -> str:
    """Return a ChatGPT message's original UTC timestamp when the API exposes one."""
    if not isinstance(message, dict):
        return fallback
    for key in ("create_time", "update_time"):
        raw_value = message.get(key)
        if raw_value is None or isinstance(raw_value, bool):
            continue
        try:
            if isinstance(raw_value, (int, float)):
                parsed = datetime.fromtimestamp(float(raw_value), tz=UTC)
            else:
                normalized = str(raw_value).strip()
                if not normalized:
                    continue
                try:
                    parsed = datetime.fromtimestamp(float(normalized), tz=UTC)
                except ValueError:
                    parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=UTC)
                    parsed = parsed.astimezone(UTC)
            return parsed.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, TypeError, ValueError):
            continue
    return fallback


def _chatgpt_message_is_cacheable(message: dict[str, object], role: str) -> bool:
    """Keep visible prompts and final replies using provider message metadata."""
    if role not in {"user", "assistant"}:
        return False
    metadata = message.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    if metadata.get("is_visually_hidden_from_conversation"):
        return False
    content = message.get("content")
    if not isinstance(content, dict):
        return False
    content_type = str(content.get("content_type") or "").strip().lower()
    if content_type not in {"", "text", "multimodal_text"}:
        return False
    if role == "user":
        return True
    recipient = str(message.get("recipient") or "").strip().lower()
    channel = str(message.get("channel") or metadata.get("channel") or "").strip().lower()
    if recipient not in {"", "all"} or channel not in {"", "final"}:
        return False
    if message.get("end_turn") is False:
        return False
    if not channel and is_chatgpt_tool_trace(_chatgpt_message_text(message)):
        return False
    status = str(message.get("status") or "").strip().lower()
    # Older conversations omit channel and completion fields on ordinary replies.
    return status in {"", "finished_successfully"}


def _extract_chatgpt_conversation_messages(
    payload: dict[str, object],
    conversation_url: str,
    captured_at: str,
) -> list[dict[str, object]]:
    """Extract visible user prompts and final assistant replies from every branch."""
    mapping = payload.get("mapping")
    if not isinstance(mapping, dict):
        return []
    conversation_id = chatgpt_conversation_id(conversation_url)
    title = str(payload.get("title") or "Untitled session").strip()
    messages: list[dict[str, object]] = []
    turn_index = -1
    for node_id, node in mapping.items():
        if not isinstance(node, dict) or not isinstance(node.get("message"), dict):
            continue
        message = node["message"]
        author = message.get("author")
        role = str(author.get("role") or "").strip().lower() if isinstance(author, dict) else ""
        if not _chatgpt_message_is_cacheable(message, role):
            continue
        content_text = _chatgpt_message_text(message)
        if not content_text:
            continue
        if role == "user":
            turn_index += 1
        if turn_index < 0:
            turn_index = 0
        message_index = len(messages)
        message_key = f"{conversation_id}:{node_id}"
        message_timestamp = _chatgpt_message_timestamp(message, captured_at)
        messages.append(
            {
                "schema_version": CHATGPT_HISTORY_SCHEMA_VERSION,
                "platform": "chatgpt",
                "conversation_id": conversation_id,
                "conversation_url": conversation_url,
                "conversation_title": title,
                "message_key": message_key,
                "turn_index": turn_index,
                "message_index": message_index,
                "role": role,
                "author_label": "You" if role == "user" else "ChatGPT",
                "content_text": content_text,
                "content_html": "",
                "content_sha256": hashlib.sha256(content_text.encode("utf-8")).hexdigest(),
                "source_links": [],
                "model_label": "",
                "first_seen_at": captured_at,
                "last_seen_at": message_timestamp,
            }
        )
    return messages


class ChatGPTHistoryStore:
    """Merge complete ChatGPT sessions into one atomic Parquet file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        rows = read_parquet_rows(path)
        if path.exists() and rows is None:
            raise RuntimeError(f"ChatGPT history Parquet is unreadable: {path}")
        self._rows_by_key = {
            str(row.get("message_key")): dict(row)
            for row in rows or []
            if str(row.get("message_key") or "").strip()
        }

    @property
    def cached_messages(self) -> int:
        return len(self._rows_by_key)

    def has_conversation(self, conversation_url: str) -> bool:
        """Return whether this store already contains text for one ChatGPT session."""
        conversation_id = chatgpt_conversation_id(conversation_url)
        return bool(conversation_id) and any(
            str(row.get("conversation_id") or "") == conversation_id
            for row in self._rows_by_key.values()
        )

    def conversation_needs_timestamp_refresh(self, conversation_url: str) -> bool:
        """Return whether cached rows lack a verified original message timestamp."""
        conversation_id = chatgpt_conversation_id(conversation_url)
        rows = [
            row
            for row in self._rows_by_key.values()
            if str(row.get("conversation_id") or "") == conversation_id
        ]
        if not rows:
            return False
        return any(
            not str(row.get("last_seen_at") or "").strip()
            or str(row.get("first_seen_at") or "").strip()
            == str(row.get("last_seen_at") or "").strip()
            for row in rows
        )

    def conversation_revision_matches(self, conversation_url: str, revision: str) -> bool:
        """Skip only matching revisions already filtered by the current schema."""
        conversation_id = chatgpt_conversation_id(conversation_url)
        rows = [
            row for row in self._rows_by_key.values()
            if row.get("conversation_id") == conversation_id
        ]
        return bool(revision and rows) and all(
            row.get("provider_revision") == revision
            and row.get("schema_version") == CHATGPT_HISTORY_SCHEMA_VERSION
            for row in rows
        )

    def replace_conversation(
        self,
        conversation_url: str,
        payload: dict[str, object],
        captured_at: str,
        provider_revision: str = "",
    ) -> tuple[int, bool]:
        """Replace one cached conversation and report new-message and unchanged counts."""
        conversation_id = chatgpt_conversation_id(conversation_url)
        previous, retained_rows = partition_conversation_rows(
            self._rows_by_key,
            conversation_id,
        )
        next_rows = {
            str(row["message_key"]): row
            for row in _extract_chatgpt_conversation_messages(payload, conversation_url, captured_at)
        }
        if not next_rows:
            raise RuntimeError(f"ChatGPT session {conversation_id} exposed no cacheable text messages.")
        new_count = sum(
            1 for key, row in next_rows.items()
            if str(previous.get(key, {}).get("content_sha256") or "") != str(row["content_sha256"])
        )
        self._rows_by_key = retained_rows
        for key, row in next_rows.items():
            row["provider_revision"] = provider_revision
            if key in previous and previous[key].get("content_sha256") == row["content_sha256"]:
                row["first_seen_at"] = previous[key].get("first_seen_at") or captured_at
            self._rows_by_key[key] = row
        unchanged = set(previous) == set(next_rows) and new_count == 0
        return new_count, unchanged

    def save(self) -> None:
        """Persist all cached ChatGPT messages with schema verification."""
        write_parquet_rows_atomic(
            self.path,
            sort_history_rows(self._rows_by_key.values()),
            CHATGPT_HISTORY_SCHEMA,
        )

    def refresh_titles(self, titles: dict[str, str]) -> None:
        """Apply the discovery index even when cached message bodies can be skipped."""
        changed = False
        for row in self._rows_by_key.values():
            title = str(titles.get(str(row.get("conversation_id"))) or "").strip()
            if title and title != row.get("conversation_title"):
                row["conversation_title"] = title
                changed = True
        if changed:
            self.save()


def cache_chatgpt_conversation_history(
    history_store: ChatGPTHistoryStore,
    conversation_urls: Iterable[str],
    page,
    request_headers: dict[str, str],
    state: TaskState,
    should_stop,
    scan_wait_seconds: float = 0.0,
    conversation_titles_by_id: dict[str, str] | None = None,
    conversation_revisions_by_id: dict[str, str] | None = None,
) -> tuple[int, int, int]:
    """Fetch and persist complete text mappings for every discovered session."""
    processed = 0
    new_messages = 0
    unchanged_sessions = 0
    failed_sessions = 0
    urls = tuple(conversation_urls)
    history_store.refresh_titles(conversation_titles_by_id or {})
    for index, conversation_url in enumerate(urls, start=1):
        if should_stop():
            break
        api_url = _chatgpt_conversation_api_url(conversation_url)
        if not api_url:
            continue
        revision = (conversation_revisions_by_id or {}).get(chatgpt_conversation_id(conversation_url), "")
        if (
            history_store.conversation_revision_matches(conversation_url, revision)
            and not history_store.conversation_needs_timestamp_refresh(conversation_url)
        ):
            processed += 1
            unchanged_sessions += 1
            continue
        if scan_wait_seconds > 0 and processed:
            if wait_for_cache_scan(scan_wait_seconds, should_stop):
                break
        try:
            payload = _get_chatgpt_api_json_via_page(page, api_url, request_headers)
            added_count, unchanged = history_store.replace_conversation(
                conversation_url,
                payload,
                utc_now(),
                provider_revision=revision,
            )
            history_store.save()
        except ChatGPTRateLimitError:
            failed_sessions += 1
            state.append_event(
                f"ChatGPT text history reached the API rate limit at session {index:,}/{len(urls):,}; "
                "deferring the remaining sessions until the next cache run."
            )
            break
        except (AttributeError, RuntimeError) as exc:
            failed_sessions += 1
            state.append_event(
                f"Failed to cache ChatGPT text session {index:,}/{len(urls):,}: "
                f"{str(exc).splitlines()[0][:300]}"
            )
            continue
        processed += 1
        new_messages += added_count
        unchanged_sessions += int(unchanged)
    state.update(failed_tweets=failed_sessions)
    return processed, new_messages, unchanged_sessions


@dataclass(slots=True)
class ChatGPTConversationWorkResult:
    """Capture one conversation result produced by a parallel worker."""

    conversation_index: int
    conversation_url: str
    candidate_file_ids: tuple[str, ...] = ()
    downloaded_count: int = 0
    skipped_known: int = 0
    failed_count: int = 0
    error: str = ""
    image_errors: tuple[str, ...] = ()
    oversized_count: int = 0


@dataclass(slots=True)
class ChatGPTImageDownloadWorkResult:
    """Capture one direct image-index download outcome."""

    candidate_file_id: str
    downloaded: bool = False
    skipped: bool = False
    error: str = ""
    skipped_size: bool = False


@dataclass(slots=True)
class ChatGPTPromptMetadataWorkResult:
    """Capture one conversation-mapping result from a prompt metadata worker."""

    conversation_index: int
    conversation_url: str
    payload: dict[str, object] | None = None
    error: str = ""


class ChatGPTImageSizeLimitError(RuntimeError):
    """Raised when a ChatGPT image exceeds the universal cache file-size limit."""


@dataclass(slots=True)
class ChatGPTResetResult:
    """Describe what a ChatGPT cache reset removed."""

    removed_media_files: int = 0
    removed_state_files: int = 0
    removed_partial_files: int = 0


def compute_sha256(content: bytes) -> str:
    """Return the SHA-256 digest for one byte payload."""
    return hashlib.sha256(content).hexdigest()


def sanitize_filename_part(value: str) -> str:
    """Convert an untrusted value into a stable filename fragment."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", (value or "").strip())
    return cleaned.strip("._-") or "image"


def chatgpt_target_dir(project_name: str = DEFAULT_CHATGPT_PROJECT_NAME) -> Path:
    """Return the dedicated local directory for one ChatGPT project."""
    return MEDIA_STORE_ROOT / "chatgpt" / sanitize_filename_part(project_name)


def extract_chatgpt_file_id(source_url: str) -> str:
    """Extract the stable file ID from a ChatGPT content URL."""
    query = parse_qs(urlsplit(source_url).query)
    file_id = str((query.get("id") or [""])[0]).strip()
    if file_id:
        return file_id

    digest = hashlib.sha1(source_url.encode("utf-8")).hexdigest()
    return f"url-{digest}"


def image_signature_extension(content: bytes) -> str:
    """Return the suffix implied by an image signature, or an empty string."""
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return ".webp"
    if len(content) >= 12 and content[4:8] == b"ftyp" and content[8:12] in {b"avif", b"avis"}:
        return ".avif"
    if len(content) >= 12 and content[4:8] == b"ftyp" and content[8:12] in {b"heic", b"heix"}:
        return ".heic"
    return ""


def infer_image_extension(source_url: str, content_type: str, content: bytes = b"") -> str:
    """Choose a local suffix from the response type or source URL."""
    signature_extension = image_signature_extension(content)
    if signature_extension:
        return signature_extension

    normalized_type = (content_type or "").split(";", 1)[0].strip().lower()
    extension = mimetypes.guess_extension(normalized_type) if normalized_type else None
    if extension == ".jpe":
        extension = ".jpg"
    if extension and extension.lower() in CHATGPT_IMAGE_SUFFIXES:
        return extension.lower()

    suffix = Path(urlsplit(source_url).path).suffix.lower()
    return suffix if suffix in CHATGPT_IMAGE_SUFFIXES else ".png"


def looks_like_image(content: bytes) -> bool:
    """Validate common raster-image signatures without decoding the image."""
    return bool(image_signature_extension(content))


def image_payload_is_decodable(content: bytes) -> bool:
    """Return whether an image payload can be fully decoded by Pillow."""
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.verify()
        with Image.open(io.BytesIO(content)) as image:
            image.load()
    except (OSError, SyntaxError, UnidentifiedImageError, ValueError):
        return False
    return True


def _is_chatgpt_thumbnail_source_url(source_url: str) -> bool:
    """Return whether a ChatGPT URL identifies a derived thumbnail encoding."""
    normalized_url = unquote(str(source_url or "")).lower()
    if "#thumbnail" in normalized_url:
        return True
    query = parse_qs(urlsplit(source_url).query)
    return any(
        str(value).strip().lower() == "thumbnail"
        for value in query.get("encoding", [])
    )


def should_cache_chatgpt_candidate(candidate: ChatGPTImageCandidate) -> bool:
    """Return whether a ChatGPT original-image candidate can be cached."""
    return bool(
        candidate.source_url.strip()
        and candidate.file_id.strip()
        and candidate.message_role.strip().lower() != "user"
        and not _is_chatgpt_thumbnail_source_url(candidate.source_url)
    )


def chatgpt_visual_properties(path: Path) -> tuple[str, int, int] | None:
    """Return a stable visual signature and decoded dimensions for one image file."""
    try:
        payload = path.read_bytes()
    except OSError:
        return None
    return analyze_image_payload(payload)


def _visual_signatures_match(left: str, right: str) -> bool:
    """Return whether two dHash values represent the same rendered image."""
    if not left or not right or len(left) != len(right):
        return False
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count() <= CHATGPT_VISUAL_HASH_DISTANCE_LIMIT
    except ValueError:
        return False


def _same_image_aspect_ratio(left_width: int, left_height: int, right_width: int, right_height: int) -> bool:
    """Return whether decoded dimensions have the same aspect ratio within a small tolerance."""
    if min(left_width, left_height, right_width, right_height) <= 0:
        return False
    cross_product = left_width * right_height
    comparison_product = right_width * left_height
    difference = abs(cross_product - comparison_product)
    return difference <= max(cross_product, comparison_product) * CHATGPT_VISUAL_ASPECT_RATIO_TOLERANCE


class ChatGPTImageCatalog:
    """Durable catalog for the dedicated ChatGPT project cache directory."""

    def __init__(self, target_dir: Path, entries: dict[str, ChatGPTCatalogEntry] | None = None) -> None:
        self.target_dir = target_dir
        self.catalog_path = target_dir / CHATGPT_CATALOG_FILENAME
        self.entries_by_file_id = entries or {}
        self.repair_result = ChatGPTCatalogRepairResult()
        self._lock = RLock()
        self._in_flight_file_ids: set[str] = set()
        self._unavailable_file_ids: set[str] = set()

    @classmethod
    def build(cls, target_dir: Path = CHATGPT_TARGET_DIR) -> "ChatGPTImageCatalog":
        """Load Parquet state and migrate one valid legacy JSON catalog when present."""
        catalog_path = target_dir / CHATGPT_CATALOG_FILENAME
        legacy_catalog_path = target_dir / LEGACY_CHATGPT_CATALOG_FILENAME
        entries: dict[str, ChatGPTCatalogEntry] = {}
        rows = read_parquet_rows(catalog_path)
        migrated_legacy = False
        if rows is None and legacy_catalog_path.exists():
            try:
                payload = json.loads(legacy_catalog_path.read_text())
            except (OSError, json.JSONDecodeError):
                payload = None
            raw_entries = payload.get("entries") if isinstance(payload, dict) else None
            if isinstance(raw_entries, dict):
                rows = [
                    {**raw_entry, "file_id": str(raw_entry.get("file_id") or raw_file_id)}
                    for raw_file_id, raw_entry in raw_entries.items()
                    if isinstance(raw_entry, dict)
                ]
                migrated_legacy = True

        for row in rows or []:
            try:
                entry = ChatGPTCatalogEntry(
                    file_id=str(row.get("file_id") or ""),
                    relative_path=str(row.get("relative_path") or ""),
                    content_sha256=str(row.get("content_sha256") or ""),
                    content_bytes=int(row.get("content_bytes") or 0),
                    source_url=str(row.get("source_url") or ""),
                    conversation_url=str(row.get("conversation_url") or ""),
                    alt_text=str(row.get("alt_text") or ""),
                    width=int(row.get("width") or 0),
                    height=int(row.get("height") or 0),
                    first_seen_at=str(row.get("first_seen_at") or ""),
                    last_seen_at=str(row.get("last_seen_at") or ""),
                    prompt_markdown=str(row.get("prompt_markdown") or ""),
                    conversation_title=str(row.get("conversation_title") or ""),
                    created_at=str(row.get("created_at") or ""),
                    visual_signature=str(row.get("visual_signature") or ""),
                )
            except (TypeError, ValueError):
                continue
            if entry.file_id and entry.relative_path:
                entries[entry.file_id] = entry
        catalog = cls(target_dir, entries)
        catalog._normalize_file_extensions()
        catalog.repair_result = catalog.prune_incomplete_entries()
        if migrated_legacy:
            catalog.save()
            retire_legacy_file(legacy_catalog_path)
        elif rows is not None:
            retire_legacy_file(legacy_catalog_path)
        return catalog

    def _normalize_file_extensions(self) -> None:
        """Correct legacy filenames when a server reports the wrong media type."""
        changed = False
        for entry in self.entries_by_file_id.values():
            path = self.target_dir / entry.relative_path
            if not path.is_file():
                continue
            try:
                with path.open("rb") as handle:
                    signature = handle.read(64)
            except OSError:
                continue
            expected_suffix = image_signature_extension(signature)
            if not expected_suffix or path.suffix.lower() == expected_suffix:
                continue
            corrected_path = path.with_suffix(expected_suffix)
            if corrected_path.exists():
                continue
            try:
                path.rename(corrected_path)
            except OSError:
                continue
            entry.relative_path = corrected_path.relative_to(self.target_dir).as_posix()
            changed = True

        if changed:
            self.save()

    def save(self) -> None:
        """Write the current catalog to a typed, atomically replaced Parquet file."""
        with self._lock:
            write_parquet_rows_atomic(
                self.catalog_path,
                (
                    {
                        "schema_version": CHATGPT_CATALOG_SCHEMA_VERSION,
                        "file_id": entry.file_id,
                        "relative_path": entry.relative_path,
                        "content_sha256": entry.content_sha256,
                        "content_bytes": entry.content_bytes,
                        "source_url": entry.source_url,
                        "conversation_url": entry.conversation_url,
                        "alt_text": entry.alt_text,
                        "width": entry.width,
                        "height": entry.height,
                        "first_seen_at": entry.first_seen_at,
                        "last_seen_at": entry.last_seen_at,
                        "prompt_markdown": entry.prompt_markdown,
                        "conversation_title": entry.conversation_title,
                        "created_at": entry.created_at,
                        "visual_signature": entry.visual_signature,
                    }
                    for entry in sorted(self.entries_by_file_id.values(), key=lambda item: item.file_id)
                ),
                CHATGPT_CATALOG_SCHEMA,
            )

    def complete_entry(self, file_id: str) -> ChatGPTCatalogEntry | None:
        """Return a catalog entry only when its local file is still valid."""
        with self._lock:
            entry = self.entries_by_file_id.get(file_id)
            if entry is None:
                return None
            path = self.target_dir / entry.relative_path
            if not path.exists() or not path.is_file() or path.stat().st_size <= 0:
                return None
            if entry.content_bytes and path.stat().st_size != entry.content_bytes:
                return None
            try:
                with path.open("rb") as handle:
                    signature = handle.read(64)
                if not looks_like_image(signature):
                    return None
            except OSError:
                return None
            # Older Safari runs only checked the file signature and could leave
            # a PNG with a damaged interior chunk in the catalog. Re-decode
            # entries without a persisted visual signature so the next sync
            # prunes and downloads those files again.
            if not entry.visual_signature and chatgpt_visual_properties(path) is None:
                return None
            return entry

    def claim_download(self, file_id: str) -> bool:
        """Claim one file ID so parallel workers do not download it twice."""
        with self._lock:
            if (
                file_id in self._unavailable_file_ids
                or file_id in self._in_flight_file_ids
                or self.complete_entry(file_id) is not None
            ):
                return False
            self._in_flight_file_ids.add(file_id)
            return True

    def release_download(self, file_id: str) -> None:
        """Release a failed or interrupted parallel download claim."""
        with self._lock:
            self._in_flight_file_ids.discard(file_id)

    def mark_unavailable(self, file_id: str) -> None:
        """Remember a missing historical file only for the current sync run."""
        with self._lock:
            self._in_flight_file_ids.discard(file_id)
            self._unavailable_file_ids.add(file_id)

    def update_metadata(self, candidate: ChatGPTImageCandidate) -> None:
        """Refresh conversation metadata when a known image is seen again."""
        self.update_metadata_batch((candidate,))

    def merge_known_metadata(
        self,
        candidates: Iterable[ChatGPTImageCandidate],
    ) -> list[ChatGPTImageCandidate]:
        """Reuse cached metadata unless one session needs authoritative prompt backfill."""
        with self._lock:
            materialized = tuple(candidates)
            sessions_needing_prompt_backfill = {
                candidate.conversation_url
                for candidate in materialized
                if candidate.conversation_url and not candidate.prompt_markdown
            }
            merged_candidates: list[ChatGPTImageCandidate] = []
            for candidate in materialized:
                existing = self.entries_by_file_id.get(candidate.file_id)
                if existing is None:
                    merged_candidates.append(candidate)
                    continue
                reuse_cached_prompt = (
                    candidate.conversation_url not in sessions_needing_prompt_backfill
                )
                merged_candidates.append(
                    replace(
                        candidate,
                        alt_text=candidate.alt_text or existing.alt_text,
                        prompt_markdown=(
                            candidate.prompt_markdown
                            or (existing.prompt_markdown if reuse_cached_prompt else "")
                        ),
                        conversation_title=(
                            candidate.conversation_title or existing.conversation_title
                        ),
                        created_at=candidate.created_at or existing.created_at,
                    )
                )
            return merged_candidates

    def update_metadata_batch(self, candidates: Iterable[ChatGPTImageCandidate]) -> int:
        """Refresh known image metadata and persist the batch with one catalog write."""
        with self._lock:
            updated_count = 0
            for candidate in candidates:
                existing = self.entries_by_file_id.get(candidate.file_id)
                if existing is None:
                    continue
                changed = False
                if candidate.conversation_title and candidate.conversation_title != existing.conversation_title:
                    existing.conversation_title = candidate.conversation_title
                    changed = True
                if candidate.conversation_url and candidate.conversation_url != existing.conversation_url:
                    existing.conversation_url = candidate.conversation_url
                    changed = True
                if candidate.created_at and candidate.created_at != existing.created_at:
                    existing.created_at = candidate.created_at
                    changed = True
                if (
                    candidate.prompt_metadata_authoritative
                    and candidate.prompt_markdown != existing.prompt_markdown
                ) or (
                    not candidate.prompt_metadata_authoritative
                    and candidate.prompt_markdown
                    and candidate.prompt_markdown != existing.prompt_markdown
                ):
                    existing.prompt_markdown = candidate.prompt_markdown
                    changed = True
                if changed:
                    updated_count += 1
            if updated_count:
                self.save()
            return updated_count

    def summarize(self) -> int:
        """Return the number of cataloged images whose files still exist."""
        with self._lock:
            return sum(1 for file_id in self.entries_by_file_id if self.complete_entry(file_id) is not None)

    def prune_incomplete_entries(self) -> ChatGPTCatalogRepairResult:
        """Remove invalid entries so only original ChatGPT images remain cached."""
        with self._lock:
            removed_file_ids: list[str] = []
            removed_local_files = 0
            reclaimed_bytes = 0
            for file_id, entry in list(self.entries_by_file_id.items()):
                if (
                    self.complete_entry(file_id) is not None
                    and not _is_chatgpt_thumbnail_source_url(entry.source_url)
                ):
                    continue
                path = self.target_dir / entry.relative_path
                if path.exists() and path.is_file():
                    try:
                        reclaimed_bytes += path.stat().st_size
                    except OSError:
                        pass
                    try:
                        path.unlink()
                        removed_local_files += 1
                    except OSError:
                        continue
                self.entries_by_file_id.pop(file_id, None)
                removed_file_ids.append(file_id)

            if removed_file_ids:
                self.save()
            return ChatGPTCatalogRepairResult(
                removed_file_ids=tuple(sorted(removed_file_ids)),
                removed_local_files=removed_local_files,
                reclaimed_bytes=reclaimed_bytes,
            )

    def deduplicate_visual_duplicates(
        self,
        *,
        dry_run: bool = False,
        analysis_workers: int | None = None,
        metrics: PerformanceMetrics | None = None,
    ) -> ChatGPTDuplicateCleanupResult:
        """Remove lower-quality copies of the same image from one ChatGPT conversation."""
        with self._lock:
            result, changed = self._deduplicate_visual_duplicates_unlocked(
                dry_run=dry_run,
                analysis_workers=analysis_workers,
                metrics=metrics,
            )
            if changed and not dry_run:
                self.save()
            return result

    def register_download(
        self,
        candidate: ChatGPTImageCandidate,
        relative_path: str,
        content_sha256: str,
        content_bytes: int,
        seen_at: str,
    ) -> bool:
        """Register one successfully downloaded image and persist immediately."""
        with self._lock:
            existing = self.entries_by_file_id.get(candidate.file_id)
            visual_properties = chatgpt_visual_properties(self.target_dir / relative_path)
            width = visual_properties[1] if visual_properties else candidate.width
            height = visual_properties[2] if visual_properties else candidate.height
            self.entries_by_file_id[candidate.file_id] = ChatGPTCatalogEntry(
                file_id=candidate.file_id,
                relative_path=relative_path,
                content_sha256=content_sha256,
                content_bytes=content_bytes,
                source_url=candidate.source_url,
                conversation_url=candidate.conversation_url,
                alt_text=candidate.alt_text,
                width=width,
                height=height,
                first_seen_at=existing.first_seen_at if existing else seen_at,
                last_seen_at=seen_at,
                prompt_markdown=(
                    candidate.prompt_markdown
                    if candidate.prompt_metadata_authoritative
                    else candidate.prompt_markdown or (existing.prompt_markdown if existing else "")
                ),
                conversation_title=candidate.conversation_title
                or (existing.conversation_title if existing else ""),
                created_at=candidate.created_at or (existing.created_at if existing else ""),
                visual_signature=visual_properties[0] if visual_properties else "",
            )
            self._in_flight_file_ids.discard(candidate.file_id)
            self._deduplicate_incoming_entry_unlocked(candidate.file_id)
            self.save()
            return candidate.file_id in self.entries_by_file_id

    def _deduplicate_incoming_entry_unlocked(self, file_id: str) -> None:
        """Discard an incoming lower-quality visual duplicate without rehashing the full catalog."""
        current = self.entries_by_file_id.get(file_id)
        if current is None:
            return
        current_record = self._entry_visual_record_unlocked(file_id, current)
        if current_record is None:
            return
        conversation_key = current.conversation_url.strip().rstrip("/")
        if not conversation_key:
            return

        for other_file_id, other in list(self.entries_by_file_id.items()):
            if other_file_id == file_id or other.conversation_url.strip().rstrip("/") != conversation_key:
                continue
            other_record = self._entry_visual_record_unlocked(other_file_id, other)
            if other_record is None or not self._entries_are_visual_duplicates(current_record, other_record):
                continue
            winner = max((current_record, other_record), key=self._entry_quality_key)
            loser_file_id, loser_entry, _width, _height = other_record if winner[0] == file_id else current_record
            try:
                (self.target_dir / loser_entry.relative_path).unlink(missing_ok=True)
            except OSError:
                continue
            self.entries_by_file_id.pop(loser_file_id, None)
            if loser_file_id == file_id:
                return

    def _entry_visual_record_unlocked(
        self,
        file_id: str,
        entry: ChatGPTCatalogEntry,
        visual_properties: tuple[str, int, int] | None | object = None,
    ) -> tuple[str, ChatGPTCatalogEntry, int, int] | None:
        """Return one entry with its persisted visual signature hydrated when necessary."""
        if entry.visual_signature and entry.width > 0 and entry.height > 0:
            return file_id, entry, entry.width, entry.height
        if visual_properties is None:
            visual_properties = chatgpt_visual_properties(self.target_dir / entry.relative_path)
        if visual_properties is None:
            return None
        signature, width, height = visual_properties
        entry.visual_signature = signature
        entry.width = width
        entry.height = height
        return file_id, entry, width, height

    def _deduplicate_visual_duplicates_unlocked(
        self,
        *,
        dry_run: bool = False,
        analysis_workers: int | None = None,
        metrics: PerformanceMetrics | None = None,
    ) -> tuple[ChatGPTDuplicateCleanupResult, bool]:
        """Select the best local copy in every same-conversation duplicate group."""
        entries_by_conversation: dict[str, list[tuple[str, ChatGPTCatalogEntry, int, int]]] = {}
        changed = False
        paths_needing_analysis = {
            file_id: self.target_dir / entry.relative_path
            for file_id, entry in self.entries_by_file_id.items()
            if not (entry.visual_signature and entry.width > 0 and entry.height > 0)
        }
        visual_properties_by_file_id = analyze_image_paths(
            paths_needing_analysis,
            workers=analysis_workers,
            metrics=metrics,
        )
        for file_id, entry in self.entries_by_file_id.items():
            conversation_key = entry.conversation_url.strip().rstrip("/")
            if not conversation_key:
                continue
            previous_visual_signature = entry.visual_signature
            previous_width = entry.width
            previous_height = entry.height
            if file_id in visual_properties_by_file_id:
                visual_record = self._entry_visual_record_unlocked(
                    file_id,
                    entry,
                    visual_properties_by_file_id[file_id],
                )
            else:
                visual_record = self._entry_visual_record_unlocked(file_id, entry)
            if visual_record is None:
                continue
            _record_file_id, _record_entry, width, height = visual_record
            if (
                entry.visual_signature != previous_visual_signature
                or entry.width != previous_width
                or entry.height != previous_height
            ):
                changed = True
            entries_by_conversation.setdefault(conversation_key, []).append((file_id, entry, width, height))

        removals: list[tuple[str, ChatGPTCatalogEntry]] = []
        for entries in entries_by_conversation.values():
            retained: list[tuple[str, ChatGPTCatalogEntry, int, int]] = []
            for current in sorted(entries, key=self._entry_quality_key, reverse=True):
                if any(self._entries_are_visual_duplicates(current, kept) for kept in retained):
                    removals.append((current[0], current[1]))
                else:
                    retained.append(current)

        reclaimed_bytes = 0
        removed_file_ids: list[str] = []
        for file_id, entry in removals:
            path = self.target_dir / entry.relative_path
            try:
                reclaimed_bytes += path.stat().st_size
            except OSError:
                pass
            removed_file_ids.append(file_id)
            if dry_run:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
            self.entries_by_file_id.pop(file_id, None)
            changed = True

        return (
            ChatGPTDuplicateCleanupResult(
                removed_file_ids=tuple(sorted(removed_file_ids)),
                reclaimed_bytes=reclaimed_bytes,
            ),
            changed,
        )

    @staticmethod
    def _entry_quality_key(entry: tuple[str, ChatGPTCatalogEntry, int, int]) -> tuple[int, int, str, str]:
        """Order visual duplicates so the highest-quality local image is retained."""
        file_id, catalog_entry, width, height = entry
        return width * height, catalog_entry.content_bytes, catalog_entry.first_seen_at, file_id

    @staticmethod
    def _entries_are_visual_duplicates(
        left: tuple[str, ChatGPTCatalogEntry, int, int],
        right: tuple[str, ChatGPTCatalogEntry, int, int],
    ) -> bool:
        """Return whether two entries are identical renderings from one conversation."""
        _left_id, left_entry, left_width, left_height = left
        _right_id, right_entry, right_width, right_height = right
        if left_entry.content_sha256 and left_entry.content_sha256 == right_entry.content_sha256:
            return True
        return _same_image_aspect_ratio(left_width, left_height, right_width, right_height) and _visual_signatures_match(
            left_entry.visual_signature,
            right_entry.visual_signature,
        )


def build_chatgpt_initial_snapshot(
    version: str,
    target_dir: Path | None = None,
    project_name: str = DEFAULT_CHATGPT_PROJECT_NAME,
) -> TaskSnapshot:
    """Hydrate the ChatGPT idle snapshot from the local image catalog."""
    resolved_target_dir = target_dir or chatgpt_target_dir(project_name)
    snapshot = TaskSnapshot(version=version)
    cached_count = ChatGPTImageCatalog.build(resolved_target_dir).summarize()
    snapshot.account_name = project_name
    snapshot.output_dir = str(resolved_target_dir)
    snapshot.discovered_images = cached_count
    snapshot.downloaded_posts = cached_count
    snapshot.downloaded_tweets = cached_count
    snapshot.downloaded_images = cached_count
    if cached_count:
        snapshot.message = f"Ready. Found existing ChatGPT cache: {cached_count:,} images."
    return snapshot


def build_chatgpt_text_snapshot(version: str, local_store_root: Path | str = LOCAL_STORE_ROOT) -> TaskSnapshot:
    """Hydrate Text mode without opening or repairing the media catalog."""
    counts = chatgpt_history_counts(local_store_root)
    return TaskSnapshot(
        version=version,
        account_name="ChatGPT",
        output_dir=str(chatgpt_history_path(local_store_root).parent),
        discovered_tweets=counts["cached_sessions"],
        downloaded_posts=counts["cached_sessions"],
        downloaded_tweets=counts["cached_messages"],
        progress_unit="sessions",
        message=(
            f"Ready. Found existing ChatGPT text cache: {counts['cached_sessions']:,} sessions, "
            f"{counts['cached_messages']:,} messages."
        ),
    )


def reset_chatgpt_state(
    target_dir: Path | None = None,
    project_name: str = DEFAULT_CHATGPT_PROJECT_NAME,
) -> ChatGPTResetResult:
    """Remove only the dedicated ChatGPT cache and its resumable state."""
    resolved_target_dir = target_dir or chatgpt_target_dir(project_name)
    result = ChatGPTResetResult()
    catalog_path = resolved_target_dir / CHATGPT_CATALOG_FILENAME
    catalog = ChatGPTImageCatalog.build(resolved_target_dir)
    for entry in catalog.entries_by_file_id.values():
        media_path = resolved_target_dir / entry.relative_path
        if media_path.exists() and media_path.is_file():
            media_path.unlink()
            result.removed_media_files += 1

    partial_dir = resolved_target_dir / CHATGPT_PARTIAL_DIRNAME
    if partial_dir.exists():
        for partial_path in partial_dir.iterdir():
            if partial_path.is_file():
                partial_path.unlink()
                result.removed_partial_files += 1
        partial_dir.rmdir()

    for state_path in (
        catalog_path,
        resolved_target_dir / LEGACY_CHATGPT_CATALOG_FILENAME,
        chatgpt_history_path_for_target(resolved_target_dir),
    ):
        if state_path.exists() and state_path.is_file():
            state_path.unlink()
            result.removed_state_files += 1
    return result


def _project_conversation_prefix(project_url: str) -> str:
    """Build the URL prefix used by ChatGPT project conversation links."""
    parsed = urlsplit(project_url)
    project_path = parsed.path.rstrip("/")
    if project_path.endswith("/project"):
        project_path = project_path[: -len("/project")]
    return f"{parsed.scheme}://{parsed.netloc}{project_path}/c/"


def is_chatgpt_conversation_url(url: str) -> bool:
    """Return whether a ChatGPT URL points to one conversation session."""
    parsed = urlsplit(url)
    path_parts = [part for part in parsed.path.split("/") if part]
    return (
        parsed.scheme.lower() == "https"
        and parsed.netloc.lower() == "chatgpt.com"
        and len(path_parts) >= 2
        and path_parts[-2] == "c"
        and bool(path_parts[-1])
    )


def chatgpt_conversation_id(conversation_url: str) -> str:
    """Return the stable conversation ID from one ChatGPT conversation URL."""
    if not is_chatgpt_conversation_url(conversation_url):
        return ""
    parsed = urlsplit(conversation_url)
    path_parts = [part for part in parsed.path.split("/") if part]
    return path_parts[-1]


def _chatgpt_conversation_api_url(conversation_url: str) -> str:
    """Build the authenticated API endpoint for one ChatGPT conversation."""
    parsed = urlsplit(conversation_url)
    conversation_id = chatgpt_conversation_id(conversation_url)
    if not conversation_id or parsed.scheme.lower() != "https" or parsed.netloc.lower() != "chatgpt.com":
        return ""
    return f"{parsed.scheme}://{parsed.netloc}{CHATGPT_CONVERSATION_API_PATH}{conversation_id}"


def _chatgpt_file_download_url(file_id: str, conversation_url: str = "") -> str:
    """Build the authenticated metadata endpoint for one ChatGPT file asset."""
    source_url = f"https://chatgpt.com{CHATGPT_FILE_DOWNLOAD_PATH}{file_id}"
    conversation_id = chatgpt_conversation_id(conversation_url)
    if not conversation_id:
        return source_url
    return f"{source_url}?{urlencode({
        'conversation_id': conversation_id,
        'download_intent': 'false',
        'inline': 'false',
    })}"


def _is_chatgpt_file_download_url(source_url: str) -> bool:
    """Return whether a source URL needs authenticated download URL resolution."""
    parsed = urlsplit(source_url)
    return (
        parsed.scheme.lower() == "https"
        and parsed.netloc.lower() == "chatgpt.com"
        and parsed.path.startswith(CHATGPT_FILE_DOWNLOAD_PATH)
    )


def _extract_chatgpt_file_id_from_asset_pointer(value: object) -> str:
    """Extract a file ID from one ChatGPT file-service asset pointer."""
    match = CHATGPT_FILE_ID_PATTERN.search(str(value or ""))
    return match.group(0) if match else ""


def _chatgpt_candidate_request_headers(response) -> dict[str, str]:
    """Keep the page's transient ChatGPT authorization headers in memory only."""
    try:
        request_headers = response.request.headers
    except (AttributeError, PlaywrightError):
        return {}
    if not isinstance(request_headers, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in request_headers.items()
        if str(key).lower() in CHATGPT_DOWNLOAD_AUTH_HEADER_NAMES and str(value)
    }


def _chatgpt_project_id(project_url: str) -> str:
    """Extract the GPT project identifier from one ChatGPT project URL."""
    path_parts = [part for part in urlsplit(project_url).path.split("/") if part]
    for index, part in enumerate(path_parts[:-1]):
        if part == "g":
            project_segment = path_parts[index + 1]
            match = CHATGPT_GIZMO_PROJECT_ID_PATTERN.match(project_segment)
            return match.group(1) if match else project_segment
    return ""


def _chatgpt_api_request_headers(request_headers: dict[str, str], referer: str) -> dict[str, str]:
    """Build one in-memory authenticated JSON request header set."""
    headers = {
        str(key): str(value)
        for key, value in request_headers.items()
        if str(key).lower() in CHATGPT_DOWNLOAD_AUTH_HEADER_NAMES and str(value)
    }
    headers.update({"Accept": "application/json", "Referer": referer})
    return headers


def _load_chatgpt_session_request_headers(context, referer: str) -> dict[str, str]:
    """Read a transient ChatGPT access token through the signed-in browser page."""
    cached_headers = getattr(context, "_chatgpt_request_headers", None)
    if isinstance(cached_headers, dict) and cached_headers.get("authorization"):
        return dict(cached_headers)
    payload = _get_chatgpt_api_json(
        context,
        CHATGPT_AUTH_SESSION_URL,
        {"Accept": "application/json", "Referer": referer},
    )
    access_token = str(payload.get("accessToken") or "").strip() if isinstance(payload, dict) else ""
    if not access_token:
        raise RuntimeError("ChatGPT browser session is not signed in or did not expose an access token.")
    request_headers = {"authorization": f"Bearer {access_token}"}
    if isinstance(context, SafariContext):
        context._chatgpt_request_headers = dict(request_headers)
    return request_headers


def _load_chatgpt_session_request_headers_via_page(page, referer: str) -> dict[str, str]:
    """Read a transient ChatGPT access token through the browser network stack."""
    page_context = getattr(page, "context", None)
    if isinstance(page_context, SafariContext):
        return _load_chatgpt_session_request_headers(page_context, referer)
    payload = _get_chatgpt_api_json_via_page(
        page,
        CHATGPT_AUTH_SESSION_URL,
        {"Accept": "application/json", "Referer": referer},
    )
    access_token = str(payload.get("accessToken") or "").strip()
    if not access_token:
        raise RuntimeError(
            "ChatGPT browser session is not signed in or did not expose an access token."
        )
    return {"authorization": f"Bearer {access_token}"}


def _get_chatgpt_api_json(
    context,
    url: str,
    headers: dict[str, str],
    request_get: Callable[..., object] | None = None,
) -> dict[str, object]:
    """Fetch authenticated ChatGPT JSON with bounded transient-error retries."""
    last_status = 0
    fetch = request_get or context.request.get
    rate_limit_attempts = 0
    attempt_index = 0
    attempt_limit = CHATGPT_API_RETRY_LIMIT
    while attempt_index < attempt_limit:
        try:
            response = fetch(
                url,
                timeout=CHATGPT_IMAGE_TIMEOUT_MS,
                headers=headers,
            )
        except (PlaywrightError, RuntimeError) as exc:
            if attempt_index + 1 >= CHATGPT_API_RETRY_LIMIT:
                raise RuntimeError(
                    "ChatGPT API request failed after transient browser connection retries."
                ) from exc
            time.sleep(CHATGPT_API_RETRY_DELAY_SECONDS * (attempt_index + 1))
            attempt_index += 1
            continue
        last_status = int(response.status)
        if response.ok:
            try:
                payload = json.loads(response.text())
            except (AttributeError, TypeError, json.JSONDecodeError) as exc:
                if attempt_index + 1 >= CHATGPT_API_RETRY_LIMIT:
                    raise RuntimeError("ChatGPT API returned invalid JSON after retries.") from exc
                time.sleep(CHATGPT_API_RETRY_DELAY_SECONDS * (attempt_index + 1))
                attempt_index += 1
                continue
            if isinstance(payload, dict):
                return payload
            raise RuntimeError("ChatGPT API returned an unexpected JSON payload.")
        if last_status == 429:
            rate_limit_attempts += 1
            if rate_limit_attempts >= CHATGPT_API_RATE_LIMIT_RETRY_LIMIT:
                raise ChatGPTRateLimitError(
                    f"ChatGPT API request returned HTTP {last_status}."
                )
            attempt_limit = max(attempt_limit, CHATGPT_API_RATE_LIMIT_RETRY_LIMIT)
            retry_after = _chatgpt_retry_after_seconds(response)
            delay_seconds = retry_after or min(
                CHATGPT_API_RATE_LIMIT_MAX_DELAY_SECONDS,
                CHATGPT_API_RETRY_DELAY_SECONDS * (2 ** (rate_limit_attempts - 1)),
            )
            time.sleep(delay_seconds)
            attempt_index += 1
            continue
        retryable_status = last_status in {408, 429} or 500 <= last_status < 600
        if not retryable_status or attempt_index + 1 >= CHATGPT_API_RETRY_LIMIT:
            break
        time.sleep(CHATGPT_API_RETRY_DELAY_SECONDS * (attempt_index + 1))
        attempt_index += 1
    raise RuntimeError(f"ChatGPT API request returned HTTP {last_status}.")


def _chatgpt_retry_after_seconds(response: object) -> float | None:
    """Parse a bounded Retry-After response header when ChatGPT sends one."""
    try:
        headers = response.headers
    except AttributeError:
        return None
    if not isinstance(headers, dict):
        return None
    raw_value = next(
        (value for key, value in headers.items() if str(key).lower() == "retry-after"),
        None,
    )
    try:
        delay_seconds = float(str(raw_value).strip())
    except (TypeError, ValueError):
        return None
    if delay_seconds < 0:
        return None
    return min(delay_seconds, CHATGPT_API_RATE_LIMIT_MAX_DELAY_SECONDS)


def _get_chatgpt_api_json_via_page(page, url: str, headers: dict[str, str]) -> dict[str, object]:
    """Fetch authenticated ChatGPT JSON inside the authorized browser page."""
    page_context = getattr(page, "context", None)
    if isinstance(page_context, SafariContext):
        return _get_chatgpt_api_json(
            page_context,
            url,
            _chatgpt_api_request_headers(headers, page.url),
            request_get=lambda request_url, **kwargs: page_context.request.get_from_page(
                page,
                request_url,
                **kwargs,
            ),
        )

    browser_headers = {
        str(key): str(value)
        for key, value in headers.items()
        if str(key).lower() in CHATGPT_DOWNLOAD_AUTH_HEADER_NAMES and str(value)
    }
    browser_headers["Accept"] = "application/json"
    try:
        result = page.evaluate(
            """async ({ url, headers }) => {
                const response = await fetch(url, {
                    credentials: 'include',
                    headers,
                });
                let payload = null;
                try {
                    payload = await response.json();
                } catch (_error) {
                    payload = null;
                }
                return { status: response.status, payload };
            }""",
            {"url": url, "headers": browser_headers},
        )
    except PlaywrightError as exc:
        raise RuntimeError("ChatGPT browser-page API request failed.") from exc

    status = int(result.get("status") or 0) if isinstance(result, dict) else 0
    payload = result.get("payload") if isinstance(result, dict) else None
    if 200 <= status < 300 and isinstance(payload, dict):
        return payload
    if 200 <= status < 300:
        raise RuntimeError("ChatGPT browser-page API returned an unexpected JSON payload.")
    raise RuntimeError(f"ChatGPT browser-page API request returned HTTP {status}.")


def _extract_chatgpt_conversation_image_payloads(
    payload: object,
    *,
    include_sediment: bool = False,
) -> list[dict[str, object]]:
    """Extract every branch image asset from a complete ChatGPT conversation mapping."""
    if not isinstance(payload, dict):
        return []
    mapping = payload.get("mapping")
    if not isinstance(mapping, dict):
        return []

    results_by_file_id: dict[str, dict[str, object]] = {}

    def message_role(node: object) -> str:
        if not isinstance(node, dict):
            return ""
        message = node.get("message")
        if not isinstance(message, dict):
            return ""
        author = message.get("author")
        return str(author.get("role") or "").strip() if isinstance(author, dict) else ""

    def message_markdown(node: object) -> str:
        if not isinstance(node, dict):
            return ""
        message = node.get("message")
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if not isinstance(content, dict):
            return ""

        text_parts: list[str] = []
        direct_text = content.get("text")
        if isinstance(direct_text, str) and direct_text.strip():
            text_parts.append(direct_text.strip())

        raw_parts = content.get("parts")
        if not isinstance(raw_parts, list):
            raw_parts = [raw_parts] if raw_parts is not None else []
        for part in raw_parts:
            if isinstance(part, str):
                if part.strip():
                    text_parts.append(part.strip())
                continue
            if not isinstance(part, dict):
                continue
            content_type = str(part.get("content_type") or part.get("type") or "").lower()
            part_text = part.get("text")
            if (
                isinstance(part_text, str)
                and part_text.strip()
                and not content_type.startswith("image")
            ):
                text_parts.append(part_text.strip())
        return "\n\n".join(dict.fromkeys(text_parts))

    def nearest_user_prompt(start_node_id: object) -> str:
        visited: set[str] = set()
        node_id = str(start_node_id or "").strip()
        while node_id and node_id not in visited:
            visited.add(node_id)
            node = mapping.get(node_id)
            if not isinstance(node, dict):
                break
            if message_role(node) == "user":
                prompt = message_markdown(node)
                if prompt:
                    return prompt
            node_id = str(node.get("parent") or "").strip()
        return ""

    def as_integer(value: object) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    def walk_asset(value: object, author_role: str, prompt_markdown: str) -> None:
        if isinstance(value, list):
            for child in value:
                walk_asset(child, author_role, prompt_markdown)
            return
        if not isinstance(value, dict):
            return

        pointer = value.get("asset_pointer") or value.get("image_asset_pointer")
        pointer_text = str(pointer or "").strip()
        file_id = _extract_chatgpt_file_id_from_asset_pointer(pointer_text)
        content_type = str(
            value.get("content_type")
            or value.get("contentType")
            or value.get("mime_type")
            or value.get("mimeType")
            or ""
        ).strip().lower()
        file_name = str(value.get("file_name") or value.get("filename") or value.get("name") or "")
        is_image = (
            content_type.startswith("image/")
            or content_type == "image_asset_pointer"
            or bool(value.get("image_asset_pointer"))
            or Path(file_name).suffix.lower() in CHATGPT_IMAGE_SUFFIXES
        )
        if file_id and is_image and (
            include_sediment or not pointer_text.lower().startswith("sediment://")
        ):
            candidate = {
                "fileId": file_id,
                "altText": str(value.get("alt_text") or value.get("alt") or file_name).strip(),
                "width": as_integer(value.get("width")),
                "height": as_integer(value.get("height")),
                "messageRole": author_role,
                "promptMarkdown": prompt_markdown,
            }
            previous = results_by_file_id.get(file_id)
            previous_is_upload = previous is not None and previous.get("messageRole") == "user"
            if author_role == "user" or previous is None or (
                not previous_is_upload
                and int(candidate["width"]) * int(candidate["height"])
                >= int(previous.get("width") or 0) * int(previous.get("height") or 0)
            ):
                results_by_file_id[file_id] = candidate

        for child in value.values():
            walk_asset(child, author_role, prompt_markdown)

    for node_id in mapping:
        node = mapping.get(node_id)
        if not isinstance(node, dict):
            continue
        message = node.get("message")
        if not isinstance(message, dict):
            continue
        author = message.get("author")
        author_role = str(author.get("role") or "") if isinstance(author, dict) else ""
        walk_asset(message.get("content"), author_role, nearest_user_prompt(node_id))

    return list(results_by_file_id.values())


def _chatgpt_prompt_metadata_worker(
    page,
    indexed_conversation_urls: list[tuple[int, str]],
    request_headers: dict[str, str],
    should_stop,
    rate_limited: Event,
    result_queue: BoundedWorkQueue[ChatGPTPromptMetadataWorkResult | None],
) -> None:
    """Fetch one partition of conversation mappings through an isolated Safari page."""
    try:
        for conversation_index, conversation_url in indexed_conversation_urls:
            if should_stop() or rate_limited.is_set():
                break
            api_url = _chatgpt_conversation_api_url(conversation_url)
            if not api_url:
                continue
            try:
                payload = _get_chatgpt_api_json_via_page(page, api_url, request_headers)
            except RuntimeError as exc:
                error = str(exc)
                if "HTTP 429" in error:
                    rate_limited.set()
                _put_chatgpt_result(
                    result_queue,
                    ChatGPTPromptMetadataWorkResult(
                        conversation_index=conversation_index,
                        conversation_url=conversation_url,
                        error=error,
                    ),
                    should_stop,
                )
            else:
                _put_chatgpt_result(
                    result_queue,
                    ChatGPTPromptMetadataWorkResult(
                        conversation_index=conversation_index,
                        conversation_url=conversation_url,
                        payload=payload,
                    ),
                    should_stop,
                )
    finally:
        _put_chatgpt_result(result_queue, None, should_stop)


def _iter_parallel_safari_prompt_metadata_results(
    browser_page,
    project_url: str,
    conversation_urls: list[str],
    request_headers: dict[str, str],
    should_stop,
    worker_count: int,
) -> Iterator[ChatGPTPromptMetadataWorkResult]:
    """Yield bounded concurrent mapping reads from separate background Safari pages."""
    context = getattr(browser_page, "context", None)
    if not isinstance(context, SafariContext):
        raise RuntimeError("Parallel prompt metadata requires a Safari browser context.")

    resolved_worker_count = max(1, min(int(worker_count), len(conversation_urls)))
    pages = [browser_page]
    extra_pages = []
    try:
        for _ in range(1, resolved_worker_count):
            page = context.new_page()
            open_chatgpt_page(page, project_url, settle_ms=0)
            pages.append(page)
            extra_pages.append(page)

        indexed_urls = list(enumerate(conversation_urls, start=1))
        assignments = [
            indexed_urls[worker_index::resolved_worker_count]
            for worker_index in range(resolved_worker_count)
        ]
        result_queue: BoundedWorkQueue[ChatGPTPromptMetadataWorkResult | None] = BoundedWorkQueue(
            CHATGPT_PROMPT_METADATA_QUEUE_SIZE
        )
        rate_limited = Event()
        workers = [
            Thread(
                target=_chatgpt_prompt_metadata_worker,
                args=(
                    page,
                    assignment,
                    request_headers,
                    should_stop,
                    rate_limited,
                    result_queue,
                ),
                daemon=True,
                name=f"chatgpt-prompt-worker-{worker_index + 1}",
            )
            for worker_index, (page, assignment) in enumerate(zip(pages, assignments, strict=True))
        ]
        for worker in workers:
            worker.start()

        finished_workers = 0
        try:
            while finished_workers < len(workers):
                try:
                    result = result_queue.get(timeout=0.2)
                except Empty:
                    if not any(worker.is_alive() for worker in workers) and result_queue.empty():
                        break
                    continue
                if result is None:
                    finished_workers += 1
                    continue
                yield result
        finally:
            rate_limited.set()
            join_deadline = time.monotonic() + CHATGPT_PROMPT_METADATA_WORKER_JOIN_TIMEOUT_SECONDS
            for worker in workers:
                worker.join(timeout=max(0.0, join_deadline - time.monotonic()))
    finally:
        for page in reversed(extra_pages):
            page.close()


def enrich_chatgpt_project_index_prompts(
    context,
    project_url: str,
    candidates: list[ChatGPTImageCandidate],
    request_headers: dict[str, str],
    state: TaskState,
    should_stop,
    persist_batch: Callable[[Iterable[ChatGPTImageCandidate]], int] | None = None,
    browser_page=None,
    worker_count: int = 1,
    skip_complete_conversations: bool = False,
) -> list[ChatGPTImageCandidate]:
    """Reconcile project-index prompts and titles with complete conversation mappings."""
    if not candidates or not request_headers:
        return candidates

    candidates_by_file_id = {candidate.file_id: candidate for candidate in candidates}
    candidate_file_ids_by_conversation: dict[str, list[str]] = {}
    for candidate in candidates:
        if candidate.conversation_url:
            candidate_file_ids_by_conversation.setdefault(candidate.conversation_url, []).append(
                candidate.file_id
            )
    conversation_urls = list(
        dict.fromkeys(
            candidate.conversation_url
            for candidate in candidates
            if candidate.conversation_url
        )
    )
    if skip_complete_conversations:
        all_conversation_count = len(conversation_urls)
        conversation_urls = [
            conversation_url
            for conversation_url in conversation_urls
            if any(
                not candidates_by_file_id[file_id].prompt_markdown
                or not candidates_by_file_id[file_id].conversation_title
                for file_id in candidate_file_ids_by_conversation.get(conversation_url, ())
            )
        ]
        skipped_conversation_count = all_conversation_count - len(conversation_urls)
        if skipped_conversation_count:
            state.append_event(
                f"Reused complete cached prompt metadata for {skipped_conversation_count:,} "
                "ChatGPT sessions."
            )
    if not conversation_urls:
        return candidates

    prompt_count = sum(bool(candidate.prompt_markdown) for candidate in candidates)
    failed_conversations = 0
    prompt_metadata_deferred = False
    persisted_update_count = 0
    pending_updates: dict[str, ChatGPTImageCandidate] = {}
    api_headers = _chatgpt_api_request_headers(request_headers, project_url)

    def persist_pending_updates() -> None:
        nonlocal persisted_update_count
        if persist_batch is None or not pending_updates:
            return
        persisted_update_count += int(persist_batch(tuple(pending_updates.values())))
        pending_updates.clear()

    def apply_payload(conversation_url: str, payload: dict[str, object]) -> None:
        nonlocal prompt_count
        conversation_title = str(payload.get("title") or "").strip()
        if conversation_title:
            for file_id in candidate_file_ids_by_conversation.get(conversation_url, ()):
                current = candidates_by_file_id[file_id]
                if current.conversation_title:
                    continue
                replacement = replace(current, conversation_title=conversation_title)
                candidates_by_file_id[file_id] = replacement
                pending_updates[file_id] = replacement
        for raw_candidate in _extract_chatgpt_conversation_image_payloads(
            payload,
            include_sediment=True,
        ):
            file_id = str(raw_candidate.get("fileId") or "").strip()
            current = candidates_by_file_id.get(file_id)
            if current is None:
                continue
            prompt_markdown = str(raw_candidate.get("promptMarkdown") or "").strip()
            replacement = replace(
                current,
                alt_text=current.alt_text or str(raw_candidate.get("altText") or "").strip(),
                prompt_markdown=prompt_markdown,
                prompt_metadata_authoritative=True,
                message_role=current.message_role or str(raw_candidate.get("messageRole") or "").strip(),
                conversation_title=current.conversation_title or conversation_title,
            )
            if not current.prompt_markdown and replacement.prompt_markdown:
                prompt_count += 1
            candidates_by_file_id[file_id] = replacement
            pending_updates[file_id] = replacement

    completed_count = 0
    processed_conversation_urls: set[str] = set()

    def process_result(result: ChatGPTPromptMetadataWorkResult) -> bool:
        nonlocal completed_count, failed_conversations, prompt_metadata_deferred
        completed_count += 1
        processed_conversation_urls.add(result.conversation_url)
        if result.error:
            if "HTTP 429" in result.error:
                persist_pending_updates()
                state.append_event(
                    f"ChatGPT prompt metadata reached the API rate limit at session "
                    f"{result.conversation_index:,}/{len(conversation_urls):,}; deferring the remaining "
                    "prompt metadata and continuing the image sync."
                )
                prompt_metadata_deferred = True
                return False
            failed_conversations += 1
            state.append_event(
                f"ChatGPT prompt metadata skipped session {result.conversation_index:,}/"
                f"{len(conversation_urls):,}: "
                f"{_summarize_chatgpt_image_error(RuntimeError(result.error))}"
            )
        elif result.payload is not None:
            apply_payload(result.conversation_url, result.payload)

        if (
            completed_count == 1
            or completed_count % CHATGPT_PROMPT_METADATA_PERSIST_BATCH_SIZE == 0
            or completed_count == len(conversation_urls)
        ):
            persist_pending_updates()
            state.append_event(
                f"ChatGPT prompt metadata inspected {completed_count:,}/{len(conversation_urls):,} "
                f"relevant sessions and matched {prompt_count:,}/{len(candidates):,} images."
            )
        return True

    resolved_worker_count = max(1, min(int(worker_count), len(conversation_urls)))
    page_context = getattr(browser_page, "context", None) if browser_page is not None else None
    use_parallel_safari = resolved_worker_count > 1 and isinstance(page_context, SafariContext)
    if use_parallel_safari:
        state.append_event(
            f"Starting {resolved_worker_count:,} parallel Safari prompt metadata workers."
        )
        try:
            for result in _iter_parallel_safari_prompt_metadata_results(
                browser_page,
                project_url,
                conversation_urls,
                request_headers,
                should_stop,
                resolved_worker_count,
            ):
                if not process_result(result):
                    break
        except RuntimeError as exc:
            if not should_stop():
                state.append_event(
                    "Parallel ChatGPT prompt metadata fell back to the primary browser page: "
                    f"{_summarize_chatgpt_image_error(exc)}"
                )

    for conversation_index, conversation_url in enumerate(conversation_urls, start=1):
        if should_stop() or prompt_metadata_deferred:
            break
        if conversation_url in processed_conversation_urls:
            continue
        api_url = _chatgpt_conversation_api_url(conversation_url)
        if not api_url:
            continue
        try:
            if browser_page is not None:
                payload = _get_chatgpt_api_json_via_page(browser_page, api_url, request_headers)
            else:
                payload = _get_chatgpt_api_json(context, api_url, api_headers)
        except RuntimeError as exc:
            result = ChatGPTPromptMetadataWorkResult(
                conversation_index=conversation_index,
                conversation_url=conversation_url,
                error=str(exc),
            )
        else:
            result = ChatGPTPromptMetadataWorkResult(
                conversation_index=conversation_index,
                conversation_url=conversation_url,
                payload=payload,
            )
        if not process_result(result):
            break

    persist_pending_updates()
    if persist_batch is not None and persisted_update_count:
        state.append_event(
            f"Persisted prompt metadata updates for {persisted_update_count:,} ChatGPT catalog entries."
        )
    if failed_conversations:
        state.append_event(
            f"ChatGPT prompt metadata could not inspect {failed_conversations:,} relevant sessions."
        )
    return [candidates_by_file_id[candidate.file_id] for candidate in candidates]


def _extract_project_links(page, project_url: str) -> list[str]:
    """Read currently rendered conversation links from the project page."""
    prefix = _project_conversation_prefix(project_url)
    return page.evaluate(
        """(prefix) => [...new Set([...document.querySelectorAll('a[href]')]
            .map((anchor) => anchor.href)
            .filter((href) => href.startsWith(prefix)))]""",
        prefix,
    )


def _click_load_more_conversations(page) -> bool:
    """Ask ChatGPT's project list to render the next conversation page."""
    return bool(
        page.evaluate(
            """() => {
                const button = [...document.querySelectorAll('button')].find((candidate) =>
                    (candidate.innerText || '').replace(/\\s+/g, ' ').trim() === 'Load more conversations'
                );
                if (!button) {
                    return false;
                }
                button.scrollIntoView({ block: 'center' });
                button.click();
                return true;
            }"""
        )
    )


def _has_load_more_conversations(page) -> bool:
    """Return whether ChatGPT currently exposes project pagination."""
    return bool(
        page.evaluate(
            """() => [...document.querySelectorAll('button')].some((candidate) =>
                (candidate.innerText || '').replace(/\\s+/g, ' ').trim() === 'Load more conversations'
            )"""
        )
    )


def _wait_for_project_conversation_links(
    page,
    project_url: str,
    should_stop,
    startup_timeout_seconds: float = DEFAULT_CHATGPT_STARTUP_TIMEOUT_SECONDS,
    scan_wait_seconds: float = CHATGPT_SCAN_WAIT_SECONDS,
) -> list[str]:
    """Wait for the project list to finish its initial asynchronous rendering."""
    previous_count = 0
    stable_rounds = 0
    current_links: list[str] = []
    timeout_seconds = min(
        max(float(startup_timeout_seconds), MIN_CHATGPT_STARTUP_TIMEOUT_SECONDS),
        MAX_CHATGPT_STARTUP_TIMEOUT_SECONDS,
    )
    poll_seconds = min(max(float(scan_wait_seconds), MIN_CHATGPT_SCAN_WAIT_SECONDS), timeout_seconds)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if should_stop():
            return []
        current_links = _extract_project_links(page, project_url)
        if current_links and _has_load_more_conversations(page):
            return current_links
        if current_links and len(current_links) == previous_count:
            stable_rounds += 1
        else:
            stable_rounds = 0
        if current_links and stable_rounds >= 3:
            return current_links
        previous_count = len(current_links)
        remaining_seconds = max(0.05, deadline - time.monotonic())
        page.wait_for_timeout(int(min(poll_seconds, remaining_seconds) * 1_000))
    if current_links:
        return current_links
    raise RuntimeError(
        f"ChatGPT project loaded without any conversation links after {timeout_seconds:g} seconds. "
        "The authorized Edge session may need to finish loading the project first."
    )


def open_chatgpt_page(
    page,
    url: str,
    settle_ms: int = 2_500,
    startup_timeout_seconds: float | None = None,
) -> None:
    """Open a ChatGPT page and tolerate its long-lived application requests."""
    timeout_seconds = startup_timeout_seconds or CHATGPT_PAGE_GOTO_TIMEOUT_MS / 1_000
    timeout_seconds = min(
        max(float(timeout_seconds), MIN_CHATGPT_STARTUP_TIMEOUT_SECONDS),
        MAX_CHATGPT_STARTUP_TIMEOUT_SECONDS,
    )
    deadline = time.monotonic() + timeout_seconds
    navigation_error: Exception | None = None
    for attempt_index in range(2):
        remaining_ms = max(1_000, int((deadline - time.monotonic()) * 1_000))
        attempts_remaining = 2 - attempt_index
        navigation_timeout_ms = min(
            CHATGPT_PAGE_GOTO_TIMEOUT_MS,
            max(1_000, remaining_ms // attempts_remaining),
        )
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=navigation_timeout_ms)
            navigation_error = None
            break
        except (PlaywrightError, RuntimeError) as exc:
            navigation_error = exc
            if attempt_index == 0 and time.monotonic() < deadline:
                page.wait_for_timeout(min(500, max(0, int((deadline - time.monotonic()) * 1_000))))
    if navigation_error is not None:
        raise RuntimeError(
            f"ChatGPT startup timed out after {timeout_seconds:g} seconds while opening the page. "
            f"Last browser error: {navigation_error}"
        ) from navigation_error
    for _ in range(30):
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"ChatGPT startup timed out after {timeout_seconds:g} seconds while waiting for the page."
            )
        title = page.title().lower()
        remaining_ms = max(100, int((deadline - time.monotonic()) * 1_000))
        body_text = page.locator("body").inner_text(timeout=min(15_000, remaining_ms))[:500].lower()
        if "just a moment" not in title and "checking your browser" not in body_text:
            break
        page.wait_for_timeout(min(1_000, remaining_ms))
    else:
        raise RuntimeError(
            f"ChatGPT startup timed out after {timeout_seconds:g} seconds on a security verification page."
        )
    if settle_ms > 0:
        remaining_ms = max(0, int((deadline - time.monotonic()) * 1_000))
        if remaining_ms:
            page.wait_for_timeout(min(settle_ms, remaining_ms))


def _listen_for_chatgpt_request_headers(page, request_headers: dict[str, str]):
    """Capture the active browser authorization headers without persisting them."""
    add_listener = getattr(page, "on", None)
    if not callable(add_listener):
        return None

    def capture(response) -> None:
        captured_headers = _chatgpt_candidate_request_headers(response)
        if captured_headers:
            request_headers.update(captured_headers)

    add_listener("response", capture)
    return capture


def _stop_listening_for_chatgpt_response(page, listener) -> None:
    """Detach one temporary ChatGPT response listener."""
    if listener is None:
        return
    remove_listener = getattr(page, "remove_listener", None)
    if not callable(remove_listener):
        return
    try:
        remove_listener("response", listener)
    except PlaywrightError:
        pass


def _wait_for_chatgpt_request_headers(page, request_headers: dict[str, str]) -> None:
    """Wait briefly for the authenticated ChatGPT page to issue one API request."""
    deadline = time.monotonic() + CHATGPT_CONVERSATION_RESPONSE_WAIT_MS / 1_000
    while not request_headers and time.monotonic() < deadline:
        page.wait_for_timeout(200)


def _collect_project_conversation_urls_via_api(
    context,
    project_url: str,
    request_headers: dict[str, str],
    state: TaskState,
    should_stop,
    conversation_titles_by_id: dict[str, str] | None = None,
) -> list[str]:
    """Load project conversations and their authoritative titles through the API."""
    project_id = _chatgpt_project_id(project_url)
    if not project_id or not request_headers:
        return []

    api_headers = _chatgpt_api_request_headers(request_headers, project_url)
    conversation_prefix = _project_conversation_prefix(project_url)
    conversation_urls: list[str] = []
    seen_ids: set[str] = set()
    seen_cursors: set[str] = set()
    cursor = "0"
    for page_index in range(CHATGPT_API_PAGE_LIMIT):
        if should_stop() or not cursor or cursor in seen_cursors:
            break
        seen_cursors.add(cursor)
        query = urlencode({"cursor": cursor, "limit": CHATGPT_PROJECT_API_PAGE_SIZE})
        payload = _get_chatgpt_api_json(
            context,
            f"https://chatgpt.com/backend-api/gizmos/{project_id}/conversations?{query}",
            api_headers,
        )
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            break
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            conversation_id = str(raw_item.get("id") or raw_item.get("conversation_id") or "").strip()
            if conversation_id and conversation_id not in seen_ids:
                seen_ids.add(conversation_id)
                conversation_urls.append(f"{conversation_prefix}{conversation_id}")
            conversation_title = str(raw_item.get("title") or "").strip()
            if conversation_id and conversation_title and conversation_titles_by_id is not None:
                conversation_titles_by_id[conversation_id] = conversation_title
        state.update(
            discovered_tweets=len(conversation_urls),
            queued_tweets=len(conversation_urls),
            progress_unit="sessions",
        )
        state.append_event(
            f"ChatGPT project API loaded {len(conversation_urls):,} sessions after page {page_index + 1}."
        )
        cursor = str(payload.get("cursor") or "").strip()
        if not raw_items:
            break
    return conversation_urls


def _collect_all_chatgpt_conversation_urls_via_api(
    context,
    referer: str,
    request_headers: dict[str, str],
    state: TaskState,
    should_stop,
    conversation_titles_by_id: dict[str, str] | None = None,
    conversation_revisions_by_id: dict[str, str] | None = None,
) -> list[str]:
    """Load every ChatGPT session for text history, independent of the media project."""
    if not request_headers:
        return []

    api_headers = _chatgpt_api_request_headers(request_headers, referer)
    conversation_urls: list[str] = []
    seen_ids: set[str] = set()
    offset = 0
    for page_index in range(CHATGPT_API_PAGE_LIMIT):
        if should_stop():
            break
        query = urlencode(
            {
                "offset": offset,
                "limit": CHATGPT_HISTORY_API_PAGE_SIZE,
                "order": "updated",
            }
        )
        payload = _get_chatgpt_api_json(
            context,
            f"https://chatgpt.com/backend-api/conversations?{query}",
            api_headers,
        )
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            break
        new_session_count = 0
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            conversation_id = str(
                raw_item.get("id") or raw_item.get("conversation_id") or ""
            ).strip()
            if not conversation_id or conversation_id in seen_ids:
                continue
            seen_ids.add(conversation_id)
            new_session_count += 1
            conversation_urls.append(f"https://chatgpt.com/c/{conversation_id}")
            revision = raw_item.get("update_time")
            if conversation_revisions_by_id is not None and isinstance(revision, (str, int, float)):
                conversation_revisions_by_id[conversation_id] = str(revision).strip()
            conversation_title = str(raw_item.get("title") or "").strip()
            if conversation_title and conversation_titles_by_id is not None:
                conversation_titles_by_id[conversation_id] = conversation_title
        state.update(progress_unit="sessions")
        state.append_event(
            f"ChatGPT history API loaded {len(conversation_urls):,} total sessions "
            f"after page {page_index + 1}."
        )
        if not raw_items or not new_session_count:
            break
        offset += len(raw_items)
    return conversation_urls


def collect_project_conversation_urls(
    page,
    project_url: str,
    state: TaskState,
    should_stop,
    startup_timeout_seconds: float = DEFAULT_CHATGPT_STARTUP_TIMEOUT_SECONDS,
    scan_wait_seconds: float = CHATGPT_SCAN_WAIT_SECONDS,
    request_headers: dict[str, str] | None = None,
    conversation_titles_by_id: dict[str, str] | None = None,
) -> list[str]:
    """Load every project conversation, or use one supplied chat session URL."""
    if is_chatgpt_conversation_url(project_url):
        page_context = getattr(page, "context", None)
        if request_headers is not None and isinstance(page_context, SafariContext):
            request_headers.update(_load_chatgpt_session_request_headers(page_context, project_url))
        state.append_event("Using the supplied ChatGPT chat session URL.")
        return [project_url]

    observed_headers = request_headers if request_headers is not None else {}
    header_listener = _listen_for_chatgpt_request_headers(page, observed_headers)
    try:
        open_chatgpt_page(page, project_url, startup_timeout_seconds=startup_timeout_seconds)
        context = getattr(page, "context", None)
        if isinstance(context, SafariContext) and not observed_headers:
            observed_headers.update(_load_chatgpt_session_request_headers(context, project_url))
        else:
            _wait_for_chatgpt_request_headers(page, observed_headers)
        if context is not None and observed_headers:
            try:
                api_urls = _collect_project_conversation_urls_via_api(
                    context,
                    project_url,
                    observed_headers,
                    state,
                    should_stop,
                    conversation_titles_by_id,
                )
            except RuntimeError as exc:
                state.append_event(f"ChatGPT project API fallback: {exc}")
            else:
                if api_urls:
                    return api_urls
    finally:
        _stop_listening_for_chatgpt_response(page, header_listener)

    open_chatgpt_page(page, project_url, startup_timeout_seconds=startup_timeout_seconds)
    conversation_urls: list[str] = []
    seen_urls: set[str] = set()
    stagnant_rounds = 0
    current_links = _wait_for_project_conversation_links(
        page,
        project_url,
        should_stop,
        startup_timeout_seconds=startup_timeout_seconds,
        scan_wait_seconds=max(float(scan_wait_seconds), CHATGPT_PROJECT_SCAN_WAIT_SECONDS),
    )

    for round_index in range(CHATGPT_PROJECT_LOAD_ROUNDS):
        if should_stop():
            break
        for url in current_links:
            if url not in seen_urls:
                seen_urls.add(url)
                conversation_urls.append(url)
        state.append_event(
            f"ChatGPT project scan loaded {len(conversation_urls):,} sessions after page {round_index + 1}."
        )
        if not _has_load_more_conversations(page):
            break
        previous_rendered_count = len(current_links)
        if not _click_load_more_conversations(page):
            break
        for _ in range(CHATGPT_PROJECT_LINK_WAIT_ROUNDS):
            page.wait_for_timeout(1_000)
            current_links = _extract_project_links(page, project_url)
            if len(current_links) > previous_rendered_count:
                break
        after_count = len(current_links)
        stagnant_rounds = stagnant_rounds + 1 if after_count <= previous_rendered_count else 0
        if stagnant_rounds >= 3:
            state.append_event("ChatGPT project pagination stopped making progress.")
            break

    return conversation_urls


def _recent_chatgpt_image_candidate(
    raw_item: dict[str, object],
    project_url: str,
    project_conversation_ids: set[str],
    conversation_titles_by_id: dict[str, str] | None = None,
) -> ChatGPTImageCandidate | None:
    """Build one original-image candidate from ChatGPT's recent image index."""
    conversation_id = str(raw_item.get("conversation_id") or "").strip()
    source_url = str(raw_item.get("url") or "").strip()
    if not conversation_id or conversation_id not in project_conversation_ids or not source_url:
        return None
    if urlsplit(source_url).scheme.lower() != "https":
        return None

    pointer_file_id = _extract_chatgpt_file_id_from_asset_pointer(raw_item.get("asset_pointer"))
    file_id = pointer_file_id or extract_chatgpt_file_id(source_url)
    if not file_id:
        return None
    try:
        width = max(0, int(raw_item.get("width") or 0))
        height = max(0, int(raw_item.get("height") or 0))
    except (TypeError, ValueError):
        width = 0
        height = 0
    candidate = ChatGPTImageCandidate(
        source_url=source_url,
        file_id=file_id,
        conversation_url=f"{_project_conversation_prefix(project_url)}{conversation_id}",
        alt_text=str(raw_item.get("title") or "").strip(),
        prompt_markdown=str(raw_item.get("prompt") or raw_item.get("prompt_text") or "").strip(),
        width=width,
        height=height,
        message_role="assistant",
        conversation_title=str((conversation_titles_by_id or {}).get(conversation_id) or "").strip(),
        created_at=str(raw_item.get("created_at") or "").strip(),
    )
    return candidate if should_cache_chatgpt_candidate(candidate) else None


def collect_chatgpt_project_index_images(
    context,
    project_url: str,
    conversation_urls: list[str],
    request_headers: dict[str, str],
    state: TaskState,
    should_stop,
    conversation_titles_by_id: dict[str, str] | None = None,
) -> list[ChatGPTImageCandidate]:
    """Collect every current project image exposed by ChatGPT's recent-image index."""
    project_conversation_ids = {
        conversation_id
        for conversation_url in conversation_urls
        if (conversation_id := chatgpt_conversation_id(conversation_url))
    }
    if not project_conversation_ids or not request_headers:
        return []

    api_headers = _chatgpt_api_request_headers(request_headers, project_url)
    candidates_by_file_id: dict[str, ChatGPTImageCandidate] = {}
    seen_cursors: set[str] = set()
    cursor = ""
    for page_index in range(CHATGPT_API_PAGE_LIMIT):
        if should_stop():
            break
        if cursor in seen_cursors:
            raise RuntimeError(
                "ChatGPT recent-image index repeated a pagination cursor before discovery completed."
            )
        seen_cursors.add(cursor)
        query_values = {"limit": CHATGPT_RECENT_IMAGE_PAGE_SIZE}
        if cursor:
            query_values["after"] = cursor
        try:
            payload = _get_chatgpt_api_json(
                context,
                f"https://chatgpt.com/backend-api/my/recent/image_gen?{urlencode(query_values)}",
                api_headers,
            )
        except RuntimeError as exc:
            if not candidates_by_file_id:
                state.append_event(f"ChatGPT recent-image index unavailable; using session scans: {exc}")
                return []
            raise RuntimeError(
                "ChatGPT recent-image index stopped before pagination completed."
            ) from exc
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            if not candidates_by_file_id:
                return []
            raise RuntimeError("ChatGPT recent-image index returned an incomplete page payload.")
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            candidate = _recent_chatgpt_image_candidate(
                raw_item,
                project_url,
                project_conversation_ids,
                conversation_titles_by_id,
            )
            if candidate is None:
                continue
            candidate = replace(candidate, request_headers=dict(request_headers))
            previous = candidates_by_file_id.get(candidate.file_id)
            if previous is None or candidate.width * candidate.height >= previous.width * previous.height:
                candidates_by_file_id[candidate.file_id] = candidate
        current_discovered_images = int(state.snapshot().get("discovered_images") or 0)
        current_image_count = max(current_discovered_images, len(candidates_by_file_id))
        state.update(
            discovered_images=current_image_count,
            queued_tweets=current_image_count,
            progress_unit="images",
        )
        if page_index == 0 or (page_index + 1) % 10 == 0:
            state.append_event(
                f"ChatGPT recent-image index found {len(candidates_by_file_id):,} project images "
                f"after page {page_index + 1}."
            )
        cursor = str(payload.get("cursor") or "").strip()
        if not raw_items or not cursor:
            break
    return list(candidates_by_file_id.values())


def _extract_original_image_payloads(page) -> list[dict[str, object]]:
    """Extract original ChatGPT image URLs currently materialized in the DOM."""
    return page.evaluate(
        """() => {
            const results = new Map();
            const isOriginalImageUrl = (value) => {
                if (!value) return false;
                try {
                    const url = new URL(value, location.href);
                    const path = url.pathname.toLowerCase();
                    if (url.hostname === 'chatgpt.com' && path.includes('/backend-api/estuary/content')) {
                        return Boolean(url.searchParams.get('id'));
                    }
                    return url.hostname.endsWith('oaiusercontent.com') ||
                        url.hostname.endsWith('blob.core.windows.net');
                } catch (_error) {
                    return false;
                }
            };

            const messages = [...document.querySelectorAll('[data-message-author-role]')];

            for (const image of document.images) {
                const candidateUrls = [
                    image.currentSrc,
                    image.src,
                    image.getAttribute('data-src'),
                    image.getAttribute('data-original'),
                ].filter(Boolean);
                const sourceUrl = candidateUrls.find(isOriginalImageUrl);
                if (!sourceUrl) continue;

                const parsed = new URL(sourceUrl, location.href);
                const fileId = parsed.searchParams.get('id') || sourceUrl;
                const message = image.closest('[data-message-author-role]');
                const messageIndex = messages.indexOf(message);
                let promptMarkdown = '';
                for (let index = messageIndex; index >= 0; index -= 1) {
                    const candidateMessage = messages[index];
                    if (candidateMessage.getAttribute('data-message-author-role') !== 'user') continue;
                    const markdownRoot = candidateMessage.querySelector('.markdown');
                    promptMarkdown = (markdownRoot?.innerText || candidateMessage.innerText || '').trim();
                    if (promptMarkdown) break;
                }
                const candidate = {
                    sourceUrl: parsed.href,
                    fileId,
                    altText: image.alt || '',
                    width: image.naturalWidth || 0,
                    height: image.naturalHeight || 0,
                    messageRole: message?.getAttribute('data-message-author-role') || '',
                    promptMarkdown,
                };
                const previous = results.get(fileId);
                if (!previous || (candidate.width * candidate.height) >= (previous.width * previous.height)) {
                    results.set(fileId, candidate);
                }
            }
            return [...results.values()];
        }"""
    )


def _listen_for_chatgpt_conversation_response(page, conversation_url: str):
    """Capture the complete conversation JSON response emitted during page startup."""
    expected_url = _chatgpt_conversation_api_url(conversation_url)
    add_listener = getattr(page, "on", None)
    if not expected_url or not callable(add_listener):
        return [], None

    responses: list[object] = []

    def capture(response) -> None:
        response_url = str(getattr(response, "url", "")).split("?", 1)[0].rstrip("/")
        if response_url == expected_url.rstrip("/"):
            responses.append(response)

    add_listener("response", capture)
    return responses, capture


def _stop_listening_for_chatgpt_conversation_response(page, listener) -> None:
    """Detach one temporary ChatGPT conversation response listener."""
    if listener is None:
        return
    remove_listener = getattr(page, "remove_listener", None)
    if not callable(remove_listener):
        return
    try:
        remove_listener("response", listener)
    except PlaywrightError:
        pass


def _wait_for_chatgpt_conversation_response(page, responses: list[object], listener) -> object | None:
    """Wait briefly for the full conversation response without delaying DOM fallback scans."""
    if listener is None:
        return None
    deadline = time.monotonic() + CHATGPT_CONVERSATION_RESPONSE_WAIT_MS / 1_000
    while time.monotonic() < deadline:
        for response in reversed(responses):
            try:
                payload = json.loads(response.text())
            except (AttributeError, PlaywrightError, TypeError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict) and isinstance(payload.get("mapping"), dict):
                return response
        page.wait_for_timeout(200)
    return responses[-1] if responses else None


def _wait_for_chatgpt_rendered_images(
    page,
    expected_image_count: int,
    should_stop,
) -> None:
    """Allow active ChatGPT image elements to materialize before the DOM fallback scan."""
    if expected_image_count <= 0:
        return
    deadline = time.monotonic() + CHATGPT_RENDERED_IMAGE_WAIT_MS / 1_000
    previous_count = 0
    stable_rounds = 0
    while not should_stop() and time.monotonic() < deadline:
        current_count = len(_extract_original_image_payloads(page))
        if current_count >= expected_image_count:
            return
        if current_count and current_count == previous_count:
            stable_rounds += 1
            if stable_rounds >= CHATGPT_RENDERED_IMAGE_STABLE_ROUNDS:
                return
        else:
            stable_rounds = 0
        previous_count = current_count
        page.wait_for_timeout(CHATGPT_RENDERED_IMAGE_POLL_MS)


def _merge_chatgpt_conversation_response_images(
    response,
    conversation_url: str,
    candidates_by_file_id: dict[str, ChatGPTImageCandidate],
    conversation_title: str,
) -> None:
    """Merge every image asset from ChatGPT's complete conversation API mapping."""
    try:
        payload = json.loads(response.text())
    except (AttributeError, PlaywrightError, TypeError, json.JSONDecodeError):
        return
    _merge_chatgpt_conversation_payload_images(
        payload,
        _chatgpt_candidate_request_headers(response),
        conversation_url,
        candidates_by_file_id,
        conversation_title,
    )


def _merge_chatgpt_conversation_payload_images(
    payload: dict[str, object],
    request_headers: dict[str, str],
    conversation_url: str,
    candidates_by_file_id: dict[str, ChatGPTImageCandidate],
    conversation_title: str,
) -> None:
    """Merge image assets from one authenticated conversation payload."""
    authoritative_title = str(payload.get("title") or "").strip() or conversation_title
    for raw_candidate in _extract_chatgpt_conversation_image_payloads(
        payload,
        include_sediment=True,
    ):
        file_id = str(raw_candidate.get("fileId") or "").strip()
        if not file_id:
            continue
        candidate = ChatGPTImageCandidate(
            source_url=_chatgpt_file_download_url(file_id, conversation_url),
            file_id=file_id,
            conversation_url=conversation_url,
            alt_text=str(raw_candidate.get("altText") or "").strip(),
            prompt_markdown=str(raw_candidate.get("promptMarkdown") or "").strip(),
            width=int(raw_candidate.get("width") or 0),
            height=int(raw_candidate.get("height") or 0),
            message_role=str(raw_candidate.get("messageRole") or "").strip(),
            conversation_title=authoritative_title,
            prompt_metadata_authoritative=True,
            request_headers=dict(request_headers),
        )
        previous = candidates_by_file_id.get(file_id)
        if candidate.message_role.strip().lower() == "user":
            candidates_by_file_id[file_id] = candidate
            continue
        if previous is not None and previous.message_role.strip().lower() == "user":
            continue
        if not should_cache_chatgpt_candidate(candidate):
            continue
        if previous is None or candidate.width * candidate.height >= previous.width * previous.height:
            candidates_by_file_id[file_id] = candidate


def _scroll_chatgpt_message_view(page, direction: str) -> dict[str, object]:
    """Move the conversation's own scroll container one viewport step."""
    return page.evaluate(
        """({ direction, stepRatio }) => {
            const messageSelector = '[data-message-author-role]';
            const scrollRoots = new Set();
            for (const message of document.querySelectorAll(messageSelector)) {
                for (let parent = message.parentElement; parent; parent = parent.parentElement) {
                    const overflowY = getComputedStyle(parent).overflowY;
                    if (
                        parent.scrollHeight > parent.clientHeight &&
                        (overflowY === 'auto' || overflowY === 'scroll')
                    ) {
                        scrollRoots.add(parent);
                    }
                }
            }

            const root = [...scrollRoots]
                .sort((left, right) => right.scrollHeight - left.scrollHeight)[0] ||
                document.scrollingElement ||
                document.documentElement;
            const previousTop = root.scrollTop;
            const step = Math.max(1, Math.floor(root.clientHeight * stepRatio));
            const maximumTop = Math.max(0, root.scrollHeight - root.clientHeight);
            root.scrollTop = direction === 'top'
                ? Math.max(0, previousTop - step)
                : Math.min(maximumTop, previousTop + step);
            const currentTop = root.scrollTop;
            const atBoundary = direction === 'top'
                ? currentTop <= 12
                : currentTop >= maximumTop - 12;
            return {
                height: root.scrollHeight,
                top: currentTop,
                viewport: root.clientHeight,
                atBoundary,
                moved: Math.abs(currentTop - previousTop) > 0,
            };
        }""",
        {"direction": direction, "stepRatio": CHATGPT_SCROLL_STEP_RATIO},
    )


def _merge_current_conversation_images(
    page,
    conversation_url: str,
    candidates_by_file_id: dict[str, ChatGPTImageCandidate],
    conversation_title: str,
) -> None:
    """Merge original images currently visible in one ChatGPT conversation."""
    for raw_candidate in _extract_original_image_payloads(page):
        source_url = str(raw_candidate.get("sourceUrl") or "").strip()
        if not source_url:
            continue
        file_id = str(raw_candidate.get("fileId") or extract_chatgpt_file_id(source_url)).strip()
        candidate = ChatGPTImageCandidate(
            source_url=source_url,
            file_id=file_id,
            conversation_url=conversation_url,
            alt_text=str(raw_candidate.get("altText") or "").strip(),
            prompt_markdown=str(raw_candidate.get("promptMarkdown") or "").strip(),
            width=int(raw_candidate.get("width") or 0),
            height=int(raw_candidate.get("height") or 0),
            message_role=str(raw_candidate.get("messageRole") or "").strip(),
            conversation_title=conversation_title,
        )
        previous = candidates_by_file_id.get(file_id)
        if candidate.message_role.strip().lower() == "user":
            candidates_by_file_id[file_id] = candidate
            continue
        if previous is not None and previous.message_role.strip().lower() == "user":
            continue
        if not should_cache_chatgpt_candidate(candidate):
            continue
        if previous is not None:
            has_authoritative_prompt_metadata = (
                previous.prompt_metadata_authoritative or bool(previous.request_headers)
            )
            candidate = replace(
                candidate,
                alt_text=candidate.alt_text or previous.alt_text,
                prompt_markdown=(
                    previous.prompt_markdown
                    if has_authoritative_prompt_metadata
                    else candidate.prompt_markdown or previous.prompt_markdown
                ),
                prompt_metadata_authoritative=has_authoritative_prompt_metadata,
                message_role=candidate.message_role or previous.message_role,
                conversation_title=previous.conversation_title or candidate.conversation_title,
                created_at=candidate.created_at or previous.created_at,
                request_headers=candidate.request_headers or previous.request_headers,
            )
        candidate_has_direct_url = not _is_chatgpt_file_download_url(candidate.source_url)
        previous_has_direct_url = previous is not None and not _is_chatgpt_file_download_url(previous.source_url)
        if (
            previous is None
            or (candidate_has_direct_url and not previous_has_direct_url)
            or (
                candidate_has_direct_url == previous_has_direct_url
                and candidate.width * candidate.height >= previous.width * previous.height
            )
        ):
            candidates_by_file_id[file_id] = candidate


def collect_conversation_images(
    page,
    conversation_url: str,
    should_stop,
    startup_timeout_seconds: float = DEFAULT_CHATGPT_STARTUP_TIMEOUT_SECONDS,
    scan_wait_seconds: float = CHATGPT_SCAN_WAIT_SECONDS,
) -> list[ChatGPTImageCandidate]:
    """Open one conversation, scan its lazy message view, and collect original images."""
    candidates_by_file_id: dict[str, ChatGPTImageCandidate] = {}
    safari_payload: dict[str, object] | None = None
    safari_request_headers: dict[str, str] = {}
    responses, response_listener = _listen_for_chatgpt_conversation_response(page, conversation_url)
    try:
        open_chatgpt_page(
            page,
            conversation_url,
            settle_ms=1_000,
            startup_timeout_seconds=startup_timeout_seconds,
        )
        try:
            conversation_title = _chatgpt_conversation_title(page.title())
        except (AttributeError, TypeError):
            conversation_title = ""
        page_context = getattr(page, "context", None)
        if response_listener is None and isinstance(page_context, SafariContext):
            safari_request_headers = _load_chatgpt_session_request_headers(
                page_context,
                conversation_url,
            )
            api_url = _chatgpt_conversation_api_url(conversation_url)
            if api_url:
                safari_payload = _get_chatgpt_api_json(
                    page_context,
                    api_url,
                    _chatgpt_api_request_headers(safari_request_headers, conversation_url),
                )
        _wait_for_chatgpt_conversation_response(page, responses, response_listener)
        for response in reversed(responses):
            _merge_chatgpt_conversation_response_images(
                response,
                conversation_url,
                candidates_by_file_id,
                conversation_title,
            )
        if safari_payload is not None:
            _merge_chatgpt_conversation_payload_images(
                safari_payload,
                safari_request_headers,
                conversation_url,
                candidates_by_file_id,
                conversation_title,
            )
    finally:
        _stop_listening_for_chatgpt_conversation_response(page, response_listener)

    _wait_for_chatgpt_rendered_images(page, len(candidates_by_file_id), should_stop)

    for direction in ("top", "bottom"):
        stable_rounds = 0
        previous_height = -1
        for _ in range(CHATGPT_SCROLL_ROUNDS):
            if should_stop():
                break
            _merge_current_conversation_images(page, conversation_url, candidates_by_file_id, conversation_title)
            scroll_metrics = _scroll_chatgpt_message_view(page, direction)
            wait_seconds = min(
                max(float(scan_wait_seconds), MIN_CHATGPT_SCAN_WAIT_SECONDS),
                MAX_CHATGPT_SCAN_WAIT_SECONDS,
            )
            page.wait_for_timeout(int(wait_seconds * 1_000))
            current_height = int(scroll_metrics.get("height") or 0)
            at_boundary = bool(scroll_metrics.get("atBoundary"))
            if at_boundary and not bool(scroll_metrics.get("moved")):
                break
            if at_boundary and current_height == previous_height:
                stable_rounds += 1
            else:
                stable_rounds = 0
            previous_height = current_height
            if stable_rounds >= 3:
                break
        if should_stop():
            break

    _merge_current_conversation_images(page, conversation_url, candidates_by_file_id, conversation_title)
    return [
        candidate for candidate in candidates_by_file_id.values()
        if should_cache_chatgpt_candidate(candidate)
    ]


def _chatgpt_conversation_title(value: str) -> str:
    """Remove the ChatGPT product suffix from one browser tab title."""
    title = re.sub(r"\s*(?:[-|–—]\s*)?ChatGPT\s*$", "", str(value or "").strip(), flags=re.IGNORECASE)
    return "" if title.casefold() == "chatgpt" else title.strip()


def _is_recoverable_chatgpt_page_error(error: Exception) -> bool:
    """Return whether a failed page can be recovered by recycling the tab."""
    error_text = str(error or "")
    return any(marker in error_text for marker in CHATGPT_RECOVERABLE_PAGE_ERROR_MARKERS)


def _close_chatgpt_page(page) -> None:
    """Close a worker page without masking the original browser error."""
    if page is None:
        return
    try:
        page.close()
    except Exception:
        pass


def _collect_chatgpt_conversation_with_recovery(
    context,
    page,
    conversation_url: str,
    should_stop,
    startup_timeout_seconds: float,
    scan_wait_seconds: float,
) -> tuple[object, list[ChatGPTImageCandidate], Exception | None]:
    """Scan one conversation and recycle the worker page after recoverable failures."""
    try:
        return (
            page,
            collect_conversation_images(
                page,
                conversation_url,
                should_stop,
                startup_timeout_seconds=startup_timeout_seconds,
                scan_wait_seconds=scan_wait_seconds,
            ),
            None,
        )
    except Exception as exc:  # pragma: no cover - depends on live ChatGPT rendering
        if not _is_recoverable_chatgpt_page_error(exc):
            return page, [], exc
        _close_chatgpt_page(page)
        try:
            retry_page = context.new_page()
            return (
                retry_page,
                collect_conversation_images(
                    retry_page,
                    conversation_url,
                    should_stop,
                    startup_timeout_seconds=startup_timeout_seconds,
                    scan_wait_seconds=scan_wait_seconds,
                ),
                None,
            )
        except Exception as retry_error:  # pragma: no cover - depends on live ChatGPT rendering
            return retry_page if "retry_page" in locals() else page, [], retry_error


def _is_unavailable_chatgpt_image_error(candidate: ChatGPTImageCandidate, error: Exception) -> bool:
    """Return whether ChatGPT no longer serves an indexed historical image asset."""
    if "HTTP 404." not in str(error):
        return False
    return _is_chatgpt_file_download_url(candidate.source_url) or (
        urlsplit(candidate.source_url).path == "/backend-api/estuary/content"
    )


@contextlib.contextmanager
def _launch_chatgpt_browser_context(descriptor, initial_url: str):
    """Open one isolated authenticated context for either Chromium or Safari."""
    if descriptor.engine == "safari":
        with SafariContext(initial_url) as context:
            yield context
        return

    if sync_playwright is None:
        raise RuntimeError("Playwright is not installed for Chromium ChatGPT sync.")
    with sync_playwright() as playwright:
        with launch_chromium_context(
            playwright,
            descriptor,
            headless=False,
            clone_profile_first=True,
            background_window=True,
        ) as context:
            yield context


def _chatgpt_context_page(context):
    """Return the owned Safari page or create a Chromium worker page."""
    if isinstance(context, SafariContext):
        return context.primary_page
    return context.new_page()


def _chatgpt_conversation_worker(
    assignments: list[tuple[int, str]],
    descriptor,
    catalog: ChatGPTImageCatalog,
    target_dir: Path,
    startup_timeout_seconds: float,
    scan_wait_seconds: float,
    should_stop,
    result_queue: BoundedWorkQueue[ChatGPTConversationWorkResult],
    max_file_size_bytes: int = 0,
    assistant_only: bool = False,
) -> None:
    """Scan and download one partition of conversations in an isolated browser context."""
    next_assignment = 0
    page = None
    try:
        initial_url = assignments[0][1] if assignments else "https://chatgpt.com/"
        with _launch_chatgpt_browser_context(descriptor, initial_url) as context:
            if context is not None:
                page = _chatgpt_context_page(context)
                for assignment_position, (conversation_index, conversation_url) in enumerate(assignments):
                    if should_stop():
                        break
                    next_assignment = assignment_position
                    if assignment_position and assignment_position % CHATGPT_PAGE_RECYCLE_INTERVAL == 0:
                        _close_chatgpt_page(page)
                        page = context.new_page()

                    page, candidates, conversation_error = _collect_chatgpt_conversation_with_recovery(
                        context,
                        page,
                        conversation_url,
                        should_stop,
                        startup_timeout_seconds,
                        scan_wait_seconds,
                    )
                    if conversation_error is not None:
                        _put_chatgpt_result(
                            result_queue,
                            ChatGPTConversationWorkResult(
                                conversation_index=conversation_index,
                                conversation_url=conversation_url,
                                failed_count=1,
                                error=str(conversation_error),
                            ),
                            should_stop,
                        )
                        next_assignment = assignment_position + 1
                        continue

                    if assistant_only:
                        candidates = [
                            candidate
                            for candidate in candidates
                            if candidate.message_role.strip().lower() == "assistant"
                        ]

                    downloaded_count = 0
                    skipped_known = 0
                    oversized_count = 0
                    image_errors: list[str] = []
                    for candidate in candidates:
                        if should_stop():
                            break
                        try:
                            downloaded = download_chatgpt_image(
                                context,
                                catalog,
                                target_dir,
                                candidate,
                                max_file_size_bytes=max_file_size_bytes,
                            )
                        except ChatGPTImageSizeLimitError:
                            skipped_known += 1
                            oversized_count += 1
                            continue
                        except Exception as exc:  # pragma: no cover - depends on live ChatGPT responses
                            if _is_unavailable_chatgpt_image_error(candidate, exc):
                                catalog.mark_unavailable(candidate.file_id)
                                skipped_known += 1
                                continue
                            image_errors.append(f"{candidate.file_id}: {exc}")
                            continue
                        if downloaded:
                            downloaded_count += 1
                        else:
                            skipped_known += 1

                    _put_chatgpt_result(
                        result_queue,
                        ChatGPTConversationWorkResult(
                            conversation_index=conversation_index,
                            conversation_url=conversation_url,
                            candidate_file_ids=tuple(candidate.file_id for candidate in candidates),
                            downloaded_count=downloaded_count,
                            skipped_known=skipped_known,
                            failed_count=len(image_errors),
                            oversized_count=oversized_count,
                            image_errors=tuple(image_errors),
                        ),
                        should_stop,
                    )
                    next_assignment = assignment_position + 1
    except Exception as exc:  # pragma: no cover - depends on live browser startup
        for conversation_index, conversation_url in assignments[next_assignment:]:
            _put_chatgpt_result(
                result_queue,
                ChatGPTConversationWorkResult(
                    conversation_index=conversation_index,
                    conversation_url=conversation_url,
                    failed_count=1,
                    error=str(exc),
                ),
                should_stop,
            )
    finally:
        _close_chatgpt_page(page)


def _iter_chatgpt_conversation_results(
    conversation_urls: list[str],
    descriptor,
    catalog: ChatGPTImageCatalog,
    target_dir: Path,
    startup_timeout_seconds: float,
    scan_wait_seconds: float,
    should_stop,
    worker_count: int,
    max_file_size_bytes: int = 0,
    assistant_only: bool = False,
) -> Iterator[ChatGPTConversationWorkResult]:
    """Yield conversation results while bounded browser workers run in parallel."""
    if not conversation_urls:
        return

    worker_count = max(1, min(worker_count, len(conversation_urls)))
    assignments = [
        list(enumerate(conversation_urls, start=1))[worker_index::worker_count]
        for worker_index in range(worker_count)
    ]
    result_queue: BoundedWorkQueue[ChatGPTConversationWorkResult] = BoundedWorkQueue(
        CHATGPT_RESULT_QUEUE_SIZE
    )
    optional_worker_args = (
        (max_file_size_bytes, True)
        if assistant_only
        else ((max_file_size_bytes,) if max_file_size_bytes > 0 else ())
    )
    workers = [
        Thread(
            target=_chatgpt_conversation_worker,
            args=(
                worker_assignments,
                descriptor,
                catalog,
                target_dir,
                startup_timeout_seconds,
                scan_wait_seconds,
                should_stop,
                result_queue,
            ) + optional_worker_args,
            daemon=True,
            name=f"chatgpt-worker-{worker_index + 1}",
        )
        for worker_index, worker_assignments in enumerate(assignments)
        if worker_assignments
    ]
    for worker in workers:
        worker.start()

    try:
        while any(worker.is_alive() for worker in workers) or not result_queue.empty():
            try:
                yield result_queue.get(timeout=0.2)
            except Empty:
                continue
    finally:
        for worker in workers:
            worker.join()

    while not result_queue.empty():
        yield result_queue.get_nowait()


def _chatgpt_index_image_worker(
    candidates: list[ChatGPTImageCandidate],
    descriptor,
    catalog: ChatGPTImageCatalog,
    target_dir: Path,
    should_stop,
    result_queue: BoundedWorkQueue[ChatGPTImageDownloadWorkResult],
    max_file_size_bytes: int = 0,
) -> None:
    """Download one partition of signed project-index originals in an isolated browser context."""
    if not candidates or should_stop():
        return
    next_candidate_index = 0
    final_start_error: Exception | None = None
    for start_attempt_index in range(CHATGPT_WORKER_START_RETRY_LIMIT):
        if should_stop() or next_candidate_index >= len(candidates):
            return
        initial_url = candidates[next_candidate_index].conversation_url or "https://chatgpt.com/"
        try:
            with _launch_chatgpt_browser_context(descriptor, initial_url) as context:
                if context is None:
                    raise RuntimeError("ChatGPT image worker did not receive a browser context.")
                if isinstance(context, SafariContext):
                    open_chatgpt_page(
                        context.primary_page,
                        initial_url,
                        settle_ms=2_500,
                    )
                worker_request_headers = _load_chatgpt_session_request_headers(
                    context,
                    initial_url,
                )
                for candidate_index in range(next_candidate_index, len(candidates)):
                    if should_stop():
                        return
                    next_candidate_index = candidate_index
                    candidate = candidates[candidate_index]
                    candidate = replace(
                        candidate,
                        request_headers={
                            **candidate.request_headers,
                            **worker_request_headers,
                        },
                    )
                    try:
                        downloaded = download_chatgpt_image(
                            context,
                            catalog,
                            target_dir,
                            candidate,
                            max_file_size_bytes=max_file_size_bytes,
                        )
                    except ChatGPTImageSizeLimitError:
                        _put_chatgpt_result(
                            result_queue,
                            ChatGPTImageDownloadWorkResult(
                                candidate_file_id=candidate.file_id,
                                skipped=True,
                                skipped_size=True,
                            ),
                            should_stop,
                        )
                        next_candidate_index = candidate_index + 1
                        continue
                    except Exception as exc:  # pragma: no cover - depends on live ChatGPT responses
                        if _is_unavailable_chatgpt_image_error(candidate, exc):
                            catalog.mark_unavailable(candidate.file_id)
                            _put_chatgpt_result(
                                result_queue,
                                ChatGPTImageDownloadWorkResult(
                                    candidate_file_id=candidate.file_id,
                                    skipped=True,
                                ),
                                should_stop,
                            )
                            next_candidate_index = candidate_index + 1
                            continue
                        _put_chatgpt_result(
                            result_queue,
                            ChatGPTImageDownloadWorkResult(
                                candidate_file_id=candidate.file_id,
                                error=str(exc),
                            ),
                            should_stop,
                        )
                    else:
                        _put_chatgpt_result(
                            result_queue,
                            ChatGPTImageDownloadWorkResult(
                                candidate_file_id=candidate.file_id,
                                downloaded=downloaded,
                                skipped=not downloaded,
                            ),
                            should_stop,
                        )
                    next_candidate_index = candidate_index + 1
                return
        except Exception as exc:  # pragma: no cover - depends on live browser startup
            final_start_error = exc
            can_retry = (
                not should_stop()
                and next_candidate_index < len(candidates)
                and start_attempt_index + 1 < CHATGPT_WORKER_START_RETRY_LIMIT
            )
            if not can_retry:
                break
            logger.warning(
                "Retrying a ChatGPT image worker after browser startup failed.",
                extra={
                    "attempt": start_attempt_index + 1,
                    "max_attempts": CHATGPT_WORKER_START_RETRY_LIMIT,
                    "error": _summarize_chatgpt_image_error(exc),
                },
            )
            time.sleep(CHATGPT_WORKER_START_RETRY_DELAY_SECONDS * (start_attempt_index + 1))

    if should_stop() or next_candidate_index >= len(candidates):
        return
    worker_error = final_start_error or RuntimeError("ChatGPT image worker startup failed.")
    for candidate in candidates[next_candidate_index:]:
        _put_chatgpt_result(
            result_queue,
            ChatGPTImageDownloadWorkResult(
                candidate_file_id=candidate.file_id,
                error=str(worker_error),
            ),
            should_stop,
        )


def _iter_chatgpt_index_image_results(
    candidates: list[ChatGPTImageCandidate],
    descriptor,
    catalog: ChatGPTImageCatalog,
    target_dir: Path,
    should_stop,
    worker_count: int,
    max_file_size_bytes: int = 0,
) -> Iterator[ChatGPTImageDownloadWorkResult]:
    """Yield bounded parallel downloads for ChatGPT's project-level image index."""
    if not candidates or should_stop():
        return

    pending_candidates: list[ChatGPTImageCandidate] = []
    for candidate in candidates:
        if catalog.complete_entry(candidate.file_id) is not None:
            yield ChatGPTImageDownloadWorkResult(
                candidate_file_id=candidate.file_id,
                skipped=True,
            )
        else:
            pending_candidates.append(candidate)

    if not pending_candidates or should_stop():
        return

    worker_count = max(1, min(worker_count, len(pending_candidates)))
    assignments = [
        pending_candidates[worker_index::worker_count]
        for worker_index in range(worker_count)
    ]
    result_queue: BoundedWorkQueue[ChatGPTImageDownloadWorkResult] = BoundedWorkQueue(
        CHATGPT_RESULT_QUEUE_SIZE
    )
    workers = [
        Thread(
            target=_chatgpt_index_image_worker,
            args=(worker_candidates, descriptor, catalog, target_dir, should_stop, result_queue)
            + ((max_file_size_bytes,) if max_file_size_bytes > 0 else ()),
            daemon=True,
            name=f"chatgpt-index-worker-{worker_index + 1}",
        )
        for worker_index, worker_candidates in enumerate(assignments)
        if worker_candidates
    ]
    for worker in workers:
        worker.start()

    expected_result_count = len(pending_candidates)
    yielded_result_count = 0
    try:
        while yielded_result_count < expected_result_count and (
            any(worker.is_alive() for worker in workers) or not result_queue.empty()
        ):
            try:
                result = result_queue.get(timeout=0.2)
            except Empty:
                continue
            yielded_result_count += 1
            yield result
    finally:
        join_deadline = time.monotonic() + CHATGPT_WORKER_JOIN_TIMEOUT_SECONDS
        for worker in workers:
            worker.join(timeout=max(0.0, join_deadline - time.monotonic()))
        lingering_workers = [worker.name for worker in workers if worker.is_alive()]
        if lingering_workers:
            logger.warning(
                "ChatGPT image workers exceeded the bounded cleanup wait after returning their results.",
                extra={"workers": lingering_workers},
            )

    while yielded_result_count < expected_result_count and not result_queue.empty():
        yielded_result_count += 1
        yield result_queue.get_nowait()


def _chatgpt_image_request_headers(candidate: ChatGPTImageCandidate) -> dict[str, str]:
    """Build transient browser-authenticated headers for one ChatGPT image request."""
    headers = {
        str(key): str(value)
        for key, value in candidate.request_headers.items()
        if str(key).lower() in CHATGPT_DOWNLOAD_AUTH_HEADER_NAMES and str(value)
    }
    headers.update(
        {
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Referer": candidate.conversation_url,
        }
    )
    return headers


def _resolve_chatgpt_image_source_url(context, candidate: ChatGPTImageCandidate) -> str:
    """Resolve a file-service asset to its short-lived original image URL."""
    if not _is_chatgpt_file_download_url(candidate.source_url):
        return candidate.source_url
    if not candidate.request_headers:
        raise RuntimeError("ChatGPT image authorization was not captured from the conversation response.")

    response = context.request.get(
        candidate.source_url,
        timeout=CHATGPT_IMAGE_TIMEOUT_MS,
        headers=_chatgpt_image_request_headers(candidate),
    )
    if not response.ok:
        raise RuntimeError(f"ChatGPT file metadata request returned HTTP {response.status}.")
    try:
        payload = json.loads(response.text())
    except (AttributeError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("ChatGPT file metadata response was not valid JSON.") from exc

    source_url = str(payload.get("download_url") or "").strip() if isinstance(payload, dict) else ""
    parsed = urlsplit(source_url)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise RuntimeError("ChatGPT file metadata did not include a valid original image URL.")
    return source_url


def _request_chatgpt_image_with_refresh(context, candidate: ChatGPTImageCandidate):
    """Request an original image and refresh an expired direct URL through file metadata."""
    source_url = _resolve_chatgpt_image_source_url(context, candidate)
    resolved_candidate = (
        candidate
        if source_url == candidate.source_url
        else replace(candidate, source_url=source_url, request_headers={})
    )
    response = context.request.get(
        source_url,
        timeout=CHATGPT_IMAGE_TIMEOUT_MS,
        headers=_chatgpt_image_request_headers(candidate),
    )
    if (
        response.ok
        or _is_chatgpt_file_download_url(candidate.source_url)
        or not candidate.file_id.startswith("file_")
        or int(response.status) not in {401, 403, 404}
    ):
        return response, source_url, resolved_candidate

    refresh_candidate = replace(
        candidate,
        source_url=_chatgpt_file_download_url(candidate.file_id, candidate.conversation_url),
    )
    try:
        refreshed_source_url = _resolve_chatgpt_image_source_url(context, refresh_candidate)
    except RuntimeError:
        return response, source_url, resolved_candidate

    refreshed_response = context.request.get(
        refreshed_source_url,
        timeout=CHATGPT_IMAGE_TIMEOUT_MS,
        headers=_chatgpt_image_request_headers(refresh_candidate),
    )
    if not refreshed_response.ok:
        return response, source_url, resolved_candidate
    return (
        refreshed_response,
        refreshed_source_url,
        replace(candidate, source_url=refreshed_source_url, request_headers={}),
    )


def _is_retryable_chatgpt_image_error(error: Exception) -> bool:
    """Return whether an image request error is safe to retry with the same browser context."""
    error_text = str(error).lower()
    if re.search(r"\bhttp (?:401|403|408|429|5\d{2})\b", error_text):
        return True
    return any(
        marker in error_text
        for marker in (
            "non-image or incomplete image payload",
            "corrupt or incomplete image payload",
            "broken png",
            "cannot identify image file",
            "timeout",
            "timed out",
            "request state disappeared",
            "fetch is aborted",
            "load failed",
            "failed to fetch",
            "undefined is not an object",
            "resumed at byte",
            "did not honor the resume range",
            "bytes[index]",
            "connection reset",
            "connection closed",
            "socket disconnected",
            "socket hang up",
            "econnreset",
            "etimedout",
            "temporarily unavailable",
            "net::err_connection",
        )
    )


def _summarize_chatgpt_image_error(error: Exception) -> str:
    """Return a compact image failure summary without retaining signed source URLs."""
    error_text = str(error).strip().splitlines()[0] if str(error).strip() else error.__class__.__name__
    error_text = re.sub(r"https?://\S+", "<URL>", error_text, flags=re.IGNORECASE)
    error_text = re.sub(r"\b(Bearer|Basic)\s+\S+", r"\1 <redacted>", error_text, flags=re.IGNORECASE)
    return error_text[:500]


def _safari_chatgpt_image_headers(
    candidate: ChatGPTImageCandidate,
    source_url: str,
) -> dict[str, str]:
    """Keep bearer headers on first-party requests and signed URLs credential-free."""
    headers = _chatgpt_image_request_headers(candidate)
    if urlsplit(source_url).netloc.lower() == "chatgpt.com":
        return headers
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in CHATGPT_DOWNLOAD_AUTH_HEADER_NAMES
    }


def _download_chatgpt_image_via_safari(
    context: SafariContext,
    catalog: ChatGPTImageCatalog,
    target_dir: Path,
    candidate: ChatGPTImageCandidate,
    max_file_size_bytes: int,
) -> bool:
    """Stream one original image through the authenticated offscreen Safari page."""
    source_url = _resolve_chatgpt_image_source_url(context, candidate)
    resolved_candidate = (
        candidate
        if source_url == candidate.source_url
        else replace(candidate, source_url=source_url, request_headers={})
    )
    partial_dir = target_dir / CHATGPT_PARTIAL_DIRNAME
    partial_dir.mkdir(parents=True, exist_ok=True)
    partial_path = partial_dir / f"{sanitize_filename_part(candidate.file_id)}.part"

    try:
        content_type, _resumed = context.primary_page.download_to_path(
            source_url,
            partial_path,
            lambda: False,
            headers=_safari_chatgpt_image_headers(candidate, source_url),
        )
    except RuntimeError as original_error:
        download_error = original_error
        can_refresh = (
            not _is_chatgpt_file_download_url(candidate.source_url)
            and candidate.file_id.startswith("file_")
            and re.search(r"\bHTTP (?:401|403|404)\b", str(original_error))
        )
        if can_refresh:
            refresh_candidate = replace(
                candidate,
                source_url=_chatgpt_file_download_url(candidate.file_id, candidate.conversation_url),
            )
            try:
                refreshed_source_url = _resolve_chatgpt_image_source_url(context, refresh_candidate)
                content_type, _resumed = context.primary_page.download_to_path(
                    refreshed_source_url,
                    partial_path,
                    lambda: False,
                    headers=_safari_chatgpt_image_headers(refresh_candidate, refreshed_source_url),
                )
            except RuntimeError as refresh_error:
                download_error = refresh_error
            else:
                download_error = None
                source_url = refreshed_source_url
                resolved_candidate = replace(candidate, source_url=source_url, request_headers={})

        if download_error is not None:
            raise download_error

    content = partial_path.read_bytes()
    if max_file_size_bytes > 0 and len(content) > max_file_size_bytes:
        partial_path.unlink(missing_ok=True)
        raise ChatGPTImageSizeLimitError(
            f"ChatGPT image {candidate.file_id} exceeded the {max_file_size_bytes:,}-byte cache limit."
        )
    if (
        not content
        or content_type.lower().startswith(("text/", "application/json"))
        or not looks_like_image(content)
    ):
        partial_path.unlink(missing_ok=True)
        raise RuntimeError("ChatGPT returned a non-image or incomplete image payload.")
    if not image_payload_is_decodable(content):
        partial_path.unlink(missing_ok=True)
        raise RuntimeError("ChatGPT returned a corrupt or incomplete image payload.")

    extension = infer_image_extension(source_url, content_type, content)
    target_path = target_dir / f"img_{sanitize_filename_part(candidate.file_id)}{extension}"
    os.replace(partial_path, target_path)
    return catalog.register_download(
        candidate=resolved_candidate,
        relative_path=target_path.relative_to(target_dir).as_posix(),
        content_sha256=compute_sha256(content),
        content_bytes=len(content),
        seen_at=utc_now(),
    )


def download_chatgpt_image(
    context,
    catalog: ChatGPTImageCatalog,
    target_dir: Path,
    candidate: ChatGPTImageCandidate,
    max_file_size_bytes: int = 0,
) -> bool:
    """Download one original image through the authenticated browser context."""
    if not should_cache_chatgpt_candidate(candidate):
        return False
    if BrowserDeletionCatalog(_chatgpt_local_store_root(target_dir)).is_excluded("chatgpt", candidate.file_id):
        return False
    if not catalog.claim_download(candidate.file_id):
        catalog.update_metadata(candidate)
        return False

    try:
        for attempt_index in range(CHATGPT_IMAGE_DOWNLOAD_RETRY_LIMIT):
            try:
                if isinstance(context, SafariContext):
                    return _download_chatgpt_image_via_safari(
                        context,
                        catalog,
                        target_dir,
                        candidate,
                        max_file_size_bytes,
                    )
                response, source_url, resolved_candidate = _request_chatgpt_image_with_refresh(context, candidate)
                if not response.ok:
                    raise RuntimeError(f"ChatGPT image request returned HTTP {response.status}.")

                advertised_length = str(response.headers.get("content-length") or "").strip()
                if max_file_size_bytes > 0 and advertised_length.isdigit() and int(advertised_length) > max_file_size_bytes:
                    raise ChatGPTImageSizeLimitError(
                        f"ChatGPT image {candidate.file_id} is {int(advertised_length):,} bytes, above the "
                        f"{max_file_size_bytes:,}-byte cache limit."
                    )
                content = response.body()
                content_type = str(response.headers.get("content-type") or "")
                if (
                    not content
                    or content_type.lower().startswith(("text/", "application/json"))
                    or not looks_like_image(content)
                ):
                    raise RuntimeError("ChatGPT returned a non-image or incomplete image payload.")
                if max_file_size_bytes > 0 and len(content) > max_file_size_bytes:
                    raise ChatGPTImageSizeLimitError(
                        f"ChatGPT image {candidate.file_id} exceeded the {max_file_size_bytes:,}-byte cache limit."
                    )

                extension = infer_image_extension(source_url, content_type, content)
                filename = f"img_{sanitize_filename_part(candidate.file_id)}{extension}"
                target_path = target_dir / filename
                partial_dir = target_dir / CHATGPT_PARTIAL_DIRNAME
                partial_dir.mkdir(parents=True, exist_ok=True)
                partial_path = partial_dir / f"{sanitize_filename_part(candidate.file_id)}.part"
                partial_path.write_bytes(content)
                os.replace(partial_path, target_path)
                registered = catalog.register_download(
                    candidate=resolved_candidate,
                    relative_path=target_path.relative_to(target_dir).as_posix(),
                    content_sha256=compute_sha256(content),
                    content_bytes=len(content),
                    seen_at=utc_now(),
                )
                return registered
            except Exception as exc:
                should_retry = (
                    attempt_index + 1 < CHATGPT_IMAGE_DOWNLOAD_RETRY_LIMIT
                    and _is_retryable_chatgpt_image_error(exc)
                )
                if not should_retry:
                    raise
                logger.warning(
                    "Retrying a transient ChatGPT image request.",
                    extra={
                        "file_id": candidate.file_id,
                        "attempt": attempt_index + 1,
                        "max_attempts": CHATGPT_IMAGE_DOWNLOAD_RETRY_LIMIT,
                        "error": _summarize_chatgpt_image_error(exc),
                    },
                )
                time.sleep(CHATGPT_IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS * (attempt_index + 1))
    except Exception:
        catalog.release_download(candidate.file_id)
        raise

    catalog.release_download(candidate.file_id)
    raise RuntimeError("ChatGPT image request retry loop exited unexpectedly.")


def sync_chatgpt_images(
    state: TaskState,
    config: CrawlConfig | None = None,
    target_dir: Path | None = None,
    should_stop=lambda: False,
    content_mode: str = "media",
) -> ChatGPTSyncResult:
    """Cache ChatGPT text history or scoped/account media through the selected browser."""
    runtime_config = config or CrawlConfig()
    normalized_content_mode = "media" if content_mode == "media" else "text"
    descriptor = browser_descriptors(runtime_config).get(runtime_config.chatgpt_browser)
    if descriptor is None:
        raise RuntimeError(f"Unsupported ChatGPT browser: {runtime_config.chatgpt_browser}")
    if descriptor.engine not in {"chromium", "safari"}:
        raise RuntimeError(f"ChatGPT sync does not support {descriptor.label}.")
    if descriptor.engine == "chromium" and sync_playwright is None:
        raise RuntimeError(
            "Playwright is not installed. Run `python3 -m pip install -r requirements.txt` "
            "and `python3 -m playwright install chromium`."
        )

    project_name = runtime_config.chatgpt_project_name or DEFAULT_CHATGPT_PROJECT_NAME
    configured_project_url = runtime_config.chatgpt_project_url.strip()
    all_generated_media = normalized_content_mode == "media" and not configured_project_url
    project_url = (
        CHATGPT_HOME_URL
        if all_generated_media
        else configured_project_url or DEFAULT_CHATGPT_PROJECT_URL
    )
    direct_session_refresh = is_chatgpt_conversation_url(project_url)
    startup_timeout_seconds = min(
        max(
            float(
                runtime_config.chatgpt_text_startup_timeout_seconds
                if normalized_content_mode == "text"
                else runtime_config.chatgpt_startup_timeout_seconds
            ),
            MIN_CHATGPT_STARTUP_TIMEOUT_SECONDS,
        ),
        MAX_CHATGPT_STARTUP_TIMEOUT_SECONDS,
    )
    scan_wait_seconds = min(
        max(float(runtime_config.chatgpt_scan_wait_seconds), MIN_CHATGPT_SCAN_WAIT_SECONDS),
        MAX_CHATGPT_SCAN_WAIT_SECONDS,
    )
    resolved_target_dir = target_dir or chatgpt_target_dir(project_name)
    history_store = ChatGPTHistoryStore(chatgpt_history_path_for_target(resolved_target_dir))
    cached_count = 0
    if normalized_content_mode == "text":
        state.update(
            account_name="ChatGPT",
            output_dir=str(history_store.path.parent),
            downloaded_tweets=history_store.cached_messages,
            progress_unit="sessions",
        )
    else:
        catalog = ChatGPTImageCatalog.build(resolved_target_dir)
        repair_result = catalog.repair_result
        performance_metrics = PerformanceMetrics()
        cleanup_result = catalog.deduplicate_visual_duplicates(metrics=performance_metrics)
        cached_count = catalog.summarize()
        state.update(
            account_name=project_name,
            output_dir=str(resolved_target_dir),
            downloaded_posts=cached_count,
            downloaded_tweets=cached_count,
            discovered_images=cached_count,
            downloaded_images=cached_count,
            downloaded_videos=0,
            queued_tweets=0,
            processed_tweets=0,
            discovery_complete=False,
        )
        if performance_metrics.has_data:
            state.update(performance_metrics=performance_metrics.snapshot())
        state.append_event(f"Prepared ChatGPT cache with {cached_count:,} existing original images.")
        if repair_result.removed_count:
            state.append_event(
                f"Pruned {repair_result.removed_count:,} incomplete ChatGPT catalog records so their "
                "assets can be downloaded again."
            )
        if cleanup_result.removed_count:
            state.append_event(
                f"Removed {cleanup_result.removed_count:,} lower-quality duplicate ChatGPT images "
                f"and reclaimed {cleanup_result.reclaimed_bytes:,} bytes."
            )
    state.append_event(
        f"ChatGPT startup timeout: {startup_timeout_seconds:g} s; "
        f"scan wait: {scan_wait_seconds:g} s."
    )
    if should_stop():
        return ChatGPTSyncResult(cached_count=cached_count, cached_messages=history_store.cached_messages, stopped=True)

    try:
        if normalized_content_mode == "text":
            state.update(phase="collecting", progress_unit="sessions")
            state.append_event(
                f"Starting an offscreen {descriptor.label} session for all ChatGPT text history."
            )
            conversation_urls: list[str] = []
            history_processed = 0
            history_new_messages = 0
            history_unchanged_sessions = 0
            with _launch_chatgpt_browser_context(descriptor, CHATGPT_HOME_URL) as discovery_context:
                if discovery_context is not None:
                    page = _chatgpt_context_page(discovery_context)
                    open_chatgpt_page(
                        page,
                        CHATGPT_HOME_URL,
                        startup_timeout_seconds=startup_timeout_seconds,
                    )
                    request_headers = _load_chatgpt_session_request_headers(
                        discovery_context,
                        CHATGPT_HOME_URL,
                    )
                    conversation_titles_by_id: dict[str, str] = {}
                    conversation_revisions_by_id: dict[str, str] = {}
                    conversation_urls = _collect_all_chatgpt_conversation_urls_via_api(
                        discovery_context,
                        CHATGPT_HOME_URL,
                        request_headers,
                        state,
                        should_stop,
                        conversation_titles_by_id,
                        conversation_revisions_by_id,
                    )
                    history_processed, history_new_messages, history_unchanged_sessions = (
                        cache_chatgpt_conversation_history(
                            history_store,
                            conversation_urls,
                            page,
                            request_headers,
                            state,
                            should_stop,
                            scan_wait_seconds=runtime_config.cache_scan_wait("chatgpt", "text"),
                            conversation_titles_by_id=conversation_titles_by_id,
                            conversation_revisions_by_id=conversation_revisions_by_id,
                        )
                    )
            stopped = should_stop()
            history_failures = state.snapshot()["failed_tweets"]
            state.update(
                discovered_tweets=len(conversation_urls),
                queued_tweets=len(conversation_urls),
                processed_tweets=history_processed,
                failed_tweets=history_failures,
                downloaded_tweets=history_store.cached_messages,
                progress_unit="sessions",
                discovery_complete=not stopped,
            )
            state.append_event(
                f"Cached ChatGPT text history for {history_processed:,}/{len(conversation_urls):,} sessions "
                f"({history_store.cached_messages:,} messages, {history_new_messages:,} new, "
                f"{history_unchanged_sessions:,} unchanged)."
            )
            return ChatGPTSyncResult(
                discovered_conversations=len(conversation_urls),
                cached_count=cached_count,
                cached_messages=history_store.cached_messages,
                incomplete=not conversation_urls or history_processed < len(conversation_urls),
                failed_count=history_failures,
                stopped=stopped,
            )

        state.update(phase="collecting")
        project_request_headers: dict[str, str] = {}
        conversation_titles_by_id: dict[str, str] = {}
        project_index_candidates: list[ChatGPTImageCandidate] = []
        conversation_urls: list[str] = []
        if all_generated_media:
            state.append_event(
                f"Starting an offscreen {descriptor.label} session for all generated ChatGPT media. "
                "User-uploaded media will be excluded."
            )
        else:
            state.append_event(
                f"Starting an offscreen {descriptor.label} session for the authorized ChatGPT sync."
            )
        with _launch_chatgpt_browser_context(descriptor, project_url) as discovery_context:
            if discovery_context is not None:
                page = _chatgpt_context_page(discovery_context)
                if all_generated_media:
                    open_chatgpt_page(
                        page,
                        CHATGPT_HOME_URL,
                        startup_timeout_seconds=startup_timeout_seconds,
                    )
                    project_request_headers.update(
                        _load_chatgpt_session_request_headers(
                            discovery_context,
                            CHATGPT_HOME_URL,
                        )
                    )
                    conversation_urls = _collect_all_chatgpt_conversation_urls_via_api(
                        discovery_context,
                        CHATGPT_HOME_URL,
                        project_request_headers,
                        state,
                        should_stop,
                        conversation_titles_by_id,
                    )
                else:
                    conversation_urls = collect_project_conversation_urls(
                        page,
                        project_url,
                        state,
                        should_stop,
                        startup_timeout_seconds=startup_timeout_seconds,
                        scan_wait_seconds=scan_wait_seconds,
                        request_headers=project_request_headers,
                        conversation_titles_by_id=conversation_titles_by_id,
                    )
                if not all_generated_media and not should_stop() and not direct_session_refresh:
                    project_index_candidates = collect_chatgpt_project_index_images(
                        discovery_context,
                        project_url,
                        conversation_urls,
                        project_request_headers,
                        state,
                        should_stop,
                        conversation_titles_by_id,
                    )
                    project_index_candidates = catalog.merge_known_metadata(
                        project_index_candidates
                    )
                if direct_session_refresh:
                    state.append_event(
                        "Refreshing only the supplied ChatGPT session; skipping the global project image index."
                    )
                if project_index_candidates and not should_stop():
                    state.append_event(
                        "Reading complete ChatGPT conversation mappings to backfill image prompts."
                    )
                    project_index_candidates = enrich_chatgpt_project_index_prompts(
                        discovery_context,
                        project_url,
                        project_index_candidates,
                        project_request_headers,
                        state,
                        should_stop,
                        persist_batch=catalog.update_metadata_batch,
                        browser_page=page,
                        worker_count=(
                            1
                            if descriptor.engine == "safari"
                            else max(
                                1,
                                min(
                                    int(runtime_config.download_workers),
                                    CHATGPT_MAX_CONVERSATION_WORKERS,
                                ),
                            )
                        ),
                        skip_complete_conversations=True,
                    )
                state.update(
                    discovered_tweets=len(conversation_urls),
                    discovered_images=len(project_index_candidates),
                    queued_tweets=len(project_index_candidates) or len(conversation_urls),
                    processed_tweets=0,
                    progress_unit="images" if project_index_candidates else "sessions",
                )
                if all_generated_media:
                    state.append_event(
                        f"Found {len(conversation_urls):,} ChatGPT sessions across the account."
                    )
                else:
                    state.append_event(
                        f"Found {len(conversation_urls):,} ChatGPT sessions in the project."
                    )
                if project_index_candidates:
                    state.append_event(
                        f"Found {len(project_index_candidates):,} current original images in "
                        "ChatGPT's project image index."
                    )

        discovered_images = {candidate.file_id for candidate in project_index_candidates}
        metadata_updated_count = catalog.update_metadata_batch(project_index_candidates)
        if metadata_updated_count:
            state.append_event(
                f"Refreshed source timestamps and session metadata for {metadata_updated_count:,} "
                "known ChatGPT images."
            )
        downloaded_count = 0
        skipped_known = 0
        size_skipped_count = 0
        failed_count = 0
        processed_conversations = 0
        worker_count = max(1, min(int(runtime_config.download_workers), CHATGPT_MAX_CONVERSATION_WORKERS))
        if descriptor.engine == "safari":
            worker_count = CHATGPT_SAFARI_WORKER_COUNT
            state.append_event(
                "Using one serialized Safari media worker to preserve the offscreen page byte stream."
            )
        if direct_session_refresh:
            worker_count = 1
            state.append_event(
                f"Starting 1 ChatGPT worker in an isolated {descriptor.label} context for this session."
            )
        elif all_generated_media:
            state.append_event(
                f"Starting {worker_count} ChatGPT worker{'' if worker_count == 1 else 's'} "
                f"with one isolated {descriptor.label} context per worker. "
                "Only assistant-generated media will be cached."
            )
        else:
            state.append_event(
                f"Starting {worker_count} parallel ChatGPT worker{'' if worker_count == 1 else 's'} "
                f"with one isolated {descriptor.label} context per worker. "
                "Direct project-index downloads run first."
            )
        indexed_images_processed = 0
        for result in _iter_chatgpt_index_image_results(
            project_index_candidates,
            descriptor,
            catalog,
            resolved_target_dir,
            should_stop,
            worker_count,
            max_file_size_bytes=runtime_config.max_media_file_size_bytes,
        ):
            indexed_images_processed += 1
            downloaded_count += int(result.downloaded)
            skipped_known += int(result.skipped)
            if result.skipped_size:
                size_skipped_count += 1
                state.append_event(
                    f"Skipped ChatGPT project-index image {result.candidate_file_id}: above the "
                    f"{runtime_config.max_media_file_size_mib:,} MiB cache limit."
                )
            if result.error:
                failed_count += 1
                failure_summary = _summarize_chatgpt_image_error(RuntimeError(result.error))
                state.append_event(
                    f"Failed ChatGPT project-index image {result.candidate_file_id}: {failure_summary}"
                )
                logger.warning(
                    "ChatGPT project-index image failed after retries.",
                    extra={
                        "file_id": result.candidate_file_id,
                        "error": failure_summary,
                    },
                )

            cached_count = catalog.summarize()
            state.update(
                phase="downloading" if downloaded_count else "collecting",
                discovered_tweets=len(conversation_urls),
                discovered_images=len(discovered_images),
                queued_tweets=len(project_index_candidates) or len(conversation_urls),
                processed_tweets=indexed_images_processed if project_index_candidates else processed_conversations,
                downloaded_posts=cached_count,
                downloaded_tweets=cached_count,
                downloaded_images=cached_count,
                downloaded_videos=0,
                skipped_tweets=skipped_known,
                failed_tweets=failed_count,
            )
            if (
                indexed_images_processed == len(project_index_candidates)
                or indexed_images_processed % CHATGPT_RECENT_IMAGE_PAGE_SIZE == 0
            ):
                state.append_event(
                    f"Cached {indexed_images_processed:,}/{len(project_index_candidates):,} "
                    "direct ChatGPT project-index images."
                )

        if project_index_candidates:
            state.append_event(
                "ChatGPT's project image index is available; skipping legacy session scans "
                "that only surface unavailable historical assets."
            )
            conversation_results = ()
        elif should_stop():
            conversation_results = ()
        else:
            if all_generated_media:
                state.append_event(
                    "Starting all-account ChatGPT session scans for generated media."
                )
            else:
                state.append_event(
                    "Starting ChatGPT session scans because no project image index was available."
                )
            conversation_results = _iter_chatgpt_conversation_results(
                conversation_urls,
                descriptor,
                catalog,
                resolved_target_dir,
                startup_timeout_seconds,
                scan_wait_seconds,
                should_stop,
                worker_count,
                max_file_size_bytes=runtime_config.max_media_file_size_bytes,
                assistant_only=all_generated_media,
            )
        for result in conversation_results:
            processed_conversations += 1
            discovered_images.update(result.candidate_file_ids)
            downloaded_count += result.downloaded_count
            skipped_known += result.skipped_known
            size_skipped_count += result.oversized_count
            failed_count += result.failed_count
            if result.oversized_count:
                state.append_event(
                    f"Skipped {result.oversized_count:,} ChatGPT image(s) above the "
                    f"{runtime_config.max_media_file_size_mib:,} MiB cache limit."
                )
            if result.error:
                state.append_event(
                    f"Failed to inspect ChatGPT session {result.conversation_index:,}/{len(conversation_urls):,}: "
                    f"{result.error}"
                )
            for image_error in result.image_errors:
                state.append_event(f"Failed ChatGPT image {image_error}")

            cached_count = catalog.summarize()
            state.update(
                phase="downloading" if downloaded_count else "collecting",
                discovered_tweets=len(conversation_urls),
                discovered_images=len(discovered_images),
                queued_tweets=len(project_index_candidates) or len(conversation_urls),
                processed_tweets=processed_conversations,
                downloaded_posts=cached_count,
                downloaded_tweets=cached_count,
                downloaded_images=cached_count,
                downloaded_videos=0,
                skipped_tweets=skipped_known,
                failed_tweets=failed_count,
            )
            state.append_event(
                f"Scanned ChatGPT session {result.conversation_index:,}/{len(conversation_urls):,}; "
                f"found {len(result.candidate_file_ids):,} original images."
            )

        cached_count = catalog.summarize()
        stopped = should_stop()
        state.update(
            discovered_tweets=len(conversation_urls),
            discovered_images=len(discovered_images),
            queued_tweets=len(project_index_candidates) or len(conversation_urls),
            processed_tweets=indexed_images_processed if project_index_candidates else processed_conversations,
            discovery_complete=not stopped,
            downloaded_posts=cached_count,
            downloaded_tweets=cached_count,
            downloaded_images=cached_count,
            downloaded_videos=0,
            skipped_tweets=skipped_known,
            failed_tweets=failed_count,
        )
        return ChatGPTSyncResult(
            discovered_conversations=len(conversation_urls),
            discovered_images=len(discovered_images),
            downloaded_count=downloaded_count,
            skipped_known=skipped_known,
            skipped_size=size_skipped_count,
            failed_count=failed_count,
            cached_count=cached_count,
            cached_messages=history_store.cached_messages,
            stopped=stopped,
        )
    except PlaywrightError as exc:
        raise RuntimeError(f"ChatGPT browser automation failed: {exc}") from exc
