"""Apply pinned, user-reviewed transcript copies without changing source records.

Code version: v1.1.2-codex.0
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import stat

from .wechat_contact_embedded import native_details
from .wechat_contacts import MAX_FORWARDED_COMPONENTS, canonical_contact_id
from .wechat_forwarded import forwarded_component_clock
from .wechat_forwarded_origins import (
    _author,
    _metadata,
    _native_scope,
    _native_time,
    _server_constraint,
    _stamp,
)
from .wechat_html_export import normalize_html_clock


VERSION = "v1.1.2-codex.0"
MAX_REVIEWS = 64
MAX_PARENTS = 256
MAX_REGISTRY_BYTES = 1024 * 1024
MAX_PACKET_BYTES = 16 * 1024 * 1024
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 200_000
_ID = re.compile(r"[0-9a-f]{64}")
_DECIMAL = re.compile(r"0|[1-9][0-9]*")
_DATA_ID = re.compile(r"[0-9a-f]{32}")
_AUTHORITY = "user_confirmed_transcript_copy"
_REFERENCE_AUTHORITY = "user_confirmed_statement_copy"
_CONTACT = re.compile(r"wechat:contact:[0-9a-f]{64}")
_REFERENCE = re.compile(r"[0-9a-f]{64}(?:_forwarded_[1-9][0-9]*)*")
_REFERENCE_POLICIES = {"earliest_forward", "native_group", "native_source"}
_JOURNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS reviewed_statement_groups (
 id TEXT PRIMARY KEY,reference_id TEXT NOT NULL UNIQUE,conversation_key TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS reviewed_statement_members (
 reference_id TEXT PRIMARY KEY,statement_id TEXT NOT NULL
) WITHOUT ROWID;
"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _identifier(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate statement-review key.")
        result[key] = value
    return result


def _registry_path(root):
    return Path(root).expanduser() / "messaging" / "wechat" / "statement_reviews.json"


def _read(root):
    path = _registry_path(root)
    try:
        if any(
            parent.is_symlink() for parent in (path, path.parent, path.parent.parent)
        ):
            return "unavailable", b""
        state = path.stat()
        if (
            not stat.S_ISREG(state.st_mode)
            or getattr(state, "st_flags", 0) & 0x40000000
        ):
            return "unavailable", b""
        with path.open("rb") as stream:
            raw = stream.read(MAX_REGISTRY_BYTES + 1)
        after = path.stat()
        if (state.st_dev, state.st_ino, state.st_size, state.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            return "changed", b""
        return ("oversized" if len(raw) > MAX_REGISTRY_BYTES else "present"), raw
    except FileNotFoundError:
        return "missing", b""
    except OSError:
        return "unavailable", b""


def reviews_digest(root) -> str:
    """Fingerprint local declarations, including missing or unusable inputs."""
    status, raw = _read(root)
    return _sha(_json([VERSION, status, hashlib.sha256(raw).hexdigest()]))


def _declarations(raw):
    value = json.loads(raw, object_pairs_hook=_unique)
    if (
        not isinstance(value, dict)
        or type(value["schema_version"]) is not int
        or value["schema_version"] not in {1, 2}
        or set(value)
        != (
            {"schema_version", "packet_reviews"}
            if value["schema_version"] == 1
            else {"schema_version", "packet_reviews", "reference_reviews"}
        )
        or not isinstance(value["packet_reviews"], list)
        or len(value["packet_reviews"]) > MAX_REVIEWS
    ):
        raise ValueError("Unsupported statement-review registry.")
    parents, evidence = set(), set()
    for review in value["packet_reviews"]:
        if (
            not isinstance(review, dict)
            or set(review)
            != {
                "authority",
                "owner_account_id",
                "preferred_parent_message_id",
                "parents",
                "evidence_id",
            }
            or review["authority"] != _AUTHORITY
            or not _author(review["owner_account_id"])
            or not _identifier(review["preferred_parent_message_id"])
            or not _identifier(review["evidence_id"])
            or review["evidence_id"] in evidence
            or not isinstance(review["parents"], list)
            or not 2 <= len(review["parents"]) <= MAX_PARENTS
        ):
            raise ValueError("Reviewed packets need exact identities and source pins.")
        selected = set()
        for parent in review["parents"]:
            if (
                not isinstance(parent, dict)
                or set(parent) != {"message_id", "text_sha256", "provenance_sha256"}
                or any(not _identifier(item) for item in parent.values())
                or parent["message_id"] in parents
            ):
                raise ValueError("Each reviewed parent must be unique and pinned.")
            selected.add(parent["message_id"])
            parents.add(parent["message_id"])
        if (
            review["preferred_parent_message_id"] not in selected
            or len(parents) > MAX_PARENTS
        ):
            raise ValueError(
                "The preferred native parent must belong to the bounded review."
            )
        evidence.add(review["evidence_id"])
    references = _reference_declarations(
        value.get("reference_reviews", []), parents, evidence
    )
    if len(value["packet_reviews"]) + len(references) > MAX_REVIEWS:
        raise ValueError("Statement reviews exceed their bounded declaration budget.")
    return [*value["packet_reviews"], *references]


def _reference_path(path):
    if not isinstance(path, list) or len(path) > 32:
        return False
    numbers = path[1:] if path[:1] == ["native"] else path
    return (not path or bool(numbers)) and all(
        type(part) is int and part > 0 for part in numbers
    )


def _reference_declarations(values, parents, evidence):
    """Parse explicit source-pinned cohorts without broadening legacy packet proofs."""
    if not isinstance(values, list) or len(values) > MAX_REVIEWS:
        raise ValueError("Reference reviews must be a bounded list.")
    references, pins, declarations = set(), {}, []
    fields = {
        "reference_id",
        "parent_message_id",
        "component_path",
        "source_id",
        "account_key",
        "source_snapshot_id",
        "parent_text_sha256",
        "provenance_sha256",
        "body_sha256",
    }
    for review in values:
        if (
            not isinstance(review, dict)
            or set(review)
            != {
                "authority",
                "contact_id",
                "preferred_reference_id",
                "selection_policy",
                "members",
                "evidence_id",
            }
            or review["authority"] != _REFERENCE_AUTHORITY
            or not isinstance(review["contact_id"], str)
            or _CONTACT.fullmatch(review["contact_id"]) is None
            or not isinstance(review["preferred_reference_id"], str)
            or _REFERENCE.fullmatch(review["preferred_reference_id"]) is None
            or review["selection_policy"] not in _REFERENCE_POLICIES
            or not _identifier(review["evidence_id"])
            or review["evidence_id"] in evidence
            or not isinstance(review["members"], list)
            or not 2 <= len(review["members"]) <= MAX_PARENTS
        ):
            raise ValueError(
                "A reference review needs exact members and a native policy."
            )
        selected, parent_pins = set(), {}
        for member in review["members"]:
            if (
                not isinstance(member, dict)
                or set(member) != fields
                or not isinstance(member["reference_id"], str)
                or _REFERENCE.fullmatch(member["reference_id"]) is None
                or member["reference_id"] in references
                or not _identifier(member["parent_message_id"])
                or not _reference_path(member["component_path"])
                or not isinstance(member["source_id"], str)
                or re.fullmatch(
                    r"(?:ios|html)-[A-Za-z0-9_-]{1,95}", member["source_id"]
                )
                is None
                or not isinstance(member["account_key"], str)
                or not member["account_key"].startswith("account:")
                or not _author(member["account_key"][8:])
                or not isinstance(member["source_snapshot_id"], str)
                or not member["source_snapshot_id"]
                or len(member["source_snapshot_id"]) > 512
                or any(ord(char) < 32 for char in member["source_snapshot_id"])
                or any(
                    not _identifier(member[key])
                    for key in (
                        "parent_text_sha256",
                        "provenance_sha256",
                        "body_sha256",
                    )
                )
            ):
                raise ValueError("A reviewed reference has invalid source pins.")
            parent = member["parent_message_id"]
            pin = {
                "message_id": parent,
                "text_sha256": member["parent_text_sha256"],
                "provenance_sha256": member["provenance_sha256"],
            }
            if parent in pins and pins[parent] != pin:
                raise ValueError("Repeated reviewed parents have contradictory pins.")
            pins[parent], parent_pins[parent] = pin, pin
            parents.add(parent)
            references.add(member["reference_id"])
            selected.add(member["reference_id"])
        if (
            review["preferred_reference_id"] not in selected
            or len(parents) > MAX_PARENTS
        ):
            raise ValueError("A reviewed preference must belong to its bounded cohort.")
        evidence.add(review["evidence_id"])
        declarations.append({**review, "parents": list(parent_pins.values())})
    return declarations


def _load(source, declarations, row_select, prepare_rows):
    identifiers = sorted(
        {part["message_id"] for review in declarations for part in review["parents"]}
    )
    rows, proofs, snapshots, total = {}, {}, {}, 0
    fields = {row[1] for row in source.execute("PRAGMA main.table_info(messages)")}
    byte_fields = ["text", "provenance"]
    if "search_text" in fields:
        byte_fields.append("search_text")
    byte_size = "+".join(
        "COALESCE(length(CAST(" + field + " AS BLOB)),0)" for field in byte_fields
    )
    for start in range(0, len(identifiers), 64):
        batch = identifiers[start : start + 64]
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
            raise ValueError("A reviewed source parent is missing.")
        for _, size in sizes:
            total += size
            if size > MAX_PACKET_BYTES or total > MAX_SOURCE_BYTES:
                raise ValueError("Reviewed source packets exceed their bounded budget.")
        values = [
            dict(row)
            for row in source.execute(
                row_select + "WHERE m.id IN (" + marks + ")", batch
            )
        ]
        prepare_rows(source, values)
        for row in values:
            proof = json.loads(row["provenance"], object_pairs_hook=_unique)
            if not isinstance(proof, dict):
                raise ValueError("A reviewed parent has invalid provenance.")
            rows[row["id"]], proofs[row["id"]] = row, proof
    snapshot_ids = sorted(
        {
            proof.get("source_snapshot")
            for proof in proofs.values()
            if type(proof.get("source_snapshot")) is int
        }
    )
    for key in snapshot_ids:
        value = source.execute(
            "SELECT id,snapshot_id,source_id,length(CAST(metadata AS BLOB)) FROM source_snapshots WHERE id=?",
            (key,),
        ).fetchone()
        if value is None:
            raise ValueError("A reviewed packet has no published snapshot.")
        total += value[3]
        if value[3] > MAX_PACKET_BYTES or total > MAX_SOURCE_BYTES:
            raise ValueError("Reviewed source metadata exceeds its bounded budget.")
        snapshot = dict(
            source.execute(
                "SELECT * FROM source_snapshots WHERE id=?", (key,)
            ).fetchone()
        )
        snapshot["metadata"] = json.loads(
            snapshot["metadata"], object_pairs_hook=_unique
        )
        snapshots[key] = snapshot
    return rows, proofs, snapshots


def _clock(component):
    raw, precision = normalize_html_clock(component.get("source_timestamp", ""))
    if not raw or precision not in {"second", "minute"}:
        return None
    source_clock = datetime.fromisoformat(raw)
    timestamp = forwarded_component_clock(component, native=True)[0]
    stamp = _stamp(timestamp, seconds=True)
    if stamp is None:
        return None
    epoch = component.get("source_create_time", "")
    if epoch:
        if (
            not isinstance(epoch, str)
            or re.fullmatch(r"[1-9][0-9]{0,11}", epoch) is None
        ):
            return None
        if stamp.tzinfo is None or int(stamp.timestamp()) != int(epoch):
            return None
    elif precision != "second":
        # Minute copy cannot manufacture seconds without this child's native epoch.
        return None
    wall = stamp.replace(tzinfo=None)
    if precision == "minute":
        wall = wall.replace(second=0)
    return stamp if source_clock == wall else None


def _constraint(component):
    server = _server_constraint(component)
    chat = component.get("source_chat_id", component.get("original_chat_id", ""))
    if (
        server is False
        or not isinstance(chat, str)
        or any(character.isspace() or character == "\0" for character in chat)
        or (
            component.get("source_chat_id")
            and component.get("original_chat_id")
            and component["source_chat_id"] != component["original_chat_id"]
        )
    ):
        raise ValueError("A reviewed child has contradictory identity constraints.")
    return server or "", chat


def _native(row, proof, snapshots, native_thread_key):
    scope = _native_scope(row, proof, snapshots, native_thread_key)
    if scope is None:
        return None
    parts = proof.get("text_components")
    if (
        _native_time(row, proof) is None
        or row["direction"] not in {"sent", "received"}
        or not _author(row["sender_id"])
        or (row["direction"] == "sent" and row["sender_id"] != row["account_id"])
        or proof.get("message_type") != 49
        or proof.get("text_extraction_kind") != "forwarded_text"
        or proof.get("appmsg_subtype") not in {None, 19}
        or proof.get("omitted_forwarded_items")
        or not isinstance(parts, list)
        or not 3 <= len(parts) <= MAX_FORWARDED_COMPONENTS
    ):
        raise ValueError("A reviewed native source is not a complete text packet.")
    details = {
        tuple(item["path"]): item
        for item in native_details(
            proof,
            parent_id=row["id"],
            account_id=row["account_id"],
            peer_id=row["peer_id"] or "",
        )
    }
    shapes, leaves, constraints, identifiers = [], [], [], []
    for number, part in enumerate(parts, 1):
        if (
            not isinstance(part, dict)
            or part.get("forwarded_item_path", [number]) != [number]
            or part.get("text_components")
            or part.get("forwarded_components")
        ):
            raise ValueError("Reviewed packet positions must be complete and flat.")
        kind = part.get("kind")
        data = part.get("source_data_id")
        sender, stamp = _author(part.get("source_sender_id")), _clock(part)
        if not sender or stamp is None or not isinstance(data, str):
            raise ValueError(
                "A reviewed child lacks its literal author, clock, or position."
            )
        identifiers.append(data)
        constraints.append(_constraint(part))
        if kind == "forwarded_text":
            detail = details.get(("native", number))
            if (
                detail is None
                or type(part.get("original_message_type", 1)) is not int
                or part.get("original_message_type", 1) != 1
                or not isinstance(part.get("text"), str)
                or not part["text"].strip()
                or detail["text"] != part["text"]
                or detail["sender_id"] != sender
                or _stamp(detail["timestamp_value"], seconds=True) != stamp
            ):
                raise ValueError("A reviewed text role disagrees with native evidence.")
            shapes.append((kind, data, sender, stamp.isoformat(), part["text"]))
            leaves.append(
                {**detail, "data_id": data, "stamp": stamp, "position": number}
            )
        elif kind == "forwarded_resource":
            resource = part.get("resource_kind")
            if resource not in {
                "image",
                "video",
                "audio",
                "file",
                "app",
                "sticker",
            } or part.get("text", ""):
                raise ValueError(
                    "A reviewed resource must remain a metadata-only placeholder."
                )
            shapes.append(
                (
                    kind,
                    data,
                    sender,
                    stamp.isoformat(),
                    resource,
                    part.get("forwarded_data_type"),
                )
            )
        else:
            raise ValueError("A reviewed packet has an unsupported child role.")
    if not (
        all(data == str(number) for number, data in enumerate(identifiers))
        or (
            len(set(identifiers)) == len(identifiers)
            and all(_DATA_ID.fullmatch(data) for data in identifiers)
        )
    ):
        raise ValueError("Reviewed child positions are incomplete or ambiguous.")
    keys = [
        (part["sender_id"], part["text"], part["stamp"].isoformat()) for part in leaves
    ]
    if (
        len(leaves) < 3
        or len(set(keys)) != len(keys)
        or len({part["sender_id"] for part in leaves}) < 2
        or not all(
            left["stamp"] < right["stamp"] for left, right in zip(leaves, leaves[1:])
        )
    ):
        raise ValueError("Reviewed text needs a unique monotonic multi-author packet.")
    return {
        "row": row,
        "proof": proof,
        "scope": scope,
        "shape": shapes,
        "leaves": leaves,
        "constraints": constraints,
    }


def _html(row, proof, snapshots, native):
    owner = native["row"]["account_id"]
    metadata = _metadata(row, proof, snapshots)
    locator = proof.get("locator", {})
    local = locator.get("export_msgid") if isinstance(locator, dict) else None
    clock = _stamp(row["timestamp_value"], seconds=True)
    native_clock = _stamp(native["row"]["timestamp_value"], seconds=True)
    if (
        row["account_id"] != owner
        or row["account_key"] != "account:" + owner
        or row["container_account_key"] != row["account_key"]
        or row["identity_status"] != "Corroborated export ID"
        or not row["source_id"].startswith("html-")
        or metadata.get("owner_verified") is not True
        or metadata.get("source_account_id") != owner
        or not isinstance(metadata.get("owner_evidence"), dict)
        or metadata["owner_evidence"].get("native_username") != owner
        or any(
            row[key] != native["row"][key] for key in ("peer_id", "kind", "direction")
        )
        or _author(proof.get("sender_native_id")) != native["row"]["sender_id"]
        or proof.get("text_extraction_kind")
        not in {"forwarded_text", "rendered_app_text"}
        or proof.get("omitted_forwarded_items")
        or proof.get("export_msgid_namespace") != "MesLocalID"
        or locator.get("export_msgid_namespace") != "MesLocalID"
        or str(locator.get("export_msgtype")) != "49"
        or not isinstance(local, str)
        or re.fullmatch(r"[1-9][0-9]*", local) is None
        or clock is None
        or native_clock is None
        or clock.replace(tzinfo=None) != native_clock.replace(tzinfo=None)
        or (clock.tzinfo is not None and clock != native_clock)
    ):
        return None
    parts = proof.get("forwarded_components")
    if not isinstance(parts, list) or len(parts) != len(native["leaves"]):
        return None
    leaves, constraints = [], []
    for position, (part, original) in enumerate(zip(parts, native["leaves"]), 1):
        if not isinstance(part, dict):
            return None
        raw, precision = normalize_html_clock(part.get("raw_timestamp", ""))
        if (
            part.get("text_kind") not in {"plain_text", "forwarded_rendered_text"}
            or part.get("forwarded_components")
            or part.get("quoted_components")
            or part.get("sender_native_id") != original["sender_id"]
            or part.get("text") != original["text"]
            or part.get("export_msgid") != local + "_" + original["data_id"]
            or precision != "second"
            or datetime.fromisoformat(raw) != original["stamp"].replace(tzinfo=None)
        ):
            return None
        constraints.append(_constraint(part))
        leaves.append(
            {
                "path": [position],
                "sender_id": original["sender_id"],
                "text": original["text"],
            }
        )
    return {"row": row, "leaves": leaves, "constraints": constraints}


def _packet_plan(
    review, rows, proofs, snapshots, output, contact_id, native_thread_key
):
    owner = review["owner_account_id"]
    natives, htmls = {}, []
    for pin in review["parents"]:
        row, proof = rows[pin["message_id"]], proofs[pin["message_id"]]
        if (
            row["account_id"] != owner
            or _sha(row["text"]) != pin["text_sha256"]
            or _sha(_json(proof)) != pin["provenance_sha256"]
        ):
            raise ValueError("A reviewed source pin or owner changed.")
        packet = _native(row, proof, snapshots, native_thread_key)
        if packet is not None:
            natives[row["id"]] = packet
        else:
            htmls.append((row, proof))
    preferred = natives.get(review["preferred_parent_message_id"])
    if preferred is None or preferred["row"]["kind"] != "group":
        raise ValueError(
            "The preferred reviewed source must be a verified native group packet."
        )
    if any(
        packet["shape"] != preferred["shape"] or packet["scope"] != preferred["scope"]
        for packet in natives.values()
    ):
        raise ValueError(
            "The reviewed native packets do not have one complete shape and snapshot."
        )
    packets = list(natives.values())
    html_packets = []
    for row, proof in htmls:
        matches = [
            value
            for native in natives.values()
            if (value := _html(row, proof, snapshots, native)) is not None
        ]
        if len(matches) != 1:
            raise ValueError(
                "A reviewed HTML packet lacks one exact native parent context."
            )
        packets.append(matches[0])
        html_packets.append(matches[0])
    for position in range(len(preferred["shape"])):
        for field in range(2):
            if (
                len(
                    {
                        packet["constraints"][position][field]
                        for packet in natives.values()
                        if packet["constraints"][position][field]
                    }
                )
                > 1
            ):
                raise ValueError(
                    "Reviewed resource or text positions assert different identities."
                )
    for number, original in enumerate(preferred["leaves"]):
        position = original["position"] - 1
        native_constraints = [
            packet["constraints"][position] for packet in natives.values()
        ]
        html_constraints = [packet["constraints"][number] for packet in html_packets]
        for field in range(2):
            if (
                len(
                    {
                        constraint[field]
                        for constraint in (*native_constraints, *html_constraints)
                        if constraint[field]
                    }
                )
                > 1
            ):
                raise ValueError(
                    "Reviewed children assert different original event identities."
                )
    lookup = {}
    for packet in packets:
        parent = packet["row"]
        expected = {
            tuple(leaf["path"]): (
                leaf.get("id", parent["id"] + "_forwarded_" + str(leaf["path"][0])),
                canonical_contact_id(
                    leaf["sender_id"], authority="native_message_sender"
                ),
            )
            for leaf in packet["leaves"]
        }
        values = output.execute(
            "SELECT * FROM refs WHERE parent_message_id=?", (parent["id"],)
        )
        fields = [item[0] for item in values.description]
        for value in values:
            reference = dict(zip(fields, value))
            if (
                reference["source_id"] != parent["source_id"]
                or reference["account_key"] != parent["account_key"]
                or reference["conversation_id"] != parent["conversation_id"]
            ):
                raise ValueError(
                    "An accepted reference no longer matches its reviewed source."
                )
            path = tuple(json.loads(reference["component_path"]))
            identity = expected.get(path)
            key = parent["id"], path
            if (
                identity is None
                or identity != (reference["id"], contact_id)
                or reference["anchor_id"] != "wechat_message_" + reference["id"]
                or key in lookup
            ):
                raise ValueError("An accepted reviewed component changed its identity.")
            lookup[key] = reference["id"]
    plans = []
    for number, original in enumerate(preferred["leaves"]):
        if (
            canonical_contact_id(
                original["sender_id"], authority="native_message_sender"
            )
            != contact_id
        ):
            continue
        references = [
            lookup.get((packet["row"]["id"], tuple(packet["leaves"][number]["path"])))
            for packet in packets
        ]
        chosen = lookup.get((preferred["row"]["id"], tuple(original["path"])))
        if chosen is None or any(reference is None for reference in references):
            raise ValueError(
                "The accepted contact cohort does not account for every reviewed copy."
            )
        plans.append((set(references), chosen, native_thread_key(preferred["row"])))
    return plans


def _member_clock(component, *, native):
    """Keep each assertion's own precision; a reviewed minute never supplies seconds."""
    raw = component.get("source_timestamp" if native else "raw_timestamp", "")
    normalized, precision = normalize_html_clock(raw)
    if not normalized or precision not in {"second", "minute"}:
        raise ValueError("A reviewed statement has no supported source clock.")
    stamp = _stamp(forwarded_component_clock(component, native=native)[0], seconds=True)
    if stamp is None:
        raise ValueError("A reviewed statement has no usable own clock.")
    if native:
        numeric = component.get("source_create_time", "")
        if numeric and not (isinstance(numeric, str) and re.fullmatch(r"0+", numeric)):
            if (
                not isinstance(numeric, str)
                or re.fullmatch(r"[1-9][0-9]{0,11}", numeric) is None
                or stamp.tzinfo is None
                or int(stamp.timestamp()) != int(numeric)
            ):
                raise ValueError(
                    "A reviewed child's numeric source clock contradicts it."
                )
            source = datetime.fromisoformat(normalized)
            wall = stamp.replace(tzinfo=None)
            if precision == "minute":
                wall = wall.replace(second=0)
            if source != wall:
                raise ValueError("A reviewed child's source clocks disagree.")
            precision = "second"
    return stamp, precision


