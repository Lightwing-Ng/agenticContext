"""Project only an authenticated requested peer into the existing Mac bank contract.

Code version: v1.8.0-codex.0
"""

from __future__ import annotations

from collections import deque
from concurrent.futures import ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from dataclasses import dataclass, field
from copy import deepcopy
from datetime import datetime
import hashlib
from itertools import chain
import json
from multiprocessing import get_context
import re
import sqlite3

from . import wechat_macos_recovery_bank as bank
from .compute_resources import discover_compute_resources
from .wechat_archive import WechatArchiveRepository, _file_identity, wechat_archive_path
from .wechat_group_membership import native_membership_fields
from .wechat_mac_refresh_source import (
    MAX_NAMES,
    RefreshError,
    RefreshIssue,
    RefreshNotice,
    SourceAccountBindingUnavailable,
)
from .wechat_resource_index import _APP_LABEL_FIELDS, _KINDS, _LABELS, _app_metadata
from .wechat_text import extract_wechat_text, is_native_pat_projection
from .wechat_wcdb import (
    MAX_TEXT_BYTES,
    MissingDictionaryError,
    WCDBBackendUnavailableError,
    WCDBDecodeError,
    decode_wcdb_text,
    get_wcdb_frame_size,
)


_SESSION = re.compile(r"wechat:[0-9a-f]{64}")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SOURCE_BINDING_FIELDS = (
    "source_id",
    "snapshot_id",
    "main_sha256",
    "wal_sha256",
    "database_relative_path",
)
MAX_ARCHIVE_ANCHOR_ROWS = 1_000_000
MAX_NATIVE_TABLES = 10_000
MAX_LEARNED_WITNESSES = 32
_MAX_STORED_WITNESS_PROOF_BYTES = 1024**2
_PROJECTION_BATCH_ROWS = 512
_PROJECTION_BATCH_BYTES = 16 * 1024**2
_PROJECTION_IN_FLIGHT = 4
_MAX_NATIVE_PROJECTION_WORKERS = 16


class _PacketBudgetExceeded(RefreshError):
    """One projected event cannot fit the unchanged serialized packet boundary."""


@dataclass
class _NativeWindow:
    after_local_id: int | None = None
    last_local_id: int | None = None
    byte_limited: bool = False


@dataclass(frozen=True)
class ConversationScope:
    session_id: str
    account_id: str
    peer_id: str
    kind: str


class ConversationComparison:
    """Keep one read-only archive comparison for a single conversation refresh."""

    def __init__(self, history_path, scope, baseline):
        self._history_path = history_path.absolute()
        self._scope = scope
        self._baseline = deepcopy(baseline)
        self._overlay_inputs = None
        self._indexes = None

    def require_current(self, history_path, scope):
        if history_path.absolute() != self._history_path or scope != self._scope:
            raise RefreshError("The archive comparison belongs to another conversation.")
        if bank._inputs(history_path) != self._baseline:
            raise RefreshNotice(RefreshIssue.ARCHIVE_CHANGED)
        overlay_inputs = _comparison_overlay_inputs(history_path)
        if self._overlay_inputs is None:
            self._overlay_inputs = overlay_inputs
        elif overlay_inputs != self._overlay_inputs:
            raise RefreshNotice(RefreshIssue.ARCHIVE_CHANGED)

    def indexes(self, local_store_root, history_path, history, scope, check_cancel):
        if self._indexes is None:
            indexes = _archive_comparison_indexes(
                local_store_root, history_path, history, scope, check_cancel
            )
            self.require_current(history_path, scope)
            self._indexes = indexes
        return self._indexes


@dataclass
class SourceProjectionContext:
    """Reuse metadata from one unchanged authenticated memory-only snapshot."""

    account_id: str
    database: object = field(repr=False)
    tables: frozenset = field(repr=False)
    names: dict = field(repr=False)
    peer_rows: dict = field(repr=False)
    source_digest: str = field(repr=False)
    total_changes: int = field(repr=False)
    schema_version: int = field(repr=False)
    bindings: dict = field(default_factory=dict, repr=False)
    learn_witnesses: bool = field(default=False, repr=False)
    learned_witnesses: dict = field(default_factory=dict, repr=False)

    def require_current(self, database, account_id):
        if (
            database is not self.database
            or account_id != self.account_id
            or bank._sha([database.source, database.account_directory])
            != self.source_digest
            or database.connection.total_changes != self.total_changes
            or database.connection.execute("PRAGMA schema_version").fetchone()[0]
            != self.schema_version
        ):
            raise RefreshError("The authenticated projection context changed.")

    def remember_witness(self, anchor):
        if not self.learn_witnesses:
            return
        key = (anchor["peer_id"], anchor["direction"])
        if key in self.learned_witnesses:
            return
        if len(self.learned_witnesses) >= MAX_LEARNED_WITNESSES:
            if anchor["direction"] != "sent" or any(
                item["direction"] == "sent"
                for item in self.learned_witnesses.values()
            ):
                return
            self.learned_witnesses.pop(next(reversed(self.learned_witnesses)))
        self.learned_witnesses[key] = deepcopy(anchor)


