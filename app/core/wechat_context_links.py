"""Project exact HTML completion cross-references without changing either source.

Code version: v1.0.2-codex.0
"""

from __future__ import annotations

import hashlib
from itertools import islice
import json
from pathlib import Path
import re
import sqlite3

from .wechat_archive import _identifier, wechat_archive_path
from .wechat_contacts import WechatContactRepository, canonical_contact_id
from .wechat_display_names import WechatDisplayNames
from .wechat_forwarded import _read_only, project_html_forwarded
from .wechat_reconcile import _database_identity, _json
from .wechat_text_observations import _file_identity, _managed_path, _read_json


_HEX = re.compile(r"[0-9a-f]{64}")
_MAX_MESSAGES = 1000
_MAX_LINKS = 2000
_TEXT_BYTES = 2 * 1024 * 1024
_PROOF_BYTES = 16 * 1024 * 1024
_PAGE_BYTES = 8 * 1024 * 1024
_KIND = "rendered_text_completion_of_source_record"
_PINS = {"target_text_sha256", "source_text_sha256", "source_provenance_sha256"}


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _schema(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "links"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or not isinstance(value["links"], list)
        or len(value["links"]) > _MAX_LINKS
    ):
        raise ValueError("Unsupported HTML context-link registry.")
    result = {}
    required = {"target_message_id", "source_message_id", "evidence_id"}
    for link in value["links"]:
        if (
            not isinstance(link, dict)
            or not required <= link.keys()
            or set(link) - required - _PINS
            or any(
                not isinstance(item, str) or _HEX.fullmatch(item) is None
                for item in link.values()
            )
            or link["target_message_id"] == link["source_message_id"]
            or link["target_message_id"] in result
        ):
            raise ValueError(
                "HTML context links need unique, body-free exact identifiers."
            )
        result[link["target_message_id"]] = link
    return result


def _row(connection, identifier):
    return connection.execute(
        "SELECT m.*,c.partition_id,c.account_key,c.peer_id,c.kind,p.account_id,"
        "p.account_key AS partition_account_key,p.source_label "
        "FROM messages m JOIN conversations c ON c.id=m.conversation_id "
        "JOIN partitions p ON p.id=c.partition_id WHERE m.id=? "
        "AND length(CAST(m.text AS BLOB))<=? AND length(CAST(m.provenance AS BLOB))<=?",
        (identifier, _TEXT_BYTES, _PROOF_BYTES),
    ).fetchone()


def _snapshot(connection, row, provenance, cache):
    number = provenance.get("source_snapshot")
    if type(number) is not int or not 0 < number <= 2**63 - 1:
        raise ValueError("HTML context links need exact source snapshots.")
    key = number, row["partition_id"]
    if key not in cache:
        stored = connection.execute(
            "SELECT * FROM source_snapshots WHERE id=? AND source_id=? "
            "AND length(CAST(metadata AS BLOB))<=?",
            (*key, 8 * 1024 * 1024),
        ).fetchone()
        if stored is None:
            raise ValueError("HTML context source snapshot is unavailable.")
        cache[key] = dict(stored), json.loads(stored["metadata"])
    stored, metadata = cache[key]
    owner = row["account_id"]
    owner_proof = metadata.get("owner_evidence", {})
    if (
        not isinstance(owner, str)
        or not owner
        or row["account_key"] != "account:" + owner
        or row["partition_account_key"] != row["account_key"]
        or row["source_label"] != "HTML export"
        or metadata.get("source_id") != row["partition_id"]
        or metadata.get("source_label") != "HTML export"
        or metadata.get("source_account_id") != owner
        or metadata.get("owner_verified") is not True
        or not isinstance(owner_proof, dict)
        or owner_proof.get("native_username") != owner
        or owner_proof.get("native_contact_id")
        != canonical_contact_id(owner, authority="native_account_owner")
    ):
        raise ValueError("HTML context owner evidence differs.")
    return stored, metadata


def _retained_components_agree(previous, candidate):
    """Require every retained source leaf in order, without comparing synthetic aggregates."""

    def nodes(values, depth=0, count=None):
        count = [0] if count is None else count
        if not isinstance(values, list) or depth > 32:
            raise ValueError("Invalid retained HTML component tree.")
        for node in values:
            count[0] += 1
            if not isinstance(node, dict) or count[0] > 4096:
                raise ValueError("Retained HTML component tree exceeds its bounds.")
            yield node
            yield from nodes(node.get("forwarded_components", []), depth + 1, count)

    indexed = {}
    for ordinal, node in enumerate(nodes(candidate)):
        if not isinstance(node.get("export_msgid"), str):
            return False
        key = _json(node.get("source_locator")), node.get("export_msgid")
        if key in indexed:
            return False
        indexed[key] = ordinal, node
    last = -1
    for old in nodes(previous):
        key = _json(old.get("source_locator")), old.get("export_msgid")
        if (
            not isinstance(old.get("source_locator"), dict)
            or not old["source_locator"]
            or key not in indexed
        ):
            return False
        ordinal, new = indexed[key]
        if ordinal <= last:
            return False
        last = ordinal
        if any(
            old.get(field, "") != new.get(field, "")
            for field in (
                "sender_export_id",
                "sender_native_id",
                "sender_label",
                "raw_timestamp",
                "direction",
                "type",
            )
        ):
            return False
        if not (
            old.get("text_kind") == "forwarded_text" and old.get("forwarded_components")
        ) and old.get("text") != new.get("text"):
            return False
        if old.get("quoted_components") and old["quoted_components"] != new.get(
            "quoted_components", []
        ):
            return False
    return True


