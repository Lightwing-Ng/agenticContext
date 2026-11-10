"""Authenticated raw overviews survive reloads without reusing changed evidence.

Code version: v1.0.0-codex.0
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
