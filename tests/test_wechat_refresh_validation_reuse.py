"""Refresh reuses exact bank proofs without trusting a changed archive.

Code version: v1.2.0-codex.0
"""

from copy import deepcopy
import errno
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from app.core.wechat_mac_refresh import WechatMacRefreshService
from app.core import wechat_macos_bank_validation_cache as proof_cache
from app.core import wechat_macos_recovery_bank as bank
from tests import test_wechat_macos_recovery_bank as fixtures

bank_archive = fixtures.bank_archive
mac_archive = fixtures.mac_archive
NEW_MESSAGE = "Synthetic new message from the selected conversation"


def _stage_group_update(fixture):
    packet = deepcopy(fixture.group)
    packet["messages"] = [fixtures._message(57, 1, raw=NEW_MESSAGE)]
    candidate = fixture.history.with_name("scoped-update.sqlite3")
    receipt = bank.stage_mac_recovery_bank(
        fixture.root,
        [packet],
        candidate,
        account_ids=(fixtures.OWNER,),
        conversation_id=fixture.group_session,
    )
    return candidate, receipt


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires the macOS clone syscall.")
@pytest.mark.parametrize("unsupported_volume", [False, True])
def test_optional_clone_preserves_sqlite_baseline_and_has_backup_fallback(
    bank_archive, monkeypatch, tmp_path, unsupported_volume,
):
    fixture = bank_archive
    fixtures._publish(fixture)
    baseline = fixture.bank_path.read_bytes()
    fallback, fallback_receipt = _stage_group_update(fixture)
    workspace = tmp_path / "private"
    workspace.mkdir(mode=0o700)
    candidate = workspace / "candidate.sqlite3"
    called = []
    unsupported_calls = []
    clone = bank._clone_verified_bank

    def observe_clone(*args):
        cloned = clone(*args)
        called.append(cloned)
        if cloned:
            assert candidate.read_bytes() == baseline
            assert candidate.stat().st_ino != fixture.bank_path.stat().st_ino
        else:
            assert not candidate.exists()
        return cloned

    monkeypatch.setattr(bank, "_clone_verified_bank", observe_clone)
    if unsupported_volume:
        def unsupported(*args):
            unsupported_calls.append(args)
            bank.ctypes.set_errno(errno.ENOTSUP)
            return -1

        monkeypatch.setattr(bank.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(
            fclonefileat=unsupported,
        ))
    packet = deepcopy(fixture.group)
    packet["messages"] = [fixtures._message(57, 1, raw=NEW_MESSAGE)]
    receipt = bank.stage_mac_recovery_bank(
        fixture.root, [packet], candidate, account_ids=(fixtures.OWNER,),
        conversation_id=fixture.group_session, clone_baseline=True,
    )
    if not unsupported_volume and called == [False]:
        pytest.skip("The temporary workspace does not admit APFS clones.")
    assert called == [not unsupported_volume]
    assert len(unsupported_calls) == int(unsupported_volume)
    assert receipt["added"] == fallback_receipt["added"] == 1
    with sqlite3.connect(candidate) as cloned, sqlite3.connect(fallback) as copied:
        assert bank._rows_digest(cloned) == bank._rows_digest(copied)
    assert fixture.bank_path.read_bytes() == baseline


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires the macOS clone syscall.")
def test_optional_clone_rejects_changed_canonical_pin_before_mutating_candidate(
    bank_archive, monkeypatch, tmp_path,
):
    fixture = bank_archive
    fixtures._publish(fixture)
    workspace = tmp_path / "private"
    workspace.mkdir(mode=0o700)
    candidate = workspace / "candidate.sqlite3"
    baseline = fixture.bank_path.read_bytes()
    prior = fixture.bank_path.stat()

    def clone_with_drift(*args):
        candidate.write_bytes(baseline)
        candidate.chmod(0o600)
        os.utime(candidate, ns=(prior.st_atime_ns, prior.st_mtime_ns))
        os.utime(fixture.bank_path, ns=(prior.st_atime_ns, prior.st_mtime_ns + 1_000))
        return 0

    monkeypatch.setattr(bank.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(
        fclonefileat=clone_with_drift,
    ))
    packet = deepcopy(fixture.group)
    packet["messages"] = [fixtures._message(57, 1, raw=NEW_MESSAGE)]
    with pytest.raises(ValueError, match="did not retain its verified baseline"):
        bank.stage_mac_recovery_bank(
            fixture.root, [packet], candidate, account_ids=(fixtures.OWNER,),
            conversation_id=fixture.group_session, clone_baseline=True,
        )
    assert candidate.read_bytes() == fixture.bank_path.read_bytes() == baseline


def _track_full_validation(monkeypatch):
    original = bank._validate_bank
    calls = []

    def validate(connection, *args, **kwargs):
        database = next(
            row[2]
            for row in connection.execute("PRAGMA database_list")
            if row[1] == "main"
        )
        calls.append(Path(database).resolve())
        return original(connection, *args, **kwargs)

    monkeypatch.setattr(bank, "_validate_bank", validate)
    return calls


