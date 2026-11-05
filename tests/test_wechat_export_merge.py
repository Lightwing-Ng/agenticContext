"""Export publication preserves old evidence without reading existing message bodies.

Code version: v1.0.2-codex.0
"""

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

import pytest

from app.core.owner_only_files import owner_only_problem
from app.core.wechat_archive import _SCHEMA, _identifier, wechat_archive_path
from app.core import wechat_export_merge as exports


def source(identifier="html-fixture", **changes):
    result = {
        "source_id": identifier,
        "source_sha256": hashlib.sha256(identifier.encode()).hexdigest(),
        "source_file": "Synthetic text history.zip",
        "owner_evidence": {"kind": "synthetic independent owner evidence"},
    }
    result.update(changes)
    return result


def record(identifier="member-one:message-1", **changes):
    result = {
        "extraction_record_id": identifier,
        "conversation_key": "member-one",
        "peer_id": "wxid_peer",
        "title": "Synthetic conversation",
        "kind": "direct",
        "sender_id": "legacy-public-alias",
        "sender_label": "  原始昵称  ",
        "direction": "received",
        "text": "  原文\nwith whitespace <script>literal</script>  ",
        "sent_at_raw": "2026-10-30 19:20:21",
        "provenance": {"locator": {"member": "member-one.html", "local_id": "1"}},
        "flags": [],
    }
    result.update(changes)
    return result


def merge(store, metadata=None, observations=None, **options):
    return exports.merge_wechat_export(
        store,
        source=metadata or source(),
        records=iter(observations if observations is not None else [record()]),
        snapshot_id=options.pop("snapshot_id", "synthetic-snapshot"),
        **options,
    )


def rows(store, table):
    path = wechat_archive_path(store)
    with sqlite3.connect(path) as connection:
        return connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()


def old_archive(store):
    path = wechat_archive_path(store)
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(_SCHEMA)
        connection.execute(
            "INSERT INTO partitions VALUES(?,?,?,?,?,?,?,?)",
            (
                "old-source",
                "source:old-source",
                None,
                "Original account",
                "Unconfirmed",
                "PDF export",
                "Original.pdf",
                "old.gz",
            ),
        )
        connection.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "old-conversation",
                "old-source",
                "source:old-source",
                "",
                "Original title",
                "unclassified",
                1,
                0,
                0,
                "",
                "",
                "Original body",
            ),
        )
        connection.execute(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "old-message",
                "old-conversation",
                1,
                "old-id",
                "Original label",
                "unknown",
                "Original body",
                0,
                "",
                "Unknown time",
                "Unavailable source time",
                "intentionally opaque old provenance",
                "[]",
                "original body",
            ),
        )
        manifest = {
            "schema_version": 1,
            "messages": 9_470_000,
            "sources": [
                {"source_id": "old-source", "messages": 9_470_000, "conversations": 1}
            ],
            "retained_evidence": {"value": "preserve this manifest field"},
        }
        connection.execute(
            "INSERT INTO archive_info VALUES('manifest',?)", (json.dumps(manifest),)
        )
    return path


def test_first_export_preserves_exact_text_labels_identifiers_and_unknown_clock(
    tmp_path,
):
    observation = record()
    result = merge(tmp_path, observations=[observation], expected_records=1)
    assert result["inserted_messages"] == result["inserted_conversations"] == 1
    assert result["old_message_rows_read"] == 0
    assert result["unique_original_message_count"] is None
    message = rows(tmp_path, "messages")[0]
    assert message[3:7] == (
        observation["sender_id"],
        observation["sender_label"],
        observation["direction"],
        observation["text"],
    )
    assert message[8] == "2026-10-30T19:20:21"
    assert message[10] == "Source clock; timezone unknown"
    assert json.loads(message[11])["source_time"] == observation["sent_at_raw"]
    partition = rows(tmp_path, "partitions")[0]
    assert partition[1] == "source:html-fixture" and partition[2] is None
    assert partition[5] == "HTML export"
    assert owner_only_problem(wechat_archive_path(tmp_path)) == ""
    if os.name == "posix":
        assert wechat_archive_path(tmp_path).stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(
    sys.platform != "darwin", reason="Existing archives require macOS clonefile."
)
def test_additive_export_does_not_read_or_replace_old_evidence(tmp_path, monkeypatch):
    path = old_archive(tmp_path)
    before = {
        table: rows(tmp_path, table)
        for table in ("partitions", "conversations", "messages")
    }
    statements = []
    connect = sqlite3.connect

    def tracing_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(exports.sqlite3, "connect", tracing_connect)
    observation = record(
        crossrefs=[{"candidate_message_id": "old-message", "status": "candidate only"}]
    )
    result = merge(tmp_path, observations=[observation], expected_records=1)
    publication_statements = list(statements)
    assert result["messages_before"] == 9_470_000
    assert result["messages_after"] == 9_470_001
    assert result["old_partition_rows_verified"] == 1
    for table, old_rows in before.items():
        assert all(old in rows(tmp_path, table) for old in old_rows)
    assert result["manifest_after"]["retained_evidence"] == {
        "value": "preserve this manifest field"
    }
    message_reads = [
        sql
        for sql in publication_statements
        if sql.lstrip().upper().startswith("SELECT") and "FROM messages" in sql
    ]
    expected_id = "wechat:" + _identifier("html-fixture", "member-one")
    assert message_reads and all(
        "WHERE conversation_id=" in sql and expected_id in sql for sql in message_reads
    )
    assert not any(
        "quick_check" in sql.lower() or "integrity_check" in sql.lower()
        for sql in publication_statements
    )
    evidence = rows(tmp_path, "message_evidence")
    assert len(evidence) == result["new_evidence"] == 1
    assert evidence[0][1] != "old-message"
    assert (
        json.loads(evidence[0][5])["cross_reference"]["candidate_message_id"]
        == "old-message"
    )
    assert owner_only_problem(path) == ""
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600


