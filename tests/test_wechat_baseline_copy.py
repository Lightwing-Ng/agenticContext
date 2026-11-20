"""Existing working copies require exact, single-use proof before baseline reuse.

Code version: v1.0.0-codex.0
"""

from contextlib import closing
import os
import sqlite3
import stat
import subprocess
from threading import Thread
from types import SimpleNamespace

import pytest

from app.core import wechat_macos_recovery_bank as bank
from tests import test_wechat_macos_recovery_bank as fixtures


mac_archive = fixtures.mac_archive
bank_archive = fixtures.bank_archive


@pytest.fixture
def baseline_copy(bank_archive, tmp_path):
    fixture = bank_archive
    fixtures._publish(fixture, packets=[fixture.direct], name="published-baseline")
    workstation = tmp_path / "private-workstation"
    workstation.mkdir(mode=0o700)
    operation = workstation / "failed-run"
    operation.mkdir(mode=0o700)
    candidate = operation / "candidate.sqlite3"
    with closing(sqlite3.connect(fixture.bank_path)) as source, closing(sqlite3.connect(candidate)) as output:
        source.backup(output)
    candidate.chmod(0o644)
    return SimpleNamespace(
        fixture=fixture, candidate=candidate,
        scratch_before=candidate.read_bytes(), bank_before=fixture.bank_path.read_bytes(),
        history_before=fixture.history.read_bytes(),
    )


def _unchanged(operation, *, scratch=True):
    assert operation.fixture.bank_path.read_bytes() == operation.bank_before
    assert operation.fixture.history.read_bytes() == operation.history_before
    if scratch:
        assert operation.candidate.read_bytes() == operation.scratch_before


def _stage(operation, cap, guard, packets=None):
    fixture = operation.fixture
    return bank.stage_mac_recovery_bank(
        fixture.root, [fixture.group] if packets is None else packets,
        operation.candidate, account_ids=(fixtures.OWNER,),
        baseline_copy=cap, publication_guard=guard,
    )


def test_existing_candidate_still_requires_explicit_verified_capability(baseline_copy):
    operation = baseline_copy
    with pytest.raises(ValueError, match="new candidate path"):
        bank.stage_mac_recovery_bank(
            operation.fixture.root, [operation.fixture.group], operation.candidate,
            account_ids=(fixtures.OWNER,),
        )
    _unchanged(operation)
    assert stat.S_IMODE(operation.candidate.stat().st_mode) == 0o644


def test_verified_reuse_matches_fresh_staging_skips_backup_and_checks_integrity_once(
    baseline_copy, monkeypatch
):
    operation = baseline_copy
    fixture = operation.fixture
    fresh, expected = fixtures._stage(fixture, packets=[fixture.group], name="fresh-reference")
    backups, statements = [], []
    connect = bank.sqlite3.connect

    class Observed(sqlite3.Connection):
        def backup(self, *args, **kwargs):
            backups.append(True)
            return super().backup(*args, **kwargs)

    def observed(*args, **kwargs):
        kwargs["factory"] = Observed
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    with bank.mac_recovery_publication_guard(fixture.root) as guard:
        cap = bank.verify_baseline_copy(fixture.root, operation.candidate, publication_guard=guard)
        assert stat.S_IMODE(operation.candidate.stat().st_mode) == 0o600
        summary = cap.summary
        with closing(connect(fixture.bank_path)) as baseline:
            metadata = dict(baseline.execute("SELECT key,value FROM archive_info"))
        assert summary["payload_sha256"] == metadata["payload_sha256"]
        assert summary["scratch_path"] == str(operation.candidate)
        assert summary["candidate_identity"] == bank._identity(operation.candidate)
        assert summary["reused_clone_credit_bytes"] == min(
            fixture.bank_path.stat().st_size,
            operation.candidate.stat().st_blocks * 512,
        )
        summary["input_identities"].clear()
        assert cap.summary["input_identities"]
        monkeypatch.setattr(bank.sqlite3, "connect", observed)
        receipt = _stage(operation, cap, guard)
        assert cap.used
        assert backups == []
        assert sum(sql.strip().lower() == "pragma integrity_check" for sql in statements) == 1
        assert receipt["added"] == expected["added"]
        assert receipt["upgraded"] == receipt["reparsed"] == 0
        assert not any(operation.candidate.with_name(operation.candidate.name + suffix).exists() for suffix in ("-journal", "-wal", "-shm"))
        with closing(connect(fresh)) as first, closing(connect(operation.candidate)) as second:
            assert bank._rows_digest(first) == bank._rows_digest(second)
            assert dict(first.execute("SELECT key,value FROM archive_info")) == dict(second.execute("SELECT key,value FROM archive_info"))
        _unchanged(operation, scratch=False)
        bank.publish_mac_recovery_bank(fixture.root, operation.candidate, receipt, publication_guard=guard)
    assert not operation.candidate.exists()
    assert fixture.history.read_bytes() == operation.history_before


