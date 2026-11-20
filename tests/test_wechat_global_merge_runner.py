"""Final merge orchestration preserves newer publication and immutable evidence.

Code version: v1.0.0-codex.0
"""

from copy import deepcopy
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import wechat_global_guard as lease
from app.core import wechat_macos_bank_merge as merger
from app.core import wechat_macos_recovery_bank as bank
from app.core.wechat_archive import wechat_archive_path
from scripts import merge_wechat_global_refresh as runner
from tests import test_wechat_macos_recovery_bank as fixtures


mac_archive = fixtures.mac_archive
bank_archive = fixtures.bank_archive


def _owner_databases(tmp_path, registered, incoming_owners):
    store = tmp_path / "store"
    history = wechat_archive_path(store)
    history.parent.mkdir(parents=True)
    with sqlite3.connect(history) as connection:
        connection.execute("CREATE TABLE partitions(account_id TEXT, account_key TEXT)")
        connection.executemany(
            "INSERT INTO partitions VALUES(?,?)",
            [(owner, "account:" + owner) for owner in registered],
        )
    incoming = tmp_path / "incoming.sqlite3"
    with sqlite3.connect(incoming) as connection:
        connection.execute("CREATE TABLE sources(account_id TEXT)")
        connection.executemany(
            "INSERT INTO sources VALUES(?)", [(owner,) for owner in incoming_owners]
        )
    return store, incoming


def test_merge_accounts_selects_both_registered_owners_once_in_stable_order(tmp_path):
    store, incoming = _owner_databases(
        tmp_path, ("owner-a", "owner-b"), ("owner-b", "owner-a", "owner-b")
    )
    assert runner.merge_accounts(store, incoming) == ("owner-a", "owner-b")


@pytest.mark.parametrize("count", (1_000, 1_001))
def test_merge_account_selector_enforces_its_bound_without_hiding_extra_owners(
    tmp_path, count
):
    owners = tuple(f"owner-{index:04d}" for index in range(count))
    store, incoming = _owner_databases(tmp_path, owners, reversed(owners))
    if count == 1_000:
        assert runner.merge_accounts(store, incoming) == owners
    else:
        with pytest.raises(
            runner.GlobalRepairError, match="incoming_bank_has_unregistered_account"
        ):
            runner.merge_accounts(store, incoming)


@pytest.mark.parametrize("owners", ((), ("owner-a", "foreign-owner")))
def test_empty_or_foreign_incoming_owner_set_is_rejected(tmp_path, owners):
    store, incoming = _owner_databases(tmp_path, ("owner-a",), owners)
    with pytest.raises(
        runner.GlobalRepairError, match="incoming_bank_has_unregistered_account"
    ):
        runner.merge_accounts(store, incoming)


def test_owner_registration_requires_its_canonical_account_key(tmp_path):
    store, incoming = _owner_databases(tmp_path, ("owner-a", "owner-b"), ("owner-b",))
    with sqlite3.connect(wechat_archive_path(store)) as connection:
        connection.execute(
            "UPDATE partitions SET account_key='account:owner-a' WHERE account_id='owner-b'"
        )
    with pytest.raises(
        runner.GlobalRepairError, match="incoming_bank_has_unregistered_account"
    ):
        runner.merge_accounts(store, incoming)


def _identity(path):
    return bank._identity(path), hashlib.sha256(path.read_bytes()).hexdigest()


