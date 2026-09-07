"""Anchored deletion policy and native Windows adapter contract tests.

Code version: v1.0.0-codex.2

The model tests run on every host. Windows additionally runs the same file
receipt contract against actual handles and harmless files under tmp_path.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core import windows_anchored_delete as deletion


CONTENT = b"A synthetic read receipt, with no account or provider data.\n"


def _metadata(*, directory=False, inode=2**96 + 7, **changes):
    values = dict(
        st_dev=23, st_ino=inode, st_size=0 if directory else len(CONTENT),
        st_mtime_ns=1_700_000_000_000_000_007,
        st_mode=(stat.S_IFDIR | 0o700) if directory else (stat.S_IFREG | 0o600),
        st_nlink=1, st_file_attributes=0,
    )
    values.update(changes)
    return SimpleNamespace(**values)


def _identity(metadata):
    identity = tuple(int(getattr(metadata, name)) for name in (
        "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode",
    ))
    if os.name == "nt":
        identity = (*identity[:-1], identity[-1] & ~0o111)
    return identity


class ModelApi:
    """An explicit handle model; it never deletes an actual filesystem path."""

    def __init__(self):
        self.events = []
        self.entries = {
            "root": _metadata(directory=True, inode=101),
            "parent": _metadata(directory=True, inode=102),
            "file": _metadata(),
        }
        self.payload = CONTENT
        self.offset = 0
        self.deleted = False
        self.fail_open = None
        self.read_error = None
        self.disposition_error = None
        self.close_errors = set()
        self.after_read = None

    def open_root(self, workspace):
        self.events.append(("open_root", str(workspace)))
        if self.fail_open == "root":
            raise PermissionError("The root is unavailable.")
        return "root"

    def open_child(self, parent, component, *, directory):
        self.events.append(("open_child", parent, component, directory))
        name = "parent" if directory else "file"
        if self.fail_open == name:
            raise PermissionError("A writer or rename already owns an incompatible handle.")
        return name

    def metadata(self, handle):
        self.events.append(("metadata", handle))
        if handle == "file" and self.offset and self.after_read:
            self.after_read(self.entries[handle])
        return self.entries[handle]

    def read(self, handle, count):
        self.events.append(("read", handle, count))
        if self.read_error:
            raise self.read_error
        result = self.payload[self.offset:self.offset + count]
        self.offset += len(result)
        return result

    def mark_delete(self, handle):
        self.events.append(("delete", handle))
        if self.disposition_error:
            raise self.disposition_error
        self.deleted = True

    def close(self, handle):
        self.events.append(("close", handle))
        if handle in self.close_errors:
            raise OSError(f"Cannot close {handle}.")


@pytest.fixture
def model(monkeypatch):
    api = ModelApi()
    monkeypatch.setattr(deletion, "_WindowsFileApi", lambda: api)
    return api


def _delete(api, tmp_path, **changes):
    kwargs = dict(
        workspace=tmp_path, relative=Path("资料 parent") / "receipt.txt",
        workspace_identity=(23, 101), expected_identity=_identity(api.entries["file"]),
        expected_sha256=hashlib.sha256(CONTENT).hexdigest(), max_bytes=len(CONTENT),
    )
    kwargs.update(changes)
    return deletion.delete_read_verified_file(**kwargs)


def test_same_handles_remain_pinned_through_receipt_and_disposition(model, tmp_path):
    assert _delete(model, tmp_path) == len(CONTENT)
    assert model.deleted
    assert model.events[:5] == [
        ("open_root", str(tmp_path)), ("metadata", "root"),
        ("open_child", "root", "资料 parent", True), ("metadata", "parent"),
        ("open_child", "parent", "receipt.txt", False),
    ]
    assert model.events[-5:] == [
        ("metadata", "file"), ("delete", "file"),
        ("close", "file"), ("close", "parent"), ("close", "root"),
    ]
    assert [event[1] for event in model.events if event[0] == "read"] == ["file", "file"]


@pytest.mark.parametrize("relative", (
    "", ".", "..", "../outside", r"..\outside", "/outside", r"\outside",
    r"C:\outside", "C:outside", r"\\server\share\outside", "receipt.txt:stream",
    "NUL", "trailing.", "bad\x00name",
))
def test_invalid_relative_path_never_opens_a_handle(relative, model, tmp_path):
    with pytest.raises(ValueError, match="workspace-relative"):
        _delete(model, tmp_path, relative=Path(relative))
    assert model.events == []


@pytest.mark.parametrize("changes", (
    {"workspace": Path("relative")}, {"expected_sha256": "not a receipt"}, {"max_bytes": -1},
))
def test_invalid_receipt_request_never_opens_a_handle(changes, model, tmp_path):
    with pytest.raises(ValueError):
        _delete(model, tmp_path, **changes)
    assert model.events == []


def test_root_replacement_is_rejected_before_opening_children(model, tmp_path):
    with pytest.raises(RuntimeError, match="workspace changed"):
        _delete(model, tmp_path, workspace_identity=(23, 999))
    assert model.events[-1] == ("close", "root")
    assert not any(event[0] == "open_child" for event in model.events)


@pytest.mark.parametrize("handle", ("root", "parent", "file"))
def test_reparse_or_junction_at_each_level_is_rejected(handle, model, tmp_path):
    model.entries[handle].st_file_attributes = deletion._FILE_ATTRIBUTE_REPARSE_POINT
    with pytest.raises(ValueError, match="reparse"):
        _delete(model, tmp_path)
    assert not model.deleted
    opened = [event for event in model.events if event[0] in {"open_root", "open_child"}]
    closed = [event for event in model.events if event[0] == "close"]
    assert len(opened) == len(closed)


@pytest.mark.parametrize("handle", ("root", "file"))
@pytest.mark.parametrize("attribute,value", (("st_file_attributes", None), ("st_ino", 0)))
def test_missing_native_identity_or_reparse_evidence_fails_closed(
    handle, attribute, value, model, tmp_path,
):
    setattr(model.entries[handle], attribute, value)
    with pytest.raises(RuntimeError, match="Windows did not provide"):
        _delete(model, tmp_path)
    assert not model.deleted


@pytest.mark.parametrize("changes", (
    {"st_nlink": 2}, {"st_mode": stat.S_IFDIR | 0o700},
    {"st_mode": stat.S_IFLNK | 0o700}, {"st_size": len(CONTENT) + 1},
))
def test_hardlinks_nonregular_files_and_oversize_files_are_retained(changes, model, tmp_path):
    for key, value in changes.items():
        setattr(model.entries["file"], key, value)
    with pytest.raises(ValueError):
        _delete(model, tmp_path)
    assert not model.deleted
    assert not any(event[0] == "read" for event in model.events)


def test_replaced_file_with_same_bytes_is_rejected(model, tmp_path):
    receipt = _identity(model.entries["file"])
    model.entries["file"].st_ino += 1
    with pytest.raises(ValueError, match="no longer matches"):
        _delete(model, tmp_path, expected_identity=receipt)
    assert not model.deleted


@pytest.mark.parametrize("mutation", ("identity", "hardlink", "hash", "size", "limit"))
def test_changes_during_handle_verification_prevent_disposition(mutation, model, tmp_path):
    if mutation == "identity":
        model.after_read = lambda metadata: setattr(metadata, "st_mtime_ns", metadata.st_mtime_ns + 1)
    elif mutation == "hardlink":
        model.after_read = lambda metadata: setattr(metadata, "st_nlink", 2)
    elif mutation == "hash":
        model.payload = b"X" + CONTENT[1:]
    elif mutation == "size":
        model.payload = CONTENT[:-1]
    else:
        model.payload = CONTENT + b"X"
    with pytest.raises(ValueError):
        _delete(model, tmp_path)
    assert not model.deleted
    assert not any(event[0] == "delete" for event in model.events)
    assert model.events[-3:] == [("close", "file"), ("close", "parent"), ("close", "root")]


@pytest.mark.parametrize("level", ("root", "parent", "file"))
def test_incompatible_existing_handle_fails_closed_and_releases_owned_handles(level, model, tmp_path):
    model.fail_open = level
    with pytest.raises(PermissionError, match="unavailable|incompatible"):
        _delete(model, tmp_path)
    assert not model.deleted
    assert len([event for event in model.events if event[0] == "close"]) == {
        "root": 0, "parent": 1, "file": 2,
    }[level]


@pytest.mark.parametrize("stage", ("read", "disposition"))
def test_original_operation_failure_survives_secondary_close_errors(stage, model, tmp_path):
    original = PermissionError(f"Cannot {stage} the synthetic file.")
    if stage == "read":
        model.read_error = original
    else:
        model.disposition_error = original
    model.close_errors = {"file", "parent"}
    with pytest.raises(PermissionError) as caught:
        _delete(model, tmp_path)
    assert caught.value is original
    assert any("completion is unverified" in note for note in original.__notes__)
    assert model.events[-1] == ("close", "root")
    assert not model.deleted


def test_close_failure_after_disposition_is_never_reported_as_success(model, tmp_path):
    model.close_errors = {"file"}
    with pytest.raises(RuntimeError, match="completion is unverified") as caught:
        _delete(model, tmp_path)
    assert caught.value.delete_disposition_set is True
    assert isinstance(caught.value.__cause__, OSError)
    assert model.events[-1] == ("close", "root")


def test_empty_file_receipt_and_exact_limit_are_supported(model, tmp_path):
    model.payload = b""
    model.entries["file"].st_size = 0
    assert _delete(model, tmp_path, expected_sha256=hashlib.sha256(b"").hexdigest(), max_bytes=0) == 0
    assert model.deleted


def test_windows_receipt_normalizes_synthetic_execute_bits_only(monkeypatch):
    from app.core import computer_use_agent

    metadata = _metadata(st_mode=stat.S_IFREG | 0o777)
    monkeypatch.setattr(deletion, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(computer_use_agent, "os", SimpleNamespace(name="nt"))
    identity = deletion._file_identity(metadata, len(CONTENT))
    assert identity[-1] == stat.S_IFREG | 0o666
    assert computer_use_agent.WorkspaceController._stable_file_identity_from_stat(metadata) == identity
    monkeypatch.setattr(deletion, "os", SimpleNamespace(name="posix"))
    assert deletion._file_identity(metadata, len(CONTENT))[-1] == stat.S_IFREG | 0o777


@pytest.mark.parametrize("directory", (False, True))
def test_native_child_open_uses_parent_handle_and_restrictive_sharing(directory):
    api = deletion._WindowsFileApi.__new__(deletion._WindowsFileApi)
    calls = []

    def nt_open(output, access, attributes, status, share, options):
        attrs = ctypes.cast(attributes, ctypes.POINTER(deletion._ObjectAttributes)).contents
        name = attrs.ObjectName.contents
        calls.append((attrs.RootDirectory, ctypes.wstring_at(name.Buffer), name.Length, access, share, options))
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(202)
        return 0

    api._nt_open_file = nt_open
    api._adopt = lambda handle: deletion._PinnedHandle(handle, 303)
    pinned = api.open_child(deletion._PinnedHandle(101, 111), "资料 page.txt", directory=directory)
    assert pinned == deletion._PinnedHandle(202, 303)
    parent, component, byte_count, access, share, options = calls[0]
    assert (parent, component, byte_count) == (101, "资料 page.txt", len("资料 page.txt".encode("utf-16-le")))
    assert share == deletion._FILE_SHARE_READ
    assert bool(access & deletion._DELETE) is not directory
    assert options & deletion._FILE_OPEN_REPARSE_POINT
    assert options & deletion._FILE_SYNCHRONOUS_IO_NONALERT
    assert options & (deletion._FILE_DIRECTORY_FILE if directory else deletion._FILE_NON_DIRECTORY_FILE)


@pytest.mark.parametrize("path,expected", (
    (r"C:\workspace 用户", "\\\\?\\C:\\workspace 用户"),
    (r"\\server\share\workspace", "\\\\?\\UNC\\server\\share\\workspace"),
))
def test_native_root_opens_reparse_point_itself_and_denies_write_delete_sharing(path, expected):
    api = deletion._WindowsFileApi.__new__(deletion._WindowsFileApi)
    api._create_file = Mock(return_value=101)
    api._adopt = lambda handle: deletion._PinnedHandle(handle, 303)
    assert api.open_root(Path(path)).handle == 101
    assert api._create_file.call_args.args == (
        expected, deletion._GENERIC_READ, deletion._FILE_SHARE_READ, None, deletion._OPEN_EXISTING,
        deletion._FILE_FLAG_BACKUP_SEMANTICS | deletion._FILE_OPEN_REPARSE_POINT, None,
    )


def test_native_disposition_targets_the_verified_handle_without_path_unlink(monkeypatch):
    api = deletion._WindowsFileApi.__new__(deletion._WindowsFileApi)
    calls = []

    def set_information(handle, info_class, buffer, size):
        info = ctypes.cast(buffer, ctypes.POINTER(deletion._FileDispositionInfo)).contents
        calls.append((handle, info_class, info.DeleteFile, size))
        return 1

    api._set_information = set_information
    unlink = Mock(side_effect=AssertionError("Path deletion is forbidden."))
    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(os, "unlink", unlink)
    api.mark_delete(deletion._PinnedHandle(505, 606))
    assert calls == [(505, 4, 1, ctypes.sizeof(deletion._FileDispositionInfo))]
    unlink.assert_not_called()


def test_adoption_transfers_handle_ownership_exactly_once(monkeypatch):
    api = deletion._WindowsFileApi.__new__(deletion._WindowsFileApi)
    monkeypatch.setattr(os, "O_BINARY", 0x8000, raising=False)
    monkeypatch.setattr(os, "O_NOINHERIT", 0x80, raising=False)
    api._open_osfhandle = Mock(return_value=12)
    api._close_handle = Mock(return_value=1)
    close_fd = Mock()
    monkeypatch.setattr(os, "close", close_fd)
    pinned = api._adopt(34)
    api.close(pinned)
    api._open_osfhandle.assert_called_once_with(34, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
    close_fd.assert_called_once_with(12)
    api._close_handle.assert_not_called()


def test_failed_adoption_closes_unowned_handle_and_preserves_original_error(monkeypatch):
    api = deletion._WindowsFileApi.__new__(deletion._WindowsFileApi)
    monkeypatch.setattr(os, "O_BINARY", 0x8000, raising=False)
    monkeypatch.setattr(os, "O_NOINHERIT", 0x80, raising=False)
    original = OSError("The descriptor table is full.")
    api._open_osfhandle = Mock(side_effect=original)
    api._close_handle = Mock(return_value=0)
    with pytest.raises(OSError) as caught:
        api._adopt(34)
    assert caught.value is original
    api._close_handle.assert_called_once_with(34)
    assert "unadopted Windows handle" in original.__notes__[0]


@dataclass
class ContractCase:
    root: Path
    path: Path
    api: ModelApi | None
    receipt: tuple[int, int, int, int, int]
    root_identity: tuple[int, int]

    def delete(self, **changes):
        kwargs = dict(
            workspace=self.root, relative=self.path.relative_to(self.root),
            workspace_identity=self.root_identity, expected_identity=self.receipt,
            expected_sha256=hashlib.sha256(CONTENT).hexdigest(), max_bytes=len(CONTENT),
        )
        kwargs.update(changes)
        return deletion.delete_read_verified_file(**kwargs)

    def assert_retained(self):
        assert self.path.read_bytes() == CONTENT
        assert self.api is None or not self.api.deleted


@pytest.fixture(params=(
    ("model", ".txt"), ("model", ".cmd"), ("native", ".txt"), ("native", ".cmd"),
) if os.name == "nt" else (("model", ".txt"), ("model", ".cmd")), ids=lambda value: "-".join(value))
def receipt_case(request, tmp_path, monkeypatch):
    """Native tests are added on Windows; every core contract also runs as a model."""
    root = tmp_path / "workspace 用户"
    backend, suffix = request.param
    path = root / "资料 parent" / f"receipt{suffix}"
    path.parent.mkdir(parents=True)
    path.write_bytes(CONTENT)
    root_stat = root.stat()
    receipt = _identity(path.stat())
    api = None
    if backend == "model":
        api = ModelApi()
        for name, entry_path in (("root", root), ("parent", path.parent), ("file", path)):
            metadata = entry_path.stat()
            api.entries[name] = _metadata(**{
                key: getattr(metadata, key) for key in (
                    "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode", "st_nlink",
                )
            })
        monkeypatch.setattr(deletion, "_WindowsFileApi", lambda: api)
    return ContractCase(root, path, api, receipt, (root_stat.st_dev, root_stat.st_ino))


def test_receipt_contract_deletes_only_the_verified_file(receipt_case):
    sibling = receipt_case.path.parent / "keep.txt"
    sibling.write_text("Keep this sibling.", encoding="utf-8")
    assert receipt_case.delete() == len(CONTENT)
    if receipt_case.api is None:
        assert not receipt_case.path.exists()
    else:
        assert receipt_case.api.deleted
    assert sibling.read_text(encoding="utf-8") == "Keep this sibling."


def test_receipt_contract_rejects_wrong_hash_and_root_identity(receipt_case):
    with pytest.raises(ValueError, match="file changed"):
        receipt_case.delete(expected_sha256="0" * 64)
    receipt_case.assert_retained()
    with pytest.raises(RuntimeError, match="workspace changed"):
        receipt_case.delete(workspace_identity=(receipt_case.root_identity[0], 0))
    receipt_case.assert_retained()


def test_receipt_contract_rejects_hardlinked_file(receipt_case):
    link = receipt_case.path.parent / "other-link.txt"
    os.link(receipt_case.path, link)
    if receipt_case.api is not None:
        receipt_case.api.entries["file"].st_nlink = 2
    with pytest.raises(ValueError, match="unlinked regular file"):
        receipt_case.delete()
    receipt_case.assert_retained()
    assert link.read_bytes() == CONTENT


def test_receipt_contract_rejects_existing_writer(receipt_case):
    with receipt_case.path.open("r+b"):
        if receipt_case.api is not None:
            receipt_case.api.fail_open = "file"
        with pytest.raises(OSError):
            receipt_case.delete()
    receipt_case.assert_retained()


def test_receipt_contract_keeps_parent_and_file_pinned_until_disposition(receipt_case, monkeypatch):
    api = receipt_case.api or deletion._WindowsFileApi()
    original_metadata = api.metadata
    checked = False
    replacement = receipt_case.root / "replacement.txt"
    replacement.write_bytes(b"An unrelated replacement must remain untouched.")

    def inspect_pinned_handles(handle):
        nonlocal checked
        metadata = original_metadata(handle)
        if stat.S_ISREG(metadata.st_mode) and not checked:
            checked = True
            if receipt_case.api is None:
                # These attempts run against actual Windows sharing enforcement.
                with pytest.raises(OSError):
                    os.rename(receipt_case.path.parent, receipt_case.root / "moved-parent")
                with pytest.raises(OSError):
                    os.replace(replacement, receipt_case.path)
                with pytest.raises(OSError):
                    receipt_case.path.open("r+b")
            else:
                assert not any(event[0] == "close" for event in api.events)
                assert [event[1] for event in api.events if event[0] == "metadata"] == [
                    "root", "parent", "file",
                ]
        return metadata

    api.metadata = inspect_pinned_handles
    monkeypatch.setattr(deletion, "_WindowsFileApi", lambda: api)
    assert receipt_case.delete() == len(CONTENT)
    assert checked
    assert replacement.read_bytes() == b"An unrelated replacement must remain untouched."


@pytest.mark.parametrize("failure", (False, True))
def test_controller_windows_dispatch_marks_edit_only_after_native_completion(failure, tmp_path, monkeypatch):
    from app.core import computer_use_agent

    path = tmp_path / "receipt.cmd"
    path.write_bytes(CONTENT)
    controller = computer_use_agent.WorkspaceController(
        tmp_path, computer_use_agent.ComputerUseSettings(workspace_path=str(tmp_path)), lambda: False,
    )
    receipt = controller.execute({"action": "read", "path": "receipt.cmd"})
    assert receipt["ok"], receipt
    generation = controller.state.edit_generation
    recorded_identity = controller.state.read_receipts["receipt.cmd"][1]
    native = Mock(side_effect=PermissionError("The native handle is still owned.")) if failure else Mock(
        return_value=len(CONTENT),
    )
    monkeypatch.setattr(deletion, "delete_read_verified_file", native)
    # Replace this module's os reference only, without changing the host's Path implementation.
    monkeypatch.setattr(computer_use_agent, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    posix = Mock(side_effect=AssertionError("The Windows controller must not use POSIX deletion."))
    monkeypatch.setattr(controller, "_delete_posix_read_verified_file", posix)
    result = controller.execute({
        "action": "delete", "path": "receipt.cmd", "expected_sha256": receipt["sha256"],
    })
    native.assert_called_once_with(
        controller.workspace, Path("receipt.cmd"), controller._workspace_identity,
        recorded_identity, receipt["sha256"], computer_use_agent.MAX_CONTROLLER_DELETE_BYTES,
    )
    posix.assert_not_called()
    assert result["ok"] is not failure
    assert controller.state.edit_generation == generation + (0 if failure else 1)
    if failure:
        assert "native handle is still owned" in result["error"]
    else:
        assert result["deleted_bytes"] == len(CONTENT)
    assert path.read_bytes() == CONTENT
