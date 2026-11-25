"""Candidate ctime rebinding requires stable exact bytes and unchanged source pins.

Code version: v1.1.1-codex.0
"""

from contextlib import closing, ExitStack
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
from types import SimpleNamespace

import pytest

from app.core import owner_only_files
from app.core import wechat_macos_recovery_bank as bank
from tests import test_wechat_macos_recovery_bank as fixtures


mac_archive = fixtures.mac_archive
bank_archive = fixtures.bank_archive
_BASIS = "candidate_ctime_only_exact_file_sha256_v1"


@pytest.fixture
def storage_case(bank_archive, tmp_path):
    fixture = bank_archive
    fixtures._publish(fixture, packets=[fixture.direct], name="before-storage-rebind")
    workstation = tmp_path / "private-workstation"
    workstation.mkdir(mode=0o700)
    owner_only_files.ensure_owner_only_directory(workstation)
    directory = workstation / "private-run"
    directory.mkdir(mode=0o700)
    owner_only_files.ensure_owner_only_directory(directory)
    scratch = directory / "baseline.sqlite3"
    with (
        closing(sqlite3.connect(fixture.bank_path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as source,
        closing(sqlite3.connect(scratch)) as target,
    ):
        source.backup(target)
    scratch.chmod(0o644)
    return SimpleNamespace(
        fixture=fixture, scratch=scratch, directory=directory,
        baseline_bytes=fixture.bank_path.read_bytes(),
        history_bytes=fixture.history.read_bytes(), scratch_bytes=scratch.read_bytes(),
    )


def _nonctime(identity):
    return {key: value for key, value in identity.items() if key != "ctime_ns"}


def _ctime_only(path):
    before = bank._identity(path)
    for _attempt in range(3):
        state = path.stat()
        if os.name == "nt":
            os.utime(path, ns=(state.st_atime_ns, state.st_mtime_ns))
        else:
            path.chmod(stat.S_IMODE(state.st_mode))
        after = bank._identity(path)
        if after["ctime_ns"] != before["ctime_ns"]:
            break
    assert _nonctime(after) == _nonctime(before)
    assert after["ctime_ns"] != before["ctime_ns"]
    return before, after


def _widen_access(path, *, directory=False, mode=0o644):
    if os.name == "nt":
        security = owner_only_files._windows_security()
        inherit = "OICI" if directory else ""
        security.set_protected_dacl(
            str(path), f"D:P(A;{inherit};FA;;;{security.current_user_sid()})(A;;FR;;;WD)",
        )
    else:
        path.chmod(0o755 if directory else mode)


def _change_bytes_keep_size_and_mtime(path):
    before = path.stat()
    with path.open("r+b") as stream:
        stream.seek(-1, os.SEEK_END)
        old = stream.read(1)
        stream.seek(-1, os.SEEK_END)
        stream.write(bytes([old[0] ^ 1]))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
    )


def _source_bytes_unchanged(operation):
    assert operation.fixture.bank_path.read_bytes() == operation.baseline_bytes
    assert operation.fixture.history.read_bytes() == operation.history_bytes


def _stage_existing(operation, cap, guard, *, packets=None):
    return bank.stage_mac_recovery_bank(
        operation.fixture.root,
        [operation.fixture.group] if packets is None else packets,
        operation.scratch, account_ids=(fixtures.OWNER,),
        baseline_copy=cap, publication_guard=guard,
    )


def _fresh_candidate(operation):
    candidate = operation.directory / "new-candidate.sqlite3"
    receipt = bank.stage_mac_recovery_bank(
        operation.fixture.root, [operation.fixture.group], candidate,
        account_ids=(fixtures.OWNER,),
    )
    return candidate, receipt


def _after_candidate_validation(monkeypatch, candidate, action):
    original = bank._validate_bank
    invoked = []

    def validate(connection, *args, **kwargs):
        value = original(connection, *args, **kwargs)
        database = next(
            row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main"
        )
        if Path(database).resolve() == candidate.resolve() and not invoked:
            invoked.append(True)
            action(candidate)
        return value

    monkeypatch.setattr(bank, "_validate_bank", validate)
    return invoked


def _assert_audit(records, phase, expected_sha):
    matching = [record for record in records if record["phase"] == phase]
    assert matching
    for record in records:
        assert set(record) == {
            "phase", "basis", "before_identity", "after_identity", "file_sha256",
        }
        assert record["basis"] == _BASIS
        assert record["file_sha256"] == expected_sha
        assert _nonctime(record["before_identity"]) == _nonctime(record["after_identity"])
        assert record["before_identity"]["ctime_ns"] != record["after_identity"]["ctime_ns"]


def test_factory_rebinds_only_ctime_after_full_semantic_validation(storage_case, monkeypatch):
    operation = storage_case
    sha = hashlib.sha256(operation.scratch_bytes).hexdigest()
    invoked = _after_candidate_validation(monkeypatch, operation.scratch, _ctime_only)
    with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
        cap = bank.verify_baseline_copy(
            operation.fixture.root, operation.scratch, publication_guard=guard
        )
        summary = cap.summary
    assert invoked == [True]
    assert summary["file_sha256"] == sha
    _assert_audit(summary["storage_rebindings"], "factory_verification", sha)
    assert operation.scratch.read_bytes() == operation.scratch_bytes
    assert owner_only_files.owner_only_problem(operation.scratch) == ""
    _source_bytes_unchanged(operation)


def test_baseline_reuse_rebinds_ctime_before_writable_open_and_records_proof(storage_case):
    operation = storage_case
    with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
        cap = bank.verify_baseline_copy(
            operation.fixture.root, operation.scratch, publication_guard=guard
        )
        _ctime_only(operation.scratch)
        receipt = _stage_existing(operation, cap, guard)
        assert cap.used
    expected_sha = hashlib.sha256(operation.scratch_bytes).hexdigest()
    assert receipt["baseline_copy"]["file_sha256"] == expected_sha
    _assert_audit(
        receipt["baseline_copy"]["storage_rebindings"], "baseline_reuse", expected_sha
    )
    assert receipt["added"] == len(operation.fixture.group["messages"])
    _source_bytes_unchanged(operation)


def test_reuse_rejects_changed_bytes_even_when_size_and_mtime_are_restored(storage_case):
    operation = storage_case
    with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
        cap = bank.verify_baseline_copy(
            operation.fixture.root, operation.scratch, publication_guard=guard
        )
        _change_bytes_keep_size_and_mtime(operation.scratch)
        modified = operation.scratch.read_bytes()
        with pytest.raises(ValueError):
            _stage_existing(operation, cap, guard)
        assert cap.used is False
    assert operation.scratch.read_bytes() == modified
    _source_bytes_unchanged(operation)


def test_reuse_keeps_canonical_ctime_strict_even_when_candidate_bytes_match(storage_case):
    operation = storage_case
    with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
        cap = bank.verify_baseline_copy(
            operation.fixture.root, operation.scratch, publication_guard=guard
        )
        _ctime_only(operation.fixture.bank_path)
        with pytest.raises(ValueError):
            _stage_existing(operation, cap, guard)
    assert operation.scratch.read_bytes() == operation.scratch_bytes
    _source_bytes_unchanged(operation)


def test_reuse_rehash_requires_a_stable_current_identity(storage_case, monkeypatch):
    operation = storage_case
    original = bank._no_baseline_copy_handles
    inspections = []
    with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
        cap = bank.verify_baseline_copy(
            operation.fixture.root, operation.scratch, publication_guard=guard
        )
        _ctime_only(operation.scratch)

        def unstable(path):
            original(path)
            if Path(path).resolve() == operation.scratch.resolve():
                inspections.append(True)
                if len(inspections) == 2:
                    _ctime_only(operation.scratch)

        monkeypatch.setattr(bank, "_no_baseline_copy_handles", unstable)
        with pytest.raises(ValueError):
            _stage_existing(operation, cap, guard)
        assert cap.used is False
    assert inspections == [True, True]
    assert operation.scratch.read_bytes() == operation.scratch_bytes
    _source_bytes_unchanged(operation)


def test_ctime_drift_inside_writable_begin_is_not_rebound(storage_case, monkeypatch):
    operation = storage_case
    connect = bank.sqlite3.connect
    fired = []

    class ChangedAfterBegin(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            result = super().execute(sql, *args, **kwargs)
            if sql.strip().upper() == "BEGIN EXCLUSIVE":
                fired.append(True)
                _ctime_only(operation.scratch)
            return result

    with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
        cap = bank.verify_baseline_copy(
            operation.fixture.root, operation.scratch, publication_guard=guard
        )

        def observed(*args, **kwargs):
            kwargs["factory"] = ChangedAfterBegin
            return connect(*args, **kwargs)

        monkeypatch.setattr(bank.sqlite3, "connect", observed)
        with pytest.raises(ValueError):
            _stage_existing(operation, cap, guard)
        assert cap.used is False
    assert fired == [True]
    assert cap.summary["storage_rebindings"] == []
    assert operation.scratch.read_bytes() == operation.scratch_bytes
    _source_bytes_unchanged(operation)


@pytest.mark.parametrize("phase", (
    "publication_before_validation", "publication_during_validation",
    "publication_after_validation", "publication_before_replace",
))
def test_publication_rebinds_ctime_with_original_receipt_immutable(
    storage_case, monkeypatch, phase
):
    operation = storage_case
    candidate, receipt = _fresh_candidate(operation)
    original_receipt = deepcopy(receipt)
    callback = None
    if phase == "publication_before_validation":
        _ctime_only(candidate)
    elif phase == "publication_during_validation":
        def change_during_workers(*args, **kwargs):
            _ctime_only(candidate)
            raise bank._ParallelBankChanged("A worker observed changed file metadata.")

        monkeypatch.setattr(bank, "_parallel_bank_checks", change_during_workers)
    elif phase == "publication_after_validation":
        _after_candidate_validation(monkeypatch, candidate, _ctime_only)
    else:
        def callback():
            _ctime_only(candidate)
    with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
        result = bank.publish_mac_recovery_bank(
            operation.fixture.root, candidate, receipt,
            publication_guard=guard, before_replace=callback,
        )
    assert receipt == original_receipt
    assert result["candidate_identity"] == original_receipt["candidate_identity"]
    assert _nonctime(result["published_candidate_identity"]) == _nonctime(receipt["candidate_identity"])
    assert result["published_candidate_identity"]["ctime_ns"] != receipt["candidate_identity"]["ctime_ns"]
    _assert_audit(result["candidate_storage_rebindings"], phase, receipt["candidate_sha256"])
    assert hashlib.sha256(operation.fixture.bank_path.read_bytes()).hexdigest() == receipt["candidate_sha256"]
    assert operation.fixture.history.read_bytes() == operation.history_bytes
    assert not candidate.exists()


def test_publication_rejects_same_size_payload_change_after_validation(storage_case, monkeypatch):
    operation = storage_case
    candidate, receipt = _fresh_candidate(operation)
    original_receipt = deepcopy(receipt)
    invoked = _after_candidate_validation(
        monkeypatch, candidate, _change_bytes_keep_size_and_mtime
    )
    with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
        with pytest.raises(ValueError):
            bank.publish_mac_recovery_bank(
                operation.fixture.root, candidate, receipt, publication_guard=guard
            )
    assert invoked == [True]
    assert receipt == original_receipt and candidate.exists()
    _source_bytes_unchanged(operation)


def test_publication_keeps_source_ctime_strict_after_final_callback(storage_case):
    operation = storage_case
    candidate, receipt = _fresh_candidate(operation)
    original_receipt = deepcopy(receipt)
    with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
        with pytest.raises(ValueError):
            bank.publish_mac_recovery_bank(
                operation.fixture.root, candidate, receipt, publication_guard=guard,
                before_replace=lambda: _ctime_only(operation.fixture.bank_path),
            )
    assert receipt == original_receipt and candidate.exists()
    _source_bytes_unchanged(operation)


@pytest.mark.parametrize("defect", ("reader", "sidecar", "public_parent", "mode", "hardlink"))
def test_publication_ctime_rebind_still_requires_private_unaliased_unopened_file(
    storage_case, defect
):
    operation = storage_case
    candidate, receipt = _fresh_candidate(operation)
    _ctime_only(candidate)
    if defect == "sidecar":
        candidate.with_name(candidate.name + "-journal").touch()
    elif defect == "public_parent":
        _widen_access(candidate.parent, directory=True)
    elif defect == "mode":
        _widen_access(candidate)
    elif defect == "hardlink":
        candidate.with_name("alias.sqlite3").hardlink_to(candidate)
    with ExitStack() as stack:
        if defect == "reader":
            connection = stack.enter_context(closing(sqlite3.connect(candidate)))
            connection.execute("SELECT COUNT(*) FROM messages").fetchone()
        with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
            with pytest.raises(ValueError):
                bank.publish_mac_recovery_bank(
                    operation.fixture.root, candidate, receipt, publication_guard=guard
                )
    _source_bytes_unchanged(operation)
