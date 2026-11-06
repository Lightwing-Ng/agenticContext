"""Restore bounded native text subsets through an append-only APFS clone.

Code version: v1.3.1-codex.0
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from .owner_only_files import ensure_owner_only

from .wechat_archive import (
    _SCHEMA,
    _identifier,
    _source_exclusions,
    wechat_archive_path,
)
from .wechat_export_merge import (
    _clonefile,
    _manifest,
    _partition_rows,
    _row_fingerprint,
)
from .wechat_reconcile import (
    _ADDITIVE_SCHEMA,
    _compatible,
    _database_identity,
    _file_identity,
    _is_replay,
    _json,
    _normalize_record,
    _normalize_sources,
    _server_id,
    _update_conversation,
)


_APPEND_TABLES = (
    "messages",
    "partitions",
    "message_evidence",
    "source_snapshots",
    "conversations",
)
_STATISTICS = {
    "message_count",
    "first_sort",
    "last_sort",
    "first_time",
    "last_time",
    "latest_text",
}
_MODULUS = 1 << 256


def _stream_sha256(path):
    """Hash original bytes with a fixed one-MiB read buffer."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _content_identity_matches(path, expected, baseline_sha256, *, database=False):
    """Verify ctime-only drift without accepting content or sidecar changes."""
    identity = _database_identity if database else _file_identity
    before = identity(path)
    if before == expected:
        return True

    def same_essentials(observed):
        original = expected[0] if database else expected
        current = observed[0] if database else observed
        return (
            original is not None
            and current is not None
            and current[:4] == original[:4]
            and current[5:] == original[5:]
            and (not database or observed[1:] == expected[1:])
        )

    if baseline_sha256 is None or not same_essentials(before):
        return False
    current_sha256 = _stream_sha256(path)
    after = identity(path)
    return current_sha256 == baseline_sha256 and same_essentials(after)


