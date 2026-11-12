"""Authenticated raw overviews survive reloads without reusing changed evidence.

Code version: v1.0.1-codex.0
"""

from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
import os
import sqlite3
from threading import Event

import pytest

from app.core import wechat_browser_snapshot as snapshots
from app.core.wechat_archive import WechatTimelineProjectionError, _file_identity
from app.core.wechat_secondary_completions import FILENAME
from tests import test_wechat_conversation_events as fixtures


event_store = fixtures.event_store


@pytest.fixture
def snapshot_store(event_store, monkeypatch):
    fixture = event_store
    fixture.snapshot_calls = []
    timeline = fixture.repository._timeline_tables

    def tracked_timeline(*args, **kwargs):
        assert kwargs.get("strict") is True
        fixture.snapshot_calls.append(True)
        return timeline(*args, **kwargs)

    monkeypatch.setattr(fixture.repository, "_timeline_tables", tracked_timeline)
    monkeypatch.setattr(snapshots, "_SNAPSHOTS", OrderedDict())
    monkeypatch.setattr(snapshots, "_PENDING", {})
    monkeypatch.setattr(snapshots, "_code_fingerprint", lambda: "reader-code-v1")
    return fixture


def _load(fixture):
    path = fixture.repository.path
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        result = snapshots.load_conversation_snapshot(
            fixture.repository, connection, _file_identity(path)
        )
        rows = (
            connection.execute("SELECT * FROM " + result[0]).fetchall()
            if result is not None else []
        )
    return result, rows


def _directory(fixture):
    return fixture.repository.path.parent / "browser_cache"


def test_process_reload_reads_signed_snapshot_without_rebuilding(snapshot_store):
    fixture = snapshot_store
    first, rows = _load(fixture)
    assert first == ("browser_snapshot_conversations", "absent", None)
    assert len(rows) == 1 and rows[0][6] == 6
    snapshots._SNAPSHOTS.clear()
    assert _load(fixture) == (first, rows)
    assert len(fixture.snapshot_calls) == 1
    directory = _directory(fixture)
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / "signing.key").stat().st_mode & 0o777 == 0o600
    assert (directory / "conversations.snapshot").stat().st_mode & 0o777 == 0o600


def test_changed_history_rebuilds_raw_summary(snapshot_store):
    fixture = snapshot_store
    assert _load(fixture)[0] is not None
    with sqlite3.connect(fixture.repository.path) as connection:
        connection.execute("UPDATE conversations SET title='New source title'")
    assert _load(fixture)[1][0][4] == "New source title"
    assert len(fixture.snapshot_calls) == 2