def test_exact_replay_does_not_consume_input_or_republish_database(tmp_path):
    merge(tmp_path)
    path = wechat_archive_path(tmp_path)
    before = exports._database_identity(path)

    def forbidden_records():
        raise AssertionError(
            "Replay must use source/SHA receipt instead of consuming messages."
        )
        yield

    result = exports.merge_wechat_export(
        tmp_path,
        source=source(),
        records=forbidden_records(),
        snapshot_id="synthetic-snapshot",
        expected_records=1,
    )
    assert result["replayed"] and not result["changed"]
    assert (
        result["inserted_messages"]
        == result["new_evidence"]
        == result["input_records"]
        == 0
    )
    assert exports._database_identity(path) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"source_sha256": "a" * 64},
        {"source_file": "Different evidence.zip"},
    ],
)
def test_same_source_cannot_change_its_receipt_evidence(tmp_path, changes):
    merge(tmp_path)
    before = wechat_archive_path(tmp_path).read_bytes()
    with pytest.raises(ValueError, match="different snapshot evidence"):
        merge(tmp_path, metadata=source(**changes))
    assert wechat_archive_path(tmp_path).read_bytes() == before


def test_verified_owner_is_explicit_and_never_claims_native_backup_identity(tmp_path):
    metadata = source(owner_verified=True, source_account_id="wxid_owner")
    merge(tmp_path, metadata=metadata)
    partition = rows(tmp_path, "partitions")[0]
    assert partition[1:3] == ("account:wxid_owner", "wxid_owner")
    assert partition[4] == "Corroborated export ID"
    snapshot = rows(tmp_path, "source_snapshots")[0]
    stored = json.loads(snapshot[3])
    assert stored["owner_evidence"] == metadata["owner_evidence"]
    assert stored["source_sha256"] == metadata["source_sha256"]
    assert rows(tmp_path, "messages")[0][3] == record()["sender_id"]


@pytest.mark.parametrize(
    "changes",
    [
        {
            "owner_verified": True,
            "source_account_id": "wxid_owner",
            "owner_evidence": {},
        },
        {"owner_verified": True, "source_account_id": ""},
        {"source_id": "ios-unconfirmed-" + "a" * 32},
        {"source_sha256": "invalid"},
    ],
)
def test_invalid_source_fails_without_creating_archive(tmp_path, changes):
    with pytest.raises(ValueError):
        merge(tmp_path, metadata=source(**changes))
    assert not wechat_archive_path(tmp_path).exists()


@pytest.mark.skipif(
    sys.platform != "darwin", reason="Existing archives require macOS clonefile."
)
def test_same_body_names_and_local_ids_stay_separate_across_export_sources(tmp_path):
    merge(tmp_path, metadata=source("html-one"))
    merge(tmp_path, metadata=source("html-two"))
    messages = rows(tmp_path, "messages")
    assert len(messages) == 2
    assert len({row[0] for row in messages}) == len({row[1] for row in messages}) == 2
    assert len({row[6] for row in messages}) == 1


@pytest.mark.skipif(
    sys.platform != "darwin", reason="Existing archives require macOS clonefile."
)
@pytest.mark.parametrize("failure", ["count", "stream", "duplicate", "owner"])
def test_invalid_or_shorter_input_preserves_complete_previous_archive(
    tmp_path, failure
):
    path = old_archive(tmp_path)
    before = path.read_bytes()
    metadata = source()
    observations = [record()]
    expected = 1
    if failure == "count":
        expected = 2
    elif failure == "stream":

        def interrupted():
            yield record()
            raise ValueError("Synthetic interrupted parser")

        observations = interrupted()
    elif failure == "duplicate":
        observations = [record(), record()]
        expected = 2
    else:
        metadata = source(owner_verified=True, source_account_id="wxid_owner")
        observations = [record(source_account_id="wxid_other")]
    with pytest.raises((ValueError, sqlite3.IntegrityError)):
        merge(
            tmp_path,
            metadata=metadata,
            observations=observations,
            expected_records=expected,
        )
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".wechat-export-*"))


