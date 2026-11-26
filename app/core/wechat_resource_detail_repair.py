"""Review and atomically publish bounded native resource-detail batches.

Code version: v1.4.3-codex.0
"""

from collections import Counter, defaultdict
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile

from . import wechat_macos_recovery_bank as bank
from . import wechat_resource_detail_index as detail_index
from .owner_only_files import ensure_owner_only, owner_only_problem
from .wechat_archive import WechatArchiveRepository, WechatTimelineProjectionError, _file_identity
from .wechat_completion_clone import sync_completion_directory
from .wechat_conversation_events import _inputs
from .wechat_global_guard import global_refresh_guard
from .wechat_resource_details import (
    COLUMNS, FILENAME, MAX_BYTES, MAX_ENTRIES, _indexed_detail_superseded, _metadata_pin,
    encode_registry, load_registry, make_entry, project_resource_details, registry_identity, row_sha256,
)
from .wechat_resource_sender_evidence import iter_secondary_native_events


VERSION = "v1.0.0-codex.0"
MAX_PACKETS = 128
MAX_EVENTS = 100_000
MAX_INDEX_PACKETS = 1024


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _readonly(path):
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA temp_store=MEMORY")
    return connection


def _candidate_bytes(path):
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BYTES
        or owner_only_problem(path)
        or getattr(before, "st_flags", 0) & 0x40000000
    ):
        raise ValueError("A detail candidate must be bounded, private and resident.")
    expected = _metadata_pin(path, before)
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
    )
    with os.fdopen(descriptor, "rb") as stream:
        if _metadata_pin(path, os.fstat(descriptor), descriptor=descriptor) != expected:
            raise ValueError("The resource detail candidate changed before opening.")
        raw = stream.read(MAX_BYTES + 1)
        if _metadata_pin(path, os.fstat(descriptor), descriptor=descriptor) != expected:
            raise ValueError("The resource detail candidate changed while reading.")
    after = path.lstat()
    if (
        len(raw) > MAX_BYTES or owner_only_problem(path)
        or _metadata_pin(path, after) != expected
    ):
        raise ValueError("The resource detail candidate changed while reading.")
    return raw


def _write_private_bytes(path, raw, *, descriptor=None):
    if descriptor is None:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600,
        )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            ensure_owner_only(path)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _quarantined(connection):
    if "secondary_completions" not in {
        row[1] for row in connection.execute("PRAGMA database_list")
    }:
        return set()
    if not connection.execute(
        "SELECT 1 FROM secondary_completions.sqlite_schema "
        "WHERE type='table' AND name='ambiguities'"
    ).fetchone():
        return set()
    return {
        row[0] for row in connection.execute("SELECT id FROM secondary_completions.ambiguities")
    }


def _candidate_rows(connection, messages, session, selected):
    if selected is None:
        yield from connection.execute(
            f"SELECT * FROM {messages} WHERE conversation_id=?", (session,),
        )
        return
    identifiers = sorted(selected)
    for start in range(0, len(identifiers), 256):
        batch = identifiers[start:start + 256]
        placeholders = ",".join("?" for _ in batch)
        yield from connection.execute(
            f"SELECT * FROM {messages} WHERE conversation_id=? AND id IN ({placeholders})",
            (session, *batch),
        )


def _manifest_sessions(target_sessions, expected_rows, selected):
    if (
        not isinstance(target_sessions, dict)
        or not expected_rows
        or not selected
        or not selected.issubset(target_sessions)
        or set(target_sessions) - set(expected_rows)
        or any(
            not isinstance(session, str) or re.fullmatch(r"wechat:[0-9a-f]{64}", session) is None
            for session in target_sessions.values()
        )
    ):
        raise ValueError("Target conversations need a bounded captured row manifest.")
    result = defaultdict(set)
    for identifier in selected:
        result[target_sessions[identifier]].add(identifier)
    return result


def _same_text_author_witness(projections, account_key):
    """Prefer the archive owner's witness when two devices prove identical text."""
    if len(projections) != 2:
        return None
    original = [
        item for item in projections if "account:" + item[0]["account_id"] == account_key
    ]
    if len(original) != 1:
        return None
    _first_header, first, first_row = projections[0]
    _second_header, second, second_row = projections[1]
    if (
        first.get("local_type") == second.get("local_type") == 1
        and all(first.get(key) == second.get(key) for key in (
            "server_id", "create_time", "sender_id", "content_sha256", "text", "native_text",
        ))
        and all(first_row[key] == second_row[key] for key in COLUMNS if key != "provenance")
    ):
        return original[0]
    return None


