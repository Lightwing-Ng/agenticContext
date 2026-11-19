"""Relative and absolute callers share proof caches without bypassing input checks.

Code version: v1.1.0-codex.0
"""

from pathlib import Path
import sqlite3

import pytest

from app.core import wechat_conversation_events as events
from app.core.wechat_archive import WechatArchiveRepository, WechatTimelineProjectionError
from tests import test_wechat_conversation_events as fixtures


event_store = fixtures.event_store


def _relative_repository(fixture, monkeypatch):
    monkeypatch.chdir(fixture.store.parent)
    repository = WechatArchiveRepository(fixture.store.name)
    assert not repository.path.is_absolute()
    return repository


def test_all_input_keys_and_fingerprints_agree_for_relative_and_absolute_roots(
    event_store, monkeypatch
):
    f = event_store
    directory = f.repository.path.parent
    optional = (
        "resource_references.sqlite3",
        "media_placeholders.json",
        "media_events.sqlite3",
    )
    for name in optional:
        (directory / name).write_bytes(b"Input metadata only; never open the payload.")
    relative = _relative_repository(f, monkeypatch)
    absolute_inputs = events._inputs(f.repository)
    relative_inputs = events._inputs(relative)
    assert relative_inputs == absolute_inputs
    assert events._fingerprint(relative_inputs) == events._fingerprint(absolute_inputs)
    assert {Path(key).name for key in relative_inputs} == {
        "history.sqlite3",
        "contacts.sqlite3",
        "conversation_quote_reviews.json",
        *optional,
    }
    assert all(Path(key).is_absolute() for key in relative_inputs)


def test_warm_cache_crosses_root_spelling_without_rescanning_source_bodies(
    event_store, monkeypatch
):
    f = event_store
    connection, initial = fixtures._run(f)
    connection.close()
    assert initial["status"] == "ready"
    path = events._cache_path(f.repository, fixtures.SESSION)
    original = path.read_bytes()
    original_mtime = path.stat().st_mtime_ns
    relative = _relative_repository(f, monkeypatch)
    monkeypatch.setattr(
        events,
        "_scan",
        lambda *args: pytest.fail("A path alias must reuse the warm proof cache."),
    )
    with sqlite3.connect(
        f.repository.path.as_uri() + "?mode=ro&immutable=1", uri=True
    ) as source:
        source.row_factory = sqlite3.Row
        current = dict(
            source.execute(
                "SELECT c.*,p.account_id,p.identity_status FROM conversations c "
                "JOIN partitions p ON p.id=c.partition_id WHERE c.id=?",
                (fixtures.SESSION,),
            ).fetchone()
        )
        reused = events.conversation_events(
            relative, source, fixtures.SESSION, current, "main.messages"
        )
    assert reused == initial
    assert events.cached_conversation_events(relative, fixtures.SESSION) is not None
    assert events.cache_only_summaries(relative) == {fixtures.SESSION: 3}
    assert events.cache_only_summaries(f.repository) == {fixtures.SESSION: 3}
    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == original_mtime


def test_strict_selected_readback_publishes_the_browser_event_proof(event_store, monkeypatch):
    fixture = event_store
    cache = events._cache_path(fixture.repository, fixtures.SESSION)
    before = fixture.repository.path.read_bytes(), fixture.registry.read_bytes()
    query = {
        "account": "account:" + fixtures.OWNER,
        "kind": "direct",
        "session": fixtures.SESSION,
        "page_size": 1,
    }
    timeline = fixture.repository._timeline_tables
    validated = []

    def observe_scope(*args, **options):
        validated.append((options["conversation_id"], options["strict"]))
        return timeline(*args, **options)

    monkeypatch.setattr(fixture.repository, "_timeline_tables", observe_scope)
    readback = fixture.repository.query(**query, strict_timeline=True)
    assert validated == [(fixtures.SESSION, True)]
    assert readback["count"] == readback["message_count"] == 3
    assert readback["event_projection"]["count_delta"] == 3
    assert [item["id"] for item in readback["messages"]] == [fixture.ids[0][1]]
    cache_state = cache.read_bytes(), cache.stat().st_mtime_ns

    monkeypatch.setattr(
        events, "_scan", lambda *_args: pytest.fail("Browser must reuse the verified event proof.")
    )
    browser = WechatArchiveRepository(fixture.store)
    assert browser.query(**query) == readback
    assert events.cache_only_summaries(browser) == {fixtures.SESSION: 3}
    assert (cache.read_bytes(), cache.stat().st_mtime_ns) == cache_state
    assert (fixture.repository.path.read_bytes(), fixture.registry.read_bytes()) == before


