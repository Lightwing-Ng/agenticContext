"""A stale scratch can advance only through preserved observations and exact journals.

Code version: v1.0.2-codex.0
"""

from contextlib import closing
import json
from copy import deepcopy
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core import wechat_macos_baseline_copy as sync
from app.core import wechat_macos_recovery_bank as bank
from app.core.owner_only_files import ensure_owner_only_directory
from tests import test_wechat_macos_recovery_bank as fixtures
from tests import test_wechat_macos_reparse as repairs


mac_archive = fixtures.mac_archive
bank_archive = fixtures.bank_archive


def _operation(fixture, tmp_path, *, kind="direct", append=False):
    seed_old, seed_new = repairs._packets(fixture, "quote")
    if kind == "app":
        old, new = repairs._app_card_packets(fixture)
    else:
        old, new = repairs._quoted_card_packets(fixture, nested=kind == "nested")
    if kind == "direct":
        for packet in (old, new):
            message = packet["messages"][0]
            packet.update({key: deepcopy(value) for key, value in fixture.direct.items() if key != "messages"})
            message.update(
                sender_id=fixtures.OWNER, sender_label=fixtures.OWNER, direction="sent",
                real_sender_id=1, sender_proof=fixtures._proof(1, fixtures.OWNER),
            )
    stable = deepcopy(fixture.group)
    stable["messages"] = [fixtures._message(98, 1, raw="Old stable event without revisions")]
    fixtures._publish(fixture, packets=[seed_old, old, stable], name="old-before-seed-journal")
    fixtures._publish(fixture, packets=[seed_new], name="old-with-existing-journal")
    with closing(sqlite3.connect(fixture.bank_path)) as connection:
        source_key = connection.execute("SELECT id FROM sources LIMIT 1").fetchone()[0]
        connection.execute(
            "INSERT INTO gaps VALUES(?,?,?,?,?,?,?)",
            ("f" * 64, fixture.group_session, source_key, 1000, "999999", "synthetic_retained_gap", "a" * 64),
        )
        connection.execute("UPDATE archive_info SET value=? WHERE key='payload_sha256'", (bank._rows_digest(connection),))
        connection.commit()
    expected_inputs = bank._inputs(fixture.history)
    with closing(sqlite3.connect(fixture.bank_path)) as connection:
        expected_payload = dict(connection.execute("SELECT key,value FROM archive_info"))["payload_sha256"]
    directory = tmp_path / "private-workstation" / "old-failed-run"
    directory.parent.mkdir(mode=0o700)
    ensure_owner_only_directory(directory.parent)
    directory.mkdir(mode=0o700)
    ensure_owner_only_directory(directory)
    scratch = directory / "candidate.sqlite3"
    with closing(sqlite3.connect(fixture.bank_path)) as source, closing(sqlite3.connect(scratch)) as output:
        source.backup(output)
    scratch.chmod(0o644)
    old_bytes = scratch.read_bytes()
    fixtures._publish(fixture, packets=[new], name="current-one-new-label-journal")
    if append:
        extra = deepcopy(fixture.group)
        extra["messages"] = [fixtures._message(99, 1, raw="New authenticated appended event")]
        fixtures._publish(fixture, packets=[extra], name="current-append")
    return SimpleNamespace(
        fixture=fixture, scratch=scratch, expected_inputs=expected_inputs,
        expected_payload=expected_payload, before=old_bytes, old=old, new=new,
        current_bytes=fixture.bank_path.read_bytes(), history_bytes=fixture.history.read_bytes(),
    )


def _synchronize(operation, guard, **kwargs):
    return sync.synchronize_baseline_copy(
        operation.fixture.root, operation.scratch, publication_guard=guard,
        expected_payload_sha256=kwargs.get("payload", operation.expected_payload),
        expected_inputs=kwargs.get("inputs", operation.expected_inputs),
    )


def _canonical_untouched(operation):
    assert operation.fixture.bank_path.read_bytes() == operation.current_bytes
    assert operation.fixture.history.read_bytes() == operation.history_bytes