def stage_resource_detail_repair(
    local_store_root, candidate_path, packets, *, message_ids=None, expected_rows=None,
    target_sessions=None, indexed=False,
):
    """Stage complete reviewed entries, retaining all existing valid overrides."""
    repository = WechatArchiveRepository(local_store_root)
    candidate = Path(candidate_path).absolute()
    index_path = repository.path.with_name(detail_index.FILENAME)
    entry_limit = detail_index.MAX_ENTRIES if indexed else MAX_ENTRIES
    if (
        candidate.exists() or candidate.is_symlink()
        or candidate in {index_path, repository.path.with_name(FILENAME)}
        or not candidate.parent.is_dir() or candidate.parent.is_symlink()
    ):
        raise ValueError("Select a new detail candidate in an existing private workstation.")
    selected = set(message_ids) if message_ids is not None else None
    if selected is not None and (
        not selected or len(selected) > entry_limit
        or any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None for value in selected)
    ):
        raise ValueError("Select a bounded nonempty resource repair scope.")
    pins = _inputs(repository)
    retained, prior_identity = load_registry(repository.path)
    json_ids = {entry["message_id"] for entry in retained}
    index_pin = detail_index.index_identity(index_path)
    indexed_hashes = {}
    if indexed:
        for entry, projected_sha, _source_key in detail_index.indexed_entries(index_path, index_pin):
            retained.append(entry)
            indexed_hashes[entry["message_id"]] = projected_sha
    reserved_index_ids = set() if indexed else detail_index.indexed_message_ids(index_path, index_pin)
    existing = {entry["message_id"]: entry for entry in retained}
    if len(existing) != len(retained):
        raise ValueError("Existing resource details contain duplicate identities.")
    groups, by_peer = defaultdict(list), defaultdict(list)
    packet_count = event_count = 0
    for packet in packets:
        packet_count += 1
        if packet_count > (MAX_INDEX_PACKETS if indexed else MAX_PACKETS):
            raise ValueError("The detail repair exceeds its source packet budget.")
        # The existing bank contract owns every native source and sender shape.
        events = iter_secondary_native_events(packet)
        header = {key: value for key, value in packet.items() if key != "messages"}
        lookup = defaultdict(list)
        for event in events:
            event_count += 1
            if event_count > (entry_limit if indexed else MAX_EVENTS):
                raise ValueError("The detail repair exceeds its native event budget.")
            key = (event["server_id"], event["local_type"] & 0xFFFFFFFF, event["create_time"])
            lookup[key].append(event)
        captured = (packet, header, lookup)
        groups[packet["session_id"]].append(captured)
        by_peer[packet["peer_id"]].append(captured)
    if not groups:
        raise ValueError("A detail repair needs authenticated native packets.")
    if expected_rows is not None:
        if not expected_rows or len(expected_rows) > entry_limit or any(
            not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{64}", key) is None
            or not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for key, value in expected_rows.items()
        ):
            raise ValueError("The capture manifest needs exact bounded upstream row hashes.")
        if selected is None:
            selected = set(expected_rows)
        elif not selected.issubset(expected_rows):
            raise ValueError("The selected repair exceeds its captured manifest.")
    manifest_sessions = None
    if target_sessions is not None:
        manifest_sessions = _manifest_sessions(target_sessions, expected_rows, selected)
        with closing(_readonly(repository.path)) as connection:
            for session, sources in groups.items():
                context = connection.execute(
                    "SELECT account_key,peer_id,kind FROM main.conversations WHERE id=?",
                    (session,),
                ).fetchone()
                for packet, _header, _lookup in sources:
                    if context is None or tuple(context) != (
                        "account:" + packet["account_id"], packet["peer_id"], packet["conversation_kind"],
                    ):
                        raise ValueError("A captured native resource crossed its account or conversation.")
                    bank._validate_account_binding(connection, packet)
        groups = {session: [] for session in manifest_sessions}
    # Validate retained scopes too, including entries unrelated to this batch.
    by_retained_session = defaultdict(list)
    for entry in retained:
        session = entry["conversation"]["id"]
        groups.setdefault(session, [])
        by_retained_session[session].append(entry)
    counts, rejected = Counter(), Counter()
    new_entries = dict(existing)
    checked_existing = set()
    incomplete_scopes = []
    for session, sources in groups.items():
        with closing(_readonly(repository.path)) as connection:
            try:
                conversations, messages, status = repository._timeline_tables(
                    connection, _file_identity(repository.path), conversation_id=session,
                    strict=True, include_resource_details=False,
                )
            except WechatTimelineProjectionError as error:
                prior = by_retained_session.get(session, ())
                if not indexed or manifest_sessions is None or any(
                    entry["message_id"] in indexed_hashes for entry in prior
                ):
                    raise
                # Existing JSON proofs stay byte-for-byte in their independent registry.
                checked_existing.update(entry["message_id"] for entry in prior)
                pending_rows = len(manifest_sessions.get(session, ()))
                rejected["incomplete_upstream_scope"] += pending_rows
                incomplete_scopes.append({"session": session, "status": error.status, "rows": pending_rows})
                continue
            if status not in {"ready", "absent"}:
                raise ValueError("The detail repair needs a verified upstream timeline.")
            context_row = connection.execute(
                f"SELECT * FROM {conversations} WHERE id=?", (session,),
            ).fetchone()
            if context_row is None:
                raise ValueError("A resource repair conversation is unavailable.")
            context = dict(context_row)
            for packet, _header, _lookup in sources:
                if (
                    context["account_key"] != "account:" + packet["account_id"]
                    or context["peer_id"] != packet["peer_id"]
                ):
                    raise ValueError("A resource repair crossed its account or room.")
                bank._validate_account_binding(connection, packet)
            quarantined = _quarantined(connection)
            for entry in by_retained_session[session]:
                identifier = entry["message_id"]
                row = connection.execute(
                    f"SELECT * FROM {messages} WHERE id=? AND conversation_id=?",
                    (identifier, session),
                ).fetchone()
                if (
                    identifier in indexed_hashes and row is not None
                    and identifier not in quarantined
                    and row_sha256(row) != entry["original_row_sha256"]
                    and _indexed_detail_superseded(dict(row), entry)
                ):
                    checked_existing.add(identifier)
                    counts["superseded_preserved"] += 1
                    continue
                if (
                    row is None or identifier in quarantined
                    or row_sha256(row) != entry["original_row_sha256"]
                ):
                    raise ValueError("Retained resource details need explicit source revalidation.")
                bank._validate_account_binding(connection, entry["packet"])
                if make_entry(dict(row), context, entry["packet"], connection=connection) != entry:
                    raise ValueError("A retained resource detail proof changed.")
                if identifier in indexed_hashes:
                    events = iter_secondary_native_events(entry["packet"])
                    projected = project_resource_details(dict(row), context, next(events), connection=connection)
                    if row_sha256(projected) != indexed_hashes[identifier]:
                        raise ValueError("An indexed resource detail no longer yields its reviewed result.")
                checked_existing.add(identifier)
            if manifest_sessions is None:
                selected_in_session = selected
            else:
                selected_in_session = manifest_sessions.get(session, set())
                sources = by_peer.get(context["peer_id"], ())
            if not sources:
                if manifest_sessions is not None and selected_in_session:
                    rejected["captured_source_absent"] += len(selected_in_session)
                continue
            if selected_in_session == set():
                continue
            found = set()
            for raw in _candidate_rows(connection, messages, session, selected_in_session):
                row = dict(raw)
                identifier = row["id"]
                if manifest_sessions is not None:
                    found.add(identifier)
                proof = json.loads(row["provenance"])
                reference = proof.get("resource_reference")
                captured_body = manifest_sessions is not None and (
                    (proof.get("message_type"), proof.get("text_extraction_kind"))
                    in {(1, "plain_text"), (49, "quoted_reply")}
                )
                if not captured_body and (proof.get("message_type") == 1 or not isinstance(reference, dict)):
                    if manifest_sessions is not None:
                        rejected["upstream_changed_since_capture"] += 1
                    continue
                key = (proof.get("server_id"), proof.get("message_type"), proof.get("unix_timestamp"))
                matches = [
                    (header, event)
                    for _packet, header, lookup in sources
                    for event in lookup.get(key, ())
                ]
                if not matches:
                    if manifest_sessions is not None:
                        rejected["captured_witness_absent"] += 1
                    continue
                counts["selected"] += 1
                if expected_rows is not None and row_sha256(row) != expected_rows[identifier]:
                    rejected["upstream_changed_since_capture"] += 1
                    continue
                if identifier in quarantined:
                    rejected["quarantined"] += 1
                    continue
                if identifier in reserved_index_ids:
                    rejected["existing_detail_requires_review"] += 1
                    continue
                if len(matches) != 1 and manifest_sessions is None:
                    rejected["ambiguous_native_witness"] += 1
                    continue
                projections = []
                no_change = False
                for header, event in matches:
                    if (
                        "account:" + header["account_id"] == context["account_key"]
                        and header["session_id"] != session
                    ):
                        continue
                    try:
                        projected = project_resource_details(row, context, event, connection=connection)
                    except ValueError:
                        continue
                    if projected != row:
                        projections.append((header, event, projected))
                    else:
                        no_change = True
                if not projections:
                    if no_change:
                        counts["no_change"] += 1
                    else:
                        rejected["native_evidence_conflict"] += 1
                    continue
                if len(projections) != 1:
                    confirmed = (
                        _same_text_author_witness(projections, context["account_key"])
                        if indexed and manifest_sessions is not None else None
                    )
                    if confirmed is None:
                        rejected["ambiguous_native_witness"] += 1
                        continue
                    projections = [confirmed]
                    counts["corroborated_text_senders"] += 1
                header, event, projected = projections[0]
                packet = {**header, "messages": [{
                    key: value for key, value in event.items() if key not in header
                }]}
                try:
                    incoming = make_entry(row, context, packet, connection=connection)
                except ValueError:
                    rejected["native_evidence_conflict"] += 1
                    continue
                if identifier in existing:
                    if incoming != existing[identifier]:
                        # Preserve the reviewed proof rather than choosing a new witness.
                        rejected["existing_detail_requires_review"] += 1
                    else:
                        counts["existing"] += 1
                    continue
                if len(new_entries) >= entry_limit:
                    raise ValueError("The complete detail repair exceeds its entry budget.")
                new_entries[identifier] = incoming
                if indexed:
                    indexed_hashes[identifier] = row_sha256(projected)
                counts["added"] += 1
                counts["authors_filled"] += bool(
                    not row["sender_id"] and projected["sender_id"]
                )
                counts["cards_enriched"] += bool(
                    json.loads(projected["provenance"]).get("native_resource_details", {}).get("app_fields")
                )
                if json.loads(projected["provenance"]).get("text_extraction_kind") == "forwarded_text":
                    counts["forwarded_records"] += 1
            if manifest_sessions is not None:
                missing = selected_in_session - found
                if missing:
                    rejected["captured_target_absent"] += len(missing)
    if checked_existing != existing.keys():
        raise ValueError("The repair did not preserve every prior detail scope.")
    if _inputs(repository) != pins or registry_identity(repository.path) != prior_identity:
        raise ValueError("A resource repair input changed during staging.")
    if indexed:
        detail_index.write_index(candidate, (
            (new_entries[key], indexed_hashes[key]) for key in sorted(new_entries.keys() - json_ids)
        ))
        digest = detail_index.file_sha256(candidate)
    else:
        encoded = encode_registry([new_entries[key] for key in sorted(new_entries)])
        _write_private_bytes(candidate, encoded)
        digest = _sha(encoded)
    return {
        "version": VERSION, "published": False, "candidate": str(candidate),
        "candidate_sha256": digest, "candidate_bytes": candidate.stat().st_size,
        "input_identities": pins, "prior_registry_identity": prior_identity,
        "changed": bool(counts["added"]), "total_entries": len(new_entries),
        "preserved_entries": len(existing), "counts": dict(counts),
        "rejected": dict(rejected), "packets": packet_count,
        **({"indexed": True, "prior_index_identity": index_pin} if indexed else {}),
        **({"incomplete_scopes": incomplete_scopes} if incomplete_scopes else {}),
    }


