"""Reuse event counts without accepting stale proofs or bypassing read budgets.

Code version: v1.0.0-codex.0
"""

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from types import SimpleNamespace

import pytest

from app.core import wechat_conversation_events as events
from tests import test_wechat_conversation_event_cache as fixtures


@pytest.fixture
def summary_store(tmp_path, monkeypatch):
    history = tmp_path / "store/messaging/wechat/history.sqlite3"
    history.parent.mkdir(parents=True)
    # Summaries inspect publication identities, without querying either database.
    history.write_bytes(b"synthetic history publication")
    registry = history.with_name("contacts.sqlite3")
    registry.write_bytes(b"synthetic contact publication")
    repository = SimpleNamespace(path=history)
    proof = fixtures._proof()
    proof["fingerprint"] = events._fingerprint(events._inputs(repository))
    path = events._cache_path(repository, proof["session"])
    path.parent.mkdir()
    path.write_text(events._encode_cache(proof), encoding="utf-8")
    reads = []
    read_cache = events._read_cache

    def tracked_read(*args, **kwargs):
        reads.append(args[0])
        return read_cache(*args, **kwargs)

    monkeypatch.setattr(events, "_SUMMARY_CACHE", OrderedDict())
    monkeypatch.setattr(events, "_read_cache", tracked_read)
    return SimpleNamespace(
        repository=repository, registry=registry, proof=proof, path=path, reads=reads
    )


def test_unchanged_proofs_reuse_counts_without_retaining_message_context(summary_store):
    fixture = summary_store
    expected = {fixture.proof["session"]: 3}
    assert events.cache_only_summaries(fixture.repository) == expected
    assert events.cache_only_summaries(fixture.repository) == expected
    assert fixture.reads == [fixture.path]
    assert list(events._SUMMARY_CACHE.values()) == [(fixture.proof["session"], 3)]


def test_invalid_proof_is_reused_until_its_publication_changes(summary_store):
    fixture = summary_store
    fixture.path.write_text("invalid proof", encoding="utf-8")
    assert events.cache_only_summaries(fixture.repository) == {}
    assert events.cache_only_summaries(fixture.repository) == {}
    assert fixture.reads == [fixture.path]
    replacement = fixture.path.with_suffix(".pending")
    replacement.write_text(events._encode_cache(fixture.proof), encoding="utf-8")
    replacement.replace(fixture.path)
    assert events.cache_only_summaries(fixture.repository) == {
        fixture.proof["session"]: 3
    }
    assert fixture.reads == [fixture.path, fixture.path]
    fixture.path.unlink()
    assert events.cache_only_summaries(fixture.repository) == {}


def test_registry_change_invalidates_a_warm_summary(summary_store):
    fixture = summary_store
    assert events.cache_only_summaries(fixture.repository)
    fixture.registry.write_bytes(b"changed contact publication")
    assert events.cache_only_summaries(fixture.repository) == {}
    assert len(fixture.reads) == 2
    fixture.proof["fingerprint"] = events._fingerprint(
        events._inputs(fixture.repository)
    )
    fixture.path.write_text(events._encode_cache(fixture.proof), encoding="utf-8")
    assert events.cache_only_summaries(fixture.repository) == {
        fixture.proof["session"]: 3
    }
    assert len(fixture.reads) == 3


def test_warm_summaries_still_charge_the_original_scan_budget(
    summary_store, monkeypatch
):
    fixture = summary_store
    assert events.cache_only_summaries(fixture.repository)
    monkeypatch.setattr(events, "MAX_SUMMARY_SCAN_BYTES", fixture.path.stat().st_size)
    assert events.cache_only_summaries(fixture.repository) == {}
    assert len(fixture.reads) == 1
    monkeypatch.setattr(
        events, "MAX_SUMMARY_SCAN_BYTES", fixture.path.stat().st_size + 1
    )
    assert events.cache_only_summaries(fixture.repository)
    assert len(fixture.reads) == 1


