"""Plan exact conversation redundancy without guessing an export's owner.

Code version: v1.2.0-codex.0
"""

from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import stat

from .wechat_archive import wechat_archive_path
from .wechat_contacts import (
    CANONICAL_AUTHORITIES,
    MESSAGE_BINDING_AUTHORITY,
    WechatContactRepository,
    canonical_contact_id,
    classify_wechat_identifier,
    contact_registry_path,
)
from .wechat_source_precedence import monotone_pair_runs
from .wechat_html_group_frames import group_frame_registry_digest, load_group_source_frames


VERSION = "v1.2.0-codex.0"
METHOD = "exact_group_author_body_clock_order_v1"
PRIVATE_METHOD = "exact_private_participants_author_body_clock_order_v1"
_ROOM = re.compile(r"[1-9][0-9]*@(?:im\.)?chatroom")
_CONTACT = re.compile(r"wechat:contact:[0-9a-f]{64}")
_SECOND = re.compile(r"[T ]\d{2}:\d{2}:\d{2}(?:[+\-Z]|$)")
_MAX_CELL_BYTES = 16 * 1024**2


def file_identity(path):
    """Pin resident inputs, including replacement and residency transitions."""
    path = Path(path)
    value = path.stat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(value.st_mode)
        or getattr(value, "st_flags", 0) & 0x40000000
        or any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))
    ):
        raise ValueError("Redundancy planning requires published local regular databases.")
    return {
        "dev": value.st_dev,
        "inode": value.st_ino,
        "size": value.st_size,
        "mtime_ns": value.st_mtime_ns,
        "ctime_ns": value.st_ctime_ns,
    }


def _json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def message_row_sha256(row):
    """Hash exactly one logical SELECT m.* row, with no joined display fields."""
    return hashlib.sha256(_json(dict(row)).encode()).hexdigest()