def publish_resource_detail_repair(local_store_root, report, *, rollback_path):
    """Atomically publish reviewed bytes while preserving the previous registry."""
    repository = WechatArchiveRepository(local_store_root)
    indexed = report.get("indexed") is True
    target = repository.path.with_name(detail_index.FILENAME if indexed else FILENAME)
    candidate, rollback = Path(report["candidate"]), Path(rollback_path).absolute()
    if (
        report.get("version") != VERSION or report.get("published") is not False
        or candidate.is_symlink() or not candidate.is_file()
        or candidate == target or report.get("changed") is not False and rollback.exists()
        or rollback.is_symlink()
        or not rollback.parent.is_dir() or rollback.parent.is_symlink()
        or rollback.resolve().is_relative_to(Path(local_store_root).resolve())
    ):
        raise ValueError("Select an untouched reviewed candidate and external rollback path.")
    if indexed:
        return _publish_indexed_resource_details(repository, local_store_root, candidate, target, rollback, report)
    raw = _candidate_bytes(candidate)
    if _sha(raw) != report["candidate_sha256"]:
        raise ValueError("The reviewed resource detail candidate changed.")
    with global_refresh_guard(local_store_root):
        if (
            _inputs(repository) != report["input_identities"]
            or registry_identity(repository.path) != report["prior_registry_identity"]
        ):
            raise ValueError("Resource detail publication inputs changed.")
        if report["changed"] is False:
            return {**report, "published": False, "unchanged": True}
        prior = target.read_bytes() if target.exists() else None
        if prior is not None:
            _write_private_bytes(rollback, prior)
        descriptor, name = tempfile.mkstemp(prefix=".resource-details-", suffix=".json", dir=target.parent)
        temporary = Path(name)
        installed = False
        try:
            _write_private_bytes(temporary, raw, descriptor=descriptor)
            if _inputs(repository) != report["input_identities"]:
                raise ValueError("A source changed before resource detail publication.")
            os.replace(temporary, target)
            installed = True
            if target.read_bytes() != raw:
                raise ValueError("Resource detail readback differs from its candidate.")
            after = _inputs(repository)
            key = str(target.resolve())
            if {k: v for k, v in after.items() if k != key} != {
                k: v for k, v in report["input_identities"].items() if k != key
            }:
                raise ValueError("An original source changed during detail publication.")
            sync_completion_directory(target.parent)
        except BaseException:
            if installed and target.is_file() and target.read_bytes() == raw:
                if prior is None:
                    target.unlink()
                else:
                    _write_private_bytes(temporary, prior)
                    os.replace(temporary, target)
            raise
        finally:
            if temporary.exists():
                temporary.unlink()
    return {
        **deepcopy(report), "published": True, "unchanged": False,
        "target": str(target), "target_identity": registry_identity(repository.path),
        "rollback": str(rollback) if prior is not None else None,
    }


