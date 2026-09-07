"""Shared Parquet schemas and atomic persistence for cached resource state."""

# Code version: v1.9.2-codex.1

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq


CHATGPT_CATALOG_FILENAME = ".chatgpt_catalog.parquet"
LEGACY_CHATGPT_CATALOG_FILENAME = ".chatgpt_catalog.json"
GROK_CATALOG_FILENAME = ".grok_catalog.parquet"
LEGACY_GROK_CATALOG_FILENAME = ".grok_catalog.json"
GROK_DOWNLOAD_MANIFEST_FILENAME = ".grok_download_manifest.parquet"
LEGACY_GROK_DOWNLOAD_MANIFEST_FILENAME = ".grok_download_manifest.json"
GROK_WORK_QUEUE_FILENAME = ".grok_work_queue.parquet"
LEGACY_GROK_WORK_QUEUE_FILENAME = ".grok_work_queue.json"
DELETED_MEDIA_FILENAME = ".browser_deleted.parquet"
LEGACY_DELETED_MEDIA_FILENAME = ".browser_deleted.json"
X_CACHE_CATALOG_FILENAME = ".cache_catalog.parquet"
GEMINI_HISTORY_FILENAME = "history.parquet"
CHATGPT_HISTORY_FILENAME = "history.parquet"
GROK_HISTORY_FILENAME = "history.parquet"
CLAUDE_HISTORY_FILENAME = "history.parquet"
PROMPT_FILENAME = "prompts.parquet"
PROMPT_REMARKS_FILENAME = "remarks.parquet"

CHATGPT_CATALOG_SCHEMA_VERSION = 2
GROK_CATALOG_SCHEMA_VERSION = 2
GROK_DOWNLOAD_MANIFEST_SCHEMA_VERSION = 2
GROK_WORK_QUEUE_SCHEMA_VERSION = 2
DELETED_MEDIA_SCHEMA_VERSION = 2
X_CACHE_CATALOG_SCHEMA_VERSION = 3
GEMINI_HISTORY_SCHEMA_VERSION = 1
CHATGPT_HISTORY_SCHEMA_VERSION = 3
GROK_HISTORY_SCHEMA_VERSION = 1
CLAUDE_HISTORY_SCHEMA_VERSION = 1
PROMPT_SCHEMA_VERSION = 2
PROMPT_REMARKS_SCHEMA_VERSION = 1


CHATGPT_CATALOG_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.int16(), nullable=False),
        pa.field("file_id", pa.string(), nullable=False),
        pa.field("relative_path", pa.string(), nullable=False),
        pa.field("content_sha256", pa.string(), nullable=False),
        pa.field("content_bytes", pa.int64(), nullable=False),
        pa.field("source_url", pa.string(), nullable=False),
        pa.field("conversation_url", pa.string(), nullable=False),
        pa.field("alt_text", pa.string(), nullable=False),
        pa.field("width", pa.int32(), nullable=False),
        pa.field("height", pa.int32(), nullable=False),
        pa.field("first_seen_at", pa.string(), nullable=False),
        pa.field("last_seen_at", pa.string(), nullable=False),
        pa.field("prompt_markdown", pa.string(), nullable=False),
        pa.field("conversation_title", pa.string(), nullable=False),
        pa.field("created_at", pa.string(), nullable=False),
        pa.field("visual_signature", pa.string(), nullable=False),
    ]
)

GROK_CATALOG_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.int16(), nullable=False),
        pa.field("identity", pa.string(), nullable=False),
        pa.field("relative_path", pa.string(), nullable=False),
        pa.field("media_kind", pa.string(), nullable=False),
        pa.field("content_sha256", pa.string(), nullable=False),
        pa.field("content_bytes", pa.int64(), nullable=False),
        pa.field("source_url", pa.string(), nullable=False),
        pa.field("first_seen_at", pa.string(), nullable=False),
        pa.field("last_seen_at", pa.string(), nullable=False),
    ]
)

