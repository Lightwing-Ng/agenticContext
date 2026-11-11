"""The durable Mac bank proof is authenticated and never grants scope authority.

Code version: v1.0.0-codex.0
"""

from contextlib import closing
import os
import sqlite3
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
