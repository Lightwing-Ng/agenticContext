"""Runtime shutdown must discard derived work without losing source observations.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from contextlib import closing, contextmanager
import os
from pathlib import Path
import subprocess
import sys
from threading import Event

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core import wechat_contact_embedded as embedded
from tests import test_wechat_contact_analysis as ordinary
from tests import test_wechat_contact_native_embedded as native


contact_analysis_store = ordinary.contact_analysis_store
native_activity_store = native.native_activity_store


@pytest.fixture(autouse=True)
def isolated_jobs(monkeypatch):
    monkeypatch.setattr(analysis, "_JOBS", {})
    monkeypatch.setattr(analysis, "_JOB_CANCELLATIONS", {})
    yield
    analysis.cancel_contact_analysis_jobs()
    for _, future in analysis._JOBS.values():
        try:
            future.result(timeout=5)
        except analysis._ContactAnalysisCancelled:
            pass
        except Exception:
            if not future.cancelled():
                raise


def _job(fixture, contact_id):
    return analysis._JOBS[(str(fixture.store.resolve()), contact_id)][1]


def test_shutdown_cancels_active_sql_and_queued_jobs(
    contact_analysis_store, monkeypatch
):
    fixture = contact_analysis_store
    entered = Event()

    def long_query(*_args):
        with closing(analysis._build_connection(":memory:")) as connection:
            connection.create_function("entered", 0, lambda: entered.set() or 1)
            connection.execute(
                "WITH RECURSIVE values_to_sum(n) AS (SELECT 1 UNION ALL "
                "SELECT n+1 FROM values_to_sum WHERE n<1000000000) "
                "SELECT sum(n) FROM values_to_sum WHERE entered()=1"
            ).fetchone()

    monkeypatch.setattr(analysis, "_build_index", long_query)
    analysis.query_contact_analysis(fixture.store, ordinary.CID, background=True)
    assert entered.wait(5)
    analysis.query_contact_analysis(fixture.store, ordinary.OTHER_CID, background=True)
    running, queued = _job(fixture, ordinary.CID), _job(fixture, ordinary.OTHER_CID)
    assert analysis.cancel_contact_analysis_jobs() == 2
    assert queued.cancelled()
    with pytest.raises(analysis._ContactAnalysisCancelled):
        running.result(timeout=2)


def test_cancelled_worker_does_not_wait_for_another_build_lock(
    contact_analysis_store, monkeypatch
):
    fixture = contact_analysis_store
    entered = Event()
    acquire = analysis._build_lock

    @contextmanager
    def observed_lock():
        entered.set()
        with acquire():
            yield

    monkeypatch.setattr(analysis, "_build_lock", observed_lock)
    analysis._BUILD_LOCK.acquire()
    try:
        analysis.query_contact_analysis(fixture.store, ordinary.CID, background=True)
        assert entered.wait(5)
        assert analysis.cancel_contact_analysis_jobs() == 1
        with pytest.raises(analysis._ContactAnalysisCancelled):
            _job(fixture, ordinary.CID).result(timeout=2)
    finally:
        analysis._BUILD_LOCK.release()


def test_shutdown_snapshot_rejects_admission_from_an_older_request(
    contact_analysis_store, monkeypatch
):
    fixture = contact_analysis_store
    contact = analysis._contact

    def stop_during_lookup(*args):
        result = contact(*args)
        analysis.cancel_contact_analysis_jobs()
        return result

    monkeypatch.setattr(analysis, "_contact", stop_during_lookup)
    result = analysis.query_contact_analysis(
        fixture.store, ordinary.CID, background=True
    )
    assert result["status"] == "indexing"
    assert not analysis._JOBS
    assert not analysis._JOB_CANCELLATIONS


def test_cancelled_refresh_keeps_ready_cache_and_can_retry(
    contact_analysis_store, monkeypatch
):
    fixture = contact_analysis_store
    ready = analysis.query_contact_analysis(fixture.store, ordinary.CID)
    cache = analysis.contact_analysis_cache_path(fixture.store, ordinary.CID)
    before = (
        cache.read_bytes(),
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
    )
    entered, release = Event(), Event()
    original = analysis._copy_complete_event_index

    def paused(*args):
        result = original(*args)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(analysis, "FORWARDED_CLOCK_VERSION", "shutdown-test-clock")
    with monkeypatch.context() as pause:
        pause.setattr(analysis, "_copy_complete_event_index", paused)
        analysis.query_contact_analysis(fixture.store, ordinary.CID, background=True)
        try:
            assert entered.wait(5)
            assert analysis.cancel_contact_analysis_jobs() == 1
        finally:
            release.set()
        with pytest.raises(analysis._ContactAnalysisCancelled):
            _job(fixture, ordinary.CID).result(timeout=2)
    assert before == (
        cache.read_bytes(),
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
    )
    assert not list(cache.parent.glob(".contact-analysis-*"))
    analysis.query_contact_analysis(fixture.store, ordinary.CID, background=True)
    _job(fixture, ordinary.CID).result(timeout=5)
    retried = analysis.query_contact_analysis(fixture.store, ordinary.CID)
    assert {row["id"] for row in retried["items"]} == {
        row["id"] for row in ready["items"]
    }
    assert [row["account_key"] for row in retried["items"]] == [
        row["account_key"] for row in ready["items"]
    ]


def test_cancellation_immediately_before_publication_keeps_previous_index(
    contact_analysis_store, monkeypatch
):
    fixture = contact_analysis_store
    analysis.query_contact_analysis(fixture.store, ordinary.CID)
    cache = analysis.contact_analysis_cache_path(fixture.store, ordinary.CID)
    before = (
        cache.read_bytes(),
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
    )
    publish = analysis._publish_build

    def cancel_before_publication(temporary, target):
        assert analysis.cancel_contact_analysis_jobs() == 1
        publish(temporary, target)

    monkeypatch.setattr(
        analysis, "FORWARDED_CLOCK_VERSION", "shutdown-test-publication"
    )
    monkeypatch.setattr(analysis, "_publish_build", cancel_before_publication)
    analysis.query_contact_analysis(fixture.store, ordinary.CID, background=True)
    with pytest.raises(analysis._ContactAnalysisCancelled):
        _job(fixture, ordinary.CID).result(timeout=5)
    assert before == (
        cache.read_bytes(),
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
    )
    assert not list(cache.parent.glob(".contact-analysis-*"))


def test_native_author_hint_build_is_cancelled_without_publishing(
    native_activity_store, monkeypatch
):
    fixture = native_activity_store
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    entered, release = Event(), Event()
    authors = embedded._authors

    def paused(value):
        entered.set()
        assert release.wait(5)
        yield from authors(value)

    monkeypatch.setattr(embedded, "_authors", paused)
    analysis.query_contact_analysis(fixture.store, fixture.target_id, background=True)
    try:
        assert entered.wait(5)
        assert analysis.cancel_contact_analysis_jobs() == 1
    finally:
        release.set()
    with pytest.raises(analysis._ContactAnalysisCancelled):
        _job(fixture, fixture.target_id).result(timeout=2)
    cache = analysis.contact_analysis_cache_path(fixture.store, fixture.target_id)
    assert not cache.exists()
    assert not (cache.parent / "native-author-candidates.sqlite3").exists()
    assert not list(cache.parent.glob(".native-authors-*"))
    assert not list(cache.parent.glob(".contact-analysis-*"))
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def test_process_reload_exits_with_an_active_contact_worker():
    script = """
from threading import Event
from types import SimpleNamespace
from app.core import wechat_contact_analysis as analysis
import main

entered = Event()
def building(*args):
    entered.set()
    while True:
        analysis._check_build_cancelled()
        Event().wait(0.01)

analysis._build_index = building
context = analysis._BuildCancellation()
key = ('synthetic-only', 'synthetic-contact')
with analysis._JOBS_LOCK:
    analysis._JOB_CANCELLATIONS[key] = context
    future = analysis._WORKER.submit(
        analysis._run_background_index, None, None, None, context, key
    )
    analysis._JOBS[key] = ('synthetic-fingerprint', future)
assert entered.wait(2)
main._stop_runtime_services(SimpleNamespace(extensions={'runtime_shutdown': lambda: None}))
raise SystemExit(3)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 3, result.stderr
