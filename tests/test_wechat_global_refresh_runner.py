"""Global repair publication preserves observations and reports its true boundary.

Code version: v1.4.1-codex.0
"""

from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core.owner_only_files import ensure_owner_only, ensure_owner_only_directory
from app.core.wechat_global_refresh import GlobalRefreshInventory
from app.core.wechat_global_guard import global_refresh_busy
from app.core.wechat_mac_refresh_projection import ConversationScope
from app.core.wechat_mac_refresh_source import KeyReference
from scripts import refresh_wechat_globally as runner
from tests import test_wechat_macos_recovery_bank as fixtures
from tests import test_wechat_macos_reparse as repairs


mac_archive = fixtures.mac_archive
bank_archive = fixtures.bank_archive


@pytest.mark.parametrize("kind", ("quote", "forward"))
def test_preservation_accepts_a_journaled_same_native_row_repair(bank_archive, kind):
    old, new = repairs._packets(bank_archive, kind)
    fixtures._publish(bank_archive, packets=[old], name="before-global-repair")
    before = runner.bank_summary(bank_archive.bank_path)
    candidate, receipt = fixtures._stage(
        bank_archive, packets=[new], name="global-repair-candidate"
    )

    after = runner.verify_preservation(
        bank_archive.bank_path, candidate, before, receipt
    )

    assert receipt["reparsed"] == 1
    assert after["counts"]["messages"] == before["counts"]["messages"]
    assert after["counts"]["projection_revisions"] == 1
    assert runner.bank_summary(bank_archive.bank_path) == before


def test_preservation_accepts_ordinary_updates_without_a_repair_journal(bank_archive):
    fixtures._publish(bank_archive, packets=[bank_archive.direct], name="before-append")
    before = runner.bank_summary(bank_archive.bank_path)
    candidate, receipt = fixtures._stage(
        bank_archive, packets=[bank_archive.group], name="ordinary-append"
    )

    after = runner.verify_preservation(
        bank_archive.bank_path, candidate, before, receipt
    )

    assert before["counts"]["projection_revisions"] == 0
    assert after["counts"]["projection_revisions"] == 0
    assert after["counts"]["messages"] > before["counts"]["messages"]


@pytest.mark.parametrize("table", ("sources", "occurrences", "conversation_proofs"))
def test_preservation_rejects_a_changed_old_observation(bank_archive, table):
    fixtures._publish(bank_archive, packets=[bank_archive.direct], name="before-change")
    before = runner.bank_summary(bank_archive.bank_path)
    candidate, receipt = fixtures._stage(
        bank_archive, packets=[bank_archive.group], name="changed-observation"
    )
    with closing(sqlite3.connect(candidate)) as connection, connection:
        connection.execute(f"UPDATE {table} SET proof=?", ('{"changed":true}',))

    with pytest.raises(
        runner.GlobalRepairError, match="prior_observation_changed_" + table
    ):
        runner.verify_preservation(bank_archive.bank_path, candidate, before, receipt)


def test_preservation_rejects_an_old_message_changed_without_a_new_journal(bank_archive):
    fixtures._publish(bank_archive, packets=[bank_archive.direct], name="before-message")
    before = runner.bank_summary(bank_archive.bank_path)
    candidate, receipt = fixtures._stage(
        bank_archive, packets=[bank_archive.group], name="changed-message"
    )
    with closing(sqlite3.connect(candidate)) as connection, connection:
        connection.execute(
            "UPDATE messages SET text='Synthetic unjournaled edit' "
            "WHERE conversation_id=?",
            (bank_archive.direct["session_id"],),
        )

    with pytest.raises(
        runner.GlobalRepairError, match="changed_message_has_no_new_repair_journal"
    ):
        runner.verify_preservation(bank_archive.bank_path, candidate, before, receipt)


@pytest.mark.parametrize(
    ("counter", "table"),
    (("added", "messages"), ("reparsed", "projection_revisions")),
)
def test_preservation_rejects_receipt_counts_that_disagree(bank_archive, counter, table):
    old, new = repairs._packets(bank_archive, "quote")
    fixtures._publish(bank_archive, packets=[old], name="before-receipt")
    before = runner.bank_summary(bank_archive.bank_path)
    candidate, receipt = fixtures._stage(
        bank_archive, packets=[new], name="bad-count-receipt"
    )
    wrong = {**receipt, counter: receipt[counter] + 1}

    with pytest.raises(runner.GlobalRepairError, match="receipt_count_mismatch_" + table):
        runner.verify_preservation(bank_archive.bank_path, candidate, before, wrong)


