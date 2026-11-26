"""Configured keys and memory-only snapshots remain scoped and fail closed.

Code version: v1.1.2-codex.0
"""

from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from app.core.owner_only_files import write_owner_only_text

from app.core import wechat_mac_refresh_cipher as cipher
from app.core import wechat_mac_refresh_source as source
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


@pytest.mark.parametrize("lock_pages", [0, 1])
def test_source_proof_separates_fixed_lock_page_from_authenticated_content(lock_pages):
    count = cipher.LOCK_BYTE_PAGE_NUMBER + 1 if lock_pages else 1
    reference = source.KeyReference(
        OWNER,
        OWNER + "_synthetic",
        "db_storage/message/message_0.db",
        Path("/synthetic/message_0.db"),
    )
    proof = source._source_receipt(
        reference,
        "a" * 64,
        "b" * 64,
        {
            "output_page_count": count,
            "authenticated_output_page_count": count - lock_pages,
            "lock_byte_page_count": lock_pages,
            "encrypted_pages_authenticated": not lock_pages,
            "content_pages_authenticated": True,
        },
    )
    assert proof["page_count"] == count
    assert proof["authenticated_page_count"] == count - lock_pages
    assert proof["authenticated_pages"] is (not lock_pages)
    if lock_pages:
        assert proof["lock_byte_page_count"] == 1
        assert proof["authenticated_content_pages"] is True
    else:
        assert "lock_byte_page_count" not in proof
        assert "authenticated_content_pages" not in proof


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


def _group_sizes(main_bytes, wal_bytes=0, shm_bytes=0):
    def entry(size):
        return (1, 1, size, 1, 1)

    return {
        "": entry(main_bytes),
        "-wal": entry(wal_bytes) if wal_bytes else None,
        "-shm": entry(shm_bytes) if shm_bytes else None,
        "-journal": None,
    }


@pytest.mark.parametrize("main_bytes", [1024**3 + 4096, 2 * 1024**3])
def test_large_sources_with_sufficient_capacity_are_admitted_without_allocating(
    monkeypatch, main_bytes
):
    monkeypatch.setattr(
        source,
        "discover_compute_resources",
        lambda: SimpleNamespace(memory_bytes=64 * 1024**3),
    )
    source._admit_snapshot(_group_sizes(main_bytes))
    assert source.MAX_DATABASE_BYTES == cipher.MAX_DATABASE_BYTES == 2 * 1024**3


@pytest.mark.parametrize(
    ("main_bytes", "wal_bytes", "shm_bytes"),
    [
        (2 * 1024**3 + 4096, 0, 0),
        (4096, cipher.MAX_WAL_BYTES + 1, 0),
        (4096, 0, source.MAX_SHM_BYTES + 1),
    ],
)
def test_oversize_source_groups_fail_before_read_or_decryption(
    registry, monkeypatch, main_bytes, wal_bytes, shm_bytes
):
    keys, _vault, database, _row = registry
    reference = keys.references(OWNER, load_keys=True)[0]
    group = _group_sizes(main_bytes, wal_bytes, shm_bytes)
    monkeypatch.setattr(
        source,
        "identity",
        lambda path, **_kwargs: group[str(path).removeprefix(str(database))],
    )
    monkeypatch.setattr(
        source,
        "read_stable",
        lambda *_args, **_kwargs: pytest.fail("oversize group was read"),
    )
    monkeypatch.setattr(
        cipher,
        "decrypt_database",
        lambda *_args, **_kwargs: pytest.fail("oversize group was decrypted"),
    )
    with pytest.raises(source.RefreshError, match="snapshot size"):
        source.read_database(reference)


@pytest.mark.parametrize("memory_bytes", [None, True, 0, -1, 32.0, "34359738368"])
def test_large_source_capacity_must_be_known_and_well_typed(monkeypatch, memory_bytes):
    monkeypatch.setattr(
        source,
        "discover_compute_resources",
        lambda: SimpleNamespace(memory_bytes=memory_bytes),
    )
    with pytest.raises(source.RefreshError, match="Physical memory"):
        source._admit_snapshot(_group_sizes(1024**3 + 4096))


