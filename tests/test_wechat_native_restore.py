"""Native subset restoration preserves old rows without a full database copy.

Code version: v1.4.0-codex.0
"""

from contextlib import closing
import json
import errno
from datetime import datetime
import hashlib
import os
import shutil
import sqlite3
import sys

import pytest

from app.core import wechat_native_restore as restore
from app.core import wechat_reconcile as reconcile
from app.core.owner_only_files import OwnerOnlyFileError, owner_only_problem
from app.core.wechat_archive import _identifier, wechat_archive_path
from app.core.wechat_reconcile import reconcile_wechat_records


_CLOCK = "2021-08-07T10:59:03+00:00"
_EPOCH = int(datetime.fromisoformat(_CLOCK).timestamp())


def source(owner="wxid_account_a", *, unknown_hash=None):
    account_hash = unknown_hash or hashlib.md5(owner.encode()).hexdigest()
    return {
        "source_id": ("ios-unconfirmed-" if unknown_hash else "ios-") + account_hash,
        "account_hash": account_hash,
        "source_account_id": None if unknown_hash else owner,
        "account_hash_verified": unknown_hash is None,
        "source_file": "Synthetic native backup",
    }


def record(metadata, record_id="native-1", *, peer="peer-direct", **changes):
    value = {
        "source_id": metadata["source_id"],
        "account_partition": metadata["source_id"],
        "source_account_id": metadata["source_account_id"],
        "extraction_record_id": record_id,
        "conversation_id": peer,
        "conversation_hash": hashlib.md5(peer.encode()).hexdigest(),
        "conversation_display_name": "Synthetic conversation",
        "conversation_kind": "group" if peer.endswith("@chatroom") else "direct",
        "sender_id": "wxid_peer",
        "sender_display_name": "Synthetic sender",
        "direction": "received",
        "text": "  Literal original\nwith whitespace  ",
        "message_type": 1,
        "text_extraction_kind": "plain_text",
        "server_id": "9223372036854775806",
        "sent_at_utc_iso": _CLOCK,
        "sent_at_raw": str(_EPOCH),
        "unix_timestamp": _EPOCH,
        "source_locator": {
            "database": "MM.sqlite",
            "table": "Chat_fixture",
            "row_id": record_id,
        },
        "flags": [],
    }
    value.update(changes)
    return value


def merge(store, metadata, observations, snapshot="snapshot-one"):
    return reconcile_wechat_records(
        store, sources=metadata, records=iter(observations), snapshot_id=snapshot
    )


def rows(store, table="messages"):
    path = wechat_archive_path(store)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection, connection:
        return connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()


@pytest.fixture(autouse=True)
def fixture_clone_backend(monkeypatch):
    # Small synthetic fixtures can exercise publication logic on non-macOS CI.
    # Production always retains the mandatory APFS-only backend.
    if sys.platform != "darwin":
        monkeypatch.setattr(restore, "_clonefile", shutil.copyfile)


def run(store, metadata, observations, *, apply=True, snapshot="restore-snapshot"):
    return restore.restore_wechat_text_subset(
        store,
        sources=metadata,
        records=iter(observations),
        snapshot_id=snapshot,
        apply=apply,
    )


def retained(store):
    return {
        table: rows(store, table)
        for table in ("messages", "partitions", "message_evidence", "source_snapshots")
    }


def seed(store):
    metadata = source()
    first = record(metadata, "existing")
    merge(store, [metadata], [first])
    return metadata, first


def test_preview_validates_a_complete_stage_without_publishing(tmp_path):
    metadata, _ = seed(tmp_path)
    path = wechat_archive_path(tmp_path)
    before = path.read_bytes()
    result = run(
        tmp_path,
        [metadata],
        [record(metadata, "new", text="New original text", server_id="222")],
        apply=False,
    )
    assert result["changed"] and not result["published"]
    assert result["inserted_messages"] == 1
    assert result["messages_before"] == 1 and result["messages_after"] == 2
    assert result["integrity_check"] == "ok"
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".wechat-native-restore-*"))


