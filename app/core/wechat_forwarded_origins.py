"""Link complete, corroborated transcript copies to their native event origins.

Code version: v1.3.3-codex.0
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import sqlite3

from .wechat_contact_embedded import native_details
from .wechat_contacts import MAX_FORWARDED_COMPONENTS, canonical_contact_id
from .wechat_forwarded import _epoch_placeholder_clock
from .wechat_text import _FORWARDED_RESOURCES


VERSION = "v1.3.3-codex.0"
_DATA_ID = re.compile(r"[0-9a-f]{32}")
_POSITIVE = re.compile(r"[1-9][0-9]*")
_SERVER_KEYS = {"svrid", "srcMsgSvrId", "dataitemsource/svrid", "dataitemsource/msgid"}
MAX_COPY_SEEDS = 200_000
MAX_COPY_COMPARISONS = 250_000
MAX_COPY_ALIGNMENTS = 50_000
MAX_COPY_PARENTS = 8_192
MAX_COPY_PACKET_BYTES = 16 * 1024 * 1024
MAX_COPY_SOURCE_BYTES = 256 * 1024 * 1024
MAX_COPY_DETAILS = 200_000


def _object(value):
    try:
        result = json.loads(value)
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def _author(value):
    try:
        canonical_contact_id(value, authority="native_message_sender")
        return value
    except (ValueError, TypeError):
        return ""


def _stamp(value, *, seconds=False):
    if not isinstance(value, str) or (
        seconds and re.search(r"[T ]\d{2}:\d{2}:\d{2}(?:[.+\-Z]|$)", value) is None
    ):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp if stamp.year > 1970 and not stamp.microsecond else None
    except ValueError:
        return None


def _native_time(row, proof):
    stamp = _stamp(row["timestamp_value"], seconds=True)
    if stamp is None or stamp.tzinfo is None:
        return None
    raw = proof.get("unix_timestamp")
    if type(raw) not in {int, str}:
        return None
    try:
        epoch = int(stamp.timestamp())
        if epoch <= 0 or Decimal(str(raw)) != epoch:
            return None
        raw_clock = proof.get("source_time")
        if raw_clock is not None:
            clock = _stamp(raw_clock, seconds=True)
            if (
                clock is None
                or stamp.replace(tzinfo=None) != clock.replace(tzinfo=None)
                or (clock.tzinfo is not None and stamp != clock)
            ):
                return None
        return epoch
    except (ValueError, OverflowError, InvalidOperation):
        return None


def _metadata(row, proof, snapshots):
    key = proof.get("source_snapshot")
    snapshot = snapshots.get(key) if type(key) is int and key > 0 else None
    if not snapshot or snapshot["source_id"] != row["source_id"]:
        return {}
    metadata = snapshot["metadata"]
    return metadata if isinstance(metadata, dict) else _object(metadata)


def _source_snapshots(source):
    """Decode each source's metadata once within this bounded linking pass."""
    return {
        row["id"]: {**dict(row), "metadata": _object(row["metadata"])}
        for row in source.execute("SELECT * FROM source_snapshots")
    }


def _native_scope(row, proof, snapshots, native_thread_key):
    owner = row["account_id"] or ""
    meta = _metadata(row, proof, snapshots)
    if (
        native_thread_key(row) is None
        or meta.get("source_account_id") != owner
        or meta.get("account_hash_verified") is not True
        or meta.get("account_hash") != hashlib.md5(owner.encode()).hexdigest()
    ):
        return None
    return row["source_id"], proof["source_snapshot"], owner


