"""Reference matching indexed excerpts without rewriting flattened export previews.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
from itertools import islice
import json
from pathlib import Path
import re
import sqlite3
import stat

from .wechat_archive import wechat_archive_path
from .wechat_contacts import (
    WechatContactRepository,
    _forwarded_member_context,
    _forwarded_native_proof,
    _forwarded_source_members,
    contact_registry_path,
)
from .wechat_context_links import _row, _snapshot
from .wechat_display_names import WechatDisplayNames
from .wechat_forwarded import _read_only, project_html_forwarded
from .wechat_reconcile import _database_identity, _json
from .wechat_text_observations import _file_identity, _managed_path, _read_json


_ID = re.compile(r"[0-9a-f]{64}")
_AUTHORITY = "user_confirmed_nested_forward"
_MAX_DECLARATIONS = 64
_MAX_MESSAGES = 1000
_MAX_EXCERPT = 64
_PAGE_BYTES = 8 * 1024 * 1024


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _identifier(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _path(value):
    return (
        isinstance(value, list)
        and 1 <= len(value) <= 32
        and all(type(number) is int and 0 < number <= 4096 for number in value)
    )


def _declarations(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "previews"}
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or not isinstance(value.get("previews"), list)
        or len(value["previews"]) > _MAX_DECLARATIONS
    ):
        raise ValueError("Unsupported indexed forward-preview registry.")
    required = {
        "target_parent_id",
        "target_component_path",
        "target_text_sha256",
        "target_provenance_sha256",
        "authority",
    }
    source = {
        "source_parent_id",
        "source_component_paths",
        "source_text_sha256",
        "source_provenance_sha256",
    }
    result = {}
    for declaration in value["previews"]:
        if (
            not isinstance(declaration, dict)
            or not required <= declaration.keys()
            or set(declaration) - required - source
            or declaration["authority"] != _AUTHORITY
            or not _path(declaration["target_component_path"])
            or any(
                not _identifier(declaration[key])
                for key in (
                    "target_parent_id",
                    "target_text_sha256",
                    "target_provenance_sha256",
                )
            )
        ):
            raise ValueError("Forward previews need exact source identifiers and pins.")
        present = source & declaration.keys()
        if present and (
            present != source
            or any(
                not _identifier(declaration[key])
                for key in (
                    "source_parent_id",
                    "source_text_sha256",
                    "source_provenance_sha256",
                )
            )
            or declaration["source_parent_id"] == declaration["target_parent_id"]
            or not isinstance(declaration["source_component_paths"], list)
            or not 3 <= len(declaration["source_component_paths"]) <= _MAX_EXCERPT
            or any(not _path(path) for path in declaration["source_component_paths"])
            or any(
                left[:-1] != right[:-1] or left[-1] + 1 != right[-1]
                for left, right in zip(
                    declaration["source_component_paths"],
                    declaration["source_component_paths"][1:],
                )
            )
        ):
            raise ValueError(
                "Indexed excerpts need contiguous, fully pinned source paths."
            )
        key = (
            declaration["target_parent_id"],
            tuple(declaration["target_component_path"]),
        )
        if key in result:
            raise ValueError("Each reviewed preview must have one declaration.")
        result[key] = declaration
    return result


def _local(path):
    state = path.lstat()
    if (
        not stat.S_ISREG(state.st_mode)
        or getattr(state, "st_flags", 0) & 0x40000000
        or any(
            Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")
        )
    ):
        raise ValueError("Preview inputs must be published resident regular files.")
    return _file_identity(path)


def _request(row, proof, snapshot):
    return {
        "id": row["id"],
        "source_id": row["partition_id"],
        "source_snapshot_id": snapshot["snapshot_id"],
        "conversation_id": row["conversation_id"],
        "account_id": row["account_id"],
        "peer_id": row["peer_id"],
        "conversation_kind": row["kind"],
        "sender_id": row["sender_id"] or "",
        "sender_label": row["sender_label"],
        "text_sha256": _sha(row["text"]),
        "timestamp_value": row["timestamp_value"],
        "source_record_id": proof.get("record_id", ""),
        "source_locator": proof.get("locator", {}),
        "forwarded_components": proof.get("forwarded_components", []),
        "is_system": False,
    }


def _raw_component(proof, path):
    values = proof.get("forwarded_components", [])
    for number in path:
        if not isinstance(values, list) or number > len(values):
            return None
        result = values[number - 1]
        if not isinstance(result, dict):
            return None
        values = result.get("forwarded_components", [])
    return result


def _projected_component(record, path):
    for index, number in enumerate(path):
        values = record.get("items", ())
        if not isinstance(values, (list, tuple)) or number > len(values):
            return None
        result = values[number - 1]
        if index + 1 < len(path):
            record = result.messaging.get("forwarded_record")
            if not isinstance(record, dict):
                return None
    return result


def _pin(row, proof, declaration, prefix):
    return (
        _sha(row["text"]) == declaration[prefix + "_text_sha256"]
        and _sha(_json(proof)) == declaration[prefix + "_provenance_sha256"]
    )


def _source_forwarder(row, proof, metadata):
    native = proof.get("sender_native_id")
    if not isinstance(native, str) or not native:
        return None
    context = _forwarded_member_context(
        metadata,
        row,
        proof.get("locator", {}),
        avatar_path=proof.get("avatar_path_metadata", ""),
        sender_native_id=native,
    )
    if context is None:
        return None
    verified = _forwarded_native_proof(
        metadata,
        row,
        _forwarded_source_members(metadata),
        {
            **proof,
            "source_locator": proof.get("locator", {}),
            "avatar_path": proof.get("avatar_path_metadata", ""),
            "text_kind": proof.get("text_extraction_kind", ""),
        },
        context,
    )
    return verified["contact_id"] if verified is not None else None


class WechatForwardedPreviews:
    """Project only explicitly reviewed previews and pinned matching indexed excerpts.

    Matching excerpts are separate source observations, not recovered original
    bundle identities. Their literal clocks and contacts retain their source
    authority. Unmatched previews remain one literal text block.
    """

    def __init__(self, local_store_root, registry_path=None):
        self.root = Path(local_store_root).absolute()
        self.base = self.root / "messaging" / "wechat"
        self.path = _managed_path(
            self.base,
            registry_path if registry_path is not None else "forwarded_previews.json",
        )
        self.history = wechat_archive_path(self.root)
        self.contacts = contact_registry_path(self.root)
        self.statuses = {}

    def project(self, messages):
        self.statuses = {}
        supplied = list(islice(messages, _MAX_MESSAGES + 1))
        if not supplied or len(supplied) > _MAX_MESSAGES or not self.path.exists():
            return {}
        connection = None
        try:
            registry_identity = _local(self.path)
            registry, _, read_identity = _read_json(self.path)
            if registry_identity != read_identity:
                return {}
            declarations = _declarations(registry)
            requested = {}
            for row in supplied:
                if not isinstance(row, dict) or not _identifier(row.get("id")):
                    return {}
                if row["id"] in requested:
                    return {}
                requested[row["id"]] = row
            declarations = {
                key: declaration
                for key, declaration in declarations.items()
                if key[0] in requested
            }
            if not declarations:
                return {}
            identities = {path: _local(path) for path in (self.history, self.contacts)}
            initial = _database_identity(self.history)
            connection = _read_only(self.history)
            rows, proofs, snapshots, metadata, requests = {}, {}, {}, {}, []
            identifiers = {key[0] for key in declarations}
            identifiers.update(
                declaration["source_parent_id"]
                for declaration in declarations.values()
                if "source_parent_id" in declaration
            )
            page_bytes = 0
            for identifier in sorted(identifiers):
                row = _row(connection, identifier)
                if row is None:
                    continue
                page_bytes += len(row["text"].encode()) + len(
                    row["provenance"].encode()
                )
                if page_bytes > _PAGE_BYTES:
                    return {}
                proof = json.loads(row["provenance"])
                if not isinstance(proof, dict):
                    continue
                snapshot, source = _snapshot(connection, row, proof, snapshots)
                if identifier in requested:
                    current = requested[identifier]
                    supplied_proof = current.get("provenance")
                    if isinstance(supplied_proof, str):
                        supplied_proof = json.loads(supplied_proof)
                    if current.get("text") != row["text"] or _json(
                        supplied_proof
                    ) != _json(proof):
                        continue
                rows[identifier], proofs[identifier], metadata[identifier] = (
                    row,
                    proof,
                    source,
                )
                requests.append(_request(row, proof, snapshot))
            resolver = WechatContactRepository(self.root)
            resolved = (
                resolver.resolve_forwarded_components(requests) if requests else {}
            )
            names = WechatDisplayNames(self.root)
            projected = {}
            for identifier, row in rows.items():
                result = resolved.get(identifier, {})
                record = project_html_forwarded(
                    proofs[identifier],
                    parent_id=identifier,
                    account_id=row["account_id"],
                    text=row["text"],
                    components=result.get("components"),
                    source_proof_status=result.get("status"),
                    display_names=names,
                )
                if record is not None:
                    projected[identifier] = record
            result = {}
            for (parent, path), declaration in declarations.items():
                if parent not in projected or not _pin(
                    rows[parent], proofs[parent], declaration, "target"
                ):
                    continue
                raw = _raw_component(proofs[parent], path)
                target = _projected_component(projected[parent], path)
                if (
                    target is None
                    or not raw
                    or not isinstance(raw.get("text"), str)
                    or not raw["text"].strip()
                    or raw.get("forwarded_components")
                    or raw.get("quoted_components")
                    or raw.get("text_kind")
                    not in {
                        "forwarded_rendered_text",
                        "rendered_forwarded_app_metadata",
                        "rendered_app_text",
                    }
                    or raw.get("type", "") not in {"", "49"}
                ):
                    continue
                reference = {
                    "authority": _AUTHORITY,
                    "relationship": "preview_only",
                    "target_parent_id": parent,
                    "target_component_path": list(path),
                    "target_text_sha256": declaration["target_text_sha256"],
                    "target_provenance_sha256": declaration["target_provenance_sha256"],
                    "original_bundle_identity_asserted": False,
                    "original_conversation_unknown": True,
                }
                record = {
                    "title": "Chat history",
                    "source_title": "",
                    "items": (),
                    "authority": _AUTHORITY,
                    "status": "preview_only",
                    "preview_text": raw["text"],
                    "original_conversation_unknown": True,
                }
                source_id = declaration.get("source_parent_id")
                if source_id in projected and _pin(
                    rows[source_id], proofs[source_id], declaration, "source"
                ):
                    source = rows[source_id]
                    scope = (
                        "partition_id",
                        "account_key",
                        "account_id",
                        "peer_id",
                        "kind",
                        "conversation_id",
                    )
                    same_scope = all(source[key] == rows[parent][key] for key in scope)
                    same_snapshot = proofs[parent].get("source_snapshot") == proofs[
                        source_id
                    ].get("source_snapshot")
                    contact = target.messaging.get("contact", {})
                    forwarder = _source_forwarder(
                        source, proofs[source_id], metadata[source_id]
                    )
                    if (
                        same_scope
                        and same_snapshot
                        and contact.get("identity_status") == "verified"
                        and contact.get("id") == forwarder
                    ):
                        children, parts, authors, signatures = [], [], set(), set()
                        for source_path in declaration["source_component_paths"]:
                            child = _projected_component(
                                projected[source_id], source_path
                            )
                            node = _raw_component(proofs[source_id], source_path)
                            identity = (
                                child.messaging.get("contact", {}) if child else {}
                            )
                            if (
                                child is None
                                or node is None
                                or node.get("quoted_components")
                                or child.messaging.get("forwarded_record")
                                or child.messaging.get("quoted_reply")
                                or child.messaging.get("resource")
                                or child.messaging.get("is_system_notice")
                                or identity.get("identity_status") != "verified"
                                or not node.get("sender_label")
                                or not isinstance(node.get("text"), str)
                                or not node["text"].strip()
                            ):
                                children = []
                                break
                            parts.append(f"{node['sender_label']}: {node['text']}")
                            authors.add(identity["id"])
                            signatures.add(
                                (
                                    identity["id"],
                                    node["text"],
                                    node.get("raw_timestamp", ""),
                                )
                            )
                            children.append(deepcopy(child))
                        if (
                            len(children) >= 3
                            and len(authors) >= 2
                            and len(signatures) == len(children)
                            and "\n".join(parts).strip() == raw["text"].strip()
                        ):
                            reference.update(
                                relationship="matching_indexed_excerpt",
                                source_parent_id=source_id,
                                source_component_paths=deepcopy(
                                    declaration["source_component_paths"]
                                ),
                                source_text_sha256=declaration["source_text_sha256"],
                                source_provenance_sha256=declaration[
                                    "source_provenance_sha256"
                                ],
                            )
                            for child, source_path in zip(
                                children,
                                declaration["source_component_paths"],
                                strict=True,
                            ):
                                child.messaging["preview_reference_proof"] = {
                                    **reference,
                                    "source_component_path": list(source_path),
                                    "source_time_basis": child.messaging["time_basis"],
                                }
                                child.messaging["time_basis"] = (
                                    "Matching indexed source observation; "
                                    + child.messaging["time_basis"]
                                )
                                child.messaging["original_conversation_unknown"] = True
                            record.update(
                                items=tuple(children),
                                authority="matching_indexed_excerpt",
                                status="matched_excerpt",
                                preview_text="",
                            )
                status = record["status"]
                self.statuses[(parent, path)] = status
                result.setdefault(parent, {})[path] = {
                    "status": status,
                    "record": record,
                    "preview_text": raw["text"],
                    "proof": reference,
                }
            if (
                _database_identity(self.history) != initial
                or _local(self.path) != registry_identity
                or any(
                    _local(path) != identity for path, identity in identities.items()
                )
            ):
                return {}
            return result
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            RecursionError,
            AttributeError,
            sqlite3.Error,
        ):
            return {}
        finally:
            if connection is not None:
                connection.close()
