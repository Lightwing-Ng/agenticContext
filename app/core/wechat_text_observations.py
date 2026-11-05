"""Present pinned Mac text observations without modifying native message evidence.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from .wechat_archive import _identifier, wechat_archive_path
from .wechat_contacts import WechatContactRepository
from .wechat_display_names import WechatDisplayNames
from .wechat_forwarded import _child, _message, _native_owner, _read_only
from .wechat_reconcile import _database_identity, _json, native_text_metadata
from .wechat_resource_index import resource_index_path


_LIMIT = 1024 * 1024
_HEX = re.compile(r"[0-9a-f]{64}")
_AUTHORITY = "user_provided_mac_client_copied_text"
_LABEL = "Mac client text supplied by user; native reference IDs unavailable."


def canonical_provenance_sha256(provenance):
    """Bind the complete source object independently of its JSON whitespace."""
    return hashlib.sha256(_json(provenance).encode()).hexdigest()


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _identifier_field(value):
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate text observation field.")
        result[key] = value
    return result


def _file_identity(path):
    return _stat_identity(path.stat(follow_symlinks=False), path)


def _stat_identity(state, path, *, descriptor=None):
    identity = (
        state.st_dev,
        state.st_ino,
        state.st_size,
        state.st_mtime_ns,
        getattr(state, "st_birthtime_ns", state.st_ctime_ns)
        if os.name == "nt" else state.st_ctime_ns,
    )
    if os.name == "nt":
        from .windows_file_metadata import file_change_time

        return (*identity, file_change_time(path, descriptor=descriptor))
    return identity


def _managed_path(base, relative):
    if (
        not isinstance(relative, (str, Path))
        or "\0" in str(relative)
        or (os.name != "nt" and "\\" in str(relative))
    ):
        raise ValueError("Invalid managed observation path.")
    requested = Path(relative)
    if ".." in requested.parts:
        raise ValueError("Observation paths must stay inside their managed root.")
    path = requested if requested.is_absolute() else base / requested
    if not path.resolve().is_relative_to(base.resolve()):
        raise ValueError("Observation paths must stay inside their managed root.")
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise ValueError("Observation paths must not contain symbolic links.")
    return path


def _read_json(path):
    before = _file_identity(path)
    if not path.is_file() or before[2] > _LIMIT:
        raise ValueError("Text observation files must be bounded regular files.")
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    )
    with os.fdopen(descriptor, "rb") as stream:
        if _stat_identity(
            os.fstat(stream.fileno()), path, descriptor=stream.fileno()
        ) != before:
            raise ValueError("Text observation file changed before opening.")
        raw = stream.read(_LIMIT + 1)
        if _stat_identity(
            os.fstat(stream.fileno()), path, descriptor=stream.fileno()
        ) != before:
            raise ValueError("Text observation file changed while reading.")
    if len(raw) > _LIMIT or _file_identity(path) != before:
        raise ValueError("Text observation file changed while reading.")
    return (
        json.loads(raw, object_pairs_hook=_unique),
        hashlib.sha256(raw).hexdigest(),
        before,
    )


def _slice_schema(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"part", "start", "end", "sha256"}
        or any(type(value.get(key)) is not int for key in ("part", "start", "end"))
        or not 0 <= value["part"] < 1000
        or not 0 <= value["start"] < value["end"] <= _LIMIT
        or not _identifier_field(value["sha256"])
    ):
        raise ValueError("Invalid Unicode-codepoint observation slice.")


def _observation_schema(value):
    required = {
        "id",
        "authority",
        "message_id",
        "account_id",
        "peer_id",
        "text_sha256",
        "provenance_sha256",
        "source",
        "reply",
        "items",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value["authority"] != _AUTHORITY
    ):
        raise ValueError("Unsupported text observation declaration.")
    if any(
        not _identifier_field(value[key])
        for key in ("id", "message_id", "text_sha256", "provenance_sha256")
    ):
        raise ValueError("Text observations need exact source hashes and identifiers.")
    if any(
        not isinstance(value[key], str)
        or not value[key]
        or len(value[key]) > 256
        or "\0" in value[key]
        for key in ("account_id", "peer_id")
    ):
        raise ValueError("Text observations need an exact account and conversation.")
    source = value["source"]
    if (
        not isinstance(source, dict)
        or set(source) != {"path", "sha256"}
        or not isinstance(source["path"], str)
        or not _identifier_field(source["sha256"])
    ):
        raise ValueError("Text observations need a pinned source artifact.")
    path = Path(source["path"])
    if (
        path.is_absolute()
        or ".." in path.parts
        or not path.parts
        or path.parts[0] != "repair_audits"
        or path.suffix != ".json"
    ):
        raise ValueError("Text observation artifacts must stay inside repair audits.")
    _slice_schema(value["reply"])
    if not isinstance(value["items"], list) or not 1 <= len(value["items"]) <= 100:
        raise ValueError("Text observation items exceed the supported bounds.")
    for item in value["items"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("peer_id"), str)
            or not item["peer_id"]
        ):
            raise ValueError(
                "Observation items need their original conversation scope."
            )
        kind = item.get("kind")
        fields = {"kind", "peer_id", "provenance_sha256"}
        if kind == "resource":
            fields |= {"message_id"}
        elif kind == "message":
            fields |= {"message_id", "text_sha256"}
        elif kind == "source_excerpt":
            fields |= {"anchor_resource_id", "excerpt"}
            _slice_schema(item.get("excerpt"))
        else:
            raise ValueError("Unsupported text observation item kind.")
        if (
            set(item) != fields
            or not _identifier_field(item["provenance_sha256"])
            or not _identifier_field(
                item.get("message_id", item.get("anchor_resource_id"))
            )
        ):
            raise ValueError(
                "Observation items need exact canonical identifiers and hashes."
            )
        if kind == "message" and not _identifier_field(item["text_sha256"]):
            raise ValueError("Canonical text observations need an exact body hash.")


def _parts(artifact):
    if (
        not isinstance(artifact, dict)
        or artifact.get("native_forward_item_source_ids_observed") is not False
    ):
        raise ValueError(
            "Mac observations must not claim native forwarding identities."
        )
    parts = artifact.get("user_input_representation")
    if not isinstance(parts, list) or not 1 <= len(parts) <= 1000:
        raise ValueError("Mac observations need bounded structured input parts.")
    for part in parts:
        if not isinstance(part, dict):
            raise ValueError("Mac input parts must be objects.")
        if part.get("kind") == "literal":
            if set(part) != {"kind", "text"} or not isinstance(part["text"], str):
                raise ValueError("Mac literal input parts must remain plain strings.")
        elif part.get("kind") == "archive_message_body":
            if (
                set(part) != {"kind", "message_id", "utf8_sha256"}
                or not _identifier_field(part["message_id"])
                or not _identifier_field(part["utf8_sha256"])
            ):
                raise ValueError("Mac archive parts must reference existing bodies.")
        else:
            raise ValueError("Unsupported Mac input part kind.")
    return parts


def _slice(parts, reference):
    _slice_schema(reference)
    if reference["part"] >= len(parts):
        raise ValueError("The observation slice part is missing.")
    part = parts[reference["part"]]
    if part["kind"] != "literal" or reference["end"] > len(part["text"]):
        raise ValueError("Observation slices must address an existing literal part.")
    text = part["text"][reference["start"] : reference["end"]]
    if _sha(text) != reference["sha256"]:
        raise ValueError("The observation slice hash differs.")
    return text


class WechatTextObservations:
    """Read a private registry once; apply only fully bound presentation overlays."""

    def __init__(self, local_store_root, registry_path=None):
        self.root = Path(local_store_root)
        self.base = wechat_archive_path(self.root).parent
        self.observations = {}
        self.registry_path = None
        self.registry_identity = None
        try:
            self.registry_path = _managed_path(
                self.base,
                registry_path
                if registry_path is not None
                else "text_observations.json",
            )
            registry, _digest, self.registry_identity = _read_json(self.registry_path)
            if (
                not isinstance(registry, dict)
                or set(registry) != {"schema_version", "observations"}
                or type(registry["schema_version"]) is not int
                or registry["schema_version"] != 1
                or not isinstance(registry["observations"], list)
                or len(registry["observations"]) > 100
            ):
                raise ValueError("Unsupported text observation registry.")
            identifiers = set()
            for observation in registry["observations"]:
                _observation_schema(observation)
                if (
                    observation["id"] in identifiers
                    or observation["message_id"] in self.observations
                ):
                    raise ValueError("Ambiguous text observation binding.")
                identifiers.add(observation["id"])
                self.observations[observation["message_id"]] = observation
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            self.observations = {}

    def project(self, message_id, account_id, peer_id, text, provenance):
        observation = (
            self.observations.get(message_id) if isinstance(message_id, str) else None
        )
        if observation is None:
            return None
        history = resources = None
        try:
            if (
                observation["account_id"] != account_id
                or observation["peer_id"] != peer_id
                or not isinstance(text, str)
                or not isinstance(provenance, dict)
            ):
                return None
            if (
                _sha(text) != observation["text_sha256"]
                or canonical_provenance_sha256(provenance)
                != observation["provenance_sha256"]
            ):
                return None
            path = wechat_archive_path(self.root)
            initial = _database_identity(path)
            if any(initial[index] and initial[index][2] for index in (1, 2)):
                return None
            history = _read_only(path)
            parent = _message(history, message_id)
            if (
                parent is None
                or not _native_owner(parent)
                or parent["account_id"] != account_id
                or parent["peer_id"] != peer_id
                or parent["text"] != text
            ):
                return None
            saved = json.loads(parent["provenance"])
            if (
                canonical_provenance_sha256(saved) != observation["provenance_sha256"]
                or type(saved.get("message_type")) is not int
                or saved["message_type"] != 49
                or saved.get("text_extraction_kind") != "quoted_reply"
                or not isinstance(saved.get("locator", {}), dict)
            ):
                return None
            metadata = native_text_metadata(saved, saved.get("locator", {}))
            reference = metadata.get("quoted_reference", {})
            if (
                reference.get("content_unavailable") is not True
                or reference.get("relationship") != "forwarded_conversation"
            ):
                return None
            source_path = _managed_path(self.base, observation["source"]["path"])
            artifact, artifact_sha, artifact_identity = _read_json(source_path)
            if artifact_sha != observation["source"]["sha256"]:
                return None
            if (
                "corroborated_reply_candidate_id" in artifact
                and artifact["corroborated_reply_candidate_id"] != message_id
            ):
                return None
            parts = _parts(artifact)
            reply = _slice(parts, observation["reply"])
            resource_path = resource_index_path(self.root)
            resource_identity = None
            children, requests, bodies = [], [], {}
            body_bytes = len(reply.encode())
            snapshots = {}
            has_snapshots = (
                history.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_snapshots'"
                ).fetchone()
                is not None
            )
            for ordinal, item in enumerate(observation["items"], start=1):
                kind = item["kind"]
                resource = None
                canonical_id = item.get("message_id", item.get("anchor_resource_id"))
                if kind == "message":
                    if canonical_id == message_id:
                        return None
                    row = _message(history, canonical_id)
                    if row is None or not _native_owner(row):
                        return None
                    body = row["text"]
                    if _sha(body) != item["text_sha256"] or not any(
                        part.get("kind") == "archive_message_body"
                        and part.get("message_id") == canonical_id
                        and part.get("utf8_sha256") == item["text_sha256"]
                        for part in parts
                    ):
                        return None
                    bodies[canonical_id] = body
                else:
                    if resources is None:
                        if resource_path.is_symlink():
                            return None
                        resource_identity = _database_identity(resource_path)
                        if any(
                            resource_identity[index] and resource_identity[index][2]
                            for index in (1, 2)
                        ):
                            return None
                        resources = _read_only(resource_path)
                    candidate = resources.execute(
                        "SELECT m.*,c.partition_id,c.account_key,c.peer_id,c.kind FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.id=?",
                        (canonical_id,),
                    ).fetchone()
                    if candidate is None:
                        return None
                    row = dict(candidate)
                    row.update(
                        {
                            key: parent[key]
                            for key in (
                                "account_id",
                                "partition_account_key",
                                "identity_status",
                            )
                        }
                    )
                    if not _native_owner(row):
                        return None
                    body = (
                        _slice(parts, item["excerpt"])
                        if kind == "source_excerpt"
                        else ""
                    )
                if row["account_id"] != account_id or row["peer_id"] != item["peer_id"]:
                    return None
                evidence = json.loads(row["provenance"])
                if canonical_provenance_sha256(evidence) != item["provenance_sha256"]:
                    return None
                if kind != "message":
                    native_type = evidence.get("message_type")
                    native_reference = evidence.get("resource_reference", {})
                    if (
                        type(native_type) is not int
                        or native_type != (49 if kind == "source_excerpt" else 3)
                        or evidence.get("text_extraction_kind")
                        != "resource_placeholder"
                        or not isinstance(native_reference, dict)
                        or native_reference.get("account_id") != account_id
                        or native_reference.get("peer_id") != item["peer_id"]
                    ):
                        return None
                    if kind == "resource":
                        if native_reference.get("kind") != "image":
                            return None
                        resource = {
                            "kind": "image",
                            "kind_label": "Image",
                            "label": "Image",
                            "status": "metadata_only",
                            "status_label": "Metadata only",
                            "action_label": "",
                            "can_reveal": False,
                        }
                body_bytes += len(body.encode())
                if body_bytes > _LIMIT:
                    return None
                flags = json.loads(row["quality_flags"])
                if not isinstance(flags, list) or any(
                    not isinstance(flag, str) for flag in flags
                ):
                    return None
                child_provenance = (
                    evidence
                    if kind != "source_excerpt"
                    else {
                        "authority": _AUTHORITY,
                        "source": observation["source"],
                        "excerpt": item["excerpt"],
                        "native_metadata_anchor": {
                            "resource_id": canonical_id,
                            "provenance_sha256": item["provenance_sha256"],
                        },
                        "native_forward_item_source_ids_observed": False,
                    }
                )
                child = _child(
                    stable_id=_identifier(
                        message_id, observation["id"], str(ordinal), canonical_id
                    ),
                    ordinal=ordinal,
                    text=body,
                    author=row["sender_label"],
                    sender=row["sender_id"] or "",
                    direction=row["direction"],
                    timestamp=row["timestamp_value"],
                    label=row["timestamp_label"] if row["timestamp_value"] else "",
                    basis=row["time_basis"],
                    provenance=child_provenance,
                    flags=(*flags, "user_provided_mac_text_observation"),
                    conversation_id=row["conversation_id"],
                    account_id=account_id,
                    peer_id=row["peer_id"],
                    resource=resource,
                )
                if kind == "source_excerpt":
                    child.messaging["observation_anchor_resource_id"] = canonical_id
                else:
                    child.messaging["canonical_message_id"] = canonical_id
                snapshot_id = evidence.get("backup_id", "")
                snapshot = evidence.get("source_snapshot")
                if (
                    has_snapshots
                    and type(snapshot) is int
                    and 0 < snapshot <= 2**63 - 1
                ):
                    key = snapshot, row["partition_id"]
                    if key not in snapshots:
                        receipt = history.execute(
                            "SELECT snapshot_id FROM source_snapshots WHERE id=? AND source_id=?",
                            key,
                        ).fetchone()
                        snapshots[key] = receipt[0] if receipt is not None else ""
                    snapshot_id = snapshots[key]
                requests.append(
                    {
                        "id": child.stable_id,
                        "source_id": row["partition_id"],
                        "source_snapshot_id": snapshot_id,
                        "account_id": account_id,
                        "conversation_id": row["conversation_id"],
                        "peer_id": row["peer_id"],
                        "conversation_kind": row["kind"],
                        "sender_id": row["sender_id"] or "",
                        "sender_label": row["sender_label"],
                        "source_record_id": evidence.get("record_id", ""),
                        "source_locator": evidence.get("locator", {}),
                        "timestamp_value": row["timestamp_value"],
                        "is_system": False,
                    }
                )
                children.append(child)
            if "user_input_utf8_sha256" in artifact:
                reconstructed = "".join(
                    part["text"]
                    if part["kind"] == "literal"
                    else bodies[part["message_id"]]
                    for part in parts
                )
                if _sha(reconstructed) != artifact["user_input_utf8_sha256"]:
                    return None
            contacts = WechatContactRepository(self.root).resolve_messages(requests)
            names = WechatDisplayNames(self.root)
            for child in children:
                child.messaging["contact"] = contacts[child.stable_id]
                child.messaging["viewer_display_name"] = names.contact_name(
                    contacts[child.stable_id]
                )
            if (
                _database_identity(path) != initial
                or _file_identity(self.registry_path) != self.registry_identity
                or _file_identity(source_path) != artifact_identity
                or (
                    resource_identity is not None
                    and _database_identity(resource_path) != resource_identity
                )
            ):
                return None
            return {
                "display_text": reply,
                "quoted_reply": {
                    "speaker": "Quoted forwarded conversation",
                    "text": "",
                    "forwarded_record": {
                        "title": "Forwarded conversation",
                        "items": tuple(children),
                        "authority": _AUTHORITY,
                    },
                },
                "text_observation": {"id": observation["id"], "label": _LABEL},
            }
        except (
            OSError,
            sqlite3.Error,
            ValueError,
            TypeError,
            KeyError,
            RecursionError,
        ):
            return None
        finally:
            if resources is not None:
                resources.close()
            if history is not None:
                history.close()