def _native_packet(proof, parent_id, owner, peer_id=""):
    if (
        proof.get("message_type") != 49
        or proof.get("text_extraction_kind") != "forwarded_text"
        or proof.get("appmsg_subtype") not in {None, 19}
    ):
        return None
    pending, count, plain = [(proof.get("text_components", []), ())], 0, 0
    components = {}
    while pending:
        values, prefix = pending.pop()
        if not isinstance(values, list):
            return None
        for number, component in enumerate(values, 1):
            count += 1
            if count > MAX_FORWARDED_COMPONENTS or not isinstance(component, dict):
                return None
            position = component.get("forwarded_item_path", [*prefix, number])
            if (
                not isinstance(position, list)
                or not position
                or any(type(part) is not int or part <= 0 for part in position)
            ):
                return None
            position = tuple(position)
            kind = component.get("kind")
            if kind == "forwarded_group":
                pending.append((component.get("text_components", []), position))
            elif kind == "forwarded_text":
                if "original_message_type" in component and (
                    type(component["original_message_type"]) is not int
                    or component["original_message_type"] != 1
                ):
                    return None
                if position in components:
                    return None
                components[position] = component
                plain += 1
            elif kind != "forwarded_resource":
                return None
    details = list(
        native_details(
            proof, parent_id=parent_id, account_id=owner, peer_id=peer_id
        )
    )
    raw_parts = proof.get("text_components", [])
    flat_text_only = (
        not proof.get("omitted_forwarded_items")
        and len(raw_parts) == len(details)
        and all(
            part.get("kind") == "forwarded_text"
            and not part.get("text_components")
            and not part.get("forwarded_components")
            for part in raw_parts
        )
    )
    flat_ordinal_ids = _flat_packet_ordinal_ids(proof, raw_parts, details)
    for detail in details:
        detail["_packet_flat_text_only"] = flat_text_only
        if flat_ordinal_ids is not None:
            detail["_packet_flat_ordinal_ids"] = flat_ordinal_ids
        component = components.get(tuple(detail["path"][1:]))
        if (
            component is not None
            and component.get("source_sender_id") == detail["sender_id"]
            and component.get("text") == detail["text"]
        ):
            detail["source_create_time"] = component.get("source_create_time", "")
    return details if plain and len(details) == plain else None


def _flat_packet_ordinal_ids(proof, parts, details):
    """Account for every flat packet position, including known body-free resources."""
    omitted = proof.get("omitted_forwarded_items", 0)
    if type(omitted) is not int or omitted != 0 or not parts or len(details) < 3:
        return None
    positions, data_ids = [], []
    for number, part in enumerate(parts, 1):
        if (
            part.get("forwarded_item_path") != [number]
            or part.get("source_data_id") != str(number - 1)
            or part.get("text_components")
            or part.get("forwarded_components")
            or part.get("quoted_components")
            or part.get("quoted_reference")
            or part.get("omitted_forwarded_items", 0) != 0
        ):
            return None
        if part["kind"] == "forwarded_text":
            positions.append(["native", number])
            data_ids.append(part["source_data_id"])
        elif part["kind"] == "forwarded_resource":
            kind = part.get("forwarded_data_type")
            if (
                part.get("text") != ""
                or type(kind) is not int
                or _FORWARDED_RESOURCES.get(kind) != part.get("resource_kind")
                or kind not in _FORWARDED_RESOURCES
                or part.get("original_message_type") == 1
            ):
                return None
        else:
            return None
    if positions != [detail["path"] for detail in details] or data_ids != [
        detail.get("source_data_id") for detail in details
    ]:
        return None
    return tuple(data_ids)


def _leaf_key(detail):
    stamp = _stamp(detail.get("timestamp_value"), seconds=True)
    if stamp is None or stamp.tzinfo is None or not _author(detail.get("sender_id")):
        return None
    return detail["sender_id"], detail["text"], int(stamp.timestamp())


def _server_constraint(detail):
    source_ids = detail.get("source_ids", {})
    if not isinstance(source_ids, dict):
        return False
    identifiers = []
    for key, value in source_ids.items():
        if key not in _SERVER_KEYS:
            continue
        if not isinstance(value, str) or re.fullmatch(r"-?[1-9][0-9]*", value) is None:
            return False
        identifiers.append(value)
    unique = set(identifiers)
    return False if len(unique) > 1 else next(iter(unique), None)


def _copy_constraints(detail):
    server = _server_constraint(detail)
    data = detail.get("source_data_id", "")
    chat = detail.get("original_chat_id", "")
    if (
        server is False
        or not isinstance(data, str)
        or (data and _DATA_ID.fullmatch(data) is None)
        or not isinstance(chat, str)
        or any(character.isspace() or character == "\0" for character in chat)
    ):
        return None
    return server or "", data, chat


def _copy_leaf(detail):
    """Use an exact source clock without claiming a missing timezone or thread."""
    stamp = _stamp(detail.get("timestamp_value"), seconds=True)
    raw = _stamp(detail.get("source_clock"), seconds=True)
    if (
        stamp is None
        or raw is None
        or stamp.replace(tzinfo=None) != raw.replace(tzinfo=None)
        or (stamp.tzinfo is not None and raw.tzinfo is not None and stamp != raw)
        or not _author(detail.get("sender_id"))
        or type(detail.get("original_message_type")) is not int
        or detail["original_message_type"] != 1
        or not isinstance(detail.get("text"), str)
        or not detail["text"].strip()
        or _copy_constraints(detail) is None
    ):
        return None
    return (
        detail["sender_id"],
        hashlib.sha256(detail["text"].encode()).hexdigest(),
        stamp.isoformat(),
        detail["source_clock"],
    )


