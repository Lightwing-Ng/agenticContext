"""Exercise exact-content compaction and source-bound optional sidecar rebinding.

Code version: v1.2.1-codex.0
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

import pytest


_SCRIPT = Path(__file__).parents[1] / "scripts" / "compact_wechat_sqlite.py"
_SPEC = importlib.util.spec_from_file_location("compact_wechat_sqlite", _SCRIPT)
compactor = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(compactor)


@pytest.fixture
def archive(tmp_path):
    runtime = tmp_path / "runtime"
    workstation = tmp_path / "workstation"
    runtime.mkdir()
    workstation.mkdir()
    database = runtime / "history.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE partitions(id TEXT PRIMARY KEY,account_key TEXT,account_id TEXT);"
            "INSERT INTO partitions VALUES('source','account','owner');"
            "CREATE TABLE messages(id TEXT PRIMARY KEY,text TEXT,evidence BLOB);"
            "CREATE INDEX message_text ON messages(text);"
            "CREATE TABLE duplicates(value TEXT,number REAL);"
            "INSERT INTO duplicates VALUES('preserve duplicate',1.5),('preserve duplicate',1.5);"
            "PRAGMA user_version=7; PRAGMA application_id=9;"
        )
        connection.executemany(
            "INSERT INTO messages VALUES(?,?,?)",
            [(str(i), "Synthetic provenance " + "x" * (1000 + i % 100), bytes([i % 256])) for i in range(500)],
        )
        connection.execute("DELETE FROM messages WHERE CAST(id AS INTEGER)%7=0")
    return database, workstation


def _prepare(archive, *, page_size=32768):
    database, workstation = archive
    candidate = workstation / (database.name + ".candidate")
    receipt = workstation / (database.name + ".json")
    return compactor.prepare(database, candidate, receipt, page_size=page_size), receipt


@pytest.mark.parametrize("page_size", [16384, 32768])
def test_vacuum_into_preserves_typed_rows_duplicates_schema_and_source_bytes(archive, page_size):
    source, _workstation = archive
    original = compactor.sha256(source)
    report, receipt = _prepare(archive, page_size=page_size)
    assert report["statistics"]["page_size"] == page_size
    assert report["statistics"]["freelist_count"] == 0
    assert report["logical_content"]["tables"]["duplicates"]["rows"] == 2
    assert report["logical_content"]["headers"]["user_version"] == 7
    assert compactor.sha256(source) == original
    assert compactor.verify(receipt)["candidate_sha256"] == report["candidate_sha256"]


@pytest.mark.parametrize("changed", ["database", "candidate"])
def test_source_or_candidate_drift_blocks_verification(archive, changed):
    report, receipt = _prepare(archive)
    with sqlite3.connect(report[changed]) as connection:
        connection.execute("INSERT INTO duplicates VALUES('New independent evidence',2.0)")
    with pytest.raises(ValueError, match="changed"):
        compactor.verify(receipt)


def test_existing_output_and_nonempty_sqlite_sidecars_are_rejected(archive):
    database, workstation = archive
    candidate = workstation / "candidate.sqlite3"
    candidate.write_bytes(b"Preserve this independent file")
    with pytest.raises(ValueError, match="new"):
        compactor.prepare(database, candidate, workstation / "receipt.json")
    journal = Path(str(database) + "-wal")
    journal.write_bytes(b"An unfinished writer")
    with pytest.raises(ValueError, match="writer"):
        compactor.prepare(database, workstation / "new.sqlite3", workstation / "new.json")


def _sidecar(archive, *, stale=False, bad_partitions=False):
    history, workstation = archive
    path = history.with_name("resource_references.sqlite3")
    with compactor.readonly(history) as connection:
        rows = connection.execute("SELECT id,account_key,account_id FROM partitions ORDER BY id").fetchall()
    pin = compactor.identity(history)
    if stale:
        pin["inode"] += 1
    fingerprint = compactor.hashlib.sha256(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE archive_info(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
            "CREATE TABLE messages(id TEXT PRIMARY KEY,text TEXT);"
            "INSERT INTO messages VALUES('source-observation','Preserve every resource observation');"
            "CREATE TABLE conversations(id TEXT PRIMARY KEY);"
        )
        connection.executemany("INSERT INTO archive_info VALUES(?,?)", [
            ("schema_version", "1"),
            ("history_file_identity", json.dumps(pin)),
            ("history_partitions_sha256", "wrong" if bad_partitions else fingerprint),
            ("unrelated_evidence", "Unchanged exact original value"),
        ])
    return path, workstation


@pytest.mark.parametrize("stale,bad_partitions", [(False, False), (True, False), (False, True)])
def test_rebind_preserves_all_content_and_never_promotes_stale_sidecars(archive, stale, bad_partitions):
    _history_report, history_receipt = _prepare(archive)
    sidecar = _sidecar(archive, stale=stale, bad_partitions=bad_partitions)
    sidecar_report, receipt = _prepare(sidecar)
    result = compactor.rebind_sidecar(receipt, history_receipt)
    verified = compactor.verify(receipt)
    if stale or bad_partitions:
        assert "stale_unchanged" in result["status"]
        assert verified["candidate_sha256"] == sidecar_report["candidate_sha256"]
        assert not verified["intentional_changes"]
    else:
        assert set(verified["intentional_changes"]) == {"history_file_identity"}
        assert json.loads(verified["intentional_changes"]["history_file_identity"]["after"]) == compactor.identity(_history_report["candidate"])


@pytest.mark.skipif(sys.platform != "darwin", reason="APFS compression is an explicit macOS feature.")
def test_filesystem_compression_keeps_sqlite_content_and_reduces_actual_allocation(archive):
    database, workstation = archive
    original = compactor.sha256(database)
    candidate = workstation / "compressed.sqlite3"
    receipt = workstation / "compressed.json"
    report = compactor.prepare(database, candidate, receipt, hfs_compression=True)
    compression = report["hfs_compression"]
    assert compression["compressed_flag"]
    assert compression["allocated_bytes_after"] < compression["allocated_bytes_before"]
    assert candidate.stat().st_flags & 0x20
    assert compactor.sha256(database) == original
    assert compactor.verify(receipt)["logical_content"] == report["logical_content"]


def test_filesystem_compression_rejects_other_platforms_before_writing(archive, monkeypatch):
    database, workstation = archive
    monkeypatch.setattr(compactor.sys, "platform", "unsupported-platform")
    with pytest.raises(ValueError, match="macOS"):
        compactor.prepare(database, workstation / "no-output.sqlite3", workstation / "no-output.json", hfs_compression=True)
    assert not list(workstation.iterdir())


def test_existing_plain_vacuum_output_can_be_adopted_without_another_copy(archive, monkeypatch):
    original, _receipt = _prepare(archive)
    source, workstation = archive
    candidate = Path(original["candidate"])
    before = compactor.identity(candidate)
    receipt = workstation / "adopted.json"
    monkeypatch.setattr(compactor.subprocess, "run", lambda *args, **kwargs: pytest.fail("Adoption must not run compression."))
    adopted = compactor.adopt_existing(source, candidate, receipt, compression_status="unsupported_recorded")
    assert adopted["preparation_method"] == "adopt_existing_vacuum_output"
    assert adopted["hfs_compression"]["status"] == "unsupported_recorded"
    assert not adopted["hfs_compression"]["enabled"]
    assert compactor.identity(candidate) == before
    assert compactor.verify(receipt)["logical_content"] == original["logical_content"]


@pytest.mark.parametrize("changed", ["logical_row", "failed_pending"])
def test_adoption_rejects_modified_content_and_failed_compression_output(archive, changed):
    report, _receipt = _prepare(archive)
    source, workstation = archive
    candidate = Path(report["candidate"])
    if changed == "logical_row":
        with sqlite3.connect(candidate) as connection:
            connection.execute("INSERT INTO duplicates VALUES('Independent unexpected evidence',3.0)")
    else:
        pending = candidate.with_name(candidate.name + ".hfs-pending")
        candidate.rename(pending)
        candidate = pending
    with pytest.raises(ValueError):
        compactor.adopt_existing(source, candidate, workstation / "rejected.json", compression_status="unsupported_recorded")
    assert not (workstation / "rejected.json").exists()


def test_adoption_rejects_a_hardlink_to_the_live_database_before_reading_payloads(archive, monkeypatch):
    source, workstation = archive
    alias = workstation / "source-alias.sqlite3"
    alias.hardlink_to(source)
    original = source.read_bytes()
    receipt = workstation / "rejected-alias.json"
    monkeypatch.setattr(compactor, "sha256", lambda path: pytest.fail("An inode alias reached payload hashing."))
    with pytest.raises(ValueError, match="distinct|independent"):
        compactor.adopt_existing(source, alias, receipt, page_size=4096)
    assert source.read_bytes() == original
    assert not receipt.exists()


def test_legacy_hardlinked_candidate_cannot_rebind_the_live_sidecar(archive):
    history_report, history_receipt = _prepare(archive)
    sidecar, workstation = _sidecar(archive)
    original = sidecar.read_bytes()
    report, receipt = _prepare((sidecar, workstation))
    candidate = Path(report["candidate"])
    candidate.unlink()
    candidate.hardlink_to(sidecar)
    report.update(
        version="v1.2.0-codex.0",
        candidate_identity=compactor.identity(candidate),
        candidate_sha256=compactor.sha256(candidate),
    )
    receipt.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="distinct|independent"):
        compactor.rebind_sidecar(receipt, history_receipt)
    assert sidecar.read_bytes() == original
    with compactor.readonly(sidecar) as connection:
        pin = connection.execute("SELECT value FROM archive_info WHERE key='history_file_identity'").fetchone()[0]
    assert json.loads(pin) == history_report["source_identity"]


@pytest.mark.parametrize("action", ["prepare", "adopt"])
def test_case_aliased_runtime_directory_cannot_be_a_workstation(archive, action):
    source, _workstation = archive
    alias = source.parent.with_name(source.parent.name.upper())
    aliases_runtime = alias.exists() and alias.samefile(source.parent)
    if not alias.exists():
        alias.mkdir()
    stage = alias / "new-stage"
    stage.mkdir()
    candidate, receipt = stage / "candidate.sqlite3", stage / "receipt.json"
    original = source.read_bytes()
    if action == "adopt":
        candidate.write_bytes(original)

    def run():
        if action == "adopt":
            return compactor.adopt_existing(source, candidate, receipt, page_size=4096)
        return compactor.prepare(source, candidate, receipt)

    if aliases_runtime:
        with pytest.raises(ValueError, match="external"):
            run()
        assert not receipt.exists()
    else:
        assert run()["status"] == "prepared_not_published"
    assert source.read_bytes() == original


def test_known_ditto_size_cap_rejects_before_hashing_or_creating_a_second_large_copy(tmp_path, monkeypatch):
    candidate = tmp_path / "large-synthetic-file"
    with candidate.open("wb") as stream:
        stream.truncate(512 * 1024**2 + 1)
    monkeypatch.setattr(compactor.sys, "platform", "darwin")
    monkeypatch.setattr(compactor, "sha256", lambda path: pytest.fail("Do not read a known oversized candidate."))
    monkeypatch.setattr(compactor.subprocess, "run", lambda *args, **kwargs: pytest.fail("Do not create another oversized copy."))
    with pytest.raises(ValueError, match="512 MiB"):
        compactor._compress_path(candidate)
    assert not candidate.with_name(candidate.name + ".hfs-pending").exists()


def _remount_fixture(archive, monkeypatch):
    history, _history_receipt = _prepare(archive)
    sidecar = _sidecar(archive)
    _sidecar_report, sidecar_receipt = _prepare(sidecar)
    history_receipt = Path(history["candidate"]).with_name("history.sqlite3.json")
    compactor.rebind_sidecar(sidecar_receipt, history_receipt)
    original_identity = compactor.identity

    def remounted_identity(path):
        result = original_identity(path)
        return {**result, "dev": result["dev"] + 1}

    monkeypatch.setattr(compactor, "identity", remounted_identity)
    output = archive[1].parent / "mount-refresh"
    output.mkdir()
    return [history_receipt, sidecar_receipt], output, original_identity


def test_mount_refresh_verifies_bytes_and_rebinds_only_candidate_pin(archive, monkeypatch):
    receipts, output, _original_identity = _remount_fixture(archive, monkeypatch)
    old_receipt_bytes = [path.read_bytes() for path in receipts]
    old_reports = [json.loads(value) for value in old_receipt_bytes]
    original_sources = [compactor.sha256(report["database"]) for report in old_reports]
    result = compactor.refresh_mount(receipts, output)
    fresh = [compactor.verify(item["receipt"]) for item in result["receipts"]]
    assert result["status"] == "mount_refresh_prepared_not_published"
    assert [path.read_bytes() for path in receipts] == old_receipt_bytes
    assert [compactor.sha256(report["database"]) for report in fresh] == original_sources
    assert fresh[0]["source_identity"]["dev"] == old_reports[0]["source_identity"]["dev"] + 1
    assert compactor.source_pin_identities(fresh[0]) == [fresh[0]["source_identity"], old_reports[0]["source_identity"]]
    change = fresh[1]["intentional_changes"]["history_file_identity"]
    assert change["before"] == old_reports[1]["intentional_changes"]["history_file_identity"]["before"]
    assert json.loads(change["after"]) == fresh[0]["candidate_identity"]


@pytest.mark.parametrize("failure", ["inode", "nonuniform_device", "candidate_bytes", "stale_pin"])
def test_mount_refresh_rejects_independent_drift_before_any_candidate_change(archive, monkeypatch, failure):
    receipts, output, original_identity = _remount_fixture(archive, monkeypatch)
    reports = [json.loads(path.read_text()) for path in receipts]
    candidate = Path(reports[1]["candidate"])
    if failure in {"inode", "nonuniform_device"}:
        def drifted_identity(path):
            result = original_identity(path)
            result["dev"] += 1
            if Path(path) == candidate:
                result["inode" if failure == "inode" else "dev"] += 1
            return result
        monkeypatch.setattr(compactor, "identity", drifted_identity)
    elif failure == "candidate_bytes":
        state = candidate.stat()
        with sqlite3.connect(candidate) as connection:
            connection.execute("UPDATE messages SET text=replace(text,'resource','RESOURCE')")
        compactor.os.utime(candidate, ns=(state.st_atime_ns, state.st_mtime_ns))
    else:
        report = reports[1]
        pin = json.loads(report["intentional_changes"]["history_file_identity"]["before"])
        pin["inode"] += 1
        report["intentional_changes"]["history_file_identity"]["before"] = json.dumps(pin)
        receipts[1].write_text(json.dumps(report))
    before = [compactor.sha256(report["candidate"]) for report in reports]
    with pytest.raises(ValueError):
        compactor.refresh_mount(receipts, output)
    assert not list(output.iterdir())
    assert [compactor.sha256(report["candidate"]) for report in reports] == before


def test_mount_refresh_rejects_a_new_hardlink_to_a_candidate_before_rebinding(archive, monkeypatch):
    receipts, output, _original_identity = _remount_fixture(archive, monkeypatch)
    reports = [json.loads(path.read_text()) for path in receipts]
    candidate = Path(reports[1]["candidate"])
    alias = candidate.with_name("retained-independent-alias.sqlite3")
    alias.hardlink_to(candidate)
    before = candidate.read_bytes()
    with pytest.raises(ValueError, match="distinct|independent"):
        compactor.refresh_mount(receipts, output)
    assert candidate.read_bytes() == alias.read_bytes() == before
    assert not list(output.iterdir())
