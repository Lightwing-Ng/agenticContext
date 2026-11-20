"""Revalidate a sealed Mac bank against current canonical timeline semantics.

Code version: v1.6.0-codex.0
"""

from __future__ import annotations

import ctypes
import json
import hashlib
import os
import shutil
import sqlite3
import stat
from contextlib import closing
from copy import deepcopy
from pathlib import Path

from . import wechat_macos_recovery_bank as bank
from . import wechat_macos_reparse as reparse
from . import wechat_media_event_index as event_index
from .wechat_archive import WechatArchiveRepository, _file_identity, wechat_archive_path
from .wechat_export_merge import _clonefile
from .wechat_macos_recovery import attach_mac_recovery
from . import wechat_macos_recovery as legacy
from .wechat_media_event_index import _validated_conversations, attach_media_event_index
from .wechat_media_event_revalidation import _validate_membership
from .wechat_media_placeholders import attach_media_placeholders

VERSION = "v1.6.0-codex.0"
_INPUT = "media_events.sqlite3"
_LEGACY_INPUT = "macos_recovery.json"
_RESERVE_BYTES = 16 * 1024**2
_MAX_SCOPES = 100_000
_DEFAULT_MAX_BASE_MESSAGES = 5_000_000
_ARCHIVED_RESERVE_BYTES = 1024**3
_MAX_PRESERVATION_RECEIPT_BYTES = 128 * 1024
_HASH_RETRY_LIMIT = 3
_HASH_IDENTITY_FIELDS = ("dev", "ino", "size", "mtime_ns", "ctime_ns")
_HASH_STORAGE_FIELDS = (*_HASH_IDENTITY_FIELDS, "mode", "uid", "gid", "nlink", "flags")
_DOCUMENT_ID_FLAG = 0x40


def _readonly(path):
    return sqlite3.connect(
        Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True
    )


def _unchanged(history, inputs):
    if bank._inputs(history) != inputs:
        raise ValueError("A bank revalidation input changed during verification.")


def _ctime_drift(metadata, inputs, *, changed_input=_INPUT):
    if changed_input not in {_INPUT, _LEGACY_INPUT}:
        raise ValueError("Unsupported controlled bank revalidation input.")
    label = "media-event" if changed_input == _INPUT else "legacy Mac recovery"
    expected = json.loads(metadata["input_identities"])
    current = {
        key: value for key, value in inputs.items() if key != "macos_recovery.sqlite3"
    }
    if set(expected) != set(current) or any(
        expected[key] != current[key] for key in current if key != changed_input
    ):
        raise ValueError("Revalidation permits only the " + label + " ctime drift.")
    before, after = expected.get(changed_input), current.get(changed_input)
    fields = {"dev", "ino", "size", "mtime_ns", "ctime_ns"}
    if (
        not isinstance(before, dict)
        or not isinstance(after, dict)
        or set(before) != fields
        or set(after) != fields
        or any(type(value) is not int for value in (*before.values(), *after.values()))
        or before["ctime_ns"] == after["ctime_ns"]
        or any(before[key] != after[key] for key in fields - {"ctime_ns"})
    ):
        raise ValueError("Revalidation needs exactly one changed " + label + " ctime.")
    return expected, current


def _base_timeline(connection, history, root, *, changed_input=_INPUT):
    repository = WechatArchiveRepository(root)
    conversations, messages, status = repository._resource_timeline_tables(
        connection, _file_identity(history)
    )
    if status not in {"ready", "absent"}:
        raise ValueError(
            "The current resource timeline cannot be revalidated: " + status
        )
    media_path = str(history.with_name(_INPUT).resolve())
    with event_index._LOCK:
        for key in tuple(event_index._VALIDATED):
            if key[0] == media_path:
                event_index._VALIDATED.pop(key)
    for name, attach in (
        ("placeholders", attach_media_placeholders),
        ("media events", attach_media_event_index),
        ("legacy Mac recovery", attach_mac_recovery),
    ):
        conversations, messages, status = attach(
            connection, history, conversations, messages
        )
        if status not in {"ready", "absent"}:
            raise ValueError(
                "The current " + name + " timeline cannot be revalidated: " + status
            )
    # The ordinary media cache omits ctime. Reprove this changed input uncached.
    has_media = any(
        row[1] == "media_event_index"
        for row in connection.execute("PRAGMA database_list")
    )
    if not has_media and changed_input == _INPUT:
        raise ValueError(
            "The changed media-event source is not a validated local input."
        )
    if not has_media:
        return conversations, messages, 0
    verified = _validated_conversations(connection, _file_identity(history))
    events = _validate_membership(connection, verified)
    if [
        row[0] for row in connection.execute("PRAGMA media_event_index.integrity_check")
    ] != ["ok"]:
        raise ValueError(
            "The current media-event source failed full integrity checking."
        )
    return conversations, messages, events


def _retained_proofs(connection):
    source_count = 0
    for source in connection.execute("SELECT * FROM macos_recovery_bank.sources"):
        packet = json.loads(source["proof"])
        if (
            bank._sha(packet) != source["id"]
            or packet["account_id"] != source["account_id"]
            or packet["peer_id"] != source["peer_id"]
            or packet["source"]["source_id"] != source["source_id"]
            or packet["source"]["snapshot_id"] != source["snapshot_id"]
        ):
            raise ValueError(
                "A retained bank source lost its exact sealed coordinates."
            )
        bank._binding(connection, packet)
        source_count += 1
    for proof in connection.execute(
        "SELECT p.*,s.proof AS source_proof FROM macos_recovery_bank.conversation_proofs p "
        "LEFT JOIN macos_recovery_bank.sources s ON s.id=p.source_id"
    ):
        if proof["source_proof"] != proof["proof"]:
            raise ValueError(
                "A retained conversation proof differs from its sealed source."
            )
        packet = json.loads(proof["proof"])
        if packet["session_id"] != proof["conversation_id"]:
            raise ValueError(
                "A retained source proof crosses its original conversation."
            )
    if connection.execute(
        "SELECT 1 FROM macos_recovery_bank.sources s WHERE NOT EXISTS("
        "SELECT 1 FROM macos_recovery_bank.conversation_proofs p WHERE p.source_id=s.id) LIMIT 1"
    ).fetchone():
        raise ValueError("A retained source has no exact conversation proof.")
    return source_count


