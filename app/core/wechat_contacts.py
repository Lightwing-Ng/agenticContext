"""Resolve WeChat identities while retaining account-specific contact observations.

Code version: v1.2.0-codex.0

A contact identifies a platform identity, not an assumed real-world person.
Syntax classifies identifiers; only explicit native evidence authorizes linking.
"""

from __future__ import annotations

from collections.abc import Iterable
import hashlib
from itertools import islice
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import sqlite3
from urllib.parse import unquote, urlsplit


CANONICAL_AUTHORITIES = frozenset(
    {
        "native_contact_username",
        "native_message_sender",
        "native_account_owner",
        "native_peer_username",
        "corroborated_export_username",
    }
)
OBSERVATION_KINDS = frozenset(
    {
        "nickname",
        "remark",
        "alias",
        "room_display_name",
        "source_sender_label",
    }
)
MAX_OBSERVATIONS = 64
MAX_EVIDENCE_BYTES = 64 * 1024
MESSAGE_BINDING_AUTHORITY = "corroborated_message_sender"
FORWARDED_BINDING_AUTHORITY = "source_verified_html_forwarded_avatar"
MAX_FORWARDED_DEPTH = 32
MAX_FORWARDED_COMPONENTS = 4_096
MAX_FORWARDED_PAGE_COMPONENTS = 16_384
MAX_FORWARDED_PARENTS = 1_024
MAX_FORWARDED_PROOF_BYTES = 16 * 1024**2
MESSAGE_CONTACT_SCHEMA = """
CREATE TABLE message_contact_bindings (
 source_id TEXT NOT NULL, message_id TEXT NOT NULL, text_sha256 TEXT NOT NULL,
 timestamp_value TEXT NOT NULL, source_record_id TEXT NOT NULL,
 contact_id TEXT NOT NULL, authority TEXT NOT NULL, evidence TEXT NOT NULL,
 PRIMARY KEY(source_id,message_id,contact_id)
) WITHOUT ROWID;
"""
CONTACT_SCHEMA = (
    """
CREATE TABLE contacts (
 id TEXT PRIMARY KEY, namespace TEXT NOT NULL, realm TEXT NOT NULL,
 username TEXT NOT NULL, kind TEXT NOT NULL, identity_status TEXT NOT NULL
);
CREATE INDEX contacts_identity ON contacts(namespace,realm,username);
CREATE TABLE contact_identifiers (
 contact_id TEXT NOT NULL, namespace TEXT NOT NULL, value TEXT NOT NULL,
 realm TEXT NOT NULL, authority TEXT NOT NULL,
 PRIMARY KEY(contact_id,namespace,value,realm,authority)
) WITHOUT ROWID;
CREATE TABLE contact_observations (
 id TEXT PRIMARY KEY, contact_id TEXT NOT NULL, source_id TEXT NOT NULL,
 source_snapshot TEXT NOT NULL, owner_account_id TEXT NOT NULL,
 room_id TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL,
 evidence TEXT NOT NULL
);
CREATE INDEX contact_observations_contact ON contact_observations(contact_id,id);
CREATE TABLE sender_bindings (
 source_id TEXT NOT NULL, sender_id TEXT NOT NULL,
 source_snapshot TEXT NOT NULL DEFAULT '', room_id TEXT NOT NULL DEFAULT '',
 contact_id TEXT NOT NULL, authority TEXT NOT NULL, evidence TEXT NOT NULL,
 PRIMARY KEY(source_id,sender_id,source_snapshot,room_id)
) WITHOUT ROWID;
CREATE TABLE contact_registry_info(key TEXT PRIMARY KEY,value TEXT NOT NULL);
"""
    + MESSAGE_CONTACT_SCHEMA
)
_SERVICES = frozenset(
    {
        "filehelper",
        "weixin",
        "fmessage",
        "medianote",
        "newsapp",
        "qqmail",
        "tmessage",
        "qmessage",
        "feedsapp",
        "weibo",
        "floatbottle",
        "notifymessage",
        "brandsessionholder",
        "weixinreminder",
    }
)


def _json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(*parts):
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _identifier(value, name):
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\0" in value
        or len(value.encode()) > 1024
    ):
        raise ValueError(f"A nonempty exact {name} is required.")
    return value


def classify_wechat_identifier(value: str) -> dict:
    """Classify an observed value without asserting that it is an identity."""
    if (
        not isinstance(value, str)
        or not value
        or "\0" in value
        or any(char.isspace() for char in value)
    ):
        category, kind = "unclassified", "unresolved"
    elif re.fullmatch(r"wxid_unknwn_[0-9]+", value):
        category, kind = "synthetic_placeholder", "unresolved"
    elif value.endswith(("@chatroom", "@im.chatroom")):
        category, kind = "room_username", "group"
    elif value.endswith("@openim"):
        category, kind = "openim_username", "openim_contact"
    elif re.fullmatch(r"wxid_[A-Za-z0-9_]+", value):
        category, kind = "wxid_username", "wechat_account"
    elif re.fullmatch(r"gh_[A-Za-z0-9_]+", value):
        category, kind = "official_username", "official_account"
    elif value in _SERVICES:
        category, kind = "service_username", "service"
    elif re.fullmatch(r"[0-9a-fA-F]{32}", value):
        category, kind = "opaque_hash", "unresolved"
    elif re.fullmatch(r"\+?[0-9]+", value):
        category, kind = "numeric_or_phone_value", "unresolved"
    elif re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{1,127}", value):
        category, kind = "legacy_username_candidate", "wechat_identity"
    else:
        category, kind = "unclassified", "unresolved"
    return {
        "category": category,
        "kind": kind,
        "requires_realm": category == "openim_username",
    }


