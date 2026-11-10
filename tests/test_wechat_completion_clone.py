"""Portable derived copies preserve bytes, private boundaries, and source leases.

Code version: v1.1.1-codex.0
"""

import hashlib
import io
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import wechat_completion_clone as clone
from app.core import wechat_secondary_completions as completions
from app.core import windows_file_metadata
from app.core.owner_only_files import owner_only_problem


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(clone, "IS_MACOS", False)
    source = tmp_path / "completed.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.executescript(completions._SCHEMA)
        connection.executemany(
            "INSERT INTO archive_info VALUES(?,?)",
            [
                ("schema_version", "1"),
                ("version", completions.PROOF_VERSION),
                ("staging_state", "complete"),
            ],
        )
    return source, tmp_path / "copied.sqlite3"


def _copy(source, destination, **kwargs):
    return clone.clone_completion_catalog(
        source, destination, expected_identity=clone._identity(source), **kwargs
    )


@pytest.mark.parametrize("reviewed", [False, True])
def test_non_macos_copy_keeps_exact_validated_catalog_bytes_and_private_mode(
    catalog, reviewed
):
    source, destination = catalog
    before = source.read_bytes()
    identity = clone._identity(source)
    kwargs = {"expected_sha256": hashlib.sha256(before).hexdigest()} if reviewed else {}
    _copy(source, destination, **kwargs)
    assert destination.read_bytes() == source.read_bytes() == before
    assert clone._identity(source) == identity
    assert not owner_only_problem(destination)
    assert list(source.parent.glob(".completion-copy-*")) == []


@pytest.mark.parametrize("failure", [False, True])
def test_macos_keeps_mandatory_archive_clone_without_byte_fallback(
    catalog, monkeypatch, failure
):
    source, destination = catalog
    monkeypatch.setattr(clone, "IS_MACOS", True)
    calls = []

    def mandatory(original, target):
        calls.append((original, target))
        if failure:
            raise ValueError("APFS clonefile failed; no full-copy fallback")

    monkeypatch.setattr(clone, "_clonefile", mandatory)
    monkeypatch.setattr(
        clone, "_catalog_contract", lambda *_: pytest.fail("No byte fallback")
    )
    if failure:
        with pytest.raises(ValueError, match="no full-copy fallback"):
            _copy(source, destination)
    else:
        _copy(source, destination)
    assert calls == [(source, destination)]
    assert not destination.exists()


@pytest.mark.parametrize(
    "defect", ["schema", "version", "incomplete", "raw_archive", "missing_witnesses"]
)
def test_non_macos_copy_rejects_other_archives_and_incomplete_catalogs(catalog, defect):
    source, destination = catalog
    with sqlite3.connect(source) as connection:
        if defect == "missing_witnesses":
            connection.execute("DROP TABLE witnesses")
        elif defect == "raw_archive":
            connection.execute("DROP TABLE entries")
        else:
            key, value = {
                "schema": ("schema_version", "4"),
                "version": ("version", "unknown"),
                "incomplete": ("staging_state", "in_progress"),
            }[defect]
            connection.execute(
                "UPDATE archive_info SET value=? WHERE key=?", (value, key)
            )
    with pytest.raises(ValueError, match="derived completion"):
        _copy(source, destination)
    assert not destination.exists()
    assert list(source.parent.glob(".completion-copy-*")) == []


def test_reviewed_hash_mismatch_leaves_no_destination_or_partial_copy(catalog):
    source, destination = catalog
    with pytest.raises(ValueError, match="SHA-256 changed"):
        _copy(source, destination, expected_sha256="0" * 64)
    assert not destination.exists()
    assert list(source.parent.glob(".completion-copy-*")) == []


@pytest.mark.parametrize("replacement", [False, True])
def test_source_change_during_streaming_fails_closed(catalog, monkeypatch, replacement):
    source, destination = catalog
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    copy_bytes = clone._copy_bytes

    def changed(original, output, size):
        result = copy_bytes(original, output, size)
        if replacement:
            other = source.with_name("other.sqlite3")
            other.write_bytes(source.read_bytes())
            other.replace(source)
        else:
            with source.open("ab") as stream:
                stream.write(b"changed")
        return result

    monkeypatch.setattr(clone, "_copy_bytes", changed)
    with pytest.raises(ValueError, match="changed during copying"):
        _copy(source, destination, expected_sha256=digest)
    assert not destination.exists()
    assert list(source.parent.glob(".completion-copy-*")) == []


def test_racing_destination_is_never_overwritten(catalog, monkeypatch):
    source, destination = catalog
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    copy_bytes = clone._copy_bytes

    def raced(original, output, size):
        result = copy_bytes(original, output, size)
        destination.write_bytes(b"independent destination")
        return result

    monkeypatch.setattr(clone, "_copy_bytes", raced)
    with pytest.raises(FileExistsError):
        _copy(source, destination, expected_sha256=digest)
    assert destination.read_bytes() == b"independent destination"
    assert list(source.parent.glob(".completion-copy-*")) == []