def prepare_source_context(account_id, database, *, _learn_witnesses=False):
    """Read bounded table and sender metadata once, retaining exact owner proof."""
    native = database.connection
    tables = frozenset(
        row[0]
        for row in native.execute("SELECT name FROM sqlite_master WHERE type='table'")
    )
    if len(tables) > MAX_NATIVE_TABLES:
        raise RefreshError("The native database exceeds its bounded table budget.")
    names, peer_rows = {}, {}
    for row in native.execute(
        "SELECT rowid,user_name,is_session FROM Name2Id LIMIT ?", (MAX_NAMES + 1,)
    ):
        if len(names) >= MAX_NAMES:
            raise RefreshError("The native name index exceeds its bounded row budget.")
        value = dict(row)
        names[value["rowid"]] = value
        peer_rows.setdefault(value["user_name"], value)
    _owner_row(names, account_id)
    context = SourceProjectionContext(
        account_id,
        database,
        tables,
        names,
        peer_rows,
        bank._sha([database.source, database.account_directory]),
        native.total_changes,
        native.execute("PRAGMA schema_version").fetchone()[0],
    )
    context.learn_witnesses = _learn_witnesses is True
    return context


def projection_exclusion_reason(error):
    """Classify failures without returning exception text or source content."""
    if isinstance(error, MissingDictionaryError):
        return "row_missing_zstd_dictionary"
    if isinstance(error, WCDBBackendUnavailableError):
        return "row_compressed_text_backend_unavailable"
    if isinstance(error, WCDBDecodeError):
        return "row_wcdb_decode_failure"
    return {
        "A native message body has an unsupported storage class.": "row_unsupported_storage_class",
        "A native event has no supported server identifier.": "row_unsupported_server_identifier",
        "A native app system notice conflicts with its typed source.": "row_app_system_type_conflict",
        "A native pat notice crosses its authenticated conversation.": "row_pat_scope_conflict",
        "A native message has no authenticated sender.": "row_missing_authenticated_sender",
        "The native text cannot be projected faithfully.": "row_unsupported_typed_projection",
    }.get(str(error), "row_projection_rejected")


def resolve_scope(local_store_root, session_id):
    if not isinstance(session_id, str) or _SESSION.fullmatch(session_id) is None:
        raise RefreshNotice(RefreshIssue.INVALID_CONVERSATION)
    path = wechat_archive_path(local_store_root)
    bank._identity(path)
    with sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
    ) as history:
        row = history.execute(
            "SELECT c.account_key,c.peer_id,c.kind,p.account_id,p.account_key "
            "FROM conversations c JOIN partitions p ON p.id=c.partition_id WHERE c.id=?",
            (session_id,),
        ).fetchone()
    if (
        row is None
        or row[2] not in {"direct", "group"}
        or not isinstance(row[1], str)
        or not isinstance(row[3], str)
        or row[0] != "account:" + row[3]
        or row[4] != row[0]
        or row[1].startswith("gh_")
        or (row[2] == "group") != row[1].endswith(("@chatroom", "@im.chatroom"))
    ):
        raise RefreshNotice(RefreshIssue.INVALID_CONVERSATION)
    return ConversationScope(session_id, row[3], row[1], row[2])


def _table(peer):
    return "Msg_" + hashlib.md5(peer.encode(), usedforsecurity=False).hexdigest()


def _name_proof(row):
    return {"row": row, "row_sha256": bank._sha(row)}


def _owner_row(names, owner):
    """Require one exact authenticated owner mapping without assuming its local ID."""
    matches = [row for row in names.values() if row["user_name"] == owner]
    if len(matches) != 1:
        raise RefreshError(
            "The authenticated native owner needs one unambiguous name row."
        )
    try:
        return bank._name_proof(_name_proof(matches[0]), owner)
    except ValueError:
        raise RefreshError(
            "The authenticated native owner mapping is invalid."
        ) from None


def _comparison_signature(row):
    proof = json.loads(row["provenance"])
    # A non-app event has no app subtype. Old source adapters may omit the
    # optional field while the current text parser explicitly returns None.
    if (
        proof.get("message_type") in {1, 10000, 10002}
        and proof.get("appmsg_subtype") is None
    ):
        proof.pop("appmsg_subtype", None)
    return bank._event_signature({**dict(row), "provenance": json.dumps(proof)})


