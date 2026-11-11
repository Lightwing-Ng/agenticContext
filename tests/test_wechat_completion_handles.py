"""Completion writers release owned handles before publication or failed cleanup.

Code version: v1.0.0-codex.0
"""

from pathlib import Path
import os
import sqlite3

import pytest

from app.core import wechat_completion_stream_copy as copier
from app.core import wechat_secondary_completions as completions
from tests import test_wechat_secondary_completions as fixtures


mac_archive = fixtures.mac_archive
bank_archive = fixtures.bank_archive
source_binding = fixtures.source_binding
witness = fixtures.witness
completion_fixture = fixtures.completion_fixture


class _ModuleProxy:
    def __init__(self, module, **overrides):
        self._module = module
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(self._module, name)


@pytest.mark.parametrize("seed", ["new", "base", "published"])
def test_stage_closes_all_connections_before_candidate_publication(
    completion_fixture, monkeypatch, tmp_path, seed
):
    fixture = completion_fixture
    arguments = {}
    if seed != "new":
        initial = fixtures._stage(fixture)
        if seed == "base":
            arguments = {
                "base_candidate_path": fixture["candidate"],
                "base_candidate_sha256": initial["candidate_sha256"],
            }
        else:
            fixtures._publish(fixture, initial)
        fixture["candidate"] = tmp_path / "next-candidate.sqlite3"
    connections = []

    class TrackedConnection(sqlite3.Connection):
        closed = False

        def close(self):
            self.closed = True
            return super().close()

    def connect(*args, **kwargs):
        connection = sqlite3.connect(*args, **kwargs, factory=TrackedConnection)
        connections.append(connection)
        return connection

    monkeypatch.setattr(completions, "sqlite3", _ModuleProxy(sqlite3, connect=connect))
    stage = completions.stage_secondary_message_completions(
        fixture["root"], fixture["candidate"], [fixture["packet"]], **arguments
    )
    assert connections and all(connection.closed for connection in connections)
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    assert completions.publish_secondary_message_completions(
        fixture["root"], fixture["candidate"],
        candidate_sha256=stage["candidate_sha256"],
    )["published"]


def test_failed_stage_closes_handles_and_keeps_committed_evidence_incomplete(
    completion_fixture, monkeypatch
):
    fixture = completion_fixture
    connections = []

    class TrackedConnection(sqlite3.Connection):
        closed = False

        def close(self):
            self.closed = True
            return super().close()

    def connect(*args, **kwargs):
        connection = sqlite3.connect(*args, **kwargs, factory=TrackedConnection)
        connections.append(connection)
        return connection

    def packets():
        yield fixture["packet"]
        raise RuntimeError("Interrupted after a verified packet")

    monkeypatch.setattr(completions, "sqlite3", _ModuleProxy(sqlite3, connect=connect))
    with pytest.raises(RuntimeError, match="verified packet"):
        completions.stage_secondary_message_completions(
            fixture["root"], fixture["candidate"], packets()
        )
    assert connections and all(connection.closed for connection in connections)
    with sqlite3.connect(fixture["candidate"]) as retained:
        assert retained.execute("SELECT COUNT(*) FROM entries").fetchone() == (2,)
        assert retained.execute(
            "SELECT value FROM archive_info WHERE key='staging_state'"
        ).fetchone() == ("in_progress",)


@pytest.mark.parametrize("failure", [FileExistsError, KeyboardInterrupt])
def test_stream_failed_reservation_closes_owned_descriptor_before_unlink(
    tmp_path, monkeypatch, failure
):
    opened = set()
    target = tmp_path / "racing.sqlite3"
    target.write_bytes(b"Independent destination")
    real_open, real_close = os.open, os.close

    class RestrictedPath(type(Path())):
        def unlink(self, *args, **kwargs):
            state = self.lstat()
            for descriptor in opened:
                held = os.fstat(descriptor)
                if (state.st_dev, state.st_ino) == (held.st_dev, held.st_ino):
                    raise PermissionError(32, "Windows cannot unlink this open inode")
            return super().unlink(*args, **kwargs)

    def open_descriptor(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        opened.add(descriptor)
        return descriptor

    def close_descriptor(descriptor):
        real_close(descriptor)
        opened.remove(descriptor)

    def reserve(*_args):
        raise failure("Original reservation failure")

    monkeypatch.setattr(copier, "Path", RestrictedPath)
    monkeypatch.setattr(
        copier, "os", _ModuleProxy(
            os, open=open_descriptor, close=close_descriptor, link=reserve
        )
    )
    with pytest.raises(failure, match="Original reservation failure"):
        copier.create_stream_output(target)
    assert not opened
    assert target.read_bytes() == b"Independent destination"
    assert not list(tmp_path.glob(".completion-stream-*"))
