"""Resource validation stays bounded and independent across immutable scopes.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError
import os
import sqlite3
from threading import Event

import pytest

from app.core import wechat_archive as archive
from app.core import wechat_native_resource_revalidation as revalidation
from tests import test_wechat_resource_timeline as resource_fixtures
from tests.test_wechat_resource_timeline import (
    _reference,
    _session,
    _sidecar,
)


store = resource_fixtures.store


def _resource_scopes(store, count):
    template = _session(store)
    sessions = []
    for index in range(count):
        peer = f"wxid_validation_cache_{index}"
        sessions.append(
            {
                **template,
                "id": "wechat:" + archive._identifier(template["partition_id"], peer),
                "peer_id": peer,
                "title": f"Resource scope {index}",
            }
        )
    path = _sidecar(
        store,
        [
            (session, _reference(session, f"Image {index}", 20 + index))
            for index, session in enumerate(sessions)
        ],
    )
    return archive.WechatArchiveRepository(store), sessions, path


def _resource_status(repository, conversation_id=None):
    with sqlite3.connect(
        repository.path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
    ) as connection:
        connection.row_factory = sqlite3.Row
        return repository._resource_timeline_tables(
            connection,
            archive._file_identity(repository.path),
            conversation_id=conversation_id,
        )[2]


def _count_validations(monkeypatch):
    calls = []
    original = archive.WechatArchiveRepository._validate_resource_conversations

    def counted(connection, **kwargs):
        calls.append(kwargs.get("conversation_id"))
        return original(connection, **kwargs)

    monkeypatch.setattr(
        archive.WechatArchiveRepository,
        "_validate_resource_conversations",
        staticmethod(counted),
    )
    return calls


def test_visiting_many_scopes_keeps_the_full_snapshot_validation(store, monkeypatch):
    repository, sessions, _path = _resource_scopes(store, 6)
    calls = _count_validations(monkeypatch)

    assert _resource_status(repository) == "ready"
    for session in sessions:
        assert _resource_status(repository, session["id"]) == "ready"
    assert _resource_status(repository) == "ready"

    assert calls.count(None) == 1
    assert len(calls) == 7


def test_scoped_validation_has_an_independent_lru_bound(store, monkeypatch):
    repository, sessions, _path = _resource_scopes(store, 3)
    monkeypatch.setattr(archive, "_RESOURCE_SCOPED_CACHE_LIMIT", 2)
    calls = _count_validations(monkeypatch)
    first, second, third = [session["id"] for session in sessions]

    for identifier in (first, second, first, third, first, second):
        assert _resource_status(repository, identifier) == "ready"

    assert calls == [first, second, third, second]


def test_full_snapshot_validation_has_its_own_lru_bound(store, monkeypatch):
    repository, _sessions, resource = _resource_scopes(store, 1)
    snapshots = [resource]
    for index in range(2):
        path = resource.with_name(f"resource-snapshot-{index}.sqlite3")
        path.write_bytes(resource.read_bytes())
        snapshots.append(path)
    selected = [snapshots[0]]
    monkeypatch.setattr(
        revalidation, "select_resource_index", lambda *_args, **_kwargs: selected[0]
    )
    monkeypatch.setattr(archive, "_RESOURCE_CACHE_LIMIT", 2)
    calls = _count_validations(monkeypatch)

    for index in (0, 1, 0, 2, 0, 1):
        selected[0] = snapshots[index]
        assert _resource_status(repository) == "ready"

    assert calls == [None] * 4


def test_cold_global_validation_does_not_block_other_scoped_readers(
    store, monkeypatch
):
    repository, sessions, _path = _resource_scopes(store, 2)
    warm, cold = [session["id"] for session in sessions]
    assert _resource_status(repository, warm) == "ready"
    original = archive.WechatArchiveRepository._validate_resource_conversations
    started, release = Event(), Event()

    def blocked_global(connection, **kwargs):
        if kwargs.get("conversation_id") is None:
            started.set()
            assert release.wait(10), "The test must release its global validator."
        return original(connection, **kwargs)

    monkeypatch.setattr(
        archive.WechatArchiveRepository,
        "_validate_resource_conversations",
        staticmethod(blocked_global),
    )
    with ThreadPoolExecutor(max_workers=3) as readers:
        global_read = readers.submit(_resource_status, repository)
        try:
            assert started.wait(3)
            warm_read = readers.submit(_resource_status, repository, warm)
            cold_read = readers.submit(_resource_status, repository, cold)
            assert warm_read.result(timeout=3) == "ready"
            assert cold_read.result(timeout=3) == "ready"
            assert not global_read.done()
        finally:
            release.set()
        assert global_read.result(timeout=3) == "ready"


@pytest.mark.parametrize("reject", (False, True))
def test_concurrent_readers_share_one_validation_and_failures_allow_retry(
    store, monkeypatch, reject
):
    repository, _sessions, _path = _resource_scopes(store, 1)
    original = archive.WechatArchiveRepository._validate_resource_conversations
    started, release, follower_started = Event(), Event(), Event()
    calls = []

    def blocked(connection, **kwargs):
        calls.append(True)
        started.set()
        assert release.wait(10), "The test must release its shared validator."
        if reject:
            raise ValueError("Synthetic invalid resource scope.")
        return original(connection, **kwargs)

    def follower():
        follower_started.set()
        return _resource_status(repository)

    monkeypatch.setattr(
        archive.WechatArchiveRepository,
        "_validate_resource_conversations",
        staticmethod(blocked),
    )
    with ThreadPoolExecutor(max_workers=2) as readers:
        first = readers.submit(_resource_status, repository)
        try:
            assert started.wait(3)
            second = readers.submit(follower)
            assert follower_started.wait(3)
            with pytest.raises(TimeoutError):
                second.result(timeout=0.1)
        finally:
            release.set()
        expected = "invalid" if reject else "ready"
        assert first.result(timeout=3) == second.result(timeout=3) == expected
    assert calls == [True]

    monkeypatch.setattr(
        archive.WechatArchiveRepository,
        "_validate_resource_conversations",
        staticmethod(original),
    )
    assert _resource_status(repository) == "ready"


@pytest.mark.parametrize("changed", ("history", "resource"))
def test_cached_validation_never_hides_changed_invalid_inputs(
    store, monkeypatch, changed
):
    repository, sessions, resource = _resource_scopes(store, 1)
    calls = _count_validations(monkeypatch)
    assert _resource_status(repository) == "ready"
    if changed == "resource":
        with sqlite3.connect(resource) as connection:
            connection.execute("UPDATE messages SET provenance='invalid json'")
        expected = "invalid"
    else:
        with sqlite3.connect(repository.path) as connection:
            connection.execute("UPDATE partitions SET account_label='Changed label'")
        expected = "stale"

    assert _resource_status(repository) == expected
    assert len(calls) == (2 if changed == "resource" else 1)
    assert repository.query(session=sessions[0]["id"])["messages"] == []


def test_identity_change_during_validation_is_still_rejected(store, monkeypatch):
    repository, _sessions, resource = _resource_scopes(store, 1)
    original = archive.WechatArchiveRepository._validate_resource_conversations
    validated, release = Event(), Event()
    calls = []

    def changed_before_use(connection, **kwargs):
        calls.append(True)
        corrected = original(connection, **kwargs)
        validated.set()
        assert release.wait(10), "The test must release its completed validator."
        return corrected

    monkeypatch.setattr(
        archive.WechatArchiveRepository,
        "_validate_resource_conversations",
        staticmethod(changed_before_use),
    )
    with ThreadPoolExecutor(max_workers=1) as reader:
        result = reader.submit(_resource_status, repository)
        try:
            assert validated.wait(3)
            state = resource.stat()
            os.utime(resource, ns=(state.st_atime_ns, state.st_mtime_ns + 1_000_000))
        finally:
            release.set()
        assert result.result(timeout=3) == "stale"

    assert _resource_status(repository) == "ready"
    assert calls == [True, True]