def test_first_store_can_be_previewed_then_published_exactly(tmp_path, monkeypatch):
    connect = sqlite3.connect
    opened = []

    def private_stage(path, *args, **kwargs):
        assert path.read_bytes() == b""
        assert owner_only_problem(path) == ""
        opened.append(path)
        return connect(path, *args, **kwargs)

    metadata = source()
    observation = record(metadata)
    monkeypatch.setattr(restore.sqlite3, "connect", private_stage)
    preview = run(tmp_path, [metadata], [observation], apply=False)
    assert (
        preview["integrity_check"] == "ok"
        and not wechat_archive_path(tmp_path).exists()
    )
    published = run(tmp_path, [metadata], [observation])
    monkeypatch.setattr(restore.sqlite3, "connect", connect)
    assert published["published"] and published["messages_after"] == 1
    assert rows(tmp_path)[0][6] == observation["text"]
    assert len(opened) == 2
    assert owner_only_problem(wechat_archive_path(tmp_path)) == ""
    if os.name == "posix":
        assert wechat_archive_path(tmp_path).stat().st_mode & 0o777 == 0o600


def test_unverified_native_stage_is_removed_before_any_private_sqlite_bytes(tmp_path, monkeypatch):
    connected = []

    def refuse(path):
        assert path.read_bytes() == b""
        raise OwnerOnlyFileError("Synthetic native restore boundary failure")

    monkeypatch.setattr(restore, "ensure_owner_only", refuse)
    monkeypatch.setattr(restore.sqlite3, "connect", lambda *args, **kwargs: connected.append(args))
    metadata = source()
    with pytest.raises(OwnerOnlyFileError, match="Synthetic native restore boundary failure"):
        run(tmp_path, [metadata], [record(metadata)])
    assert connected == []
    assert not wechat_archive_path(tmp_path).exists()
    assert not list(wechat_archive_path(tmp_path).parent.glob(".wechat-native-restore-*"))


def test_pushmail_notice_preserves_its_native_type_and_bounded_text(tmp_path):
    metadata, _ = seed(tmp_path)
    before = retained(tmp_path)
    observation = record(
        metadata,
        "pushmail-notice",
        peer="qqmail",
        message_type=35,
        text_extraction_kind="pushmail_text",
        server_id="7766",
        text="From: Fixture sender\nSubject: Original subject\nOriginal digest",
    )
    result = run(tmp_path, [metadata], [observation])
    assert result["inserted_messages"] == 1 and result["published"]
    saved = next(row for row in rows(tmp_path) if row not in before["messages"])
    provenance = json.loads(saved[11])
    assert saved[6] == observation["text"]
    assert provenance["message_type"] == 35
    assert provenance["text_extraction_kind"] == "pushmail_text"
    assert provenance["locator"] == observation["source_locator"]
    assert all(row in rows(tmp_path) for row in before["messages"])
    assert rows(tmp_path, "partitions") == before["partitions"]


@pytest.mark.parametrize(
    "message_type,text_kind",
    [
        (35, "plain_text"),
        (35, "system_text"),
        (1, "pushmail_text"),
        (3, "pushmail_text"),
        (49, "pushmail_text"),
        (10000, "pushmail_text"),
    ],
)
def test_pushmail_wrong_kind_or_relabeling_cannot_publish(
    tmp_path, message_type, text_kind
):
    metadata, _ = seed(tmp_path)
    path = wechat_archive_path(tmp_path)
    before = path.read_bytes()
    observation = record(
        metadata,
        "invalid-pushmail",
        message_type=message_type,
        text_extraction_kind=text_kind,
    )
    with pytest.raises(ValueError, match="explicitly classified native text"):
        run(tmp_path, [metadata], [observation])
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".wechat-native-restore-*"))


def test_replay_does_not_replace_the_live_file_or_append_evidence(tmp_path):
    metadata, original = seed(tmp_path)
    before = wechat_archive_path(tmp_path).read_bytes()
    result = run(tmp_path, [metadata], [original], snapshot="snapshot-one")
    assert not result["changed"] and not result["published"]
    assert result["replayed_observations"] == 1
    assert wechat_archive_path(tmp_path).read_bytes() == before


