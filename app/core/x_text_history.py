"""Persist text from the authenticated X likes timeline independently of media."""

# Code version: v1.0.1-codex.0

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .resource_persistence import (
    X_HISTORY_FILENAME,
    X_HISTORY_SCHEMA,
    X_HISTORY_SCHEMA_VERSION,
    read_parquet_rows,
    write_parquet_rows_atomic,
)


X_STATUS_URL_PATTERN = re.compile(
    r"^https://x\.com/(?:[A-Za-z0-9_]{1,15}|i)/status/(?P<status_id>[0-9]{1,30})$"
)
X_HANDLE_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,15}$")


@dataclass(frozen=True, slots=True)
class XTextPost:
    """One visible liked post with a canonical status URL and nonempty text."""

    url: str
    content_text: str
    author_handle: str = ""
    created_at: str = ""


def x_text_history_path(local_store_root: Path | str) -> Path:
    """Return the typed text-history path without touching the X media catalog."""

    return Path(local_store_root).expanduser() / "llm" / "x" / X_HISTORY_FILENAME


class XTextHistoryStore:
    """Upsert unique post text while retaining older successful observations."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        persisted = read_parquet_rows(self.path)
        if persisted is None and self.path.exists():
            raise RuntimeError("Existing X text history could not be read; it was not overwritten.")
        self._rows: dict[str, dict[str, object]] = {}
        for row in persisted or []:
            status_id = str(row.get("conversation_id") or "")
            if (
                row.get("schema_version") != X_HISTORY_SCHEMA_VERSION
                or row.get("platform") != "x"
                or not status_id.isdigit()
                or row.get("message_key") != f"x:{status_id}"
            ):
                raise RuntimeError("Existing X text history has an unsupported row; it was not overwritten.")
            self._rows[status_id] = row

    @property
    def cached_posts(self) -> int:
        """Count persisted unique posts with text, including posts without media."""

        return len(self._rows)

    def upsert(self, posts: list[XTextPost]) -> int:
        """Atomically add or update valid post text and return the changed count."""

        observed_at = datetime.now(UTC).isoformat(timespec="seconds")
        next_rows = dict(self._rows)
        changed = 0
        for post in posts:
            match = X_STATUS_URL_PATTERN.fullmatch(str(post.url or "").strip())
            content_text = str(post.content_text or "").replace("\x00", "").strip()
            if match is None or not content_text:
                continue
            status_id = match.group("status_id")
            author_handle = str(post.author_handle or "").strip().lstrip("@")
            if not X_HANDLE_PATTERN.fullmatch(author_handle):
                author_handle = ""
            previous = next_rows.get(status_id)
            source_url = match.group(0)
            next_row: dict[str, object] = {
                "schema_version": X_HISTORY_SCHEMA_VERSION,
                "platform": "x",
                "conversation_id": status_id,
                "conversation_url": source_url,
                "conversation_title": f"Post by @{author_handle}" if author_handle else f"X post {status_id}",
                "message_key": f"x:{status_id}",
                "turn_index": 0,
                "message_index": 0,
                "role": "post",
                "author_label": f"@{author_handle}" if author_handle else "X",
                "content_text": content_text,
                "content_html": "",
                "content_sha256": hashlib.sha256(content_text.encode("utf-8")).hexdigest(),
                "source_links": [source_url],
                "model_label": "",
                "first_seen_at": str(previous.get("first_seen_at") or observed_at) if previous else observed_at,
                "last_seen_at": observed_at,
            }
            if previous != next_row:
                next_rows[status_id] = next_row
                changed += 1
        if changed:
            write_parquet_rows_atomic(
                self.path,
                (next_rows[key] for key in sorted(next_rows, key=int)),
                X_HISTORY_SCHEMA,
            )
            self._rows = next_rows
        return changed