GROK_DOWNLOAD_MANIFEST_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.int16(), nullable=False),
        pa.field("identity", pa.string(), nullable=False),
        pa.field("asset_id", pa.string(), nullable=False),
        pa.field("asset_name", pa.string(), nullable=False),
        pa.field("media_kind", pa.string(), nullable=False),
        pa.field("source_url", pa.string(), nullable=False),
        pa.field("status", pa.string(), nullable=False),
        pa.field("relative_path", pa.string(), nullable=False),
        pa.field("temp_relative_path", pa.string(), nullable=False),
        pa.field("content_sha256", pa.string(), nullable=False),
        pa.field("content_bytes", pa.int64(), nullable=False),
        pa.field("expected_bytes", pa.int64(), nullable=False),
        pa.field("created_at", pa.string(), nullable=False),
        pa.field("attempts", pa.int32(), nullable=False),
        pa.field("last_error", pa.string(), nullable=False),
        pa.field("updated_at", pa.string(), nullable=False),
    ]
)

GROK_WORK_QUEUE_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.int16(), nullable=False),
        pa.field("asset_id", pa.string(), nullable=False),
        pa.field("identity", pa.string(), nullable=False),
        pa.field("asset_name", pa.string(), nullable=False),
        pa.field("media_kind", pa.string(), nullable=False),
        pa.field("source_url", pa.string(), nullable=False),
        pa.field("preview_url", pa.string(), nullable=False),
        pa.field("expected_bytes", pa.int64(), nullable=False),
        pa.field("created_at", pa.string(), nullable=False),
        pa.field("discovered_at", pa.string(), nullable=False),
        pa.field("updated_at", pa.string(), nullable=False),
        pa.field("status", pa.string(), nullable=False),
        pa.field("resolution_attempts", pa.int32(), nullable=False),
        pa.field("download_attempts", pa.int32(), nullable=False),
        pa.field("last_error", pa.string(), nullable=False),
    ]
)

DELETED_MEDIA_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.int16(), nullable=False),
        pa.field("stable_id", pa.string(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
        pa.field("resource_key", pa.string(), nullable=False),
        pa.field("original_relative_path", pa.string(), nullable=False),
        pa.field("preview_relative_path", pa.string(), nullable=False),
        pa.field("deleted_at", pa.string(), nullable=False),
        pa.field("media_kind", pa.string(), nullable=False),
        pa.field("filename", pa.string(), nullable=False),
        pa.field("title", pa.string(), nullable=False),
        pa.field("description", pa.string(), nullable=False),
        pa.field("creator", pa.string(), nullable=False),
        pa.field("source_url", pa.string(), nullable=False),
        pa.field("captured_at", pa.string(), nullable=False),
        pa.field("captured_at_label", pa.string(), nullable=False),
        pa.field("content_bytes", pa.int64(), nullable=False),
        pa.field("project_name", pa.string(), nullable=False),
        pa.field("alt_text", pa.string(), nullable=False),
        pa.field("prompt_markdown", pa.string(), nullable=False),
        pa.field("width", pa.int32(), nullable=False),
        pa.field("height", pa.int32(), nullable=False),
        pa.field("chatgpt_session_key", pa.string(), nullable=False),
        pa.field("chatgpt_branch_key", pa.string(), nullable=False),
    ]
)

PROMPT_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.int16(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
        pa.field("conversation_id", pa.string(), nullable=False),
        pa.field("message_key", pa.string(), nullable=False),
        pa.field("content_text", pa.string(), nullable=False),
        pa.field("conversation_title", pa.string(), nullable=False),
        pa.field("conversation_url", pa.string(), nullable=False),
        pa.field("author_label", pa.string(), nullable=False),
        pa.field("captured_at", pa.string(), nullable=False),
        pa.field("added_at", pa.string(), nullable=False),
    ]
)

