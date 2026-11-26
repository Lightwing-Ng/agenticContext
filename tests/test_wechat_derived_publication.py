"""Scoped retirement publication keeps native events and resource pins recoverable.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import wechat_derived_publication as publication
from app.core import wechat_derived_retirement as retirement
from app.core.wechat_archive import _file_identity, wechat_history_partition_fingerprint
from app.core.wechat_derived_redundancy import plan_derived_redundancy
from tests import test_wechat_derived_retirement as fixtures


prepared = fixtures.prepared
redundancy_store = fixtures.redundancy_store


def _rows(path):
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        return {row["id"]: dict(row) for row in connection.execute("SELECT * FROM messages")}


def _sidecar(path, history, *, pin=None):
    with closing(sqlite3.connect(history)) as connection, connection:
        fingerprint = wechat_history_partition_fingerprint(connection)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(
            "CREATE TABLE messages(id TEXT PRIMARY KEY,payload TEXT);"
            "CREATE TABLE archive_info(key TEXT PRIMARY KEY,value TEXT);"
        )
        connection.execute("INSERT INTO messages VALUES('native-resource','opaque resource metadata')")
        connection.executemany("INSERT INTO archive_info VALUES(?,?)", [
            ("schema_version", "1"),
            ("history_file_identity", publication._json(pin or _file_identity(history))),
            ("history_partitions_sha256", fingerprint),
            ("untouched_metadata", '{"spaces": "are preserved", "order": 3}'),
        ])


@pytest.fixture
def publication_store(prepared, tmp_path, monkeypatch):
    fixture = prepared
    monkeypatch.setattr(publication, "_clonefile", shutil.copyfile)
    monkeypatch.setattr(publication, "_no_writers", lambda path: None)
    fixture.plan = plan_derived_redundancy(fixture.store)
    fixture.staged = retirement.stage_retirement(
        fixture.store, fixture.plan, backup_directory=tmp_path / "rollback", apply=True
    )
    fixture.candidate = Path(fixture.staged["candidate_path"])
    fixture.receipt = tmp_path / "rollback" / "publication.json"
    fixture.sidecars = [fixture.history.with_name(name) for name in publication._SIDECARS]
    for path in fixture.sidecars:
        _sidecar(path, fixture.history)
    return fixture


def _publish(fixture):
    return publication.publish_retirement(
        fixture.store, fixture.plan, fixture.staged, receipt_path=fixture.receipt
    )


def test_success_rebinds_only_current_pins_and_keeps_scoped_compressed_rollback(publication_store):
    fixture = publication_store
    original, registry = _rows(fixture.history), fixture.registry.read_bytes()
    infos = {path: publication._info(path) for path in fixture.sidecars}
    contents = {path: _rows(path) for path in fixture.sidecars}
    report = _publish(fixture)

    assert report["published"] is True and report["aliases_added"] == report["alias_count"] == 3
    assert report["filesystem_bytes_reclaimed"] == 0
    assert _rows(fixture.history) == {key: row for key, row in original.items() if key not in fixture.derived_ids}
    assert fixture.registry.read_bytes() == registry
    assert not fixture.candidate.exists()
    assert json.loads(fixture.receipt.read_text()) == report
    assert Path(report["rollback_path"]).is_file()
    with closing(sqlite3.connect(fixture.history)) as source, source:
        assert source.execute("SELECT COUNT(*) FROM derived_message_aliases").fetchone()[0] == 3
    for path in fixture.sidecars:
        assert _rows(path) == contents[path]
        assert publication._info(path) == {**infos[path], "history_file_identity": publication._json(_file_identity(fixture.history))}
    assert not list(fixture.history.parent.glob(".wechat-derived-publish-*"))


@pytest.mark.parametrize("changed", ["history", "registry", "candidate"])
def test_changed_input_refuses_before_replacement(publication_store, changed):
    fixture = publication_store
    path = getattr(fixture, changed)
    with path.open("ab") as stream:
        stream.write(b"changed input")
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    with pytest.raises(ValueError, match="differs|changed"):
        _publish(fixture)
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())
    assert not fixture.receipt.exists()
    assert not list(fixture.history.parent.glob(".wechat-derived-publish-*"))


def test_current_resource_containing_retired_message_refuses(publication_store):
    fixture = publication_store
    with closing(sqlite3.connect(fixture.sidecars[0])) as connection, connection:
        connection.execute("INSERT INTO messages VALUES(?,?)", (fixture.derived_ids[0], "must remain reachable"))
    before = fixture.history.read_bytes()
    with pytest.raises(ValueError, match="references a retired"):
        _publish(fixture)
    assert fixture.history.read_bytes() == before
    assert not fixture.receipt.exists()


def test_retired_html_resource_parent_also_refuses(publication_store):
    fixture = publication_store
    with closing(sqlite3.connect(fixture.sidecars[1])) as connection, connection:
        connection.executescript(
            "CREATE TABLE html_forwarded_resources(id TEXT PRIMARY KEY,parent_id TEXT);"
            "CREATE INDEX html_resource_parent ON html_forwarded_resources(parent_id);"
        )
        connection.execute("INSERT INTO html_forwarded_resources VALUES('child',?)", (fixture.derived_ids[0],))
    with pytest.raises(ValueError, match="references a retired"):
        _publish(fixture)
    assert not fixture.receipt.exists()


def test_stale_index_remains_byte_exact_and_is_not_reactivated(publication_store):
    fixture = publication_store
    stale = fixture.sidecars[1]
    info = publication._info(stale)
    publication._rebind(stale, info, {**_file_identity(fixture.history), "inode": 0})
    with closing(sqlite3.connect(stale)) as connection, connection:
        connection.execute("INSERT INTO messages VALUES(?,'stale derived metadata')", (fixture.derived_ids[0],))
    before = stale.read_bytes()
    report = _publish(fixture)
    assert stale.read_bytes() == before
    assert report["sidecars"][stale.name] == "already stale; unchanged"


def test_cloud_only_index_is_not_opened_or_reactivated(publication_store, monkeypatch):
    fixture = publication_store
    cold, stat, info = fixture.sidecars[1], Path.stat, publication._info

    class CloudState:
        def __init__(self, state):
            self.state, self.st_flags = state, getattr(state, "st_flags", 0) | 0x40000000

        def __getattr__(self, name):
            return getattr(self.state, name)

    def cloud_stat(path, *args, **kwargs):
        state = stat(path, *args, **kwargs)
        return CloudState(state) if path == cold else state

    def never_open(path):
        if path == cold:
            raise AssertionError("Cloud-only resource metadata must not be opened.")
        return info(path)

    before = cold.read_bytes()
    with monkeypatch.context() as context:
        context.setattr(Path, "stat", cloud_stat)
        context.setattr(publication, "_info", never_open)
        report = _publish(fixture)
    assert cold.read_bytes() == before
    assert report["sidecars"][cold.name].startswith("cloud-only; unchanged")


def test_second_replace_failure_restores_all_rows_and_current_resource_pins(publication_store, monkeypatch):
    fixture = publication_store
    original, registry = _rows(fixture.history), fixture.registry.read_bytes()
    infos = {path: publication._info(path) for path in fixture.sidecars}
    contents = {path: _rows(path) for path in fixture.sidecars}
    original_inode = fixture.history.stat().st_ino
    replace = publication.os.replace
    calls = 0

    def fail_second(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("Synthetic second replacement failure")
        return replace(source, destination)

    monkeypatch.setattr(publication.os, "replace", fail_second)
    with pytest.raises(OSError, match="second replacement failure"):
        _publish(fixture)
    assert _rows(fixture.history) == original
    assert fixture.history.stat().st_ino != original_inode
    assert fixture.registry.read_bytes() == registry
    assert not fixture.receipt.exists()
    with closing(sqlite3.connect(fixture.history)) as connection, connection:
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='derived_message_aliases'").fetchone()
    for path in fixture.sidecars:
        assert _rows(path) == contents[path]
        assert publication._info(path) == {**infos[path], "history_file_identity": publication._json(_file_identity(fixture.history))}
    assert Path(fixture.staged["report"]["rollback_path"]).exists()
    assert not list(fixture.history.parent.glob(".wechat-derived-publish-*"))


def test_clone_failure_has_no_full_copy_fallback(publication_store, monkeypatch):
    fixture = publication_store
    before = fixture.history.read_bytes()

    def unavailable(*_args):
        raise ValueError("APFS clone unavailable; no fallback")

    monkeypatch.setattr(publication, "_clonefile", unavailable)
    with pytest.raises(ValueError, match="no fallback"):
        _publish(fixture)
    assert fixture.history.read_bytes() == before
    assert fixture.candidate.exists() and not fixture.receipt.exists()


@pytest.mark.parametrize("field", ["unknown-count", "source-owner", "source-count", "conversation-count"])
def test_only_scoped_manifest_counts_may_change(publication_store, field):
    fixture = publication_store
    with closing(sqlite3.connect(fixture.candidate)) as connection, connection:
        manifest = publication._manifest(connection)
        if field == "unknown-count":
            manifest["unique_original_message_count"] = 6
        elif field == "source-owner":
            manifest["sources"][2]["account_id"] = "invented-owner"
        elif field == "source-count":
            manifest["sources"][2]["messages"] = 1
        else:
            connection.execute("UPDATE conversations SET message_count=1 WHERE partition_id=?", (fixture.sources[2],))
        connection.execute("UPDATE archive_info SET value=? WHERE key='manifest'", (publication._json(manifest),))
    fixture.staged["report"]["candidate_identity"] = retirement.file_identity(fixture.candidate)
    before = fixture.history.read_bytes()
    with pytest.raises(ValueError, match="manifest|counter"):
        _publish(fixture)
    assert fixture.history.read_bytes() == before
    assert not fixture.receipt.exists()


@pytest.mark.parametrize("which", ["history", "candidate", "resource"])
def test_even_empty_journals_block_publication(publication_store, which):
    fixture = publication_store
    path = fixture.sidecars[0] if which == "resource" else getattr(fixture, which)
    Path(str(path) + "-journal").write_bytes(b"")
    before = fixture.history.read_bytes()
    with pytest.raises(ValueError, match="journal"):
        _publish(fixture)
    assert fixture.history.read_bytes() == before
    assert not fixture.receipt.exists()


def test_receipt_failure_rolls_back_before_removing_temporary_evidence(publication_store, monkeypatch):
    fixture = publication_store
    original, original_open = _rows(fixture.history), Path.open

    def fail_receipt(path, mode="r", *args, **kwargs):
        if path == fixture.receipt and mode == "x":
            raise OSError("Synthetic publication receipt failure")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_receipt)
    with pytest.raises(OSError, match="receipt failure"):
        _publish(fixture)
    assert _rows(fixture.history) == original
    for path in fixture.sidecars:
        assert json.loads(publication._info(path)["history_file_identity"]) == _file_identity(fixture.history)
    assert not fixture.receipt.exists()
    assert not list(fixture.history.parent.glob(".wechat-derived-publish-*"))


def test_failed_recovery_keeps_clones_for_manual_review(publication_store, monkeypatch):
    fixture = publication_store
    replace, calls = publication.os.replace, 0

    def fail_publication_and_restore(source, destination):
        nonlocal calls
        calls += 1
        if calls in {2, 3}:
            raise OSError("Synthetic publication and rollback failure")
        return replace(source, destination)

    monkeypatch.setattr(publication.os, "replace", fail_publication_and_restore)
    with pytest.raises(RuntimeError, match="retain temporary clones"):
        _publish(fixture)
    assert list(fixture.history.parent.glob(".wechat-derived-publish-*"))
    assert Path(fixture.staged["report"]["rollback_path"]).is_file()
    assert not fixture.receipt.exists()


def test_publication_preserves_the_staging_free_space_floor(publication_store, monkeypatch):
    fixture = publication_store
    before = fixture.history.read_bytes()
    monkeypatch.setattr(publication.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    with pytest.raises(ValueError, match="free-space floor"):
        _publish(fixture)
    assert fixture.history.read_bytes() == before
    assert fixture.candidate.exists() and not fixture.receipt.exists()
    assert not list(fixture.history.parent.glob(".wechat-derived-publish-*"))