def _adjacent_copy_parts(left, right):
    first, second = left["path"], right["path"]
    return (
        first[0] == second[0] == "native"
        and all(type(value) is int for value in (*first[1:], *second[1:]))
        and first[:-1] == second[:-1]
        and second[-1] == first[-1] + 1
    )


def _copy_overlap(first, second, offset):
    """Accept one unambiguous, complete overlap supported by several speakers."""
    left, right = first["details"], second["details"]
    begin, end = max(0, -offset), min(len(left), len(right) - offset)
    if end - begin < 3:
        return []
    pairs = [(left[index], right[index + offset]) for index in range(begin, end)]
    keys = [first["keys"][index] for index in range(begin, end)]
    if (
        any(
            key is None or key != second["keys"][index + offset]
            for index, key in zip(range(begin, end), keys)
        )
        or len({key[0] for key in keys}) < 2
        or any(first["counts"][key] != 1 or second["counts"][key] != 1 for key in keys)
        or any(
            not _adjacent_copy_parts(a[0], b[0]) or not _adjacent_copy_parts(a[1], b[1])
            for a, b in zip(pairs, pairs[1:])
        )
    ):
        return []
    stamps = [_stamp(key[2]) for key in keys]
    try:
        if not (
            all(a < b for a, b in zip(stamps, stamps[1:]))
            or all(a > b for a, b in zip(stamps, stamps[1:]))
        ):
            return []
    except TypeError:
        return []
    for a, b in pairs:
        if any(
            x and y and x != y
            for x, y in zip(_copy_constraints(a), _copy_constraints(b))
        ):
            return []
    return [(a["id"], b["id"]) for a, b in pairs]