def test_refresh_stages_an_update_from_the_signed_baseline_proof(
    bank_archive, monkeypatch
):
    fixture = bank_archive
    fixtures._publish(fixture)
    assert fixtures._detail(fixture, fixture.group_session)["count"] == 9
    assert (fixture.history.parent / "browser_cache" / proof_cache._FILENAME).is_file()
    original_bank = fixture.bank_path.read_bytes()
    original_history = fixture.history.read_bytes()
    monkeypatch.setattr(bank, "_VALIDATED", {})
    validated = _track_full_validation(monkeypatch)

    candidate, receipt = _stage_group_update(fixture)

    assert validated == []
    assert receipt["added"] == 1 and candidate.is_file()
    assert fixture.bank_path.read_bytes() == original_bank
    assert fixture.history.read_bytes() == original_history


def test_refresh_reads_the_new_message_after_one_candidate_validation(
    bank_archive, monkeypatch
):
    fixture = bank_archive
    fixtures._publish(fixture)
    candidate, receipt = _stage_group_update(fixture)
    validated = _track_full_validation(monkeypatch)
    original_history = fixture.history.read_bytes()

    bank.publish_mac_recovery_bank(
        fixture.root, candidate, receipt, reuse_validation=True
    )
    monkeypatch.setattr(bank, "_VALIDATED", {})
    detail = fixtures._detail(fixture, fixture.group_session)

    assert validated == [candidate.resolve()]
    assert detail["count"] == 10
    assert [row["text"] for row in detail["messages"]].count(NEW_MESSAGE) == 1
    assert fixture.history.read_bytes() == original_history


def test_refresh_readback_revalidates_a_changed_published_bank(
    bank_archive, monkeypatch
):
    fixture = bank_archive
    fixtures._publish(fixture)
    candidate, receipt = _stage_group_update(fixture)
    validated = _track_full_validation(monkeypatch)

    bank.publish_mac_recovery_bank(
        fixture.root, candidate, receipt, reuse_validation=True
    )
    monkeypatch.setattr(bank, "_VALIDATED", {})
    with sqlite3.connect(fixture.bank_path) as connection:
        updated = connection.execute(
            "UPDATE messages SET text=? WHERE text=?", ("changed", NEW_MESSAGE)
        )
        assert updated.rowcount == 1
    detail = fixtures._detail(fixture, fixture.group_session)

    assert validated == [candidate.resolve(), fixture.bank_path.resolve()]
    assert detail["resource_index_status"] == "invalid"
    assert detail["count"] == 2
    assert "changed" not in {row["text"] for row in detail["messages"]}


def test_publication_does_not_sign_a_result_under_changed_verifier_code(bank_archive, monkeypatch):
    fixture = bank_archive
    fixtures._publish(fixture)
    candidate, receipt = _stage_group_update(fixture)
    validated = _track_full_validation(monkeypatch)
    verifier_code = {"fingerprint": "before"}
    monkeypatch.setattr(proof_cache, "_code_fingerprint", lambda: verifier_code["fingerprint"])

    bank.publish_mac_recovery_bank(
        fixture.root, candidate, receipt, reuse_validation=True,
        before_replace=lambda: verifier_code.update(fingerprint="after"),
    )
    monkeypatch.setattr(bank, "_VALIDATED", {})

    assert fixtures._detail(fixture, fixture.group_session)["count"] == 10
    assert validated == [candidate.resolve(), fixture.bank_path.resolve()]


def test_scoped_staging_rejects_another_conversation_from_the_same_account(
    bank_archive,
):
    fixture = bank_archive
    fixtures._publish(fixture)
    original_bank = fixture.bank_path.read_bytes()

    with pytest.raises(ValueError, match="conversation"):
        bank.stage_mac_recovery_bank(
            fixture.root,
            [fixture.direct],
            fixture.history.with_name("wrong-conversation.sqlite3"),
            account_ids=(fixtures.OWNER,),
            conversation_id=fixture.group_session,
        )

    assert fixture.bank_path.read_bytes() == original_bank


def test_refresh_can_rebind_ctime_with_public_archive_directory(bank_archive, monkeypatch):
    fixture = bank_archive
    fixtures._publish(fixture)
    fixture.history.parent.chmod(0o755)
    stage = bank.stage_mac_recovery_bank
    full_sha = bank._file_sha
    candidate_scans = []

    def track_candidate_scan(path, **kwargs):
        if path.name == "candidate.sqlite3":
            candidate_scans.append(path)
        return full_sha(path, **kwargs)

    def stage_with_ctime_drift(*args, **kwargs):
        receipt = stage(*args, **kwargs)
        receipt["candidate_identity"]["ctime_ns"] -= 1
        return receipt

    monkeypatch.setattr(bank, "stage_mac_recovery_bank", stage_with_ctime_drift)
    monkeypatch.setattr(bank, "_file_sha", track_candidate_scan)
    packet = deepcopy(fixture.group)
    packet["messages"] = [fixtures._message(57, 1, raw=NEW_MESSAGE)]
    service = WechatMacRefreshService(fixture.root)
    result = service._publish([packet], fixtures.OWNER, bank._inputs(fixture.history))

    assert result["added"] == 1
    assert result["candidate_storage_rebindings"][0]["phase"] == "publication_before_validation"
    assert len(candidate_scans) == 1  # Staging hashes the candidate before the ctime rebind.
    assert fixtures._detail(fixture, fixture.group_session)["count"] == 10
    assert not list(fixture.history.parent.glob(".mac-refresh-*"))
