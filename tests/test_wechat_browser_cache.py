"""Conversation list reuse preserves source freshness and account boundaries.

Code version: v1.0.0-codex.0
"""

from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
import sqlite3
from threading import Event

import pytest

from app.core import wechat_browser_cache as cache
from tests import test_wechat_conversation_events as fixtures


event_store = fixtures.event_store


@pytest.fixture(autouse=True)
def isolated_page_cache(monkeypatch):
    monkeypatch.setattr(cache, "_PAGES", OrderedDict())
    monkeypatch.setattr(cache, "_PENDING", {})


def _count_queries(repository, monkeypatch):
    calls = []
    original = repository._query_uncached

    def counted(**parameters):
        calls.append(parameters)
        return original(**parameters)

    monkeypatch.setattr(repository, "_query_uncached", counted)
    return calls


def test_repeated_list_skips_timeline_rebuild_and_returns_detached_rows(
    event_store, monkeypatch
):
    repository = event_store.repository
    calls = _count_queries(repository, monkeypatch)
    first = repository.query()
    expected_title = first["conversations"][0]["title"]
    first["conversations"][0]["title"] = "Caller-owned edit"
    first["accounts"].clear()
    second = repository.query()
    assert len(calls) == 1
    assert second["conversations"][0]["title"] == expected_title
    assert second["accounts"]
    assert second["message_count"] == 6


def test_account_filters_and_search_have_separate_cached_pages(event_store, monkeypatch):
    repository = event_store.repository
    calls = _count_queries(repository, monkeypatch)
    for _repeat in range(2):
        assert repository.query(account="account:" + fixtures.OWNER)["count"] == 1
        assert repository.query(account="account:another-owner")["count"] == 0
        assert repository.query(query="no-such-literal-body")["count"] == 0
    assert len(calls) == 3


def test_changed_primary_file_invalidates_cached_title(event_store, monkeypatch):
    repository = event_store.repository
    calls = _count_queries(repository, monkeypatch)
    repository.query()
    with sqlite3.connect(repository.path) as connection:
        connection.execute("UPDATE conversations SET title='Updated source title'")
    assert repository.query()["conversations"][0]["title"] == "Updated source title"
    assert len(calls) == 2


def test_new_or_removed_event_proof_updates_cached_list_counts(event_store, monkeypatch):
    repository = event_store.repository
    calls = _count_queries(repository, monkeypatch)
    assert repository.query()["message_count"] == 6
    connection, _result = fixtures._run(event_store)
    connection.close()
    assert repository.query()["message_count"] == 3
    fixtures.events._cache_path(repository, fixtures.SESSION).unlink()
    assert repository.query()["message_count"] == 6
    assert len(calls) == 3


def test_contact_registry_change_invalidates_canonical_counts(event_store):
    connection, _result = fixtures._run(event_store)
    connection.close()
    assert event_store.repository.query()["message_count"] == 3
    with sqlite3.connect(event_store.registry) as connection:
        connection.execute("DELETE FROM sender_bindings")
    assert event_store.repository.query()["message_count"] == 6


@pytest.mark.parametrize(
    "name",
    [
        "resource_references.sqlite3",
        "resource_revalidated.sqlite3",
        "media_placeholders.json",
        "media_events.sqlite3",
        "macos_recovery.json",
        "macos_recovery.sqlite3",
    ],
)
def test_every_optional_timeline_publication_changes_page_identity(event_store, name):
    parameters = {"session": "", "page_size": 100}
    before = cache._page_identity(event_store.repository, parameters)
    assert before is not None
    event_store.repository.path.with_name(name).write_bytes(b"Synthetic new publication")
    assert cache._page_identity(event_store.repository, parameters) != before


def test_publication_during_load_is_not_cached(event_store, monkeypatch):
    repository = event_store.repository
    original = repository._query_uncached
    calls = []

    def replace_after_read(**parameters):
        calls.append(True)
        result = original(**parameters)
        if len(calls) == 1:
            with sqlite3.connect(repository.path) as connection:
                connection.execute("UPDATE conversations SET title='Replaced during read'")
        return result

    monkeypatch.setattr(repository, "_query_uncached", replace_after_read)
    repository.query()
    assert repository.query()["conversations"][0]["title"] == "Replaced during read"
    repository.query()
    assert len(calls) == 2


def test_equal_counts_after_proof_replacement_do_not_cache_intermediate_counts(
    event_store, monkeypatch
):
    repository = event_store.repository
    connection, _result = fixtures._run(event_store)
    connection.close()
    proof = fixtures.events._cache_path(repository, fixtures.SESSION)
    original_bytes = proof.read_bytes()
    original = repository._query_uncached
    calls = []

    def replace_proof_around_read(**parameters):
        calls.append(True)
        if len(calls) == 1:
            proof.write_text("{}")
            result = original(**parameters)
            proof.write_bytes(original_bytes)
            return result
        return original(**parameters)

    monkeypatch.setattr(repository, "_query_uncached", replace_proof_around_read)
    assert repository.query()["message_count"] == 6
    assert repository.query()["message_count"] == 3
    assert repository.query()["message_count"] == 3
    assert len(calls) == 2


def test_concurrent_readers_share_one_cold_page(event_store, monkeypatch):
    repository = event_store.repository
    original = repository._query_uncached
    entered, waiting, release = Event(), Event(), Event()
    calls = []

    class ObservedFuture(Future):
        def result(self, timeout=None):
            waiting.set()
            return super().result(timeout)

    def blocked(**parameters):
        calls.append(True)
        entered.set()
        assert release.wait(10)
        return original(**parameters)

    monkeypatch.setattr(cache, "Future", ObservedFuture)
    monkeypatch.setattr(repository, "_query_uncached", blocked)
    with ThreadPoolExecutor(max_workers=2) as readers:
        first = readers.submit(repository.query)
        try:
            assert entered.wait(3)
            second = readers.submit(repository.query)
            assert waiting.wait(3)
        finally:
            release.set()
        assert first.result(timeout=3) == second.result(timeout=3)
    assert len(calls) == 1


def test_failed_load_can_retry_without_reusing_pending_failure(event_store, monkeypatch):
    repository = event_store.repository
    original = repository._query_uncached
    calls = []

    def fail_once(**parameters):
        calls.append(True)
        if len(calls) == 1:
            raise ValueError("Synthetic unavailable timeline")
        return original(**parameters)

    monkeypatch.setattr(repository, "_query_uncached", fail_once)
    with pytest.raises(ValueError, match="Synthetic unavailable timeline"):
        repository.query()
    assert repository.query()["count"] == 1
    assert len(calls) == 2


def test_page_cache_is_bounded_and_details_still_query_live(event_store, monkeypatch):
    repository = event_store.repository
    calls = _count_queries(repository, monkeypatch)
    monkeypatch.setattr(cache, "MAX_PAGES", 2)
    for account in ("a", "b", "a", "c", "a", "b"):
        repository.query(account=account)
    assert len(calls) == 4
    for _repeat in range(2):
        assert repository.query(session=fixtures.SESSION)["current"] is not None
    assert len(calls) == 6
