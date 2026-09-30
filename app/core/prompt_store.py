"""Snapshot-backed prompt bookmarks for the local resource browser."""

# Code version: v1.3.0-claude.0

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Mapping

from .chat_history_browser import (
    ChatHistoryMessage,
    find_chat_history_message,
    load_chat_history_messages,
    normalize_chat_history_source,
)
from .local_media_browser import LocalMediaPaginationItem, build_local_store_pagination
from .resource_persistence import (
    PROMPT_FILENAME,
    PROMPT_REMARKS_FILENAME,
    PROMPT_REMARKS_SCHEMA,
    PROMPT_REMARKS_SCHEMA_VERSION,
    PROMPT_SCHEMA,
    PROMPT_SCHEMA_VERSION,
    read_parquet_rows,
    write_parquet_rows_atomic,
)


PROMPT_STORE_DIRNAME = "prompt"
PROMPT_PAGE_SIZE = 24
PROMPT_REMARK_MAX_LENGTH = 48
# A prompt saved from a media card whose conversation has no cached text history.
PROMPT_MEDIA_MESSAGE_KEY_PREFIX = "media-prompt:"
# Removed prompts that one running process can still restore.
PROMPT_RESTORE_LIMIT = 64


@dataclass(frozen=True, slots=True)
class SavedPrompt:
    """Describe one locally saved prompt and its source pointer."""

    stable_id: str
    source: str
    conversation_id: str
    message_key: str
    conversation_title: str
    conversation_url: str
    author_label: str
    content_text: str
    captured_at: str
    added_at: str
    remarks: tuple[str, ...]
    # How many saved prompts carry this exact text, this one included.
    duplicate_count: int = 1

    @property
    def text_key(self) -> str:
        """Return the key shared by every saved prompt with the same text."""
        return prompt_text_key(self.content_text)


@dataclass(frozen=True, slots=True)
class PromptPage:
    """Contain one filtered and paginated prompt-bookmark result."""

    items: tuple[SavedPrompt, ...]
    total_count: int
    current_page: int
    total_pages: int
    page_size: int = PROMPT_PAGE_SIZE

    @property
    def pagination_items(self) -> tuple[LocalMediaPaginationItem, ...]:
        """Reuse the local browser's compact pagination contract."""
        return build_local_store_pagination(self.total_pages, self.current_page)

    @property
    def pagination_active_index(self) -> int:
        """Return the zero-based index for the active pagination indicator."""
        for index, item in enumerate(self.pagination_items):
            if item.is_active:
                return index
        return 0


def prompt_pointer_key(source: str, conversation_id: str, message_key: str) -> str:
    """Return a deterministic, non-content key for one cached message pointer."""
    return "\x1f".join(
        (
            str(source or "").strip().lower(),
            str(conversation_id or "").strip(),
            str(message_key or "").strip(),
        )
    )


def _normalized_prompt_text(value: object) -> str:
    """Collapse whitespace so every cached copy of one prompt compares equal."""
    return " ".join(str(value or "").replace("\x00", "").split())


