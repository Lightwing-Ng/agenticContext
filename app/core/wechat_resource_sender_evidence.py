"""Fill missing resource authors from exact authenticated secondary group events.

Code version: v1.0.1-codex.0

Callers must authenticate the captured database and validate its account anchors
against the primary archive before passing packets here. These helpers perform
no file I/O, source discovery, publication, or inference from neighboring rows.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
import hashlib
import json
import re

from . import wechat_macos_recovery as legacy
from . import wechat_macos_recovery_bank as bank
from .wechat_native_resource_revalidation import _relative


CODE_VERSION = "v1.0.1-codex.0"
PROOF_VERSION = "v1.0.0-codex.0"
VERSION = PROOF_VERSION
_RESOURCE_TYPES = {
    "image": 3,
    "audio": 34,
    "video": 43,
    "sticker": 47,
    "app": 49,
    "file": 49,
}
_HEADER_FIELDS = (
    legacy._PACKET_FIELDS | {"conversation_kind", "peer_proof", "account_binding"}
) - {"messages"}
_MESSAGE_FIELDS = legacy._MESSAGE_FIELDS | {"real_sender_id", "sender_proof"}


def _json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def secondary_native_events(packet):
    """Validate the existing packet contract and return independent event objects.

    Structural validation is deliberately shared with the native recovery writer.
    It does not replace the caller's raw-page authentication and archive-anchor
    validation. Keep the complete packet as an immutable publication receipt.
    """
    if not isinstance(packet, dict):
        raise ValueError("Secondary author evidence needs a native recovery packet.")
    bank._validate_packet(packet, (packet.get("account_id"),))
    header = {key: value for key, value in packet.items() if key != "messages"}
    return tuple(
        {**deepcopy(header), **deepcopy(message)} for message in packet["messages"]
    )


def iter_secondary_native_events(packet):
    """Yield one validated packet without copying every shared header in memory.

    Internal streaming consumers must treat the yielded objects as read-only.
    Public callers needing independent mutable objects use secondary_native_events.
    """
    if not isinstance(packet, dict):
        raise ValueError("Secondary author evidence needs a native recovery packet.")
    bank._validate_packet(packet, (packet.get("account_id"),))
    header = {key: value for key, value in packet.items() if key != "messages"}
    return ({**header, **message} for message in packet["messages"])


def _primary_context(row, conversation):
    if not isinstance(row, Mapping) or not isinstance(conversation, Mapping):
        raise ValueError("A secondary author needs its original row and conversation.")
    try:
        proof = json.loads(row["provenance"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(
            "A secondary author needs original object provenance."
        ) from error
    if not isinstance(proof, dict):
        raise ValueError("A secondary author needs original object provenance.")
    reference, locator = proof.get("resource_reference"), proof.get("locator")
    account_key, peer = conversation.get("account_key"), conversation.get("peer_id")
    if (
        not isinstance(account_key, str)
        or not account_key.startswith("account:")
        or not account_key[8:]
        or conversation.get("kind") != "group"
        or not isinstance(peer, str)
        or not peer.endswith(("@chatroom", "@im.chatroom"))
        or row.get("conversation_id") != conversation.get("id")
        or not isinstance(row.get("id"), str)
        or re.fullmatch(r"[0-9a-f]{64}", row["id"]) is None
        or proof.get("record_id") != row["id"]
        or not isinstance(reference, dict)
        or reference.get("account_id") != account_key[8:]
        or reference.get("peer_id") != peer
        or reference.get("kind") not in _RESOURCE_TYPES
        or not isinstance(locator, dict)
        or locator.get("table")
        != "Chat_" + hashlib.md5(peer.encode(), usedforsecurity=False).hexdigest()
        or type(locator.get("local_id")) is not int
        or locator["local_id"] <= 0
        or row.get("ordinal") != locator["local_id"]
        or type(proof.get("message_type")) is not int
        or proof["message_type"] != _RESOURCE_TYPES[reference["kind"]]
        or type(proof.get("unix_timestamp")) is not int
        or not 1 <= proof["unix_timestamp"] <= 253402271999
        or reference.get("unix_timestamp", proof["unix_timestamp"])
        != proof["unix_timestamp"]
        or row.get("direction") not in {"sent", "received", "unknown"}
    ):
        raise ValueError(
            "A secondary author cannot change original account, peer, row or event metadata."
        )
    owner = account_key[8:]
    relative = _relative(proof.get("source_relative_path"), owner)
    legacy._digest(proof.get("source_sha256"))
    server = legacy._server(proof.get("server_id"))
    backup = legacy._string(proof.get("backup_id"))
    return {
        "message_id": row["id"],
        "account_id": owner,
        "peer_id": peer,
        "server_id": server,
        "message_type": proof["message_type"],
        "unix_timestamp": proof["unix_timestamp"],
        "source_relative_path": relative,
        "source_sha256": proof["source_sha256"],
        "backup_id": backup,
        "local_id": locator["local_id"],
    }, proof


def _validated_event(event):
    if not isinstance(event, dict) or set(event) - (_HEADER_FIELDS | _MESSAGE_FIELDS):
        raise ValueError("A secondary witness needs the exact flattened packet fields.")
    header = {key: event[key] for key in _HEADER_FIELDS if key in event}
    message = {key: event[key] for key in _MESSAGE_FIELDS if key in event}
    bank._validate_packet(
        {**header, "messages": [message]}, (header.get("account_id"),)
    )
    return header, message


def validate_secondary_native_event(event):
    """Validate one flattened event using the existing native packet contract."""
    return _validated_event(event)


def match_secondary_native_event(row, conversation, events, *, max_clock_skew=0):
    """Match one unambiguous witnessed event while preserving source clock facts.

    Identity recovery always uses zero clock skew. Other presentation callers
    may explicitly permit one second for independently observed device clocks;
    the recorded proof retains both values and never changes the original row.
    """
    if type(max_clock_skew) is not int or max_clock_skew not in (0, 1):
        raise ValueError(
            "Secondary event matching permits only exact or one-second source clocks."
        )
    primary, _ = _primary_context(row, conversation)
    if not isinstance(events, Iterable) or isinstance(events, (str, bytes, Mapping)):
        raise ValueError(
            "Pass the complete candidate event sequence, not a guessed witness."
        )
    matches = {}
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("Secondary event candidates must be objects.")
        typ, stamp = event.get("local_type"), event.get("create_time")
        if (
            event.get("peer_id") != primary["peer_id"]
            or event.get("server_id") != primary["server_id"]
            or type(typ) is not int
            or typ & 0xFFFFFFFF != primary["message_type"]
            or type(stamp) is not int
            or abs(stamp - primary["unix_timestamp"]) > max_clock_skew
        ):
            continue
        header, message = _validated_event(event)
        physical = (
            header["account_id"],
            header["source"]["source_id"],
            header["source"]["snapshot_id"],
            header["source"]["table"],
            message["local_id"],
        )
        if physical in matches and matches[physical]["event"] != event:
            raise ValueError(
                "One secondary native source row has conflicting evidence."
            )
        secondary = {
            "account_id": header["account_id"],
            "receipt_id": header["receipt_id"],
            "source": deepcopy(header["source"]),
            "peer_proof": deepcopy(header["peer_proof"]),
            "packet_header_sha256": _sha(header),
            "account_binding_sha256": _sha(header["account_binding"])
            if "account_binding" in header
            else None,
            "anchors_sha256": _sha(header["anchors"]),
            **{
                key: deepcopy(message[key])
                for key in (
                    "local_id",
                    "local_type",
                    "server_id",
                    "create_time",
                    "real_sender_id",
                    "sender_id",
                    "sender_proof",
                    "content_sha256",
                )
                if key in message
            },
        }
        matches[physical] = {
            "event": deepcopy(event),
            "primary": primary,
            "secondary_source": secondary,
            "clock_skew_seconds": message["create_time"] - primary["unix_timestamp"],
        }
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError(
            "Resource author recovery needs one unambiguous secondary native event."
        )
    return next(iter(matches.values()))


def recover_native_resource_sender_from_secondary(row, conversation, events):
    """Fill only an absent author from an exact same-group native event.

    Source owner, message ID, ordinal, text, original clock and direction remain
    unchanged. A known author is retained, and contradictory evidence is refused.
    """
    matched = match_secondary_native_event(row, conversation, events)
    if matched is None:
        return {
            "message": dict(row),
            "resolution": {"version": VERSION, "status": "unmatched"},
        }
    sender = matched["event"]["sender_id"]
    if not sender or sender.endswith(("@chatroom", "@im.chatroom")):
        raise ValueError("A resource author needs an exact native account identity.")
    owner = matched["primary"]["account_id"]
    if row["direction"] not in {"unknown", "sent" if sender == owner else "received"}:
        raise ValueError(
            "The secondary author conflicts with the retained primary direction."
        )
    known = row.get("sender_id")
    if known:
        if known != sender:
            raise ValueError(
                "The secondary native author conflicts with a known original author."
            )
        return {
            "message": dict(row),
            "resolution": {
                "version": VERSION,
                "status": "known_sender_preserved",
                "sender_id": sender,
            },
        }
    resolution = {
        "version": VERSION,
        "status": "resolved",
        "basis": "exact_group_event_authenticated_secondary_native_sender",
        "sender_id": sender,
        "primary": matched["primary"],
        "secondary_source": matched["secondary_source"],
        "clock_skew_seconds": 0,
        "original_row_sha256": _sha(dict(row)),
    }
    result = dict(row)
    proof = json.loads(row["provenance"])
    proof["secondary_source_sender_evidence"] = resolution
    flags = json.loads(row["quality_flags"])
    if not isinstance(flags, list) or any(not isinstance(flag, str) for flag in flags):
        raise ValueError(
            "The original resource quality flags must remain a string list."
        )
    result["sender_id"] = sender
    if not result.get("sender_label") or result["sender_label"] == "Unknown sender":
        result["sender_label"] = sender
    flags = [flag for flag in flags if flag != "sender_id_unavailable"]
    result["quality_flags"] = _json(
        list(dict.fromkeys([*flags, "secondary_native_sender_verified"]))
    )
    result["provenance"] = _json(proof)
    if sender.casefold() not in result["search_text"]:
        result["search_text"] += "\n" + sender.casefold()
    return {"message": result, "resolution": resolution}
