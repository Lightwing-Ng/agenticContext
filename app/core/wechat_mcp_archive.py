"""Read-only WeChat archive queries for the Tunnel MCP tools.

Code version: v1.1.1-codex.0

The tools address only the configured ``local_store/messaging/wechat`` archive.
Callers cannot pass a filesystem path. Connections use SQLite ``mode=ro`` and
``immutable=1``. Duplicate imports stay as separate references; display-name
matches stay as separate contacts.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.core.wechat_archive import (
    WechatArchiveRepository,
    WechatTimelineProjectionError,
    _file_identity,
    wechat_archive_path,
)
from app.core.wechat_contact_analysis import (
    INDEX_VERSION,
    ContactAnalysisSourceChanged,
    _attach_reference_archive,
    _cache_ready,
    _check_reference_sources,
    _project_reference_rows,
    _sources,
    contact_analysis_cache_path,
)
from app.core.wechat_contacts import (
    WechatContactRepository,
    canonical_contact_id,
    contact_registry_path,
)
from app.core.wechat_display_names import WechatDisplayNames


VERSION = "v1.1.1-codex.0"
QUERY_TIMEOUT_SECONDS = 10.0
MAX_PAGE_SIZE = 50
MAX_TEXT_CHARACTERS = 20_000
MAX_CONTACTS = 50
MAX_EVIDENCE = 8
MAX_CONTEXT = 20
_CONTACT_ID = re.compile(r"wechat:contact:[0-9a-f]{64}")
_MESSAGE_ID = re.compile(r"[0-9a-f]{64}")
_DISPLAY_ZONE = timezone(timedelta(hours=8))
_SIDECARS = ("-wal", "-journal", "-shm")
_PROVENANCE_KEYS = (
    "record_id",
    "server_id",
    "unix_timestamp",
    "source_time",
    "source_snapshot",
    "parent_message_id",
    "component_path",
    "component_locator",
    "component_export_msgid",
    "identity_basis",
    "original_source_ids",
    "locator",
)


class WechatArchiveQueryError(Exception):
    """A stable archive-query failure that must not look like an empty result."""

    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


def archive_order_key(value: str) -> float:
    """Match ``wechat_archive._time_fields`` ordering for one ISO timestamp."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(_DISPLAY_ZONE)
    ordering = parsed.replace(tzinfo=UTC).timestamp()
    if not math.isfinite(ordering):
        raise ValueError("Invalid source time")
    return ordering


def archive_status(local_store_root: Path | str) -> dict[str, Any]:
    """Describe the configured archive without scanning message bodies."""
    root = _authorized_root(local_store_root)
    history, contacts = _required_databases(root)
    publication = _publication_snapshot(root)
    history_identity = _file_identity(history)
    contact_identity = _file_identity(contacts)
    try:
        with _readonly(history) as connection:
            _require_tables(
                connection,
                "archive",
                {"messages", "conversations", "partitions", "archive_info"},
            )
            info = dict(connection.execute("SELECT key, value FROM archive_info"))
            partitions = [
                _public_partition(row)
                for row in connection.execute(
                    "SELECT id, account_label, identity_status, source_label, source_file "
                    "FROM partitions ORDER BY id"
                )
            ]
            coverage = _archive_coverage(connection)
        with _readonly(contacts) as connection:
            _require_tables(connection, "contact registry", {"contacts", "contact_registry_info"})
            registry = dict(connection.execute("SELECT key, value FROM contact_registry_info"))
    except sqlite3.DatabaseError as exc:
        raise _unreadable(exc) from exc
    _require_unchanged(history, history_identity)
    _require_unchanged(contacts, contact_identity)
    _require_publication_unchanged(root, publication)
    manifest = _object_or_empty(info.get("manifest"))
    registry_manifest = _object_or_empty(registry.get("manifest"))
    return {
        "ok": True,
        "interface_version": VERSION,
        "index_version": INDEX_VERSION,
        "history": _file_record(history, history_identity),
        "contacts": _file_record(contacts, contact_identity),
        "resource_index": _resource_record(history.with_name("resource_references.sqlite3")),
        "schema_version": manifest.get("schema_version"),
        "message_count": manifest.get("messages"),
        "sources": manifest.get("sources") if isinstance(manifest.get("sources"), list) else partitions,
        "partitions": partitions,
        "contact_registry_schema_version": registry.get("schema_version"),
        "contact_registry_published_at": registry_manifest.get("at"),
        "coverage": coverage,
        "updated_at": _iso_mtime(history_identity["mtime_ns"]),
    }


def find_contacts(
    local_store_root: Path | str,
    *,
    query: str,
    match: str = "exact",
    limit: int = 20,
) -> dict[str, Any]:
    """Return each matching contact separately, with the evidence that selected it."""
    if match not in {"exact", "contains"}:
        raise WechatArchiveQueryError("invalid_arguments", "match must be exact or contains.")
    text = _bounded_query(query)
    if match == "contains" and len(text) < 2:
        raise WechatArchiveQueryError(
            "invalid_arguments",
            "A contains search needs at least two characters.",
        )
    size = _page_size(limit, maximum=MAX_CONTACTS)
    root = _authorized_root(local_store_root)
    _history, contacts = _required_databases(root)
    publication = _publication_snapshot(root)
    fingerprint = _source_fingerprint(root)
    names = WechatDisplayNames(root)
    try:
        with _readonly(contacts) as connection:
            connection.execute("PRAGMA query_only=ON")
            identifiers = _matching_contact_ids(connection, text, match, size + 1)
            preferred_ids = [
                contact_id
                for contact_id, preferred in names.names.items()
                if preferred == text or (match == "contains" and text in preferred)
            ]
            merged: list[str] = []
            seen: set[str] = set()
            for contact_id in [*identifiers, *preferred_ids]:
                if contact_id in seen:
                    continue
                seen.add(contact_id)
                merged.append(contact_id)
            contacts_by_id = _contact_rows(connection, merged[:size])
            evidence = {
                contact_id: _matching_evidence(connection, contact_id, text, match)
                for contact_id in contacts_by_id
            }
            authorities = {
                contact_id: _identifier_rows(connection, contact_id)
                for contact_id in contacts_by_id
            }
    except sqlite3.DatabaseError as exc:
        raise _unreadable(exc) from exc
    results = []
    for contact_id, row in contacts_by_id.items():
        profile = _contact_profile(row, names, authorities.get(contact_id, []))
        items = list(evidence[contact_id]["items"])
        preferred = names.names.get(contact_id, "")
        if preferred and (preferred == text or (match == "contains" and text in preferred)):
            items.insert(
                0,
                {
                    "kind": "preferred_display_name",
                    "value": preferred,
                    "source_id": "display_names.json",
                    "owner_account_id": "",
                    "room_id": "",
                    "authority": "local_display_preference",
                },
            )
        if not items and profile["username"] == text:
            items.append(
                {
                    "kind": "username",
                    "value": profile["username"],
                    "source_id": "",
                    "owner_account_id": "",
                    "room_id": "",
                    "authority": "contact_username",
                }
            )
        profile["evidence"] = items[:MAX_EVIDENCE]
        profile["evidence_count"] = evidence[contact_id]["count"] + (
            1 if items[:1] and items[0]["kind"] == "preferred_display_name" else 0
        )
        profile["evidence_truncated"] = (
            evidence[contact_id]["truncated"] or len(items) > MAX_EVIDENCE
        )
        profile["match_quality"] = "exact" if match == "exact" else "substring"
        profile.update(_conversation_count(root, contact_id, fingerprint))
        results.append(profile)
    results.sort(key=lambda item: (item["identity_match"] != "verified", item["contact_id"]))
    _check_reference_sources(root, fingerprint)
    _require_publication_unchanged(root, publication)
    return {
        "ok": True,
        "query": text,
        "match": match,
        "contacts": results,
        "truncated": len(identifiers) > size or len(merged) > size,
        "identity_note": (
            "Each contact_id is a separate platform identity. "
            "Equal display names are not the same account."
        ),
    }


