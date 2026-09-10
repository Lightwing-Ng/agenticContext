"""Read-through Parquet cache for Web Agent source discovery.

Code version: v2.1.7-codex.1
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import re
from threading import Condition, RLock, Thread
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pyarrow as pa

from .resource_persistence import read_parquet_rows, write_parquet_rows_atomic


AGENT_SOURCE_CACHE_FILENAME = "agent_source_catalog.parquet"
AGENT_SOURCE_CACHE_SCHEMA_VERSION = 1
AGENT_SOURCE_CACHE_TTL_SECONDS = 15 * 60
AGENT_SOURCE_CACHE_RETRY_COOLDOWN_SECONDS = 60
AGENT_SOURCE_CACHE_MAX_CLOCK_SKEW_SECONDS = 5 * 60

AGENT_SOURCE_CACHE_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.int16(), nullable=False),
        pa.field("cache_key", pa.string(), nullable=False),
        pa.field("platform", pa.string(), nullable=False),
        pa.field("browser", pa.string(), nullable=False),
        pa.field("source_kind", pa.string(), nullable=False),
        pa.field("project_url", pa.string(), nullable=False),
        pa.field("cached_at", pa.string(), nullable=False),
        pa.field("payload_json", pa.string(), nullable=False),
    ]
)

LOGGER = logging.getLogger(__name__)
CHATGPT_PROJECT_PATH_PATTERN = re.compile(
    r"^/g/(g-p-[0-9a-f]{32})(?:-[^/]*)?/project/?$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class AgentSourceCacheKey:
    """Canonical identity for one provider/browser source catalog."""

    platform: str
    browser: str
    source_kind: str
    project_url: str = ""

    @classmethod
    def from_values(
        cls,
        platform: str,
        browser: str,
        source_kind: str,
        project_url: str = "",
    ) -> "AgentSourceCacheKey":
        """Normalize every cache dimension before it reaches memory or disk."""
        return cls(
            platform=str(platform or "").strip().lower(),
            browser=str(browser or "").strip().lower(),
            source_kind=str(source_kind or "").strip().lower(),
            project_url=_canonical_project_url(project_url, platform=platform),
        )

    @property
    def serialized(self) -> str:
        """Return a stable opaque key for the Parquet row."""
        return json.dumps(
            [self.platform, self.browser, self.source_kind, self.project_url],
            ensure_ascii=False,
            separators=(",", ":"),
        )


@dataclass(frozen=True, slots=True)
class AgentSourceCacheEntry:
    """One decoded Agent source cache entry."""

    payload: dict[str, Any]
    cached_at: datetime

    def is_fresh(self, now: datetime, ttl_seconds: int) -> bool:
        """Return whether this entry is still within the reuse window."""
        current_time = _as_utc(now)
        cached_at = _as_utc(self.cached_at)
        if _cache_timestamp_exceeds_clock_skew(cached_at, current_time):
            return False
        return current_time <= _cache_expiration(cached_at, ttl_seconds)


class AgentSourceCache:
    """Serve Agent catalogs from memory, Parquet, or the authenticated browser.

    Fresh reads are L1 memory hits after the first catalog load. Callers can
    return expired catalogs without revalidating them, so passive UI reads do
    not launch a browser. Explicit refreshes remain synchronous so the caller
    receives the newest available catalog or an observable stale fallback.
    """

    def __init__(
        self,
        local_store_root: Path | str,
        *,
        ttl_seconds: int = AGENT_SOURCE_CACHE_TTL_SECONDS,
    ) -> None:
        self.local_store_root = Path(local_store_root).expanduser().resolve(strict=False)
        self.ttl_seconds = max(0, int(ttl_seconds))
        self._condition = Condition(RLock())
        self._entries: dict[AgentSourceCacheKey, AgentSourceCacheEntry] = {}
        self._disk_loaded_keys: set[AgentSourceCacheKey] = set()
        self._refreshing: set[AgentSourceCacheKey] = set()
        self._refresh_failed_at: dict[AgentSourceCacheKey, datetime] = {}
        self._publication_generation: dict[AgentSourceCacheKey, int] = {}
        self._catalog_loaded = False

    def store(
        self,
        *,
        platform: str,
        browser: str,
        source_kind: str,
        payload: dict[str, Any],
        project_url: str = "",
        now: datetime | None = None,
    ) -> None:
        """Publish an already collected catalog into L1 and the Parquet L2 cache."""
        key = AgentSourceCacheKey.from_values(platform, browser, source_kind, project_url)
        cached_at = _cache_write_timestamp(now)
        with self._condition:
            self._load_catalog_locked()
            current = self._entries.get(key)
            current_is_valid = bool(
                current is not None
                and not _cache_timestamp_exceeds_clock_skew(
                    current.cached_at,
                    _utc_now(),
                )
            )
            if current_is_valid and current is not None and cached_at < current.cached_at:
                return
            self._entries[key] = AgentSourceCacheEntry(
                payload=dict(payload),
                cached_at=cached_at,
            )
            self._publication_generation[key] = (
                self._publication_generation.get(key, 0) + 1
            )
            self._disk_loaded_keys.discard(key)
            self._refresh_failed_at.pop(key, None)
            try:
                self._persist_catalog_locked()
            except (OSError, RuntimeError, pa.ArrowException) as exc:
                LOGGER.warning("Could not persist Agent source cache: %s", exc)
            self._condition.notify_all()

    def get_or_collect(
        self,
        *,
        platform: str,
        browser: str,
        source_kind: str,
        project_url: str = "",
        collector: Callable[[], dict[str, Any]],
        force_refresh: bool = False,
        now: datetime | None = None,
        stale_while_revalidate: bool = True,
        collect_on_miss: bool = True,
    ) -> dict[str, Any]:
        """Return a cached catalog or collect it through one coalesced flight."""
        key = AgentSourceCacheKey.from_values(platform, browser, source_kind, project_url)
        requested_now = _cache_write_timestamp(now) if now is not None else None

        with self._condition:
            self._load_catalog_locked()
            cached = self._entries.get(key)
            current_time = requested_now or _utc_now()
            if cached and not force_refresh and cached.is_fresh(current_time, self.ttl_seconds):
                layer = self._consume_cache_layer_locked(key)
                return _with_cache_metadata(
                    cached.payload,
                    status="hit",
                    layer=layer,
                    cached_at=cached.cached_at,
                    now=current_time,
                    ttl_seconds=self.ttl_seconds,
                )

            if cached and not force_refresh:
                refresh_started = False
                if (
                    stale_while_revalidate
                    and not self._refresh_cooldown_active_locked(key, current_time)
                ):
                    refresh_started = self._start_background_refresh_locked(key, collector)
                layer = self._consume_cache_layer_locked(key)
                return _with_cache_metadata(
                    cached.payload,
                    status="stale",
                    layer=layer,
                    cached_at=cached.cached_at,
                    now=current_time,
                    ttl_seconds=self.ttl_seconds,
                    refresh_in_progress=refresh_started or key in self._refreshing,
                )

            if not collect_on_miss and not force_refresh:
                return _with_cache_metadata(
                    {},
                    status="unprobed",
                    layer="none",
                    cached_at=None,
                    now=current_time,
                    ttl_seconds=self.ttl_seconds,
                )

            while key in self._refreshing:
                self._condition.wait()
                cached = self._entries.get(key)
                current_time = requested_now or _utc_now()
                if cached and cached.is_fresh(current_time, self.ttl_seconds):
                    return _with_cache_metadata(
                        cached.payload,
                        status="hit",
                        layer="memory",
                        cached_at=cached.cached_at,
                        now=current_time,
                        ttl_seconds=self.ttl_seconds,
                    )

            self._refreshing.add(key)
            refresh_generation = self._publication_generation.get(key, 0)

        return self._collect_and_store(
            key,
            collector,
            cached,
            refresh_generation=refresh_generation,
            requested_now=requested_now,
        )

    def _collect_and_store(
        self,
        key: AgentSourceCacheKey,
        collector: Callable[[], dict[str, Any]],
        cached: AgentSourceCacheEntry | None,
        *,
        refresh_generation: int,
        requested_now: datetime | None,
    ) -> dict[str, Any]:
        """Run one browser collection outside the state lock and publish it atomically."""
        try:
            payload = dict(collector())
        except (RuntimeError, ValueError):
            current, superseded = self._fail_refresh(
                key,
                refresh_generation=refresh_generation,
                requested_now=requested_now,
            )
            fallback = current or cached
            if fallback:
                return _with_cache_metadata(
                    fallback.payload,
                    status="hit" if superseded and current is not None else "stale",
                    layer="memory",
                    cached_at=fallback.cached_at,
                    now=requested_now or _utc_now(),
                    ttl_seconds=self.ttl_seconds,
                )
            raise
        except Exception:
            self._fail_refresh(
                key,
                refresh_generation=refresh_generation,
                requested_now=requested_now,
            )
            raise

        cached_at = _cache_write_timestamp(requested_now)
        with self._condition:
            current = self._entries.get(key)
            current_is_valid = bool(
                current is not None
                and not _cache_timestamp_exceeds_clock_skew(
                    current.cached_at,
                    _utc_now(),
                )
            )
            publish = self._publication_generation.get(key, 0) == refresh_generation
            if publish:
                publication_time = (
                    max(cached_at, current.cached_at)
                    if current_is_valid and current is not None
                    else cached_at
                )
                current = AgentSourceCacheEntry(
                    payload=payload,
                    cached_at=publication_time,
                )
                self._entries[key] = current
                self._publication_generation[key] = refresh_generation + 1
                self._refresh_failed_at.pop(key, None)
                try:
                    self._persist_catalog_locked()
                except (OSError, RuntimeError, pa.ArrowException) as exc:
                    LOGGER.warning("Could not persist Agent source cache: %s", exc)
            self._finish_refresh_locked(key)

        selected = current or AgentSourceCacheEntry(payload=payload, cached_at=cached_at)
        return _with_cache_metadata(
            selected.payload,
            status=("refreshed" if cached else "miss") if publish else "hit",
            layer="memory",
            cached_at=selected.cached_at,
            now=requested_now or _utc_now(),
            ttl_seconds=self.ttl_seconds,
        )

    def _start_background_refresh_locked(
        self,
        key: AgentSourceCacheKey,
        collector: Callable[[], dict[str, Any]],
    ) -> bool:
        """Start at most one daemon refresh for a stale catalog key."""
        if key in self._refreshing:
            return False
        self._refreshing.add(key)
        refresh_generation = self._publication_generation.get(key, 0)
        Thread(
            target=self._run_background_refresh,
            args=(key, collector, refresh_generation),
            name=f"agent-source-refresh-{key.platform}-{key.browser}",
            daemon=True,
        ).start()
        return True

    def _run_background_refresh(
        self,
        key: AgentSourceCacheKey,
        collector: Callable[[], dict[str, Any]],
        refresh_generation: int,
    ) -> None:
        """Refresh a stale key without delaying the page that served stale data."""
        with self._condition:
            cached = self._entries.get(key)
        try:
            self._collect_and_store(
                key,
                collector,
                cached,
                refresh_generation=refresh_generation,
                requested_now=None,
            )
        except Exception as exc:
            LOGGER.warning("Background Agent source refresh failed for %s: %s", key.serialized, exc)

    def _fail_refresh(
        self,
        key: AgentSourceCacheKey,
        *,
        refresh_generation: int,
        requested_now: datetime | None,
    ) -> tuple[AgentSourceCacheEntry | None, bool]:
        """Record a short retry cooldown before releasing a failed refresh slot."""
        with self._condition:
            superseded = (
                self._publication_generation.get(key, 0) != refresh_generation
            )
            if not superseded:
                self._refresh_failed_at[key] = requested_now or _utc_now()
            current = self._entries.get(key)
            self._finish_refresh_locked(key)
            return current, superseded

    def _finish_refresh_locked(self, key: AgentSourceCacheKey) -> None:
        """Release a refresh slot while the state lock is held."""
        self._refreshing.discard(key)
        self._condition.notify_all()

    def _refresh_cooldown_active_locked(
        self,
        key: AgentSourceCacheKey,
        now: datetime,
    ) -> bool:
        """Avoid repeatedly starting a browser refresh after a recent failure."""
        failed_at = self._refresh_failed_at.get(key)
        if failed_at is None:
            return False
        if now - failed_at < timedelta(seconds=AGENT_SOURCE_CACHE_RETRY_COOLDOWN_SECONDS):
            return True
        self._refresh_failed_at.pop(key, None)
        return False

    def _load_catalog_locked(self) -> None:
        """Load the durable catalog once into the process-local L1 cache."""
        if self._catalog_loaded:
            return
        self._catalog_loaded = True
        loaded_at = _utc_now()
        rows = read_parquet_rows(agent_source_cache_path(self.local_store_root)) or []
        for row in rows:
            if row.get("schema_version") != AGENT_SOURCE_CACHE_SCHEMA_VERSION:
                continue
            try:
                key = AgentSourceCacheKey.from_values(
                    row["platform"],
                    row["browser"],
                    row["source_kind"],
                    row.get("project_url", ""),
                )
                payload = json.loads(str(row["payload_json"]))
                cached_at = _as_utc(datetime.fromisoformat(str(row["cached_at"])))
            except (
                KeyError,
                TypeError,
                ValueError,
                OverflowError,
                json.JSONDecodeError,
            ):
                continue
            if (
                not isinstance(payload, dict)
                or _cache_timestamp_exceeds_clock_skew(cached_at, loaded_at)
            ):
                continue
            current = self._entries.get(key)
            if current is None or cached_at >= current.cached_at:
                self._entries[key] = AgentSourceCacheEntry(
                    payload=dict(payload),
                    cached_at=cached_at,
                )
                self._disk_loaded_keys.add(key)

    def _consume_cache_layer_locked(self, key: AgentSourceCacheKey) -> str:
        """Report Parquet only for the first L2 read before using the L1 copy."""
        if key in self._disk_loaded_keys:
            self._disk_loaded_keys.discard(key)
            return "parquet"
        return "memory"

    def _persist_catalog_locked(self) -> None:
        """Persist the complete small catalog with one atomic Parquet replacement."""
        rows = [
            {
                "schema_version": AGENT_SOURCE_CACHE_SCHEMA_VERSION,
                "cache_key": key.serialized,
                "platform": key.platform,
                "browser": key.browser,
                "source_kind": key.source_kind,
                "project_url": key.project_url,
                "cached_at": _as_utc(entry.cached_at).isoformat(),
                "payload_json": json.dumps(
                    entry.payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
            for key, entry in sorted(self._entries.items(), key=lambda item: item[0].serialized)
        ]
        write_parquet_rows_atomic(
            agent_source_cache_path(self.local_store_root),
            rows,
            AGENT_SOURCE_CACHE_SCHEMA,
        )


_SHARED_CACHES: dict[Path, AgentSourceCache] = {}
_SHARED_CACHES_LOCK = RLock()


def get_or_collect_agent_source(
    *,
    local_store_root: Path | str,
    platform: str,
    browser: str,
    source_kind: str,
    project_url: str = "",
    collector: Callable[[], dict[str, Any]],
    force_refresh: bool = False,
    now: datetime | None = None,
    ttl_seconds: int = AGENT_SOURCE_CACHE_TTL_SECONDS,
    stale_while_revalidate: bool = True,
    collect_on_miss: bool = True,
) -> dict[str, Any]:
    """Use a shared process-local cache for callers outside the Flask app."""
    root = Path(local_store_root).expanduser().resolve(strict=False)
    with _SHARED_CACHES_LOCK:
        cache = _SHARED_CACHES.get(root)
        if cache is None or cache.ttl_seconds != max(0, int(ttl_seconds)):
            cache = AgentSourceCache(root, ttl_seconds=ttl_seconds)
            _SHARED_CACHES[root] = cache
    return cache.get_or_collect(
        platform=platform,
        browser=browser,
        source_kind=source_kind,
        project_url=project_url,
        collector=collector,
        force_refresh=force_refresh,
        now=now,
        stale_while_revalidate=stale_while_revalidate,
        collect_on_miss=collect_on_miss,
    )


def agent_source_cache_path(local_store_root: Path | str) -> Path:
    """Return the shared Parquet path used by all Agent source adapters."""
    return Path(local_store_root) / "agent" / AGENT_SOURCE_CACHE_FILENAME


def _with_cache_metadata(
    payload: dict[str, Any],
    *,
    status: str,
    layer: str,
    cached_at: datetime | None,
    now: datetime,
    ttl_seconds: int,
    refresh_in_progress: bool = False,
) -> dict[str, Any]:
    """Add operational cache metadata without mutating the stored provider payload."""
    result = dict(payload)
    normalized_now = _as_utc(now)
    normalized_cached_at = _as_utc(cached_at) if cached_at is not None else None
    expires_at = (
        _cache_expiration(normalized_cached_at, ttl_seconds)
        if normalized_cached_at is not None
        else None
    )
    result["cache"] = {
        "status": status,
        "layer": layer,
        "cached_at": normalized_cached_at.isoformat() if normalized_cached_at is not None else "",
        "expires_at": _as_utc(expires_at).isoformat() if expires_at is not None else "",
        "age_seconds": (
            max(0, int((normalized_now - normalized_cached_at).total_seconds()))
            if normalized_cached_at is not None
            else 0
        ),
        "browser_check_required": status in {"miss", "refreshed", "stale", "unprobed"},
        "refresh_in_progress": refresh_in_progress,
        "ttl_seconds": ttl_seconds,
    }
    return result


def _canonical_project_url(value: str, *, platform: str = "") -> str:
    """Normalize equivalent Project URLs into one cache identity."""
    candidate = str(value or "").strip()
    if not candidate:
        return ""
    try:
        parsed = urlsplit(candidate)
        if not parsed.scheme or not parsed.netloc:
            return candidate
        if parsed.username or parsed.password:
            return candidate
        hostname = (parsed.hostname or "").lower()
        port = f":{parsed.port}" if parsed.port else ""
        if (
            str(platform or "").strip().lower() == "chatgpt"
            and parsed.scheme.lower() == "https"
            and hostname in {"chatgpt.com", "www.chatgpt.com"}
            and parsed.port in {None, 443}
            and (match := CHATGPT_PROJECT_PATH_PATTERN.fullmatch(parsed.path))
        ):
            return f"https://chatgpt.com/g/{match.group(1).lower()}/project"
        query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
        return urlunsplit(
            (
                parsed.scheme.lower(),
                f"{hostname}{port}",
                parsed.path.rstrip("/") or "/",
                query,
                "",
            )
        )
    except ValueError:
        return candidate


def _utc_now() -> datetime:
    """Return one timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def _cache_timestamp_exceeds_clock_skew(
    value: datetime,
    reference: datetime,
) -> bool:
    """Reject timestamps farther ahead than the bounded clock-skew allowance."""
    candidate = _as_utc(value)
    current_time = _as_utc(reference)
    return bool(
        candidate > current_time
        and candidate - current_time
        > timedelta(seconds=AGENT_SOURCE_CACHE_MAX_CLOCK_SKEW_SECONDS)
    )


def _cache_write_timestamp(value: datetime | None) -> datetime:
    """Clamp an implausibly future publication timestamp to the current UTC clock."""
    current_time = _utc_now()
    if value is None:
        return current_time
    candidate = _as_utc(value)
    return (
        current_time
        if _cache_timestamp_exceeds_clock_skew(candidate, current_time)
        else candidate
    )


def _cache_expiration(cached_at: datetime, ttl_seconds: int) -> datetime:
    """Add one cache TTL without overflowing the datetime range."""
    try:
        return _as_utc(cached_at) + timedelta(seconds=max(0, int(ttl_seconds)))
    except OverflowError:
        return datetime.max.replace(tzinfo=timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """Normalize a datetime to timezone-aware UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