def test_replaced_temporary_name_after_link_is_preserved(catalog, monkeypatch):
    source, destination = catalog
    link = clone.os.link
    replacement = source.with_name("replacement.sqlite3")
    replacement.write_bytes(b"independent temporary replacement")
    replaced = []

    def raced(temporary, target):
        link(temporary, target)
        replacement.replace(temporary)
        replaced.append(Path(temporary))

    monkeypatch.setattr(clone.os, "link", raced)
    with pytest.raises(ValueError, match="temporary name was replaced"):
        _copy(source, destination)
    assert replaced[0].read_bytes() == b"independent temporary replacement"
    assert destination.read_bytes() == source.read_bytes()


@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
def test_interrupted_stream_removes_only_its_private_temp(
    catalog, monkeypatch, failure
):
    source, destination = catalog
    before = source.read_bytes()

    def interrupted(original, output, size):
        if output is not None:
            output.write(original.read(16))
        raise failure("interrupted copy")

    monkeypatch.setattr(clone, "_copy_bytes", interrupted)
    with pytest.raises(failure, match="interrupted copy"):
        _copy(source, destination, expected_sha256=hashlib.sha256(before).hexdigest())
    assert source.read_bytes() == before
    assert not destination.exists()
    assert list(source.parent.glob(".completion-copy-*")) == []


def test_private_boundary_is_applied_before_any_bytes(catalog, monkeypatch):
    source, destination = catalog
    called = []

    def no_boundary(path):
        assert Path(path).read_bytes() == b""
        called.append(path)
        raise OSError("private boundary unavailable")

    monkeypatch.setattr(clone, "ensure_owner_only", no_boundary)
    with pytest.raises(OSError, match="private boundary unavailable"):
        _copy(source, destination)
    assert called
    assert not destination.exists()
    assert list(source.parent.glob(".completion-copy-*")) == []


def test_streaming_buffer_and_growth_are_bounded(monkeypatch):
    monkeypatch.setattr(clone, "COPY_BLOCK_BYTES", 7)
    reads = []

    class Input(io.BytesIO):
        def read(self, size):
            reads.append(size)
            return super().read(size)

    payload = b"exact synthetic bytes" * 10
    output = io.BytesIO()
    assert (
        clone._copy_bytes(Input(payload), output, len(payload))
        == hashlib.sha256(payload).hexdigest()
    )
    assert output.getvalue() == payload
    assert max(reads) <= 7
    with pytest.raises(ValueError, match="grew during bounded copying"):
        clone._copy_bytes(Input(payload + b"extra"), io.BytesIO(), len(payload))


def test_changed_source_before_copy_cannot_be_rebased_to_a_new_identity(catalog):
    source, destination = catalog
    identity = clone._identity(source)
    with source.open("ab") as stream:
        stream.write(b"external change")
    with pytest.raises(ValueError, match="source identity changed"):
        clone.clone_completion_catalog(source, destination, expected_identity=identity)
    assert not destination.exists()


@pytest.mark.parametrize("windows", [False, True])
def test_completion_file_flush_uses_the_platform_supported_handle(
    catalog, monkeypatch, windows
):
    source, _destination = catalog
    before = source.read_bytes()
    opened, flushed = [], []
    real_open = Path.open

    def track(path, mode="r", *args, **kwargs):
        opened.append(mode)
        return real_open(path, mode, *args, **kwargs)

    def fsync(descriptor):
        if windows and opened[-1] == "rb":
            raise OSError(9, "Bad file descriptor")
        flushed.append(descriptor)

    monkeypatch.setattr(clone, "IS_WINDOWS", windows)
    monkeypatch.setattr(Path, "open", track)
    monkeypatch.setattr(clone, "os", SimpleNamespace(fsync=fsync))
    clone.sync_completion_file(source)
    assert opened == ["r+b" if windows else "rb"]
    assert len(flushed) == 1
    assert source.read_bytes() == before


@pytest.mark.parametrize("windows", [False, True])
def test_completion_directory_flush_keeps_posix_without_a_windows_directory_fd(
    catalog, monkeypatch, windows
):
    source, _destination = catalog
    calls = []

    def opened(path, flags):
        assert not windows, "Windows cannot use a POSIX directory descriptor."
        calls.append(("open", path, flags))
        return 42

    monkeypatch.setattr(clone, "IS_WINDOWS", windows)
    monkeypatch.setattr(
        clone,
        "os",
        SimpleNamespace(
            open=opened,
            O_RDONLY=0,
            fsync=lambda descriptor: calls.append(("fsync", descriptor)),
            close=lambda descriptor: calls.append(("close", descriptor)),
        ),
    )
    clone.sync_completion_directory(source.parent)
    assert calls == (
        [] if windows else [("open", source.parent, 0), ("fsync", 42), ("close", 42)]
    )