def _native_component(proof, path):
    values, prefix = proof.get("text_components"), []
    component = None
    for number in path:
        if not isinstance(values, list) or number > len(values):
            raise ValueError("A reviewed native component is missing.")
        component = values[number - 1]
        prefix.append(number)
        if (
            not isinstance(component, dict)
            or component.get("forwarded_item_path", prefix) != prefix
        ):
            raise ValueError("A reviewed native component changed position.")
        if len(prefix) < len(path):
            if component.get("kind") != "forwarded_group":
                raise ValueError("A reviewed path crosses a non-transcript component.")
            values = component.get("text_components")
    if (
        component is None
        or component.get("kind") != "forwarded_text"
        or type(component.get("original_message_type", 1)) is not int
        or component.get("original_message_type", 1) != 1
        or component.get("quoted_components")
        or component.get("quoted_reference")
        or component.get("forwarded_components")
        or component.get("text_components")
    ):
        raise ValueError("A reviewed native reference must be literal ordinary text.")
    return component


def _html_frame(row, proof, snapshots, *, embedded):
    owner = row["account_id"]
    metadata, locator = _metadata(row, proof, snapshots), proof.get("locator", {})
    member = locator.get("member", {}) if isinstance(locator, dict) else {}
    if (
        row["account_key"] != "account:" + owner
        or row["container_account_key"] != row["account_key"]
        or row["identity_status"] != "Corroborated export ID"
        or not row["source_id"].startswith("html-")
        or metadata.get("owner_verified") is not True
        or metadata.get("source_account_id") != owner
        or not isinstance(metadata.get("owner_evidence"), dict)
        or metadata["owner_evidence"].get("native_username") != owner
        or not isinstance(locator, dict)
        or not isinstance(member, dict)
        or not _identifier(locator.get("archive_sha256"))
        or metadata.get("source_sha256") != locator["archive_sha256"]
        or not _identifier(member.get("sha256"))
        or not isinstance(locator.get("member_path"), str)
        or not locator["member_path"]
        or proof.get("export_msgid_namespace") != "MesLocalID"
        or locator.get("export_msgid_namespace") != "MesLocalID"
        or not isinstance(locator.get("export_msgid"), str)
        or re.fullmatch(r"[1-9][0-9]*", locator["export_msgid"]) is None
        or str(locator.get("export_msgtype")) != ("49" if embedded else "1")
    ):
        raise ValueError(
            "A reviewed HTML reference has no complete verified source frame."
        )
    return locator