@pytest.mark.parametrize("kind", ("app", "direct", "nested"))
def test_one_exact_revision_synchronizes_only_changed_rows_and_yields_reuse_capability(
    bank_archive, tmp_path, monkeypatch, kind
):
    operation = _operation(bank_archive, tmp_path, kind=kind)
    statements = []
    connect = sync.sqlite3.connect

    class Observed(sqlite3.Connection):
        def backup(self, *_args, **_kwargs):
            pytest.fail("Synchronization must never clone the full bank.")

    def observe(*args, **kwargs):
        kwargs["factory"] = Observed
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sync.sqlite3, "connect", observe)
    with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
        cap, audit = _synchronize(operation, guard)
        assert audit["committed"] and not audit["already_current"]
        assert audit["journal_bridged_messages"] == 1
        assert audit["changed_rows"]["messages"] == {"updated": 1, "inserted": 0}
        assert audit["changed_rows"]["projection_revisions"] == {"updated": 0, "inserted": 1}
        assert all(audit["changed_rows"][table]["updated"] == 0 for table in sync._IMMUTABLE)
        assert not any(sql.lstrip().upper().startswith(("DELETE ", "VACUUM", "REPLACE ")) for sql in statements)
        with closing(connect(operation.scratch)) as copied, closing(connect(operation.fixture.bank_path)) as current:
            assert bank._rows_digest(copied) == bank._rows_digest(current)
            assert dict(copied.execute("SELECT key,value FROM archive_info")) == dict(current.execute("SELECT key,value FROM archive_info"))
        _canonical_untouched(operation)
        # The capability is the unchanged bank API's real, guard-bound capability.
        receipt = bank.stage_mac_recovery_bank(
            operation.fixture.root, [operation.new], operation.scratch,
            account_ids=(fixtures.OWNER,), baseline_copy=cap, publication_guard=guard,
        )
        assert receipt["added"] == receipt["reparsed"] == 0


def test_current_append_with_authenticated_occurrence_is_allowed(bank_archive, tmp_path):
    operation = _operation(bank_archive, tmp_path, append=True)
    with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
        _cap, audit = _synchronize(operation, guard)
    assert audit["changed_rows"]["messages"] == {"updated": 1, "inserted": 1}
    assert audit["changed_rows"]["occurrences"]["inserted"] == 1
    assert audit["required_free_bytes"] == (
        len(operation.before)
        + max(0, operation.fixture.bank_path.stat().st_size - audit["working_allocated_before_bytes"])
        + 1024**3
    )
    _canonical_untouched(operation)


def test_appended_then_reparsed_message_keeps_its_historical_occurrence_proof(
    bank_archive, tmp_path
):
    operation = _operation(bank_archive, tmp_path)
    old, new = repairs._app_card_packets(operation.fixture)
    fixtures._publish(operation.fixture, packets=[old], name="appended-old-projection")
    fixtures._publish(operation.fixture, packets=[new], name="appended-new-journal")
    operation.current_bytes = operation.fixture.bank_path.read_bytes()
    with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
        _cap, audit = _synchronize(operation, guard)
    assert audit["changed_rows"]["messages"] == {"updated": 1, "inserted": 1}
    assert audit["changed_rows"]["projection_revisions"]["inserted"] == 2
    _canonical_untouched(operation)


def test_new_conversation_must_match_its_original_archive_binding(bank_archive, tmp_path):
    operation = _operation(bank_archive, tmp_path, kind="app")
    fixtures._publish(operation.fixture, packets=[operation.fixture.direct], name="new-direct-conversation")
    with closing(sqlite3.connect(operation.fixture.bank_path)) as current:
        current.execute(
            "UPDATE conversations SET title='Unproved new title' WHERE id=?",
            (operation.fixture.direct_session,),
        )
        current.execute("UPDATE archive_info SET value=? WHERE key='payload_sha256'", (bank._rows_digest(current),))
        current.commit()
    operation.current_bytes = operation.fixture.bank_path.read_bytes()
    with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
        with pytest.raises(ValueError, match="new_conversation_has_no_matching_binding"):
            _synchronize(operation, guard)
    assert operation.scratch.read_bytes() == operation.before
    _canonical_untouched(operation)