PROMPT_LEGACY_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.int16(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
        pa.field("conversation_id", pa.string(), nullable=False),
        pa.field("message_key", pa.string(), nullable=False),
        pa.field("added_at", pa.string(), nullable=False),
    ]
)

PROMPT_REMARKS_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.int16(), nullable=False),
        pa.field("prompt_id", pa.string(), nullable=False),
        pa.field("remark", pa.string(), nullable=False),
    ]
)

X_CACHE_CATALOG_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.int16(), nullable=False),
        pa.field("relative_tweet_dir", pa.string(), nullable=False),
        pa.field("canonical_urls", pa.list_(pa.string()), nullable=False),
        pa.field("status_ids", pa.list_(pa.string()), nullable=False),
        pa.field("image_count", pa.int32(), nullable=False),
        pa.field("video_count", pa.int32(), nullable=False),
        pa.field("title", pa.string(), nullable=False),
        pa.field("description", pa.string(), nullable=False),
        pa.field("uploader", pa.string(), nullable=False),
        pa.field("uploader_id", pa.string(), nullable=False),
        pa.field("display_id", pa.string(), nullable=False),
        pa.field("webpage_url", pa.string(), nullable=False),
        pa.field("timestamp", pa.float64()),
        pa.field("upload_date", pa.string(), nullable=False),
        pa.field("resource_type", pa.string(), nullable=False),
    ]
)

GEMINI_HISTORY_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.int16(), nullable=False),
        pa.field("platform", pa.string(), nullable=False),
        pa.field("conversation_id", pa.string(), nullable=False),
        pa.field("conversation_url", pa.string(), nullable=False),
        pa.field("conversation_title", pa.string(), nullable=False),
        pa.field("message_key", pa.string(), nullable=False),
        pa.field("turn_index", pa.int32(), nullable=False),
        pa.field("message_index", pa.int32(), nullable=False),
        pa.field("role", pa.string(), nullable=False),
        pa.field("author_label", pa.string(), nullable=False),
        pa.field("content_text", pa.string(), nullable=False),
        pa.field("content_html", pa.string(), nullable=False),
        pa.field("content_sha256", pa.string(), nullable=False),
        pa.field("source_links", pa.list_(pa.string()), nullable=False),
        pa.field("model_label", pa.string(), nullable=False),
        pa.field("first_seen_at", pa.string(), nullable=False),
        pa.field("last_seen_at", pa.string(), nullable=False),
    ]
)

CHATGPT_HISTORY_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.int16(), nullable=False),
        pa.field("provider_revision", pa.string(), nullable=True),
        pa.field("platform", pa.string(), nullable=False),
        pa.field("conversation_id", pa.string(), nullable=False),
        pa.field("conversation_url", pa.string(), nullable=False),
        pa.field("conversation_title", pa.string(), nullable=False),
        pa.field("message_key", pa.string(), nullable=False),
        pa.field("turn_index", pa.int32(), nullable=False),
        pa.field("message_index", pa.int32(), nullable=False),
        pa.field("role", pa.string(), nullable=False),
        pa.field("author_label", pa.string(), nullable=False),
        pa.field("content_text", pa.string(), nullable=False),
        pa.field("content_html", pa.string(), nullable=False),
        pa.field("content_sha256", pa.string(), nullable=False),
        pa.field("source_links", pa.list_(pa.string()), nullable=False),
        pa.field("model_label", pa.string(), nullable=False),
        pa.field("first_seen_at", pa.string(), nullable=False),
        pa.field("last_seen_at", pa.string(), nullable=False),
    ]
)