def _html_component(proof, path, locator):
    values, component = proof.get("forwarded_components"), None
    for position, number in enumerate(path):
        if not isinstance(values, list) or number > len(values):
            raise ValueError("A reviewed HTML component is missing.")
        component = values[number - 1]
        if not isinstance(component, dict):
            raise ValueError("A reviewed HTML component is invalid.")
        if position + 1 < len(path):
            values = component.get("forwarded_components")
    exported = component.get("export_msgid", "") if component else ""
    prefix = locator["export_msgid"] + "_"
    suffix = (
        exported[len(prefix) :]
        if isinstance(exported, str) and exported.startswith(prefix)
        else ""
    )
    # Older exports can retain the parent prefix while omitting the child ID.
    # Explicit reviews still pin the whole source parent and component position.
    if (
        component is None
        or component.get("text_kind") not in {"plain_text", "forwarded_rendered_text"}
        or component.get("forwarded_components")
        or component.get("quoted_components")
        or not (
            "export_msgid" in component
            and isinstance(exported, str)
            and exported.startswith(prefix)
            and (
                exported == prefix
                or _DECIMAL.fullmatch(suffix)
                or _DATA_ID.fullmatch(suffix)
            )
        )
    ):
        raise ValueError("A reviewed HTML reference is not source-pinned literal text.")
    return component


