"""Retry only eligible failures linked through a verified, immutable global merge.

Code version: v1.1.0-codex.0
"""

from copy import deepcopy
import json
import sqlite3
from types import SimpleNamespace

import pytest

from scripts import refresh_wechat_globally as runner
from tests import test_wechat_global_refresh_runner as fixtures


def _chain(monkeypatch, tmp_path):
    inventory = fixtures._retry_inventory(tmp_path)
    workstations = tmp_path / "workstations"
    workstations.mkdir(mode=0o700)
    workstation = workstations / "repair"
    workstation.mkdir(mode=0o700)
    origin_dir = workstation / "run-original"
    merge_dir = workstation / "merge-verified"
    origin_dir.mkdir(mode=0o700)
    merge_dir.mkdir(mode=0o700)
    incoming = origin_dir / "candidate.sqlite3"
    payload = "a" * 64
    with sqlite3.connect(incoming) as connection:
        connection.execute("CREATE TABLE archive_info(key TEXT PRIMARY KEY,value TEXT)")
        connection.execute("INSERT INTO archive_info VALUES('payload_sha256',?)", (payload,))
        connection.executescript(
            "CREATE TABLE sources(id TEXT PRIMARY KEY,account_id TEXT,peer_id TEXT,"
            "source_id TEXT,snapshot_id TEXT,proof TEXT);"
            "CREATE TABLE gaps(id TEXT PRIMARY KEY,conversation_id TEXT,source_id TEXT,"
            "local_id INTEGER,server_id TEXT,reason TEXT,content_sha256 TEXT);"
        )
    incoming.chmod(0o600)
    origin_path, merge_path = origin_dir / "result.json", merge_dir / "result.json"
    failures = [
        fixtures._scope_failure(inventory),
        fixtures._scope_failure(
            inventory, scope_index=1, reference_index=1,
            reason="scope_account_binding_unavailable",
        ),
        fixtures._scope_failure(
            inventory, scope_index=2, reference_index=2,
            reason="scope_projection_failed",
        ),
    ]
    origin = {
        "version": runner.VERSION, "status": "failed_before_publication",
        "failed_phase": "staging", "published": False,
        "local_store": str(inventory.history_path.parents[2]),
        "input_identities": {"history.sqlite3": deepcopy(inventory.history_identity)},
        "preparation_progress": {
            "selected_sources": 3, "completed_sources": 2, "skipped_sources": 1,
        },
    }
    identity = runner.bank._identity(incoming)
    merged = {
        "version": "v1.0.0-codex.0", "status": "verified", "published": True,
        "local_store": origin["local_store"],
        "input_identities": deepcopy(origin["input_identities"]),
        "global_report": str(origin_path), "incoming": str(incoming),
        "incoming_summary": {"identity": identity, "payload_sha256": payload},
        "staging_receipt": {
            "merge": {"incoming_identity": deepcopy(identity), "incoming_payload_sha256": payload},
        },
    }
    runner.write_json(origin_path, origin)
    runner.write_json(merge_path, merged)
    exclusions = origin_dir / "exclusions.jsonl"
    exclusions.write_text("".join(json.dumps(value) + "\n" for value in failures))
    exclusions.chmod(0o600)
    monkeypatch.setattr(runner, "WORKSTATIONS", workstations)
    return SimpleNamespace(
        inventory=inventory, workstation=workstation, origin=origin, merged=merged,
        origin_path=origin_path, merge_path=merge_path, incoming=incoming,
        exclusions=exclusions, failures=failures,
    )


def test_verified_merge_chain_selects_projection_failures_with_exact_scopes(
    monkeypatch, tmp_path
):
    operation = _chain(monkeypatch, tmp_path)
    # An unrelated row rejection remains outside the retry set; repeated coordinates
    # do not cause a selected source or scope to be processed twice.
    with operation.exclusions.open("a") as stream:
        stream.write(json.dumps(operation.failures[-1]) + "\n")
        stream.write(json.dumps({**operation.failures[-1], "reason": "row_missing_authenticated_sender"}) + "\n")
    assert runner.retry_scope_sources(operation.merge_path, operation.inventory) == frozenset(
        (value["account_id"], value["database_relative_path"], value["session_id"])
        for value in operation.failures
    )