def test_server_match_adds_only_evidence_and_preserves_every_old_row(tmp_path):
    metadata, old = seed(tmp_path)
    before = retained(tmp_path)
    newer = record(metadata, "different-row-location", text=old["text"])
    result = run(tmp_path, [metadata], [newer])
    assert result["published"] and result["inserted_messages"] == 0
    assert result["matched_messages"] == result["new_evidence"] == 1
    assert rows(tmp_path, "messages") == before["messages"]
    assert rows(tmp_path, "partitions") == before["partitions"]
    evidence = json.loads(rows(tmp_path, "message_evidence")[0][5])
    assert "text" not in evidence and "content_sha256" in evidence
    assert all(
        item in rows(tmp_path, "source_snapshots")
        for item in before["source_snapshots"]
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("text", "Conflicting body"),
        ("sender_id", "wxid_other"),
        ("direction", "sent"),
        ("unix_timestamp", 12345),
    ],
)
def test_same_server_conflicts_retain_both_source_occurrences(tmp_path, field, value):
    metadata, old = seed(tmp_path)
    change = record(metadata, "new-observation", **{field: value})
    result = run(tmp_path, [metadata], [change])
    assert result["inserted_messages"] == result["conflict_variants"] == 1
    assert len(rows(tmp_path)) == 2
    assert any(
        row[0] == _identifier(metadata["source_id"], old["extraction_record_id"])
        and row[6] == old["text"]
        for row in rows(tmp_path)
    )


def test_extraction_identity_conflict_uses_a_stable_variant(tmp_path):
    metadata, old = seed(tmp_path)
    changed = {**old, "text": "Another exact source version"}
    first = run(tmp_path, [metadata], [changed])
    assert first["identity_conflicts"] == first["conflict_variants"] == 1
    before = rows(tmp_path)
    second = run(tmp_path, [metadata], [changed])
    assert not second["changed"] and second["replayed_observations"] == 1
    assert rows(tmp_path) == before


@pytest.mark.parametrize("server", [None, "0", 0, 1.5, True])
def test_no_stable_server_id_never_matches_by_body_alone(tmp_path, server):
    metadata = source()
    old = record(metadata, "first", server_id=server)
    merge(tmp_path, [metadata], [old])
    result = run(tmp_path, [metadata], [record(metadata, "second", server_id=server)])
    assert result["inserted_messages"] == 1 and result["new_evidence"] == 0
    assert len(rows(tmp_path)) == 2


def test_cross_account_peer_and_unknown_hash_boundaries_are_preserved(tmp_path):
    first, second = source(), source("wxid_second_owner")
    unknown = source(unknown_hash="1" * 32)
    merge(tmp_path, [first], [record(first, "known")])
    result = run(
        tmp_path,
        [first, second, unknown],
        [
            record(first, "another-peer", peer="other-peer"),
            record(second, "other-account"),
            record(unknown, "unknown-one"),
            record(unknown, "unknown-two"),
        ],
    )
    assert result["inserted_messages"] == 4
    partitions = {row[0]: row for row in rows(tmp_path, "partitions")}
    assert partitions[unknown["source_id"]][1] == "source:" + unknown["source_id"]
    assert partitions[unknown["source_id"]][2] is None
    assert len(rows(tmp_path)) == 5


def test_different_snapshot_metadata_retains_numeric_snapshot_identity(tmp_path):
    metadata, old = seed(tmp_path)
    revised = {**metadata, "source_file": "Another original native copy"}
    original_partition = rows(tmp_path, "partitions")
    result = run(tmp_path, [revised], [old], snapshot="snapshot-one")
    assert result["added_source_snapshots"] == 1 and result["new_evidence"] == 1
    snapshots = rows(tmp_path, "source_snapshots")
    assert len(snapshots) == 2 and snapshots[0][1] == snapshots[1][1]
    assert rows(tmp_path, "partitions") == original_partition
    evidence = json.loads(rows(tmp_path, "message_evidence")[0][5])
    assert evidence["source_snapshot"] == snapshots[-1][0]