def _install_guards(connection):
    """Enforce immutable prior rows and record every append independently of callers."""
    from .wechat_shared_storage import shared_storage_installed

    shared_storage = shared_storage_installed(connection)
    protected_tables = set(_APPEND_TABLES)
    if shared_storage:
        protected_tables.update({"message_storage", "shared_message_bodies"})
    for table in _APPEND_TABLES:
        physical_table = (
            "message_storage" if shared_storage and table == "messages" else table
        )
        connection.execute(
            f"CREATE TEMP TABLE restore_added_{table}(id PRIMARY KEY) WITHOUT ROWID"
        )
        connection.execute(
            f"CREATE TEMP TRIGGER restore_no_replace_{table} BEFORE INSERT ON main.{physical_table} "
            f"WHEN EXISTS(SELECT 1 FROM main.{physical_table} WHERE id=NEW.id) "
            "BEGIN SELECT RAISE(ABORT,'Restore cannot replace retained rows'); END"
        )
        connection.execute(
            f"CREATE TEMP TRIGGER restore_log_{table} AFTER INSERT ON main.{physical_table} "
            f"BEGIN INSERT INTO restore_added_{table} VALUES(NEW.id); END"
        )
        connection.execute(
            f"CREATE TEMP TRIGGER restore_no_delete_{table} BEFORE DELETE ON main.{physical_table} "
            "BEGIN SELECT RAISE(ABORT,'Restore cannot delete retained rows'); END"
        )
    connection.execute(
        "CREATE TEMP TABLE restore_touched(id TEXT PRIMARY KEY) WITHOUT ROWID"
    )
    connection.execute(
        "CREATE TEMP TRIGGER restore_conversation_scope BEFORE UPDATE ON main.conversations "
        "WHEN OLD.id NOT IN (SELECT id FROM restore_touched) "
        "OR NEW.id IS NOT OLD.id OR NEW.partition_id IS NOT OLD.partition_id "
        "OR NEW.account_key IS NOT OLD.account_key OR NEW.peer_id IS NOT OLD.peer_id "
        "OR NEW.title IS NOT OLD.title OR NEW.kind IS NOT OLD.kind "
        "BEGIN SELECT RAISE(ABORT,'Restore cannot alter conversation identity'); END"
    )
    connection.execute(
        "CREATE TEMP TRIGGER restore_manifest_scope BEFORE UPDATE ON main.archive_info "
        "WHEN OLD.key!='manifest' OR NEW.key IS NOT OLD.key "
        "BEGIN SELECT RAISE(ABORT,'Restore can update only its manifest'); END"
    )
    connection.execute(
        "CREATE TEMP TRIGGER restore_manifest_insert BEFORE INSERT ON main.archive_info "
        "WHEN NEW.key!='manifest' "
        "BEGIN SELECT RAISE(ABORT,'Restore can insert only its manifest'); END"
    )
    connection.execute(
        "CREATE TEMP TRIGGER restore_manifest_delete BEFORE DELETE ON main.archive_info "
        "BEGIN SELECT RAISE(ABORT,'Restore cannot replace retained metadata'); END"
    )
    scope = {
        "statistics": False,
        "manifest": False,
        "shared_storage": shared_storage,
    }

    def authorize(action, table, column, database, trigger):
        if (
            database == "temp"
            and table.startswith("restore_added_")
            and action
            in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}
        ):
            expected_trigger = "restore_log_" + table.removeprefix("restore_added_")
            return (
                sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_INSERT and trigger == expected_trigger
                else sqlite3.SQLITE_DENY
            )
        if shared_storage and table == "shared_message_bodies" and action in {
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_DELETE,
        }:
            return sqlite3.SQLITE_DENY
        if (
            shared_storage
            and table == "message_storage"
            and action == sqlite3.SQLITE_INSERT
            and trigger != "shared_messages_insert"
        ):
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_DELETE and table in protected_tables:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_UPDATE and table in protected_tables:
            return (
                sqlite3.SQLITE_OK
                if table == "conversations"
                and column in _STATISTICS
                and scope["statistics"]
                else sqlite3.SQLITE_DENY
            )
        if (
            action
            in {sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_INSERT, sqlite3.SQLITE_DELETE}
            and table == "archive_info"
        ):
            return (
                sqlite3.SQLITE_OK
                if scope["manifest"] and action != sqlite3.SQLITE_DELETE
                else sqlite3.SQLITE_DENY
            )
        if action in {
            sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_DROP_INDEX,
            sqlite3.SQLITE_DROP_TRIGGER,
            sqlite3.SQLITE_ALTER_TABLE,
            sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_CREATE_INDEX,
            sqlite3.SQLITE_CREATE_TRIGGER,
            sqlite3.SQLITE_DROP_TEMP_TABLE,
            sqlite3.SQLITE_DROP_TEMP_INDEX,
            sqlite3.SQLITE_DROP_TEMP_TRIGGER,
            sqlite3.SQLITE_CREATE_TEMP_TABLE,
            sqlite3.SQLITE_CREATE_TEMP_INDEX,
            sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
            sqlite3.SQLITE_CREATE_VIEW,
            sqlite3.SQLITE_DROP_VIEW,
            sqlite3.SQLITE_CREATE_TEMP_VIEW,
            sqlite3.SQLITE_DROP_TEMP_VIEW,
            sqlite3.SQLITE_CREATE_VTABLE,
            sqlite3.SQLITE_DROP_VTABLE,
            sqlite3.SQLITE_ATTACH,
            sqlite3.SQLITE_DETACH,
        }:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA and column is not None:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorize)
    return scope


def _added_fingerprint(connection, table):
    count, fingerprint = 0, 0
    for row in connection.execute(
        f"SELECT saved.* FROM restore_added_{table} added CROSS JOIN main.{table} saved "
        "WHERE saved.id=added.id"
    ):
        count += 1
        fingerprint = (fingerprint + _row_fingerprint(row)) % _MODULUS
    return count, fingerprint


