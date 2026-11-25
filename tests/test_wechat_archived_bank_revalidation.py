"""A verified compressed preservation authorizes one newly validated bank clone.

Code version: v1.0.1-codex.0
"""

from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core import wechat_macos_recovery_bank as bank
from app.core import wechat_macos_recovery_revalidation as revalidation
from app.core.owner_only_files import ensure_owner_only
from scripts import archive_wechat_rollbacks as archiver
from tests import test_wechat_sealed_bank_revalidation as sealed


mac_archive = sealed.mac_archive
bank_archive = sealed.bank_archive
sealed_case = sealed.sealed_case
clone_document_flags = sealed.clone_document_flags


@pytest.fixture
def archived_case(sealed_case, monkeypatch):
    if shutil.which("zstd") is None:
        pytest.skip("The existing zstd CLI is required for real streamed preservation tests.")
    if os.name == "nt":
        def inspect_writers(path):
            bank._no_baseline_copy_handles(path)
            return []

        monkeypatch.setattr(archiver, "_assert_no_writers", inspect_writers)
    case = sealed_case
    archive = case.directory / "source-preservation.sqlite3.zst"
    record = archiver.archive_file(case.source, archive, level=1, threads=1, minimum_free_bytes=0)
    ensure_owner_only(archive)
    archive.chmod(0o400)
    receipt = Path(record["receipt"])
    ensure_owner_only(receipt)
    case.archive = archive
    case.archive_receipt = receipt
    case.archive_record = record
    case.archive_bytes = archive.read_bytes()
    case.receipt_bytes = receipt.read_bytes()
    return case


def _stage(case, guard, **changes):
    options = {
        "preservation_receipt_path": case.archive_receipt,
        "expected_source_sha256": case.source_sha,
        "expected_payload_sha256": case.payload_sha,
        "publication_guard": guard,
        **changes,
    }
    return revalidation.stage_archived_sealed_mac_recovery_bank_revalidation(
        case.fixture.root, case.source, case.candidate, **options
    )


def _unchanged(case):
    sealed._unchanged(case)
    assert case.archive.read_bytes() == case.archive_bytes
    assert case.archive_receipt.read_bytes() == case.receipt_bytes


def _rewrite_receipt(case, change):
    value = deepcopy(case.archive_record)
    change(value)
    case.archive_receipt.write_text(json.dumps(value))
    case.receipt_bytes = case.archive_receipt.read_bytes()
    return value


def _small_budget(monkeypatch, case, delta=0):
    required = case.source.stat().st_size + 1024**3
    monkeypatch.setattr(revalidation.shutil, "disk_usage", lambda _path: SimpleNamespace(free=required + delta))
    return required


def test_real_archive_verification_authorizes_exactly_one_clone_and_normal_publish(
    archived_case, monkeypatch
):
    case = archived_case
    required = _small_budget(monkeypatch, case)
    verify = Mock(wraps=archiver.verify_receipt)
    monkeypatch.setattr(archiver, "verify_receipt", verify)
    before_paths = set(case.directory.iterdir())
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        receipt = _stage(case, guard)
        assert verify.call_count == 1
        assert verify.call_args.kwargs == {"check_source": False}
        case.clones.assert_called_once_with(case.source, case.candidate)
        assert set(case.directory.iterdir()) == before_paths | {case.candidate}
        assert not case.preserved.exists()
        assert revalidation._source_mode(case.candidate.stat()) == 0o600
        assert receipt["preservation_kind"] == "verified_zstd_full_byte_archive"
        preservation = receipt["preservation"]
        assert preservation["archive_path"] == str(case.archive)
        assert preservation["receipt_path"] == str(case.archive_receipt)
        assert preservation["archive_sha256"] == hashlib.sha256(case.archive_bytes).hexdigest()
        assert preservation["receipt_sha256"] == hashlib.sha256(case.receipt_bytes).hexdigest()
        assert preservation["decompressed_sha256"] == case.source_sha
        assert preservation["decompressed_bytes"] == case.source.stat().st_size
        assert receipt["capacity_budget"] == {
            "stage_required_bytes": required, "free_after_archive_bytes": required,
            "rollback_accounted_as_existing_archive": True, "reserve_bytes": 1024**3,
        }
        schema, rows, metadata = sealed._logical_contents(case.candidate)
        old_schema, old_rows, old_metadata = sealed._logical_contents(case.source)
        assert schema == old_schema and rows == old_rows
        assert {key: value for key, value in metadata.items() if key != "input_identities"} == {
            key: value for key, value in old_metadata.items() if key != "input_identities"
        }
        assert receipt["revalidation"]["historical_byte_equivalence"] == "not_established"
        assert receipt["added"] == receipt["upgraded"] == receipt["reparsed"] == 0
        _unchanged(case)
        bank.publish_mac_recovery_bank(
            case.fixture.root, case.candidate, receipt, publication_guard=guard
        )
    assert not case.candidate.exists()
    assert case.source.read_bytes() == case.before[case.source]
    assert case.archive.read_bytes() == case.archive_bytes
    assert case.archive_receipt.read_bytes() == case.receipt_bytes


