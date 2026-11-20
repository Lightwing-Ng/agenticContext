"""Global merge leases exclude new and in-flight local refresh publication.

Code version: v1.0.2-codex.0
"""

from contextlib import contextmanager
import json
import multiprocessing
import os
from types import SimpleNamespace
from unittest.mock import Mock

from flask import Blueprint, Flask
import pytest

from app.core import wechat_global_guard as guard
from app.core import wechat_mac_refresh as refresh
from app.core import wechat_mac_refresh_projection as projection
from app.core import wechat_macos_recovery_bank as bank
from app.core.wechat_archive import wechat_archive_path
from app.web.wechat_refresh_routes import (
    WECHAT_REFRESH_EXTENSION,
    register_wechat_refresh_routes,
)
from tests import test_wechat_mac_refresh as refresh_fixtures


mac_archive = refresh_fixtures.mac_archive
refresh_archive = refresh_fixtures.refresh_archive
inprocess_runtime = refresh_fixtures.inprocess_runtime
SESSION = "wechat:" + "a" * 64


def _hold_guard(root, connection):
    try:
        with guard.global_refresh_guard(root):
            connection.send("locked")
            connection.recv()
    finally:
        connection.close()


@contextmanager
def _foreign_guard(root):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_hold_guard, args=(str(root), child))
    process.start()
    child.close()
    try:
        assert parent.poll(10), "The isolated global lease did not start."
        assert parent.recv() == "locked"
        yield
    finally:
        if process.is_alive():
            parent.send("release")
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()
        assert process.exitcode == 0


def test_absent_guard_probe_does_not_create_runtime_files(tmp_path):
    root = tmp_path / "absent-store"
    assert guard.global_refresh_busy(root) is False
    assert not root.exists()


def test_global_merge_guard_is_cross_process_and_persists_one_lock_inode(tmp_path):
    root = tmp_path / "store"
    path = wechat_archive_path(root).with_name(guard.LOCK_FILENAME)
    with _foreign_guard(root):
        assert guard.global_refresh_busy(root) is True
        assert guard.global_refresh_busy(root) is True
        inode = path.stat().st_ino
        # Windows enforces the exclusive byte lease against foreign reads.
        if os.name == "nt":
            with pytest.raises(PermissionError):
                path.read_text()
        else:
            _assert_fixed_metadata(path, root)
    assert path.stat().st_ino == inode
    assert guard.global_refresh_busy(root) is False
    _assert_fixed_metadata(path, root)


def _assert_fixed_metadata(path, root):
    contents = path.read_text()
    assert json.loads(contents) == {
        "schema_version": 1,
        "purpose": "global_chat_repair_final_merge",
    }
    assert str(root) not in contents


def test_stale_ordinary_lock_file_does_not_report_a_running_repair(tmp_path):
    path = wechat_archive_path(tmp_path).with_name(guard.LOCK_FILENAME)
    path.parent.mkdir(parents=True)
    path.write_text("stale metadata from an interrupted process\n")
    inode = path.stat().st_ino
    assert guard.global_refresh_busy(tmp_path) is False
    with guard.global_refresh_guard(tmp_path):
        assert guard.global_refresh_busy(tmp_path) is True
    assert guard.global_refresh_busy(tmp_path) is False
    assert path.stat().st_ino == inode


def test_global_merge_guard_releases_after_an_exception(tmp_path):
    with pytest.raises(RuntimeError, match="synthetic merge failure"):
        with guard.global_refresh_guard(tmp_path):
            assert guard.global_refresh_busy(tmp_path) is True
            raise RuntimeError("synthetic merge failure")
    assert guard.global_refresh_busy(tmp_path) is False
    with guard.global_refresh_guard(tmp_path):
        assert guard.global_refresh_busy(tmp_path) is True


@pytest.mark.parametrize("kind", ("symlink", "hardlink"))
def test_guard_rejects_aliased_files_without_overwriting_them(tmp_path, kind):
    path = wechat_archive_path(tmp_path).with_name(guard.LOCK_FILENAME)
    path.parent.mkdir(parents=True)
    original = tmp_path / "unrelated.txt"
    original.write_text("preserve unrelated bytes")
    if kind == "symlink":
        path.symlink_to(original)
    else:
        path.hardlink_to(original)
    with pytest.raises(OSError):
        guard.global_refresh_busy(tmp_path)
    with pytest.raises(OSError):
        with guard.global_refresh_guard(tmp_path):
            pytest.fail("An aliased lock must not be acquired.")
    assert original.read_text() == "preserve unrelated bytes"


