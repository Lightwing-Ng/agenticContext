"""Read cached text sessions for the local browser."""

# Code version: v1.16.0-codex.1

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit, urlunsplit

from .agent_source_cache import agent_source_cache_path

from .local_media_browser import (
    LocalMediaItem,
    LocalMediaPaginationItem,
    build_local_store_pagination,
    format_datetime_label,
)
from .resource_persistence import (
    CHATGPT_HISTORY_FILENAME,
    CLAUDE_HISTORY_FILENAME,
    GEMINI_HISTORY_FILENAME,
    GROK_HISTORY_FILENAME,
    ZHIHU_HISTORY_FILENAME,
    read_parquet_rows,
)


CHAT_HISTORY_PAGE_SIZE = 100
CHAT_HISTORY_SESSION_PAGE_SIZE = 100
CHAT_HISTORY_SOURCE_VALUES = frozenset(
    {"all", "chatgpt", "claude", "gemini", "grok", "zhihu"}
)
CHAT_HISTORY_SORT_VALUES = frozenset({"newest", "oldest", "name"})


@dataclass(frozen=True, slots=True)
class ChatHistoryMediaReference:
    """Point from one cached message to an existing media-browser item."""

    stable_id: str
    label: str
    href: str
    media_url: str
    media_kind: str
    alt_text: str


@dataclass(frozen=True, slots=True)
class ChatHistoryMessage:
    """Describe one typed message without duplicating any media payload."""

    stable_id: str
    source: str
    conversation_id: str
    conversation_url: str
    conversation_title: str
    message_key: str
    message_index: int
    role: str
    author_label: str
    content_text: str
    content_html: str
    source_links: tuple[str, ...]
    model_label: str
    first_seen_at: str
    last_seen_at: str
    media_refs: tuple[ChatHistoryMediaReference, ...] = ()


@dataclass(frozen=True, slots=True)
class ChatHistorySession:
    """Describe one session as the primary browser table row."""

    stable_id: str
    source: str
    conversation_id: str
    conversation_url: str
    conversation_title: str
    message_count: int
    first_seen_at: str
    last_seen_at: str
    latest_message: str
    latest_role: str
    latest_author_label: str
    model_label: str
    source_links: tuple[str, ...]
    media_refs: tuple[ChatHistoryMediaReference, ...] = ()


@dataclass(frozen=True, slots=True)
class ChatHistoryPage:
    """Contain one filtered, sorted, and paginated text-history result."""

    items: tuple[ChatHistoryMessage, ...]
    total_count: int
    conversation_count: int
    current_page: int
    total_pages: int
    page_size: int = CHAT_HISTORY_PAGE_SIZE
    session_view: bool = False
    sessions: tuple[ChatHistorySession, ...] = ()
    session_id: str = ""
    current_session: ChatHistorySession | None = None
    previous_session: ChatHistorySession | None = None
    next_session: ChatHistorySession | None = None
    project_count: int = 0
    answerer_options: tuple[str, ...] = ()
    selected_answerer: str = ""

    @property
    def session_detail(self) -> bool:
        """Return whether this page contains one session's complete message history."""
        return bool(self.session_id and self.current_session)

    @property
    def pagination_unit(self) -> str:
        """Return the user-visible unit represented by one page."""
        return "message" if self.session_detail else ("session" if self.session_view else "message")

    @property
    def pagination_items(self) -> tuple[LocalMediaPaginationItem, ...]:
        """Reuse the local store's compact five-page pagination contract."""
        return build_local_store_pagination(self.total_pages, self.current_page)

    @property
    def pagination_active_index(self) -> int:
        """Return the zero-based index for the active pagination indicator."""
        for index, item in enumerate(self.pagination_items):
            if item.is_active:
                return index
        return 0


