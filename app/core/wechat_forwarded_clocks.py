"""Project pinned native child clocks without rewriting HTML source observations.

Code version: v1.1.1-codex.0
"""

from __future__ import annotations

from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from threading import Lock

from .wechat_archive import _identifier, wechat_archive_path
from .wechat_contact_embedded import _identity, _readonly
from .wechat_contacts import WechatContactRepository, _forwarded_source_members
from .wechat_forwarded import _epoch_placeholder_clock, project_html_forwarded
from .wechat_forwarded_origins import _author, _native_packet, _native_time, _stamp
from .wechat_html_export import normalize_html_clock
from .wechat_text import _FORWARDED_RESOURCES, native_components_text


VERSION = "v1.1.1-codex.0"
MAX_REGISTRY_BYTES = 1024 * 1024
MAX_LINKS = 256
MAX_INDEXED_REGISTRY_BYTES = 512 * 1024 * 1024
MAX_INDEXED_LINKS = 500_000
MAX_FILTER_CANDIDATES = 2_000_000
MAX_REGISTRY_INFO_BYTES = 64 * 1024
MAX_ROWS = 2048
MAX_ROW_BYTES = 4 * 1024 * 1024
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_COMPONENTS = 1000
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
_HEX = re.compile(r"[0-9a-f]{64}")
_DATA = re.compile(r"[0-9a-f]{32}")
_SELECT = (
    "SELECT m.*,c.partition_id AS source_id,c.account_key,c.peer_id,c.kind,"
    "p.account_id,p.identity_status,p.account_key AS container_account_key "
    "FROM messages m JOIN conversations c ON c.id=m.conversation_id "
    "JOIN partitions p ON p.id=c.partition_id WHERE m.id=?"
)
_PIN_KEYS = (
    "target_text_sha256", "target_provenance_sha256",
    "native_text_sha256", "native_provenance_sha256",
)
_LINK_COLUMNS = ("target_message_id", "native_message_id", *_PIN_KEYS)
_LINK_FILTER = " AND ".join(
    "typeof(" + key + ")='text' AND length(" + key + ")=64 AND "
    + key + " NOT GLOB '*[^0-9a-f]*'" for key in _LINK_COLUMNS
)
_RECEIPT_KEYS = ("candidate_agreement_receipt", "candidate_agreement_receipt_sha256")
_REGISTRY_CACHE = OrderedDict()
_REGISTRY_LOCK = Lock()


@dataclass(frozen=True)
class _Registry:
    root: Path
    path: Path
    identity: tuple
    digest: str
    indexed: bool
    links: dict
    info: dict


def _valid_link(link):
    return isinstance(link, dict) and all(
        isinstance(link.get(key), str) and _HEX.fullmatch(link[key])
        for key in _LINK_COLUMNS
    )