@pytest.mark.skipif(
    sys.platform != "darwin", reason="Existing archives require macOS clonefile."
)
@pytest.mark.parametrize("changed", ["archive", "policy"])
def test_concurrent_archive_or_policy_change_prevents_publication(tmp_path, changed):
    path = old_archive(tmp_path)
    before = path.read_bytes()

    def observations():
        yield record()
        if changed == "archive":
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "INSERT INTO archive_info VALUES('external-change','retained')"
                )
        else:
            path.with_name("exclusions.json").write_text(
                '{"schema_version":1,"sources":[]}', encoding="utf-8"
            )

    with pytest.raises(ValueError, match="changed during export publication"):
        merge(tmp_path, observations=observations())
    assert len(rows(tmp_path, "partitions")) == 1
    assert rows(tmp_path, "messages")[0][0] == "old-message"
    if changed == "policy":
        assert path.read_bytes() == before


def test_clonefile_unavailable_has_no_full_copy_fallback(tmp_path, monkeypatch):
    path = old_archive(tmp_path)
    before = path.read_bytes()
    monkeypatch.setattr(exports.sys, "platform", "unsupported")
    with pytest.raises(ValueError, match="APFS clonefile is required"):
        merge(tmp_path)
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".wechat-export-*"))


def test_guard_rejects_old_message_reads_and_protected_updates_and_deletes(tmp_path):
    path = old_archive(tmp_path)
    with sqlite3.connect(path) as connection:
        exports._install_guard(connection)
        for sql in (
            "SELECT * FROM messages",
            "SELECT COUNT(*) FROM messages",
            "UPDATE partitions SET account_label='replacement' WHERE id='old-source'",
            "DELETE FROM conversations WHERE id='old-conversation'",
            "DELETE FROM messages WHERE id='old-message'",
        ):
            with pytest.raises(sqlite3.DatabaseError):
                connection.execute(sql)


def test_no_source_owner_is_inferred_from_unverified_metadata(tmp_path):
    merge(
        tmp_path,
        metadata=source(source_account_id="wxid_unconfirmed", owner_verified=False),
    )
    partition = rows(tmp_path, "partitions")[0]
    assert partition[2] is None and partition[1] == "source:html-fixture"


def test_raw_export_provenance_is_retained_when_standard_pointers_are_added(tmp_path):
    observation = record(
        provenance={
            "record_id": "export-local-id",
            "source_snapshot": "original-value",
            "source_time": "original-clock",
        }
    )
    merge(tmp_path, observations=[observation])
    provenance = json.loads(rows(tmp_path, "messages")[0][11])
    assert provenance["raw_export_provenance"] == observation["provenance"]
    assert provenance["record_id"] == observation["extraction_record_id"]
    assert type(provenance["source_snapshot"]) is int


def test_fingerprint_verification_detects_new_message_tampering(tmp_path, monkeypatch):
    verify = exports._verify_new_messages

    def changed_before_verification(
        connection, scope, conversations, count, fingerprint
    ):
        return verify(
            connection,
            scope,
            conversations,
            count,
            (fingerprint + 1) % exports._MODULUS,
        )

    monkeypatch.setattr(exports, "_verify_new_messages", changed_before_verification)
    with pytest.raises(ValueError, match="content fingerprints differ"):
        merge(tmp_path)
    assert not wechat_archive_path(tmp_path).exists()


def test_initial_archive_publication_syncs_a_writable_candidate(tmp_path, monkeypatch):
    modes = {}
    native_open, native_fsync = Path.open, exports.os.fsync

    def opened(path, mode="r", *args, **kwargs):
        handle = native_open(path, mode, *args, **kwargs)
        modes[handle.fileno()] = mode
        return handle

    def windows_fsync(descriptor):
        if modes.get(descriptor) == "rb":
            raise OSError(9, "A read-only descriptor cannot be committed on Windows.")
        return native_fsync(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", opened)
        patch.setattr(exports.os, "fsync", windows_fsync)
        result = merge(tmp_path)

    assert result["changed"]
    assert result["inserted_messages"] == 1
    assert any(mode == "r+b" for mode in modes.values())


@pytest.mark.parametrize("existing", [False, True])
def test_private_boundary_failure_prevents_any_export_publication(
    tmp_path, monkeypatch, existing,
):
    from shutil import copyfile

    from app.core.owner_only_files import OwnerOnlyFileError

    # This synthetic copy isolates private publication from the separately tested
    # APFS-only production staging contract.
    monkeypatch.setattr(exports, "_clonefile", copyfile)
    path = old_archive(tmp_path) if existing else wechat_archive_path(tmp_path)
    original = path.read_bytes() if existing else None

    def denied(_path):
        raise OwnerOnlyFileError("Synthetic private boundary failure")

    monkeypatch.setattr(exports, "ensure_owner_only", denied)
    with pytest.raises(OwnerOnlyFileError, match="Synthetic private boundary failure"):
        merge(tmp_path)
    if existing:
        assert path.read_bytes() == original
    else:
        assert not path.exists()
    assert not list(path.parent.glob(".wechat-export-*"))