def canonical_contact_id(
    username: str, *, authority: str, namespace: str = "wechat", realm: str = ""
) -> str:
    """Key exact native identity, including an OpenIM application realm."""
    _identifier(username, "native username")
    if any(character.isspace() for character in username):
        raise ValueError("Native usernames cannot contain whitespace.")
    if authority not in CANONICAL_AUTHORITIES:
        raise ValueError("A canonical contact requires native identity evidence.")
    if re.fullmatch(r"wxid_unknwn_[0-9]+", username) and authority not in {
        "native_contact_username",
        "native_account_owner",
    }:
        raise ValueError(
            "Exporter placeholders require direct native identity evidence."
        )
    if (
        namespace not in {"wechat", "openim"}
        or not isinstance(realm, str)
        or "\0" in realm
    ):
        raise ValueError("Unsupported identity namespace or realm.")
    if username.endswith("@openim"):
        if namespace != "openim" or not realm:
            raise ValueError("OpenIM usernames require their application realm.")
        _identifier(realm, "OpenIM application realm")
    elif namespace != "wechat" or realm:
        raise ValueError("WeChat usernames use the unqualified WeChat namespace.")
    return "wechat:contact:" + _digest(namespace, realm, username)


def provisional_contact_id(
    source_id: str, conversation_id: str, message_id: str
) -> str:
    """Keep unidentified utterances separate instead of merging generic labels."""
    for value, name in (
        (source_id, "source ID"),
        (conversation_id, "conversation ID"),
        (message_id, "message ID"),
    ):
        _identifier(value, name)
    return "wechat:contact:unresolved:" + _digest(
        source_id, conversation_id, message_id
    )


def source_contact_id(
    source_id: str,
    sender_id: str,
    *,
    conversation_id: str = "",
    source_snapshot: str = "",
) -> str:
    """Key an explicitly source-local identifier without asserting a native identity."""
    _identifier(source_id, "source ID")
    _identifier(sender_id, "source sender ID")
    if any(
        not isinstance(scope, str) or "\0" in scope
        for scope in (conversation_id, source_snapshot)
    ):
        raise ValueError("Invalid conversation scope.")
    return "wechat:contact:source:" + _digest(
        source_id, source_snapshot, conversation_id, sender_id
    )


def contact_observation(
    contact_id: str,
    *,
    source_id: str,
    kind: str,
    value: str,
    source_snapshot: str = "",
    owner_account_id: str = "",
    room_id: str = "",
    evidence: dict | None = None,
) -> dict:
    """Retain a name version with its exact account, snapshot, and room scope."""
    _identifier(contact_id, "contact ID")
    _identifier(source_id, "source ID")
    if (
        kind not in OBSERVATION_KINDS
        or not isinstance(value, str)
        or "\0" in value
        or len(value.encode()) > 64 * 1024
    ):
        raise ValueError("Unsupported contact name observation.")
    if any(
        not isinstance(scope, str) or "\0" in scope
        for scope in (source_snapshot, owner_account_id, room_id)
    ):
        raise ValueError("Contact scopes must be exact strings.")
    if evidence is not None and not isinstance(evidence, dict):
        raise ValueError("Contact evidence must be an object.")
    encoded = _json(evidence or {})
    if len(encoded.encode()) > MAX_EVIDENCE_BYTES:
        raise ValueError("Contact evidence exceeds the supported size.")
    result = {
        "contact_id": contact_id,
        "source_id": source_id,
        "source_snapshot": source_snapshot,
        "owner_account_id": owner_account_id,
        "room_id": room_id,
        "kind": kind,
        "value": value,
        "evidence": encoded,
    }
    return {"id": _digest(_json(result)), **result}


def contact_registry_path(local_store_root: Path | str) -> Path:
    return (
        Path(local_store_root).expanduser()
        / "messaging"
        / "wechat"
        / "contacts.sqlite3"
    )


def initialize_contact_registry(connection: sqlite3.Connection) -> None:
    """Initialize an empty, caller-owned staging database; never open live data."""
    connection.executescript(CONTACT_SCHEMA)
    connection.execute("INSERT INTO contact_registry_info VALUES('schema_version','2')")