def _comparison_overlay_inputs(history_path):
    from .wechat_archive_relocation import receipt_identity
    from .wechat_resource_details import detail_input_identities
    from .wechat_secondary_completions import completion_input_identities

    relocation = receipt_identity(history_path)
    signing_key = None
    if relocation and not relocation.get("unavailable"):
        key_path = history_path.parent / "browser_cache" / "signing.key"
        try:
            state = key_path.lstat()
        except FileNotFoundError:
            pass
        else:
            signing_key = (
                state.st_dev, state.st_ino, state.st_size, state.st_mtime_ns, state.st_ctime_ns
            )
    return (
        completion_input_identities(history_path),
        detail_input_identities(history_path),
        relocation,
        signing_key,
    )


def _archive_comparison_indexes(local_store_root, history_path, history, scope, check_cancel):
    history.execute("PRAGMA temp_store=MEMORY")
    repository = WechatArchiveRepository(local_store_root)
    _conversations, message_table, status = repository._timeline_tables(
        history,
        _file_identity(history_path),
        conversation_id=scope.session_id,
        strict=True,
    )
    if status not in {"ready", "absent"}:
        raise RefreshError("The existing conversation timeline is temporarily unavailable.")
    known = {}
    for index, existing in enumerate(history.execute(
        f"SELECT * FROM {message_table} WHERE conversation_id=?",
        (scope.session_id,),
    )):
        if index >= MAX_ARCHIVE_ANCHOR_ROWS:
            raise RefreshError("The existing conversation exceeds its bounded row budget.")
        check_cancel()
        proof = json.loads(existing["provenance"])
        known.setdefault(str(proof.get("server_id")), set()).add(
            _comparison_signature(existing)
        )
    archive = {}
    for index, row in enumerate(history.execute(
        "SELECT id,sender_id,direction,text,timestamp_value,provenance"
        " FROM main.messages WHERE conversation_id=?",
        (scope.session_id,),
    )):
        check_cancel()
        if index >= MAX_ARCHIVE_ANCHOR_ROWS:
            raise RefreshError("The original conversation exceeds its bounded anchor budget.")
        proof = json.loads(row["provenance"])
        if proof.get("message_type") == 1:
            archive.setdefault(str(proof.get("server_id")), []).append(row)
    return known, archive


def _native_rows(connection, table, *, local_id=None, window=None):
    layout = list(connection.execute('PRAGMA table_info("' + table + '")'))
    columns = [row[1] for row in layout]
    if (local_id is not None or window is not None) and not any(
        row[1] == "local_id"
        and row[5]
        and (window is None or row[2].upper() == "INTEGER")
        for row in layout
    ):
        raise RefreshError("A native witness requires an indexed local primary key.")
    required = {
        "local_id",
        "server_id",
        "local_type",
        "create_time",
        "real_sender_id",
        "message_content",
    }
    marker = next(
        (name for name in columns if name.casefold() == "wcdb_ct_message_content"), None
    )
    if (
        not required.issubset(columns)
        or marker is None
        or _IDENTIFIER.fullmatch(marker) is None
    ):
        raise RefreshError("A native message table has an unsupported schema.")
    condition = " WHERE local_id=?" if local_id is not None else ""
    parameters = [local_id] if local_id is not None else []
    if window is not None and window.after_local_id is not None:
        condition = " WHERE local_id>?"
        parameters = [window.after_local_id]
    limit = " LIMIT ?" if window is not None else ""
    if window is not None:
        parameters.append(bank.MAX_PACKET_MESSAGES)
    return connection.execute(
        'SELECT local_id,server_id,local_type,create_time,real_sender_id,message_content,"'
        + marker
        + '" AS compression_marker FROM "'
        + table
        + '"'
        + condition
        + " ORDER BY local_id"
        + limit,
        parameters,
    )


def _server(value):
    value = str(value)
    if re.fullmatch(r"[1-9][0-9]{0,18}", value) is None or int(value) > 2**63 - 1:
        raise RefreshError("A native event has no supported server identifier.")
    return value