def _verify_conversation_statistics(connection, identifier, message_read="messages"):
    """Check summary fields independently of the update helper after appending rows."""
    total = connection.execute(
        f"SELECT COUNT(*) FROM {message_read} WHERE conversation_id=?",
        (identifier,),
    ).fetchone()[0]
    first = connection.execute(
        f"SELECT time_sort,timestamp_value FROM {message_read} "
        "WHERE conversation_id=? AND time_sort!=0 ORDER BY time_sort,ordinal LIMIT 1",
        (identifier,),
    ).fetchone()
    last = connection.execute(
        f"SELECT time_sort,timestamp_value,text FROM {message_read} "
        "WHERE conversation_id=? AND time_sort!=0 ORDER BY time_sort DESC,ordinal DESC LIMIT 1",
        (identifier,),
    ).fetchone()
    if first is None:
        first = connection.execute(
            f"SELECT time_sort,timestamp_value FROM {message_read} "
            "WHERE conversation_id=? ORDER BY ordinal LIMIT 1",
            (identifier,),
        ).fetchone()
        last = connection.execute(
            f"SELECT time_sort,timestamp_value,text FROM {message_read} "
            "WHERE conversation_id=? ORDER BY ordinal DESC LIMIT 1",
            (identifier,),
        ).fetchone()
    expected = (total, first[0], last[0], first[1], last[1], last[2][:300])
    actual = connection.execute(
        "SELECT message_count,first_sort,last_sort,first_time,last_time,latest_text "
        "FROM conversations WHERE id=?",
        (identifier,),
    ).fetchone()
    if tuple(actual) != expected:
        raise ValueError(
            "Staged conversation statistics disagree with retained messages."
        )


def restore_wechat_text_subset(
    local_store_root: Path | str,
    *,
    sources: list[dict],
    records: Iterable[dict],
    snapshot_id: str,
    apply: bool = False,
) -> dict:
    """Restore one original snapshot through the shared append-only batch core."""
    return restore_wechat_text_batches(
        local_store_root,
        batches=[{"sources": sources, "records": records, "snapshot_id": snapshot_id}],
        snapshot_id=snapshot_id,
        apply=apply,
    )


def _prepare_batches(batches, exclusions):
    """Freeze source metadata and reject cross-batch owner conflicts before staging."""
    if not isinstance(batches, (list, tuple)) or not batches:
        raise ValueError("Restore needs at least one declared native snapshot batch.")
    prepared, metadata, excluded = [], {}, set()
    for batch in batches:
        if not isinstance(batch, dict) or set(batch) != {
            "sources",
            "records",
            "snapshot_id",
        }:
            raise ValueError(
                "A native snapshot batch needs sources, records, and snapshot_id."
            )
        original_snapshot = batch["snapshot_id"]
        if (
            not isinstance(original_snapshot, str)
            or not original_snapshot
            or "\0" in original_snapshot
        ):
            raise ValueError("Restore needs a stable original snapshot identifier.")
        batch_metadata, omitted = _normalize_sources(batch["sources"], exclusions)
        excluded.update(omitted)
        records = batch["records"]
        if isinstance(records, (str, bytes, bytearray, dict)):
            raise ValueError("A native batch needs an iterable of text observations.")
        try:
            iterator = iter(records)
        except TypeError as error:
            raise ValueError(
                "A native batch needs an iterable of text observations."
            ) from error
        for identifier, source in batch_metadata.items():
            prior = metadata.get(identifier)
            if prior is not None and any(
                prior[key] != source[key]
                for key in ("owner", "account_key", "verified")
            ):
                raise ValueError(
                    "Native snapshot batches disagree about their source account."
                )
            metadata.setdefault(identifier, source)
        prepared.append(
            {
                "metadata": batch_metadata,
                "records": iterator,
                "snapshot_id": original_snapshot,
                "snapshots": {},
            }
        )
    return prepared, metadata, sorted(excluded)


def _batch_records(batches):
    for batch in batches:
        for record in batch["records"]:
            yield batch, record