@pytest.mark.parametrize("defect", ("not_verified", "not_published", "wrong_origin_phase",
                                    "origin_published", "origin_not_failed"))
def test_retry_chain_requires_successful_merge_and_exact_failed_staging_boundary(
    monkeypatch, tmp_path, defect
):
    operation = _chain(monkeypatch, tmp_path)
    if defect == "not_verified":
        operation.merged["status"] = "published"
    elif defect == "not_published":
        operation.merged["published"] = False
    elif defect == "wrong_origin_phase":
        operation.origin["failed_phase"] = "preparing_sources"
    elif defect == "origin_published":
        operation.origin["published"] = True
    else:
        operation.origin["status"] = "verified"
    runner.write_json(operation.origin_path, operation.origin)
    runner.write_json(operation.merge_path, operation.merged)
    with pytest.raises(runner.GlobalRepairError):
        runner.retry_scope_sources(operation.merge_path, operation.inventory)


@pytest.mark.parametrize(
    "progress",
    [
        {},
        {"selected_sources": 3, "completed_sources": 1, "skipped_sources": 1},
        {"selected_sources": 3, "completed_sources": 3, "skipped_sources": 1},
        {"selected_sources": 0, "completed_sources": 0, "skipped_sources": 0},
        {"selected_sources": 3, "completed_sources": True, "skipped_sources": 2},
        {"selected_sources": 3, "completed_sources": 4, "skipped_sources": -1},
    ],
)
def test_retry_chain_requires_every_selected_physical_source_to_finish(
    monkeypatch, tmp_path, progress
):
    operation = _chain(monkeypatch, tmp_path)
    operation.origin["preparation_progress"] = progress
    runner.write_json(operation.origin_path, operation.origin)
    with pytest.raises(runner.GlobalRepairError, match="scan_was_incomplete"):
        runner.retry_scope_sources(operation.merge_path, operation.inventory)


@pytest.mark.parametrize("defect", ("incoming_path", "summary_identity", "replay_identity",
                                    "replay_payload", "current_identity", "current_payload"))
def test_retry_chain_rejects_changed_or_disconnected_incoming_candidate(
    monkeypatch, tmp_path, defect
):
    operation = _chain(monkeypatch, tmp_path)
    if defect == "incoming_path":
        operation.merged["incoming"] = str(operation.incoming.with_name("other.sqlite3"))
    elif defect == "summary_identity":
        operation.merged["incoming_summary"]["identity"]["size"] += 1
    elif defect == "replay_identity":
        operation.merged["staging_receipt"]["merge"]["incoming_identity"]["ino"] += 1
    elif defect == "replay_payload":
        operation.merged["staging_receipt"]["merge"]["incoming_payload_sha256"] = "b" * 64
    elif defect == "current_identity":
        operation.incoming.touch()
    else:
        # Pin the candidate's current identity in both reports while leaving their
        # payload claim stale, so the read-only archive_info check must reject it.
        with sqlite3.connect(operation.incoming) as connection:
            connection.execute("UPDATE archive_info SET value=?", ("b" * 64,))
        identity = runner.bank._identity(operation.incoming)
        operation.merged["incoming_summary"]["identity"] = identity
        operation.merged["staging_receipt"]["merge"]["incoming_identity"] = deepcopy(identity)
    runner.write_json(operation.merge_path, operation.merged)
    with pytest.raises(runner.GlobalRepairError):
        runner.retry_scope_sources(operation.merge_path, operation.inventory)


@pytest.mark.parametrize("defect", ("merge_store", "origin_store", "merge_history",
                                    "both_stale_history", "other_workstation"))
