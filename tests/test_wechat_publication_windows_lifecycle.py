"""Verify synthetic APFS publications also honor Windows lifecycle boundaries.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import wechat_export_contacts as exports
from app.core import wechat_shared_storage_publish as publisher
from app.core.wechat_archive import _file_identity
from app.core.wechat_contacts import contact_registry_path
from tests.test_wechat_export_contacts import publish, registry as registry
from tests.test_wechat_shared_storage_publish import (
    _messages,
    _publish,
    _sidecar,
    _sidecar_info,
    publication as publication,
    shared_archive as shared_archive,
)
from tests.test_wechat_windows_lifecycle import _windows_lifecycle


@pytest.mark.parametrize("fail_sync", [False, True])
def test_export_publication_uses_writable_sync_and_releases_native_handles(
    registry, monkeypatch, fail_sync
):
    target = contact_registry_path(registry)
    before = target.read_bytes()
    original_open = Path.open

    def no_mac_lock(path, mode="r", *args, **kwargs):
        assert path != target or mode != "rb", (
            "Windows must not retain the macOS registry lock."
        )
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", no_mac_lock)
    monkeypatch.setattr(exports, "sys", SimpleNamespace(platform="win32"))
    connections, synchronized = _windows_lifecycle(
        monkeypatch, exports, fail_sync=fail_sync
    )
    if fail_sync:
        with pytest.raises(OSError, match="Synthetic durable-write failure"):
            publish(registry)
        with original_open(target, "rb") as unchanged:
            assert unchanged.read() == before
    else:
        assert publish(registry)["changed"]
    assert synchronized
    assert not connections
    with closing(sqlite3.connect(target)) as output:
        assert output.execute("PRAGMA quick_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("failure", [None, "sync", "sidecar"])
def test_shared_publication_closes_handles_and_preserves_rollback(
    publication, monkeypatch, failure
):
    sidecar = publication.history.with_name("media_events.sqlite3")
    original_info = _sidecar(sidecar, publication.history)
    original_sidecar = sidecar.read_bytes()
    connections, synchronized = _windows_lifecycle(
        monkeypatch, publisher, fail_sync=failure == "sync"
    )
    native_replace = publisher.os.replace
    failed = False

    def fail_sidecar_once(source, target):
        nonlocal failed
        if failure == "sidecar" and Path(target) == sidecar and not failed:
            failed = True
            raise OSError("Synthetic sidecar publication failure")
        return native_replace(source, target)

    monkeypatch.setattr(publisher.os, "replace", fail_sidecar_once)
    if failure:
        message = (
            "Synthetic durable-write failure"
            if failure == "sync"
            else "Synthetic sidecar publication failure"
        )
        with pytest.raises(OSError, match=message):
            _publish(publication)
    else:
        assert _publish(publication)["changed"]
    assert synchronized
    assert not connections
    assert _messages(publication.history) == publication.original_messages
    rollback = publication.backup / publication.history.name
    assert hashlib.sha256(rollback.read_bytes()).hexdigest() == publication.original_digest
    if failure == "sync":
        assert sidecar.read_bytes() == original_sidecar
    else:
        current = _sidecar_info(sidecar)
        assert json.loads(current.pop("history_file_identity")) == _file_identity(
            publication.history
        )
        assert current == {
            key: value for key, value in original_info.items()
            if key != "history_file_identity"
        }
    assert not list(publication.history.parent.glob(".wechat-share-*"))
