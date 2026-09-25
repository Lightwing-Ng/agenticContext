"""Focused persistence checks for text captured from the X likes timeline."""

# Code version: v1.0.1-codex.0

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.resource_persistence import read_parquet_rows
from app.core.x_text_history import XTextHistoryStore, XTextPost, x_text_history_path


def test_x_text_history_keeps_posts_without_media_and_upserts_by_status_id(tmp_path: Path) -> None:
    path = x_text_history_path(tmp_path)
    store = XTextHistoryStore(path)
    first = XTextPost(
        url="https://x.com/poster/status/123",
        content_text="A text-only liked post",
        author_handle="poster",
        created_at="2026-09-25T10:00:00Z",
    )
    first_observed = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    with patch("app.core.x_text_history.datetime") as clock:
        clock.now.return_value = first_observed
        assert store.upsert([first]) == 1
        assert store.upsert([first]) == 0
    assert store.cached_posts == 1

    updated = XTextPost(
        url="https://x.com/poster/status/123",
        content_text="The same liked post with complete text",
        author_handle="poster",
        created_at="2026-09-25T10:00:00Z",
    )
    with patch("app.core.x_text_history.datetime") as clock:
        clock.now.return_value = first_observed
        assert store.upsert([updated]) == 1
    assert store.cached_posts == 1

    rows = read_parquet_rows(path)
    assert rows is not None and len(rows) == 1
    assert rows[0]["platform"] == "x"
    assert rows[0]["message_key"] == "x:123"
    assert rows[0]["content_text"] == updated.content_text
    assert rows[0]["author_label"] == "@poster"
    assert rows[0]["conversation_url"] == updated.url
    assert rows[0]["first_seen_at"] == first_observed.isoformat(timespec="seconds")
    assert rows[0]["last_seen_at"] == first_observed.isoformat(timespec="seconds")
    assert rows[0]["last_seen_at"] != updated.created_at
    assert not (tmp_path / "x").exists()
    assert XTextHistoryStore(path).cached_posts == 1

    later_observed = datetime(2026, 9, 25, 12, 1, tzinfo=UTC)
    with patch("app.core.x_text_history.datetime") as clock:
        clock.now.return_value = later_observed
        assert store.upsert([updated]) == 1
    rows = read_parquet_rows(path)
    assert rows is not None
    assert rows[0]["first_seen_at"] == first_observed.isoformat(timespec="seconds")
    assert rows[0]["last_seen_at"] == later_observed.isoformat(timespec="seconds")


def test_x_text_history_preserves_unreadable_file(tmp_path: Path) -> None:
    path = x_text_history_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not parquet")

    with pytest.raises(RuntimeError, match="not overwritten"):
        XTextHistoryStore(path)

    assert path.read_bytes() == b"not parquet"
