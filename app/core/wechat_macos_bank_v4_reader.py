"""Validate retained schema-4 Mac revisions without writing or reparsing sources.

Code version: v1.0.0-codex.0

Only the three retained monotone revision policies are accepted. New parser
policies and revision publication remain owned by the canonical bank writer.
"""

from __future__ import annotations

from itertools import groupby
import json
import re

from . import wechat_text


MAX_REVISIONS = 32
POLICY = "same-authenticated-native-row-v1"
SCHEMA = """
CREATE TABLE projection_revisions (
 message_id TEXT NOT NULL, sequence INTEGER NOT NULL,
 prior_row TEXT NOT NULL, prior_signature TEXT NOT NULL,
 next_row_sha256 TEXT NOT NULL, next_signature TEXT NOT NULL,
 native_identity_sha256 TEXT NOT NULL, source_key TEXT NOT NULL,
 parser_revision TEXT NOT NULL, reason TEXT NOT NULL,
 previous_digest TEXT NOT NULL, digest TEXT NOT NULL,
 PRIMARY KEY(message_id,sequence)
) WITHOUT ROWID;
"""
_RAW_FIELDS = (
    "account_id",
    "peer_id",
    "database_relative_path",
    "table",
    "local_id",
    "local_type",
    "server_id",
    "create_time",
    "content_sha256",
    "real_sender_id",
)
_STABLE_COLUMNS = (
    "id",
    "conversation_id",
    "ordinal",
    "time_sort",
    "timestamp_value",
    "timestamp_label",
    "time_basis",
)
_METADATA_SOURCE_FIELDS = (
    *_RAW_FIELDS,
    "source_id",
    "snapshot_id",
    "main_sha256",
    "wal_sha256",
)

_REASONS = {
    "app_card_metadata_completion",
    "forwarded_completion",
    "quoted_reply_completion",
}
_CARD_LABEL_FIELDS = (
    "safe_title",
    "app_name",
    "app_name_source",
    "publisher_name",
    "publisher_name_source",
)


def _empty_metadata(value):
    return value is None or value == "" or value == {} or value == []


def _card_metadata(resource):
    if resource.get("app_metadata_version") != "1":
        return None
    return {
        "schema_version": 1,
        **{key: resource[key] for key in _CARD_LABEL_FIELDS if key in resource},
    }


def _bank():
    from . import wechat_macos_recovery_bank

    return wechat_macos_recovery_bank


def _has_table(connection, schema="main"):
    return (
        connection.execute(
            f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name='projection_revisions'"
        ).fetchone()
        is not None
    )


def _identity(row):
    bank = _bank()
    native = json.loads(row["provenance"]).get("macos_recovery")
    if not isinstance(native, dict) or any(
        field not in native for field in _RAW_FIELDS
    ):
        raise ValueError("Parser repair needs exact authenticated native coordinates.")
    value = {field: native[field] for field in _RAW_FIELDS}
    if str(json.loads(row["provenance"]).get("server_id")) != str(
        value["server_id"]
    ) or row["conversation_id"] != native.get("session_id"):
        raise ValueError("Parser repair coordinates cross their canonical event.")
    return value, bank._sha(value)


def _projection(row):
    bank = _bank()
    proof = json.loads(row["provenance"])
    return {
        **{key: proof[key] for key in bank._SIGNATURE_FIELDS if key in proof},
        "text": row["text"],
        "text_extraction_kind": proof.get("text_extraction_kind"),
        "original_message_type": proof.get("message_type"),
        "flags": json.loads(row["quality_flags"]),
    }


def _components_preserved(old, new, depth=0):
    """Every old source path must retain its complete leaf and author metadata."""
    if depth > 8 or not isinstance(old, list) or not isinstance(new, list):
        return False
    selected = {}
    ordered = []
    for item in new:
        path = item.get("forwarded_item_path") if isinstance(item, dict) else None
        if (
            not isinstance(path, list)
            or not path
            or len(path) != depth + 1
            or any(type(x) is not int or x < 1 for x in path)
        ):
            return False
        key = tuple(path)
        if key in selected:
            return False
        selected[key] = item
        ordered.append(key)
    if ordered != sorted(ordered):
        return False
    prior_paths = [
        tuple(item.get("forwarded_item_path", ()))
        for item in old
        if isinstance(item, dict)
    ]
    if len(prior_paths) != len(set(prior_paths)):
        return False
    positions = [ordered.index(path) for path in prior_paths if path in selected]
    if positions != sorted(positions):
        return False
    for prior in old:
        if not isinstance(prior, dict):
            return False
        path = prior.get("forwarded_item_path")
        if not isinstance(path, list) or tuple(path) not in selected:
            return False
        current = selected[tuple(path)]
        if prior.get("kind") == "forwarded_group":
            if {k: v for k, v in prior.items() if k != "text_components"} != {
                k: v for k, v in current.items() if k != "text_components"
            } or not _components_preserved(
                prior.get("text_components"), current.get("text_components"), depth + 1
            ):
                return False
        elif prior != current:
            return False
    return True