def _reference_member(
    pin, rows, proofs, snapshots, output, contact_id, native_thread_key
):
    parent = pin["parent_message_id"]
    row, proof = rows[parent], proofs[parent]
    cursor = output.execute("SELECT * FROM refs WHERE id=?", (pin["reference_id"],))
    value = cursor.fetchone()
    if value is None:
        raise ValueError("A reviewed reference is not an accepted contact observation.")
    reference = dict(zip((item[0] for item in cursor.description), value, strict=True))
    path = pin["component_path"]
    snapshot = snapshots.get(proof.get("source_snapshot"), {})
    if (
        _sha(row["text"]) != pin["parent_text_sha256"]
        or _sha(_json(proof)) != pin["provenance_sha256"]
        or snapshot.get("source_id") != row["source_id"]
        or snapshot.get("snapshot_id") != pin["source_snapshot_id"]
        or row["source_id"] != pin["source_id"]
        or row["account_key"] != pin["account_key"]
        or reference["parent_message_id"] != parent
        or json.loads(reference["component_path"]) != path
        or reference["source_id"] != row["source_id"]
        or reference["account_key"] != row["account_key"]
        or reference["conversation_id"] != row["conversation_id"]
        or reference["anchor_id"] != "wechat_message_" + reference["id"]
    ):
        raise ValueError("A reviewed reference or its source pins changed.")
    native = row["source_id"].startswith("ios-")
    numbers = path[1:] if native and path[:1] == ["native"] else path
    if (native and path and path[:1] != ["native"]) or (
        not native and path[:1] == ["native"]
    ):
        raise ValueError("A reviewed component belongs to another source namespace.")
    expected = parent + "".join("_forwarded_" + str(number) for number in numbers)
    if reference["id"] != expected:
        raise ValueError("A reviewed reference ID no longer identifies its component.")
    if native:
        if (
            _native_scope(row, proof, snapshots, native_thread_key) is None
            or _native_time(row, proof) is None
            or row["direction"] not in {"sent", "received"}
            or not _author(row["sender_id"])
            or row["direction"] == "sent"
            and row["sender_id"] != row["account_id"]
        ):
            raise ValueError(
                "A reviewed native source has no verified owner and clock."
            )
        if path:
            if (
                proof.get("message_type") != 49
                or proof.get("text_extraction_kind") != "forwarded_text"
                or proof.get("appmsg_subtype") not in {None, 19}
            ):
                raise ValueError("A reviewed child is not ordinary forwarded text.")
            component = _native_component(proof, numbers)
            author, body = (
                _author(component.get("source_sender_id")),
                component.get("text"),
            )
            stamp, precision = _member_clock(component, native=True)
            constraint = _constraint(component)
        else:
            if (
                proof.get("message_type") != 1
                or proof.get("text_extraction_kind") != "plain_text"
            ):
                raise ValueError("A reviewed original must be ordinary native text.")
            author, body = _author(row["sender_id"]), row["text"]
            stamp, precision = _stamp(row["timestamp_value"], seconds=True), "second"
            server = proof.get("server_id", "")
            if server in {"", "0", 0, None}:
                server = ""
            elif (
                type(server) not in {str, int}
                or re.fullmatch(r"-?[1-9][0-9]*", str(server)) is None
            ):
                raise ValueError("A reviewed original has an invalid server identity.")
            constraint = str(server), row["peer_id"]
    else:
        locator = _html_frame(row, proof, snapshots, embedded=bool(path))
        if path:
            if proof.get("text_extraction_kind") not in {
                "forwarded_text",
                "rendered_app_text",
            }:
                raise ValueError("A reviewed HTML child has an unsupported role.")
            component = _html_component(proof, numbers, locator)
            author, body = (
                _author(component.get("sender_native_id")),
                component.get("text"),
            )
            stamp, precision = _member_clock(component, native=False)
            constraint = _constraint(component)
        else:
            if proof.get("text_extraction_kind") != "plain_text" or proof.get(
                "primary_text_sha256"
            ) != _sha(row["text"]):
                raise ValueError(
                    "A reviewed HTML original lacks its literal text hash."
                )
            author, body = _author(proof.get("sender_native_id")), row["text"]
            normalized, precision = normalize_html_clock(
                proof.get("source_time", row["timestamp_value"])
            )
            stamp = _stamp(normalized, seconds=True)
            constraint = "", ""
    if (
        not author
        or canonical_contact_id(author, authority="native_message_sender") != contact_id
        or not isinstance(body, str)
        or not body.strip()
        or _sha(body) != pin["body_sha256"]
        or stamp is None
        or precision not in {"second", "minute"}
        or _stamp(reference["timestamp_value"], seconds=True) != stamp
    ):
        raise ValueError(
            "A reviewed statement changed author, literal text, or own clock."
        )
    return {
        "reference": reference,
        "row": row,
        "proof": proof,
        "stamp": stamp,
        "precision": precision,
        "constraint": constraint,
        "body": body,
        "native": native,
    }