def test_retry_chain_cannot_cross_archive_history_or_workstation_boundaries(
    monkeypatch, tmp_path, defect
):
    operation = _chain(monkeypatch, tmp_path)
    if defect == "merge_store":
        operation.merged["local_store"] = str(tmp_path / "foreign-store")
    elif defect == "origin_store":
        operation.origin["local_store"] = str(tmp_path / "foreign-store")
    elif defect == "merge_history":
        operation.merged["input_identities"]["history.sqlite3"]["ino"] += 1
    elif defect == "both_stale_history":
        for value in (operation.origin, operation.merged):
            value["input_identities"]["history.sqlite3"]["ino"] += 1
    else:
        other = operation.workstation.parent / "foreign-workstation"
        other.mkdir(mode=0o700)
        moved = other / "run-original"
        operation.origin_path.parent.rename(moved)
        operation.origin_path = moved / "result.json"
        operation.merged["global_report"] = str(operation.origin_path)
        operation.merged["incoming"] = str(moved / "candidate.sqlite3")
    runner.write_json(operation.origin_path, operation.origin)
    runner.write_json(operation.merge_path, operation.merged)
    with pytest.raises(runner.GlobalRepairError):
        runner.retry_scope_sources(operation.merge_path, operation.inventory)


@pytest.mark.parametrize("target", ("merge_report", "origin_report", "incoming"))
def test_retry_chain_rejects_symlink_evidence_paths(monkeypatch, tmp_path, target):
    operation = _chain(monkeypatch, tmp_path)
    path = {
        "merge_report": operation.merge_path,
        "origin_report": operation.origin_path,
        "incoming": operation.incoming,
    }[target]
    preserved = path.with_name("preserved-" + path.name)
    path.rename(preserved)
    path.symlink_to(preserved)
    with pytest.raises(runner.GlobalRepairError):
        runner.retry_scope_sources(operation.merge_path, operation.inventory)


def test_validated_merge_does_not_authorize_a_foreign_projection_scope(
    monkeypatch, tmp_path
):
    operation = _chain(monkeypatch, tmp_path)
    foreign = {**operation.failures[-1], "account_id": operation.inventory.account_ids[0]}
    operation.exclusions.write_text(json.dumps(foreign) + "\n")
    with pytest.raises(runner.GlobalRepairError, match="coordinates_are_no_longer_eligible"):
        runner.retry_scope_sources(operation.merge_path, operation.inventory)


def test_verified_direct_report_cannot_enable_projection_failure_retries(
    monkeypatch, tmp_path
):
    operation = _chain(monkeypatch, tmp_path)
    operation.origin.update(status="verified", published=True)
    runner.write_json(operation.origin_path, operation.origin)
    operation.exclusions.write_text(json.dumps(operation.failures[-1]) + "\n")
    with pytest.raises(runner.GlobalRepairError, match="no_selected_scope_failures"):
        runner.retry_scope_sources(operation.origin_path, operation.inventory)


@pytest.mark.parametrize(
    "target",
    ("merge_report", "origin_report", "merge_run", "origin_run", "workstation", "incoming"),
)
def test_retry_chain_requires_private_reports_candidate_and_workstation_paths(
    monkeypatch, tmp_path, target
):
    operation = _chain(monkeypatch, tmp_path)
    paths = {
        "merge_report": operation.merge_path, "origin_report": operation.origin_path,
        "merge_run": operation.merge_path.parent, "origin_run": operation.origin_path.parent,
        "workstation": operation.workstation, "incoming": operation.incoming,
    }
    path = paths[target]
    path.chmod(0o755 if path.is_dir() else 0o644)
    if target == "incoming":
        # Re-pin identity after the permission change so file protection itself,
        # rather than an incidental stale ctime, must cause the rejection.
        identity = runner.bank._identity(path)
        operation.merged["incoming_summary"]["identity"] = identity
        operation.merged["staging_receipt"]["merge"]["incoming_identity"] = deepcopy(identity)
        runner.write_json(operation.merge_path, operation.merged)
    with pytest.raises(runner.GlobalRepairError):
        runner.retry_scope_sources(operation.merge_path, operation.inventory)