def _project_message(row, owner, kind, names, *, peer_id=None, text_limit=MAX_TEXT_BYTES):
    typ = row["local_type"] & 0xFFFFFFFF
    sender_row = names.get(row["real_sender_id"])
    sender = sender_row["user_name"] if sender_row else ""
    raw = row["message_content"]
    if not isinstance(raw, (str, bytes)):
        raise RefreshError("A native message body has an unsupported storage class.")
    message = {
        "local_id": row["local_id"],
        "local_type": row["local_type"],
        "server_id": _server(row["server_id"]),
        "create_time": row["create_time"],
        "sender_id": sender,
        "sender_label": sender,
        "direction": "sent" if sender == owner else "received",
        "real_sender_id": row["real_sender_id"],
        "content_sha256": hashlib.sha256(
            raw.encode() if isinstance(raw, str) else raw
        ).hexdigest(),
    }
    projection = None
    if typ in {1, 49, 10000, 10002}:
        decoded = decode_wcdb_text(raw, row["compression_marker"], limit=text_limit)
        if kind == "group" and sender and sender != owner:
            for prefix in (sender + ":\r\n", sender + ":\n"):
                if decoded.startswith(prefix):
                    decoded = decoded[len(prefix) :]
                    break
        projection = extract_wechat_text(typ, decoded)
        if (
            kind == "group"
            and peer_id
            and projection is not None
            and projection.get("system_message_subtype") == "sysmsgtemplate"
            and (membership := native_membership_fields(decoded, peer_id, typ)) is not None
        ):
            projection["system_membership"] = membership
    pat = is_native_pat_projection(projection, row["local_type"])
    if (
        typ == 49
        and projection is not None
        and projection.get("text_extraction_kind") == "system_text"
        and not pat
    ):
        raise RefreshError(
            "A native app system notice conflicts with its typed source."
        )
    if pat and not is_native_pat_projection(
        projection, row["local_type"], peer_id=peer_id
    ):
        raise RefreshError(
            "A native pat notice crosses its authenticated conversation."
        )
    if typ in {10000, 10002} or pat:
        # A pat's internal row sender is not its action participant. Keep the
        # original sender coordinate while presenting the typed notice neutrally.
        message.update(sender_id="", sender_label="System Message", direction="unknown")
    else:
        if not sender:
            raise RefreshError("A native message has no authenticated sender.")
        if sender_row:
            message["sender_proof"] = _name_proof(sender_row)
    if projection is not None:
        message.update(text=projection["text"], native_text=projection)
    else:
        if typ in {1, 10000, 10002}:
            raise RefreshError("The native text cannot be projected faithfully.")
        resource_kind = _KINDS.get(typ, "unknown")
        label = _LABELS[resource_kind]
        message.update(
            text=label,
            resource={
                "kind": resource_kind,
                "label": label,
                "native_time": row["create_time"],
            },
        )
        if typ == 49:
            subtype = str(row["local_type"] >> 32)
            message["resource"]["app_subtype"] = subtype
            metadata = _app_metadata(decoded, False, False)
            if metadata.get("app_subtype") == subtype:
                message["resource"].update({
                    key: metadata[key]
                    for key in (*_APP_LABEL_FIELDS, "app_metadata_version")
                    if key in metadata
                })
    return message


def _project_native_row(row, names, scope, *, text_limit=MAX_TEXT_BYTES):
    try:
        return _project_message(
            row, scope.account_id, scope.kind, names, peer_id=scope.peer_id, text_limit=text_limit,
        ), None
    except (WCDBDecodeError, WCDBBackendUnavailableError, RefreshError) as error:
        return None, projection_exclusion_reason(error)


def _native_comparison_signature(packet, message):
    projected = bank._row(packet, message)
    return _comparison_signature({
        "sender_id": projected[3],
        "direction": projected[5],
        "text": projected[6],
        "timestamp_value": projected[8],
        "provenance": projected[11],
    })


def _project_native_batch(rows, limits, names, scope, packet):
    result = []
    for row, limit in zip(rows, limits, strict=True):
        message, reason = _project_native_row(row, names, scope, text_limit=limit)
        signature = _native_comparison_signature(packet, message) if reason is None else None
        result.append((message, reason, signature))
    return result


def _projectable_rows(rows, check_cancel):
    for index, row in enumerate(rows):
        check_cancel()
        if index >= bank.MAX_PACKET_MESSAGES:
            raise RefreshError("This conversation exceeds its bounded native refresh budget.")
        yield row


def _projection_row_cost(row, names):
    server = row["server_id"]
    sender_id = row["real_sender_id"]
    if (
        not (type(server) is int or isinstance(server, str) and len(server) <= 19)
        or any(type(row[field]) is not int for field in ("local_id", "local_type", "create_time"))
        or sender_id is not None and type(sender_id) is not int
        or row["compression_marker"] is not None and type(row["compression_marker"]) is not int
    ):
        return None
    sender = names.get(sender_id)
    name_bytes = 0
    if sender is not None:
        name = sender["user_name"]
        if not isinstance(name, str) or type(sender["is_session"]) not in (int, type(None)):
            return None
        name_bytes = 4 * len(name)
    value = row["message_content"]
    size = 4 * len(value) if isinstance(value, (str, bytes)) else 0
    limit = MAX_TEXT_BYTES
    if (
        isinstance(value, bytes) and row["compression_marker"] in (2, 3, 4, 5)
        and row["local_type"] & 0xFFFFFFFF in {1, 49, 10000, 10002}
    ):
        try:
            declared = get_wcdb_frame_size(value)
        except (WCDBDecodeError, WCDBBackendUnavailableError):
            return None
        if declared is None or declared > MAX_TEXT_BYTES:
            return None
        # Enforce the declared size during decoding, not only during scheduling.
        limit = declared
        size = max(size, 4 * limit)
    return 1024 + size + name_bytes, limit


