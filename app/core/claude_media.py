"""Cache rendered first-party Claude images through an owned Safari session.

Code version: v1.1.2-codex.0
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit

from PIL import Image, UnidentifiedImageError

from .browser_sessions import CLAUDE_HOME_URL, browser_descriptors, goto_with_retry
from .cache_timing import wait_for_cache_scan
from .claude_history import (
    CLAUDE_RENDER_SETTLE_MILLISECONDS,
    ClaudeConversationLink,
    _prepare_claude_conversation_for_rendering,
    _wait_for_claude_ready,
    discover_claude_conversations,
)
from .config import LOCAL_STORE_ROOT, CrawlConfig
from .local_media_browser import BrowserDeletionCatalog
from .safari_automation import SafariAuthenticationRequiredError, SafariContext
from .state import TaskSnapshot, TaskState


CLAUDE_MEDIA_RELATIVE_DIR = Path("media") / "claude"
CLAUDE_MEDIA_CATALOG_FILENAME = "catalog.json"
CLAUDE_MEDIA_SCHEMA_VERSION = 1
CLAUDE_MEDIA_RETRY_LIMIT = 2
CLAUDE_MAX_IMAGE_PIXELS = 40_000_000
CLAUDE_SUPPORTED_IMAGE_FORMATS = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "GIF": ".gif",
    "WEBP": ".webp",
}
CLAUDE_VOLATILE_QUERY_RE = re.compile(
    r"(?:^|[_-])(?:token|signature|sig|expires|expiry|exp|auth|jwt|key)(?:$|[_-])"
    r"|^(?:x-amz-|x-goog-|awsaccesskeyid|policy)",
    re.IGNORECASE,
)
CLAUDE_MEDIA_HOST_RE = re.compile(r"(?:[a-z0-9-]+\.)*claude\.ai\Z", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ClaudeImageCandidate:
    """One image actually rendered inside a Claude message body."""

    asset_id: str
    source_url: str
    source_fingerprint: str
    conversation_id: str
    conversation_url: str
    conversation_title: str
    message_index: int
    role: str
    alt_text: str


@dataclass(frozen=True, slots=True)
class ClaudeMediaSyncResult:
    """Report real downloaded image bytes separately from discovered references."""

    sessions: int = 0
    discovered_images: int = 0
    cached_images: int = 0
    downloaded_images: int = 0
    skipped_known: int = 0
    skipped_excluded: int = 0
    skipped_unsupported: int = 0
    failed_images: int = 0
    failed_sessions: int = 0
    stopped: bool = False
    skipped_size: int = 0

    @property
    def incomplete(self) -> bool:
        return self.failed_images > 0 or self.failed_sessions > 0


class ClaudeMediaSizeLimitError(RuntimeError):
    """Identify a configured size skip separately from a failed image transfer."""


def claude_media_dir(local_store_root: Path | str = LOCAL_STORE_ROOT) -> Path:
    """Return the dedicated Claude media directory inside the local store."""

    return Path(local_store_root).expanduser() / CLAUDE_MEDIA_RELATIVE_DIR


def _require_safe_store_path(path: Path, local_store_root: Path) -> None:
    """Reject links and escapes anywhere in a Claude media storage path."""

    path = path.expanduser().absolute()
    local_store_root = local_store_root.expanduser().absolute()
    try:
        path.relative_to(local_store_root)
    except ValueError as exc:
        raise RuntimeError("Claude media path escapes the local store.") from exc
    for component in (path, *path.parents):
        if component.is_symlink():
            raise RuntimeError("Claude media storage path contains a symbolic link.")
        if component == local_store_root:
            break
    for component in local_store_root.parents:
        if component.is_symlink():
            raise RuntimeError("Claude media storage path contains a symbolic link.")
    if not path.resolve(strict=False).is_relative_to(local_store_root.resolve(strict=False)):
        raise RuntimeError("Claude media path escapes the local store.")


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_claude_image_url(value: str) -> str:
    """Admit only Claude first-party HTTPS images; never fetch arbitrary page links."""

    candidate = str(value or "").strip()
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or not CLAUDE_MEDIA_HOST_RE.fullmatch(parsed.hostname)
        or port not in {None, 443}
        or parsed.username
        or parsed.password
        or not parsed.path.startswith("/")
    ):
        return ""
    return candidate


def _image_fingerprint(source_url: str) -> str:
    """Ignore expiring signatures while retaining stable first-party asset IDs."""

    parsed = urlsplit(source_url)
    stable_query = sorted(
        (key.lower(), value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=False)
        if not CLAUDE_VOLATILE_QUERY_RE.search(key)
    )
    identity = json.dumps(
        [parsed.hostname.lower(), parsed.path, stable_query],
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _candidate_from_rendered_image(
    raw: dict[str, Any],
    conversation: ClaudeConversationLink,
) -> ClaudeImageCandidate | None:
    source_url = _safe_claude_image_url(str(raw.get("source_url") or ""))
    if not source_url:
        return None
    try:
        message_index = int(raw.get("message_index"))
        image_index = int(raw.get("image_index"))
    except (TypeError, ValueError):
        return None
    if message_index < 0 or image_index < 0:
        return None
    role = str(raw.get("role") or "").strip().lower()
    if role not in {"user", "assistant"}:
        return None
    source_fingerprint = _image_fingerprint(source_url)
    identity = f"{conversation.conversation_id}:{message_index}:{image_index}:{source_fingerprint}"
    asset_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return ClaudeImageCandidate(
        asset_id=asset_id,
        source_url=source_url,
        source_fingerprint=source_fingerprint,
        conversation_id=conversation.conversation_id,
        conversation_url=conversation.url,
        conversation_title=conversation.title,
        message_index=message_index,
        role=role,
        alt_text=str(raw.get("alt_text") or "").strip()[:500],
    )


def discover_claude_rendered_images(
    page: Any,
    conversation: ClaudeConversationLink,
) -> tuple[list[ClaudeImageCandidate], int]:
    """Inspect loaded message bodies only; skip avatars, external URLs, blobs, and links."""

    payload = page.evaluate(
        r"""() => {
            const images = [];
            const articles = [...document.querySelectorAll('main [role="article"]')];
            articles.forEach((article, messageIndex) => {
                const userRoot = article.querySelector('[data-testid="user-message"]');
                const assistantRoot = article.querySelector('[data-cds="Prose"], .prose');
                const role = userRoot ? 'user' : assistantRoot ? 'assistant' : '';
                const body = userRoot || assistantRoot;
                if (!body || !role) return;
                [...body.querySelectorAll('img')].forEach((image, imageIndex) => {
                    images.push({
                        source_url: String(image.currentSrc || image.src || ''),
                        alt_text: String(image.getAttribute('alt') || ''),
                        message_index: messageIndex,
                        image_index: imageIndex,
                        role,
                    });
                });
            });
            return images;
        }"""
    )
    candidates: dict[str, ClaudeImageCandidate] = {}
    unsupported = 0
    for raw in payload if isinstance(payload, list) else []:
        if not isinstance(raw, dict):
            unsupported += 1
            continue
        candidate = _candidate_from_rendered_image(raw, conversation)
        if candidate is None:
            unsupported += 1
            continue
        candidates.setdefault(candidate.asset_id, candidate)
    return list(candidates.values()), unsupported


def _validated_image_extension(content: bytes) -> str:
    """Decode the complete image before assigning a local raster extension."""

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                if image.width * image.height > CLAUDE_MAX_IMAGE_PIXELS:
                    return ""
                image_format = str(image.format or "").upper()
                image.verify()
            extension = CLAUDE_SUPPORTED_IMAGE_FORMATS.get(image_format, "")
            if not extension:
                return ""
            with Image.open(io.BytesIO(content)) as image:
                image.load()
            return extension
    except (
        OSError,
        SyntaxError,
        UnidentifiedImageError,
        ValueError,
        Image.DecompressionBombWarning,
        Image.DecompressionBombError,
    ):
        return ""


class ClaudeMediaCatalog:
    """Persist image provenance atomically without storing signed download URLs."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().absolute()
        self.local_store_root = self.root.parent.parent
        self.path = self.root / CLAUDE_MEDIA_CATALOG_FILENAME
        self._records: dict[str, dict[str, Any]] = {}
        self._present_ids: set[str] = set()
        self._verified_signatures: dict[str, tuple[int, int, int, int, int]] = {}
        _require_safe_store_path(self.path, self.local_store_root)
        if self.path.exists():
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or payload.get("schema_version") != CLAUDE_MEDIA_SCHEMA_VERSION
                or not isinstance(payload.get("assets"), list)
            ):
                raise RuntimeError("Claude media catalog has an unsupported or invalid schema.")
            for raw in payload["assets"]:
                if not isinstance(raw, dict):
                    raise RuntimeError("Claude media catalog contains an invalid asset record.")
                asset_id = str(raw.get("asset_id") or "")
                relative_path = str(raw.get("relative_path") or "")
                if (
                    not re.fullmatch(r"[0-9a-f]{24}", asset_id)
                    or not re.fullmatch(r"img_[0-9a-f]{24}\.(?:jpg|png|gif|webp)", relative_path)
                    or relative_path != f"img_{asset_id}{Path(relative_path).suffix}"
                ):
                    raise RuntimeError("Claude media catalog contains an unsafe asset path.")
                self._records[asset_id] = dict(raw)
            self._present_ids = {
                asset_id
                for asset_id, record in self._records.items()
                if self._cached_record(record)
            }

    def _cached_record(self, record: dict[str, Any]) -> bool:
        path = self.root / str(record["relative_path"])
        _require_safe_store_path(path, self.local_store_root)
        try:
            return (
                path.is_file()
                and int(record.get("content_bytes") or 0) > 0
                and path.stat().st_size == int(record.get("content_bytes") or 0)
            )
        except (OSError, TypeError, ValueError):
            return False

    @property
    def cached_count(self) -> int:
        return len(self._present_ids)

    def contains(self, candidate: ClaudeImageCandidate) -> bool:
        record = self._records.get(candidate.asset_id)
        if (
            not record
            or record.get("source_fingerprint") != candidate.source_fingerprint
            or candidate.asset_id not in self._present_ids
        ):
            return False
        if not self._cached_record(record):
            self._present_ids.discard(candidate.asset_id)
            self._verified_signatures.pop(candidate.asset_id, None)
            return False
        path = self.root / str(record["relative_path"])
        stat_result = path.stat()
        signature = (
            stat_result.st_dev,
            stat_result.st_ino,
            stat_result.st_size,
            stat_result.st_mtime_ns,
            stat_result.st_ctime_ns,
        )
        if self._verified_signatures.get(candidate.asset_id) == signature:
            return True
        if hashlib.sha256(path.read_bytes()).hexdigest() == str(record.get("content_sha256") or ""):
            self._verified_signatures[candidate.asset_id] = signature
            return True
        self._present_ids.discard(candidate.asset_id)
        self._verified_signatures.pop(candidate.asset_id, None)
        return False

    def register(
        self,
        candidate: ClaudeImageCandidate,
        *,
        relative_path: str,
        content: bytes,
    ) -> None:
        if not re.fullmatch(r"img_[0-9a-f]{24}\.(?:jpg|png|gif|webp)", relative_path):
            raise RuntimeError("Claude media asset path is invalid.")
        if relative_path != f"img_{candidate.asset_id}{Path(relative_path).suffix}":
            raise RuntimeError("Claude media asset path does not match its identity.")
        path = self.root / relative_path
        _require_safe_store_path(path, self.local_store_root)
        self._records[candidate.asset_id] = {
            "asset_id": candidate.asset_id,
            "relative_path": relative_path,
            "media_kind": "image",
            "conversation_id": candidate.conversation_id,
            "conversation_url": candidate.conversation_url,
            "conversation_title": candidate.conversation_title,
            "message_index": candidate.message_index,
            "role": candidate.role,
            "alt_text": candidate.alt_text,
            "source_fingerprint": candidate.source_fingerprint,
            "content_sha256": hashlib.sha256(content).hexdigest(),
            "content_bytes": len(content),
            "cached_at": _utc_now(),
        }
        self.save()
        self._present_ids.add(candidate.asset_id)
        stat_result = path.stat()
        self._verified_signatures[candidate.asset_id] = (
            stat_result.st_dev,
            stat_result.st_ino,
            stat_result.st_size,
            stat_result.st_mtime_ns,
            stat_result.st_ctime_ns,
        )

    def save(self) -> None:
        _require_safe_store_path(self.root, self.local_store_root)
        self.root.mkdir(parents=True, exist_ok=True)
        _require_safe_store_path(self.path, self.local_store_root)
        payload = {
            "schema_version": CLAUDE_MEDIA_SCHEMA_VERSION,
            "assets": sorted(self._records.values(), key=lambda row: row["asset_id"]),
        }
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.root,
            prefix=".catalog.",
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            _require_safe_store_path(self.path, self.local_store_root)
            os.replace(handle.name, self.path)
        finally:
            Path(handle.name).unlink(missing_ok=True)


