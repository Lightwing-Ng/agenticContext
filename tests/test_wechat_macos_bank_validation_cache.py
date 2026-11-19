"""The durable Mac bank proof is authenticated and never grants scope authority.

Code version: v1.1.0-codex.0
"""

from contextlib import closing
from copy import deepcopy
import errno
import hashlib
import os
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from app.core import wechat_macos_bank_validation_cache as proof_cache
from app.core import wechat_macos_recovery_bank as bank
from app.core.wechat_archive import WechatArchiveRepository
from tests import test_wechat_macos_recovery_bank as fixtures


bank_archive = fixtures.bank_archive
mac_archive = fixtures.mac_archive


def _attach(fixture):
    uri = fixture.history.resolve().as_uri() + "?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        return bank._attach(connection, fixture.history)


def _proof(fixture):
    return fixture.history.with_name("browser_cache") / proof_cache._FILENAME


def test_bank_payload_digest_keeps_the_existing_order_and_bytes(bank_archive):
    fixture = bank_archive
    fixtures._publish(fixture)
    with closing(sqlite3.connect(fixture.bank_path)) as connection:
        canonical = hashlib.sha256()
        for table in (
            "sources", "conversation_proofs", "event_keys", "occurrences", "gaps",
            "messages", "conversations", "shadow_replacements",
        ):
            canonical.update(table.encode())
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1,2"):
                canonical.update(bank._json(tuple(row)).encode())
                canonical.update(b"\n")
        assert bank._rows_digest(connection) == canonical.hexdigest()


def test_publication_preserves_a_signed_proof_for_a_fresh_reader(bank_archive, monkeypatch):
    fixture = bank_archive
    candidate, receipt = fixtures._stage(fixture)
    bank.publish_mac_recovery_bank(fixture.root, candidate, receipt, reuse_validation=True)
    assert _proof(fixture).is_file()
    bank._VALIDATED.clear()

    def reject(*args, **kwargs):
        pytest.fail("A fresh reader repeated the complete publication proof.")

    monkeypatch.setattr(bank, "_validate_bank", reject)
    assert _attach(fixture)["schema_version"] == "3"


def test_publication_cannot_persist_a_proof_under_changed_code(bank_archive, monkeypatch):
    fixture = bank_archive
    candidate, receipt = fixtures._stage(fixture)
    monkeypatch.setattr(proof_cache, "_code_fingerprint", lambda: "original")

    def change_code():
        monkeypatch.setattr(proof_cache, "_code_fingerprint", lambda: "changed")

    bank.publish_mac_recovery_bank(
        fixture.root, candidate, receipt, reuse_validation=True, before_replace=change_code,
    )
    assert fixture.bank_path.is_file() and not _proof(fixture).exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires the macOS clone syscall.")
@pytest.mark.parametrize("unsupported_volume", [False, True])
def test_optional_clone_preserves_baseline_and_retains_backup_fallback(
    bank_archive, monkeypatch, tmp_path, unsupported_volume,
):
    fixture = bank_archive
    fixtures._publish(fixture)
    baseline = fixture.bank_path.read_bytes()
    packet = deepcopy(fixture.group)
    packet["messages"].append(fixtures._message(57, 1, raw="Private staged candidate message"))
    fallback, reference = fixtures._stage(fixture, packets=[packet], name="backup-candidate")
    workspace = tmp_path / "private"
    workspace.mkdir(mode=0o700)
    candidate = workspace / "clone-candidate.sqlite3"
    clone = bank._clone_verified_bank
    observed = []
    attempted = []

    def verify_clone(*args):
        cloned = clone(*args)
        observed.append(cloned)
        if cloned:
            assert candidate.read_bytes() == baseline
            assert candidate.stat().st_ino != fixture.bank_path.stat().st_ino
        else:
            assert not candidate.exists()
        return cloned

    monkeypatch.setattr(bank, "_clone_verified_bank", verify_clone)
    if unsupported_volume:
        def unsupported(*args):
            attempted.append(args)
            bank.ctypes.set_errno(errno.ENOTSUP)
            return -1

        monkeypatch.setattr(bank.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(
            fclonefileat=unsupported,
        ))

    receipt = bank.stage_mac_recovery_bank(
        fixture.root, [packet], candidate, account_ids=(fixtures.OWNER,),
        conversation_id=fixture.group_session, clone_baseline=True,
    )
    if not unsupported_volume and observed == [False]:
        pytest.skip("The temporary workspace does not admit APFS clones.")
    assert observed == [not unsupported_volume]
    assert len(attempted) == int(unsupported_volume)
    assert receipt["added"] == reference["added"] == 1
    with closing(sqlite3.connect(candidate)) as staged, closing(sqlite3.connect(fallback)) as copied:
        assert bank._rows_digest(staged) == bank._rows_digest(copied)
    assert fixture.bank_path.read_bytes() == baseline