def _reference_plan(
    review, rows, proofs, snapshots, output, contact_id, native_thread_key
):
    if review["contact_id"] != contact_id:
        return []
    members = [
        _reference_member(
            pin, rows, proofs, snapshots, output, contact_id, native_thread_key
        )
        for pin in review["members"]
    ]
    if len({member["body"] for member in members}) != 1:
        raise ValueError("Reviewed statements do not have identical literal bodies.")
    minutes = {member["stamp"].replace(tzinfo=None, second=0) for member in members}
    seconds = {
        member["stamp"].replace(tzinfo=None)
        for member in members
        if member["precision"] == "second"
    }
    epochs = {
        member["stamp"].timestamp()
        for member in members
        if member["precision"] == "second" and member["stamp"].tzinfo is not None
    }
    if len(minutes) != 1 or len(seconds) > 1 or len(epochs) > 1:
        raise ValueError("Reviewed source clocks contradict their own precision.")
    for field in range(2):
        if (
            len(
                {
                    member["constraint"][field]
                    for member in members
                    if member["constraint"][field]
                }
            )
            > 1
        ):
            raise ValueError(
                "Reviewed statements assert contradictory original identities."
            )
    preferred = next(
        member
        for member in members
        if member["reference"]["id"] == review["preferred_reference_id"]
    )
    if not preferred["native"]:
        raise ValueError("A reviewed statement must prefer a verified native source.")
    policy = review["selection_policy"]
    if policy in {"native_group", "native_source"}:
        if (
            _metadata(preferred["row"], preferred["proof"], snapshots).get(
                "source_label"
            )
            != "iPad archive"
        ):
            raise ValueError(
                "The reviewed native preference is not an iPad observation."
            )
        if policy == "native_group" and preferred["row"]["kind"] != "group":
            raise ValueError(
                "The reviewed group preference has no native group context."
            )
    else:
        native_members = [member for member in members if member["native"]]
        if any(
            not json.loads(member["reference"]["component_path"])
            for member in native_members
        ):
            raise ValueError(
                "Earliest-forward selection cannot relabel an original message."
            )
        earliest = min(
            _stamp(member["row"]["timestamp_value"], seconds=True)
            for member in native_members
        )
        if _stamp(preferred["row"]["timestamp_value"], seconds=True) != earliest:
            raise ValueError(
                "The reviewed preferred source was not the earliest forwarding."
            )
    conversation_key = native_thread_key(preferred["row"])
    if not conversation_key:
        raise ValueError("A reviewed native preference has no verified conversation.")
    return [
        (
            {member["reference"]["id"] for member in members},
            preferred["reference"]["id"],
            conversation_key,
        )
    ]


