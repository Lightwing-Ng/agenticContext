"""Plan secondary group invitations from two bracketing primary text anchors.

Code version: v1.2.2-codex.0

This evidence strategy performs no publication or source discovery. The shared
completion catalog owns packet authentication, input pins, SQL attachment, and
publication. A foreign viewer's literal copy always keeps its explicit context.
"""

from __future__ import annotations

from collections import defaultdict
import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
from xml.etree import ElementTree

from . import wechat_macos_recovery_bank as bank
from .wechat_resource_sender_evidence import validate_secondary_native_event
from .wechat_text import (
    MAX_XML_BYTES, _system_template_text, _system_xml_payload, extract_wechat_text,
)
from .wechat_wcdb import WCDBBackendUnavailableError, WCDBDecodeError
from .wechat_wcdb_compat import decode_wcdb_text_compatible


CODE_VERSION = "v1.2.2-codex.0"
# Decoder transport changes do not change already published evidence semantics.
PROOF_VERSION = "v1.2.1-codex.0"
VERSION = PROOF_VERSION
STRATEGY = "secondary_group_invitation_v1"
MAX_SCOPE_ROWS = 1_000_000
MAX_SCOPE_BYTES = 512 * 1024**2
MAX_ANCHOR_DISTANCE = 86_400
_SCOPE = ("id", "partition_id", "account_key", "peer_id", "kind")
_COLUMNS = (
    "id", "conversation_id", "ordinal", "sender_id", "sender_label", "direction",
    "text", "time_sort", "timestamp_value", "timestamp_label", "time_basis",
    "provenance", "quality_flags", "search_text",
)
_INVITATION = re.compile(
    r'[^"\r\n]+ invited (?:you to a group chat with .+|.+ to the group chat\.?)'
)
_PARTICIPANT_TEMPLATES = {
    "$username$ invited you to a group chat with $others$",
    "$username$ invited $names$ to the group chat",
    "$username$ invited $names$ to the group chat.",
}