def _projection_batches(rows, names):
    batch, limits, cost = [], [], 0
    for row in rows:
        budget = _projection_row_cost(row, names)
        if batch and (budget is None or cost + budget[0] > _PROJECTION_BATCH_BYTES):
            yield batch, limits
            batch, limits, cost = [], [], 0
        if budget is None or budget[0] > _PROJECTION_BATCH_BYTES:
            # Unusual or oversized SQLite fields stay in the owning process.
            yield [row], None
            continue
        size, limit = budget
        batch.append(row)
        limits.append(limit)
        cost += size
        if len(batch) == _PROJECTION_BATCH_ROWS:
            yield batch, limits
            batch, limits, cost = [], [], 0
    if batch:
        yield batch, limits


@contextmanager
def native_projection_executor():
    """Scale this bounded CPU stage independently of the general worker pool."""
    resources = discover_compute_resources()
    cores = resources.physical_cpu_count or resources.logical_cpu_count
    workers = max(1, min(
        _MAX_NATIVE_PROJECTION_WORKERS, cores - 2,
        resources.max_in_flight_bytes // _PROJECTION_BATCH_BYTES,
    ))
    if workers == 1 or not hasattr(ProcessPoolExecutor, "terminate_workers"):
        yield None, 1
        return
    try:
        executor = ProcessPoolExecutor(max_workers=workers, mp_context=get_context("spawn"))
    except OSError:
        yield None, 1
        return
    try:
        yield executor, workers
    except BaseException:
        executor.terminate_workers()
        raise
    else:
        executor.shutdown()


def _ordered_native_projection(
    rows, names, scope, executor, check_cancel, *, packet, in_flight=_PROJECTION_IN_FLIGHT,
):
    """Decode bounded batches in a process pool and consume them in native order."""
    rows = _projectable_rows(rows, check_cancel)
    header = {key: value for key, value in packet.items() if key != "messages"}
    if executor is None or len(bank._json(header).encode()) > 64 * 1024:
        for row in rows:
            yield row, *_project_native_row(row, names, scope), None
        return

    batches = _projection_batches(rows, names)
    first, second = next(batches, None), next(batches, None)
    if second is None:
        for row in first[0] if first else ():
            yield row, *_project_native_row(row, names, scope), None
        return
    pending = deque()
    in_flight = max(1, min(in_flight, _MAX_NATIVE_PROJECTION_WORKERS))

    def submit(batch, limits):
        nonlocal executor
        if limits is None or executor is None or len(batch) < max(1, _PROJECTION_BATCH_ROWS // 8):
            return batch, None
        selected_names = {
            identifier: names[identifier] for row in batch
            if (identifier := row["real_sender_id"]) in names
        }
        try:
            future = executor.submit(
                _project_native_batch, [dict(row) for row in batch], limits, selected_names, scope, header,
            )
        except BrokenProcessPool:
            executor = None
            future = None
        return batch, future

    def consume(batch, future):
        nonlocal executor
        if future is not None:
            while not future.done():
                check_cancel()
                wait((future,), timeout=0.1)
            check_cancel()
            try:
                output = future.result()
            except BrokenProcessPool:
                executor = None
            else:
                for index, (row, result) in enumerate(zip(batch, output, strict=True)):
                    if index % 64 == 0:
                        check_cancel()
                    yield row, *result
                return
        for row in batch:
            check_cancel()
            yield row, *_project_native_row(row, names, scope), None

    for batch, limits in chain((first, second), batches):
        pending.append(submit(batch, limits))
        if len(pending) == in_flight:
            yield from consume(*pending.popleft())
    while pending:
        yield from consume(*pending.popleft())


def _archive_anchor(archived, row, message, peer, peer_row):
    proof = json.loads(archived["provenance"])
    try:
        stamp = datetime.fromisoformat(archived["timestamp_value"])
    except (ValueError, TypeError):
        return None
    if (
        row["local_type"] != 1
        or proof.get("message_type") != 1
        or str(proof.get("server_id")) != message["server_id"]
        or proof.get("unix_timestamp") != row["create_time"]
        or archived["text"] != message["text"]
        or archived["sender_id"] != message["sender_id"]
        or archived["direction"] != message["direction"]
        or stamp.tzinfo is None
        or stamp.timestamp() != row["create_time"]
    ):
        return None
    return {
        "archive_message_id": archived["id"],
        "peer_id": peer,
        "peer_proof": _name_proof(peer_row),
        "server_id": message["server_id"],
        "message_type": 1,
        "create_time": row["create_time"],
        "direction": message["direction"],
        "sender_id": message["sender_id"],
        "text_sha256": hashlib.sha256(message["text"].encode()).hexdigest(),
        "table": _table(peer),
        "local_id": row["local_id"],
        "content_sha256": message["content_sha256"],
        "real_sender_id": row["real_sender_id"],
        "sender_proof": message["sender_proof"],
    }


def _stored_witnesses(local_store_root, scope, source, directory, check_cancel):
    """Reuse at most 32 known coordinates; never search unrelated chat bodies."""
    path = bank.mac_recovery_bank_path(local_store_root)
    if not path.exists():
        return []
    identity = bank._identity(path)
    selected = {}
    with sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
    ) as stored:
        operations = 0

        def bounded_query():
            nonlocal operations
            check_cancel()
            operations += 1
            return int(operations > 100)

        stored.set_progress_handler(bounded_query, 1_000)
        # Proofs for other shards must not consume the stored-witness limit.
        matching_proof = (
            "CASE WHEN length(CAST(proof AS BLOB)) <= ? THEN "
            "CASE WHEN json_valid(proof) THEN "
            "json_extract(proof,'$.account_id')=? AND "
            "json_extract(proof,'$.account_binding.account_directory')=? AND "
            "json_extract(proof,'$.source.database_relative_path')=? END END"
        )
        source_parameters = (
            _MAX_STORED_WITNESS_PROOF_BYTES, scope.account_id, directory,
            source["database_relative_path"],
        )
        queries = (
            (
                "SELECT proof FROM conversation_proofs WHERE conversation_id=? AND "
                + matching_proof + " ORDER BY source_id LIMIT 16",
                (scope.session_id, *source_parameters),
            ),
            (
                "SELECT proof FROM sources WHERE account_id=? AND "
                + matching_proof + " ORDER BY id LIMIT 32",
                (scope.account_id, *source_parameters),
            ),
        )
        for query, parameters in queries:
            for (encoded,) in stored.execute(query, parameters):
                check_cancel()
                if (
                    not isinstance(encoded, str)
                    or len(encoded.encode()) > _MAX_STORED_WITNESS_PROOF_BYTES
                ):
                    raise RefreshError(
                        "A stored account proof exceeds its bounded metadata budget."
                    )
                packet = json.loads(encoded)
                binding = packet.get("account_binding")
                if (
                    packet.get("account_id") != scope.account_id
                    or not isinstance(binding, dict)
                    or binding.get("account_directory") != directory
                    or packet.get("source", {}).get("database_relative_path")
                    != source["database_relative_path"]
                ):
                    continue
                bank._account_binding_shape(packet)
                for anchor in binding["anchors"]:
                    selected.setdefault(
                        (
                            anchor["table"],
                            anchor["local_id"],
                            anchor["archive_message_id"],
                        ),
                        anchor,
                    )
                    if len(selected) >= 32:
                        break
                if len(selected) >= 32:
                    break
            if len(selected) >= 32:
                break
    if bank._identity(path) != identity:
        raise RefreshError("The stored account proof changed during verification.")
    return list(selected.values())