def build_claude_media_initial_snapshot(
    version: str,
    local_store_root: Path | str = LOCAL_STORE_ROOT,
) -> TaskSnapshot:
    """Hydrate Claude Media from present, size-matched image files."""

    catalog = ClaudeMediaCatalog(claude_media_dir(local_store_root))
    count = catalog.cached_count
    return TaskSnapshot(
        version=version,
        account_name="Claude",
        output_dir=str(catalog.root),
        progress_unit="images",
        downloaded_posts=count,
        downloaded_tweets=count,
        downloaded_images=count,
        discovered_images=count,
        message=f"Ready. Found {count:,} cached Claude rendered-image files.",
    )


def _download_claude_image(
    page: Any,
    catalog: ClaudeMediaCatalog,
    candidate: ClaudeImageCandidate,
    should_stop: Callable[[], bool],
    max_bytes: int,
) -> bool:
    """Save one authenticated first-party image only after full byte validation."""

    if catalog.contains(candidate):
        return False
    partial_dir = catalog.root / ".partial"
    partial_path = partial_dir / f"{candidate.asset_id}.part"
    _require_safe_store_path(partial_dir, catalog.local_store_root)
    partial_dir.mkdir(parents=True, exist_ok=True)
    _require_safe_store_path(partial_path, catalog.local_store_root)
    try:
        try:
            content_type, _resumed = page.download_to_path(
                candidate.source_url,
                partial_path,
                should_stop,
                max_bytes=max_bytes,
            )
        except RuntimeError as exc:
            if max_bytes > 0 and re.fullmatch(
                r"(?:Safari media request failed: )?Safari media exceeds the "
                r"(?:configured|[\d,]+-byte) cache limit\.",
                str(exc),
            ):
                raise ClaudeMediaSizeLimitError(
                    "Claude image exceeded the configured media size limit."
                ) from exc
            raise
        _require_safe_store_path(partial_path, catalog.local_store_root)
        size = partial_path.stat().st_size
        if size <= 0:
            raise RuntimeError("Claude returned an empty image response.")
        if size > max_bytes:
            raise ClaudeMediaSizeLimitError("Claude image exceeded the configured media size limit.")
        if str(content_type or "").lower().startswith(("text/", "application/json")):
            raise RuntimeError("Claude returned a non-image response.")
        content = partial_path.read_bytes()
        extension = _validated_image_extension(content)
        if not extension:
            raise RuntimeError("Claude returned an unsupported or incomplete raster image.")
        relative_path = f"img_{candidate.asset_id}{extension}"
        destination = catalog.root / relative_path
        _require_safe_store_path(destination, catalog.local_store_root)
        os.replace(partial_path, destination)
        catalog.register(candidate, relative_path=relative_path, content=content)
        return True
    finally:
        _require_safe_store_path(partial_path, catalog.local_store_root)
        partial_path.unlink(missing_ok=True)