def link_forwarded_copies(
    source, output, *, row_select, prepare_rows, native_thread_key
):
    """Group proven repeated transcript excerpts while retaining all receiving evidence.

    A single matching body and clock never establishes a copy. The proof requires
    an exact, unique multi-author sequence in the same verified source, snapshot,
    receiving conversation, and forwarding author. It creates no original thread.
    """
    if not source.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_snapshots'"
    ).fetchone():
        return
    snapshots = _source_snapshots(source)
    parents = [
        row[0]
        for row in output.execute(
            "SELECT DISTINCT parent_message_id FROM refs "
            "WHERE component_path LIKE '[\"native\",%' ORDER BY parent_message_id LIMIT ?",
            (MAX_COPY_PARENTS + 1,),
        )
    ]
    if len(parents) > MAX_COPY_PARENTS:
        return
    reference_ids = {
        row[0]
        for row in output.execute(
            "SELECT id FROM refs WHERE component_path LIKE '[\"native\",%' LIMIT ?",
            (MAX_COPY_DETAILS + 1,),
        )
    }
    if len(reference_ids) > MAX_COPY_DETAILS:
        return
    fields = {row[1] for row in source.execute("PRAGMA main.table_info(messages)")}
    byte_fields = ["provenance", "text"]
    if "search_text" in fields:
        byte_fields.append("search_text")
    byte_size = "+".join(
        "COALESCE(length(CAST(" + field + " AS BLOB)),0)" for field in byte_fields
    )
    packets, details_by_id = {}, {}
    source_bytes, detail_count = 0, 0
    for start in range(0, len(parents), 20):
        ids = parents[start : start + 20]
        marks = ",".join("?" for _ in ids)
        # SQLite can report BLOB lengths before Python materializes or decodes
        # the packet payload. No memberships change until collection succeeds.
        for _, size in source.execute(
            "SELECT id,"
            + byte_size
            + " FROM main.messages WHERE id IN ("
            + marks
            + ")",
            ids,
        ):
            source_bytes += size
            if size > MAX_COPY_PACKET_BYTES or source_bytes > MAX_COPY_SOURCE_BYTES:
                return
        rows = [
            dict(row)
            for row in source.execute(row_select + "WHERE m.id IN (" + marks + ")", ids)
        ]
        prepare_rows(source, rows)
        for row in rows:
            proof = _object(row["provenance"])
            native = _native_scope(row, proof, snapshots, native_thread_key)
            if (
                native is None
                or row["direction"] not in {"sent", "received"}
                or not _author(row["sender_id"])
                or _native_time(row, proof) is None
            ):
                continue
            details = _native_packet(proof, row["id"], native[2], row["peer_id"] or "")
            if details is None or len(details) < 3:
                continue
            detail_count += len(details)
            if detail_count > MAX_COPY_DETAILS:
                return
            keys = [_copy_leaf(detail) for detail in details]
            packets[row["id"]] = {
                "scope": (*native, row["conversation_id"], row["sender_id"]),
                "details": details,
                "keys": keys,
                "counts": Counter(keys),
            }
            details_by_id.update((detail["id"], detail) for detail in details)

    seeds, alignments = defaultdict(list), defaultdict(set)
    seed_count, comparisons = 0, 0
    for parent, packet in sorted(packets.items()):
        for index in range(len(packet["keys"]) - 2):
            keys = tuple(packet["keys"][index : index + 3])
            parts = packet["details"][index : index + 3]
            if (
                None in keys
                or len(set(keys)) != 3
                or len({key[0] for key in keys}) < 2
                or any(packet["counts"][key] != 1 for key in keys)
                or not all(_adjacent_copy_parts(a, b) for a, b in zip(parts, parts[1:]))
            ):
                continue
            seed = packet["scope"], keys
            seed_count += 1
            comparisons += len(seeds[seed])
            if seed_count > MAX_COPY_SEEDS or comparisons > MAX_COPY_COMPARISONS:
                # No memberships have changed yet. Excessively repeated inputs
                # remain independent instead of producing a partial proof.
                return
            for other, position in seeds[seed]:
                pair = other, parent
                if pair not in alignments and len(alignments) >= MAX_COPY_ALIGNMENTS:
                    return
                alignments[pair].add(index - position)
            seeds[seed].append((parent, index))

    constraint_cache = {}

    def constraints(statement):
        if statement in constraint_cache:
            return constraint_cache[statement]
        values = [set(), set(), set()]
        cursor = output.execute(
            "SELECT r.id,r.parent_message_id,r.component_path FROM refs r "
            "JOIN statement_members sm ON sm.reference_id=r.id WHERE sm.statement_id=?",
            (statement,),
        )
        primary = []
        for identifier, parent, path in cursor:
            if identifier in details_by_id:
                parts = _copy_constraints(details_by_id[identifier])
                if parts is None:
                    constraint_cache[statement] = None
                    return None
                for found, part in zip(values, parts):
                    if part:
                        found.add(part)
            elif path == "[]":
                primary.append(parent)
        for parent in primary:
            row = source.execute(row_select + "WHERE m.id=?", (parent,)).fetchone()
            if row is None:
                continue
            row = dict(row)
            prepare_rows(source, [row])
            proof = _object(row["provenance"])
            if _native_scope(row, proof, snapshots, native_thread_key):
                server = proof.get("server_id")
                if (
                    proof.get("message_type") != 1
                    or type(server) not in {int, str}
                    or re.fullmatch(r"-?[1-9][0-9]*", str(server)) is None
                ):
                    constraint_cache[statement] = None
                    return None
                values[0].add(str(server))
                values[2].add(row["peer_id"])
        result = values if all(len(value) <= 1 for value in values) else None
        constraint_cache[statement] = result
        return result

    for (first, second), offsets in sorted(alignments.items()):
        if len(offsets) != 1:
            continue
        links = _copy_overlap(packets[first], packets[second], next(iter(offsets)))
        for left, right in links:
            if left not in reference_ids or right not in reference_ids:
                continue
            groups = list(
                output.execute(
                    "SELECT sg.id,sg.reference_id,r.component_path FROM statement_members sm "
                    "JOIN statement_groups sg ON sg.id=sm.statement_id "
                    "JOIN refs r ON r.id=sg.reference_id WHERE sm.reference_id IN (?,?)",
                    (left, right),
                )
            )
            if len(groups) != 2 or groups[0][0] == groups[1][0]:
                continue
            winner, loser = sorted(
                groups, key=lambda group: (group[2] != "[]", group[1], group[0])
            )
            a, b = constraints(winner[0]), constraints(loser[0])
            if a is None or b is None or any(len(x | y) > 1 for x, y in zip(a, b)):
                continue
            output.execute(
                "UPDATE statement_members SET statement_id=? WHERE statement_id=?",
                (winner[0], loser[0]),
            )
            constraint_cache[winner[0]] = [x | y for x, y in zip(a, b)]
            constraint_cache.pop(loser[0], None)
    output.execute(
        "DELETE FROM statement_groups WHERE id NOT IN (SELECT statement_id FROM statement_members)"
    )