def prompt_text_key(content_text: object) -> str:
    """Return a deterministic key shared by every prompt with the same text."""
    normalized = _normalized_prompt_text(content_text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def prompt_content_key(source: str, conversation_id: str, content_text: object) -> str:
    """Return a deterministic key for one prompt text within one conversation."""
    scoped = "\x1f".join(
        (
            str(source or "").strip().lower(),
            str(conversation_id or "").strip()[:160],
            _normalized_prompt_text(content_text),
        )
    )
    return hashlib.sha256(scoped.encode("utf-8")).hexdigest()[:24]


def _prompt_stable_id(pointer: str) -> str:
    """Return a stable browser identifier without persisting prompt content."""
    digest = hashlib.sha256(pointer.encode("utf-8")).hexdigest()[:24]
    return f"prompt-{digest}"


def _timestamp_value(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


class PromptStore:
    """Persist prompt snapshots with source pointers for local durability."""

    def __init__(self, local_store_root: Path | str) -> None:
        self.local_store_root = Path(local_store_root).expanduser().resolve(strict=False)
        self.store_root = self.local_store_root / PROMPT_STORE_DIRNAME
        self.catalog_path = self.store_root / PROMPT_FILENAME
        self.remarks_path = self.store_root / PROMPT_REMARKS_FILENAME
        self._lock = RLock()
        self._entries = self._load_entries()
        self._remarks = self._load_remarks()
        self._removed: dict[str, tuple[str, dict[str, Any], list[str]]] = {}
        self._backfill_legacy_snapshots()

    def _load_entries(self) -> dict[str, dict[str, Any]]:
        entries: dict[str, dict[str, Any]] = {}
        for row in read_parquet_rows(self.catalog_path) or []:
            source = normalize_chat_history_source(str(row.get("source") or ""))
            conversation_id = str(row.get("conversation_id") or "").strip()[:160]
            message_key = str(row.get("message_key") or "").strip()[:240]
            if source == "all" or not conversation_id or not message_key:
                continue
            pointer = prompt_pointer_key(source, conversation_id, message_key)
            entries[pointer] = {
                "source": source,
                "conversation_id": conversation_id,
                "message_key": message_key,
                "content_text": str(row.get("content_text") or "").replace("\x00", "").strip(),
                "conversation_title": str(row.get("conversation_title") or "").strip(),
                "conversation_url": str(row.get("conversation_url") or "").strip(),
                "author_label": str(row.get("author_label") or "").strip(),
                "captured_at": str(row.get("captured_at") or "").strip(),
                "added_at": str(row.get("added_at") or ""),
            }
        return entries

    @staticmethod
    def _has_snapshot(entry: dict[str, Any]) -> bool:
        return bool(str(entry.get("content_text") or "").strip())

    @staticmethod
    def _capture_snapshot(entry: dict[str, Any], message: ChatHistoryMessage) -> None:
        """Copy the user message fields needed after its remote history disappears."""
        entry.update(
            {
                "content_text": message.content_text,
                "conversation_title": message.conversation_title,
                "conversation_url": message.conversation_url,
                "author_label": message.author_label,
                "captured_at": message.last_seen_at,
            }
        )

    def _backfill_legacy_snapshots(self) -> None:
        """Upgrade pointer-only rows while their source history is still available."""
        with self._lock:
            pending = tuple(
                (pointer, entry)
                for pointer, entry in self._entries.items()
                if not self._has_snapshot(entry)
            )
            if not pending:
                return

            messages_by_pointer: dict[str, ChatHistoryMessage] = {}
            for source in {str(entry["source"]) for _, entry in pending}:
                for message in load_chat_history_messages(self.local_store_root, source):
                    messages_by_pointer[
                        prompt_pointer_key(message.source, message.conversation_id, message.message_key)
                    ] = message

            changed = False
            for pointer, entry in pending:
                message = messages_by_pointer.get(pointer)
                if message is None or message.role != "user":
                    continue
                self._capture_snapshot(entry, message)
                changed = True
            if changed:
                self._save_entries()

    def _load_remarks(self) -> dict[str, list[str]]:
        remarks: dict[str, list[str]] = {}
        for row in read_parquet_rows(self.remarks_path) or []:
            prompt_id = str(row.get("prompt_id") or "").strip()
            remark = self._normalize_remark(row.get("remark"))
            if not prompt_id or not remark:
                continue
            values = remarks.setdefault(prompt_id, [])
            if remark.casefold() not in {value.casefold() for value in values}:
                values.append(remark)
        return remarks

    @staticmethod
    def _normalize_remark(value: object) -> str:
        return " ".join(str(value or "").split())[:PROMPT_REMARK_MAX_LENGTH]

    def remark_options(self) -> tuple[str, ...]:
        """Return stored remarks for the shared prompt selector."""
        with self._lock:
            values = {
                remark
                for prompt_remarks in self._remarks.values()
                for remark in prompt_remarks
                if remark
            }
        return tuple(sorted(values, key=lambda value: (value.casefold(), value)))

    def _find_entry_for_stable_id(self, stable_id: str) -> tuple[str, dict[str, Any]]:
        normalized_id = str(stable_id or "").strip()
        for pointer, entry in self._entries.items():
            if _prompt_stable_id(pointer) == normalized_id:
                return pointer, entry
        raise LookupError("The saved prompt was not found.")

    def _resolve_stable_id(
        self,
        stable_id: str,
    ) -> tuple[str, dict[str, Any], ChatHistoryMessage | None]:
        pointer, entry = self._find_entry_for_stable_id(stable_id)
        message = find_chat_history_message(
            self.local_store_root,
            source=str(entry["source"]),
            conversation_id=str(entry["conversation_id"]),
            message_key=str(entry["message_key"]),
        )
        if message is not None and message.role != "user":
            message = None
        if message is None and not self._has_snapshot(entry):
            raise LookupError("The saved prompt is no longer available.")
        return pointer, entry, message

    def has_any(self) -> bool:
        """Return whether at least one pointer is saved, even if history is unavailable."""
        with self._lock:
            return bool(self._entries)

    def saved_pointer_keys(self) -> frozenset[str]:
        """Return the saved pointer keys for current-row action state."""
        with self._lock:
            return frozenset(self._entries)

    def saved_content_keys(self) -> frozenset[str]:
        """Return conversation-scoped text keys so media cards can show saved prompts."""
        with self._lock:
            return frozenset(
                prompt_content_key(entry["source"], entry["conversation_id"], entry["content_text"])
                for entry in self._entries.values()
                if self._has_snapshot(entry)
            )

    def _text_counts(self) -> Counter[str]:
        """Count saved prompt snapshots by text so identical prompts can be flagged."""
        return Counter(
            prompt_text_key(entry["content_text"])
            for entry in self._entries.values()
            if self._has_snapshot(entry)
        )

    def _find_entry_for_content_key(self, content_key: str) -> dict[str, Any] | None:
        for entry in self._entries.values():
            if self._has_snapshot(entry) and content_key == prompt_content_key(
                entry["source"], entry["conversation_id"], entry["content_text"]
            ):
                return entry
        return None

    def add_pointer(
        self,
        *,
        source: str,
        conversation_id: str,
        message_key: str,
    ) -> tuple[SavedPrompt, bool]:
        """Save one message pointer once with a durable prompt-content snapshot."""
        normalized_source = normalize_chat_history_source(source)
        normalized_conversation_id = str(conversation_id or "").strip()[:160]
        normalized_message_key = str(message_key or "").strip()[:240]
        if (
            normalized_source == "all"
            or not normalized_conversation_id
            or not normalized_message_key
        ):
            raise ValueError("A valid cached message pointer is required.")

        message = find_chat_history_message(
            self.local_store_root,
            source=normalized_source,
            conversation_id=normalized_conversation_id,
            message_key=normalized_message_key,
        )
        if message is None:
            raise LookupError("The cached message is no longer available.")
        if message.role != "user":
            raise ValueError("Only user messages can be saved as prompts.")

        pointer = prompt_pointer_key(
            normalized_source,
            normalized_conversation_id,
            normalized_message_key,
        )
        with self._lock:
            entry = self._entries.get(pointer)
            created = entry is None
            if entry is None:
                entry = {
                    "source": normalized_source,
                    "conversation_id": normalized_conversation_id,
                    "message_key": normalized_message_key,
                    "added_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                }
                self._capture_snapshot(entry, message)
                self._entries[pointer] = entry
                self._save_entries()
            elif not self._has_snapshot(entry):
                self._capture_snapshot(entry, message)
                self._save_entries()
            return self._build_item(entry), created

    def add_media_prompt(
        self,
        *,
        source: str,
        conversation_id: str,
        prompt_text: str,
        conversation_title: str = "",
        conversation_url: str = "",
        captured_at: str = "",
    ) -> tuple[SavedPrompt, bool]:
        """Save once the prompt recorded with one cached media item.

        That prompt is the user turn that produced the media. When the turn is in the
        cached text history its own pointer is saved, so the Text and Media entry points
        agree; otherwise the text is kept as a snapshot under a content-derived key.
        """
        normalized_source = normalize_chat_history_source(source)
        normalized_conversation_id = str(conversation_id or "").strip()[:160]
        content_text = str(prompt_text or "").replace("\x00", "").strip()
        if normalized_source == "all" or not normalized_conversation_id:
            raise ValueError("A cached media prompt requires its source conversation.")
        if not content_text:
            raise ValueError("This media item has no indexed prompt.")

        content_key = prompt_content_key(normalized_source, normalized_conversation_id, content_text)
        with self._lock:
            entry = self._find_entry_for_content_key(content_key)
            if entry is not None:
                return self._build_item(entry), False

        normalized_text = _normalized_prompt_text(content_text)
        message = next(
            (
                candidate
                for candidate in load_chat_history_messages(self.local_store_root, normalized_source)
                if candidate.conversation_id == normalized_conversation_id
                and candidate.role == "user"
                and _normalized_prompt_text(candidate.content_text) == normalized_text
            ),
            None,
        )
        message_key = (
            message.message_key[:240]
            if message is not None
            else f"{PROMPT_MEDIA_MESSAGE_KEY_PREFIX}{content_key}"
        )
        pointer = prompt_pointer_key(normalized_source, normalized_conversation_id, message_key)
        with self._lock:
            entry = self._entries.get(pointer) or self._find_entry_for_content_key(content_key)
            if entry is not None:
                return self._build_item(entry), False
            entry = {
                "source": normalized_source,
                "conversation_id": normalized_conversation_id,
                "message_key": message_key,
                "added_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            }
            if message is not None:
                self._capture_snapshot(entry, message)
            else:
                entry.update(
                    {
                        "content_text": content_text,
                        "conversation_title": str(conversation_title or "").strip(),
                        "conversation_url": str(conversation_url or "").strip(),
                        "author_label": "You",
                        "captured_at": str(captured_at or "").strip(),
                    }
                )
            self._entries[pointer] = entry
            self._save_entries()
            return self._build_item(entry), True

    def remove(self, stable_id: str) -> int:
        """Stop saving one prompt and its remarks; return how many saved prompts keep its text.

        The removed prompt stays restorable until this process exits, so the page that
        removed it can undo an accidental removal.
        """
        with self._lock:
            pointer, entry = self._find_entry_for_stable_id(stable_id)
            prompt_id = _prompt_stable_id(pointer)
            del self._entries[pointer]
            remarks = self._remarks.pop(prompt_id, [])
            self._save_entries()
            if remarks:
                self._save_remarks()
            self._removed.pop(prompt_id, None)
            self._removed[prompt_id] = (pointer, entry, remarks)
            while len(self._removed) > PROMPT_RESTORE_LIMIT:
                del self._removed[next(iter(self._removed))]
            return self._text_counts()[prompt_text_key(entry.get("content_text"))]

    def restore(self, stable_id: str) -> SavedPrompt:
        """Save again one prompt this process removed, together with its remarks."""
        prompt_id = str(stable_id or "").strip()
        with self._lock:
            removed = self._removed.pop(prompt_id, None)
            if removed is None:
                raise LookupError("The removed prompt can no longer be restored.")
            pointer, entry, remarks = removed
            if pointer not in self._entries:
                self._entries[pointer] = entry
                self._save_entries()
                if remarks:
                    self._remarks[prompt_id] = list(remarks)
                    self._save_remarks()
            return self._build_item(self._entries[pointer])

    def add_remark(self, stable_id: str, remark: object) -> tuple[SavedPrompt, bool]:
        """Add one normalized remark to a saved prompt."""
        normalized_remark = self._normalize_remark(remark)
        if not normalized_remark:
            raise ValueError("A remark is required.")
        with self._lock:
            pointer, entry, message = self._resolve_stable_id(stable_id)
            prompt_id = _prompt_stable_id(pointer)
            values = self._remarks.setdefault(prompt_id, [])
            created = not any(value.casefold() == normalized_remark.casefold() for value in values)
            if created:
                values.append(normalized_remark)
                self._save_remarks()
            return self._build_item(entry, message), created

    def remove_remark(self, stable_id: str, remark: object) -> SavedPrompt:
        """Remove one remark from a saved prompt."""
        normalized_remark = self._normalize_remark(remark)
        if not normalized_remark:
            raise ValueError("A remark is required.")
        with self._lock:
            pointer, entry, message = self._resolve_stable_id(stable_id)
            prompt_id = _prompt_stable_id(pointer)
            values = self._remarks.get(prompt_id, [])
            self._remarks[prompt_id] = [
                value for value in values if value.casefold() != normalized_remark.casefold()
            ]
            if self._remarks[prompt_id]:
                self._remarks = {key: value for key, value in self._remarks.items() if value}
            else:
                self._remarks.pop(prompt_id, None)
            self._save_remarks()
            return self._build_item(entry, message)

    def query(
        self,
        *,
        source: str = "all",
        query: str = "",
        sort: str = "newest",
        page: object = 1,
    ) -> PromptPage:
        """Return saved prompt snapshots, using history only for legacy rows."""
        normalized_source = normalize_chat_history_source(source)
        query_terms = tuple(str(query or "").strip()[:120].casefold().split())
        with self._lock:
            entries = tuple(self._entries.items())
            text_counts = self._text_counts()
        messages = {
            prompt_pointer_key(message.source, message.conversation_id, message.message_key): message
            for message in load_chat_history_messages(self.local_store_root, normalized_source)
        }
        items: list[SavedPrompt] = []
        for pointer, entry in entries:
            if normalized_source != "all" and entry["source"] != normalized_source:
                continue
            message = messages.get(pointer)
            if message is not None and message.role != "user":
                message = None
            if message is None and not self._has_snapshot(entry):
                continue
            item = self._build_item(entry, message, text_counts)
            searchable = " ".join(
                (
                    item.conversation_title,
                    item.author_label,
                    item.content_text,
                )
            ).casefold()
            if query_terms and not all(term in searchable for term in query_terms):
                continue
            items.append(item)

        normalized_sort = str(sort or "").strip().lower()
        if normalized_sort == "name":
            items.sort(key=lambda item: (item.conversation_title.casefold(), item.content_text.casefold(), item.stable_id))
        else:
            direction = -1 if normalized_sort != "oldest" else 1
            items.sort(key=lambda item: (direction * _timestamp_value(item.added_at), item.stable_id))

        total_count = len(items)
        try:
            requested_page = max(1, int(page))
        except (TypeError, ValueError):
            requested_page = 1
        total_pages = max(1, (total_count + PROMPT_PAGE_SIZE - 1) // PROMPT_PAGE_SIZE)
        current_page = min(total_pages, requested_page)
        start = (current_page - 1) * PROMPT_PAGE_SIZE
        return PromptPage(
            items=tuple(items[start : start + PROMPT_PAGE_SIZE]),
            total_count=total_count,
            current_page=current_page,
            total_pages=total_pages,
        )

    def _build_item(
        self,
        entry: dict[str, Any],
        message: ChatHistoryMessage | None = None,
        text_counts: Mapping[str, int] | None = None,
    ) -> SavedPrompt:
        pointer = prompt_pointer_key(
            str(entry["source"]),
            str(entry["conversation_id"]),
            str(entry["message_key"]),
        )
        if not self._has_snapshot(entry) and message is None:
            raise LookupError("The saved prompt is no longer available.")

        message_content = str(message.content_text) if message is not None else ""
        message_title = str(message.conversation_title) if message is not None else ""
        message_url = str(message.conversation_url) if message is not None else ""
        message_author = str(message.author_label) if message is not None else ""
        message_captured_at = str(message.last_seen_at) if message is not None else ""
        content_text = str(entry.get("content_text") or "").strip() or message_content
        conversation_title = str(entry.get("conversation_title") or "").strip() or message_title
        conversation_url = str(entry.get("conversation_url") or "").strip() or message_url
        author_label = str(entry.get("author_label") or "").strip() or message_author
        captured_at = str(entry.get("captured_at") or "").strip() or message_captured_at
        counts = self._text_counts() if text_counts is None else text_counts
        # A pointer-only row is not part of the snapshot count yet.
        duplicate_count = counts.get(prompt_text_key(content_text), 0) + (
            0 if self._has_snapshot(entry) else 1
        )
        return SavedPrompt(
            stable_id=_prompt_stable_id(pointer),
            source=str(entry["source"]),
            conversation_id=str(entry["conversation_id"]),
            message_key=str(entry["message_key"]),
            conversation_title=conversation_title,
            conversation_url=conversation_url,
            author_label=author_label,
            content_text=content_text,
            captured_at=captured_at,
            added_at=str(entry.get("added_at") or ""),
            remarks=tuple(self._remarks.get(_prompt_stable_id(pointer), ())),
            duplicate_count=duplicate_count,
        )

    def _save_entries(self) -> None:
        rows: Iterable[dict[str, Any]] = (
            {
                "schema_version": PROMPT_SCHEMA_VERSION,
                "source": entry["source"],
                "conversation_id": entry["conversation_id"],
                "message_key": entry["message_key"],
                "content_text": entry.get("content_text", ""),
                "conversation_title": entry.get("conversation_title", ""),
                "conversation_url": entry.get("conversation_url", ""),
                "author_label": entry.get("author_label", ""),
                "captured_at": entry.get("captured_at", ""),
                "added_at": entry["added_at"],
            }
            for _, entry in sorted(self._entries.items())
        )
        write_parquet_rows_atomic(self.catalog_path, rows, PROMPT_SCHEMA)

    def _save_remarks(self) -> None:
        rows: Iterable[dict[str, Any]] = (
            {
                "schema_version": PROMPT_REMARKS_SCHEMA_VERSION,
                "prompt_id": prompt_id,
                "remark": remark,
            }
            for prompt_id, values in sorted(self._remarks.items())
            for remark in sorted(values, key=lambda value: (value.casefold(), value))
        )
        write_parquet_rows_atomic(self.remarks_path, rows, PROMPT_REMARKS_SCHEMA)