@pytest.mark.parametrize("reseal_compressed_hash", (False, True))
def test_corrupt_compressed_bytes_fail_even_when_a_new_hash_is_claimed(
    archived_case, monkeypatch, reseal_compressed_hash
):
    case = archived_case
    before = case.archive.stat()
    verify = Mock(wraps=archiver.verify_receipt)
    monkeypatch.setattr(archiver, "verify_receipt", verify)
    damaged = bytearray(case.archive_bytes)
    damaged[-1] ^= 1
    case.archive.chmod(0o600)
    case.archive.write_bytes(damaged)
    case.archive.chmod(0o400)
    os.utime(case.archive, ns=(before.st_atime_ns, before.st_mtime_ns))
    case.archive_bytes = bytes(damaged)
    if reseal_compressed_hash:
        _rewrite_receipt(case, lambda value: value.update(
            archive_sha256=hashlib.sha256(case.archive_bytes).hexdigest()
        ))
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        with pytest.raises(ValueError):
            _stage(case, guard)
    case.clones.assert_not_called()
    if reseal_compressed_hash:
        assert verify.call_count == 1
    assert not case.candidate.exists()
    _unchanged(case)


@pytest.mark.parametrize("defect", (
    "roundtrip_int", "unchanged_string", "deleted_int", "source_sha", "decompressed_sha", "source_path",
    "source_size", "decompressed_size", "archive_size", "pending_status",
))
def test_receipt_booleans_coordinates_and_final_status_are_independently_verified(
    archived_case, defect
):
    case = archived_case

    def alter(value):
        if defect == "roundtrip_int":
            value["full_roundtrip_verified"] = 1
        elif defect == "unchanged_string":
            value["source_unchanged"] = "true"
        elif defect == "deleted_int":
            value["source_deleted"] = 0
        elif defect == "source_sha":
            value["source_sha256"] = "0" * 64
        elif defect == "decompressed_sha":
            value["decompressed_sha256"] = "0" * 64
        elif defect == "source_path":
            value["source"] = str(case.source.with_name("foreign-source.sqlite3"))
        elif defect == "source_size":
            value["source_identity"]["bytes"] += 1
        elif defect == "decompressed_size":
            value["decompressed_bytes"] += 1
        elif defect == "archive_size":
            value["archive_bytes"] += 1
        else:
            value["status"] = "roundtrip_verified_publication_pending"

    _rewrite_receipt(case, alter)
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        with pytest.raises(ValueError):
            _stage(case, guard)
    case.clones.assert_not_called()
    assert not case.candidate.exists()
    _unchanged(case)


@pytest.mark.parametrize("drift", ("source_bytes", "history"))
def test_source_bytes_and_other_canonical_input_drift_never_use_the_archive_as_authority(
    archived_case, drift
):
    case = archived_case
    if drift == "source_bytes":
        before = case.source.stat()
        raw = bytearray(case.before[case.source])
        raw[-1] ^= 1
        case.source.write_bytes(raw)
        os.utime(case.source, ns=(before.st_atime_ns, before.st_mtime_ns))
        case.before[case.source] = bytes(raw)
    else:
        sealed._ctime_only(case.fixture.history)
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        with pytest.raises(ValueError):
            _stage(case, guard)
    case.clones.assert_not_called()
    _unchanged(case)


def test_verified_archive_does_not_authorize_a_resealed_conflicting_legacy_body(
    archived_case,
):
    case = archived_case

    def changed(packet):
        message = packet["messages"][-1]
        text = "X" + message["text"][1:]
        message["text"] = text
        message["native_text"]["text"] = text
        message["native_text"]["text_components"][0]["text"] = text

    sealed._rewrite_legacy(case, changed)
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        with pytest.raises(ValueError):
            _stage(case, guard)
    case.clones.assert_not_called()
    assert not case.candidate.exists()
    _unchanged(case)