@pytest.mark.parametrize("defect", ("immutable", "missing", "unjournaled", "prior_mismatch", "new_unproved"))
def test_nonmonotone_or_unproved_current_state_never_mutates_old_scratch(
    bank_archive, tmp_path, defect
):
    operation = _operation(bank_archive, tmp_path, append=defect == "new_unproved")
    if defect == "prior_mismatch":
        with closing(sqlite3.connect(operation.scratch)) as old:
            old.execute("UPDATE messages SET search_text='Different old search projection' WHERE text<>? AND id NOT IN (SELECT message_id FROM projection_revisions)", ("Old stable event without revisions",))
            operation.expected_payload = bank._rows_digest(old)
            old.execute("UPDATE archive_info SET value=? WHERE key='payload_sha256'", (operation.expected_payload,))
            old.commit()
        operation.before = operation.scratch.read_bytes()
    with closing(sqlite3.connect(operation.fixture.bank_path)) as current:
        if defect == "immutable":
            current.execute("UPDATE gaps SET reason='changed'")
        elif defect == "missing":
            current.execute("DELETE FROM gaps")
        elif defect == "unjournaled":
            current.execute("UPDATE messages SET sender_label='Unjournaled label' WHERE text='Old stable event without revisions'")
        elif defect == "new_unproved":
            current.execute("DELETE FROM occurrences WHERE server_id='599'")
        current.execute("UPDATE archive_info SET value=? WHERE key='payload_sha256'", (bank._rows_digest(current),))
        current.commit()
    operation.current_bytes = operation.fixture.bank_path.read_bytes()
    with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
        with pytest.raises(ValueError):
            _synchronize(operation, guard)
    assert operation.scratch.read_bytes() == operation.before
    _canonical_untouched(operation)


@pytest.mark.parametrize("defect", ("payload", "inputs", "sidecar", "reader", "space"))
def test_stale_authority_or_unsafe_working_file_is_rejected_before_mutation(
    bank_archive, tmp_path, monkeypatch, defect
):
    operation = _operation(bank_archive, tmp_path)
    options = {}
    reader = None
    if defect == "payload":
        options["payload"] = "f" * 64
    elif defect == "inputs":
        inputs = deepcopy(operation.expected_inputs)
        inputs["history.sqlite3"]["mtime_ns"] += 1
        options["inputs"] = inputs
    elif defect == "sidecar":
        operation.scratch.with_name(operation.scratch.name + "-journal").touch()
    elif defect == "reader":
        reader = sqlite3.connect(operation.scratch)
        reader.execute("SELECT COUNT(*) FROM messages").fetchone()
    else:
        monkeypatch.setattr(sync.shutil, "disk_usage", lambda _path: SimpleNamespace(free=0))
    try:
        with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
            with pytest.raises(ValueError):
                _synchronize(operation, guard, **options)
    finally:
        if reader is not None:
            reader.close()
    assert operation.scratch.read_bytes() == operation.before
    _canonical_untouched(operation)


def test_injected_write_failure_rolls_back_all_changes(bank_archive, tmp_path, monkeypatch):
    operation = _operation(bank_archive, tmp_path)
    apply = sync._apply_differences

    def fail(connection, layout):
        apply(connection, layout)
        raise ValueError("Synthetic private failure detail")

    monkeypatch.setattr(sync, "_apply_differences", fail)
    with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
        with pytest.raises(sync.BaselineCopySynchronizationError) as failed:
            _synchronize(operation, guard)
    assert failed.value.audit_summary["committed"] is False
    assert failed.value.audit_summary["rollback_completed"] is True
    assert "Synthetic private" not in str(failed.value)
    assert operation.scratch.read_bytes() == operation.before
    _canonical_untouched(operation)