def test_active_global_merge_blocks_start_before_scope_or_source_work(
    tmp_path, monkeypatch
):
    registry = refresh_fixtures.Registry()
    service = refresh.WechatMacRefreshService(
        tmp_path, key_registry=registry, platform="darwin"
    )
    resolve = Mock(side_effect=AssertionError("Scope resolution must not run."))
    read = Mock(side_effect=AssertionError("Decryption must not run."))
    monkeypatch.setattr(refresh, "resolve_scope", resolve)
    monkeypatch.setattr(refresh, "read_database", read)
    with _foreign_guard(tmp_path):
        with pytest.raises(guard.GlobalRefreshBusy, match=guard.BUSY_MESSAGE):
            service.start(SESSION)
    assert registry.loaded_keys == 0
    resolve.assert_not_called()
    read.assert_not_called()
    assert service._active is None and service._tasks == {}


def test_lease_started_after_ui_task_prevents_replacement_and_preserves_user_bank(
    refresh_archive, monkeypatch
):
    root, history, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)
    database = refresh_fixtures._database(scope, records)
    try:
        packet, _counts = projection.prepare_packet(root, scope, database)
    finally:
        database.close()
    initial = {**packet, "messages": packet["messages"][:1]}
    candidate = root / "initial-bank.sqlite3"
    receipt = bank.stage_mac_recovery_bank(
        root, [initial], candidate, account_ids=[scope.account_id]
    )
    bank.publish_mac_recovery_bank(root, candidate, receipt)
    target = bank.mac_recovery_bank_path(root)
    original = target.read_bytes()
    service = refresh.WechatMacRefreshService(
        root, key_registry=refresh_fixtures.Registry(), platform="darwin"
    )
    monkeypatch.setattr(
        refresh,
        "read_database",
        lambda *_args, **_kwargs: refresh_fixtures._database(scope, records),
    )
    publish = bank.publish_mac_recovery_bank
    publication_attempts = []

    def guarded_publish(*args, before_replace=None, **kwargs):
        assert before_replace is not None
        # The UI task has completed staging before the foreign final merge starts.
        with _foreign_guard(root):
            publication_attempts.append(True)
            return publish(*args, before_replace=before_replace, **kwargs)

    monkeypatch.setattr(bank, "publish_mac_recovery_bank", guarded_publish)
    state = refresh_fixtures._wait(service, service.start(session)["task_id"])
    assert publication_attempts == [True]
    assert state["state"] == "failed" and state["partial"]
    assert state["added"] == 0
    assert target.read_bytes() == original
    assert not list(history.parent.glob(".mac-refresh-*"))
    assert guard.global_refresh_busy(root) is False

    # Retrying after the lease releases keeps the earlier user update and adds only
    # the remaining authenticated event, through the same real bank publication.
    monkeypatch.setattr(bank, "publish_mac_recovery_bank", publish)
    result = refresh_fixtures._wait(service, service.start(session)["task_id"])
    assert result["state"] == "succeeded"
    assert result["added"] == 1 and result["total"] == 4
    assert not list(history.parent.glob(".mac-refresh-*"))


def test_global_busy_route_uses_explicit_409_and_stays_loopback_only():
    start = Mock(side_effect=guard.GlobalRefreshBusy())
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.extensions[WECHAT_REFRESH_EXTENSION] = SimpleNamespace(start=start)
    blueprint = Blueprint("local_resources", __name__)
    register_wechat_refresh_routes(blueprint)
    app.register_blueprint(blueprint)
    client = app.test_client()
    blocked = client.post(
        "/api/browser/wechat/refresh",
        json={"session_id": SESSION},
        environ_overrides={"REMOTE_ADDR": "192.168.1.9"},
    )
    assert blocked.status_code == 403
    start.assert_not_called()
    response = client.post(
        "/api/browser/wechat/refresh", json={"session_id": SESSION}
    )
    assert response.status_code == 409
    assert response.json == {"error": guard.BUSY_MESSAGE}
    assert response.cache_control.no_store
    start.assert_called_once_with(SESSION)
