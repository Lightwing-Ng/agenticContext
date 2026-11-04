"""Associate explicitly confirmed people without rewriting native WeChat identities.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import stat


VERSION = "v1.0.0-codex.0"
AUTHORITY = "user_confirmed_same_person"
MAX_REGISTRY_BYTES = 1024 * 1024
MAX_ASSOCIATIONS = 64
MAX_MEMBERS = 32
MAX_UNION_REFERENCES = 1_000_000
_CONTACT = re.compile(r"wechat:contact:[0-9a-f]{64}")
_DIGEST = re.compile(r"[0-9a-f]{64}")


class ContactMergeSourceChanged(ValueError):
    """Reject an association that changed during a bounded read or union build."""


def contact_merge_registry_path(root):
    return Path(root).expanduser() / "messaging" / "wechat" / "contact_merges.json"


def _identity(path):
    value = path.stat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(value.st_mode)
        or getattr(value, "st_flags", 0) & 0x40000000
    ):
        raise ValueError("Contact associations must be local regular files.")
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate contact-association key.")
        result[key] = value
    return result


def _contact(value):
    return isinstance(value, str) and _CONTACT.fullmatch(value) is not None


def read_contact_merges(root):
    """Read one pinned, bounded declaration; names never authorize an association."""
    path = contact_merge_registry_path(root)
    if path.is_symlink() or any(
        parent.is_symlink() for parent in (path.parent, path.parent.parent)
    ):
        raise ValueError("Contact associations cannot use symbolic links.")
    try:
        before = _identity(path)
        if before[2] > MAX_REGISTRY_BYTES:
            raise ValueError("Contact-association registry is too large.")
        with path.open("rb") as source:
            raw = source.read(MAX_REGISTRY_BYTES + 1)
        if _identity(path) != before:
            raise ContactMergeSourceChanged("Contact associations changed while reading.")
    except FileNotFoundError:
        raw = None
    if raw is None:
        return {
            "digest": hashlib.sha256((VERSION + ":missing").encode()).hexdigest(),
            "associations": [],
        }
    if len(raw) > MAX_REGISTRY_BYTES:
        raise ValueError("Contact-association registry is too large.")
    try:
        value = json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ValueError("Invalid contact-association registry.") from error
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "associations"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or not isinstance(value["associations"], list)
        or len(value["associations"]) > MAX_ASSOCIATIONS
    ):
        raise ValueError("Unsupported contact-association registry.")
    used = set()
    associations = []
    for declaration in value["associations"]:
        required = {
            "target_contact_id", "member_contact_ids", "authority", "evidence_id"
        }
        if (
            not isinstance(declaration, dict)
            or not required <= set(declaration)
            or set(declaration) - required
            - {"confirmed_at", "confirmation", "native_identities"}
            or not _contact(declaration["target_contact_id"])
            or declaration["authority"] != AUTHORITY
            or not isinstance(declaration["evidence_id"], str)
            or _DIGEST.fullmatch(declaration["evidence_id"]) is None
            or not isinstance(declaration["member_contact_ids"], list)
            or not 2 <= len(declaration["member_contact_ids"]) <= MAX_MEMBERS
        ):
            raise ValueError("A contact association needs explicit, bounded user authority.")
        members = declaration["member_contact_ids"]
        if (
            any(not _contact(member) for member in members)
            or len(set(members)) != len(members)
            or declaration["target_contact_id"] not in members
            or used.intersection(members)
        ):
            raise ValueError("Contact associations cannot overlap, repeat, or form chains.")
        for key, limit in (("confirmation", 4096), ("confirmed_at", 100)):
            if key in declaration and (
                not isinstance(declaration[key], str)
                or not declaration[key].strip()
                or len(declaration[key]) > limit
            ):
                raise ValueError("Invalid contact-association confirmation metadata.")
        pins = declaration.get("native_identities", [])
        if not isinstance(pins, list):
            raise ValueError("Native identity pins must be a list.")
        if pins and (
            len(pins) != len(members)
            or any(
                not isinstance(pin, dict)
                or set(pin) != {"contact_id", "username"}
                or not _contact(pin["contact_id"])
                or not isinstance(pin["username"], str)
                or not pin["username"]
                or len(pin["username"]) > 256
                for pin in pins
            )
            or {pin["contact_id"] for pin in pins} != set(members)
        ):
            raise ValueError("Native identity pins must identify every distinct member.")
        used.update(members)
        target = declaration["target_contact_id"]
        associations.append(
            {**declaration, "member_contact_ids": [target, *sorted(set(members) - {target})]}
        )
    return {
        "digest": hashlib.sha256(VERSION.encode() + b"\0" + raw).hexdigest(),
        "associations": associations,
    }


def resolve_contact_merge(root, contact_id):
    if not _contact(contact_id):
        raise ValueError("A canonical WeChat contact ID is required.")
    registry = read_contact_merges(root)
    association = next(
        (
            item for item in registry["associations"]
            if contact_id in item["member_contact_ids"]
        ),
        None,
    )
    return {
        "requested_contact_id": contact_id,
        "canonical_contact_id": association["target_contact_id"] if association else contact_id,
        "member_contact_ids": association["member_contact_ids"] if association else [contact_id],
        "is_associated": association is not None,
        "registry_digest": registry["digest"],
        "association_authority": AUTHORITY if association else "",
        "association_evidence_id": association["evidence_id"] if association else "",
        "native_identity_pins": association.get("native_identities", []) if association else [],
    }


def copy_contact_reference_union(output, members, *, checkpoint=lambda: None):
    """Union proved memberships by exact reference/group IDs, never by text similarity."""
    output.executescript(
        "CREATE TABLE person_reference_contacts(reference_id TEXT,contact_id TEXT,"
        "PRIMARY KEY(reference_id,contact_id)) WITHOUT ROWID;"
        "CREATE TABLE person_member_groups(contact_id TEXT,member_statement_id TEXT,"
        "statement_id TEXT,PRIMARY KEY(contact_id,member_statement_id)) WITHOUT ROWID;"
    )
    references, groups, reference_group, same_group = {}, {}, {}, {}
    parents = {}

    def find(key):
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    def union(left, right):
        left, right = find(left), find(right)
        if left != right:
            parents[max(left, right)] = min(left, right)

    for order, (contact_id, source) in enumerate(members):
        checkpoint()
        member_refs = set()
        for row in source.execute("SELECT * FROM refs ORDER BY id"):
            value = tuple(row)
            identifier = value[0]
            if identifier in references and references[identifier] != value:
                raise ContactMergeSourceChanged("An exact source reference has conflicting metadata.")
            references[identifier] = value
            member_refs.add(identifier)
            if len(references) > MAX_UNION_REFERENCES:
                raise ValueError("Person reference union exceeds its bounded limit.")
            output.execute(
                "INSERT OR IGNORE INTO person_reference_contacts VALUES(?,?)",
                (identifier, contact_id),
            )
            if len(member_refs) % 400 == 0:
                checkpoint()
        definitions = {}
        for identifier, preferred, conversation in source.execute(
            "SELECT id,reference_id,conversation_key FROM statement_groups ORDER BY id"
        ):
            key = (order, identifier)
            parents[key] = key
            groups[key] = (identifier, preferred, conversation, contact_id)
            if len(groups) > MAX_UNION_REFERENCES:
                raise ValueError("Person event union exceeds its bounded limit.")
            definitions[identifier] = key
            if preferred not in member_refs:
                raise ContactMergeSourceChanged("A member group has no preferred source reference.")
            if identifier in same_group:
                old = same_group[identifier]
                if groups[old][2] != conversation:
                    raise ContactMergeSourceChanged("An exact event group has conflicting context.")
                union(old, key)
            else:
                same_group[identifier] = key
        assigned = set()
        for reference, statement in source.execute(
            "SELECT reference_id,statement_id FROM statement_members ORDER BY reference_id"
        ):
            if reference not in member_refs or statement not in definitions:
                raise ContactMergeSourceChanged("A member reference has invalid event membership.")
            key = definitions[statement]
            assigned.add(reference)
            if reference in reference_group:
                union(reference_group[reference], key)
            else:
                reference_group[reference] = key
        if assigned != member_refs:
            raise ContactMergeSourceChanged("A member cache has unassigned source references.")
        for key in definitions.values():
            preferred = groups[key][1]
            if find(reference_group[preferred]) != find(key):
                raise ContactMergeSourceChanged("A preferred reference belongs to another event.")
    checkpoint()
    output.executemany(
        "INSERT INTO refs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", references.values()
    )
    roots = {find(key) for key in groups}
    output.executemany(
        "INSERT INTO statement_groups VALUES(?,?,?)",
        [groups[key][:3] for key in sorted(roots)],
    )
    output.executemany(
        "INSERT INTO statement_members VALUES(?,?)",
        [(reference, groups[find(key)][0]) for reference, key in reference_group.items()],
    )
    output.executemany(
        "INSERT INTO person_member_groups VALUES(?,?,?)",
        [(value[3], value[0], groups[find(key)][0]) for key, value in groups.items()],
    )
    return {"references": len(references), "statements": len(roots)}