def _object_evidence(encoded):
    if not isinstance(encoded, str) or len(encoded.encode()) > MAX_EVIDENCE_BYTES:
        return {}
    try:
        value = json.loads(encoded)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _message_bindings(connection, messages):
    """Read optional per-message evidence without caching every requested ID."""
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_contact_bindings'"
        ).fetchone()
        is None
    ):
        return {}
    sources = {
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT source_id FROM message_contact_bindings"
        )
    }
    if not sources:
        return {}
    result = {}
    for offset in range(0, len(messages), 400):
        by_source = {}
        for message in messages[offset : offset + 400]:
            if message["source_id"] in sources and message.get("is_system") is not True:
                by_source.setdefault(message["source_id"], []).append(message["id"])
        for source_id, identifiers in by_source.items():
            placeholders = ",".join("?" for _ in identifiers)
            for row in connection.execute(
                "SELECT * FROM message_contact_bindings WHERE source_id=? AND message_id IN ("
                + placeholders
                + ")",
                [source_id, *identifiers],
            ):
                result.setdefault((source_id, row["message_id"]), []).append(dict(row))
    return result


def _matching_message_proofs(message, bindings, room, snapshot):
    digest = message.get("text_sha256")
    timestamp = message.get("timestamp_value")
    record = message.get("source_record_id")
    if (
        not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or not isinstance(timestamp, str)
        or not isinstance(record, str)
        or not record
    ):
        return []
    result = []
    for binding in bindings:
        if (
            binding["authority"] != MESSAGE_BINDING_AUTHORITY
            or binding["text_sha256"] != digest
            or binding["timestamp_value"] != timestamp
            or binding["source_record_id"] != record
        ):
            continue
        result.append({**binding, "source_snapshot": snapshot, "room_id": room})
    return result


def _fallback(message, reason):
    system = message.get("is_system") is True
    label = str(message.get("sender_label") or "")
    sender = message.get("sender_id")
    raw_identifier = (
        [
            {
                "namespace": "source_sender_id",
                "realm": message["source_id"],
                "value": sender,
                "authority": "unverified_source_field",
            }
        ]
        if isinstance(sender, str) and sender
        else []
    )
    locator = message.get("source_locator")
    locator = locator if isinstance(locator, dict) else {}
    evidence = {
        "authority": "source_message_locator",
        "source_id": message["source_id"],
        "source_snapshot": message.get("source_snapshot_id") or "",
        "conversation_id": message["conversation_id"],
        "message_id": message["id"],
        "source_record_id": message.get("source_record_id") or "",
        "room_id": message.get("peer_id")
        if message.get("conversation_kind") == "group"
        else "",
        "details": locator,
    }
    return {
        "id": "wechat:contact:system"
        if system
        else provisional_contact_id(
            message["source_id"], message["conversation_id"], message["id"]
        ),
        "platform": "wechat",
        "namespace": "system" if system else "unresolved",
        "realm": "",
        "kind": "system" if system else "unresolved",
        "identity_status": "system" if system else "unresolved",
        "identifiers": raw_identifier,
        "display_name": "System" if system else label or "Unknown sender",
        "source_display_name": label,
        "preferred_name": label or ("System" if system else "Unknown sender"),
        "names": [],
        "remarks": [],
        "evidence": [evidence],
        "observation_count": 0,
        "observations_truncated": False,
        "resolution_reason": "system_event" if system else reason,
    }


def _forwarded_safe_path(value, *, canonical=True):
    if not value or "\\" in value or any(ord(char) < 32 for char in value):
        raise ValueError("Unsafe ZIP member path.")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or re.match(r"^[A-Za-z]:", value):
        raise ValueError("Unsafe ZIP member path.")
    if canonical and any(part in {"", "."} for part in value.rstrip("/").split("/")):
        raise ValueError("Noncanonical ZIP member paths are ambiguous.")


def _forwarded_local_reference(member_path, value):
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return None
    relative = unquote(parsed.path)
    try:
        _forwarded_safe_path(relative, canonical=False)
    except ValueError:
        return None
    resolved = posixpath.normpath(
        posixpath.join(str(PurePosixPath(member_path).parent), relative)
    )
    try:
        _forwarded_safe_path(resolved)
    except ValueError:
        return None
    return resolved


def _forwarded_native_avatar_id(value):
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return ""
    decoded = unquote(parsed.path)
    try:
        _forwarded_safe_path(decoded, canonical=False)
    except ValueError:
        return ""
    path = PurePosixPath(decoded)
    if path.parent.name != "Portrait" or path.suffix.lower() != ".jpg":
        return ""
    return path.stem


def _forwarded_projection(components):
    """Copy a bounded presentation tree without changing persisted hierarchy evidence."""
    projected, flat = [], []
    pending = [(components, projected, (), 1)]
    while pending:
        values, target, prefix, depth = pending.pop()
        if not isinstance(values, list) or depth > MAX_FORWARDED_DEPTH:
            raise ValueError(
                "Forwarded component structure exceeds the supported bounds."
            )
        for index, value in enumerate(values):
            if not isinstance(value, dict) or len(flat) >= MAX_FORWARDED_COMPONENTS:
                raise ValueError(
                    "Forwarded component structure exceeds the supported bounds."
                )
            path = (*prefix, index)
            item = dict(value)
            children = []
            item["forwarded_components"] = children
            item.pop("contact", None)
            target.append(item)
            flat.append((item, value, path))
            nested = value.get("forwarded_components", [])
            if not isinstance(nested, list):
                raise ValueError("Forwarded children must remain a list.")
            if nested:
                pending.append((nested, children, path, depth + 1))
    return projected, flat