def test_decode_budget_change_revalidates_a_warm_summary(summary_store, monkeypatch):
    fixture = summary_store
    assert events.cache_only_summaries(fixture.repository)
    monkeypatch.setattr(events, "MAX_CACHE_BYTES", fixture.path.stat().st_size - 1)
    assert events.cache_only_summaries(fixture.repository) == {}
    assert len(fixture.reads) == 2


def test_proof_changed_after_read_is_not_returned_or_memoized(
    summary_store, monkeypatch
):
    fixture = summary_store
    read_cache = events._read_cache

    def changed_read(*args, **kwargs):
        result = read_cache(*args, **kwargs)
        with fixture.path.open("ab") as stream:
            stream.write(b" ")
        return result

    monkeypatch.setattr(events, "_read_cache", changed_read)
    assert events.cache_only_summaries(fixture.repository) == {}
    assert not events._SUMMARY_CACHE


def test_cached_proof_changed_after_budget_check_is_rejected(
    summary_store, monkeypatch
):
    fixture = summary_store
    assert events.cache_only_summaries(fixture.repository)
    cached_summary = events._cached_summary

    def changed_summary(path, fingerprint, identity):
        with path.open("ab") as stream:
            stream.write(b" ")
        return cached_summary(path, fingerprint, identity)

    monkeypatch.setattr(events, "_cached_summary", changed_summary)
    assert events.cache_only_summaries(fixture.repository) == {}
    assert len(fixture.reads) == 1


def test_removed_proof_does_not_discard_an_unrelated_summary(
    summary_store, monkeypatch
):
    fixture = summary_store
    other = {**fixture.proof, "session": "wechat:" + "f" * 64}
    other_path = events._cache_path(fixture.repository, other["session"])
    other_path.write_text(events._encode_cache(other), encoding="utf-8")
    cached_summary = events._cached_summary

    def removed_summary(path, fingerprint, identity):
        if path == fixture.path:
            path.unlink()
        return cached_summary(path, fingerprint, identity)

    monkeypatch.setattr(events, "_cached_summary", removed_summary)
    assert events.cache_only_summaries(fixture.repository) == {other["session"]: 3}


def test_source_changed_during_a_warm_read_discards_the_result(
    summary_store, monkeypatch
):
    fixture = summary_store
    assert events.cache_only_summaries(fixture.repository)
    cached_summary = events._cached_summary

    def changed_summary(*args):
        result = cached_summary(*args)
        fixture.registry.write_bytes(b"concurrent contact publication")
        return result

    monkeypatch.setattr(events, "_cached_summary", changed_summary)
    assert events.cache_only_summaries(fixture.repository) == {}
    assert len(fixture.reads) == 1


def test_summary_storage_evicts_old_publication_identities(summary_store, monkeypatch):
    fixture = summary_store
    monkeypatch.setattr(events, "_SUMMARY_CACHE_LIMIT", 2)
    for number in range(4):
        fixture.proof["future_metadata"] = "x" * number
        fixture.path.write_text(events._encode_cache(fixture.proof), encoding="utf-8")
        assert events.cache_only_summaries(fixture.repository)
    assert len(fixture.reads) == 4
    assert len(events._SUMMARY_CACHE) == 2


def test_concurrent_readers_decode_an_unchanged_proof_once(summary_store, monkeypatch):
    fixture = summary_store
    start = Barrier(4)
    entered, release = Event(), Event()
    read_cache = events._read_cache

    def blocked_read(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return read_cache(*args, **kwargs)

    def summary():
        start.wait(timeout=5)
        return events.cache_only_summaries(fixture.repository)

    monkeypatch.setattr(events, "_read_cache", blocked_read)
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(summary) for _ in range(4)]
        try:
            assert entered.wait(timeout=5)
        finally:
            release.set()
        assert [future.result(timeout=5) for future in futures] == [
            {fixture.proof["session"]: 3}
        ] * 4
    assert fixture.reads == [fixture.path]
