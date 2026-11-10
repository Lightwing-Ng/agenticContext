"""Complete native-owner notices with the existing authenticated bank adapter.

Code version: v1.0.0-codex.0
No publication, source discovery, foreign-viewer inference, or clock repair occurs.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib

from . import wechat_macos_recovery_bank as bank
from .wechat_resource_sender_evidence import validate_secondary_native_event
from .wechat_secondary_system_notices import (
    MAX_SCOPE_BYTES, MAX_SCOPE_ROWS, _COLUMNS, _SCOPE, _object, _owner, _sha,
)
from .wechat_text import MAX_XML_BYTES, extract_wechat_text
from .wechat_wcdb import WCDBBackendUnavailableError, WCDBDecodeError
from .wechat_wcdb_compat import decode_wcdb_text_compatible

VERSION = "v1.0.0-codex.0"
STRATEGY = "native_owner_system_notice_v1"


def _native_system(event):
    header, message = validate_secondary_native_event(event)
    if (
        "account_binding" not in header
        or message["local_type"] & 0xFFFFFFFF not in {10000, 10002}
        or message.get("native_text", {}).get("text_extraction_kind") != "system_text"
        or message["native_text"].get("system_message_subtype") != "sysmsgtemplate"
    ):
        raise ValueError("Native-owner notices need an independently bound typed system template.")
    return header, message


def _envelope(witness, event):
    header, message = _native_system(event)
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
        raise ValueError("A native-owner notice needs its exact authenticated source envelope.")
    try:
        raw = base64.b64decode(witness["native_content_base64"], validate=True)
        if hashlib.sha256(raw).hexdigest() != message["content_sha256"]:
            raise ValueError("A native-owner notice changed its raw body hash.")
        if witness.get("native_storage_class") == "text":
            raw = raw.decode("utf-8")
        elif witness.get("native_storage_class") != "blob":
            raise ValueError("A native-owner notice has an unsupported source storage class.")
        text = decode_wcdb_text_compatible(raw, witness["compression_marker"])
    except (UnicodeError, WCDBDecodeError, WCDBBackendUnavailableError) as error:
        raise ValueError("A native-owner source envelope cannot be revalidated.") from error
    if (
        text != witness.get("decoded_text")
        or hashlib.sha256(text.encode()).hexdigest() != witness.get("decoded_text_sha256")
        or extract_wechat_text(message["local_type"] & 0xFFFFFFFF, text)
        != message["native_text"]
    ):
        raise ValueError("A native-owner notice no longer reproduces its closed native projection.")
    return header, message


def project_native_owner_notice_plan(plan, conversation, anchors, event):
    """Reproject one exact native-owner event without foreign clock or role inference."""
    if (
        plan.get("strategy") != STRATEGY
        or plan.get("operation") != "append"
        or plan.get("anchors") != []
        or anchors != []
        or plan.get("event") != event
    ):
        raise ValueError("A native-owner notice uses only its closed anchor-free strategy.")
    owner = _owner(conversation, plan["target_partition"])
    header, message = _envelope(plan.get("system_template_witness"), event)
    if (
        header["account_id"] != owner
        or header["session_id"] != conversation["id"]
        or header["peer_id"] != conversation["peer_id"]
        or header["conversation_kind"] != conversation["kind"]
        or plan.get("target_scope") != {key: conversation[key] for key in _SCOPE}
        or plan.get("event_key") != {
            "peer_id": header["peer_id"], "server_id": message["server_id"],
            "message_type": message["local_type"] & 0xFFFFFFFF,
        }
    ):
        raise ValueError("A native-owner notice crossed its exact primary owner, peer or session.")
    packet = {**header, "messages": [message]}
    projected = dict(zip(_COLUMNS, bank._row(packet, message), strict=True))
    proof = {
        "version": VERSION, "strategy": STRATEGY,
        "source_viewer_account_id": owner,
        "target_conversation_id": conversation["id"],
        "source_event_sha256": _sha(event),
        "source_envelope_sha256": _sha(plan["system_template_witness"]),
        "native_projection_adapter": "wechat_macos_recovery_bank._row",
    }
    if plan.get("message", projected) != projected or plan.get("proof", proof) != proof:
        raise ValueError("A native-owner plan changed its exact existing-adapter projection.")
    return {"message": projected, "proof": proof}


def build_native_owner_notice_plans(
    history, resource_catalog, packet, *, native_system_roles=(), observed_event_keys=()
):
    """Prepare existing-owner notices from explicit independently bound native packets."""
    bank._validate_packet(packet, (packet.get("account_id"),))
    if packet.get("conversation_kind") != "group":
        return []
    bank._binding(history, packet)
    found = history.execute(
        "SELECT * FROM main.conversations WHERE id=?", (packet["session_id"],)
    ).fetchone()
    conversation = dict(found)
    partition = dict(history.execute(
        "SELECT * FROM main.partitions WHERE id=?", (conversation["partition_id"],)
    ).fetchone())
    if _owner(conversation, partition) != packet["account_id"]:
        raise ValueError("A native-owner plan requires the current canonical source owner.")
    existing = set(observed_event_keys)
    for connection in (history, resource_catalog):
        if connection is None:
            continue
        size = 0
        for index, row in enumerate(connection.execute(
            "SELECT provenance FROM messages WHERE conversation_id=? LIMIT ?",
            (conversation["id"], MAX_SCOPE_ROWS + 1),
        )):
            size += len(row[0].encode())
            if index >= MAX_SCOPE_ROWS or size > MAX_SCOPE_BYTES:
                raise ValueError("Native-owner system membership exceeds its bounded scope.")
            provenance = _object(row[0])
            existing.add((conversation["id"], str(provenance.get("server_id"))))
    header = {key: value for key, value in packet.items() if key != "messages"}
    witnesses = {}
    for witness in native_system_roles:
        key = (
            witness.get("source_viewer_account_id"), witness.get("peer_id"),
            witness.get("server_id"), witness.get("source", {}).get("source_id"),
        )
        if key in witnesses and witnesses[key] != witness:
            raise ValueError("A native-owner event has conflicting retained raw envelopes.")
        witnesses[key] = witness
    plans = []
    for message in packet["messages"]:
        if (conversation["id"], message["server_id"]) in existing:
            continue
        event = {**header, **message}
        try:
            _native_system(event)
        except ValueError:
            continue
        witness = witnesses.get((
            packet["account_id"], packet["peer_id"], message["server_id"],
            packet["source"]["source_id"],
        ))
        if witness is None:
            continue
        plan = {
            "strategy": STRATEGY, "operation": "append", "anchors": [],
            "target_partition": partition,
            "target_scope": {key: conversation[key] for key in _SCOPE},
            "event": deepcopy(event), "system_template_witness": deepcopy(witness),
            "event_key": {"peer_id": packet["peer_id"], "server_id": message["server_id"],
                          "message_type": message["local_type"] & 0xFFFFFFFF},
        }
        try:
            projected = project_native_owner_notice_plan(plan, conversation, [], event)
        except ValueError:
            continue
        plans.append({**plan, **projected})
    return plans


def project_notice_plan(plan, conversation, anchors, event):
    """Dispatch only known native-owner plans; retain the frozen foreign strategy."""
    if plan.get("strategy") == STRATEGY:
        return project_native_owner_notice_plan(plan, conversation, anchors, event)
    from .wechat_secondary_system_notices import project_notice_plan as project_foreign
    return project_foreign(plan, conversation, anchors, event)