def _journal_tables(connection):
    names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    found = {"reviewed_statement_groups", "reviewed_statement_members"} & names
    if found and len(found) != 2:
        raise ValueError("The reviewed statement journal is incomplete.")
    return bool(found)


def _ensure_journal(connection):
    # Execute individual DDL statements without committing the caller's transaction.
    for statement in _JOURNAL_SCHEMA.split(";"):
        if statement.strip():
            connection.execute(statement)


def restore_reviewed_groups(retained, output):
    """Undo only previously journaled unions before replaying current grouping rules."""
    if not _journal_tables(retained):
        return {"restored_groups": 0, "restored_members": 0}
    groups = [
        tuple(row)
        for row in retained.execute(
            "SELECT id,reference_id,conversation_key FROM reviewed_statement_groups ORDER BY id LIMIT ?",
            (MAX_MEMBERS + 1,),
        )
    ]
    members = [
        tuple(row)
        for row in retained.execute(
            "SELECT reference_id,statement_id FROM reviewed_statement_members ORDER BY reference_id LIMIT ?",
            (MAX_MEMBERS + 1,),
        )
    ]
    if not groups and not members:
        return {"restored_groups": 0, "restored_members": 0}
    group_ids, reference_ids = {row[0] for row in groups}, {row[0] for row in members}
    member_map = dict(members)
    if (
        len(groups) > MAX_MEMBERS
        or len(members) > MAX_MEMBERS
        or len(group_ids) != len(groups)
        or len(reference_ids) != len(members)
        or {row[1] for row in members} != group_ids
        or any(row[1] not in reference_ids for row in groups)
        or any(member_map[row[1]] != row[0] for row in groups)
    ):
        raise ValueError("The reviewed statement journal is inconsistent.")
    current, accepted = {}, set()
    for reference, _ in members:
        row = output.execute(
            "SELECT statement_id FROM statement_members WHERE reference_id=?",
            (reference,),
        ).fetchone()
        if (
            row is None
            or row[0] not in group_ids
            or output.execute("SELECT 1 FROM refs WHERE id=?", (reference,)).fetchone()
            is None
        ):
            raise ValueError("A reviewed member changed before restoration.")
        current[reference] = row[0]
    for group in group_ids:
        accepted.update(
            row[0]
            for row in output.execute(
                "SELECT reference_id FROM statement_members WHERE statement_id=?",
                (group,),
            )
        )
    if accepted != reference_ids:
        raise ValueError("A reviewed group has unexpected extra memberships.")
    _ensure_journal(output)
    output.execute("SAVEPOINT restore_statement_reviews")
    try:
        output.executemany(
            "DELETE FROM statement_groups WHERE id=?",
            [(identifier,) for identifier in group_ids],
        )
        output.executemany("INSERT INTO statement_groups VALUES(?,?,?)", groups)
        output.executemany(
            "UPDATE statement_members SET statement_id=? WHERE reference_id=?",
            [(statement, reference) for reference, statement in members],
        )
        output.execute("DELETE FROM reviewed_statement_groups")
        output.execute("DELETE FROM reviewed_statement_members")
        output.execute("RELEASE restore_statement_reviews")
    except BaseException:
        output.execute("ROLLBACK TO restore_statement_reviews")
        output.execute("RELEASE restore_statement_reviews")
        raise
    return {"restored_groups": len(groups), "restored_members": len(members)}