def _readonly(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA temp_store=MEMORY")
    return connection


def _object(raw):
    if not isinstance(raw, str) or len(raw.encode()) > _MAX_CELL_BYTES:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _plain(row, proof, *, native):
    locator = proof.get("locator")
    exported_type = (
        locator.get("export_msgtype", proof.get("export_msgtype"))
        if isinstance(locator, dict) else proof.get("export_msgtype")
    )
    if exported_type is not None and (
        isinstance(exported_type, bool) or str(exported_type) != "1"
    ):
        return False
    message_type = proof.get("message_type")
    if (
        not isinstance(row["text"], str)
        or not row["text"].strip()
        or proof.get("text_extraction_kind") not in (("plain_text",) if native else (None, "plain_text"))
        or message_type is not None and (type(message_type) is not int or message_type != 1)
        or native and message_type != 1
        or any(
            proof.get(key)
            for key in (
                "forwarded_components", "quoted_components", "text_components",
                "quoted_reference", "resource_reference", "payment_card_fields",
            )
        )
    ):
        return False
    return True


def _clock(row, proof, *, native):
    raw = proof.get("source_time")
    if raw is None:
        raw = row["timestamp_value"]
    if not isinstance(raw, str) or _SECOND.search(raw) is None:
        return None
    try:
        source = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        presented = datetime.fromisoformat(row["timestamp_value"].replace("Z", "+00:00"))
        if (
            source.year <= 1970
            or source.microsecond
            or presented.microsecond
            or source.replace(tzinfo=None) != presented.replace(tzinfo=None)
            or source.tzinfo is not None
            and presented.tzinfo is not None
            and source != presented
        ):
            return None
        if native and (
            presented.tzinfo is None
            or isinstance(proof.get("unix_timestamp"), bool)
            or Decimal(str(proof.get("unix_timestamp"))) != Decimal(str(presented.timestamp()))
        ):
            return None
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        return None
    return {
        "wall": source.replace(tzinfo=None).isoformat(),
        "offset": source.utcoffset().total_seconds() if source.tzinfo is not None else None,
    }


def _room(row, proof, metadata, native, html_frame):
    peer, title = row["peer_id"] or "", row["conversation_title"]
    if native:
        return peer if row["kind"] == "group" and _ROOM.fullmatch(peer) else None
    if row["source_id"].startswith("xlsx-"):
        if row["kind"] not in {"group", "unclassified"}:
            return None
        values = {value for value in (peer, title) if _ROOM.fullmatch(value)}
        return next(iter(values)) if len(values) == 1 else None
    owner_evidence = metadata.get("owner_evidence")
    if (
        row["source_id"].startswith("html-")
        and row["kind"] == "group"
        and _ROOM.fullmatch(peer)
        and (
            metadata.get("owner_verified") is True
            and row["identity_status"] == "Corroborated export ID"
            and metadata.get("source_account_id") == row["account_id"]
            and isinstance(owner_evidence, dict)
            and owner_evidence.get("native_username") == row["account_id"]
            or html_frame(row, proof, metadata)
        )
    ):
        return peer
    return None


def _ordinary_participant(value):
    return isinstance(value, str) and classify_wechat_identifier(value)["kind"] in {
        "wechat_account", "wechat_identity"
    }


def _private_pair(owner, peer):
    if owner == peer or not _ordinary_participant(owner) or not _ordinary_participant(peer):
        return None
    return tuple(sorted((owner, peer)))


def _verified_private_users(registry):
    result = set()
    for row in registry.execute(
        "SELECT id,username FROM contacts WHERE namespace='wechat' AND realm='' "
        "AND kind IN ('wechat_account','wechat_identity') AND identity_status='verified'"
    ):
        if _ordinary_participant(row["username"]) and row["id"] == canonical_contact_id(
            row["username"], authority="native_contact_username"
        ):
            result.add(row["username"])
    return result


def _private_scope(row, metadata, native, identity, verified_users):
    if row["kind"] != "direct" or row["direction"] not in {"sent", "received"}:
        return None
    owner, peer = row["account_id"], row["peer_id"]
    pair = _private_pair(owner, peer)
    if pair is None or not set(pair) <= verified_users:
        return None
    author = owner if row["direction"] == "sent" else peer
    if identity.get("id") != canonical_contact_id(author, authority="native_message_sender"):
        return None
    if native:
        if row["sender_id"] != author:
            return None
    else:
        owner_evidence = metadata.get("owner_evidence")
        if (
            not row["source_id"].startswith("html-")
            or row["identity_status"] != "Corroborated export ID"
            or row["account_key"] != "account:" + owner
            or row["container_account_key"] != row["account_key"]
            or metadata.get("source_id") != row["source_id"]
            or metadata.get("owner_verified") is not True
            or metadata.get("source_account_id") != owner
            or not isinstance(owner_evidence, dict)
            or owner_evidence.get("native_username") != owner
        ):
            return None
    return pair


def _contact_filter(history, registry, contact_id):
    if contact_id is None:
        return ""
    if not isinstance(contact_id, str) or _CONTACT.fullmatch(contact_id) is None:
        raise ValueError("Select one canonical verified contact ID.")
    row = registry.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
    if (
        row is None
        or row["identity_status"] != "verified"
        or canonical_contact_id(
            row["username"], authority="native_contact_username", namespace=row["namespace"], realm=row["realm"]
        ) != contact_id
    ):
        raise ValueError("The selected contact lacks its exact native identity.")
    history.executescript(
        "CREATE TEMP TABLE planner_senders(value TEXT PRIMARY KEY) WITHOUT ROWID;"
        "CREATE TEMP TABLE planner_proofs(value TEXT PRIMARY KEY) WITHOUT ROWID;"
    )
    authorities = sorted(CANONICAL_AUTHORITIES)
    marks = ",".join("?" for _ in authorities)
    history.executemany(
        "INSERT OR IGNORE INTO planner_senders VALUES(?)",
        registry.execute(
            "SELECT sender_id FROM sender_bindings WHERE contact_id=? AND authority IN (" + marks + ")",
            (contact_id, *authorities),
        ),
    )
    if registry.execute("SELECT 1 FROM sqlite_master WHERE name='message_contact_bindings'").fetchone():
        history.executemany(
            "INSERT OR IGNORE INTO planner_proofs VALUES(?)",
            registry.execute(
                "SELECT message_id FROM message_contact_bindings WHERE contact_id=? AND authority=?",
                (contact_id, MESSAGE_BINDING_AUTHORITY),
            ),
        )
    return (
        " AND (m.sender_id IN (SELECT value FROM temp.planner_senders)"
        " OR m.id IN (SELECT value FROM temp.planner_proofs))"
    )


def _conversation_scope(conversations, selected_sources, conversation_ids):
    available = {
        row["id"]: row for row in conversations if row["partition_id"] in selected_sources
    }
    if conversation_ids is None:
        return list(available.values())
    if not isinstance(conversation_ids, (list, tuple, set, frozenset)) or len(conversation_ids) > 20_000:
        raise ValueError("Select a bounded collection of exact retained derived conversation IDs.")
    if any(not isinstance(value, str) or not value or "\0" in value for value in conversation_ids):
        raise ValueError("Select exact retained derived conversation IDs.")
    selected = set(conversation_ids)
    if len(selected) != len(conversation_ids) or not selected <= available.keys():
        raise ValueError("Conversation IDs must be unique and belong to the selected derived sources.")
    return [row for key, row in available.items() if key in selected]


def inventory_derived_redundancy(local_store_root, *, source_ids=None, conversation_ids=None):
    """Size explicit planning scopes using metadata only, without reading messages.

    A supported proof family is an audit candidate, never a deletion decision.
    Private candidates retain their exact participant pair, while public,
    service, OpenIM, and unresolved identities remain outside this proof family.
    """
    history_path = wechat_archive_path(Path(local_store_root).expanduser())
    initial = file_identity(history_path)
    with closing(_readonly(history_path)) as history:
        partitions = {row["id"]: dict(row) for row in history.execute("SELECT * FROM partitions")}
        eligible = {
            key for key, row in partitions.items()
            if key.startswith(("xlsx-", "html-")) and row["source_label"] in {"Excel export", "HTML export"}
        }
        selected = eligible if source_ids is None else set(source_ids)
        if not selected <= eligible:
            raise ValueError("Select only retained Excel or HTML source partitions.")
        conversations = [dict(row) for row in history.execute("SELECT * FROM conversations ORDER BY id")]
        native_by_room, native_by_pair = defaultdict(list), defaultdict(list)
        for row in conversations:
            partition = partitions.get(row["partition_id"], {})
            if (
                row["partition_id"].startswith("ios-")
                and partition.get("identity_status") == "Verified archive ID"
                and row["kind"] == "group" and _ROOM.fullmatch(row["peer_id"] or "")
            ):
                native_by_room[row["peer_id"]].append(row)
            if (
                row["partition_id"].startswith("ios-")
                and partition.get("identity_status") == "Verified archive ID"
                and row["kind"] == "direct"
            ):
                pair = _private_pair(partition.get("account_id"), row["peer_id"])
                if pair is not None:
                    native_by_pair[pair].append(row)
        scopes, counts = [], Counter()
        for row in _conversation_scope(conversations, selected, conversation_ids):
            source = partitions[row["partition_id"]]
            room = row["peer_id"] or ""
            if row["partition_id"].startswith("xlsx-"):
                values = {value for value in (room, row["title"]) if _ROOM.fullmatch(value)}
                room = next(iter(values)) if len(values) == 1 else ""
            elif row["kind"] != "group" or _ROOM.fullmatch(room) is None:
                room = ""
            pair = None
            unsupported_reason = None
            if row["kind"] == "direct":
                if source["identity_status"] == "Corroborated export ID":
                    pair = _private_pair(source.get("account_id"), row["peer_id"])
                native = native_by_pair.get(pair, [])
                if source["identity_status"] != "Corroborated export ID" or not source.get("account_id"):
                    unsupported_reason = "export_owner_unconfirmed"
                elif not _ordinary_participant(source["account_id"]) or not _ordinary_participant(row["peer_id"]):
                    unsupported_reason = "official_service_openim_or_unresolved_participant"
                elif pair is None:
                    unsupported_reason = "self_or_ambiguous_participant_pair"
                elif not native:
                    unsupported_reason = "exact_native_participant_pair_absent"
                family = (
                    "private_participant_identity_missing" if pair is None else
                    "native_private_scope_missing" if not native else
                    "private_candidate_requires_message_proof"
                )
            else:
                native = native_by_room.get(room, [])
                family = (
                    "literal_group_identifier_missing" if not room else
                    "native_group_scope_missing" if not native else
                    "group_candidate_requires_message_proof"
                )
            count = row["message_count"]
            if type(count) is not int or count < 0:
                raise ValueError("A derived conversation has an invalid metadata message count.")
            scopes.append({
                "conversation_id": row["id"], "source_id": row["partition_id"],
                "source_label": source["source_label"], "kind": row["kind"],
                "literal_group_id": room or None, "message_count": count,
                "participant_pair": list(pair) if pair else None,
                "unsupported_reason": unsupported_reason,
                "estimated_native_rows": sum(value["message_count"] for value in native),
                "native_conversations": len(native), "proof_family": family,
            })
            counts["conversations"] += 1
            counts["messages"] += count
            counts[family + "_conversations"] += 1
            counts[family + "_messages"] += count
    if file_identity(history_path) != initial:
        raise ValueError("The archive changed during metadata-only redundancy inventory.")
    return {"version": VERSION, "history_identity": initial, "conversations": scopes, "counts": dict(counts)}


def plan_derived_redundancy(
    local_store_root,
    *,
    contact_id=None,
    source_ids=None,
    conversation_ids=None,
    max_derived_rows=250_000,
    max_native_rows=2_000_000,
):
    """Return source-pinned deletion candidates; never mutate an archive or media.

    Exact conversation IDs can divide large source archives into bounded,
    complete scopes without splitting message-order or duplicate checks.
    Groups require a literal numeric group ID. Private chats require an exact,
    independently verified owner/peer pair and direction-consistent authors.
    Missing owner or export
    timezone evidence remains explicitly unknown; it is never filled from the
    matched native observation. A candidate needs three unique ordered anchors.
    """
    from .wechat_contact_analysis import (
        _global_group_source_frame,
        _native_conversation_key,
        _native_statement_key,
        _requests,
        _ROW_SELECT,
    )

    if any(type(value) is not int or value < 1 for value in (max_derived_rows, max_native_rows)):
        raise ValueError("Row budgets must be positive integers.")
    root = Path(local_store_root).expanduser()
    history_path, registry_path = wechat_archive_path(root), contact_registry_path(root)
    initial_history, initial_registry = file_identity(history_path), file_identity(registry_path)
    initial_frames = group_frame_registry_digest(root)
    counts = Counter()
    resolver = WechatContactRepository(root)
    derived, native = [], []
    wanted_signatures = set()
    with closing(_readonly(history_path)) as history, closing(_readonly(registry_path)) as registry:
        message_columns = [row[1] for row in history.execute("PRAGMA table_info(messages)")]
        suffix = _contact_filter(history, registry, contact_id)
        verified_users = _verified_private_users(registry)
        partitions = {row["id"]: dict(row) for row in history.execute("SELECT * FROM partitions")}
        eligible = {
            key for key, row in partitions.items()
            if key.startswith(("xlsx-", "html-")) and row["source_label"] in {"Excel export", "HTML export"}
        }
        selected = eligible if source_ids is None else set(source_ids)
        if not selected <= eligible:
            raise ValueError("Select only retained Excel or HTML source partitions.")
        snapshots = {}
        if history.execute("SELECT 1 FROM sqlite_master WHERE name='source_snapshots'").fetchone():
            snapshots = {row["id"]: dict(row) for row in history.execute("SELECT * FROM source_snapshots")}
        for snapshot in snapshots.values():
            snapshot["decoded"] = _object(snapshot["metadata"]) or {}
        source_frames = load_group_source_frames(root, snapshots)

        def html_frame(row, proof, metadata):
            return _global_group_source_frame(
                row, proof, metadata,
                source_frames.get((proof.get("source_snapshot"), row["peer_id"])),
            )

        def staged(conversation, is_native):
            bucket = "native_rows" if is_native else "derived_rows"
            budget = max_native_rows if is_native else max_derived_rows
            count = history.execute(
                "SELECT COUNT(*) FROM messages m WHERE m.conversation_id=?" + suffix,
                (conversation["id"],),
            ).fetchone()[0]
            if counts[bucket] + count > budget:
                raise ValueError("The selected source scope exceeds its " + bucket + " budget.")
            counts[bucket] += count
            cursor = history.execute(
                _ROW_SELECT + "WHERE m.conversation_id=?" + suffix + " ORDER BY m.ordinal,m.id",
                (conversation["id"],),
            )
            while batch := cursor.fetchmany(400):
                rows = [dict(row) for row in batch]
                supported = [
                    row for row in rows
                    if (proof := _object(row["provenance"])) is not None
                    and _plain(row, proof, native=is_native)
                ]
                counts["unsupported_native_rows" if is_native else "unsupported_derived_rows"] += len(rows) - len(supported)
                rows = supported
                if not rows:
                    continue
                row_hashes = {
                    row["id"]: message_row_sha256({key: row[key] for key in message_columns}) for row in rows
                }
                identities = resolver.resolve_messages(_requests(history, rows))
                for row in rows:
                    proof = _object(row["provenance"])
                    identity = identities.get(row["id"], {})
                    if (
                        proof is None
                        or not _plain(row, proof, native=is_native)
                        or identity.get("identity_status") != "verified"
                        or contact_id is not None and identity.get("id") != contact_id
                    ):
                        continue
                    snapshot = snapshots.get(proof.get("source_snapshot"), {})
                    metadata = snapshot.get("decoded", {})
                    room = _room(row, proof, metadata, is_native, html_frame)
                    pair = _private_scope(row, metadata, is_native, identity, verified_users)
                    clock = _clock(row, proof, native=is_native)
                    if room is None and pair is None or clock is None:
                        continue
                    event = None
                    if is_native:
                        owner = row["account_id"] or ""
                        event = _native_statement_key(row, identity["id"], _native_conversation_key(row))
                        if (
                            event is None
                            or snapshot.get("source_id") != row["source_id"]
                            or metadata.get("source_id") != row["source_id"]
                            or metadata.get("source_account_id") != owner
                            or metadata.get("account_hash_verified") is not True
                            or metadata.get("account_hash") != hashlib.md5(owner.encode()).hexdigest()
                            or not isinstance(proof.get("locator"), dict)
                            or not proof["locator"]
                        ):
                            continue
                    body_hash = hashlib.sha256(row["text"].encode()).hexdigest()
                    scope_key = "private:" + _json(pair) if pair else "group:" + room
                    signature = (scope_key, identity["id"], body_hash, clock["wall"])
                    if is_native and signature not in wanted_signatures:
                        continue
                    yield {
                        "id": row["id"], "source_id": row["source_id"],
                        "conversation_id": row["conversation_id"], "ordinal": row["ordinal"],
                        "contact_id": identity["id"], "room": room, "clock": clock,
                        "participant_pair": pair, "owner": row["account_id"],
                        "direction": row["direction"],
                        "body_hash": body_hash, "row_hash": row_hashes[row["id"]],
                        "signature": signature,
                        "event": event, "snapshot_id": proof.get("source_snapshot"),
                        "server_id": str(proof.get("server_id", "")),
                        "rank": 0 if metadata.get("source_label") == "iPad archive" else 1,
                        "owner_unconfirmed": row["account_key"].startswith("source:"),
                    }

        conversations = [dict(row) for row in history.execute("SELECT * FROM conversations ORDER BY id")]
        scoped = _conversation_scope(conversations, selected, conversation_ids)
        counts["selected_derived_conversations"] = len(scoped)
        for conversation in scoped:
            derived.extend(staged(conversation, False))
        rooms = {row["room"] for row in derived if row["room"] is not None}
        private_pairs = {row["participant_pair"] for row in derived if row["participant_pair"] is not None}
        wanted_signatures.update(row["signature"] for row in derived)
        for conversation in conversations:
            partition = partitions.get(conversation["partition_id"], {})
            pair = _private_pair(partition.get("account_id"), conversation["peer_id"])
            if conversation["partition_id"].startswith("ios-") and (
                conversation["kind"] == "group" and conversation["peer_id"] in rooms
                or conversation["kind"] == "direct" and pair in private_pairs
            ):
                native.extend(staged(conversation, True))

    derived_counts = Counter((row["conversation_id"], row["signature"]) for row in derived)
    native_events = defaultdict(lambda: defaultdict(list))
    for row in native:
        native_events[row["signature"]][row["event"]].append(row)
    pairs = defaultdict(list)
    for left in derived:
        groups = native_events.get(left["signature"], {})
        if derived_counts[(left["conversation_id"], left["signature"])] != 1 or len(groups) != 1:
            counts["ambiguous_or_unmatched_rows"] += 1
            continue
        for observations in groups.values():
            for right in observations:
                if (
                    left["clock"]["offset"] is not None
                    and right["clock"]["offset"] is not None
                    and left["clock"]["offset"] != right["clock"]["offset"]
                ):
                    continue
                pairs[(left["conversation_id"], right["conversation_id"], right["snapshot_id"])].append((left, right))
    accepted = defaultdict(list)
    for group in pairs.values():
        for run in monotone_pair_runs(
            group,
            left_order=lambda pair: pair[0]["ordinal"],
            right_order=lambda pair: pair[1]["ordinal"],
            clock=lambda pair: pair[0]["clock"]["wall"],
        ):
            if len({pair[0]["signature"] for pair in run}) < 3:
                continue
            authors = len({pair[0]["contact_id"] for pair in run})
            for left, right in run:
                accepted[left["id"]].append((right["rank"], right["id"], left, right, len(run), authors))
    matches = []
    for candidates in accepted.values():
        _, _, left, right, anchor_count, author_count = min(candidates, key=lambda value: value[:2])
        evidence = {
            "method": PRIVATE_METHOD if left["participant_pair"] else METHOD,
            "derived_source_id": left["source_id"],
            "native_source_id": right["source_id"],
            "derived_conversation_id": left["conversation_id"],
            "native_conversation_id": right["conversation_id"],
            "room_id": left["room"], "contact_id": left["contact_id"],
            "body_sha256": left["body_hash"], "wall_clock": left["clock"]["wall"],
            "derived_timezone_unknown": left["clock"]["offset"] is None,
            "derived_owner_unconfirmed": left["owner_unconfirmed"],
            "native_server_id": right["server_id"], "native_event_key": right["event"],
            "native_source_snapshot_id": right["snapshot_id"],
            "anchor_count": anchor_count, "author_count": author_count,
        }
        if left["participant_pair"]:
            evidence.update(
                chat_kind="direct", participant_pair=list(left["participant_pair"]),
                derived_owner_id=left["owner"], native_owner_id=right["owner"],
                derived_direction=left["direction"], native_direction=right["direction"],
            )
        matches.append({
            "derived_id": left["id"], "native_id": right["id"],
            "derived_sha256": left["row_hash"], "native_sha256": right["row_hash"],
            "evidence": evidence,
        })
    if (
        file_identity(history_path) != initial_history
        or file_identity(registry_path) != initial_registry
        or group_frame_registry_digest(root) != initial_frames
    ):
        raise ValueError("The retained source databases changed during redundancy planning.")
    counts.update({"eligible_derived_rows": len(derived), "eligible_native_rows": len(native), "matches": len(matches)})
    return {
        "version": VERSION, "history_identity": initial_history, "registry_identity": initial_registry,
        "group_frame_registry_digest": initial_frames,
        "scope": {"source_ids": sorted({row["partition_id"] for row in scoped}), "conversation_ids": [row["id"] for row in scoped], "contact_id": contact_id},
        "matches": sorted(matches, key=lambda row: row["derived_id"]), "counts": dict(counts),
    }