def search_messages(
    local_store_root: Path | str,
    *,
    contact_id: str | None = None,
    conversation_id: str | None = None,
    query: str = "",
    not_before: str | None = None,
    not_after: str | None = None,
    sort: str = "newest",
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """Page messages for one verified contact or one conversation."""
    if sort not in {"newest", "oldest"}:
        raise WechatArchiveQueryError("invalid_arguments", "sort must be newest or oldest.")
    if not contact_id and not conversation_id:
        raise WechatArchiveQueryError(
            "invalid_arguments",
            "Provide a contact_id or a conversation_id. A keyword alone is not a bounded query.",
        )
    text = _bounded_query(query, allow_empty=True)
    selected_page = _page_number(page)
    size = _page_size(page_size, maximum=MAX_PAGE_SIZE)
    start, end = _time_bounds(not_before, not_after)
    root = _authorized_root(local_store_root)
    _required_databases(root)
    if contact_id:
        return _search_contact(
            root,
            contact_id=contact_id,
            conversation_id=conversation_id or "",
            query=text,
            start=start,
            end=end,
            sort=sort,
            page=selected_page,
            page_size=size,
        )
    return _search_conversation(
        root,
        conversation_id=str(conversation_id),
        query=text,
        start=start,
        end=end,
        sort=sort,
        page=selected_page,
        page_size=size,
    )


def message_context(
    local_store_root: Path | str,
    *,
    message_id: str,
    before: int = 5,
    after: int = 5,
) -> dict[str, Any]:
    """Return one archive message and the neighboring messages in its conversation."""
    identifier = _message_id(message_id)
    before_count = _context_count(before)
    after_count = _context_count(after)
    root = _authorized_root(local_store_root)
    _required_databases(root)
    publication = _publication_snapshot(root)
    try:
        with _archive_connection(root, message_id=identifier) as (
            connection,
            message_table,
            conversation_table,
            identity,
            history,
            resource_status,
        ):
            target = _message_row(
                connection, message_table, conversation_table, identifier
            )
            if target is None:
                raise WechatArchiveQueryError(
                    "not_found",
                    "No archive message has that id. Reference ids that are not archive "
                    "message ids need the parent_message_id returned with the reference.",
                    message_id=identifier,
                )
            previous = _neighbors(
                connection,
                message_table,
                conversation_table,
                target,
                direction="before",
                limit=before_count,
            )
            following = _neighbors(
                connection,
                message_table,
                conversation_table,
                target,
                direction="after",
                limit=after_count,
            )
            _require_unchanged(history, identity)
    except sqlite3.DatabaseError as exc:
        raise _unreadable(exc) from exc
    rows = [*reversed(previous), target, *following]
    messages = [
        _public_archive_message(row, is_target=row["id"] == identifier) for row in rows
    ]
    _attach_sender_contacts(root, messages, evidence_rows=rows)
    _require_publication_unchanged(root, publication)
    return {
        "ok": True,
        "message_id": identifier,
        "conversation_id": target["conversation_id"],
        "target_index": len(previous),
        "before": len(previous),
        "after": len(following),
        "resource_index_status": resource_status,
        "messages": messages,
    }


def _search_contact(
    root: Path,
    *,
    contact_id: str,
    conversation_id: str,
    query: str,
    start: float | None,
    end: float | None,
    sort: str,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    if _CONTACT_ID.fullmatch(contact_id) is None:
        raise WechatArchiveQueryError(
            "invalid_arguments",
            "contact_id must be a canonical wechat:contact:<64 hex> id.",
        )
    if conversation_id and len(conversation_id) > 200:
        raise WechatArchiveQueryError("invalid_arguments", "conversation_id is too long.")
    fingerprint = _source_fingerprint(root)
    publication = _publication_snapshot(root)
    if not isinstance(fingerprint, str):
        raise WechatArchiveQueryError(
            "archive_missing",
            "The configured WeChat history or contact registry is missing.",
        )
    contact = _load_contact(root, contact_id)
    if contact["identity_match"] != "verified":
        raise WechatArchiveQueryError(
            "identity_unverified",
            "This contact id is not a verified platform identity. "
            "The messages were not searched or merged.",
            contact=contact,
        )
    cache = contact_analysis_cache_path(root, contact_id)
    problem = _index_problem(cache, fingerprint)
    if problem is not None:
        code, message = problem
        raise WechatArchiveQueryError(code, message, contact=contact)
    cache_identity = _file_identity(cache)
    history = wechat_archive_path(root)
    names = WechatDisplayNames(root)
    try:
        with _readonly(cache) as connection:
            verified_accounts = _attach_reference_archive(connection, history)
            where, parameters = _reference_filters(
                query, conversation_id, start, end
            )
            total = connection.execute(
                "SELECT COUNT(*) FROM refs r "
                + (
                    "CROSS JOIN archive.messages m ON m.id=r.parent_message_id "
                    "JOIN archive.conversations c ON c.id=r.conversation_id "
                    "JOIN archive.partitions p ON p.id=r.source_id "
                    if query
                    else ""
                )
                + where,
                parameters,
            ).fetchone()[0]
            unknown_time_excluded = 0
            if start is not None or end is not None:
                unknown_parameters = [conversation_id] if conversation_id else []
                unknown_time_excluded = connection.execute(
                    "SELECT COUNT(*) FROM refs r WHERE r.has_time=0"
                    + (" AND r.conversation_id=?" if conversation_id else ""),
                    unknown_parameters,
                ).fetchone()[0]
            pages = max(1, math.ceil(total / page_size)) if total else 1
            direction = "ASC" if sort == "oldest" else "DESC"
            rows = connection.execute(
                "SELECT m.*,c.title AS conversation_title,c.kind,c.peer_id,r.source_id,"
                "r.account_key,p.account_id,p.account_label,p.identity_status,p.source_label,"
                "p.source_file,r.id AS reference_id,r.parent_message_id,r.component_path,"
                "r.anchor_id,r.time_sort AS reference_time_sort,"
                "r.timestamp_value AS reference_timestamp_value,"
                "r.timestamp_label AS reference_timestamp_label,"
                "r.time_basis AS reference_time_basis,r.source_clock "
                "FROM refs r CROSS JOIN archive.messages m ON m.id=r.parent_message_id "
                "JOIN archive.conversations c ON c.id=r.conversation_id "
                "JOIN archive.partitions p ON p.id=r.source_id"
                + where
                + " ORDER BY r.has_time DESC,r.time_sort "
                + direction
                + ",r.ordinal "
                + direction
                + ",r.id "
                + direction
                + " LIMIT ? OFFSET ?",
                [*parameters, page_size, (page - 1) * page_size],
            ).fetchall()
            projected = _project_reference_rows(
                connection,
                rows,
                {"id": contact_id},
                names,
                verified_accounts,
            )
            parents = {row["id"]: dict(row) for row in rows}
            for item in projected:
                parent = parents[item["parent_message_id"]]
                item["_source_text_sha256"] = hashlib.sha256(parent["text"].encode()).hexdigest()
            embedded_contacts = _forwarded_sender_contacts(root, projected, parents)
        _check_reference_sources(root, fingerprint)
    except ContactAnalysisSourceChanged as exc:
        raise WechatArchiveQueryError("source_changed", str(exc), contact=contact) from exc
    except sqlite3.DatabaseError as exc:
        raise _unreadable(exc) from exc
    messages = [_public_reference(item) for item in projected]
    _attach_sender_contacts(
        root, messages, evidence_rows=projected, embedded_contacts=embedded_contacts,
    )
    _require_unchanged(cache, cache_identity)
    _require_database(cache, root)
    _check_reference_sources(root, fingerprint)
    _require_publication_unchanged(root, publication)
    return {
        "ok": True,
        "contact": contact,
        "conversation_id": conversation_id or None,
        "query": query,
        "sort": sort,
        "page": page,
        "page_size": page_size,
        "total_count": total,
        "total_pages": pages,
        "unknown_time_excluded": unknown_time_excluded,
        "messages": messages,
    }


def _search_conversation(
    root: Path,
    *,
    conversation_id: str,
    query: str,
    start: float | None,
    end: float | None,
    sort: str,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    if not conversation_id or len(conversation_id) > 200:
        raise WechatArchiveQueryError("invalid_arguments", "conversation_id is required.")
    _required_databases(root)
    publication = _publication_snapshot(root)
    try:
        with _archive_connection(root, conversation_id=conversation_id) as (
            connection,
            message_table,
            conversation_table,
            identity,
            history,
            resource_status,
        ):
            filters = ["m.conversation_id=?"]
            parameters: list[Any] = [conversation_id]
            if query:
                filters.append("instr(m.search_text,?)>0")
                parameters.append(query.casefold())
            if start is not None:
                filters.append("m.time_sort>=?")
                parameters.append(start)
            if end is not None:
                filters.append("m.time_sort<=?")
                parameters.append(end)
            if start is not None or end is not None:
                filters.append("m.time_sort>0")
            where = " WHERE " + " AND ".join(filters)
            joined = (
                f" FROM {message_table} m JOIN {conversation_table} c ON c.id=m.conversation_id "
                "JOIN main.partitions p ON p.id=c.partition_id"
            )
            total = connection.execute("SELECT COUNT(*)" + joined + where, parameters).fetchone()[0]
            exists = connection.execute(
                f"SELECT 1 FROM {conversation_table} WHERE id=?",
                (conversation_id,),
            ).fetchone()
            if exists is None:
                raise WechatArchiveQueryError(
                    "not_found",
                    "No conversation has that id in the current archive.",
                    conversation_id=conversation_id,
                )
            pages = max(1, math.ceil(total / page_size)) if total else 1
            direction = "ASC" if sort == "oldest" else "DESC"
            rows = connection.execute(
                "SELECT m.*,c.title AS conversation_title,c.kind AS conversation_kind,c.peer_id,"
                "c.partition_id AS source_id,p.account_id,p.account_label,"
                "p.identity_status AS source_identity_status,p.source_label,p.source_file"
                + joined
                + where
                + " ORDER BY (m.time_sort=0) ASC,m.time_sort "
                + direction
                + ",m.ordinal "
                + direction
                + ",m.id "
                + direction
                + " LIMIT ? OFFSET ?",
                [*parameters, page_size, (page - 1) * page_size],
            ).fetchall()
            _require_unchanged(history, identity)
    except sqlite3.DatabaseError as exc:
        raise _unreadable(exc) from exc
    messages = [_public_archive_message(row) for row in rows]
    _attach_sender_contacts(root, messages, evidence_rows=rows)
    _require_publication_unchanged(root, publication)
    return {
        "ok": True,
        "contact": None,
        "conversation_id": conversation_id,
        "query": query,
        "sort": sort,
        "page": page,
        "page_size": page_size,
        "total_count": total,
        "total_pages": pages,
        "unknown_time_excluded": None,
        "resource_index_status": resource_status,
        "messages": messages,
        "identity_note": "This page is scoped to a conversation, not to one verified contact.",
    }


def _authorized_root(local_store_root: Path | str) -> Path:
    root = Path(local_store_root).expanduser()
    if root.is_symlink():
        raise WechatArchiveQueryError(
            "archive_refused",
            "The configured local store root is a symbolic link.",
        )
    if not root.is_dir():
        raise WechatArchiveQueryError(
            "archive_unavailable",
            "The configured local store root is not available.",
        )
    current = root.resolve()
    for part in ("messaging", "wechat"):
        current = current / part
        if current.is_symlink():
            raise WechatArchiveQueryError(
                "archive_refused",
                "The WeChat archive path contains a symbolic link.",
            )
        if not current.exists():
            raise WechatArchiveQueryError(
                "archive_missing",
                "The configured WeChat archive directory is missing.",
                missing="messaging/wechat",
            )
        if not current.is_dir():
            raise WechatArchiveQueryError(
                "archive_refused",
                "The configured WeChat archive path is not a directory.",
            )
    resolved = current.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise WechatArchiveQueryError(
            "archive_refused",
            "The WeChat archive path escapes the configured local store.",
        )
    return root.resolve()


def _required_databases(root: Path) -> tuple[Path, Path]:
    history = wechat_archive_path(root)
    contacts = contact_registry_path(root)
    missing = [
        name
        for name, path in (("history.sqlite3", history), ("contacts.sqlite3", contacts))
        if not path.exists()
    ]
    if missing:
        raise WechatArchiveQueryError(
            "archive_missing",
            "The configured WeChat archive is missing a required database.",
            missing=missing,
        )
    _require_database(history, root)
    _require_database(contacts, root)
    return history, contacts


def _publication_snapshot(root: Path) -> dict[str, tuple | None]:
    """Pin every publication used by the query without hydrating optional files."""
    directory = wechat_archive_path(root).parent
    snapshot = {}
    for name in (
        "history.sqlite3", "contacts.sqlite3", "resource_references.sqlite3",
        "display_names.json", "media_placeholders.json",
    ):
        path = directory / name
        if path.is_symlink():
            raise WechatArchiveQueryError("archive_refused", "An archive publication is linked.")
        if not path.exists():
            snapshot[name] = None
            continue
        state = path.stat()
        if not path.is_file() or state.st_nlink != 1:
            raise WechatArchiveQueryError(
                "archive_refused", "Archive publications must be unlinked regular files."
            )
        snapshot[name] = (
            state.st_dev, state.st_ino, state.st_size, state.st_mtime_ns,
            state.st_ctime_ns, getattr(state, "st_flags", 0),
        )
        if path.suffix == ".sqlite3":
            for suffix in _SIDECARS:
                if Path(str(path) + suffix).exists():
                    raise WechatArchiveQueryError(
                        "source_changed", "An archive publication has an unpublished journal."
                    )
    return snapshot


def _require_publication_unchanged(root: Path, snapshot: dict) -> None:
    try:
        current = _publication_snapshot(root)
    except (OSError, WechatArchiveQueryError) as exc:
        raise WechatArchiveQueryError(
            "source_changed", "An archive publication changed while the query was running."
        ) from exc
    if current != snapshot:
        raise WechatArchiveQueryError(
            "source_changed", "An archive publication changed while the query was running."
        )


def _archive_coverage(connection: sqlite3.Connection) -> dict[str, Any]:
    """Read published conversation bounds, then seek only their endpoint metadata."""
    summary = connection.execute(
        "SELECT COUNT(*) AS conversations,COALESCE(SUM(message_count),0) AS messages,"
        "COALESCE(SUM(first_sort!=0 OR last_sort!=0),0) AS timed FROM conversations"
    ).fetchone()
    coverage = {
        "status": "ready" if summary["messages"] else "empty",
        "scope": "published_history",
        "basis": "published_conversation_summaries",
        "conversation_count": summary["conversations"],
        "message_count": summary["messages"],
        "timed_conversation_count": summary["timed"],
        "undated_conversation_count": summary["conversations"] - summary["timed"],
        "limitations": (
            "Bounds retain the archive's source-time ordering and original time bases; "
            "they are not verified real-world event dates. Zero is the archive's unknown-time "
            "sentinel. Counts describe published history, excluding optional resource and "
            "media overlays. Unknown-time message and anomaly totals are not scanned."
        ),
    }
    for endpoint, column, direction in (
        ("first", "first_sort", "ASC"), ("last", "last_sort", "DESC"),
    ):
        bound = connection.execute(
            f"SELECT id,{column} AS ordering FROM conversations WHERE {column}!=0 "
            f"ORDER BY {column} {direction},id {direction} LIMIT 1"
        ).fetchone()
        row = None
        if bound is not None:
            row = connection.execute(
                "SELECT timestamp_value,time_basis,quality_flags FROM messages "
                "WHERE conversation_id=? AND time_sort=? "
                f"ORDER BY ordinal {direction},id {direction} LIMIT 1",
                (bound["id"], bound["ordering"]),
            ).fetchone()
            if row is None:
                raise WechatArchiveQueryError(
                    "archive_incomplete", "A published coverage endpoint has no source message."
                )
        coverage[f"{endpoint}_timestamp_value"] = row["timestamp_value"] if row else None
        coverage[f"{endpoint}_time_basis"] = row["time_basis"] if row else None
        coverage[f"{endpoint}_quality_flags"] = _quality_flags(row["quality_flags"])[0] if row else []
    return coverage


def _require_database(path: Path, root: Path) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise WechatArchiveQueryError(
            "archive_refused",
            "A WeChat database must be a regular file inside the configured archive.",
        )
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()) or resolved.parent.is_symlink():
        raise WechatArchiveQueryError(
            "archive_refused",
            "A WeChat database path escapes the configured archive.",
        )
    if getattr(path.stat(), "st_flags", 0) & 0x40000000:
        raise WechatArchiveQueryError(
            "archive_unavailable",
            "A required WeChat database is cloud-only and was not read.",
        )
    for suffix in _SIDECARS:
        if Path(str(path) + suffix).exists():
            raise WechatArchiveQueryError(
                "source_changed",
                "A WeChat database has an unpublished journal. Wait until publication finishes.",
            )


class _ReadOnlyConnection(sqlite3.Connection):
    """Close a read connection at the end of its context, including failed queries."""

    def __exit__(self, exc_type, exc, traceback):
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


def _readonly(path: Path, *, allow_temp: bool = False) -> sqlite3.Connection:
    connection = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True,
        factory=_ReadOnlyConnection,
    )
    connection.row_factory = sqlite3.Row
    deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS
    connection.query_deadline = deadline
    connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 10_000)
    if allow_temp:
        # Persistent main and attached databases remain URI read-only. Only controlled,
        # connection-local projections may be written during this construction phase.
        connection.execute("PRAGMA temp_store=MEMORY")
    else:
        connection.execute("PRAGMA query_only=ON")
    return connection


