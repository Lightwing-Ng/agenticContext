"""Verify metadata-only OneDrive inspection, exact size binding, and exclusions.

Code version: v1.3.0-codex.0
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import verify_wechat_onedrive as inspector


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    public = tmp_path / "public"
    cache = tmp_path / "cache"
    root = public / "workstation"
    root.mkdir(parents=True)
    cache.mkdir()
    monkeypatch.setattr(inspector, "PUBLIC_ROOT", public)
    monkeypatch.setattr(inspector, "CACHE_ROOT", cache)
    return root, cache


def _uploaded(path):
    return {
        "documentSize": path.lstat().st_size,
        **{key: int(key == "isUploaded") for key in inspector.archiver.UPLOAD_FIELDS},
        "upload_error_present": False,
    }


def test_canonical_mapping_is_exact_and_rejects_sibling_prefix(workspace):
    root, cache = workspace
    assert inspector.canonical_cache_path(root / "payload.zst") == cache / "workstation/payload.zst"
    with pytest.raises(ValueError):
        inspector.canonical_cache_path(root.parent.with_name("public-other") / "payload.zst")


def test_provider_parser_retains_missing_fields_and_detects_upload_error(monkeypatch):
    response = SimpleNamespace(returncode=0, stdout="documentSize = 123;\nisUploaded = 1;\nuploadingError = serverUnavailable;\n")
    monkeypatch.setattr(inspector.archiver.subprocess, "run", lambda *args, **kwargs: response)
    parsed = inspector.archiver._provider_state(Path("/explicit-public-file"))
    assert parsed["documentSize"] == 123
    assert parsed["isUploaded"] == 1
    assert parsed["isUploading"] is None
    assert parsed["upload_error_present"] is True


def test_size_mismatch_cannot_pass_provider_upload(workspace, monkeypatch):
    root, _ = workspace
    payload = root / "archive.zst"
    payload.write_bytes(b"Synthetic preserved bytes")
    monkeypatch.setattr(inspector.archiver, "_provider_state", lambda path: {**_uploaded(path), "documentSize": 1})
    report = inspector.inspect_workspace(root, root / "report.json")
    assert report["totals"]["pending_or_unverified_files"] == 1
    assert report["all_provider_uploads_reported"] is False
    assert report["all_scanned_files_uploaded"] is False
    assert report["records"][0]["size_matches_provider"] is False


def test_dataless_payload_and_cache_are_never_opened(workspace, monkeypatch):
    root, cache = workspace
    payload = root / "archive.zst"
    payload.write_bytes(b"Public placeholder fixture")
    cached = cache / "workstation/archive.zst"
    cached.parent.mkdir()
    cached.write_bytes(b"Resident cache fixture")
    original = inspector._metadata

    def dataless(path):
        result = original(path)
        if path == payload:
            result.update(dataless=True, allocated_bytes=0)
        return result

    monkeypatch.setattr(inspector, "_metadata", dataless)
    monkeypatch.setattr(inspector.archiver, "_provider_state", _uploaded)
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: pytest.fail("Payload content was opened"))
    report = inspector.inspect_workspace(root, root / "report.json")
    assert report["all_provider_uploads_reported"] is True
    assert report["all_scanned_files_uploaded"] is False
    assert report["cloud_preservation_verified"] is False
    assert report["safe_to_release_local_content"] is False
    assert report["records"][0]["provider_upload_reported"] is True
    assert report["records"][0]["cloud_preservation_verified"] is False
    assert report["records"][0]["safe_to_release_local_content"] is False
    assert report["public_allocation_after"]["dataless_files"] == 1
    blocks = getattr(cached.stat(), "st_blocks", None)
    assert report["canonical_cache_allocation_after"]["allocated_bytes"] == (blocks * 512 if blocks is not None else None)
    assert report["payload_bytes_read"] == 0
    assert report["remote_content_hash_verified"] is False


def test_in_progress_payload_and_dataless_transaction_are_explicitly_excluded(workspace, monkeypatch):
    root, _ = workspace
    pending = root / ".archive.unique.pending"
    transaction = root / ".archive.unique.pending.json"
    pending.write_bytes(b"Being compressed")
    transaction.write_bytes(b"Do not hydrate this control")
    original = inspector._metadata
    monkeypatch.setattr(inspector, "_metadata", lambda path: {**original(path), "dataless": path == transaction})
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: pytest.fail("A placeholder was opened"))
    monkeypatch.setattr(inspector.archiver, "_provider_state", lambda path: pytest.fail("In-progress item was queried"))
    report = inspector.inspect_workspace(root, root / "report.json")
    assert report["totals"]["in_progress_files"] == 2
    assert report["totals"]["excluded_items"] == 2
    assert report["all_scanned_files_uploaded"] is False
    assert report["all_provider_uploads_reported"] is False


def test_completed_transaction_is_checked_alongside_its_final_artifacts(workspace, monkeypatch):
    root, _ = workspace
    archive = root / "archive.zst"
    receipt = root / "archive.zst.receipt.json"
    transaction = root / ".archive.unique.pending.json"
    archive.write_bytes(b"Final compressed bytes")
    receipt.write_text("{}")
    transaction.write_text(json.dumps({"status": "completed_receipt_published", "archive": str(archive), "receipt": str(receipt)}))
    monkeypatch.setattr(inspector.archiver, "_provider_state", _uploaded)
    report = inspector.inspect_workspace(root, root / "report.json")
    assert report["all_provider_uploads_reported"] is True
    assert report["all_scanned_files_uploaded"] is False
    assert report["totals"]["provider_reported_uploaded_files"] == 3


def test_malformed_transaction_is_retained_as_unverified(workspace, monkeypatch):
    root, _ = workspace
    (root / ".archive.unique.pending.json").write_text("[]")
    monkeypatch.setattr(inspector.archiver, "_provider_state", lambda path: pytest.fail("An unverified transaction was queried"))
    report = inspector.inspect_workspace(root, root / "report.json")
    assert report["totals"]["in_progress_files"] == 1
    assert report["records"][0]["reason"] == "transaction_state_unverified"


def test_file_change_during_provider_query_and_new_file_prevent_root_completion(workspace, monkeypatch):
    root, _ = workspace
    payload = root / "archive.zst"
    payload.write_bytes(b"Old fixture")

    def mutate(path):
        state = _uploaded(path)
        path.write_bytes(b"New fixture")
        (root / "new-control.json").write_text("{}")
        return state

    monkeypatch.setattr(inspector.archiver, "_provider_state", mutate)
    report = inspector.inspect_workspace(root, root / "report.json")
    assert report["records"][0]["identity_stable"] is False
    assert report["namespace"]["stable"] is False
    assert report["namespace"]["added"] == [str(root / "new-control.json")]
    assert report["all_scanned_files_uploaded"] is False
    assert report["all_provider_uploads_reported"] is False


def test_metadata_only_cli_never_certifies_cloud_preservation_or_release(workspace, monkeypatch, capsys):
    root, _ = workspace
    (root / "archive.zst").write_bytes(b"Synthetic local bytes without remote evidence")
    monkeypatch.setattr(inspector.archiver, "_provider_state", _uploaded)
    report_path = root / "inspection.json"
    assert inspector.main([str(root), "--report", str(report_path)]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "all_provider_uploads_reported_cloud_unverified"
    assert output["all_provider_uploads_reported"] is True
    assert output["cloud_preservation_verified"] is False
    assert output["safe_to_release_local_content"] is False
    stored = json.loads(report_path.read_text())
    assert stored["all_scanned_files_uploaded"] is False
    assert stored["remote_content_hash_verified"] is False


def test_missing_allocation_metadata_never_reads_payloads_or_claims_zero_allocation(workspace, monkeypatch):
    root, cache = workspace
    payload = root / "archive.zst"
    payload.write_bytes(b"Synthetic public placeholder")
    cached = cache / "workstation/archive.zst"
    cached.parent.mkdir()
    cached.write_bytes(b"Synthetic resident cache")
    lstat = Path.lstat

    class NoAllocation:
        def __init__(self, state, dataless):
            self.state = state
            self.st_flags = getattr(state, "st_flags", 0) | (inspector.archiver.DATALESS if dataless else 0)

        def __getattr__(self, name):
            if name == "st_blocks":
                raise AttributeError("Windows does not expose st_blocks")
            return getattr(self.state, name)

    def missing_blocks(path, *args, **kwargs):
        state = lstat(path, *args, **kwargs)
        return NoAllocation(state, path == payload) if path in {payload, cached} else state

    monkeypatch.setattr(Path, "lstat", missing_blocks)
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: pytest.fail("Metadata inspection opened a payload"))
    monkeypatch.setattr(inspector.archiver, "_provider_state", _uploaded)
    report = inspector.inspect_workspace(root, root / "report.json")
    assert report["records"][0]["before"]["dataless"]
    assert report["public_allocation_after"]["dataless_files"] == 1
    for key in ("public_allocation_after", "canonical_cache_allocation_after"):
        assert report[key]["files"] == 1
        assert report[key]["allocated_bytes"] is None
        assert report[key]["unique_inode_allocated_bytes"] is None
        assert report[key]["allocation_unknown_files"] == 1
    assert report["cloud_preservation_verified"] is False
    assert report["safe_to_release_local_content"] is False
    assert report["all_scanned_files_uploaded"] is False
    assert report["remote_content_hash_verified"] is False
    assert report["eviction_performed"] is False
    assert report["source_files_deleted"] is False
    assert report["payload_bytes_read"] == 0


@pytest.mark.parametrize("allocation", [None, 4096])
def test_allocation_summary_propagates_unknown_values_without_losing_inode_deduplication(allocation):
    common = {"device": 1, "inode": 2, "bytes": 10, "kind": "file", "dataless": False}
    summary = inspector._allocation_summary([
        {**common, "allocated_bytes": 4096},
        {**common, "allocated_bytes": allocation},
    ])
    assert summary["files"] == 2 and summary["logical_bytes"] == 20
    assert summary["allocated_bytes"] == (None if allocation is None else 8192)
    assert summary["unique_inode_allocated_bytes"] == (None if allocation is None else 4096)
    assert summary["allocation_unknown_files"] == int(allocation is None)


@pytest.mark.parametrize("attributes", [0x1000, 0x40000, 0x400000, 0x400])
def test_native_placeholder_control_is_never_opened_or_queried(workspace, monkeypatch, attributes):
    root, _cache = workspace
    control = root / ".archive.unique.pending.json"
    control.write_bytes(b"Private synthetic transaction control")
    lstat = Path.lstat

    class NativeAttributes:
        st_file_attributes = attributes
        st_reparse_tag = 0x9000001A

        def __init__(self, metadata):
            self.metadata = metadata

        def __getattr__(self, name):
            return getattr(self.metadata, name)

    def native_metadata(path, *args, **kwargs):
        value = lstat(path, *args, **kwargs)
        return NativeAttributes(value) if path == control else value

    monkeypatch.setattr(Path, "lstat", native_metadata)
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: pytest.fail("A native placeholder was opened"))
    monkeypatch.setattr(inspector.archiver, "_provider_state", lambda *args: pytest.fail("An in-progress native placeholder was queried"))
    report = inspector.inspect_workspace(root, root / "report.json")
    row = report["records"][0]
    assert row["before"]["dataless"] is (attributes != 0x400)
    assert row["before"]["file_attributes"] == attributes
    assert row["before"]["reparse_tag"] == 0x9000001A
    assert row["reason"] == "transaction_state_unavailable_without_downloading_payloads"
    assert report["all_provider_uploads_reported"] is False
    assert report["cloud_preservation_verified"] is report["safe_to_release_local_content"] is False
    assert report["payload_bytes_read"] == 0


@pytest.mark.parametrize("flag", [0x400, 0x1000, 0x40000, 0x400000, "mac-dataless"])
@pytest.mark.parametrize("location", ["root", "source_parent", "report_parent", "child", "cache_parent"])
def test_unavailable_directory_keeps_subtrees_unverified_without_entering_them(workspace, monkeypatch, flag, location):
    root, cache = workspace
    (root / "blocked").mkdir()
    unsafe = root / "blocked/evidence.zst"
    unsafe.write_bytes(b"Untouched synthetic archived evidence")
    (root / "safe.zst").write_bytes(b"Known synthetic sibling")
    report_parent = root.parent / "reports"
    report_parent.mkdir()
    cached_parent = cache / "workstation"
    cached_parent.mkdir()
    blocked = {"root": root, "source_parent": root.parent, "report_parent": report_parent,
               "child": root / "blocked", "cache_parent": cached_parent}[location]
    report_path = report_parent / "inspection.json" if location == "report_parent" else root / "inspection.json"
    lstat, scandir = Path.lstat, os.scandir

    class FlaggedDirectory:
        def __init__(self, value):
            self.value = value
            self.st_flags = getattr(value, "st_flags", 0) | (inspector.archiver.DATALESS if flag == "mac-dataless" else 0)
            self.st_file_attributes = 0x10 | (0 if flag == "mac-dataless" else flag)
            self.st_reparse_tag = 0xA0000003 if flag == 0x400 else 0

        def __getattr__(self, name):
            return getattr(self.value, name)

    def metadata(path, *args, **kwargs):
        if path != blocked and path.is_relative_to(blocked):
            pytest.fail("Metadata traversed an unavailable ancestor")
        value = lstat(path, *args, **kwargs)
        return FlaggedDirectory(value) if path == blocked else value

    def no_descent(path):
        if Path(path).is_relative_to(blocked):
            pytest.fail("Enumeration entered an unavailable directory")
        return scandir(path)

    monkeypatch.setattr(Path, "lstat", metadata)
    monkeypatch.setattr(inspector.os, "scandir", no_descent)
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: pytest.fail("Metadata inspection read archived payloads"))
    monkeypatch.setattr(inspector.archiver, "_provider_state", _uploaded)
    if location in {"root", "source_parent", "report_parent"}:
        with pytest.raises(ValueError, match="unavailable|reparse"):
            inspector.inspect_workspace(root, report_path)
    else:
        report = inspector.inspect_workspace(root, report_path)
        assert report["all_provider_uploads_reported"] is False
        assert report["cloud_preservation_verified"] is report["safe_to_release_local_content"] is False
        assert report["canonical_cache_allocation_after"]["allocated_bytes"] is None
        if location == "child":
            assert report["namespace"]["stable"] is False
            assert any(row["path"] == str(blocked) and row["reason"] == "unavailable_directory_requires_review"
                       for row in report["enumeration_errors"])
            assert str(unsafe) not in {row["path"] for row in report["records"]}
            assert report["public_allocation_after"]["allocated_bytes"] is None
        else:
            assert all(row.get("cache_inspection_error") for row in report["records"])
            assert report["canonical_cache_missing_files"] == 0
            assert report["canonical_cache_unverified_files"] == len(report["records"])
        assert report["payload_bytes_read"] == 0
    assert lstat(unsafe).st_size == len(b"Untouched synthetic archived evidence")