def _forwarded_source_members(source):
    members = source.get("selected_text_members")
    if not isinstance(members, list):
        raise ValueError("A reviewed HTML source needs selected member receipts.")
    result = {}
    for member in members:
        path = member.get("member_path") if isinstance(member, dict) else None
        if (
            not isinstance(path, str)
            or path in result
            or not re.fullmatch(r"[0-9a-f]{64}", str(member.get("sha256", "")))
            or type(member.get("size")) is not int
            or not 0 <= member["size"] <= MAX_FORWARDED_PROOF_BYTES
        ):
            raise ValueError("Invalid selected HTML member evidence.")
        result[path] = member
    return result


def _forwarded_member_context(source, conversation, locator):
    path = locator.get("member_path")
    page = locator.get("page")
    if (
        not isinstance(path, str)
        or type(page) is not int
        or page < 0
        or type(locator.get("message_ordinal")) is not int
        or locator["message_ordinal"] < 1
        or type(locator.get("page_message_ordinal")) is not int
        or locator["page_message_ordinal"] < 1
    ):
        return None
    _forwarded_safe_path(path)
    matches = []
    for entry in source.get("source_conversation_catalog", []):
        if not isinstance(entry, dict):
            raise ValueError("Invalid HTML conversation evidence.")
        if (
            entry.get("index_avatar_native_id", "") != conversation["peer_id"]
            or entry.get("conversation_kind") != conversation["kind"]
        ):
            continue
        main = entry.get("member_path", "")
        data = entry.get("declared_async_counts", {}).get("data_path", "")
        data_path = _forwarded_local_reference(main, data) if data else None
        expected = main if page == 0 else f"{data_path}/msg-{page}.js"
        if path == expected:
            matches.append((main, data_path))
    return matches[0] if len(matches) == 1 else None


def _forwarded_native_proof(source, conversation, members, component, parent_context):
    if component.get("is_system", False) is not False or component.get("text_kind") in {
        "notice",
        "system_text",
    }:
        return None
    native = component.get("sender_native_id")
    if (
        not isinstance(native, str)
        or not native
        or native.endswith("@openim")
        or component.get("realm", "") != ""
        or component.get("namespace", "wechat") != "wechat"
    ):
        return None
    # An exporter-generated unknown username is not an identity assertion.
    canonical = canonical_contact_id(native, authority="native_message_sender")
    avatar = component.get("avatar_path", "")
    if not isinstance(avatar, str) or _forwarded_native_avatar_id(avatar) != native:
        return None
    locator = component.get("source_locator")
    if not isinstance(locator, dict) or locator.get("member_path") not in members:
        return None
    context = _forwarded_member_context(source, conversation, locator)
    if context is None or context != parent_context:
        return None
    main, data = context
    portrait = _forwarded_local_reference(main, avatar)
    resource_root = (
        PurePosixPath(data).parent
        if data
        else PurePosixPath(main)
        .with_suffix("")
        .with_name(PurePosixPath(main).stem + "_files")
    )
    if portrait is None or PurePosixPath(portrait).parent != resource_root / "Portrait":
        return None
    return {
        "contact_id": canonical,
        "native_username": native,
        "authority": FORWARDED_BINDING_AUTHORITY,
        "source_locator": locator,
        "selected_member": members[locator["member_path"]],
        "avatar_path": avatar,
        "archive_sha256": source["source_sha256"],
    }