class _archive_connection:
    """Open the history database and its validated timeline, then close it."""

    def __init__(
        self, root: Path, *, conversation_id: str | None = None,
        message_id: str | None = None,
    ) -> None:
        self.root = root
        self.conversation_id = conversation_id
        self.message_id = message_id
        self.connection: sqlite3.Connection | None = None

    def __enter__(self):
        history = wechat_archive_path(self.root)
        _require_database(history, self.root)
        resource = history.with_name("resource_references.sqlite3")
        self.resource_status = "absent"
        if resource.exists() or resource.is_symlink():
            if resource.is_symlink():
                raise WechatArchiveQueryError(
                    "archive_refused",
                    "The WeChat resource index is a symbolic link.",
                )
            if getattr(resource.stat(), "st_flags", 0) & 0x40000000:
                # The archive repository also refuses to attach this evictable file.
                # Main history remains readable, and every result names the omission.
                self.resource_status = "dataless"
            else:
                self.resource_status = "attached"
        identity = _file_identity(history)
        connection = _readonly(history, allow_temp=True)
        repository = WechatArchiveRepository(self.root)
        try:
            conversation_id = self.conversation_id
            if self.message_id:
                conversation_id = repository._timeline_conversation_id(
                    connection, self.message_id
                )
                if conversation_id is None:
                    connection.close()
                    raise WechatArchiveQueryError(
                        "not_found", "No archive message has that id.",
                        message_id=self.message_id,
                    )
            conversation_table, message_table, status = repository._timeline_tables(
                connection, identity, conversation_id=conversation_id, strict=True,
            )
            connection.execute("PRAGMA query_only=ON")
        except WechatTimelineProjectionError as exc:
            connection.close()
            if time.monotonic() >= connection.query_deadline:
                raise WechatArchiveQueryError(
                    "query_timeout", "The archive query exceeded its execution budget."
                ) from exc
            raise WechatArchiveQueryError(
                "source_changed" if exc.status == "stale" else "index_incomplete",
                "The scoped media evidence could not be validated.",
                projection_status=exc.status,
            ) from exc
        except sqlite3.DatabaseError as exc:
            connection.close()
            raise _unreadable(exc) from exc
        if status == "stale":
            connection.close()
            raise WechatArchiveQueryError(
                "source_changed",
                "The WeChat resource index does not match the current history database.",
            )
        if status == "invalid":
            connection.close()
            raise WechatArchiveQueryError(
                "index_incomplete",
                "The WeChat resource index is present but cannot be used.",
            )
        if status not in {"ready", "absent"}:
            connection.close()
            raise WechatArchiveQueryError(
                "index_incomplete",
                "The WeChat resource index is not ready.",
            )
        self.connection = connection
        return (
            connection,
            message_table,
            conversation_table,
            identity,
            history,
            self.resource_status,
        )

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.connection is not None:
            self.connection.close()