@pytest.mark.parametrize("target", ("merge_report", "origin_report", "incoming"))
def test_retry_chain_requires_unaliased_evidence_inodes(monkeypatch, tmp_path, target):
    operation = _chain(monkeypatch, tmp_path)
    path = {
        "merge_report": operation.merge_path,
        "origin_report": operation.origin_path,
        "incoming": operation.incoming,
    }[target]
    path.with_name("alias-" + path.name).hardlink_to(path)
    if target == "incoming":
        identity = runner.bank._identity(path)
        operation.merged["incoming_summary"]["identity"] = identity
        operation.merged["staging_receipt"]["merge"]["incoming_identity"] = deepcopy(identity)
        runner.write_json(operation.merge_path, operation.merged)
    with pytest.raises(runner.GlobalRepairError):
        runner.retry_scope_sources(operation.merge_path, operation.inventory)


def _repin_incoming(operation):
    identity = runner.bank._identity(operation.incoming)
    operation.merged["incoming_summary"]["identity"] = identity
    operation.merged["staging_receipt"]["merge"]["incoming_identity"] = deepcopy(identity)
    runner.write_json(operation.merge_path, operation.merged)


def _conflict_source(operation, *, scope_index=0, reference_index=1,
                     relative=None, defect=None, rows=1, reason="native_or_projection_conflict",
                     snapshot="sealed-snapshot"):
    scope = operation.inventory.scopes[scope_index]
    reference = operation.inventory.references[reference_index]
    proof = {
        "account_id": scope.account_id, "session_id": scope.session_id,
        "peer_id": scope.peer_id,
        "source": {
            "database_relative_path": relative or reference.relative_path,
            "source_id": "synthetic-conflict-source", "snapshot_id": snapshot,
        },
    }
    owner, peer, session = scope.account_id, scope.peer_id, scope.session_id
    if defect == "proof_owner":
        proof["account_id"] = "wxid_foreign_synthetic_owner"
    elif defect == "source_owner":
        owner = "wxid_foreign_synthetic_owner"
    elif defect == "proof_session":
        proof["session_id"] = "wechat:" + "d" * 64
    elif defect == "gap_session":
        session = "wechat:" + "d" * 64
    elif defect == "proof_peer":
        proof["peer_id"] = "wxid_foreign_synthetic_peer"
    elif defect == "source_peer":
        peer = "wxid_foreign_synthetic_peer"
    encoded = json.dumps(proof, sort_keys=True, separators=(",", ":"))
    source_key = runner.bank._sha(proof)
    if defect == "source_key":
        source_key = "f" * 64
    with sqlite3.connect(operation.incoming) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO sources VALUES(?,?,?,?,?,?)",
            (source_key, owner, peer, "synthetic-conflict-source", snapshot, encoded),
        )
        for index in range(rows):
            connection.execute(
                "INSERT INTO gaps VALUES(?,?,?,?,?,?,?)",
                (
                    runner.bank._sha([source_key, reason, index]), session,
                    "missing-source-key" if defect == "missing_source" else source_key,
                    index + 1, str(index + 10), reason, "c" * 64,
                ),
            )
    _repin_incoming(operation)
    return (scope.account_id, relative or reference.relative_path, scope.session_id), encoded


def _failure_pairs(operation):
    return {
        (row["account_id"], row["database_relative_path"], row["session_id"])
        for row in operation.failures
    }


def test_conflict_opt_in_adds_only_exact_joined_pairs_and_decodes_each_source_once(
    monkeypatch, tmp_path
):
    operation = _chain(monkeypatch, tmp_path)
    pair, encoded = _conflict_source(operation, rows=25)
    ignored_pair, _ignored = _conflict_source(
        operation, scope_index=1, reference_index=0, reason="other_gap_reason"
    )
    original_loads, decoded = runner.json.loads, []

    def observe(raw, *args, **kwargs):
        if raw == encoded:
            decoded.append(True)
        return original_loads(raw, *args, **kwargs)

    monkeypatch.setattr(runner.json, "loads", observe)
    audit = {}
    selected = runner.retry_scope_sources(
        operation.merge_path, operation.inventory,
        include_conflicts=True, selection_audit=audit,
    )
    assert selected == frozenset(_failure_pairs(operation) | {pair})
    assert ignored_pair not in selected
    assert decoded == [True]
    assert audit == {
        "recorded_conflict_pairs": 1, "eligible_conflict_pairs": 1,
        "unavailable_conflict_pairs": 0, "decoded_conflict_sources": 1,
    }


