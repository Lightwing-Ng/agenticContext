"""Stage source-verified metadata timeline pins without reading media payloads.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3

from .wechat_archive import _file_identity, wechat_archive_path
from .wechat_export_merge import _clonefile
from .wechat_media_event_index import _local_index, _validated_conversations


VERSION = "v1.0.1-codex.0"
_RESERVE_BYTES = 16 * 1024**2


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(4 * 1024**2):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_membership(connection, conversations):
    """Reprove disjoint membership against the current primary, not the old pin."""
    if connection.execute(
        "SELECT 1 FROM media_event_index.messages e WHERE NOT EXISTS "
        "(SELECT 1 FROM media_event_index.conversations c "
        "WHERE c.id=e.conversation_id) LIMIT 1"
    ).fetchone():
        raise ValueError("A media event has no verified source conversation.")
    counts = dict(connection.execute(
        "SELECT conversation_id,COUNT(*) FROM media_event_index.messages "
        "GROUP BY conversation_id"
    ))
    expected = {row["id"]: row["message_count"] for row in conversations}
    if counts != expected:
        raise ValueError("Media event counts differ from their published summaries.")
    if connection.execute(
        "SELECT e.id FROM media_event_index.messages e WHERE EXISTS "
        "(SELECT 1 FROM main.messages m WHERE m.id=e.id) LIMIT 1"
    ).fetchone():
        raise ValueError("The current primary already contains a media event ID.")
    return sum(counts.values())


def stage_media_event_revalidation(local_store_root, candidate_path):
    """Create a reversible candidate; never publish it or mutate either input.

    The original account, snapshot, metadata, and member digests must still match
    the current primary. Every event remains disjoint from primary messages.
    Only the candidate's history pin changes. Ordinary reads stay fail-closed
    until a caller explicitly publishes the independently checked candidate.
    """
    history = wechat_archive_path(local_store_root).absolute()
    original, status = _local_index(history)
    if status != "ready":
        raise ValueError("Revalidation requires resident, idle metadata inputs.")
    candidate = Path(candidate_path).absolute()
    if candidate.exists() or candidate.is_symlink():
        raise FileExistsError("Keep existing revalidation candidates unchanged.")
    if not candidate.parent.is_dir() or candidate.parent.is_symlink():
        raise ValueError("Select an existing regular candidate directory.")
    if shutil.disk_usage(candidate.parent).free < _RESERVE_BYTES:
        raise ValueError("Insufficient space for a bounded copy-on-write update.")
    history_identity, original_identity = _file_identity(history), _file_identity(original)
    original_digest = _sha256(original)
    with closing(sqlite3.connect(
        history.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
    )) as source:
        source.execute("ATTACH DATABASE ? AS media_event_index", (
            original.resolve().as_uri() + "?mode=ro&immutable=1",
        ))
        info = dict(source.execute("SELECT key,value FROM media_event_index.archive_info"))
        prior_identity = json.loads(info.get("history_file_identity", "null"))
        if not isinstance(prior_identity, dict) or set(prior_identity) != set(history_identity):
            raise ValueError("The original metadata lacks a complete history receipt.")
        conversations = _validated_conversations(source, prior_identity)
        events = _validate_membership(source, conversations)
    if _file_identity(history) != history_identity or _file_identity(original) != original_identity:
        raise ValueError("A source changed during metadata revalidation.")
    try:
        _clonefile(original, candidate)
        candidate.chmod(0o600)
        if _sha256(candidate) != original_digest:
            raise ValueError("The metadata candidate is not an exact original clone.")
        with closing(sqlite3.connect(candidate)) as output:
            output.execute("PRAGMA journal_mode=DELETE")
            output.execute("PRAGMA synchronous=FULL")
            output.execute(
                "UPDATE archive_info SET value=? WHERE key='history_file_identity'",
                (json.dumps(history_identity, sort_keys=True, separators=(",", ":")),),
            )
            output.commit()
            if output.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("The metadata candidate failed its integrity check.")
        with closing(sqlite3.connect(
            history.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
        )) as source:
            source.execute("ATTACH DATABASE ? AS media_event_index", (
                candidate.resolve().as_uri() + "?mode=ro&immutable=1",
            ))
            verified = _validated_conversations(source, history_identity)
            if verified != conversations:
                raise ValueError("The metadata candidate changed conversation evidence.")
            # All non-pin bytes came from the exact original clone. The SQL update
            # above targets only the existing history receipt, never event rows.
            candidate_info = dict(source.execute(
                "SELECT key,value FROM media_event_index.archive_info"
            ))
            expected_info = {**info, "history_file_identity": candidate_info["history_file_identity"]}
            if candidate_info != expected_info:
                raise ValueError("The metadata candidate changed another receipt.")
        if _file_identity(history) != history_identity or _file_identity(original) != original_identity:
            raise ValueError("A source changed before metadata candidate completion.")
        with candidate.open("r+b") as stream:
            os.fsync(stream.fileno())
        return {
            "version": VERSION,
            "candidate": str(candidate),
            "source": str(original),
            "source_sha256": original_digest,
            "candidate_sha256": _sha256(candidate),
            "source_identity": original_identity,
            "prior_history_identity": prior_identity,
            "history_identity": history_identity,
            "conversations": len(conversations),
            "events": events,
            "media_payload_files_read": 0,
            "cloud_inputs_hydrated": 0,
            "published": False,
        }
    except BaseException:
        # Only this function's unpublished, reproducible candidate may be removed.
        candidate.unlink(missing_ok=True)
        raise