def _html_leaves(components, path=(), count=None):
    count = [0] if count is None else count
    if not isinstance(components, list) or len(path) > 32:
        raise ValueError("Unsupported HTML transcript shape.")
    for number, part in enumerate(components, 1):
        count[0] += 1
        if count[0] > MAX_FORWARDED_COMPONENTS or not isinstance(part, dict):
            raise ValueError("Unsupported HTML transcript shape.")
        position = (*path, number)
        children = part.get("forwarded_components", [])
        if children:
            if part.get("text") and part.get("text_kind") != "forwarded_text":
                raise ValueError("A transcript wrapper has its own unsupported text.")
            yield from _html_leaves(children, position, count)
            continue
        if part.get("text_kind") in {"resource_placeholder", "deferred"} or str(
            part.get("type", "")
        ) in {"3", "34", "43", "47", "62"}:
            continue
        if (
            part.get("text_kind") not in {"plain_text", "forwarded_rendered_text"}
            or part.get("quoted_components")
            or not isinstance(part.get("text"), str)
            or not part["text"].strip()
        ):
            raise ValueError("A transcript has unsupported text roles.")
        yield position, part


def _bridge_component_clock(raw, native_value):
    """Require the native child's own clock and preserve unavailable HTML clocks."""
    native = _stamp(native_value, seconds=True)
    if native is None:
        return False
    if raw is None or (
        isinstance(raw, str) and (not raw.strip() or _epoch_placeholder_clock(raw))
    ):
        return True
    child = _stamp(raw, seconds=True)
    return (
        child is not None
        and child.replace(tzinfo=None) == native.replace(tzinfo=None)
        and (child.tzinfo is None or native.tzinfo is None or child == native)
    )


def _bridge_packet_data_ids(details):
    """Accept decimal text positions only inside one fully accounted flat packet."""
    data_ids = [part.get("source_data_id") for part in details]
    if all(
        isinstance(value, str) and _DATA_ID.fullmatch(value) is not None
        for value in data_ids
    ) and len(set(data_ids)) == len(data_ids):
        return data_ids
    if len(details) < 3:
        return None
    if all(
        part.get("_packet_flat_text_only") is True
        and part.get("path") == ["native", number]
        and data_id == str(number - 1)
        for number, (part, data_id) in enumerate(zip(details, data_ids), start=1)
    ):
        return data_ids
    ordinal_ids = details[0].get("_packet_flat_ordinal_ids")
    if (
        not isinstance(ordinal_ids, tuple)
        or tuple(data_ids) != ordinal_ids
        or any(
            part.get("_packet_flat_ordinal_ids") != ordinal_ids
            or not isinstance(data_id, str)
            or re.fullmatch(r"0|[1-9][0-9]*", data_id) is None
            or part.get("path") != ["native", int(data_id) + 1]
            for part, data_id in zip(details, data_ids)
        )
    ):
        return None
    return data_ids


def _packet_bridge(html, native, snapshots):
    row, proof = html
    original, native_proof, details, scope = native
    owner = scope[2]
    meta = _metadata(row, proof, snapshots)
    locator = proof.get("locator", {})
    local = locator.get("export_msgid") if isinstance(locator, dict) else None
    if (
        row["account_id"] != owner
        or row["account_key"] != "account:" + owner
        or row["container_account_key"] != row["account_key"]
        or row["identity_status"] != "Corroborated export ID"
        or not row["source_id"].startswith("html-")
        or meta.get("owner_verified") is not True
        or meta.get("source_account_id") != owner
        or not isinstance(meta.get("owner_evidence"), dict)
        or meta["owner_evidence"].get("native_username") != owner
        or row["peer_id"] != original["peer_id"]
        or row["kind"] != original["kind"]
        or row["direction"] != original["direction"]
        or row["direction"] not in {"sent", "received"}
        or _author(proof.get("sender_native_id")) != _author(original["sender_id"])
        or not _author(original["sender_id"])
        or proof.get("export_msgid_namespace") != "MesLocalID"
        or locator.get("export_msgid_namespace") != "MesLocalID"
        or not isinstance(local, str)
        or _POSITIVE.fullmatch(local) is None
    ):
        return None
    first = _stamp(row["timestamp_value"], seconds=True)
    second = _stamp(original["timestamp_value"], seconds=True)
    if (
        first is None
        or second is None
        or first.replace(tzinfo=None) != second.replace(tzinfo=None)
        or (first.tzinfo is not None and second.tzinfo is not None and first != second)
        or _native_time(original, native_proof) is None
    ):
        return None
    try:
        leaves = list(_html_leaves(proof.get("forwarded_components")))
    except (ValueError, TypeError, RecursionError):
        return None
    if len(leaves) != len(details) or len(leaves) < 3:
        return None
    data_ids = _bridge_packet_data_ids(details)
    if data_ids is None:
        return None
    links = []
    for (path, leaf), detail, data_id in zip(leaves, details, data_ids):
        if (
            leaf.get("export_msgid") != local + "_" + data_id
            or _author(leaf.get("sender_native_id")) != detail["sender_id"]
            or leaf["text"] != detail["text"]
            or not _bridge_component_clock(
                leaf.get("raw_timestamp"), detail.get("timestamp_value")
            )
        ):
            return None
        links.append((path, detail["id"]))
    return links