def _source_fingerprint(root: Path) -> str | None:
    try:
        paths = _sources(root)
    except ContactAnalysisSourceChanged as exc:
        raise WechatArchiveQueryError("source_changed", str(exc)) from exc
    if paths is None:
        return None
    return paths[2]


def _index_problem(cache: Path, fingerprint: str) -> tuple[str, str] | None:
    if cache.is_symlink() or cache.parent.is_symlink():
        return ("archive_refused", "The contact reference index path is a symbolic link.")
    if not cache.exists():
        return (
            "index_incomplete",
            "The contact reference index has not been published. "
            "This tool does not build or modify it.",
        )
    if (
        not cache.is_file() or cache.stat().st_nlink != 1
        or not cache.resolve().is_relative_to(cache.parent.resolve())
    ):
        return ("archive_refused", "The contact reference index escapes the configured archive.")
    for suffix in _SIDECARS:
        if Path(str(cache) + suffix).exists():
            return (
                "source_changed",
                "The contact reference index has an unpublished journal.",
            )
    if _cache_ready(cache, fingerprint):
        return None
    return (
        "source_changed",
        "The contact reference index does not match the current archive.",
    )


def _load_contact(root: Path, contact_id: str) -> dict[str, Any]:
    contacts = contact_registry_path(root)
    names = WechatDisplayNames(root)
    try:
        with _readonly(contacts) as connection:
            row = connection.execute(
                "SELECT * FROM contacts WHERE id=?", (contact_id,)
            ).fetchone()
            authorities = _identifier_rows(connection, contact_id) if row is not None else []
    except sqlite3.DatabaseError as exc:
        raise _unreadable(exc) from exc
    if row is None:
        raise WechatArchiveQueryError(
            "contact_not_found",
            "No WeChat contact has that id.",
            contact_id=contact_id,
        )
    return _contact_profile(row, names, authorities)