def test_failed_strict_readback_leaves_the_tolerant_source_unpublished(event_store):
    fixture = event_store
    with sqlite3.connect(fixture.repository.path) as connection:
        connection.execute(
            "UPDATE conversations SET partition_id=? WHERE id=?",
            (fixtures.NATIVE, fixtures.SESSION),
        )
    index = fixture.repository.path.with_name("resource_references.sqlite3")
    index.write_bytes(b"Synthetic invalid native resource index")
    cache = events._cache_path(fixture.repository, fixtures.SESSION)
    query = {
        "account": "account:" + fixtures.OWNER,
        "kind": "direct",
        "session": fixtures.SESSION,
        "page_size": 1,
    }
    with pytest.raises(WechatTimelineProjectionError) as error:
        fixture.repository.query(**query, strict_timeline=True)
    assert error.value.status == "invalid"
    assert not cache.exists()

    fallback = fixture.repository.query(**query, persist_event_cache=False)
    assert fallback["current"]["id"] == fixtures.SESSION
    assert fallback["resource_index_status"] == "invalid"
    assert fallback["count"] == fallback["message_count"] == 6
    assert not cache.exists()


def test_unavailable_optional_keys_are_canonical_only_after_original_validation(
    event_store, monkeypatch
):
    f = event_store
    names = {
        "resource_references.sqlite3",
        "media_placeholders.json",
        "media_events.sqlite3",
    }
    for name in names:
        (f.repository.path.parent / name).write_bytes(b"Resident metadata stub")
    relative = _relative_repository(f, monkeypatch)
    checked = set()
    identity, resolve = events._identity, Path.resolve

    def checked_identity(path, *, optional=False):
        checked.add(str(path))
        if path.name in names:
            raise ValueError(
                "Optional input is unavailable without reading its payload."
            )
        return identity(path, optional=optional)

    def resolve_after_validation(path, *args, **kwargs):
        if path.name in names | {"history.sqlite3", "contacts.sqlite3"}:
            assert str(path) in checked
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(events, "_identity", checked_identity)
    monkeypatch.setattr(Path, "resolve", resolve_after_validation)
    absolute_inputs = events._inputs(f.repository)
    relative_inputs = events._inputs(relative)
    assert relative_inputs == absolute_inputs
    assert all(
        value.get("unavailable")
        for key, value in relative_inputs.items()
        if Path(key).name in names
    )


@pytest.mark.parametrize("name", ["history.sqlite3", "contacts.sqlite3"])
def test_mandatory_symlink_is_rejected_before_resolution(
    event_store, monkeypatch, name
):
    f = event_store
    path = f.repository.path.with_name(name)
    resident = path.with_name("preserved-" + name)
    path.rename(resident)
    path.symlink_to(resident)
    resolve = Path.resolve
    resolved_unsafe = []

    def guarded_resolve(value, *args, **kwargs):
        if value == path:
            resolved_unsafe.append(value)
        return resolve(value, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", guarded_resolve)
    with pytest.raises(ValueError, match="published local files"):
        events._inputs(f.repository)
    assert not resolved_unsafe


def test_review_symlink_is_rejected_without_resolving_its_target(
    event_store, monkeypatch
):
    f = event_store
    path = events.reviewed_quotes_path(f.store)
    path.symlink_to(f.repository.path.with_name("contacts.sqlite3"))
    resolve = Path.resolve
    resolved_unsafe = []

    def guarded_resolve(value, *args, **kwargs):
        if value == path:
            resolved_unsafe.append(value)
        return resolve(value, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", guarded_resolve)
    with pytest.raises(ValueError, match="quote reviews cannot be symbolic links"):
        events._inputs(f.repository)
    assert not resolved_unsafe
