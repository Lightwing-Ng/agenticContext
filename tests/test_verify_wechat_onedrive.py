"""Verify metadata-only OneDrive inspection, exact size binding, and exclusions.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

import json
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
    assert report["canonical_cache_allocation_after"]["allocated_bytes"] == cached.stat().st_blocks * 512
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