def apply_reviewed_packet_groups(
    root, source, output, contact_id, *, row_select, prepare_rows, native_thread_key
):
    """Union only completely pinned, accepted copies; journal the whole prior groups."""
    status, raw = _read(root)
    summary = {
        "status": status,
        "reviews_applied": 0,
        "statements_joined": 0,
        "members_retained": 0,
    }
    if status != "present":
        return summary
    try:
        declarations = _declarations(raw)
        if not declarations:
            return {**summary, "status": "empty"}
        rows, proofs, snapshots = _load(source, declarations, row_select, prepare_rows)
        plans = []
        applied_reviews = 0
        for review in declarations:
            planner = (
                _reference_plan
                if review["authority"] == _REFERENCE_AUTHORITY
                else _packet_plan
            )
            reviewed_plans = planner(
                review,
                rows,
                proofs,
                snapshots,
                output,
                contact_id,
                native_thread_key,
            )
            plans.extend(reviewed_plans)
            applied_reviews += bool(reviewed_plans)
        if not plans:
            return {**summary, "status": "no_contact_members"}
        baseline_groups, baseline_members, updates = {}, {}, []
        for references, preferred, conversation_key in plans:
            group_ids = set()
            for reference in references:
                row = output.execute(
                    "SELECT statement_id FROM statement_members WHERE reference_id=?",
                    (reference,),
                ).fetchone()
                if row is None:
                    raise ValueError(
                        "An accepted reviewed reference has no statement group."
                    )
                group_ids.add(row[0])
            members = [
                tuple(value)
                for group in group_ids
                for value in output.execute(
                    "SELECT reference_id,statement_id FROM statement_members WHERE statement_id=?",
                    (group,),
                )
            ]
            if {member[0] for member in members} != references:
                raise ValueError("A reviewed cohort has undeclared statement members.")
            if any(member[0] in baseline_members for member in members):
                raise ValueError(
                    "Different reviewed positions share an existing statement."
                )
            member_groups = {}
            for member in members:
                baseline_members[member[0]] = member
                member_groups.setdefault(member[1], set()).add(member[0])
            for group in group_ids:
                row = output.execute(
                    "SELECT id,reference_id,conversation_key FROM statement_groups WHERE id=?",
                    (group,),
                ).fetchone()
                if row is None:
                    raise ValueError("A reviewed statement group is missing.")
                if row[1] not in member_groups[group]:
                    raise ValueError(
                        "A reviewed statement's preferred reference has foreign membership."
                    )
                baseline_groups[group] = tuple(row)
            winner = next(member[1] for member in members if member[0] == preferred)
            updates.append((references, preferred, winner, group_ids, conversation_key))
        if len(baseline_members) > MAX_MEMBERS:
            raise ValueError(
                "Reviewed statement membership exceeds its bounded budget."
            )
        if _read(root) != (status, raw):
            raise ValueError("Statement reviews changed during validation.")
        if (
            _journal_tables(output)
            and output.execute(
                "SELECT 1 FROM reviewed_statement_members LIMIT 1"
            ).fetchone()
        ):
            if all(
                len(groups) == 1
                and baseline_groups[winner][1:] == (preferred, conversation_key)
                for _, preferred, winner, groups, conversation_key in updates
            ):
                return {**summary, "status": "already_applied"}
            raise ValueError(
                "Restore an earlier review journal before applying another review."
            )
    except (
        ValueError,
        TypeError,
        KeyError,
        UnicodeError,
        RecursionError,
        sqlite3.DatabaseError,
        OverflowError,
    ):
        return {**summary, "status": "rejected"}
    _ensure_journal(output)
    output.execute("SAVEPOINT apply_statement_reviews")
    try:
        output.executemany(
            "INSERT INTO reviewed_statement_groups VALUES(?,?,?)",
            baseline_groups.values(),
        )
        output.executemany(
            "INSERT INTO reviewed_statement_members VALUES(?,?)",
            baseline_members.values(),
        )
        for references, preferred, winner, groups, conversation_key in updates:
            output.executemany(
                "UPDATE statement_members SET statement_id=? WHERE reference_id=?",
                [(winner, reference) for reference in references],
            )
            output.execute(
                "UPDATE statement_groups SET reference_id=?,conversation_key=? WHERE id=?",
                (preferred, conversation_key, winner),
            )
            output.executemany(
                "DELETE FROM statement_groups WHERE id=?",
                [(group,) for group in groups if group != winner],
            )
        output.execute("RELEASE apply_statement_reviews")
    except BaseException:
        output.execute("ROLLBACK TO apply_statement_reviews")
        output.execute("RELEASE apply_statement_reviews")
        raise
    return {
        "status": "applied",
        "reviews_applied": applied_reviews,
        "statements_joined": sum(len(groups) - 1 for _, _, _, groups, _ in updates),
        "members_retained": len(baseline_members),
    }