def _copy_private_index(source, target):
    with Path(source).open("rb") as incoming, Path(target).open("xb") as destination:
        ensure_owner_only(target)
        shutil.copyfileobj(incoming, destination, length=4 * 1024**2)
        destination.flush()
        os.fsync(destination.fileno())


def _publish_indexed_resource_details(repository, local_store_root, candidate, target, rollback, report):
    expected_digest = report["candidate_sha256"]
    before = detail_index.index_identity(candidate)
    if before is None or detail_index.file_sha256(candidate) != expected_digest:
        raise ValueError("The reviewed resource detail index changed.")
    rebased_key = str(target.absolute())
    prior = report["prior_index_identity"]
    expected_inputs = report["input_identities"]

    def unchanged():
        return (
            _inputs(repository) == expected_inputs
            and detail_index.index_identity(target) == prior
            and detail_index.index_identity(candidate) == before
        )

    with global_refresh_guard(local_store_root):
        if not unchanged():
            raise ValueError("Resource detail index inputs changed before publication.")
        if report["changed"] is False:
            return {**report, "published": False, "unchanged": True}
        if prior is not None:
            _copy_private_index(target, rollback)
            if detail_index.file_sha256(rollback) != detail_index.file_sha256(target):
                raise ValueError("The resource detail index rollback copy differs from its source.")
        with tempfile.TemporaryDirectory(prefix=".resource-detail-index-", dir=target.parent) as directory:
            staged = Path(directory) / target.name
            _copy_private_index(candidate, staged)
            if detail_index.file_sha256(staged) != expected_digest or not unchanged():
                raise ValueError("Resource detail index changed while preparing publication.")
            os.replace(staged, target)
            try:
                final_pin = detail_index.index_identity(target)
                final_inputs = _inputs(repository)
                if (
                    detail_index.file_sha256(target) != expected_digest
                    or {k: v for k, v in final_inputs.items() if k != rebased_key}
                    != {k: v for k, v in expected_inputs.items() if k != rebased_key}
                ):
                    raise ValueError("Published resource detail index failed source readback.")
                sync_completion_directory(target.parent)
            except BaseException:
                if detail_index.file_sha256(target) == expected_digest:
                    if prior is None:
                        target.unlink()
                    else:
                        _copy_private_index(rollback, staged)
                        os.replace(staged, target)
                    sync_completion_directory(target.parent)
                raise
    return {
        **deepcopy(report), "published": True, "unchanged": False,
        "target": str(target), "target_identity": final_pin,
        "rollback": str(rollback) if prior is not None else None,
    }