def test_failed_capacity_probe_cannot_enable_a_large_read(registry, monkeypatch):
    keys, _vault, database, _row = registry
    reference = keys.references(OWNER, load_keys=True)[0]
    group = _group_sizes(1024**3 + 4096)
    monkeypatch.setattr(
        source,
        "identity",
        lambda path, **_kwargs: group[str(path).removeprefix(str(database))],
    )

    def failed_probe():
        raise OSError("synthetic unavailable physical capacity")

    monkeypatch.setattr(source, "discover_compute_resources", failed_probe)
    monkeypatch.setattr(
        source,
        "read_stable",
        lambda *_args, **_kwargs: pytest.fail("unadmitted source was read"),
    )
    with pytest.raises(source.RefreshError, match="Physical memory"):
        source.read_database(reference)


def test_admitted_large_read_uses_pinned_bounds_and_existing_authentication(
    registry, monkeypatch
):
    keys, _vault, database, _row = registry
    reference = keys.references(OWNER, load_keys=True)[0]
    encrypted = database.read_bytes()
    group = _group_sizes(7 * 1024**3 // 4, 4 * 1024**2, 1024**2)
    events = []
    monkeypatch.setattr(
        source,
        "identity",
        lambda path, **_kwargs: group[str(path).removeprefix(str(database))],
    )

    def probe():
        events.append("admission")
        return SimpleNamespace(memory_bytes=32 * 1024**3)

    def bounded_read(path, maximum, **_kwargs):
        suffix = str(path).removeprefix(str(database))
        assert maximum == group[suffix][2]
        events.append(suffix or "main")
        return encrypted if not suffix else b""

    monkeypatch.setattr(source, "discover_compute_resources", probe)
    monkeypatch.setattr(source, "read_stable", bounded_read)
    decrypt = cipher.decrypt_database

    def authenticated(main, wal, key, **kwargs):
        assert kwargs["max_database_bytes"] == 2 * 1024**3
        events.append("authentication")
        return decrypt(main, wal, key, **kwargs)

    monkeypatch.setattr(cipher, "decrypt_database", authenticated)
    snapshot = source.read_database(reference)
    try:
        assert events == ["admission", "main", "-wal", "-shm", "authentication"]
        assert snapshot.connection.execute("PRAGMA user_version").fetchone()[0] == 123
        assert snapshot.source["authenticated_pages"] is True
    finally:
        snapshot.close()


def test_legacy_small_source_does_not_require_capacity_detection(monkeypatch):
    monkeypatch.setattr(
        source,
        "discover_compute_resources",
        lambda: pytest.fail("small source requested large-source capacity"),
    )
    source._admit_snapshot(_group_sizes(1024**3))


def test_large_snapshot_admission_includes_wal_growth_and_overhead(monkeypatch):
    group = _group_sizes(1024**3, 4 * 1024**2, 1024**2)
    output_bound = group[""][2] + group["-wal"][2]
    estimated = (
        4 * output_bound
        + 2 * group["-wal"][2]
        + group["-shm"][2]
        + source.SNAPSHOT_MEMORY_OVERHEAD
    )
    monkeypatch.setattr(
        source,
        "discover_compute_resources",
        lambda: SimpleNamespace(memory_bytes=4 * estimated),
    )
    source._admit_snapshot(group)
    monkeypatch.setattr(
        source,
        "discover_compute_resources",
        lambda: SimpleNamespace(memory_bytes=4 * estimated - 1),
    )
    with pytest.raises(source.RefreshError, match="memory budget"):
        source._admit_snapshot(group)


def test_source_growth_cannot_exceed_the_previously_admitted_read(registry, monkeypatch):
    keys, _vault, database, _row = registry
    reference = keys.references(OWNER, load_keys=True)[0]
    admitted_size = database.stat().st_size
    admit = source._admit_snapshot

    def grow_after_admission(group):
        admit(group)
        with database.open("ab") as output:
            output.write(b"a concurrent page growth")

    monkeypatch.setattr(source, "_admit_snapshot", grow_after_admission)
    monkeypatch.setattr(
        cipher,
        "decrypt_database",
        lambda *_args, **_kwargs: pytest.fail("grown group was decrypted"),
    )
    read = source.read_stable

    def bounded_read(path, maximum, **kwargs):
        assert maximum == admitted_size
        return read(path, maximum, **kwargs)

    monkeypatch.setattr(source, "read_stable", bounded_read)
    with pytest.raises(source.SourceSnapshotChanged, match="changed before it could be read"):
        source.read_database(reference)