def chat_history_path(local_store_root: Path | str, source: str = "gemini") -> Path:
    """Return the typed history file for one supported chat source."""
    normalized_source = normalize_chat_history_source(source)
    filename = {
        "chatgpt": CHATGPT_HISTORY_FILENAME,
        "claude": CLAUDE_HISTORY_FILENAME,
        "gemini": GEMINI_HISTORY_FILENAME,
        "grok": GROK_HISTORY_FILENAME,
        "zhihu": ZHIHU_HISTORY_FILENAME,
    }.get(normalized_source, GEMINI_HISTORY_FILENAME)
    return Path(local_store_root).expanduser() / "llm" / normalized_source / filename


def normalize_chat_history_source(value: str | None) -> str:
    """Normalize a text-cache source to the current allowlist."""
    normalized = str(value or "").strip().lower()
    return normalized if normalized in CHAT_HISTORY_SOURCE_VALUES else "all"


def normalize_chat_history_sort(value: str | None) -> str:
    """Normalize text-history ordering to the current allowlist."""
    normalized = str(value or "").strip().lower()
    return normalized if normalized in CHAT_HISTORY_SORT_VALUES else "newest"


def format_chat_message_timestamp_label(value: str | None) -> str:
    """Format a cached date or timestamp according to the UI datetime contract."""
    formatted = format_datetime_label(value)
    return "Unknown time" if formatted == "Unknown date" else formatted


def _safe_external_url(value: Any) -> str:
    """Keep only absolute HTTP(S) links from cached browser metadata."""
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment))


def _timestamp_value(value: str) -> float:
    """Return a sortable UTC timestamp without making invalid rows fatal."""
    from datetime import UTC, datetime

    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _message_from_row(row: dict[str, Any], source: str) -> ChatHistoryMessage | None:
    """Convert one persisted history row into a display-safe message."""
    content_text = str(row.get("content_text") or "").replace("\x00", "").strip()
    content_html = str(row.get("content_html") or "").replace("\x00", "").strip()
    conversation_id = str(row.get("conversation_id") or "").strip()
    message_key = str(row.get("message_key") or "").strip()
    if not content_text or not conversation_id or not message_key:
        return None

    source_links = tuple(
        dict.fromkeys(
            link
            for link in (_safe_external_url(value) for value in row.get("source_links") or [])
            if link
        )
    )
    stable_digest = hashlib.sha256(f"{source}:{message_key}".encode("utf-8")).hexdigest()[:24]
    first_seen_at = str(row.get("first_seen_at") or "").strip()
    persisted_last_seen_at = str(row.get("last_seen_at") or first_seen_at).strip()
    first_seen_value = _timestamp_value(first_seen_at)
    persisted_last_seen_value = _timestamp_value(persisted_last_seen_at)
    gemini_capture_fallback = (
        source == "gemini"
        and first_seen_value > 0
        and persisted_last_seen_value >= first_seen_value
    )
    chatgpt_capture_fallback = (
        source == "chatgpt"
        and first_seen_value > 0
        and persisted_last_seen_value == first_seen_value
    )
    last_seen_at = "" if gemini_capture_fallback or chatgpt_capture_fallback else persisted_last_seen_at
    return ChatHistoryMessage(
        stable_id=f"chat-{stable_digest}",
        source=source,
        conversation_id=conversation_id,
        conversation_url=_safe_external_url(row.get("conversation_url")),
        conversation_title=str(row.get("conversation_title") or "Untitled session").strip(),
        message_key=message_key,
        message_index=int(row.get("message_index") or 0),
        role=str(row.get("role") or "message").strip().lower(),
        author_label=str(row.get("author_label") or row.get("role") or "Message").strip(),
        content_text=content_text,
        content_html=content_html,
        source_links=source_links,
        model_label=str(row.get("model_label") or "").strip(),
        first_seen_at=first_seen_at,
        last_seen_at=last_seen_at,
    )


