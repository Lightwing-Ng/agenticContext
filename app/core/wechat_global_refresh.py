"""Prepare bounded global Mac refresh packets without publishing or reading new keys.

Code version: v1.2.0-codex.0

Inventory reads canonical scope metadata and the existing verified-key registry. The
iterator decrypts one configured shard at a time and retains the existing per-scope
comparison, packet validation, and account binding. It never publishes, writes a
plaintext snapshot, discovers credentials, or substitutes an inferred sender.
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import sqlite3

from . import wechat_macos_recovery_bank as bank
from .wechat_archive import wechat_archive_path
from .wechat_mac_refresh_projection import (
    ConversationScope,
    MAX_ARCHIVE_ANCHOR_ROWS,
    _SESSION,
    _table,
    iter_prepare_packets,
    prepare_source_context,
)
from .wechat_mac_refresh_source import (
    KeyRegistry,
    RefreshError,
    SourceAccountBindingUnavailable,
    _DIRECTORY,
    read_database,
)


MAX_GLOBAL_SCOPES = 100_000


class GlobalRefreshCancelled(RefreshError):
    """The caller cancelled preparation; never reinterpret it as a skipped source."""


class GlobalRefreshInputChanged(RefreshError):
    """The pinned canonical archive changed during read-only preparation."""


@dataclass(frozen=True)
class GlobalRefreshInventory:
    """Private scope/reference metadata plus a content-free count summary.

    ``account_ids`` contains only canonical owners with accepted references and is
    the publisher's explicit account allowlist. ``summary`` contains
    total_archived_accounts, accounts_total, accounts_with_keys,
    accounts_without_keys, eligible_scopes, configured_sources,
    configured_source_bytes, unavailable_sources, and unavailable_accounts.
    Source bytes describe encrypted main files, not an estimate of free RAM.
    """

    account_ids: tuple[str, ...]
    scopes: tuple[ConversationScope, ...] = field(repr=False)
    references: tuple = field(repr=False)
    history_path: Path = field(repr=False)
    history_identity: dict = field(repr=False)
    summary: dict


def _reference_identity(reference):
    return (
        reference.account_id,
        reference.account_directory,
        reference.relative_path,
        str(reference.database_path),
    )


def _archived_scopes(local_store_root, account_id=None):
    if account_id is not None and (
        not isinstance(account_id, str) or _DIRECTORY.fullmatch(account_id) is None
    ):
        raise RefreshError("Select an existing verified WeChat account.")
    history = wechat_archive_path(local_store_root).resolve()
    identity = bank._identity(history)
    scopes = []
    clause = (
        "WHERE c.account_key=? AND p.account_id=? "
        "AND p.identity_status IN ('Verified archive ID','Corroborated export ID') "
        if account_id is not None else ""
    )
    args = ("account:" + account_id, account_id) if account_id is not None else ()
    with closing(sqlite3.connect(history.as_uri() + "?mode=ro&immutable=1", uri=True)) as source:
        if account_id is not None and source.execute(
            "SELECT 1 FROM partitions WHERE id=? AND account_key=? AND account_id=? "
            "AND identity_status='Verified archive ID'",
            (
                "ios-" + hashlib.md5(account_id.encode(), usedforsecurity=False).hexdigest(),
                *args,
            ),
        ).fetchone() is None:
            if bank._identity(history) != identity:
                raise GlobalRefreshInputChanged("The canonical archive changed during inventory.")
            return history, identity, scopes, 0
        rows = source.execute(
            "SELECT c.id,c.account_key,c.peer_id,c.kind,p.account_id,p.account_key "
            "FROM conversations c JOIN partitions p ON p.id=c.partition_id "
            + clause +
            "ORDER BY p.account_id,c.id LIMIT ?",
            (*args, MAX_GLOBAL_SCOPES + 1),
        ).fetchall()
        if len(rows) > MAX_GLOBAL_SCOPES:
            raise RefreshError("The global archive exceeds its bounded scope budget.")
        total_archived_accounts = (
            source.execute(
                "SELECT COUNT(DISTINCT account_id) FROM partitions "
                "WHERE account_id IS NOT NULL AND account_id<>''"
            ).fetchone()[0]
            if account_id is None else int(bool(rows))
        )
    for session, account_key, peer, kind, owner, partition_key in rows:
        # These are the same eligibility boundaries enforced by resolve_scope.
        if (
            not isinstance(session, str)
            or _SESSION.fullmatch(session) is None
            or kind not in {"direct", "group"}
            or not isinstance(peer, str)
            or not peer
            or not isinstance(owner, str)
            or not owner
            or account_key != "account:" + owner
            or partition_key != account_key
            or peer.startswith("gh_")
            or (kind == "group") != peer.endswith(("@chatroom", "@im.chatroom"))
        ):
            continue
        scopes.append(ConversationScope(session, owner, peer, kind))
    if bank._identity(history) != identity:
        raise GlobalRefreshInputChanged("The canonical archive changed during inventory.")
    return history, identity, scopes, total_archived_accounts


def archived_account_refresh_available(local_store_root, account_id):
    """Check canonical conversation metadata without opening native sources."""
    return account_id is not None and bool(_archived_scopes(local_store_root, account_id)[2])


def inventory_global_refresh(local_store_root, registry=None, *, account_id=None):
    """Inventory archived scopes, optionally only one account, without decryption."""
    registry = registry if registry is not None else KeyRegistry()
    history, identity, scopes, total_archived_accounts = _archived_scopes(
        local_store_root, account_id
    )
    owners = sorted({scope.account_id for scope in scopes})
    references, configured, unavailable, unavailable_accounts = [], set(), 0, 0
    for owner in owners:
        try:
            accepted = registry.references(owner)
            if any(reference.account_id != owner for reference in accepted):
                raise RefreshError("A configured source belongs to another account.")
            missing = registry.missing_database_count(accepted) if accepted else 0
            if type(missing) is not int or missing < 0:
                raise RefreshError("The key coverage inventory is invalid.")
        except (OSError, ValueError, TypeError, sqlite3.Error):
            unavailable_accounts += 1
            continue
        if accepted:
            configured.add(owner)
            references.extend(accepted)
        unavailable += missing
    paths = [str(reference.database_path) for reference in references]
    if len(paths) != len(set(paths)):
        raise RefreshError("A global source has ambiguous configured account ownership.")
    source_bytes = sum(reference.database_path.stat().st_size for reference in references)
    if bank._identity(history) != identity:
        raise GlobalRefreshInputChanged("The canonical archive changed during inventory.")
    return GlobalRefreshInventory(
        tuple(sorted(configured)),
        tuple(scopes),
        tuple(sorted(references, key=_reference_identity)),
        history,
        identity,
        {
            "total_archived_accounts": total_archived_accounts,
            "accounts_total": len(owners),
            "accounts_with_keys": len(configured),
            "accounts_without_keys": len(set(owners) - configured),
            "eligible_scopes": len(scopes),
            "configured_sources": len(references),
            "configured_source_bytes": source_bytes,
            "unavailable_sources": unavailable,
            "unavailable_accounts": unavailable_accounts,
        },
    )


def iter_global_refresh_packets(
    local_store_root,
    *,
    registry=None,
    inventory=None,
    check_cancel=None,
    on_progress=None,
    on_excluded=None,
    scope_sources=None,
):
    """Yield validated candidate packets while keeping one decrypted shard resident.

    Progress exposes checked, candidates, excluded, processed_scopes,
    processed_scope_sources, skipped_scopes, skipped_sources, completed_sources,
    unavailable_sources, oversized_scopes, and reason_codes. ``processed_scopes``
    counts distinct successfully compared canonical conversations;
    ``processed_scope_sources`` and ``skipped_scopes`` count source/scope pairs.
    ``reason_codes`` uses row_, scope_, source_, and account_ prefixes to retain
    the counting unit. Counts describe preparation, never committed changes.

    ``on_excluded`` is a private-report callback for stable source coordinates and
    safe reason codes. It receives no plaintext body, credential, or exception text.
    A scope retains the one-million-row anchor limit while packet windows retain
    the existing row and byte limits. Counts include actual retry examinations.
    ``scope_sources`` optionally restricts work to an exact frozenset of
    (account_id, database_relative_path, session_id) tuples validated against the
    full inventory. Unselected sources are never decrypted. The consumer must
    close this generator if staging stops early so its memory snapshot is closed.
    """
    registry = registry if registry is not None else KeyRegistry()
    inventory = inventory or inventory_global_refresh(local_store_root, registry)
    history = wechat_archive_path(local_store_root).resolve()
    if history != inventory.history_path:
        raise GlobalRefreshInputChanged("The inventory belongs to another archive.")
    if scope_sources is not None:
        sources = {
            (reference.account_id, reference.relative_path)
            for reference in inventory.references
        }
        scopes = {(scope.account_id, scope.session_id) for scope in inventory.scopes}
        if (
            not isinstance(scope_sources, frozenset)
            or len(scope_sources) > MAX_GLOBAL_SCOPES
            or any(
                not isinstance(value, tuple)
                or len(value) != 3
                or any(not isinstance(part, str) for part in value)
                or value[:2] not in sources
                or (value[0], value[2]) not in scopes
                for value in scope_sources
            )
        ):
            raise RefreshError("The selected global scopes do not match the inventory.")
    selected_references = [
        reference
        for reference in inventory.references
        if scope_sources is None
        or any(value[:2] == (reference.account_id, reference.relative_path) for value in scope_sources)
    ]
    reasons = Counter()
    processed = set()
    status = {
        "checked": 0,
        "candidates": 0,
        "excluded": 0,
        "processed_scopes": 0,
        "processed_scope_sources": 0,
        "skipped_scopes": 0,
        "skipped_sources": 0,
        "completed_sources": 0,
        "unavailable_sources": inventory.summary["unavailable_sources"],
        "oversized_scopes": 0,
        "selected_sources": len(selected_references),
        "selected_scope_sources": len(scope_sources) if scope_sources is not None else sum(
            sum(scope.account_id == reference.account_id for scope in inventory.scopes)
            for reference in inventory.references
        ),
        "deferred_scope_sources": 0,
        "retried_scope_sources": 0,
        "recovered_scope_sources": 0,
    }

    def running(*, check_inputs=False):
        if check_cancel is not None:
            try:
                check_cancel()
            except Exception:
                raise GlobalRefreshCancelled("Global preparation was cancelled.") from None
        if check_inputs and bank._identity(history) != inventory.history_identity:
            raise GlobalRefreshInputChanged("The canonical archive changed during preparation.")

    def progress(phase):
        if on_progress is not None:
            on_progress({"phase": phase, **status, "reason_codes": dict(reasons)})

    def excluded(reason, reference, scope=None, **values):
        if on_excluded is not None:
            on_excluded(
                {
                    "reason": reason,
                    "account_id": reference.account_id,
                    "database_relative_path": reference.relative_path,
                    **({"session_id": scope.session_id} if scope is not None else {}),
                    **values,
                }
            )

    running(check_inputs=True)
    progress("preparing")
    for owner in inventory.account_ids:
        expected = [value for value in inventory.references if value.account_id == owner]
        selected = [value for value in selected_references if value.account_id == owner]
        if not selected:
            continue
        running(check_inputs=True)
        try:
            loaded = {
                _reference_identity(value): value
                for value in registry.references(owner, load_keys=True)
            }
            if set(loaded) != {_reference_identity(value) for value in expected}:
                raise RefreshError("The configured source inventory changed.")
        except (OSError, ValueError, TypeError, sqlite3.Error):
            reasons["account_key_registry_unavailable"] += 1
            status["skipped_sources"] += len(selected)
            progress("comparing")
            continue
        for reference in selected:
            running(check_inputs=True)
            database = None
            try:
                progress("reading")
                database = read_database(
                    loaded[_reference_identity(reference)], check_cancel=running
                )
                context = prepare_source_context(owner, database, _learn_witnesses=True)
            except (GlobalRefreshCancelled, GlobalRefreshInputChanged):
                if database is not None:
                    database.close()
                raise
            except (OSError, ValueError, TypeError, RuntimeError, sqlite3.Error):
                if database is not None:
                    database.close()
                reasons["source_read_or_metadata_failed"] += 1
                status["skipped_sources"] += 1
                excluded("source_read_or_metadata_failed", reference)
                progress("comparing")
                continue
            try:
                if database.partial:
                    reasons["source_incomplete_wal_coverage"] += 1
                def process_scope(scope, *, retry=False):
                    running(check_inputs=True)
                    table = _table(scope.peer_id)
                    try:
                        native_count = database.connection.execute(
                            'SELECT COUNT(*) FROM "' + table + '"'
                        ).fetchone()[0]
                    except sqlite3.Error:
                        reason = "scope_native_table_unavailable"
                        reasons[reason] += 1
                        status["skipped_scopes"] += 1
                        excluded(reason, reference, scope)
                        progress("comparing")
                        return False
                    if native_count > MAX_ARCHIVE_ANCHOR_ROWS:
                        reason = "scope_native_row_budget_exceeded"
                        reasons[reason] += 1
                        status["skipped_scopes"] += 1
                        status["oversized_scopes"] += 1
                        excluded(reason, reference, scope, native_rows=native_count)
                        progress("comparing")
                        return False
                    row_reasons = Counter()

                    def row_excluded(value):
                        row_reasons[value["reason"]] += 1
                        if on_excluded is not None:
                            on_excluded(dict(value))

                    def credit_rows(value):
                        status["checked"] += value.get("total", 0)
                        status["excluded"] += value.get("excluded", 0)
                        reasons.update(row_reasons)
                        row_reasons.clear()

                    emitted = False
                    try:
                        packets = iter_prepare_packets(
                            local_store_root,
                            scope,
                            database,
                            context=context,
                            check_cancel=running,
                            on_excluded=row_excluded,
                            on_counts=credit_rows,
                        )
                        for packet in packets:
                            running(check_inputs=True)
                            status["candidates"] += len(packet["messages"])
                            progress("comparing")
                            emitted = True
                            yield packet
                    except (GlobalRefreshCancelled, GlobalRefreshInputChanged):
                        raise
                    except SourceAccountBindingUnavailable:
                        if not retry and not emitted:
                            status["deferred_scope_sources"] += 1
                            progress("comparing")
                            return True
                        reason = "scope_account_binding_unavailable"
                        reasons[reason] += 1
                        status["skipped_scopes"] += 1
                        excluded(reason, reference, scope)
                        progress("comparing")
                        return False
                    except (OSError, ValueError, TypeError, RuntimeError, sqlite3.Error):
                        reason = "scope_projection_failed"
                        reasons[reason] += 1
                        status["skipped_scopes"] += 1
                        excluded(reason, reference, scope)
                        progress("comparing")
                        return False
                    running(check_inputs=True)
                    status["processed_scope_sources"] += 1
                    processed.add(scope.session_id)
                    status["processed_scopes"] = len(processed)
                    if retry:
                        status["recovered_scope_sources"] += 1
                    progress("comparing")
                    return False

                pending = []
                for scope in inventory.scopes:
                    if (
                        scope.account_id != owner
                        or _table(scope.peer_id) not in context.tables
                        or scope_sources is not None
                        and (owner, reference.relative_path, scope.session_id) not in scope_sources
                    ):
                        continue
                    if (yield from process_scope(scope)):
                        pending.append(scope)
                for scope in pending:
                    status["retried_scope_sources"] += 1
                    yield from process_scope(scope, retry=True)
                status["completed_sources"] += 1
            finally:
                database.close()
            progress("comparing")
    running(check_inputs=True)
    progress("complete")
