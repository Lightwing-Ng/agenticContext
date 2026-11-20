"""Private scratch authority stays separate from global retry selection.

Code version: v1.2.0-codex.0
"""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core import wechat_mac_refresh_source as source
from scripts import refresh_wechat_globally as runner
from tests.test_wechat_global_refresh_runner import _isolated_run


def _failed(operation):
    directory = operation.workstation / "run-failed"
    directory.mkdir(mode=0o700)
    path = directory / "result.json"
    inputs = runner.bank._inputs(runner.wechat_archive_path(operation.local_store))
    value = {
        "status": "failed_before_publication", "failed_phase": "staging",
        "published": False, "local_store": str(operation.local_store),
        "input_identities": inputs, "before": deepcopy(operation.before),
    }
    runner.write_json(path, value)
    return path, value


def test_reuse_forwards_verified_capability_without_changing_scope_or_old_report(monkeypatch, tmp_path):
    operation = _isolated_run(monkeypatch, tmp_path)
    path, _value = _failed(operation)
    prior_bytes = path.read_bytes()
    candidate = path.with_name("candidate.sqlite3")
    candidate.write_bytes(b"Synthetic private scratch")
    capability = SimpleNamespace(summary={"scratch_path": str(candidate), "payload_sha256": "before"})
    verify = Mock(return_value=capability)
    monkeypatch.setattr(runner.bank, "verify_baseline_copy", verify, raising=False)
    stage = Mock(wraps=runner.bank.stage_mac_recovery_bank)
    monkeypatch.setattr(runner.bank, "stage_mac_recovery_bank", stage)

    result = runner.run(operation.local_store, operation.workstation, apply=True, reuse_failed_report=path)

    report = json.loads(result.read_text())
    assert report["status"] == "verified"
    assert report["baseline_reuse"]["consumed_by_publication"] is True
    assert path.read_bytes() == prior_bytes
    assert "scope_sources" not in operation.packet_calls[0]
    assert stage.call_args.args[2] == candidate
    assert stage.call_args.kwargs["baseline_copy"] is capability
    assert stage.call_args.kwargs["publication_guard"] is verify.call_args.kwargs["publication_guard"]


@pytest.mark.parametrize("field", ("status", "failed_phase", "published", "local_store", "input_identities", "before"))
def test_reuse_rejects_disconnected_report_before_native_preparation(monkeypatch, tmp_path, field):
    operation = _isolated_run(monkeypatch, tmp_path)
    path, value = _failed(operation)
    value[field] = "synthetic drift"
    runner.write_json(path, value)
    verify = Mock()
    monkeypatch.setattr(runner.bank, "verify_baseline_copy", verify, raising=False)
    with pytest.raises(runner.GlobalRepairError):
        runner.run(operation.local_store, operation.workstation, apply=True, reuse_failed_report=path)
    reports = [item for item in operation.workstation.glob("run-*/result.json") if item != path]
    assert len(reports) == 1
    assert json.loads(reports[0].read_text())["published"] is False
    assert operation.packet_calls == operation.publications == []
    verify.assert_not_called()


def _inventory(tmp_path, sizes):
    references = []
    for index, size in enumerate(sizes):
        path = tmp_path / f"source-{index}.db"
        path.write_bytes(bytes(size))
        references.append(SimpleNamespace(account_id="synthetic-owner", relative_path=f"source-{index}.db", database_path=path))
    return SimpleNamespace(references=references)


def test_serial_capacity_uses_largest_snapshot_including_wal(monkeypatch, tmp_path):
    inventory = _inventory(tmp_path, (100, 200))
    Path(str(inventory.references[0].database_path) + "-wal").write_bytes(bytes(80))
    Path(str(inventory.references[0].database_path) + "-shm").write_bytes(bytes(12))
    admitted = Mock()
    monkeypatch.setattr(source, "_admit_snapshot", admitted)
    result = runner.source_capacity(inventory, None)
    assert result["serial_snapshot_peak_bytes"] == source.SNAPSHOT_MEMORY_OVERHEAD + 4 * 180 + 2 * 80 + 12
    assert len(result["source_groups"]) == admitted.call_count == 2
    assert result["serial_snapshot_peak_bytes"] < source.SNAPSHOT_MEMORY_OVERHEAD + 4 * 300 + 2 * 80 + 12
    selected = runner.source_capacity(inventory, frozenset({("synthetic-owner", "source-1.db", "session")}))
    assert selected["serial_snapshot_peak_bytes"] == source.SNAPSHOT_MEMORY_OVERHEAD + 800
    assert len(selected["source_groups"]) == 1


