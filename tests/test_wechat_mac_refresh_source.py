"""Configured keys and memory-only snapshots remain scoped and fail closed.

Code version: v1.0.1-codex.0
"""

from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import sys

import pytest

from app.core import wechat_mac_refresh_cipher as cipher
from app.core import wechat_mac_refresh_source as source
from app.core.owner_only_files import write_owner_only_text
from tests.test_wechat_mac_refresh_cipher import KEY, encrypt_page, plain_page


OWNER = "wxid_synthetic_refresh"


@pytest.fixture
def registry(tmp_path, monkeypatch):
    if sys.platform != "darwin":

        def reversible_boundary(key, iv, data, operation):
            cipher._valid_key(key)
            return bytes(
                value ^ key[index % 32] ^ iv[index % 16]
                for index, value in enumerate(data)
            )

        monkeypatch.setattr(cipher, "_aes_cbc", reversible_boundary)
    native = tmp_path / "native"
    directory = OWNER + "_abcd"
    relative = "db_storage/message/message_0.db"
    database = native / directory / relative
    database.parent.mkdir(parents=True)
    database.write_bytes(encrypt_page(plain_page(1, marker=123, count=1), 1))
    vault = tmp_path / "Keys/macos-synthetic/verified-db-keys-v1.jsonl"
    vault.parent.mkdir(parents=True)
    row = {
        "account_directory": directory,
        "relative_path": relative,
        "database_path": str(database),
        "enc_key": KEY.hex(),
        "verification": "synthetic-page-one-authenticated",
    }
    write_owner_only_text(vault, json.dumps(row) + "\n")
    return (
        source.KeyRegistry(key_root=tmp_path / "Keys", native_root=native),
        vault,
        database,
        row,
    )


def test_registry_availability_reads_metadata_without_using_or_disclosing_keys(
    registry, monkeypatch
):
    keys, _vault, database, _row = registry
    monkeypatch.setattr(
        cipher,
        "decrypt_database",
        lambda *_args, **_kwargs: pytest.fail("metadata decrypted a database"),
    )
    public_reference = keys.references(OWNER)[0]
    assert public_reference.database_path == database
    assert public_reference.key is None
    loaded = keys.references(OWNER, load_keys=True)[0]
    assert loaded.key == KEY
    assert KEY.hex() not in repr(loaded)
    assert keys.references("wxid_another_owner") == []
    assert keys.missing_database_count([loaded]) == 0
    (database.parent / "message_1.db").write_bytes(b"not read during availability")
    assert keys.missing_database_count([loaded]) == 1


@pytest.mark.parametrize(
    "change", ["cross_owner", "wrong_path", "invalid_key", "traversal"]
)
def test_registry_rejects_invalid_database_bindings(registry, change):
    keys, vault, _database, row = registry
    if change == "cross_owner":
        row["account_directory"] = "wxid_another_owner_abcd"
    elif change == "wrong_path":
        row["database_path"] += ".another"
    elif change == "invalid_key":
        row["enc_key"] = "incorrect"
    else:
        row["relative_path"] = "../db_storage/message/message_0.db"
    vault.write_text(json.dumps(row) + "\n")
    if change in {"cross_owner", "traversal"}:
        assert keys.references(OWNER) == []
    else:
        with pytest.raises(source.RefreshError):
            keys.references(OWNER)


def test_conflicting_keys_cannot_be_selected_silently(registry):
    keys, vault, _database, row = registry
    another = dict(row, enc_key=bytes(reversed(KEY)).hex())
    vault.write_text(json.dumps(row) + "\n" + json.dumps(another) + "\n")
    with pytest.raises(source.RefreshError, match="conflict"):
        keys.references(OWNER, load_keys=True)


def test_source_stays_readonly_and_plaintext_is_not_written(registry):
    keys, vault, database, _row = registry
    original = database.read_bytes()
    before = {path for path in vault.parents[2].rglob("*") if path.is_file()}
    snapshot = source.read_database(keys.references(OWNER, load_keys=True)[0])
    try:
        assert snapshot.connection.execute("PRAGMA user_version").fetchone()[0] == 123
        assert snapshot.source["authenticated_pages"] is True
        assert (
            snapshot.source["page_count"]
            == snapshot.source["authenticated_page_count"]
            == 1
        )
        assert not snapshot.partial
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            snapshot.connection.execute("CREATE TABLE forbidden(value)")
    finally:
        snapshot.close()
    assert database.read_bytes() == original
    assert before == {path for path in vault.parents[2].rglob("*") if path.is_file()}


def test_each_current_snapshot_authenticates_the_key_and_page(registry):
    keys, _vault, database, _row = registry
    reference = keys.references(OWNER, load_keys=True)[0]
    with pytest.raises(cipher.RecoveryError, match="authentication"):
        source.read_database(replace(reference, key=bytes(reversed(KEY))))
    data = bytearray(database.read_bytes())
    data[100] ^= 1
    database.write_bytes(data)
    with pytest.raises(cipher.RecoveryError, match="authentication"):
        source.read_database(reference)


def test_live_group_drift_is_rejected_before_decryption(registry, monkeypatch):
    keys, _vault, database, _row = registry
    reference = keys.references(OWNER, load_keys=True)[0]
    read = source.read_stable

    def changed(path, maximum, **kwargs):
        data = read(path, maximum, **kwargs)
        if Path(path) == database:
            Path(str(database) + "-wal").write_bytes(b"a concurrent WAL appeared")
        return data

    monkeypatch.setattr(source, "read_stable", changed)
    monkeypatch.setattr(
        cipher,
        "decrypt_database",
        lambda *_args, **_kwargs: pytest.fail("unstable group decrypted"),
    )
    with pytest.raises(source.RefreshError, match="changed"):
        source.read_database(reference)


def test_nonempty_rollback_journal_and_symlinks_are_rejected(registry):
    keys, _vault, database, _row = registry
    reference = keys.references(OWNER, load_keys=True)[0]
    journal = Path(str(database) + "-journal")
    journal.write_bytes(b"uncommitted rollback journal")
    with pytest.raises(source.RefreshError, match="journal"):
        source.read_database(reference)
    journal.unlink()
    alternate = database.with_name("symbolic.db")
    alternate.symlink_to(database)
    with pytest.raises(source.RefreshError, match="symlink"):
        source.read_database(replace(reference, database_path=alternate))