def test_unrelated_conversation_bodies_are_not_read_or_parsed(tmp_path, monkeypatch):
    metadata, _ = seed(tmp_path)
    merge(
        tmp_path,
        [metadata],
        [record(metadata, "unrelated", peer="unrelated-peer", server_id="789")],
        snapshot="other",
    )
    path = wechat_archive_path(tmp_path)
    unrelated_id = _identifier(metadata["source_id"], "unrelated")
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE messages SET provenance='opaque unrelated evidence' WHERE id=?",
            (unrelated_id,),
        )
    before = rows(tmp_path)
    statements = []
    connect = sqlite3.connect

    def traced(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(restore.sqlite3, "connect", traced)
    result = run(tmp_path, [metadata], [record(metadata, "recovered", server_id="456")])
    assert result["indexed_existing_messages"] == 1
    assert result["indexed_conversations"] == 1
    assert any(
        "INDEXED BY message_conversation_time WHERE conversation_id=" in sql
        for sql in statements
    )
    assert any(sql == "PRAGMA integrity_check" for sql in statements)
    assert all(old in rows(tmp_path) for old in before)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE messages SET text='bad'",
        "DELETE FROM messages",
        "UPDATE partitions SET account_id='bad'",
        "UPDATE source_snapshots SET metadata='bad'",
        "DELETE FROM message_evidence",
        "UPDATE conversations SET peer_id='bad'",
        "DROP TRIGGER temp.restore_no_replace_messages",
        "INSERT OR REPLACE INTO messages SELECT * FROM messages LIMIT 1",
        "INSERT OR REPLACE INTO source_snapshots(snapshot_id,source_id,metadata) SELECT snapshot_id,source_id,metadata FROM source_snapshots WHERE id=1",
        "CREATE TEMP VIEW source_snapshots AS SELECT 0 AS id",
    ],
)
def test_guards_abort_forbidden_changes_without_touching_live(
    tmp_path, monkeypatch, statement
):
    metadata, _ = seed(tmp_path)
    path = wechat_archive_path(tmp_path)
    before = path.read_bytes()

    def forbidden(connection, identifier):
        connection.execute(statement)

    monkeypatch.setattr(restore, "_update_conversation", forbidden)
    with pytest.raises(sqlite3.DatabaseError):
        run(tmp_path, [metadata], [record(metadata, "new", server_id="456")])
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".wechat-native-restore-*"))


def test_clone_failure_has_no_full_copy_fallback(tmp_path, monkeypatch):
    metadata, _ = seed(tmp_path)
    before = wechat_archive_path(tmp_path).read_bytes()

    def fail(source_path, destination):
        raise ValueError("APFS clone unavailable")

    monkeypatch.setattr(restore, "_clonefile", fail)
    with pytest.raises(ValueError, match="APFS clone"):
        run(tmp_path, [metadata], [record(metadata, "new", server_id="456")])
    assert wechat_archive_path(tmp_path).read_bytes() == before


@pytest.mark.parametrize("change_policy", [False, True])
def test_concurrent_archive_or_policy_change_prevents_publication(
    tmp_path, change_policy
):
    metadata, _ = seed(tmp_path)
    path = wechat_archive_path(tmp_path)

    def incoming():
        yield record(metadata, "new", server_id="456")
        if change_policy:
            path.with_name("exclusions.json").write_text(
                '{"schema_version":1,"excluded_source_ids":[]}'
            )
        else:
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    "INSERT INTO archive_info VALUES('concurrent','preserve')"
                )

    with pytest.raises(ValueError, match="archive or exclusions changed"):
        restore.restore_wechat_text_subset(
            tmp_path,
            sources=[metadata],
            records=incoming(),
            snapshot_id="new-snapshot",
            apply=True,
        )
    assert len(rows(tmp_path)) == 1
    if not change_policy:
        with closing(sqlite3.connect(path)) as connection, connection:
            assert (
                connection.execute(
                    "SELECT value FROM archive_info WHERE key='concurrent'"
                ).fetchone()[0]
                == "preserve"
            )


def test_manifest_mismatch_is_rejected_before_any_publication(tmp_path):
    metadata, _ = seed(tmp_path)
    path = wechat_archive_path(tmp_path)
    with closing(sqlite3.connect(path)) as connection, connection:
        manifest = json.loads(
            connection.execute(
                "SELECT value FROM archive_info WHERE key='manifest'"
            ).fetchone()[0]
        )
        manifest["messages"] += 1
        connection.execute(
            "UPDATE archive_info SET value=? WHERE key='manifest'",
            (json.dumps(manifest),),
        )
    before = path.read_bytes()
    with pytest.raises(ValueError, match="counts disagree"):
        run(tmp_path, [metadata], [record(metadata, "new", server_id="456")])
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO archive_info VALUES('unrelated','bad')",
        "INSERT OR REPLACE INTO archive_info VALUES('retained','bad')",
        "DELETE FROM archive_info WHERE key='retained'",
    ],
)
def test_manifest_scope_cannot_replace_unrelated_metadata(statement):
    with closing(sqlite3.connect(":memory:")) as connection, connection:
        connection.executescript(restore._SCHEMA + restore._ADDITIVE_SCHEMA)
        connection.execute("INSERT INTO archive_info VALUES('retained','unchanged')")
        scope = restore._install_guards(connection)
        scope["manifest"] = True
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(statement)
        assert (
            connection.execute(
                "SELECT value FROM archive_info WHERE key='retained'"
            ).fetchone()[0]
            == "unchanged"
        )