def sync_claude_media(
    state: TaskState,
    config: CrawlConfig,
    should_stop: Callable[[], bool],
    local_store_root: Path | str = LOCAL_STORE_ROOT,
) -> ClaudeMediaSyncResult:
    """Cache real rendered Claude image files from a task-owned Safari window."""

    descriptor = browser_descriptors(config).get(config.claude_browser)
    if descriptor is None:
        raise RuntimeError(f"Unsupported Claude browser: {config.claude_browser}")
    if descriptor.engine != "safari":
        raise RuntimeError("Claude rendered-image cache currently requires Safari.")

    catalog = ClaudeMediaCatalog(claude_media_dir(local_store_root))
    deletion_catalog = BrowserDeletionCatalog(catalog.local_store_root)
    state.update(
        phase="collecting",
        progress_unit="images",
        account_name="Claude",
        output_dir=str(catalog.root),
        downloaded_posts=catalog.cached_count,
        downloaded_tweets=catalog.cached_count,
        downloaded_images=catalog.cached_count,
        message="Opening authenticated Claude messages to discover rendered images...",
    )
    state.append_event("Claude Media caches supported first-party raster images rendered in message bodies.")

    discovered_images = 0
    downloaded_images = 0
    skipped_known = 0
    skipped_excluded = 0
    skipped_unsupported = 0
    skipped_size = 0
    failed_images = 0
    failed_sessions = 0
    processed_images = 0
    processed_sessions = 0
    stopped = False
    with SafariContext(CLAUDE_HOME_URL, lock_blocking=False) as context:
        page = context.primary_page
        goto_with_retry(page, CLAUDE_HOME_URL, attempts=3, timeout_ms=90_000, should_stop=should_stop)
        if should_stop():
            stopped = True
            conversations: list[ClaudeConversationLink] = []
        else:
            _wait_for_claude_ready(page)
            conversations = discover_claude_conversations(page, should_stop=should_stop)
            stopped = should_stop()
            if not conversations and not stopped:
                raise RuntimeError(
                    "Claude media discovery returned no rendered sessions. "
                    "The authenticated Chats page may not have loaded."
                )
            if not stopped:
                state.update(
                    phase="downloading",
                    discovered_tweets=len(conversations),
                    discovery_complete=False,
                    message=f"Found {len(conversations):,} Claude sessions; caching rendered images...",
                )
                state.append_event(f"Found {len(conversations):,} Claude sessions in Safari.")

        for index, conversation in enumerate(conversations, start=1):
            if should_stop():
                stopped = True
                break
            if index > 1 and wait_for_cache_scan(
                config.cache_scan_wait("claude", "media"), should_stop
            ):
                stopped = True
                break
            try:
                goto_with_retry(
                    page, conversation.url, attempts=2, timeout_ms=90_000, should_stop=should_stop
                )
                if should_stop():
                    stopped = True
                    break
                page.wait_for_timeout(CLAUDE_RENDER_SETTLE_MILLISECONDS)
                _prepare_claude_conversation_for_rendering(page)
                candidates, unsupported = discover_claude_rendered_images(page, conversation)
            except Exception as exc:
                failed_sessions += 1
                state.append_event(
                    f"Failed to inspect Claude session {index:,}/{len(conversations):,}: "
                    f"{type(exc).__name__}."
                )
                continue
            processed_sessions = index
            skipped_unsupported += unsupported
            discovered_images += len(candidates)
            state.update(
                discovered_images=discovered_images,
                queued_tweets=discovered_images,
                processed_tweets=processed_images,
                downloaded_images=catalog.cached_count,
                failed_tweets=failed_images + failed_sessions,
            )
            for candidate in candidates:
                if should_stop():
                    stopped = True
                    break
                if deletion_catalog.is_excluded("claude", candidate.asset_id):
                    skipped_excluded += 1
                    processed_images += 1
                    state.update(
                        processed_tweets=processed_images,
                        skipped_tweets=skipped_known + skipped_excluded + skipped_size,
                    )
                    continue
                if catalog.contains(candidate):
                    skipped_known += 1
                    processed_images += 1
                    state.update(
                        processed_tweets=processed_images,
                        skipped_tweets=skipped_known + skipped_excluded + skipped_size,
                    )
                    continue
                last_error: Exception | None = None
                for _attempt in range(CLAUDE_MEDIA_RETRY_LIMIT):
                    try:
                        if _download_claude_image(
                            page,
                            catalog,
                            candidate,
                            should_stop,
                            config.max_media_file_size_bytes,
                        ):
                            downloaded_images += 1
                        last_error = None
                        break
                    except SafariAuthenticationRequiredError:
                        raise
                    except ClaudeMediaSizeLimitError:
                        skipped_size += 1
                        last_error = None
                        state.append_event(
                            f"Skipped Claude image {candidate.asset_id} above the "
                            f"{config.max_media_file_size_mib:,} MiB cache limit."
                        )
                        break
                    except Exception as exc:
                        last_error = exc
                        if should_stop():
                            stopped = True
                            break
                if last_error is not None and not stopped:
                    failed_images += 1
                    state.append_event(
                        f"Failed Claude image {candidate.asset_id} in session "
                        f"{index:,}/{len(conversations):,}: {type(last_error).__name__}."
                    )
                processed_images += 1
                state.update(
                    processed_tweets=processed_images,
                    downloaded_posts=catalog.cached_count,
                    downloaded_tweets=catalog.cached_count,
                    downloaded_images=catalog.cached_count,
                    skipped_tweets=skipped_known + skipped_excluded + skipped_size,
                    failed_tweets=failed_images + failed_sessions,
                )
            if stopped:
                break

    stopped = stopped or should_stop()
    result = ClaudeMediaSyncResult(
        sessions=processed_sessions,
        discovered_images=discovered_images,
        cached_images=catalog.cached_count,
        downloaded_images=downloaded_images,
        skipped_known=skipped_known,
        skipped_excluded=skipped_excluded,
        skipped_unsupported=skipped_unsupported,
        failed_images=failed_images,
        failed_sessions=failed_sessions,
        stopped=stopped,
        skipped_size=skipped_size,
    )
    message = (
        f"{'Stopped' if stopped else 'Finished'} Claude rendered-image cache after "
        f"{processed_sessions:,}/{len(conversations):,} sessions: "
        f"{discovered_images:,} eligible images, {downloaded_images:,} new files, "
        f"{skipped_known:,} already cached, {skipped_excluded:,} excluded by deletion, "
        f"{skipped_size:,} over the size limit, "
        f"{skipped_unsupported:,} unsupported references, "
        f"{failed_images + failed_sessions:,} failures; {result.cached_images:,} recorded files present. "
        "Other attachments and videos are outside this image-only mode."
    )
    state.update(
        phase="stopped" if stopped else ("failed" if result.incomplete else "completed"),
        discovered_tweets=len(conversations),
        discovery_complete=not stopped,
        discovered_images=discovered_images,
        queued_tweets=discovered_images,
        processed_tweets=processed_images,
        downloaded_posts=result.cached_images,
        downloaded_tweets=result.cached_images,
        downloaded_images=result.cached_images,
        skipped_tweets=skipped_known + skipped_excluded + skipped_size,
        failed_tweets=failed_images + failed_sessions,
        message=message,
    )
    state.append_event(message)
    return result