@pytest.mark.parametrize("defect", ("payload", "resealed_payload", "metadata", "schema"))
def test_verification_rejects_every_nonbaseline_content_before_permission_changes(
    baseline_copy, defect
):
    operation = baseline_copy
    with closing(sqlite3.connect(operation.candidate)) as connection:
        if defect == "schema":
            connection.execute("CREATE TABLE unexpected(value TEXT)")
        elif defect == "metadata":
            connection.execute("INSERT INTO archive_info VALUES('unexpected','not-the-baseline')")
        else:
            connection.execute("UPDATE messages SET text='Different synthetic projection'")
            if defect == "resealed_payload":
                connection.execute(
                    "UPDATE archive_info SET value=? WHERE key='payload_sha256'",
                    (bank._rows_digest(connection),),
                )
        connection.commit()
    before = operation.candidate.read_bytes()
    with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
        with pytest.raises(ValueError):
            bank.verify_baseline_copy(operation.fixture.root, operation.candidate, publication_guard=guard)
    assert operation.candidate.read_bytes() == before
    assert stat.S_IMODE(operation.candidate.stat().st_mode) == 0o644
    _unchanged(operation, scratch=False)


@pytest.mark.parametrize("defect", ("symlink", "hardlink", "public_parent", "sidecar", "sidecar_symlink"))
def test_reuse_requires_an_independent_private_resident_working_file(baseline_copy, defect):
    operation = baseline_copy
    if defect == "symlink":
        actual = operation.candidate.with_name("preserved.sqlite3")
        operation.candidate.rename(actual)
        operation.candidate.symlink_to(actual)
    elif defect == "hardlink":
        operation.candidate.with_name("alias.sqlite3").hardlink_to(operation.candidate)
    elif defect == "public_parent":
        operation.candidate.parent.chmod(0o755)
    elif defect == "sidecar":
        operation.candidate.with_name(operation.candidate.name + "-journal").touch()
    else:
        operation.candidate.with_name(operation.candidate.name + "-journal").symlink_to("absent-journal-target")
    with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
        with pytest.raises(ValueError):
            bank.verify_baseline_copy(operation.fixture.root, operation.candidate, publication_guard=guard)
    _unchanged(operation)


def test_every_active_sqlite_reader_blocks_reuse_verification(baseline_copy):
    operation = baseline_copy
    with closing(sqlite3.connect(operation.candidate)) as reader:
        reader.execute("SELECT COUNT(*) FROM messages").fetchone()
        with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
            with pytest.raises(ValueError, match="open handle"):
                bank.verify_baseline_copy(operation.fixture.root, operation.candidate, publication_guard=guard)
    _unchanged(operation)


@pytest.mark.parametrize("result", (None, "timeout", "stderr", "bad_status"))
def test_uncertain_handle_inspection_never_authorizes_reuse(baseline_copy, monkeypatch, result):
    operation = baseline_copy
    if result is None:
        monkeypatch.setattr(bank.shutil, "which", lambda _command: None)
    else:
        def inspect(*_args, **_kwargs):
            if result == "timeout":
                raise subprocess.TimeoutExpired("lsof", 30)
            return SimpleNamespace(returncode=2 if result == "bad_status" else 1, stdout="", stderr="uncertain" if result == "stderr" else "")
        monkeypatch.setattr(bank.subprocess, "run", inspect)
    with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
        with pytest.raises(ValueError):
            bank.verify_baseline_copy(operation.fixture.root, operation.candidate, publication_guard=guard)
    _unchanged(operation)


