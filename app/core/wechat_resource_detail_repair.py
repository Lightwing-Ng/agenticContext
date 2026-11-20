"""Review and atomically publish bounded native resource-detail batches.

Code version: v1.2.1-codex.0
"""

from collections import Counter, defaultdict
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile

from . import wechat_macos_recovery_bank as bank
from .owner_only_files import ensure_owner_only, owner_only_problem
from .wechat_archive import WechatArchiveRepository, _file_identity
from .wechat_conversation_events import _inputs
from .wechat_global_guard import global_refresh_guard
from .wechat_resource_details import (
    FILENAME, MAX_BYTES, MAX_ENTRIES, _metadata_pin, encode_registry, load_registry,
    make_entry, project_resource_details, registry_identity, row_sha256,
)
from .wechat_resource_sender_evidence import iter_secondary_native_events


VERSION = "v1.0.0-codex.0"
MAX_PACKETS = 128
MAX_EVENTS = 100_000


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
        not stat.S_ISREG(before.st_mode) or path.is_symlink()
        or before.st_size > MAX_BYTES or owner_only_problem(path)
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
    if (
        len(raw) > MAX_BYTES
        or _metadata_pin(path, path.lstat()) != expected
        or owner_only_problem(path)
    ):
        raise ValueError("The resource detail candidate changed while reading.")
    return raw


def _write_private(path, raw):
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        ensure_owner_only(path)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


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


def stage_resource_detail_repair(
    local_store_root, candidate_path, packets, *, message_ids=None, expected_rows=None,
    target_sessions=None,
):
    """Stage complete reviewed entries, retaining all existing valid overrides."""
    repository = WechatArchiveRepository(local_store_root)
    candidate = Path(candidate_path).absolute()
    if (
        candidate.exists() or candidate.is_symlink()
        or candidate == repository.path.with_name(FILENAME)
        or not candidate.parent.is_dir() or candidate.parent.is_symlink()
    ):
        raise ValueError("Select a new detail candidate in an existing private workstation.")
    selected = set(message_ids) if message_ids is not None else None
    if selected is not None and (
        not selected or len(selected) > MAX_ENTRIES
        or any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None for value in selected)
    ):
        raise ValueError("Select a bounded nonempty resource repair scope.")
    pins = _inputs(repository)
    retained, prior_identity = load_registry(repository.path)
    existing = {entry["message_id"]: entry for entry in retained}
    if len(existing) != len(retained):
        raise ValueError("Existing resource details contain duplicate identities.")
    groups, by_peer = defaultdict(list), defaultdict(list)
    packet_count = event_count = 0
    for packet in packets:
        packet_count += 1
        if packet_count > MAX_PACKETS:
            raise ValueError("The detail repair exceeds its source packet budget.")
        # The existing bank contract owns every native source and sender shape.
        events = iter_secondary_native_events(packet)
        header = {key: value for key, value in packet.items() if key != "messages"}
        lookup = defaultdict(list)
        for event in events:
            event_count += 1
            if event_count > MAX_EVENTS:
                raise ValueError("The detail repair exceeds its native event budget.")
            key = (event["server_id"], event["local_type"] & 0xFFFFFFFF, event["create_time"])
            lookup[key].append(event)
        captured = (packet, header, lookup)
        groups[packet["session_id"]].append(captured)
        by_peer[packet["peer_id"]].append(captured)
    if not groups:
        raise ValueError("A detail repair needs authenticated native packets.")
    if expected_rows is not None:
        if not expected_rows or len(expected_rows) > MAX_ENTRIES or any(
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
                        "account:" + packet["account_id"], packet["peer_id"], "group",
                    ):
                        raise ValueError("A captured native resource crossed its account or room.")
                    bank._validate_account_binding(connection, packet)
        groups = {session: [] for session in manifest_sessions}
    # Validate retained scopes too, including entries unrelated to this batch.
    for entry in retained:
        groups.setdefault(entry["conversation"]["id"], [])
    counts, rejected = Counter(), Counter()
    new_entries = dict(existing)
    checked_existing = set()
    for session, sources in groups.items():
        with closing(_readonly(repository.path)) as connection:
            conversations, messages, status = repository._timeline_tables(
                connection, _file_identity(repository.path), conversation_id=session,
                strict=True, include_resource_details=False,
            )
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
            for entry in retained:
                if entry["conversation"]["id"] != session:
                    continue
                identifier = entry["message_id"]
                row = connection.execute(
                    f"SELECT * FROM {messages} WHERE id=? AND conversation_id=?",
                    (identifier, session),
                ).fetchone()
                if (
                    row is None or identifier in quarantined
                    or row_sha256(row) != entry["original_row_sha256"]
                ):
                    raise ValueError("Retained resource details need explicit source revalidation.")
                bank._validate_account_binding(connection, entry["packet"])
                if make_entry(dict(row), context, entry["packet"], connection=connection) != entry:
                    raise ValueError("A retained resource detail proof changed.")
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
                captured_text = (
                    manifest_sessions is not None and proof.get("message_type") == 1
                    and proof.get("text_extraction_kind") == "plain_text"
                )
                if not captured_text and (proof.get("message_type") == 1 or not isinstance(reference, dict)):
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
                    rejected["ambiguous_native_witness"] += 1
                    continue
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
                if len(new_entries) >= MAX_ENTRIES:
                    raise ValueError("The complete detail repair exceeds its entry budget.")
                new_entries[identifier] = incoming
                counts["added"] += 1
                counts["authors_filled"] += bool(
                    not row["sender_id"] and projected["sender_id"]
                )
                counts["cards_enriched"] += bool(
                    json.loads(projected["provenance"]).get("native_resource_details", {}).get("app_fields")
                )
            if manifest_sessions is not None:
                missing = selected_in_session - found
                if missing:
                    rejected["captured_target_absent"] += len(missing)
    if checked_existing != existing.keys():
        raise ValueError("The repair did not preserve every prior detail scope.")
    if _inputs(repository) != pins or registry_identity(repository.path) != prior_identity:
        raise ValueError("A resource repair input changed during staging.")
    encoded = encode_registry([new_entries[key] for key in sorted(new_entries)])
    _write_private(candidate, encoded)
    return {
        "version": VERSION, "published": False, "candidate": str(candidate),
        "candidate_sha256": _sha(encoded), "candidate_bytes": len(encoded),
        "input_identities": pins, "prior_registry_identity": prior_identity,
        "changed": bool(counts["added"]), "total_entries": len(new_entries),
        "preserved_entries": len(existing), "counts": dict(counts),
        "rejected": dict(rejected), "packets": packet_count,
    }


def publish_resource_detail_repair(local_store_root, report, *, rollback_path):
    """Atomically publish reviewed bytes while preserving the previous registry."""
    repository = WechatArchiveRepository(local_store_root)
    target = repository.path.with_name(FILENAME)
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
            _write_private(rollback, prior)
        descriptor, name = tempfile.mkstemp(prefix=".resource-details-", suffix=".json", dir=target.parent)
        temporary = Path(name)
        installed = False
        try:
            with os.fdopen(descriptor, "wb") as stream:
                ensure_owner_only(temporary)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
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
            if os.name != "nt":
                parent = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
        except BaseException:
            if installed and target.is_file() and target.read_bytes() == raw:
                if prior is None:
                    target.unlink()
                else:
                    _write_private(temporary, prior)
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