@pytest.mark.parametrize("guard_kind", ("missing", "forged", "expired"))
def test_archived_mode_requires_an_active_publication_guard(archived_case, guard_kind):
    case = archived_case
    if guard_kind == "expired":
        with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
            pass
    else:
        guard = None if guard_kind == "missing" else SimpleNamespace()
    with pytest.raises(ValueError):
        _stage(case, guard)
    case.clones.assert_not_called()
    _unchanged(case)


def test_insufficient_measured_space_keeps_the_verified_archive_without_cloning(
    archived_case, monkeypatch
):
    case = archived_case
    _small_budget(monkeypatch, case, delta=-1)
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        with pytest.raises(ValueError):
            _stage(case, guard)
    case.clones.assert_not_called()
    assert not case.candidate.exists()
    _unchanged(case)


@pytest.mark.parametrize("target,defect", (
    ("archive", "reader"), ("receipt", "reader"),
    ("archive", "hardlink"), ("receipt", "hardlink"),
    ("archive", "mode"), ("receipt", "mode"),
    ("archive", "symlink"), ("receipt", "symlink"),
))
def test_preservation_artifacts_must_remain_private_unaliased_and_closed(
    archived_case, target, defect
):
    case = archived_case
    path = case.archive if target == "archive" else case.archive_receipt
    if defect == "hardlink":
        path.with_name("alias-" + path.name).hardlink_to(path)
    elif defect == "mode":
        sealed._widen_access(path)
    elif defect == "symlink":
        actual = path.with_name("preserved-" + path.name)
        path.rename(actual)
        path.symlink_to(actual)
    with ExitStack() as stack:
        if defect == "reader":
            if os.name == "nt":
                # Windows refuses conflicting writers and permits shared read handles.
                path.chmod(0o600)
                stack.enter_context(path.open("r+b"))
            else:
                stack.enter_context(path.open("rb"))
        with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
            with pytest.raises(ValueError):
                _stage(case, guard)
    case.clones.assert_not_called()
    assert not case.candidate.exists()
    _unchanged(case)


def test_existing_candidate_is_never_overwritten_in_archived_mode(archived_case):
    case = archived_case
    case.candidate.write_bytes(b"Existing synthetic review artifact")
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        with pytest.raises(FileExistsError):
            _stage(case, guard)
    assert case.candidate.read_bytes() == b"Existing synthetic review artifact"
    case.clones.assert_not_called()
    _unchanged(case)


def test_source_ctime_change_during_semantic_preflight_is_proved_and_audited(
    archived_case, monkeypatch
):
    case = archived_case
    compare = revalidation._compare_base
    changed = []

    def drift(*args, **kwargs):
        result = compare(*args, **kwargs)
        sealed._ctime_only(case.source)
        changed.append(True)
        return result

    monkeypatch.setattr(revalidation, "_compare_base", drift)
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        receipt = _stage(case, guard)
    assert changed == [True]
    events = receipt["revalidation"]["source_storage_rebindings"]
    assert events
    assert all(event["file_sha256"] == case.source_sha for event in events)
    _unchanged(case)


def test_archived_mode_metadata_refusal_rolls_back_only_the_new_clone(
    archived_case, monkeypatch
):
    case = archived_case
    invoked = sealed._after_input_update(
        monkeypatch, case,
        lambda connection: connection.execute("INSERT INTO archive_info VALUES('unexpected','synthetic')"),
    )
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        with pytest.raises(ValueError):
            _stage(case, guard)
    assert invoked == [True]
    assert sealed._logical_contents(case.candidate) == sealed._logical_contents(case.source)
    _unchanged(case)


def test_archived_mode_postcommit_failure_preserves_source_and_compressed_evidence(
    archived_case, monkeypatch
):
    case = archived_case
    validate = bank._validate_bank

    def refuse(connection, *args, **kwargs):
        path = next(row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main")
        if Path(path).resolve() == case.candidate.resolve():
            raise ValueError("Synthetic postcommit refusal")
        return validate(connection, *args, **kwargs)

    monkeypatch.setattr(bank, "_validate_bank", refuse)
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        with pytest.raises(ValueError, match="Synthetic postcommit refusal"):
            _stage(case, guard)
    schema, rows, metadata = sealed._logical_contents(case.candidate)
    old_schema, old_rows, old_metadata = sealed._logical_contents(case.source)
    assert schema == old_schema and rows == old_rows
    assert metadata["input_identities"] != old_metadata["input_identities"]
    _unchanged(case)
