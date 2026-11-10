"""Restore only exact reviewed prior quote projections in a fresh streaming output.

Code version: v1.0.0-codex.0
Every decision is replayed; an independent upstream-active check still decides
retention. Current source candidates and historical quarantine ledgers stay intact.
"""

from collections import Counter
import gzip
import hashlib
import json
import os
from pathlib import Path

from .owner_only_files import ensure_owner_only
from .wechat_completion_stream_copy import encode_stream_values
from .wechat_quote_enrichment_replay import replay_quote_enrichment

CODE_VERSION = "v1.0.0-codex.0"


def prepare_reviewed_audit(
    source_info, candidate_sha256, path, expected_sha256, reviewed_source_sha256
):
    from . import wechat_secondary_completions as common

    path = Path(path).absolute()
    identity = common._identity(path)
    if common._file_sha(path) != expected_sha256 or common._identity(path) != identity:
        raise ValueError("The reviewed quote-enrichment decision manifest changed.")
    parent = json.loads(source_info.get("base_candidate", "null"))
    lineage = parent.get("sha256") if isinstance(parent, dict) else None
    if reviewed_source_sha256 not in {candidate_sha256, lineage}:
        raise ValueError(
            "The quote-enrichment audit belongs to another reviewed generation."
        )
    return {
        "path": str(path),
        "sha256": expected_sha256,
        "identity": identity,
        "reviewed_source_sha256": reviewed_source_sha256,
    }


def _insert_prior(output, projection, codec_counts, common):
    original, conversation = projection["original"], projection["conversation"]
    message = {
        **projection["message"],
        "time_sort": float(projection["message"]["time_sort"]),
    }
    header, native = common._header_event(projection["event"])
    source_key = common._sha(header)
    witness_key = common._sha(
        [source_key, native["local_id"], native["content_sha256"]]
    )
    if output.execute(
        "SELECT sha256 FROM sources WHERE id=?", (source_key,)
    ).fetchone() != (source_key,):
        raise ValueError("Restored quotes lost an audit-retained source header.")
    if output.execute(
        "SELECT sha256 FROM witnesses WHERE id=?", (witness_key,)
    ).fetchone() != (common._sha(native),):
        raise ValueError("Restored quotes lost an audit-retained native witness.")
    if common._sha(message) != projection["prior_row_sha256"]:
        raise ValueError("A restored quote changed its exact prior projected hash.")
    columns = [row[1] for row in output.execute("PRAGMA table_info(entries)")]
    entry = (
        message["id"],
        message["conversation_id"],
        "replace",
        "native_quote",
        common._json(original),
        common._sha(original),
        common._json(conversation),
        source_key,
        witness_key,
        None,
        projection["prior_row_sha256"],
    )
    output.execute(
        "INSERT INTO messages VALUES(" + ",".join("?" for _ in common.COLUMNS) + ")",
        encode_stream_values(
            [message[column] for column in common.COLUMNS],
            common.COLUMNS,
            {"provenance"},
            codec_counts,
        ),
    )
    output.execute(
        "INSERT INTO entries VALUES(" + ",".join("?" for _ in columns) + ")",
        encode_stream_values(
            entry, columns, {"original_row", "conversation_row", "plan"}, codec_counts
        ),
    )
    codec_counts["processed_json_rows"] += 2
    return {
        "id": message["id"],
        "conversation_id": message["conversation_id"],
        "operation": "replace",
        "strategy": "native_quote",
        "original_sha256": common._sha(original),
        "projected_sha256": projection["prior_row_sha256"],
        "source_key": source_key,
        "witness_key": witness_key,
    }


