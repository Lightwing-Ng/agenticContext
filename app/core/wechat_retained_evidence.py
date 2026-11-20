"""Validate retained export references without promoting native identity.

Code version: v1.0.3-codex.0
"""

import hashlib
import json

from .wechat_archive import _identifier


def _retained_native_observation(connection, evidence, native_source_id):
    """Exclude only proved retired export cross-references from native matching.

    Retirement moves message_id while preserving the original export envelope.
    It does not make an export owner, sender, clock, or cross-reference a native
    identity assertion. A return value of None retains that secondary evidence.
    """
    from .wechat_reconcile import _json

    observation = json.loads(evidence["observation"])
    if not isinstance(observation, dict):
        raise ValueError("Retained message evidence must be an observation object.")
    if evidence["source_id"] == native_source_id:
        return observation

    def refuse():
        raise ValueError(
            "Retained message evidence crosses its source account without "
            "a proved export retirement."
        )

    cross = observation.get("cross_reference")
    if (
        set(observation) != {"kind", "content_sha256", "cross_reference"}
        or observation["kind"] != "export_cross_reference"
        or not isinstance(cross, dict)
        or cross.get("global_message_identity_asserted") is not False
        or cross.get("time_zone_inferred") is not False
        or evidence["match_kind"] != "cross_reference"
        or not isinstance(evidence["extraction_record_id"], str)
        or not evidence["extraction_record_id"]
        or evidence["id"] != _identifier(
            evidence["source_id"], evidence["extraction_record_id"],
            _json(observation),
        )
        or not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='derived_message_aliases'"
        ).fetchone()
        or not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='export_imports'"
        ).fetchone()
    ):
        refuse()
    old_id = _identifier(evidence["source_id"], evidence["extraction_record_id"])
    alias = connection.execute(
        "SELECT * FROM derived_message_aliases WHERE old_id=?", (old_id,)
    ).fetchone()
    if (
        alias is None or alias["native_id"] != evidence["message_id"]
        or alias["source_id"] != evidence["source_id"]
    ):
        refuse()
    metadata, proof = json.loads(alias["metadata"]), json.loads(alias["proof"])
    if not isinstance(metadata, dict) or not isinstance(proof, dict):
        refuse()
    row, provenance = metadata.get("row", {}), metadata.get("provenance", {})
    if not isinstance(row, dict) or not isinstance(provenance, dict):
        refuse()
    snapshot_id = provenance.get("source_snapshot")
    if type(snapshot_id) is not int:
        refuse()
    snapshot = connection.execute(
        "SELECT * FROM source_snapshots WHERE id=?", (snapshot_id,)
    ).fetchone()
    partition = connection.execute(
        "SELECT * FROM partitions WHERE id=?", (evidence["source_id"],)
    ).fetchone()
    imported = connection.execute(
        "SELECT * FROM export_imports WHERE source_id=?", (evidence["source_id"],)
    ).fetchone()
    if snapshot is None or partition is None or imported is None:
        refuse()
    from .wechat_export_merge import _source_metadata
    from .wechat_derived_retirement import _context, _native, _wall

    source_metadata = _source_metadata(json.loads(snapshot["metadata"]))
    export_owner = (
        source_metadata["source_account_id"]
        if source_metadata["owner_verified"] else None
    )
    export_account = (
        "account:" + export_owner if export_owner
        else "source:" + evidence["source_id"]
    )
    if (
        partition["source_label"] not in {"HTML export", "Excel export"}
        or not partition["id"].startswith(("html-", "xlsx-"))
        or metadata.get("source") != dict(partition)
        or partition["account_id"] != export_owner
        or partition["account_key"] != export_account
        or snapshot["source_id"] != evidence["source_id"]
        or snapshot["snapshot_id"] != evidence["snapshot_id"]
        or imported["snapshot_id"] != evidence["snapshot_id"]
        or imported["source_sha256"] != source_metadata.get("source_sha256")
        or imported["metadata"] != snapshot["metadata"]
        or _json(source_metadata) != _json(json.loads(snapshot["metadata"]))
        or source_metadata.get("source_id") != partition["id"]
        or source_metadata.get("source_label") != partition["source_label"]
        or row.get("id") != old_id
        or provenance.get("record_id") != evidence["extraction_record_id"]
    ):
        refuse()
    native = connection.execute(
        "SELECT * FROM messages WHERE id=?", (evidence["message_id"],)
    ).fetchone()
    if native is None:
        refuse()
    conversation, native_partition = _context(connection, native)
    native_proof = _native(connection, native, conversation, native_partition)
    body_sha256 = hashlib.sha256(native["text"].encode()).hexdigest()
    if (
        native_partition["id"] != native_source_id
        or proof.get("method") != "exact_group_author_body_clock_order_v1"
        or type(proof.get("anchor_count")) is not int or proof["anchor_count"] < 3
        or proof.get("derived_source_id") != evidence["source_id"]
        or proof.get("native_source_id") != native_source_id
        or proof.get("native_conversation_id") != native["conversation_id"]
        or proof.get("derived_conversation_id") != row.get("conversation_id")
        or proof.get("native_source_snapshot_id") != native_proof.get("source_snapshot")
        or proof.get("native_server_id") != str(native_proof.get("server_id"))
        or proof.get("room_id") != conversation["peer_id"]
        or proof.get("wall_clock") != _wall(native["timestamp_value"])
        or _wall(row.get("timestamp_value")) != proof["wall_clock"]
        or observation["content_sha256"] != body_sha256
        or metadata.get("body_sha256") != body_sha256
        or proof.get("body_sha256") != body_sha256
    ):
        refuse()
    return None