def _contact_profile(row: sqlite3.Row | dict, names: WechatDisplayNames, authorities: list[dict]) -> dict[str, Any]:
    record = dict(row)
    identity_match = "unverified"
    if record.get("identity_status") == "verified":
        try:
            expected = canonical_contact_id(
                record["username"],
                authority="native_contact_username",
                namespace=record["namespace"],
                realm=record["realm"],
            )
        except (TypeError, ValueError):
            expected = ""
        identity_match = "verified" if expected == record["id"] else "unverified"
    elif record.get("identity_status") == "source_scoped":
        identity_match = "source_scoped"
    preferred = ""
    if identity_match == "verified":
        preferred = names.contact_name({**record, "platform": "wechat", "identity_status": "verified"})
    return {
        "contact_id": record["id"],
        "username": record.get("username") or "",
        "kind": record.get("kind") or "",
        "namespace": record.get("namespace") or "",
        "realm": record.get("realm") or "",
        "identity_status": record.get("identity_status") or "",
        "identity_match": identity_match,
        "display_name": preferred or record.get("username") or "",
        "preferred_display_name": preferred or None,
        "identifier_authorities": authorities,
    }


def _conversation_count(root: Path, contact_id: str, fingerprint: str | None) -> dict[str, Any]:
    if fingerprint is None:
        return {"conversation_count": None, "conversation_count_status": "archive_missing"}
    if _CONTACT_ID.fullmatch(contact_id) is None:
        return {"conversation_count": None, "conversation_count_status": "not_indexed"}
    cache = contact_analysis_cache_path(root, contact_id)
    problem = _index_problem(cache, fingerprint)
    if problem is not None:
        return {"conversation_count": None, "conversation_count_status": problem[0]}
    try:
        with _readonly(cache) as connection:
            count = connection.execute(
                "SELECT COUNT(DISTINCT conversation_id) FROM refs"
            ).fetchone()[0]
    except sqlite3.DatabaseError:
        return {"conversation_count": None, "conversation_count_status": "archive_unreadable"}
    return {"conversation_count": count, "conversation_count_status": "ready"}