def load_chat_history_messages(
    local_store_root: Path | str,
    source: str = "all",
) -> tuple[ChatHistoryMessage, ...]:
    """Load all valid cached messages for one source without paging them."""
    normalized_source = normalize_chat_history_source(source)
    source_paths = (
        ((normalized_source, chat_history_path(local_store_root, normalized_source)),)
        if normalized_source != "all"
        else (
            ("chatgpt", chat_history_path(local_store_root, "chatgpt")),
            ("claude", chat_history_path(local_store_root, "claude")),
            ("gemini", chat_history_path(local_store_root, "gemini")),
            ("grok", chat_history_path(local_store_root, "grok")),
            ("zhihu", chat_history_path(local_store_root, "zhihu")),
        )
    )
    messages = tuple(
        item
        for row_source, path in source_paths
        for row in (read_parquet_rows(path) or [])
        if (item := _message_from_row(row, row_source)) is not None
    )
    return _refresh_session_metadata(local_store_root, messages)


def _refresh_session_metadata(
    local_store_root: Path | str, messages: tuple[ChatHistoryMessage, ...],
) -> tuple[ChatHistoryMessage, ...]:
    """Overlay newer discovery metadata without fetching or duplicating message bodies."""
    history_path = chat_history_path(local_store_root, "chatgpt")
    if not history_path.is_file() or not any(item.source == "chatgpt" for item in messages):
        return messages
    history_updated = history_path.stat().st_mtime
    metadata: dict[str, tuple[float, str, str]] = {}
    for row in read_parquet_rows(agent_source_cache_path(local_store_root)) or []:
        observed = _timestamp_value(str(row.get("cached_at") or ""))
        if row.get("platform") != "chatgpt" or observed < history_updated:
            continue
        try:
            payload = json.loads(str(row.get("payload_json") or "{}"))
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        candidates = payload.get("sessions", payload.get("recent_sessions", []))
        if row.get("source_kind") == "session-history":
            candidates = [{"url": payload.get("conversation_url"), "title": payload.get("title")}]
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            url = _safe_external_url(candidate.get("url"))
            parsed = urlsplit(url)
            if parsed.hostname != "chatgpt.com" or "/c/" not in parsed.path:
                continue
            conversation_id = parsed.path.rsplit("/c/", 1)[-1].strip("/")
            title = str(candidate.get("title") or "").strip()
            if not title or not conversation_id:
                continue
            if observed >= metadata.get(conversation_id, (0, "", ""))[0]:
                metadata[conversation_id] = (observed, title, url)
    return tuple(
        replace(item, conversation_title=metadata[item.conversation_id][1],
                conversation_url=metadata[item.conversation_id][2])
        if item.source == "chatgpt" and item.conversation_id in metadata else item
        for item in messages
    )


def find_chat_history_message(
    local_store_root: Path | str,
    *,
    source: str,
    conversation_id: str,
    message_key: str,
) -> ChatHistoryMessage | None:
    """Resolve one durable message pointer from the current typed history cache."""
    normalized_source = normalize_chat_history_source(source)
    normalized_conversation_id = str(conversation_id or "").strip()[:160]
    normalized_message_key = str(message_key or "").strip()[:240]
    if (
        normalized_source == "all"
        or not normalized_conversation_id
        or not normalized_message_key
    ):
        return None
    return next(
        (
            message
            for message in load_chat_history_messages(local_store_root, normalized_source)
            if message.conversation_id == normalized_conversation_id
            and message.message_key == normalized_message_key
        ),
        None,
    )


def _sort_messages(messages: Iterable[ChatHistoryMessage], sort: str) -> tuple[ChatHistoryMessage, ...]:
    """Sort messages by recency or session title."""
    normalized_sort = normalize_chat_history_sort(sort)
    if normalized_sort == "name":
        return tuple(
            sorted(
                messages,
                key=lambda item: (item.conversation_title.casefold(), item.message_index, item.stable_id),
            )
        )
    direction = -1 if normalized_sort == "newest" else 1
    return tuple(
        sorted(
            messages,
            key=lambda item: (
                not bool(item.last_seen_at),
                direction * _timestamp_value(item.last_seen_at),
                item.stable_id,
            ),
        )
    )