def _global_binding(
    local_store_root,
    native,
    history,
    scope,
    source,
    directory,
    names,
    peer_rows,
    tables,
    check_cancel,
    *,
    learned_witnesses=(),
):
    owner_row = _owner_row(names, scope.account_id)
    owner_proof = _name_proof(owner_row)
    selected = []
    coordinates = _stored_witnesses(
        local_store_root, scope, source, directory, check_cancel
    )
    if learned_witnesses:
        if (
            not isinstance(learned_witnesses, (tuple, list))
            or len(learned_witnesses) > MAX_LEARNED_WITNESSES
        ):
            raise RefreshError("The learned source witness pool exceeds its budget.")
        candidates = sorted(
            [*learned_witnesses, *coordinates],
            key=lambda value: value.get("direction") != "sent",
        )
        selected_coordinates = {}
        for coordinate in candidates:
            key = (
                coordinate["table"],
                coordinate["local_id"],
                coordinate["archive_message_id"],
            )
            selected_coordinates.setdefault(key, coordinate)
            if len(selected_coordinates) >= MAX_LEARNED_WITNESSES:
                break
        coordinates = list(selected_coordinates.values())
    for coordinate in coordinates:
        check_cancel()
        peer = coordinate["peer_id"]
        table = _table(peer)
        if table not in tables or peer not in peer_rows:
            continue
        archived = history.execute(
            "SELECT m.*,c.peer_id,c.partition_id,p.account_id,p.identity_status "
            "FROM messages m JOIN conversations c ON c.id=m.conversation_id "
            "JOIN partitions p ON p.id=c.partition_id WHERE m.id=?",
            (coordinate["archive_message_id"],),
        ).fetchone()
        if (
            archived is None
            or archived["peer_id"] != peer
            or archived["account_id"] != scope.account_id
        ):
            continue
        partition = (
            "ios-"
            + hashlib.md5(scope.account_id.encode(), usedforsecurity=False).hexdigest()
        )
        if (
            archived["partition_id"] != partition
            or archived["identity_status"] != "Verified archive ID"
        ):
            continue
        rows = _native_rows(native, table, local_id=coordinate["local_id"])
        row = rows.fetchone()
        if row is None or row["local_type"] != 1:
            continue
        try:
            message = _project_message(
                row,
                scope.account_id,
                "group" if peer.endswith(("@chatroom", "@im.chatroom")) else "direct",
                names,
            )
        except (ValueError, WCDBBackendUnavailableError):
            continue
        if message["server_id"] != coordinate["server_id"]:
            continue
        anchor = _archive_anchor(archived, row, message, peer, peer_rows[peer])
        if anchor and (
            message["direction"] != "sent"
            or row["real_sender_id"] == owner_row["rowid"]
            and message["sender_proof"] == owner_proof
        ):
            if not any(
                item["peer_id"] == peer and item["direction"] == message["direction"]
                for item in selected
            ):
                selected.append(anchor)
        if len({item["peer_id"] for item in selected}) >= 2 and any(
            item["direction"] == "sent" for item in selected
        ):
            sent = next(item for item in selected if item["direction"] == "sent")
            other = next(
                item for item in selected if item["peer_id"] != sent["peer_id"]
            )
            return {
                "schema_version": 2,
                "account_id": scope.account_id,
                "account_directory": directory,
                "owner_proof": owner_proof,
                "source": {field: source[field] for field in _SOURCE_BINDING_FIELDS},
                "anchors": [sent, other],
            }
    raise SourceAccountBindingUnavailable(
        "This database has insufficient independently revalidated account evidence."
    )