def _matching_contact_ids(connection, text: str, match: str, limit: int) -> list[str]:
    if match == "exact":
        rows = connection.execute(
            "SELECT contact_id FROM ("
            "SELECT DISTINCT contact_id FROM contact_observations WHERE value=? "
            "UNION SELECT id AS contact_id FROM contacts WHERE username=? "
            "UNION SELECT DISTINCT contact_id FROM contact_identifiers WHERE value=?"
            ") ORDER BY contact_id LIMIT ?",
            (text, text, text, limit),
        ).fetchall()
        found = [row[0] for row in rows]
        return found
    rows = connection.execute(
        "SELECT contact_id FROM ("
        "SELECT DISTINCT contact_id FROM contact_observations WHERE instr(value,?)>0 "
        "UNION SELECT id AS contact_id FROM contacts WHERE instr(username,?)>0 "
        "UNION SELECT DISTINCT contact_id FROM contact_identifiers WHERE instr(value,?)>0"
        ") ORDER BY contact_id LIMIT ?",
        (text, text, text, limit),
    ).fetchall()
    return [row[0] for row in rows]


def _contact_rows(connection, identifiers: list[str]) -> dict[str, sqlite3.Row]:
    rows = {}
    for contact_id in identifiers:
        row = connection.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
        if row is not None:
            rows[contact_id] = row
    return rows


def _matching_evidence(connection, contact_id: str, text: str, match: str) -> dict[str, Any]:
    if match == "exact":
        predicate = "value=?"
    else:
        predicate = "instr(value,?)>0"
    count = connection.execute(
        f"SELECT COUNT(*) FROM contact_observations WHERE contact_id=? AND {predicate}",
        (contact_id, text),
    ).fetchone()[0]
    items = [
        {
            "kind": row["kind"],
            "value": row["value"],
            "source_id": row["source_id"],
            "owner_account_id": row["owner_account_id"],
            "room_id": row["room_id"],
        }
        for row in connection.execute(
            "SELECT kind, value, source_id, owner_account_id, room_id "
            f"FROM contact_observations WHERE contact_id=? AND {predicate} "
            "ORDER BY kind, source_id, room_id LIMIT ?",
            (contact_id, text, MAX_EVIDENCE),
        )
    ]
    return {"items": items, "count": count, "truncated": count > len(items)}


def _identifier_rows(connection, contact_id: str) -> list[dict[str, str]]:
    return [
        {
            "namespace": row["namespace"],
            "value": row["value"],
            "authority": row["authority"],
            "realm": row["realm"],
        }
        for row in connection.execute(
            "SELECT namespace, value, authority, realm FROM contact_identifiers "
            "WHERE contact_id=? ORDER BY authority, value LIMIT 12",
            (contact_id,),
        )
    ]


def _reference_filters(query: str, conversation_id: str, start: float | None, end: float | None):
    filters = []
    parameters: list[Any] = []
    if conversation_id:
        filters.append("r.conversation_id=?")
        parameters.append(conversation_id)
    if start is not None:
        filters.append("r.has_time=1 AND r.time_sort>=?")
        parameters.append(start)
    if end is not None:
        filters.append("r.has_time=1 AND r.time_sort<=?")
        parameters.append(end)
    if query:
        filters.append(
            "instr(reference_text(m.text,m.provenance,r.component_path,"
            "p.account_id,r.parent_message_id,c.peer_id),?)>0"
        )
        parameters.append(query.casefold())
    return (" WHERE " + " AND ".join(filters)) if filters else "", parameters


def _message_row(connection, message_table, conversation_table, message_id: str):
    return connection.execute(
        "SELECT m.*,c.title AS conversation_title,c.kind AS conversation_kind,c.peer_id,"
        "c.partition_id AS source_id,p.account_id,p.account_label,"
        "p.identity_status AS source_identity_status,p.source_label,p.source_file "
        f"FROM {message_table} m JOIN {conversation_table} c ON c.id=m.conversation_id "
        "JOIN main.partitions p ON p.id=c.partition_id WHERE m.id=?",
        (message_id,),
    ).fetchone()


def _neighbors(connection, message_table, conversation_table, target, *, direction: str, limit: int):
    if limit == 0:
        return []
    comparison = "<" if direction == "before" else ">"
    order = "DESC" if direction == "before" else "ASC"
    select = (
        "SELECT m.*,c.title AS conversation_title,c.kind AS conversation_kind,c.peer_id,"
        "c.partition_id AS source_id,p.account_id,p.account_label,"
        "p.identity_status AS source_identity_status,p.source_label,p.source_file "
        f"FROM {message_table} m JOIN {conversation_table} c ON c.id=m.conversation_id "
        "JOIN main.partitions p ON p.id=c.partition_id "
        "WHERE m.conversation_id=? AND "
    )
    unknown = target["time_sort"] == 0
    if unknown:
        condition = f"m.time_sort=0 AND (m.ordinal,m.id) {comparison} (?,?)"
        key = [target["ordinal"], target["id"]]
        ordering = f"m.ordinal {order},m.id {order}"
    else:
        condition = f"m.time_sort!=0 AND (m.time_sort,m.ordinal,m.id) {comparison} (?,?,?)"
        key = [target["time_sort"], target["ordinal"], target["id"]]
        ordering = f"m.time_sort {order},m.ordinal {order},m.id {order}"
    # Separate the unknown-time tail from the indexed chronological range. An OR
    # over the two ranges plus an expression ORDER BY sorts whole conversations.
    rows = connection.execute(
        select + condition + f" ORDER BY {ordering} LIMIT ?",
        [target["conversation_id"], *key, limit],
    ).fetchall()
    if len(rows) < limit and (
        (unknown and direction == "before") or (not unknown and direction == "after")
    ):
        condition = "m.time_sort!=0" if unknown else "m.time_sort=0"
        ordering = (
            "m.time_sort DESC,m.ordinal DESC,m.id DESC"
            if unknown else "m.ordinal ASC,m.id ASC"
        )
        rows.extend(connection.execute(
            select + condition + f" ORDER BY {ordering} LIMIT ?",
            (target["conversation_id"], limit - len(rows)),
        ).fetchall())
    return rows