def test_distinct_sealed_snapshots_of_one_coordinate_produce_one_retry_pair(
    monkeypatch, tmp_path
):
    operation = _chain(monkeypatch, tmp_path)
    first, _encoded = _conflict_source(operation, snapshot="first-committed-snapshot")
    second, _encoded = _conflict_source(operation, snapshot="second-committed-snapshot")
    assert first == second
    audit = {}
    selected = runner.retry_scope_sources(
        operation.merge_path, operation.inventory,
        include_conflicts=True, selection_audit=audit,
    )
    assert selected == frozenset(_failure_pairs(operation) | {first})
    assert audit == {
        "recorded_conflict_pairs": 2, "eligible_conflict_pairs": 1,
        "unavailable_conflict_pairs": 0, "decoded_conflict_sources": 2,
    }


def test_default_selector_does_not_expand_successful_scopes_from_historical_gaps(
    monkeypatch, tmp_path
):
    operation = _chain(monkeypatch, tmp_path)
    pair, _encoded = _conflict_source(operation)
    selected = runner.retry_scope_sources(operation.merge_path, operation.inventory)
    assert selected == frozenset(_failure_pairs(operation))
    assert pair not in selected


def test_conflicts_can_supply_the_retry_set_without_failed_scope_exclusions(
    monkeypatch, tmp_path
):
    operation = _chain(monkeypatch, tmp_path)
    operation.exclusions.write_text("")
    pair, _encoded = _conflict_source(operation)
    assert runner.retry_scope_sources(
        operation.merge_path, operation.inventory, include_conflicts=True
    ) == frozenset({pair})


@pytest.mark.parametrize(
    "defect",
    ("source_key", "missing_source", "source_owner", "proof_owner", "proof_session",
     "gap_session", "source_peer", "proof_peer"),
)
def test_conflict_opt_in_rejects_broken_source_identity_or_foreign_scope(
    monkeypatch, tmp_path, defect
):
    operation = _chain(monkeypatch, tmp_path)
    _conflict_source(operation, defect=defect)
    with pytest.raises(runner.GlobalRepairError):
        runner.retry_scope_sources(
            operation.merge_path, operation.inventory, include_conflicts=True
        )


def test_unconfigured_historical_conflict_pairs_are_counted_once_and_skipped(
    monkeypatch, tmp_path
):
    operation = _chain(monkeypatch, tmp_path)
    available, _encoded = _conflict_source(operation)
    unavailable, _encoded = _conflict_source(
        operation, relative="db_storage/message/message_99.db", rows=3
    )
    audit = {}
    selected = runner.retry_scope_sources(
        operation.merge_path, operation.inventory,
        include_conflicts=True, selection_audit=audit,
    )
    assert selected == frozenset(_failure_pairs(operation) | {available})
    assert unavailable not in selected
    assert audit == {
        "recorded_conflict_pairs": 2, "eligible_conflict_pairs": 1,
        "unavailable_conflict_pairs": 1, "decoded_conflict_sources": 2,
    }


def test_conflict_candidate_identity_is_checked_after_source_proof_decoding(
    monkeypatch, tmp_path
):
    operation = _chain(monkeypatch, tmp_path)
    _pair, encoded = _conflict_source(operation)
    original_loads = runner.json.loads

    def changed(raw, *args, **kwargs):
        value = original_loads(raw, *args, **kwargs)
        if raw == encoded:
            operation.incoming.touch()
        return value

    monkeypatch.setattr(runner.json, "loads", changed)
    with pytest.raises(runner.GlobalRepairError):
        runner.retry_scope_sources(
            operation.merge_path, operation.inventory, include_conflicts=True
        )


@pytest.mark.parametrize("status", ("verified", "no_candidate_updates"))
def test_conflict_opt_in_requires_the_verified_merge_chain_not_a_direct_report(
    monkeypatch, tmp_path, status
):
    operation = _chain(monkeypatch, tmp_path)
    operation.origin.update(status=status, published=status == "verified")
    runner.write_json(operation.origin_path, operation.origin)
    with pytest.raises(runner.GlobalRepairError):
        runner.retry_scope_sources(
            operation.origin_path, operation.inventory, include_conflicts=True
        )
