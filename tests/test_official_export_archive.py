"""Complete, non-destructive official-export archival checks.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import warnings
import zipfile

import pytest

from scripts import official_export_archive as archive


ARCHIVE_ERRORS = (ValueError, OSError, RuntimeError, zipfile.BadZipFile)


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "official-export"
    contents = {
        "conversations-000.json": b'{"conversation": "A preserved conversation"}',
        "file-attachment.dat": bytes(range(256)) * 8,
        ".DS_Store": b"\x00\x01hidden metadata\x00",
        "pages/page-sample/content/current.json": b'{"text": "Page text"}',
        "sites/projects/project-sample/source/repository.bundle": b"bundle\x00data",
        "notes/\u56de\u5fc6 file.txt": b"Keep every memory.\n",
    }
    for relative, content in contents.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (root / "empty-directory").mkdir()
    return root


def _tree(root: Path) -> tuple[dict[str, bytes], set[str]]:
    return (
        {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file()
        },
        {
            path.relative_to(root).as_posix()
            for path in root.rglob("*") if path.is_dir()
        },
    )


def _source_metadata(root: Path) -> dict[str, tuple[int, int]]:
    return {
        "." if path == root else path.relative_to(root).as_posix(): (
            stat.S_IMODE(path.stat().st_mode), path.stat().st_mtime_ns
        )
        for path in [root, *root.rglob("*")]
    }


def _refresh_checksums(directory: Path) -> None:
    """Model a self-consistent but untrusted downloaded archive."""
    lines = []
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.name != "SHA256SUMS":
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append(f"{digest}  {path.name}\n")
    (directory / "SHA256SUMS").write_text("".join(lines), encoding="utf-8")


def _assert_archive_refused(directory: Path, destination: Path) -> None:
    with pytest.raises(ARCHIVE_ERRORS):
        archive.verify_archive(directory)
    with pytest.raises(ARCHIVE_ERRORS):
        archive.restore_archive(directory, destination)
    assert not destination.exists()


def test_complete_round_trip_preserves_source_and_every_file(
    source: Path, tmp_path: Path,
) -> None:
    expected_files, expected_directories = _tree(source)
    expected_metadata = _source_metadata(source)
    directory = tmp_path / "retained-archive"

    created = archive.create_archive(source, directory)
    verified = archive.verify_archive(directory)
    destination = tmp_path / "restored"
    restored = archive.restore_archive(directory, destination)

    for result in (created, verified, restored):
        assert result["verified"] is True
        assert result["file_count"] == len(expected_files)
        assert result["directory_count_excluding_root"] == len(expected_directories)
    assert created["ready_to_delete_source"] is True
    assert _tree(destination / source.name) == (expected_files, expected_directories)
    assert _tree(source) == (expected_files, expected_directories)
    assert _source_metadata(source) == expected_metadata
    assert {
        "official-export.zip", "source-manifest.v1.json",
        "verification.v1.json", "SHA256SUMS",
    } <= {path.name for path in directory.iterdir()}
    before = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
    archive.verify_archive(directory)
    assert {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()} == before


@pytest.mark.parametrize("existing_kind", ["file", "directory"])
def test_create_never_overwrites_existing_destination(
    source: Path, tmp_path: Path, existing_kind: str,
) -> None:
    destination = tmp_path / "existing"
    if existing_kind == "directory":
        destination.mkdir()
        sentinel = destination / "sentinel.txt"
    else:
        sentinel = destination
    sentinel.write_bytes(b"existing user record")
    expected = _tree(source)

    with pytest.raises(ARCHIVE_ERRORS):
        archive.create_archive(source, destination)

    assert sentinel.read_bytes() == b"existing user record"
    assert _tree(source) == expected


@pytest.mark.parametrize("existing_kind", ["file", "directory"])
def test_restore_never_overwrites_existing_destination(
    source: Path, tmp_path: Path, existing_kind: str,
) -> None:
    directory = tmp_path / "archive"
    archive.create_archive(source, directory)
    destination = tmp_path / "existing"
    if existing_kind == "directory":
        destination.mkdir()
        sentinel = destination / "sentinel.txt"
    else:
        sentinel = destination
    sentinel.write_bytes(b"existing user record")

    with pytest.raises(ARCHIVE_ERRORS):
        archive.restore_archive(directory, destination)

    assert sentinel.read_bytes() == b"existing user record"


def test_archive_inside_source_is_rejected_before_any_write(source: Path) -> None:
    before = _tree(source)
    destination = source / "nested-archive"

    with pytest.raises(ARCHIVE_ERRORS):
        archive.create_archive(source, destination)

    assert not destination.exists()
    assert _tree(source) == before


def test_unreadable_directory_cannot_be_silently_omitted(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    unreadable = source / "pages"
    original_scandir = os.scandir

    def fail_for_pages(path):
        if not isinstance(path, int) and Path(os.fsdecode(path)) == unreadable:
            raise PermissionError("Synthetic unreadable directory")
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", fail_for_pages)
    destination = tmp_path / "archive"

    with pytest.raises(PermissionError):
        archive.create_archive(source, destination)

    assert not destination.exists()
    assert (unreadable / "page-sample/content/current.json").read_bytes() == b'{"text": "Page text"}'


def test_final_archive_publication_never_overwrites_a_concurrent_file(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_create_zip = archive._create_zip
    sentinel = b"independent record created during archive verification"

    def create_then_add_existing_record(root: Path, path: Path, manifest: dict) -> None:
        original_create_zip(root, path, manifest)
        path.with_name("official-export.zip").write_bytes(sentinel)

    monkeypatch.setattr(archive, "_create_zip", create_then_add_existing_record)
    destination = tmp_path / "archive"

    with pytest.raises(ARCHIVE_ERRORS):
        archive.create_archive(source, destination)

    assert (destination / "official-export.zip").read_bytes() == sentinel
    assert not (destination / "verification.v1.json").exists()


def _symlink(link: Path, target: Path, *, is_directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=is_directory)
    except (OSError, NotImplementedError):
        pytest.skip("This host cannot create a synthetic symlink")


def test_source_root_symlink_is_rejected(source: Path, tmp_path: Path) -> None:
    link = tmp_path / "source-alias"
    _symlink(link, source, is_directory=True)
    expected = _tree(source)

    with pytest.raises(ARCHIVE_ERRORS):
        archive.create_archive(link, tmp_path / "archive")

    assert _tree(source) == expected


@pytest.mark.parametrize("target_kind", ["file", "directory", "missing"])
def test_nested_symlinks_are_not_followed(
    source: Path, tmp_path: Path, target_kind: str,
) -> None:
    target = tmp_path / "outside"
    if target_kind == "file":
        target.write_bytes(b"outside private record")
    elif target_kind == "directory":
        target.mkdir()
        (target / "outside.txt").write_bytes(b"outside private record")
    _symlink(source / "linked-entry", target, is_directory=target_kind == "directory")

    with pytest.raises(ARCHIVE_ERRORS):
        archive.create_archive(source, tmp_path / "archive")

    if target_kind == "file":
        assert target.read_bytes() == b"outside private record"
    elif target_kind == "directory":
        assert (target / "outside.txt").read_bytes() == b"outside private record"
    else:
        assert not target.exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="Named pipes are unavailable")
def test_named_pipe_is_rejected_without_reading_it(source: Path, tmp_path: Path) -> None:
    os.mkfifo(source / "not-a-regular-file")

    with pytest.raises(ARCHIVE_ERRORS):
        archive.create_archive(source, tmp_path / "archive")


@pytest.mark.parametrize("tampered_file", ["official-export.zip", "source-manifest.v1.json"])
def test_existing_checksums_detect_tampered_archive_artifacts(
    source: Path, tmp_path: Path, tampered_file: str,
) -> None:
    directory = tmp_path / "archive"
    archive.create_archive(source, directory)
    path = directory / tampered_file
    path.write_bytes(path.read_bytes() + b"\nchanged after completion")

    _assert_archive_refused(directory, tmp_path / "restored")


@pytest.mark.parametrize(
    "member_name",
    ["../outside.txt", "/outside.txt", "C:/outside.txt", "..\\outside.txt", "C:\\outside.txt"],
)
def test_unsafe_zip_paths_are_rejected_even_with_refreshed_checksums(
    source: Path, tmp_path: Path, member_name: str,
) -> None:
    directory = tmp_path / "archive"
    archive.create_archive(source, directory)
    with zipfile.ZipFile(directory / "official-export.zip", "a") as zipped:
        zipped.writestr(member_name, b"must not escape restore destination")
    _refresh_checksums(directory)

    _assert_archive_refused(directory, tmp_path / "restored")
    assert not (tmp_path / "outside.txt").exists()


def test_unsafe_manifest_path_is_rejected_with_matching_zip_and_checksums(
    source: Path, tmp_path: Path,
) -> None:
    directory = tmp_path / "archive"
    archive.create_archive(source, directory)
    manifest_path = directory / "source-manifest.v1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload = b"untrusted added file"
    entry = dict(manifest["files"][0])
    entry.update(path="../outside.txt", size=len(payload), sha256=hashlib.sha256(payload).hexdigest())
    manifest["files"].append(entry)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with zipfile.ZipFile(directory / "official-export.zip", "a") as zipped:
        zipped.writestr(f"{source.name}/../outside.txt", payload)
    _refresh_checksums(directory)

    _assert_archive_refused(directory, tmp_path / "restored")
    assert not (tmp_path / "outside.txt").exists()


def test_duplicate_zip_members_are_rejected_even_when_bytes_match(
    source: Path, tmp_path: Path,
) -> None:
    directory = tmp_path / "archive"
    archive.create_archive(source, directory)
    name = f"{source.name}/conversations-000.json"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(directory / "official-export.zip", "a") as zipped:
            zipped.writestr(name, (source / "conversations-000.json").read_bytes())
    _refresh_checksums(directory)

    _assert_archive_refused(directory, tmp_path / "restored")


def test_unexpected_zip_file_is_not_silently_ignored(source: Path, tmp_path: Path) -> None:
    directory = tmp_path / "archive"
    archive.create_archive(source, directory)
    with zipfile.ZipFile(directory / "official-export.zip", "a") as zipped:
        zipped.writestr(f"{source.name}/unlisted-memory.txt", b"unexpected memory")
    _refresh_checksums(directory)

    _assert_archive_refused(directory, tmp_path / "restored")


def test_symlink_zip_member_is_rejected_before_extraction(
    source: Path, tmp_path: Path,
) -> None:
    directory = tmp_path / "archive"
    archive.create_archive(source, directory)
    path = directory / "official-export.zip"
    target_name = f"{source.name}/notes/\u56de\u5fc6 file.txt"
    with zipfile.ZipFile(path, metadata_encoding="utf-8") as zipped:
        entries = [(item, zipped.read(item)) for item in zipped.infolist()]
    with zipfile.ZipFile(path, "w") as zipped:
        for item, content in entries:
            if item.filename == target_name:
                item.create_system = 3
                item.external_attr = (stat.S_IFLNK | 0o777) << 16
            zipped.writestr(item, content)
    _refresh_checksums(directory)

    _assert_archive_refused(directory, tmp_path / "restored")


@pytest.mark.parametrize("mutation", ["rewrite", "new-file"])
def test_source_changes_during_creation_never_produce_deletion_readiness(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    original_create_zip = archive._create_zip

    def create_then_mutate(root: Path, path: Path, manifest: dict) -> None:
        original_create_zip(root, path, manifest)
        if mutation == "rewrite":
            (root / "conversations-000.json").write_bytes(b"newer source record")
        else:
            (root / "new-memory.txt").write_bytes(b"newly arrived record")

    monkeypatch.setattr(archive, "_create_zip", create_then_mutate)
    directory = tmp_path / "archive"

    with pytest.raises(ARCHIVE_ERRORS):
        archive.create_archive(source, directory)

    report = directory / "verification.v1.json"
    if report.exists():
        assert json.loads(report.read_text()).get("ready_to_delete_source") is not True
    assert not (directory / "official-export.zip").exists()
    if mutation == "rewrite":
        assert (source / "conversations-000.json").read_bytes() == b"newer source record"
    else:
        assert (source / "new-memory.txt").read_bytes() == b"newly arrived record"


def test_restore_retains_original_nanosecond_modification_times(
    source: Path, tmp_path: Path,
) -> None:
    file = source / "file-attachment.dat"
    directory = source / "pages"
    for index, path in enumerate((file, directory)):
        timestamp = 1_700_000_000_123_456_700 + index * 1_000
        os.utime(path, ns=(timestamp, timestamp))
    expected = {path.relative_to(source): path.stat().st_mtime_ns for path in (file, directory)}
    retained = tmp_path / "archive"

    archive.create_archive(source, retained)
    archive.restore_archive(retained, tmp_path / "restored")

    recovered = tmp_path / "restored" / source.name
    assert {relative: (recovered / relative).stat().st_mtime_ns for relative in expected} == expected


def test_portable_zip_fallback_preserves_the_same_complete_tree(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(archive, "_use_native_zip", lambda: False)
    expected = _tree(source)
    directory = tmp_path / "portable-archive"

    assert archive.create_archive(source, directory)["ready_to_delete_source"] is True
    assert archive.verify_archive(directory)["verified"] is True
    archive.restore_archive(directory, tmp_path / "portable-restored")

    assert _tree(tmp_path / "portable-restored" / source.name) == expected
    assert _tree(source) == expected


def test_portable_zip_retains_modification_time_before_zip_epoch(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(archive, "_use_native_zip", lambda: False)
    attachment = source / "file-attachment.dat"
    original_time = 1_234_567_890_000_000
    os.utime(attachment, ns=(original_time, original_time))
    expected_time = attachment.stat().st_mtime_ns
    directory = tmp_path / "old-time-archive"

    assert archive.create_archive(source, directory)["ready_to_delete_source"] is True
    archive.restore_archive(directory, tmp_path / "old-time-restored")

    restored = tmp_path / "old-time-restored" / source.name / attachment.name
    assert restored.read_bytes() == attachment.read_bytes()
    assert restored.stat().st_mtime_ns == attachment.stat().st_mtime_ns == expected_time


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no POSIX extended attributes")
def test_restore_retains_original_extended_attribute(source: Path, tmp_path: Path) -> None:
    path = source / "file-attachment.dat"
    attribute = "user.agenticcontext.archive_test"
    value = b"preserved metadata"
    try:
        if sys.platform == "darwin":
            subprocess.run(["/usr/bin/xattr", "-w", attribute, value.decode(), str(path)], check=True)
        else:
            os.setxattr(path, attribute, value)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("The isolated temporary filesystem does not support extended attributes")
    retained = tmp_path / "archive"

    archive.create_archive(source, retained)
    archive.restore_archive(retained, tmp_path / "restored")

    recovered = tmp_path / "restored" / source.name / path.name
    if sys.platform == "darwin":
        result = subprocess.run(
            ["/usr/bin/xattr", "-p", attribute, str(recovered)], capture_output=True, check=True,
        ).stdout.rstrip(b"\n")
    else:
        result = os.getxattr(recovered, attribute)
    assert result == value


def test_cli_create_verify_and_restore_are_json_and_non_destructive(
    source: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    directory = tmp_path / "archive"
    destination = tmp_path / "restored"
    before = _tree(source)
    commands = (
        ["create", str(source), str(directory)],
        ["verify", str(directory)],
        ["restore", str(directory), str(destination)],
    )
    for command in commands:
        assert archive.main(command) == 0
        assert json.loads(capsys.readouterr().out)["verified"] is True
    assert _tree(source) == before
    assert _tree(destination / source.name) == before