def test_restart_reuses_whole_bank_validation_but_still_checks_scope(
    bank_archive, monkeypatch
):
    fixture = bank_archive
    fixtures._publish(fixture)
    repository = WechatArchiveRepository(fixture.root)
    parameters = {"session": fixture.group_session, "persist_event_cache": False}
    original = repository.query(**parameters)
    assert original["resource_index_status"] == "absent"
    assert original["count"] == original["current"]["message_count"] == 9
    assert _proof(fixture).is_file()
    bank._VALIDATED.clear()
    bank._SHADOW_VALIDATED.clear()
    original_scope_check = bank._validate_shadow_scope
    scopes = []

    def verify_scope(connection, history, inputs):
        scopes.extend(row[0] for row in connection.execute(
            "SELECT id FROM temp.macos_recovery_bank_scopes"
        ))
        return original_scope_check(connection, history, inputs)

    def unexpected_full_validation(*args, **kwargs):
        raise AssertionError("An unchanged signed whole-bank proof must survive a restart.")

    monkeypatch.setattr(bank, "_validate_shadow_scope", verify_scope)
    monkeypatch.setattr(bank, "_validate_bank", unexpected_full_validation)
    assert repository.query(**parameters) == original
    assert scopes == [fixture.group_session]


@pytest.mark.parametrize(
    "change", ["tamper", "key", "bank", "code", "python", "sqlite", "symlink"],
)
def test_changed_receipt_or_pin_requires_full_validation(bank_archive, monkeypatch, change):
    fixture = bank_archive
    fixtures._publish(fixture)
    expected = _attach(fixture)
    path = _proof(fixture)
    assert path.is_file()
    bank._VALIDATED.clear()

    if change == "tamper":
        encoded = path.read_bytes()
        path.write_bytes(bytes([encoded[0] ^ 1]) + encoded[1:])
    elif change == "key":
        (path.parent / "signing.key").write_bytes(b"k" * 32)
    elif change == "bank":
        replacement = fixture.bank_path.with_suffix(".replacement")
        replacement.write_bytes(fixture.bank_path.read_bytes())
        os.replace(replacement, fixture.bank_path)
    elif change == "code":
        monkeypatch.setattr(proof_cache, "_code_fingerprint", lambda: "changed")
    elif change == "python":
        monkeypatch.setattr(proof_cache, "sys", SimpleNamespace(version="changed"))
    elif change == "sqlite":
        monkeypatch.setattr(proof_cache, "sqlite3", SimpleNamespace(sqlite_version="changed"))
    elif change == "symlink":
        original = path.with_suffix(".saved")
        path.rename(original)
        try:
            path.symlink_to(original)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                pytest.skip("Windows file symlinks require developer mode or elevation.")
            raise

    original_validate = bank._validate_bank
    calls = []

    def validate(*args, **kwargs):
        calls.append(True)
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(bank, "_validate_bank", validate)
    assert _attach(fixture) == expected
    assert calls == [True]
    if change == "symlink":
        assert path.is_symlink()


def test_failed_validation_does_not_create_a_proof(bank_archive, monkeypatch):
    fixture = bank_archive
    fixtures._publish(fixture)

    def reject(*args, **kwargs):
        raise ValueError("Synthetic rejected bank proof.")

    monkeypatch.setattr(bank, "_validate_bank", reject)
    with pytest.raises(ValueError, match="Synthetic rejected bank proof"):
        _attach(fixture)
    assert not _proof(fixture).parent.exists()


def test_input_change_during_validation_does_not_create_a_proof(bank_archive, monkeypatch):
    fixture = bank_archive
    fixtures._publish(fixture)
    original_validate = bank._validate_bank

    def validate_then_change(*args, **kwargs):
        result = original_validate(*args, **kwargs)
        fixture.history.with_name("media_events.sqlite3").touch()
        return result

    monkeypatch.setattr(bank, "_validate_bank", validate_then_change)
    with pytest.raises(ValueError, match="Mac recovery bank changed during validation"):
        _attach(fixture)
    assert not _proof(fixture).parent.exists()


def test_previous_proof_does_not_authorize_new_source_inputs(bank_archive):
    fixture = bank_archive
    fixtures._publish(fixture)
    _attach(fixture)
    path = _proof(fixture)
    proof = path.read_bytes()
    bank._VALIDATED.clear()
    fixture.history.with_name("media_events.sqlite3").touch()

    with pytest.raises(ValueError, match="stale against its original timeline inputs"):
        _attach(fixture)
    assert path.read_bytes() == proof
