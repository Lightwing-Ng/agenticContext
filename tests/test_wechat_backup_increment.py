"""Check real resource receipt shapes used by coordinated native increments.

Code version: v1.1.1-codex.0
"""

import json
import sqlite3

import pytest

from app.core.wechat_archive import import_wechat_archive, wechat_archive_path
from scripts.apply_wechat_backup_increment import resource_validation, guarded_text_restore
from scripts.inspect_wechat_backup_increment import reject_manifest_sidecars
from scripts.inspect_wechat_backup_increment import require_previous_publication
from app.core import wechat_native_restore
from tests import test_wechat_native_restore as restore_fixtures
from tests.test_wechat_native_restore import source, record, merge
from tests.test_wechat_archive import make_archive
from tests.test_wechat_resource_timeline import _reference, _session, _sidecar


fixture_clone_backend = restore_fixtures.fixture_clone_backend


@pytest.mark.parametrize("stale", [False, True])
def test_resource_guard_uses_real_dictionary_identity_shape(tmp_path, stale):
    root = tmp_path / "store"
    import_wechat_archive(make_archive(tmp_path / "archive"), root)
    conversation = _session(root, kind="direct")
    resource = _sidecar(root, [(conversation, _reference(conversation, "[Video]", 17))])
    with sqlite3.connect(resource) as connection:
        # Native extension receipts declare the complete event count explicitly.
        connection.execute("INSERT INTO archive_info VALUES('messages','1')")
    if stale:
        with sqlite3.connect(resource) as connection:
            raw = connection.execute("SELECT value FROM archive_info WHERE key='history_file_identity'").fetchone()[0]
            value = json.loads(raw)
            assert isinstance(value, dict)
            value["inode"] += 1
            connection.execute("UPDATE archive_info SET value=? WHERE key='history_file_identity'", (json.dumps(value),))
        with pytest.raises(ValueError, match="stale primary identity"):
            resource_validation(wechat_archive_path(root), resource)
    else:
        assert resource_validation(wechat_archive_path(root), resource) == {"events": 1, "conversations": 1}


@pytest.mark.parametrize("selected_wal", [False, True])
def test_sidecars_are_discovered_by_original_manifest_path(selected_wal):
    connection = sqlite3.connect(":memory:")
    path = "Documents/native-account/DB/message_1.sqlite"
    try:
        connection.execute("CREATE TABLE Files(domain TEXT,relativePath TEXT)")
        connection.execute("INSERT INTO Files VALUES(?,?)", ("AppDomain-another-app", path + "-wal"))
        connection.execute("INSERT INTO Files VALUES(?,?)", ("AppDomain-com.tencent.xin", "Documents/another/DB/message_1.sqlite-wal"))
        if selected_wal:
            connection.execute("INSERT INTO Files VALUES(?,?)", ("AppDomain-com.tencent.xin", path + "-wal"))
            with pytest.raises(ValueError, match="selected SQLite sidecar"):
                reject_manifest_sidecars(connection, [path])
        else:
            reject_manifest_sidecars(connection, [path])
    finally:
        connection.close()


def test_actual_sqlite_disk_interrupt_preserves_original_and_restores_adapters(tmp_path):
    store = tmp_path / "store"
    metadata = source()
    merge(store, [metadata], [record(metadata)])
    path = wechat_archive_path(store)
    before = path.read_bytes()
    original_sqlite, original_os = wechat_native_restore.sqlite3, wechat_native_restore.os
    calls = 0

    def space_check():
        nonlocal calls
        calls += 1
        if calls > 1:
            raise ValueError("Injected actual-free-space reserve")

    with pytest.raises(ValueError, match="actual disk reserve"):
        guarded_text_restore(
            store, sources=[metadata], records=[record(metadata, "later", server_id="9", text="new")],
            snapshot_id="later-snapshot", apply=True, check=space_check, progress_steps=1,
        )
    assert calls > 1
    assert path.read_bytes() == before
    assert wechat_native_restore.sqlite3 is original_sqlite
    assert wechat_native_restore.os is original_os


@pytest.mark.parametrize("published", [False, True])
def test_unpublished_previous_snapshot_cannot_be_a_skip_base(tmp_path, published):
    operation = tmp_path / "coordinated-increment-v1.0.2"
    operation.mkdir()
    path = operation / "final-v1.0.0.json"
    state = "canonical_increment_published_and_readback_verified_clock_pending" if published else "all_candidates_verified_not_published"
    path.write_text(json.dumps({"state": state}))
    if published:
        assert require_previous_publication(tmp_path)["path"] == str(path)
    else:
        with pytest.raises(ValueError, match="no verified primary publication"):
            require_previous_publication(tmp_path)