def test_serial_capacity_rejects_uncommitted_journal(tmp_path):
    inventory = _inventory(tmp_path, (100,))
    Path(str(inventory.references[0].database_path) + "-journal").write_bytes(b"active")
    with pytest.raises(runner.GlobalRepairError, match="committed_group"):
        runner.source_capacity(inventory, None)


def test_failure_frames_excludes_exception_text_and_native_locals():
    private_body = "Synthetic secret body and key material"
    try:
        raise ValueError(private_body)
    except ValueError as error:
        frames = runner.failure_frames(error)
    assert frames and len(frames) <= 24
    assert all(set(frame) == {"file", "function", "line"} for frame in frames)
    assert private_body not in json.dumps(frames)


def test_cli_sanitizes_a_busy_publication_lease(monkeypatch, tmp_path, capsys):
    private_error = "Synthetic sensitive lock detail"
    monkeypatch.setattr(runner, "run", Mock(side_effect=ValueError(private_error)))
    assert runner.main([
        "--local-store", str(tmp_path / "store"),
        "--workstation", str(tmp_path / "workstation"), "--apply",
    ]) == 1
    result = capsys.readouterr()
    assert "did not complete" in result.err
    assert private_error not in result.out + result.err


def test_candidate_growth_stops_before_publication_when_free_space_floor_is_crossed(monkeypatch, tmp_path):
    operation = _isolated_run(monkeypatch, tmp_path)
    target = runner.bank.mac_recovery_bank_path(operation.local_store)
    before_bytes = target.read_bytes()
    def capacity(_directory):
        growing = list(operation.workstation.glob("run-*/candidate.sqlite3"))
        return SimpleNamespace(free=1024**3 - 1 if growing else 2 * 1024**3)
    def growing_stage(_root, _packets, candidate, **kwargs):
        candidate.write_bytes(b"Synthetic growing candidate")
        kwargs["progress"]({"added": 1})
        pytest.fail("The low-space progress callback must stop staging.")
    monkeypatch.setattr(runner.shutil, "disk_usage", capacity)
    monkeypatch.setattr(runner.bank, "stage_mac_recovery_bank", growing_stage)
    with pytest.raises(runner.GlobalRepairError):
        runner.run(operation.local_store, operation.workstation, apply=True)
    report = json.loads(next(operation.workstation.glob("run-*/result.json")).read_text())
    assert report["error_code"] == "insufficient_free_space_during_staging"
    assert report["capacity_budget"]["minimum_free_during_staging_bytes"] == 1024**3 - 1
    assert report["published"] is False
    assert operation.publications == []
    assert target.read_bytes() == before_bytes


def test_refresh_uses_old_report_only_for_scratch_and_preserves_current_scope(monkeypatch, tmp_path):
    from app.core import wechat_macos_baseline_copy as synchronization

    operation = _isolated_run(monkeypatch, tmp_path)
    path, prior = _failed(operation)
    prior["before"]["payload_sha256"] = "old sealed baseline"
    runner.write_json(path, prior)
    old_report_bytes = path.read_bytes()
    candidate = path.with_name("candidate.sqlite3")
    candidate.write_bytes(b"Synthetic closed working copy")
    capability = SimpleNamespace(summary={"scratch_path": str(candidate)})
    audit = {"committed": True, "changed_rows": {"projection_revisions": 1}}
    synchronize = Mock(return_value=(capability, audit))
    monkeypatch.setattr(synchronization, "synchronize_baseline_copy", synchronize)

    result = runner.run(
        operation.local_store, operation.workstation, apply=True,
        reuse_failed_report=path, refresh_failed_copy=True,
    )

    report = json.loads(result.read_text())
    assert report["status"] == "verified"
    assert report["baseline_reuse"]["baseline_refresh"] == audit
    assert report["baseline_reuse"]["consumed_by_publication"] is True
    assert path.read_bytes() == old_report_bytes
    assert "scope_sources" not in operation.packet_calls[0]
    assert synchronize.call_args.args == (operation.local_store, candidate)
    assert synchronize.call_args.kwargs["expected_payload_sha256"] == prior["before"]["payload_sha256"]
    assert synchronize.call_args.kwargs["expected_inputs"] == prior["input_identities"]