def _resource_upgrade(old_row, new_row):
    """Fill source-bound card labels while preserving every known label and role."""
    old_proof, new_proof = (json.loads(row["provenance"]) for row in (old_row, new_row))
    old, new = (proof.get("resource_reference") for proof in (old_proof, new_proof))
    if (
        not isinstance(old, dict)
        or not isinstance(new, dict)
        or old.get("kind") != "app"
        or new.get("kind") != "app"
        or new.get("app_metadata_version") != "1"
        or old_row["text"] != new_row["text"]
        or old_row["text"] != "[App message]"
        or old_proof.get("text_extraction_kind") != "resource_placeholder"
        or new_proof.get("text_extraction_kind") != "resource_placeholder"
        or new_proof.get("app_card_metadata") != _card_metadata(new)
    ):
        return False
    labels = {*_CARD_LABEL_FIELDS, "app_metadata_version"}
    if {key: value for key, value in old.items() if key not in labels} != {
        key: value for key, value in new.items() if key not in labels
    }:
        return False
    if any(
        not _empty_metadata(old.get(key)) and old[key] != new.get(key)
        for key in _CARD_LABEL_FIELDS
    ):
        return False
    prior, current = _projection(old_row), _projection(new_row)
    return prior.get("app_card_metadata") != current.get("app_card_metadata") and {
        key: value for key, value in prior.items() if key != "app_card_metadata"
    } == {key: value for key, value in current.items() if key != "app_card_metadata"}


def _reason(old_row, new_row):
    old, new = _projection(old_row), _projection(new_row)
    if any(
        "text_components" in projection
        and wechat_text.native_components_text(projection["text_components"])
        != projection["text"]
        for projection in (old, new)
    ):
        return None
    raw, _digest = _identity(old_row)
    if raw["local_type"] & 0xFFFFFFFF != 49:
        return None
    if _resource_upgrade(old_row, new_row):
        return "app_card_metadata_completion"
    if (
        old["text_extraction_kind"] == new["text_extraction_kind"] == "forwarded_text"
        and old.get("appmsg_subtype") == new.get("appmsg_subtype") == 19
        and type(old.get("omitted_forwarded_items")) is int
        and type(new.get("omitted_forwarded_items")) is int
        and 0 <= new["omitted_forwarded_items"] < old["omitted_forwarded_items"]
        and old.get("forwarded_record_title") == new.get("forwarded_record_title")
        and _components_preserved(
            old.get("text_components"), new.get("text_components")
        )
        and {
            k: v
            for k, v in old.items()
            if k not in {"text", "flags", "text_components", "omitted_forwarded_items"}
        }
        == {
            k: v
            for k, v in new.items()
            if k not in {"text", "flags", "text_components", "omitted_forwarded_items"}
        }
        and set(old["flags"])
        - {
            "forwarded_non_text_or_unreadable_items_omitted",
            "forwarded_record_partially_unreadable",
        }
        <= set(new["flags"])
        and set(new["flags"])
        <= {
            "forwarded_text_only",
            "forwarded_resource_metadata_only",
            "forwarded_non_text_or_unreadable_items_omitted",
            "forwarded_record_partially_unreadable",
        }
    ):
        return "forwarded_completion"
    reference = old.get("quoted_reference")
    prior_components, current_components = (
        old.get("text_components"),
        new.get("text_components"),
    )
    if (
        old["text_extraction_kind"] == new["text_extraction_kind"] == "quoted_reply"
        and old.get("appmsg_subtype") == new.get("appmsg_subtype") == 57
        and isinstance(reference, dict)
        and reference == new.get("quoted_reference")
        and isinstance(prior_components, list)
        and isinstance(current_components, list)
        and all(item.get("kind") == "reply_text" for item in prior_components)
        and current_components[: len(prior_components)] == prior_components
        and len(current_components) == len(prior_components) + 1
        and set(old["flags"])
        & {"quoted_text_unavailable", "quoted_non_text_content_omitted"}
        and set(new["flags"]) == {"quoted_reply_envelope", "quoted_native_reply_body"}
    ):
        quote = current_components[-1]
        if (
            set(quote)
            == {
                "kind",
                "text",
                "appmsg_subtype",
                "text_extraction_kind",
                "flags",
                *reference,
            }
            and quote.get("kind") == "quoted_text"
            and isinstance(quote.get("text"), str)
            and quote["text"].strip()
            and quote.get("appmsg_subtype") == 57
            and quote.get("text_extraction_kind") == "quoted_reply"
            and quote.get("flags") == ["quoted_native_reply_body"]
            and all(quote[key] == value for key, value in reference.items())
            and reference.get("original_message_type") == 49
        ):
            return "quoted_reply_completion"
    return None