def test_same_size_and_mtime_source_change_still_invalidates(snapshot_store):
    fixture = snapshot_store
    assert _load(fixture)[0] is not None
    before = fixture.registry.stat()
    with sqlite3.connect(fixture.registry) as connection:
        connection.execute("DELETE FROM sender_bindings")
    assert fixture.registry.stat().st_size == before.st_size
    os.utime(fixture.registry, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert _load(fixture)[0] is not None
    assert len(fixture.snapshot_calls) == 2


def test_event_proofs_do_not_change_persisted_raw_counts(snapshot_store):
    fixture = snapshot_store
    result, rows = _load(fixture)
    directory = fixture.repository.path.with_name("conversation_events")
    directory.mkdir()
    proof = directory / "synthetic.json"
    proof.write_text('{"count_delta":3}', encoding="utf-8")
    snapshots._SNAPSHOTS.clear()
    assert _load(fixture) == (result, rows)
    proof.unlink()
    assert _load(fixture) == (result, rows)
    assert rows[0][6] == 6
    assert len(fixture.snapshot_calls) == 1


def test_reader_code_change_rebuilds_disk_snapshot(snapshot_store, monkeypatch):
    fixture = snapshot_store
    assert _load(fixture)[0] is not None
    snapshots._SNAPSHOTS.clear()
    monkeypatch.setattr(snapshots, "_code_fingerprint", lambda: "reader-code-v2")
    assert _load(fixture)[0] is not None
    assert len(fixture.snapshot_calls) == 2


def test_tampered_payload_cannot_be_accepted_after_reload(snapshot_store):
    fixture = snapshot_store
    expected = _load(fixture)
    path = _directory(fixture) / "conversations.snapshot"
    original = path.read_bytes()
    path.write_bytes(original[:-1] + b" ")
    snapshots._SNAPSHOTS.clear()
    assert _load(fixture) == expected
    assert len(fixture.snapshot_calls) == 2


def test_missing_key_requires_new_verification(snapshot_store):
    fixture = snapshot_store
    expected = _load(fixture)
    key_path = _directory(fixture) / "signing.key"
    original = key_path.read_bytes()
    key_path.unlink()
    snapshots._SNAPSHOTS.clear()
    assert _load(fixture) == expected
    assert key_path.read_bytes() != original
    assert len(fixture.snapshot_calls) == 2


def test_optional_write_error_keeps_verified_memory_result(snapshot_store, monkeypatch):
    fixture = snapshot_store

    def unavailable(*args, **kwargs):
        raise OSError("Synthetic full cache filesystem")

    monkeypatch.setattr(snapshots, "_write_snapshot", unavailable)
    first = _load(fixture)
    assert first[0] is not None
    assert _load(fixture) == first
    assert len(fixture.snapshot_calls) == 1
    assert not (_directory(fixture) / "conversations.snapshot").exists()


def test_native_coverage_metadata_survives_disk_reload(snapshot_store, monkeypatch):
    fixture = snapshot_store
    timeline = fixture.repository._timeline_tables

    def ready(*args, **kwargs):
        conversations, messages, _status = timeline(*args, **kwargs)
        return conversations, messages, "ready"

    monkeypatch.setattr(fixture.repository, "_timeline_tables", ready)
    monkeypatch.setattr(
        fixture.repository, "_resource_index_accounts", lambda _connection: (fixtures.OWNER,)
    )
    first = _load(fixture)
    assert first[0] == ("browser_snapshot_conversations", "ready", (fixtures.OWNER,))
    snapshots._SNAPSHOTS.clear()
    assert _load(fixture) == first
    assert len(fixture.snapshot_calls) == 1


@pytest.mark.parametrize("target", ["directory", "signing.key"])
def test_nonprivate_storage_is_an_optional_miss(snapshot_store, target):
    fixture = snapshot_store
    assert _load(fixture)[0] is not None
    directory = _directory(fixture)
    path = directory if target == "directory" else directory / target
    path.chmod(0o755 if target == "directory" else 0o644)
    assert _load(fixture) == (None, [])


def test_nonprivate_snapshot_is_reverified_without_trusting_its_bytes(snapshot_store):
    fixture = snapshot_store
    expected = _load(fixture)
    path = _directory(fixture) / "conversations.snapshot"
    path.chmod(0o644)
    snapshots._SNAPSHOTS.clear()
    assert _load(fixture) == expected
    assert len(fixture.snapshot_calls) == 2
    assert path.stat().st_mode & 0o777 == 0o644


def test_symlinked_signing_key_is_not_followed(snapshot_store, tmp_path):
    fixture = snapshot_store
    assert _load(fixture)[0] is not None
    key_path = _directory(fixture) / "signing.key"
    target = tmp_path / "unrelated-key"
    target.write_bytes(key_path.read_bytes())
    target.chmod(0o600)
    key_path.unlink()
    key_path.symlink_to(target)
    assert _load(fixture) == (None, [])


@pytest.mark.parametrize("failure", ["raise", "status"])
def test_incomplete_timeline_is_not_persisted(snapshot_store, monkeypatch, failure):
    fixture = snapshot_store

    def incomplete(*args, **kwargs):
        if failure == "raise":
            raise WechatTimelineProjectionError("invalid")
        return "main.conversations", "main.messages", "stale"

    monkeypatch.setattr(fixture.repository, "_timeline_tables", incomplete)
    assert _load(fixture) == (None, [])
    assert not (_directory(fixture) / "conversations.snapshot").exists()


def test_source_change_during_build_discards_summary(snapshot_store, monkeypatch):
    fixture = snapshot_store
    timeline = fixture.repository._timeline_tables

    def changed(*args, **kwargs):
        result = timeline(*args, **kwargs)
        with sqlite3.connect(fixture.registry) as connection:
            connection.execute("DELETE FROM sender_bindings")
        return result

    monkeypatch.setattr(fixture.repository, "_timeline_tables", changed)
    assert _load(fixture) == (None, [])
    assert not (_directory(fixture) / "conversations.snapshot").exists()
    assert not snapshots._SNAPSHOTS


def test_source_change_before_atomic_publish_discards_summary(
    snapshot_store, monkeypatch
):
    fixture = snapshot_store
    write_snapshot = snapshots._write_snapshot

    def changed(*args, **kwargs):
        with sqlite3.connect(fixture.registry) as connection:
            connection.execute("DELETE FROM sender_bindings")
        return write_snapshot(*args, **kwargs)

    monkeypatch.setattr(snapshots, "_write_snapshot", changed)
    assert _load(fixture) == (None, [])
    assert not (_directory(fixture) / "conversations.snapshot").exists()


@pytest.mark.parametrize("limit", ["rows", "bytes"])
def test_oversized_summary_is_an_optional_miss(snapshot_store, monkeypatch, limit):
    if limit == "rows":
        monkeypatch.setattr(snapshots, "MAX_ROWS", 0)
    else:
        monkeypatch.setattr(snapshots, "MAX_SNAPSHOT_BYTES", 32)
    assert _load(snapshot_store) == (None, [])
    assert not (_directory(snapshot_store) / "conversations.snapshot").exists()


def test_missing_contact_publication_keeps_original_path(snapshot_store):
    fixture = snapshot_store
    fixture.registry.unlink()
    assert _load(fixture) == (None, [])
    assert not _directory(fixture).exists()


def test_unexpected_timeline_error_propagates_and_pending_can_retry(
    snapshot_store, monkeypatch
):
    fixture = snapshot_store
    timeline = fixture.repository._timeline_tables

    def broken(*args, **kwargs):
        raise RuntimeError("Synthetic unexpected verifier failure")

    monkeypatch.setattr(fixture.repository, "_timeline_tables", broken)
    with pytest.raises(RuntimeError, match="unexpected verifier"):
        _load(fixture)
    assert not snapshots._PENDING
    monkeypatch.setattr(fixture.repository, "_timeline_tables", timeline)
    assert _load(fixture)[0] is not None


def test_concurrent_readers_share_one_verified_snapshot(snapshot_store, monkeypatch):
    fixture = snapshot_store
    timeline = fixture.repository._timeline_tables
    entered, waiting, release = Event(), Event(), Event()

    class ObservedFuture(Future):
        def result(self, timeout=None):
            waiting.set()
            return super().result(timeout)

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return timeline(*args, **kwargs)

    monkeypatch.setattr(snapshots, "Future", ObservedFuture)
    monkeypatch.setattr(fixture.repository, "_timeline_tables", blocked)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(_load, fixture)
        try:
            assert entered.wait(timeout=5)
            second = executor.submit(_load, fixture)
            assert waiting.wait(timeout=5)
        finally:
            release.set()
        assert first.result(timeout=5) == second.result(timeout=5)
    assert len(fixture.snapshot_calls) == 1


def test_code_digest_detects_source_change_with_restored_mtime(tmp_path, monkeypatch):
    path = tmp_path / "wechat_synthetic.py"
    path.write_text("value = 1\n", encoding="utf-8")
    monkeypatch.setattr(snapshots, "_CODE_DIGEST", None)
    monkeypatch.setattr(snapshots, "_code_files", lambda: [path])
    before = path.stat()
    first = snapshots._code_fingerprint()
    path.write_text("value = 2\n", encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert snapshots._code_fingerprint() != first


def _load_previous(fixture):
    path = fixture.repository.path
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        result = snapshots.load_previous_conversation_snapshot(
            fixture.repository, connection, _file_identity(path)
        )
        table = connection.execute(
            "SELECT name FROM sqlite_temp_master WHERE type='table' AND name=?",
            ("browser_snapshot_conversations",),
        ).fetchone()
        rows = connection.execute("SELECT * FROM " + table[0]).fetchall() if table else []
    return result, rows


def _add_completion_input(fixture):
    completion = fixture.repository.path.with_name(FILENAME)
    with sqlite3.connect(completion) as connection:
        connection.execute("CREATE TABLE completion_fixture (value TEXT)")
    return completion


@pytest.fixture
def previous_snapshot(snapshot_store):
    fixture = snapshot_store
    repository = fixture.repository
    completion = _add_completion_input(fixture)
    directory, key, _identity = snapshots._storage(repository)
    pin = snapshots._pin(repository, _file_identity(repository.path))
    with closing(sqlite3.connect(repository.path)) as connection:
        rows = connection.execute("SELECT * FROM main.conversations").fetchall()
    assert len(rows) == 1
    payload = {
        "pin": pin,
        "rows": [list(row) for row in rows],
        "resource_status": "ready",
        "resource_index_accounts": [rows[0][2]],
    }
    assert snapshots._write_snapshot(directory, key, payload, lambda: True)
    return fixture, completion, rows


def _change_completion_input(completion):
    with sqlite3.connect(completion) as connection:
        connection.execute("INSERT INTO completion_fixture VALUES ('updated')")


def test_previous_snapshot_reads_only_signed_summary(previous_snapshot, monkeypatch):
    fixture, completion, rows = previous_snapshot
    _change_completion_input(completion)
    monkeypatch.setattr(snapshots, "_code_fingerprint", lambda: "reader-code-v2")

    def unexpected_write(*_args):
        pytest.fail("The previous snapshot loader must only read existing cache files.")

    for name in ("_storage", "_build", "_write_snapshot"):
        monkeypatch.setattr(snapshots, name, unexpected_write)
    directory = _directory(fixture)
    before = {path: snapshots._state(path.lstat()) for path in directory.iterdir()}
    assert _load_previous(fixture) == (
        ("browser_snapshot_conversations", "ready", (rows[0][2],)), rows,
    )
    assert {path: snapshots._state(path.lstat()) for path in directory.iterdir()} == before
    assert fixture.snapshot_calls == []
    assert not snapshots._PENDING and not snapshots._SNAPSHOTS


def test_previous_snapshot_accepts_an_unchanged_archive_after_reader_update(
    previous_snapshot, monkeypatch
):
    fixture, _completion, rows = previous_snapshot
    monkeypatch.setattr(snapshots, "_code_fingerprint", lambda: "updated-reader-code")

    assert _load_previous(fixture) == (
        ("browser_snapshot_conversations", "ready", (rows[0][2],)), rows,
    )


def test_previous_snapshot_does_not_create_storage(snapshot_store):
    assert _load_previous(snapshot_store) == (None, [])
    assert not _directory(snapshot_store).exists()


def test_previous_snapshot_allows_first_completion_publication(snapshot_store):
    verified = _load(snapshot_store)
    assert verified[0] is not None
    _add_completion_input(snapshot_store)
    assert _load_previous(snapshot_store) == verified


@pytest.mark.parametrize(
    "drift",
    ("unchanged", "missing", "unavailable", "registry", "tampered"),
)
def test_previous_snapshot_requires_only_stable_completion_input_change(previous_snapshot, drift):
    fixture, completion, _rows = previous_snapshot
    if drift == "missing":
        completion.unlink()
    elif drift == "unavailable":
        completion.with_name(completion.name + "-wal").touch()
    elif drift == "registry":
        _change_completion_input(completion)
        before = fixture.registry.stat()
        os.utime(fixture.registry, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
    elif drift == "tampered":
        _change_completion_input(completion)
        snapshot = _directory(fixture) / "conversations.snapshot"
        snapshot.write_bytes(snapshot.read_bytes() + b" ")
    assert _load_previous(fixture) == (None, [])


@pytest.mark.parametrize("name", ("signing.key", "conversations.snapshot"))
def test_previous_snapshot_discards_summary_if_cache_path_changes(
    previous_snapshot, monkeypatch, name
):
    fixture, completion, _rows = previous_snapshot
    _change_completion_input(completion)
    install = snapshots._install

    def drift_after_install(connection, payload):
        result = install(connection, payload)
        path = _directory(fixture) / name
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(path.read_bytes())
        replacement.chmod(0o600)
        replacement.replace(path)
        return result

    monkeypatch.setattr(snapshots, "_install", drift_after_install)
    assert _load_previous(fixture) == (None, [])