def _retained_events(connection):
    if connection.execute(
        "SELECT 1 FROM macos_recovery_bank.event_keys k WHERE NOT EXISTS("
        "SELECT 1 FROM macos_recovery_bank.messages m WHERE m.id=k.message_id) LIMIT 1"
    ).fetchone():
        raise ValueError("A retained event key has no canonical bank message.")
    count = 0
    for row in connection.execute(
        "SELECT m.*,k.account_id,k.peer_id,k.server_id,k.signature,c.account_key,"
        "c.peer_id AS conversation_peer FROM macos_recovery_bank.messages m "
        "LEFT JOIN macos_recovery_bank.event_keys k ON k.message_id=m.id "
        "LEFT JOIN macos_recovery_bank.conversations c ON c.id=m.conversation_id"
    ):
        if (
            row["signature"] is None
            or bank._event_signature(row) != row["signature"]
            or row["account_key"] != "account:" + row["account_id"]
            or row["conversation_peer"] != row["peer_id"]
            or str(json.loads(row["provenance"]).get("server_id")) != row["server_id"]
        ):
            raise ValueError(
                "A retained bank event differs from its canonical signature."
            )
        count += 1
    connection.execute(
        "CREATE TEMP TABLE revalidation_existing(id TEXT PRIMARY KEY,conversation_id TEXT,"
        "account_id TEXT,peer_id TEXT,server_id TEXT,signature TEXT,seen INTEGER NOT NULL) WITHOUT ROWID"
    )
    connection.execute(
        "CREATE INDEX temp.revalidation_existing_event ON revalidation_existing(account_id,peer_id,server_id)"
    )
    occurrences = 0
    for row in connection.execute(
        "SELECT o.*,s.account_id,s.peer_id,k.signature AS bank_signature,"
        "k.account_id AS bank_account,k.peer_id AS bank_peer,k.server_id AS bank_server,"
        "m.conversation_id AS bank_conversation FROM macos_recovery_bank.occurrences o "
        "LEFT JOIN macos_recovery_bank.sources s ON s.id=o.source_id "
        "LEFT JOIN macos_recovery_bank.event_keys k ON k.message_id=o.canonical_id "
        "LEFT JOIN macos_recovery_bank.messages m ON m.id=o.canonical_id"
    ):
        if row["account_id"] is None:
            raise ValueError("A retained occurrence has no sealed source owner.")
        if row["canonical_store"] == "existing_timeline":
            value = tuple(
                row[key]
                for key in (
                    "canonical_id",
                    "conversation_id",
                    "account_id",
                    "peer_id",
                    "server_id",
                    "signature",
                )
            )
            prior = connection.execute(
                "SELECT id,conversation_id,account_id,peer_id,server_id,signature "
                "FROM temp.revalidation_existing WHERE id=?",
                (value[0],),
            ).fetchone()
            if prior is not None and tuple(prior) != value:
                raise ValueError(
                    "Retained deduplication references disagree on one canonical target."
                )
            connection.execute(
                "INSERT OR IGNORE INTO temp.revalidation_existing VALUES(?,?,?,?,?,?,0)",
                value,
            )
        elif row["canonical_store"] in {"macos_bank", "macos_bank_shadow"}:
            if (
                (
                    row["signature"] != row["bank_signature"]
                    and not reparse.historical_occurrence(
                        connection, row, schema="macos_recovery_bank"
                    )
                )
                or row["account_id"] != row["bank_account"]
                or row["peer_id"] != row["bank_peer"]
                or row["server_id"] != row["bank_server"]
                or row["conversation_id"] != row["bank_conversation"]
            ):
                raise ValueError(
                    "A retained occurrence lost its exact bank canonical event."
                )
            if (
                row["canonical_store"] == "macos_bank_shadow"
                and not connection.execute(
                    "SELECT 1 FROM macos_recovery_bank.shadow_replacements WHERE original_id=?",
                    (row["canonical_id"],),
                ).fetchone()
            ):
                raise ValueError(
                    "A retained shadow occurrence has no original stub proof."
                )
        else:
            raise ValueError(
                "A retained occurrence uses an unsupported canonical store."
            )
        occurrences += 1
    return count, occurrences


def _base_message_budget(value):
    if type(value) is not int or value < 1:
        raise ValueError("A bank revalidation base-message budget must be a positive integer.")
    return value


def _compare_base(
    connection, conversations, messages, *,
    max_base_messages=_DEFAULT_MAX_BASE_MESSAGES, admission=None,
):
    max_base_messages = _base_message_budget(max_base_messages)
    if admission is not None and type(admission) is not dict:
        raise ValueError("Bank revalidation admission evidence must be an output dictionary.")
    audit = admission if admission is not None else {}
    audit.clear()
    audit.update(
        max_base_messages=max_base_messages, max_scope_messages=bank.MAX_SCOPE_MESSAGES,
        scope_counts={}, admitted_messages=0, streamed_messages=0,
        streamed_scope_counts={}, status="counting",
    )
    if connection.execute(
        f"SELECT 1 FROM {messages} m WHERE m.id IN (SELECT id FROM macos_recovery_bank.messages) "
        "AND EXISTS(SELECT 1 FROM macos_recovery_bank.messages b "
        "WHERE b.id=m.id AND b.conversation_id IS NOT m.conversation_id) LIMIT 1"
    ).fetchone():
        raise ValueError(
            "A current base message reuses a bank ID in another conversation."
        )
    if connection.execute(
        f"SELECT 1 FROM {messages} m WHERE m.id IN (SELECT id FROM temp.revalidation_existing) "
        "AND EXISTS(SELECT 1 FROM temp.revalidation_existing e "
        "WHERE e.id=m.id AND e.conversation_id IS NOT m.conversation_id) LIMIT 1"
    ).fetchone():
        raise ValueError("A retained deduplication target changed in another base conversation.")
    proofs = connection.execute(
        "SELECT DISTINCT p.conversation_id,s.account_id,s.peer_id "
        "FROM macos_recovery_bank.conversation_proofs p "
        "JOIN macos_recovery_bank.sources s ON s.id=p.source_id "
        "ORDER BY p.conversation_id,s.account_id,s.peer_id LIMIT ?",
        (_MAX_SCOPES + 1,),
    ).fetchall()
    if len(proofs) > _MAX_SCOPES:
        raise ValueError("Bank revalidation exceeds its bounded conversation budget.")
    scopes = []
    for proof in proofs:
        matching = connection.execute(
            f"SELECT id,account_key,peer_id FROM {conversations} WHERE id=? LIMIT 2",
            (proof["conversation_id"],),
        ).fetchall()
        if (
            len(matching) != 1 or type(proof["account_id"]) is not str
            or matching[0]["account_key"] != "account:" + proof["account_id"]
            or matching[0]["peer_id"] != proof["peer_id"]
            or proof["conversation_id"] in audit["scope_counts"]
        ):
            raise ValueError("A retained proof context is missing or has different source membership.")
        scope = matching[0]
        count = connection.execute(
            f"SELECT COUNT(*) FROM {messages} WHERE conversation_id=?", (scope["id"],),
        ).fetchone()[0]
        if count > bank.MAX_SCOPE_MESSAGES:
            raise ValueError("Bank revalidation exceeds its per-conversation base-message budget.")
        audit["scope_counts"][scope["id"]] = count
        audit["admitted_messages"] += count
        if audit["admitted_messages"] > max_base_messages:
            raise ValueError("Bank revalidation exceeds its admitted total base-message budget.")
        scopes.append(scope)
    audit.update(admitted_scopes=len(scopes), status="admitted")
    rows, shadows = 0, 0
    for scope in scopes:
        scope_rows = 0
        for row in connection.execute(
            f"SELECT * FROM {messages} WHERE conversation_id=?", (scope["id"],)
        ):
            scope_rows += 1
            rows += 1
            if (
                scope_rows > bank.MAX_SCOPE_MESSAGES
                or scope_rows > audit["scope_counts"][scope["id"]]
                or rows > audit["admitted_messages"] or rows > max_base_messages
            ):
                raise ValueError("The current base exceeds its exact admitted message counts.")
            proof = json.loads(row["provenance"])
            server = str(proof.get("server_id"))
            owner = scope["account_key"].removeprefix("account:")
            by_id = connection.execute(
                "SELECT k.*,s.original_row_sha256 FROM macos_recovery_bank.event_keys k "
                "LEFT JOIN macos_recovery_bank.shadow_replacements s ON s.original_id=k.message_id "
                "WHERE k.message_id=?",
                (row["id"],),
            ).fetchone()
            accepted_shadow = False
            if by_id is not None:
                accepted_shadow = (
                    by_id["original_row_sha256"] is not None
                    and bank._sha(dict(row)) == by_id["original_row_sha256"]
                    and by_id["account_id"] == owner
                    and by_id["peer_id"] == scope["peer_id"]
                    and by_id["server_id"] == server
                )
                if not accepted_shadow:
                    raise ValueError(
                        "A current base message overlaps a retained bank message ID."
                    )
                shadows += 1
            # Staging compares server events within one canonical conversation.
            by_event = connection.execute(
                "SELECT k.message_id FROM macos_recovery_bank.event_keys k "
                "JOIN macos_recovery_bank.messages m ON m.id=k.message_id "
                "WHERE k.account_id=? AND k.peer_id=? AND k.server_id=? AND m.conversation_id=?",
                (owner, scope["peer_id"], server, scope["id"]),
            ).fetchone()
            if by_event is not None and not (
                accepted_shadow and by_event[0] == row["id"]
            ):
                raise ValueError(
                    "A current base event overlaps a retained bank server event."
                )
            expected_event = connection.execute(
                "SELECT signature FROM temp.revalidation_existing "
                "WHERE account_id=? AND peer_id=? AND server_id=? AND conversation_id=?",
                (owner, scope["peer_id"], server, scope["id"]),
            ).fetchall()
            if expected_event and any(
                item[0] != bank._event_signature(row) for item in expected_event
            ):
                raise ValueError(
                    "A current base variant conflicts with a retained deduplication event."
                )
            target = connection.execute(
                "SELECT * FROM temp.revalidation_existing WHERE id=?", (row["id"],)
            ).fetchone()
            if target is not None:
                if (
                    target["seen"]
                    or target["conversation_id"] != row["conversation_id"]
                    or target["account_id"] != owner
                    or target["peer_id"] != scope["peer_id"]
                    or target["server_id"] != server
                    or target["signature"] != bank._event_signature(row)
                ):
                    raise ValueError(
                        "A retained deduplication target changed in the current base."
                    )
                connection.execute(
                    "UPDATE temp.revalidation_existing SET seen=1 WHERE id=?",
                    (row["id"],),
                )
        audit["streamed_scope_counts"][scope["id"]] = scope_rows
        audit["streamed_messages"] = rows
        if scope_rows != audit["scope_counts"][scope["id"]]:
            raise ValueError("A current base conversation lost rows after exact admission.")
    if connection.execute(
        "SELECT 1 FROM temp.revalidation_existing WHERE seen=0 LIMIT 1"
    ).fetchone():
        raise ValueError(
            "A retained deduplication target is missing from the current base."
        )
    if rows != audit["admitted_messages"] or audit["streamed_scope_counts"] != audit["scope_counts"]:
        raise ValueError("The current base differs from its complete message admission.")
    audit["status"] = "verified"
    return rows, shadows


