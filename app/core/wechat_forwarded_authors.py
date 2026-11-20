"""Recover missing native authors from pinned complete transcript copies.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .wechat_archive import wechat_archive_path
from .wechat_contact_embedded import _readonly
from .wechat_forwarded_clocks import (
    _ProjectionContext,
    _SELECT,
    _native_clock,
    _native_identity,
    _native_parts,
    _path_identity,
)
from .wechat_forwarded_origins import _author, _copy_constraints, _native_packet


VERSION = "v1.0.0-codex.0"
MAX_REGISTRY_BYTES = 1024 * 1024
MAX_LINKS = 256
MAX_ROWS = 2048
_HEX = re.compile(r"[0-9a-f]{64}")
_DATA = re.compile(r"[0-9a-f]{32}")
_PIN_KEYS = tuple(
    prefix + "_" + field + "_sha256"
    for prefix in ("target", "native")
    for field in ("text", "provenance")
)
_LINK_KEYS = ("target_message_id", "native_message_id", *_PIN_KEYS)


@dataclass(frozen=True)
class _Registry:
    path: Path
    identity: tuple
    digest: str
    links: dict


def _registry(root):
    path = wechat_archive_path(root).parent / "forwarded_author_links.json"
    before = _path_identity(path, root)
    if before[2] > MAX_REGISTRY_BYTES:
        raise ValueError("Forwarded author registry exceeds its supported bounds.")
    raw = path.read_bytes()
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or not isinstance(value.get("links"), list)
        or len(value["links"]) > MAX_LINKS
    ):
        raise ValueError("Invalid forwarded author registry.")
    links = {}
    for link in value["links"]:
        if (
            not isinstance(link, dict)
            or any(
                not isinstance(link.get(key), str)
                or not _HEX.fullmatch(link[key])
                for key in _LINK_KEYS
            )
            or link["target_message_id"] == link["native_message_id"]
            or link["target_message_id"] in links
        ):
            raise ValueError("Ambiguous or invalid forwarded author pointer.")
        links[link["target_message_id"]] = {
            key: link[key] for key in _LINK_KEYS
        }
    if _path_identity(path, root) != before:
        raise ValueError("Forwarded author registry changed while reading.")
    return _Registry(path, before, hashlib.sha256(raw).hexdigest(), links)


def registry_digest(root):
    """Fingerprint available local evidence without hydrating cloud placeholders."""
    try:
        return _registry(Path(root).expanduser().absolute()).digest
    except (OSError, ValueError, TypeError, RecursionError):
        return ""


def _receiving_context(row):
    owner, sender, peer = row["account_id"], row["sender_id"], row["peer_id"]
    room = isinstance(peer, str) and peer.endswith(("@chatroom", "@im.chatroom"))
    if (
        not _author(owner)
        or not _author(sender)
        or row["kind"] not in {"group", "direct"}
        or not isinstance(peer, str)
        or row["direction"] != ("sent" if sender == owner else "received")
        or (row["kind"] == "group" and not room)
        or (
            row["kind"] == "direct"
            and (
                room
                or not _author(peer)
                or sender not in {owner, peer}
                or row["direction"] != ("sent" if sender == owner else "received")
            )
        )
    ):
        raise ValueError("Forwarded author packet has an invalid receiving context.")


def _packet(connection, row, proof, context):
    owner, snapshot, metadata = _native_identity(connection, row, proof, context)
    if snapshot["source_id"] != row["source_id"]:
        raise ValueError("Forwarded author snapshot differs from its partition.")
    _receiving_context(row)
    text_parts = _native_parts({**proof, "_expected_text": row["text"]})
    parts = proof["text_components"]
    if (
        len(parts) < 3
        or len(parts) != len(text_parts)
        or not all(_DATA.fullmatch(part["source_data_id"]) for part in parts)
    ):
        raise ValueError("Author recovery requires a complete opaque text packet.")
    return owner, snapshot, metadata, parts


def _link(connection, target, native, link, digest, context):
    for prefix, row in (("target", target), ("native", native)):
        for field in ("text", "provenance"):
            if context.sha(row, field) != link[prefix + "_" + field + "_sha256"]:
                raise ValueError("Pinned forwarded author source bytes changed.")
    target_proof, native_proof = context.proof(target), context.proof(native)
    target_owner, target_snapshot, _, target_parts = _packet(
        connection, target, target_proof, context
    )
    native_owner, native_snapshot, native_meta, native_parts = _packet(
        connection, native, native_proof, context
    )
    if (
        len(target_parts) != len(native_parts)
        or not all(_author(part.get("source_sender_id")) for part in native_parts)
        or len({part["source_sender_id"] for part in native_parts}) < 2
    ):
        raise ValueError("Native counterpart does not identify the whole transcript.")
    projected_target = deepcopy(target_proof)
    for target_part, native_part, projected_part in zip(
        target_parts, native_parts, projected_target["text_components"]
    ):
        if (
            target_part["source_data_id"] != native_part["source_data_id"]
            or target_part["kind"] != native_part["kind"]
            or target_part.get("text") != native_part.get("text")
            or not isinstance(target_part.get("source_sender_id", ""), str)
            or (
                target_part.get("source_sender_id")
                and target_part["source_sender_id"] != native_part["source_sender_id"]
            )
        ):
            raise ValueError("Forwarded author packet identity or ordering differs.")
        # This temporary tree enables the existing detail decoder to inspect the
        # target's own clocks. Source provenance and timestamps are never changed.
        projected_part["source_sender_id"] = native_part["source_sender_id"]
    target_details = _native_packet(
        projected_target, target["id"], target_owner, target["peer_id"]
    )
    native_details = _native_packet(
        native_proof, native["id"], native_owner, native["peer_id"]
    )
    if (
        target_details is None
        or native_details is None
        or len(target_details) != len(target_parts)
        or len(native_details) != len(native_parts)
    ):
        raise ValueError("Forwarded author packet has unaccounted text leaves.")
    result = {}
    for number, (target_part, native_part, target_detail, native_detail) in enumerate(
        zip(target_parts, native_parts, target_details, native_details), 1
    ):
        target_clock = _native_clock(target_part, target_detail)
        native_clock = _native_clock(native_part, native_detail)
        target_constraints = _copy_constraints(target_detail)
        native_constraints = _copy_constraints(native_detail)
        if (
            target_clock.tzinfo is None
            or native_clock.tzinfo is None
            or target_clock != native_clock
            or target_constraints is None
            or native_constraints is None
            or any(
                first and second and first != second
                for first, second in zip(target_constraints, native_constraints)
            )
        ):
            raise ValueError("Forwarded author clocks or original identifiers differ.")
        if target_part.get("source_sender_id"):
            continue
        result[(number,)] = {
            "sender_id": native_detail["sender_id"],
            "identity_basis": "verified_complete_native_transcript_copy",
            "evidence": {
                "version": VERSION,
                "registry_sha256": digest,
                **link,
                "target_component_path": [number],
                "native_component_path": native_detail["path"],
                "native_reference_id": native_detail["id"],
                "target_source_id": target_snapshot["source_id"],
                "target_source_snapshot": target_proof["source_snapshot"],
                "native_source_id": native_snapshot["source_id"],
                "native_source_snapshot": native_proof["source_snapshot"],
                "native_source_snapshot_id": native_snapshot["snapshot_id"],
                "native_source_label": native_meta.get("source_label", ""),
                "source_data_id": native_part["source_data_id"],
                "body_sha256": hashlib.sha256(native_detail["text"].encode()).hexdigest(),
                "sender_id": native_detail["sender_id"],
                "target_source_sender_id": target_part.get("source_sender_id", ""),
                "timestamp_value": target_detail["timestamp_value"],
                "match_basis": "complete_opaque_native_packet_copy",
            },
        }
    return result


class WechatForwardedAuthors:
    """Resolve bounded native evidence pointers without changing source records."""

    def __init__(self, root):
        self.root = Path(root).expanduser().absolute()

    def linked_rows(self, connection):
        """Read only explicitly pinned target parents through the primary key."""
        try:
            registry = _registry(self.root)
            rows = []
            for identifier in sorted(registry.links):
                row = connection.execute(_SELECT, (identifier,)).fetchone()
                if row is not None:
                    rows.append(dict(row))
            if _path_identity(registry.path, self.root) != registry.identity:
                return []
            return rows
        except (OSError, ValueError, TypeError, RecursionError, sqlite3.DatabaseError):
            return []

    def project(self, rows):
        try:
            if not isinstance(rows, (list, tuple)) or len(rows) > MAX_ROWS:
                return {}
            supplied, duplicate = {}, set()
            for value in rows:
                row = dict(value)
                identifier = row.get("id")
                if not isinstance(identifier, str) or not _HEX.fullmatch(identifier):
                    continue
                if identifier in supplied:
                    duplicate.add(identifier)
                else:
                    supplied[identifier] = row
            for identifier in duplicate:
                supplied.pop(identifier, None)
            registry = _registry(self.root)
            links = [link for target, link in registry.links.items() if target in supplied]
            if not links:
                return {}
            history = wechat_archive_path(self.root)
            before = _path_identity(history, self.root)
            result = {}
            with closing(_readonly(history)) as connection:
                context = _ProjectionContext(connection)
                for link in links:
                    try:
                        target, native = (
                            context.row(link[key])
                            for key in ("target_message_id", "native_message_id")
                        )
                        caller = supplied[target["id"]]
                        if any(
                            field in caller and caller[field] != target[field]
                            for field in ("text", "provenance")
                        ):
                            raise ValueError("Caller author source differs from the archive.")
                        resolved = _link(
                            connection, target, native, link, registry.digest, context
                        )
                        if resolved:
                            result[target["id"]] = resolved
                    except (
                        ValueError, TypeError, KeyError, AttributeError,
                        RecursionError, OverflowError,
                    ):
                        continue
            if (
                _path_identity(history, self.root) != before
                or _path_identity(registry.path, self.root) != registry.identity
            ):
                return {}
            return result
        except (
            OSError, ValueError, TypeError, KeyError, AttributeError,
            RecursionError, sqlite3.DatabaseError,
        ):
            return {}