def restore_wechat_text_batches(
    local_store_root: Path | str,
    *,
    batches: list[dict],
    snapshot_id: str,
    apply: bool = False,
    page_cache_kib: int = 262144,
) -> dict:
    """Validate a clone completely; publish only with ``apply=True``.

    Each batch retains its original snapshot identifier and source metadata.
    ``snapshot_id`` labels the combined audit, not any source observation.
    Matching reads only incoming source/conversation indexes and exact message PKs.
    Integrity checking necessarily traverses the staged database. Prior message
    bodies are never rewritten or globally fingerprinted. Ordinals append within
    their conversation; original native row/local IDs remain source-locator fields.
    """
    if not isinstance(snapshot_id, str) or not snapshot_id or "\0" in snapshot_id:
        raise ValueError("Restore needs a stable snapshot identifier.")
    if type(apply) is not bool:
        raise ValueError("Restore publication must be an explicit boolean.")
    if type(page_cache_kib) is not int or not 1 <= page_cache_kib <= 2097152:
        raise ValueError("Restore page cache must be between 1 and 2097152 KiB.")
    target = wechat_archive_path(local_store_root)
    policy = target.with_name("exclusions.json")
    policy_identity = _file_identity(policy)
    exclusions = _source_exclusions(policy)
    prepared, metadata, excluded = _prepare_batches(batches, exclusions)
    initial = _database_identity(target)
    if any(value and value[2] for value in initial[1:]):
        raise ValueError("Finish active SQLite sidecars before restoring native text.")
    original_sha256 = _stream_sha256(target) if initial[0] is not None else None
    policy_sha256 = _stream_sha256(policy) if policy_identity is not None else None
    if (
        _database_identity(target) != initial
        or _file_identity(policy) != policy_identity
    ):
        raise ValueError(
            "The archive or exclusions changed while hashing original bytes."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=".wechat-native-restore-", suffix=".sqlite3", dir=target.parent
    )
    os.close(fd)
    stage = Path(name)
    connection = None
    report = {
        "version": "v1.2.0",
        "snapshot_id": snapshot_id,
        "source_snapshot_ids": list(
            dict.fromkeys(batch["snapshot_id"] for batch in prepared)
        ),
        "input_batches": len(prepared),
        "page_cache_kib": page_cache_kib,
        "apply": apply,
        "published": False,
        "input_records": 0,
        "inserted_messages": 0,
        "matched_messages": 0,
        "new_evidence": 0,
        "replayed_observations": 0,
        "identity_conflicts": 0,
        "conflict_variants": 0,
        "added_partitions": 0,
        "added_conversations": 0,
        "added_source_snapshots": 0,
        "excluded_sources": excluded,
        "excluded_records": 0,
        "indexed_existing_messages": 0,
        "indexed_existing_evidence": 0,
        "unique_original_message_count": None,
        "database_path": str(target),
        "publication_guard": "optimistic_identity_with_ctime_sha256_recheck",
        "requires_coordinated_single_writer": True,
        "original_main_sha256": original_sha256,
        "exclusions_sha256": policy_sha256,
    }
    try:
        if initial[0] is not None:
            stage.unlink()
            _clonefile(target, stage)
            if not _content_identity_matches(
                target, initial, original_sha256, database=True
            ):
                raise ValueError("The archive changed while creating its APFS clone.")
        ensure_owner_only(stage)
        connection = sqlite3.connect(stage)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA recursive_triggers=ON")
        connection.execute("PRAGMA temp_store=FILE")
        connection.execute(f"PRAGMA cache_size=-{page_cache_kib}")
        if initial[0] is None:
            connection.executescript(_SCHEMA)
        connection.executescript(_ADDITIVE_SCHEMA)
        manifest_before = _manifest(connection, existing=initial[0] is not None)
        old_partitions = _partition_rows(connection)
        if any(row[0] in exclusions for row in old_partitions):
            raise ValueError("An excluded source remains in the existing archive.")
        statistics = connection.execute(
            "SELECT COUNT(*),COALESCE(SUM(message_count),0) FROM conversations"
        ).fetchone()
        if statistics[1] != manifest_before["messages"]:
            raise ValueError(
                "Existing conversation and manifest message counts disagree."
            )
        report.update(
            messages_before=statistics[1],
            conversations_before=statistics[0],
            source_partitions_before=len(old_partitions),
        )
        scope = _install_guards(connection)
        message_read = (
            "messages" if scope["shared_storage"]
            else "messages INDEXED BY message_conversation_time"
        )
        message_alias = (
            "messages m" if scope["shared_storage"]
            else "messages m INDEXED BY message_conversation_time"
        )
        expected = {
            table: [0, 0]
            for table in (
                "messages",
                "message_evidence",
                "partitions",
                "source_snapshots",
            )
        }
        expected_conversations = {}

        def expect_append(table, values):
            expected[table][0] += 1
            expected[table][1] = (
                expected[table][1] + _row_fingerprint(values)
            ) % _MODULUS

        for identifier, source in metadata.items():
            prior = connection.execute(
                "SELECT * FROM partitions WHERE id=?", (identifier,)
            ).fetchone()
            if prior is not None:
                if (
                    prior["account_key"] != source["account_key"]
                    or prior["account_id"] != source["owner"]
                ):
                    raise ValueError(
                        "Native source conflicts with an existing account partition."
                    )
            else:
                owner = source["owner"]
                alias = str(source["metadata"].get("source_account_alias") or "")
                filename = str(source["metadata"].get("source_file") or "Native backup")
                label = (
                    f"{alias or owner} · {owner[-6:]}"
                    if owner
                    else "Unconfirmed · " + filename
                )
                connection.execute(
                    "INSERT INTO partitions VALUES(?,?,?,?,?,?,?,?)",
                    (
                        identifier,
                        source["account_key"],
                        owner,
                        label,
                        "Verified archive ID"
                        if owner
                        else "Source account unconfirmed",
                        "iPhone archive",
                        filename,
                        "",
                    ),
                )
                report["added_partitions"] += 1
                expect_append(
                    "partitions",
                    connection.execute(
                        "SELECT * FROM partitions WHERE id=?", (identifier,)
                    ).fetchone(),
                )
        for batch in prepared:
            original_snapshot = batch["snapshot_id"]
            snapshots = batch["snapshots"]
            for identifier, source in batch["metadata"].items():
                prior = connection.execute(
                    "SELECT id FROM source_snapshots WHERE snapshot_id=? AND source_id=? AND metadata=?",
                    (original_snapshot, identifier, source["metadata_json"]),
                ).fetchone()
                if prior is None:
                    cursor = connection.execute(
                        "INSERT INTO source_snapshots(snapshot_id,source_id,metadata) VALUES(?,?,?)",
                        (original_snapshot, identifier, source["metadata_json"]),
                    )
                    snapshots[identifier] = cursor.lastrowid
                    report["added_source_snapshots"] += 1
                    expect_append(
                        "source_snapshots",
                        (
                            snapshots[identifier],
                            original_snapshot,
                            identifier,
                            source["metadata_json"],
                        ),
                    )
                else:
                    snapshots[identifier] = prior[0]
        conversations, by_hash, ordinals = {}, {}, {}
        for identifier, source in metadata.items():
            for row in connection.execute(
                "SELECT * FROM conversations INDEXED BY conversation_account_kind WHERE account_key=? AND partition_id=?",
                (source["account_key"], identifier),
            ):
                value = dict(row)
                conversations[row["id"]] = value
                if row["peer_id"]:
                    by_hash.setdefault(
                        (identifier, hashlib.md5(row["peer_id"].encode()).hexdigest()),
                        set(),
                    ).add(row["id"])
        loaded = set()
        server_loaded = set()

        def load_candidates(identifiers, source, *, needs_server):
            for conversation_id in identifiers:
                if conversation_id not in loaded:
                    maximum, count = connection.execute(
                        f"SELECT COALESCE(MAX(ordinal),0),COUNT(*) FROM {message_read} WHERE conversation_id=?",
                        (conversation_id,),
                    ).fetchone()
                    summary = connection.execute(
                        "SELECT message_count FROM conversations WHERE id=?",
                        (conversation_id,),
                    ).fetchone()[0]
                    if count != summary:
                        raise ValueError(
                            "An incoming conversation has inconsistent retained counts."
                        )
                    ordinals[conversation_id] = maximum
                    loaded.add(conversation_id)
                if (
                    source["verified"]
                    and needs_server
                    and conversation_id not in server_loaded
                ):
                    for row in connection.execute(
                        f"SELECT id,provenance FROM {message_read} WHERE conversation_id=?",
                        (conversation_id,),
                    ):
                        report["indexed_existing_messages"] += 1
                        sid = _server_id(json.loads(row["provenance"]).get("server_id"))
                        if sid:
                            connection.execute(
                                "INSERT OR IGNORE INTO reconcile_server_ids VALUES(?,?,?)",
                                (conversation_id, sid, row["id"]),
                            )
                    for row in connection.execute(
                        f"SELECT e.message_id,e.source_id,e.observation FROM {message_alias} CROSS JOIN message_evidence e INDEXED BY evidence_message WHERE m.conversation_id=? AND e.message_id=m.id",
                        (conversation_id,),
                    ):
                        report["indexed_existing_evidence"] += 1
                        if row["source_id"] != source["metadata"]["source_id"]:
                            raise ValueError(
                                "Retained message evidence crosses its source account."
                            )
                        sid = _server_id(
                            json.loads(row["observation"]).get("server_id")
                        )
                        if sid:
                            connection.execute(
                                "INSERT OR IGNORE INTO reconcile_server_ids VALUES(?,?,?)",
                                (conversation_id, sid, row["message_id"]),
                            )
                    server_loaded.add(conversation_id)

        touched = set()
        for batch, raw in _batch_records(prepared):
            report["input_records"] += 1
            if not isinstance(raw, dict) or not isinstance(raw.get("source_id"), str):
                raise ValueError(
                    "Native text records need declared source identifiers."
                )
            identifier = raw["source_id"]
            if identifier in exclusions:
                report["excluded_records"] += 1
                continue
            if identifier not in batch["metadata"]:
                raise ValueError("A native record lacks declared source metadata.")
            source = batch["metadata"][identifier]
            record = _normalize_record(raw, source, batch["snapshots"][identifier])
            candidates = set(by_hash.get((identifier, record["conversation_hash"]), ()))
            for key in (record["peer"], record["conversation_hash"]):
                candidate = "wechat:" + _identifier(identifier, key)
                if key and candidate in conversations:
                    candidates.add(candidate)
            if candidates:
                preferred = "wechat:" + _identifier(identifier, record["peer"])
                conversation_id = (
                    preferred
                    if record["peer"] and preferred in candidates
                    else min(candidates)
                )
            else:
                conversation_id = "wechat:" + _identifier(
                    identifier, record["peer"] or record["conversation_hash"]
                )
                connection.execute(
                    "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        conversation_id,
                        identifier,
                        source["account_key"],
                        record["peer"],
                        record["title"],
                        record["kind"],
                        0,
                        0,
                        0,
                        "",
                        "",
                        "",
                    ),
                )
                conversations[conversation_id] = {
                    "id": conversation_id,
                    "partition_id": identifier,
                }
                candidates.add(conversation_id)
                report["added_conversations"] += 1
                expected_conversations[conversation_id] = (
                    conversation_id,
                    identifier,
                    source["account_key"],
                    record["peer"],
                    record["title"],
                    record["kind"],
                )
            by_hash.setdefault((identifier, record["conversation_hash"]), set()).update(
                candidates
            )
            load_candidates(candidates, source, needs_server=bool(record["server_id"]))
            direct = connection.execute(
                "SELECT * FROM messages WHERE id=?", (record["base_id"],)
            ).fetchone()
            match, match_kind = None, "new"
            identity_conflict = direct is not None and (
                direct["conversation_id"] not in candidates
                or not _compatible(direct, record)
            )
            if identity_conflict:
                report["identity_conflicts"] += 1
                variant_id = _identifier(
                    identifier, record["record_id"], "variant", record["observation_id"]
                )
                variant = connection.execute(
                    "SELECT * FROM messages WHERE id=?", (variant_id,)
                ).fetchone()
                if variant is not None:
                    if variant["conversation_id"] not in candidates or not _compatible(
                        variant, record
                    ):
                        raise ValueError(
                            "A retained variant conflicts with its native identity."
                        )
                    match, match_kind = variant, "extraction_id_variant"
            elif direct is not None:
                match, match_kind = direct, "extraction_id"
            sid_conflict = False
            if match is None and source["verified"] and record["server_id"]:
                marks = ",".join("?" for _ in candidates)
                for candidate in connection.execute(
                    "SELECT m.* FROM reconcile_server_ids s CROSS JOIN messages m WHERE s.conversation_id IN ("
                    + marks
                    + ") AND s.server_id=? AND m.id=s.message_id ORDER BY m.id",
                    [*sorted(candidates), record["server_id"]],
                ):
                    sid_conflict = True
                    if _compatible(candidate, record):
                        match, match_kind = candidate, "server_id"
                        break
            if match is not None:
                if _is_replay(match, record):
                    report["replayed_observations"] += 1
                    continue
                evidence = (
                    record["observation_id"],
                    match["id"],
                    identifier,
                    batch["snapshot_id"],
                    record["record_id"],
                    record["observation_json"],
                    match_kind,
                )
                prior = connection.execute(
                    "SELECT * FROM message_evidence WHERE id=?", (evidence[0],)
                ).fetchone()
                if prior is not None:
                    if tuple(prior) != evidence:
                        raise ValueError(
                            "Retained evidence conflicts with its identifier."
                        )
                    report["replayed_observations"] += 1
                else:
                    connection.execute(
                        "INSERT INTO message_evidence VALUES(?,?,?,?,?,?,?)", evidence
                    )
                    expect_append("message_evidence", evidence)
                    report["new_evidence"] += 1
                    report["matched_messages"] += 1
                if source["verified"] and record["server_id"]:
                    connection.execute(
                        "INSERT OR IGNORE INTO reconcile_server_ids VALUES(?,?,?)",
                        (match["conversation_id"], record["server_id"], match["id"]),
                    )
                continue
            message_id = (
                _identifier(
                    identifier, record["record_id"], "variant", record["observation_id"]
                )
                if identity_conflict
                else record["base_id"]
            )
            ordinals[conversation_id] += 1
            row = (
                message_id,
                conversation_id,
                ordinals[conversation_id],
                record["sender_id"] or None,
                record["sender"],
                record["direction"],
                record["text"],
                record["time_sort"],
                record["timestamp"],
                record["timestamp_label"],
                record["basis"],
                _json(record["provenance"]),
                record["flags"],
                "\n".join(
                    (record["text"], record["sender"], record["title"], record["peer"])
                ).casefold(),
            )
            connection.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row
            )
            expect_append("messages", row)
            if source["verified"] and record["server_id"]:
                connection.execute(
                    "INSERT OR IGNORE INTO reconcile_server_ids VALUES(?,?,?)",
                    (conversation_id, record["server_id"], message_id),
                )
            touched.add(conversation_id)
            connection.execute(
                "INSERT OR IGNORE INTO restore_touched VALUES(?)", (conversation_id,)
            )
            report["inserted_messages"] += 1
            report["conflict_variants"] += bool(identity_conflict or sid_conflict)
        scope["statistics"] = True
        try:
            for identifier in touched:
                _update_conversation(connection, identifier)
        finally:
            scope["statistics"] = False
        for identifier in touched:
            _verify_conversation_statistics(connection, identifier, message_read)
        for table in expected:
            if _added_fingerprint(connection, table) != tuple(expected[table]):
                raise ValueError("Staged native rows differ from validated input.")
        added_conversations = {
            row[0]: tuple(row)[1:]
            for row in connection.execute(
                "SELECT c.id,c.partition_id,c.account_key,c.peer_id,c.title,c.kind FROM restore_added_conversations added CROSS JOIN conversations c WHERE c.id=added.id"
            )
        }
        if added_conversations != {
            key: value[1:] for key, value in expected_conversations.items()
        }:
            raise ValueError(
                "Staged conversation identities differ from validated input."
            )
        for old in old_partitions:
            if (
                tuple(
                    connection.execute(
                        "SELECT * FROM partitions WHERE id=?", (old[0],)
                    ).fetchone()
                )
                != old
            ):
                raise ValueError("A retained account partition changed.")
        if (
            connection.execute(
                "SELECT COALESCE(SUM(message_count),0) FROM conversations"
            ).fetchone()[0]
            != report["messages_before"] + report["inserted_messages"]
        ):
            raise ValueError(
                "Conversation counts differ from append-only message totals."
            )
        report.update(
            messages_after=report["messages_before"] + report["inserted_messages"],
            touched_conversations=len(touched),
            indexed_conversations=len(loaded),
            server_indexed_conversations=len(server_loaded),
            count_verification="Incoming conversations checked against indexed rows; untouched totals retain manifest and summary counters.",
            old_messages_unchanged=True,
            old_partitions_unchanged=True,
            old_evidence_unchanged=True,
            old_conversation_identity_unchanged=True,
        )
        report["changed"] = any(
            report[key]
            for key in (
                "inserted_messages",
                "new_evidence",
                "added_partitions",
                "added_conversations",
                "added_source_snapshots",
            )
        )
        if report["changed"]:
            manifest = json.loads(_json(manifest_before))
            counts = {
                row[0]: (row[1], row[2])
                for row in connection.execute(
                    "SELECT p.id,COUNT(c.id),COALESCE(SUM(c.message_count),0) FROM partitions p LEFT JOIN conversations c ON c.partition_id=p.id GROUP BY p.id"
                )
            }
            entries = {}
            for entry in manifest["sources"]:
                if (
                    not isinstance(entry, dict)
                    or not isinstance(entry.get("source_id"), str)
                    or entry["source_id"] in entries
                ):
                    raise ValueError(
                        "Retained manifest sources need unique identifiers."
                    )
                entries[entry["source_id"]] = entry
            for identifier, source in metadata.items():
                entry = entries.get(identifier)
                if entry is None:
                    entry = {"source_id": identifier, "account_id": source["owner"]}
                    manifest["sources"].append(entry)
                entry.update(
                    conversations=counts[identifier][0], messages=counts[identifier][1]
                )
            manifest["messages"] = report["messages_after"]
            manifest["unique_original_message_count"] = None
            events = manifest.setdefault("reconciliations", [])
            if not isinstance(events, list):
                raise ValueError("Retained reconciliation history is not a list.")
            events.append(
                {
                    "snapshot_id": snapshot_id,
                    "at": datetime.now(UTC).isoformat(),
                    "method": "apfs_native_text_subset"
                    if len(prepared) == 1
                    else "apfs_native_text_batches",
                    "source_snapshot_ids": report["source_snapshot_ids"],
                    **{
                        key: report[key]
                        for key in (
                            "inserted_messages",
                            "matched_messages",
                            "new_evidence",
                            "conflict_variants",
                            "messages_before",
                            "messages_after",
                        )
                    },
                }
            )
            scope["manifest"] = True
            try:
                if connection.execute(
                    "SELECT 1 FROM archive_info WHERE key='manifest'"
                ).fetchone():
                    connection.execute(
                        "UPDATE archive_info SET value=? WHERE key='manifest'",
                        (_json(manifest),),
                    )
                else:
                    connection.execute(
                        "INSERT INTO archive_info VALUES('manifest',?)",
                        (_json(manifest),),
                    )
            finally:
                scope["manifest"] = False
        connection.commit()
        if [row[0] for row in connection.execute("PRAGMA integrity_check")] != ["ok"]:
            raise ValueError("The restored native archive failed integrity_check.")
        report["integrity_check"] = "ok"
        connection.close()
        connection = None
        report["staged_database_bytes"] = stage.stat().st_size
        if apply and report["changed"]:
            # A writable open can decompress APFS files; Windows needs it for fsync.
            with stage.open("r+b" if os.name == "nt" else "rb") as stream:
                os.fsync(stream.fileno())
        if not _content_identity_matches(
            target, initial, original_sha256, database=True
        ) or not _content_identity_matches(policy, policy_identity, policy_sha256):
            raise ValueError(
                "The archive or exclusions changed during native restoration."
            )
        if apply and report["changed"]:
            os.replace(stage, target)
            report["published"] = True
        report["database_bytes"] = target.stat().st_size if target.exists() else 0
        return report
    finally:
        if connection is not None:
            connection.close()
        for suffix in ("", "-journal", "-wal", "-shm"):
            Path(str(stage) + suffix).unlink(missing_ok=True)