def _isolated_run(monkeypatch, tmp_path, *, staged_inputs=None, stage_error=None,
                  readback_error=None, readback_mismatch=False):
    """Use real lock capabilities while replacing bank mutation with bounded fakes."""
    store, source = _owner_databases(
        tmp_path, ("owner-a", "owner-b"), ("owner-b", "owner-a")
    )
    workstations = tmp_path / "workstations"
    workstations.mkdir(mode=0o700)
    workstation = workstations / "repair"
    workstation.mkdir(mode=0o700)
    incoming = workstation / "immutable-incoming.sqlite3"
    source.rename(incoming)
    incoming.chmod(0o600)
    incoming_before = _identity(incoming)
    target = bank.mac_recovery_bank_path(store)
    prior_bytes = b"The newer UI publication must survive the global merge."
    target.write_bytes(prior_bytes)
    target.chmod(0o600)
    inputs = {
        "history.sqlite3": bank._identity(wechat_archive_path(store)),
        target.name: bank._identity(target),
    }
    before = {
        "exists": True, "identity": bank._identity(target),
        "counts": {"messages": 2}, "payload_sha256": "current-ui-bank",
    }
    after = {
        "exists": True, "counts": {"messages": 3},
        "payload_sha256": "merged-current-and-incoming",
    }
    phases, publications, received_guards = [], [], []
    history = wechat_archive_path(store)
    original_summary = runner.bank_summary

    def assert_guarded(phase):
        assert lease.global_refresh_busy(store) is True
        with pytest.raises(ValueError, match="Another process is publishing"):
            with bank._publication_lock(history):
                pytest.fail("Every merge write phase must retain publication exclusion.")
        phases.append(phase)

    def summary(path):
        if path == incoming:
            assert not lease.global_refresh_busy(store)
            return {
                "exists": True, "identity": bank._identity(incoming),
                "counts": {"messages": 1}, "payload_sha256": "incoming",
            }
        if path == target:
            assert_guarded("readback" if publications else "baseline")
            if publications:
                if readback_error:
                    raise readback_error
                if readback_mismatch:
                    return {**deepcopy(after), "counts": {"messages": 4}}
                return deepcopy(after)
            return deepcopy(before)
        return original_summary(path)

    def archive(path, destination):
        assert_guarded("rollback")
        assert path == target and path.read_bytes() == prior_bytes
        assert destination.parent.is_relative_to(workstation)
        return {
            "archive": str(destination), "receipt": str(destination) + ".json",
            "source_sha256": "a" * 64, "archive_sha256": "b" * 64,
            "archive_bytes": 1, "decompressed_bytes": len(prior_bytes),
            "full_roundtrip_verified": True,
        }

    def stage(root, observed_incoming, candidate, *, account_ids,
              publication_guard, progress):
        assert_guarded("staging")
        bank._require_publication_guard(publication_guard, history)
        received_guards.append(publication_guard)
        assert root == store and observed_incoming == incoming
        assert account_ids == ("owner-a", "owner-b")
        assert target.read_bytes() == prior_bytes
        assert _identity(incoming) == incoming_before
        if stage_error:
            raise stage_error
        candidate.write_bytes(prior_bytes + b" Authenticated incoming update.")
        candidate.chmod(0o600)
        progress({"replayed_occurrences": 1, "phase": "safe_phase", "private": "hidden"})
        return {
            "added": 1, "upgraded": 0, "reparsed": 0, "occurrences_added": 1,
            "input_identities": deepcopy(inputs if staged_inputs is None else staged_inputs),
        }

    def preserve(previous, candidate, observed_before, receipt):
        assert_guarded("preservation")
        assert previous == target and previous.read_bytes() == prior_bytes
        assert candidate.read_bytes().startswith(prior_bytes)
        assert observed_before == before
        assert receipt["input_identities"] == inputs
        return deepcopy(after)

    def publish(root, candidate, receipt, *, publication_guard):
        assert_guarded("publication")
        assert publication_guard is received_guards[0]
        bank._require_publication_guard(publication_guard, history)
        assert root == store and receipt["input_identities"] == inputs
        assert target.read_bytes() == prior_bytes
        candidate.replace(target)
        publications.append(True)
        return {"published": True}

    monkeypatch.setattr(runner, "WORKSTATIONS", workstations)
    monkeypatch.setattr(runner, "bank_summary", summary)
    monkeypatch.setattr(runner, "archive_file", archive)
    monkeypatch.setattr(runner, "verify_preservation", preserve)
    monkeypatch.setattr(runner.bank, "_inputs", lambda _history: deepcopy(inputs))
    monkeypatch.setattr(runner.bank, "publish_mac_recovery_bank", publish)
    monkeypatch.setattr(merger, "stage_merged_mac_recovery_bank", stage)
    monkeypatch.setattr(
        runner.shutil, "disk_usage", lambda _path: SimpleNamespace(free=2**50)
    )
    return SimpleNamespace(
        store=store, workstation=workstation, incoming=incoming,
        incoming_before=incoming_before, target=target, prior_bytes=prior_bytes,
        phases=phases, publications=publications, received_guards=received_guards,
    )


