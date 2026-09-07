"""Native privacy checks for Agent settings, context, and runtime artifacts.

Code version: v1.0.1-codex.1
"""

from __future__ import annotations

from copy import deepcopy
import ctypes
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
from pathlib import Path
from threading import Event

import pytest

from app.core import owner_only_permissions as privacy


def test_atomic_artifact_is_private_before_content_and_does_not_change_parent(
    tmp_path, monkeypatch
):
    parent = tmp_path / "ordinary user directory"
    parent.mkdir()
    before = (
        privacy.read_owner_only_acl(parent)
        if os.name == "nt"
        else parent.stat().st_mode
    )
    destination = parent / "private 用户.json"
    original_fdopen = os.fdopen
    inspected = []

    def inspect_before_content(descriptor, *args, **kwargs):
        candidates = list(parent.glob(".private 用户.json.*.tmp"))
        assert len(candidates) == 1
        if os.name == "nt":
            # The creation handle has zero sharing, so inspect its actual DACL.
            import msvcrt

            api = privacy._WindowsPermissions()
            evidence = api._read_acl(msvcrt.get_osfhandle(descriptor))
            privacy._assert_acl(evidence, directory=False)
        else:
            privacy.assert_owner_only_path(candidates[0], directory=False)
        assert os.fstat(descriptor).st_size == 0
        inspected.append(True)
        return original_fdopen(descriptor, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", inspect_before_content)
    privacy.atomic_write_owner_only_text(destination, "private synthetic content\n")
    assert inspected == [True]
    assert destination.read_bytes() == b"private synthetic content\n"
    privacy.assert_owner_only_path(destination, directory=False)
    after = (
        privacy.read_owner_only_acl(parent)
        if os.name == "nt"
        else parent.stat().st_mode
    )
    assert before == after
    assert sorted(item.name for item in parent.iterdir()) == [destination.name]


def test_private_directory_and_preserved_file_are_hardened(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    context = runtime / "context.md"
    context.write_bytes(b"An existing synthetic context.")
    privacy.ensure_owner_only_directory(runtime)
    privacy.ensure_owner_only_file(context)
    privacy.assert_owner_only_path(runtime, directory=True)
    privacy.assert_owner_only_path(context, directory=False)
    assert context.read_bytes() == b"An existing synthetic context."


def test_new_private_directory_has_inheritable_owner_acl_and_remains_writable(tmp_path):
    runtime = tmp_path / "nested" / "private runtime"
    privacy.ensure_owner_only_directory(runtime)
    privacy.assert_owner_only_path(runtime, directory=True)
    artifact = runtime / "context.md"
    privacy.atomic_write_owner_only_bytes(artifact, b"private\x00bytes\n")
    assert artifact.read_bytes() == b"private\x00bytes\n"
    privacy.assert_owner_only_path(artifact, directory=False)
    if os.name == "nt":
        evidence = privacy.read_owner_only_acl(runtime)
        assert evidence["entries"] == [
            {
                "type": 0,
                "flags": 3,
                "mask": privacy._FILE_ALL_ACCESS,
                "sid": evidence["current_user"],
            }
        ]
        assert evidence["protected"]
        assert evidence["owner"] == evidence["current_user"]


def test_replacement_retains_private_acl_and_uses_exact_bytes(tmp_path):
    path = tmp_path / "settings.json"
    privacy.atomic_write_owner_only_text(path, "old\n")
    privacy.atomic_write_owner_only_text(path, "new\nsecond line\n")
    privacy.assert_owner_only_path(path, directory=False)
    assert path.read_bytes() == b"new\nsecond line\n"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_linked_parent_file_and_hardlink_are_rejected_without_touching_target(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "existing.txt"
    target.write_bytes(b"Retain this synthetic target.")
    parent_link = tmp_path / "linked-parent"
    parent_link.symlink_to(outside, target_is_directory=True)
    leaf_link = tmp_path / "linked-file"
    leaf_link.symlink_to(target)
    hardlink = tmp_path / "hardlink"
    os.link(target, hardlink)
    for path in (parent_link / "new.txt", leaf_link, hardlink):
        with pytest.raises(OSError, match="linked"):
            privacy.atomic_write_owner_only_bytes(path, b"Must not be written.")
    with pytest.raises(OSError, match="linked"):
        privacy.ensure_owner_only_file(hardlink)
    assert target.read_bytes() == b"Retain this synthetic target."
    assert not (outside / "new.txt").exists()


@pytest.mark.parametrize(
    "changed_field",
    ("owner", "protected", "foreign-allow", "mask", "inheritance", "null"),
)
def test_acl_verification_rejects_any_weaker_security_descriptor(changed_field):
    evidence = {
        "owner": "S-1-5-21-100",
        "current_user": "S-1-5-21-100",
        "protected": True,
        "entries": [
            {
                "type": 0,
                "flags": 0,
                "mask": privacy._FILE_ALL_ACCESS,
                "sid": "S-1-5-21-100",
            }
        ],
    }
    privacy._assert_acl(evidence, directory=False)
    changed = deepcopy(evidence)
    if changed_field == "owner":
        changed["owner"] = "S-1-5-21-200"
    elif changed_field == "protected":
        changed["protected"] = False
    elif changed_field == "foreign-allow":
        changed["entries"].append({"type": 0, "flags": 0, "mask": 1, "sid": "S-1-1-0"})
    elif changed_field == "mask":
        changed["entries"][0]["mask"] = 1
    elif changed_field == "inheritance":
        changed["entries"][0]["flags"] = 16
    else:
        changed["entries"] = []
    with pytest.raises(OSError, match="protected DACL"):
        privacy._assert_acl(changed, directory=False)


def test_primary_write_error_survives_secondary_cleanup_failure(tmp_path, monkeypatch):
    target = tmp_path / "private.txt"
    primary = OSError("Synthetic fsync failure.")
    original_unlink = Path.unlink

    def failed_unlink(path, *args, **kwargs):
        if path.name.startswith(".private.txt."):
            raise OSError("Synthetic temporary cleanup failure.")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", lambda _descriptor: (_ for _ in ()).throw(primary))
    if os.name == "nt":

        def failed_discard(_api, _descriptor):
            raise OSError("Synthetic temporary cleanup failure.")

        monkeypatch.setattr(privacy._WindowsPermissions, "discard", failed_discard)
    else:
        monkeypatch.setattr(Path, "unlink", failed_unlink)
    with pytest.raises(OSError) as caught:
        privacy.atomic_write_owner_only_text(target, "Only synthetic bytes.")
    assert caught.value is primary
    assert "cleanup failure" in primary.__notes__[0]
    assert not target.exists()


def test_changed_destination_link_before_replace_is_rejected(tmp_path, monkeypatch):
    target = tmp_path / "private.txt"
    other = tmp_path / "other.txt"
    other.write_bytes(b"Keep unrelated bytes.")
    original_fsync = os.fsync

    def replace_after_flush(descriptor):
        original_fsync(descriptor)
        target.symlink_to(other)

    monkeypatch.setattr(os, "fsync", replace_after_flush)
    with pytest.raises(OSError, match="linked"):
        privacy.atomic_write_owner_only_text(target, "Must not replace the link.")
    assert other.read_bytes() == b"Keep unrelated bytes."


def test_protecting_directory_does_not_change_existing_external_hardlink_acl(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"Unrelated original bytes.")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    os.link(outside, runtime / "linked-child.txt")
    if os.name == "nt":
        # Inspect through an attribute-only handle because the privacy API rejects hardlinks.
        api = privacy._WindowsPermissions()

        def original_acl():
            handle = api.kernel.CreateFileW(
                str(outside), privacy._READ_CONTROL, 3, None, 3, 0, None
            )
            assert handle not in (None, privacy._INVALID_HANDLE)
            try:
                return api._read_acl(handle)
            finally:
                assert api.kernel.CloseHandle(handle)

        before = original_acl()
        privacy.ensure_owner_only_directory(runtime)
        assert original_acl() == before
    else:
        before = outside.stat().st_mode
        privacy.ensure_owner_only_directory(runtime)
        assert outside.stat().st_mode == before
    assert outside.read_bytes() == b"Unrelated original bytes."
    privacy.assert_owner_only_path(runtime, directory=True)


def test_native_rename_buffer_encodes_full_unicode_name_and_replace_flag(tmp_path):
    class Kernel:
        def SetFileInformationByHandle(self, handle, information_class, buffer, size):
            assert handle == 123
            assert information_class == 3
            information = privacy._FileRenameInformation.from_buffer(buffer)
            assert information.flags == 1
            assert information.root is None
            assert (
                size
                >= privacy._FileRenameInformation.name.offset
                + information.name_length
                + 2
            )
            encoded = ctypes.string_at(
                ctypes.addressof(buffer) + privacy._FileRenameInformation.name.offset,
                information.name_length,
            )
            assert encoded.decode(
                "utf-16-le"
            ) == privacy._WindowsPermissions._native_name(target)
            assert (
                ctypes.string_at(
                    ctypes.addressof(buffer)
                    + privacy._FileRenameInformation.name.offset
                    + information.name_length,
                    2,
                )
                == b"\x00\x00"
            )
            return True

    target = tmp_path / "safe 用户.json"
    api = privacy._WindowsPermissions.__new__(privacy._WindowsPermissions)
    api.kernel = Kernel()
    api._rename(123, target)


def test_native_file_stays_pinned_until_publish_and_old_temporary_name_is_not_cleaned(
    tmp_path, monkeypatch
):
    if os.name != "nt":
        # A deterministic API model exercises the same orchestration on other hosts.
        class Model:
            user_sid = owner_sid = "test-current-user"

            def create_file(self, path):
                self.temporary = path
                return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)

            def inspect(self, _path, *, directory):
                return {"owner": self.user_sid}

            def publish(self, descriptor, target):
                assert os.fstat(descriptor).st_size == len(b"new bytes")
                os.replace(self.temporary, target)
                self.temporary.write_bytes(b"Another operation owns this later file.")

            def discard(self, _descriptor):
                pytest.fail("A successfully published file must not be removed.")

        api = Model()
        target = tmp_path / "private.txt"
        privacy._write_native_sibling(target, b"new bytes", api)
        assert api.temporary.read_bytes() == b"Another operation owns this later file."
    else:
        target = tmp_path / "private.txt"
        original_publish = privacy._WindowsPermissions.publish
        observed = []

        def publish_with_swap_attempt(api, descriptor, destination):
            temporary = next(tmp_path.glob(".private.txt.*.tmp"))
            replacement = tmp_path / "attacker.txt"
            replacement.write_bytes(b"Must not replace private content.")
            with pytest.raises(PermissionError):
                os.replace(replacement, temporary)
            original_publish(api, descriptor, destination)
            temporary.write_bytes(b"Another operation owns this later file.")
            observed.append(temporary)

        monkeypatch.setattr(
            privacy._WindowsPermissions, "publish", publish_with_swap_attempt
        )
        privacy.atomic_write_owner_only_bytes(target, b"new bytes")
        assert observed[0].read_bytes() == b"Another operation owns this later file."
    assert target.read_bytes() == b"new bytes"


def test_private_parent_cannot_be_swapped_during_native_creation(tmp_path, monkeypatch):
    parent = tmp_path / "runtime"
    parent.mkdir()
    target = parent / "private.txt"
    if os.name == "nt":
        original_create = privacy._WindowsPermissions.create_file

        def attempt_parent_swap(api, temporary):
            with pytest.raises(PermissionError):
                parent.rename(tmp_path / "moved-runtime")
            return original_create(api, temporary)

        monkeypatch.setattr(
            privacy._WindowsPermissions, "create_file", attempt_parent_swap
        )
    privacy.atomic_write_owner_only_text(target, "safe")
    assert parent.is_dir()
    assert target.read_text(encoding="utf-8") == "safe"


@pytest.mark.parametrize("change", (False, True))
def test_native_directory_access_participates_in_sharing_and_avoids_acl_propagation(
    tmp_path, change
):
    class Kernel:
        def CreateFileW(self, _path, access, sharing, *_arguments):
            if change:
                assert access == privacy._MAXIMUM_ALLOWED
                assert sharing == 0
            else:
                assert access & privacy._FILE_LIST_DIRECTORY
                assert not sharing & 4
            return 123

        def GetFileInformationByHandle(self, _handle, information):
            information._obj.attributes = privacy._FILE_ATTRIBUTE_DIRECTORY
            return True

        def CloseHandle(self, _handle):
            return True

    api = privacy._WindowsPermissions.__new__(privacy._WindowsPermissions)
    api.kernel = Kernel()
    with api._existing(tmp_path, directory=True, change=change) as handle:
        assert handle == 123


@pytest.mark.parametrize("changed", ("file", "parent", "missing-parent"))
def test_native_publication_requires_the_actual_expected_file_and_pinned_parent(
    tmp_path, changed
):
    target = tmp_path / "private.txt"
    api = privacy._WindowsPermissions.__new__(privacy._WindowsPermissions)
    api._pinned_parent_handle = 2
    names = {
        1: privacy.ntpath.normcase(
            privacy.ntpath.normpath(privacy._WindowsPermissions._native_name(target))
        ),
        2: privacy.ntpath.normcase(
            privacy.ntpath.normpath(privacy._WindowsPermissions._native_name(tmp_path))
        ),
    }
    api._final_name = names.__getitem__
    api._verify_published_name(1, target)
    if changed == "file":
        names[1] += "wrong-name"
    elif changed == "parent":
        names[2] += "moved-parent"
    else:
        api._pinned_parent_handle = None
    with pytest.raises(OSError, match="unexpected path|parent changed"):
        api._verify_published_name(1, target)


def test_already_private_directory_never_reopens_for_exclusive_mutation(tmp_path):
    api = privacy._WindowsPermissions.__new__(privacy._WindowsPermissions)
    current_user = "S-1-5-21-100"
    calls = []

    @contextmanager
    def inspect_existing(_path, *, directory, change):
        assert directory
        assert not change
        calls.append(True)
        yield 123

    api._existing = inspect_existing
    api._read_acl = lambda _handle: {
        "owner": current_user,
        "current_user": current_user,
        "protected": True,
        "entries": [
            {
                "type": 0,
                "flags": 3,
                "mask": privacy._FILE_ALL_ACCESS,
                "sid": current_user,
            }
        ],
    }
    api.protect(tmp_path, directory=True)
    assert calls == [True]


def test_two_runs_can_share_private_root_while_one_retains_parent_handles(tmp_path):
    root = tmp_path / "runtime"
    first_run, second_run = root / "run-one", root / "run-two"
    for directory in (root, first_run, second_run):
        privacy.ensure_owner_only_directory(directory)
    pinned, completed = Event(), Event()

    def first_worker():
        @contextmanager
        def retain_parents():
            if os.name == "nt":
                with privacy._WindowsPermissions().pin_parent_chain(
                    first_run / "context.md"
                ):
                    yield
            else:
                yield

        with retain_parents():
            pinned.set()
            assert completed.wait(10)
            privacy.atomic_write_owner_only_text(
                first_run / "context.md", "first synthetic context"
            )

    def second_worker():
        assert pinned.wait(10)
        try:
            privacy.ensure_owner_only_directory(root)
            privacy.atomic_write_owner_only_text(
                second_run / "context.md", "second synthetic context"
            )
            privacy.atomic_write_owner_only_text(root / "last-run.json", "{}")
        finally:
            completed.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(first_worker), executor.submit(second_worker)]
        for future in futures:
            future.result(timeout=15)
    assert (first_run / "context.md").read_text() == "first synthetic context"
    assert (second_run / "context.md").read_text() == "second synthetic context"
    for path in (
        first_run / "context.md",
        second_run / "context.md",
        root / "last-run.json",
    ):
        privacy.assert_owner_only_path(path, directory=False)
