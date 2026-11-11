"""Reviewed quote reconciliation preserves prior hashes and upstream precedence.

Code version: v1.0.0-codex.0
"""

import json
import sqlite3

import pytest

from app.core import wechat_secondary_completions as completions
from app.core.wechat_archive import _file_identity
from tests import test_wechat_secondary_completions as fixtures
from tests import test_wechat_quote_enrichment_replay as quote_replay_cases

bank_archive = fixtures.bank_archive
completion_fixture = fixtures.completion_fixture
mac_archive = fixtures.mac_archive
source_binding = fixtures.source_binding
witness = fixtures.witness

reviewed_replay_case = quote_replay_cases.replay_case


def _materialize_reviewed_enrichment(case, tmp_path):
    import gzip

    fixture, derived, decision, projected = case
    source = tmp_path / "reviewed-quote-enrichment.sqlite3"
    destination = sqlite3.connect(source)
    try:
        derived.backup(destination)
    finally:
        destination.close()
    with sqlite3.connect(source) as output:
        output.execute(
            "INSERT INTO ambiguities VALUES(?,?,?)",
            (
                "f" * 64,
                fixture["conversation"]["id"],
                "Multiple physical native witnesses match the original event.",
            ),
        )
        output.executemany(
            "INSERT INTO archive_info VALUES(?,?)",
            [
                ("schema_version", "1"),
                ("version", completions.VERSION),
                ("staging_state", "complete"),
                (
                    "input_identities",
                    completions._json(completions._pins(fixture["history"])),
                ),
                ("primary_catalog_sha256", completions._file_sha(fixture["catalog"])),
                ("expected_existing_catalog", "null"),
                ("base_candidate", "null"),
                (
                    "report",
                    completions._json(
                        {"counts": {}, "rejected": {"ambiguous_original_events": 2}}
                    ),
                ),
            ],
        )
    audit = tmp_path / "reviewed-quote-decisions.jsonl.gz"
    with gzip.open(audit, "wt") as stream:
        stream.write(completions._json(decision) + "\n")
    return (
        source,
        audit,
        completions._file_sha(source),
        completions._file_sha(audit),
        projected,
    )


@pytest.mark.parametrize("upstream_wins", [False, True])
def test_stream_reconciliation_restores_only_reviewed_exact_prior_and_respects_upstream(
    reviewed_replay_case, tmp_path, upstream_wins
):
    fixture = reviewed_replay_case[0]
    if upstream_wins:
        primary = {**fixture["quote"], "text": "Verified upstream quote body"}
        with sqlite3.connect(fixture["history"]) as history:
            history.execute(
                "INSERT INTO messages VALUES("
                + ",".join("?" for _ in completions.COLUMNS)
                + ")",
                tuple(primary[column] for column in completions.COLUMNS),
            )
            history.execute("UPDATE conversations SET message_count=1")
        with sqlite3.connect(fixture["catalog"]) as catalog:
            catalog.execute(
                "UPDATE archive_info SET value=? WHERE key='history_file_identity'",
                (json.dumps(_file_identity(fixture["history"])),),
            )
    source, audit, reviewed_sha, audit_sha, projected = (
        _materialize_reviewed_enrichment(reviewed_replay_case, tmp_path)
    )
    source_bytes = source.read_bytes()
    report = completions.compact_secondary_message_completions(
        fixture["root"],
        source,
        tmp_path / "reviewed-restoration-output.sqlite3",
        candidate_sha256=reviewed_sha,
        stream_compaction=True,
        quote_enrichment_audit_path=audit,
        quote_enrichment_audit_sha256=audit_sha,
        quote_enrichment_source_sha256=reviewed_sha,
    )
    counts = report["quote_enrichment_reconciliation"]["counts"]
    assert counts["verified_prior_projections"] == 1
    assert counts["remaining_quarantines"] == 1
    assert counts.get("restored_prior_active", 0) == int(not upstream_wins)
    assert counts.get("pruned_prior_upstream", 0) == int(upstream_wins)
    assert report["stored_before_counts"] == {}
    assert report["before_counts"] == {"native_quote": 1}
    with completions._readonly(report["candidate"]) as output:
        assert output.execute("SELECT id FROM ambiguities").fetchall()[0][0] == "f" * 64
        assert (
            output.execute(
                "SELECT COUNT(*) FROM incremental_resource_conflicts"
            ).fetchone()[0]
            == 1
        )
        if not upstream_wins:
            assert (
                completions._decoded_message(
                    output.execute("SELECT * FROM messages").fetchone()
                )
                == projected
            )
            assert (
                output.execute("SELECT projected_sha256 FROM entries").fetchone()[0]
                == reviewed_replay_case[2]["prior_row_sha256"]
            )
        else:
            assert output.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
    assert source.read_bytes() == source_bytes
    assert report["validation_cache_seeded"]