def _build_chat_history_sessions(messages: Iterable[ChatHistoryMessage]) -> tuple[ChatHistorySession, ...]:
    """Aggregate messages into one deterministic row per session."""
    grouped: dict[tuple[str, str], list[ChatHistoryMessage]] = {}
    for message in messages:
        grouped.setdefault((message.source, message.conversation_id), []).append(message)

    sessions: list[ChatHistorySession] = []
    for (_source, conversation_id), conversation_messages in grouped.items():
        ordered_messages = tuple(
            sorted(
                conversation_messages,
                key=lambda item: (_timestamp_value(item.last_seen_at), item.message_index, item.stable_id),
            )
        )
        latest = ordered_messages[-1]
        source_links = tuple(dict.fromkeys(link for item in ordered_messages for link in item.source_links))
        first_seen_at = min(
            (item.first_seen_at for item in ordered_messages),
            key=_timestamp_value,
            default="",
        )
        sessions.append(
            ChatHistorySession(
                stable_id=f"session-{hashlib.sha256(f'{latest.source}:{conversation_id}'.encode('utf-8')).hexdigest()[:24]}",
                source=latest.source,
                conversation_id=conversation_id,
                conversation_url=latest.conversation_url,
                conversation_title=latest.conversation_title,
                message_count=len(ordered_messages),
                first_seen_at=first_seen_at,
                last_seen_at=latest.last_seen_at,
                latest_message=latest.content_text,
                latest_role=latest.role,
                latest_author_label=latest.author_label,
                model_label=latest.model_label,
                source_links=source_links,
            )
        )
    return tuple(sessions)


def _sort_chat_history_sessions(
    sessions: Iterable[ChatHistorySession],
    sort: str,
) -> tuple[ChatHistorySession, ...]:
    """Sort session rows by recency or session title."""
    normalized_sort = normalize_chat_history_sort(sort)
    if normalized_sort == "name":
        return tuple(sorted(sessions, key=lambda item: (item.conversation_title.casefold(), item.stable_id)))
    direction = -1 if normalized_sort == "newest" else 1
    return tuple(
        sorted(
            sessions,
            key=lambda item: (
                not bool(item.last_seen_at),
                direction * _timestamp_value(item.last_seen_at),
                item.stable_id,
            ),
        )
    )


def _message_matches_query(message: ChatHistoryMessage, query_terms: tuple[str, ...]) -> bool:
    """Return whether every normalized search term appears in one message's searchable fields."""
    if not query_terms:
        return True
    searchable_text = " ".join(
        (
            message.conversation_title,
            message.author_label,
            message.role,
            message.model_label,
            message.content_text,
            message.content_html,
            *message.source_links,
        )
    ).casefold()
    return all(term in searchable_text for term in query_terms)


def count_cached_projects(local_store_root: Path | str, source: str = "all") -> int:
    """Count distinct project containers in each provider's latest cached catalog.

    Provider adapters normalize projects, Gems, and collections to projects.
    Source filtering applies; message search and pagination do not filter catalogs.
    Reading this metric never starts provider discovery or refreshes browser state.
    """
    selected_source = normalize_chat_history_source(source)
    latest: dict[str, tuple[float, list[Any]]] = {}
    for row in read_parquet_rows(agent_source_cache_path(local_store_root)) or []:
        platform = str(row.get("platform") or "").lower()
        if platform not in CHAT_HISTORY_SOURCE_VALUES or platform == "all":
            continue
        if selected_source != "all" and platform != selected_source:
            continue
        try:
            payload = json.loads(str(row.get("payload_json") or "{}"))
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        catalog = payload.get("agent_sources", payload)
        if not isinstance(catalog, dict) or not isinstance(catalog.get("projects"), list):
            continue
        observed = _timestamp_value(str(row.get("cached_at") or ""))
        if platform not in latest or observed >= latest[platform][0]:
            latest[platform] = (observed, catalog["projects"])
    identities: set[tuple[str, str]] = set()
    for platform, (_, projects) in latest.items():
        for project in projects:
            if not isinstance(project, dict):
                continue
            url = _safe_external_url(project.get("url"))
            if url:
                parsed = urlsplit(url)
                identity = urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""))
            else:
                identity = str(project.get("id") or "").strip()
            if identity:
                identities.add((platform, identity))
    return len(identities)


