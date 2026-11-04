"""Defer optional Jury archive reads without losing preserved deliberations.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event

import pytest

from app.core.computer_use_agent import ComputerUseSettings
from app.core.config import CrawlConfig
from app.core.jury import JuryService


def _service(root):
    return JuryService(
        lambda: ComputerUseSettings(browser="edge"),
        CrawlConfig,
        root,
    )


def _archive(root, *, running=True):
    root.mkdir(parents=True)
    record = {
        "session_id": "a" * 32,
        "browser": "edge",
        "phase": "discussing" if running else "failed",
        "running": running,
        "started_at": "2026-11-01T00:00:00+00:00",
        "question": "Preserved question",
        "title": "Preserved deliberation",
        "rounds": [],
    }
    path = root / (record["session_id"] + ".json")
    path.write_text(json.dumps(record), encoding="utf-8")
    return path, record


def test_construction_and_unrelated_shutdown_do_not_read_archive(tmp_path, monkeypatch):
    root = tmp_path / "jury"
    path, _ = _archive(root)
    before = path.read_bytes()

    def forbidden(_service):
        pytest.fail("Unrelated application startup must not hydrate Jury archives.")

    monkeypatch.setattr(JuryService, "_load", forbidden)
    service = _service(root)
    service.stop_at_exit()
    assert service.records == {}
    assert service._archive_loaded is False
    assert path.read_bytes() == before


@pytest.mark.parametrize("operation", ["status", "sessions", "stop", "dismiss_failed"])
def test_explicit_jury_operations_load_preserved_records_once(
    tmp_path, monkeypatch, operation
):
    root = tmp_path / "jury"
    path, record = _archive(root, running=False)
    before = path.read_bytes()
    original = JuryService._load
    loaded = []

    def observed(service):
        loaded.append(service)
        original(service)

    monkeypatch.setattr(JuryService, "_load", observed)
    service = _service(root)
    assert loaded == []
    argument = "edge" if operation == "sessions" else record["session_id"]
    result = getattr(service, operation)(argument)
    assert result
    assert loaded == [service]
    service.sessions("edge")
    assert loaded == [service]
    if operation == "dismiss_failed":
        assert not path.exists()
    else:
        assert path.read_bytes() == before
    service.stop_at_exit()


def test_first_load_recovers_interrupted_state_without_rewriting_source(tmp_path):
    root = tmp_path / "jury"
    path, record = _archive(root)
    before = path.read_bytes()
    service = _service(root)
    restored = service.status(record["session_id"])
    assert restored["running"] is False
    assert restored["phase"] == "interrupted"
    assert restored["termination_reason"] == "service_restarted"
    assert service.threads == {}
    assert path.read_bytes() == before
    service.stop_at_exit()


def test_concurrent_first_read_loads_archive_only_once(tmp_path, monkeypatch):
    root = tmp_path / "jury"
    _, record = _archive(root)
    original = JuryService._load
    entered, release = Event(), Event()
    loaded = []

    def paused(service):
        loaded.append(service)
        entered.set()
        assert release.wait(5)
        original(service)

    monkeypatch.setattr(JuryService, "_load", paused)
    service = _service(root)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.status, record["session_id"])
        assert entered.wait(5)
        second = pool.submit(service.sessions, "edge")
        release.set()
        assert first.result(timeout=5)["phase"] == "interrupted"
        assert second.result(timeout=5)["sessions"][0]["phase"] == "interrupted"
    assert loaded == [service]
    service.stop_at_exit()


def test_lazy_archive_load_preserves_newer_in_memory_record(tmp_path):
    root = tmp_path / "jury"
    _, record = _archive(root)
    service = _service(root)
    current = {**record, "phase": "stopped", "running": False}
    service.records[record["session_id"]] = current
    assert service.status(record["session_id"])["phase"] == "stopped"
    assert service.records[record["session_id"]] is current
    service.stop_at_exit()


def test_safari_admission_restores_cleanup_flags_before_reuse(tmp_path, monkeypatch):
    from app.core import safari_automation

    root = tmp_path / "jury"
    path, record = _archive(root, running=False)
    record.update(
        browser="safari", safari_cleanup_pending=True, resource_cleanup_pending=True
    )
    path.write_text(json.dumps(record), encoding="utf-8")
    verified = []
    monkeypatch.setattr(
        safari_automation,
        "verify_safari_context_cleanup_ready",
        lambda: verified.append(True),
    )
    service = _service(root)
    service._require_safari_cleanup_ready()
    assert verified == [True]
    assert service.status(record["session_id"])["safari_cleanup_pending"] is False
    assert service.status(record["session_id"])["resource_cleanup_pending"] is False
    service.stop_at_exit()