def _explicit_child_clock_matches(detail, original, epoch):
    """Use the child's own full clock and reject contradictory numeric source time."""
    stamp = _stamp(detail.get("timestamp_value"), seconds=True)
    raw = _stamp(detail.get("source_clock"), seconds=True)
    native = _stamp(original["timestamp_value"], seconds=True)
    if stamp is None or raw is None or native is None or native.tzinfo is None:
        return False
    if any(
        clock.replace(tzinfo=None) != native.replace(tzinfo=None)
        or clock.tzinfo is not None
        and clock != native
        for clock in (stamp, raw)
    ):
        return False
    if "source_create_time" not in detail:
        return False
    numeric = detail["source_create_time"]
    return numeric == "" or (
        isinstance(numeric, str)
        and re.fullmatch(r"[1-9][0-9]{0,11}", numeric) is not None
        and int(numeric) == epoch
    )


def _link_explicit_server_origins(
    source,
    output,
    contact_id,
    packets,
    refs,
    snapshots,
    *,
    row_select,
    prepare_rows,
    native_thread_key,
    native_statement_key,
):
    """Resolve asserted child server IDs only against already accepted native originals.

    The packet's receiving conversation and parent server ID are not the child's
    original identity. A contradictory or ambiguous original remains independent.
    """
    wanted = {
        (scope[2], server)
        for _, _, details, scope in packets
        for detail in details
        if detail["id"] in refs
        and (server := _server_constraint(detail)) not in {None, False}
    }
    if not wanted:
        return
    events = defaultdict(lambda: defaultdict(list))
    originals = output.execute(
        "SELECT id,parent_message_id,source_id,account_key FROM refs "
        "WHERE component_path='[]'"
    )
    while batch := originals.fetchmany(200):
        by_parent = {row[1]: row for row in batch}
        marks = ",".join("?" for _ in by_parent)
        rows = [
            dict(row)
            for row in source.execute(
                row_select + "WHERE m.id IN (" + marks + ")", list(by_parent)
            )
        ]
        prepare_rows(source, rows)
        for row in rows:
            reference = by_parent[row["id"]]
            proof = _object(row["provenance"])
            scope = _native_scope(row, proof, snapshots, native_thread_key)
            server = proof.get("server_id")
            if (
                scope is None
                or reference[0] != reference[1]
                or reference[2:] != (row["source_id"], row["account_key"])
                or type(server) not in {str, int}
                or re.fullmatch(r"-?[1-9][0-9]*", str(server)) is None
                or (scope[2], str(server)) not in wanted
                or type(proof.get("message_type")) is not int
                or proof["message_type"] != 1
                or proof.get("text_extraction_kind") != "plain_text"
                or row["direction"] not in {"sent", "received"}
                or not _author(row["sender_id"])
            ):
                continue
            epoch = _native_time(row, proof)
            statement = native_statement_key(row, contact_id, native_thread_key(row))
            if epoch is None or statement is None:
                continue
            # Establish uniqueness before filtering a candidate's body or clock.
            key = (statement, row["sender_id"], row["text"], epoch, row["peer_id"])
            events[(scope[2], str(server))][key].append((reference[0], row, epoch))

    proposals = defaultdict(set)
    for _, _, details, scope in packets:
        for detail in details:
            if detail["id"] not in refs:
                continue
            server = _server_constraint(detail)
            candidates = events.get((scope[2], server), {})
            if server in {None, False} or len(candidates) != 1:
                continue
            if (
                canonical_contact_id(
                    detail["sender_id"], authority="native_message_sender"
                )
                != contact_id
            ):
                continue
            matching = [
                reference
                for reference, original, epoch in next(iter(candidates.values()))
                if detail["sender_id"] == original["sender_id"]
                and detail["text"] == original["text"]
                and (
                    not detail.get("original_chat_id")
                    or detail["original_chat_id"] == original["peer_id"]
                )
                and _explicit_child_clock_matches(detail, original, epoch)
            ]
            if not matching:
                continue
            statement = output.execute(
                "SELECT statement_id FROM statement_members WHERE reference_id=?",
                (detail["id"],),
            ).fetchone()
            targets = {
                row[0]
                for reference in matching
                for row in output.execute(
                    "SELECT statement_id FROM statement_members WHERE reference_id=?",
                    (reference,),
                )
            }
            if statement and len(targets) == 1:
                proposals[statement[0]].update(targets)
    for statement, targets in proposals.items():
        if len(targets) != 1:
            continue
        target = next(iter(targets))
        if (
            statement == target
            or output.execute(
                "SELECT 1 FROM refs r JOIN statement_members sm ON sm.reference_id=r.id "
                "WHERE sm.statement_id=? AND r.component_path='[]' "
                "AND r.source_id GLOB 'ios-*' LIMIT 1",
                (statement,),
            ).fetchone()
        ):
            continue
        output.execute(
            "UPDATE statement_members SET statement_id=? WHERE statement_id=?",
            (target, statement),
        )