def _same_event(old, new):
    old_native, old_digest = _identity(old)
    new_native, new_digest = _identity(new)
    if old_native != new_native or old_digest != new_digest:
        return False
    if any(old[field] != new[field] for field in _STABLE_COLUMNS):
        return False
    return all(
        old[field] == new[field] for field in ("sender_id", "sender_label", "direction")
    )


def validate(connection):
    """Require a complete, bounded chain from every preserved projection to today's row."""
    bank = _bank()
    if not _has_table(connection):
        return
    if (
        connection.execute("SELECT 1 FROM projection_revisions LIMIT 1").fetchone()
        is None
    ):
        raise ValueError("A semantic-revision bank cannot lose its entire history.")
    if connection.execute(
        "SELECT 1 FROM messages m WHERE CASE WHEN instr(m.provenance,'\"macos_semantic_reparse\"')>0 "
        "THEN json_type(m.provenance,'$.macos_semantic_reparse') END IS NOT NULL "
        "AND NOT EXISTS(SELECT 1 FROM projection_revisions p WHERE p.message_id=m.id) LIMIT 1"
    ).fetchone():
        raise ValueError("A repaired canonical row lost its revision history.")
    cursor = connection.execute(
        "SELECT * FROM projection_revisions ORDER BY message_id,sequence"
    )
    columns = [item[0] for item in cursor.description]
    for identifier, group in groupby(cursor, key=lambda row: row[0]):
        chain = [dict(zip(columns, row, strict=True)) for row in group]
        if not 1 <= len(chain) <= MAX_REVISIONS:
            raise ValueError("A native repair has invalid revision coverage.")
        latest = connection.execute(
            "SELECT * FROM messages WHERE id=?", (identifier,)
        ).fetchone()
        message_columns = [
            item[1] for item in connection.execute("PRAGMA table_info(messages)")
        ]
        if latest is None:
            raise ValueError("A native revision lost its canonical message.")
        latest = dict(zip(message_columns, latest, strict=True))
        previous_digest, signatures = "", set()
        for index, record in enumerate(chain):
            prior = json.loads(record["prior_row"])
            following = (
                json.loads(chain[index + 1]["prior_row"])
                if index + 1 < len(chain)
                else latest
            )
            body = {key: value for key, value in record.items() if key != "digest"}
            if (
                record["sequence"] != index + 1
                or record["previous_digest"] != previous_digest
                or record["digest"] != bank._sha({"policy": POLICY, **body})
                or set(prior) != set(message_columns)
                or set(following) != set(message_columns)
                or prior["id"] != identifier
                or following["id"] != identifier
                or bank._event_signature(prior) != record["prior_signature"]
                or bank._event_signature(following) != record["next_signature"]
                or bank._sha(following) != record["next_row_sha256"]
                or json.loads(following["provenance"]).get("macos_semantic_reparse")
                != {
                    "policy": POLICY,
                    "sequence": index + 1,
                    "prior_row_sha256": bank._sha(prior),
                    "previous_digest": previous_digest,
                }
                or _identity(prior)[1] != record["native_identity_sha256"]
                or _identity(following)[1] != record["native_identity_sha256"]
                or record["prior_signature"] in signatures
                or record["next_signature"] in signatures | {record["prior_signature"]}
                or _reason(prior, following) != record["reason"]
                or record["reason"] not in _REASONS
                or not _same_event(prior, following)
                or not isinstance(record["parser_revision"], str)
                or re.fullmatch(
                    r"v[0-9]+\.[0-9]+\.[0-9]+-codex\.[0-9]+", record["parser_revision"]
                )
                is None
            ):
                raise ValueError(
                    "A native semantic repair revision chain is inconsistent."
                )
            source = connection.execute(
                "SELECT proof FROM sources WHERE id=?", (record["source_key"],)
            ).fetchone()
            if source is None:
                raise ValueError(
                    "A native semantic repair lost its authenticated source proof."
                )
            packet = json.loads(source[0])
            proof = json.loads(following["provenance"])
            native = proof["macos_recovery"]
            source_native = native
            if record["reason"] == "app_card_metadata_completion":
                source_native = proof.get("app_card_metadata_origin")
                if (
                    not isinstance(source_native, dict)
                    or set(source_native) != set(_METADATA_SOURCE_FIELDS)
                    or native != json.loads(prior["provenance"])["macos_recovery"]
                    or any(
                        source_native[field] != native[field] for field in _RAW_FIELDS
                    )
                ):
                    raise ValueError(
                        "App card enrichment lost its original receipt or exact metadata source."
                    )
            if (
                bank._sha(packet) != record["source_key"]
                or packet["account_id"] != source_native["account_id"]
                or packet["peer_id"] != source_native["peer_id"]
                or any(
                    packet["source"][key] != source_native[key]
                    for key in (
                        "source_id",
                        "snapshot_id",
                        "main_sha256",
                        "wal_sha256",
                        "database_relative_path",
                        "table",
                    )
                )
            ):
                raise ValueError(
                    "A repaired projection differs from its retained source proof."
                )
            previous_digest = record["digest"]
            signatures.add(record["prior_signature"])
        key = connection.execute(
            "SELECT signature FROM event_keys WHERE message_id=?", (identifier,)
        ).fetchone()
        if key is None or key[0] != bank._event_signature(latest):
            raise ValueError(
                "A repaired event key differs from its current projection."
            )
    rows = connection.execute(
        "SELECT o.* FROM occurrences o WHERE canonical_id IN (SELECT message_id FROM projection_revisions)"
    )
    fields = [item[0] for item in rows.description]
    for row in rows:
        occurrence = dict(zip(fields, row, strict=True))
        current_signature = connection.execute(
            "SELECT signature FROM event_keys WHERE message_id=?",
            (occurrence["canonical_id"],),
        ).fetchone()[0]
        if occurrence["signature"] != current_signature and not historical_occurrence(
            connection, occurrence
        ):
            raise ValueError(
                "A preserved occurrence has no sealed projection in its revision history."
            )