def prepare_packet(
    local_store_root,
    scope,
    database,
    *,
    check_cancel=None,
    context=None,
    on_excluded=None,
    on_counts=None,
    comparison=None,
    projection_executor=None,
    projection_in_flight=_PROJECTION_IN_FLIGHT,
    _window=None,
    _local_anchor_pool=None,
):
    """Return only new or changed target events; identical replay creates no new bank copy."""
    check_cancel = check_cancel if check_cancel is not None else lambda: None
    check_cancel()
    native = database.connection
    context = context or prepare_source_context(scope.account_id, database)
    context.require_current(database, scope.account_id)
    tables, names, peer_rows = context.tables, context.names, context.peer_rows
    table = _table(scope.peer_id)
    if table not in tables:
        return None, {"total": 0, "existing": 0, "excluded": 0}
    if scope.peer_id not in peer_rows:
        raise RefreshError(
            "The authenticated native peer does not match this conversation."
        )
    source = dict(database.source, table=table)
    packet = {
        "schema_version": 2,
        "receipt_id": "refresh-" + source["snapshot_id"],
        "account_id": scope.account_id,
        "session_id": scope.session_id,
        "peer_id": scope.peer_id,
        "conversation_kind": scope.kind,
        "peer_proof": _name_proof(peer_rows[scope.peer_id]),
        "source": source,
        "anchors": [],
        "messages": [],
    }
    history_path = wechat_archive_path(local_store_root)
    if comparison is not None:
        comparison.require_current(history_path, scope)
    history = sqlite3.connect(
        history_path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
    )
    history.row_factory = sqlite3.Row
    try:
        if comparison is None:
            known, archive = _archive_comparison_indexes(
                local_store_root, history_path, history, scope, check_cancel
            )
        else:
            known, archive = comparison.indexes(
                local_store_root, history_path, history, scope, check_cancel
            )
        local_anchors = {} if _local_anchor_pool is None else _local_anchor_pool
        counts = {"total": 0, "existing": 0, "excluded": 0}
        message_bytes = 0
        header_bytes = len(bank._json(packet).encode())
        header_reserve = min(64 * 1024, max(1024, bank.MAX_PACKET_BYTES // 16))
        def record_exclusion(row, reason):
            counts["excluded"] += 1
            if on_excluded is not None:
                on_excluded({
                    "reason": reason,
                    "account_id": scope.account_id,
                    "session_id": scope.session_id,
                    "source_id": source["source_id"],
                    "snapshot_id": source["snapshot_id"],
                    "database_relative_path": source["database_relative_path"],
                    "table": table,
                    "local_id": row["local_id"] if type(row["local_id"]) is int else None,
                    "message_type": row["local_type"] & 0xFFFFFFFF if type(row["local_type"]) is int else None,
                })

        for row, message, reason, signature in _ordered_native_projection(
            _native_rows(native, table, window=_window), names, scope,
            projection_executor if _window is None else None, check_cancel, packet=packet,
            in_flight=projection_in_flight,
        ):
            counts["total"] += 1
            previous_local_id = _window.last_local_id if _window is not None else None
            if _window is not None:
                _window.last_local_id = row["local_id"]
            if reason is not None:
                record_exclusion(row, reason)
                continue
            try:
                for original in archive.get(message["server_id"], ()):
                    anchor = _archive_anchor(
                        original, row, message, scope.peer_id, peer_rows[scope.peer_id]
                    )
                    if anchor:
                        if len(local_anchors) < 2 or _local_anchor_pool is None:
                            local_anchors[message["server_id"]] = anchor
                        context.remember_witness(anchor)
                if signature is None:
                    signature = _native_comparison_signature(packet, message)
                if signature in known.get(message["server_id"], ()):
                    counts["existing"] += 1
                    continue
                if _window is not None:
                    encoded_bytes = len(bank._json(message).encode()) + 1
                    if (
                        header_bytes + header_reserve + message_bytes + encoded_bytes
                        > bank.MAX_PACKET_BYTES
                    ):
                        if not packet["messages"]:
                            raise _PacketBudgetExceeded(
                                "A native projection exceeds its packet byte budget."
                            )
                        counts["total"] -= 1
                        _window.last_local_id = previous_local_id
                        _window.byte_limited = True
                        break
                    message_bytes += encoded_bytes
                # Validate every candidate with the established packet validator below.
                packet["messages"].append(message)
            except _PacketBudgetExceeded:
                raise
            except (WCDBDecodeError, WCDBBackendUnavailableError, RefreshError) as error:
                record_exclusion(row, projection_exclusion_reason(error))
        context.require_current(database, scope.account_id)
        if on_counts is not None:
            on_counts(dict(counts))
        if not packet["messages"]:
            if comparison is not None:
                comparison.require_current(history_path, scope)
            return None, counts
        if len(local_anchors) >= 2:
            packet["anchors"] = [
                {
                    field: anchor[field]
                    for field in ("server_id", "archive_message_id", "text_sha256")
                }
                for anchor in list(local_anchors.values())[:2]
            ]
        else:
            source["account_directory"] = database.account_directory
            binding_key = (
                str(history_path.resolve()),
                tuple(_file_identity(history_path).items()),
            )
            if binding_key not in context.bindings:
                context.bindings[binding_key] = _global_binding(
                    local_store_root,
                    native,
                    history,
                    scope,
                    source,
                    database.account_directory,
                    names,
                    peer_rows,
                    tables,
                    check_cancel,
                    learned_witnesses=tuple(context.learned_witnesses.values()),
                )
            packet["account_binding"] = deepcopy(context.bindings[binding_key])
        bank._validate_packet(packet, (scope.account_id,))
        bank._binding(history, packet)
        if comparison is not None:
            comparison.require_current(history_path, scope)
        return packet, counts
    finally:
        history.close()


def iter_prepare_packets(
    local_store_root,
    scope,
    database,
    *,
    context=None,
    check_cancel=None,
    on_excluded=None,
    on_counts=None,
    _native_count=None,
):
    """Split one pinned scope into bounded packets without weakening any proof.

    Every window examines at most the existing packet row limit. The full scope
    retains the existing one-million-row anchor boundary, and serialized packets
    retain their existing byte boundary. The original prepare_packet API remains
    a single-packet operation with its original physical-row limit.
    """
    check_cancel = check_cancel if check_cancel is not None else lambda: None
    context = context or prepare_source_context(scope.account_id, database)
    context.require_current(database, scope.account_id)
    table = _table(scope.peer_id)
    if table not in context.tables:
        return
    # Global refresh already counted the table on this read-only snapshot.
    total = _native_count
    if total is None:
        total = database.connection.execute(
            'SELECT COUNT(*) FROM "' + table + '"'
        ).fetchone()[0]
    if type(total) is not int or not 0 <= total <= MAX_ARCHIVE_ANCHOR_ROWS:
        raise RefreshError("The native scope exceeds its bounded global row budget.")
    history = wechat_archive_path(local_store_root)
    history_pin = bank._identity(history)
    scanned, after_local_id, anchors = 0, None, {}
    while scanned < total:
        check_cancel()
        if bank._identity(history) != history_pin:
            raise RefreshError("The archive changed between native packet windows.")
        context.require_current(database, scope.account_id)
        window = _NativeWindow(after_local_id=after_local_id)
        packet, counts = prepare_packet(
            local_store_root,
            scope,
            database,
            context=context,
            check_cancel=check_cancel,
            on_excluded=on_excluded,
            on_counts=on_counts,
            _window=window,
            _local_anchor_pool=anchors,
        )
        if (
            not counts["total"]
            or type(window.last_local_id) is not int
            or after_local_id is not None
            and window.last_local_id <= after_local_id
        ):
            raise RefreshError("Native packet windows did not make bounded progress.")
        scanned += counts["total"]
        after_local_id = window.last_local_id
        if scanned > total:
            raise RefreshError("The native scope changed between packet windows.")
        if packet is not None:
            yield packet
