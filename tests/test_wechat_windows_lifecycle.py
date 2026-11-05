"""Exercise native Windows sync and closed-SQLite publication boundaries.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from contextlib import closing
import errno
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import wechat_media_event_revalidation as media
from app.core import wechat_native_resource_revalidation as native
from app.core import wechat_reconcile as reconcile
from app.core import wechat_resource_catalog_refresh as refresh
from app.core import wechat_resource_index as resources_module
from app.core.owner_only_files import OwnerOnlyFileError, owner_only_problem
from app.core.wechat_archive import wechat_archive_path
from tests.test_compact_wechat_sqlite import archive as archive
from tests.test_compact_wechat_sqlite import compactor
from tests.test_wechat_media_event_revalidation import (
    global_media_store as global_media_store,
    placeholder_store as placeholder_store,
    stale_media as stale_media,
)
from tests.test_wechat_native_resource_revalidation import native_metadata as native_metadata
from tests.test_wechat_reconcile import record, source
from tests.test_wechat_resource_index import resources as resources


def _operation(request, kind, tmp_path):
    if kind == "resources":
        fixture = request.getfixturevalue("resources")
        return SimpleNamespace(
            module=resources_module,
            run=lambda: resources_module.build_resource_index(fixture.store, fixture.snapshot),
            output=resources_module.resource_index_path(fixture.store),
            preserved=[fixture.history, fixture.source],
            count=9,
        )
    if kind in {"refresh", "native"}:
        fixture = request.getfixturevalue("native_metadata")
        candidate = fixture.original.with_name("windows-refresh.sqlite3")
        module = refresh if kind == "refresh" else native
        run = (
            lambda: refresh.stage_resource_catalog_refresh(fixture.store, candidate)
        ) if kind == "refresh" else (
            lambda: native.stage_native_resource_revalidation(
                fixture.store, candidate, conversation_ids=[fixture.conversation["id"]]
            )
        )
        return SimpleNamespace(
            module=module,
            run=run,
            output=candidate,
            preserved=[fixture.history, fixture.original],
            count=2,
        )
    if kind == "media":
        fixture = request.getfixturevalue("stale_media")
        candidate = fixture.index.with_name("windows-media.sqlite3")
        return SimpleNamespace(
            module=media,
            run=lambda: media.stage_media_event_revalidation(fixture.store, candidate),
            output=candidate,
            preserved=[fixture.history, fixture.index],
            count=2160,
        )
    if kind == "compact":
        database, workstation = request.getfixturevalue("archive")
        candidate = workstation / "windows-compacted.sqlite3"
        return SimpleNamespace(
            module=compactor,
            run=lambda: compactor.prepare(database, candidate, workstation / "receipt.json"),
            output=candidate,
            preserved=[database],
            count=428,
        )
    metadata = source()
    store = tmp_path / "reconciled"
    reconcile.reconcile_wechat_records(
        store, sources=[metadata], records=[record(metadata)], snapshot_id="first"
    )
    candidate = wechat_archive_path(store)
    return SimpleNamespace(
        module=reconcile,
        run=lambda: reconcile.reconcile_wechat_records(
            store,
            sources=[metadata],
            records=[record(metadata, "second", server_id="9223372036854775805")],
            snapshot_id="second",
        ),
        output=candidate,
        preserved=[],
        count=2,
    )


def _windows_lifecycle(monkeypatch, module, *, fail_sync=False):
    """Reject the exact CRT and file-sharing violations seen in Windows CI."""
    opened, connections, synchronized = {}, set(), []
    original_open, original_connect = Path.open, sqlite3.connect

    class Connection(sqlite3.Connection):
        def close(self):
            try:
                return super().close()
            finally:
                connections.discard(self)

    def connect(*args, **kwargs):
        connection = original_connect(*args, factory=Connection, **kwargs)
        connections.add(connection)
        return connection

    def open_file(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        opened[stream.fileno()] = stream
        return stream

    class WindowsOS:
        name = "nt"

        def __getattr__(self, name):
            return getattr(os, name)

        def open(self, path, *args, **kwargs):
            if Path(path).is_dir():
                raise PermissionError("The Windows CRT cannot open directory descriptors.")
            return os.open(path, *args, **kwargs)

        def fsync(self, descriptor):
            stream = opened[descriptor]
            if not stream.writable():
                raise OSError(errno.EBADF, "The Windows CRT requires a writable sync handle.")
            assert not connections, "Close SQLite handles before syncing a candidate."
            synchronized.append(Path(stream.name))
            if fail_sync:
                raise OSError(errno.EIO, "Synthetic durable-write failure")
            return os.fsync(descriptor)

        def replace(self, *args, **kwargs):
            assert not connections, "Close SQLite handles before atomic replacement."
            return os.replace(*args, **kwargs)

    class SQLite:
        def __getattr__(self, name):
            return connect if name == "connect" else getattr(sqlite3, name)

    if os.name != "nt":
        from app.core import windows_file_metadata

        # The foreign-host CRT facade also needs a metadata API boundary.
        # Real Windows keeps its native ChangeTime inspection unchanged.
        def change_time(path, *, descriptor=None):
            value = path.stat() if descriptor is None else os.fstat(descriptor)
            return value.st_ctime_ns

        monkeypatch.setattr(windows_file_metadata, "file_change_time", change_time)
    monkeypatch.setattr(Path, "open", open_file)
    monkeypatch.setattr(module, "os", WindowsOS())
    monkeypatch.setattr(module, "sqlite3", SQLite())
    return connections, synchronized


@pytest.mark.parametrize("kind", ["resources", "refresh", "native", "media", "compact", "reconcile"])
def test_candidates_close_sqlite_and_sync_writable_handles(request, kind, tmp_path, monkeypatch):
    operation = _operation(request, kind, tmp_path)
    before = {path: path.read_bytes() for path in operation.preserved}
    connections, synchronized = _windows_lifecycle(monkeypatch, operation.module)
    operation.run()
    assert synchronized
    assert not connections
    assert {path: path.read_bytes() for path in operation.preserved} == before
    if kind == "resources":
        assert owner_only_problem(operation.output) == ""
    with closing(sqlite3.connect(operation.output)) as output:
        assert output.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert output.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == operation.count


@pytest.mark.parametrize("kind", ["resources", "refresh", "native", "media", "compact", "reconcile"])
def test_candidate_sync_failures_propagate_without_changing_sources(request, kind, tmp_path, monkeypatch):
    operation = _operation(request, kind, tmp_path)
    originals = list(operation.preserved)
    if operation.output.exists():
        originals.append(operation.output)
    before = {path: path.read_bytes() for path in originals}
    connections, synchronized = _windows_lifecycle(monkeypatch, operation.module, fail_sync=True)
    with pytest.raises(OSError, match="Synthetic durable-write failure"):
        operation.run()
    assert synchronized
    assert not connections
    assert {path: path.read_bytes() for path in originals} == before
    if kind not in {"compact", "reconcile"}:
        assert not operation.output.exists()


def test_resource_boundary_failure_prevents_private_sqlite_writes(resources, monkeypatch):
    target = resources_module.resource_index_path(resources.store)
    target.write_bytes(b"Preserve the previous published catalog.")
    before = target.read_bytes(), resources.history.read_bytes()
    candidates = []

    def refuse_boundary(path):
        assert path.stat().st_size == 0
        candidates.append(path)
        raise OwnerOnlyFileError("Synthetic native access-control failure")

    def no_connection(*args, **kwargs):
        raise AssertionError("SQLite must not open before the private boundary is verified.")

    monkeypatch.setattr(resources_module, "ensure_owner_only", refuse_boundary)
    monkeypatch.setattr(resources_module, "sqlite3", SimpleNamespace(connect=no_connection))
    with pytest.raises(OwnerOnlyFileError, match="Synthetic native access-control failure"):
        resources_module.build_resource_index(resources.store, resources.snapshot)
    assert len(candidates) == 1
    assert not candidates[0].exists()
    assert (target.read_bytes(), resources.history.read_bytes()) == before