def test_rollback_error_exposes_uncertainty_without_native_exception_text(
    bank_archive, tmp_path, monkeypatch
):
    operation = _operation(bank_archive, tmp_path)
    connect = sync.sqlite3.connect

    class RollbackFails(sqlite3.Connection):
        def rollback(self):
            raise sqlite3.OperationalError("Synthetic private rollback detail")

    def connection(*args, **kwargs):
        if "?mode=rw" in str(args[0]):
            kwargs["factory"] = RollbackFails
        return connect(*args, **kwargs)

    monkeypatch.setattr(sync.sqlite3, "connect", connection)
    monkeypatch.setattr(sync, "_apply_differences", Mock(side_effect=ValueError("Synthetic write fault")))
    with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
        with pytest.raises(sync.BaselineCopySynchronizationError) as failed:
            _synchronize(operation, guard)
    assert failed.value.audit_summary["committed"] is False
    assert failed.value.audit_summary["rollback_completed"] is False
    assert str(failed.value) == "baseline_copy_sync_rollback_failed"
    _canonical_untouched(operation)


def test_incomplete_copy_cannot_commit_an_independently_valid_old_payload(
    bank_archive, tmp_path, monkeypatch
):
    operation = _operation(bank_archive, tmp_path)
    monkeypatch.setattr(sync, "_apply_differences", lambda _connection, _layout: {})
    with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
        with pytest.raises(sync.BaselineCopySynchronizationError) as failed:
            _synchronize(operation, guard)
    assert failed.value.audit_summary["committed"] is False
    assert operation.scratch.read_bytes() == operation.before
    _canonical_untouched(operation)


def test_input_change_during_sync_rolls_back_before_commit(bank_archive, tmp_path, monkeypatch):
    operation = _operation(bank_archive, tmp_path)
    apply = sync._apply_differences

    def drift(connection, layout):
        counts = apply(connection, layout)
        operation.fixture.history.touch()
        return counts

    monkeypatch.setattr(sync, "_apply_differences", drift)
    with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
        with pytest.raises(sync.BaselineCopySynchronizationError) as failed:
            _synchronize(operation, guard)
    assert failed.value.audit_summary["committed"] is False
    assert operation.scratch.read_bytes() == operation.before
    _canonical_untouched(operation)


def test_committed_factory_failure_is_audited_and_can_restart_without_payload_writes(
    bank_archive, tmp_path, monkeypatch
):
    operation = _operation(bank_archive, tmp_path)
    verify = bank.verify_baseline_copy
    fail = Mock(side_effect=ValueError("Synthetic post-commit handle"))
    monkeypatch.setattr(bank, "verify_baseline_copy", fail)
    with bank.mac_recovery_publication_guard(operation.fixture.root) as guard:
        with pytest.raises(sync.BaselineCopySynchronizationError) as failed:
            _synchronize(operation, guard)
        assert failed.value.audit_summary["committed"] is True
        assert "Synthetic post-commit" not in str(failed.value)
        diagnostic = failed.value.audit_summary["capability_failure"]
        assert diagnostic["error_type"] == "ValueError"
        assert diagnostic["frames"]
        assert all(set(frame) == {"file", "function", "line"} for frame in diagnostic["frames"])
        assert "Synthetic post-commit" not in json.dumps(failed.value.audit_summary)
        current_copy = operation.scratch.read_bytes()
        monkeypatch.setattr(bank, "verify_baseline_copy", verify)
        monkeypatch.setattr(sync, "_apply_differences", Mock(side_effect=AssertionError("No payload writes on restart")))
        _cap, audit = _synchronize(operation, guard)
        assert audit["already_current"] and audit["committed"]
        assert all(not any(counts.values()) for counts in audit["changed_rows"].values())
        assert operation.scratch.read_bytes() == current_copy
    _canonical_untouched(operation)