class WechatContactRepository:
    """Resolve a page from a read-only identity sidecar without rewriting messages."""

    def __init__(self, local_store_root: Path | str):
        self.path = contact_registry_path(local_store_root)

    def resolve_forwarded_components(self, parent_messages: Iterable[dict]) -> dict:
        """Project canonical nested contacts from exact published HTML evidence only.

        The caller cannot authorize a native hint. Parent PK, source, account,
        room, snapshot, original tree, member receipt, and avatar must all agree
        with the read-only history database. Runtime paths address presentation
        nodes; they do not assert the original immediate forwarding hierarchy.
        """
        parents = list(islice(parent_messages, MAX_FORWARDED_PARENTS + 1))
        if len(parents) > MAX_FORWARDED_PARENTS:
            return {
                parent["id"]: {"components": [], "status": "parent_request_limit"}
                for parent in parents
            }
        result, requests, proofs = {}, [], {}
        history_path = self.path.with_name("history.sqlite3")
        connection = None

        def file_identity():
            state = history_path.stat()
            return (
                state.st_dev,
                state.st_ino,
                state.st_size,
                state.st_mtime_ns,
                state.st_ctime_ns,
            )

        initial = file_identity() if history_path.is_file() else None
        component_count = 0
        component_bytes = 0
        try:
            if initial is not None and not history_path.is_symlink():
                connection = sqlite3.connect(
                    history_path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
                )
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
            snapshots = {}
            for parent in parents:
                identifier = parent.get("id")
                _identifier(identifier, "parent message ID")
                try:
                    tree, flat = _forwarded_projection(
                        parent.get("forwarded_components", [])
                    )
                    component_count += len(flat)
                    component_bytes += len(
                        _json(parent.get("forwarded_components", [])).encode()
                    )
                    if (
                        component_bytes > MAX_FORWARDED_PROOF_BYTES
                        or component_count > MAX_FORWARDED_PAGE_COMPONENTS
                    ):
                        raise ValueError("Forwarded proof exceeds the supported size.")
                except (ValueError, TypeError, RecursionError):
                    result[identifier] = {
                        "components": [],
                        "status": "component_resource_limit",
                    }
                    continue
                result[identifier] = {
                    "components": tree,
                    "status": "unverified_source_proof",
                }
                scoped = []
                for item, original, path in flat:
                    locator = original.get("source_locator", {})
                    key = _digest(
                        identifier,
                        "forwarded",
                        _json(locator),
                        str(original.get("export_msgid", "")),
                        _json(path),
                    )
                    request = {
                        **parent,
                        "id": key,
                        "sender_id": original.get("sender_export_id", ""),
                        "sender_label": original.get("sender_label", ""),
                        "source_locator": locator,
                        "is_system": original.get("is_system") is True
                        or original.get("text_kind") in {"notice", "system_text"},
                    }
                    item["contact"] = _fallback(
                        request, "unverified_forwarded_export_proof"
                    )
                    scoped.append((item, original, request))
                try:
                    if (
                        connection is None
                        or parent.get("is_system") is not False
                        or parent.get("realm", "") != ""
                        or parent.get("namespace", "wechat") != "wechat"
                    ):
                        continue
                    source, members, conversation, context = (
                        self._forwarded_parent_proof(connection, parent, snapshots)
                    )
                    for item, original, request in scoped:
                        try:
                            proof = _forwarded_native_proof(
                                source, conversation, members, original, context
                            )
                        except (ValueError, TypeError, KeyError):
                            proof = None
                        if proof is None or request["is_system"]:
                            continue
                        proof["parent_message_id"] = identifier
                        proof["parent_source_record_id"] = parent["source_record_id"]
                        proof["parent_text_sha256"] = parent["text_sha256"]
                        request["_projected_component"] = item
                        requests.append(request)
                        proofs[request["id"]] = proof
                    result[identifier]["status"] = "source_proof_verified"
                except (
                    sqlite3.DatabaseError,
                    ValueError,
                    TypeError,
                    KeyError,
                    RecursionError,
                    AttributeError,
                ):
                    continue
            if initial is not None and file_identity() != initial:
                return {
                    key: {**value, "status": "history_changed"}
                    for key, value in result.items()
                }
        finally:
            if connection is not None:
                connection.close()
        if requests:
            registry = None
            try:
                if self.path.is_file() and not self.path.is_symlink():
                    registry = sqlite3.connect(
                        self.path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
                    )
                    registry.row_factory = sqlite3.Row
                else:
                    # A transient empty schema supports the same object contract;
                    # no identity or database file is created in the local store.
                    registry = sqlite3.connect(":memory:")
                    registry.row_factory = sqlite3.Row
                    initialize_contact_registry(registry)
                registry.execute("PRAGMA query_only=ON")
                resolved = self._resolve(registry, requests, forwarded_proofs=proofs)
                for request in requests:
                    request["_projected_component"]["contact"] = resolved[request["id"]]
            except (sqlite3.DatabaseError, ValueError, TypeError, KeyError):
                pass
            finally:
                if registry is not None:
                    registry.close()
        return result

    @staticmethod
    def _forwarded_parent_proof(connection, parent, snapshots):
        row = connection.execute(
            "SELECT m.*,c.partition_id,c.account_key,c.peer_id,c.kind,p.account_id,p.account_key AS partition_account_key "
            "FROM messages m JOIN conversations c ON c.id=m.conversation_id "
            "JOIN partitions p ON p.id=c.partition_id WHERE m.id=?",
            (parent["id"],),
        ).fetchone()
        if row is None:
            raise ValueError("Published forwarded parent is missing.")
        for requested, actual in (
            ("source_id", "partition_id"),
            ("conversation_id", "conversation_id"),
            ("account_id", "account_id"),
            ("peer_id", "peer_id"),
            ("conversation_kind", "kind"),
            ("timestamp_value", "timestamp_value"),
        ):
            if parent.get(requested) != row[actual]:
                raise ValueError(
                    "Forwarded source scope differs from the published parent."
                )
        if len(row["provenance"].encode()) > MAX_FORWARDED_PROOF_BYTES or _digest(
            row["text"]
        ) != parent.get("text_sha256"):
            raise ValueError("Forwarded parent content proof differs.")
        provenance = json.loads(row["provenance"])
        if (
            provenance.get("text_extraction_kind") == "system_text"
            or provenance.get("record_id") != parent.get("source_record_id")
            or _json(provenance.get("locator")) != _json(parent.get("source_locator"))
            or _json(provenance.get("forwarded_components", []))
            != _json(parent.get("forwarded_components", []))
            or parent["id"] != _digest(row["partition_id"], provenance["record_id"])
        ):
            raise ValueError("Forwarded parent or component evidence differs.")
        snapshot = provenance.get("source_snapshot")
        if type(snapshot) is not int or snapshot < 1:
            raise ValueError("A published HTML source snapshot is required.")
        cache_key = (snapshot, row["partition_id"])
        if cache_key not in snapshots:
            stored = connection.execute(
                "SELECT * FROM source_snapshots WHERE id=? AND source_id=?", cache_key
            ).fetchone()
            if stored is None or len(stored["metadata"].encode()) > 8 * 1024**2:
                raise ValueError("A bounded source snapshot is required.")
            source = json.loads(stored["metadata"])
            snapshots[cache_key] = (
                stored["snapshot_id"],
                source,
                _forwarded_source_members(source),
            )
        snapshot_id, source, members = snapshots[cache_key]
        owner = row["account_id"]
        sha = source.get("source_sha256", "")
        owner_evidence = source.get("owner_evidence")
        if (
            not isinstance(sha, str)
            or re.fullmatch(r"[0-9a-f]{64}", sha) is None
            or re.fullmatch(
                r"html-" + sha[:24] + r"(?:-text-completion-v1)?", row["partition_id"]
            )
            is None
            or source.get("source_id") != row["partition_id"]
            or source.get("source_label") != "HTML export"
            or source.get("owner_verified") is not True
            or not isinstance(owner, str)
            or not owner
            or source.get("source_account_id") != owner
            or row["account_key"] != "account:" + owner
            or row["partition_account_key"] != row["account_key"]
            or not isinstance(owner_evidence, dict)
            or owner_evidence.get("native_username") != owner
            or owner_evidence.get("native_contact_id")
            != canonical_contact_id(owner, authority="native_account_owner")
            or snapshot_id != sha
            or parent.get("source_snapshot_id") != sha
        ):
            raise ValueError("HTML source identity or owner proof differs.")
        locator = provenance["locator"]
        if not isinstance(locator, dict):
            raise ValueError("An exact original parent locator is required.")
        member = members.get(locator.get("member_path"))
        context = _forwarded_member_context(source, row, locator)
        if (
            member is None
            or locator.get("member") != member
            or context is None
            or locator.get("archive_sha256") != sha
            or provenance["record_id"] != _digest("html-text-v1", sha, _json(locator))
        ):
            raise ValueError("HTML parent member proof differs.")
        return source, members, row, context

    def query_observations(
        self,
        contact_id: str,
        *,
        page: int = 1,
        page_size: int = MAX_OBSERVATIONS,
        source_id: str | None = None,
        source_snapshot: str | None = None,
        owner_account_id: str | None = None,
        room_id: str | None = None,
    ) -> dict:
        """Page through every retained name version, optionally within an exact scope."""
        _identifier(contact_id, "contact ID")
        if (
            type(page) is not int
            or page < 1
            or type(page_size) is not int
            or not 1 <= page_size <= 256
        ):
            raise ValueError(
                "Observation pagination requires positive bounded integers."
            )
        filters = {
            "source_id": source_id,
            "source_snapshot": source_snapshot,
            "owner_account_id": owner_account_id,
            "room_id": room_id,
        }
        if any(
            value is not None and (not isinstance(value, str) or "\0" in value)
            for value in filters.values()
        ):
            raise ValueError("Observation scopes must be exact strings.")
        empty = {
            "items": [],
            "total_count": 0,
            "page": 1,
            "page_size": page_size,
            "total_pages": 1,
        }
        if not self.path.is_file() or self.path.is_symlink():
            return empty
        connection = sqlite3.connect(
            self.path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            where = " WHERE contact_id=?"
            parameters = [contact_id]
            for key, value in filters.items():
                if value is not None:
                    where += " AND " + key + "=?"
                    parameters.append(value)
            total = connection.execute(
                "SELECT COUNT(*) FROM contact_observations" + where, parameters
            ).fetchone()[0]
            pages = max(1, (total + page_size - 1) // page_size)
            selected_page = min(page, pages)
            result = []
            for row in connection.execute(
                "SELECT * FROM contact_observations"
                + where
                + " ORDER BY id LIMIT ? OFFSET ?",
                [*parameters, page_size, (selected_page - 1) * page_size],
            ):
                result.append(
                    {**dict(row), "evidence": _object_evidence(row["evidence"])}
                )
            return {
                "items": result,
                "total_count": total,
                "page": selected_page,
                "page_size": page_size,
                "total_pages": pages,
            }
        except sqlite3.DatabaseError:
            return empty
        finally:
            connection.close()

    def resolve_messages(self, messages: Iterable[dict]) -> dict[str, dict]:
        rows = list(messages)
        for message in rows:
            if not isinstance(message, dict):
                raise ValueError("Contact resolver messages must be objects.")
            for key in ("id", "source_id", "conversation_id"):
                _identifier(message.get(key), key)

        def unavailable():
            return {
                message["id"]: _fallback(message, "registry_unavailable")
                for message in rows
            }

        if not rows or not self.path.is_file() or self.path.is_symlink():
            return unavailable()
        connection = sqlite3.connect(
            self.path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            return self._resolve(connection, rows)
        except (sqlite3.DatabaseError, ValueError, TypeError, KeyError):
            return unavailable()
        finally:
            connection.close()

    def _resolve(self, connection, messages, *, forwarded_proofs=None):
        forwarded_proofs = forwarded_proofs or {}
        bindings = {}
        message_bindings = _message_bindings(connection, messages)
        by_source = {}
        for message in messages:
            sender = message.get("sender_id")
            if isinstance(sender, str) and sender:
                by_source.setdefault(message["source_id"], set()).add(sender)
        for source_id, senders in by_source.items():
            senders = sorted(senders)
            for offset in range(0, len(senders), 400):
                batch = senders[offset : offset + 400]
                placeholders = ",".join("?" for _ in batch)
                for row in connection.execute(
                    "SELECT * FROM sender_bindings WHERE source_id=? AND sender_id IN ("
                    + placeholders
                    + ")",
                    [source_id, *batch],
                ):
                    bindings.setdefault((source_id, row["sender_id"]), []).append(
                        dict(row)
                    )
        selected = {}
        reasons = {}
        for message in messages:
            if message.get("is_system") is True:
                continue
            room = (
                message.get("peer_id", "")
                if message.get("conversation_kind") == "group"
                else ""
            )
            snapshot = message.get("source_snapshot_id") or ""
            candidates = [
                binding
                for binding in bindings.get(
                    (message["source_id"], message.get("sender_id")), ()
                )
                if binding["source_snapshot"] in {"", snapshot}
                and binding["room_id"] in {"", room}
                and binding["authority"]
                in CANONICAL_AUTHORITIES | {"source_scoped_identifier"}
            ]
            candidates.extend(
                _matching_message_proofs(
                    message,
                    message_bindings.get((message["source_id"], message["id"]), ()),
                    room,
                    snapshot,
                )
            )
            proof = forwarded_proofs.get(message["id"])
            if proof is not None:
                candidates.append(
                    {
                        "contact_id": proof["contact_id"],
                        "authority": FORWARDED_BINDING_AUTHORITY,
                        "source_id": message["source_id"],
                        "source_snapshot": snapshot,
                        "room_id": room,
                        "evidence": _json(proof),
                    }
                )
            ids = {binding["contact_id"] for binding in candidates}
            if len(ids) == 1:
                selected[message["id"]] = candidates
            else:
                reasons[message["id"]] = (
                    "conflicting_bindings" if ids else "unmatched_sender_identifier"
                )
        contacts = {}
        observations = {}
        identifiers = {}
        global_names = {}
        ids = sorted(
            {
                binding["contact_id"]
                for values in selected.values()
                for binding in values
            }
        )
        for offset in range(0, len(ids), 400):
            batch = ids[offset : offset + 400]
            placeholders = ",".join("?" for _ in batch)
            for row in connection.execute(
                "SELECT * FROM contacts WHERE id IN (" + placeholders + ")", batch
            ):
                contacts[row["id"]] = dict(row)
            for row in connection.execute(
                "SELECT * FROM contact_identifiers WHERE contact_id IN ("
                + placeholders
                + ") ORDER BY contact_id,namespace,realm,value,authority",
                batch,
            ):
                identifiers.setdefault(row["contact_id"], []).append(
                    {
                        key: row[key]
                        for key in ("namespace", "value", "realm", "authority")
                    }
                )
            query = (
                "SELECT * FROM (SELECT *,COUNT(*) OVER(PARTITION BY contact_id) AS total_count,ROW_NUMBER() OVER(PARTITION BY contact_id ORDER BY id) AS row_number FROM contact_observations WHERE contact_id IN ("
                + placeholders
                + ")) WHERE row_number<=? ORDER BY contact_id,row_number"
            )
            for row in connection.execute(query, [*batch, MAX_OBSERVATIONS]):
                observations.setdefault(row["contact_id"], []).append(dict(row))
            query = (
                "WITH frequencies AS (SELECT contact_id,value,COUNT(*) AS frequency "
                "FROM contact_observations WHERE kind='nickname' AND value!='' "
                "AND contact_id IN (" + placeholders + ") GROUP BY contact_id,value), "
                "ranked AS (SELECT contact_id,value,ROW_NUMBER() OVER("
                "PARTITION BY contact_id ORDER BY frequency DESC,value COLLATE NOCASE,value) "
                "AS rank FROM frequencies) SELECT contact_id,value FROM ranked WHERE rank=1"
            )
            for row in connection.execute(query, batch):
                global_names[row["contact_id"]] = row["value"]
        proof_contacts = {
            proof["contact_id"]: proof for proof in forwarded_proofs.values()
        }
        registered_ids = set(contacts)
        for identifier, proof in proof_contacts.items():
            native = proof["native_username"]
            contacts.setdefault(
                identifier,
                {
                    "id": identifier,
                    "namespace": "wechat",
                    "realm": "",
                    "username": native,
                    "kind": classify_wechat_identifier(native)["kind"],
                    "identity_status": "verified",
                },
            )
            identifiers.setdefault(identifier, []).append(
                {
                    "namespace": "native_username",
                    "realm": "",
                    "value": native,
                    "authority": FORWARDED_BINDING_AUTHORITY,
                }
            )
        result = {}
        preferred_names = {}
        profiles = {}
        binding_evidence = {}
        for message in messages:
            values = selected.get(message["id"], [])
            contact = contacts.get(values[0]["contact_id"]) if values else None
            if contact is None:
                result[message["id"]] = _fallback(
                    message, reasons.get(message["id"], "missing_contact")
                )
                continue
            if contact["identity_status"] == "verified":
                try:
                    expected = canonical_contact_id(
                        contact["username"],
                        authority="native_contact_username",
                        namespace=contact["namespace"],
                        realm=contact["realm"],
                    )
                except ValueError:
                    result[message["id"]] = _fallback(
                        message, "invalid_canonical_binding"
                    )
                    continue
                if contact["id"] != expected or any(
                    value["authority"]
                    not in CANONICAL_AUTHORITIES
                    | {MESSAGE_BINDING_AUTHORITY, FORWARDED_BINDING_AUTHORITY}
                    for value in values
                ):
                    result[message["id"]] = _fallback(
                        message, "invalid_canonical_binding"
                    )
                    continue
            elif contact["identity_status"] != "source_scoped" or any(
                value["authority"] != "source_scoped_identifier"
                or contact["id"]
                != source_contact_id(
                    message["source_id"],
                    message["sender_id"],
                    conversation_id=value["room_id"],
                    source_snapshot=value["source_snapshot"],
                )
                for value in values
            ):
                result[message["id"]] = _fallback(message, "invalid_scoped_binding")
                continue
            observed = observations.get(contact["id"], [])
            if contact["id"] not in profiles:
                kind = (
                    classify_wechat_identifier(contact["username"])["kind"]
                    if contact["identity_status"] == "verified"
                    else contact["kind"]
                )
                if kind == "unresolved" and contact["identity_status"] == "verified":
                    kind = "wechat_identity"
                clean = [
                    {
                        **{
                            key: item[key]
                            for key in (
                                "source_id",
                                "source_snapshot",
                                "owner_account_id",
                                "room_id",
                                "kind",
                                "value",
                            )
                        },
                        "evidence": _object_evidence(item["evidence"]),
                    }
                    for item in observed
                ]
                count = observed[0]["total_count"] if observed else 0
                profiles[contact["id"]] = {
                    "kind": kind,
                    "display_name": global_names.get(
                        contact["id"], contact["username"]
                    ),
                    "names": [item for item in clean if item["kind"] != "remark"],
                    "remarks": [item for item in clean if item["kind"] == "remark"],
                    "observation_count": count,
                    "observations_truncated": count > MAX_OBSERVATIONS,
                }
            profile = profiles[contact["id"]]
            display = profile["display_name"]
            room = (
                message.get("peer_id", "")
                if message.get("conversation_kind") == "group"
                else ""
            )
            context = (
                contact["id"],
                message["source_id"],
                message.get("source_snapshot_id") or "",
                message.get("account_id") or "",
                room,
            )
            if context not in preferred_names:
                preferred = connection.execute(
                    "SELECT value FROM contact_observations WHERE contact_id=? AND source_id=? "
                    "AND source_snapshot IN ('',?) AND owner_account_id IN ('',?) "
                    "AND room_id IN ('',?) AND value!='' "
                    "AND kind IN ('remark','room_display_name','nickname','alias') ORDER BY CASE kind "
                    "WHEN 'remark' THEN 0 WHEN 'room_display_name' THEN 1 WHEN 'nickname' THEN 2 "
                    "WHEN 'alias' THEN 3 ELSE 99 END,"
                    "(source_snapshot=''),(room_id=''),value COLLATE NOCASE,value,id LIMIT 1",
                    context,
                ).fetchone()
                preferred_names[context] = preferred[0] if preferred else ""
            corroborated = any(
                item["authority"] == MESSAGE_BINDING_AUTHORITY for item in values
            )
            evidence_context = (
                *context,
                message.get("sender_id"),
                message["id"]
                if corroborated or message["id"] in forwarded_proofs
                else "",
            )
            if evidence_context not in binding_evidence:
                binding_evidence[evidence_context] = [
                    {
                        "authority": item["authority"],
                        "source_id": item["source_id"],
                        "source_snapshot": item["source_snapshot"],
                        "room_id": item["room_id"],
                        "details": _object_evidence(item["evidence"]),
                        **(
                            {
                                "message_id": item["message_id"],
                                "verification": {
                                    key: item[key]
                                    for key in (
                                        "text_sha256",
                                        "timestamp_value",
                                        "source_record_id",
                                    )
                                },
                            }
                            if item["authority"] == MESSAGE_BINDING_AUTHORITY
                            else {}
                        ),
                    }
                    for item in values[:MAX_OBSERVATIONS]
                ]
            result[message["id"]] = {
                **profile,
                "id": contact["id"],
                "platform": "wechat",
                "namespace": contact["namespace"],
                "realm": contact["realm"],
                "identity_status": contact["identity_status"],
                "identifiers": identifiers.get(contact["id"], []),
                "source_display_name": str(message.get("sender_label") or ""),
                "preferred_name": preferred_names[context]
                or str(message.get("sender_label") or display),
                "evidence": binding_evidence[evidence_context],
                "resolution_reason": FORWARDED_BINDING_AUTHORITY
                if message["id"] in forwarded_proofs
                else MESSAGE_BINDING_AUTHORITY
                if corroborated
                else "authoritative_identifier"
                if contact["identity_status"] == "verified"
                else "source_scoped_identifier",
            }
            if message["id"] in forwarded_proofs:
                result[message["id"]]["registry_persisted"] = (
                    contact["id"] in registered_ids
                )
        return result
