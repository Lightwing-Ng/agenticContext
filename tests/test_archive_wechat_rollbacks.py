"""Exercise source preservation, failed publication, and upload-proof boundaries.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import archive_wechat_rollbacks as archiver


@pytest.fixture
def rollback(tmp_path):
    source = tmp_path / "rollback.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE evidence(body TEXT)")
        connection.execute("INSERT INTO evidence VALUES(?)", ("Immutable synthetic evidence.\n" * 1000,))
    workstation = tmp_path / "workstation"
    workstation.mkdir()
    return source, workstation / "rollback.sqlite3.zst"


@pytest.fixture
def preserved(rollback, monkeypatch):
    if shutil.which("zstd") is None:
        pytest.skip("The real zstd CLI is required for this integration fixture.")
    monkeypatch.setattr(archiver, "_assert_no_writers", lambda path: [])
    source, target = rollback
    return source, target, archiver.archive_file(source, target)


def test_real_zstd_roundtrip_retains_source_and_rechecks_both_hashes(preserved):
    source, target, report = preserved
    receipt = Path(report["receipt"])
    assert source.exists()
    assert report["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert report["archive_sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert report["source_deleted"] is False
    assert report["cloud_upload_verified"] is False
    assert report["remote_content_hash_verified"] is False
    assert archiver.verify_receipt(receipt)["source_checked"] is True
    target.write_bytes(target.read_bytes()[:-8])
    with pytest.raises(ValueError, match="differs from its receipt"):
        archiver.verify_receipt(receipt)
    assert source.exists()


def test_source_change_retains_partial_artifact_and_transaction(rollback, monkeypatch):
    if shutil.which("zstd") is None:
        pytest.skip("The real zstd CLI is required.")
    source, target = rollback
    monkeypatch.setattr(archiver, "_assert_no_writers", lambda path: [])
    original = archiver._verify_compressed

    def mutate_after_verification(path, zstd):
        result = original(path, zstd)
        with source.open("ab") as stream:
            stream.write(b"Concurrent source change")
        return result

    monkeypatch.setattr(archiver, "_verify_compressed", mutate_after_verification)
    with pytest.raises(ValueError, match="identity changed"):
        archiver.archive_file(source, target)
    assert not target.exists()
    assert source.exists()
    transactions = list(target.parent.glob("*.pending.json"))
    assert len(transactions) == 1
    transaction = json.loads(transactions[0].read_text())
    assert transaction["status"] == "failed_source_retained"
    assert Path(transaction["pending_archive"]).exists()


def test_existing_destination_is_never_overwritten(rollback):
    source, target = rollback
    target.write_bytes(b"Existing evidence")
    with pytest.raises(ValueError, match="already exists"):
        archiver.archive_file(source, target)
    assert target.read_bytes() == b"Existing evidence"
    assert not list(target.parent.glob("*.pending*"))


def test_source_symlink_and_dataless_file_are_refused(rollback, monkeypatch):
    source, target = rollback
    alias = source.with_name("alias.sqlite3")
    alias.symlink_to(source)
    with pytest.raises(ValueError, match="regular file"):
        archiver.archive_file(alias, target)
    old_lstat = Path.lstat

    def dataless(path):
        value = old_lstat(path)
        if path != source:
            return value
        return SimpleNamespace(st_mode=value.st_mode, st_flags=archiver.DATALESS)

    monkeypatch.setattr(Path, "lstat", dataless)
    with pytest.raises(ValueError, match="placeholders"):
        archiver.archive_file(source, target)
    assert not target.exists()


def test_write_handle_gate_allows_only_fileprovider(rollback, monkeypatch):
    source, _ = rollback
    response = SimpleNamespace(returncode=0, stderr="", stdout="p12\ncfileproviderd\nf4\naw\n")
    monkeypatch.setattr(archiver.subprocess, "run", lambda *args, **kwargs: response)
    assert archiver._assert_no_writers(source)[0]["command"] == "fileproviderd"
    response.stdout += "p13\ncsqlite3\nf5\nau\n"
    with pytest.raises(ValueError, match="open for writing"):
        archiver._assert_no_writers(source)


def test_nonempty_sqlite_wal_is_not_silently_omitted(rollback, monkeypatch):
    source, _ = rollback
    monkeypatch.setattr(archiver.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stderr="", stdout=""))
    Path(str(source) + "-wal").write_bytes(b"Pending transaction")
    with pytest.raises(ValueError, match="separate preservation"):
        archiver._assert_no_writers(source)


def test_real_lsof_detects_a_process_holding_the_source_for_writing(rollback):
    if shutil.which("lsof") is None:
        pytest.skip("The native lsof CLI is required.")
    source, _ = rollback
    holder = subprocess.Popen(
        [sys.executable, "-c", "import sys; f=open(sys.argv[1], 'rb+'); print('ready', flush=True); sys.stdin.read(1)", str(source)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "ready"
        with pytest.raises(ValueError, match="open for writing"):
            archiver._assert_no_writers(source)
    finally:
        holder.communicate("x", timeout=10)


def test_provider_success_is_preliminary_and_cannot_authorize_local_release(preserved, monkeypatch):
    _, target, report = preserved
    receipt = Path(report["receipt"])
    calls = []

    def state(path):
        calls.append(path)
        return {
            "documentSize": path.stat().st_size,
            **{key: int(key == "isUploaded") for key in archiver.UPLOAD_FIELDS},
            "upload_error_present": False,
        }

    monkeypatch.setattr(archiver, "_provider_state", state)
    result = archiver.check_upload(receipt)
    assert result["status"] == "provider_uploads_reported_cloud_unverified"
    assert result["all_provider_uploads_reported"] is True
    assert all(row["provider_upload_reported"] for row in result["checks"])
    assert calls == [target, receipt]
    assert result["cloud_preservation_verified"] is False
    assert result["safe_to_release_local_content"] is False
    assert result["remote_content_hash_verified"] is False
    monkeypatch.setattr(archiver, "_provider_state", lambda path: {**state(path), "isUploading": 1})
    assert archiver.check_upload(receipt)["status"] == "provider_uploads_pending_or_unverified"
    monkeypatch.setattr(archiver, "_provider_state", state)
    target.write_bytes(b"X" * target.stat().st_size)
    changed = archiver.check_upload(receipt)
    assert changed["status"] == "provider_uploads_pending_or_unverified"
    assert changed["checks"][0]["archive_identity_matches_receipt"] is False


def test_provider_fields_missing_or_error_remain_unverified(monkeypatch, tmp_path):
    response = SimpleNamespace(returncode=0, stdout="isUploaded = 1;\nuploadingError = failure;\n", stderr="")
    monkeypatch.setattr(archiver.subprocess, "run", lambda *args, **kwargs: response)
    state = archiver._provider_state(tmp_path / "explicit-file")
    assert state["isUploading"] is None
    assert state["upload_error_present"] is True


def test_legacy_preservation_receipt_still_verifies_original_archive_format(preserved):
    _, _, report = preserved
    path = Path(report["receipt"])
    report["version"] = "v1.0.0-codex.0"
    path.write_text(json.dumps(report))
    assert archiver.verify_receipt(path)["status"] == "full_roundtrip_verified"


def test_upload_cli_returns_unverified_even_when_provider_reports_success(preserved, monkeypatch, capsys):
    _, _, report = preserved
    monkeypatch.setattr(archiver, "_provider_state", lambda path: {
        "documentSize": path.stat().st_size,
        **{key: int(key == "isUploaded") for key in archiver.UPLOAD_FIELDS},
        "upload_error_present": False,
    })
    assert archiver.main(["check-upload", report["receipt"]]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["all_provider_uploads_reported"] is True
    assert output["cloud_preservation_verified"] is False
    assert output["safe_to_release_local_content"] is False
