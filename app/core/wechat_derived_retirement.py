"""Stage proved export redundancies as compact aliases without publishing history.

Code version: v1.1.3-codex.0
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from datetime import datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile


VERSION = "v1.1.3-codex.0"
MAX_MATCHES = 250_000
WRITE_BATCH_SIZE = 256
METADATA_BATCH_SIZE = 64
_ALIAS_SCHEMA = """CREATE TABLE IF NOT EXISTS derived_message_aliases (
 old_id TEXT PRIMARY KEY,native_id TEXT NOT NULL,source_id TEXT NOT NULL,
 metadata TEXT NOT NULL,proof TEXT NOT NULL
) WITHOUT ROWID"""
_ID = re.compile(r"[0-9a-f]{64}")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def logical_message_sha256(row):
    """Hash only the complete logical messages row, without joined source fields."""
    return hashlib.sha256(_json(dict(row)).encode()).hexdigest()


def file_identity(path):
    state = Path(path).stat()
    if Path(path).is_symlink() or not stat.S_ISREG(state.st_mode) or getattr(state, "st_flags", 0) & 0x40000000:
        raise ValueError("Retirement inputs must be resident regular files.")
    return dict(dev=state.st_dev, inode=state.st_ino, size=state.st_size,
                mtime_ns=state.st_mtime_ns, ctime_ns=state.st_ctime_ns)


def _readonly(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _inputs(root, plan):
    if "group_frame_registry_digest" in plan:
        from .wechat_html_group_frames import group_frame_registry_digest

        if group_frame_registry_digest(root) != plan["group_frame_registry_digest"]:
            raise ValueError("The reviewed group-source declarations changed after planning.")
    directory = Path(root).expanduser().absolute() / "messaging" / "wechat"
    history, registry = directory / "history.sqlite3", directory / "contacts.sqlite3"
    for path, key in ((history, "history_identity"), (registry, "registry_identity")):
        if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise ValueError("Finish all source database journals before retirement.")
        if file_identity(path) != plan.get(key):
            raise ValueError("A retirement input differs from its pinned plan.")
    return history, registry


def _no_writers(path):
    result = subprocess.run(["lsof", "+c", "0", "-nP", "-Fpcfa", "--", str(path)],
                            capture_output=True, text=True, timeout=30, check=False)
    if result.returncode not in (0, 1) or result.stderr.strip():
        raise ValueError("Open-file inspection failed before retirement.")
    command = ""
    for line in result.stdout.splitlines():
        if line.startswith("c"):
            command = line[1:].casefold()
        elif line in {"aw", "au"} and command not in {"fileproviderd", "fileprovider"}:
            raise ValueError("A process has the retirement input open for writing.")


def _object(encoded):
    value = json.loads(encoded)
    if not isinstance(value, dict):
        raise ValueError("Expected object-shaped source evidence.")
    return value


def _plain(row):
    provenance = _object(row["provenance"])
    locator = provenance.get("locator")
    exported_type = (
        locator.get("export_msgtype", provenance.get("export_msgtype"))
        if isinstance(locator, dict) else provenance.get("export_msgtype")
    )
    if (
        provenance.get("resource_reference") is not None
        or provenance.get("message_type") not in {None, 1}
        or isinstance(provenance.get("message_type"), bool)
        or exported_type is not None
        and (isinstance(exported_type, bool) or str(exported_type) != "1")
    ):
        raise ValueError("Only complete top-level plain text can be retired.")
    disallowed = {"forwarded_components", "text_components", "quoted_components", "quoted_reference", "referred_message", "refermsg", "payment", "resource", "resource_reference", "payment_card_fields"}
    pending = [provenance]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            if any(value.get(key) for key in disallowed):
                raise ValueError("Only complete top-level plain text can be retired.")
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    if not isinstance(row["text"], str) or not row["text"] or row["text"].lstrip().startswith(("<msg", "<?xml")):
        raise ValueError("Only complete top-level plain text can be retired.")
    return provenance


def _context(connection, row):
    conversation = connection.execute("SELECT * FROM conversations WHERE id=?", (row["conversation_id"],)).fetchone()
    if conversation is None:
        raise ValueError("A retirement conversation is missing.")
    partition = connection.execute("SELECT * FROM partitions WHERE id=?", (conversation["partition_id"],)).fetchone()
    if partition is None:
        raise ValueError("A retirement source partition is missing.")
    return dict(conversation), dict(partition)


def _wall(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None).isoformat(timespec="seconds")


def _native(connection, row, conversation, partition):
    provenance = _plain(row)
    owner = partition["account_id"]
    owner_hash = hashlib.md5(owner.encode()).hexdigest() if owner else ""
    if (not owner or partition["id"] != "ios-" + owner_hash
            or partition["account_key"] != "account:" + owner
            or partition["identity_status"] != "Verified archive ID"
            or partition["source_label"] not in {"iPhone archive", "iPad archive"}
            or conversation["kind"] != "group" or provenance.get("message_type") != 1
            or provenance.get("text_extraction_kind") != "plain_text"
            or not re.fullmatch(r"-?[1-9][0-9]*", str(provenance.get("server_id", "")))):
        raise ValueError("The retained counterpart is not a verified native text event.")
    snapshot = connection.execute("SELECT * FROM source_snapshots WHERE id=?", (provenance.get("source_snapshot"),)).fetchone()
    metadata = _object(snapshot["metadata"]) if snapshot else {}
    if (metadata.get("source_id") != partition["id"] or metadata.get("source_account_id") != owner
            or metadata.get("account_hash") != owner_hash or metadata.get("account_hash_verified") is not True):
        raise ValueError("The native source snapshot does not prove its owner.")
    return provenance


def _pair(connection, match):
    derived_id, native_id = match.get("derived_id"), match.get("native_id")
    if (not isinstance(derived_id, str) or not _ID.fullmatch(derived_id)
            or not isinstance(native_id, str) or not _ID.fullmatch(native_id) or derived_id == native_id):
        raise ValueError("A retirement needs two distinct exact message IDs.")
    rows = {row["id"]: dict(row) for row in connection.execute("SELECT * FROM messages WHERE id IN (?,?)", (derived_id, native_id))}
    native = rows.get(native_id)
    if native is None or logical_message_sha256(native) != match.get("native_sha256"):
        raise ValueError("The retained native row differs from its plan.")
    native_conversation, native_partition = _context(connection, native)
    native_provenance = _native(connection, native, native_conversation, native_partition)
    derived = rows.get(derived_id)
    if derived is None:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='derived_message_aliases' AND type='table'").fetchone():
            alias = connection.execute("SELECT * FROM derived_message_aliases WHERE old_id=?", (derived_id,)).fetchone()
            if alias and alias["native_id"] == native_id and _object(alias["metadata"]).get("original_sha256") == match.get("derived_sha256"):
                return None
        raise ValueError("The planned derived observation is unavailable.")
    if logical_message_sha256(derived) != match.get("derived_sha256"):
        raise ValueError("The derived row differs from its plan.")
    derived_provenance = _plain(derived)
    conversation, partition = _context(connection, derived)
    evidence = match.get("evidence", {})
    rooms = {value for value in (conversation["peer_id"], conversation["title"])
             if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*@(?:im\.)?chatroom", value)}
    expected_account = "account:" + partition["account_id"] if partition["account_id"] else "source:" + partition["id"]
    if (partition["source_label"] not in {"Excel export", "HTML export"}
            or not partition["id"].startswith(("xlsx-", "html-"))
            or partition["account_key"] != expected_account
            or conversation["kind"] not in {"group", "unclassified"}
            or rooms != {native_conversation["peer_id"]}
            or derived["text"] != native["text"] or _wall(derived["timestamp_value"]) != _wall(native["timestamp_value"])
            or not isinstance(evidence, dict) or evidence.get("method") != "exact_group_author_body_clock_order_v1"
            or type(evidence.get("anchor_count")) is not int or evidence["anchor_count"] < 3
            or evidence.get("body_sha256") != hashlib.sha256(native["text"].encode()).hexdigest()
            or evidence.get("derived_source_id") != partition["id"]
            or evidence.get("native_source_id") != native_partition["id"]
            or evidence.get("derived_conversation_id") != conversation["id"]
            or evidence.get("native_conversation_id") != native_conversation["id"]
            or evidence.get("derived_owner_unconfirmed") is not (not bool(partition["account_id"]))
            or evidence.get("wall_clock") != _wall(derived["timestamp_value"])
            or evidence.get("room_id") != native_conversation["peer_id"]
            or evidence.get("native_server_id") != str(native_provenance["server_id"])):
        raise ValueError("The exact group/source/event proof is incomplete or conflicting.")
    return dict(derived=derived, native=native, conversation=conversation,
                partition=partition, provenance=derived_provenance, proof=evidence)


def _authors(root, connection, pairs):
    from .wechat_contact_analysis import _requests, _ROW_SELECT
    from .wechat_contacts import WechatContactRepository

    resolver = WechatContactRepository(root)
    for offset in range(0, len(pairs), 150):
        batch = pairs[offset:offset+150]
        ids = sorted({pair[key]["id"] for pair in batch for key in ("derived", "native")})
        rows = [dict(row) for row in connection.execute(_ROW_SELECT + "WHERE m.id IN (" + ",".join("?" for _ in ids) + ")", ids)]
        identities = resolver.resolve_messages(_requests(connection, rows))
        for pair in batch:
            for key in ("derived", "native"):
                identity = identities.get(pair[key]["id"], {})
                if identity.get("identity_status") != "verified" or identity.get("id") != pair["proof"].get("contact_id"):
                    raise ValueError("A retirement author no longer matches its verified identity.")


def _guards(connection, pairs, physical):
    connection.execute("CREATE TEMP TABLE retirement_allowed_messages(id TEXT PRIMARY KEY,native_id TEXT)")
    connection.executemany("INSERT INTO retirement_allowed_messages VALUES(?,?)", [(p["derived"]["id"], p["native"]["id"]) for p in pairs])
    connection.execute("CREATE TEMP TABLE retirement_allowed_conversations(id TEXT PRIMARY KEY)")
    connection.executemany("INSERT INTO retirement_allowed_conversations VALUES(?)", [(key,) for key in sorted({p["conversation"]["id"] for p in pairs})])
    protected = {physical, "conversations", "partitions", "source_snapshots", "export_imports", "shared_message_bodies", "archive_info", "message_evidence"}
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in sorted(protected & tables):
        for operation in ("INSERT", "UPDATE", "DELETE"):
            allow = "0"
            if table == physical and operation == "DELETE":
                allow = "OLD.id IN (SELECT id FROM retirement_allowed_messages)"
            elif table == "conversations" and operation == "UPDATE":
                allow = "OLD.id=NEW.id AND OLD.id IN (SELECT id FROM retirement_allowed_conversations)"
            elif table == "archive_info" and operation == "UPDATE":
                allow = "OLD.key='manifest' AND NEW.key=OLD.key"
            elif table == "message_evidence" and operation == "UPDATE":
                columns = [row[1] for row in connection.execute("PRAGMA table_info(message_evidence)") if row[1] != "message_id"]
                unchanged = " AND ".join('NEW."'+name+'" IS OLD."'+name+'"' for name in columns)
                allow = "NEW.message_id=(SELECT native_id FROM retirement_allowed_messages WHERE id=OLD.message_id)" + (" AND " + unchanged if unchanged else "")
            connection.execute(f'CREATE TEMP TRIGGER retirement_guard_{table}_{operation.lower()} BEFORE {operation} ON main."{table}" WHEN NOT COALESCE(({allow}),0) BEGIN SELECT RAISE(ABORT,\'Retirement touched an unauthorized row\'); END')


def _fsync(path):
    with Path(path).open("rb") as stream:
        os.fsync(stream.fileno())


def _backup(path, rows, header, *, check_space=None):
    with path.open("xb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as output:
        values = [header, *rows]
        for offset in range(0, len(values), METADATA_BATCH_SIZE):
            encoded = b"".join((_json(value) + "\n").encode() for value in values[offset:offset+METADATA_BATCH_SIZE])
            if check_space is not None:
                check_space("rollback", len(encoded)*2)
            output.write(encoded)
    path.chmod(0o600)
    _fsync(path)
    with gzip.open(path, "rt", encoding="utf-8") as source:
        if [json.loads(line) for line in source] != [header, *rows]:
            raise ValueError("The compact retirement rollback failed round-trip verification.")


def _counters(connection, identifiers):
    for identifier in sorted(identifiers):
        count = connection.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=?", (identifier,)).fetchone()[0]
        first = connection.execute("SELECT time_sort,timestamp_value FROM messages WHERE conversation_id=? ORDER BY (time_sort>0) DESC,time_sort,ordinal,id LIMIT 1", (identifier,)).fetchone()
        last = connection.execute("SELECT time_sort,timestamp_value,text FROM messages WHERE conversation_id=? ORDER BY (time_sort>0) DESC,time_sort DESC,ordinal DESC,id DESC LIMIT 1", (identifier,)).fetchone()
        connection.execute("UPDATE conversations SET message_count=?,first_sort=?,last_sort=?,first_time=?,last_time=?,latest_text=? WHERE id=?",
                           (count, first[0] if first else 0, last[0] if last else 0,
                            first[1] if first else "", last[1] if last else "", last[2][:300] if last else "", identifier))


def stage_retirement(local_store_root, plan, *, backup_directory, apply=False):
    """Return a verified candidate; the caller owns sidecar handling and publication.

    Dry runs create no files. Apply writes only its new APFS clone and an external,
    compressed rollback containing the removed rows, never a full archive copy.
    """
    if not isinstance(plan, dict) or not isinstance(plan.get("version"), str):
        raise ValueError("An explicitly versioned retirement plan is required.")
    matches = plan.get("matches")
    if not isinstance(matches, list) or len(matches) > MAX_MATCHES or any(not isinstance(row, dict) for row in matches):
        raise ValueError("The retirement plan exceeds its supported bounds.")
    if len({row.get("derived_id") for row in matches}) != len(matches):
        raise ValueError("A derived observation appears more than once in the plan.")
    history, registry = _inputs(local_store_root, plan)
    with closing(_readonly(history)) as source:
        pairs = [pair for match in matches if (pair := _pair(source, match)) is not None]
        _authors(local_store_root, source, pairs)
        total = source.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        manifest_row = source.execute("SELECT value FROM archive_info WHERE key='manifest'").fetchone()
        manifest = _object(manifest_row[0]) if manifest_row else None
        if manifest is None or manifest.get("messages") != total:
            raise ValueError("The archive manifest does not match its complete logical row count.")
        retired_by_source = Counter(pair["partition"]["id"] for pair in pairs)
        for source_id, count in retired_by_source.items():
            declared = [item for item in manifest.get("sources", []) if isinstance(item, dict) and item.get("source_id") == source_id]
            actual = source.execute("SELECT COUNT(*) FROM conversations c JOIN messages m ON m.conversation_id=c.id WHERE c.partition_id=?", (source_id,)).fetchone()[0]
            if len(declared) != 1 or type(declared[0].get("messages")) is not int or declared[0]["messages"] != actual or actual < count:
                raise ValueError("An affected source manifest count is incomplete or conflicting.")
        page_size = source.execute("PRAGMA page_size").fetchone()[0]
    _inputs(local_store_root, plan)
    estimates = [len(_json(pair).encode()) * 4 + page_size * 20 for pair in pairs]
    estimate = sum(estimates)
    overhead = page_size*32
    batch_estimates = [sum(estimates[offset:offset+WRITE_BATCH_SIZE])+overhead
                       for offset in range(0, len(estimates), WRITE_BATCH_SIZE)]
    peak_write = max(batch_estimates, default=0)
    affected = {pair["conversation"]["id"] for pair in pairs}
    metadata_reserve = overhead + max(page_size*8*min(len(affected), METADATA_BATCH_SIZE), len(_json(manifest).encode())*4)
    minimum = plan.get("minimum_free_bytes", 1024**3)
    if type(minimum) is not int or minimum < 0:
        raise ValueError("The minimum free-space budget must be a nonnegative integer.")
    required = minimum + peak_write + metadata_reserve
    report = dict(version=VERSION, dry_run=not apply, matches=len(pairs), already_retired=len(matches)-len(pairs),
                  messages_before=total, messages_after=total-len(pairs),
                  estimated_peak_incremental_bytes=peak_write+metadata_reserve,
                  worst_case_incremental_bytes=estimate, peak_write_batch_bytes=peak_write,
                  metadata_write_reserve_bytes=metadata_reserve, write_batch_size=WRITE_BATCH_SIZE,
                  conversations_affected=len(affected),
                  required_free_bytes=required, filesystem_bytes_reclaimed=0, published=False,
                  plan_sha256=hashlib.sha256(_json(plan).encode()).hexdigest())
    if not apply or not pairs:
        return dict(candidate_path=None, report=report)
    backup = Path(backup_directory).expanduser().absolute()
    if backup.exists() or backup.is_symlink() or backup.resolve().is_relative_to(Path(local_store_root).resolve()):
        raise ValueError("Select a new external rollback directory outside the runtime store.")
    free_samples = []

    def space(phase, next_write=0):
        available = shutil.disk_usage(history.parent).free
        free_samples.append(dict(phase=phase, free_bytes=available))
        if available < minimum + next_write + metadata_reserve:
            raise ValueError("Retirement would exceed the free-space budget during " + phase + ".")
        return available

    space("initial", peak_write)
    _no_writers(history)
    _no_writers(registry)
    backup.mkdir(parents=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=".wechat-derived-retire-", suffix=".sqlite3", dir=history.parent)
    os.close(descriptor)
    candidate = Path(name)
    candidate.unlink()
    try:
        from .wechat_export_merge import _clonefile

        _clonefile(history, candidate)
        candidate.chmod(0o600)
        allocated_before = candidate.stat().st_blocks*512
        _inputs(local_store_root, plan)
        rollback = []
        with closing(_readonly(history)) as source:
            rollback.extend(dict(kind="conversation", row=pair["conversation"]) for pair in {p["conversation"]["id"]: p for p in pairs}.values())
            for pair in pairs:
                rollback.append(dict(kind="message", row=pair["derived"]))
                if source.execute("SELECT 1 FROM sqlite_master WHERE name='message_evidence' AND type='table'").fetchone():
                    rollback.extend(dict(kind="message_evidence", row=dict(row)) for row in source.execute("SELECT * FROM message_evidence WHERE message_id=?", (pair["derived"]["id"],)))
        rollback_path = backup / "removed-observations.jsonl.gz"
        _backup(rollback_path, rollback, dict(kind="header", version=VERSION, plan_sha256=report["plan_sha256"], history_identity=plan["history_identity"], manifest=manifest), check_space=space)
        with closing(sqlite3.connect(candidate)) as output:
            output.row_factory = sqlite3.Row
            output.execute("PRAGMA journal_mode=DELETE")
            output.execute("PRAGMA synchronous=FULL")
            output.execute("PRAGMA cache_size=-16384")
            space("alias-schema", overhead)
            output.execute(_ALIAS_SCHEMA)
            output.commit()
            space("alias-schema-committed")
            physical = "message_storage" if output.execute("SELECT type FROM sqlite_master WHERE name='messages'").fetchone()[0] == "view" else "messages"
            has_evidence = output.execute("SELECT 1 FROM sqlite_master WHERE name='message_evidence' AND type='table'").fetchone()
            _guards(output, pairs, physical)
            matches_by_id = {value["derived_id"]: value for value in matches}
            output.commit()
            for batch_index, offset in enumerate(range(0, len(pairs), WRITE_BATCH_SIZE)):
                space("message-batch", batch_estimates[batch_index])
                output.execute("BEGIN IMMEDIATE")
                for pair in pairs[offset:offset+WRITE_BATCH_SIZE]:
                    derived, native = pair["derived"], pair["native"]
                    match = matches_by_id[derived["id"]]
                    if _pair(output, match) is None:
                        raise ValueError("A staged retirement unexpectedly already exists.")
                    metadata = dict(original_sha256=match["derived_sha256"], body_sha256=pair["proof"]["body_sha256"],
                                    row={k: v for k, v in derived.items() if k not in {"text", "search_text", "provenance"}},
                                    provenance_sha256=hashlib.sha256(derived["provenance"].encode()).hexdigest(),
                                    provenance={k: pair["provenance"][k] for k in ("record_id", "source_snapshot", "source_time", "locator") if k in pair["provenance"]},
                                    conversation={k: v for k, v in pair["conversation"].items() if k != "latest_text"}, source=pair["partition"])
                    output.execute("INSERT INTO derived_message_aliases VALUES(?,?,?,?,?)", (derived["id"], native["id"], pair["partition"]["id"], _json(metadata), _json(pair["proof"])))
                    if has_evidence:
                        output.execute("UPDATE message_evidence SET message_id=? WHERE message_id=?", (native["id"], derived["id"]))
                    output.execute("DELETE FROM " + physical + " WHERE id=?", (derived["id"],))
                output.commit()
                space("message-batch-committed")
            conversations = sorted(affected)
            for offset in range(0, len(conversations), METADATA_BATCH_SIZE):
                subset = conversations[offset:offset+METADATA_BATCH_SIZE]
                space("conversation-batch", page_size*8*len(subset)+overhead)
                output.execute("BEGIN IMMEDIATE")
                _counters(output, subset)
                output.commit()
                space("conversation-batch-committed")
            space("manifest", len(_json(manifest).encode())*4+overhead)
            manifest["messages"] = total-len(pairs)
            for item in manifest["sources"]:
                if isinstance(item, dict) and item.get("source_id") in retired_by_source:
                    item["messages"] -= retired_by_source[item["source_id"]]
            output.execute("UPDATE archive_info SET value=? WHERE key='manifest'", (_json(manifest),))
            output.commit()
            space("manifest-committed")
            if [tuple(row) for row in output.execute("PRAGMA quick_check")] != [("ok",)]:
                raise ValueError("The retirement candidate failed its integrity check.")
            if output.execute("SELECT COUNT(*) FROM messages").fetchone()[0] != total-len(pairs):
                raise ValueError("The retirement changed an unexpected number of observations.")
            for pair in pairs:
                retained = output.execute("SELECT * FROM messages WHERE id=?", (pair["native"]["id"],)).fetchone()
                if retained is None or logical_message_sha256(retained) != logical_message_sha256(pair["native"]):
                    raise ValueError("Retirement changed a retained native observation.")
        _fsync(candidate)
        _inputs(local_store_root, plan)
        space("verified")
        report.update(candidate_identity=file_identity(candidate), rollback_path=str(rollback_path),
                      rollback_sha256=hashlib.sha256(rollback_path.read_bytes()).hexdigest(),
                      evidence_rows_moved=sum(row["kind"] == "message_evidence" for row in rollback),
                      candidate_allocated_bytes=candidate.stat().st_blocks*512,
                      candidate_allocated_bytes_delta=candidate.stat().st_blocks*512-allocated_before,
                      message_write_batches=len(batch_estimates),
                      observed_free_bytes_before=free_samples[0]["free_bytes"],
                      observed_free_bytes_after=free_samples[-1]["free_bytes"],
                      observed_free_bytes_minimum=min(row["free_bytes"] for row in free_samples),
                      free_space_observations=free_samples)
        receipt = backup / "staging-receipt.json"
        receipt.write_text(_json(report)+"\n", encoding="utf-8")
        receipt.chmod(0o600)
        _fsync(receipt)
        return dict(candidate_path=str(candidate), report=report)
    except BaseException:
        candidate.unlink(missing_ok=True)
        for suffix in ("-journal", "-wal", "-shm"):
            Path(str(candidate)+suffix).unlink(missing_ok=True)
        raise


retire_derived_redundancy = stage_retirement