def test_refresh_committed_audit_survives_a_later_capability_failure(monkeypatch, tmp_path):
    from app.core import wechat_macos_baseline_copy as synchronization

    operation = _isolated_run(monkeypatch, tmp_path)
    path, prior = _failed(operation)
    prior["before"]["payload_sha256"] = "old sealed baseline"
    runner.write_json(path, prior)
    audit = {"committed": True, "after_identity": {"ino": 42}, "already_current": False}
    monkeypatch.setattr(
        synchronization, "synchronize_baseline_copy",
        Mock(side_effect=synchronization.BaselineCopySynchronizationError("fixed_synthetic_code", audit)),
    )
    with pytest.raises(runner.GlobalRepairError):
        runner.run(
            operation.local_store, operation.workstation, apply=True,
            reuse_failed_report=path, refresh_failed_copy=True,
        )
    result = next(item for item in operation.workstation.glob("run-*/result.json") if item != path)
    report = json.loads(result.read_text())
    assert report["baseline_refresh"] == audit
    assert report["failed_phase"] == "verifying_reusable_baseline"
    assert report["published"] is False
    assert operation.packet_calls == operation.publications == []


def test_cli_refresh_requires_explicit_failed_copy_authority(monkeypatch, tmp_path, capsys):
    run = Mock()
    monkeypatch.setattr(runner, "run", run)
    with pytest.raises(SystemExit) as exited:
        runner.main([
            "--local-store", str(tmp_path / "store"),
            "--workstation", str(tmp_path / "workstation"),
            "--refresh-failed-copy", "--apply",
        ])
    assert exited.value.code == 2
    assert "requires --reuse-failed-report" in capsys.readouterr().err
    run.assert_not_called()


def _committed_sync_report(operation):
    path, value = _failed(operation)
    scratch_dir = operation.workstation / "run-prior-derived-copy"
    scratch_dir.mkdir(mode=0o700)
    scratch = scratch_dir / "candidate.sqlite3"
    scratch.write_bytes(b"Synthetic synchronized baseline")
    value.update(
        failed_phase="verifying_reusable_baseline",
        error_type="BaselineCopySynchronizationError",
        baseline_refresh={
            "committed": True, "scratch_path": str(scratch),
            "after_identity": runner.bank._identity(scratch),
            "input_identities": deepcopy(value["input_identities"]),
            "current_payload_sha256": value["before"]["payload_sha256"],
        },
    )
    runner.write_json(path, value)
    return path, scratch, value


def test_resume_committed_sync_uses_strict_factory_without_repeating_sync(monkeypatch, tmp_path):
    from app.core import wechat_macos_baseline_copy as synchronization

    operation = _isolated_run(monkeypatch, tmp_path)
    path, scratch, _value = _committed_sync_report(operation)
    original_report = path.read_bytes()
    scratch.chmod(0o600)
    capability = SimpleNamespace(summary={"scratch_path": str(scratch)})
    verify = Mock(return_value=capability)
    synchronize = Mock(side_effect=AssertionError("Committed synchronization must not repeat."))
    monkeypatch.setattr(runner.bank, "verify_baseline_copy", verify)
    monkeypatch.setattr(synchronization, "synchronize_baseline_copy", synchronize)

    result = runner.run(operation.local_store, operation.workstation, apply=True, reuse_failed_report=path)

    report = json.loads(result.read_text())
    assert report["status"] == "verified"
    assert report["baseline_reuse"]["resumed_committed_synchronization"] is True
    assert verify.call_args.args == (operation.local_store, scratch)
    assert path.read_bytes() == original_report
    assert "scope_sources" not in operation.packet_calls[0]
    synchronize.assert_not_called()


@pytest.mark.parametrize("defect", ("uncommitted", "wrong_error", "old_inputs", "payload", "mtime", "outside_workstation"))
def test_resume_rejects_disconnected_committed_audit_before_factory(monkeypatch, tmp_path, defect):
    operation = _isolated_run(monkeypatch, tmp_path)
    path, _scratch, value = _committed_sync_report(operation)
    audit = value["baseline_refresh"]
    if defect == "uncommitted":
        audit["committed"] = False
    elif defect == "wrong_error":
        value["error_type"] = "UnrelatedError"
    elif defect == "old_inputs":
        audit["input_identities"] = {}
    elif defect == "payload":
        audit["current_payload_sha256"] = "unrelated"
    elif defect == "mtime":
        audit["after_identity"]["mtime_ns"] -= 1
    else:
        audit["scratch_path"] = str(operation.workstation.parent / "candidate.sqlite3")
    runner.write_json(path, value)
    verify = Mock()
    monkeypatch.setattr(runner.bank, "verify_baseline_copy", verify)
    with pytest.raises(runner.GlobalRepairError):
        runner.run(operation.local_store, operation.workstation, apply=True, reuse_failed_report=path)
    verify.assert_not_called()
