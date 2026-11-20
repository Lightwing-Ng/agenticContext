"""Long publication leases remain local, active, and backed by the same lock inode.

Code version: v1.1.2-codex.0
"""

import hashlib
import os
import sqlite3
from threading import Thread
from types import SimpleNamespace

import pytest

from app.core import wechat_macos_recovery_bank as bank
from tests import test_wechat_macos_recovery_bank as fixtures
from tests import test_wechat_macos_reparse as repairs


mac_archive = fixtures.mac_archive
bank_archive = fixtures.bank_archive


@pytest.fixture
def staged_publication(bank_archive):
    fixture = bank_archive
    fixtures._publish(fixture, packets=[fixture.direct], name="before-long-lease")
    candidate, receipt = fixtures._stage(
        fixture, packets=[fixture.group], name="guarded-candidate"
    )
    return SimpleNamespace(
        fixture=fixture, candidate=candidate, receipt=receipt,
        bank_before=fixture.bank_path.read_bytes(),
        history_before=fixture.history.read_bytes(),
    )


def _assert_preserved(operation):
    assert operation.fixture.bank_path.read_bytes() == operation.bank_before
    assert operation.fixture.history.read_bytes() == operation.history_before
    assert operation.candidate.is_file()


def test_active_guard_publishes_without_reacquiring_its_file_lock(staged_publication):
    operation = staged_publication
    fixture = operation.fixture

    with bank.mac_recovery_publication_guard(fixture.root) as guard:
        assert bank._require_publication_guard(guard, fixture.history) is guard
        result = bank.publish_mac_recovery_bank(
            fixture.root, operation.candidate, operation.receipt,
            publication_guard=guard,
        )
        assert bank._require_publication_guard(guard, fixture.history) is guard

    assert result["bank_sha256"] == operation.receipt["candidate_sha256"]
    assert hashlib.sha256(fixture.bank_path.read_bytes()).hexdigest() == result["bank_sha256"]
    assert fixture.history.read_bytes() == operation.history_before
    assert not operation.candidate.exists()


def test_expired_guard_cannot_publish(staged_publication):
    operation = staged_publication
    with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
        pass

    with pytest.raises(ValueError, match="guard is inactive or out of scope"):
        bank.publish_mac_recovery_bank(
            operation.fixture.root, operation.candidate, operation.receipt,
            publication_guard=guard,
        )

    _assert_preserved(operation)


def test_guard_cannot_authorize_another_history(staged_publication, tmp_path):
    operation = staged_publication
    another_root = tmp_path / "another-local-store"

    with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
        with pytest.raises(ValueError, match="guard is inactive or out of scope"):
            bank.publish_mac_recovery_bank(
                another_root, operation.candidate, operation.receipt,
                publication_guard=guard,
            )

    assert not another_root.exists()
    _assert_preserved(operation)


def test_wrong_thread_is_rejected_before_waiting_on_the_creator_lock(staged_publication):
    operation = staged_publication
    results = []

    def borrowed_publish(guard):
        try:
            bank.publish_mac_recovery_bank(
                operation.fixture.root, operation.candidate, operation.receipt,
                publication_guard=guard,
            )
        except Exception as error:
            results.append(error)

    with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
        worker = Thread(target=borrowed_publish, args=(guard,))
        worker.start()
        worker.join(timeout=2)
        waited_for_owner = worker.is_alive()
    worker.join(timeout=2)

    assert not waited_for_owner, "A borrowed guard waited behind its creator's RLock."
    assert not worker.is_alive()
    assert len(results) == 1 and isinstance(results[0], ValueError)
    assert "guard is inactive or out of scope" in str(results[0])
    _assert_preserved(operation)