def validate_mac_recovery_bank_revalidation(local_store_root):
    """Return fresh semantic evidence without creating a candidate or changing files."""
    history = wechat_archive_path(local_store_root)
    original = bank.mac_recovery_bank_path(local_store_root)
    inputs = bank._inputs(history)
    with closing(_readonly(original)) as sealed:
        metadata = dict(sealed.execute("SELECT key,value FROM archive_info"))
        if metadata.get("schema_version") not in {"3", "4"}:
            raise ValueError(
                "Semantic bank revalidation requires the complete schema-3 bank."
            )
        expected, current = _ctime_drift(metadata, inputs)
        bank._validate_bank(
            sealed, history, {**expected, original.name: inputs[original.name]}
        )
        if [row[0] for row in sealed.execute("PRAGMA integrity_check")] != ["ok"]:
            raise ValueError("The retained bank failed full SQLite integrity checking.")
        accounts = [
            row[0]
            for row in sealed.execute(
                "SELECT DISTINCT account_id FROM sources ORDER BY account_id"
            )
        ]
        if json.loads(metadata["account_ids"]) != accounts:
            raise ValueError(
                "The retained bank account manifest differs from its source owners."
            )
    original_sha = bank._file_sha(original)
    media_sha = bank._file_sha(history.with_name(_INPUT))
    _unchanged(history, inputs)
    with closing(_readonly(history)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA temp_store=MEMORY")
        conversations, messages, media_events = _base_timeline(
            connection, history, local_store_root
        )
        connection.execute(
            "ATTACH DATABASE ? AS macos_recovery_bank",
            (original.resolve().as_uri() + "?mode=ro&immutable=1",),
        )
        sources = _retained_proofs(connection)
        count, occurrences = _retained_events(connection)
        connection.execute(
            "CREATE TEMP TABLE macos_recovery_bank_scopes(id TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        connection.execute(
            "INSERT INTO temp.macos_recovery_bank_scopes SELECT id FROM macos_recovery_bank.conversations"
        )
        bank._validate_shadow_sources(connection, history)
        base_rows, shadows = _compare_base(connection, conversations, messages)
        existing = connection.execute(
            "SELECT COUNT(*) FROM temp.revalidation_existing"
        ).fetchone()[0]
        gaps = connection.execute(
            "SELECT COUNT(*) FROM macos_recovery_bank.gaps"
        ).fetchone()[0]
    _unchanged(history, inputs)
    return {
        "version": VERSION,
        "mode": "fresh_current_timeline_semantic_revalidation",
        "historical_byte_equivalence": "not_established",
        "changed_input": _INPUT,
        "changed_fields": ["ctime_ns"],
        "previous_input_identity": expected[_INPUT],
        "current_input_identity": current[_INPUT],
        "current_input_sha256": media_sha,
        "source_bank_sha256": original_sha,
        "source_payload_sha256": metadata["payload_sha256"],
        "source_metadata": metadata,
        "current_input_identities": current,
        "input_identities": inputs,
        "account_ids": accounts,
        "sources": sources,
        "messages": count,
        "occurrences": occurrences,
        "existing_timeline_targets": existing,
        "current_base_rows_checked": base_rows,
        "validated_shadow_overlaps": shadows,
        "current_media_events": media_events,
        "historical_gaps_preserved": gaps,
        "published": False,
    }


def stage_mac_recovery_bank_revalidation(
    local_store_root, candidate_path, *, preserved_path
):
    """Preserve the sealed original and stage only a freshly revalidated input pin.

    The returned receipt can be passed to the unmodified canonical publisher.
    This operation makes no claim that the changed input has its historical bytes.
    """
    history = wechat_archive_path(local_store_root)
    original = bank.mac_recovery_bank_path(local_store_root)
    candidate, preserved = (
        Path(candidate_path).absolute(),
        Path(preserved_path).absolute(),
    )
    protected = {history.with_name(name).resolve() for name in bank._SOURCE_INPUTS}
    if candidate == preserved or any(
        path.resolve() in protected for path in (candidate, preserved)
    ):
        raise ValueError(
            "Revalidation outputs must be distinct from every canonical source."
        )
    for path in (candidate, preserved):
        if path.exists() or path.is_symlink():
            raise FileExistsError("Preserve existing revalidation outputs unchanged.")
        if not path.parent.is_dir() or path.parent.is_symlink():
            raise ValueError("Revalidation outputs need existing regular directories.")
        if path.parent.stat().st_dev != original.stat().st_dev:
            raise ValueError("Revalidation requires same-volume APFS output paths.")
    required = 2 * original.stat().st_size + _RESERVE_BYTES
    if (
        min(shutil.disk_usage(path.parent).free for path in (candidate, preserved))
        < required
    ):
        raise ValueError(
            "Insufficient space for both preserved and candidate logical bank sizes."
        )
    evidence = validate_mac_recovery_bank_revalidation(local_store_root)
    inputs = evidence["input_identities"]
    _unchanged(history, inputs)
    _clonefile(original, preserved)
    preserved.chmod(0o400)
    if bank._file_sha(preserved) != evidence["source_bank_sha256"]:
        raise ValueError(
            "The preserved bank does not match the verified original bytes."
        )
    _unchanged(history, inputs)
    _clonefile(original, candidate)
    candidate.chmod(0o600)
    if bank._file_sha(candidate) != evidence["source_bank_sha256"]:
        raise ValueError("The candidate is not an exact clone of the sealed bank.")
    with closing(sqlite3.connect(candidate)) as output:
        output.execute("PRAGMA journal_mode=DELETE")
        output.execute("PRAGMA synchronous=FULL")
        output.execute(
            "UPDATE archive_info SET value=? WHERE key='input_identities'",
            (bank._json(evidence["current_input_identities"]),),
        )
        output.commit()
        expected_metadata = {
            **evidence["source_metadata"],
            "input_identities": bank._json(evidence["current_input_identities"]),
        }
        if (
            dict(output.execute("SELECT key,value FROM archive_info"))
            != expected_metadata
        ):
            raise ValueError(
                "Revalidation changed metadata beyond its permitted input pin."
            )
        bank._validate_bank(output, history, inputs)
        if [row[0] for row in output.execute("PRAGMA integrity_check")] != ["ok"]:
            raise ValueError(
                "The revalidated candidate failed full SQLite integrity checking."
            )
    for path in (candidate, preserved):
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
    _unchanged(history, inputs)
    if bank._file_sha(preserved) != evidence["source_bank_sha256"]:
        raise ValueError("The preserved bank changed during candidate staging.")
    return {
        "candidate_path": str(candidate),
        "candidate_sha256": bank._file_sha(candidate),
        "candidate_identity": bank._identity(candidate),
        "input_identities": inputs,
        "account_ids": evidence["account_ids"],
        "preserved_path": str(preserved),
        "preserved_sha256": evidence["source_bank_sha256"],
        "preserved_identity": bank._identity(preserved),
        "preserved_owner_read_only": stat.S_IMODE(preserved.stat().st_mode) == 0o400,
        "added": 0,
        "upgraded": 0,
        "total": evidence["messages"],
        "revalidation": evidence,
    }


def _private_directory(path):
    for parent in (path, path.parent):
        value = parent.lstat()
        if (
            not stat.S_ISDIR(value.st_mode) or not hasattr(os, "getuid")
            or value.st_uid != os.getuid() or stat.S_IMODE(value.st_mode) & 0o077
        ):
            raise ValueError("Controlled revalidation requires private owned output directories.")


def _controlled_file(path, history, *, modes):
    identity = bank._identity(path)
    value = path.lstat()
    if (
        not hasattr(os, "getuid") or value.st_uid != os.getuid()
        or value.st_nlink != 1 or stat.S_IMODE(value.st_mode) not in modes
        or value.st_dev != history.stat().st_dev
        or any(os.path.lexists(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal"))
    ):
        raise ValueError("Controlled revalidation requires an unaliased resident private file.")
    _private_directory(path.parent)
    return identity


def _guarded_inputs(history, inputs, guard):
    bank._require_publication_guard(guard, history)
    _unchanged(history, inputs)


def _hash_storage(value):
    return {key: getattr(value, "st_" + key, 0) for key in _HASH_STORAGE_FIELDS}


def _native_fchflags(descriptor, flags):
    """Set flags on the already anchored new clone, without a pathname fallback."""
    if type(descriptor) is not int or descriptor < 0 or type(flags) is not int or not 0 <= flags <= 0xFFFFFFFF:
        raise ValueError("New clone tracking preparation requires valid descriptor flags.")
    try:
        setter = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True).fchflags
        setter.argtypes = (ctypes.c_int, ctypes.c_uint32)
        setter.restype = ctypes.c_int
        result = setter(descriptor, flags)
    except (AttributeError, OSError):
        raise ValueError("Native descriptor tracking preparation is unavailable.") from None
    if result != 0:
        raise ValueError("Native descriptor tracking preparation was refused.")


def _prepare_new_clone_tracking(
    path, source, history, inputs, guard, *, mode, audit, protected_paths=(),
):
    """Prepare only a new clone's document-ID bit before any hash baseline.

    macOS documents UF_TRACKED as an owner-settable document-ID flag. Setting
    that bit on this independently owned clone is a metadata preparation step;
    the later complete byte hash and all strict hashing storage checks remain
    required. No existing source or preservation artifact is changed here.
    """
    if mode not in {0o400, 0o600} or type(audit) is not list:
        raise ValueError("New clone tracking preparation requires private output evidence.")
    if not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("New clone tracking preparation requires a no-follow descriptor.")
    path, source, history = (Path(value).absolute() for value in (path, source, history))
    protected_paths = tuple(Path(value).absolute() for value in protected_paths)
    protected = (
        {history.with_name(name) for name in bank._SOURCE_INPUTS}
        | {source}
        | set(protected_paths)
    )
    if path.resolve() in {value.resolve() for value in protected}:
        raise ValueError("Tracking preparation may change only a new noncanonical clone.")
    _guarded_inputs(history, inputs, guard)
    source_before = _hash_storage(source.lstat())
    _controlled_file(source, history, modes={0o600, 0o644})
    bank._no_baseline_copy_handles(source)
    _controlled_file(path, history, modes={mode})
    bank._no_baseline_copy_handles(path)
    before = _hash_storage(path.lstat())
    if (
        not stat.S_ISREG(before["mode"]) or before["uid"] != os.getuid()
        or before["nlink"] != 1 or stat.S_IMODE(before["mode"]) != mode
        or before["dev"] != source_before["dev"] or before["flags"] & 0x40000000
    ):
        raise ValueError("Tracking preparation requires an owned resident new clone.")
    protected_inodes = {
        (value["dev"], value["ino"]) for value in inputs.values()
        if isinstance(value, dict) and "dev" in value and "ino" in value
    } | {(source_before["dev"], source_before["ino"])}
    for protected_path in protected_paths:
        value = Path(protected_path).lstat()
        protected_inodes.add((value.st_dev, value.st_ino))
    if (before["dev"], before["ino"]) in protected_inodes:
        raise ValueError("Tracking preparation cannot change a protected source inode.")
    states = [before]

    def check_preparation():
        for value in states:
            if (
                any(value[key] != before[key] for key in before if key not in {"ctime_ns", "flags"})
                or (value["flags"] & ~_DOCUMENT_ID_FLAG) != (before["flags"] & ~_DOCUMENT_ID_FLAG)
                or (value["flags"] & before["flags"] & _DOCUMENT_ID_FLAG) != (before["flags"] & _DOCUMENT_ID_FLAG)
            ):
                raise ValueError("A new clone changed outside its tracking metadata preparation.")

    native_called = False
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
        try:
            states.append(_hash_storage(os.fstat(descriptor)))
            states.append(_hash_storage(path.lstat()))
            check_preparation()
            _controlled_file(path, history, modes={mode})
            _guarded_inputs(history, inputs, guard)
            if _hash_storage(source.lstat()) != source_before:
                raise ValueError("The sealed source changed during new clone preparation.")
            requested = states[-2]["flags"] | _DOCUMENT_ID_FLAG
            if states[-2]["flags"] != requested:
                _native_fchflags(descriptor, requested)
                native_called = True
            states.append(_hash_storage(os.fstat(descriptor)))
            states.append(_hash_storage(path.lstat()))
            check_preparation()
            if any(value["flags"] != (before["flags"] | _DOCUMENT_ID_FLAG) for value in states[-2:]):
                raise ValueError("Native clone preparation did not establish its exact tracking flag.")
        finally:
            os.close(descriptor)
        bank._no_baseline_copy_handles(path)
        _controlled_file(path, history, modes={mode})
        states.append(_hash_storage(path.lstat()))
        check_preparation()
        if states[-1]["flags"] != (before["flags"] | _DOCUMENT_ID_FLAG):
            raise ValueError("The new clone tracking flag changed after preparation.")
        if _hash_storage(source.lstat()) != source_before:
            raise ValueError("The sealed source changed during new clone preparation.")
        bank._no_baseline_copy_handles(source)
    finally:
        _guarded_inputs(history, inputs, guard)
    result = {
        "phase": "new_clone_tracking_preparation",
        "basis": "new_owned_clone_document_id_flag_preparation_v1",
        "path": str(path), "source_path": str(source),
        "flag_mask": _DOCUMENT_ID_FLAG,
        "flags_before": before["flags"], "flags_after": states[-1]["flags"],
        "before_identity": before, "after_identity": states[-1],
        "native_called": native_called,
    }
    audit.append(result)
    return result


def _closed_sha(
    path, history, inputs, guard, *, modes, expected_sha256=None,
    audit=None, phase="closed_artifact_hash",
):
    """Retry only a ctime change whose entire read matches independent known bytes."""
    if expected_sha256 is not None:
        legacy._digest(expected_sha256)
    if audit is not None and type(audit) is not list:
        raise ValueError("Closed artifact hash evidence must be an output list.")
    baseline = None
    attempts = _HASH_RETRY_LIMIT if expected_sha256 is not None else 1
    for attempt in range(attempts):
        _guarded_inputs(history, inputs, guard)
        states = [_hash_storage(path.lstat())]
        if baseline is None:
            baseline = states[0]

        def check_storage():
            for state in states:
                if any(state[key] != value for key, value in baseline.items() if key != "ctime_ns"):
                    raise ValueError("A controlled revalidation artifact changed storage fields during hashing.")
                if expected_sha256 is None and state != states[0]:
                    raise ValueError("A controlled revalidation artifact changed during hashing.")

        digest = hashlib.sha256()
        try:
            _controlled_file(path, history, modes=modes)
            bank._no_baseline_copy_handles(path)
            states.append(_hash_storage(path.lstat()))
            check_storage()
            descriptor = os.open(
                path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            )
            with os.fdopen(descriptor, "rb") as stream:
                states.append(_hash_storage(os.fstat(stream.fileno())))
                check_storage()
                while block := stream.read(4 * 1024**2):
                    digest.update(block)
                states.append(_hash_storage(os.fstat(stream.fileno())))
                check_storage()
            bank._no_baseline_copy_handles(path)
            _controlled_file(path, history, modes=modes)
            states.append(_hash_storage(path.lstat()))
            check_storage()
        finally:
            _guarded_inputs(history, inputs, guard)
        _controlled_file(path, history, modes=modes)
        states.append(_hash_storage(path.lstat()))
        check_storage()
        actual_sha = digest.hexdigest()
        if expected_sha256 is not None and actual_sha != expected_sha256:
            raise ValueError("The complete controlled artifact hash differs from its independent SHA.")
        identity = {key: states[-1][key] for key in _HASH_IDENTITY_FIELDS}
        if all(state == states[0] for state in states):
            return actual_sha, identity
        if audit is not None:
            audit.append({
                "phase": phase, "basis": "candidate_ctime_only_exact_file_sha256_v1",
                "before_identity": {key: states[0][key] for key in _HASH_IDENTITY_FIELDS},
                "after_identity": identity, "file_sha256": actual_sha,
            })
        if attempt + 1 == attempts:
            raise ValueError("The known-SHA artifact did not stabilize within three complete hash passes.")
    raise ValueError("The controlled artifact hash did not complete.")


def _retained_semantics(
    root, history, source, *, changed_input,
    max_base_messages=_DEFAULT_MAX_BASE_MESSAGES,
):
    current_inputs = bank._inputs(history)
    admission = {}
    with closing(_readonly(history)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA temp_store=MEMORY")
        conversations, messages, media_events = _base_timeline(
            connection, history, root, changed_input=changed_input,
        )
        connection.execute(
            "ATTACH DATABASE ? AS macos_recovery_bank",
            (source.resolve().as_uri() + "?mode=ro&immutable=1",),
        )
        sources = _retained_proofs(connection)
        count, occurrences = _retained_events(connection)
        connection.execute(
            "CREATE TEMP TABLE macos_recovery_bank_scopes(id TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        connection.execute(
            "INSERT INTO temp.macos_recovery_bank_scopes SELECT id FROM macos_recovery_bank.conversations"
        )
        bank._validate_shadow_sources(connection, history)
        rows, shadows = _compare_base(
            connection, conversations, messages,
            max_base_messages=max_base_messages, admission=admission,
        )
        result = {
            "sources": sources, "messages": count, "occurrences": occurrences,
            "existing_timeline_targets": connection.execute(
                "SELECT COUNT(*) FROM temp.revalidation_existing"
            ).fetchone()[0],
            "current_base_rows_checked": rows, "validated_shadow_overlaps": shadows,
            "current_media_events": media_events,
            "historical_gaps_preserved": connection.execute(
                "SELECT COUNT(*) FROM macos_recovery_bank.gaps"
            ).fetchone()[0],
            "base_message_admission": admission,
        }
    _unchanged(history, current_inputs)
    admission["input_identities"] = current_inputs
    return result


def validate_sealed_mac_recovery_bank_revalidation(
    local_store_root, source_path, *, expected_source_sha256,
    expected_payload_sha256, publication_guard,
    max_base_messages=_DEFAULT_MAX_BASE_MESSAGES,
):
    """Reprove one private sealed bank against current legacy-overlay semantics.

    Only the legacy JSON input's ctime may differ. Its previous raw bytes are
    not inferred from the native source receipts or its current self-seal.
    """
    history = wechat_archive_path(local_store_root)
    bank._require_publication_guard(publication_guard, history)
    max_base_messages = _base_message_budget(max_base_messages)
    legacy._digest(expected_source_sha256)
    legacy._digest(expected_payload_sha256)
    source = Path(source_path).expanduser().absolute()
    protected = {history.with_name(name).resolve() for name in bank._SOURCE_INPUTS}
    if source.resolve() in protected:
        raise ValueError("Controlled revalidation requires a separate sealed artifact.")
    inputs = bank._inputs(history)
    source_rebindings = []
    digest, initial_identity = _closed_sha(
        source, history, inputs, publication_guard, modes={0o600, 0o644},
        expected_sha256=expected_source_sha256, audit=source_rebindings,
        phase="sealed_revalidation_source_hash",
    )
    if digest != expected_source_sha256:
        raise ValueError("The sealed artifact differs from its expected full-file SHA.")
    with closing(_readonly(source)) as sealed:
        metadata = dict(sealed.execute("SELECT key,value FROM archive_info"))
        if metadata.get("schema_version") not in {"3", "4"}:
            raise ValueError("Controlled revalidation requires a complete schema-3 or schema-4 bank.")
        if metadata.get("payload_sha256") != expected_payload_sha256:
            raise ValueError("The sealed artifact differs from its expected payload.")
        expected, current = _ctime_drift(metadata, inputs, changed_input=_LEGACY_INPUT)
        bank._validate_bank(sealed, history, {**expected, "macos_recovery.sqlite3": inputs["macos_recovery.sqlite3"]})
        if [row[0] for row in sealed.execute("PRAGMA integrity_check")] != ["ok"]:
            raise ValueError("The sealed artifact failed full SQLite integrity checking.")
        schema = bank._baseline_schema(sealed)
        accounts = [row[0] for row in sealed.execute("SELECT DISTINCT account_id FROM sources ORDER BY account_id")]
        if json.loads(metadata["account_ids"]) != accounts:
            raise ValueError("The sealed account manifest differs from its retained owners.")
    packet, legacy_sha = legacy._read(history.with_name(_LEGACY_INPUT))
    legacy._validate_packet(packet)
    with closing(_readonly(history)) as connection:
        connection.row_factory = sqlite3.Row
        legacy._validate_binding(connection, packet)
    _guarded_inputs(history, inputs, publication_guard)
    counts = _retained_semantics(
        local_store_root, history, source, changed_input=_LEGACY_INPUT,
        max_base_messages=max_base_messages,
    )
    if legacy._read(history.with_name(_LEGACY_INPUT))[1] != legacy_sha:
        raise ValueError("The current legacy input changed during semantic revalidation.")
    source_identity = bank._candidate_storage_rebind(
        source, history, initial_identity, expected_source_sha256,
        history=history, guard=publication_guard, inputs=inputs,
        phase="sealed_revalidation_after_validation", audit=source_rebindings,
    )
    _controlled_file(source, history, modes={0o600, 0o644})
    bank._no_baseline_copy_handles(source)
    _guarded_inputs(history, inputs, publication_guard)
    return {
        "version": VERSION, "mode": "sealed_artifact_current_legacy_semantic_revalidation",
        "historical_byte_equivalence": "not_established",
        "changed_input": _LEGACY_INPUT, "changed_fields": ["ctime_ns"],
        "previous_input_identity": expected[_LEGACY_INPUT],
        "current_input_identity": current[_LEGACY_INPUT],
        "current_input_sha256": legacy_sha,
        "source_bank_path": str(source), "source_bank_sha256": digest,
        "source_payload_sha256": expected_payload_sha256,
        "source_initial_identity": initial_identity, "source_bank_identity": source_identity,
        "source_storage_rebindings": source_rebindings,
        "source_metadata": metadata, "source_schema": schema,
        "current_input_identities": current, "input_identities": inputs,
        "account_ids": accounts, "current_legacy_messages": len(packet["messages"]),
        **counts, "published": False,
    }


def _update_controlled_candidate(candidate, history, evidence, publication_guard, candidate_before):
    """Apply the same single-field transaction and closed-reader proof for either preservation."""
    inputs = evidence["input_identities"]
    with closing(sqlite3.connect(candidate.resolve().as_uri() + "?mode=rw", uri=True, timeout=0)) as output:
        if output.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
            raise ValueError("Controlled revalidation requires an ordinary rollback journal.")
        output.execute("PRAGMA synchronous=FULL")
        output.execute("PRAGMA locking_mode=EXCLUSIVE")
        output.execute("BEGIN EXCLUSIVE")
        try:
            if bank._identity(candidate) != candidate_before:
                raise ValueError("The controlled candidate changed before its write lease.")
            output.execute(
                "UPDATE archive_info SET value=? WHERE key='input_identities'",
                (bank._json(evidence["current_input_identities"]),),
            )
            expected_metadata = {
                **evidence["source_metadata"],
                "input_identities": bank._json(evidence["current_input_identities"]),
            }
            if (
                dict(output.execute("SELECT key,value FROM archive_info")) != expected_metadata
                or bank._baseline_schema(output) != evidence["source_schema"]
            ):
                raise ValueError("Controlled revalidation changed more than the permitted input pin.")
            _guarded_inputs(history, inputs, publication_guard)
            output.commit()
        except Exception:
            output.rollback()
            raise
    with closing(_readonly(candidate)) as verified:
        _guarded_inputs(history, inputs, publication_guard)
        if bank._validate_bank(verified, history) != expected_metadata:
            raise ValueError("The controlled candidate lost its exact metadata after staging.")
        if bank._baseline_schema(verified) != evidence["source_schema"]:
            raise ValueError("The controlled candidate changed its closed schema.")
        if [row[0] for row in verified.execute("PRAGMA integrity_check")] != ["ok"]:
            raise ValueError("The controlled candidate failed full SQLite integrity checking.")


def stage_sealed_mac_recovery_bank_revalidation(
    local_store_root, source_path, candidate_path, *, preserved_path,
    expected_source_sha256, expected_payload_sha256, publication_guard,
):
    """Preserve two new APFS clones and update only a proven legacy input pin.

    The original failed artifact is never opened for writing or chmodded. The
    existing two-output logical capacity contract remains conservative even
    though clonefile shares unchanged physical extents.
    """
    history = wechat_archive_path(local_store_root)
    bank._require_publication_guard(publication_guard, history)
    source, candidate, preserved = (
        Path(path).expanduser().absolute() for path in (source_path, candidate_path, preserved_path)
    )
    protected = {history.with_name(name).resolve() for name in bank._SOURCE_INPUTS} | {source.resolve()}
    if candidate.resolve() == preserved.resolve() or any(path.resolve() in protected for path in (candidate, preserved)):
        raise ValueError("Controlled outputs must be distinct new noncanonical paths.")
    source_identity = _controlled_file(source, history, modes={0o600, 0o644})
    bank._no_baseline_copy_handles(source)
    for path in (candidate, preserved):
        if os.path.lexists(path):
            raise FileExistsError("Preserve existing controlled revalidation outputs unchanged.")
        _private_directory(path.parent)
        if path.parent.stat().st_dev != source_identity["dev"]:
            raise ValueError("Controlled revalidation requires same-volume APFS output paths.")
    required = 2 * source_identity["size"] + _RESERVE_BYTES
    if min(shutil.disk_usage(path.parent).free for path in (candidate, preserved)) < required:
        raise ValueError("Insufficient space for both preserved and candidate logical bank sizes.")
    evidence = validate_sealed_mac_recovery_bank_revalidation(
        local_store_root, source, expected_source_sha256=expected_source_sha256,
        expected_payload_sha256=expected_payload_sha256, publication_guard=publication_guard,
    )
    inputs = evidence["input_identities"]
    _guarded_inputs(history, inputs, publication_guard)
    if min(shutil.disk_usage(path.parent).free for path in (candidate, preserved)) < required:
        raise ValueError("Insufficient space for both preserved and candidate logical bank sizes.")
    clone_identities, clone_rebindings = {}, {}
    clone_preparations = []
    for path, mode in ((preserved, 0o400), (candidate, 0o600)):
        _clonefile(source, path)
        path.chmod(mode)
        _prepare_new_clone_tracking(
            path, source, history, inputs, publication_guard,
            mode=mode, audit=clone_preparations,
        )
        clone_rebindings[path] = []
        clone_sha, clone_identity = _closed_sha(
            path, history, inputs, publication_guard, modes={mode},
            expected_sha256=expected_source_sha256, audit=clone_rebindings[path],
            phase="sealed_revalidation_clone_hash",
        )
        if clone_sha != expected_source_sha256:
            raise ValueError("A controlled APFS clone differs from its sealed source bytes.")
        clone_identities[path] = clone_identity
    _guarded_inputs(history, inputs, publication_guard)
    source_rebindings = evidence["source_storage_rebindings"]
    source_identity = bank._candidate_storage_rebind(
        source, history, evidence["source_bank_identity"], expected_source_sha256,
        history=history, guard=publication_guard, inputs=inputs,
        phase="sealed_revalidation_before_update", audit=source_rebindings,
    )
    candidate_rebindings = clone_rebindings[candidate]
    candidate_before = bank._candidate_storage_rebind(
        candidate, history, clone_identities[candidate], expected_source_sha256,
        history=history, guard=publication_guard, inputs=inputs,
        phase="sealed_revalidation_clone_before_update", audit=candidate_rebindings, owner_only=True,
    )
    if _controlled_file(candidate, history, modes={0o600}) != candidate_before:
        raise ValueError("The controlled clone changed before its write lease.")
    bank._no_baseline_copy_handles(candidate)
    _update_controlled_candidate(candidate, history, evidence, publication_guard, candidate_before)
    for path in (candidate, preserved):
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
    preserved_sha, preserved_identity = _closed_sha(preserved, history, inputs, publication_guard, modes={0o400})
    if preserved_sha != expected_source_sha256:
        raise ValueError("The preserved controlled artifact changed after staging.")
    candidate_sha, candidate_identity = _closed_sha(candidate, history, inputs, publication_guard, modes={0o600})
    source_identity = bank._candidate_storage_rebind(
        source, history, source_identity, expected_source_sha256,
        history=history, guard=publication_guard, inputs=inputs,
        phase="sealed_revalidation_after_staging", audit=source_rebindings,
    )
    bank._no_baseline_copy_handles(source)
    _guarded_inputs(history, inputs, publication_guard)
    evidence["source_bank_identity"] = source_identity
    evidence["candidate_storage_rebindings"] = candidate_rebindings
    evidence["preserved_storage_rebindings"] = clone_rebindings[preserved]
    evidence["clone_tracking_preparations"] = clone_preparations
    return {
        "candidate_path": str(candidate), "candidate_sha256": candidate_sha,
        "candidate_identity": candidate_identity, "input_identities": inputs,
        "account_ids": evidence["account_ids"], "preserved_path": str(preserved),
        "preserved_sha256": preserved_sha, "preserved_identity": preserved_identity,
        "preserved_owner_read_only": True, "added": 0, "upgraded": 0, "reparsed": 0,
        "total": evidence["messages"], "revalidation": evidence,
    }


def _archive_identity(value):
    names = {"device": "dev", "inode": "ino", "bytes": "size", "mtime_ns": "mtime_ns", "ctime_ns": "ctime_ns"}
    if not isinstance(value, dict) or set(value) != set(names) or any(type(item) is not int for item in value.values()):
        raise ValueError("A preservation receipt has unsupported storage coordinates.")
    return {names[key]: item for key, item in value.items()}


def _receipt_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("A preservation receipt contains duplicate fields.")
        result[key] = value
    return result


def _verified_archive(receipt, source, history, inputs, guard, expected_sha):
    """Verify independent compressed bytes and their complete restored stream."""
    from scripts.archive_wechat_rollbacks import verify_receipt

    before = _controlled_file(receipt, history, modes={0o400, 0o600})
    if before["size"] > _MAX_PRESERVATION_RECEIPT_BYTES:
        raise ValueError("The preservation receipt exceeds its bounded metadata size.")
    receipt_sha, receipt_identity = _closed_sha(receipt, history, inputs, guard, modes={0o400, 0o600})
    descriptor = os.open(receipt, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if any(getattr(opened, "st_" + key) != value for key, value in receipt_identity.items()):
            raise ValueError("The preservation receipt changed before parsing.")
        raw = stream.read(_MAX_PRESERVATION_RECEIPT_BYTES + 1)
    if len(raw) > _MAX_PRESERVATION_RECEIPT_BYTES or _controlled_file(receipt, history, modes={0o400, 0o600}) != receipt_identity:
        raise ValueError("The preservation receipt changed during parsing.")
    record = json.loads(raw, object_pairs_hook=_receipt_fields)
    if (
        type(record) is not dict
        or record.get("status") != "byte_exact_preservation_verified_upload_pending"
        or record.get("full_roundtrip_verified") is not True
        or record.get("source_unchanged") is not True or record.get("source_deleted") is not False
        or record.get("source_sha256") != expected_sha or record.get("decompressed_sha256") != expected_sha
        or type(record.get("archive_bytes")) is not int or record["archive_bytes"] <= 0
        or type(record.get("decompressed_bytes")) is not int or record["decompressed_bytes"] <= 0
        or type(record.get("source")) is not str or type(record.get("archive")) is not str
    ):
        raise ValueError("Require a completed exact full-byte preservation receipt.")
    archive = Path(record["archive"])
    recorded_source = Path(record["source"])
    if (
        not archive.is_absolute() or archive.parent != receipt.parent
        or not recorded_source.is_absolute() or recorded_source != source
        or archive.resolve() in {source.resolve(), receipt.resolve()}
    ):
        raise ValueError("The preservation receipt must name its exact source and adjacent archive.")
    source_before = _controlled_file(source, history, modes={0o600, 0o644})
    source_at_archive = _archive_identity(record.get("source_identity"))
    if (
        record["decompressed_bytes"] != source_before["size"]
        or any(source_at_archive[key] != value for key, value in source_before.items() if key != "ctime_ns")
    ):
        raise ValueError("The preserved stream differs from the selected sealed source coordinates.")
    archive_before = _controlled_file(archive, history, modes={0o400, 0o600})
    archive_at_publication = _archive_identity(record.get("archive_identity"))
    if (
        archive_before["size"] != record["archive_bytes"]
        or any(archive_at_publication[key] != value for key, value in archive_before.items() if key != "ctime_ns")
    ):
        raise ValueError("The compressed artifact differs from its published storage coordinates.")
    legacy._digest(record.get("archive_sha256"))
    bank._no_baseline_copy_handles(archive)
    # This independently hashes compressed bytes and every decompressed byte.
    # F is checked separately, allowing only its existing SHA-proven ctime gate.
    verification = verify_receipt(receipt, check_source=False)
    _guarded_inputs(history, inputs, guard)
    if (
        verification.get("status") != "full_roundtrip_verified"
        or _controlled_file(archive, history, modes={0o400, 0o600}) != archive_before
        or _controlled_file(receipt, history, modes={0o400, 0o600}) != receipt_identity
    ):
        raise ValueError("Preservation evidence changed during full stream verification.")
    bank._no_baseline_copy_handles(archive)
    bank._no_baseline_copy_handles(receipt)
    return {
        "archive_path": str(archive), "receipt_path": str(receipt),
        "archive_sha256": record["archive_sha256"], "receipt_sha256": receipt_sha,
        "decompressed_sha256": expected_sha, "decompressed_bytes": record["decompressed_bytes"],
        "archive_bytes": record["archive_bytes"], "archive_identity": archive_before,
        "receipt_identity": receipt_identity, "source_identity_at_archive": source_at_archive,
        "full_roundtrip_verified": True, "source_deleted": False, "storage_rebindings": [],
    }


def _recheck_archive(preservation, history, inputs, guard):
    """Keep verified archive bytes fixed without retagging the immutable receipt."""
    for kind in ("archive", "receipt"):
        path = Path(preservation[kind + "_path"])
        before = preservation[kind + "_identity"]
        current = _controlled_file(path, history, modes={0o400, 0o600})
        bank._no_baseline_copy_handles(path)
        if current != before:
            if any(current[key] != value for key, value in before.items() if key != "ctime_ns"):
                raise ValueError("A verified preservation artifact changed storage coordinates.")
            digest, current = _closed_sha(path, history, inputs, guard, modes={0o400, 0o600})
            if digest != preservation[kind + "_sha256"]:
                raise ValueError("A verified preservation artifact changed its exact bytes.")
            preservation["storage_rebindings"].append({
                "artifact": kind, "before_identity": before, "after_identity": current,
                "sha256": digest, "basis": "closed_preservation_ctime_only_exact_sha256_v1",
            })
            preservation[kind + "_identity"] = current
        if _controlled_file(path, history, modes={0o400, 0o600}) != current:
            raise ValueError("A verified preservation artifact changed after checking.")
    _guarded_inputs(history, inputs, guard)


def stage_archived_sealed_mac_recovery_bank_revalidation(
    local_store_root, source_path, candidate_path, *, preservation_receipt_path,
    expected_source_sha256, expected_payload_sha256, publication_guard,
    max_base_messages=_DEFAULT_MAX_BASE_MESSAGES,
    on_preflight=None,
):
    """Stage one APFS clone backed by an independently verified full-byte archive.

    The separate two-clone API retains its original contract. Here the existing
    resident archive is already charged to measured free space; one complete
    candidate and an additional GiB must fit after preservation verification.
    """
    history = wechat_archive_path(local_store_root)
    bank._require_publication_guard(publication_guard, history)
    max_base_messages = _base_message_budget(max_base_messages)
    if on_preflight is not None and not callable(on_preflight):
        raise ValueError("A controlled preflight callback must be callable.")
    legacy._digest(expected_source_sha256)
    legacy._digest(expected_payload_sha256)
    source, candidate, receipt = (
        Path(path).expanduser().absolute() for path in (source_path, candidate_path, preservation_receipt_path)
    )
    protected = {history.with_name(name).resolve() for name in bank._SOURCE_INPUTS}
    if source.resolve() in protected or candidate.resolve() in protected | {source.resolve(), receipt.resolve()}:
        raise ValueError("Archived revalidation requires distinct noncanonical artifact paths.")
    if os.path.lexists(candidate):
        raise FileExistsError("Preserve existing archived revalidation outputs unchanged.")
    _private_directory(candidate.parent)
    source_identity = _controlled_file(source, history, modes={0o600, 0o644})
    bank._no_baseline_copy_handles(source)
    if candidate.parent.stat().st_dev != source_identity["dev"]:
        raise ValueError("Archived revalidation requires a same-volume APFS candidate.")
    inputs = bank._inputs(history)
    preservation = _verified_archive(receipt, source, history, inputs, publication_guard, expected_source_sha256)
    if candidate.resolve() == Path(preservation["archive_path"]).resolve():
        raise ValueError("Preserve the independent compressed archive unchanged.")
    required = source_identity["size"] + _ARCHIVED_RESERVE_BYTES
    free = shutil.disk_usage(candidate.parent).free
    if free < required:
        raise ValueError("Insufficient space for one logical candidate and the 1 GiB archive-preserved reserve.")
    archive_rebindings = []
    bank._candidate_storage_rebind(
        source, history, preservation["source_identity_at_archive"], expected_source_sha256,
        history=history, guard=publication_guard, inputs=inputs,
        phase="archive_preservation_source_binding", audit=archive_rebindings,
    )
    evidence = validate_sealed_mac_recovery_bank_revalidation(
        local_store_root, source, expected_source_sha256=expected_source_sha256,
        expected_payload_sha256=expected_payload_sha256, publication_guard=publication_guard,
        max_base_messages=max_base_messages,
    )
    _guarded_inputs(history, inputs, publication_guard)
    evidence["source_storage_rebindings"][:0] = archive_rebindings
    if on_preflight is not None:
        on_preflight(deepcopy(evidence))
        _guarded_inputs(history, inputs, publication_guard)
    _recheck_archive(preservation, history, inputs, publication_guard)
    evidence["source_bank_identity"] = bank._candidate_storage_rebind(
        source, history, evidence["source_bank_identity"], expected_source_sha256,
        history=history, guard=publication_guard, inputs=inputs,
        phase="archived_revalidation_after_preflight", audit=evidence["source_storage_rebindings"],
    )
    free = shutil.disk_usage(candidate.parent).free
    if free < required:
        raise ValueError("Insufficient space for one logical candidate and the 1 GiB archive-preserved reserve.")
    _clonefile(source, candidate)
    candidate.chmod(0o600)
    clone_preparations = []
    _prepare_new_clone_tracking(
        candidate, source, history, inputs, publication_guard, mode=0o600,
        audit=clone_preparations, protected_paths=(receipt, preservation["archive_path"]),
    )
    candidate_rebindings = []
    clone_sha, clone_identity = _closed_sha(
        candidate, history, inputs, publication_guard, modes={0o600},
        expected_sha256=expected_source_sha256, audit=candidate_rebindings,
        phase="archived_revalidation_clone_hash",
    )
    if clone_sha != expected_source_sha256:
        raise ValueError("The archive-preserved APFS clone differs from its sealed source.")
    source_identity = bank._candidate_storage_rebind(
        source, history, evidence["source_bank_identity"], expected_source_sha256,
        history=history, guard=publication_guard, inputs=inputs,
        phase="archived_revalidation_before_update", audit=evidence["source_storage_rebindings"],
    )
    candidate_before = bank._candidate_storage_rebind(
        candidate, history, clone_identity, expected_source_sha256,
        history=history, guard=publication_guard, inputs=inputs,
        phase="archived_revalidation_clone_before_update", audit=candidate_rebindings, owner_only=True,
    )
    if _controlled_file(candidate, history, modes={0o600}) != candidate_before:
        raise ValueError("The archived candidate changed before its write lease.")
    bank._no_baseline_copy_handles(candidate)
    _update_controlled_candidate(candidate, history, evidence, publication_guard, candidate_before)
    with candidate.open("rb") as stream:
        os.fsync(stream.fileno())
    candidate_sha, candidate_identity = _closed_sha(candidate, history, inputs, publication_guard, modes={0o600})
    source_identity = bank._candidate_storage_rebind(
        source, history, source_identity, expected_source_sha256,
        history=history, guard=publication_guard, inputs=inputs,
        phase="archived_revalidation_after_staging", audit=evidence["source_storage_rebindings"],
    )
    bank._no_baseline_copy_handles(source)
    _recheck_archive(preservation, history, inputs, publication_guard)
    evidence["source_bank_identity"] = source_identity
    evidence["candidate_storage_rebindings"] = candidate_rebindings
    evidence["clone_tracking_preparations"] = clone_preparations
    return {
        "candidate_path": str(candidate), "candidate_sha256": candidate_sha,
        "candidate_identity": candidate_identity, "input_identities": inputs,
        "account_ids": evidence["account_ids"], "preservation_kind": "verified_zstd_full_byte_archive",
        "preservation": preservation, "capacity_budget": {
            "stage_required_bytes": required, "reserve_bytes": _ARCHIVED_RESERVE_BYTES,
            "free_after_archive_bytes": free, "rollback_accounted_as_existing_archive": True,
        },
        "added": 0, "upgraded": 0, "reparsed": 0, "total": evidence["messages"],
        "revalidation": evidence,
    }
