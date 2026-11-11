"""Project redundant native quote padding only against a retained page original.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import re

from .wechat_quoted_contacts import _native_quote, _quoted_author, _source_envelope
from .wechat_reconcile import native_text_metadata
from .wechat_resource_snapshot_adapter import _relative
from .wechat_text import native_components_text


VERSION = "v1.0.1-codex.0"
MATCH_BASIS = "same_native_page_original_without_quote_padding_v1"
MAX_PAGE_ITEMS = 1_000
_HEX = re.compile(r"[0-9a-f]{64}")
_SERVER = re.compile(r"[1-9][0-9]{0,18}")
_DATABASE = re.compile(r"(?:message_[0-9]+|MM)\.sqlite")


def _server(value):
    return (
        isinstance(value, str)
        and _SERVER.fullmatch(value) is not None
        and int(value) <= 2**63 - 1
    )


def _key(item, server):
    context = item.messaging
    proof = context.get("provenance")
    if not isinstance(proof, dict) or not _server(server):
        return None
    snapshot = proof.get("source_snapshot")
    scope = (
        item.conversation_id,
        context.get("account_key"),
        context.get("partition_id"),
    )
    if (
        type(snapshot) is not int
        or snapshot <= 0
        or any(not isinstance(value, str) or not value for value in scope)
    ):
        return None
    return *scope, snapshot, server


def _declares_database(value, metadata, relative):
    if not isinstance(value, dict):
        return False
    if value.get("path") == relative:
        return True
    if (
        set(value) != {"bytes", "member_path", "sha256"}
        or type(value["bytes"]) is not int
        or value["bytes"] < 0
        or metadata.get("container_kind") != "legacy_ios"
        or any(
            not isinstance(metadata.get(key), str)
            or _HEX.fullmatch(metadata[key]) is None
            for key in ("archive_sha256", "identity_sha256")
        )
    ):
        return False
    try:
        return _relative(value["member_path"], metadata["account_hash"]) == relative
    except ValueError:
        return False


def _proof(item, partitions, display_names, snapshots):
    context = item.messaging
    envelope = _source_envelope(context, partitions, display_names, snapshots)
    if envelope is None:
        return None
    owner, snapshot = envelope
    proof = context["provenance"]
    locator = proof.get("locator")
    if not isinstance(locator, dict):
        return None
    local = locator.get("local_id")
    database = locator.get("database")
    digest = locator.get("database_sha256")
    peer = context.get("peer_id")
    if (
        type(local) is not int
        or not 0 < local <= 2**63 - 1
        or type(locator.get("row_id")) is not int
        or locator["row_id"] != local
        or not isinstance(database, str)
        or _DATABASE.fullmatch(database) is None
        or not isinstance(digest, str)
        or _HEX.fullmatch(digest) is None
        or not isinstance(peer, str)
        or not peer
        or type(proof.get("unix_timestamp")) is not int
        or not _server(proof.get("server_id"))
    ):
        return None
    relative = "Documents/" + snapshot["metadata"]["account_hash"] + "/DB/" + database
    table = "Chat_" + hashlib.md5(peer.encode(), usedforsecurity=False).hexdigest()
    if (
        locator.get("database_relative_path") != relative
        or locator.get("table") != table
    ):
        return None
    # Historical iPad receipts keep database digests only in message locators.
    # Both messages must still select the same exact digest and native scope.
    # A declared database inventory, when present, must independently agree.
    if "input_databases" in snapshot["metadata"]:
        inputs = snapshot["metadata"]["input_databases"]
        if not isinstance(inputs, list) or len(inputs) > 128:
            return None
        declared = [
            value
            for value in inputs
            if _declares_database(value, snapshot["metadata"], relative)
        ]
        if len(declared) != 1 or declared[0].get("sha256") != digest:
            return None
    return (
        (
            owner,
            peer,
            snapshot["snapshot_id"],
            relative,
            digest,
            table,
        ),
        local,
        proof,
    )


def project_native_quote_padding(items, display_names, account_rows, source_snapshots):
    """Change display copy only when an exact original is present on this page.

    Missing, cross-page, ambiguous, conflicting, or intentionally padded originals
    retain their quoted whitespace. Source text, references, and clocks are never
    rewritten, and this projection performs no database or filesystem reads.
    """
    if not isinstance(items, (list, tuple)) or len(items) > MAX_PAGE_ITEMS:
        return
    partitions = {row["partition_id"]: row for row in account_rows}
    originals = defaultdict(list)
    for item in items:
        proof = item.messaging.get("provenance")
        key = _key(item, proof.get("server_id")) if isinstance(proof, dict) else None
        if key is not None:
            originals[key].append(item)
        quote = item.messaging.get("quoted_reply")
        if isinstance(quote, dict) and quote.get("display_text_basis") == MATCH_BASIS:
            for field in ("display_text", "display_text_basis", "display_original_id"):
                quote.pop(field, None)
    for item in items:
        candidate = _native_quote(item.messaging)
        if candidate is None:
            continue
        quote, reference = candidate
        raw = quote.get("text")
        if (
            "display_text" in quote
            or quote.get("forwarded_record")
            or quote.get("payment")
            or not isinstance(raw, str)
            or not raw.startswith(("\r", "\n"))
            or type(reference.get("original_message_type")) is not int
            or reference["original_message_type"] != 1
        ):
            continue
        clean = raw.lstrip("\r\n")
        if not clean:
            continue
        key = _key(item, reference.get("source_ids", {}).get("svrid"))
        matching = originals.get(key, ())
        if len(matching) != 1:
            continue
        original = matching[0]
        if original is item or original.content_text != clean:
            continue
        current = _proof(item, partitions, display_names, source_snapshots)
        previous = _proof(original, partitions, display_names, source_snapshots)
        if current is None or previous is None or current[0] != previous[0]:
            continue
        source, local, proof = current
        _, old_local, old_proof = previous
        if (
            old_local >= local
            or old_proof.get("message_type") != 1
            or type(old_proof.get("message_type")) is not int
            or old_proof.get("text_extraction_kind") != "plain_text"
            or old_proof["unix_timestamp"] > proof["unix_timestamp"]
            or _quoted_author(reference, source[1])
            != original.messaging.get("sender_id")
        ):
            continue
        try:
            metadata = native_text_metadata(proof, proof["locator"])
            components = metadata.get("text_components", [])
            if (
                metadata.get("appmsg_subtype") != 57
                or len(components) != 2
                or [value.get("kind") for value in components]
                != ["reply_text", "quoted_text"]
                or components[1].get("text") != raw
                or native_components_text(components) != item.content_text
            ):
                continue
        except (ValueError, TypeError, KeyError, RecursionError):
            continue
        quote.update(
            display_text=clean,
            display_text_basis=MATCH_BASIS,
            display_original_id=original.stable_id,
        )