def test_stream_reconciliation_requires_reviewed_manifest_hash_and_generation(
    reviewed_replay_case, tmp_path
):
    fixture = reviewed_replay_case[0]
    source, audit, reviewed_sha, audit_sha, _ = _materialize_reviewed_enrichment(
        reviewed_replay_case, tmp_path
    )
    target = tmp_path / "wrong-review.sqlite3"
    for declared_audit, declared_source, message in [
        ("0" * 64, reviewed_sha, "manifest changed"),
        (audit_sha, "0" * 64, "another reviewed generation"),
    ]:
        with pytest.raises(ValueError, match=message):
            completions.compact_secondary_message_completions(
                fixture["root"],
                source,
                target,
                candidate_sha256=reviewed_sha,
                stream_compaction=True,
                quote_enrichment_audit_path=audit,
                quote_enrichment_audit_sha256=declared_audit,
                quote_enrichment_source_sha256=declared_source,
            )
        assert not target.exists()


def test_stream_reconciliation_forged_prior_hash_is_not_restored_or_publishable(
    reviewed_replay_case, tmp_path
):
    import gzip

    fixture = reviewed_replay_case[0]
    source, audit, reviewed_sha, _audit_sha, _ = _materialize_reviewed_enrichment(
        reviewed_replay_case, tmp_path
    )
    decision = {**reviewed_replay_case[2], "prior_row_sha256": "0" * 64}
    with gzip.open(audit, "wt") as stream:
        stream.write(completions._json(decision) + "\n")
    target = tmp_path / "forged-restoration.sqlite3"
    with pytest.raises(ValueError):
        completions.compact_secondary_message_completions(
            fixture["root"],
            source,
            target,
            candidate_sha256=reviewed_sha,
            stream_compaction=True,
            quote_enrichment_audit_path=audit,
            quote_enrichment_audit_sha256=completions._file_sha(audit),
            quote_enrichment_source_sha256=reviewed_sha,
        )
    with completions._readonly(target) as partial:
        assert (
            partial.execute(
                "SELECT value FROM archive_info WHERE key='staging_state'"
            ).fetchone()[0]
            == "in_progress"
        )
        assert partial.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    with pytest.raises(ValueError, match="not completed staging"):
        completions.publish_secondary_message_completions(
            fixture["root"], target, candidate_sha256=completions._file_sha(target)
        )


def test_stream_reconciliation_accepts_only_the_exact_reviewed_one_hop_parent(
    reviewed_replay_case, tmp_path
):
    fixture = reviewed_replay_case[0]
    source, audit, reviewed_sha, audit_sha, _ = _materialize_reviewed_enrichment(
        reviewed_replay_case, tmp_path
    )
    extended = completions.stage_secondary_message_completions(
        fixture["root"],
        tmp_path / "guarded-source.sqlite3",
        (),
        notice_plans=(),
        base_candidate_path=source,
        base_candidate_sha256=reviewed_sha,
    )
    report = completions.compact_secondary_message_completions(
        fixture["root"],
        extended["candidate"],
        tmp_path / "lineage-restoration.sqlite3",
        candidate_sha256=extended["candidate_sha256"],
        stream_compaction=True,
        quote_enrichment_audit_path=audit,
        quote_enrichment_audit_sha256=audit_sha,
        quote_enrichment_source_sha256=reviewed_sha,
    )
    assert (
        report["quote_enrichment_reconciliation"]["counts"][
            "verified_prior_projections"
        ]
        == 1
    )
    assert (
        report["quote_enrichment_reconciliation"]["counts"]["remaining_quarantines"]
        == 1
    )


def test_stream_reconciliation_audit_drift_cannot_complete_output(
    reviewed_replay_case, tmp_path
):
    fixture = reviewed_replay_case[0]
    source, audit, reviewed_sha, audit_sha, _ = _materialize_reviewed_enrichment(
        reviewed_replay_case, tmp_path
    )
    target = tmp_path / "audit-drift-output.sqlite3"

    def drift(value):
        if (
            value["phase"] == "replaying_reviewed_quote_enrichments"
            and value.get("verified_prior_projections") == 1
        ):
            audit.write_bytes(b"A different review appeared during the final callback.")

    with pytest.raises(ValueError, match="compare-and-swap"):
        completions.compact_secondary_message_completions(
            fixture["root"],
            source,
            target,
            candidate_sha256=reviewed_sha,
            stream_compaction=True,
            quote_enrichment_audit_path=audit,
            quote_enrichment_audit_sha256=audit_sha,
            quote_enrichment_source_sha256=reviewed_sha,
            progress=drift,
        )
    with completions._readonly(target) as retained:
        assert (
            retained.execute(
                "SELECT value FROM archive_info WHERE key='staging_state'"
            ).fetchone()[0]
            == "in_progress"
        )