@pytest.mark.parametrize("defect", ("forged", "path", "history", "process", "guard"))
def test_capability_cannot_be_forged_or_borrowed_for_another_scope(
    baseline_copy, monkeypatch, defect, tmp_path
):
    operation = baseline_copy
    fixture = operation.fixture
    with bank.mac_recovery_publication_guard(fixture.root) as guard:
        cap = bank.verify_baseline_copy(fixture.root, operation.candidate, publication_guard=guard)
        candidate, root, observed_guard = operation.candidate, fixture.root, guard
        if defect == "forged":
            cap = SimpleNamespace(**cap.summary)
        elif defect == "path":
            candidate = operation.candidate.with_name("unproved.sqlite3")
        elif defect == "history":
            root = tmp_path / "another-store"
        elif defect == "process":
            actual = os.getpid()
            monkeypatch.setattr(bank.os, "getpid", lambda: actual + 1)
        else:
            observed_guard = SimpleNamespace()
        with pytest.raises((ValueError, OSError)):
            bank.stage_mac_recovery_bank(
                root, [fixture.group], candidate, account_ids=(fixtures.OWNER,),
                baseline_copy=cap, publication_guard=observed_guard,
            )
    _unchanged(operation)


def test_expired_publication_guard_invalidates_a_verified_copy(baseline_copy):
    operation = baseline_copy
    with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
        cap = bank.verify_baseline_copy(operation.fixture.root, operation.candidate, publication_guard=guard)
    with pytest.raises(ValueError, match="guard is inactive"):
        _stage(operation, cap, guard)
    _unchanged(operation)


def test_a_new_valid_guard_cannot_inherit_an_old_guards_capability(baseline_copy):
    operation = baseline_copy
    with bank.mac_recovery_publication_guard(operation.fixture.root) as original:
        cap = bank.verify_baseline_copy(operation.fixture.root, operation.candidate, publication_guard=original)
    with bank.mac_recovery_publication_guard(operation.fixture.root) as replacement:
        with pytest.raises(ValueError, match="inactive or out of scope"):
            _stage(operation, cap, replacement)
    _unchanged(operation)


def test_wrong_thread_fails_without_waiting_on_the_owner_guard(baseline_copy):
    operation = baseline_copy
    failures = []
    with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
        cap = bank.verify_baseline_copy(operation.fixture.root, operation.candidate, publication_guard=guard)

        def borrow():
            try:
                _stage(operation, cap, guard)
            except Exception as error:
                failures.append(error)

        thread = Thread(target=borrow)
        thread.start()
        thread.join(2)
        assert not thread.is_alive()
    assert len(failures) == 1 and isinstance(failures[0], ValueError)
    _unchanged(operation)


@pytest.mark.parametrize("drift", ("scratch", "canonical"))
def test_identity_drift_after_verification_rejects_before_any_staging_write(baseline_copy, drift):
    operation = baseline_copy
    with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
        cap = bank.verify_baseline_copy(operation.fixture.root, operation.candidate, publication_guard=guard)
        (operation.candidate if drift == "scratch" else operation.fixture.bank_path).touch()
        with pytest.raises(ValueError, match="changed before reuse"):
            _stage(operation, cap, guard)
    _unchanged(operation)


def test_exclusive_sqlite_lease_survives_temp_schema_commits_and_failure(
    baseline_copy,
):
    operation = baseline_copy
    fixture = operation.fixture

    def packets():
        # Stage has already executed its TEMP schema before advancing this iterator.
        with closing(sqlite3.connect(operation.candidate, timeout=0)) as competitor:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competitor.execute("SELECT COUNT(*) FROM messages").fetchone()
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competitor.execute("BEGIN EXCLUSIVE")
        yield fixture.group
        raise ValueError("Synthetic failure after new rows")

    with bank.mac_recovery_publication_guard(fixture.root) as guard:
        cap = bank.verify_baseline_copy(fixture.root, operation.candidate, publication_guard=guard)
        with pytest.raises(ValueError, match="Synthetic failure"):
            _stage(operation, cap, guard, packets())
        assert cap.used
        with pytest.raises(ValueError, match="inactive or out of scope"):
            _stage(operation, cap, guard)
        # A failed transaction needs a fresh full verification and starts counters
        # at zero rather than inheriting attempted additions from the old run.
        renewed = bank.verify_baseline_copy(fixture.root, operation.candidate, publication_guard=guard)
        receipt = _stage(operation, renewed, guard)
        assert receipt["added"] == len(fixture.group["messages"])
        assert receipt["existing"] == 0
    _unchanged(operation, scratch=False)