def link_forwarded_origins(
    source,
    output,
    contact_id,
    *,
    row_select,
    prepare_rows,
    native_thread_key,
    native_statement_key,
    link_originals=True,
):
    """Only alter reference membership; never rewrite source observations or paths.

    A compatible reference-cache upgrade can retain its proven original links and
    replay only the HTML/native packet bridges without rescanning direct messages.
    """
    if not source.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_snapshots'"
    ).fetchone():
        return
    snapshots = _source_snapshots(source)
    reference_rows = output.execute(
        "SELECT r.id,r.parent_message_id,r.component_path,sm.statement_id "
        "FROM refs r JOIN statement_members sm ON sm.reference_id=r.id "
        "WHERE r.component_path!='[]'"
    )
    reference_rows.row_factory = sqlite3.Row
    refs = {row["id"]: dict(row) for row in reference_rows}
    if not refs:
        return
    packets, original_scopes, html_parents = [], defaultdict(set), []
    primary_ids = (
        output.execute("SELECT parent_message_id FROM refs WHERE component_path='[]'")
        if link_originals
        else None
    )
    while primary_ids is not None and (batch := primary_ids.fetchmany(200)):
        ids = [value[0] for value in batch]
        marks = ",".join("?" for _ in ids)
        rows = [
            dict(row)
            for row in source.execute(row_select + "WHERE m.id IN (" + marks + ")", ids)
        ]
        prepare_rows(source, rows)
        for row in rows:
            proof = _object(row["provenance"])
            scope = _native_scope(row, proof, snapshots, native_thread_key)
            if (
                scope
                and row["kind"] == "direct"
                and proof.get("message_type") == 1
                and proof.get("text_extraction_kind") == "plain_text"
                and _native_time(row, proof) is not None
                and native_statement_key(row, contact_id, native_thread_key(row))
            ):
                original_scopes[(scope, row["peer_id"])].add(row["conversation_id"])

    parent_ids = sorted({row["parent_message_id"] for row in refs.values()})
    for start in range(0, len(parent_ids), 20):
        ids = parent_ids[start : start + 20]
        marks = ",".join("?" for _ in ids)
        rows = [
            dict(row)
            for row in source.execute(row_select + "WHERE m.id IN (" + marks + ")", ids)
        ]
        prepare_rows(source, rows)
        for row in rows:
            proof = _object(row["provenance"])
            scope = _native_scope(row, proof, snapshots, native_thread_key)
            if scope:
                details = _native_packet(proof, row["id"], scope[2], row["peer_id"] or "")
                if (
                    details
                    and row["kind"] in {"direct", "group"}
                    and _native_time(row, proof) is not None
                ):
                    compact = {
                        key: value
                        for key, value in row.items()
                        if key not in {"text", "search_text", "provenance"}
                    }
                    packets.append(
                        (
                            compact,
                            {"unix_timestamp": proof["unix_timestamp"]},
                            details,
                            scope,
                        )
                    )
            elif row["source_id"].startswith("html-") and proof.get(
                "forwarded_components"
            ):
                compact = {
                    key: value
                    for key, value in row.items()
                    if key not in {"text", "search_text", "provenance"}
                }
                html_parents.append((compact, proof))

    original_cache = {}
    wanted_keys = defaultdict(set)
    original_packets = packets if link_originals else ()
    for row, _, details, scope in original_packets:
        if row["kind"] != "direct":
            continue
        authors = {_author(part.get("sender_id")) for part in details}
        if "" not in authors and len(authors) == 2 and scope[2] in authors:
            peer = next(author for author in authors if author != scope[2])
            wanted_keys[(scope, peer)].update(
                key for part in details if (key := _leaf_key(part)) is not None
            )
    proposed = defaultdict(set)
    for row, proof, details, scope in original_packets:
        if row["kind"] != "direct":
            continue
        authors = {_author(part.get("sender_id")) for part in details}
        if "" in authors or len(authors) != 2 or scope[2] not in authors:
            continue
        peer = next(author for author in authors if author != scope[2])
        cache_key = scope, peer
        if cache_key not in original_cache:
            events = defaultdict(dict)
            for conversation in sorted(original_scopes.get(cache_key, ())):
                cursor = source.execute(
                    row_select + "WHERE m.conversation_id=?", (conversation,)
                )
                while batch := cursor.fetchmany(200):
                    originals = [dict(value) for value in batch]
                    prepare_rows(source, originals)
                    for original in originals:
                        native_proof = _object(original["provenance"])
                        if (
                            _native_scope(
                                original, native_proof, snapshots, native_thread_key
                            )
                            != scope
                            or original["kind"] != "direct"
                            or original["peer_id"] != peer
                            or native_proof.get("message_type") != 1
                            or native_proof.get("text_extraction_kind") != "plain_text"
                            or original["sender_id"] not in authors
                        ):
                            continue
                        epoch = _native_time(original, native_proof)
                        statement = native_statement_key(
                            original, contact_id, native_thread_key(original)
                        )
                        if epoch is None or statement is None:
                            continue
                        key = original["sender_id"], original["text"], epoch
                        if key not in wanted_keys[cache_key]:
                            continue
                        events[key][statement] = (
                            statement,
                            original["ordinal"],
                            str(native_proof["server_id"]),
                            original["sender_id"],
                            original["peer_id"],
                            original["id"],
                        )
            original_cache[cache_key] = events
        events = original_cache[cache_key]
        multiplicity = Counter(_leaf_key(part) for part in details)
        matching = []
        for detail in details:
            key = _leaf_key(detail)
            candidates = events.get(key, {})
            constraint = _server_constraint(detail)
            if (
                key is None
                or multiplicity[key] != 1
                or len(candidates) != 1
                or constraint is False
            ):
                continue
            event = next(iter(candidates.values()))
            if (constraint and constraint != event[2]) or (
                detail.get("original_chat_id")
                and detail["original_chat_id"] != event[4]
            ):
                continue
            matching.append((detail, event))
        orders = [event[1] for _, event in matching]
        if (
            len({event[2] for _, event in matching}) < 3
            or {event[3] for _, event in matching} != authors
            or not (
                all(a < b for a, b in zip(orders, orders[1:]))
                or all(a > b for a, b in zip(orders, orders[1:]))
            )
        ):
            continue
        for detail, event in matching:
            if detail["id"] in refs:
                primary = output.execute(
                    "SELECT statement_id FROM statement_members WHERE reference_id=?",
                    (event[5],),
                ).fetchone()
                if primary:
                    proposed[detail["id"]].add(primary[0])
    for reference, statements in proposed.items():
        if len(statements) == 1:
            output.execute(
                "UPDATE statement_members SET statement_id=? WHERE reference_id=?",
                (next(iter(statements)), reference),
            )

    _link_explicit_server_origins(
        source,
        output,
        contact_id,
        packets,
        refs,
        snapshots,
        row_select=row_select,
        prepare_rows=prepare_rows,
        native_thread_key=native_thread_key,
        native_statement_key=native_statement_key,
    )

    bridges = defaultdict(set)
    for html in html_parents:
        for native in packets:
            links = _packet_bridge(html, native, snapshots)
            if links is None:
                continue
            parent_id = html[0]["id"]
            by_path = {
                tuple(json.loads(ref["component_path"])): ref["id"]
                for ref in refs.values()
                if ref["parent_message_id"] == parent_id
            }
            for path, native_reference in links:
                html_reference = by_path.get(path)
                if html_reference is not None and native_reference in refs:
                    target = output.execute(
                        "SELECT statement_id FROM statement_members WHERE reference_id=?",
                        (native_reference,),
                    ).fetchone()
                    if target:
                        bridges[html_reference].add(target[0])
    for reference, statements in bridges.items():
        if len(statements) == 1:
            output.execute(
                "UPDATE statement_members SET statement_id=? WHERE reference_id=?",
                (next(iter(statements)), reference),
            )
    output.execute(
        "DELETE FROM statement_groups WHERE id NOT IN (SELECT statement_id FROM statement_members)"
    )