GROK_HISTORY_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.int16(), nullable=False),
        pa.field("platform", pa.string(), nullable=False),
        pa.field("conversation_id", pa.string(), nullable=False),
        pa.field("conversation_url", pa.string(), nullable=False),
        pa.field("conversation_title", pa.string(), nullable=False),
        pa.field("message_key", pa.string(), nullable=False),
        pa.field("turn_index", pa.int32(), nullable=False),
        pa.field("message_index", pa.int32(), nullable=False),
        pa.field("role", pa.string(), nullable=False),
        pa.field("author_label", pa.string(), nullable=False),
        pa.field("content_text", pa.string(), nullable=False),
        pa.field("content_html", pa.string(), nullable=False),
        pa.field("content_sha256", pa.string(), nullable=False),
        pa.field("source_links", pa.list_(pa.string()), nullable=False),
        pa.field("model_label", pa.string(), nullable=False),
        pa.field("first_seen_at", pa.string(), nullable=False),
        pa.field("last_seen_at", pa.string(), nullable=False),
    ]
)

CLAUDE_HISTORY_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.int16(), nullable=False),
        pa.field("platform", pa.string(), nullable=False),
        pa.field("conversation_id", pa.string(), nullable=False),
        pa.field("conversation_url", pa.string(), nullable=False),
        pa.field("conversation_title", pa.string(), nullable=False),
        pa.field("message_key", pa.string(), nullable=False),
        pa.field("turn_index", pa.int32(), nullable=False),
        pa.field("message_index", pa.int32(), nullable=False),
        pa.field("role", pa.string(), nullable=False),
        pa.field("author_label", pa.string(), nullable=False),
        pa.field("content_text", pa.string(), nullable=False),
        pa.field("content_html", pa.string(), nullable=False),
        pa.field("content_sha256", pa.string(), nullable=False),
        pa.field("source_links", pa.list_(pa.string()), nullable=False),
        pa.field("model_label", pa.string(), nullable=False),
        pa.field("first_seen_at", pa.string(), nullable=False),
        pa.field("last_seen_at", pa.string(), nullable=False),
    ]
)


def _parquet_cleanup_note(action: str, path: Path, failure: BaseException) -> str:
    detail = " ".join(str(failure).split())[:240]
    name = str(path)
    candidate = name if len(name) <= 480 else "..." + name[-477:]
    return f"{action} (candidate: {candidate}): {type(failure).__name__}: {detail}"


def _read_parquet_table(path: Path) -> pa.Table:
    """Close a single-file reader before returning its materialized table."""
    reader = pq.ParquetFile(path, pre_buffer=False)
    primary_error = None
    try:
        return reader.read()
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            reader.close()
        except BaseException as exc:
            if primary_error is None:
                raise
            primary_error.add_note(_parquet_cleanup_note(
                "Closing the Parquet reader also failed", path, exc,
            ))
            if exc is not primary_error:
                for note in getattr(exc, "__notes__", ()):
                    primary_error.add_note(note)


def read_parquet_rows(path: Path) -> list[dict[str, Any]] | None:
    """Read rows from one Parquet state file, returning None when it is unavailable or invalid."""
    if not path.is_file():
        return None
    try:
        return [dict(row) for row in _read_parquet_table(path).to_pylist()]
    except (OSError, ValueError, pa.ArrowException):
        return None


def write_parquet_rows_atomic(
    path: Path,
    rows: Iterable[Mapping[str, Any]],
    schema: pa.Schema,
) -> None:
    """Atomically persist typed rows and verify the temporary Parquet file before replacement."""
    materialized_rows = [dict(row) for row in rows]
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    primary_error = None
    published = False
    try:
        table = pa.Table.from_pylist(materialized_rows, schema=schema)
        pq.write_table(table, temporary_path, compression="zstd")
        verified = _read_parquet_table(temporary_path)
        if verified.num_rows != len(materialized_rows) or verified.schema.names != schema.names:
            raise RuntimeError(f"Parquet verification failed for {path}.")
        os.replace(temporary_path, path)
        published = True
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if not published:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError as exc:
                if primary_error is None:
                    raise
                primary_error.add_note(_parquet_cleanup_note(
                    "Removing the unpublished Parquet temporary file also failed; "
                    "the candidate may remain", temporary_path, exc,
                ))


def retire_legacy_file(path: Path) -> bool:
    """Remove one superseded legacy state file after its Parquet replacement is durable."""
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True
