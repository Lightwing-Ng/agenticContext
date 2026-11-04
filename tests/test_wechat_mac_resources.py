"""Mac resource lookup preserves account boundaries and inspects metadata only.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import wechat_mac_resources as resources
from app.core.wechat_mac_resources import WechatMacResourceResolver


OWNER = "wxid_owner_a"
OTHER = "wxid_owner_b"
PEER = "synthetic_room@chatroom"
CHECKSUM = "0123456789abcdef0123456789abcdef"
MONTH = "2026-05"
TIMESTAMP = 1_778_480_608


def native_hash(value):
    return hashlib.md5(value.encode(), usedforsecurity=False).hexdigest()


@pytest.fixture
def mac_tree(tmp_path):
    home = tmp_path / "home"
    modern = (
        home / "Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
    )
    legacy = (
        home
        / "Library/Containers/com.tencent.xinWeChat/Data/Library/Application Support/com.tencent.xinWeChat/2.0b4.0.9"
    )
    modern.mkdir(parents=True)
    legacy.mkdir(parents=True)
    resolver = WechatMacResourceResolver(tmp_path / "store", home=home)
    return SimpleNamespace(home=home, modern=modern, legacy=legacy, resolver=resolver)


def modern_account(tree, owner=OWNER, suffix="abcd"):
    account = tree.modern / f"{owner}_{suffix}"
    account.mkdir()
    return account


def image_scope(account, peer=PEER):
    folder = account / "msg/attach" / native_hash(peer) / MONTH / "Img"
    folder.mkdir(parents=True)
    return folder


def reference(**values):
    return {
        "account_id": OWNER,
        "peer_id": PEER,
        "kind": "image",
        "checksum_md5": CHECKSUM,
        "unix_timestamp": TIMESTAMP,
        **values,
    }


def test_native_account_discovery_rejects_prefix_and_alias_spoofs(mac_tree):
    for name in [f"{OWNER}extra_abcd", f"{OWNER}_abcde", "DisplayAlias_abcd"]:
        (mac_tree.modern / name).mkdir()
    modern_account(mac_tree, OTHER)
    assert mac_tree.resolver.account_mapping(OWNER)["status"] == "unmapped"
    modern_account(mac_tree, suffix="aBcD")
    (mac_tree.legacy / native_hash(OWNER)).mkdir()
    # A fresh page query discovers changes after the prior request's snapshot.
    resolver = WechatMacResourceResolver(
        mac_tree.home.parent / "store", home=mac_tree.home
    )
    mapping = resolver.account_mapping(OWNER)
    assert mapping == {
        "status": "mapped",
        "root_count": 2,
        "evidence": ["legacy_directory_hash", "native_username_directory"],
    }
    assert str(mac_tree.home) not in json.dumps(mapping)
    assert mac_tree.resolver.account_mapping(OWNER.upper())["status"] == "unmapped"
    assert not (mac_tree.home.parent / "store").exists()


@pytest.mark.parametrize(
    "owner", [None, "", "../wxid_owner_a", "wxid_owner_a/../../", "DisplayAlias", []]
)
def test_invalid_owner_never_searches_other_accounts(mac_tree, owner):
    modern_account(mac_tree)
    assert mac_tree.resolver.account_mapping(owner)["root_count"] == 0
    assert mac_tree.resolver.resolve(reference(account_id=owner))["status"] == "missing"


def test_exact_image_match_never_reads_media_and_keeps_other_accounts_separate(
    mac_tree, monkeypatch
):
    folder = image_scope(modern_account(mac_tree))
    wanted = folder / f"{CHECKSUM}.dat"
    wanted.write_bytes(b"opaque local payload")
    image_scope(modern_account(mac_tree, OTHER)).joinpath(
        f"{CHECKSUM}.dat"
    ).write_bytes(b"other")

    def no_open(*_args, **_kwargs):
        raise AssertionError("Media payload reads are outside the lookup boundary")

    monkeypatch.setattr(Path, "open", no_open)
    result = mac_tree.resolver.resolve(reference(checksum_md5=CHECKSUM.upper()))
    assert result["status"] == "file"
    assert result["path"] == wanted
    assert result["evidence"] == "checksum_filename"
    assert (
        mac_tree.resolver.resolve(reference(account_id="wxid_absent"))["path"] is None
    )


def test_thumbnail_only_match_is_labeled_as_a_thumbnail(mac_tree):
    folder = image_scope(modern_account(mac_tree))
    thumbnail = folder / f"{CHECKSUM}_t.dat"
    thumbnail.touch()
    result = mac_tree.resolver.resolve(reference())
    assert result["path"] == thumbnail
    assert result["evidence"] == "checksum_thumbnail_filename"
    assert "thumbnail" in result["label"]


def test_thumbnail_source_reference_is_honored_without_a_thumbnail_suffix(mac_tree):
    folder = image_scope(modern_account(mac_tree))
    thumbnail = folder / f"{CHECKSUM}.dat"
    thumbnail.touch()
    result = mac_tree.resolver.resolve(reference(variant="thumbnail"))
    assert result["path"] == thumbnail
    assert result["evidence"] == "checksum_thumbnail_reference"
    assert "thumbnail" in result["label"]


def test_video_checksum_uses_only_the_matching_owner_and_month(mac_tree):
    account = modern_account(mac_tree)
    month = account / "msg/video" / MONTH
    month.mkdir(parents=True)
    wanted = month / f"{CHECKSUM}.mp4"
    wanted.touch()
    result = mac_tree.resolver.resolve(reference(kind="video"))
    assert result["status"] == "file" and result["path"] == wanted
    assert (
        mac_tree.resolver.resolve(reference(kind="video", unix_timestamp=0))["status"]
        == "folder"
    )


def test_duplicate_native_matches_preserve_uncertainty(mac_tree):
    first = image_scope(modern_account(mac_tree, suffix="abcd"))
    second = image_scope(modern_account(mac_tree, suffix="ef01"))
    for folder in (first, second):
        (folder / f"{CHECKSUM}.dat").touch()
    assert mac_tree.resolver.account_mapping(OWNER)["root_count"] == 2
    result = mac_tree.resolver.resolve(reference())
    assert result["status"] == "folder"
    assert result["path"] == first


def test_unique_filename_and_size_is_a_candidate_and_collision_is_not(mac_tree):
    folder = modern_account(mac_tree) / "msg/file" / MONTH
    folder.mkdir(parents=True)
    wanted = folder / "report.pdf"
    wanted.write_bytes(b"data")
    resource = reference(kind="file", filename="report.pdf", byte_size=4)
    result = mac_tree.resolver.resolve(resource)
    assert result["status"] == "candidate" and result["path"] == wanted
    assert "possible" in result["label"]
    (folder / "report(1).pdf").write_bytes(b"data")
    assert mac_tree.resolver.resolve(resource)["status"] == "folder"
    assert mac_tree.resolver.resolve({**resource, "byte_size": 3})["status"] == "folder"


@pytest.mark.parametrize(
    "filename",
    ["../report.pdf", "sub/report.pdf", r"sub\report.pdf", ".", "..", "bad\0.pdf"],
)
def test_filename_paths_cannot_escape_the_scoped_folder(mac_tree, filename):
    folder = modern_account(mac_tree) / "msg/file" / MONTH
    folder.mkdir(parents=True)
    result = mac_tree.resolver.resolve(
        reference(kind="file", filename=filename, byte_size=4)
    )
    assert result["status"] == "folder" and result["path"] == folder


def test_symlink_account_is_not_an_identity_mapping(mac_tree):
    other = modern_account(mac_tree, OTHER)
    image_scope(other).joinpath(f"{CHECKSUM}.dat").touch()
    (mac_tree.modern / f"{OWNER}_abcd").symlink_to(other, target_is_directory=True)
    assert mac_tree.resolver.account_mapping(OWNER)["status"] == "unmapped"
    assert mac_tree.resolver.resolve(reference())["status"] == "missing"


def test_symlink_inside_a_real_account_cannot_reveal_another_account_file(mac_tree):
    account = modern_account(mac_tree)
    folder = account / "msg/attach" / native_hash(PEER) / MONTH
    folder.mkdir(parents=True)
    other = image_scope(modern_account(mac_tree, OTHER))
    (other / f"{CHECKSUM}.dat").touch()
    (folder / "Img").symlink_to(other, target_is_directory=True)
    result = mac_tree.resolver.resolve(reference())
    assert result["status"] == "folder" and result["path"] == folder
    assert not result["path"].is_relative_to(other)


def test_symlink_file_is_not_selected(mac_tree, tmp_path):
    folder = image_scope(modern_account(mac_tree))
    outside = tmp_path / "unrelated.dat"
    outside.write_bytes(b"protected")
    (folder / f"{CHECKSUM}.dat").symlink_to(outside)
    result = mac_tree.resolver.resolve(reference())
    assert result["status"] == "folder" and result["path"] == folder
    assert outside.read_bytes() == b"protected"


@pytest.mark.parametrize("block_account", [False, True])
def test_dataless_files_and_account_roots_are_not_hydrated(
    mac_tree, monkeypatch, block_account
):
    account = modern_account(mac_tree)
    folder = image_scope(account)
    media = folder / f"{CHECKSUM}.dat"
    media.touch()
    blocked = account if block_account else media
    original = Path.lstat

    def flagged(path, *args, **kwargs):
        state = original(path, *args, **kwargs)
        if path == blocked:
            return SimpleNamespace(
                st_mode=state.st_mode, st_size=state.st_size, st_flags=0x40000000
            )
        return state

    monkeypatch.setattr(Path, "lstat", flagged)
    result = mac_tree.resolver.resolve(reference())
    assert result["status"] == ("missing" if block_account else "folder")
    assert result["path"] != media


def test_legacy_account_and_peer_native_hashes_select_only_their_image_folder(mac_tree):
    account = mac_tree.legacy / native_hash(OWNER)
    folder = account / "Message/MessageTemp" / native_hash(PEER) / "Image"
    folder.mkdir(parents=True)
    wanted = folder / f"{CHECKSUM}.jpg"
    wanted.touch()
    result = mac_tree.resolver.resolve(reference())
    assert result["status"] == "file" and result["path"] == wanted
    assert mac_tree.resolver.account_mapping(OWNER)["evidence"] == [
        "legacy_directory_hash"
    ]


def test_scoped_legacy_folder_precedes_an_empty_modern_account_folder(mac_tree):
    modern = modern_account(mac_tree)
    (modern / "msg").mkdir()
    legacy = mac_tree.legacy / native_hash(OWNER)
    folder = legacy / "Message/MessageTemp" / native_hash(PEER) / "Image"
    folder.mkdir(parents=True)
    result = mac_tree.resolver.resolve(reference(checksum_md5=None))
    assert result["status"] == "folder" and result["path"] == folder


def test_directory_caps_fail_closed_instead_of_claiming_unique_files(
    mac_tree, monkeypatch
):
    account = modern_account(mac_tree)
    folder = account / "msg/file" / MONTH
    folder.mkdir(parents=True)
    (folder / "report.pdf").write_bytes(b"data")
    (folder / "other.pdf").touch()
    monkeypatch.setattr(resources, "_FILE_LIMIT", 1)
    assert (
        mac_tree.resolver.resolve(
            reference(kind="file", filename="report.pdf", byte_size=4)
        )["status"]
        == "folder"
    )
    modern_account(mac_tree, OTHER)
    monkeypatch.setattr(resources, "_ACCOUNT_LIMIT", 1)
    resolver = WechatMacResourceResolver(
        mac_tree.home.parent / "store", home=mac_tree.home
    )
    assert resolver.account_mapping(OWNER)["status"] == "unmapped"


@pytest.mark.parametrize(
    "kind,timestamp",
    [("app", None), ([], float("nan")), ("unknown", -1), ("audio", True)],
)
def test_unsupported_or_incomplete_references_keep_an_account_folder(
    mac_tree, kind, timestamp
):
    account = modern_account(mac_tree)
    result = mac_tree.resolver.resolve(reference(kind=kind, unix_timestamp=timestamp))
    assert result["status"] == "folder" and result["path"] == account


def test_locator_free_page_does_not_enumerate_resource_folders(mac_tree, monkeypatch):
    folder = image_scope(modern_account(mac_tree))
    original = resources.os.scandir
    enumerated = []

    def record(path):
        enumerated.append(path)
        return original(path)

    monkeypatch.setattr(resources.os, "scandir", record)
    for _ in range(100):
        result = mac_tree.resolver.resolve(reference(checksum_md5=None, filename=None))
        assert result["status"] == "folder" and result["path"] == folder
    assert enumerated == [mac_tree.modern]