def reconcile_reviewed_quotes(
    derived,
    history,
    catalog,
    output,
    *,
    audit,
    manifest_path,
    upstream,
    record_pruned,
    codec_stats,
    progress,
):
    from . import wechat_secondary_completions as common

    manifest_path = Path(manifest_path)
    descriptor = os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    digest = hashlib.sha256()
    byte_count = 0
    seen_ids, seen_rows, source_cache = set(), set(), {}
    snapshots = common._snapshot_resolver(history, catalog)
    counts, codec_counts = Counter(), Counter(codec_stats)
    with os.fdopen(descriptor, "wb") as raw:
        ensure_owner_only(manifest_path)
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as decisions:
            with gzip.open(audit["path"], "rt", encoding="utf-8") as reviewed:
                for line in reviewed:
                    if len(line.encode("utf-8")) > 16 * 1024:
                        raise ValueError(
                            "A reviewed quote decision exceeds its record budget."
                        )
                    decision = json.loads(line)
                    if not isinstance(decision, dict):
                        raise ValueError("A reviewed quote decision must be an object.")
                    projection = replay_quote_enrichment(
                        derived,
                        history,
                        catalog,
                        decision,
                        snapshots=snapshots,
                        source_cache=source_cache,
                    )
                    if (
                        decision["ledger_id"] in seen_ids
                        or decision["message_id"] in seen_rows
                    ):
                        raise ValueError(
                            "A reviewed quote manifest repeats a ledger or message."
                        )
                    seen_ids.add(decision["ledger_id"])
                    seen_rows.add(decision["message_id"])
                    original = projection["original"]
                    active = history.execute(
                        "SELECT "
                        + common._active_predicate(upstream)
                        + " FROM (SELECT ? AS id,? AS conversation_id,'replace' AS operation,? AS original_row,NULL AS plan) e",
                        (
                            original["id"],
                            original["conversation_id"],
                            common._json(original),
                        ),
                    ).fetchone()[0]
                    row = {
                        "id": original["id"],
                        "conversation_id": original["conversation_id"],
                        "operation": "replace",
                        "strategy": "native_quote",
                        "original_sha256": common._sha(original),
                        "projected_sha256": projection["prior_row_sha256"],
                        "source_key": decision["prior_source_key"],
                        "witness_key": decision["prior_witness_key"],
                    }
                    if active:
                        _insert_prior(output, projection, codec_counts, common)
                        counts["restored_prior_active"] += 1
                    else:
                        record_pruned(
                            {
                                **row,
                                "reason": "upstream_precedence",
                                "regenerated_from_reviewed_ledger": decision[
                                    "ledger_id"
                                ],
                            }
                        )
                        counts["pruned_prior_upstream"] += 1
                    removed = output.execute(
                        "DELETE FROM ambiguities WHERE id=? AND reason=?",
                        (
                            original["id"],
                            "New authenticated native quote body conflicts with a completed resource.",
                        ),
                    ).rowcount
                    if removed != 1:
                        raise ValueError(
                            "Only the exactly replayed historical quote quarantine may be resolved."
                        )
                    counts["verified_prior_projections"] += 1
                    record = {
                        **row,
                        "ledger_id": decision["ledger_id"],
                        "observed_source_key": decision["source_key"],
                        "observed_witness_key": decision["witness_key"],
                        "decision": "restored_prior_active"
                        if active
                        else "verified_prior_upstream_precedence",
                    }
                    payload = (common._json(record) + "\n").encode()
                    digest.update(payload)
                    byte_count += len(payload)
                    decisions.write(payload)
                    if counts["verified_prior_projections"] % 256 == 0:
                        output.commit()
                    if counts["verified_prior_projections"] % 1000 == 0:
                        progress(**dict(counts))
        raw.flush()
        os.fsync(raw.fileno())
    output.commit()
    observed, observed_bytes = hashlib.sha256(), 0
    with gzip.open(manifest_path, "rb") as stream:
        while block := stream.read(1024**2):
            observed.update(block)
            observed_bytes += len(block)
    if observed.hexdigest() != digest.hexdigest() or observed_bytes != byte_count:
        raise ValueError(
            "The restored-quote decision manifest failed round-trip verification."
        )
    if common._identity(Path(audit["path"])) != audit["identity"]:
        raise ValueError("The reviewed quote manifest changed during replay.")
    counts["remaining_quarantines"] = output.execute(
        "SELECT COUNT(*) FROM ambiguities"
    ).fetchone()[0]
    progress(**dict(counts))
    return {
        "version": CODE_VERSION,
        "audit_input": audit,
        "counts": dict(counts),
        "manifest": str(manifest_path),
        "manifest_sha256": common._file_sha(manifest_path),
        "manifest_uncompressed_sha256": digest.hexdigest(),
        "manifest_uncompressed_bytes": byte_count,
        "json_cell_transport": dict(codec_counts),
    }