def test_preservation_rejects_source_bank_identity_drift(bank_archive):
    fixtures._publish(bank_archive, packets=[bank_archive.direct], name="before-drift")
    before = runner.bank_summary(bank_archive.bank_path)
    candidate, receipt = fixtures._stage(
        bank_archive, packets=[bank_archive.group], name="drift-candidate"
    )
    with closing(sqlite3.connect(bank_archive.bank_path)) as connection, connection:
        connection.execute(
            "UPDATE archive_info SET value='changed' WHERE key='payload_sha256'"
        )

    with pytest.raises(
        runner.GlobalRepairError, match="bank_changed_before_preservation_check"
    ):
        runner.verify_preservation(bank_archive.bank_path, candidate, before, receipt)


def _isolated_run(monkeypatch, tmp_path, *, staged_inputs=None, readback_error=None):
    """Exercise orchestration while all native, archival, and publication I/O is stubbed."""
    local_store = tmp_path / "local-store"
    target = runner.bank.mac_recovery_bank_path(local_store)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"Synthetic prior publication")
    workstations = tmp_path / "workstations"
    workstations.mkdir(mode=0o700)
    workstation = workstations / "global-repair"
    workstation.mkdir(mode=0o700)
    ensure_owner_only_directory(workstation)
    monkeypatch.setattr(runner, "WORKSTATIONS", workstations)
    inputs = {"history.sqlite3": {"size": 1, "mtime_ns": 1}}
    before = {
        "exists": True,
        "identity": runner.bank._identity(target),
        "counts": {"messages": 1},
        "payload_sha256": "before",
    }
    after = {
        "exists": True,
        "counts": {"messages": 2},
        "payload_sha256": "candidate",
    }
    inventory = GlobalRefreshInventory(
        account_ids=(fixtures.OWNER,),
        scopes=(
            ConversationScope(
                "wechat:" + "a" * 64, fixtures.OWNER, fixtures.PEER, "direct"
            ),
        ),
        references=(
            KeyReference(
                fixtures.OWNER, fixtures.OWNER, "db_storage/message/message_1.db",
                tmp_path / "synthetic-encrypted-source.db",
            ),
        ),
        history_path=runner.wechat_archive_path(local_store),
        history_identity=deepcopy(inputs["history.sqlite3"]),
        summary={
            "total_archived_accounts": 1,
            "accounts_total": 1,
            "accounts_with_keys": 1,
            "accounts_without_keys": 0,
            "eligible_scopes": 1,
            "configured_sources": 1,
            "configured_source_bytes": 0,
            "unavailable_sources": 0,
            "unavailable_accounts": 0,
        },
    )
    helper = ModuleType("app.core.wechat_global_refresh")
    helper.inventory_global_refresh = lambda _root: inventory
    monkeypatch.setattr(runner, "source_capacity", lambda _inventory, _selection: {
        "serial_snapshot_peak_bytes": 4 * _inventory.summary["configured_source_bytes"],
        "source_groups": [],
    })
    packet_calls = []

    def packets(_root, **kwargs):
        packet_calls.append(kwargs)
        status = {
            "phase": "comparing", "checked": 1, "candidates": 1,
            "excluded": 0, "processed_scopes": 1, "processed_scope_sources": 1,
            "skipped_scopes": 0, "skipped_sources": 0, "completed_sources": 0,
            "unavailable_sources": 0, "oversized_scopes": 0, "reason_codes": {},
        }
        kwargs["on_progress"](status)
        yield {}
        kwargs["on_progress"]({**status, "phase": "complete", "completed_sources": 1})

    helper.iter_global_refresh_packets = packets
    monkeypatch.setitem(sys.modules, helper.__name__, helper)
    monkeypatch.setattr(runner.bank, "_inputs", lambda _history: deepcopy(inputs))
    summary_calls = []

    def summary(_path):
        summary_calls.append(_path)
        if len(summary_calls) == 1:
            return deepcopy(before)
        if readback_error is not None:
            raise readback_error
        return deepcopy(after)

    monkeypatch.setattr(runner, "bank_summary", summary)
    monkeypatch.setattr(runner, "verify_preservation", lambda *_args: deepcopy(after))
    monkeypatch.setattr(
        runner,
        "archive_file",
        lambda *_args: {
            key: True if key == "full_roundtrip_verified" else 1
            for key in (
                "archive", "receipt", "source_sha256", "archive_sha256",
                "archive_bytes", "decompressed_bytes", "full_roundtrip_verified",
            )
        },
    )
    receipt = {
        "added": 1, "upgraded": 0, "reparsed": 0, "occurrences_added": 1,
        "input_identities": deepcopy(inputs if staged_inputs is None else staged_inputs),
    }
    def stage(_root, packets, _candidate, **kwargs):
        for _packet in packets:
            pass
        kwargs["progress"]({"added": 1, "upgraded": 0, "reparsed": 0})
        return receipt

    monkeypatch.setattr(runner.bank, "stage_mac_recovery_bank", stage)
    publications = []

    def publish(*_args, publication_guard):
        runner.bank._require_publication_guard(
            publication_guard, runner.wechat_archive_path(local_store)
        )
        publications.append(True)
        return {"published": True}

    monkeypatch.setattr(runner.bank, "publish_mac_recovery_bank", publish)
    return SimpleNamespace(
        local_store=local_store, workstation=workstation,
        publications=publications, before=before, after=after,
        inventory=inventory, packet_calls=packet_calls,
    )


