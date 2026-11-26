"""Keep disk capacity refusals visible across the local refresh worker.

Code version: v1.1.1-codex.0
"""

import json
from pathlib import Path
import platform
import subprocess
from types import SimpleNamespace

from app.core import wechat_mac_refresh as refresh
from app.core import wechat_mac_refresh_worker as worker
from app.core.owner_only_files import ensure_owner_only_directory
from tests import test_wechat_mac_refresh as fixtures


inprocess_runtime = fixtures.inprocess_runtime
mac_archive = fixtures.mac_archive
refresh_archive = fixtures.refresh_archive


def test_low_disk_space_reaches_refresh_status_without_staging(refresh_archive, monkeypatch):
    root, history, session, _records = refresh_archive
    service = refresh.WechatMacRefreshService(
        root, key_registry=fixtures.Registry(), platform="darwin"
    )
    monkeypatch.setattr(refresh.shutil, "disk_usage", lambda _path: SimpleNamespace(free=0))
    monkeypatch.setattr(
        refresh, "read_database", lambda *_args, **_kwargs: SimpleNamespace(
            partial=False, close=lambda: None,
        ),
    )
    monkeypatch.setattr(
        refresh, "prepare_packet", lambda *_args, **_kwargs: (
            {"session_id": session, "messages": [{}]}, {"total": 1, "excluded": 0},
        ),
    )
    before = history.read_bytes()

    state = fixtures._wait(service, service.start(session)["task_id"])

    assert (state["state"], state["phase"], state["added"], state["candidates"]) == (
        "failed", "checking", 0, 1,
    )
    assert state["message"] == refresh.RefreshIssue.DISK_SPACE.value
    assert worker._safe_result(json.dumps(state))["message"] == state["message"]
    assert history.read_bytes() == before
    assert not refresh.bank.mac_recovery_bank_path(root).exists()
    assert not list(history.parent.glob(".mac-refresh-*"))


def test_mac_refresh_stages_on_same_volume_outside_the_archive(
    refresh_archive, monkeypatch, tmp_path,
):
    operation = fixtures._large_bank_publication(refresh_archive, monkeypatch)
    temporary = tmp_path / "system-private-temp"
    temporary.mkdir(mode=0o700)
    ensure_owner_only_directory(temporary)
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(subprocess, "check_output", lambda *_args, **_kwargs: str(temporary))
    monkeypatch.setattr(refresh.os, "getuid", lambda: temporary.stat().st_uid, raising=False)
    checked = []
    monkeypatch.setattr(
        refresh.shutil, "disk_usage",
        lambda directory: checked.append(Path(directory)) or SimpleNamespace(free=10**12),
    )

    assert operation.service._publish(operation.packets, fixtures.OWNER, operation.baseline) == {
        "published": True,
    }

    candidate, = operation.staged
    assert candidate.is_relative_to(temporary.resolve())
    assert checked == [temporary.resolve()]
    assert not candidate.parent.parent.exists()
