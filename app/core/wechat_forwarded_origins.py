"""Link complete, corroborated transcript copies to their native event origins.

Code version: v1.0.3-codex.0
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


VERSION = "v1.0.3-codex.0"
_DATA_ID = re.compile(r"[0-9a-f]{32}")
_POSITIVE = re.compile(r"[1-9][0-9]*")
_SERVER_KEYS = {"svrid", "srcMsgSvrId", "dataitemsource/svrid"}


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
    return _object(snapshot["metadata"])


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
    pending, count, plain = [proof.get("text_components", [])], 0, 0
    while pending:
        values = pending.pop()
        if not isinstance(values, list):
            return None
        for component in values:
            count += 1
            if count > MAX_FORWARDED_COMPONENTS or not isinstance(component, dict):
                return None
            kind = component.get("kind")
            if kind == "forwarded_group":
                pending.append(component.get("text_components", []))
            elif kind == "forwarded_text":
                if "original_message_type" in component and (
                    type(component["original_message_type"]) is not int
                    or component["original_message_type"] != 1
                ):
                    return None
                plain += 1
            elif kind != "forwarded_resource":
                return None
    details = list(
        native_details(proof, parent_id=parent_id, account_id=owner, peer_id=peer_id)
    )
    return details if plain and len(details) == plain else None


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
    data_ids = [part.get("source_data_id") for part in details]
    if any(
        not isinstance(value, str) or _DATA_ID.fullmatch(value) is None
        for value in data_ids
    ) or len(set(data_ids)) != len(data_ids):
        return None
    links = []
    for (path, leaf), detail, data_id in zip(leaves, details, data_ids):
        child_stamp = _stamp(leaf.get("raw_timestamp"), seconds=True)
        native_stamp = _stamp(detail.get("timestamp_value"), seconds=True)
        if (
            leaf.get("export_msgid") != local + "_" + data_id
            or _author(leaf.get("sender_native_id")) != detail["sender_id"]
            or leaf["text"] != detail["text"]
            or child_stamp is None
            or native_stamp is None
            or child_stamp.replace(tzinfo=None) != native_stamp.replace(tzinfo=None)
            or (
                child_stamp.tzinfo is not None
                and native_stamp.tzinfo is not None
                and child_stamp != native_stamp
            )
        ):
            return None
        links.append((path, detail["id"]))
    return links


def link_forwarded_origins(
    source,
    output,
    contact_id,
    *,
    row_select,
    prepare_rows,
    native_thread_key,
    native_statement_key,
):
    """Only alter reference membership; never rewrite source observations or paths."""
    if not source.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_snapshots'"
    ).fetchone():
        return
    snapshots = {
        row["id"]: dict(row) for row in source.execute("SELECT * FROM source_snapshots")
    }
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
    primary_ids = output.execute(
        "SELECT parent_message_id FROM refs WHERE component_path='[]'"
    )
    while batch := primary_ids.fetchmany(200):
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
                    and row["kind"] == "direct"
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
    for _, _, details, scope in packets:
        authors = {_author(part.get("sender_id")) for part in details}
        if "" not in authors and len(authors) == 2 and scope[2] in authors:
            peer = next(author for author in authors if author != scope[2])
            wanted_keys[(scope, peer)].update(
                key for part in details if (key := _leaf_key(part)) is not None
            )
    proposed = defaultdict(set)
    for row, proof, details, scope in packets:
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