def _report(workstation: Path):
    paths = list(workstation.glob("run-*/result.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text())


def test_run_refuses_staging_inputs_that_drifted_from_preflight(monkeypatch, tmp_path):
    operation = _isolated_run(
        monkeypatch, tmp_path,
        staged_inputs={"history.sqlite3": {"size": 2, "mtime_ns": 2}},
    )

    with pytest.raises(runner.GlobalRepairError):
        runner.run(operation.local_store, operation.workstation, apply=True)

    report = _report(operation.workstation)
    assert operation.publications == []
    assert report["published"] is False
    assert report["status"] == "failed_before_publication"
    assert report["error_code"] == "staging_baseline_differs_from_reviewed_inputs"
    assert report["failed_phase"] == "staging"


def test_post_publication_readback_failure_retains_published_true(monkeypatch, tmp_path):
    operation = _isolated_run(
        monkeypatch, tmp_path, readback_error=OSError("Synthetic readback failure")
    )

    with pytest.raises(runner.GlobalRepairError):
        runner.run(operation.local_store, operation.workstation, apply=True)

    report = _report(operation.workstation)
    assert operation.publications == [True]
    assert report["published"] is True
    assert report["status"] == "published_verification_failed"
    assert report["publication_receipt"] == {"published": True}


def test_successful_run_verifies_the_published_counts_and_payload(monkeypatch, tmp_path):
    operation = _isolated_run(monkeypatch, tmp_path)

    result = runner.run(operation.local_store, operation.workstation, apply=True)

    report = json.loads(result.read_text())
    assert operation.publications == [True]
    assert report["published"] is True
    assert report["status"] == "verified"
    assert report["after"] == operation.after
    assert report["inventory"]["configured_account_scopes"] == 1
    assert report["preparation_progress"]["completed_sources"] == 1
    assert report["preparation_progress"]["processed_scopes"] == 1
    assert report["coverage"]["partial"] is False


def _assert_publication_guards(root):
    assert global_refresh_busy(root)
    history = runner.wechat_archive_path(root)
    with pytest.raises(ValueError, match="Another process is publishing"):
        with runner.bank._publication_lock(history):
            pytest.fail("The global apply must keep publication exclusion.")


def _assert_apply_guards_released(root):
    assert not global_refresh_busy(root)
    with runner.bank.mac_recovery_publication_guard(root) as guard:
        runner.bank._require_publication_guard(guard, runner.wechat_archive_path(root))


def test_apply_keeps_both_guards_from_baseline_through_publication_readback(
    monkeypatch, tmp_path
):
    operation = _isolated_run(monkeypatch, tmp_path)
    phases = []

    def instrument(target, attribute, phase):
        original = getattr(target, attribute)

        def observe(*args, **kwargs):
            _assert_publication_guards(operation.local_store)
            phases.append(phase)
            return original(*args, **kwargs)

        monkeypatch.setattr(target, attribute, observe)

    instrument(runner, "bank_summary", "summary")
    instrument(runner.bank, "_inputs", "input_pin")
    instrument(runner, "archive_file", "rollback")
    instrument(runner.bank, "stage_mac_recovery_bank", "staging")
    instrument(runner, "verify_preservation", "preservation")
    instrument(runner.bank, "publish_mac_recovery_bank", "publication")
    result = runner.run(operation.local_store, operation.workstation, apply=True)

    assert json.loads(result.read_text())["status"] == "verified"
    assert phases == [
        "summary", "input_pin", "rollback", "input_pin", "staging",
        "preservation", "publication", "summary",
    ]
    _assert_apply_guards_released(operation.local_store)


def test_inventory_only_never_acquires_either_publication_guard(monkeypatch, tmp_path):
    operation = _isolated_run(monkeypatch, tmp_path)

    def forbidden(*_args, **_kwargs):
        pytest.fail("An inventory-only run must not acquire publication guards.")

    monkeypatch.setattr(runner, "global_refresh_guard", forbidden)
    monkeypatch.setattr(runner.bank, "mac_recovery_publication_guard", forbidden)
    result = runner.run(operation.local_store, operation.workstation)

    assert json.loads(result.read_text())["status"] == "inventory_complete"
    assert operation.publications == operation.packet_calls == []
    assert not global_refresh_busy(operation.local_store)


def test_apply_without_candidates_releases_both_guards_and_does_not_publish(
    monkeypatch, tmp_path
):
    operation = _isolated_run(monkeypatch, tmp_path)
    helper = sys.modules["app.core.wechat_global_refresh"]

    def empty_packets(_root, **_kwargs):
        _assert_publication_guards(operation.local_store)
        yield from ()

    monkeypatch.setattr(helper, "iter_global_refresh_packets", empty_packets)
    result = runner.run(operation.local_store, operation.workstation, apply=True)

    report = json.loads(result.read_text())
    assert report["status"] == "no_candidate_updates"
    assert report["published"] is False
    assert operation.publications == []
    _assert_apply_guards_released(operation.local_store)


@pytest.mark.parametrize("failure", ("staging_pin", "published_readback"))
def test_apply_releases_both_guards_after_pre_or_post_publication_failure(
    monkeypatch, tmp_path, failure
):
    operation = _isolated_run(
        monkeypatch, tmp_path,
        staged_inputs={} if failure == "staging_pin" else None,
        readback_error=OSError("safe synthetic failure") if failure == "published_readback" else None,
    )
    with pytest.raises(runner.GlobalRepairError):
        runner.run(operation.local_store, operation.workstation, apply=True)
    assert _report(operation.workstation)["published"] is (failure == "published_readback")
    _assert_apply_guards_released(operation.local_store)


def test_completion_coverage_keeps_unavailable_and_rejected_work_visible():
    report = {
        "inventory": {
            "accounts_without_keys": 1,
            "unavailable_sources": 13,
            "unavailable_accounts": 0,
        },
        "preparation_progress": {
            "skipped_sources": 2, "skipped_scopes": 4, "excluded": 123,
        },
        "staging_receipt": {"conflicts": 7},
        "reason_codes": {"source_incomplete_wal_coverage": 1},
    }

    coverage = runner.completion_coverage(report)

    assert coverage["partial"] is True
    assert coverage["limitations"] == {
        "accounts_without_keys": 1,
        "unconfigured_message_databases": 13,
        "unavailable_accounts": 0,
        "skipped_sources": 2,
        "skipped_scope_sources": 4,
        "excluded_records": 123,
        "conflicting_candidates": 7,
        "incomplete_wal_sources": 1,
    }


def test_unavailable_recorded_conflict_pairs_keep_opt_in_coverage_partial():
    report = {
        "inventory": {
            "accounts_without_keys": 0, "unavailable_sources": 0,
            "unavailable_accounts": 0,
        },
        "retry_conflicts": True,
        "selection": {"unavailable_conflict_pairs": 2},
    }
    coverage = runner.completion_coverage(report)
    assert coverage == {
        "partial": True,
        "limitations": {
            "accounts_without_keys": 0, "unconfigured_message_databases": 0,
            "unavailable_accounts": 0, "skipped_sources": 0,
            "skipped_scope_sources": 0, "excluded_records": 0,
            "conflicting_candidates": 0, "incomplete_wal_sources": 0,
            "unavailable_recorded_conflict_pairs": 2,
        },
    }
    report.pop("retry_conflicts")
    default_coverage = runner.completion_coverage(report)
    assert default_coverage["partial"] is False
    assert "unavailable_recorded_conflict_pairs" not in default_coverage["limitations"]


def _retry_inventory(tmp_path):
    owner, other = fixtures.OWNER, "wxid_other_synthetic_owner"
    scopes = (
        ConversationScope("wechat:" + "a" * 64, owner, fixtures.PEER, "direct"),
        ConversationScope("wechat:" + "b" * 64, owner, fixtures.ROOM, "group"),
        ConversationScope("wechat:" + "c" * 64, other, "wxid_synthetic_peer", "direct"),
    )
    references = tuple(
        KeyReference(
            account, account, f"db_storage/message/message_{index}.db",
            tmp_path / f"synthetic-source-{index}.db",
        )
        for index, account in enumerate((owner, owner, other), 1)
    )
    return GlobalRefreshInventory(
        account_ids=(owner, other), scopes=scopes, references=references,
        history_path=tmp_path / "local-store" / "messaging" / "wechat" / "history.sqlite3",
        history_identity={"dev": 1, "ino": 2, "size": 3, "mtime_ns": 4, "ctime_ns": 5},
        summary={
            "total_archived_accounts": 2, "accounts_total": 2,
            "accounts_with_keys": 2, "accounts_without_keys": 0,
            "eligible_scopes": 3, "configured_sources": 3,
            "configured_source_bytes": 0, "unavailable_sources": 0,
            "unavailable_accounts": 0,
        },
    )


def _scope_failure(inventory, *, scope_index=0, reference_index=0, reason=None):
    scope, reference = inventory.scopes[scope_index], inventory.references[reference_index]
    return {
        "reason": reason or "scope_native_row_budget_exceeded",
        "account_id": scope.account_id,
        "session_id": scope.session_id,
        "database_relative_path": reference.relative_path,
    }


def _retry_report(monkeypatch, tmp_path, inventory, failures, **changes):
    workstations = tmp_path / "workstations"
    workstations.mkdir(mode=0o700, exist_ok=True)
    ensure_owner_only_directory(workstations)
    monkeypatch.setattr(runner, "WORKSTATIONS", workstations)
    prior_run = workstations / "prior-audit"
    prior_run.mkdir(mode=0o700)
    ensure_owner_only_directory(prior_run)
    report = {
        "version": runner.VERSION, "status": "verified", "published": True,
        "local_store": str(inventory.history_path.parents[2]),
        "input_identities": {"history.sqlite3": deepcopy(inventory.history_identity)},
        **changes,
    }
    report_path = prior_run / "result.json"
    runner.write_json(report_path, report)
    exclusions = prior_run / "exclusions.jsonl"
    exclusions.write_text(
        "".join(json.dumps(record) + "\n" for record in failures)
    )
    ensure_owner_only(exclusions)
    return report_path


def test_retry_selects_exact_scope_source_pairs_and_ignores_row_failures(monkeypatch, tmp_path):
    inventory = _retry_inventory(tmp_path)
    oversized = _scope_failure(inventory)
    unbound = _scope_failure(
        inventory, scope_index=1, reference_index=1,
        reason="scope_account_binding_unavailable",
    )
    row_failure = _scope_failure(
        inventory, scope_index=2, reference_index=2,
        reason="row_missing_authenticated_sender",
    )
    unsupported_scope = {**row_failure, "reason": "scope_projection_failed"}
    report_path = _retry_report(
        monkeypatch, tmp_path, inventory,
        [oversized, unbound, oversized, row_failure, unsupported_scope],
    )

    selected = runner.retry_scope_sources(report_path, inventory)

    assert selected == frozenset(
        (value["account_id"], value["database_relative_path"], value["session_id"])
        for value in (oversized, unbound)
    )


@pytest.mark.parametrize("status", ("staging", "preparing_sources", "published"))
def test_retry_rejects_a_nonterminal_report(monkeypatch, tmp_path, status):
    inventory = _retry_inventory(tmp_path)
    report_path = _retry_report(
        monkeypatch, tmp_path, inventory, [_scope_failure(inventory)], status=status
    )

    with pytest.raises(runner.GlobalRepairError, match="retry_report_is_not_terminal"):
        runner.retry_scope_sources(report_path, inventory)


@pytest.mark.parametrize("defect", ("cross_account", "stale_session", "stale_reference"))
def test_retry_rejects_coordinates_outside_current_inventory(monkeypatch, tmp_path, defect):
    inventory = _retry_inventory(tmp_path)
    failure = _scope_failure(inventory)
    if defect == "cross_account":
        failure["account_id"] = inventory.scopes[2].account_id
    elif defect == "stale_session":
        failure["session_id"] = "wechat:" + "d" * 64
    else:
        failure["database_relative_path"] = "db_storage/message/message_99.db"
    report_path = _retry_report(monkeypatch, tmp_path, inventory, [failure])

    with pytest.raises(
        runner.GlobalRepairError, match="retry_coordinates_are_no_longer_eligible"
    ):
        runner.retry_scope_sources(report_path, inventory)


def test_retry_rejects_a_report_from_another_archive(monkeypatch, tmp_path):
    inventory = _retry_inventory(tmp_path)
    report_path = _retry_report(
        monkeypatch, tmp_path, inventory, [_scope_failure(inventory)],
        local_store=str(tmp_path / "another-archive"),
    )

    with pytest.raises(
        runner.GlobalRepairError, match="retry_report_belongs_to_another_archive"
    ):
        runner.retry_scope_sources(report_path, inventory)


def test_retry_rejects_a_stale_canonical_history_pin(monkeypatch, tmp_path):
    inventory = _retry_inventory(tmp_path)
    stale = {**inventory.history_identity, "mtime_ns": 999}
    report_path = _retry_report(
        monkeypatch, tmp_path, inventory, [_scope_failure(inventory)],
        input_identities={"history.sqlite3": stale},
    )

    with pytest.raises(
        runner.GlobalRepairError, match="retry_report_history_identity_is_stale"
    ):
        runner.retry_scope_sources(report_path, inventory)


def test_retry_rejects_exclusion_coordinates_changed_during_read(monkeypatch, tmp_path):
    inventory = _retry_inventory(tmp_path)
    report_path = _retry_report(monkeypatch, tmp_path, inventory, [_scope_failure(inventory)])
    failures = report_path.with_name("exclusions.jsonl")
    identity = runner.bank._identity
    observations = []

    def changing(path, **kwargs):
        value = identity(path, **kwargs)
        if path == failures:
            observations.append(True)
            if len(observations) > 1:
                value = {**value, "mtime_ns": value["mtime_ns"] + 1}
        return value

    monkeypatch.setattr(runner.bank, "_identity", changing)

    with pytest.raises(
        runner.GlobalRepairError, match="retry_coordinates_changed_during_read"
    ):
        runner.retry_scope_sources(report_path, inventory)


def test_run_forwards_only_audited_retry_pairs_to_packet_preparation(monkeypatch, tmp_path):
    operation = _isolated_run(monkeypatch, tmp_path)
    failure = _scope_failure(operation.inventory)
    report_path = _retry_report(monkeypatch, tmp_path, operation.inventory, [failure])

    result = runner.run(
        operation.local_store, operation.workstation, apply=True, retry_report=report_path
    )

    expected = frozenset(
        {(failure["account_id"], failure["database_relative_path"], failure["session_id"])}
    )
    assert len(operation.packet_calls) == 1
    assert operation.packet_calls[0]["scope_sources"] == expected
    report = json.loads(result.read_text())
    assert report["selection"] == {"scope_source_pairs": 1, "scopes": 1, "sources": 1}
    assert report["status"] == "verified"


def test_run_forwards_conflict_opt_in_and_records_exact_selection_audit(
    monkeypatch, tmp_path
):
    operation = _isolated_run(monkeypatch, tmp_path)
    failure = _scope_failure(operation.inventory)
    selection = frozenset({(
        failure["account_id"], failure["database_relative_path"], failure["session_id"],
    )})
    report_path = operation.workstation / "verified-merge-report.json"
    audit = {
        "recorded_conflict_pairs": 3, "eligible_conflict_pairs": 1,
        "unavailable_conflict_pairs": 1, "decoded_conflict_sources": 3,
    }

    def select(path, inventory, *, include_conflicts, selection_audit):
        assert path == report_path and inventory is operation.inventory
        assert include_conflicts is True
        selection_audit.update(audit)
        return selection

    monkeypatch.setattr(runner, "retry_scope_sources", select)
    result = runner.run(
        operation.local_store, operation.workstation, apply=True,
        retry_report=report_path, include_conflicts=True,
    )
    assert operation.packet_calls[0]["scope_sources"] == selection
    report = json.loads(result.read_text())
    assert report["retry_conflicts"] is True
    assert report["selection"] == {
        "scope_source_pairs": 1, "scopes": 1, "sources": 1, **audit,
    }
    assert report["status"] == "verified"


def test_programmatic_conflict_retry_requires_a_report_without_preparing_sources(
    monkeypatch, tmp_path
):
    operation = _isolated_run(monkeypatch, tmp_path)
    with pytest.raises(runner.GlobalRepairError):
        runner.run(
            operation.local_store, operation.workstation,
            apply=True, include_conflicts=True,
        )
    report = _report(operation.workstation)
    assert report["error_code"] == "conflict_retry_requires_a_verified_merge_report"
    assert report["published"] is False
    assert operation.packet_calls == operation.publications == []
    _assert_apply_guards_released(operation.local_store)


def test_cli_conflict_flag_requires_retry_report_before_running(
    monkeypatch, tmp_path, capsys
):
    run = Mock()
    monkeypatch.setattr(runner, "run", run)
    with pytest.raises(SystemExit) as exited:
        runner.main([
            "--local-store", str(tmp_path / "store"),
            "--workstation", str(tmp_path / "workstation"), "--retry-conflicts",
        ])
    assert exited.value.code == 2
    assert "--retry-conflicts requires --retry-report" in capsys.readouterr().err
    run.assert_not_called()


def test_cli_conflict_flag_is_explicitly_forwarded_to_the_run(monkeypatch, tmp_path):
    store, workstation = tmp_path / "store", tmp_path / "workstation"
    report_path = tmp_path / "verified-merge-result.json"
    run = Mock(return_value=tmp_path / "result.json")
    monkeypatch.setattr(runner, "run", run)
    assert runner.main([
        "--local-store", str(store), "--workstation", str(workstation),
        "--retry-report", str(report_path), "--retry-conflicts", "--apply",
    ]) == 0
    run.assert_called_once_with(
        store, workstation, apply=True, retry_report=report_path,
        include_conflicts=True,
    )


def _capacity_run(monkeypatch, tmp_path, *, before_delta=1, after_delta=0):
    """Model one compressed rollback's real allocation without allocating a large bank."""
    operation = _isolated_run(monkeypatch, tmp_path)
    source_bytes = 11
    operation.inventory.summary["configured_source_bytes"] = source_bytes
    bank_bytes = operation.before["identity"]["size"]
    required = 2 * bank_bytes + 1024**3
    archives, observations = [], []
    archive_file = runner.archive_file

    def archive(source, destination):
        receipt = archive_file(source, destination)
        destination.write_bytes(b"z")
        receipt_path = destination.with_suffix(destination.suffix + ".json")
        receipt.update(archive=str(destination), receipt=str(receipt_path))
        runner.write_json(receipt_path, receipt)
        archives.append((destination, receipt_path))
        return receipt

    def capacity(_directory):
        delta = after_delta if archives else before_delta
        observations.append(bool(archives))
        return SimpleNamespace(free=required + delta)

    stage = Mock(wraps=runner.bank.stage_mac_recovery_bank)
    monkeypatch.setattr(runner, "archive_file", archive)
    monkeypatch.setattr(runner.shutil, "disk_usage", capacity)
    monkeypatch.setattr(runner.bank, "stage_mac_recovery_bank", stage)
    return SimpleNamespace(
        operation=operation, required=required, bank_bytes=bank_bytes,
        archives=archives, observations=observations, stage=stage,
    )


def test_small_compressed_rollback_can_proceed_between_old_and_new_capacity_bounds(
    monkeypatch, tmp_path
):
    capacity = _capacity_run(monkeypatch, tmp_path)
    operation = capacity.operation
    assert capacity.required + 1 < capacity.required + capacity.bank_bytes
    path = runner.run(operation.local_store, operation.workstation, apply=True)
    report = json.loads(path.read_text())
    assert report["status"] == "verified" and operation.publications == [True]
    assert capacity.observations == [False, True]
    assert report["capacity_budget"] == {
        "serial_snapshot_peak_bytes": 44,
        "source_groups": [],
        "snapshot_storage": "memory_only",
        "stage_required_bytes": capacity.required,
        "free_before_rollback_bytes": capacity.required + 1,
        "free_after_rollback_bytes": capacity.required,
        "rollback_accounted_as_existing_archive": True,
    }
    assert report["rollback"]["archive_bytes"] == 1
    assert all(path.exists() for pair in capacity.archives for path in pair)
    _assert_apply_guards_released(operation.local_store)


def test_insufficient_capacity_before_rollback_starts_no_backup_or_native_preparation(
    monkeypatch, tmp_path
):
    capacity = _capacity_run(monkeypatch, tmp_path, before_delta=-1)
    operation = capacity.operation
    with pytest.raises(runner.GlobalRepairError):
        runner.run(operation.local_store, operation.workstation, apply=True)
    report = _report(operation.workstation)
    assert report["published"] is False
    assert report["capacity_budget"]["stage_required_bytes"] == capacity.required
    assert report["capacity_budget"]["free_before_rollback_bytes"] == capacity.required - 1
    assert capacity.observations == [False]
    assert capacity.archives == operation.packet_calls == operation.publications == []
    assert "rollback" not in report
    capacity.stage.assert_not_called()
    _assert_apply_guards_released(operation.local_store)


def test_insufficient_capacity_after_rollback_keeps_the_verified_archive_and_stops(
    monkeypatch, tmp_path
):
    capacity = _capacity_run(monkeypatch, tmp_path, after_delta=-1)
    operation = capacity.operation
    with pytest.raises(runner.GlobalRepairError):
        runner.run(operation.local_store, operation.workstation, apply=True)
    report = _report(operation.workstation)
    assert report["published"] is False
    assert report["rollback"]["full_roundtrip_verified"] is True
    assert report["capacity_budget"]["free_after_rollback_bytes"] == capacity.required - 1
    assert report["capacity_budget"]["rollback_accounted_as_existing_archive"] is True
    assert capacity.observations == [False, True]
    assert len(capacity.archives) == 1
    archive, receipt_path = capacity.archives[0]
    assert archive.read_bytes() == b"z"
    assert json.loads(receipt_path.read_text())["full_roundtrip_verified"] is True
    assert operation.packet_calls == operation.publications == []
    capacity.stage.assert_not_called()
    _assert_apply_guards_released(operation.local_store)


@pytest.mark.parametrize("drift", ("bank", "source"))
def test_rollback_capacity_recheck_keeps_bank_and_source_identity_guards(
    monkeypatch, tmp_path, drift
):
    capacity = _capacity_run(monkeypatch, tmp_path)
    operation = capacity.operation
    archive = runner.archive_file
    inputs = runner.bank._inputs

    def changed_backup(source, destination):
        receipt = archive(source, destination)
        if drift == "bank":
            source.write_bytes(b"Synthetic newer user publication")
        return receipt

    def changed_inputs(history):
        value = inputs(history)
        if drift == "source" and capacity.archives:
            value["history.sqlite3"]["mtime_ns"] += 1
        return value

    monkeypatch.setattr(runner, "archive_file", changed_backup)
    monkeypatch.setattr(runner.bank, "_inputs", changed_inputs)
    with pytest.raises(runner.GlobalRepairError):
        runner.run(operation.local_store, operation.workstation, apply=True)
    report = _report(operation.workstation)
    assert report["error_code"] == (
        "bank_changed_during_rollback" if drift == "bank" else "source_changed_during_rollback"
    )
    assert report["published"] is False
    assert report["rollback"]["full_roundtrip_verified"] is True
    assert operation.packet_calls == operation.publications == []
    capacity.stage.assert_not_called()
    _assert_apply_guards_released(operation.local_store)