def test_fsync_time_live_change_is_checked_before_the_final_rename(
    tmp_path, monkeypatch
):
    metadata, _ = seed(tmp_path)
    path = wechat_archive_path(tmp_path)
    fsync = restore.os.fsync

    def concurrent(descriptor):
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute(
                "INSERT INTO archive_info VALUES('late-writer','preserve')"
            )
        fsync(descriptor)

    monkeypatch.setattr(restore.os, "fsync", concurrent)
    with pytest.raises(ValueError, match="archive or exclusions changed"):
        run(tmp_path, [metadata], [record(metadata, "new", server_id="456")])
    assert len(rows(tmp_path)) == 1
    with closing(sqlite3.connect(path)) as connection, connection:
        assert (
            connection.execute(
                "SELECT value FROM archive_info WHERE key='late-writer'"
            ).fetchone()[0]
            == "preserve"
        )


@pytest.mark.parametrize("platform_name", ["nt", "posix"])
def test_stage_sync_uses_a_windows_writable_handle_and_preserves_posix_read_only(
    tmp_path, monkeypatch, platform_name,
):
    from pathlib import Path

    metadata, _ = seed(tmp_path)
    before = retained(tmp_path)
    open_file = Path.open
    handles = []

    def track_stage(path, *args, **kwargs):
        handle = open_file(path, *args, **kwargs)
        if path.name.startswith(".wechat-native-restore-"):
            handles.append(handle)
        return handle

    class PlatformOs:
        name = platform_name

        def __getattr__(self, name):
            return getattr(os, name)

        def fsync(self, descriptor):
            handle = next(item for item in handles if not item.closed and item.fileno() == descriptor)
            if self.name == "nt" and not handle.writable():
                raise OSError(errno.EBADF, "Windows cannot fsync a read-only stage descriptor")
            assert handle.writable() == (self.name == "nt")
            if os.name == "nt" and self.name == "posix":
                # Model a POSIX read-only flush while still using a legal native CRT handle.
                with open_file(Path(handle.name), "r+b") as native:
                    return os.fsync(native.fileno())
            return os.fsync(descriptor)

    monkeypatch.setattr(Path, "open", track_stage)
    monkeypatch.setattr(restore, "os", PlatformOs())
    result = run(tmp_path, [metadata], [record(metadata, "sync-portable", server_id="7799")])
    assert result["published"] and result["inserted_messages"] == 1
    assert all(item in rows(tmp_path) for item in before["messages"])
    assert handles and all(handle.closed for handle in handles)
    assert not list(wechat_archive_path(tmp_path).parent.glob(".wechat-native-restore-*"))


def test_stage_sync_failure_preserves_the_live_archive_and_cleans_unpublished_stage(tmp_path, monkeypatch):
    metadata, _ = seed(tmp_path)
    path = wechat_archive_path(tmp_path)
    before = path.read_bytes()

    def fail_sync(_descriptor):
        raise OSError(errno.EIO, "Synthetic native stage sync failure")

    monkeypatch.setattr(restore.os, "fsync", fail_sync)
    with pytest.raises(OSError, match="Synthetic native stage sync failure"):
        run(tmp_path, [metadata], [record(metadata, "unpublished-sync", server_id="7800")])
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".wechat-native-restore-*"))


def test_incorrect_summary_fields_cannot_pass_on_counts_alone(tmp_path, monkeypatch):
    metadata, _ = seed(tmp_path)
    before = wechat_archive_path(tmp_path).read_bytes()
    update = restore._update_conversation

    def wrong(connection, identifier):
        update(connection, identifier)
        connection.execute(
            "UPDATE conversations SET first_time='incorrect' WHERE id=?", (identifier,)
        )

    monkeypatch.setattr(restore, "_update_conversation", wrong)
    with pytest.raises(ValueError, match="statistics disagree"):
        run(tmp_path, [metadata], [record(metadata, "new", server_id="456")])
    assert wechat_archive_path(tmp_path).read_bytes() == before