def query_chat_history(
    local_store_root: Path | str,
    *,
    source: str = "all",
    query: str = "",
    sort: str = "newest",
    page: object = 1,
    page_size: int = CHAT_HISTORY_PAGE_SIZE,
    session_view: bool = False,
    session: str = "",
    answerer: str = "",
) -> ChatHistoryPage:
    """Read cached text sessions, global search results, or one session's complete history."""
    normalized_source = normalize_chat_history_source(source)
    normalized_query = str(query or "").strip()[:120].casefold()
    query_terms = tuple(normalized_query.split())
    project_count = count_cached_projects(local_store_root, normalized_source)
    all_messages = load_chat_history_messages(local_store_root, normalized_source)
    answerer_options = (
        tuple(
            sorted(
                {item.author_label for item in all_messages if item.author_label},
                key=str.casefold,
            )
        )
        if normalized_source == "zhihu"
        else ()
    )
    requested_answerer = str(answerer or "").replace("\x00", "").strip()[:160]
    selected_answerer = (
        requested_answerer if requested_answerer in answerer_options else ""
    )
    if selected_answerer:
        all_messages = tuple(
            item for item in all_messages if item.author_label == selected_answerer
        )
    requested_session = str(session or "").strip()[:160]
    all_sessions = _sort_chat_history_sessions(_build_chat_history_sessions(all_messages), sort)
    selected_session = None
    if requested_session:
        selected_session = next(
            (
                item
                for item in all_sessions
                if item.stable_id == requested_session
                or f"{item.source}:{item.conversation_id}" == requested_session
            ),
            None,
        )
    if selected_session is not None:
        selected_index = next(
            (index for index, item in enumerate(all_sessions) if item.stable_id == selected_session.stable_id),
            -1,
        )
        session_messages = tuple(
            sorted(
                (
                    message
                    for message in all_messages
                    if (message.source, message.conversation_id)
                    == (selected_session.source, selected_session.conversation_id)
                ),
                key=lambda item: (
                    item.message_index,
                    _timestamp_value(item.last_seen_at),
                    item.stable_id,
                ),
            )
        )
        if query_terms:
            session_messages = tuple(
                message for message in session_messages if _message_matches_query(message, query_terms)
            )
        safe_page_size = max(1, int(page_size))
        try:
            requested_page = max(1, int(page))
        except (TypeError, ValueError):
            requested_page = 1
        total_pages = max(1, (len(session_messages) + safe_page_size - 1) // safe_page_size)
        current_page = min(total_pages, requested_page)
        start = (current_page - 1) * safe_page_size
        return ChatHistoryPage(
            items=session_messages[start : start + safe_page_size],
            total_count=len(session_messages),
            conversation_count=1,
            project_count=project_count,
            current_page=current_page,
            total_pages=total_pages,
            page_size=safe_page_size,
            session_view=True,
            sessions=(selected_session,),
            session_id=selected_session.stable_id,
            current_session=selected_session,
            previous_session=all_sessions[selected_index - 1] if selected_index > 0 else None,
            next_session=all_sessions[selected_index + 1] if 0 <= selected_index < len(all_sessions) - 1 else None,
            answerer_options=answerer_options,
            selected_answerer=selected_answerer,
        )

    messages = all_messages
    if query_terms:
        messages = tuple(item for item in messages if _message_matches_query(item, query_terms))

    safe_page_size = max(1, int(page_size))
    try:
        requested_page = max(1, int(page))
    except (TypeError, ValueError):
        requested_page = 1
    total_count = len(messages)
    sessions = _sort_chat_history_sessions(_build_chat_history_sessions(messages), sort)
    if session_view:
        safe_page_size = max(1, int(page_size))
        total_pages = max(1, (len(sessions) + safe_page_size - 1) // safe_page_size)
        current_page = min(total_pages, requested_page)
        start = (current_page - 1) * safe_page_size
        page_sessions = sessions[start : start + safe_page_size]
        selected_ids = {(session.source, session.conversation_id) for session in page_sessions}
        page_items = tuple(
            sorted(
                (
                    message
                    for message in messages
                    if (message.source, message.conversation_id) in selected_ids
                ),
                key=lambda item: (
                    item.source,
                    item.conversation_id,
                    item.message_index,
                    _timestamp_value(item.last_seen_at),
                    item.stable_id,
                ),
            )
        )
    else:
        ordered = _sort_messages(messages, sort)
        total_pages = max(1, (total_count + safe_page_size - 1) // safe_page_size)
        current_page = min(total_pages, requested_page)
        start = (current_page - 1) * safe_page_size
        page_items = ordered[start : start + safe_page_size]
        page_sessions = sessions
    return ChatHistoryPage(
        items=tuple(page_items),
        total_count=total_count,
        conversation_count=len({(item.source, item.conversation_id) for item in messages}),
        project_count=project_count,
        current_page=current_page,
        total_pages=total_pages,
        page_size=safe_page_size,
        session_view=bool(session_view),
        sessions=tuple(page_sessions),
        answerer_options=answerer_options,
        selected_answerer=selected_answerer,
    )


def build_chat_history_markdown(page: ChatHistoryPage, *, message_count: int | None = None) -> str:
    """Render a cached session page as a portable Markdown document."""
    if not page.session_detail or page.current_session is None:
        return ""

    session = page.current_session
    title = " ".join(session.conversation_title.split()) or "Untitled session"
    lines = [
        f"# {title}",
        "",
        f"- Source: {session.source.title()}",
        f"- Messages: {(session.message_count if message_count is None else message_count):,}",
    ]
    if session.conversation_url:
        lines.append(f"- Original: {session.conversation_url}")
    lines.extend(("", "## Messages", ""))

    for index, message in enumerate(page.items, start=1):
        role = " ".join(message.author_label.split()) or message.role.title() or "Message"
        timestamp = format_chat_message_timestamp_label(message.last_seen_at)
        content = message.content_text.strip() or "(empty message)"
        lines.extend((f"### {index}. {role} · {timestamp}", "", content, ""))
        for link_index, source_link in enumerate(message.source_links, start=1):
            lines.append(f"Source link {link_index}: {source_link}")
        if message.source_links:
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def attach_media_references(
    page: ChatHistoryPage,
    media_items: Iterable[LocalMediaItem],
    media_href_factory: Callable[[str], str],
    media_url_factory: Callable[[LocalMediaItem], str],
) -> ChatHistoryPage:
    """Attach browser pointers and local preview URLs without copying files."""
    media_by_link: dict[str, list[LocalMediaItem]] = {}
    for item in media_items:
        for link in (item.source_url, item.resource_key):
            normalized_link = _safe_external_url(link)
            if normalized_link:
                media_by_link.setdefault(normalized_link, []).append(item)

    def references_for_links(candidate_links: Iterable[str]) -> tuple[ChatHistoryMediaReference, ...]:
        related: dict[str, LocalMediaItem] = {}
        for link in candidate_links:
            for item in media_by_link.get(link, []):
                related[item.stable_id] = item
        return tuple(
            ChatHistoryMediaReference(
                stable_id=item.stable_id,
                label=item.filename,
                href=media_href_factory(item.stable_id),
                media_url=media_url_factory(item),
                media_kind=item.media_kind,
                alt_text=item.alt_text or item.title or item.filename or "Cached media",
            )
            for item in related.values()
        )

    enriched_items: list[ChatHistoryMessage] = []
    for message in page.items:
        refs = references_for_links((message.conversation_url, *message.source_links))
        enriched_items.append(replace(message, media_refs=refs))
    enriched_sessions = tuple(
        replace(
            session,
            media_refs=references_for_links((session.conversation_url, *session.source_links)),
        )
        for session in page.sessions
    )
    return replace(page, items=tuple(enriched_items), sessions=enriched_sessions)
