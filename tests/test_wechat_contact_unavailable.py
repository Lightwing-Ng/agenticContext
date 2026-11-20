"""Unavailable identity declarations must not masquerade as background indexing.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core.wechat_contact_merges import (
    ContactMergeSourceChanged,
    contact_merge_registry_path,
)
from tests import test_wechat_contact_analysis as fixtures


CID = fixtures.CID
contact_analysis_store = fixtures.contact_analysis_store


def _no_background_job(*_args, **_kwargs):
    raise AssertionError("An unavailable identity registry must not queue indexing.")


def test_cloud_only_registry_fails_without_hydration_or_background_indexing(
    contact_analysis_store, monkeypatch
):
    fixture = contact_analysis_store
    path = contact_merge_registry_path(fixture.store)
    path.write_text('{"schema_version":1,"associations":[]}')
    before = fixture.history.read_bytes(), fixture.registry.read_bytes(), path.read_bytes()
    original_stat, original_open = Path.stat, Path.open

    class CloudState:
        def __init__(self, state):
            self.state = state
            self.st_flags = getattr(state, "st_flags", 0) | 0x40000000

        def __getattr__(self, name):
            return getattr(self.state, name)

    def cloud_stat(candidate, *args, **kwargs):
        state = original_stat(candidate, *args, **kwargs)
        return CloudState(state) if candidate == path else state

    def never_hydrate(candidate, *args, **kwargs):
        if candidate == path:
            raise AssertionError("A cloud-only identity registry must not be opened.")
        return original_open(candidate, *args, **kwargs)

    with monkeypatch.context() as cold:
        cold.setattr(Path, "stat", cloud_stat)
        cold.setattr(Path, "open", never_hydrate)
        cold.setattr(analysis._WORKER, "submit", _no_background_job)
        with pytest.raises(analysis.ContactAnalysisUnavailable, match="contact_merges.json"):
            analysis.query_contact_analysis(fixture.store, CID, background=True)
    assert (fixture.history.read_bytes(), fixture.registry.read_bytes(), path.read_bytes()) == before
    assert not analysis.contact_analysis_cache_path(fixture.store, CID).exists()
    assert (str(fixture.store.resolve()), CID) not in analysis._JOBS


@pytest.mark.parametrize(
    "declaration",
    [
        "{",
        '{"schema_version":2,"associations":[]}',
        '{"schema_version":1,"schema_version":1,"associations":[]}',
    ],
    ids=["malformed", "unsupported", "duplicate-keys"],
)
def test_invalid_registry_is_an_explicit_failure_instead_of_pending(
    contact_analysis_store, monkeypatch, declaration
):
    fixture = contact_analysis_store
    path = contact_merge_registry_path(fixture.store)
    path.write_text(declaration)
    before = fixture.history.read_bytes(), fixture.registry.read_bytes(), path.read_bytes()
    monkeypatch.setattr(analysis._WORKER, "submit", _no_background_job)
    with pytest.raises(analysis.ContactAnalysisUnavailable, match="check its contents") as error:
        analysis.query_contact_analysis(fixture.store, CID, background=True)
    assert isinstance(error.value.__cause__, ValueError)
    # Existing non-UI consumers must retain their fail-closed source-error boundary.
    assert isinstance(error.value, analysis.ContactAnalysisSourceChanged)
    assert (fixture.history.read_bytes(), fixture.registry.read_bytes(), path.read_bytes()) == before
    assert not analysis.contact_analysis_cache_path(fixture.store, CID).exists()


def test_unreadable_registry_is_an_explicit_failure(contact_analysis_store, monkeypatch):
    fixture = contact_analysis_store

    def unreadable(*_args, **_kwargs):
        raise PermissionError("Synthetic identity declaration is unreadable.")

    monkeypatch.setattr(analysis, "resolve_contact_merge", unreadable)
    monkeypatch.setattr(analysis._WORKER, "submit", _no_background_job)
    with pytest.raises(analysis.ContactAnalysisUnavailable, match="available locally") as error:
        analysis.query_contact_analysis(fixture.store, CID, background=True)
    assert isinstance(error.value.__cause__, PermissionError)


def test_identity_registry_drift_remains_retryable(contact_analysis_store, monkeypatch):
    fixture = contact_analysis_store
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()

    def changed(*_args, **_kwargs):
        raise ContactMergeSourceChanged("Contact associations changed while reading.")

    monkeypatch.setattr(analysis, "resolve_contact_merge", changed)
    monkeypatch.setattr(analysis._WORKER, "submit", _no_background_job)
    with pytest.raises(analysis.ContactAnalysisSourceChanged, match="changed while reading") as error:
        analysis.query_contact_analysis(fixture.store, CID, background=True)
    assert isinstance(error.value.__cause__, ContactMergeSourceChanged)
    assert (fixture.history.read_bytes(), fixture.registry.read_bytes()) == before
    assert not analysis.contact_analysis_cache_path(fixture.store, CID).exists()