def _windows_metadata(monkeypatch, source, *, changed_field=None):
    """Model Windows creation-time stat and change-time fstat in this module only."""
    real_path = Path
    real_os = clone.os
    source_inode = (source.stat().st_dev, source.stat().st_ino)
    births = {}
    mutation = {"offset": 0}

    def fields(state):
        values = {name: getattr(state, name) for name in dir(state) if name.startswith("st_")}
        key = state.st_dev, state.st_ino
        values["st_birthtime_ns"] = births.setdefault(key, state.st_ctime_ns)
        return values

    class WindowsStatPath(type(Path())):
        def lstat(self):
            values = fields(super().lstat())
            values["st_ctime_ns"] = values["st_birthtime_ns"]
            return SimpleNamespace(**values)

    def fstat(descriptor):
        state = real_os.fstat(descriptor)
        values = fields(state)
        # This models CPython 3.13's distinct ctime semantics, even for a new file.
        values["st_ctime_ns"] = values["st_birthtime_ns"] + 10_000
        if changed_field and (state.st_dev, state.st_ino) == source_inode:
            values["st_" + changed_field] += 1
        return SimpleNamespace(**values)

    def change_time(path, *, descriptor=None):
        state = real_os.fstat(descriptor) if descriptor is not None else real_path(path).lstat()
        offset = mutation["offset"] if (state.st_dev, state.st_ino) == source_inode else 0
        return state.st_ctime_ns + offset

    monkeypatch.setattr(clone, "IS_WINDOWS", True)
    monkeypatch.setattr(clone, "Path", WindowsStatPath)
    monkeypatch.setattr(clone, "os", SimpleNamespace(**{**vars(real_os), "fstat": fstat}))
    monkeypatch.setattr(windows_file_metadata, "file_change_time", change_time)
    return mutation


def test_windows_copy_compares_matching_native_metadata_instead_of_split_ctime(
    catalog, monkeypatch
):
    source, destination = catalog
    _windows_metadata(monkeypatch, source)
    _copy(source, destination)
    assert destination.read_bytes() == source.read_bytes()
    assert not list(source.parent.glob(".completion-copy-*"))


@pytest.mark.parametrize("field", ["dev", "ino", "size", "mtime_ns", "birthtime_ns"])
def test_windows_copy_still_rejects_each_open_descriptor_identity_mismatch(
    catalog, monkeypatch, field
):
    source, destination = catalog
    _windows_metadata(monkeypatch, source, changed_field=field)
    with pytest.raises(ValueError, match="opened completion source identity"):
        _copy(source, destination)
    assert not destination.exists()


def test_windows_native_change_time_drift_cannot_hide_behind_retained_birthtime(
    catalog, monkeypatch
):
    source, destination = catalog
    mutation = _windows_metadata(monkeypatch, source)
    copy_bytes = clone._copy_bytes

    def changed(original, output, size):
        result = copy_bytes(original, output, size)
        mutation["offset"] += 1
        return result

    monkeypatch.setattr(clone, "_copy_bytes", changed)
    with pytest.raises(ValueError, match="changed during checksum validation"):
        _copy(source, destination)
    assert not destination.exists()


def test_windows_copy_requires_matching_path_and_descriptor_change_time(
    catalog, monkeypatch
):
    source, destination = catalog
    _windows_metadata(monkeypatch, source)
    change_time = windows_file_metadata.file_change_time

    def changed(path, *, descriptor=None):
        return change_time(path, descriptor=descriptor) + int(descriptor is not None)

    monkeypatch.setattr(windows_file_metadata, "file_change_time", changed)
    with pytest.raises(ValueError, match="opened completion source identity"):
        _copy(source, destination)
    assert not destination.exists()


@pytest.mark.parametrize("failure", ["birthtime", "native_api", "nonregular"])
def test_windows_copy_cannot_fall_back_when_native_metadata_is_unavailable(
    catalog, monkeypatch, failure
):
    source, destination = catalog
    _windows_metadata(monkeypatch, source)
    fstat = clone.os.fstat

    def unavailable(descriptor):
        state = vars(fstat(descriptor)).copy()
        if failure == "birthtime":
            del state["st_birthtime_ns"]
        else:
            state["st_mode"] = 0
        return SimpleNamespace(**state)

    if failure == "native_api":
        def unavailable_api(*_args, **_kwargs):
            raise OSError("Native metadata API failed")

        monkeypatch.setattr(windows_file_metadata, "file_change_time", unavailable_api)
        error, pattern = OSError, "Native metadata API failed"
    else:
        monkeypatch.setattr(clone.os, "fstat", unavailable)
        error = ValueError
        pattern = "native creation metadata" if failure == "birthtime" else "regular opened file"
    with pytest.raises(error, match=pattern):
        _copy(source, destination)
    assert not destination.exists()