def _json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def _sha(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _object(raw):
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("An invitation needs object source provenance.")
    return value


def _owner(conversation, partition):
    owner = partition.get("account_id")
    if (
        not isinstance(owner, str)
        or not owner
        or partition.get("identity_status") != "Verified archive ID"
        or partition.get("id")
        != "ios-" + hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest()
        or partition.get("account_key") != "account:" + owner
        or conversation.get("partition_id") != partition["id"]
        or conversation.get("account_key") != partition["account_key"]
        or conversation.get("kind") != "group"
        or not isinstance(conversation.get("peer_id"), str)
        or not re.fullmatch(r"[0-9]+@(?:im\.)?chatroom", conversation["peer_id"])
    ):
        raise ValueError("An invitation needs an independently verified primary group.")
    return owner


def _invitation(event):
    header, message = validate_secondary_native_event(event)
    projection = message.get("native_text", {})
    if (
        message["local_type"] & 0xFFFFFFFF not in {10000, 10002}
        or projection.get("text_extraction_kind") != "system_text"
        or projection.get("system_message_subtype") != "sysmsgtemplate"
        or _INVITATION.fullmatch(message["text"]) is None
        or " changed the group name " in message["text"]
    ):
        raise ValueError("Only a typed literal group invitation can be supplemented.")
    return header, message


def _role_template(witness, event):
    """Adapt only a completely revalidated closed native system template."""
    header, message = _invitation(event)
    if not isinstance(witness, dict) or (
        witness.get("source_viewer_account_id") != header["account_id"]
        or witness.get("peer_id") != header["peer_id"]
        or witness.get("source") != header["source"]
        or any(witness.get(key) != message[key] for key in (
            "local_id", "server_id", "local_type", "create_time", "content_sha256"
        ))
        or not isinstance(witness.get("native_content_base64"), str)
        or len(witness["native_content_base64"]) > MAX_XML_BYTES * 2
    ):
        raise ValueError("An invitation participant needs the exact native envelope.")
    try:
        raw = base64.b64decode(witness["native_content_base64"], validate=True)
        if hashlib.sha256(raw).hexdigest() != message["content_sha256"]:
            raise ValueError("An invitation native body differs from its source hash.")
        if witness.get("native_storage_class") == "text":
            raw = raw.decode("utf-8")
        elif witness.get("native_storage_class") != "blob":
            raise ValueError("An invitation source storage class is unsupported.")
        body = decode_wcdb_text_compatible(raw, witness["compression_marker"])
    except (UnicodeError, WCDBDecodeError, WCDBBackendUnavailableError) as error:
        raise ValueError("An invitation role envelope cannot be revalidated.") from error
    if (
        body != witness.get("decoded_text")
        or hashlib.sha256(body.encode()).hexdigest()
        != witness.get("decoded_text_sha256")
        or extract_wechat_text(message["local_type"] & 0xFFFFFFFF, body)
        != message.get("native_text")
    ):
        raise ValueError("Invitation roles lost their exact decoded source projection.")
    payload = _system_xml_payload(body, header["peer_id"])
    if payload is None:
        raise ValueError("Invitation roles require a known scoped system envelope.")
    root = ElementTree.fromstring(payload)
    content = root.find("sysmsgtemplate/content_template")
    template = content.findtext("template") if content is not None else None
    if template not in _PARTICIPANT_TEMPLATES:
        raise ValueError("An invitation role needs a closed known source template.")
    if _system_template_text(content) != message["text"]:
        raise ValueError("The raw template differs from its retained literal notice.")
    return header, content, template


def _participant(witness, event, owner):
    """Use only visible profile usernames, never nicknames or hidden action IDs."""
    _header, content, template = _role_template(witness, event)
    roles = []
    for link in content.findall("link_list/link"):
        name = link.get("name")
        if (
            name in {"username", "names", "others"}
            and "$" + name + "$" in template
            and link.get("type") == "link_profile"
            and any(member.findtext("username") == owner
                    for member in link.findall("memberlist/member"))
        ):
            roles.append(name)
    if not roles:
        raise ValueError("The raw invitation does not identify this primary owner.")
    return {
        "source_envelope_sha256": _sha(witness),
        "visible_participant_roles": sorted(roles),
        "literal_template": template,
    }


def render_system_template_for_viewer(event, witness, target_owner):
    """Render profile roles for the target while retaining the foreign static viewer."""
    header, content, template = _role_template(witness, event)
    rendered = deepcopy(content)
    for member in rendered.findall("link_list/link/memberlist/member"):
        if member.findtext("username") == target_owner:
            member.find("nickname").text = "You"
    if template == "$username$ invited you to a group chat with $others$":
        rendered.find("template").text = (
            "$username$ invited " + header["account_id"]
            + " to a group chat with $others$"
        )
    result = _system_template_text(rendered)
    if result is None:
        raise ValueError("The viewer-aware system template cannot be rendered faithfully.")
    return result, template


def _anchor(row, record, conversation, partition, event_header):
    if set(row) != set(_COLUMNS) or record.get("store") != "primary":
        raise ValueError("Invitation anchors must retain complete primary rows.")
    if row["id"] != record.get("id") or _sha(row) != record.get("sha"):
        raise ValueError("An invitation anchor changed its complete primary row.")
    witness_header, witness = validate_secondary_native_event(record["witness"])
    if witness_header != event_header:
        raise ValueError("Invitation anchors cross their authenticated native snapshot.")
    proof = _object(row["provenance"])
    snapshot = record.get("source_snapshot", {})
    metadata = _object(snapshot.get("metadata", "{}"))
    epoch = proof.get("unix_timestamp")
    stamp = datetime.fromisoformat(row["timestamp_value"])
    expected_table = "Chat_" + hashlib.md5(
        conversation["peer_id"].encode(), usedforsecurity=False
    ).hexdigest()
    if (
        row["conversation_id"] != conversation["id"]
        or proof.get("message_type") != 1
        or proof.get("text_extraction_kind") != "plain_text"
        or proof.get("locator", {}).get("table") != expected_table
        or type(proof.get("source_snapshot")) is not int
        or proof["source_snapshot"] != snapshot.get("id")
        or snapshot.get("source_id") != partition["id"]
        or metadata.get("source_account_id") != partition["account_id"]
        or metadata.get("source_id") != partition["id"]
        or metadata.get("account_hash_verified") is not True
        or metadata.get("account_hash") != partition["id"][4:]
        or witness["local_type"] != 1
        or witness.get("native_text", {}).get("text_extraction_kind") != "plain_text"
        or str(proof.get("server_id")) != witness["server_id"]
        or not row["sender_id"]
        or row["sender_id"] != witness["sender_id"]
        or row["text"] != witness["text"]
        or type(epoch) is not int
        or abs(epoch - witness["create_time"]) > 1
        or stamp.tzinfo is None
        or stamp.timestamp() != epoch
        or not isinstance(row["time_sort"], (int, float))
        or isinstance(row["time_sort"], bool)
        or not math.isfinite(row["time_sort"])
        or not float(row["time_sort"] - epoch).is_integer()
        or abs(row["time_sort"] - epoch) > 86_400
    ):
        raise ValueError("An invitation anchor lost its exact actor, body, clock or owner.")
    return (
        epoch - witness["create_time"],
        int(row["time_sort"] - epoch),
        int(stamp.utcoffset().total_seconds()),
        row["time_basis"],
    )


def project_notice_plan(plan, conversation, anchors, event):
    """Reprove an append plan using current primary anchors and its native witness."""
    if (
        plan.get("strategy") != STRATEGY
        or plan.get("operation") != "append"
        or event != plan.get("event")
        or len(anchors) != 2
        or len(plan.get("anchors", ())) != 2
        or len({row["id"] for row in anchors}) != 2
    ):
        raise ValueError("An invitation append needs two distinct retained anchors.")
    partition = plan["target_partition"]
    owner = _owner(conversation, partition)
    header, native = _invitation(event)
    if (
        header["account_id"] == owner
        or header["peer_id"] != conversation["peer_id"]
        or header.get("conversation_kind") != "group"
        or plan.get("target_scope")
        != {key: conversation[key] for key in _SCOPE}
    ):
        raise ValueError("A secondary invitation crossed its verified group or viewer.")
    coordinates = [
        _anchor(row, record, conversation, partition, header)
        for row, record in zip(anchors, plan["anchors"], strict=True)
    ]
    lower, upper = (
        record["witness"]["create_time"] for record in plan["anchors"]
    )
    mode = plan.get("anchor_mode", "bracket")
    participation = None
    if mode == "authenticated_participant":
        participation = _participant(plan.get("system_template_witness"), event, owner)
        bracketed = native["create_time"] < lower <= upper
    elif mode == "bracket":
        bracketed = lower < native["create_time"] < upper
    else:
        raise ValueError("An invitation uses an unsupported anchor strategy.")
    if (
        coordinates[0] != coordinates[1]
        or not bracketed
        or native["create_time"] - lower > MAX_ANCHOR_DISTANCE
        or upper - native["create_time"] > MAX_ANCHOR_DISTANCE
        or plan["anchors"][0]["witness"]["server_id"]
        == plan["anchors"][1]["witness"]["server_id"]
    ):
        raise ValueError("Invitation clocks need two consistent bracketing native anchors.")
    clock_delta, sort_delta, utc_offset, time_basis = coordinates[0]
    raw_text = native["text"]
    role_witness = plan.get("system_template_witness")
    literal_template = None
    if role_witness is not None:
        display, literal_template = render_system_template_for_viewer(
            event, role_witness, owner
        )
        rendering = "typed_profile_roles"
    elif re.search(r"\byou(?:r|rs|rself)?\b", raw_text, flags=re.IGNORECASE):
        display = "Source account " + header["account_id"] + ": " + raw_text
        rendering = "explicit_source_viewer_caption"
    else:
        display = raw_text
        rendering = "literal_source_copy"
    epoch = native["create_time"] + clock_delta
    if mode == "authenticated_participant" and not (
        conversation["first_sort"] <= epoch + sort_delta <= conversation["last_sort"]
    ):
        raise ValueError("A witnessed invitation falls outside the primary conversation.")
    stamp = datetime.fromtimestamp(epoch, timezone(timedelta(seconds=utc_offset)))
    identity = {
        "strategy": STRATEGY,
        "target_conversation_id": conversation["id"],
        "source_viewer_account_id": header["account_id"],
        "peer_id": header["peer_id"],
        "server_id": native["server_id"],
    }
    identifier = _sha(identity)
    proof = {
        "version": VERSION,
        **identity,
        "raw_system_text": raw_text,
        "raw_system_text_sha256": hashlib.sha256(raw_text.encode()).hexdigest(),
        "source": deepcopy(header["source"]),
        "source_event_sha256": _sha(event),
        "primary_clock_delta_seconds": clock_delta,
        "primary_sort_offset_seconds": sort_delta,
        "anchor_mode": mode,
        "native_owner_participation": participation,
        "raw_system_template": literal_template,
        "viewer_rendering": rendering,
        "system_template_witness_sha256": _sha(role_witness)
        if role_witness is not None else None,
        "anchors": [
            {"id": record["id"], "row_sha256": record["sha"],
             "native_witness_sha256": _sha(record["witness"])}
            for record in plan["anchors"]
        ],
    }
    provenance = {
        "record_id": identifier,
        "server_id": native["server_id"],
        "message_type": native["local_type"] & 0xFFFFFFFF,
        "text_extraction_kind": "system_text",
        "system_message_subtype": "sysmsgtemplate",
        "unix_timestamp": epoch,
        "secondary_source_invitation": proof,
    }
    message = dict(zip(_COLUMNS, (
        identifier, conversation["id"], anchors[0]["ordinal"] + 1, "",
        "System Message", "unknown", display, epoch + sort_delta,
        stamp.isoformat(), f"{stamp.day} {stamp.strftime('%b %Y %H:%M:%S')}",
        time_basis, _json(provenance),
        _json(["system_notice", "secondary_source_invitation"]), display.casefold(),
    ), strict=True))
    if plan.get("message", message) != message or plan.get("proof", proof) != proof:
        raise ValueError("An invitation plan changed its verified projection.")
    return {"message": message, "proof": proof}


def build_notice_plans(history, resource_catalog, packet, *, native_system_roles=()):
    """Plan every eligible invitation in a packet without inventing new accounts."""
    bank._validate_packet(packet, (packet.get("account_id"),))
    if packet.get("conversation_kind") != "group":
        return []
    header = {key: value for key, value in packet.items() if key != "messages"}
    roles = {}
    for witness in native_system_roles:
        key = (witness.get("source_viewer_account_id"), witness.get("server_id"))
        if key in roles and roles[key] != witness:
            raise ValueError("One native invitation has conflicting raw role evidence.")
        roles[key] = witness
    native = defaultdict(list)
    notices = []
    for message in packet["messages"]:
        if message["local_type"] == 1:
            native[message["server_id"]].append(message)
        elif message["local_type"] & 0xFFFFFFFF in {10000, 10002}:
            event = {**header, **message}
            try:
                _invitation(event)
            except ValueError:
                continue
            notices.append(event)
    if not notices:
        return []
    plans = []
    for value in history.execute(
        "SELECT * FROM main.conversations WHERE peer_id=? AND kind='group'",
        (packet["peer_id"],),
    ).fetchall():
        conversation = dict(value)
        found = history.execute(
            "SELECT * FROM main.partitions WHERE id=?", (conversation["partition_id"],)
        ).fetchone()
        if found is None:
            continue
        partition = dict(found)
        try:
            owner = _owner(conversation, partition)
        except ValueError:
            continue
        if owner == packet["account_id"]:
            continue
        existing, matched, snapshots = set(), defaultdict(list), {}
        total_bytes = 0
        rows = history.execute(
            "SELECT * FROM main.messages WHERE conversation_id=? LIMIT ?",
            (conversation["id"], MAX_SCOPE_ROWS + 1),
        )
        for index, row in enumerate(rows):
            row = dict(row)
            total_bytes += len(row["provenance"].encode()) + len(row["text"].encode())
            if index >= MAX_SCOPE_ROWS or total_bytes > MAX_SCOPE_BYTES:
                raise ValueError("Invitation primary anchors exceed their bounded scope.")
            provenance = _object(row["provenance"])
            server = str(provenance.get("server_id"))
            existing.add(server)
            candidates = native.get(server, ())
            if len(candidates) != 1:
                continue
            snapshot_id = provenance.get("source_snapshot")
            if type(snapshot_id) is not int:
                continue
            if snapshot_id not in snapshots:
                snapshot = history.execute(
                    "SELECT * FROM main.source_snapshots WHERE id=?", (snapshot_id,)
                ).fetchone()
                snapshots[snapshot_id] = dict(snapshot) if snapshot is not None else {}
            record = {
                "id": row["id"], "store": "primary", "row": row, "sha": _sha(row),
                "witness": {**header, **candidates[0]},
                "source_snapshot": snapshots[snapshot_id],
            }
            try:
                coordinates = _anchor(row, record, conversation, partition, header)
            except (ValueError, TypeError, KeyError):
                continue
            matched[coordinates].append(record)
        if resource_catalog is not None:
            for index, row in enumerate(resource_catalog.execute(
                "SELECT provenance FROM messages WHERE conversation_id=? LIMIT ?",
                (conversation["id"], MAX_SCOPE_ROWS + 1),
            )):
                if index >= MAX_SCOPE_ROWS:
                    raise ValueError("Invitation resource membership exceeds its scope.")
                existing.add(str(_object(row[0]).get("server_id")))
        for event in notices:
            if event["server_id"] in existing:
                continue
            choices = []
            def propose(pair, *, mode="bracket", participation=None):
                plan = {
                    "strategy": STRATEGY, "operation": "append",
                    "target_partition": partition,
                    "target_scope": {key: conversation[key] for key in _SCOPE},
                    "anchors": pair, "event": deepcopy(event),
                    "anchor_mode": mode,
                    "event_key": {"peer_id": event["peer_id"],
                                  "server_id": event["server_id"],
                                  "message_type": event["local_type"] & 0xFFFFFFFF},
                }
                template_witness = roles.get((header["account_id"], event["server_id"]))
                if template_witness is not None:
                    plan["system_template_witness"] = deepcopy(template_witness)
                try:
                    result = project_notice_plan(
                        plan, conversation, [r["row"] for r in pair], event
                    )
                except ValueError:
                    return
                choices.append({**plan, **result})
            for records in matched.values():
                earlier = [r for r in records if r["witness"]["create_time"] < event["create_time"]]
                later = [r for r in records if r["witness"]["create_time"] > event["create_time"]]
                if earlier and later:
                    pair = [
                        max(earlier, key=lambda r: (r["witness"]["create_time"], r["id"])),
                        min(later, key=lambda r: (r["witness"]["create_time"], r["id"])),
                    ]
                    propose(pair)
            participation = roles.get((header["account_id"], event["server_id"]))
            if not choices and participation is not None:
                for records in matched.values():
                    following = sorted(
                        (r for r in records
                         if r["witness"]["create_time"] > event["create_time"]),
                        key=lambda r: (r["witness"]["create_time"], r["id"]),
                    )
                    distinct = {}
                    for record in following:
                        distinct.setdefault(record["witness"]["server_id"], record)
                    if len(distinct) >= 2:
                        propose(
                            list(distinct.values())[:2], mode="authenticated_participant",
                            participation=participation,
                        )
            if choices:
                def distance(proposal):
                    first, last = (
                        a["witness"]["create_time"] for a in proposal["anchors"]
                    )
                    if proposal["anchor_mode"] == "authenticated_participant":
                        return last - event["create_time"], first - event["create_time"]
                    return last - first, max(
                        event["create_time"] - first, last - event["create_time"]
                    )
                closest = min(map(distance, choices))
                choices = [proposal for proposal in choices if distance(proposal) == closest]
                if len({_sha(proposal["message"]) for proposal in choices}) == 1:
                    plans.append(choices[0])
    return plans