def historical_occurrence(connection, occurrence, *, schema="main"):
    """Accept an unchanged occurrence only through a sealed prior projection of its row."""
    bank = _bank()
    if not _has_table(connection, schema):
        return False
    source = connection.execute(
        f"SELECT proof FROM {schema}.sources WHERE id=?", (occurrence["source_id"],)
    ).fetchone()
    if source is None:
        return False
    packet = json.loads(source[0])
    message = json.loads(occurrence["proof"])
    if (
        message.get("content_sha256") != occurrence["content_sha256"]
        or message.get("local_id") != occurrence["local_id"]
        or message.get("server_id") != occurrence["server_id"]
        or packet.get("session_id") != occurrence["conversation_id"]
    ):
        return False
    bank._validate_packet({**packet, "messages": [message]}, (packet["account_id"],))
    columns = [
        item[1] for item in connection.execute(f"PRAGMA {schema}.table_info(messages)")
    ]
    projected = dict(zip(columns, bank._row(packet, message), strict=True))
    proof = json.loads(projected["provenance"])
    metadata = _card_metadata(message.get("resource", {}))
    if metadata is not None:
        proof["app_card_metadata"] = metadata
        projected["provenance"] = bank._json(proof)
    rows = connection.execute(
        f"SELECT prior_row FROM {schema}.projection_revisions WHERE message_id=? AND prior_signature=?",
        (occurrence["canonical_id"], occurrence["signature"]),
    )
    for record in rows:
        prior = json.loads(record[0])
        raw, _digest = _identity(prior)
        if (
            packet["account_id"] == raw["account_id"]
            and packet["peer_id"] == raw["peer_id"]
            and message["server_id"] == raw["server_id"]
            and bank._event_signature(projected)
            == bank._event_signature(prior)
            == occurrence["signature"]
        ):
            return True
    return False