def test_guard_cannot_be_reused_after_a_process_identity_change(staged_publication, monkeypatch):
    operation = staged_publication
    actual_pid = os.getpid()

    with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
        with monkeypatch.context() as changed_process:
            changed_process.setattr(bank.os, "getpid", lambda: actual_pid + 1)
            with pytest.raises(ValueError, match="guard is inactive or out of scope"):
                bank.publish_mac_recovery_bank(
                    operation.fixture.root, operation.candidate, operation.receipt,
                    publication_guard=guard,
                )

    _assert_preserved(operation)


def test_ordinary_publisher_cannot_bypass_an_active_guard(staged_publication):
    operation = staged_publication

    with bank.mac_recovery_publication_guard(operation.fixture.root):
        with pytest.raises(ValueError, match="Another process is publishing"):
            bank.publish_mac_recovery_bank(
                operation.fixture.root, operation.candidate, operation.receipt
            )

    _assert_preserved(operation)
    bank.publish_mac_recovery_bank(
        operation.fixture.root, operation.candidate, operation.receipt
    )


@pytest.mark.skipif(os.name == "nt", reason="Windows prevents renaming an open lock file.")
def test_guard_rejects_replacement_of_its_named_lock_inode(staged_publication):
    operation = staged_publication
    lock = operation.fixture.history.with_name(bank._PUBLICATION_LOCK_NAME)

    with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
        lock.rename(lock.with_name("preserved-original-lock"))
        lock.touch(mode=0o600)
        with pytest.raises(ValueError, match="owner-private and unchanged"):
            bank.publish_mac_recovery_bank(
                operation.fixture.root, operation.candidate, operation.receipt,
                publication_guard=guard,
            )

    _assert_preserved(operation)


def test_final_callback_cannot_release_the_guard_and_then_publish(staged_publication):
    operation = staged_publication
    manager = bank.mac_recovery_publication_guard(operation.fixture.root)
    guard = manager.__enter__()
    released = []

    def release_guard():
        manager.__exit__(None, None, None)
        released.append(True)

    try:
        with pytest.raises(ValueError, match="guard is inactive or out of scope"):
            bank.publish_mac_recovery_bank(
                operation.fixture.root, operation.candidate, operation.receipt,
                publication_guard=guard, before_replace=release_guard,
            )
    finally:
        if not released:
            manager.__exit__(None, None, None)

    assert released == [True]
    _assert_preserved(operation)
    bank.publish_mac_recovery_bank(
        operation.fixture.root, operation.candidate, operation.receipt
    )


@pytest.mark.skipif(os.name == "nt", reason="Windows prevents renaming an open lock file.")
def test_final_callback_lock_replacement_is_rechecked_before_replace(staged_publication):
    operation = staged_publication
    lock = operation.fixture.history.with_name(bank._PUBLICATION_LOCK_NAME)

    def replace_lock():
        lock.rename(lock.with_name("callback-preserved-original-lock"))
        lock.touch(mode=0o600)

    with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
        with pytest.raises(ValueError, match="owner-private and unchanged"):
            bank.publish_mac_recovery_bank(
                operation.fixture.root, operation.candidate, operation.receipt,
                publication_guard=guard, before_replace=replace_lock,
            )

    _assert_preserved(operation)


@pytest.mark.parametrize("schema", ("3", "4"))
def test_guarded_publication_records_the_current_writer(bank_archive, schema):
    fixture = bank_archive
    if schema == "4":
        old, new = repairs._packets(fixture, "quote")
        fixtures._publish(fixture, packets=[old], name="writer-tag-before-repair")
        packets = [new]
    else:
        packets = [fixture.group]
    candidate, receipt = fixtures._stage(fixture, packets=packets, name="writer-tag-candidate")

    with bank.mac_recovery_publication_guard(fixture.root) as guard:
        bank.publish_mac_recovery_bank(
            fixture.root, candidate, receipt, publication_guard=guard
        )
    with sqlite3.connect(fixture.bank_path) as connection:
        metadata = dict(connection.execute("SELECT key,value FROM archive_info"))

    assert metadata["schema_version"] == schema
    assert metadata["code_version"] == bank.WRITER_VERSION