def _public_reference(item: dict) -> dict[str, Any]:
    text, truncated = _bound_text(str(item.get("content_text") or ""))
    flags = item.get("quality_flags")
    return {
        "id": item.get("id"),
        "parent_message_id": item.get("parent_message_id"),
        "conversation_id": item.get("conversation_id"),
        "conversation_title": item.get("conversation_title") or "",
        "ordinal": item.get("ordinal"),
        "sender_id": item.get("sender_id"),
        "sender_label": item.get("sender_label") or "",
        "direction": item.get("direction") or "",
        "text": text,
        "text_truncated": truncated,
        "timestamp_value": item.get("timestamp_value") or "",
        "timestamp_label": item.get("timestamp_label") or "",
        "time_basis": item.get("time_basis") or "",
        "time_sort": item.get("time_sort"),
        "source_id": item.get("source_id") or "",
        "source_label": item.get("source_label") or "",
        "source_file": Path(str(item.get("source_file") or "")).name,
        "source_identity_status": item.get("identity_status") or "",
        "quality_flags": flags if isinstance(flags, list) else [],
        "quality_flags_unparsed": not isinstance(flags, list),
        "provenance": _public_provenance(item.get("provenance")),
        "reference_kind": item.get("reference_kind") or "",
        "is_embedded": bool(item.get("is_embedded")),
        "component_path": item.get("component_path") or [],
        "scope_contact_id": item.get("contact_id"),
    }


def _public_archive_message(row, *, is_target: bool = False) -> dict[str, Any]:
    record = dict(row)
    provenance = _object_or_empty(record.get("provenance"))
    flags, unparsed = _quality_flags(record.get("quality_flags"))
    text, truncated = _bound_text(str(record.get("text") or ""))
    return {
        "id": record.get("id"),
        "parent_message_id": record.get("id"),
        "conversation_id": record.get("conversation_id"),
        "conversation_title": record.get("conversation_title") or "",
        "ordinal": record.get("ordinal"),
        "sender_id": record.get("sender_id"),
        "sender_label": record.get("sender_label") or "",
        "direction": record.get("direction") or "",
        "text": text,
        "text_truncated": truncated,
        "timestamp_value": record.get("timestamp_value") or "",
        "timestamp_label": record.get("timestamp_label") or "",
        "time_basis": record.get("time_basis") or "",
        "time_sort": record.get("time_sort"),
        "source_id": record.get("source_id") or "",
        "source_label": record.get("source_label") or "",
        "source_file": Path(str(record.get("source_file") or "")).name,
        "source_identity_status": record.get("source_identity_status") or "",
        "quality_flags": flags,
        "quality_flags_unparsed": unparsed,
        "provenance": _public_provenance(provenance),
        "reference_kind": "archive_message",
        "is_embedded": False,
        "component_path": [],
        "is_target": is_target,
    }


