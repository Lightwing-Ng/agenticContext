"""Group complete opaque-ID transcript copies without changing source observations.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import hashlib
import json
import re
import sqlite3

from .wechat_forwarded_origins import (
    MAX_COPY_DETAILS,
    MAX_COPY_PACKET_BYTES,
    MAX_COPY_PARENTS,
    MAX_COPY_SOURCE_BYTES,
    _DATA_ID,
    _author,
    _copy_constraints,
    _metadata,
    _native_packet,
    _native_scope,
    _native_time,
    _object,
    _server_constraint,
    _source_snapshots,
    _stamp,
)
from .wechat_html_export import normalize_html_clock


VERSION = "v1.1.0-codex.0"
_LOCAL_ID = re.compile(r"[1-9][0-9]*")
_SERVER_ID = re.compile(r"-?[1-9][0-9]*")
_HEX_ID = re.compile(r"[0-9a-f]{64}")


def _clock(raw, epoch=""):
    """Keep a literal wall clock separate from any explicitly asserted instant."""
    stamp = _stamp(raw, seconds=True)
    if stamp is None:
        value, precision = normalize_html_clock(raw)
        stamp = _stamp(value, seconds=True) if precision == "second" else None
    if stamp is None:
        raise ValueError("A complete packet needs each child's own second clock.")
    instant = int(stamp.timestamp()) if stamp.tzinfo is not None else None
    if epoch:
        if (
            not isinstance(epoch, str)
            or re.fullmatch(r"[1-9][0-9]{0,11}", epoch) is None
        ):
            raise ValueError("A packet asserts an invalid child epoch.")
        numeric = int(epoch)
        if instant is not None and instant != numeric:
            raise ValueError("A packet asserts contradictory child clocks.")
        instant = numeric
    return stamp.replace(tzinfo=None).isoformat(), instant


def _flat(parts, native, *, minimum=3):
    if not isinstance(parts, list) or not minimum <= len(parts) <= MAX_COPY_DETAILS:
        return None
    identifiers = []
    for part in parts:
        if not isinstance(part, dict):
            return None
        raw = part.get("source_data_id") if native else part.get("export_msgid")
        identifier = (
            raw if native else raw.rsplit("_", 1)[-1] if isinstance(raw, str) else None
        )
        if not isinstance(identifier, str) or _DATA_ID.fullmatch(identifier) is None:
            return None
        identifiers.append(identifier)
    return tuple(identifiers) if len(set(identifiers)) == len(identifiers) else None


def _text_part(part):
    return (
        isinstance(part.get("text"), str)
        and bool(part["text"].strip())
        and not any(
            part.get(key)
            for key in (
                "text_components",
                "forwarded_components",
                "quoted_components",
                "quoted_reference",
                "resource_reference",
                "omitted_forwarded_items",
            )
        )
    )


def _chat(part):
    first, second = part.get("source_chat_id", ""), part.get("original_chat_id", "")
    if first and second and first != second:
        raise ValueError("A packet asserts different original chats.")
    value = first or second
    if not isinstance(value, str) or any(
        character.isspace() or character == "\0" for character in value
    ):
        raise ValueError("A packet asserts an invalid original chat.")
    return value


def _native(row, proof, snapshots, thread_key):
    scope = _native_scope(row, proof, snapshots, thread_key)
    if (
        scope is None
        or _native_time(row, proof) is None
        or row["direction"] not in {"sent", "received"}
        or not _author(row["sender_id"])
        or (row["direction"] == "sent" and row["sender_id"] != row["account_id"])
        or type(proof.get("message_type")) is not int
        or proof["message_type"] != 49
        or proof.get("text_extraction_kind") != "forwarded_text"
        or proof.get("omitted_forwarded_items", 0) != 0
    ):
        raise ValueError("A packet has no verified native source.")
    parts = proof["text_components"]
    details = _native_packet(proof, row["id"], row["account_id"])
    if details is None or len(details) != len(parts):
        raise ValueError("A packet has unaccounted native children.")
    leaves = []
    for number, (part, detail) in enumerate(zip(parts, details), 1):
        if (
            part.get("kind") != "forwarded_text"
            or part.get("forwarded_item_path", [number]) != [number]
            or part.get("original_message_type", 1) != 1
            or type(part.get("original_message_type", 1)) is not int
            or detail["path"] != ["native", number]
            or not _text_part(part)
            or not _author(part.get("source_sender_id"))
            or detail["sender_id"] != part["source_sender_id"]
            or detail["text"] != part["text"]
            or detail.get("source_data_id") != part["source_data_id"]
        ):
            raise ValueError("A packet is not complete flat native text.")
        wall, instant = _clock(
            part.get("source_timestamp", ""), part.get("source_create_time", "")
        )
        projected = _stamp(detail["timestamp_value"], seconds=True)
        if (
            projected is None
            or (
                instant is not None
                and (projected.tzinfo is None or int(projected.timestamp()) != instant)
            )
            or (instant is None and projected.replace(tzinfo=None).isoformat() != wall)
        ):
            raise ValueError(
                "A native child disagrees with its projected source clock."
            )
        constraints = _copy_constraints(detail)
        if constraints is None:
            raise ValueError("A native child has contradictory identifiers.")
        leaves.append(
            {
                "id": detail["id"],
                "path": detail["path"],
                "author": detail["sender_id"],
                "body": detail["text"],
                "wall": wall,
                "instant": instant,
                "constraints": (constraints[0], constraints[1], _chat(part)),
            }
        )
    return leaves


def _html(row, proof, snapshots):
    owner = row["account_id"] or ""
    metadata = _metadata(row, proof, snapshots)
    locator = proof.get("locator", {})
    local = locator.get("export_msgid") if isinstance(locator, dict) else None
    if (
        not _author(owner)
        or row["account_key"] != "account:" + owner
        or row["container_account_key"] != row["account_key"]
        or row["identity_status"] != "Corroborated export ID"
        or metadata.get("owner_verified") is not True
        or metadata.get("source_account_id") != owner
        or not isinstance(metadata.get("owner_evidence"), dict)
        or metadata["owner_evidence"].get("native_username") != owner
        or not _author(proof.get("sender_native_id"))
        or row["direction"] not in {"sent", "received"}
        or (row["direction"] == "sent" and proof["sender_native_id"] != owner)
        or row["kind"] not in {"direct", "group"}
        or not isinstance(row["peer_id"], str)
        or not row["peer_id"]
        or any(character.isspace() or character == "\0" for character in row["peer_id"])
        or (row["kind"] == "group")
        != row["peer_id"].endswith(("@chatroom", "@im.chatroom"))
        or proof.get("text_extraction_kind")
        not in {"forwarded_text", "rendered_app_text"}
        or proof.get("omitted_forwarded_items", 0) != 0
        or proof.get("export_msgid_namespace") != "MesLocalID"
        or locator.get("export_msgid_namespace") != "MesLocalID"
        or str(locator.get("export_msgtype")) != "49"
        or not isinstance(local, str)
        or _LOCAL_ID.fullmatch(local) is None
    ):
        raise ValueError("A packet has no independently verified HTML owner.")
    leaves = []
    for number, part in enumerate(proof["forwarded_components"], 1):
        identifier = part["export_msgid"].rsplit("_", 1)[-1]
        if (
            part["export_msgid"] != local + "_" + identifier
            or part.get("text_kind") not in {"plain_text", "forwarded_rendered_text"}
            or str(part.get("type", "")) not in {"", "1"}
            or not _text_part(part)
            or not _author(part.get("sender_native_id"))
        ):
            raise ValueError("A packet is not complete flat HTML text.")
        wall, instant = _clock(
            part.get("raw_timestamp", ""), part.get("source_create_time", "")
        )
        server = _server_constraint(part)
        if server is False:
            raise ValueError("An HTML child asserts different original server IDs.")
        leaves.append(
            {
                "id": row["id"] + "_forwarded_" + str(number),
                "path": [number],
                "author": part["sender_native_id"],
                "body": part["text"],
                "wall": wall,
                "instant": instant,
                "constraints": (server or "", identifier, _chat(part)),
            }
        )
    return leaves


def _two_item_pairs(packets):
    """Require two native forwarding events and an exact HTML receipt for each.

    A format copy corroborates its parent observation, not a new original event.
    Short packets keep their stronger, source-local proof separate from the
    ordinary complete-packet path.
    """
    native = [packet for packet in packets if packet["native"]]
    html = [packet for packet in packets if not packet["native"]]
    if len(native) < 2 or len(native) != len(html):
        raise ValueError("A short packet needs one HTML receipt per native event.")
    scopes, servers, local_ids = set(), set(), set()
    for packet in native:
        row, proof = packet["row"], packet["proof"]
        locator = proof.get("locator")
        if row["kind"] != "group" or not isinstance(locator, dict):
            raise ValueError("A short packet has no verified receiving group.")
        server, local = proof.get("server_id"), locator.get("local_id")
        if (
            type(server) not in {str, int}
            or _SERVER_ID.fullmatch(str(server)) is None
            or type(local) not in {str, int}
            or _LOCAL_ID.fullmatch(str(local)) is None
            or locator.get("table")
            != "Chat_" + hashlib.md5(row["peer_id"].encode()).hexdigest()
        ):
            raise ValueError("A short packet has no independent parent identity.")
        scopes.add(
            (
                row["source_id"],
                proof["source_snapshot"],
                row["account_id"],
                row["peer_id"],
                row["sender_id"],
                row["direction"],
            )
        )
        servers.add(str(server))
        local_ids.add(str(local))
    if len(scopes) != 1 or len(servers) != len(native) or len(local_ids) != len(native):
        raise ValueError("Short packet parents do not prove distinct local events.")
    html_keys = {
        (
            packet["row"]["source_id"],
            packet["proof"]["source_snapshot"],
            packet["row"]["peer_id"],
            packet["proof"]["locator"]["export_msgid"],
        )
        for packet in html
    }
    if len(html_keys) != len(html):
        raise ValueError("A short packet reuses an HTML parent identity.")
    used = set()
    for packet in native:
        row = packet["row"]
        stamp = _stamp(row["timestamp_value"], seconds=True)
        matches = []
        for index, candidate in enumerate(html):
            other, proof = candidate["row"], candidate["proof"]
            clock = _stamp(other["timestamp_value"], seconds=True)
            if (
                clock is not None
                and stamp is not None
                and other["account_id"] == row["account_id"]
                and other["peer_id"] == row["peer_id"]
                and other["kind"] == row["kind"]
                and other["direction"] == row["direction"]
                and proof.get("sender_native_id") == row["sender_id"]
                and clock.replace(tzinfo=None) == stamp.replace(tzinfo=None)
                and (clock.tzinfo is None or clock == stamp)
            ):
                matches.append(index)
        if len(matches) != 1 or matches[0] in used:
            raise ValueError("A short packet has ambiguous or unmatched HTML receipts.")
        used.add(matches[0])
    if len(used) != len(html):
        raise ValueError("A short packet has an unpaired source observation.")


def _load(source, output, row_select, prepare_rows):
    cursor = output.execute(
        "SELECT * FROM refs ORDER BY id LIMIT ?",
        (MAX_COPY_DETAILS + 1,),
    )
    fields = [value[0] for value in cursor.description]
    references = [dict(zip(fields, value)) for value in cursor]
    parents = sorted(
        {
            value["parent_message_id"]
            for value in references
            if value["component_path"] != "[]"
        }
    )
    if len(references) > MAX_COPY_DETAILS or len(parents) > MAX_COPY_PARENTS:
        raise ValueError("Packet references exceed the bounded copy budget.")
    columns = {row[1] for row in source.execute("PRAGMA main.table_info(messages)")}
    byte_size = "+".join(
        "COALESCE(length(CAST(" + name + " AS BLOB)),0)"
        for name in ("text", "provenance", "search_text")
        if name in columns
    )
    rows, total = {}, 0
    for start in range(0, len(parents), 20):
        batch = parents[start : start + 20]
        marks = ",".join("?" for _ in batch)
        sizes = source.execute(
            "SELECT id,"
            + byte_size
            + " FROM main.messages WHERE id IN ("
            + marks
            + ")",
            batch,
        ).fetchall()
        if len(sizes) != len(batch):
            raise ValueError("An accepted packet parent is missing.")
        for _, size in sizes:
            total += size
            if size > MAX_COPY_PACKET_BYTES or total > MAX_COPY_SOURCE_BYTES:
                raise ValueError("Packet source bytes exceed the bounded copy budget.")
        values = [
            dict(row)
            for row in source.execute(
                row_select + "WHERE m.id IN (" + marks + ")", batch
            )
        ]
        prepare_rows(source, values)
        rows.update((row["id"], row) for row in values)
        if len(values) != len(batch):
            raise ValueError("An accepted packet lost its source partition.")
    by_parent = defaultdict(list)
    for reference in references:
        by_parent[reference["parent_message_id"]].append(reference)
    return rows, total, byte_size, by_parent


def _reference_matches(reference, row, leaf):
    return (
        reference["id"] == leaf["id"]
        and json.loads(reference["component_path"]) == leaf["path"]
        and reference["anchor_id"] == "wechat_message_" + leaf["id"]
        and all(
            reference[key] == row[key]
            for key in ("source_id", "account_key", "conversation_id")
        )
    )


def _group_members(output, group, budget):
    if group not in budget["members"]:
        cursor = output.execute(
            "SELECT r.* FROM refs r JOIN statement_members sm ON sm.reference_id=r.id "
            "WHERE sm.statement_id=? LIMIT ?",
            (group, MAX_COPY_DETAILS + 1),
        )
        fields = [value[0] for value in cursor.description]
        members = [dict(zip(fields, value)) for value in cursor]
        budget["member_count"] += len(members)
        if budget["member_count"] > MAX_COPY_DETAILS:
            raise ValueError("Packet cohorts exceed the bounded member budget.")
        count = output.execute(
            "SELECT COUNT(*) FROM statement_members WHERE statement_id=?", (group,)
        ).fetchone()[0]
        representative = output.execute(
            "SELECT reference_id FROM statement_groups WHERE id=?", (group,)
        ).fetchone()
        if (
            count != len(members)
            or representative is None
            or representative[0] not in {value["id"] for value in members}
        ):
            raise ValueError("A packet statement has incomplete prior membership.")
        budget["members"][group] = members
    return budget["members"][group]


def _plan(
    output,
    packets,
    preferred,
    rows,
    snapshots,
    thread_key,
    row_select,
    prepare_rows,
    budget,
):
    positions = []
    for packet in packets:
        accepted = {}
        by_id = {
            leaf["id"]: (number, leaf) for number, leaf in enumerate(packet["leaves"])
        }
        for reference in budget["by_parent"][packet["row"]["id"]]:
            found = by_id.get(reference["id"])
            if found is None or not _reference_matches(
                reference, packet["row"], found[1]
            ):
                raise ValueError(
                    "An accepted packet reference changed its source identity."
                )
            accepted[found[0]] = reference["id"]
        positions.append(accepted)
    if not positions[0] or any(set(value) != set(positions[0]) for value in positions):
        raise ValueError(
            "Complete packet copies need the same accepted contact positions."
        )
    plans, used_groups = [], set()
    packets_by_parent = {packet["row"]["id"]: packet for packet in packets}
    preference = (
        output.execute(
            "SELECT 1 FROM sqlite_temp_master WHERE type='table' AND name='source_preference'"
        ).fetchone()
        or output.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_preference'"
        ).fetchone()
    )
    for position in positions[0]:
        leaf = preferred["leaves"][position]
        references = [value[position] for value in positions]
        marks = ",".join("?" for _ in references)
        memberships = output.execute(
            "SELECT reference_id,statement_id FROM statement_members WHERE reference_id IN ("
            + marks
            + ")",
            references,
        ).fetchall()
        if len(memberships) != len(references):
            raise ValueError(
                "An accepted packet reference has no statement membership."
            )
        groups = {value[1] for value in memberships}
        if used_groups & groups:
            raise ValueError("Different packet positions share an existing statement.")
        used_groups.update(groups)
        constraints = [set(), {leaf["constraints"][1]}, set()]
        instants = {
            packet["leaves"][position]["instant"]
            for packet in packets
            if packet["leaves"][position]["instant"] is not None
        }
        candidates = []
        for group in groups:
            for reference in _group_members(output, group, budget):
                parent = rows.get(reference["parent_message_id"])
                if parent is None:
                    size = None
                    parent_value = (
                        budget["source"]
                        .execute(
                            "SELECT "
                            + budget["byte_size"]
                            + " FROM main.messages WHERE id=?",
                            (reference["parent_message_id"],),
                        )
                        .fetchone()
                    )
                    if parent_value is not None:
                        size = parent_value[0]
                    if (
                        size is None
                        or size > MAX_COPY_PACKET_BYTES
                        or budget["bytes"] + size > MAX_COPY_SOURCE_BYTES
                    ):
                        raise ValueError(
                            "An attached original exceeds the source budget."
                        )
                    budget["bytes"] += size
                    raw = (
                        budget["source"]
                        .execute(
                            row_select + "WHERE m.id=?",
                            (reference["parent_message_id"],),
                        )
                        .fetchone()
                    )
                    if raw is None:
                        raise ValueError("An attached original is missing.")
                    parent = dict(raw)
                    prepare_rows(budget["source"], [parent])
                    rows[parent["id"]] = parent
                proof = _object(parent["provenance"])
                packet = packets_by_parent.get(parent["id"])
                if packet is not None:
                    original = packet["leaves"][position]
                    if not _reference_matches(reference, parent, original):
                        raise ValueError(
                            "An attached packet member has a foreign role."
                        )
                    found = original["constraints"]
                    fallback = packet["rank"]
                elif reference["component_path"] == "[]":
                    raw_clock = proof.get("source_time")
                    clock = _clock(
                        parent["timestamp_value"] if raw_clock is None else raw_clock
                    )
                    if (
                        _native_scope(parent, proof, snapshots, thread_key) is None
                        or _native_time(parent, proof) is None
                        or proof.get("message_type") != 1
                        or proof.get("text_extraction_kind") != "plain_text"
                        or parent["sender_id"] != leaf["author"]
                        or parent["text"] != leaf["body"]
                        or clock[0] != leaf["wall"]
                        or any(
                            reference[key] != parent[key]
                            for key in ("source_id", "account_key", "conversation_id")
                        )
                        or reference["id"] != parent["id"]
                        or reference["anchor_id"] != "wechat_message_" + parent["id"]
                    ):
                        raise ValueError(
                            "An attached original has conflicting source evidence."
                        )
                    server = proof.get("server_id")
                    if (
                        type(server) not in {int, str}
                        or re.fullmatch(r"-?[1-9][0-9]*", str(server)) is None
                    ):
                        raise ValueError(
                            "An attached original has no server event identity."
                        )
                    instant = clock[1]
                    if instant is not None:
                        instants.add(instant)
                    if len(instants) > 1:
                        raise ValueError(
                            "An attached original asserts a different instant."
                        )
                    found, fallback = (str(server), "", parent["peer_id"]), 1
                else:
                    raise ValueError(
                        "A packet cohort contains an unproved external copy."
                    )
                for values, item in zip(constraints, found):
                    if item:
                        values.add(item)
                if any(len(values) > 1 for values in constraints):
                    raise ValueError(
                        "Packet copies contradict an existing original identity."
                    )
                rank = (
                    output.execute(
                        "SELECT rank FROM source_preference WHERE reference_id=?",
                        (reference["id"],),
                    ).fetchone()
                    if preference
                    else None
                )
                candidates.append(
                    (rank[0] if rank else fallback, reference["id"], group)
                )
        _, winner_ref, winner = min(candidates)
        chosen = output.execute(
            "SELECT conversation_key FROM statement_groups WHERE id=?", (winner,)
        ).fetchone()
        if chosen is None:
            raise ValueError("A packet statement group is missing.")
        plans.append((groups, winner, winner_ref))
    return plans


def link_identified_packet_copies(
    source, output, *, row_select, prepare_rows, native_thread_key
):
    """Union only complete opaque-ID packets, retaining all receiving observations."""
    if not source.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_snapshots'"
    ).fetchone():
        return
    try:
        rows, size, byte_size, by_parent = _load(
            source, output, row_select, prepare_rows
        )
        metadata_sizes = source.execute(
            "SELECT length(CAST(metadata AS BLOB)) FROM source_snapshots"
        ).fetchall()
        if (
            any(value[0] > MAX_COPY_PACKET_BYTES for value in metadata_sizes)
            or sum(value[0] for value in metadata_sizes) + size > MAX_COPY_SOURCE_BYTES
        ):
            return
        snapshots = _source_snapshots(source)
        cohorts, poisoned = defaultdict(list), set()
        details_count = 0
        for row in rows.values():
            if _HEX_ID.fullmatch(row["id"]) is None:
                continue
            proof = _object(row["provenance"])
            native = row["source_id"].startswith("ios-")
            if not native and not row["source_id"].startswith("html-"):
                continue
            parts = proof.get("text_components" if native else "forwarded_components")
            identity = _flat(parts, native, minimum=2)
            if identity is None:
                continue
            details_count += len(parts)
            if details_count > MAX_COPY_DETAILS:
                return
            try:
                leaves = (
                    _native(row, proof, snapshots, native_thread_key)
                    if native
                    else _html(row, proof, snapshots)
                )
                clocks = [datetime.fromisoformat(leaf["wall"]) for leaf in leaves]
                if not all(left < right for left, right in zip(clocks, clocks[1:])):
                    raise ValueError(
                        "A complete packet needs strictly increasing child clocks."
                    )
                rank = (
                    10
                    if native
                    and _metadata(row, proof, snapshots).get("source_label")
                    == "iPad archive"
                    else 11
                    if native
                    else 12
                )
                cohorts[identity].append(
                    {
                        "row": row,
                        "proof": proof,
                        "leaves": leaves,
                        "native": native,
                        "rank": rank,
                    }
                )
            except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
                poisoned.add(identity)
        plans, group_cohorts = {}, defaultdict(set)
        budget = {
            "bytes": size + sum(value[0] for value in metadata_sizes),
            "source": source,
            "byte_size": byte_size,
            "members": {},
            "member_count": 0,
            "by_parent": by_parent,
        }
        for identity, packets in cohorts.items():
            if (
                identity in poisoned
                or len(packets) < 2
                or not any(packet["native"] for packet in packets)
            ):
                continue
            try:
                preferred = min(
                    (packet for packet in packets if packet["native"]),
                    key=lambda packet: (packet["rank"], packet["row"]["id"]),
                )
                signature = [
                    (
                        leaf["author"],
                        hashlib.sha256(leaf["body"].encode()).hexdigest(),
                        leaf["wall"],
                    )
                    for leaf in preferred["leaves"]
                ]
                for packet in packets:
                    if [
                        (
                            leaf["author"],
                            hashlib.sha256(leaf["body"].encode()).hexdigest(),
                            leaf["wall"],
                        )
                        for leaf in packet["leaves"]
                    ] != signature:
                        raise ValueError(
                            "Opaque packet IDs assert different complete content."
                        )
                for position in range(len(identity)):
                    for name in ("instant",):
                        if (
                            len(
                                {
                                    packet["leaves"][position][name]
                                    for packet in packets
                                    if packet["leaves"][position][name] is not None
                                }
                            )
                            > 1
                        ):
                            raise ValueError(
                                "Opaque packet IDs assert different explicit instants."
                            )
                    for index in range(3):
                        if (
                            len(
                                {
                                    packet["leaves"][position]["constraints"][index]
                                    for packet in packets
                                    if packet["leaves"][position]["constraints"][index]
                                }
                            )
                            > 1
                        ):
                            raise ValueError(
                                "Opaque packet IDs assert different original identities."
                            )
                if len(identity) == 2:
                    _two_item_pairs(packets)
                updates = _plan(
                    output,
                    packets,
                    preferred,
                    rows,
                    snapshots,
                    native_thread_key,
                    row_select,
                    prepare_rows,
                    budget,
                )
                plans[identity] = updates
                for groups, _, _ in updates:
                    for group in groups:
                        group_cohorts[group].add(identity)
            except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
                continue
        collisions = {
            identity
            for values in group_cohorts.values()
            if len(values) > 1
            for identity in values
        }
    except (
        ValueError,
        TypeError,
        KeyError,
        RecursionError,
        OverflowError,
        sqlite3.DatabaseError,
    ):
        return
    output.execute("SAVEPOINT identified_packet_copies")
    try:
        for identity, updates in plans.items():
            if identity in collisions:
                continue
            for groups, winner, reference in updates:
                for group in groups - {winner}:
                    output.execute(
                        "UPDATE statement_members SET statement_id=? WHERE statement_id=?",
                        (winner, group),
                    )
                    output.execute("DELETE FROM statement_groups WHERE id=?", (group,))
                output.execute(
                    "UPDATE statement_groups SET reference_id=? WHERE id=?",
                    (reference, winner),
                )
        output.execute("RELEASE identified_packet_copies")
    except BaseException:
        output.execute("ROLLBACK TO identified_packet_copies")
        output.execute("RELEASE identified_packet_copies")
        raise