class WechatContextLinks:
    """Read only explicitly indexed completion sources for the current page."""

    def __init__(self, local_store_root, registry_path=None):
        self.root = Path(local_store_root).absolute()
        self.base = self.root / "messaging" / "wechat"
        self.path = (
            _managed_path(self.base, registry_path)
            if registry_path is not None
            else self.base / "context_links.json"
        )
        self.history = wechat_archive_path(self.root)
        self.statuses = {}

    def project(self, messages):
        self.statuses = {}
        supplied = list(islice(messages, _MAX_MESSAGES + 1))
        if not supplied or len(supplied) > _MAX_MESSAGES or not self.path.is_file():
            return {}
        connection = None
        try:
            path = _managed_path(self.base, self.path)
            registry, _, identity = _read_json(path)
            links = _schema(registry)
            requested = {}
            for row in supplied:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                    return {}
                if row["id"] in links:
                    if row["id"] in requested:
                        return {}
                    requested[row["id"]] = row
            if not requested or not self.history.is_file() or self.history.is_symlink():
                return {}
            initial = _database_identity(self.history)
            if any(initial[index] and initial[index][2] for index in (1, 2)):
                return {}
            connection = _read_only(self.history)
            snapshots, pending, page_bytes = {}, {}, 0
            for identifier, supplied_row in requested.items():
                self.statuses[identifier] = "unverified_completion_link"
                try:
                    link = links[identifier]
                    target = _row(connection, identifier)
                    source = _row(connection, link["source_message_id"])
                    if target is None or source is None:
                        continue
                    target_provenance = json.loads(target["provenance"])
                    source_provenance = json.loads(source["provenance"])
                    caller_provenance = supplied_row.get("provenance")
                    if isinstance(caller_provenance, str):
                        caller_provenance = json.loads(caller_provenance)
                    if supplied_row.get("text") != target["text"] or _json(
                        caller_provenance
                    ) != _json(target_provenance):
                        self.statuses[identifier] = "primary_changed"
                        continue
                    if not isinstance(target_provenance, dict) or not isinstance(
                        source_provenance, dict
                    ):
                        continue
                    retained_quote = target_provenance.get("quoted_components", [])
                    if retained_quote and _json(retained_quote) != _json(
                        source_provenance.get("quoted_components", [])
                    ):
                        continue
                    locator = source_provenance.get("locator")
                    if not isinstance(locator, dict) or _json(locator) != _json(
                        target_provenance.get("locator")
                    ):
                        continue
                    archive = locator.get("archive_sha256", "")
                    if not isinstance(archive, str) or _HEX.fullmatch(archive) is None:
                        continue
                    base = "html-" + archive[:24]
                    record_id = _identifier("html-text-v1", archive, _json(locator))
                    if (
                        source["partition_id"] != base + "-text-completion-v1"
                        or target["partition_id"] != base
                        or source_provenance.get("record_id") != record_id
                        or target_provenance.get("record_id") != record_id
                        or source["id"]
                        != _identifier(source["partition_id"], record_id)
                        or target["id"]
                        != _identifier(target["partition_id"], record_id)
                        or source_provenance.get("export_msgid_namespace")
                        != "MesLocalID"
                        or target_provenance.get("export_msgid_namespace")
                        != "MesLocalID"
                        or locator.get("export_msgid_namespace") != "MesLocalID"
                        or not isinstance(locator.get("export_msgid"), str)
                        or re.fullmatch(r"[0-9]{1,20}", locator["export_msgid"]) is None
                        or not source["peer_id"]
                        or source["peer_id"] != target["peer_id"]
                        or source["account_id"] != target["account_id"]
                        or source["kind"] != target["kind"]
                        or (
                            (source["sender_id"] or target["sender_id"])
                            and source["sender_id"] != target["sender_id"]
                        )
                        or source["direction"] != target["direction"]
                        or source["timestamp_value"] != target["timestamp_value"]
                    ):
                        continue
                    source_snapshot, source_metadata = _snapshot(
                        connection, source, source_provenance, snapshots
                    )
                    target_snapshot, target_metadata = _snapshot(
                        connection, target, target_provenance, snapshots
                    )
                    if any(
                        snapshot["snapshot_id"] != archive
                        for snapshot in (source_snapshot, target_snapshot)
                    ) or any(
                        metadata.get("source_sha256") != archive
                        for metadata in (source_metadata, target_metadata)
                    ):
                        continue
                    evidence = connection.execute(
                        "SELECT * FROM message_evidence WHERE id=? AND length(CAST(observation AS BLOB))<=?",
                        (link["evidence_id"], 1024 * 1024),
                    ).fetchone()
                    if evidence is None:
                        continue
                    observation = json.loads(evidence["observation"])
                    if not isinstance(observation, dict):
                        continue
                    cross = observation.get("cross_reference", {})
                    if (
                        set(observation)
                        != {"kind", "content_sha256", "cross_reference"}
                        or observation["kind"] != "export_cross_reference"
                        or not isinstance(cross, dict)
                        or cross.get("kind") != _KIND
                        or cross.get("global_message_identity_asserted") is not False
                        or cross.get("time_zone_inferred") is not False
                        or evidence["message_id"] != source["id"]
                        or evidence["source_id"] != source["partition_id"]
                        or evidence["extraction_record_id"] != record_id
                        or evidence["snapshot_id"] != archive
                        or evidence["match_kind"] != "cross_reference"
                        or evidence["id"]
                        != _identifier(
                            source["partition_id"], record_id, _json(observation)
                        )
                        or observation["content_sha256"] != _sha(source["text"])
                        or cross.get("content_sha256") != _sha(source["text"])
                        or any(
                            cross.get(key) != target[field]
                            for key, field in (
                                ("existing_message_id", "id"),
                                ("existing_source_id", "partition_id"),
                                ("existing_account_key", "account_key"),
                                ("existing_conversation_id", "conversation_id"),
                                ("existing_timestamp", "timestamp_value"),
                            )
                        )
                        or cross.get("existing_body_sha256") != _sha(target["text"])
                    ):
                        continue
                    pins = {
                        "target_text_sha256": _sha(target["text"]),
                        "source_text_sha256": _sha(source["text"]),
                        "source_provenance_sha256": _sha(_json(source_provenance)),
                    }
                    if any(
                        key in link and link[key] != value
                        for key, value in pins.items()
                    ):
                        continue
                    components = source_provenance.get("forwarded_components")
                    if (
                        not isinstance(components, list)
                        or not components
                        or not _retained_components_agree(
                            target_provenance.get("forwarded_components", []),
                            components,
                        )
                    ):
                        continue
                    page_bytes += len(source["text"].encode()) + len(
                        source["provenance"].encode()
                    )
                    if page_bytes > _PAGE_BYTES:
                        return {}
                    request = {
                        "id": source["id"],
                        "source_id": source["partition_id"],
                        "source_snapshot_id": archive,
                        "conversation_id": source["conversation_id"],
                        "account_id": source["account_id"],
                        "peer_id": source["peer_id"],
                        "conversation_kind": source["kind"],
                        "sender_id": source["sender_id"] or "",
                        "sender_label": source["sender_label"],
                        "text_sha256": _sha(source["text"]),
                        "timestamp_value": source["timestamp_value"],
                        "source_record_id": record_id,
                        "source_locator": locator,
                        "forwarded_components": components,
                        "is_system": False,
                    }
                    pending[identifier] = (
                        source,
                        source_provenance,
                        request,
                        link,
                        pins,
                        bool(retained_quote),
                    )
                except (ValueError, TypeError, KeyError, RecursionError):
                    continue
            resolved = (
                WechatContactRepository(self.root).resolve_forwarded_components(
                    [item[2] for item in pending.values()]
                )
                if pending
                else {}
            )
            names = WechatDisplayNames(self.root)
            result = {}
            for identifier, (
                source,
                provenance,
                request,
                link,
                pins,
                requires_quote,
            ) in pending.items():
                projection = resolved.get(source["id"], {})
                record = project_html_forwarded(
                    provenance,
                    parent_id=source["id"],
                    account_id=source["account_id"],
                    text=source["text"],
                    components=projection.get("components"),
                    source_proof_status=projection.get("status"),
                    display_names=names,
                )
                if record is not None:
                    linked = {
                        "forwarded_record": record,
                        "proof": {
                            "authority": _KIND,
                            "source_message_id": source["id"],
                            "evidence_id": link["evidence_id"],
                            **pins,
                        },
                    }
                    # Reuse only the quote roles proven by this completion source itself.
                    from .wechat_history_browser import _html_quoted_reply_presentation

                    quote = _html_quoted_reply_presentation(
                        source["text"], provenance, forwarded_record=record
                    )
                    if requires_quote and not quote:
                        continue
                    if quote:
                        record["title"] = quote["display_text"]
                        record["source_title"] = quote["display_text"]
                        linked["quoted_reply"] = quote["quoted_reply"]
                    result[identifier] = linked
                    self.statuses[identifier] = "source_verified_completion"
            return (
                result
                if _database_identity(self.history) == initial
                and _file_identity(path) == identity
                else {}
            )
        except (
            OSError,
            sqlite3.Error,
            ValueError,
            TypeError,
            KeyError,
            RecursionError,
            AttributeError,
        ):
            return {}
        finally:
            if connection is not None:
                connection.close()
