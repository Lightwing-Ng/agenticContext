"""Recovered group frames require exact imported archive, index and fragment pins.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import wechat_html_group_frames as frames
from app.core.wechat_archive import wechat_archive_path


PEER = "98000000004@chatroom"


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def synthetic_group_registry(fixture):
    """Build the same pinned snapshot/catalog shape retained by the Vale importer."""
    archive = fixture["archive"]
    member_path = "Synthetic/group_files/Data/msg-0.js"
    member_hash = _sha("Synthetic shared source group fragment")
    selected = [
        {"member_path": "Synthetic/index.html", "sha256": _sha("Synthetic root index")},
        {
            "member_path": "Synthetic/group.html",
            "sha256": _sha("Synthetic group main HTML"),
        },
        {"member_path": member_path, "sha256": member_hash},
    ]
    metadata = fixture["metadata"]
    metadata["selected_text_members"] = deepcopy(selected)
    metadata["source_conversation_catalog"] = [
        {
            "conversation_kind": "group",
            "index_avatar_native_id": PEER,
            "member_path": "Synthetic/group.html",
            "title": "Synthetic group",
        }
    ]
    raw = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    snapshot = {
        "id": fixture["numeric"],
        "source_id": "html-" + archive[:24],
        "snapshot_id": archive,
        "metadata": raw,
    }
    entry = {
        "snapshot_numeric_id": snapshot["id"],
        "snapshot_id": archive,
        "source_id": snapshot["source_id"],
        "source_metadata_sha256": _sha(raw),
        "archive_sha256": archive,
        "peer_id": PEER,
        "index_member": deepcopy(selected[0]),
        "main_html_member": deepcopy(selected[1]),
        "declared_data_path": "group_files/Data",
        "fragments": [deepcopy(selected[2])],
    }
    return snapshot, {"schema_version": 1, "frames": [entry]}


@pytest.fixture
def group_registry(tmp_path):
    root = tmp_path / "store"
    archive = _sha("synthetic-html-precedence-export")
    metadata = {
        "source_id": "html-" + archive[:24],
        "source_sha256": archive,
        "source_account_id": "wxid_verified_owner",
        "owner_verified": True,
        "owner_evidence": {"native_username": "wxid_verified_owner"},
    }
    snapshot, registry = synthetic_group_registry(
        {"archive": archive, "metadata": metadata, "numeric": 13}
    )
    path = wechat_archive_path(root).parent / "html_group_source_frames.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(registry), encoding="utf-8")
    return SimpleNamespace(root=root, path=path, snapshot=snapshot, registry=registry)


def _load(fixture):
    return frames.load_group_source_frames(fixture.root, {13: fixture.snapshot})


def _write(fixture):
    fixture.path.write_text(json.dumps(fixture.registry), encoding="utf-8")


def test_exact_snapshot_and_catalog_restore_the_reviewed_frame(group_registry):
    fixture = group_registry
    before = fixture.path.read_bytes(), deepcopy(fixture.snapshot)
    result = _load(fixture)
    assert set(result) == {(13, PEER)}
    frame = result[(13, PEER)]
    assert (
        frame["index_member_path"]
        == frame["physical_member_path"]
        == "Synthetic/group.html"
    )
    assert frame["archive_sha256"] == fixture.snapshot["snapshot_id"]
    assert frame["main_html_sha256"] == _sha("Synthetic group main HTML")
    assert frame["declared_data_path"] == "group_files/Data"
    assert frame["selected_fragment_sha256"] == {
        "Synthetic/group_files/Data/msg-0.js": _sha(
            "Synthetic shared source group fragment"
        )
    }
    assert before == (fixture.path.read_bytes(), fixture.snapshot)


@pytest.mark.parametrize(
    "field",
    [
        "snapshot_numeric_id",
        "snapshot_id",
        "source_id",
        "source_metadata_sha256",
        "archive_sha256",
        "peer_id",
    ],
)
def test_a_changed_registry_identity_is_rejected(group_registry, field):
    fixture = group_registry
    fixture.registry["frames"][0][field] = (
        14 if field == "snapshot_numeric_id" else "f" * 64
    )
    _write(fixture)
    assert _load(fixture) == {}


@pytest.mark.parametrize(
    "change", ["raw_bytes", "numeric", "source", "snapshot", "missing"]
)
def test_another_loaded_snapshot_cannot_satisfy_the_registry(group_registry, change):
    fixture = group_registry
    if change == "raw_bytes":
        fixture.snapshot["metadata"] += " "
    elif change == "numeric":
        fixture.snapshot["id"] = True
    elif change == "source":
        fixture.snapshot["source_id"] = "html-other"
    elif change == "snapshot":
        fixture.snapshot["snapshot_id"] = "f" * 64
    else:
        assert frames.load_group_source_frames(fixture.root, {}) == {}
        return
    assert _load(fixture) == {}


@pytest.mark.parametrize(
    "change",
    [
        "catalog_peer",
        "catalog_kind",
        "catalog_main",
        "catalog_duplicate",
        "catalog_other_peer_same_main",
        "index_hash",
        "main_hash",
        "fragment_hash",
        "duplicate_selected",
        "missing_fragment",
        "owner",
        "archive",
    ],
)
def test_repinning_metadata_does_not_bypass_semantic_bindings(group_registry, change):
    fixture = group_registry
    metadata = json.loads(fixture.snapshot["metadata"])
    catalog = metadata["source_conversation_catalog"]
    selected = metadata["selected_text_members"]
    if change == "catalog_peer":
        catalog[0]["index_avatar_native_id"] = "98000000005@chatroom"
    elif change == "catalog_kind":
        catalog[0]["conversation_kind"] = "direct"
    elif change == "catalog_main":
        catalog[0]["member_path"] = "Synthetic/other.html"
    elif change == "catalog_duplicate":
        catalog.append(deepcopy(catalog[0]))
    elif change == "catalog_other_peer_same_main":
        catalog.append({**catalog[0], "index_avatar_native_id": "98000000005@chatroom"})
    elif change in {"index_hash", "main_hash", "fragment_hash"}:
        selected[{"index_hash": 0, "main_hash": 1, "fragment_hash": 2}[change]][
            "sha256"
        ] = "f" * 64
    elif change == "duplicate_selected":
        selected.append(deepcopy(selected[2]))
    elif change == "missing_fragment":
        selected.pop()
    elif change == "owner":
        metadata["owner_evidence"]["native_username"] = "wxid_other_owner"
    else:
        metadata["source_sha256"] = "f" * 64
    raw = json.dumps(metadata)
    fixture.snapshot["metadata"] = raw
    fixture.registry["frames"][0]["source_metadata_sha256"] = _sha(raw)
    _write(fixture)
    assert _load(fixture) == {}


@pytest.mark.parametrize(
    "change",
    [
        "duplicate_frame",
        "duplicate_fragment",
        "fragment_path",
        "absolute_main",
        "escape_data",
        "encoded_escape_data",
        "encoded_nul",
        "unknown_field",
        "missing_index",
        "missing_fragment",
        "ambiguous_path",
    ],
)
def test_unsafe_or_ambiguous_registry_paths_and_entries_fail_closed(
    group_registry, change
):
    fixture = group_registry
    entry = fixture.registry["frames"][0]
    if change == "duplicate_frame":
        fixture.registry["frames"].append(deepcopy(entry))
    elif change == "duplicate_fragment":
        entry["fragments"].append(deepcopy(entry["fragments"][0]))
    elif change == "fragment_path":
        entry["fragments"][0]["member_path"] = "Synthetic/other_files/Data/msg-0.js"
    elif change == "absolute_main":
        entry["main_html_member"]["member_path"] = "/Synthetic/group.html"
    elif change == "escape_data":
        entry["declared_data_path"] = "../group_files/Data"
    elif change == "encoded_escape_data":
        entry["declared_data_path"] = "%2e%2e/group_files/Data"
    elif change == "encoded_nul":
        entry["declared_data_path"] = "group_files/Data%00"
    elif change == "unknown_field":
        entry["guessed_peer"] = PEER
    elif change == "missing_index":
        entry.pop("index_member")
    elif change == "missing_fragment":
        entry["fragments"] = []
    else:
        entry["fragments"][0]["member_path"] = "Synthetic/group_files//Data/msg-0.js"
    _write(fixture)
    assert _load(fixture) == {}


def test_duplicate_json_fields_are_not_silently_selected(group_registry):
    fixture = group_registry
    fixture.path.write_text(
        '{"schema_version":1,"schema_version":1,"frames":[]}', encoding="utf-8"
    )
    assert _load(fixture) == {}


def test_missing_and_oversized_registry_states_are_fingerprinted(
    group_registry, monkeypatch
):
    fixture = group_registry
    present = frames.group_frame_registry_digest(fixture.root)
    assert present == frames.group_frame_registry_digest(fixture.root)
    monkeypatch.setattr(frames, "MAX_REGISTRY_BYTES", 16)
    oversized = frames.group_frame_registry_digest(fixture.root)
    assert _load(fixture) == {}
    fixture.path.unlink()
    missing = frames.group_frame_registry_digest(fixture.root)
    assert len({present, oversized, missing}) == 3
    assert _load(fixture) == {}


@pytest.mark.parametrize("kind", ["file_symlink", "parent_symlink", "hardlink"])
def test_registry_links_cannot_redirect_source_proof(group_registry, kind):
    fixture = group_registry
    if kind == "file_symlink":
        target = fixture.path.with_name("registry-target.json")
        fixture.path.rename(target)
        fixture.path.symlink_to(target)
    elif kind == "hardlink":
        os.link(fixture.path, fixture.path.with_name("registry-link.json"))
    else:
        parent = fixture.path.parent
        target = parent.with_name("separate-wechat")
        parent.rename(target)
        parent.symlink_to(target, target_is_directory=True)
    assert _load(fixture) == {}


def test_cloud_only_registry_is_not_opened(group_registry, monkeypatch):
    fixture = group_registry
    original = Path.lstat
    original_open = frames.os.open

    def cloud_info(path):
        value = original(path)
        if path == fixture.path:
            attributes = {
                name: getattr(value, name)
                for name in dir(value)
                if name.startswith("st_")
            }
            attributes["st_flags"] = 0x40000000
            return SimpleNamespace(**attributes)
        return value

    def forbid_registry_open(path, *args, **kwargs):
        assert Path(path) != fixture.path, (
            "Cloud-only evidence must not be materialized."
        )
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", cloud_info)
    monkeypatch.setattr(frames.os, "open", forbid_registry_open)
    assert _load(fixture) == {}


def test_registry_replacement_during_read_cannot_publish_frames(
    group_registry, monkeypatch
):
    fixture = group_registry
    original = frames.os.open

    def replace_registry(path, *args, **kwargs):
        descriptor = original(path, *args, **kwargs)
        replacement = fixture.path.with_name("replacement.json")
        replacement.write_bytes(fixture.path.read_bytes())
        replacement.replace(fixture.path)
        return descriptor

    monkeypatch.setattr(frames.os, "open", replace_registry)
    assert _load(fixture) == {}