def _registry_info(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or len(_json(value).encode()) > MAX_REGISTRY_INFO_BYTES:
        raise ValueError("Clock registry metadata exceeds its supported bounds.")
    result = {
        key: value[key] for key in _RECEIPT_KEYS
        if isinstance(value.get(key), str) and len(value[key].encode()) <= 4096
    }
    if "source_identity" in value:
        if not isinstance(value["source_identity"], str) or len(value["source_identity"]) > 512:
            raise ValueError("Invalid clock catalog source identity.")
        result["source_identity"] = value["source_identity"]
    return result


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _path_identity(path, root):
    current = path
    while current != root:
        if current.is_symlink() or current.parent == current:
            raise ValueError("Clock evidence must stay within its local source root.")
        current = current.parent
    if root.is_symlink():
        raise ValueError("Clock evidence requires a local source root.")
    return _identity(path)


def _registry(root):
    root = Path(root).expanduser().absolute()
    directory = wechat_archive_path(root).parent
    indexed_path = directory / "forwarded_clock_links.sqlite3"
    indexed = indexed_path.exists() or indexed_path.is_symlink()
    path = indexed_path if indexed else directory / "forwarded_clock_links.json"
    before = _path_identity(path, root)
    limit = MAX_INDEXED_REGISTRY_BYTES if indexed else MAX_REGISTRY_BYTES
    if before[2] > limit:
        raise ValueError("Clock link registry exceeds its supported bounds.")
    cache_key = (path, before, MAX_LINKS, MAX_INDEXED_LINKS, MAX_REGISTRY_INFO_BYTES)
    with _REGISTRY_LOCK:
        cached = _REGISTRY_CACHE.get(cache_key)
        if cached is not None:
            if _path_identity(path, root) != before:
                raise ValueError("Clock link registry changed while reading.")
            _REGISTRY_CACHE.move_to_end(cache_key)
            return cached
        digest = hashlib.sha256()
        if indexed:
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            with closing(_readonly(path)) as connection:
                if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise ValueError("Unsupported indexed clock registry version.")
                schema = connection.execute(
                    "SELECT type FROM sqlite_master WHERE name='links'"
                ).fetchone()
                fields = connection.execute("PRAGMA table_info(links)").fetchall()
                if (
                    schema is None or schema[0] != "table"
                    or {row[1] for row in fields} != set(_LINK_COLUMNS)
                    or any(row[2].upper() != "TEXT" or row[5] != int(row[1] == "target_message_id") for row in fields)
                    or connection.execute("SELECT COUNT(*) FROM links").fetchone()[0] > MAX_INDEXED_LINKS
                ):
                    raise ValueError("Invalid indexed clock registry schema.")
                info = {}
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='info'").fetchone():
                    size = connection.execute(
                        "SELECT COALESCE(SUM(length(CAST(key AS BLOB))+length(CAST(value AS BLOB))),0) FROM info"
                    ).fetchone()[0]
                    if size > MAX_REGISTRY_INFO_BYTES:
                        raise ValueError("Clock registry metadata exceeds its supported bounds.")
                    info = dict(connection.execute("SELECT key,value FROM info"))
            links, info = {}, _registry_info(info)
        else:
            raw = path.read_bytes()
            digest.update(raw)
            value = json.loads(raw)
            values = value.get("links") if isinstance(value, dict) else None
            if (
                not isinstance(value, dict) or type(value.get("schema_version")) is not int
                or value["schema_version"] != 1 or not isinstance(values, list)
                or len(values) > MAX_LINKS
            ):
                raise ValueError("Invalid clock link registry.")
            links, rejected = {}, set()
            for link in values:
                target = link.get("target_message_id") if isinstance(link, dict) else None
                if not isinstance(target, str) or not _HEX.fullmatch(target):
                    continue
                if target in links or not _valid_link(link):
                    rejected.add(target)
                else:
                    links[target] = link
            for target in rejected:
                links.pop(target, None)
            info = _registry_info(value.get("info"))
        if _path_identity(path, root) != before:
            raise ValueError("Clock link registry changed while reading.")
        registry = _Registry(root, path, before, digest.hexdigest(), indexed, links, info)
        _REGISTRY_CACHE[cache_key] = registry
        while len(_REGISTRY_CACHE) > 4:
            _REGISTRY_CACHE.popitem(last=False)
        return registry


def _selected_links(registry, candidates=None):
    if not registry.indexed:
        if candidates is None:
            return list(registry.links.values())
        result = {}
        for number, target in enumerate(candidates, 1):
            if number > MAX_FILTER_CANDIDATES:
                raise ValueError("Clock registry candidates exceed their supported bounds.")
            if isinstance(target, str) and target in registry.links:
                result[target] = registry.links[target]
        return list(result.values())
    columns = ",".join(_LINK_COLUMNS)
    with closing(_readonly(registry.path)) as connection:
        if candidates is None:
            values = connection.execute("SELECT " + columns + " FROM links WHERE " + _LINK_FILTER + " ORDER BY target_message_id")
            result = [dict(row) for row in values if _valid_link(dict(row))]
        else:
            result, batch = {}, []
            for number, target in enumerate(candidates, 1):
                if number > MAX_FILTER_CANDIDATES:
                    raise ValueError("Clock registry candidates exceed their supported bounds.")
                if not isinstance(target, str) or not _HEX.fullmatch(target):
                    continue
                batch.append(target)
                if len(batch) == 400:
                    _indexed_batch(connection, columns, batch, result)
                    batch = []
            if batch:
                _indexed_batch(connection, columns, batch, result)
            result = list(result.values())
    if _path_identity(registry.path, registry.root) != registry.identity:
        raise ValueError("Clock registry changed while selecting links.")
    return result


def _indexed_batch(connection, columns, batch, result):
    marks = ",".join("?" for _ in batch)
    for row in connection.execute(
        "SELECT " + columns + " FROM links WHERE target_message_id IN (" + marks + ") AND " + _LINK_FILTER, batch
    ):
        link = dict(row)
        if _valid_link(link):
            result[link["target_message_id"]] = link


def registry_digest(root):
    """Fingerprint a stable local registry; unavailable evidence supplies no links."""
    try:
        return _registry(root).digest
    except (OSError, ValueError, TypeError, RecursionError, sqlite3.DatabaseError):
        return ""


def linked_parent_ids(root, candidates=None):
    """Expose only reviewed target IDs, never scan an archive for candidates."""
    try:
        registry = _registry(root)
        if registry.indexed and candidates is None:
            with closing(_readonly(registry.path)) as connection:
                targets = tuple(row[0] for row in connection.execute(
                    "SELECT target_message_id FROM links WHERE " + _LINK_FILTER + " ORDER BY target_message_id"
                ))
            if _path_identity(registry.path, registry.root) != registry.identity:
                return ()
            return targets
        links = _selected_links(registry, candidates)
        if _path_identity(registry.path, registry.root) != registry.identity:
            return ()
        return tuple(sorted(link["target_message_id"] for link in links))
    except (OSError, ValueError, TypeError, RecursionError, sqlite3.DatabaseError):
        return ()


class _ProjectionContext:
    def __init__(self, connection):
        self.connection = connection
        self.bytes = 0
        self.rows = {}
        self.proofs = {}
        self.hashes = {}
        self.snapshots = {}
        self.html_snapshots = {}
        self.native_packets = {}

    def charge(self, size):
        if not isinstance(size, int) or size < 0 or self.bytes + size > MAX_SOURCE_BYTES:
            raise ValueError("Clock evidence exceeds its cumulative budget.")
        self.bytes += size

    def row(self, identifier):
        if identifier not in self.rows:
            size = self.connection.execute(
                "SELECT length(CAST(text AS BLOB))+length(CAST(provenance AS BLOB)) "
                "FROM messages WHERE id=?", (identifier,),
            ).fetchone()
            if size is None or size[0] > MAX_ROW_BYTES:
                raise ValueError("Clock source row is missing or too large.")
            self.charge(size[0])
            row = self.connection.execute(_SELECT, (identifier,)).fetchone()
            if row is None:
                raise ValueError("Clock source partition is missing.")
            self.rows[identifier] = dict(row)
        return self.rows[identifier]

    def proof(self, row):
        if row["id"] not in self.proofs:
            proof = json.loads(row["provenance"])
            if not isinstance(proof, dict):
                raise ValueError("Clock provenance must be an object.")
            self.proofs[row["id"]] = proof
        return self.proofs[row["id"]]

    def sha(self, row, field):
        key = row["id"], field
        if key not in self.hashes:
            self.hashes[key] = _sha(row[field])
        return self.hashes[key]


def _snapshot(connection, proof, context=None):
    key = proof.get("source_snapshot")
    if type(key) is not int or key <= 0:
        raise ValueError("Clock evidence needs a published source snapshot.")
    if context is not None and key in context.snapshots:
        return context.snapshots[key]
    size = connection.execute(
        "SELECT length(CAST(metadata AS BLOB)) FROM source_snapshots WHERE id=?", (key,)
    ).fetchone()
    if size is None or size[0] > MAX_SNAPSHOT_BYTES:
        raise ValueError("Clock source snapshot is missing or too large.")
    if context is not None:
        context.charge(size[0])
    row = connection.execute("SELECT * FROM source_snapshots WHERE id=?", (key,)).fetchone()
    metadata = json.loads(row["metadata"])
    if not isinstance(metadata, dict):
        raise ValueError("Clock snapshot metadata must be an object.")
    result = dict(row), metadata
    if context is not None:
        context.snapshots[key] = result
    return result


def _native_identity(connection, row, proof, context=None):
    snapshot, metadata = _snapshot(connection, proof, context)
    partition = connection.execute(
        "SELECT * FROM partitions WHERE id=?", (snapshot["source_id"],)
    ).fetchone()
    owner = partition["account_id"] if partition else ""
    account_hash = hashlib.md5(owner.encode()).hexdigest() if owner else ""
    locator = proof.get("locator", {})
    record = proof.get("record_id")
    relative = locator.get("database_relative_path") if isinstance(locator, dict) else None
    if (
        not partition or not owner
        or partition["id"] != "ios-" + account_hash
        or partition["identity_status"] != "Verified archive ID"
        or metadata.get("source_id") != partition["id"]
        or metadata.get("source_account_id") != owner
        or metadata.get("account_hash_verified") is not True
        or metadata.get("account_hash") != account_hash
        or row["account_id"] != owner
        or row["account_key"] != partition["account_key"]
        or row["account_key"] != "account:" + owner
        or row["container_account_key"] != row["account_key"]
        or row["identity_status"] not in {"Verified archive ID", "Corroborated export ID"}
        or not isinstance(relative, str)
        or Path(relative).parts[:3] != ("Documents", account_hash, "DB")
        or len(Path(relative).parts) != 4
        or Path(relative).name != locator.get("database")
        or Path(relative).name in {".", ".."}
        or locator.get("table") != "Chat_" + hashlib.md5(row["peer_id"].encode()).hexdigest()
        or not isinstance(record, str) or not record
        or _native_time(row, proof) is None
    ):
        raise ValueError("Native clock source identity differs.")
    observation = {
        **proof, "content_sha256": _sha(row["text"]),
        "sender_id": row["sender_id"] or "", "sender_label": row["sender_label"],
        "direction": row["direction"], "timestamp_value": row["timestamp_value"],
        "time_basis": row["time_basis"], "flags": json.loads(row["quality_flags"]),
    }
    source = partition["id"]
    observation_id = _identifier(source, record, _json(observation))
    if row["id"] not in {
        _identifier(source, record), _identifier(source, record, "variant", observation_id)
    }:
        raise ValueError("Native clock record ID differs.")
    return owner, snapshot, metadata


def _html_identity(connection, row, proof, context=None):
    snapshot, metadata = _snapshot(connection, proof, context)
    if (
        row["identity_status"] != "Corroborated export ID"
        or snapshot["source_id"] != row["source_id"]
    ):
        raise ValueError("HTML clock source has no verified owner.")
    request = {
        "id": row["id"], "source_id": row["source_id"],
        "conversation_id": row["conversation_id"], "account_id": row["account_id"],
        "peer_id": row["peer_id"], "conversation_kind": row["kind"],
        "timestamp_value": row["timestamp_value"], "text_sha256": _sha(row["text"]),
        "source_record_id": proof.get("record_id"), "source_locator": proof.get("locator"),
        "forwarded_components": proof.get("forwarded_components"),
        "source_snapshot_id": snapshot["snapshot_id"],
    }
    snapshots = context.html_snapshots if context is not None else {}
    cache_key = proof["source_snapshot"], row["source_id"]
    if cache_key not in snapshots:
        snapshots[cache_key] = (
            snapshot["snapshot_id"], metadata, _forwarded_source_members(metadata)
        )
    WechatContactRepository._forwarded_parent_proof(connection, request, snapshots)
    if project_html_forwarded(
        proof, parent_id=row["id"], account_id=row["account_id"], text=row["text"],
        components=proof.get("forwarded_components"), source_proof_status="source_proof_verified",
    ) is None:
        raise ValueError("HTML clock source rendering differs.")


def _native_parts(proof):
    parts = proof.get("text_components")
    if not isinstance(parts, list) or not 1 <= len(parts) <= MAX_COMPONENTS:
        raise ValueError("Native clock packet exceeds its supported shape.")
    if proof.get("omitted_forwarded_items", 0) != 0:
        raise ValueError("Native clock packet has omitted components.")
    texts, ids = [], set()
    for number, part in enumerate(parts, 1):
        data = part.get("source_data_id") if isinstance(part, dict) else None
        if (
            not isinstance(data, str)
            or not (_DATA.fullmatch(data) or re.fullmatch(r"0|[1-9][0-9]*", data))
            or data in ids
            or part.get("forwarded_item_path", [number]) != [number]
            or any(part.get(key) for key in (
                "text_components", "forwarded_components", "quoted_components", "quoted_reference",
                "omitted_forwarded_items",
            ))
        ):
            raise ValueError("Native clock packet is not complete and flat.")
        ids.add(data)
        if part.get("kind") == "forwarded_text":
            texts.append(part)
        elif (
            part.get("kind") != "forwarded_resource" or part.get("text") != ""
            or type(part.get("forwarded_data_type")) is not int
            or part["forwarded_data_type"] not in _FORWARDED_RESOURCES
            or part.get("resource_kind") != _FORWARDED_RESOURCES[part["forwarded_data_type"]]
            or part.get("original_message_type") == 1
        ):
            raise ValueError("Native nontext position has no explicit resource proof.")
    if native_components_text(parts) != proof.pop("_expected_text"):
        raise ValueError("Native packet rendering differs.")
    if not all(_DATA.fullmatch(part["source_data_id"]) for part in parts) and not all(
        part["source_data_id"] == str(number - 1) for number, part in enumerate(parts, 1)
    ):
        raise ValueError("Native packet IDs have no consistent complete namespace.")
    return texts


def _clock_agrees(raw, value):
    if raw is None or isinstance(raw, str) and (not raw.strip() or _epoch_placeholder_clock(raw)):
        return True
    normalized, precision = normalize_html_clock(raw)
    first, second = _stamp(normalized or raw, seconds=not bool(normalized)), _stamp(value)
    if first is None or second is None:
        return False
    if precision == "minute":
        second = second.replace(second=0)
    return (
        first.replace(tzinfo=None) == second.replace(tzinfo=None)
        and (first.tzinfo is None or second.tzinfo is None or first == second)
    )


def _native_clock(part, detail):
    stamp = _stamp(detail["timestamp_value"], seconds=True)
    if stamp is None:
        raise ValueError("Every native text child needs its own genuine clock.")
    numeric = part.get("source_create_time", "")
    if not isinstance(numeric, str):
        raise ValueError("Invalid numeric native child clock.")
    numeric = numeric.strip()
    if numeric and not re.fullmatch(r"0+", numeric):
        if not re.fullmatch(r"[1-9][0-9]{0,11}", numeric) or stamp.tzinfo is None:
            raise ValueError("Contradictory numeric native child clock.")
        if datetime.fromtimestamp(int(numeric), UTC) != stamp.astimezone(UTC):
            raise ValueError("Contradictory numeric native child clock.")
        if not _clock_agrees(part.get("source_timestamp"), detail["timestamp_value"]):
            raise ValueError("Native child's literal and numeric clocks disagree.")
    return stamp


def _same_receiving_context(target, native, html_proof):
    forwarders = html_proof.get("sender_native_id"), native["sender_id"]
    for row, forwarder in zip((target, native), forwarders):
        if (
            row["kind"] not in {"group", "direct"} or not _author(row["account_id"])
            or not isinstance(row["peer_id"], str) or not _author(forwarder)
        ):
            raise ValueError("Clock packet has an unsupported receiving context.")
        room = row["peer_id"].endswith(("@chatroom", "@im.chatroom"))
        if row["kind"] == "group":
            if not room:
                raise ValueError("Group clock packet has no room ID.")
        elif (
            room or not _author(row["peer_id"])
            or forwarder not in {row["account_id"], row["peer_id"]}
            or row["direction"] != ("sent" if forwarder == row["account_id"] else "received")
        ):
            raise ValueError("Direct clock packet has an invalid participant or direction.")
    if target["kind"] != native["kind"] or forwarders[0] != forwarders[1]:
        return False
    if target["kind"] == "group":
        return target["peer_id"] == native["peer_id"]
    return sorted((target["account_id"], target["peer_id"])) == sorted((native["account_id"], native["peer_id"]))


def _copy_receipt(info):
    if not isinstance(info, dict):
        return False
    receipt, digest = (info.get(key) for key in _RECEIPT_KEYS)
    if (
        not isinstance(receipt, str) or not receipt.strip()
        or not isinstance(digest, str) or not _HEX.fullmatch(digest)
        or not isinstance(info.get("source_identity"), str)
    ):
        return False
    try:
        identity = json.loads(info["source_identity"])
        return isinstance(identity, list) and len(identity) == 5 and all(type(value) is int for value in identity)
    except (ValueError, TypeError, RecursionError):
        return False


def _link(connection, target, native, link, digest, context=None, registry_info=None):
    context = context if context is not None else _ProjectionContext(connection)
    for prefix, row in (("target", target), ("native", native)):
        for field in ("text", "provenance"):
            if context.sha(row, field) != link[prefix + "_" + field + "_sha256"]:
                raise ValueError("Pinned clock source bytes changed.")
    html_proof, native_proof = context.proof(target), context.proof(native)
    same_context = _same_receiving_context(target, native, html_proof)
    _html_identity(connection, target, html_proof, context)
    if native["id"] not in context.native_packets:
        owner, snapshot, metadata = _native_identity(connection, native, native_proof, context)
        parts = _native_parts({**native_proof, "_expected_text": native["text"]})
        details = _native_packet(native_proof, native["id"], owner, native["peer_id"])
        if details is None or len(parts) != len(details):
            raise ValueError("Native packet does not account for every text leaf.")
        for part, detail in zip(parts, details):
            _native_clock(part, detail)
        context.native_packets[native["id"]] = snapshot, metadata, parts, details
    snapshot, metadata, parts, details = context.native_packets[native["id"]]
    opaque = all(_DATA.fullmatch(part["source_data_id"]) for part in parts)
    strong_copy = (
        opaque and len(parts) >= 3
        and len({part["source_sender_id"] for part in parts}) >= 2
        and _copy_receipt(registry_info)
    )
    if not same_context and not strong_copy:
        raise ValueError("Clock packets have different receiving groups or forwarders.")
    leaves = html_proof.get("forwarded_components")
    local = html_proof.get("locator", {}).get("export_msgid")
    if (
        not isinstance(leaves, list) or not 1 <= len(leaves) <= MAX_COMPONENTS
        or details is None or len(leaves) != len(details) or len(parts) != len(details)
        or not isinstance(local, str) or not re.fullmatch(r"[1-9][0-9]*", local)
        or html_proof.get("export_msgid_namespace") != "MesLocalID"
        or html_proof.get("locator", {}).get("export_msgid_namespace") != "MesLocalID"
    ):
        raise ValueError("Clock packets do not contain the same complete transcript.")
    if not opaque:
        first, second = _stamp(target["timestamp_value"], seconds=True), _stamp(native["timestamp_value"], seconds=True)
        if (
            target["kind"] != "group" or target["account_id"] != native["account_id"]
            or target["direction"] != native["direction"] or len(parts) < 3
            or len({part["source_sender_id"] for part in parts}) < 2
            or first is None or second is None
            or first.replace(tzinfo=None) != second.replace(tzinfo=None)
            or (first.tzinfo is not None and second.tzinfo is not None and first != second)
        ):
            raise ValueError("Ordinal packet IDs require the same proven receiving event.")
    result = {}
    for number, (leaf, part, detail) in enumerate(zip(leaves, parts, details), 1):
        if (
            not isinstance(leaf, dict) or leaf.get("forwarded_components")
            or leaf.get("quoted_components") or leaf.get("resource_reference")
            or leaf.get("text_kind") not in {"plain_text", "forwarded_rendered_text"}
            or leaf.get("export_msgid") != local + "_" + part["source_data_id"]
            or leaf.get("sender_native_id") != detail["sender_id"]
            or leaf.get("text") != part.get("text") or leaf.get("text") != detail["text"]
            or not _author(detail["sender_id"])
            or not _clock_agrees(leaf.get("raw_timestamp"), detail["timestamp_value"])
        ):
            raise ValueError("Clock packet component identity, ordering, or time differs.")
        raw = leaf.get("raw_timestamp", "")
        if not isinstance(raw, str) or (raw.strip() and not _epoch_placeholder_clock(raw)):
            continue
        result[(number,)] = {
            "timestamp_value": detail["timestamp_value"],
            "timestamp_label": detail["timestamp_label"],
            "time_basis": "Recovered native packet time; " + detail["time_basis"],
            "source_clock": detail["source_clock"],
            "quality_flags": [*detail["quality_flags"], "recovered_pinned_native_forwarded_clock"],
            "proof": {
                "version": VERSION, "registry_sha256": digest, **link,
                "target_component_path": [number], "native_component_path": detail["path"],
                "source_data_id": part["source_data_id"], "target_source_clock": raw,
                "native_source_id": snapshot["source_id"],
                "source_snapshot_id": snapshot["snapshot_id"],
                "native_source_snapshot": native_proof["source_snapshot"],
                "native_source_label": metadata.get("source_label", ""),
                "native_source_clock": detail["source_clock"],
                "timestamp_value": detail["timestamp_value"],
                "time_basis": detail["time_basis"],
                "sender_id": detail["sender_id"],
                "body_sha256": _sha(detail["text"]),
                "match_basis": "same_receiving_context" if same_context else "complete_opaque_packet_copy",
                **(registry_info or {}),
            },
        }
    return result


class WechatForwardedClocks:
    """Resolve reviewed, bounded pointers against unchanged local archive evidence."""

    def __init__(self, root):
        self.root = Path(root).expanduser().absolute()

    def project(self, rows):
        try:
            if not isinstance(rows, (list, tuple)) or len(rows) > MAX_ROWS:
                return {}
            supplied, duplicate = {}, set()
            for row in rows:
                target = row.get("id") if isinstance(row, dict) else None
                if not isinstance(target, str) or not _HEX.fullmatch(target):
                    continue
                if target in supplied:
                    duplicate.add(target)
                else:
                    supplied[target] = row
            for target in duplicate:
                supplied.pop(target, None)
            registry = _registry(self.root)
            links = _selected_links(registry, supplied)
            if not links:
                return {}
            history = wechat_archive_path(self.root)
            before = _path_identity(history, self.root)
            if "source_identity" in registry.info:
                pinned_identity = json.loads(registry.info["source_identity"])
                if (
                    not isinstance(pinned_identity, list)
                    or any(type(value) is not int for value in pinned_identity)
                    or tuple(pinned_identity) != before
                ):
                    return {}
            result = {}
            with closing(_readonly(history)) as connection:
                context = _ProjectionContext(connection)
                for link in links:
                    try:
                        target, native = (
                            context.row(link[key]) for key in ("target_message_id", "native_message_id")
                        )
                        caller = supplied[target["id"]]
                        if any(caller.get(key) != target[key] for key in ("text", "provenance")):
                            raise ValueError("Caller clock source differs from the archive.")
                        result[target["id"]] = _link(
                            connection, target, native, link, registry.digest, context, registry.info
                        )
                    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
                        continue
            if (
                _path_identity(history, self.root) != before
                or _path_identity(registry.path, self.root) != registry.identity
            ):
                return {}
            return result
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, sqlite3.DatabaseError):
            return {}