def _sender_requests(root: Path, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Retain bounded source proof internally, before public text/provenance truncation."""
    records = [dict(message) for message in messages]
    provenances = [_object_or_empty(record.get("provenance")) for record in records]
    snapshot_ids = {
        value for proof in provenances
        if type(value := proof.get("source_snapshot")) is int
    }
    snapshots = {}
    if snapshot_ids:
        with _readonly(wechat_archive_path(root)) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_snapshots'"
            ).fetchone()
            if exists:
                marks = ",".join("?" for _ in snapshot_ids)
                snapshots = {
                    row["id"]: dict(row) for row in connection.execute(
                        "SELECT id,snapshot_id,source_id FROM source_snapshots "
                        f"WHERE id IN ({marks})", sorted(snapshot_ids),
                    )
                }
    requests = []
    for message, proof in zip(records, provenances, strict=True):
        sender = message.get("sender_id")
        snapshot = snapshots.get(proof.get("source_snapshot"), {})
        snapshot_id = message.get("source_snapshot_id") or (
            snapshot.get("snapshot_id", "")
            if snapshot.get("source_id") == message.get("source_id") else ""
        )
        full_text = message.get("text", message.get("content_text", ""))
        requests.append(
            {
                "id": message.get("id") or message.get("parent_message_id"),
                "source_id": message.get("source_id") or "",
                "conversation_id": message.get("conversation_id") or "",
                "sender_id": sender if isinstance(sender, str) else "",
                "sender_label": message.get("sender_label") or "",
                "is_system": message.get("is_system") is True
                or proof.get("text_extraction_kind") == "system_text",
                "peer_id": message.get("peer_id") or "",
                "conversation_kind": message.get("kind", message.get("conversation_kind")) or "",
                "source_snapshot_id": snapshot_id,
                "account_id": message.get("account_id") or "",
                "source_record_id": proof.get("record_id") or "",
                "source_locator": proof.get("locator") or {},
                "text_sha256": message.get("_source_text_sha256")
                or hashlib.sha256(str(full_text).encode()).hexdigest(),
                "timestamp_value": message.get("timestamp_value") or "",
            }
        )
    return requests


def _forwarded_sender_contacts(root: Path, references: list[dict], parents: dict) -> dict:
    """Reuse the source/member/avatar proof instead of trusting embedded native hints."""
    selected = {
        item["parent_message_id"] for item in references
        if item.get("component_path") and type(item["component_path"][0]) is int
    }
    if not selected:
        return {}
    rows = [parents[identifier] for identifier in selected]
    requests = _sender_requests(root, rows)
    for request, row in zip(requests, rows, strict=True):
        request["forwarded_components"] = _object_or_empty(
            row.get("provenance")
        ).get("forwarded_components", [])
    projections = WechatContactRepository(root).resolve_forwarded_components(requests)
    contacts = {}
    for item in references:
        path = item.get("component_path") or []
        if not path or any(type(part) is not int or part < 1 for part in path):
            continue
        projection = projections.get(item["parent_message_id"], {})
        if projection.get("status") != "source_proof_verified":
            continue
        children = projection.get("components", [])
        component = {}
        for position in path:
            if not isinstance(children, list) or position > len(children):
                component = {}
                break
            component = children[position - 1]
            children = component.get("forwarded_components", [])
        contact = component.get("contact", {})
        if contact:
            contacts[item["id"]] = contact
    return contacts


def _attach_sender_contacts(
    root: Path, messages: list[dict[str, Any]], *, evidence_rows=None, embedded_contacts=None,
) -> None:
    records = [dict(row) for row in (evidence_rows if evidence_rows is not None else messages)]
    requests = _sender_requests(root, records)
    if not requests:
        return
    try:
        resolved = WechatContactRepository(root).resolve_messages(requests)
    except (sqlite3.DatabaseError, TypeError, ValueError):
        for message in messages:
            message["sender_contact"] = {
                "identity_match": "unresolved",
                "identity_status": "unresolved",
            }
        return
    for message, request in zip(messages, requests, strict=True):
        contact = resolved.get(request["id"], {})
        if message.get("is_embedded"):
            contact = (embedded_contacts or {}).get(request["id"], {
                "identity_status": "unresolved",
                "resolution_reason": "embedded_sender_requires_source_proof",
            })
        message["sender_contact"] = {
            "contact_id": contact.get("id"),
            "identity_status": contact.get("identity_status") or "unresolved",
            "identity_match": contact.get("identity_status") or "unresolved",
            "display_name": contact.get("display_name") or "",
            "resolution_reason": contact.get("resolution_reason") or "",
        }


def _public_partition(row) -> dict[str, Any]:
    record = dict(row)
    return {
        "source_id": record["id"],
        "account_label": record["account_label"],
        "identity_status": record["identity_status"],
        "source_label": record["source_label"],
        "source_file": Path(str(record["source_file"])).name,
    }


def _resource_record(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        return {"present": True, "status": "refused"}
    if not path.exists():
        return {"present": False, "status": "absent"}
    if getattr(path.stat(), "st_flags", 0) & 0x40000000:
        return {"present": True, "status": "dataless", "name": path.name}
    identity = _file_identity(path)
    return {
        "present": True,
        "status": "present",
        **_file_record(path, identity),
    }


def _file_record(path: Path, identity: dict[str, int]) -> dict[str, Any]:
    return {
        "name": path.name,
        "bytes": identity["size"],
        "mtime_ns": identity["mtime_ns"],
        "updated_at": _iso_mtime(identity["mtime_ns"]),
    }


def _iso_mtime(mtime_ns: int) -> str:
    return datetime.fromtimestamp(mtime_ns / 1_000_000_000, UTC).isoformat()


def _require_tables(connection, label: str, required: set[str]) -> None:
    present = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if not required <= present:
        raise WechatArchiveQueryError(
            "archive_incomplete",
            f"The WeChat {label} is missing required tables.",
            missing=sorted(required - present),
        )


def _require_unchanged(path: Path, identity: dict[str, int]) -> None:
    if _file_identity(path) != identity:
        raise WechatArchiveQueryError(
            "source_changed",
            "The WeChat archive changed while it was being read.",
        )


def _object_or_empty(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _quality_flags(value: Any) -> tuple[list[Any], bool]:
    if isinstance(value, list):
        return value[:20], False
    if not isinstance(value, str) or not value:
        return [], True
    try:
        parsed = json.loads(value)
    except ValueError:
        return [], True
    if isinstance(parsed, list):
        return parsed[:20], False
    return [], True


def _public_provenance(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    return {
        key: _public_value(source.get(key))
        for key in _PROVENANCE_KEYS
        if key in source
    }


def _public_value(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return None
    if isinstance(value, str):
        if value.startswith(("/", "~")) or re.match(r"^[A-Za-z]:[\\/]", value):
            return "[withheld]"
        return value[:500]
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, list):
        return [_public_value(item, depth + 1) for item in value[:20]]
    if isinstance(value, dict):
        return {
            str(key)[:80]: _public_value(item, depth + 1)
            for key, item in list(value.items())[:20]
        }
    return None


def _bound_text(value: str) -> tuple[str, bool]:
    if len(value) <= MAX_TEXT_CHARACTERS:
        return value, False
    return value[:MAX_TEXT_CHARACTERS], True


def _bounded_query(value: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise WechatArchiveQueryError("invalid_arguments", "query must be a string.")
    text = value.strip()
    if not text and not allow_empty:
        raise WechatArchiveQueryError("invalid_arguments", "query must not be empty.")
    if len(text) > 200:
        raise WechatArchiveQueryError("invalid_arguments", "query is too long.")
    return text


def _page_number(value: int) -> int:
    if type(value) is not int or value < 1 or value > 10_000:
        raise WechatArchiveQueryError("invalid_arguments", "page is outside the supported range.")
    return value


def _page_size(value: int, *, maximum: int) -> int:
    if type(value) is not int or value < 1 or value > maximum:
        raise WechatArchiveQueryError(
            "invalid_arguments",
            "The requested page size is outside the supported range.",
        )
    return value


def _context_count(value: int) -> int:
    if type(value) is not int or value < 0 or value > MAX_CONTEXT:
        raise WechatArchiveQueryError(
            "invalid_arguments",
            "Context size must be from 0 through 20.",
        )
    return value


def _message_id(value: str) -> str:
    if not isinstance(value, str) or _MESSAGE_ID.fullmatch(value) is None:
        raise WechatArchiveQueryError(
            "invalid_arguments",
            "message_id must be a 64-character archive message id.",
        )
    return value


def _time_bounds(not_before: str | None, not_after: str | None) -> tuple[float | None, float | None]:
    start = _bound_time("not_before", not_before)
    end = _bound_time("not_after", not_after)
    if start is not None and end is not None and start > end:
        raise WechatArchiveQueryError(
            "invalid_arguments",
            "not_before is later than not_after.",
        )
    return start, end


def _bound_time(name: str, value: str | None) -> float | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise WechatArchiveQueryError("invalid_arguments", f"{name} must be an ISO timestamp.")
    try:
        return archive_order_key(value)
    except (ValueError, OverflowError, OSError) as exc:
        raise WechatArchiveQueryError(
            "invalid_arguments",
            f"{name} must be an ISO timestamp.",
        ) from exc


def _unreadable(exc: sqlite3.DatabaseError) -> WechatArchiveQueryError:
    if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT or str(exc) == "interrupted":
        return WechatArchiveQueryError(
            "query_timeout", "The archive query exceeded its execution budget."
        )
    return WechatArchiveQueryError(
        "archive_unreadable",
        "The WeChat archive could not be read.",
    )


# Imported for callers that want the same registry path helper without opening it.
__all__ = [
    "VERSION",
    "WechatArchiveQueryError",
    "archive_order_key",
    "archive_status",
    "find_contacts",
    "message_context",
    "search_messages",
]