def test_sidless_records_do_not_load_retained_provenance(tmp_path):
    metadata, _ = seed(tmp_path)
    path = wechat_archive_path(tmp_path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE messages SET provenance='opaque retained evidence'")
    old = rows(tmp_path)[0]
    result = run(tmp_path, [metadata], [record(metadata, "no-sid", server_id=None)])
    assert result["inserted_messages"] == 1
    assert (
        result["server_indexed_conversations"]
        == result["indexed_existing_messages"]
        == 0
    )
    assert old in rows(tmp_path)


def test_each_incoming_conversation_checks_actual_retained_index_counts(tmp_path):
    metadata, _ = seed(tmp_path)
    merge(
        tmp_path,
        [metadata],
        [record(metadata, "other", peer="other-peer", server_id="789")],
        snapshot="other",
    )
    path = wechat_archive_path(tmp_path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE conversations SET message_count=message_count+1 WHERE peer_id='peer-direct'"
        )
        connection.execute(
            "UPDATE conversations SET message_count=message_count-1 WHERE peer_id='other-peer'"
        )
    before = path.read_bytes()
    with pytest.raises(ValueError, match="inconsistent retained counts"):
        run(tmp_path, [metadata], [record(metadata, "new", server_id=None)])
    assert path.read_bytes() == before


def change_ctime_only(path):
    before = restore._file_identity(path)
    permissions = path.stat().st_mode & 0o777
    # The owner-write bit also toggles Windows' native read-only attribute.
    path.chmod(permissions ^ 0o200)
    path.chmod(permissions)
    after = restore._file_identity(path)
    assert before[:4] == after[:4] and before[4] != after[4]


def test_windows_identity_retains_native_birth_and_change_tokens(tmp_path, monkeypatch):
    path = tmp_path / "synthetic-policy.json"
    path.write_bytes(b"unchanged synthetic policy")
    metadata = path.stat()
    token = [101]

    class WindowsOs:
        name = "nt"

        def __getattr__(self, name):
            return getattr(os, name)

    def change_time(observed):
        assert observed == path
        return token[0]

    monkeypatch.setattr(reconcile, "os", WindowsOs())
    monkeypatch.setattr(reconcile, "file_change_time", change_time)
    before = reconcile._file_identity(path)
    token[0] += 1
    after = reconcile._file_identity(path)
    assert before[:4] == after[:4]
    assert (before[4], after[4]) == (101, 102)
    assert before[5] == after[5] == getattr(
        metadata, "st_birthtime_ns", metadata.st_ctime_ns
    )

    def unavailable(_path):
        raise OSError("Synthetic native metadata failure")

    monkeypatch.setattr(reconcile, "file_change_time", unavailable)
    with pytest.raises(OSError, match="Synthetic native metadata failure"):
        reconcile._file_identity(path)


def test_birth_drift_cannot_enter_the_change_time_only_hash_fallback(tmp_path, monkeypatch):
    path = tmp_path / "synthetic-policy.json"
    path.write_bytes(b"unchanged synthetic policy")
    original = (1, 2, path.stat().st_size, 3, 101, 201)
    monkeypatch.setattr(restore, "_file_identity", lambda _path: (*original[:4], 102, 202))

    def unexpected_content_read(_path):
        pytest.fail("A changed birth identity must be refused before fallback hashing.")

    monkeypatch.setattr(restore, "_stream_sha256", unexpected_content_read)
    assert restore._content_identity_matches(path, original, "f" * 64) is False


@pytest.mark.parametrize("phase", ["clone", "final"])
def test_ctime_only_drift_requires_matching_original_bytes(tmp_path, monkeypatch, phase):
    metadata, _ = seed(tmp_path)
    path = wechat_archive_path(tmp_path)
    before = retained(tmp_path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    clone = restore._clonefile

    def clone_with_metadata_drift(original, stage):
        clone(original, stage)
        change_ctime_only(original)

    if phase == "clone":
        monkeypatch.setattr(restore, "_clonefile", clone_with_metadata_drift)

    def incoming():
        yield record(metadata, "new", server_id="456")
        if phase == "final":
            change_ctime_only(path)

    result = run(tmp_path, [metadata], incoming())
    assert result["published"] and result["inserted_messages"] == 1
    assert result["original_main_sha256"] == digest
    assert all(item in rows(tmp_path) for item in before["messages"])
    assert rows(tmp_path, "partitions") == before["partitions"]


def test_changed_main_bytes_cannot_hide_behind_restored_size_and_mtime(tmp_path):
    metadata, _ = seed(tmp_path)
    path = wechat_archive_path(tmp_path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("INSERT INTO archive_info VALUES('drift-fixture','AAAA')")
    initial = path.stat()
    identity = restore._file_identity(path)

    def incoming():
        yield record(metadata, "new", server_id="456")
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute(
                "UPDATE archive_info SET value='BBBB' WHERE key='drift-fixture'"
            )
        os.utime(path, ns=(initial.st_atime_ns, initial.st_mtime_ns))
        current = restore._file_identity(path)
        assert current[:4] == identity[:4] and current[4] != identity[4]

    with pytest.raises(ValueError, match="archive or exclusions changed"):
        run(tmp_path, [metadata], incoming())
    assert len(rows(tmp_path)) == 1
    with closing(sqlite3.connect(path)) as connection, connection:
        assert connection.execute(
            "SELECT value FROM archive_info WHERE key='drift-fixture'"
        ).fetchone()[0] == "BBBB"


@pytest.mark.parametrize("change_bytes", [False, True])
def test_policy_ctime_drift_requires_unchanged_policy_bytes(tmp_path, change_bytes):
    metadata, _ = seed(tmp_path)
    path = wechat_archive_path(tmp_path)
    policy = path.with_name("exclusions.json")
    policy.write_text('{"schema_version":1,"sources":[],"note":"AAAA"}')
    initial = policy.stat()
    digest = hashlib.sha256(policy.read_bytes()).hexdigest()

    def incoming():
        yield record(metadata, "new", server_id="456")
        if change_bytes:
            policy.write_text(policy.read_text().replace("AAAA", "BBBB"))
            os.utime(policy, ns=(initial.st_atime_ns, initial.st_mtime_ns))
        else:
            change_ctime_only(policy)

    if change_bytes:
        before = path.read_bytes()
        with pytest.raises(ValueError, match="archive or exclusions changed"):
            run(tmp_path, [metadata], incoming())
        assert path.read_bytes() == before
    else:
        result = run(tmp_path, [metadata], incoming())
        assert result["published"] and result["exclusions_sha256"] == digest


@pytest.mark.parametrize("suffix", ["-wal", "-journal", "-shm"])
def test_ctime_fallback_cannot_accept_changed_sidecars(tmp_path, suffix):
    metadata, _ = seed(tmp_path)
    path = wechat_archive_path(tmp_path)
    before = path.read_bytes()

    def incoming():
        yield record(metadata, "new", server_id="456")
        change_ctime_only(path)
        path.with_name(path.name + suffix).write_bytes(b"")

    with pytest.raises(ValueError, match="archive or exclusions changed"):
        run(tmp_path, [metadata], incoming())
    assert path.read_bytes() == before


def test_sidecar_created_during_ctime_hash_is_rejected(tmp_path, monkeypatch):
    metadata, _ = seed(tmp_path)
    path = wechat_archive_path(tmp_path)
    before = path.read_bytes()
    sha256 = restore._stream_sha256
    during_final = False

    def racing_sha256(original):
        digest = sha256(original)
        if during_final and original == path:
            path.with_name(path.name + "-wal").write_bytes(b"")
        return digest

    def incoming():
        nonlocal during_final
        yield record(metadata, "new", server_id="456")
        change_ctime_only(path)
        during_final = True

    monkeypatch.setattr(restore, "_stream_sha256", racing_sha256)
    with pytest.raises(ValueError, match="archive or exclusions changed"):
        run(tmp_path, [metadata], incoming())
    assert path.read_bytes() == before


def test_baseline_hash_must_finish_under_unchanged_identity(tmp_path, monkeypatch):
    metadata, _ = seed(tmp_path)
    path = wechat_archive_path(tmp_path)
    before = path.read_bytes()
    sha256 = restore._stream_sha256

    def drifting_sha256(original):
        digest = sha256(original)
        change_ctime_only(original)
        return digest

    monkeypatch.setattr(restore, "_stream_sha256", drifting_sha256)
    with pytest.raises(ValueError, match="while hashing original bytes"):
        run(tmp_path, [metadata], [record(metadata, "new", server_id="456")])
    assert path.read_bytes() == before
