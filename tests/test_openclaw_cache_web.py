"""Provider-separated OpenClaw archive routes and local worker boundaries.

Code version: v1.0.1-codex.0
"""

from unittest.mock import Mock, patch

import pytest

from app.core.config import CrawlConfig
from app.core.job_lock import CacheTaskLock
from app.core.openclaw_history import OPENCLAW_SOURCE_LABELS
from app.core.openclaw_history_service import OpenClawHistoryService
from app.core.state import TaskSnapshot, TaskState


@pytest.mark.parametrize("source,label", OPENCLAW_SOURCE_LABELS.items())
def test_archive_cache_has_own_source_and_no_browser_controls(client, source, label):
    response = client.get(f"/cache/{source}?content_mode=media")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert f'aria-label="Cache source: {label}"' in html
    assert 'data-cache-page-content-mode="text"' in html
    assert 'data-cache-local="true"' in html
    assert "Read archived sessions" in html
    assert "Browse cached sessions" not in html
    assert 'data-dock-section="local-resources"' in html
    assert "data-browser-session-panel" not in html
    assert "data-cache-content-mode-option" not in html
    assert "OpenClaw does not need to be installed or running." in html
    assert client.get(f"/cache/{source}/media/safari").status_code == 404


def test_existing_cache_lists_separate_openclaw_origins(client):
    html = client.get("/cache/chatgpt").get_data(as_text=True)
    for source, label in OPENCLAW_SOURCE_LABELS.items():
        assert f'data-cache-source-switcher-option="{source}"' in html
        assert label in html


def test_unattributed_archive_explains_infrastructure_messages(client):
    html = client.get("/cache/unknown_openclaw").get_data(as_text=True)
    assert "Delivery mirrors, gateway notices" in html
    assert "without inferred model attribution" in html
    assert "Delivery mirrors, gateway notices" not in client.get("/cache/chatgpt_openclaw").get_data(as_text=True)


def test_start_uses_archived_source_and_text_mode(client, app):
    service = app.extensions["cache_runtimes"]["chatgpt_openclaw"]["text"].service
    service.start = Mock()
    response = client.post("/cache/chatgpt_openclaw/start", data={"cache_content_mode": "media"})
    assert response.status_code == 302
    assert response.location == "/cache/chatgpt_openclaw"
    assert service.start.call_args.kwargs == {"content_mode": "text"}


def test_worker_reads_preserved_archive_without_live_directory(tmp_path):
    state = TaskState("test", snapshot_factory=TaskSnapshot)
    service = OpenClawHistoryService(
        state, tmp_path, CacheTaskLock(tmp_path / "lock"), source="gemini_openclaw",
    )
    result = {
        "sessions": 0, "messages": 0, "imported_sessions": 0, "imported_messages": 0,
        "files_total": 1, "files_scanned": 1, "failed": 0, "stopped": False,
    }

    class ImmediateThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    with patch("app.core.openclaw_history_service.Thread", ImmediateThread), patch(
        "app.core.openclaw_history_service.import_openclaw_history", return_value=result,
    ) as importer:
        service.start(CrawlConfig())
    assert importer.call_args.args == (tmp_path,)
    assert importer.call_args.kwargs["source"] == "gemini_openclaw"
    assert "source_directory" not in importer.call_args.kwargs
    assert state.snapshot()["phase"] == "finished"
    assert state.snapshot()["discovery_complete"]


@pytest.mark.parametrize("failed,stopped", [(1, False), (0, True)])
def test_partial_worker_never_reports_complete(tmp_path, failed, stopped):
    state = TaskState("test", snapshot_factory=TaskSnapshot)
    service = OpenClawHistoryService(
        state, tmp_path, CacheTaskLock(tmp_path / "lock"), source="chatgpt_openclaw",
    )
    result = {
        "sessions": 0, "messages": 0, "imported_sessions": 0, "imported_messages": 0,
        "files_total": 1, "files_scanned": 1, "failed": failed, "stopped": stopped,
    }
    with patch("app.core.openclaw_history_service.import_openclaw_history", return_value=result):
        service._run()
    assert not state.snapshot()["discovery_complete"]
    assert state.snapshot()["phase"] == ("stopped" if stopped else "failed")