def _report(operation):
    paths = list(operation.workstation.glob("merge-*/result.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text())


def _assert_released(operation):
    assert not lease.global_refresh_busy(operation.store)
    with bank.mac_recovery_publication_guard(operation.store) as guard:
        bank._require_publication_guard(guard, wechat_archive_path(operation.store))
    assert _identity(operation.incoming) == operation.incoming_before
    assert all(not guard.active for guard in operation.received_guards)


def test_merge_keeps_both_guards_through_latest_ui_baseline_and_readback(
    monkeypatch, tmp_path
):
    operation = _isolated_run(monkeypatch, tmp_path)
    path = runner.run(
        operation.store, operation.workstation, operation.incoming, apply=True
    )
    report = json.loads(path.read_text())
    assert report["status"] == "verified" and report["published"] is True
    assert report["account_count"] == 2
    assert report["progress"] == {"replayed_occurrences": 1}
    assert operation.phases == [
        "baseline", "rollback", "staging", "preservation", "publication", "readback",
    ]
    assert operation.publications == [True]
    assert operation.target.read_bytes().startswith(operation.prior_bytes)
    _assert_released(operation)


@pytest.mark.parametrize("failure", ("receipt_pin", "source_pin"))
def test_changed_pins_abort_before_publication_and_leave_current_bank_intact(
    monkeypatch, tmp_path, failure
):
    operation = _isolated_run(
        monkeypatch, tmp_path,
        staged_inputs={} if failure == "receipt_pin" else None,
        stage_error=(
            merger.BankMergeError("source_input_identity_mismatch")
            if failure == "source_pin" else None
        ),
    )
    with pytest.raises(runner.GlobalRepairError, match="bank_merge_failed"):
        runner.run(operation.store, operation.workstation, operation.incoming, apply=True)
    report = _report(operation)
    assert report["status"] == "failed_before_publication"
    assert report["published"] is False
    assert operation.publications == []
    assert "preservation" not in operation.phases
    assert operation.target.read_bytes() == operation.prior_bytes
    _assert_released(operation)


@pytest.mark.parametrize("failure", ("io_error", "different_counts"))
def test_post_publication_readback_failure_reports_publication_and_releases_locks(
    monkeypatch, tmp_path, failure
):
    operation = _isolated_run(
        monkeypatch, tmp_path,
        readback_error=OSError("private readback detail") if failure == "io_error" else None,
        readback_mismatch=failure == "different_counts",
    )
    with pytest.raises(runner.GlobalRepairError, match="bank_merge_failed"):
        runner.run(operation.store, operation.workstation, operation.incoming, apply=True)
    report = _report(operation)
    assert report["status"] == "published_verification_failed"
    assert report["published"] is True
    assert report["failed_phase"] == "published"
    assert report["error_type"] == (
        "OSError" if failure == "io_error" else "GlobalRepairError"
    )
    if failure == "different_counts":
        assert report["error_code"] == "published_merge_readback_differs_from_candidate"
    assert "private readback detail" not in json.dumps(report)
    assert operation.publications == [True]
    assert operation.target.read_bytes().startswith(operation.prior_bytes)
    _assert_released(operation)


def test_inventory_only_does_not_lock_stage_or_publish(monkeypatch, tmp_path):
    operation = _isolated_run(monkeypatch, tmp_path)
    path = runner.run(operation.store, operation.workstation, operation.incoming)
    report = json.loads(path.read_text())
    assert report["status"] == "inventory_only_not_verified"
    assert report["published"] is False
    assert operation.phases == operation.publications == []
    assert operation.target.read_bytes() == operation.prior_bytes
    _assert_released(operation)


def _real_merge_inputs(bank_archive, monkeypatch, tmp_path):
    incoming, _receipt = fixtures._stage(
        bank_archive, packets=[bank_archive.direct], name="earlier-global-candidate"
    )
    fixtures._publish(
        bank_archive, packets=[bank_archive.group], name="newer-ui-publication"
    )
    workstations = tmp_path / "canonical-workstations"
    workstations.mkdir(mode=0o700)
    workstation = workstations / "isolated-repair"
    workstation.mkdir(mode=0o700)
    incoming = incoming.rename(workstation / "immutable-global-candidate.sqlite3")
    monkeypatch.setattr(runner, "WORKSTATIONS", workstations)
    monkeypatch.setattr(
        runner.shutil, "disk_usage", lambda _path: SimpleNamespace(free=2**50)
    )
    backups = []

    def archive(source, destination):
        assert lease.global_refresh_busy(bank_archive.root)
        with pytest.raises(ValueError, match="Another process is publishing"):
            with bank._publication_lock(bank_archive.history):
                pytest.fail("The current bank rollback must be taken under exclusion.")
        backups.append(source.read_bytes())
        return {
            "archive": str(destination), "receipt": str(destination) + ".json",
            "source_sha256": hashlib.sha256(backups[-1]).hexdigest(),
            "archive_sha256": "a" * 64, "archive_bytes": 1,
            "decompressed_bytes": len(backups[-1]), "full_roundtrip_verified": True,
        }

    monkeypatch.setattr(runner, "archive_file", archive)
    return workstation, incoming, backups


def test_real_merge_cli_preserves_newer_ui_events_and_immutable_incoming_bank(
    bank_archive, monkeypatch, tmp_path
):
    fixture = bank_archive
    workstation, incoming, backups = _real_merge_inputs(
        fixture, monkeypatch, tmp_path
    )
    incoming_before = _identity(incoming)
    original_history = fixture.history.read_bytes()
    prior_bank = fixture.bank_path.read_bytes()
    with sqlite3.connect(fixture.bank_path) as connection:
        prior_events = set(connection.execute("SELECT id,text FROM messages"))
    with sqlite3.connect(incoming) as connection:
        incoming_events = set(connection.execute("SELECT id,text FROM messages"))
    stage_errors = []
    original_stage = merger.stage_merged_mac_recovery_bank

    def stage(*args, **kwargs):
        try:
            return original_stage(*args, **kwargs)
        except Exception as error:
            stage_errors.append(error)
            raise

    monkeypatch.setattr(merger, "stage_merged_mac_recovery_bank", stage)
    try:
        report_path = runner.run(fixture.root, workstation, incoming, apply=True)
    except runner.GlobalRepairError:
        if stage_errors:
            raise stage_errors[0]
        raise

    report = json.loads(report_path.read_text())
    assert report["status"] == "verified" and report["published"] is True
    assert report["staging_receipt"]["added"] == len(incoming_events)
    assert report["rollback"]["full_roundtrip_verified"] is True
    assert backups == [prior_bank]
    with sqlite3.connect(fixture.bank_path) as connection:
        assert set(connection.execute("SELECT id,text FROM messages")) == (
            prior_events | incoming_events
        )
    assert fixture.history.read_bytes() == original_history
    assert _identity(incoming) == incoming_before
    assert not lease.global_refresh_busy(fixture.root)
    with bank.mac_recovery_publication_guard(fixture.root) as guard:
        bank._require_publication_guard(guard, fixture.history)


def test_real_merge_cli_rejects_stale_incoming_source_pin_without_publication(
    bank_archive, monkeypatch, tmp_path
):
    fixture = bank_archive
    workstation, incoming, _backups = _real_merge_inputs(
        fixture, monkeypatch, tmp_path
    )
    with sqlite3.connect(incoming) as connection:
        connection.execute(
            "UPDATE archive_info SET value=? WHERE key='input_identities'",
            (json.dumps({"history.sqlite3": {"size": 0}}),),
        )
    incoming_before = _identity(incoming)
    prior_bank = fixture.bank_path.read_bytes()
    original_history = fixture.history.read_bytes()

    with pytest.raises(runner.GlobalRepairError, match="bank_merge_failed"):
        runner.run(fixture.root, workstation, incoming, apply=True)

    paths = list(workstation.glob("merge-*/result.json"))
    assert len(paths) == 1
    report = json.loads(paths[0].read_text())
    assert report["status"] == "failed_before_publication"
    assert report["published"] is False
    assert report["failed_phase"] == "replaying_validated_observations"
    assert "publication_receipt" not in report
    assert fixture.bank_path.read_bytes() == prior_bank
    assert fixture.history.read_bytes() == original_history
    assert _identity(incoming) == incoming_before
    assert not lease.global_refresh_busy(fixture.root)
    with bank.mac_recovery_publication_guard(fixture.root) as guard:
        bank._require_publication_guard(guard, fixture.history)
